import io
import json
import threading
import unittest
from http.client import IncompleteRead
from unittest.mock import Mock, patch

from test_models import FIXTURE, URL

from f1tv_dl.auth import SessionCredentials, SessionManager, SessionStore
from f1tv_dl.diagnostics import (
    MAX_MANIFEST_BYTES,
    ProfileReport,
    check_manifests,
    inspect_manifests,
)
from f1tv_dl.errors import APIError, AuthenticationError, F1TVError, OperationCancelled
from f1tv_dl.models import Content, ContentRef
from f1tv_dl.web import WebClient

MASTER_URL = "https://cdn.example/master.m3u8?token=private-signed-token"
MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",LANGUAGE="en",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
hd.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=18000000,RESOLUTION=3840x2160,CODECS="hvc1.2.4.L153,mp4a.40.2",AUDIO="main"
uhd.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",URI="data:private-license-data"
#EXT-X-MAP:URI="init.mp4?token=private-init"
#EXTINF:6,
segment.m4s?token=private-segment
#EXT-X-ENDLIST
"""
MPD = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013">
<Period><AdaptationSet codecs="hvc1.2.4.L153" width="3840" height="2160">
<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"/>
<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
<cenc:pssh>private-license-data</cenc:pssh></ContentProtection>
<Representation id="uhd"/><SegmentTemplate initialization="private-init" media="private-segment"/>
</AdaptationSet></Period></MPD>"""


class Response(io.BytesIO):
    def __init__(self, text, url):
        super().__init__(text.encode() if isinstance(text, str) else text)
        self.url = url

    def geturl(self):
        return self.url


