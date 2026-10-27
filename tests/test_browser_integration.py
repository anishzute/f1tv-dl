"""Real Chromium tests using intercepted fixture pages; never contact F1 TV."""

import base64
import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, quote, urlsplit

from f1tv_dl.browser import ACCOUNT_LOGIN_URL, BrowserFlow
from f1tv_dl.errors import APIError, AuthenticationError

AVAILABLE = importlib.util.find_spec("playwright") is not None
HOME = "https://f1tv.formula1.com/"
ACCOUNT = "https://account.formula1.com/login"
IDP = "https://id.formula1.com/as/authorize"
FINISH = "https://f1tv.formula1.com/signed-in"
# F1's configured OAuth redirect omits the locale, including the doubled slash.
CALLBACK = "https://account.formula1.com/?code=fixture&state=fixture#//login/"
ACCESS_CLAIMS = (
    base64.urlsafe_b64encode(json.dumps({"subscriptionToken": "fixture-subscription"}).encode())
    .decode()
    .rstrip("=")
)
ACCESS_TOKEN = f"header.{ACCESS_CLAIMS}.signature"


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
        self.account_requests = 0
        self.callback_requests = 0
        self.account_stalls = 0
        self.callback_stalls = 0
        self.callback_token = False
        self.oauth_stalls = False
        self.pending_routes = []
        self.finish = FINISH
        self.context.route("**/*", self.respond)

    def respond(self, route):
        request = route.request
        host, path = urlsplit(request.url).hostname, urlsplit(request.url).path
        if host == "account.formula1.com" and path == "/blocked-script.js":
            self.pending_routes.append(route)
            return
        elif host == "id.formula1.com" and path == "/as/token":
            if self.oauth_stalls:
                self.pending_routes.append(route)
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "https://account.formula1.com"},
                body=json.dumps({"access_token": ACCESS_TOKEN}),
            )
            return
        elif (
            host == "account.formula1.com"
            and path == "/"
            and "code" in parse_qs(urlsplit(request.url).query)
        ):
            self.callback_requests += 1
            html = '<div>F1</div><script src="/blocked-script.js"></script>'
            if self.callback_requests > self.callback_stalls and self.callback_token:
                html = (
                    '<div>F1</div><script>fetch("https://id.formula1.com/as/token",'
                    '{method:"POST",body:new URLSearchParams({grant_type:"authorization_code",'
                    'code:"fixture"})})</script><script src="/blocked-script.js"></script>'
                )
        elif host == "consent.formula1.com":
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
            self.account_requests += 1
            if self.account_requests <= self.account_stalls:
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<div>F1</div><script src="/blocked-script.js"></script>',
                )
                return
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
                f'if(result.ok)location.href="{self.finish}";'
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

    def login(self, start=HOME, **options):
        page = self.context.new_page()
        stages = []
        exchange = Mock()
        flow = BrowserFlow(
            self.context,
            exchange,
            "fixture@example.test",
            "fixture-password",
            status=stages.append,
            **options,
        )
        page.goto(start, wait_until="commit")
        token = flow.wait(8, self.browser_error)
        self.assertEqual(token, "fixture-entitlement")
        self.assertEqual(len(self.submissions), 1)
        self.assertIn("/second", self.choices)
        self.assertNotIn("fixture-password", " ".join(stages))
        exchange.assert_not_called()
        return flow, stages

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

    def test_stalled_initial_loading_screen_is_refreshed_once(self):
        self.account_stalls = 1
        flow, stages = self.login(ACCOUNT_LOGIN_URL, stall_timeout=0.2)
        self.assertEqual(self.account_requests, 2)
        self.assertEqual(len(flow.refreshed_shells), 1)
        self.assertTrue(any("Refreshing" in stage for stage in stages))

    def test_an_unrecoverable_loading_screen_is_not_refreshed_in_a_loop(self):
        self.account_stalls = 10
        page = self.context.new_page()
        flow = BrowserFlow(self.context, Mock(), None, None, stall_timeout=0.1)
        page.goto(ACCOUNT_LOGIN_URL, wait_until="commit")
        with self.assertRaisesRegex(AuthenticationError, "loading screen after a refresh"):
            flow.wait(1, self.browser_error)
        self.assertEqual(self.account_requests, 2)

    def test_stalled_callback_finishes_from_oauth_without_waiting_for_cookies_or_load(self):
        self.finish = CALLBACK
        self.callback_stalls = 1
        self.callback_token = True
        page = self.context.new_page()
        exchange = Mock(return_value="fixture-entitlement")
        flow = BrowserFlow(
            self.context, exchange, "fixture@example.test", "fixture-password", stall_timeout=0.2
        )
        page.goto(IDP, wait_until="commit")
        self.assertEqual(flow.wait(8, self.browser_error), "fixture-entitlement")
        self.assertEqual(self.callback_requests, 2)
        self.assertEqual(len(self.submissions), 1)
        exchange.assert_called_once_with("fixture-subscription")
        self.assertNotEqual(page.evaluate("document.readyState"), "complete")
        self.assertEqual(page.locator("body").inner_text(), "F1")
        self.assertEqual(self.context.cookies([HOME, ACCOUNT_LOGIN_URL]), [])

    def test_an_in_flight_oauth_exchange_is_not_reloaded_or_replayed(self):
        self.callback_token = True
        self.oauth_stalls = True
        page = self.context.new_page()
        exchange = Mock()
        flow = BrowserFlow(self.context, exchange, None, None, stall_timeout=0.1)
        page.goto(CALLBACK, wait_until="commit")
        with self.assertRaises(AuthenticationError):
            flow.wait(0.8, self.browser_error)
        self.assertTrue(flow.token_exchange_started)
        self.assertEqual(self.callback_requests, 1)
        self.assertEqual(flow.refreshed_shells, set())
        exchange.assert_not_called()

    def test_account_scoped_session_is_detected_without_an_f1tv_cookie(self):
        value = quote(json.dumps({"accessToken": ACCESS_TOKEN}))
        self.context.add_cookies([{"name": "auth-session", "value": value, "url": ACCOUNT}])
        self.assertEqual(self.context.cookies(HOME), [])
        exchange = Mock(return_value="fixture-entitlement")
        flow = BrowserFlow(self.context, exchange, None, None)
        self.assertEqual(flow.session_token(self.browser_error), "fixture-entitlement")
        exchange.assert_called_once_with("fixture-subscription")

    def test_account_storage_session_is_detected_while_its_page_still_shows_the_logo(self):
        page = self.context.new_page()
        page.goto(ACCOUNT_LOGIN_URL)
        page.set_content("<div>F1</div>")
        page.evaluate(
            "value => localStorage.setItem('ascSessionInfo', JSON.stringify(value))",
            {"data": {"subscriptionToken": "fixture-subscription"}},
        )
        exchange = Mock(return_value="fixture-entitlement")
        flow = BrowserFlow(self.context, exchange, None, None)
        self.assertEqual(flow.wait(2, self.browser_error), "fixture-entitlement")
        exchange.assert_called_once_with("fixture-subscription")

    def test_account_verification_screen_is_preserved(self):
        page = self.context.new_page()
        page.goto(ACCOUNT_LOGIN_URL)
        page.set_content("<h1>Verify your account</h1><button>Continue</button>")
        flow = BrowserFlow(self.context, Mock(), None, None, stall_timeout=0.1)
        with self.assertRaises(AuthenticationError):
            flow.wait(0.8, self.browser_error)
        self.assertEqual(page.locator("h1").inner_text(), "Verify your account")
        self.assertEqual(flow.refreshed_shells, set())

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
