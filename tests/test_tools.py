"""Tests for the catalogue and order tools against the supplied CSV schemas."""
import csv
import unittest
from datetime import date

import config
import tools


def _csv_rows(name):
    with open(config.DATA_DIR / name, newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


class DatasetTests(unittest.TestCase):
    def setUp(self):
        tools.reload_data()

    def test_assignment_csv_schemas_and_relationships(self):
        products, orders = _csv_rows("products.csv"), _csv_rows("orders.csv")
        customers, accounts = _csv_rows("customers.csv"), _csv_rows("customer_accounts.csv")
        self.assertEqual(len(products), 300)
        self.assertEqual(len(orders), 300)
        product_names = {row["product_name"].strip().casefold() for row in products}
        self.assertEqual(len(product_names), len(products))
        research_sources = _csv_rows("product_research_sources.csv")
        self.assertEqual({row["category"] for row in research_sources},
                         {row["category"] for row in products})
        self.assertTrue(all(row["source_url"].startswith("https://") for row in research_sources))
        self.assertEqual(list(products[0]), [
            "product_id", "product_name", "category", "price", "brand", "rating", "stock", "specifications",
        ])
        self.assertEqual(list(orders[0]), [
            "order_id", "customer_id", "product_id", "order_date", "order_status",
            "expected_delivery_date", "payment_status",
        ])
        product_ids = {row["product_id"] for row in products}
        self.assertEqual(len(product_ids), len(products))
        self.assertEqual(len({row["order_id"] for row in orders}), len(orders))
        self.assertTrue(all(row["product_id"] in product_ids for row in orders))
        order_customer_ids = {row["customer_id"] for row in orders}
        self.assertEqual({row["customer_id"] for row in customers}, order_customer_ids)
        self.assertTrue(all(row["customer_id"] in order_customer_ids for row in accounts))
        self.assertTrue(all(row["product_name"].strip() and row["specifications"].strip()
                            for row in products))

    def test_policies_cover_exactly_the_current_categories(self):
        categories = {product["category"] for product in tools.load_products()}
        self.assertEqual(set(tools.load_policy()), categories)
        for policy in tools.load_policy().values():
            self.assertIn("Synthetic demo policy", policy["conditions"])


class ProductToolTests(unittest.TestCase):
    def setUp(self):
        tools.reload_data()
        self.products = tools.load_products()

    def test_search_supports_each_catalogue_category(self):
        for category in tools._valid_categories():
            with self.subTest(category=category):
                result = tools.search_products(category=category, limit=20)
                self.assertGreater(result["total_matches"], 0)
                self.assertTrue(all(p["category"] == category for p in result["products"]))

    def test_category_alias_resolves_plural_dataset_category(self):
        self.assertEqual(tools._canon_category("smartphone"), "Smartphones")
        result = tools.search_products(category="smartphone")
        self.assertTrue(all(p["category"] == "Smartphones" for p in result["products"]))
        self.assertEqual(tools._canon_category("earbuds"), "Audio")
        self.assertEqual(tools._canon_category("smartwatch"), "Wearables")

    def test_category_listing_matches_catalogue(self):
        result = tools.list_categories()
        categories = result["categories"]
        self.assertEqual({row["category"] for row in categories}, set(tools._valid_categories()))
        self.assertEqual(sum(row["products"] for row in categories), len(self.products))

    def test_product_lookup_and_comparison_use_current_ids(self):
        product = self.products[0]
        result = tools.get_product_details(product["product_id"].lower())
        self.assertEqual(result["product_id"], product["product_id"])
        comparison = tools.compare_products([product["product_id"], self.products[1]["product_id"]])
        self.assertNotIn("error", comparison)
        self.assertEqual({p["product_id"] for p in comparison["products"]},
                         {product["product_id"], self.products[1]["product_id"]})

    def test_comparison_requires_products_from_same_category(self):
        first = self.products[0]
        second = next(p for p in self.products if p["category"] != first["category"])
        result = tools.compare_products([first["product_id"], second["product_id"]])
        self.assertEqual(result["error"], "MIXED_CATEGORIES")

    def test_price_stock_filtering_and_sorting(self):
        result = tools.search_products(sort_by="price_asc", limit=20)
        prices = [product["price_inr"] for product in result["products"]]
        self.assertEqual(prices, sorted(prices))

        stocked = next(product for product in self.products if product["stock"] > 0)
        result = tools.search_products(category=stocked["category"], in_stock_only=True, limit=20)
        self.assertTrue(all(product["stock"] > 0 for product in result["products"]))

        out_of_stock = next((product for product in self.products if product["stock"] == 0), None)
        if out_of_stock:
            details = tools.get_product_details(out_of_stock["product_id"])
            self.assertEqual(details["stock"], 0)

    def test_unknown_category_and_bad_arguments_return_errors(self):
        self.assertEqual(tools.search_products(category="not a category")["error"], "UNKNOWN_CATEGORY")
        self.assertEqual(tools.search_products(min_rating=9)["error"], "INVALID_ARGUMENT")
        self.assertEqual(tools.get_return_policy("not a category")["error"], "UNKNOWN_CATEGORY")


class OrderToolTests(unittest.TestCase):
    def setUp(self):
        tools.reload_data()
        tools.reset_runtime_state()
        self.orders = tools.load_orders()

    def test_order_status_only_returns_available_order_fields(self):
        order = self.orders[0]
        result = tools.get_order_status(order["order_id"], order["customer_id"])
        self.assertEqual(result["order_id"], order["order_id"])
        self.assertEqual(result["order_status"], order["order_status"])
        self.assertEqual(result["payment_status"], order["payment_status"])
        self.assertNotIn("total_amount", result)
        self.assertNotIn("payment_method", result)
        self.assertNotIn("delivered_date", result)

    def test_unpadded_order_numbers_match_canonical_ids(self):
        order = self.orders[0]
        unpadded_id = "O" + str(int(order["order_id"][1:]))
        result = tools.get_order_status(unpadded_id, order["customer_id"])
        self.assertEqual(result["order_id"], order["order_id"])

    def test_customer_order_list_is_scoped_and_sorted(self):
        order = self.orders[0]
        result = tools.list_my_orders(order["customer_id"])
        self.assertGreater(result["order_count"], 0)
        self.assertTrue(all(row["order_id"] in {
            entry["order_id"] for entry in self.orders if entry["customer_id"] == order["customer_id"]
        } for row in result["orders"]))
        dates = [row["order_date"] for row in result["orders"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertTrue(all("total_amount" not in row for row in result["orders"]))

    def test_other_customers_and_missing_orders_are_indistinguishable(self):
        order = self.orders[0]
        another_customer = next(cid for cid in tools.load_customers() if cid != order["customer_id"])
        other = tools.get_order_status(order["order_id"], another_customer)
        missing = tools.get_order_status("O999999", another_customer)
        self.assertEqual(other["error"], "ORDER_NOT_FOUND")
        self.assertEqual(other["error"], missing["error"])
        self.assertNotIn(order["product_id"], str(other))

    def test_missing_customer_identity_is_rejected(self):
        self.assertEqual(tools.get_order_status(self.orders[0]["order_id"])["error"], "NOT_SIGNED_IN")
        self.assertEqual(tools.list_my_orders(None)["error"], "NOT_SIGNED_IN")

    def test_actual_delivery_date_is_not_invented_for_return_eligibility(self):
        order = next(row for row in self.orders if row["order_status"] == "Delivered")
        result = tools.check_return_eligibility(order["order_id"], order["customer_id"])
        self.assertEqual(result["error"], "DATA_UNAVAILABLE")
        self.assertEqual(result["decision"], "UNKNOWN")
        self.assertFalse(result["eligible"])

    def test_return_request_refuses_when_eligibility_cannot_be_verified(self):
        order = next(row for row in self.orders if row["order_status"] == "Delivered")
        result = tools.initiate_return(order["order_id"], order["customer_id"], "changed my mind")
        self.assertFalse(result["created"])
        self.assertEqual(result["error"], "DATA_UNAVAILABLE")
        self.assertEqual(tools.get_created_returns(), {})

    def test_existing_return_status_is_reported_from_orders_csv(self):
        order = next(row for row in self.orders if row["order_status"] == "Return Requested")
        self.assertEqual(tools.get_order_status(order["order_id"], order["customer_id"])["return_status"],
                         "Return Requested")

    def test_refund_amount_and_timeline_are_not_fabricated(self):
        order = next(row for row in self.orders if row["payment_status"] == "Refunded")
        result = tools.get_refund_details(order["order_id"], order["customer_id"])
        self.assertEqual(result["refund_state"], "COMPLETED")
        self.assertNotIn("refund_amount", result)
        self.assertNotIn("expected_by", result)

    def test_returned_orders_are_not_eligible_again(self):
        order = next(row for row in self.orders if row["order_status"] == "Returned")
        result = tools.check_return_eligibility(order["order_id"], order["customer_id"])
        self.assertEqual(result["decision"], "NOT_ELIGIBLE")
        self.assertIn("already exists", result["message"])


class FormattingTests(unittest.TestCase):
    def test_inr_indian_grouping(self):
        self.assertEqual(tools.inr(999), "₹999")
        self.assertEqual(tools.inr(54999), "₹54,999")
        self.assertEqual(tools.inr(119999), "₹1,19,999")

    def test_business_days_skip_weekends(self):
        self.assertEqual(tools.add_business_days(date(2026, 10, 27), 5), date(2026, 11, 3))

    def test_number_parsing(self):
        self.assertEqual(tools._num("₹50,000", "x"), 50000)
        self.assertEqual(tools._num("under 60000", "x"), 60000)
        self.assertEqual(tools._num("1 lakh", "x"), 100000)
        self.assertIsNone(tools._num("None", "x"))
        with self.assertRaises(tools.ArgError):
            tools._num("cheap", "x")


if __name__ == "__main__":
    unittest.main()
