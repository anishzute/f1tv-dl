"""Command-line entry point, preserving the original f1tv-dl flags."""

import argparse
import getpass
import logging
import math
import os
import re
import sys
from pathlib import Path

from . import __version__
from .api import F1TVClient
from .auth import Authenticator, SessionManager, SessionStore
from .config import Settings, default_session_path
from .errors import AuthenticationError, F1TVError
from .http import HttpClient
from .media import download, probe, require_binary, select_streams
from .models import ContentRef, safe_filename
from .relay import HLSRelay

LOG = logging.getLogger(__name__)


def positive_number(value):
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number") from None


def video_size(value):
    if value != "best" and not re.fullmatch(r"[1-9]\d*x[1-9]\d*", value):
        raise argparse.ArgumentTypeError("must be 'best' or WIDTHxHEIGHT")
    return value


def time_offset(value):
    value = value.strip("'")
    if not re.fullmatch(r"-?\d{2}:[0-5]\d:[0-5]\d\.\d{3}", value):
        raise argparse.ArgumentTypeError("must be (-)HH:MM:SS.mmm")
    return value


def parser():
    command = argparse.ArgumentParser(
        prog="f1tv-dl",
        description="List F1 TV feeds or download a video using your subscription.",
        epilog="For negative audio offsets use --itsoffset=-00:00:04.750.",
    )
    command.add_argument("url", help="HTTPS F1 TV detail URL")
    command.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    command.add_argument(
        "-c", "--channel", help="Feed name, driver name, number, or three-letter code"
    )
    modes = command.add_mutually_exclusive_group()
    modes.add_argument("--channel-list", action="store_true", help="List feeds without logging in")
    modes.add_argument(
        "--stream-url", action="store_true", help="Print the authorized playback URL"
    )
    command.add_argument(
        "-a", "--audio-stream", default="eng", help="Primary audio language (default: eng)"
    )
    command.add_argument(
        "-i",
        "--international-audio",
        choices=["eng", "nld", "deu", "fra", "por", "spa", "jpn", "fx"],
        help="Add an audio track from the INTERNATIONAL feed",
    )
    command.add_argument("-t", "--itsoffset", type=time_offset, default="00:00:00.000")
    command.add_argument("-s", "--video-size", type=video_size, default="best")
    command.add_argument("-f", "--format", choices=["mp4", "ts"], default="mp4")
    command.add_argument(
        "-o",
        "--output-directory",
        type=Path,
        default=Path(os.environ.get("F1TV_OUTDIR", ".")),
    )
    command.add_argument("-U", "--username", default=os.environ.get("F1TV_USER"))
    command.add_argument(
        "-P",
        "--password",
        default=os.environ.get("F1TV_PASSWORD") or os.environ.get("F1TV_PASS"),
        help="F1 TV password; prefer F1TV_PASSWORD or the interactive prompt",
    )
    command.add_argument(
        "--token", default=os.environ.get("F1TV_TOKEN"), help="Existing entitlement token"
    )
    command.add_argument("--login-method", choices=["password", "browser"], default="password")
    command.add_argument(
        "--headless", action="store_true", help="Use a headless browser for browser login"
    )
    command.add_argument("--browser-executable", help="Path to an existing Chromium executable")
    command.add_argument(
        "--session-file", type=Path, help="Session cache path (default: platform cache)"
    )
    command.add_argument(
        "--no-cache", action="store_true", help="Do not read or save session files"
    )
    command.add_argument("--country", default=os.environ.get("F1TV_COUNTRY", "USA"))
    command.add_argument(
        "--entitlement", default=os.environ.get("F1TV_ENTITLEMENT", "F1_TV_Pro_Annual")
    )
    command.add_argument(
        "--timeout", type=positive_number, default=30.0, help="Network timeout in seconds"
    )
    command.add_argument(
        "--duration", type=positive_number, help="Limit download to this many seconds"
    )
    command.add_argument(
        "--overwrite", action="store_true", help="Replace an existing output after success"
    )
    command.add_argument(
        "-l", "--log-level", choices=["trace", "debug", "info", "warn", "error"], default="info"
    )
    return command


