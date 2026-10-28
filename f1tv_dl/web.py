"""Device and request context required by F1's web API."""

import platform
import re
import uuid
from dataclasses import dataclass

from . import __version__
from .config import BASE_URL
from .errors import F1TVError

# Public protocol versions from the supplied working browser recording.
WEB_APP_VERSION = "55.0.5"
WEB_PLAYER_VERSION = "8.212.0"
PLAYBACK_PROFILES = ("web", "android-tv")


def validate_playback_profile(profile):
    if profile not in PLAYBACK_PROFILES:
        raise F1TVError("Choose Web or Android TV as the playback profile.")


def request_id():
    return "WEB-" + str(uuid.uuid4())


@dataclass(frozen=True, repr=False)
class WebClient:
    user_agent: str
    device_info: str
    session_id: str

    @classmethod
    def create(cls, user_agent=None, browser_version=None):
        agent = (
            user_agent if isinstance(user_agent, str) and user_agent else "f1tv-dl/" + __version__
        )
        chrome = re.search(r"(?:Chrome|Chromium)/(\d+)", agent)
        version = chrome[1] if chrome else str(browser_version or "0").split(".")[0]
        if not version.isdigit():
            version = "0"
        system = platform.system().lower()
        release = re.match(r"[\d.]+", platform.release())
        os_version = release[0] if release else "0"
        windows = re.search(r"Windows NT ([\d.]+)", agent)
        mac = re.search(r"Mac OS X ([\d_]+)", agent)
        if windows:
            system, os_version = "windows", windows[1]
        elif mac:
            system, os_version = "macos", mac[1].replace("_", ".")
        elif "Linux" in agent:
            system = "linux"
        device = ";".join(
            (
                "device=web",
                "screen=browser",
                "os=" + system,
                "browser=chrome",
                "browserVersion=" + version,
                "osVersion=" + os_version,
                "appVersion=" + WEB_APP_VERSION,
                "playerVersion=" + WEB_PLAYER_VERSION,
                "p=true",  # Request the same Tiled Media feed pipeline as player_tm in the trace.
                "tms=1",
            )
        )
        return cls(agent, device, request_id())

    def headers(self, profile="web"):
        validate_playback_profile(profile)
        device_info = self.device_info
        if profile == "android-tv":
            # Match the successful UHD manifest probe without altering the cached login identity.
            device_info = ";".join(
                [
                    "device=android_tv",
                    *(part for part in device_info.split(";") if not part.startswith("device=")),
                ]
            )
        return {
            "User-Agent": self.user_agent,
            "x-f1-device-info": device_info,
            "sessionid": self.session_id,
            "correlationid": request_id(),
        }

    def media_headers(self):
        # CDN requests carry the public website origin, never API bearer tokens.
        return {"User-Agent": self.user_agent, "Origin": BASE_URL}

    def as_dict(self):
        return {
            "user_agent": self.user_agent,
            "device_info": self.device_info,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return None
        values = [data.get(name) for name in ("user_agent", "device_info", "session_id")]
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 2048
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
            for value in values
        ):
            return None
        return cls(*values)
