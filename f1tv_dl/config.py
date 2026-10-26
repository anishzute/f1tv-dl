"""Service defaults and local configuration."""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://f1tv.formula1.com"
LOGIN_URL = "https://api.formula1.com/v2/account/subscriber/authenticate/by-password"
# Public client identifiers used by the original application, not account secrets.
API_KEY = "fCUCjWrKPu9ylJwRAv8BpGLEgiAuThx7"
DISTRIBUTION_CHANNEL = "d861e38f-05ea-4063-8776-a7e2b6d885a4"
USER_AGENT = "AppleTV6,2/11.1"


@dataclass(frozen=True)
class Settings:
    country: str = "USA"
    entitlement: str = "F1_TV_Pro_Annual"
    timeout: float = 30.0


def default_session_path() -> Path | None:
    configured = os.environ.get("LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME")
    if configured:
        root = Path(configured)
    else:
        try:
            user_home = Path.home()
        except RuntimeError:
            # Arbitrary container UIDs may have neither a passwd entry nor a home.
            return None
        root = user_home / "AppData" / "Local" if os.name == "nt" else user_home / ".cache"
    return root / "f1tv-dl" / "session.json"
