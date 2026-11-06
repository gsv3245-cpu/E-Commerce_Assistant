"""Tests for OTP authentication and the authenticated dashboard API."""
import csv
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import server
import tools


class ServerTests(unittest.TestCase):
    email = "customer@example.com"
    otp = "012345"

    def setUp(self):
        mode_patch = patch.object(config, "OTP_DEVELOPMENT_MODE", True)
        mode_patch.start()
        self.addCleanup(mode_patch.stop)
        tools.reload_data()
        tools.reset_runtime_state()
        server._pending_otps.clear()
        server._otp_ip_requests.clear()
        server._histories.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        mapping_file = Path(self.temp_dir.name) / "customer_accounts.csv"
        customers_file = Path(self.temp_dir.name) / "customers.csv"
        customers_file.write_bytes(config.CUSTOMERS_FILE.read_bytes())
        delivered = next(row for row in tools.load_orders() if row["order_status"] == "Delivered")
        self.customer_id = delivered["customer_id"]
        with open(mapping_file, "w", newline="", encoding="utf-8") as target:
            writer = csv.writer(target)
            writer.writerow(["email", "customer_id"])
            writer.writerow([self.email, self.customer_id])
        mapping_patch = patch.object(config, "CUSTOMER_ACCOUNTS_FILE", mapping_file)
        mapping_patch.start()
        self.addCleanup(mapping_patch.stop)
        customers_patch = patch.object(config, "CUSTOMERS_FILE", customers_file)
        customers_patch.start()
        self.addCleanup(customers_patch.stop)
        server.app.config.update(TESTING=True, SECRET_KEY="test-session-secret")
        self.client = server.app.test_client()

    def request_code(self, client, email=None):
        with patch.object(server.secrets, "randbelow", return_value=int(self.otp)), \
                patch.object(server.app.logger, "info"):
            return client.post("/api/auth/request-otp", json={"email": email or self.email})

    def sign_in(self, client=None, email=None):
        client = client or self.client
        response = self.request_code(client, email)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["dev_mode"])
        self.assertEqual(response.json["dev_otp"], self.otp)
        return client.post("/api/auth/verify-otp", json={
            "email": email or self.email,
            "code": self.otp,
        })

    def test_root_serves_login_until_authenticated_then_dashboard(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        login_page = self.client.get("/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertEqual(login_page.headers.get("Cache-Control"), "no-store")
        self.assertIn(b"id=\"codeLead\"", login_page.data)
        login_page.close()
        self.assertEqual(self.sign_in().status_code, 200)
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        dashboard.close()

    def test_otp_validation_expiration_and_attempt_limit(self):
        invalid = self.client.post("/api/auth/request-otp", json={"email": "not-an-email"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.request_code(self.client).status_code, 200)
        wrong = self.client.post("/api/auth/verify-otp", json={"email": self.email, "code": "999999"})
        self.assertEqual(wrong.status_code, 401)
        self.assertIn("not correct", wrong.json["error"])
        self.assertEqual(self.client.post(
            "/api/auth/verify-otp", json={"email": self.email, "code": self.otp}
        ).status_code, 200)

        self.request_code(self.client)
        server._pending_otps[self.email]["expires_at"] = time.monotonic() - 1
        expired = self.client.post("/api/auth/verify-otp", json={"email": self.email, "code": self.otp})
        self.assertEqual(expired.status_code, 401)
        self.assertIn("expired", expired.json["error"])

        self.request_code(self.client)
        for _ in range(config.OTP_MAX_ATTEMPTS):
            limited = self.client.post(
                "/api/auth/verify-otp", json={"email": self.email, "code": "999999"}
            )
        self.assertEqual(limited.status_code, 429)

    def test_otp_resend_is_rate_limited(self):
        self.assertEqual(self.request_code(self.client).status_code, 200)
        response = self.request_code(self.client)
        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.json["resend_in"], 0)

    def test_development_otp_is_only_returned_to_loopback_clients(self):
        remote_client = server.app.test_client()
        response = remote_client.post(
            "/api/auth/request-otp",
            json={"email": self.email},
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("dev_otp", response.json)
        self.assertNotIn(self.email, server._pending_otps)

    def test_smtp_otp_is_sent_and_never_returned_to_the_browser(self):
        with patch.object(config, "OTP_DEVELOPMENT_MODE", False), \
                patch.object(server.mailer, "smtp_is_configured", return_value=True), \
                patch.object(server.mailer, "send_otp_email") as send_email, \
                patch.object(server.secrets, "randbelow", return_value=int(self.otp)):
            response = self.client.post("/api/auth/request-otp", json={"email": self.email})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["dev_mode"])
        self.assertNotIn("dev_otp", response.json)
        send_email.assert_called_once_with(self.email, self.otp)
        verified = self.client.post("/api/auth/verify-otp", json={
            "email": self.email,
            "code": self.otp,
        })
        self.assertEqual(verified.status_code, 200)

    def test_smtp_otp_does_not_send_to_unlinked_emails(self):
        with patch.object(config, "OTP_DEVELOPMENT_MODE", False), \
                patch.object(server.mailer, "smtp_is_configured", return_value=True), \
                patch.object(server.mailer, "send_otp_email") as send_email:
            response = self.client.post(
                "/api/auth/request-otp", json={"email": "unknown@example.com"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("dev_otp", response.json)
        send_email.assert_not_called()

    def test_smtp_signup_sends_code_then_creates_named_profile(self):
        email = "new-customer@example.com"
        with patch.object(config, "OTP_DEVELOPMENT_MODE", False), \
                patch.object(server.mailer, "smtp_is_configured", return_value=True), \
                patch.object(server.mailer, "send_otp_email") as send_email, \
                patch.object(server.secrets, "randbelow", return_value=int(self.otp)):
            response = self.client.post("/api/auth/request-otp", json={
                "email": email, "intent": "signup",
                "first_name": "Asha", "last_name": "Iyer",
            })
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("dev_otp", response.json)
            send_email.assert_called_once_with(email, self.otp)
            verified = self.client.post("/api/auth/verify-otp", json={
                "email": email, "code": self.otp,
            })

        self.assertEqual(verified.status_code, 200)
        self.assertEqual(self.client.get("/api/me").json["customer"]["name"], "Asha Iyer")

    def test_smtp_failure_invalidates_the_pending_code(self):
        with patch.object(config, "OTP_DEVELOPMENT_MODE", False), \
                patch.object(server.mailer, "smtp_is_configured", return_value=True), \
                patch.object(server.mailer, "send_otp_email", side_effect=OSError("offline")), \
                patch.object(server.app.logger, "exception"), \
                patch.object(server.secrets, "randbelow", return_value=int(self.otp)):
            response = self.client.post("/api/auth/request-otp", json={"email": self.email})

        self.assertEqual(response.status_code, 503)
        self.assertNotIn(self.email, server._pending_otps)

    def test_valid_otp_requires_an_explicit_customer_mapping(self):
        response = self.sign_in(self.client, "unlinked@example.com")
        self.assertEqual(response.status_code, 403)
        self.assertIn("not linked", response.json["error"])
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_signup_requires_both_names_and_persists_profile_after_otp(self):
        email = "new-customer@example.com"
        missing_name = self.client.post("/api/auth/request-otp", json={
            "email": email, "intent": "signup", "first_name": "Asha",
        })
        self.assertEqual(missing_name.status_code, 400)
        self.assertNotIn(email, server._pending_otps)

        with patch.object(server.secrets, "randbelow", return_value=int(self.otp)), \
                patch.object(server.app.logger, "info"):
            requested = self.client.post("/api/auth/request-otp", json={
                "email": email, "intent": "signup",
                "first_name": "  Asha ", "last_name": "  Iyer  ",
            })
        self.assertEqual(requested.status_code, 200)
        verified = self.client.post("/api/auth/verify-otp", json={
            "email": email, "code": self.otp,
        })
        self.assertEqual(verified.status_code, 200)
        profile = self.client.get("/api/me")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json["customer"]["name"], "Asha Iyer")
        self.assertNotIn("email", profile.json["customer"])
        customer_id = profile.json["customer"]["customer_id"]
        self.assertEqual(server._customer_accounts()[email], customer_id)
        self.assertEqual(tools.get_customer(customer_id)["name"], "Asha Iyer")

    def test_signup_rejects_an_email_already_linked_to_an_account(self):
        response = self.client.post("/api/auth/request-otp", json={
            "email": self.email, "intent": "signup",
            "first_name": "Asha", "last_name": "Iyer",
        })
        self.assertEqual(response.status_code, 409)

    def test_authenticated_dashboard_apis_are_scoped_and_data_aware(self):
        self.assertEqual(self.sign_in().status_code, 200)
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json["customer"]["customer_id"], self.customer_id)

        orders = self.client.get("/api/orders")
        self.assertEqual(orders.status_code, 200)
        self.assertTrue(orders.json["orders"])
        self.assertTrue(all(
            order["order_id"] in {
                row["order_id"] for row in tools.load_orders() if row["customer_id"] == self.customer_id
            } for order in orders.json["orders"]
        ))
        self.assertTrue(all("total_amount" not in order for order in orders.json["orders"]))

        products = self.client.get("/api/products?category=Smartphones")
        self.assertEqual(products.status_code, 200)
        self.assertTrue(all(p["category"] == "Smartphones" for p in products.json["products"]))
        product_ids = [p["product_id"] for p in tools.load_products()[:2]]
        comparison = self.client.get("/api/compare?ids=" + ",".join(product_ids))
        self.assertEqual(comparison.status_code, 200)
        self.assertEqual(len(comparison.json["products"]), 2)

        self.assertEqual(self.client.get("/api/policy").status_code, 200)
        self.assertEqual(self.client.get("/api/returns").status_code, 200)
        self.assertEqual(self.client.get("/api/evaluation").status_code, 200)
        delivered = next(
            order for order in orders.json["orders"] if order["order_status"] == "Delivered"
        )
        eligibility = self.client.get(
            f"/api/orders/{delivered['order_id']}/eligibility?reason=changed%20my%20mind"
        )
        self.assertEqual(eligibility.status_code, 200)
        self.assertEqual(eligibility.json["error"], "DATA_UNAVAILABLE")
        self.assertFalse(eligibility.json["eligible"])

    def test_sessions_are_isolated_and_logout_revokes_access(self):
        self.assertEqual(self.sign_in().status_code, 200)
        other_client = server.app.test_client()
        self.assertEqual(other_client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
