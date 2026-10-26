import base64
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from f1tv_dl.browser import (
    ACCOUNT_HOSTS,
    BrowserFlow,
    access_subscription,
    account_shell_stage,
    browser_error_message,
    cookie_json,
    proxy_options,
    session_response_kind,
    subscription_cookie,
    trusted_surface,
)
from f1tv_dl.errors import OperationCancelled


def claims_token(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class BrowserDataTests(unittest.TestCase):
    def test_current_identity_provider_is_trusted_but_spoofed_origins_are_not(self):
        self.assertTrue(trusted_surface("https://id.formula1.com/as/authorize", ACCOUNT_HOSTS))
        for url in (
            "https://id.formula1.com.attacker.test/login",
            "http://id.formula1.com/login",
            "https://id.formula1.com:9000/login",
            "https://user@id.formula1.com/login",
            "https://id.formula1.com:bad/login",
            "about:blank",
        ):
            with self.subTest(url=url):
                self.assertFalse(trusted_surface(url, ACCOUNT_HOSTS))

    def test_session_cookie_can_be_encoded_more_than_once(self):
        payload = {"data": {"subscriptionToken": "subscription"}}
        value = quote(quote(json.dumps(payload)))
        self.assertEqual(cookie_json(value), payload)
        self.assertEqual(
            subscription_cookie({"name": "login-session", "value": value}), "subscription"
        )

    def test_modern_auth_cookie_contains_a_nested_subscription_token(self):
        value = quote(
            json.dumps({"accessToken": claims_token({"subscriptionToken": "subscription"})})
        )
        self.assertEqual(
            subscription_cookie({"name": "auth-session", "value": value}), "subscription"
        )

    def test_malformed_cookies_are_ignored(self):
        for name, value in (
            ("login-session", "bad"),
            ("auth-session", "{}"),
            ("auth-session", '{"accessToken":"bad.jwt.parts"}'),
        ):
            with self.subTest(name=name, value=value):
                self.assertIsNone(subscription_cookie({"name": name, "value": value}))

    def test_oauth_access_token_is_only_used_for_its_nested_subscription(self):
        self.assertEqual(
            access_subscription(claims_token({"subscriptionToken": "subscription"})),
            "subscription",
        )
        for value in (None, "opaque-access-token", claims_token({"sub": "account-id"})):
            self.assertIsNone(access_subscription(value))

    def test_only_official_session_responses_are_observed(self):
        self.assertEqual(session_response_kind("https://id.formula1.com/as/token"), "oauth")
        self.assertEqual(
            session_response_kind(
                "https://f1tv.formula1.com/2.0/R/ENG/WEB_DASH/ALL/USER/ENTITLEMENT"
            ),
            "entitlement",
        )
        for url in (
            "https://id.formula1.com.attacker.test/as/token",
            "http://id.formula1.com/as/token",
            "https://id.formula1.com/as/authorize",
            "https://account.formula1.com/as/token",
            "https://id.formula1.com:9000/as/token",
        ):
            self.assertIsNone(session_response_kind(url))

    def test_only_the_account_login_shell_is_eligible_for_recovery(self):
        self.assertEqual(account_shell_stage("https://account.formula1.com/#/en/login?"), "login")
        self.assertEqual(
            account_shell_stage(
                "https://account.formula1.com/?code=fixture&state=fixture#/en/login"
            ),
            "callback",
        )
        self.assertEqual(
            account_shell_stage(
                "https://account.formula1.com/#/en/login?code=fixture&state=fixture"
            ),
            "callback",
        )
        for url in (
            "https://id.formula1.com/as/authorize",
            "https://account.formula1.com/#/en/my-account",
            "https://account.formula1.com/verify",
            "https://account.formula1.com.attacker.test/#/en/login",
        ):
            self.assertIsNone(account_shell_stage(url))

    def test_proxy_credentials_are_separate_from_the_server_url(self):
        with patch.dict(
            os.environ, {"https_proxy": "http://user:p%40ss@proxy.test:8080"}, clear=True
        ):
            self.assertEqual(
                proxy_options(),
                {"server": "http://proxy.test:8080", "username": "user", "password": "p@ss"},
            )

    def test_browser_errors_explain_trust_without_exposing_private_diagnostics(self):
        message = browser_error_message(
            Exception("net::ERR_CERT_AUTHORITY_INVALID https://private/?token=secret")
        )
        self.assertIn("certificate", message)
        self.assertNotIn("private", message)
        self.assertNotIn("secret", message)

    def test_cancelled_login_does_not_inspect_cookies(self):
        context = Mock()
        cancel = threading.Event()
        cancel.set()
        flow = BrowserFlow(context, Mock(), "user", "password", cancel=cancel)
        with self.assertRaises(OperationCancelled):
            flow.wait(10, Exception)
        context.cookies.assert_not_called()
