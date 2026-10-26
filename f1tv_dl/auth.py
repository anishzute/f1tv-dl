"""Authentication and an atomic, owner-readable session cache."""

import base64
import json
import logging
import os
import re
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import API_KEY, BASE_URL, DISTRIBUTION_CHANNEL, LOGIN_URL
from .errors import APIError, AuthenticationError, F1TVError

LOG = logging.getLogger(__name__)


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

    def load(self) -> str | None:
        if self.path is None:
            return None
        try:
            token = json.loads(self.path.read_text(encoding="utf-8"))["token"]
            return token if token_is_fresh(token) else None
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, token: str):
        if self.path is None:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".session-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "token": token}, handle)
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


class Authenticator:
    def __init__(self, http):
        self.http = http

    def exchange_subscription_token(self, token: str) -> str:
        payload = self.http.get_json(
            BASE_URL + "/2.0/R/ENG/BIG_SCREEN_HLS/ALL/USER/ENTITLEMENT",
            headers={"ascendontoken": token},
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

    def browser_login(self, username=None, password=None, *, headless=False, executable=None):
        """Use the normal sign-in flow; never suppress TLS checks or browser challenges."""
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            raise AuthenticationError(
                'Browser login requires: pip install "f1tv-dl[browser]"; '
                "then run: playwright install chromium"
            ) from None

        with sync_playwright() as playwright:
            browser = None
            try:
                options = {"headless": headless}
                if executable:
                    options["executable_path"] = executable
                proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                if proxy:
                    options["proxy"] = {"server": proxy}
                browser = playwright.chromium.launch(**options)
                context = browser.new_context()
                page = context.new_page()
                LOG.info("Complete sign-in in the browser. Waiting up to 120 seconds.")
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                # Cookie consent is optional and varies by region. Leave it for the user
                # in an interactive browser; a headless run uses only visible known controls.
                deadline = time.monotonic() + 120
                filled_user = filled_password = submitted = advanced_user = False
                while time.monotonic() < deadline:
                    for cookie in context.cookies(BASE_URL):
                        if cookie["name"] == "entitlement_token":
                            token = unquote(cookie["value"])
                            if token_is_fresh(token):
                                return token
                        if cookie["name"] == "login-session":
                            try:
                                session = json.loads(unquote(cookie["value"]))
                                subscription = session["data"]["subscriptionToken"]
                            except (ValueError, KeyError, TypeError):
                                continue
                            if isinstance(subscription, str) and token_is_fresh(subscription):
                                return self.exchange_subscription_token(subscription)
                    consent = page.locator("#truste-consent-button")
                    if consent.count() and consent.first.is_visible():
                        consent.first.click(timeout=2000)
                    login = page.get_by_role("link", name="Sign in", exact=False)
                    if not filled_user and login.count() and login.first.is_visible():
                        login.first.click(timeout=5000)
                    # Only fill credentials on the official account pages. Users can
                    # complete any external identity-provider interaction themselves.
                    if urlsplit(page.url).hostname not in {
                        "f1tv.formula1.com",
                        "account.formula1.com",
                    }:
                        page.wait_for_timeout(500)
                        continue
                    user = page.locator(
                        'input[name="Login"]:visible, input[name="username"]:visible, '
                        'input[name="email"]:visible, input[type="email"]:visible'
                    )
                    secret = page.locator('input[type="password"]:visible')
                    if username and not filled_user and user.count() and user.first.is_visible():
                        user.first.fill(username)
                        filled_user = True
                    if filled_user and not secret.count() and not advanced_user:
                        next_button = page.get_by_role(
                            "button", name=re.compile(r"^(continue|next)$", re.I)
                        )
                        if next_button.count() and next_button.first.is_visible():
                            next_button.first.click(timeout=10000)
                            advanced_user = True
                    if (
                        password
                        and not filled_password
                        and secret.count()
                        and secret.first.is_visible()
                    ):
                        secret.first.fill(password)
                        filled_password = True
                    if filled_user and filled_password and not submitted:
                        button = page.locator('button[type="submit"]:visible')
                        if (
                            button.count()
                            and button.first.is_visible()
                            and button.first.is_enabled()
                        ):
                            button.first.click(timeout=10000)
                            submitted = True
                    page.wait_for_timeout(500)
                raise AuthenticationError(
                    "Browser sign-in timed out before an entitlement token was issued. "
                    "Complete sign-in in the official player and supply F1TV_TOKEN."
                )
            except Error:
                raise AuthenticationError(
                    "Browser login failed. Install Chromium with 'playwright install chromium', "
                    "check display/proxy/CA trust, or supply F1TV_TOKEN."
                ) from None
            finally:
                if browser is not None:
                    with suppress(Error):
                        browser.close()


class SessionManager:
    def __init__(self, store, login, token: str | None = None):
        self.store = store
        self.login = login
        self.explicit = token is not None
        self.token = token if self.explicit else store.load()
        if self.explicit and not token_is_fresh(token):
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
        token = self.login()
        if not token_is_fresh(token):
            raise F1TVError("Login returned an invalid or expired session token.")
        self.token = token
        self.store.save(token)
        return token
