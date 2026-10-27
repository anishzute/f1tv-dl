"""Real Chromium tests of native OAuth callbacks; every browser request is intercepted."""

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from f1tv_dl.browser import BrowserAuthorization, start_browser
from f1tv_dl.errors import AuthenticationError, OperationCancelled
from f1tv_dl.oauth import REDIRECT_URI, OAuthSession

AVAILABLE = importlib.util.find_spec("playwright") is not None
IDP = "https://id.formula1.com"


def access_token():
    claims = (
        base64.urlsafe_b64encode(json.dumps({"subscriptionToken": "fixture-subscription"}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{claims}.signature"


@unittest.skipUnless(AVAILABLE, "Optional Playwright browser extra is not installed")
class BrowserOAuthIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import Error, sync_playwright

        cls.browser_error = Error
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        executable = cls.playwright.chromium.executable_path
        if not Path(executable).is_file():
            executable = shutil.which("chromium") or shutil.which("google-chrome")
        if not executable:
            raise unittest.SkipTest("Chromium is not installed; run playwright install chromium")
        cls.executable = executable
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.oauth = OAuthSession()
        self.submissions = []
        self.authorization_params = None
        self.unexpected_requests = []
        self.fragment_response = False
        self.popup = False
        self.wrong_state = False
        self.stages = []
        self.context.route("**/*", self.respond)
        self.flow = BrowserAuthorization(
            self.context,
            self.oauth,
            "fixture@example.test",
            "fixture-password",
            status=self.stages.append,
        )
        self.page = self.context.new_page()

    def callback_url(self):
        params = urlencode(
            {
                "code": "fixture-code",
                "state": "wrong-state" if self.wrong_state else self.oauth.state,
            }
        )
        if self.fragment_response:
            return REDIRECT_URI + "?" + params
        return "https://account.formula1.com/?" + params + "#//login/"

    def respond(self, route):
        request = route.request
        parts = urlsplit(request.url)
        if parts.hostname == "id.formula1.com" and parts.path == "/as/authorize":
            self.authorization_params = parse_qs(parts.query)
            navigate = (
                f"window.open({json.dumps(self.callback_url())})"
                if self.popup
                else f"location.href={json.dumps(self.callback_url())}"
            )
            html = (
                '<button type="button">Sign In</button><form>'
                '<input name="username" autocomplete="username">'
                '<input name="password" type="password" autocomplete="current-password">'
                '<button type="button" onclick="signIn()">Sign In</button></form>'
                '<script>async function signIn(){const response=await fetch("/authenticate",'
                '{method:"POST",body:JSON.stringify({'
                'username:document.querySelector("input[name=username]").value,'
                'password:document.querySelector("input[name=password]").value})});'
                f"if(response.ok){{{navigate}}}else location.hash='rejected';}}</script>"
            )
        elif parts.hostname == "id.formula1.com" and parts.path == "/authenticate":
            self.submissions.append(request.post_data_json)
            valid = request.post_data_json == {
                "username": "fixture@example.test",
                "password": "fixture-password",
            }
            route.fulfill(status=200 if valid else 401, content_type="application/json", body="{}")
            return
        elif parts.hostname == "id.formula1.com.attacker.test":
            html = '<input name="username"><input name="password" type="password">'
        else:
            # Includes F1TV's homepage and every script belonging to the old account application.
            self.unexpected_requests.append((parts.hostname, parts.path))
            route.fulfill(status=503, body="These resources must not be needed for sign-in")
            return
        route.fulfill(status=200, content_type="text/html", body=html)

    def complete(self):
        self.page.goto(self.oauth.authorization_url, wait_until="commit")
        self.flow.wait(6, self.browser_error)
        self.assertEqual(self.oauth.code, "fixture-code")
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.unexpected_requests, [])
        self.assertEqual(self.context.cookies(), [])
        callback_page = self.context.pages[-1]
        self.assertIn("Completing sign-in", callback_page.locator("body").inner_text())
        self.assertNotIn("fixture-password", " ".join(self.stages))
        self.assertNotIn(self.oauth.state, " ".join(self.stages))
        http = Mock()
        http.get_json.return_value = {"access_token": access_token()}
        self.assertEqual(self.oauth.exchange(http), "fixture-subscription")
        form = http.get_json.call_args.kwargs["form"]
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        self.assertEqual(self.authorization_params["code_challenge"], [challenge])
        self.assertEqual(form["redirect_uri"], self.authorization_params["redirect_uri"][0])
        self.assertEqual(form["code"], "fixture-code")
        http.get_json.assert_called_once()

    def test_query_callback_completes_without_homepage_account_scripts_or_cookies(self):
        self.complete()

    def test_fragment_callback_completes_without_account_scripts(self):
        self.fragment_response = True
        self.complete()

    def test_callback_can_arrive_in_a_popup(self):
        self.popup = True
        self.complete()

    def test_manual_browser_sign_in_works_without_app_credentials(self):
        self.flow.username = self.flow.password = None
        self.page.goto(self.oauth.authorization_url)
        self.page.locator('input[name="username"]').fill("fixture@example.test")
        self.page.locator('input[name="password"]').fill("fixture-password")
        self.page.get_by_role("button", name="Sign In", exact=True).last.click()
        self.flow.wait(4, self.browser_error)
        self.assertEqual(self.oauth.code, "fixture-code")
        self.assertEqual(len(self.submissions), 1)

    def test_callback_state_mismatch_cannot_complete_sign_in(self):
        self.wrong_state = True
        self.page.goto(self.oauth.authorization_url, wait_until="commit")
        with self.assertRaisesRegex(AuthenticationError, "state did not match"):
            self.flow.wait(4, self.browser_error)
        self.assertIsNone(self.oauth.code)

    def test_credentials_are_never_filled_on_a_spoofed_identity_provider(self):
        self.page.goto("https://id.formula1.com.attacker.test/login")
        self.flow.fill_identity_form(self.page.main_frame)
        self.assertEqual(self.page.locator('input[name="username"]').input_value(), "")
        self.assertEqual(self.page.locator('input[name="password"]').input_value(), "")

    def test_user_selected_account_is_preserved(self):
        self.page.goto(self.oauth.authorization_url)
        self.page.locator('input[name="username"]').fill("other@example.test")
        self.flow.fill_identity_form(self.page.main_frame)
        self.assertEqual(
            self.page.locator('input[name="username"]').input_value(), "other@example.test"
        )
        self.assertEqual(self.page.locator('input[name="password"]').input_value(), "")
        self.assertEqual(self.submissions, [])

    def test_rejected_password_is_not_replayed_after_url_change(self):
        self.flow.password = "wrong-password"
        self.page.goto(self.oauth.authorization_url, wait_until="commit")
        with self.assertRaises(AuthenticationError):
            self.flow.wait(0.8, self.browser_error)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(urlsplit(self.page.url).fragment, "rejected")

    def test_cancellation_does_not_submit_credentials(self):
        self.flow.cancel = threading.Event()
        self.flow.cancel.set()
        self.page.goto(self.oauth.authorization_url)
        with self.assertRaises(OperationCancelled):
            self.flow.wait(2, self.browser_error)
        self.assertEqual(self.submissions, [])

    def test_closed_sign_in_window_has_an_actionable_error(self):
        self.page.close()
        with self.assertRaisesRegex(AuthenticationError, "browser closed"):
            self.flow.wait(2, self.browser_error)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux dynamic library test")
    def test_conda_library_override_cannot_break_chromium_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "lib"
            library.mkdir()
            (library / "libnss3.so").write_bytes(b"incompatible Conda library")
            environment = {**os.environ, "CONDA_PREFIX": directory, "LD_LIBRARY_PATH": str(library)}
            with self.assertRaises(self.browser_error):
                self.playwright.chromium.launch(
                    executable_path=self.executable, headless=True, env=environment
                )
            with patch.dict(os.environ, environment, clear=True):
                browser, context, page, client = start_browser(
                    self.playwright, self.browser_error, executable=self.executable, headless=True
                )
            try:
                self.assertTrue(page.evaluate("navigator.userAgent").startswith("Mozilla/"))
                self.assertTrue(client.user_agent.startswith("Mozilla/"))
                self.assertEqual(context.cookies(), [])
            finally:
                browser.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop display test")
    def test_real_startup_without_a_display_reports_the_display_problem(self):
        with patch.dict(os.environ, {"DISPLAY": ":98765", "WAYLAND_DISPLAY": "invalid-display"}):
            with self.assertRaisesRegex(AuthenticationError, "desktop display"):
                start_browser(
                    self.playwright, self.browser_error, executable=self.executable, headless=False
                )
