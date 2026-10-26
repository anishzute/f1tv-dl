import copy
import json
import unittest
from pathlib import Path

from f1tv_dl.errors import F1TVError
from f1tv_dl.models import Channel, Content, ContentRef, safe_filename

URL = "https://f1tv.formula1.com/detail/1000010409/2026-united-states-grand-prix"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/content.json").read_text())


class URLTests(unittest.TestCase):
    def test_real_url_and_trailing_slash(self):
        for suffix in ("", "/", "?foo=bar#video"):
            with self.subTest(suffix=suffix):
                ref = ContentRef.parse(URL + suffix)
                self.assertEqual(ref.id, "1000010409")
                self.assertEqual(ref.name, "2026-united-states-grand-prix")

    def test_localized_and_id_only_urls(self):
        ref = ContentRef.parse("https://f1tv.formula1.com/en/detail/123")
        self.assertEqual((ref.id, ref.name), ("123", "123"))

    def test_reject_spoofed_hosts_and_invalid_paths(self):
        for url in (
            "https://evil-f1tv.com/detail/123/race",
            "https://f1tv.formula1.com.evil.test/detail/123/race",
            "https://f1tv.formula1.com@evil.test/detail/123/race",
            "https://user@f1tv.formula1.com/detail/123/race",
            "http://f1tv.formula1.com/detail/123/race",
            "https://f1tv.formula1.com:9999/detail/123/race",
            "https://f1tv.formula1.com/detail/no-id/race",
            URL + "/extra",
            "https://f1tv.formula1.com:invalid/detail/123",
            "file:///detail/123/race",
            URL + "\n",
        ):
            with self.subTest(url=url), self.assertRaises(F1TVError):
                ContentRef.parse(url)

    def test_safe_filename_cannot_escape_destination(self):
        for value in ("../../etc/passwd", "x\\y:z", "CON", "..", "", "a" * 1000):
            name = safe_filename(value)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertFalse(name.startswith("."))
            self.assertLessEqual(len(name), 185)
        self.assertEqual(safe_filename("CON"), "f1tv-CON")


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.content = Content.from_response(ContentRef.parse(URL), FIXTURE)

    def test_live_fixture_has_26_channels_and_default(self):
        self.assertEqual(len(self.content.channels), 26)
        self.assertEqual(self.content.select_channel().title, "F1 LIVE")
        self.assertEqual(self.content.select_channel().channel_id, "1033")

    def test_driver_aliases_and_case_insensitive_names(self):
        for query in ("VER", "ver", "Max Verstappen", "verstappen", "3", "1009"):
            with self.subTest(query=query):
                self.assertEqual(self.content.select_channel(query).title, "VER")

    def test_international_playback_omits_advertised_channel_id(self):
        self.assertIsNone(self.content.select_channel("international").channel_id)

    def test_ambiguous_missing_and_empty_channels(self):
        for query in ("a", "missing", "   "):
            with self.subTest(query=query), self.assertRaises(F1TVError):
                self.content.select_channel(query)

    def test_no_channels_and_empty_additional_streams(self):
        for streams in (None, []):
            payload = copy.deepcopy(FIXTURE)
            payload["resultObj"]["containers"][0]["metadata"]["additionalStreams"] = streams
            content = Content.from_response(ContentRef.parse(URL), payload)
            self.assertIsNone(content.select_channel())
            with self.assertRaises(F1TVError):
                content.select_channel("DATA")

    def test_metadata_must_match_requested_id(self):
        with self.assertRaises(F1TVError):
            Content.from_response(ContentRef("999", "wrong"), FIXTURE)

    def test_channel_id_fallback_when_playback_url_absent(self):
        self.assertEqual(Channel.from_dict({"channelId": 12}).channel_id, "12")
