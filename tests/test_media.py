import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from f1tv_dl.errors import DRMError, MediaError
from f1tv_dl.media import Selection, build_command, check_manifest, download, probe, select_streams


def stream(index, kind, **fields):
    return {"index": index, "codec_type": kind, **fields}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.low = stream(3, "video", width=640, height=360, bit_rate="900000")
        self.high = stream(9, "video", width=1920, height=1080, bit_rate="6000000")
        self.eng = stream(11, "audio", tags={"language": "eng"}, disposition={"default": 1})
        self.nld = stream(17, "audio", tags={"language": "nld"})
        self.payload = {"streams": [self.low, self.high, self.eng, self.nld]}

    def test_best_video_and_requested_audio_use_absolute_indexes(self):
        result = select_streams(self.payload, "nl")
        self.assertEqual((result.video, result.audio, result.language), (9, 17, "nld"))

    def test_exact_resolution(self):
        result = select_streams(self.payload, "eng", "640x360")
        self.assertEqual(result.video, 3)

    def test_unavailable_resolution_is_an_error(self):
        with self.assertRaises(MediaError):
            select_streams(self.payload, resolution="1280x720")

    def test_missing_language_falls_back_to_default(self):
        with self.assertLogs(level="WARNING"):
            result = select_streams(self.payload, "jpn")
        self.assertEqual(result.audio, 11)

    def test_hls_audio_is_bound_to_selected_program(self):
        low_audio = stream(4, "audio", tags={"language": "eng"})
        payload = self.payload | {
            "programs": [
                {"streams": [self.low, low_audio]},
                {"streams": [self.high, self.eng, self.nld]},
            ]
        }
        self.assertEqual(select_streams(payload, "eng", "640x360").audio, 4)
        self.assertEqual(select_streams(payload, "nld").audio, 17)

    def test_secondary_audio_does_not_require_video(self):
        result = select_streams({"streams": [self.nld]}, "nld", audio_only=True)
        self.assertIsNone(result.video)
        self.assertEqual(result.audio, 17)

    def test_missing_audio_is_an_error(self):
        with self.assertRaises(MediaError):
            select_streams({"streams": [self.high]})

    def test_attached_cover_art_is_not_selected(self):
        attached = stream(1, "video", width=4000, height=4000, disposition={"attached_pic": 1})
        payload = {"streams": [attached, self.high, self.eng]}
        self.assertEqual(select_streams(payload).video, 9)


class ManifestTests(unittest.TestCase):
    def test_clear_hls_dash_and_aes128_are_supported(self):
        for text in (
            "#EXTM3U\n#EXTINF:2,\nsegment.ts\n",
            '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n',
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period/></MPD>',
        ):
            check_manifest(text)

    def test_drm_is_rejected_before_ffmpeg(self):
        for text in (
            '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"',
            '#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=AES-128,KEYFORMAT="com.widevine"',
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">'
            "<Period><ContentProtection/></Period></MPD>",
        ):
            with self.subTest(text=text), self.assertRaises(DRMError):
                check_manifest(text)

    def test_invalid_manifest_fails_cleanly(self):
        for text in ("<html>error</html>", "not a manifest", "<MPD"):
            with self.subTest(text=text), self.assertRaises(MediaError):
                check_manifest(text)


class FFmpegTests(unittest.TestCase):
    def test_secondary_audio_adds_one_audio_stream_and_no_extra_video(self):
        command = build_command(
            "ffmpeg",
            "https://cdn.test/main.mpd",
            Selection(8, 13, "eng"),
            Path("out.mp4"),
            secondary=("https://cdn.test/intl.mpd", Selection(None, 29, "nld")),
            offset="-00:00:04.750",
            duration=5,
        )
        mappings = [command[i + 1] for i, value in enumerate(command) if value == "-map"]
        self.assertEqual(mappings, ["0:8", "0:13", "1:29"])
        self.assertLess(command.index("-itsoffset"), command.index("https://cdn.test/intl.mpd"))
        self.assertIn("language=nld", command)
        self.assertNotIn("Sky", command)

    def test_failed_download_preserves_existing_file_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.mp4"
            output.write_bytes(b"original")
            process = Mock()
            process.stdout = io.StringIO()
            process.wait.return_value = 1
            process.poll.return_value = 1
            with (
                patch("f1tv_dl.media.require_binary", return_value="ffmpeg"),
                patch("f1tv_dl.media.subprocess.Popen", return_value=process),
            ):
                with self.assertRaises(MediaError):
                    download(
                        "https://cdn.test/a.mpd", Selection(0, 1, "eng"), output, overwrite=True
                    )
            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_existing_file_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.mp4"
            output.write_bytes(b"original")
            with (
                patch("f1tv_dl.media.require_binary", return_value="ffmpeg"),
                patch("f1tv_dl.media.subprocess.Popen") as process,
            ):
                with self.assertRaises(MediaError):
                    download("https://cdn.test/a.mpd", Selection(0, 1, "eng"), output)
                process.assert_not_called()

    def test_probe_errors_do_not_leak_signed_urls(self):
        result = Mock(returncode=1, stderr="secret signed URL", stdout="")
        with (
            patch("f1tv_dl.media.require_binary", return_value="ffprobe"),
            patch("f1tv_dl.media.subprocess.run", return_value=result),
        ):
            with self.assertRaises(MediaError) as caught:
                probe("https://cdn.test/a.mpd?token=secret")
        self.assertNotIn("secret", str(caught.exception))

    def test_probe_valid_json(self):
        result = Mock(returncode=0, stdout=json.dumps({"streams": []}))
        with (
            patch("f1tv_dl.media.require_binary", return_value="ffprobe"),
            patch("f1tv_dl.media.subprocess.run", return_value=result),
        ):
            self.assertEqual(probe("https://cdn.test/a.mpd"), {"streams": []})
