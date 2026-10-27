"""Authentication and an atomic, owner-readable session cache."""

import base64
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import API_KEY, BASE_URL, DISTRIBUTION_CHANNEL, LOGIN_URL
from .errors import APIError, AuthenticationError, F1TVError
from .web import WebClient

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class SessionCredentials:
    entitlement_token: str
    subscription_token: str
    web_client: WebClient | None = None


def token_is_fresh(token: str) -> bool:
    """Inspect JWT expiration only; the F1 TV service validates the signature."""
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
        return False
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"]) > time.time() + 60
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        # Some upstream tokens are opaque. Validate them with the service instead.
        return token.count(".") != 2


class SessionStore:
    def __init__(self, path: Path | None):
        self.path = path

    def load(self) -> str | SessionCredentials | None:
        if self.path is None:
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            token = data["token"]
            if not token_is_fresh(token):
                return None
            subscription = data.get("subscription_token")
            if subscription is not None:
                return (
                    SessionCredentials(
                        token, subscription, WebClient.from_dict(data.get("web_client"))
                    )
                    if token_is_fresh(subscription)
                    else None
                )
            return token
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, token: str | SessionCredentials):
        if self.path is None:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".session-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                data = (
                    {
                        "version": 2,
                        "token": token.entitlement_token,
                        "subscription_token": token.subscription_token,
                    }
                    if isinstance(token, SessionCredentials)
                    else {"version": 1, "token": token}
                )
                if isinstance(token, SessionCredentials) and token.web_client is not None:
                    data["version"] = 3
                    data["web_client"] = token.web_client.as_dict()
                json.dump(data, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            # A read-only cache must not prevent using an otherwise valid login.
            LOG.warning("Could not save the session cache; this login will only last for this run.")
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def clear(self):
        if self.path is not None:
            self.path.unlink(missing_ok=True)


class Authenticator:
    def __init__(self, http):
        self.http = http

    def exchange_subscription_token(
        self, token: str, *, platform="BIG_SCREEN_HLS", web_client=None
    ) -> str:
        headers = {"ascendontoken": token}
        if platform == "WEB_DASH":
            headers.update((web_client or WebClient.create()).headers())
        payload = self.http.get_json(
            BASE_URL + f"/2.0/R/ENG/{platform}/ALL/USER/ENTITLEMENT",
            headers=headers,
            operation="Entitlement lookup",
        )
        result = payload.get("resultObj")
        entitlement = result.get("entitlementToken") if isinstance(result, dict) else None
        if not isinstance(entitlement, str) or not token_is_fresh(entitlement):
            raise AuthenticationError("F1 TV did not return a valid entitlement token.")
        return entitlement

    def password_login(self, username: str, password: str) -> str:
        if not username or not password:
            raise AuthenticationError("Provide F1TV_USER and F1TV_PASSWORD, or an F1TV_TOKEN.")
        try:
            payload = self.http.get_json(
                LOGIN_URL,
                headers={"apikey": API_KEY, "DistributionChannel": DISTRIBUTION_CHANNEL},
                body={"Login": username, "Password": password},
                operation="Password login",
            )
        except APIError as error:
            if error.status in (401, 403):
                raise AuthenticationError(
                    f"F1 TV rejected password login (HTTP {error.status}). Check your credentials; "
                    "if browser verification is required, use --login-method browser "
                    "or provide an existing entitlement token with F1TV_TOKEN."
                ) from None
            raise
        data = payload.get("data", payload)
        subscription = data.get("subscriptionToken") if isinstance(data, dict) else None
        if not isinstance(subscription, str) or not subscription:
            raise AuthenticationError(
                "Login did not return a subscription token. Check your credentials or use "
                "--login-method browser for interactive verification."
            )
        return self.exchange_subscription_token(subscription)

    def browser_login(
        self,
        username=None,
        password=None,
        *,
        headless=False,
        executable=None,
        cancel=None,
        status=None,
        timeout=180,
    ):
        from .browser import browser_login

        return browser_login(
            lambda token, web_client: self.exchange_subscription_token(
                token, platform="WEB_DASH", web_client=web_client
            ),
            username,
            password,
            http=self.http,
            headless=headless,
            executable=executable,
            cancel=cancel,
            status=status,
            timeout=timeout,
        )


class SessionManager:
    def __init__(self, store, login, token: str | SessionCredentials | None = None):
        self.store = store
        self.login = login
        self.explicit = token is not None
        credentials = token if self.explicit else store.load()
        self.token = (
            credentials.entitlement_token
            if isinstance(credentials, SessionCredentials)
            else credentials
        )
        self.subscription_token = (
            credentials.subscription_token if isinstance(credentials, SessionCredentials) else None
        )
        self.web_client = (
            credentials.web_client or WebClient.create()
            if isinstance(credentials, SessionCredentials)
            else None
        )
        if self.explicit and (
            not token_is_fresh(self.token)
            or (self.subscription_token is not None and not token_is_fresh(self.subscription_token))
        ):
            raise AuthenticationError(
                "F1TV_TOKEN is empty, malformed, or expired. Provide a fresh token."
            )

    def get(self) -> str:
        if not self.token:
            return self.refresh()
        return self.token

    def refresh(self) -> str:
        if self.explicit:
            raise AuthenticationError(
                "F1 TV rejected F1TV_TOKEN. Provide a fresh entitlement token from your session."
            )
        credentials = self.login()
        token = (
            credentials.entitlement_token
            if isinstance(credentials, SessionCredentials)
            else credentials
        )
        subscription = (
            credentials.subscription_token if isinstance(credentials, SessionCredentials) else None
        )
        if not token_is_fresh(token) or (
            subscription is not None and not token_is_fresh(subscription)
        ):
            raise F1TVError("Login returned an invalid or expired session token.")
        self.token = token
        self.subscription_token = subscription
        self.web_client = (
            credentials.web_client or WebClient.create()
            if isinstance(credentials, SessionCredentials)
            else None
        )
        self.store.save(credentials)
        return token
