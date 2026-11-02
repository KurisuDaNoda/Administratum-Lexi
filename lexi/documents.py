"""Immutable PDF versions, page-aware evidence and signed source links."""
import hashlib
import hmac
import os
from urllib.parse import urlencode, urlsplit, urlunsplit

from minio import Minio

from . import storage

MODEL = 'text-embedding-3-small'
DIMENSIONS = 1536
PIPELINE = 'pdf-markdown-sections-v1'
NUMBERED_PIPELINE = 'pdf-native-numbered-rules-v3'
EMBEDDING_CONFIG = 'pdf-rule-text-v2:'+MODEL+':1536'


class CitedAnswer(str):
    def __new__(cls, text, sources):
        answer=super().__new__(cls,text)
        answer.page_sources=[]
        seen=set()
        for source in sources:
            url=source.get('viewer_url')
            if url and url not in seen and any(source.get(k) and source[k] in text for k in ('source_url','viewer_url')):
                answer.page_sources.append(source);seen.add(url)
        return answer


def object_store():
    endpoint = os.getenv('MINIO_ENDPOINT','127.0.0.1:19000')
    return Minio(endpoint, access_key=os.environ['MINIO_ACCESS_KEY'],
        secret_key=os.environ['MINIO_SECRET_KEY'], secure=os.getenv('MINIO_SECURE','false').lower()=='true')


def initialize():
    with storage.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS documents (
            document_id text PRIMARY KEY, title text NOT NULL, kind text NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS document_versions (
            version_id text PRIMARY KEY, document_id text NOT NULL REFERENCES documents,
            file_sha256 text NOT NULL, edition integer NOT NULL, revision text NOT NULL,
            published_date date, source_url text, edition_evidence text NOT NULL,
            bucket text NOT NULL, object_key text NOT NULL, object_version_id text,
            page_count integer NOT NULL CHECK(page_count>0), pipeline text NOT NULL,
            is_active boolean NOT NULL DEFAULT false,
            extraction_reviewed boolean NOT NULL DEFAULT false,
            imported_at timestamptz NOT NULL DEFAULT now())''')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS document_one_active_version
            ON document_versions(document_id,edition) WHERE is_active''')
        conn.execute('''CREATE TABLE IF NOT EXISTS document_sections (
            section_id text PRIMARY KEY, version_id text NOT NULL REFERENCES document_versions ON DELETE CASCADE,
            ordinal integer NOT NULL, heading_path text NOT NULL, body text NOT NULL,
            page_start integer NOT NULL, page_end integer NOT NULL,
            page_spans jsonb NOT NULL, UNIQUE(version_id,ordinal))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS document_chunks (
            chunk_id text PRIMARY KEY, section_id text NOT NULL REFERENCES document_sections ON DELETE CASCADE,
            version_id text NOT NULL REFERENCES document_versions ON DELETE CASCADE,
            ordinal integer NOT NULL, body text NOT NULL, page_start integer NOT NULL,
            page_end integer NOT NULL, printed_page_label text,
            embedding vector, embedding_model text, embedding_config_id text,
            embedded_at timestamptz,
            search_text tsvector GENERATED ALWAYS AS (to_tsvector('english',body)) STORED,
            UNIQUE(version_id,ordinal))''')
        conn.execute('CREATE INDEX IF NOT EXISTS document_chunks_text_idx ON document_chunks USING gin(search_text)')
        conn.execute('CREATE INDEX IF NOT EXISTS document_chunks_section_idx ON document_chunks(section_id,ordinal)')
        conn.execute('''CREATE TABLE IF NOT EXISTS document_pages (
            version_id text NOT NULL REFERENCES document_versions ON DELETE CASCADE,
            page_number integer NOT NULL, printed_page_label text, body text NOT NULL,
            extraction_metadata jsonb NOT NULL, PRIMARY KEY(version_id,page_number))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS document_relations (
            from_version_id text REFERENCES document_versions, to_version_id text REFERENCES document_versions,
            relation text NOT NULL, notes text, PRIMARY KEY(from_version_id,to_version_id,relation))''')


def signing_token(version):
    key = os.environ.get('VIEWER_SIGNING_KEY','')
    if len(key)<32:
        raise ValueError('VIEWER_SIGNING_KEY must contain at least 32 characters')
    return hmac.new(key.encode(),('pdf:'+version).encode(),hashlib.sha256).hexdigest()


def viewer_url(version, page):
    return os.getenv('VIEWER_PUBLIC_BASE_URL','http://localhost:8081').rstrip('/') + '/view/' + version + '?' + urlencode({'page':page,'token':signing_token(version)})


def pdf_source_url(url, page):
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.path.lower().endswith('.pdf'):
        return url  # Generated reading copies cite the HTML source without fictitious PDF fragments.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, 'page='+str(page)))


def evidence(row):
    # Rows always come from a version-filtered query; physical page numbers are 1-based.
    result = dict(row)
    result['source_url'] = pdf_source_url(row.get('source_url'),row['page_start'])
    result['viewer_url'] = viewer_url(row['version_id'],row['page_start'])
    result['page_number_kind'] = '1-based physical PDF page; printed labels may differ'
    result['evidence_scope'] = 'This version only. Check applicable amendments separately.'
    return result


def version_record(conn, version):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute('''SELECT v.*, d.title, d.kind FROM document_versions v
            JOIN documents d USING(document_id) WHERE version_id=%s''',(version,)).fetchone()
