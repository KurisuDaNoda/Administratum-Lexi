"""Download/store an immutable PDF, extract page-aware rules, and optionally embed/activate."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

import pymupdf
import pymupdf4llm
import requests
import tiktoken
from psycopg.types.json import Jsonb
from minio.versioningconfig import VersioningConfig, ENABLED

from scripts.import_data import ROOT, load_env
from lexi import documents, storage


def download(url):
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password:
        raise ValueError('Provide an HTTP(S) source URL without credentials')
    data=bytearray()
    with requests.get(url,stream=True,timeout=(15,90)) as response:
        response.raise_for_status()
        for block in response.iter_content(128*1024):
            data.extend(block)
            if len(data)>100*1024*1024:
                raise ValueError('PDF exceeds the 100 MiB import limit')
    if not data.startswith(b'%PDF-'):
        raise ValueError('The URL did not return a PDF')
    return bytes(data)


def extract(data):
    with pymupdf.open(stream=data,filetype='pdf') as pdf:
        if pdf.needs_pass:
            raise ValueError('Encrypted PDF requires a decrypted copy')
        # No network model/OCR service is used. Scanned/empty pages are reported.
        pages=pymupdf4llm.to_markdown(pdf,page_chunks=True,show_progress=False)
        if len(pages)!=len(pdf):
            raise ValueError('Extraction did not preserve page boundaries')
        return [{'page':i+1,'printed_label':pdf[i].get_label() or None,'text':page['text']}
                for i,page in enumerate(pages)]


def make_sections(pages):
    if pages and all('numbered_rules' in page for page in pages):
        from scripts.pdf_rules import numbered_sections
        return numbered_sections(pages)
    result=[]
    path=[]  # (Markdown heading level, heading), rather than assuming no level gaps.
    current={'heading_path':'Document opening','spans':[]}
    for page in pages:
        buffer=[]
        def flush():
            if buffer:
                current['spans'].append({'page':page['page'],'printed_label':page['printed_label'], 'text':'\n'.join(buffer)})
                buffer.clear()
        for line in page['text'].splitlines():
            heading=re.match(r'^(#{1,6})\s+(.+?)\s*#*$',line)
            if heading:
                flush()
                if current['spans']:
                    result.append(current)
                level=len(heading[1])
                while path and path[-1][0]>=level:
                    path.pop()
                path.append((level,heading[2]))
                current={'heading_path':' > '.join(item[1] for item in path),'spans':[]}
            buffer.append(line)
        flush()
    if current['spans']:
        result.append(current)
    return result


def chunk_section(section,encoding,target=1400):
    # Preserve paragraph/table/FAQ blocks whenever possible. The full parent
    # section is always retrievable to recover cross-chunk exceptions.
    units=[]
    for span in section['spans']:
        for paragraph in re.split(r'\n\s*\n',span['text']):
            if not paragraph.strip():
                continue
            tokens=encoding.encode(paragraph)
            if len(tokens)<=7000:
                units.append((paragraph,span['page'],span['printed_label']))
            else:
                # Rare giant block: overlap windows rather than truncate it.
                for start in range(0,len(tokens),6200):
                    text=encoding.decode(tokens[start:start+6400])
                    units.append((text,span['page'],span['printed_label']))
    chunks=[];pending=[];count=0
    def emit():
        if pending:
            chunks.append({'body':'\n\n'.join(u[0] for u in pending),
                'page_start':pending[0][1], 'page_end':pending[-1][1],
                'printed_page_label':pending[0][2]})
    for unit in units:
        size=len(encoding.encode(unit[0]))
        if pending and count+size>target:
            emit();pending=[];count=0
        pending.append(unit);count+=size
    emit()
    return chunks


def embed_version(version):
    from openai import OpenAI
    if not os.getenv('OPENAI_API_KEY'):
        raise ValueError('OPENAI_API_KEY is required for --embed')
    client=OpenAI(max_retries=4,timeout=60)
    config=documents.EMBEDDING_CONFIG
    with storage.connect() as conn:
        rows=conn.execute('''SELECT c.chunk_id,c.body,s.heading_path,d.title FROM document_chunks c
            JOIN document_sections s USING(section_id) JOIN document_versions v ON v.version_id=c.version_id
            JOIN documents d USING(document_id) WHERE c.version_id=%s
            AND (c.embedding IS NULL OR c.embedding_config_id IS DISTINCT FROM %s) ORDER BY c.ordinal''',(version,config)).fetchall()
    saved=0
    for offset in range(0,len(rows),12):
        batch=rows[offset:offset+12]
        texts=[f'Document: {r[3]}\nSection: {r[2]}\n\n{r[1]}' for r in batch]
        response=client.embeddings.create(model=documents.MODEL,dimensions=1536,input=texts,encoding_format='float')
        values=sorted(response.data,key=lambda x:x.index)
        if [v.index for v in values]!=list(range(len(batch))):
            raise ValueError('Embedding response mismatch')
        with storage.connect() as conn:
            for row,value in zip(batch,values):
                from scripts.embed_data import combine_vectors
                vector=combine_vectors([value.embedding],[1],1536)
                conn.execute('''UPDATE document_chunks SET embedding=%s::vector,embedding_model=%s,
                    embedding_config_id=%s,embedded_at=now() WHERE chunk_id=%s''',
                    (json.dumps(vector),documents.MODEL,config,row[0]))
        saved+=len(batch)
        print(f'Embedded {saved}/{len(rows)} chunks',flush=True)


def main(argv=None):
    load_env(ROOT/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--url')
    source.add_argument('--file',type=Path)
    parser.add_argument('--source-url',help='Original publication URL when importing a local file')
    parser.add_argument('--title',required=True)
    parser.add_argument('--document-id',required=True,help='Stable logical ID, e.g. core-rules')
    parser.add_argument('--kind',default='rules')
    parser.add_argument('--edition',type=int,required=True)
    parser.add_argument('--edition-evidence',required=True,help='Verified edition evidence, not just file download date')
    parser.add_argument('--revision',required=True)
    parser.add_argument('--published-date')
    parser.add_argument('--reviewed',action='store_true',help='Extraction and edition have been manually reviewed')
    parser.add_argument('--activate',action='store_true',help='Make version eligible for Lexi retrieval; requires --reviewed')
    parser.add_argument('--embed',action='store_true')
    parser.add_argument('--sections-json',type=Path,help='Generated PDF sidecar containing original rule text and verified page mapping')
    parser.add_argument('--extractor',choices=['layout','numbered'],default='layout',
        help='numbered: native numbered-rule extraction with vector dice recovery for digital Core Rules')
    args=parser.parse_args(argv)
    if args.activate and not args.reviewed:
        parser.error('--activate requires --reviewed after examining extraction')
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,99}',args.document_id):
        parser.error('Use a lowercase slug for --document-id')
    if args.source_url and urlsplit(args.source_url).scheme not in ('http','https'):
        parser.error('--source-url must be HTTP(S)')
    data=download(args.url) if args.url else args.file.read_bytes()
    checksum=hashlib.sha256(data).hexdigest()
    pipeline=documents.NUMBERED_PIPELINE if args.extractor=='numbered' else documents.PIPELINE
    sidecar=None
    if args.sections_json:
        sidecar=json.loads(args.sections_json.read_text(encoding='utf-8'))
        if sidecar['pdf_sha256']!=checksum:
            raise ValueError('Sidecar does not match PDF checksum')
        pipeline='generated-rule-sections-v1:'+hashlib.sha256(args.sections_json.read_bytes()).hexdigest()
    metadata={'file':checksum,'edition':args.edition,'revision':args.revision,
        'document_id':args.document_id,'pipeline':pipeline,
        'source_url':args.url or args.source_url,'edition_evidence':args.edition_evidence,
        'title':args.title,'published_date':args.published_date}
    version=hashlib.sha256(json.dumps(metadata,sort_keys=True).encode()).hexdigest()
    documents.initialize()
    with storage.connect() as conn:
        existing=documents.version_record(conn,version)
    if not existing:
        if sidecar:
            pages=sidecar['pages']
        elif args.extractor=='numbered':
            from scripts.pdf_rules import extract_numbered
            pages=extract_numbered(data)
        else:
            pages=extract(data)
        sections=sidecar['sections'] if sidecar else make_sections(pages)
        encoding=tiktoken.get_encoding('cl100k_base')
        if not sections or not any(p['text'].strip() for p in pages):
            raise ValueError('No extractable text: OCR or another extraction method is required')
        output=ROOT/'generated'/'documents'/version
        output.mkdir(parents=True,exist_ok=True)
        (output/'pages.json').write_text(json.dumps(pages,ensure_ascii=False,indent=2),encoding='utf-8')
        (output/'extracted.md').write_text('\n\n'.join(f'<!-- PDF page {p["page"]} -->\n'+p['text'] for p in pages),encoding='utf-8')
        quality={'status':'needs_review','replacement_character_pages':[p['page'] for p in pages if '\ufffd' in p['text']],
            'strikethrough_pages':[p['page'] for p in pages if '~~' in p['text']],
            'short_or_empty_pages':[p['page'] for p in pages if len(p['text'].strip())<40],
            'note':'Inspect original pages against extracted text, especially numerical tables and symbols, before activation.'}
        quality['unplaced_dice_pages']=[p['page'] for p in pages if p.get('extraction_metadata',{}).get('unplaced_dice')]
        (output/'quality-report.json').write_text(json.dumps(quality,indent=2),encoding='utf-8')
        bucket=os.getenv('MINIO_BUCKET','lexi-documents');store=documents.object_store()
        if not store.bucket_exists(bucket):
            store.make_bucket(bucket)
        store.set_bucket_versioning(bucket,VersioningConfig(ENABLED))
        key=f'{args.document_id}/{version}/original.pdf'
        uploaded=store.put_object(bucket,key,io.BytesIO(data),len(data),content_type='application/pdf')
        with storage.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(400003)')
            conn.execute('INSERT INTO documents(document_id,title,kind) VALUES(%s,%s,%s) ON CONFLICT(document_id) DO NOTHING',
                (args.document_id,args.title,args.kind))
            conn.execute('''INSERT INTO document_versions(version_id,document_id,file_sha256,edition,revision,published_date,
                source_url,edition_evidence,bucket,object_key,object_version_id,page_count,pipeline)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (version,args.document_id,checksum,args.edition,args.revision,args.published_date,
                 args.url or args.source_url,args.edition_evidence,bucket,key,uploaded.version_id,len(pages),pipeline))
            for page in pages:
                conn.execute('''INSERT INTO document_pages(version_id,page_number,printed_page_label,body,extraction_metadata)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (version,page['page'],page['printed_label'],page['text'],Jsonb(page.get('extraction_metadata',{}))))
            ordinal=0
            for i,section in enumerate(sections):
                section_id=hashlib.sha256(f'{version}:section:{i}'.encode()).hexdigest()
                spans=section['spans']
                conn.execute('''INSERT INTO document_sections VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (section_id,version,i,section['heading_path'],'\n\n'.join(s['text'] for s in spans),section.get('page_start',spans[0]['page']),section.get('page_end',spans[-1]['page']),Jsonb(spans)))
                for chunk in chunk_section(section,encoding):
                    if sidecar:
                        # Exact original text, conservative range for the complete rule.
                        chunk['page_start']=section['page_start']
                        chunk['page_end']=section['page_end']
                    ident=hashlib.sha256(f'{version}:chunk:{ordinal}'.encode()).hexdigest()
                    conn.execute('''INSERT INTO document_chunks(chunk_id,section_id,version_id,ordinal,body,page_start,page_end,printed_page_label)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                        (ident,section_id,version,ordinal,chunk['body'],chunk['page_start'],chunk['page_end'],chunk['printed_page_label']))
                    ordinal+=1
        print(json.dumps({'version_id':version,'pdf_pages':len(pages),'sections':len(sections),'chunks':ordinal,
            'short_or_empty_pages':[p['page'] for p in pages if len(p['text'].strip())<40],
            'review_extraction_at':str(output)},indent=2),flush=True)
    if args.embed:
        embed_version(version)
    if args.reviewed or args.activate:
        quality_path=ROOT/'generated'/'documents'/version/'quality-report.json'
        if args.activate and quality_path.exists():
            quality=json.loads(quality_path.read_text(encoding='utf-8'))
            if quality.get('replacement_character_pages') or quality.get('unplaced_dice_pages'):
                raise ValueError('Unresolved extraction symbols/dice remain; correct the extraction before activation')
        with storage.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(400003)')
            conn.execute('UPDATE document_versions SET extraction_reviewed=true WHERE version_id=%s',(version,))
            if args.activate:
                conn.execute('UPDATE document_versions SET is_active=false WHERE document_id=%s AND edition=%s',(args.document_id,args.edition))
                conn.execute('UPDATE document_versions SET is_active=true WHERE version_id=%s',(version,))
    result={'version_id':version,'activated_this_run':args.activate,'viewer_url':documents.viewer_url(version,1)}
    print(json.dumps(result,indent=2))
    return result


if __name__=='__main__':
    main()
