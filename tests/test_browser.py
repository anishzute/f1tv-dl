import os
import threading
import unittest
from unittest.mock import Mock, patch

from f1tv_dl.browser import (
    BrowserAuthorization,
    browser_environment,
    browser_error_message,
    proxy_options,
    start_browser,
)
from f1tv_dl.errors import AuthenticationError, OperationCancelled
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

    def test_startup_errors_report_the_cause_without_private_diagnostics(self):
        cases = (
            ("Target closed. Missing X server or $DISPLAY", "desktop display"),
            (
                "Target closed. error while loading shared libraries: libnss3.so: not found",
                "libnss3.so",
            ),
            ("Target closed. Host system is missing dependencies", "install-deps chromium"),
            ("Target closed. GLIBCXX_3.4.30 not found", "compatible system libraries"),
            ("Target closed. No space left on device", "temporary directory is full"),
            ("Target closed. No usable sandbox", "process sandbox"),
            ("Target closed. <process did exit: exitCode=127, signal=null>", "exit code 127"),
        )
        for detail, expected in cases:
            with self.subTest(expected=expected):
                message = browser_error_message(
                    Exception(detail + " https://private.test/?code=private-code"), startup=True
                )
                self.assertIn(expected, message)
                self.assertNotIn("private", message)


class BrowserStartupTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(
            os.environ,
            {
                "CONDA_PREFIX": "/fixture/conda",
                "LD_LIBRARY_PATH": "/fixture/conda/lib:/opt/browser/lib",
                "LD_PRELOAD": "/fixture/conda/lib/preload.so /opt/browser/preload.so",
                "GTK_PATH": "/fixture/conda/lib/gtk",
                "GIO_MODULE_DIR": "/fixture/conda/lib/gio/modules",
                "DISPLAY": ":88",
                "SSL_CERT_FILE": "/fixture/roots.pem",
                "https_proxy": "http://user:fixture-password@proxy.test:8080",
                "F1TV_PASSWORD": "fixture-secret",
                "F1TV_TOKEN": "fixture-token",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.error = type("BrowserError", (Exception,), {})
        self.playwright = Mock()
        self.browser = self.playwright.chromium.launch.return_value
        self.browser.version = "154.0.0.0"
        self.page = self.browser.new_context.return_value.new_page.return_value
        self.page.evaluate.return_value = "Mozilla/5.0 Chrome/154.0.0.0"
        installed = patch("f1tv_dl.browser.installed_chromium", return_value="/usr/bin/chromium")
        self.installed = installed.start()
        self.addCleanup(installed.stop)

    def test_browser_environment_keeps_desktop_network_and_unrelated_libraries(self):
        environment = browser_environment()
        self.assertEqual(environment["LD_LIBRARY_PATH"], "/opt/browser/lib")
        self.assertEqual(environment["LD_PRELOAD"], "/opt/browser/preload.so")
        self.assertNotIn("GTK_PATH", environment)
        self.assertNotIn("GIO_MODULE_DIR", environment)
        self.assertEqual(environment["DISPLAY"], ":88")
        self.assertEqual(environment["SSL_CERT_FILE"], "/fixture/roots.pem")
        self.assertEqual(environment["https_proxy"], os.environ["https_proxy"])
        self.assertNotIn("F1TV_PASSWORD", environment)
        self.assertNotIn("F1TV_TOKEN", environment)
        self.assertEqual(os.environ["F1TV_PASSWORD"], "fixture-secret")
        self.assertIn("/fixture/conda/lib", os.environ["LD_LIBRARY_PATH"])

    def test_failed_managed_browser_falls_back_before_navigation_or_credential_entry(self):
        self.playwright.chromium.launch.side_effect = [self.error("Target closed"), self.browser]
        stages = []
        browser, context, page, client = start_browser(
            self.playwright, self.error, headless=True, status=stages.append
        )
        self.assertIs(browser, self.browser)
        self.assertIs(page, self.page)
        calls = self.playwright.chromium.launch.call_args_list
        self.assertNotIn("executable_path", calls[0].kwargs)
        self.assertEqual(calls[1].kwargs["executable_path"], "/usr/bin/chromium")
        self.assertIn("installed Chrome", stages[0])
        self.assertEqual(client.user_agent, "Mozilla/5.0 Chrome/154.0.0.0")
        page.goto.assert_not_called()
        context.cookies.assert_not_called()

    def test_explicit_browser_path_is_respected_and_never_replaced(self):
        self.playwright.chromium.launch.side_effect = self.error(
            "error while loading shared libraries: libnss3.so: not found"
        )
        with self.assertRaisesRegex(AuthenticationError, "libnss3.so"):
            start_browser(self.playwright, self.error, executable="/custom/chrome")
        self.installed.assert_not_called()
        self.assertEqual(self.playwright.chromium.launch.call_count, 1)

    def test_display_failure_is_still_reported_when_the_fallback_also_fails(self):
        self.playwright.chromium.launch.side_effect = self.error("Missing X server or $DISPLAY")
        with self.assertRaisesRegex(AuthenticationError, "desktop display"):
            start_browser(self.playwright, self.error)
        self.assertEqual(self.playwright.chromium.launch.call_count, 2)

    def test_context_startup_failure_closes_the_failed_process_before_fallback(self):
        failed = Mock()
        failed.new_context.side_effect = self.error("Target closed")
        self.playwright.chromium.launch.side_effect = [failed, self.browser]
        start_browser(self.playwright, self.error)
        failed.close.assert_called_once()

    def test_cancellation_before_start_does_not_launch_or_select_a_browser(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            start_browser(self.playwright, self.error, cancel=cancel)
        self.playwright.chromium.launch.assert_not_called()
        self.installed.assert_not_called()

    def test_cancellation_during_start_closes_the_partial_browser_without_retry(self):
        cancel = threading.Event()

        def metadata(script):
            cancel.set()
            return "Mozilla/5.0 Chrome/154.0.0.0"

        self.page.evaluate.side_effect = metadata
        with self.assertRaises(OperationCancelled):
            start_browser(self.playwright, self.error, cancel=cancel)
        self.browser.close.assert_called_once()
        self.assertEqual(self.playwright.chromium.launch.call_count, 1)
