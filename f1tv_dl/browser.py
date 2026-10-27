"""Chromium is used only for F1's interactive identity-provider authorization."""

import logging
import os
import re
import time
from contextlib import suppress
from urllib.parse import unquote, urlsplit

from .auth import SessionCredentials
from .errors import AuthenticationError, OperationCancelled
from .http import HttpClient
from .oauth import OAuthSession, callback_location, trusted_origin
from .web import WebClient

LOG = logging.getLogger(__name__)
SIGN_IN = re.compile(r"^(sign\s*in|log\s*in)$", re.I)
NEXT = re.compile(r"^(continue|next)$", re.I)
CALLBACK_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>F1 TV Downloader sign-in</title><body>
<h1>Completing sign-in</h1><p>Return to F1 TV Downloader.</p></body></html>"""


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise OperationCancelled()


def first_visible(locator, *, last=False):
    indexes = range(locator.count())
    for index in reversed(indexes) if last else indexes:
        item = locator.nth(index)
        if item.is_visible():
            return item
    return None


def proxy_options():
    proxy = next(
        (
            os.environ[key]
            for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
            if os.environ.get(key)
        ),
        None,
    )
    if not proxy:
        return None
    try:
        parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
        host = parts.hostname
        if not host:
            raise ValueError
        host = f"[{host}]" if ":" in host else host
        result = {"server": f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")}
        if parts.username is not None:
            result["username"] = unquote(parts.username)
        if parts.password is not None:
            result["password"] = unquote(parts.password)
        return result
    except ValueError:
        raise AuthenticationError("The configured browser proxy URL is invalid.") from None


class BrowserAuthorization:
    def __init__(self, context, oauth, username=None, password=None, *, cancel=None, status=None):
        self.context = context
        self.oauth = oauth
        self.username = username
        self.password = password
        self.cancel = cancel
        self.status = status
        self.last_status = None
        self.password_submitted = False
        self.advanced = set()
        # The callback document is a local response. F1's account application is never loaded.
        context.route("https://account.formula1.com/**", self.receive_callback)

    def emit(self, message):
        if message != self.last_status:
            self.last_status = message
            LOG.info(message)
            if self.status:
                self.status(message)

    def receive_callback(self, route):
        if route.request.resource_type == "document" and callback_location(route.request.url):
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=CALLBACK_HTML)
        else:
            route.continue_()

    def fill_identity_form(self, frame):
        if (
            self.password_submitted
            or not (self.username or self.password)
            or not trusted_origin(frame.url, "id.formula1.com")
        ):
            return
        user = first_visible(
            frame.locator(
                'input[name="username" i]:visible, input[name="email" i]:visible, '
                'input[type="email"]:visible, input[autocomplete="username"]:visible'
            )
        )
        secret = first_visible(frame.locator('input[type="password"]:visible'))
        current = user.input_value(timeout=1000) if user is not None else ""
        if (
            self.username
            and current
            and current.strip().casefold() != self.username.strip().casefold()
        ):
            self.emit("Complete sign-in in Chromium for the account you selected.")
            return
        if self.username and user is not None and not current:
            user.fill(self.username, timeout=1500)
        if self.password and secret is not None and not secret.input_value(timeout=1000):
            secret.fill(self.password, timeout=1500)
        key = (id(frame), frame.url)
        if secret is None and user is not None and self.username and key not in self.advanced:
            button = first_visible(frame.get_by_role("button", name=NEXT))
            if button is not None and button.is_enabled(timeout=1000):
                self.advanced.add(key)
                button.click(timeout=1500)
        elif secret is not None and self.username and self.password:
            button = first_visible(frame.locator('button[type="submit"]:visible'))
            if button is None:
                button = first_visible(frame.get_by_role("button", name=SIGN_IN), last=True)
            if button is not None and button.is_enabled(timeout=1000):
                self.password_submitted = True
                button.click(timeout=1500)
                self.emit("Complete any account verification in Chromium.")

    def wait(self, timeout, browser_error):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            check_cancel(self.cancel)
            pages = [page for page in self.context.pages if not page.is_closed()]
            if not pages:
                raise AuthenticationError("The sign-in browser closed before sign-in finished.")
            for page in pages:
                if self.oauth.capture(page.url):
                    self.emit("F1 authorization received. Completing sign-in...")
                    return
                for frame in page.frames:
                    try:
                        self.fill_identity_form(frame)
                    except browser_error:
                        # A redirect can replace a form while optional autofill is reading it.
                        continue
            with suppress(browser_error):
                pages[0].wait_for_timeout(200)
        raise AuthenticationError(
            "F1 sign-in timed out. Complete sign-in and any verification in Chromium, then retry."
        )


def browser_error_message(error):
    detail = str(error)
    if "ERR_CERT_" in detail or "certificate" in detail.lower():
        return (
            "Chromium could not verify the sign-in website certificate. "
            "Check browser or proxy certificate trust, then retry."
        )
    if "Executable doesn't exist" in detail:
        return "Chromium is not installed. Install browser support, then retry."
    if "closed" in detail.lower():
        return "Chromium closed before sign-in finished. Please try again."
    return (
        "Chromium could not open the F1 sign-in page. "
        "Check browser setup, desktop display, proxy, and network access."
    )


def browser_login(
    exchange,
    username=None,
    password=None,
    *,
    http=None,
    headless=False,
    executable=None,
    cancel=None,
    status=None,
    timeout=180,
):
    check_cancel(cancel)
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise AuthenticationError(
            'Browser login requires: pip install "f1tv-dl[browser]"; '
            "then run: playwright install chromium"
        ) from None
    http = http if http is not None else HttpClient()
    if status:
        status("Connecting to the official F1 sign-in service...")
    oauth = OAuthSession.discover(http)
    check_cancel(cancel)
    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                options = {"headless": headless}
                if executable:
                    options["executable_path"] = executable
                proxy = proxy_options()
                if proxy:
                    options["proxy"] = proxy
                browser = playwright.chromium.launch(**options)
                context = browser.new_context()
                page = context.new_page()
                web_client = WebClient.create(page.evaluate("navigator.userAgent"), browser.version)
                flow = BrowserAuthorization(
                    context, oauth, username, password, cancel=cancel, status=status
                )
                flow.emit("Opening F1 sign-in directly in Chromium...")
                page.goto(oauth.authorization_url, wait_until="commit", timeout=15000)
                flow.wait(timeout, Error)
            finally:
                if browser is not None:
                    with suppress(Error):
                        browser.close()
    except Error as error:
        raise AuthenticationError(browser_error_message(error)) from None
    check_cancel(cancel)
    subscription = oauth.exchange(http)
    check_cancel(cancel)
    flow.emit("Authorizing the F1 TV session...")
    return SessionCredentials(exchange(subscription, web_client), subscription, web_client)
