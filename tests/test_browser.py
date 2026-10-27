import os
import threading
import unittest
from unittest.mock import Mock, patch

from f1tv_dl.browser import BrowserAuthorization, browser_error_message, proxy_options
from f1tv_dl.errors import OperationCancelled
from f1tv_dl.oauth import trusted_origin


class BrowserDataTests(unittest.TestCase):
    def test_autofill_requires_the_exact_https_identity_provider(self):
        self.assertTrue(trusted_origin("https://id.formula1.com/as/authorize", "id.formula1.com"))
        for url in (
            "https://id.formula1.com.attacker.test/login",
            "http://id.formula1.com/login",
            "https://id.formula1.com:9000/login",
            "https://user@id.formula1.com/login",
            "https://id.formula1.com:bad/login",
            "about:blank",
        ):
            self.assertFalse(trusted_origin(url, "id.formula1.com"))

    def test_proxy_credentials_are_separate_from_the_server_url(self):
        with patch.dict(
            os.environ, {"https_proxy": "http://user:p%40ss@proxy.test:8080"}, clear=True
        ):
            self.assertEqual(
                proxy_options(),
                {"server": "http://proxy.test:8080", "username": "user", "password": "p@ss"},
            )

    def test_browser_errors_do_not_expose_authorization_details(self):
        message = browser_error_message(
            Exception("net::ERR_CERT_AUTHORITY_INVALID https://private/?code=secret")
        )
        self.assertIn("certificate", message)
        self.assertNotIn("private", message)
        self.assertNotIn("secret", message)

    def test_cancellation_does_not_read_browser_state(self):
        context = Mock()
        cancel = threading.Event()
        cancel.set()
        flow = BrowserAuthorization(context, Mock(), cancel=cancel)
        with self.assertRaises(OperationCancelled):
            flow.wait(10, Exception)
        context.cookies.assert_not_called()
