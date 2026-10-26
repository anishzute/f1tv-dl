import base64
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock, patch

from test_models import FIXTURE, URL

from f1tv_dl.api import F1TVClient, result_object
from f1tv_dl.auth import Authenticator, SessionManager, SessionStore, token_is_fresh
from f1tv_dl.errors import APIError, AuthenticationError, DRMError, F1TVError
from f1tv_dl.models import Content, ContentRef


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class AuthTests(unittest.TestCase):
    def test_token_expiration_and_malformed_values(self):
        self.assertTrue(token_is_fresh(jwt(time.time() + 3600)))
        self.assertTrue(token_is_fresh("opaque-token"))
        for token in (
            jwt(time.time() - 5),
            jwt(time.time() + 20),
            "bad.jwt.parts",
            "",
            "a\nb",
            None,
        ):
            with self.subTest(token=token):
                self.assertFalse(token_is_fresh(token))

    def test_atomic_cache_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache" / "session.json"
            store = SessionStore(path)
            self.assertIsNone(store.load())
            store.save("first-token")
            store.save("second-token")
            self.assertEqual(store.load(), "second-token")
            self.assertEqual(list(path.parent.iterdir()), [path])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_expired_or_missing_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            for text in ("not JSON", "[]", "{}", json.dumps({"token": jwt(0)})):
                with self.subTest(text=text):
                    path.write_text(text)
                    self.assertIsNone(SessionStore(path).load())

    def test_no_cache_never_reads_or_writes(self):
        store = SessionStore(None)
        store.save("token")
        self.assertIsNone(store.load())

    def test_password_login_exchanges_subscription_for_entitlement(self):
        http = Mock()
        http.get_json.side_effect = [
            {"data": {"subscriptionToken": "subscription"}},
            {"resultObj": {"entitlementToken": "entitlement"}},
        ]
        self.assertEqual(Authenticator(http).password_login("user", "secret"), "entitlement")
        self.assertEqual(
            http.get_json.call_args_list[0].kwargs["body"], {"Login": "user", "Password": "secret"}
        )
        self.assertEqual(
            http.get_json.call_args_list[1].kwargs["headers"], {"ascendontoken": "subscription"}
        )

    def test_login_403_is_actionable_and_does_not_leak_password(self):
        http = Mock()
        http.get_json.side_effect = APIError("Password login", 403)
        with self.assertRaises(AuthenticationError) as caught:
            Authenticator(http).password_login("private-user", "private-password")
        self.assertIn("--login-method browser", str(caught.exception))
        self.assertNotIn("private-", str(caught.exception))
        self.assertEqual(http.get_json.call_count, 1)

    def test_login_missing_token_is_an_error(self):
        http = Mock()
        http.get_json.return_value = {"data": {}}
        with self.assertRaises(AuthenticationError):
            Authenticator(http).password_login("user", "secret")

    def test_explicit_token_is_never_cached_or_replaced(self):
        store, login = Mock(), Mock()
        session = SessionManager(store, login, token="explicit")
        self.assertEqual(session.get(), "explicit")
        with self.assertRaises(AuthenticationError):
            session.refresh()
        store.load.assert_not_called()
        store.save.assert_not_called()
        login.assert_not_called()

    def test_cached_session_and_refresh(self):
        store, login = Mock(), Mock(return_value="fresh")
        store.load.return_value = "cached"
        session = SessionManager(store, login)
        self.assertEqual(session.get(), "cached")
        self.assertEqual(session.refresh(), "fresh")
        store.save.assert_called_once_with("fresh")

    def test_invalid_explicit_token_fails_early(self):
        with self.assertRaises(AuthenticationError):
            SessionManager(Mock(), Mock(), token=jwt(0))


