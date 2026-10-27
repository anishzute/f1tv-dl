"""FFprobe stream selection and shell-free FFmpeg downloads."""

import json
import logging
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

from .config import BASE_URL, USER_AGENT
from .errors import DRMError, MediaError, OperationCancelled
from .http import validate_media_url

LOG = logging.getLogger(__name__)
LANGUAGES = {
    "en": "eng",
    "nl": "nld",
    "dut": "nld",
    "de": "deu",
    "ger": "deu",
    "fr": "fra",
    "fre": "fra",
    "pt": "por",
    "es": "spa",
    "ja": "jpn",
}


def normalize_language(value: str) -> str:
    value = value.casefold().strip()
    return LANGUAGES.get(value, value)


def check_manifest(text: str):
    """Reject DRM markers; ordinary HLS AES-128 is handled by FFmpeg."""
    text = text.lstrip()
    if text.startswith("#EXTM3U"):
        for line in text.splitlines():
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                if (
                    "SAMPLE-AES" in line.upper()
                    or re.search(r'KEYFORMAT="(?!identity")[^\"]+"', line, re.I)
                    or "skd://" in line.lower()
                ):
                    raise DRMError()
        return
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise MediaError("The playback URL did not return an HLS or DASH manifest.") from None
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        raise MediaError("The playback URL did not return an HLS or DASH manifest.")
    if any(node.tag.rsplit("}", 1)[-1] == "ContentProtection" for node in root.iter()):
        raise DRMError()


