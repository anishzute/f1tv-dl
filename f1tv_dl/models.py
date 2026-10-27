"""Content URLs, metadata, and deterministic channel selection."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import F1TVError


@dataclass(frozen=True)
class ContentRef:
    id: str
    name: str

    @classmethod
    def parse(cls, url: str):
        try:
            parts = urlsplit(url)
            match = re.fullmatch(r"/(?:[a-z]{2}/)?detail/(\d+)(?:/([^/]+))?/?", parts.path)
            valid = (
                parts.scheme == "https"
                and parts.hostname == "f1tv.formula1.com"
                and parts.port in (None, 443)
                and not parts.username
                and not parts.password
                and not any(char.isspace() or ord(char) < 32 for char in url)
                and match
            )
        except ValueError:
            valid = False
        if not valid:
            raise F1TVError(
                "Use an HTTPS F1 TV detail URL: https://f1tv.formula1.com/detail/ID/title"
            )
        return cls(match[1], unquote(match[2] or match[1]))


@dataclass(frozen=True)
class Channel:
    title: str
    channel_id: str | None
    reporting_name: str = ""
    driver_name: str = ""
    racing_number: str = ""
    default: bool = False
    web_channel_id: str | None = None

    @classmethod
    def from_dict(cls, data):
        playback_url = data.get("playbackUrl")
        if playback_url:
            # INTERNATIONAL advertises a channelId but its playback URL deliberately omits it.
            channel_id = parse_qs(urlsplit(playback_url).query).get("channelId", [None])[0]
        else:
            channel_id = data.get("channelId")
        driver = " ".join(
            str(data.get(key) or "").strip() for key in ("driverFirstName", "driverLastName")
        ).strip()
        return cls(
            title=str(data.get("title") or data.get("reportingName") or "Main"),
            channel_id=str(channel_id) if channel_id is not None else None,
            reporting_name=str(data.get("reportingName") or ""),
            driver_name=driver,
            racing_number=str(data.get("racingNumber", "")) if driver else "",
            default=data.get("default") is True,
            web_channel_id=(
                str(data["channelId"]) if data.get("channelId") is not None else channel_id
            ),
        )

    def aliases(self):
        return [
            value.casefold()
            for value in (
                self.title,
                self.reporting_name,
                self.driver_name,
                self.racing_number,
                self.channel_id or "",
                *self.reporting_name.split("|"),
            )
            if value
        ]


@dataclass(frozen=True)
class Content:
    ref: ContentRef
    title: str
    channels: tuple[Channel, ...]
    duration: float = 0

    @classmethod
    def from_response(cls, ref, payload):
        try:
            containers = payload["resultObj"]["containers"]
            data = next(
                item for item in containers if str(item.get("id", item.get("contentId"))) == ref.id
            )
            metadata = data["metadata"]
            streams = metadata.get("additionalStreams") or []
            channels = tuple(Channel.from_dict(item) for item in streams)
            duration = metadata.get("duration")
            duration = max(0, duration) if isinstance(duration, (int, float)) else 0
            return cls(ref, str(metadata.get("title") or ref.name), channels, duration)
        except (KeyError, TypeError, ValueError, StopIteration, AttributeError):
            raise F1TVError(
                "Content metadata was missing or did not match the requested video."
            ) from None

    def select_channel(self, query: str | None = None) -> Channel | None:
        if not self.channels:
            if query:
                raise F1TVError("This video has no alternate channels.")
            return None
        if not query:
            return next(
                (channel for channel in self.channels if channel.default),
                next(
                    (c for c in self.channels if c.title.casefold() == "f1 live"), self.channels[0]
                ),
            )
        query = query.strip().casefold()
        if not query:
            raise F1TVError("Channel selection cannot be empty.")
        matches = [channel for channel in self.channels if query in channel.aliases()]
        if not matches:
            matches = [c for c in self.channels if any(query in alias for alias in c.aliases())]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise F1TVError(
                "Channel selection is ambiguous: " + ", ".join(c.title for c in matches)
            )
        raise F1TVError("Channel not found. Use --channel-list to see available feeds.")


def safe_filename(name: str) -> str:
    value = re.sub(r"[^\w. -]+", "-", name, flags=re.UNICODE).strip(" .-")[:180]
    if not value:
        return "f1tv-video"
    if value.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        value = "f1tv-" + value
    return value
