import tempfile
import unittest
import json
from unittest.mock import patch
from pathlib import Path

from scripts.import_data import clean_html, collect_urls, read_export
from scripts import import_data


class ExportParsingTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "example.csv"
            path.write_text(text, encoding="utf-8-sig")
            return read_export(path)

    def test_multiline_html_and_empty_last_field(self):
        rows = self.parse('id|description|parameter|\n000001|<div>First\n\nSecond</div>||\n')
        self.assertEqual(rows, [{"id": "000001", "description": "<div>First\n\nSecond</div>", "parameter": ""}])

    def test_truncated_record_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse('id|description|\n000001|unfinished')

    def test_extra_delimiter_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse('id|description|\n000001|text|unexpected|\n')

    def test_inline_keywords_and_source_links(self):
        html = '<span>HERETIC</span> <span>ASTARTES</span><br><a href="/rules/#test">Rule</a>'
        self.assertEqual(clean_html(html)[0], 'HERETIC ASTARTES\nRule')
        self.assertEqual(collect_urls(html, 'https://wahapedia.ru/factions/example'), {'https://wahapedia.ru/rules/#test'})


class ImportEmbeddingFlowTests(unittest.TestCase):
    def run_import(self, flags=(), embedding_error=None):
        with tempfile.TemporaryDirectory() as folder:
            report = {"snapshot_id": "test", "document_counts": {}, "warnings": [], "vectorized": False}
            events = []
            def embed(*args):
                events.append(('embed', args))
                if embedding_error:
                    raise embedding_error
                return {"status": "complete"}
            with patch.object(import_data, 'build', return_value=({}, report)), \
                 patch.object(import_data, 'save_database', side_effect=lambda *_: events.append(('save',))), \
                 patch.object(import_data, 'run_embeddings', side_effect=embed):
                if embedding_error:
                    with self.assertRaises(RuntimeError):
                        import_data.main(['--output-dir', folder, *flags])
                else:
                    import_data.main(['--output-dir', folder, *flags])
            stored = json.loads((Path(folder)/'test'/'validation-report.json').read_text())
            return events, stored

    def test_default_embeds_after_commit_and_targets_imported_snapshot(self):
        events, report = self.run_import()
        self.assertEqual(events, [('save',), ('embed', ('test', 'text-embedding-3-small', 1536))])
        self.assertTrue(report['vectorized'])

    def test_skip_and_dry_run_do_not_embed(self):
        for flag, expected in [('--skip-embeddings', [('save',)]), ('--dry-run', [])]:
            events, report = self.run_import([flag])
            self.assertEqual(events, expected)
            self.assertEqual(report['embedding_status'], 'skipped')

    def test_failed_embedding_preserves_import_status(self):
        _, report = self.run_import(embedding_error=RuntimeError('API unavailable'))
        self.assertTrue(report['database_imported'])
        self.assertFalse(report['vectorized'])
        self.assertEqual(report['embedding_status'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
