import math
from types import SimpleNamespace
import unittest

import tiktoken

from scripts.embed_data import combine_vectors, fetch_vectors, make_segments, render_document


class EmbeddingTests(unittest.TestCase):
    def doc(self, text):
        return {"document_id": "datasheet:1", "name": "Test Unit", "entity_type": "datasheet",
                "content": {"description": text, "source_records": [{"description": "DUPLICATE"}],
                            "legend": "FLAVOUR", "source_url": "https://example.com"}}

    def test_renderer_preserves_conditions_without_duplicate_html_or_provenance(self):
        _, body = render_document(self.doc('<b>Only</b> if every model has this ability.'))
        self.assertIn('Only if every model has this ability.', body)
        for excluded in ('<b>', 'DUPLICATE', 'FLAVOUR', 'https://'):
            self.assertNotIn(excluded, body)

    def test_long_document_covers_all_unicode_text(self):
        doc = self.doc('漢字 Æ special rules. ' * 1000)
        encoding = tiktoken.get_encoding('cl100k_base')
        parts = make_segments(doc, encoding, max_tokens=100)
        self.assertGreater(len(parts), 1)
        header, body = render_document(doc)
        self.assertEqual(''.join(text[len(header)+2:] for text, _ in parts), body)
        self.assertTrue(all(len(encoding.encode(text)) <= 100 for text, _ in parts))

    def test_weighted_vector_is_normalized(self):
        result = combine_vectors([[1, 0], [0, 1]], [3, 1], 2)
        self.assertAlmostEqual(result[0] / result[1], 3)
        self.assertAlmostEqual(sum(x*x for x in result), 1)
        with self.assertRaises(ValueError):
            combine_vectors([[math.nan, 1]], [1], 2)

    def test_api_response_order_is_recovered(self):
        def create(**kwargs):
            self.assertEqual(kwargs['input'], ['a', 'b'])
            return SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0, 1]),
                SimpleNamespace(index=0, embedding=[1, 0])], usage=SimpleNamespace(total_tokens=2))
        client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        self.assertEqual(fetch_vectors(client, ['a', 'b'], 2, 'test'), ([[1, 0], [0, 1]], 2))


if __name__ == '__main__':
    unittest.main()
