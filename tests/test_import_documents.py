import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

from scripts.import_documents import file_metadata,import_folder


class FolderImportTests(unittest.TestCase):
    def test_unknown_files_stay_unverified_and_relative_paths_make_distinct_ids(self):
        folder=Path('Documents')
        _,_,a=file_metadata(folder/'army-a'/'rules.pdf',folder,{})
        _,_,b=file_metadata(folder/'army-b'/'rules.pdf',folder,{})
        self.assertNotEqual(a['document_id'],b['document_id'])
        self.assertEqual(a['edition'],0)
        self.assertIn('Unverified',a['edition_evidence'])

    def test_manifest_import_passes_metadata_without_review_or_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/'rules.PDF').write_bytes(b'%PDF-test')
            manifest={'rules.PDF':{'title':'Core Rules','document_id':'core-rules','edition':11,
                'edition_evidence':'Verified official release','revision':'June 2026'}}
            with patch('scripts.import_documents.documents.initialize'), \
                 patch('scripts.import_documents.import_pdf',return_value={'version_id':'test'}) as importer:
                report=import_folder(folder,manifest,embed=True)
            argv=importer.call_args.args[0]
            self.assertIn('--embed',argv)
            self.assertNotIn('--reviewed',argv)
            self.assertNotIn('--activate',argv)
            self.assertEqual(report['files'][0]['status'],'imported')

    def test_known_identical_pdf_reuses_version_and_dry_run_never_writes(self):
        known={'title':'Known Rules','edition':11,'version_id':'a'*64,'is_active':False,'extraction_reviewed':False}
        connection=MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value=('document_versions',)
        cursor=connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.execute.return_value.fetchone.return_value=known
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/'renamed.pdf').write_bytes(b'%PDF-test')
            with patch('scripts.import_documents.storage.connect',return_value=connection), \
                 patch('scripts.import_documents.documents.initialize') as init, \
                 patch('scripts.import_documents.import_pdf') as importer, \
                 patch('scripts.import_documents.embed_version') as embed:
                report=import_folder(folder,{},embed=True,dry_run=True)
            init.assert_not_called();importer.assert_not_called();embed.assert_not_called()
            self.assertEqual(report['files'][0]['status'],'would_reuse')
            self.assertEqual(report['files'][0]['version_id'],'a'*64)


if __name__=='__main__':
    unittest.main()
