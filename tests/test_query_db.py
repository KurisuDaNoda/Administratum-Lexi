import unittest

from scripts.query_db import embedding_configuration, search


class FakeConnection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement.as_string(), params))
        return next(self.rows)


class QueryTests(unittest.TestCase):
    def test_name_is_parameterized_and_complete_document_is_returned(self):
        doc = {'content': {'rules': [1], 'enhancements': [2], 'stratagems': [3]}}
        conn = FakeConnection([[('01', 'Example', 'https://example.com', doc, None)]])
        name = "Example' OR true --"
        results = search(conn, ['detachments'], 'snapshot', name=name)
        statement, params = conn.calls[0]
        self.assertNotIn(name, statement)
        self.assertIn(name, params)
        self.assertEqual(results[0]['document'], doc)
        self.assertEqual(results[0]['source_url'], 'https://example.com')

    def test_semantic_results_are_ranked_across_tables(self):
        conn = FakeConnection([[('1', 'A', None, {}, .4)], [('2', 'B', None, {}, .9)]])
        result = search(conn, ['datasheets', 'detachments'], 'snapshot', vector=[1, 0], config_id='config', limit=1)
        self.assertEqual(result[0]['name'], 'B')
        self.assertIn('embedding_config_id=%s', conn.calls[0][0])
        self.assertIn('config', conn.calls[0][1])

    def test_mixed_models_are_rejected(self):
        conn = FakeConnection([[('a', 'model-a', 2), ('b', 'model-b', 2)]])
        with self.assertRaisesRegex(ValueError, 'Multiple embedding configurations'):
            embedding_configuration(conn, ['datasheets'], 'snapshot')

    def test_absent_embeddings_are_reported(self):
        with self.assertRaisesRegex(ValueError, 'No matching embeddings'):
            embedding_configuration(FakeConnection([[]]), ['datasheets'], 'snapshot')


if __name__ == '__main__':
    unittest.main()