def credentials(args):
    username, password = args.username, args.password
    if args.login_method == "browser":
        return username, password
    if not (username and password) and not sys.stdin.isatty():
        raise AuthenticationError(
            "Login required. Set F1TV_USER and F1TV_PASSWORD (F1TV_PASS also works), "
            "or supply F1TV_TOKEN."
        )
    if not username:
        username = input("F1 TV username: ").strip()
    if not password:
        password = getpass.getpass("F1 TV password: ")
    return username, password


def run(args):
    ContentRef.parse(args.url)
    if not re.fullmatch(r"[A-Za-z]{2,3}", args.country):
        raise F1TVError("Country must be a two- or three-letter country code.")
    if not re.fullmatch(r"[A-Za-z0-9_]+", args.entitlement):
        raise F1TVError("Entitlement must contain only letters, digits, and underscores.")
    http = HttpClient(timeout=args.timeout)
    client = F1TVClient(http, Settings(args.country.upper(), args.entitlement, args.timeout))
    content = client.content(args.url)
    if args.channel_list:
        print(content.title)
        if not content.channels:
            print("This video has no alternate channels.")
        for channel in content.channels:
            details = [channel.title]
            if channel.driver_name:
                details += [channel.driver_name, "#" + channel.racing_number]
            if channel.default:
                details.append("default")
            print(" | ".join(details))
        return
    channel = content.select_channel(args.channel)
    if args.international_audio and not content.channels:
        raise F1TVError("This video has no INTERNATIONAL feed for secondary audio.")
    international = content.select_channel("INTERNATIONAL") if args.international_audio else None
    # Validate dependencies and destination before any credential submission.
    output = None
    if not args.stream_url:
        require_binary("ffmpeg")
        require_binary("ffprobe")
        suffix = "-" + safe_filename(channel.title) if args.channel and channel else ""
        output = args.output_directory / (
            safe_filename(content.ref.name) + suffix + "." + args.format
        )
        if output.exists() and not args.overwrite:
            raise F1TVError("Output file already exists. Use --overwrite to replace it.")
    authenticator = Authenticator(http)

    def login():
        username, password = credentials(args)
        if args.login_method == "browser":
            return authenticator.browser_login(
                username,
                password,
                headless=args.headless,
                executable=args.browser_executable,
            )
        LOG.info("Signing in to F1 TV.")
        return authenticator.password_login(username, password)

    sessions = SessionManager(
        SessionStore(None if args.no_cache else args.session_file or default_session_path()),
        login,
        token=args.token,
    )
    playback = client.playback(content, channel, sessions)
    if args.stream_url:
        print(playback.url)
        return
    agent = sessions.web_client.user_agent if sessions.web_client is not None else None
    media_headers = sessions.web_client.media_headers() if sessions.web_client is not None else None
    with HLSRelay(http, headers=media_headers) as relay:
        url = relay.prepare(
            playback.url,
            http.get_text(playback.url, headers=media_headers, operation="Manifest lookup"),
        )
        selected = select_streams(
            relay.run(probe, url, timeout=args.timeout, user_agent=agent),
            args.audio_stream,
            args.video_size,
        )
        secondary = None
        if international is not None:
            secondary_playback = client.playback(content, international, sessions)
            secondary_url = relay.prepare(
                secondary_playback.url,
                http.get_text(
                    secondary_playback.url,
                    headers=media_headers,
                    operation="International manifest lookup",
                ),
            )
            secondary_selection = select_streams(
                relay.run(probe, secondary_url, timeout=args.timeout, user_agent=agent),
                args.international_audio,
                audio_only=True,
            )
            secondary = (secondary_url, secondary_selection)
        LOG.info(
            "Downloading %s (%sx%s, audio %s) to %s",
            channel.title if channel else content.title,
            selected.width,
            selected.height,
            selected.language,
            output,
        )
        relay.run(
            download,
            url,
            selected,
            output,
            overwrite=args.overwrite,
            output_format=args.format,
            secondary=secondary,
            offset=args.itsoffset,
            duration=args.duration,
            timeout=args.timeout,
            user_agent=agent,
        )
    LOG.info("Download complete: %s", output)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    level = {"trace": logging.DEBUG, "warn": logging.WARNING}.get(args.log_level)
    level = level or getattr(logging, args.log_level.upper())
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)
    try:
        run(args)
        return 0
    except (F1TVError, OSError, EOFError) as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        LOG.error("Cancelled.")
        return 130
