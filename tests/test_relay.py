import io
import re
import threading
import unittest
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from f1tv_dl.errors import APIError, DRMError, F1TVError, MediaError, OperationCancelled
from f1tv_dl.relay import HLSRelay

ROOT = "https://media.test/signed/master.m3u8?signature=private-signature"
MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nvideo/stream.m3u8\n"
PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:1\n"
    '#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\nsegment.mp4\n#EXT-X-ENDLIST\n'
)
HEADERS = {"User-Agent": "fixture-Chromium", "Origin": "https://f1tv.formula1.com"}


class Response(io.BytesIO):
    def __init__(self, data, url, *, status=200, headers=None):
        super().__init__(data)
        self.url = url
        self.status = status
        self.headers = {"Content-Length": str(len(data)), **(headers or {})}

    def geturl(self):
        return self.url


class FixtureHTTP:
    def __init__(self):
        self.calls = []
        self.entries = {
            ROOT: MASTER.encode(),
            "https://media.test/signed/video/stream.m3u8": PLAYLIST.encode(),
            "https://media.test/signed/video/init.mp4": b"initialization",
            "https://media.test/signed/video/segment.mp4": b"0123456789",
        }
        self.redirects = {}

    @contextmanager
    def open_stream(self, url, *, headers, operation):
        self.calls.append((url, headers))
        data = self.entries[url]
        if isinstance(data, Exception):
            raise data
        status, response_headers = 200, {}
        if headers.get("Range"):
            start, end = headers["Range"][6:].split("-")
            start, end = int(start), int(end or len(data) - 1)
            response_headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            data, status = data[start : end + 1], 206
        with Response(
            data, self.redirects.get(url, url), status=status, headers=response_headers
        ) as response:
            yield response


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.http = FixtureHTTP()
        self.client = build_opener(ProxyHandler({}))
        self.relay = HLSRelay(self.http, headers=HEADERS)
        self.relay.__enter__()
        self.addCleanup(self.relay.__exit__)

    def get(self, url, headers=None):
        with self.client.open(Request(url, headers=headers or {}), timeout=3) as response:
            return response.status, response.read(), response.headers

    def media_url(self):
        local = self.relay.prepare(ROOT, MASTER)
        _, data, _ = self.get(local)
        video = next(line for line in data.decode().splitlines() if not line.startswith("#"))
        return video

    def test_relative_playlists_init_segments_and_keys_stay_on_loopback(self):
        playlist_url = "https://media.test/signed/video/stream.m3u8"
        self.http.entries[playlist_url] = (
            PLAYLIST.replace("#EXT-X-MAP:", '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-MAP:')
        ).encode()
        self.http.entries["https://media.test/signed/video/key.bin"] = b"1234567890123456"
        video = self.media_url()
        _, body, _ = self.get(video)
        self.assertNotIn(b"private-signature", body)
        self.assertNotIn(b"media.test", body)
        references = re.findall(r'URI="([^"]+)"|^(http://[^\s]+)$', body.decode(), re.M)
        payloads = [self.get(a or b)[1] for a, b in references]
        self.assertEqual(payloads, [b"1234567890123456", b"initialization", b"0123456789"])
        self.assertTrue(all(headers == HEADERS for _, headers in self.http.calls))

    def test_ranges_are_forwarded_without_buffering_or_reencoding_media(self):
        _, body, _ = self.get(self.media_url())
        segment = next(line for line in body.decode().splitlines() if not line.startswith("#"))
        status, data, headers = self.get(segment, {"Range": "bytes=2-5"})
        self.assertEqual((status, data), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(self.http.calls[-1][1]["Range"], "bytes=2-5")

    def test_redirected_playlists_use_the_final_upstream_directory(self):
        self.http.redirects[ROOT] = "https://media.test/redirected/master.m3u8"
        self.http.entries["https://media.test/redirected/video/stream.m3u8"] = PLAYLIST.encode()
        self.get(self.media_url())
        self.assertEqual(self.http.calls[-1][0], "https://media.test/redirected/video/stream.m3u8")

    def test_unknown_paths_and_unexpected_host_headers_cannot_fetch_upstream(self):
        local = self.relay.prepare(ROOT, MASTER)
        for url, headers in (
            (local.rsplit("/", 1)[0] + "/unknown", {}),
            (local, {"Host": "untrusted.test"}),
        ):
            with self.subTest(url=url), self.assertRaises(HTTPError) as caught:
                self.get(url, headers)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
        self.assertEqual(self.http.calls, [])

    def test_media_http_failure_is_reported_without_the_upstream_url(self):
        self.http.entries[ROOT] = APIError("HLS media lookup", 403)
        local = self.relay.prepare(ROOT, MASTER)

        def probe():
            try:
                self.get(local)
            except HTTPError as error:
                error.close()
            raise MediaError("FFprobe could not read the stream.")

        with self.assertRaises(APIError) as caught:
            self.relay.run(probe)
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("private-signature", str(caught.exception))
        self.assertNotIn("media.test", str(caught.exception))

    def test_child_playlist_drm_is_rejected_before_requesting_the_key(self):
        self.http.entries["https://media.test/signed/video/stream.m3u8"] = (
            b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="license"\n'
        )
        video = self.media_url()
        with self.assertRaises(HTTPError) as caught:
            self.get(video)
        caught.exception.close()
        self.assertIsInstance(self.relay.failure, DRMError)
        self.assertEqual(len(self.http.calls), 2)

    def test_https_playlists_cannot_downgrade_or_open_local_files(self):
        for uri in (
            "http://media.test/insecure.m3u8",
            "file:///private/file",
            "ftp://media.test/a",
        ):
            with self.subTest(uri=uri):
                self.http.entries[ROOT] = ("#EXTM3U\n#EXTINF:1,\n" + uri + "\n").encode()
                local = self.relay.prepare(ROOT, MASTER)
                with self.assertRaises(HTTPError) as caught:
                    self.get(local)
                caught.exception.close()
                self.assertIsInstance(self.relay.failure, F1TVError)
        self.assertTrue(all(url == ROOT for url, _ in self.http.calls))

    def test_cancellation_preserves_the_operation_cancelled_result(self):
        cancel = threading.Event()
        self.relay.cancel = cancel

        def interrupted():
            cancel.set()
            raise MediaError("Media read interrupted.")

        with self.assertRaises(OperationCancelled):
            self.relay.run(interrupted)

    def test_context_exit_closes_the_listener(self):
        local = self.relay.prepare(ROOT, MASTER)
        self.relay.__exit__()
        with self.assertRaises(OSError):
            self.get(local)
