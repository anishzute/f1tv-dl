"""Inspect entitled manifests without fetching media, keys, or DRM licenses."""

import json
import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.client import HTTPException
from urllib.parse import urljoin, urlsplit

from . import __version__
from .api import result_object, select_playback_url
from .config import BASE_URL
from .errors import APIError, AuthenticationError, F1TVError, OperationCancelled
from .http import validate_media_url
from .web import WebClient

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MANIFESTS = 6
ATTRIBUTES = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))')
DRM_IDS = {
    "widevine": "Widevine",
    "playready": "PlayReady",
    "fairplay": "FairPlay",
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "Widevine",
    "9a04f079-9840-4286-ab92-e65be0885f95": "PlayReady",
    "94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "FairPlay",
    "com.apple.streamingkeydelivery": "FairPlay",
    "com.widevine": "Widevine",
    "com.microsoft.playready": "PlayReady",
}
PROFILES = (
    ("web", "Web", "WEB_HLS", False),
    ("android_tv_header", "Android TV header (experimental)", "WEB_HLS", True),
    ("android_tv_big_screen", "Android TV big-screen (experimental)", "BIG_SCREEN_HLS", True),
)


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise OperationCancelled()


@dataclass
class ProfileReport:
    profile: str
    label: str
    http_status: int | None = None
    manifest_http_status: int | None = None
    stream_type: str = "Unknown"
    api_drm_marked: bool = False
    manifests_checked: int = 0
    manifest_formats: list[str] = field(default_factory=list)
    resolutions: list[str] = field(default_factory=list)
    codecs: list[str] = field(default_factory=list)
    encryption: list[str] = field(default_factory=list)
    drm_systems: list[str] = field(default_factory=list)
    tiled_video: bool = False
    error: str | None = None

    @property
    def uhd(self):
        return any(int(size.split("x")[1]) >= 2160 for size in self.resolutions)


@dataclass
class ManifestReport:
    content_id: str
    feed: str
    profiles: list[ProfileReport]
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    app_version: str = __version__
    report_version: int = 1
    notes: tuple[str, ...] = (
        "Android TV requests are experimental header/endpoint probes, not hardware emulation.",
        "Manifest dimensions do not establish DRM license eligibility or playable downloads.",
        "HLS follows the highest video variant and associated audio, up to six manifests.",
        "No video segments, encryption keys, or licenses are requested.",
    )

    @property
    def inspected(self):
        return any(profile.manifests_checked for profile in self.profiles)

    def to_json(self):
        return json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n"

    def to_text(self):
        lines = ["F1 TV manifest check", f"Content: {self.content_id} | Feed: {self.feed}", ""]
        for profile in self.profiles:
            status = f"HTTP {profile.http_status}" if profile.http_status else "No HTTP response"
            lines.extend(
                (profile.label, f"  Playback: {status}; stream type: {profile.stream_type}")
            )
            lines.append("  Resolutions: " + (", ".join(profile.resolutions) or "Not verified"))
            if profile.manifest_formats:
                lines.append("  Manifest format: " + ", ".join(profile.manifest_formats))
            if profile.tiled_video:
                uhd = "Not verified (tiled video)"
            elif profile.uhd:
                uhd = "2160p or higher advertised (playability not verified)"
            elif profile.manifests_checked and not profile.error and not profile.tiled_video:
                uhd = "Not advertised in inspected manifests"
            else:
                uhd = "Not verified"
            lines.append("  4K: " + uhd)
            drm = ", ".join(profile.drm_systems)
            if not drm:
                drm = (
                    "API marks DRM"
                    if profile.api_drm_marked
                    else ("No markers detected" if profile.manifests_checked else "Not verified")
                )
            lines.append("  DRM: " + drm)
            if profile.encryption:
                lines.append("  Encryption: " + ", ".join(profile.encryption))
            if profile.codecs:
                lines.append("  Codecs: " + ", ".join(profile.codecs))
            lines.append(f"  Manifests inspected: {profile.manifests_checked}")
            if profile.tiled_video:
                lines.append(
                    "  Video uses tiles; representation sizes may differ from playback size."
                )
            if profile.error:
                lines.append("  Issue: " + profile.error)
            lines.append("")
        lines.extend(self.notes)
        lines.append("Reports omit tokens, signed URLs, license data, and raw responses.")
        return "\n".join(lines) + "\n"


