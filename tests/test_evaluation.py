import unittest

from evaluation.metrics import _products, mentioned_products


class ProductMentionMatchingTests(unittest.TestCase):
    def test_brandless_model_names_are_not_truncated_to_short_fragments(self):
        reply = "A 15-inch laptop is available under 60000."
        self.assertEqual(mentioned_products(reply, _products()), [])


if __name__ == "__main__":
    unittest.main()
