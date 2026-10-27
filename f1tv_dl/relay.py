"""Serve HLS to FFmpeg over loopback using Python's verified upstream HTTP transport."""

import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit

from .errors import APIError, F1TVError, MediaError, OperationCancelled
from .http import validate_media_url
from .media import check_manifest

URI_ATTRIBUTE = re.compile(r'(?<![\w-])URI="([^"]+)"')
BYTE_RANGE = re.compile(r"bytes=\d+-\d*\Z")
CONTENT_RANGE = re.compile(r"bytes (?:\d+-\d+/(?:\d+|\*)|\*/\d+)\Z")
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Server tracebacks and upstream errors can contain signed URLs.
        pass


class HLSRelay:
    def __init__(self, http, *, headers=None, cancel=None):
        self.http = http
        self.headers = dict(headers or {})
        self.cancel = cancel
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.resources = {}
        self.locations = {}
        self.failure = None
        self.server = None
        self.thread = None
        self.secret = secrets.token_urlsafe(24)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)

    def _start(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                relay._serve(self)

            def do_HEAD(self):
                relay._serve(self, head=True)

            def log_message(self, *args):
                pass

        self.server = RelayServer(("127.0.0.1", 0), Handler)
        self.authority = f"127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.1),
            name="f1tv-media-relay",
            daemon=True,
        )
        self.thread.start()

    def prepare(self, url, manifest):
        check_manifest(manifest)
        if not manifest.lstrip().startswith("#EXTM3U"):
            return url
        if self.server is None:
            self._start()
        return self._link(url, playlist=True)

    def run(self, operation, *args, **kwargs):
        if self.cancel is not None and self.cancel.is_set():
            raise OperationCancelled()
        self.failure = None
        try:
            return operation(*args, **kwargs)
        except MediaError:
            if self.cancel is not None and self.cancel.is_set():
                raise OperationCancelled() from None
            if self.failure is not None:
                raise self.failure from None
            raise

    def _stopped(self):
        return self.closed.is_set() or (self.cancel is not None and self.cancel.is_set())

    def _link(self, url, *, playlist=False):
        validate_media_url(url)
        suffix = urlsplit(url).path.rsplit("/", 1)[-1].rpartition(".")[2].lower()
        if playlist:
            suffix = "m3u8"
        elif not re.fullmatch(r"[a-z0-9]{1,8}", suffix):
            suffix = "mp4"
        key = (url, playlist)
        with self.lock:
            if key not in self.locations:
                path = f"/{self.secret}/{len(self.resources)}/media.{suffix}"
                self.resources[path] = key
                self.locations[key] = "http://" + self.authority + path
            return self.locations[key]

    def _rewrite(self, url, text):
        check_manifest(text)
        if not text.lstrip().startswith("#EXTM3U"):
            raise MediaError("An HLS playlist returned an unsupported media format.")

        def link(uri, *, playlist=False):
            target = urljoin(url, uri)
            if urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https":
                raise MediaError("Refusing an insecure HLS media URL.")
            playlist = playlist or urlsplit(target).path.lower().endswith(".m3u8")
            return self._link(target, playlist=playlist)

        lines = []
        variant = False
        for line in text.splitlines():
            if line.startswith("#"):
                playlist = line.startswith(
                    ("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-RENDITION-REPORT:")
                )
                line = URI_ATTRIBUTE.sub(
                    lambda match, playlist=playlist: (
                        'URI="' + link(match[1], playlist=playlist) + '"'
                    ),
                    line,
                )
                if line.startswith("#EXT-X-STREAM-INF:"):
                    variant = True
            elif line.strip():
                line = link(line.strip(), playlist=variant)
                variant = False
            lines.append(line)
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _serve(self, handler, *, head=False):
        with self.lock:
            resource = self.resources.get(urlsplit(handler.path).path)
        if handler.headers.get("Host") != self.authority or resource is None or self._stopped():
            handler.send_error(404)
            return
        url, playlist = resource
        headers = dict(self.headers)
        byte_range = handler.headers.get("Range")
        if byte_range and not playlist:
            if not BYTE_RANGE.fullmatch(byte_range):
                handler.send_error(416)
                return
            headers["Range"] = byte_range
        started = False
        try:
            with self.http.open_stream(
                url, headers=headers, operation="HLS media lookup"
            ) as response:
                if playlist:
                    try:
                        data = response.read(MAX_PLAYLIST_BYTES + 1)
                        if len(data) > MAX_PLAYLIST_BYTES:
                            raise MediaError(
                                "The service returned an excessively large HLS playlist."
                            )
                        text = data.decode("utf-8-sig")
                    except UnicodeError:
                        raise MediaError("The service returned an invalid HLS playlist.") from None
                    data = self._rewrite(response.geturl(), text)
                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    handler.send_header("Content-Length", str(len(data)))
                    handler.end_headers()
                    started = True
                    if not head:
                        handler.wfile.write(data)
                else:
                    handler.send_response(response.status)
                    handler.send_header("Content-Type", "application/octet-stream")
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit():
                        handler.send_header("Content-Length", length)
                    content_range = response.headers.get("Content-Range", "")
                    if CONTENT_RANGE.fullmatch(content_range):
                        handler.send_header("Content-Range", content_range)
                    handler.end_headers()
                    started = True
                    if not head:
                        while not self._stopped():
                            data = response.read(65536)
                            if not data:
                                break
                            try:
                                handler.wfile.write(data)
                            except (BrokenPipeError, ConnectionResetError):
                                return
        except (BrokenPipeError, ConnectionResetError):
            return
        except (F1TVError, OSError) as error:
            self.failure = (
                error
                if isinstance(error, F1TVError)
                else MediaError("HLS media transfer failed. Check network access and CA trust.")
            )
            if not started:
                handler.send_error((error.status or 502) if isinstance(error, APIError) else 502)
