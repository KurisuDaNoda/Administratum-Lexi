"""Import all PDFs in Data/Documents, preserving known provenance and reporting each file."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from psycopg.rows import dict_row

from scripts.import_data import ROOT,load_env
from scripts.import_pdf import main as import_pdf,embed_version
from lexi import documents,storage


def file_metadata(path,folder,manifest):
    relative=path.relative_to(folder).as_posix()
    supplied=manifest.get(relative,{})
    if not isinstance(supplied,dict):
        raise ValueError(f'Metadata for {relative} must be an object')
    allowed={'title','document_id','edition','edition_evidence','revision','published_date','source_url','kind','extractor'}
    unknown=set(supplied)-allowed
    if unknown:
        raise ValueError(f'Unknown metadata fields for {relative}: {sorted(unknown)}')
    slug=re.sub('[^a-z0-9]+','-',str(Path(relative).with_suffix('')).lower()).strip('-') or 'document'
    if len(slug)>100:
        slug=slug[:87]+'-'+hashlib.sha256(relative.encode()).hexdigest()[:12]
    defaults={'document_id':slug,'title':path.stem.replace('-',' ').replace('_',' '),
        'edition':0,'edition_evidence':'Unverified local PDF; identify edition before review/activation.',
        'revision':'Unspecified local revision','kind':'rules','extractor':'layout'}
    return relative,supplied,defaults


def import_folder(folder,manifest,embed=False,dry_run=False):
    if not folder.is_dir():
        raise ValueError(f'Documents folder not found: {folder}')
    files=sorted((p for p in folder.rglob('*') if p.is_file() and p.suffix.lower()=='.pdf'),key=lambda p:str(p).lower())
    if not files:
        raise ValueError(f'No PDF files found in {folder}')
    # Validate the entire plan before writing. Sidecars and non-PDFs are ignored.
    plans=[]
    for path in files:
        relative,supplied,defaults=file_metadata(path,folder,manifest)
        checksum=hashlib.sha256(path.read_bytes()).hexdigest()
        plans.append((path,relative,supplied,defaults,checksum))
    if not dry_run:
        documents.initialize()
    report={'folder':str(folder),'dry_run':dry_run,'files':[],'errors':0}
    for path,relative,supplied,defaults,checksum in plans:
        entry={'file':relative,'file_sha256':checksum}
        try:
            known=None
            # Identical bytes can reuse the original version, source URL, and
            # review state. Explicit metadata takes precedence over deduplication.
            if not supplied:
                with storage.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
                    if conn.execute("SELECT to_regclass('document_versions')").fetchone()[0]:
                        known=cur.execute('''SELECT v.*,d.title,d.kind FROM document_versions v
                            JOIN documents d USING(document_id) WHERE file_sha256=%s AND pipeline IN (%s,%s)
                            ORDER BY is_active DESC, imported_at DESC LIMIT 1''',
                            (checksum,documents.PIPELINE,documents.NUMBERED_PIPELINE)).fetchone()
            metadata={**defaults,**supplied}
            entry.update(title=known['title'] if known else metadata['title'],
                edition=known['edition'] if known else metadata['edition'],
                status='would_reuse' if dry_run and known else 'would_import' if dry_run else 'pending')
            if dry_run:
                if known:
                    entry['version_id']=known['version_id']
                report['files'].append(entry)
                continue
            if known:
                if embed:
                    embed_version(known['version_id'])
                entry.update(status='reused',version_id=known['version_id'],
                    is_active=known['is_active'],extraction_reviewed=known['extraction_reviewed'],
                    viewer_url=documents.viewer_url(known['version_id'],1))
            else:
                argv=['--file',str(path),'--title',metadata['title'],'--document-id',metadata['document_id'],
                    '--edition',str(metadata['edition']),'--edition-evidence',metadata['edition_evidence'],
                    '--revision',metadata['revision'],'--kind',metadata['kind'],'--extractor',metadata['extractor']]
                for field in ('source_url','published_date'):
                    if metadata.get(field):
                        argv.extend(['--'+field.replace('_','-'),str(metadata[field])])
                if embed:
                    argv.append('--embed')
                result=import_pdf(argv)
                entry.update(status='imported',**result)
                entry.update(is_active=False,extraction_reviewed=False)
            report['files'].append(entry)
        except (Exception,SystemExit) as exc:
            # Continue with remaining files, preserving completed work and avoiding
            # printing driver/API error bodies containing credentials.
            entry.update(status='failed',error_type=type(exc).__name__)
            if isinstance(exc,(ValueError,FileNotFoundError)):
                entry['error']=str(exc)
            report['files'].append(entry);report['errors']+=1
        print(f"{relative}: {entry['status']}",flush=True)
    return report


def main(argv=None):
    load_env(ROOT/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path,default=ROOT/'Data'/'Documents')
    parser.add_argument('--manifest',type=Path,help='JSON mapping relative PDF paths to publication metadata')
    parser.add_argument('--embed',action='store_true',help='Generate/resume chunk embeddings; does not activate documents')
    parser.add_argument('--dry-run',action='store_true',help='List planned imports; reads existing DB but makes no writes or API calls')
    parser.add_argument('--report',type=Path,default=ROOT/'generated'/'document-folder-import.json')
    args=parser.parse_args(argv)
    metadata=args.manifest or args.folder/'manifest.json'
    if args.manifest and not metadata.is_file():
        parser.error('Manifest file not found')
    manifest=json.loads(metadata.read_text(encoding='utf-8-sig')) if metadata.exists() else {}
    if not isinstance(manifest,dict):
        parser.error('Manifest must map relative PDF filenames to metadata objects')
    report=import_folder(args.folder,manifest,embed=args.embed,dry_run=args.dry_run)
    if not args.dry_run:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))
    return report


if __name__=='__main__':
    try:
        result=main()
        sys.exit(1 if result['errors'] else 0)
    except Exception as exc:
        print(f'Document folder import failed ({type(exc).__name__}). Check paths, metadata and service configuration.',file=sys.stderr)
        sys.exit(1)
