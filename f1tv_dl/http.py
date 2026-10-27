"""Small HTTP transport respecting system proxy settings and CA trust."""

import json
import ssl
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, getproxies

from .config import USER_AGENT
from .errors import APIError, F1TVError


def network_summary():
    proxies = getproxies()
    configured = any(proxies.get(scheme) for scheme in ("http", "https"))
    return "HTTP proxy configured." if configured else "No HTTP proxy configured."


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
                "X-f1-device-info",
                "Sessionid",
                "Correlationid",
            ):
                redirected.remove_header(header)
        return redirected


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        self.opener = build_opener(SafeRedirectHandler())

    def request(
        self,
        url,
        *,
        params=None,
        headers=None,
        body=None,
        form=None,
        operation="Request",
        stream=False,
    ):
        validate_media_url(url)
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        request_headers.update(headers or {})
        data = None
        if body is not None and form is not None:
            raise ValueError("Provide a JSON body or a form, not both.")
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=request_headers)
        for attempt in range(self.retries + 1):
            try:
                response = self.opener.open(request, timeout=self.timeout)
                if stream:
                    return response
                with response:
                    return response.read()
            except HTTPError as error:
                status = error.code
                error.close()
                if data is None and status in {429, 500, 502, 503, 504} and attempt < self.retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise APIError(operation, status) from None
            except (URLError, TimeoutError, OSError) as error:
                if isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError):
                    raise F1TVError(
                        f"{operation} could not verify the server certificate. "
                        "Check Python CA trust and SSL_CERT_FILE."
                    ) from None
                # Never retry password submissions or expose URLs containing signed tokens.
                raise F1TVError(
                    f"{operation} could not connect. Check network access, proxy, and CA trust."
                ) from None
        raise APIError(operation)  # Defensive; every loop path returns or raises.

    @contextmanager
    def open_stream(self, url, *, headers=None, operation="Media lookup"):
        response = self.request(
            url,
            headers={"Accept": "*/*", "Accept-Encoding": "identity", **(headers or {})},
            operation=operation,
            stream=True,
        )
        try:
            yield response
        finally:
            response.close()

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
