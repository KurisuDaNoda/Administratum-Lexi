import os
import unittest
from unittest.mock import patch

import tiktoken
from fastapi.testclient import TestClient

from lexi import documents
from lexi.document_viewer import app
from scripts.import_pdf import make_sections, chunk_section


class ChunkingTests(unittest.TestCase):
    def test_rule_crossing_pages_keeps_parent_context_and_page_spans(self):
        pages=[{'page':3,'printed_label':'1','text':'# Charging\n\nYou can charge.\n\n## Restrictions\n\nOnly if eligible.'},
               {'page':4,'printed_label':'2','text':'You cannot charge after falling back.\n\n# Fighting\n\nFight rules.'}]
        sections=make_sections(pages)
        rule=next(s for s in sections if s['heading_path']=='Charging > Restrictions')
        self.assertEqual([s['page'] for s in rule['spans']],[3,4])
        chunks=chunk_section(rule,tiktoken.get_encoding('cl100k_base'))
        self.assertEqual((chunks[0]['page_start'],chunks[0]['page_end']),(3,4))
        self.assertIn('cannot charge',chunks[0]['body'])

    def test_oversized_rules_are_split_without_silent_truncation(self):
        section={'heading_path':'Test','spans':[{'page':8,'printed_label':'6','text':'An exception applies.\n\n'*2000}]}
        chunks=chunk_section(section,tiktoken.get_encoding('cl100k_base'))
        self.assertGreater(len(chunks),1)
        self.assertEqual(sum(c['body'].count('An exception applies.') for c in chunks),2000)
        self.assertTrue(all(c['page_start']==8 for c in chunks))


class CitationTests(unittest.TestCase):
    def test_physical_page_url_preserves_query_and_exact_version(self):
        with patch.dict(os.environ,{'VIEWER_SIGNING_KEY':'x'*32,'VIEWER_PUBLIC_BASE_URL':'https://lexi.example'}):
            value=documents.evidence({'version_id':'a'*64,'page_start':24,'page_end':25,'source_url':'https://example.com/file.pdf?download=1#old'})
            self.assertEqual(value['source_url'],'https://example.com/file.pdf?download=1#page=24')
            self.assertIn('/view/'+'a'*64+'?page=24&token=',value['viewer_url'])
            answer=documents.CitedAnswer('Read '+value['source_url'],[value,value])
            self.assertEqual(len(answer.page_sources),1)

    def test_invalid_token_never_reads_storage(self):
        with patch.dict(os.environ,{'VIEWER_SIGNING_KEY':'x'*32}), patch('lexi.document_viewer.storage.connect') as connect:
            response=TestClient(app).get('/view/'+'a'*64,params={'token':'wrong','page':2})
            self.assertEqual(response.status_code,403)
            connect.assert_not_called()

    def test_viewer_escapes_title_and_checks_page_bounds(self):
        record={'title':'<script>alert(1)</script>','page_count':5,'source_url':'https://example.com/file.pdf',
            'edition':11,'revision':'Test'}
        with patch('lexi.document_viewer.authorize',return_value=record):
            client=TestClient(app)
            response=client.get('/view/'+'a'*64,params={'token':'test','page':3})
            self.assertIn('PDF page 3 / 5',response.text)
            self.assertNotIn('<script>',response.text)
            self.assertIn('Content-Security-Policy',response.headers)
            self.assertEqual(client.get('/view/'+'a'*64,params={'token':'test','page':6}).status_code,404)


if __name__=='__main__':
    unittest.main()