def require_binary(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise MediaError(f"{name} is required. Install FFmpeg and ensure it is on PATH.")
    return executable


def input_options(timeout: float, url: str, user_agent=None) -> list[str]:
    options = [
        "-user_agent",
        user_agent or USER_AGENT,
        "-rw_timeout",
        str(int(timeout * 1_000_000)),
    ]
    if user_agent:
        # FFmpeg propagates HTTP headers to HLS variants, init files, and segments.
        options += ["-headers", f"Origin: {BASE_URL}\r\n"]
    if urlsplit(url).scheme == "https":
        options += ["-tls_verify", "1"]
        ca_file = os.environ.get("SSL_CERT_FILE") or ssl.get_default_verify_paths().cafile
        if ca_file:
            options += ["-ca_file", ca_file]
    return options


def media_environment():
    # FFmpeg recognizes lowercase proxy variables; urllib also accepts uppercase.
    environment = os.environ.copy()
    for name in ("http_proxy", "https_proxy", "no_proxy"):
        if name not in environment and name.upper() in environment:
            environment[name] = environment[name.upper()]
    # The private HLS relay is local; remote traffic retains the configured proxies.
    environment["no_proxy"] = ",".join(
        filter(None, (environment.get("no_proxy", ""), "localhost", "127.0.0.1", "::1"))
    )
    return environment


def media_failure(tool: str, status: int, diagnostics: str) -> MediaError:
    """Classify diagnostics using fixed messages; raw output can contain bearer URLs."""
    text = diagnostics.casefold()
    http_status = re.search(r"(?:http error|server returned)\s+([45]\d{2})\b", text)
    if re.search(
        r"unrecognized option|option \S+ not found|error (?:setting|applying) option", text
    ):
        detail = (
            "This FFmpeg build does not support a required option. "
            "Install a current FFmpeg release."
        )
    elif any(
        marker in text
        for marker in (
            "certificate verification failed",
            "certificate verify failed",
            "certificate is not trusted",
            "unable to get local issuer certificate",
            "peer's certificate",
        )
    ):
        detail = (
            "TLS certificate verification failed. "
            "Check your system CA certificates and SSL_CERT_FILE."
        )
    elif http_status:
        detail = f"The media server returned HTTP {http_status[1]}."
        if http_status[1] in {"401", "403"}:
            detail += " Sign in again and retry."
        elif http_status[1] == "404":
            detail += " The playlist or a media segment is unavailable."
    elif any(
        marker in text
        for marker in ("name or service not known", "getaddrinfo", "failed to resolve")
    ):
        detail = "The media hostname could not be resolved. Check DNS and proxy settings."
    elif "timed out" in text or "timeout" in text:
        detail = (
            "The media connection timed out. "
            "Check network access or increase the timeout in Settings."
        )
    elif "connection refused" in text or "network is unreachable" in text:
        detail = "The media server could not be reached. Check network and proxy settings."
    elif "tls" in text and ("handshake" in text or "error" in text):
        detail = "The TLS connection failed. Check proxy settings and CA trust."
    elif "not on whitelist" in text or "protocol not found" in text:
        detail = (
            "This FFmpeg build cannot open a required media protocol. "
            "Install a current FFmpeg release."
        )
    elif "allowed_extensions" in text or "allowed_segment_extensions" in text:
        detail = "FFmpeg rejected a media segment's file type. Install a current FFmpeg release."
    elif "decoder not found" in text or "unknown decoder" in text:
        detail = "This FFmpeg build is missing a required decoder. Install a full FFmpeg build."
    elif "moov atom not found" in text or "invalid data found" in text:
        detail = "The playlist or media data could not be parsed. Update FFmpeg and retry."
    else:
        detail = "Check stream access, availability, and FFmpeg support."
    prefix = (
        f"{tool} failed (exit code {status})."
        if status
        else f"{tool} could not read any media streams."
    )
    return MediaError(f"{prefix} {detail}")


def probe(url: str, *, timeout: float = 30.0, user_agent=None) -> dict:
    validate_media_url(url)
    command = [
        require_binary("ffprobe"),
        "-v",
        "warning",
        *input_options(timeout, url, user_agent),
        "-show_streams",
        "-show_programs",
        "-of",
        "json",
        url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 15,
            check=False,
            env=media_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise MediaError("FFprobe timed out while reading the stream.") from None
    except OSError:
        raise MediaError("FFprobe could not be started.") from None
    if result.returncode:
        raise media_failure("FFprobe", result.returncode, result.stderr)
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
            raise ValueError
    except (ValueError, TypeError):
        raise MediaError("FFprobe returned invalid stream information.") from None
    if not payload["streams"]:
        raise media_failure("FFprobe", 0, result.stderr)
    return payload


def numeric(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def video_rank(stream):
    try:
        frame_rate = Fraction(stream.get("avg_frame_rate", "0/1"))
    except (ValueError, ZeroDivisionError, TypeError):
        frame_rate = Fraction(0)
    return (
        numeric(stream.get("width")) * numeric(stream.get("height")),
        frame_rate,
        numeric(stream.get("bit_rate") or stream.get("tags", {}).get("variant_bitrate")),
    )


@dataclass(frozen=True)
class Selection:
    video: int | None
    audio: int
    language: str
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class DownloadProgress:
    seconds: float = 0
    total_bytes: int = 0
    speed: str = ""


def select_streams(
    payload: dict, language: str = "eng", resolution: str = "best", *, audio_only=False
) -> Selection:
    streams = payload.get("streams", [])
    videos = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ]
    video = None
    if not audio_only:
        if resolution != "best":
            try:
                width, height = map(int, resolution.split("x"))
            except (ValueError, AttributeError):
                raise MediaError("Video size must be 'best' or WIDTHxHEIGHT.") from None
            videos = [
                stream
                for stream in videos
                if numeric(stream.get("width")) == width and numeric(stream.get("height")) == height
            ]
        if not videos:
            raise MediaError(f"No video stream matches {resolution}.")
        video = max(videos, key=video_rank)
        # HLS exposes variants as programs. Keep video and audio in the same program.
        programs = [
            program
            for program in payload.get("programs", [])
            if any(s.get("index") == video.get("index") for s in program.get("streams", []))
        ]
        if programs:
            streams = programs[0].get("streams", [])
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not audio:
        raise MediaError("No audio streams are available for the selected feed.")
    requested = normalize_language(language)
    matches = [
        stream
        for stream in audio
        if normalize_language(stream.get("tags", {}).get("language", "und")) == requested
    ]
    if not matches:
        available = sorted({s.get("tags", {}).get("language", "und") for s in audio})
        LOG.warning(
            "Audio language %s is unavailable (available: %s); using the default track.",
            requested,
            ", ".join(available),
        )
        matches = [stream for stream in audio if stream.get("disposition", {}).get("default")]
        matches = matches or audio[:1]
    selected_audio = max(matches, key=lambda s: numeric(s.get("bit_rate")))
    try:
        return Selection(
            video=int(video["index"]) if video else None,
            audio=int(selected_audio["index"]),
            language=normalize_language(selected_audio.get("tags", {}).get("language", requested)),
            width=numeric(video.get("width")) if video else 0,
            height=numeric(video.get("height")) if video else 0,
        )
    except (KeyError, ValueError, TypeError):
        raise MediaError("FFprobe returned invalid stream indexes.") from None


def build_command(
    binary: str,
    url: str,
    selection: Selection,
    output: Path,
    *,
    output_format="mp4",
    secondary=None,
    offset="00:00:00.000",
    duration=None,
    timeout=30.0,
    user_agent=None,
):
    if selection.video is None:
        raise MediaError("A video stream is required for the primary feed.")
    command = [
        binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
        *input_options(timeout, url, user_agent),
        "-i",
        validate_media_url(url),
    ]
    if secondary is not None:
        secondary_url, secondary_selection = secondary
        command += [
            *input_options(timeout, secondary_url, user_agent),
            "-itsoffset",
            offset,
            "-i",
            validate_media_url(secondary_url),
        ]
    command += ["-map", f"0:{selection.video}", "-map", f"0:{selection.audio}"]
    if secondary is not None:
        command += ["-map", f"1:{secondary_selection.audio}"]
    command += [
        "-c",
        "copy",
        "-metadata:s:a:0",
        f"language={selection.language}",
        "-disposition:a:0",
        "default",
    ]
    if secondary is not None:
        command += [
            "-metadata:s:a:1",
            f"language={secondary_selection.language}",
            "-metadata:s:a:1",
            "title=International commentary",
            "-disposition:a:1",
            "0",
        ]
    if duration is not None:
        command += ["-t", str(duration)]
    if output_format == "mp4":
        command += ["-movflags", "+faststart", "-f", "mp4"]
    elif output_format == "ts":
        command += ["-f", "mpegts"]
    else:
        raise MediaError("Output format must be mp4 or ts.")
    # Only the private temporary file is overwritten. The final file has separate checks.
    return command + ["-y", str(output)]


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def download(
    url, selection, output: Path, *, overwrite=False, progress=None, cancel=None, **options
):
    if cancel is not None and cancel.is_set():
        raise OperationCancelled()
    binary = require_binary("ffmpeg")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise MediaError("Output file already exists. Use --overwrite to replace it.")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem[:80]}-",
        suffix=f".part{output.suffix}",
        dir=output.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    process = None
    reader = None
    try:
        command = build_command(binary, url, selection, temporary, **options)
        # Keep diagnostics out of the console: they can contain signed URLs and account tokens.
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                env=media_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            lines = queue.Queue()

            def read_progress():
                try:
                    for line in process.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)

            reader = threading.Thread(target=read_progress, name="ffmpeg-progress", daemon=True)
            reader.start()
            values = {}
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise OperationCancelled()
                    try:
                        line = lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        break
                    key, _, value = line.strip().partition("=")
                    values[key] = value
                    if key == "progress" and progress is not None:
                        progress(
                            DownloadProgress(
                                seconds=max(0, numeric(values.get("out_time_us"))) / 1_000_000,
                                total_bytes=max(0, numeric(values.get("total_size"))),
                                speed=values.get("speed", "").strip(),
                            )
                        )
                    if line.startswith("out_time=") and sys.stderr and sys.stderr.isatty():
                        print(
                            "\rDownloaded " + line.strip().split("=", 1)[1], end="", file=sys.stderr
                        )
                status = process.wait()
            finally:
                stop_process(process)
                reader.join(timeout=5)
                process.stdout.close()
                if sys.stderr and sys.stderr.isatty():
                    print(file=sys.stderr)
            errors.seek(0)
            diagnostics = errors.read(65536).decode("utf-8", errors="replace") if status else ""
        if cancel is not None and cancel.is_set():
            raise OperationCancelled()
        if status:
            raise media_failure("FFmpeg", status, diagnostics)
        if temporary.stat().st_size == 0:
            raise MediaError("FFmpeg returned an empty download.")
        if overwrite:
            os.replace(temporary, output)
        else:
            # A hard link publishes atomically and also protects against a concurrent writer.
            try:
                os.link(temporary, output)
            except FileExistsError:
                raise MediaError(
                    "Output file was created by another process; it was preserved."
                ) from None
    finally:
        if process is not None:
            stop_process(process)
        if reader is not None:
            reader.join(timeout=5)
        temporary.unlink(missing_ok=True)
