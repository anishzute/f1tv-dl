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
from f1tv_dl.auth import (
    Authenticator,
    SessionCredentials,
    SessionManager,
    SessionStore,
    token_is_fresh,
)
from f1tv_dl.errors import APIError, AuthenticationError, DRMError, F1TVError
from f1tv_dl.models import Content, ContentRef
from f1tv_dl.web import WebClient


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

    def test_browser_credentials_roundtrip_without_exposing_tokens_in_repr(self):
        credentials = SessionCredentials("fixture-entitlement", "fixture-subscription")
        self.assertNotIn("fixture-", repr(credentials))
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.json")
            store.save(credentials)
            self.assertEqual(store.load(), credentials)
            session = SessionManager(store, Mock())
            self.assertEqual(session.get(), "fixture-entitlement")
            self.assertEqual(session.subscription_token, "fixture-subscription")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)

    def test_expired_subscription_cache_requires_a_fresh_login(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.json")
            store.save(SessionCredentials("fixture-entitlement", jwt(0)))
            self.assertIsNone(store.load())

    def test_session_refresh_replaces_both_browser_tokens(self):
        store, login = Mock(), Mock()
        store.load.return_value = SessionCredentials("old-entitlement", "old-subscription")
        login.return_value = SessionCredentials("new-entitlement", "new-subscription")
        session = SessionManager(store, login)
        self.assertEqual(session.refresh(), "new-entitlement")
        self.assertEqual(session.subscription_token, "new-subscription")
        store.save.assert_called_once_with(login.return_value)

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
        self.page = self.context.new_page.return_value
        self.driver = Mock()
        self.driver.chromium.launch.return_value = self.browser
        manager = MagicMock()
        manager.__enter__.return_value = self.driver
        api = ModuleType("playwright.sync_api")
        api.Error = self.error
        api.sync_playwright = Mock(return_value=manager)
        modules = {"playwright": ModuleType("playwright"), "playwright.sync_api": api}
        module_patch = patch.dict("sys.modules", modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.oauth = Mock()
        self.oauth.authorization_url = "https://id.formula1.com/as/authorize?state=fixture"
        discovery = patch("f1tv_dl.browser.OAuthSession.discover", return_value=self.oauth)
        discovery.start()
        self.addCleanup(discovery.stop)
        flow = patch("f1tv_dl.browser.BrowserAuthorization")
        self.flow = flow.start()
        self.addCleanup(flow.stop)

    def test_direct_authorization_closes_chromium_before_exchanging_the_code(self):
        def redeem(http):
            self.browser.close.assert_called_once()
            return "subscription"

        self.oauth.exchange.side_effect = redeem
        http = Mock()
        http.get_json.return_value = {"resultObj": {"entitlementToken": "entitlement"}}
        token = Authenticator(http).browser_login(headless=True, executable="chromium")
        self.assertEqual(token.entitlement_token, "entitlement")
        self.assertEqual(token.subscription_token, "subscription")
        self.assertIsNotNone(token.web_client)
        self.assertEqual(self.page.goto.call_args.args[0], self.oauth.authorization_url)
        self.assertEqual(self.page.goto.call_args.kwargs["wait_until"], "commit")
        self.context.cookies.assert_not_called()
        self.assertNotIn("ignore_https_errors", self.browser.new_context.call_args.kwargs)
        options = self.driver.chromium.launch.call_args.kwargs
        self.assertTrue(options["headless"])
        self.assertEqual(options["executable_path"], "chromium")
        self.oauth.exchange.assert_called_once_with(http)
        self.assertIn("/WEB_DASH/", http.get_json.call_args.args[0])

    def test_browser_failure_closes_and_hides_sensitive_diagnostics(self):
        self.page.goto.side_effect = self.error("failure with private-authorization-code")
        with self.assertRaises(AuthenticationError) as caught:
            Authenticator(Mock()).browser_login()
        self.assertNotIn("private-", str(caught.exception))
        self.browser.close.assert_called_once()
        self.oauth.exchange.assert_not_called()


class APITests(unittest.TestCase):
    def test_android_tv_uses_the_successful_probe_profile_and_matching_uhd_feed(self):
        context = WebClient.create("Mozilla/5.0 Chrome/154.0.0.0")
        sessions = SessionManager(
            SessionStore(None),
            Mock(),
            token=SessionCredentials("entitlement", "subscription", context),
        )
        self.http.get_json.return_value = {
            "resultObj": {
                "streamType": "HDR_UHD_CMAF",
                "url": "https://cdn.test/default-hd.m3u8",
                "tme": {
                    "feeds": [
                        {"metadata": {"channel_id": 1009}, "url": "https://cdn.test/driver.m3u8"},
                        {"metadata": {"channel_id": 1025}, "url": "https://cdn.test/uhd.m3u8"},
                    ]
                },
            }
        }
        playback = self.client.playback(
            self.content,
            self.content.select_channel("INTERNATIONAL"),
            sessions,
            profile="android-tv",
        )
        self.assertEqual(playback.url, "https://cdn.test/uhd.m3u8")
        self.assertEqual(playback.stream_type, "HLS")
        call = self.http.get_json.call_args
        self.assertIn("/3.0/R/ENG/WEB_HLS/", call.args[0])
        self.assertNotIn("channelId", call.kwargs["params"])
        headers = call.kwargs["headers"]
        self.assertEqual(headers["ascendontoken"], "subscription")
        self.assertEqual(headers["entitlementToken"], "entitlement")
        self.assertIn("device=android_tv;", headers["x-f1-device-info"])
        self.assertEqual(headers["User-Agent"], context.user_agent)
        self.assertIn("device=web;", context.device_info)
        self.assertEqual(context.headers()["x-f1-device-info"], context.device_info)

    def test_android_tv_survives_a_single_token_refresh_and_still_rejects_drm(self):
        self.sessions.subscription_token = "subscription"
        self.sessions.web_client = WebClient.create()
        self.http.get_json.side_effect = [APIError("Playback", 401), self.playback]
        self.client.playback(self.content, None, self.sessions, profile="android-tv")
        self.sessions.refresh.assert_called_once()
        for call in self.http.get_json.call_args_list:
            self.assertIn("device=android_tv;", call.kwargs["headers"]["x-f1-device-info"])
        self.http.get_json.side_effect = None
        self.http.get_json.return_value = {
            "resultObj": {
                "streamType": "HDR_UHD_CMAF",
                "drmType": "Widevine",
                "url": "https://cdn.test/protected.m3u8",
            }
        }
        with self.assertRaises(DRMError):
            self.client.playback(self.content, None, self.sessions, profile="android-tv")

    def test_invalid_profile_and_legacy_android_tv_login_fail_before_playback_request(self):
        for profile in ("invalid", "android-tv"):
            with self.subTest(profile=profile), self.assertRaises(F1TVError):
                self.client.playback(self.content, None, self.sessions, profile=profile)
        self.http.get_json.assert_not_called()

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

    def test_native_browser_session_uses_recorded_web_playback_and_selects_its_feed(self):
        sessions = SessionManager(
            SessionStore(None), Mock(), token=SessionCredentials("entitlement", "subscription")
        )
        self.http.get_json.return_value = {
            "resultCode": "OK",
            "resultObj": {
                "streamType": "SDR_HD_CMAF",
                "channelId": 1009,
                "tme": {
                    "feeds": [
                        {"metadata": {"channel_id": 999}, "url": "https://cdn.test/other.m3u8"},
                        {"metadata": {"channel_id": 1009}, "url": "https://cdn.test/selected.m3u8"},
                    ]
                },
            },
        }
        playback = self.client.playback(self.content, self.content.select_channel("VER"), sessions)
        self.assertEqual(playback.url, "https://cdn.test/selected.m3u8")
        self.assertEqual(playback.stream_type, "HLS")
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "web-auth-protocol.json").read_text()
        )
        call = self.http.get_json.call_args
        self.assertTrue(call.args[0].endswith(fixture["playback_path"]))
        self.assertEqual(call.kwargs["params"]["player"], fixture["playback_player"])
        self.assertEqual(
            {
                k.lower(): v
                for k, v in call.kwargs["headers"].items()
                if k.lower() in {"ascendontoken", "entitlementtoken"}
            },
            {"ascendontoken": "subscription", "entitlementtoken": "entitlement"},
        )

    def test_web_playback_does_not_substitute_a_different_channel(self):
        sessions = SessionManager(
            SessionStore(None), Mock(), token=SessionCredentials("entitlement", "subscription")
        )
        self.http.get_json.return_value = {
            "resultObj": {
                "streamType": "SDR_HD_CMAF",
                "channelId": 1009,
                "tme": {
                    "feeds": [{"metadata": {"channel_id": 999}, "url": "https://cdn.test/x.m3u8"}]
                },
            }
        }
        with self.assertRaisesRegex(F1TVError, "selected feed"):
            self.client.playback(self.content, self.content.select_channel("VER"), sessions)

    def test_web_international_uses_content_route_and_explicit_returned_feed_id(self):
        channel = self.content.select_channel("INTERNATIONAL")
        self.assertIsNone(channel.channel_id)
        self.assertIsNotNone(channel.web_channel_id)
        sessions = SessionManager(
            SessionStore(None), Mock(), token=SessionCredentials("entitlement", "subscription")
        )
        self.http.get_json.return_value = {
            "resultObj": {
                "streamType": "SDR_HD_CMAF",
                "channelId": 1033,
                "tme": {
                    "feeds": [
                        {
                            "metadata": {"channel_id": 1033},
                            "url": "https://cdn.test/wrong-default.m3u8",
                        },
                        {
                            "metadata": {"channel_id": channel.web_channel_id},
                            "url": "https://cdn.test/international.m3u8",
                        },
                    ]
                },
            }
        }
        payload = self.http.get_json.return_value

        def authorize(url, **kwargs):
            if "channelId" in kwargs["params"]:
                raise APIError("Playback authorization", 404)
            return payload

        self.http.get_json.side_effect = authorize
        for profile in ("web", "android-tv"):
            with self.subTest(profile=profile):
                self.http.get_json.reset_mock()
                result = self.client.playback(self.content, channel, sessions, profile=profile)
                self.assertEqual(result.url, "https://cdn.test/international.m3u8")
                self.assertNotIn("channelId", self.http.get_json.call_args.kwargs["params"])
                self.assertEqual(self.http.get_json.call_count, 1)

    def test_missing_international_content_route_uses_default_feed_list_once(self):
        sessions = SessionManager(
            SessionStore(None),
            Mock(),
            token=SessionCredentials("entitlement", "subscription"),
        )
        channel = self.content.select_channel("INTERNATIONAL")
        self.http.get_json.side_effect = [
            APIError("Playback authorization", 404),
            {
                "resultObj": {
                    "streamType": "HDR_UHD_CMAF",
                    "channelId": 1033,
                    "url": "https://cdn.test/wrong-default.m3u8",
                    "tme": {
                        "feeds": [
                            {
                                "metadata": {"channel_id": 1033},
                                "url": "https://cdn.test/wrong.m3u8",
                            },
                            {
                                "metadata": {"channel_id": 1025},
                                "url": "https://cdn.test/international.m3u8",
                            },
                        ]
                    },
                }
            },
        ]
        playback = self.client.playback(self.content, channel, sessions, profile="android-tv")
        self.assertEqual(playback.url, "https://cdn.test/international.m3u8")
        calls = self.http.get_json.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotIn("channelId", calls[0].kwargs["params"])
        self.assertEqual(calls[1].kwargs["params"]["channelId"], "1033")
        self.assertNotEqual(
            calls[0].kwargs["headers"]["correlationid"],
            calls[1].kwargs["headers"]["correlationid"],
        )
        self.assertEqual(
            calls[0].kwargs["headers"]["sessionid"],
            calls[1].kwargs["headers"]["sessionid"],
        )
        for call in calls:
            self.assertIn("device=android_tv;", call.kwargs["headers"]["x-f1-device-info"])

    def test_content_route_never_substitutes_a_top_level_default_url(self):
        sessions = SessionManager(
            SessionStore(None),
            Mock(),
            token=SessionCredentials("entitlement", "subscription"),
        )
        self.http.get_json.return_value = {
            "resultObj": {
                "channelId": 1033,
                "streamType": "HDR_UHD_CMAF",
                "url": "https://cdn.test/wrong.m3u8",
            }
        }
        with self.assertRaisesRegex(F1TVError, "selected feed"):
            self.client.playback(
                self.content, self.content.select_channel("INTERNATIONAL"), sessions
            )

    def test_content_route_fallback_is_limited_to_404_and_never_loops(self):
        sessions = SessionManager(
            SessionStore(None),
            Mock(),
            token=SessionCredentials("entitlement", "subscription"),
        )
        for status, channel, expected_calls in (
            (403, "INTERNATIONAL", 1),
            (451, "INTERNATIONAL", 1),
            (500, "INTERNATIONAL", 1),
            (404, "VER", 1),
            (404, "INTERNATIONAL", 2),
        ):
            with self.subTest(status=status, channel=channel):
                self.http.get_json.reset_mock()
                self.http.get_json.side_effect = APIError("Playback authorization", status)
                with self.assertRaises(F1TVError):
                    self.client.playback(
                        self.content, self.content.select_channel(channel), sessions
                    )
                self.assertEqual(self.http.get_json.call_count, expected_calls)

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
