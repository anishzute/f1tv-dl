import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from f1tv_dl.auth import Authenticator, SessionCredentials, SessionManager, SessionStore
from f1tv_dl.web import WebClient

AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36"


class WebClientTests(unittest.TestCase):
    def test_actual_browser_context_and_public_protocol_fields_are_sent(self):
        client = WebClient.create(AGENT)
        first, second = client.headers(), client.headers()
        self.assertEqual(first["User-Agent"], AGENT)
        fields = dict(part.split("=", 1) for part in first["x-f1-device-info"].split(";"))
        self.assertEqual(fields["device"], "web")
        self.assertEqual(fields["screen"], "browser")
        self.assertEqual(fields["os"], "linux")
        self.assertEqual(fields["browserVersion"], "154")
        self.assertEqual(fields["appVersion"], "55.0.5")
        self.assertEqual(fields["playerVersion"], "8.212.0")
        self.assertEqual(fields["p"], "true")
        self.assertEqual(first["sessionid"], second["sessionid"])
        self.assertNotEqual(first["correlationid"], second["correlationid"])
        self.assertRegex(first["sessionid"], r"^WEB-[0-9a-f-]{36}$")
        self.assertNotIn(AGENT, repr(client))

    def test_context_roundtrip_and_invalid_cached_headers(self):
        client = WebClient.create(AGENT)
        self.assertEqual(WebClient.from_dict(client.as_dict()), client)
        for value in (None, {}, {**client.as_dict(), "user_agent": "bad\r\nheader: value"}):
            self.assertIsNone(WebClient.from_dict(value))

    def test_browser_request_context_is_preserved_in_the_private_session_cache(self):
        client = WebClient.create(AGENT)
        credentials = SessionCredentials("entitlement", "subscription", client)
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / "session.json")
            store.save(credentials)
            self.assertEqual(store.load(), credentials)
            session = SessionManager(store, Mock())
            self.assertEqual(session.web_client.session_id, client.session_id)
            self.assertEqual(session.web_client.user_agent, AGENT)

    def test_entitlement_and_playback_share_the_same_browser_session(self):
        from test_models import FIXTURE, URL

        from f1tv_dl.api import F1TVClient
        from f1tv_dl.models import Content, ContentRef

        client = WebClient.create(AGENT)
        http = Mock()
        http.get_json.side_effect = [
            {"resultObj": {"entitlementToken": "fixture-entitlement"}},
            {"resultObj": {"url": "https://cdn.test/main.m3u8", "streamType": "HLS"}},
        ]
        entitlement = Authenticator(http).exchange_subscription_token(
            "fixture-subscription", platform="WEB_DASH", web_client=client
        )
        sessions = SessionManager(
            SessionStore(None),
            Mock(),
            token=SessionCredentials(entitlement, "fixture-subscription", client),
        )
        content = Content.from_response(ContentRef.parse(URL), FIXTURE)
        F1TVClient(http).playback(content, content.select_channel(), sessions)
        headers = [call.kwargs["headers"] for call in http.get_json.call_args_list]
        self.assertEqual(headers[0]["sessionid"], headers[1]["sessionid"])
        self.assertEqual(headers[0]["x-f1-device-info"], headers[1]["x-f1-device-info"])
        self.assertEqual(headers[0]["User-Agent"], AGENT)
        self.assertNotEqual(headers[0]["correlationid"], headers[1]["correlationid"])
        self.assertTrue(
            all(re.fullmatch(r"WEB-[0-9a-f-]{36}", h["correlationid"]) for h in headers)
        )
