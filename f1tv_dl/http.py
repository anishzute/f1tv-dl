"""Small HTTP transport respecting system proxy settings and CA trust."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import USER_AGENT
from .errors import APIError, F1TVError


def validate_media_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme in {"https", "http"}
            and parts.hostname
            and (parts.port is None or 1 <= parts.port <= 65535)
            and not parts.username
            and not parts.password
            and not any(char.isspace() or ord(char) < 32 for char in url)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise F1TVError("The service returned an invalid stream URL.")
    return url


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_media_url(newurl)
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if old.scheme == "https" and new.scheme != "https":
            raise F1TVError("Refusing an insecure HTTPS redirect.")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and (old.scheme, old.netloc) != (new.scheme, new.netloc):
            for header in (
                "Authorization",
                "Entitlementtoken",
                "Ascendontoken",
                "Cookie",
                "Apikey",
            ):
                redirected.remove_header(header)
        return redirected


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        self.opener = build_opener(SafeRedirectHandler())

    def request(self, url, *, params=None, headers=None, body=None, operation="Request"):
        validate_media_url(url)
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        request_headers.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=request_headers)
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as error:
                status = error.code
                error.close()
                if data is None and status in {429, 500, 502, 503, 504} and attempt < self.retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise APIError(operation, status) from None
            except (URLError, TimeoutError, OSError):
                # Never retry password submissions or expose URLs containing signed tokens.
                raise F1TVError(
                    f"{operation} could not connect. Check network access, proxy, and CA trust."
                ) from None
        raise APIError(operation)  # Defensive; every loop path returns or raises.

    def get_json(self, url, **kwargs):
        raw = self.request(url, **kwargs)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise F1TVError("The service returned an invalid JSON response.") from None
        if not isinstance(result, dict):
            raise F1TVError("The service returned an unexpected JSON response.")
        return result

    def get_text(self, url, **kwargs):
        try:
            return self.request(url, **kwargs).decode("utf-8-sig")
        except UnicodeError:
            raise F1TVError("The service returned an invalid manifest.") from None
