"""Desktop operations independent of Tk, sharing the CLI's API and media modules."""

import math
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .api import F1TVClient
from .auth import Authenticator, SessionManager, SessionStore, token_is_fresh
from .config import Settings, default_session_path
from .errors import AuthenticationError, F1TVError, OperationCancelled
from .http import HttpClient
from .media import check_manifest, download, probe, require_binary, stop_process
from .media import select_streams as select_media
from .models import safe_filename


@dataclass(frozen=True)
class DownloadOptions:
    directory: Path
    channel: str | None = None
    resolution: str = "best"
    audio: str = "eng"
    international: str | None = None
    offset: str = "00:00:00.000"
    format: str = "mp4"
    duration: float | None = None
    overwrite: bool = False

    def validate(self):
        if self.resolution != "best" and not re.fullmatch(r"[1-9]\d*x[1-9]\d*", self.resolution):
            raise F1TVError("Choose Best available or enter a resolution such as 1920x1080.")
        if self.format not in {"mp4", "ts"}:
            raise F1TVError("Choose MP4 or TS as the file format.")
        if not re.fullmatch(r"-?\d{2}:[0-5]\d:[0-5]\d\.\d{3}", self.offset):
            raise F1TVError("Audio offset must use HH:MM:SS.mmm, optionally with a minus sign.")
        if self.duration is not None and (not math.isfinite(self.duration) or self.duration <= 0):
            raise F1TVError("Sample duration must be a positive number of seconds, or blank.")


@dataclass(frozen=True)
class VideoInfo:
    resolutions: tuple[str, ...]
    languages: tuple[str, ...]


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise OperationCancelled()


def require_sign_in():
    raise AuthenticationError(
        "Sign in on the Account tab to continue. Your saved session may have expired."
    )


class DesktopService:
    def __init__(self, settings=None, session_path=None):
        self.settings = settings or Settings()
        self.http = HttpClient(timeout=self.settings.timeout)
        self.client = F1TVClient(self.http, self.settings)
        self.auth = Authenticator(self.http)
        self.store = SessionStore(
            session_path if session_path is not None else default_session_path()
        )
        self.sessions = SessionManager(self.store, require_sign_in)

    @property
    def signed_in(self):
        return bool(self.sessions.token and token_is_fresh(self.sessions.token))

    def sign_in(
        self,
        method,
        *,
        username="",
        password="",
        token="",
        remember=True,
        executable=None,
        cancel=None,
        status=None,
    ):
        check_cancel(cancel)
        if method == "browser":
            session_token = self.auth.browser_login(
                username or None,
                password or None,
                executable=executable or None,
                cancel=cancel,
                status=status,
            )
        elif method == "password":
            session_token = self.auth.password_login(username.strip(), password)
        elif method == "token":
            session_token = token.strip()
        else:
            raise F1TVError("Choose a sign-in method.")
        check_cancel(cancel)
        sessions = SessionManager(SessionStore(None), require_sign_in, token=session_token)
        sessions.explicit = method == "token"
        # Keep the old session intact if the login above fails. Explicit tokens stay in memory.
        if method != "token":
            if remember:
                self.store.save(session_token)
            else:
                self.store.clear()
        self.sessions = sessions

    def sign_out(self):
        self.sessions = SessionManager(SessionStore(None), require_sign_in)
        self.store.clear()

    def load_video(self, url):
        return self.client.content(url)

    def stream_url(self, content, channel=None):
        return self.client.playback(content, content.select_channel(channel), self.sessions).url

    def inspect(self, content, channel=None, cancel=None):
        url = self.stream_url(content, channel)
        check_cancel(cancel)
        check_manifest(self.http.get_text(url, operation="Manifest lookup"))
        payload = probe(url, timeout=self.settings.timeout)
        check_cancel(cancel)
        streams = payload.get("streams", [])
        resolutions = {
            (s.get("width", 0), s.get("height", 0))
            for s in streams
            if s.get("codec_type") == "video" and s.get("width") and s.get("height")
        }
        languages = {
            s.get("tags", {}).get("language", "und")
            for s in streams
            if s.get("codec_type") == "audio"
        }
        return VideoInfo(
            tuple(
                f"{w}x{h}"
                for w, h in sorted(resolutions, key=lambda wh: wh[0] * wh[1], reverse=True)
            ),
            tuple(sorted(languages)),
        )

    def download(self, content, options, *, progress=None, status=None, cancel=None):
        options.validate()
        require_binary("ffmpeg")
        require_binary("ffprobe")
        channel = content.select_channel(options.channel)
        international = content.select_channel("INTERNATIONAL") if options.international else None
        if options.international and international is None:
            raise F1TVError("This video does not offer an INTERNATIONAL feed.")
        suffix = "-" + safe_filename(channel.title) if options.channel and channel else ""
        output = options.directory / (
            safe_filename(content.ref.name) + suffix + "." + options.format
        )
        if output.exists() and not options.overwrite:
            raise F1TVError(
                "This file already exists. Enable Replace existing file to overwrite it."
            )
        check_cancel(cancel)
        if status:
            status("Checking the selected video and audio…")
        url = self.stream_url(content, options.channel)
        check_manifest(self.http.get_text(url, operation="Manifest lookup"))
        check_cancel(cancel)
        selected = select_media(
            probe(url, timeout=self.settings.timeout), options.audio, options.resolution
        )
        secondary = None
        if international is not None:
            check_cancel(cancel)
            secondary_url = self.stream_url(content, "INTERNATIONAL")
            check_manifest(
                self.http.get_text(secondary_url, operation="International manifest lookup")
            )
            selected_audio = select_media(
                probe(secondary_url, timeout=self.settings.timeout),
                options.international,
                audio_only=True,
            )
            secondary = (secondary_url, selected_audio)
        check_cancel(cancel)
        if status:
            status(f"Downloading {selected.width}×{selected.height}, audio {selected.language}…")
        download(
            url,
            selected,
            output,
            overwrite=options.overwrite,
            progress=progress,
            cancel=cancel,
            output_format=options.format,
            secondary=secondary,
            offset=options.offset,
            duration=options.duration,
            timeout=self.settings.timeout,
        )
        return output.resolve()


def install_browser_support(cancel=None, status=None):
    """Install the optional login browser after the user clicks its setup button."""
    steps = [
        (
            "Installing browser support…",
            [sys.executable, "-m", "pip", "install", "playwright>=1.48,<2"],
        ),
        (
            "Downloading the sign-in browser…",
            [sys.executable, "-m", "playwright", "install", "chromium"],
        ),
    ]
    for message, command in steps:
        check_cancel(cancel)
        if status:
            status(message)
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            while process.poll() is None:
                check_cancel(cancel)
                threading.Event().wait(0.1)
            if process.returncode:
                raise F1TVError(
                    "Browser setup failed. Check internet access and that this Python "
                    "environment allows installing packages. "
                    "You can also use password or token sign-in."
                )
        finally:
            stop_process(process)