class BrowserAuthTests(unittest.TestCase):
    def setUp(self):
        self.error = type("BrowserError", (Exception,), {})
        self.browser = Mock()
        self.context = self.browser.new_context.return_value
        self.context.pages = []
        self.page = self.context.new_page.return_value
        self.driver = Mock()
        self.driver.chromium.launch.return_value = self.browser
        manager = MagicMock()
        manager.__enter__.return_value = self.driver
        api = ModuleType("playwright.sync_api")
        api.Error = self.error
        self.timeout_error = type("BrowserTimeout", (self.error,), {})
        api.TimeoutError = self.timeout_error
        api.sync_playwright = Mock(return_value=manager)
        modules = {"playwright": ModuleType("playwright"), "playwright.sync_api": api}
        self.module_patch = patch.dict("sys.modules", modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def test_browser_captures_entitlement_and_always_closes(self):
        self.context.cookies.return_value = [
            {"name": "entitlement_token", "value": "browser-token"}
        ]
        token = Authenticator(Mock()).browser_login(headless=True, executable="chromium")
        self.assertEqual(token, "browser-token")
        self.assertEqual(
            self.page.goto.call_args.args[0], "https://account.formula1.com/#/en/login?"
        )
        self.browser.close.assert_called_once()
        options = self.driver.chromium.launch.call_args.kwargs
        self.assertTrue(options["headless"])
        self.assertNotIn("ignore_https_errors", self.browser.new_context.call_args.kwargs)

    def test_browser_subscription_cookie_is_exchanged(self):
        self.context.cookies.return_value = [
            {
                "name": "login-session",
                "value": json.dumps({"data": {"subscriptionToken": "subscription"}}),
            }
        ]
        http = Mock()
        http.get_json.return_value = {"resultObj": {"entitlementToken": "entitlement"}}
        self.assertEqual(Authenticator(http).browser_login(), "entitlement")
        self.assertEqual(
            http.get_json.call_args.kwargs["headers"], {"ascendontoken": "subscription"}
        )
        self.browser.close.assert_called_once()

    def test_browser_failure_closes_and_hides_sensitive_diagnostics(self):
        self.page.goto.side_effect = self.error("failure with private-cookie")
        with self.assertRaises(AuthenticationError) as caught:
            Authenticator(Mock()).browser_login()
        self.assertNotIn("private-cookie", str(caught.exception))
        self.browser.close.assert_called_once()

    def test_initial_navigation_timeout_retries_the_account_address_once(self):
        self.page.goto.side_effect = [self.timeout_error("private-url"), None]
        self.context.cookies.return_value = [{"name": "entitlement_token", "value": "session"}]
        self.assertEqual(Authenticator(Mock()).browser_login(), "session")
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(self.page.goto.call_args_list[0], self.page.goto.call_args_list[1])
        self.assertEqual(self.page.goto.call_args.kwargs["wait_until"], "commit")


class APITests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.client = F1TVClient(self.http)
        self.content = Content.from_response(ContentRef.parse(URL), FIXTURE)
        self.sessions = Mock()
        self.sessions.get.return_value = "cached"
        self.sessions.refresh.return_value = "fresh"
        self.playback = {
            "resultCode": "OK",
            "resultObj": {"url": "https://cdn.test/a.m3u8", "streamType": "HLS"},
        }

    def test_metadata_lookup_preserves_correct_content_id(self):
        self.http.get_json.return_value = FIXTURE
        self.assertEqual(self.client.content(URL).ref.id, "1000010409")
        self.assertEqual(self.http.get_json.call_args.kwargs["params"]["contentId"], "1000010409")

    def test_international_request_has_no_channel_id(self):
        self.http.get_json.return_value = self.playback
        self.client.playback(
            self.content, self.content.select_channel("INTERNATIONAL"), self.sessions
        )
        self.assertNotIn("channelId", self.http.get_json.call_args.kwargs["params"])

    def test_driver_request_uses_channel_id(self):
        self.http.get_json.return_value = self.playback
        self.client.playback(self.content, self.content.select_channel("VER"), self.sessions)
        self.assertEqual(self.http.get_json.call_args.kwargs["params"]["channelId"], "1009")

    def test_401_refreshes_once(self):
        self.http.get_json.side_effect = [APIError("Playback", 401), self.playback]
        self.client.playback(self.content, None, self.sessions)
        self.sessions.refresh.assert_called_once()
        self.assertEqual(
            self.http.get_json.call_args.kwargs["headers"]["entitlementToken"], "fresh"
        )

    def test_second_401_is_not_retried(self):
        self.http.get_json.side_effect = APIError("Playback", 401)
        with self.assertRaises(APIError):
            self.client.playback(self.content, None, self.sessions)
        self.assertEqual(self.http.get_json.call_count, 2)
        self.sessions.refresh.assert_called_once()

    def test_403_does_not_retry_password(self):
        self.http.get_json.side_effect = APIError("Playback", 403)
        with self.assertRaisesRegex(F1TVError, "subscription, region"):
            self.client.playback(self.content, None, self.sessions)
        self.sessions.refresh.assert_not_called()

    def test_drm_and_unsupported_or_invalid_playback(self):
        for fields, exception in (
            ({"drmType": "widevine"}, DRMError),
            ({"streamType": "OTHER"}, F1TVError),
            ({"url": "file:///etc/passwd"}, F1TVError),
            ({"url": None}, F1TVError),
        ):
            with self.subTest(fields=fields):
                self.http.get_json.return_value = {"resultObj": self.playback["resultObj"] | fields}
                with self.assertRaises(exception):
                    self.client.playback(self.content, None, self.sessions)

    def test_api_error_envelope_is_not_success(self):
        with self.assertRaises(F1TVError):
            result_object({"resultCode": "ERROR", "resultObj": {}})
