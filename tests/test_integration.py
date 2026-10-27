"""Real FFmpeg tests against generated local streams; no external service required."""

import functools
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_models import FIXTURE, URL

from f1tv_dl.auth import SessionCredentials, SessionManager, SessionStore
from f1tv_dl.cli import main
from f1tv_dl.desktop import DesktopService, DownloadOptions
from f1tv_dl.errors import APIError, MediaError
from f1tv_dl.http import HttpClient
from f1tv_dl.media import check_manifest, download, probe, select_streams
from f1tv_dl.models import Content, ContentRef
from f1tv_dl.web import WebClient


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
class FFmpegIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000",
                "-t",
                "3",
                "-map",
                "0:v",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:a",
                "-filter:v:0",
                "scale=160:90",
                "-filter:v:1",
                "scale=320:180",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-preset",
                "ultrafast",
                "-g",
                "10",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-metadata:s:a:0",
                "language=eng",
                "-metadata:s:a:1",
                "language=nld",
                str(cls.root / "source.mp4"),
            ]
        )
        (cls.root / "dash").mkdir()
        cls.run_ffmpeg(
            [
                "-i",
                str(cls.root / "source.mp4"),
                "-map",
                "0",
                "-c",
                "copy",
                "-f",
                "dash",
                "-seg_duration",
                "1",
                "-adaptation_sets",
                "id=0,streams=v id=1,streams=2 id=2,streams=3",
                str(cls.root / "dash/master.mpd"),
            ]
        )
        (cls.root / "hls").mkdir()
        cls.run_ffmpeg(
            [
                "-i",
                str(cls.root / "source.mp4"),
                "-map",
                "0:v:0",
                "-map",
                "0:v:1",
                "-map",
                "0:a:0",
                "-map",
                "0:a:1",
                "-c",
                "copy",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_playlist_type",
                "vod",
                "-var_stream_map",
                "a:0,agroup:audio,language:eng,default:yes "
                "a:1,agroup:audio,language:nld v:0,agroup:audio v:1,agroup:audio",
                "-hls_segment_filename",
                str(cls.root / "hls/segment_%v_%03d.ts"),
                "-master_pl_name",
                "master.m3u8",
                str(cls.root / "hls/stream_%v.m3u8"),
            ]
        )
        (cls.root / "cmaf").mkdir()
        cls.run_ffmpeg(
            [
                "-i",
                str(cls.root / "source.mp4"),
                "-map",
                "0:v:0",
                "-map",
                "0:v:1",
                "-map",
                "0:a:0",
                "-map",
                "0:a:1",
                "-c",
                "copy",
                "-f",
                "hls",
                "-hls_segment_type",
                "fmp4",
                "-hls_time",
                "1",
                "-hls_playlist_type",
                "vod",
                "-var_stream_map",
                "a:0,agroup:audio,language:eng,default:yes "
                "a:1,agroup:audio,language:nld v:0,agroup:audio v:1,agroup:audio",
                "-hls_fmp4_init_filename",
                "init_%v.mp4",
                "-hls_segment_filename",
                str(cls.root / "cmaf/segment_%v_%03d.mp4"),
                "-master_pl_name",
                "master.m3u8",
                str(cls.root / "cmaf/stream_%v.m3u8"),
            ]
        )
        handler = functools.partial(QuietHandler, directory=str(cls.root))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def run_ffmpeg(cls, args):
        result = subprocess.run(
            [cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Could not generate test media: " + result.stderr)

    def inspect_output(self, output):
        result = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(output)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return json.loads(result.stdout)["streams"]

    def test_dash_resolution_language_and_secondary_audio_download(self):
        url = self.base_url + "/dash/master.mpd"
        check_manifest(HttpClient().get_text(url))
        payload = probe(url)
        primary = select_streams(payload, "eng", "160x90")
        secondary = select_streams(payload, "nld", audio_only=True)
        output = self.root / "dash-result.mp4"
        download(url, primary, output, secondary=(url, secondary), duration=1)
        streams = self.inspect_output(output)
        video = [s for s in streams if s["codec_type"] == "video"]
        audio = [s for s in streams if s["codec_type"] == "audio"]
        self.assertEqual(len(video), 1)
        self.assertEqual((video[0]["width"], video[0]["height"]), (160, 90))
        self.assertEqual([s["tags"]["language"] for s in audio], ["eng", "nld"])

    def test_hls_selects_best_video_and_dutch_audio(self):
        url = self.base_url + "/hls/master.m3u8"
        check_manifest(HttpClient().get_text(url))
        selected = select_streams(probe(url), "nld")
        output = self.root / "hls-result.ts"
        download(url, selected, output, output_format="ts", duration=1)
        streams = self.inspect_output(output)
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = [s for s in streams if s["codec_type"] == "audio"]
        self.assertEqual((video["width"], video["height"]), (320, 180))
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["tags"]["language"], "nld")

    def test_cmaf_media_requests_keep_the_browser_origin_for_probe_and_both_inputs(self):
        agent = "Mozilla/5.0 Chrome/154.0.0.0"
        received = []

        class OriginHandler(QuietHandler):
            def do_GET(handler):
                path = urlsplit(handler.path).path
                if path != "/cmaf/master.m3u8":
                    if handler.headers.get("Origin") != "https://f1tv.formula1.com":
                        handler.send_error(403)
                        return
                    received.append((path, handler.headers.get("User-Agent")))
                super(OriginHandler, handler).do_GET()

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(OriginHandler, directory=str(self.root))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/cmaf/master.m3u8?token=private-fixture"
            check_manifest(HttpClient().get_text(url))
            with self.assertRaises(MediaError) as caught:
                probe(url)
            self.assertNotIn("private-fixture", str(caught.exception))
            payload = probe(url, user_agent=agent)
            primary = select_streams(payload, "eng", "160x90")
            secondary = select_streams(payload, "nld", audio_only=True)
            output = self.root / "cmaf-browser-result.mp4"
            download(
                url,
                primary,
                output,
                secondary=(url, secondary),
                user_agent=agent,
                duration=1,
            )
            streams = self.inspect_output(output)
            video = next(s for s in streams if s["codec_type"] == "video")
            audio = [s for s in streams if s["codec_type"] == "audio"]
            self.assertEqual((video["width"], video["height"]), (160, 90))
            self.assertEqual([s["tags"]["language"] for s in audio], ["eng", "nld"])
            self.assertTrue(any("init_" in path for path, _ in received))
            self.assertTrue(any("segment_" in path for path, _ in received))
            self.assertTrue(all(user_agent == agent for _, user_agent in received))
            self.assertIn("HTTP 403", str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is not installed")
    def test_https_probe_and_download_verify_a_trusted_certificate(self):
        certificate = self.root / "test-ca.pem"
        key = self.root / "test-key.pem"
        subprocess.run(
            [
                shutil.which("openssl"),
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        handler = functools.partial(QuietHandler, directory=str(self.root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"https://127.0.0.1:{server.server_port}/dash/master.mpd"
            with patch.dict(os.environ, {"SSL_CERT_FILE": str(certificate)}):
                check_manifest(HttpClient().get_text(url))
                selected = select_streams(probe(url), "eng")
                output = self.root / "https-result.mp4"
                download(url, selected, output, duration=1)
            self.assertTrue(output.stat().st_size > 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_download_with_real_media_pipeline(self):
        real_http = HttpClient()
        base_url = self.base_url

        class FixtureTransport:
            def get_json(self, url, **kwargs):
                if "/CONTENT/VIDEO/" in url:
                    return FIXTURE
                if "/CONTENT/PLAY" in url:
                    return {
                        "resultObj": {"url": base_url + "/dash/master.mpd", "streamType": "DASH"}
                    }
                raise AssertionError("Unexpected external request")

            def get_text(self, url, **kwargs):
                return real_http.get_text(url, **kwargs)

        with patch("f1tv_dl.cli.HttpClient", return_value=FixtureTransport()):
            status = main(
                [
                    URL,
                    "-c",
                    "VER",
                    "-i",
                    "nld",
                    "-s",
                    "160x90",
                    "--duration",
                    "1",
                    "--token",
                    "fixture-token",
                    "--no-cache",
                    "-o",
                    str(self.root / "cli-output"),
                ]
            )
        self.assertEqual(status, 0)
        output = self.root / "cli-output/2026-united-states-grand-prix-VER.mp4"
        streams = self.inspect_output(output)
        self.assertEqual([s["codec_type"] for s in streams], ["video", "audio", "audio"])
        self.assertFalse(list(output.parent.glob("*.part*")))

    def test_desktop_download_has_real_progress_and_two_audio_tracks(self):
        real_http = HttpClient()
        service = DesktopService(session_path=self.root / "desktop-session.json")
        service.sign_in("token", token="fixture-token")
        url = self.base_url + "/dash/master.mpd"
        service.stream_url = lambda *args: url
        service.http = real_http
        content = Content.from_response(ContentRef.parse(URL), FIXTURE)
        progress = []
        # Windows GUI launchers run without a console or stderr stream.
        with patch("f1tv_dl.media.sys.stderr", None):
            output = service.download(
                content,
                DownloadOptions(
                    self.root / "desktop-output",
                    channel="VER",
                    resolution="160x90",
                    international="nld",
                    duration=1,
                ),
                progress=progress.append,
            )
        streams = self.inspect_output(output)
        self.assertEqual([s["codec_type"] for s in streams], ["video", "audio", "audio"])
        self.assertTrue(progress)
        self.assertGreater(progress[-1].seconds, 0)
        self.assertGreater(progress[-1].total_bytes, 0)

    def test_queued_web_downloads_send_required_headers_and_browser_identity_to_media(self):
        agent = "Mozilla/5.0 Chrome/154.0.0.0"
        received = []

        class WebPlaybackHandler(QuietHandler):
            def do_GET(handler):
                if handler.headers.get("User-Agent") != agent:
                    handler.send_error(500)
                    return
                if "/CONTENT/PLAY" in handler.path:
                    required = (
                        "x-f1-device-info",
                        "sessionid",
                        "correlationid",
                        "ascendontoken",
                        "entitlementtoken",
                    )
                    if not all(handler.headers.get(name) for name in required):
                        handler.send_error(500)
                        return
                    received.append({name: handler.headers.get(name) for name in required})
                    channel = parse_qs(urlsplit(handler.path).query)["channelId"][0]
                    payload = {
                        "resultObj": {
                            "streamType": "SDR_HD_CMAF",
                            "channelId": channel,
                            "tme": {
                                "feeds": [
                                    {
                                        "metadata": {"channel_id": channel},
                                        "url": f"http://127.0.0.1:{handler.server.server_port}/dash/master.mpd",
                                    }
                                ]
                            },
                        }
                    }
                    body = json.dumps(payload).encode()
                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/json")
                    handler.send_header("Content-Length", str(len(body)))
                    handler.end_headers()
                    handler.wfile.write(body)
                else:
                    if handler.headers.get("Origin") != "https://f1tv.formula1.com":
                        handler.send_error(403)
                        return
                    if handler.headers.get("ascendontoken") or handler.headers.get(
                        "entitlementtoken"
                    ):
                        handler.send_error(500)
                        return
                    super(WebPlaybackHandler, handler).do_GET()

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(WebPlaybackHandler, directory=str(self.root))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(APIError) as caught:
                HttpClient(retries=0).get_json(
                    base + "/3.0/R/ENG/WEB_HLS/ALL/CONTENT/PLAY?channelId=1009",
                    headers={"User-Agent": agent, "entitlementToken": "fixture-entitlement"},
                )
            self.assertEqual(caught.exception.status, 500)
            service = DesktopService(session_path=self.root / "queue-session.json")
            context = WebClient.create(agent)
            service.sessions = SessionManager(
                SessionStore(None),
                lambda: None,
                token=SessionCredentials("fixture-entitlement", "fixture-subscription", context),
            )
            content = Content.from_response(ContentRef.parse(URL), FIXTURE)
            with patch("f1tv_dl.api.BASE_URL", base):
                result = service.download_many(
                    content,
                    DownloadOptions(self.root / "web-queue", resolution="160x90", duration=1),
                    ["VER", "HAM"],
                )
            self.assertEqual(result.failed, ())
            self.assertEqual(len(result.completed), 2)
            for output in result.completed:
                self.assertGreater(output.stat().st_size, 0)
                self.assertEqual(self.inspect_output(output)[0]["width"], 160)
            self.assertEqual(received[0]["sessionid"], received[1]["sessionid"])
            self.assertNotEqual(received[0]["correlationid"], received[1]["correlationid"])
            self.assertFalse(list((self.root / "web-queue").glob("*.part*")))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
