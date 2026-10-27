import copy
import unittest

from test_models import FIXTURE, URL

from f1tv_dl.models import Channel, Content, ContentRef
from f1tv_dl.text import display_text


class DisplayTextTests(unittest.TestCase):
    def test_escaped_unicode_and_entities_become_readable_text(self):
        self.assertEqual(
            display_text(r"RACE \u2022 S\u00e3o Paulo &amp; P\u00e9rez"), "RACE • São Paulo & Pérez"
        )
        self.assertEqual(display_text(r"\ud83c\udfc1"), "🏁")
        self.assertEqual(display_text(r"\U0001F3C1"), "🏁")
        self.assertEqual(display_text("Hülkenberg"), "Hülkenberg")

    def test_invalid_and_double_backslashed_escapes_are_preserved(self):
        for value in (r"\ud800", r"\u0000", r"\U00110000", r"\\u2022"):
            self.assertEqual(display_text(value), value)

    def test_only_metadata_text_is_decoded(self):
        raw = {
            "title": r"P\u00e9rez",
            "driverFirstName": "Sergio",
            "driverLastName": r"P\u00e9rez",
            "playbackUrl": "https://f1tv.formula1.com/play?channelId=1006",
        }
        channel = Channel.from_dict(raw)
        self.assertEqual(channel.title, "Pérez")
        self.assertEqual(channel.driver_name, "Sergio Pérez")
        self.assertEqual(channel.channel_id, "1006")
        payload = copy.deepcopy(FIXTURE)
        payload["resultObj"]["containers"][0]["metadata"]["title"] = r"Race \u2022 Highlights"
        content = Content.from_response(ContentRef.parse(URL), payload)
        self.assertEqual(content.title, "Race • Highlights")
