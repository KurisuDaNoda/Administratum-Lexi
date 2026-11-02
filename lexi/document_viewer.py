"""Local source viewer: exact stored PDF pages, plus the complete original PDF."""
import hmac
import html
import io
import re
import threading

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse
import pymupdf

from scripts.import_data import ROOT, load_env
from . import documents, storage

load_env(ROOT/'.env')
app = FastAPI(title='Lexi source viewer', docs_url=None, redoc_url=None, openapi_url=None)
RENDER_LOCK=threading.Lock()


def authorize(version, token):
    if not re.fullmatch('[0-9a-f]{64}',version) or not hmac.compare_digest(documents.signing_token(version),token):
        raise HTTPException(403,'Invalid document link')
    with storage.connect() as conn:
        record=documents.version_record(conn,version)
    if not record:
        raise HTTPException(404,'Document not found')
    return record


def original(record):
    return documents.object_store().get_object(record['bucket'],record['object_key'],version_id=record['object_version_id'])


@app.get('/health')
def health():
    return {'status':'ok'}


@app.get('/view/{version}',response_class=HTMLResponse)
def view(version: str, token: str=Query(...), page: int=Query(1,ge=1)):
    record=authorize(version,token)
    if page>record['page_count']:
        raise HTTPException(404,'Page not found')
    esc=lambda value: html.escape(str(value),quote=True)
    def link(number):
        return f'/view/{version}?page={number}&amp;token={esc(token)}'
    source=f'<a href="{esc(record["source_url"])}" rel="noreferrer">Official source</a>' if record['source_url'] else ''
    previous=f'<a href="{link(page-1)}">← Previous</a>' if page>1 else '<span>← Previous</span>'
    following=f'<a href="{link(page+1)}">Next →</a>' if page<record['page_count'] else '<span>Next →</span>'
    content=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{esc(record['title'])} — PDF page {page}</title>
    <style>body{{margin:0;background:#23212a;color:#eee;font:16px system-ui}}header{{padding:16px;background:#302c3a;position:sticky;top:0}}h1{{font-size:19px;margin:0 0 8px}}nav{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}a{{color:#e4beef}}input{{width:65px}}img{{display:block;max-width:100%;height:auto;margin:auto}}small{{display:block;margin:10px 0 0;color:#ccc}}main{{max-width:1200px;margin:auto}}form{{display:flex;gap:6px}}</style>
    <header><h1>{esc(record['title'])}</h1><nav>{previous}<strong>PDF page {page} / {record['page_count']}</strong>{following}
    <form action="/view/{version}"><input type="hidden" name="token" value="{esc(token)}"><input aria-label="PDF page" type="number" name="page" min="1" max="{record['page_count']}" value="{page}"><button>Go</button></form>
    <a href="/pdf/{version}?token={esc(token)}#page={page}">Open complete PDF</a>{source}</nav>
    <small>Edition {record['edition']} · {esc(record['revision'])} · Physical PDF pages may differ from printed page labels. This is the stored version, not necessarily the latest rules.</small></header>
    <main><img alt="PDF page {page}" src="/page/{version}/{page}.png?token={esc(token)}"></main></html>'''
    return HTMLResponse(content,headers={'Referrer-Policy':'no-referrer',
        'Content-Security-Policy':"default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",'Cache-Control':'no-store'})


@app.get('/page/{version}/{page}.png')
def page_image(version: str,page: int,token: str=Query(...)):
    record=authorize(version,token)
    if not 1<=page<=record['page_count']:
        raise HTTPException(404,'Page not found')
    response=original(record)
    try:
        data=response.read()
    finally:
        response.close();response.release_conn()
    with RENDER_LOCK, pymupdf.open(stream=data,filetype='pdf') as pdf:
        image=pdf[page-1].get_pixmap(matrix=pymupdf.Matrix(1.4,1.4),alpha=False).tobytes('png')
    return Response(image,media_type='image/png',headers={'Cache-Control':'private, max-age=3600'})


@app.get('/pdf/{version}')
def full_pdf(version: str,token: str=Query(...)):
    record=authorize(version,token)
    response=original(record)
    def stream():
        try:
            yield from response.stream(128*1024)
        finally:
            response.close();response.release_conn()
    return StreamingResponse(stream(),media_type='application/pdf',headers={
        'Content-Disposition':'inline; filename="source.pdf"','Cache-Control':'private, max-age=3600',
        'X-Content-Type-Options':'nosniff'})