class ManifestCheckTests(unittest.TestCase):
    def setUp(self):
        self.content = Content.from_response(ContentRef.parse(URL), FIXTURE)
        self.channel = self.content.select_channel("INTERNATIONAL")
        self.client = WebClient.create("Chrome/145.0.0.0")
        self.sessions = SessionManager(
            SessionStore(None),
            Mock(),
            token=SessionCredentials("private-entitlement", "private-subscription", self.client),
        )
        self.http = Mock()
        self.http.get_json.return_value = {
            "resultCode": "OK",
            "resultObj": {"streamType": "HDR-UHD-CMAF-WV", "url": MASTER_URL},
        }
        self.responses = []

    def manifests(self, mapping, *, redirects=None):
        def open_stream(url, **kwargs):
            self.assertIn(url, mapping, "Only referenced manifests may be requested")
            response = Response(mapping[url], (redirects or {}).get(url, url))
            self.responses.append(response)
            return response

        self.http.open_stream.side_effect = open_stream

    def check(self, **kwargs):
        return check_manifests(self.http, self.content, self.channel, self.sessions, **kwargs)

    def test_hls_uhd_widevine_checks_only_manifests_and_omits_all_secrets(self):
        self.manifests(
            {
                MASTER_URL: MASTER,
                "https://cdn.example/uhd.m3u8": MEDIA,
                "https://cdn.example/audio.m3u8": "#EXTM3U\n#EXTINF:6,\naudio.m4s\n",
            }
        )
        report = self.check()
        self.assertTrue(report.inspected)
        for profile in report.profiles:
            self.assertEqual(profile.resolutions, ["3840x2160", "1920x1080"])
            self.assertEqual(profile.drm_systems, ["Widevine"])
            self.assertEqual(profile.encryption, ["SAMPLE-AES-CTR"])
            self.assertEqual(profile.codecs, ["avc1", "hvc1", "mp4a"])
            self.assertEqual(profile.manifests_checked, 3)
        self.assertTrue(all(response.closed for response in self.responses))
        serialized = report.to_json() + report.to_text() + repr(report)
        self.assertNotIn("private-", serialized)
        self.assertNotIn("https://", serialized)
        self.assertEqual(len(json.loads(report.to_json())["profiles"]), 3)
        self.assertIn("2160p or higher advertised", report.to_text())

    def test_profiles_keep_feed_and_tokens_but_do_not_change_saved_browser_context(self):
        self.manifests({MASTER_URL: "#EXTM3U\n"})
        self.check()
        calls = self.http.get_json.call_args_list
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertEqual(call.kwargs["params"]["channelId"], "1025")
            self.assertEqual(call.kwargs["headers"]["ascendontoken"], "private-subscription")
            self.assertEqual(call.kwargs["headers"]["entitlementToken"], "private-entitlement")
        self.assertIn("device=web;", calls[0].kwargs["headers"]["x-f1-device-info"])
        self.assertIn("device=android_tv;", calls[1].kwargs["headers"]["x-f1-device-info"])
        self.assertIn("/BIG_SCREEN_HLS/", calls[2].args[0])
        self.assertIn("device=web;", self.sessions.web_client.device_info)
        for call in self.http.open_stream.call_args_list:
            self.assertEqual(call.kwargs["headers"], self.client.media_headers())

    def test_dash_inherits_dimensions_and_reports_protection_without_reading_pssh(self):
        self.manifests({MASTER_URL: MPD})
        report = self.check()
        self.assertEqual(report.profiles[0].resolutions, ["3840x2160"])
        self.assertEqual(report.profiles[0].drm_systems, ["Widevine"])
        self.assertEqual(report.profiles[0].encryption, ["Common encryption (CENC)"])
        self.assertEqual(self.http.open_stream.call_count, 3)
        self.assertNotIn("private-license", report.to_json())

    def test_clear_hd_and_aes128_are_not_mislabeled_as_drm(self):
        self.manifests({MASTER_URL: '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'})
        profile = self.check().profiles[0]
        self.assertEqual(profile.drm_systems, [])
        self.assertEqual(profile.encryption, ["AES-128"])
        self.assertEqual(self.http.open_stream.call_count, 3)

    def test_failed_profile_does_not_stop_others_or_leak_api_responses(self):
        self.http.get_json.side_effect = [
            APIError("Playback authorization", 451),
            {
                "resultObj": {
                    "url": MASTER_URL,
                    "drmToken": "private-drm-token",
                    "drmType": "Widevine",
                }
            },
            {"resultCode": "private-server-message", "resultObj": {}},
        ]
        self.manifests({MASTER_URL: MPD})
        report = self.check()
        self.assertEqual(report.profiles[0].http_status, 451)
        self.assertIn("does not establish UHD eligibility", report.profiles[0].error)
        self.assertTrue(report.profiles[1].api_drm_marked)
        self.assertEqual(report.profiles[1].manifests_checked, 1)
        self.assertIsNotNone(report.profiles[2].error)
        self.assertNotIn("private-", report.to_json())

    def test_selected_tme_feed_is_inspected_instead_of_root_or_other_feeds(self):
        self.http.get_json.return_value = {
            "resultObj": {
                "url": "https://cdn.example/wrong.m3u8",
                "tme": {
                    "feeds": [
                        {"metadata": {"channel_id": 1009}, "url": "https://cdn.example/wrong.m3u8"},
                        {"metadata": {"channel_id": 1025}, "url": MASTER_URL},
                    ]
                },
            }
        }
        self.manifests({MASTER_URL: "#EXTM3U\n"})
        self.assertTrue(self.check().inspected)
        self.assertEqual(self.http.open_stream.call_count, 3)

    def test_missing_selected_tme_feed_does_not_inspect_default_feed(self):
        self.http.get_json.return_value = {
            "resultObj": {
                "tme": {
                    "feeds": [
                        {"metadata": {"channel_id": 1009}, "url": MASTER_URL},
                    ]
                }
            }
        }
        report = self.check()
        self.assertFalse(report.inspected)
        self.assertIn("selected feed", report.profiles[0].error)
        self.http.open_stream.assert_not_called()

    def test_redirected_master_resolves_relative_variants_from_final_url(self):
        self.manifests(
            {
                MASTER_URL: MASTER,
                "https://cdn.example/final/uhd.m3u8": MEDIA,
                "https://cdn.example/final/audio.m3u8": "#EXTM3U\n",
            },
            redirects={MASTER_URL: "https://cdn.example/final/master.m3u8"},
        )
        self.assertTrue(self.check().inspected)

    def test_manifest_http_failure_preserves_advertised_sizes_as_partial(self):
        self.manifests({MASTER_URL: MASTER})
        real_open = self.http.open_stream.side_effect
        self.http.open_stream.side_effect = lambda url, **kw: (
            real_open(url, **kw) if url == MASTER_URL else self.fail_child()
        )
        report = self.check()
        self.assertEqual(report.profiles[0].http_status, 200)
        self.assertEqual(report.profiles[0].manifest_http_status, 403)
        self.assertEqual(report.profiles[0].manifests_checked, 1)
        self.assertIsNotNone(report.profiles[0].error)

    @staticmethod
    def fail_child():
        raise APIError("Manifest lookup", 403)

    def test_https_downgrade_is_rejected_before_fetching_child(self):
        self.manifests(
            {MASTER_URL: "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttp://cdn.example/a.m3u8\n"}
        )
        report = self.check()
        self.assertIn("HTTPS", report.profiles[0].error)
        self.assertEqual(self.http.open_stream.call_count, 3)

    def test_playlist_cycles_terminate_without_fetching_segments(self):
        self.manifests({MASTER_URL: "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n" + MASTER_URL})
        report = self.check()
        self.assertEqual(report.profiles[0].manifests_checked, 1)
        self.assertEqual(self.http.open_stream.call_count, 3)

    def test_cancellation_between_profiles_prevents_further_requests(self):
        cancel = threading.Event()
        self.manifests({MASTER_URL: "#EXTM3U\n"})

        def progress(message):
            cancel.set()

        with self.assertRaises(OperationCancelled):
            self.check(cancel=cancel, status=progress)
        self.http.open_stream.assert_not_called()
        self.assertLessEqual(self.http.get_json.call_count, 1)

    def test_legacy_token_requires_browser_session_before_playback(self):
        self.sessions.subscription_token = None
        with self.assertRaisesRegex(AuthenticationError, "Browser sign-in"):
            self.check()
        self.http.get_json.assert_not_called()

    def test_tiled_dash_does_not_claim_assembled_4k_resolution(self):
        self.manifests(
            {
                MASTER_URL: MPD.replace(
                    '<Representation id="uhd"/>',
                    '<SupplementalProperty schemeIdUri="urn:mpeg:dash:srd:2014"/>'
                    '<Representation id="uhd"/>',
                )
            }
        )
        report = self.check()
        self.assertTrue(report.profiles[0].tiled_video)
        self.assertIn("Not verified (tiled video)", report.to_text())

    def test_xml_declarations_and_non_manifest_responses_are_safe_errors(self):
        for text in (
            '<!DOCTYPE MPD [<!ENTITY private "data">]><MPD/>',
            "<html>private-signed-token</html>",
            "not a manifest",
        ):
            with self.subTest(text=text):
                self.manifests({MASTER_URL: text})
                report = self.check()
                self.assertFalse(report.inspected)
                self.assertNotIn("private-", report.to_json())

    def test_oversized_and_interrupted_manifests_fail_without_exposing_data(self):
        self.manifests({MASTER_URL: b"#EXTM3U\n" + b"x" * 30})
        with patch("f1tv_dl.diagnostics.MAX_MANIFEST_BYTES", 16):
            self.assertIn("limit", self.check().profiles[0].error)
        self.http.open_stream.side_effect = IncompleteRead(b"private-token")
        self.assertIn("transfer failed", self.check().profiles[0].error)
        self.assertLess(MAX_MANIFEST_BYTES, 10 * 1024 * 1024)

    def test_depth_limit_reports_partial_and_cancellation_prevents_initial_fetch(self):
        urls = [f"https://cdn.example/{i}.m3u8" for i in range(8)]
        self.manifests(
            {
                url: f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n{urls[i + 1]}"
                for i, url in enumerate(urls[:-1])
            }
        )
        profile = ProfileReport("test", "Test")
        with self.assertRaisesRegex(F1TVError, "six-manifest"):
            inspect_manifests(self.http, urls[0], profile, headers={})
        self.assertEqual(profile.manifests_checked, 6)
        cancel = threading.Event()
        cancel.set()
        self.http.open_stream.reset_mock()
        with self.assertRaises(OperationCancelled):
            self.check(cancel=cancel)
        self.http.open_stream.assert_not_called()
