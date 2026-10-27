import base64
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit

from f1tv_dl.errors import APIError, AuthenticationError, F1TVError
from f1tv_dl.oauth import (
    AUTHORIZATION_ENDPOINT,
    CLIENT_ID,
    DISCOVERY_URL,
    ISSUER,
    REDIRECT_URI,
    TOKEN_ENDPOINT,
    OAuthSession,
    subscription_from_access_token,
)

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": AUTHORIZATION_ENDPOINT,
    "token_endpoint": TOKEN_ENDPOINT,
    "code_challenge_methods_supported": ["S256"],
}


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def access_token():
    return jwt({"subscriptionToken": jwt({"exp": time.time() + 3600})})


def callback(session, **params):
    values = {"code": "fixture-code", "state": session.state, **params}
    return "https://account.formula1.com/?" + urlencode(values) + "#//login/"


class OAuthTests(unittest.TestCase):
    def test_public_authorization_parameters_match_the_recorded_browser_flow(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "web-auth-protocol.json").read_text()
        )
        query = parse_qs(urlsplit(OAuthSession().authorization_url).query)
        self.assertEqual(
            {k: query[k][0] for k in fixture["authorization"]}, fixture["authorization"]
        )

    def test_authorization_uses_unique_state_nonce_and_s256_pkce(self):
        first, second = OAuthSession(), OAuthSession()
        args = parse_qs(urlsplit(first.authorization_url).query)
        self.assertEqual(args["client_id"], [CLIENT_ID])
        self.assertEqual(args["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(args["response_type"], ["code"])
        self.assertEqual(args["state"], [first.state])
        self.assertEqual(args["code_challenge_method"], ["S256"])
        expected = base64.urlsafe_b64encode(hashlib.sha256(first.verifier.encode()).digest())
        self.assertEqual(args["code_challenge"], [expected.decode().rstrip("=")])
        self.assertNotIn(first.verifier, first.authorization_url)
        self.assertNotEqual(first.verifier, second.verifier)
        self.assertNotEqual(first.state, second.state)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotIn(first.state, repr(first))

    def test_discovery_requires_the_official_endpoints_and_s256(self):
        http = Mock()
        http.get_json.return_value = DISCOVERY
        self.assertIsInstance(OAuthSession.discover(http), OAuthSession)
        self.assertEqual(http.get_json.call_args.args[0], DISCOVERY_URL)
        for key, value in (
            ("issuer", "https://attacker.test"),
            ("authorization_endpoint", "http://id.formula1.com/as/authorize"),
            ("token_endpoint", "https://id.formula1.com.attacker.test/as/token"),
            ("code_challenge_methods_supported", ["plain"]),
        ):
            with self.subTest(key=key):
                http.get_json.return_value = {**DISCOVERY, key: value}
                with self.assertRaises(AuthenticationError):
                    OAuthSession.discover(http)

    def test_callbacks_in_query_or_fragment_are_bound_to_the_attempt(self):
        for style in ("query", "fragment"):
            with self.subTest(style=style):
                session = OAuthSession()
                args = urlencode({"code": "code+with/slash=", "state": session.state})
                url = (
                    "https://account.formula1.com/?" + args + "#//login/"
                    if style == "query"
                    else REDIRECT_URI + "?" + args
                )
                self.assertTrue(session.capture(url))
                self.assertEqual(session.code, "code+with/slash=")

    def test_unrelated_or_spoofed_locations_are_ignored(self):
        session = OAuthSession()
        for url in (
            AUTHORIZATION_ENDPOINT,
            REDIRECT_URI,
            "https://account.formula1.com.attacker.test/?code=private&state=private",
            "http://account.formula1.com/?code=private&state=private",
            "https://account.formula1.com:9000/?code=private&state=private",
            "https://user@account.formula1.com/?code=private&state=private",
            "https://account.formula1.com/unrelated?code=private&state=private",
        ):
            self.assertFalse(session.capture(url))
            self.assertIsNone(session.code)

    def test_missing_mismatched_unicode_or_duplicate_state_is_rejected_without_secrets(self):
        for value in ("", "wrong-state", "\u2603"):
            session = OAuthSession()
            with self.assertRaises(AuthenticationError) as caught:
                session.capture(callback(session, state=value))
            self.assertNotIn("fixture-code", str(caught.exception))
            self.assertIsNone(session.code)
        session = OAuthSession()
        url = callback(session).replace("#", "&state=" + session.state + "#")
        with self.assertRaises(AuthenticationError):
            session.capture(url)

    def test_callback_errors_and_duplicate_codes_are_rejected(self):
        session = OAuthSession()
        url = "https://account.formula1.com/?" + urlencode(
            {"state": session.state, "error": "access_denied", "error_description": "private"}
        )
        with self.assertRaises(AuthenticationError) as caught:
            session.capture(url)
        self.assertNotIn("private", str(caught.exception))
        for suffix in ("&code=another", "&code="):
            other = OAuthSession()
            with self.assertRaises(AuthenticationError):
                other.capture(callback(other).replace("#", suffix + "#"))

    def test_code_exchange_uses_the_original_redirect_and_private_verifier_once(self):
        session = OAuthSession()
        session.capture(callback(session))
        verifier = session.verifier
        http = Mock()
        token = access_token()
        http.get_json.return_value = {"access_token": token}
        subscription = session.exchange(http)
        self.assertEqual(subscription, subscription_from_access_token(token))
        self.assertEqual(http.get_json.call_args.args[0], TOKEN_ENDPOINT)
        self.assertEqual(
            http.get_json.call_args.kwargs["form"],
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": "fixture-code",
                "code_verifier": verifier,
            },
        )
        self.assertEqual(session.verifier, "")
        self.assertIsNone(session.code)
        with self.assertRaises(AuthenticationError):
            session.exchange(http)
        self.assertEqual(http.get_json.call_count, 1)

    def test_network_or_server_failure_never_replays_the_code(self):
        for failure in (APIError("Exchange", 503), F1TVError("Network failed")):
            session = OAuthSession()
            session.capture(callback(session))
            http = Mock()
            http.get_json.side_effect = failure
            with self.assertRaises(AuthenticationError):
                session.exchange(http)
            with self.assertRaises(AuthenticationError):
                session.exchange(http)
            self.assertEqual(http.get_json.call_count, 1)
            self.assertEqual(session.verifier, "")

    def test_stale_or_malformed_subscription_claims_are_rejected(self):
        for value in (
            None,
            "opaque-access-token",
            "bad.jwt.parts",
            jwt({"sub": "private-account"}),
            jwt({"subscriptionToken": jwt({"exp": 1})}),
            jwt(["unexpected"]),
        ):
            with self.assertRaises(AuthenticationError) as caught:
                subscription_from_access_token(value)
            self.assertNotIn("private-account", str(caught.exception))
