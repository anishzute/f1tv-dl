"""F1's public OAuth client with per-attempt state and S256 PKCE, owned by Python."""

import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

from . import __version__
from .auth import token_is_fresh
from .errors import AuthenticationError, F1TVError

ISSUER = "https://id.formula1.com/as"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = ISSUER + "/authorize"
TOKEN_ENDPOINT = ISSUER + "/token"
# Published by account.formula1.com for its public browser client; these are not secrets.
CLIENT_ID = "df7c78a7-b07c-4ec9-9e0e-9da2149e939f"
REDIRECT_URI = "https://account.formula1.com/#//login/"
SCOPE = "openid profile f1tv_groups email address"
ACR_VALUES = "df7178db6ded1b80e7c4a604cb5a122b"
USER_AGENT = "f1tv-dl/" + __version__


def trusted_origin(url, host):
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname == host
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
        )
    except (ValueError, TypeError):
        return False


def callback_location(url):
    return trusted_origin(url, "account.formula1.com") and urlsplit(url).path in {"", "/"}


def subscription_from_access_token(token):
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        subscription = claims.get("subscriptionToken") if isinstance(claims, dict) else None
    except (ValueError, IndexError, TypeError, AttributeError):
        subscription = None
    # The entitlement endpoint validates the token's signature and account rights.
    if not isinstance(subscription, str) or not token_is_fresh(subscription):
        raise AuthenticationError("F1 sign-in did not return a valid subscription session.")
    return subscription


class OAuthSession:
    """One authorization attempt. Secrets stay in memory and are never represented in logs."""

    def __init__(self):
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.nonce = secrets.token_urlsafe(32)
        self.code = None
        self.used = False
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
        self.authorization_url = (
            AUTHORIZATION_ENDPOINT
            + "?"
            + urlencode(
                {
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "response_type": "code",
                    "scope": SCOPE,
                    "state": self.state,
                    "nonce": self.nonce,
                    "code_challenge": challenge.decode().rstrip("="),
                    "code_challenge_method": "S256",
                    "acr_values": ACR_VALUES,
                    "journey": "login",
                    "language": "en",
                    "ch": "ACWB",
                }
            )
        )

    @classmethod
    def discover(cls, http):
        discovery = http.get_json(
            DISCOVERY_URL,
            headers={"User-Agent": USER_AGENT},
            operation="F1 sign-in service discovery",
        )
        if (
            discovery.get("issuer") != ISSUER
            or discovery.get("authorization_endpoint") != AUTHORIZATION_ENDPOINT
            or discovery.get("token_endpoint") != TOKEN_ENDPOINT
            or "S256" not in (discovery.get("code_challenge_methods_supported") or [])
        ):
            raise AuthenticationError("F1's sign-in service returned an unsupported configuration.")
        return cls()

    def capture(self, url):
        """Accept a callback only for this attempt, in query or fragment response format."""
        if not callback_location(url):
            return False
        parts = urlsplit(url)
        try:
            params = parse_qs(
                parts.query + "&" + parts.fragment.partition("?")[2],
                keep_blank_values=True,
                max_num_fields=64,
            )
        except ValueError:
            raise AuthenticationError("F1 returned an invalid sign-in callback.") from None
        if "code" not in params and "error" not in params:
            return False
        if self.code is not None or self.used:
            raise AuthenticationError("This sign-in callback has already been handled.")
        states = params.get("state", [])
        if (
            len(states) != 1
            or not states[0].isascii()
            or not hmac.compare_digest(states[0], self.state)
        ):
            raise AuthenticationError("F1 sign-in state did not match. Start a new sign-in.")
        if "error" in params:
            raise AuthenticationError("F1 did not authorize this sign-in. Start a new sign-in.")
        codes = params.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise AuthenticationError("F1 did not return a valid authorization code.")
        self.code = codes[0]
        return True

    def exchange(self, http):
        if self.code is None or self.used:
            raise AuthenticationError("There is no unused F1 authorization code to exchange.")
        # Mark before the request: a transport failure must never replay this one-time code.
        self.used = True
        try:
            payload = http.get_json(
                TOKEN_ENDPOINT,
                headers={"User-Agent": USER_AGENT},
                form={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "code": self.code,
                    "code_verifier": self.verifier,
                },
                operation="F1 authorization code exchange",
            )
        except F1TVError:
            raise AuthenticationError(
                "F1 could not complete the authorization code exchange. "
                "Start a new sign-in and check network access if it happens again."
            ) from None
        finally:
            self.code = None
            self.verifier = ""
        return subscription_from_access_token(payload.get("access_token"))