def drm_name(value):
    value = str(value).lower()
    if value == "wv":
        return "Widevine"
    return next((label for marker, label in DRM_IDS.items() if marker in value), "Unspecified DRM")


def add_unique(target, values):
    target[:] = sorted(set(target).union(values))


def add_size(report, width, height):
    if re.fullmatch(r"[1-9]\d{0,5}", str(width)) and re.fullmatch(r"[1-9]\d{0,5}", str(height)):
        add_unique(report.resolutions, [f"{width}x{height}"])
        report.resolutions.sort(key=lambda size: tuple(map(int, size.split("x"))), reverse=True)


def add_codecs(report, value):
    # Report codec families only, never arbitrary manifest attribute contents.
    codecs = []
    for codec in str(value).lower().split(","):
        family = codec.strip().split(".")[0]
        if family in {
            "avc1",
            "avc3",
            "hvc1",
            "hev1",
            "av01",
            "vp09",
            "mp4a",
            "ac-3",
            "ec-3",
            "opus",
        }:
            codecs.append(family)
    add_unique(report.codecs, codecs)


def attributes(line):
    return {
        match[1]: match[2] if match[2] is not None else match[3]
        for match in ATTRIBUTES.finditer(line.partition(":")[2])
    }


def inspect_hls(text, report):
    variants, audio = [], []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            attrs = attributes(line)
            method = attrs.get("METHOD", "").upper()
            if method in {"AES-128", "SAMPLE-AES", "SAMPLE-AES-CTR"}:
                add_unique(report.encryption, [method])
            keyformat = attrs.get("KEYFORMAT", "identity")
            if keyformat != "identity":
                add_unique(report.drm_systems, [drm_name(keyformat)])
            elif method.startswith("SAMPLE-AES"):
                add_unique(report.drm_systems, ["Unspecified DRM"])
            if attrs.get("URI", "").lower().startswith("skd:"):
                add_unique(report.drm_systems, ["FairPlay"])
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = attributes(line)
            if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                audio.append(attrs)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = attributes(line)
            size = pending.get("RESOLUTION", "").split("x")
            if len(size) == 2:
                add_size(report, *size)
            add_codecs(report, pending.get("CODECS", ""))
        elif line and not line.startswith("#") and pending is not None:
            variants.append((pending, line))
            pending = None
    children = []
    if variants:

        def rank(item):
            size = re.fullmatch(r"(\d{1,6})x(\d{1,6})", item[0].get("RESOLUTION", ""))
            bitrate = item[0].get("BANDWIDTH", "0")
            return (
                int(size[1]) * int(size[2]) if size else 0,
                int(bitrate) if re.fullmatch(r"\d{1,12}", bitrate) else 0,
            )

        selected, uri = max(variants, key=rank)
        children.append(uri)
        # Only audio associated with the chosen video variant, not subtitles or I-frame media.
        children.extend(
            item["URI"] for item in audio if item.get("GROUP-ID") == selected.get("AUDIO")
        )
    return children


def inspect_dash(text, report):
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise F1TVError("Manifest inspection refused XML document declarations.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise F1TVError("The playback response was not an HLS or DASH manifest.") from None
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        raise F1TVError("The playback response was not an HLS or DASH manifest.")
    for adaptation in root.iter():
        if adaptation.tag.rsplit("}", 1)[-1] != "AdaptationSet":
            continue
        for representation in adaptation:
            if representation.tag.rsplit("}", 1)[-1] == "Representation":
                add_size(
                    report,
                    representation.get("width", adaptation.get("width", "")),
                    representation.get("height", adaptation.get("height", "")),
                )
                add_codecs(report, representation.get("codecs", adaptation.get("codecs", "")))
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1]
        scheme = node.get("schemeIdUri", "").lower()
        if name == "ContentProtection":
            if "mp4protection" in scheme:
                add_unique(report.encryption, ["Common encryption (CENC)"])
            else:
                add_unique(report.drm_systems, [drm_name(scheme)])
        if "urn:mpeg:dash:srd" in scheme:
            report.tiled_video = True
    if report.encryption and not report.drm_systems:
        add_unique(report.drm_systems, ["Unspecified DRM"])


