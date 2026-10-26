"""Real Chromium tests using intercepted fixture pages; never contact F1 TV."""

import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote, urlsplit

from f1tv_dl.browser import ACCOUNT_LOGIN_URL, BrowserFlow
from f1tv_dl.errors import APIError, AuthenticationError

AVAILABLE = importlib.util.find_spec("playwright") is not None
HOME = "https://f1tv.formula1.com/"
ACCOUNT = "https://account.formula1.com/login"
IDP = "https://id.formula1.com/as/authorize"
FINISH = "https://f1tv.formula1.com/signed-in"


@unittest.skipUnless(AVAILABLE, "Optional Playwright browser extra is not installed")
class BrowserFlowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import Error, sync_playwright

        cls.browser_error = Error
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        if Path(cls.playwright.chromium.executable_path).is_file():
            executable = cls.playwright.chromium.executable_path
        else:
            executable = shutil.which("chromium") or shutil.which("google-chrome")
        if not executable:
            raise unittest.SkipTest("Chromium is not installed; run playwright install chromium")
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.submissions = []
        self.choices = []
        self.popup = False
        self.home_requests = 0
        self.context.route("**/*", self.respond)

    def respond(self, route):
        request = route.request
        host, path = urlsplit(request.url).hostname, urlsplit(request.url).path
        if host == "consent.formula1.com":
            self.choices.append(path)
            html = (
                "<button onclick=\"parent.postMessage('essential','*')\">"
                "Essential only cookies</button>"
            )
        elif request.url == HOME:
            self.home_requests += 1
            target = 'target="_blank"' if self.popup else ""
            html = (
                f'<a href="{ACCOUNT}" {target}>Sign in</a>'
                '<iframe id="consent" src="https://consent.formula1.com/first" '
                'style="position:fixed;inset:0;width:100%;height:100%;z-index:10"></iframe>'
                '<script>onmessage=e=>{if(e.data==="essential")'
                'document.querySelector("iframe").remove()}</script>'
            )
        elif request.url in (ACCOUNT, "https://account.formula1.com/"):
            html = (
                '<iframe src="https://consent.formula1.com/second" '
                'style="position:fixed;inset:0;width:100%;height:100%;z-index:10"></iframe>'
                f'<script>onmessage=e=>{{if(e.data==="essential")location.href="{IDP}"}}</script>'
            )
        elif request.url == IDP:
            # Current F1 SSO has a header Sign In button and a form Sign In button.
            html = (
                '<button type="button">Sign In</button><form>'
                '<input name="username" autocomplete="username">'
                '<input name="password" type="password" autocomplete="current-password">'
                '<button type="button" onclick="signIn()">Sign In</button></form>'
                "<script>async function signIn(){"
                'const result=await fetch("/authenticate",{method:"POST",body:JSON.stringify({'
                'username:document.querySelector("input[name=username]").value,'
                'password:document.querySelector("input[name=password]").value})});'
                f'if(result.ok)location.href="{FINISH}";'
                'else location.hash="rejected";}</script>'
            )
        elif host == "id.formula1.com" and path == "/authenticate":
            self.submissions.append(request.post_data_json)
            valid = request.post_data_json == {
                "username": "fixture@example.test",
                "password": "fixture-password",
            }
            route.fulfill(status=200 if valid else 401, body="{}", content_type="application/json")
            return
        elif request.url == FINISH:
            html = (
                '<script>document.cookie="entitlement_token=fixture-entitlement; '
                'Path=/; Secure"</script>Signed in'
            )
        elif path == "/waiting":
            html = (
                '<script>setTimeout(()=>{document.cookie="entitlement_token=fixture-entitlement; '
                'Path=/; Secure"},400)</script>'
            )
        elif host == "id.formula1.com.attacker.test":
            html = (
                '<input name="username"><input name="password" type="password">'
                "<button>Sign In</button>"
            )
        else:
            route.fulfill(status=404, body="No external requests are allowed in these tests")
            return
        route.fulfill(status=200, body=html, content_type="text/html")

    def login(self, start=HOME):
        page = self.context.new_page()
        page.goto(start)
        stages = []
        exchange = Mock()
        flow = BrowserFlow(
            self.context, exchange, "fixture@example.test", "fixture-password", status=stages.append
        )
        token = flow.wait(8, self.browser_error)
        self.assertEqual(token, "fixture-entitlement")
        self.assertEqual(len(self.submissions), 1)
        self.assertIn("/second", self.choices)
        self.assertNotIn("fixture-password", " ".join(stages))
        exchange.assert_not_called()

    def test_two_consent_frames_and_modern_identity_provider_complete_sign_in(self):
        self.login()
        self.assertIn("/first", self.choices)

    def test_direct_account_sign_in_completes_without_loading_the_homepage(self):
        self.login(ACCOUNT_LOGIN_URL)
        self.assertEqual(self.home_requests, 0)
        self.assertNotIn("/first", self.choices)

    def test_sign_in_can_complete_in_a_popup(self):
        self.popup = True
        self.login()

    def test_credentials_are_not_filled_on_a_spoofed_identity_provider(self):
        page = self.context.new_page()
        page.goto("https://id.formula1.com.attacker.test/login")
        flow = BrowserFlow(self.context, Mock(), "fixture@example.test", "fixture-password")
        flow.advance(page)
        self.assertEqual(page.locator('input[name="username"]').input_value(), "")
        self.assertEqual(page.locator('input[name="password"]').input_value(), "")

    def test_rejected_credentials_are_not_submitted_again_after_a_url_change(self):
        page = self.context.new_page()
        page.goto(IDP)
        flow = BrowserFlow(self.context, Mock(), "fixture@example.test", "wrong-password")
        with self.assertRaises(AuthenticationError):
            flow.wait(1.5, self.browser_error)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(urlsplit(page.url).fragment, "rejected")

    def test_user_selected_account_is_not_overwritten_or_submitted(self):
        page = self.context.new_page()
        page.goto(IDP)
        page.locator('input[name="username"]').fill("other@example.test")
        flow = BrowserFlow(self.context, Mock(), "fixture@example.test", "fixture-password")
        flow.advance(page)
        self.assertEqual(page.locator('input[name="username"]').input_value(), "other@example.test")
        self.assertEqual(page.locator('input[name="password"]').input_value(), "")
        self.assertEqual(self.submissions, [])

    def test_exchange_failure_does_not_close_browser_before_the_site_finishes(self):
        page = self.context.new_page()
        value = quote(json.dumps({"data": {"subscriptionToken": "fixture-subscription"}}))
        self.context.add_cookies([{"name": "login-session", "value": value, "url": HOME}])
        page.goto(HOME + "waiting")
        exchange = Mock(side_effect=APIError("Entitlement lookup", 403))
        flow = BrowserFlow(self.context, exchange, None, None)
        self.assertEqual(flow.wait(4, self.browser_error), "fixture-entitlement")
        exchange.assert_called_once_with("fixture-subscription")
