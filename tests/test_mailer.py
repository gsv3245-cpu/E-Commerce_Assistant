"""SMTP message construction and transport tests."""
import unittest
from unittest.mock import patch

import config
import mailer


class OtpMailTests(unittest.TestCase):
    def test_sends_otp_over_starttls_and_strips_app_password_spacing(self):
        with patch.multiple(
            config,
            SMTP_HOST="smtp.gmail.com",
            SMTP_PORT=587,
            SMTP_USERNAME="sender@example.com",
            SMTP_PASSWORD="abcd efgh ijkl mnop",
            SMTP_FROM="sender@example.com",
        ), patch("mailer.smtplib.SMTP") as smtp_factory:
            smtp = smtp_factory.return_value.__enter__.return_value
            mailer.send_otp_email("customer@example.com", "012345")

        smtp_factory.assert_called_once_with("smtp.gmail.com", 587, timeout=config.SMTP_TIMEOUT_SECONDS)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("sender@example.com", "abcdefghijklmnop")
        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "customer@example.com")
        self.assertIn("012345", message.get_content())

    def test_refuses_to_send_without_required_smtp_credentials(self):
        with patch.multiple(
            config,
            SMTP_HOST="smtp.gmail.com",
            SMTP_PORT=587,
            SMTP_USERNAME="sender@example.com",
            SMTP_PASSWORD="",
            SMTP_FROM="sender@example.com",
        ), self.assertRaises(RuntimeError):
            mailer.send_otp_email("customer@example.com", "012345")


if __name__ == "__main__":
    unittest.main()
