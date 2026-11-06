import unittest

from agent import _normalize_search_args
import tools


class SearchArgumentNormalizationTests(unittest.TestCase):
    def test_under_budget_drops_duplicated_minimum_price(self):
        args = {
            "category": "Laptop",
            "max_price": "under 60000",
            "min_price": "60000",
        }

        normalized = _normalize_search_args("search_products", args, "Show me laptops under 60000")

        self.assertIsNone(normalized["min_price"])
        result = tools.search_products(**normalized)
        self.assertGreater(result["total_matches"], 0)

    def test_explicit_minimum_price_is_preserved(self):
        args = {"max_price": "under 60000", "min_price": "60000"}

        normalized = _normalize_search_args(
            "search_products", args, "Minimum price 60000, but under 60000"
        )

        self.assertEqual(normalized["min_price"], "60000")

    def test_other_tools_are_unchanged(self):
        args = {"min_price": "60000", "max_price": "under 60000"}

        normalized = _normalize_search_args("get_order_status", args, "under 60000")

        self.assertEqual(normalized, args)


if __name__ == "__main__":
    unittest.main()