def inspect_manifests(http, url, report, *, headers, cancel=None):
    pending, visited = deque([url]), set()
    while pending and len(visited) < MAX_MANIFESTS:
        check_cancel(cancel)
        url = pending.popleft()
        if url in visited:
            continue
        visited.add(url)
        validate_media_url(url)
        if urlsplit(url).scheme != "https":
            raise F1TVError("Manifest inspection requires HTTPS for all playlists.")
        with http.open_stream(url, headers=headers, operation="Manifest lookup") as response:
            final_url = response.geturl()
            validate_media_url(final_url)
            if urlsplit(final_url).scheme != "https":
                raise F1TVError("Manifest inspection refused an insecure redirect.")
            chunks, length = [], 0
            while True:
                check_cancel(cancel)
                chunk = response.read(min(65536, MAX_MANIFEST_BYTES + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > MAX_MANIFEST_BYTES:
                    raise F1TVError("Manifest exceeds the 8 MB inspection limit.")
        try:
            text = b"".join(chunks).decode("utf-8-sig").lstrip()
        except UnicodeError:
            raise F1TVError("The playback response was not a UTF-8 manifest.") from None
        if text.startswith("#EXTM3U"):
            children = inspect_hls(text, report)
            pending.extend(urljoin(final_url, child) for child in children)
            add_unique(report.manifest_formats, ["HLS"])
        else:
            inspect_dash(text, report)
            add_unique(report.manifest_formats, ["DASH"])
        report.manifests_checked += 1
    if pending:
        raise F1TVError("Reached the six-manifest inspection limit; results are partial.")


def check_manifests(http, content, channel, sessions, *, cancel=None, status=None):
    """Compare web playback with experimental Android TV device/endpoint probes."""
    check_cancel(cancel)
    token = sessions.get()
    subscription = sessions.subscription_token
    if not isinstance(subscription, str) or not subscription:
        raise AuthenticationError(
            "Manifest comparison requires Browser sign-in. Sign in on the Account tab, "
            "or use --login-method browser --no-cache in the CLI."
        )
    client = sessions.web_client or WebClient.create()
    selected = channel.web_channel_id if channel is not None else None
    params = {"contentId": content.ref.id, "player": "player_tm"}
    if selected is not None:
        params["channelId"] = selected
    report = ManifestReport(content.ref.id, channel.title if channel else "Main feed", [])
    for key, label, platform, android in PROFILES:
        check_cancel(cancel)
        if status:
            status(f"Checking {label.lower()} manifest...")
        check_cancel(cancel)
        profile = ProfileReport(key, label)
        report.profiles.append(profile)
        headers = {
            **client.headers("android-tv" if android else "web"),
            "ascendontoken": subscription,
            "entitlementToken": token,
        }
        try:
            payload = http.get_json(
                BASE_URL + f"/3.0/R/ENG/{platform}/ALL/CONTENT/PLAY",
                params=params,
                headers=headers,
                operation="Playback authorization",
            )
            profile.http_status = 200
            result = result_object(payload)
            kind = result.get("streamType", "")
            if isinstance(kind, str) and re.fullmatch(
                r"(?:HLS|DASH|(?:SDR|HDR)[_-](?:HD|UHD|SD)[A-Z0-9_-]{0,32})", kind.upper()
            ):
                profile.stream_type = kind.upper()
            profile.api_drm_marked = bool(
                result.get("drmType") or result.get("drmToken") or result.get("isDrmProtected")
            )
            if result.get("drmType"):
                add_unique(profile.drm_systems, [drm_name(result["drmType"])])
            url = select_playback_url(result, selected)
            if not isinstance(url, str) or not url:
                raise F1TVError("F1 TV returned no manifest URL for the selected feed.")
            inspect_manifests(http, url, profile, headers=client.media_headers(), cancel=cancel)
        except OperationCancelled:
            raise
        except APIError as error:
            if profile.http_status is None:
                profile.http_status = error.status
            else:
                profile.manifest_http_status = error.status
            profile.error = str(error)
            if error.status == 451:
                profile.error += (
                    " Access is restricted; this response does not establish UHD eligibility."
                )
            elif error.status == 401:
                profile.error += " Sign in again before repeating the check."
        except F1TVError as error:
            profile.error = str(error)
        except (OSError, HTTPException):
            profile.error = "Manifest transfer failed. Check network access and try again."
    check_cancel(cancel)
    return report
