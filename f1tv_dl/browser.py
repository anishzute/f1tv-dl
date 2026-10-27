"""The official browser sign-in flow, including consent frames and modern F1 SSO."""

import base64
import json
import logging
import os
import re
import time
from contextlib import suppress
from urllib.parse import parse_qs, unquote, urlsplit

from .config import BASE_URL
from .errors import AuthenticationError, F1TVError, OperationCancelled

LOG = logging.getLogger(__name__)
ACCOUNT_LOGIN_URL = "https://account.formula1.com/#/en/login?"
ACCOUNT_HOSTS = {"f1tv.formula1.com", "account.formula1.com", "id.formula1.com"}
CONSENT_HOSTS = ACCOUNT_HOSTS | {"consent.formula1.com"}
ESSENTIAL_COOKIES = re.compile(
    r"^(essential only cookies|only essential cookies|reject all|reject optional cookies|"
    r"continue without accepting|accept necessary cookies)$",
    re.I,
)
SIGN_IN = re.compile(r"^(sign\s*in|log\s*in)$", re.I)
NEXT = re.compile(r"^(continue|next)$", re.I)
VERIFICATION = re.compile(r"verify|verification|security check|captcha|one.time code", re.I)
SESSION_URLS = [BASE_URL, ACCOUNT_LOGIN_URL, "https://id.formula1.com/"]
STORAGE_SESSIONS = """() => {
    const sessions = [];
    for (const storage of [localStorage, sessionStorage]) {
        for (const name of ['login-session', 'auth-session', 'ascSessionInfo']) {
            const value = storage.getItem(name);
            if (value) sessions.push({name, value});
        }
    }
    return sessions;
}"""


def trusted_surface(url, hosts):
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname in hosts
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
        )
    except ValueError:
        return False


def first_visible(locator, *, last=False):
    indexes = range(locator.count())
    if last:
        indexes = reversed(indexes)
    for index in indexes:
        item = locator.nth(index)
        if item.is_visible():
            return item
    return None


def cookie_json(value):
    try:
        for _ in range(3):
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def access_subscription(token):
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("subscriptionToken") if isinstance(claims, dict) else None
    except (ValueError, IndexError, TypeError, AttributeError):
        return None


def subscription_cookie(cookie):
    data = cookie_json(cookie.get("value", ""))
    if cookie.get("name") in {"login-session", "ascSessionInfo"}:
        result = data.get("data", {})
        return result.get("subscriptionToken") if isinstance(result, dict) else None
    if cookie.get("name") == "auth-session":
        return access_subscription(data.get("accessToken"))
    return None


def session_response_kind(url):
    if not trusted_surface(url, ACCOUNT_HOSTS):
        return None
    parts = urlsplit(url)
    if parts.hostname == "id.formula1.com" and parts.path == "/as/token":
        return "oauth"
    if parts.hostname == "f1tv.formula1.com" and parts.path.endswith("/USER/ENTITLEMENT"):
        return "entitlement"
    return None


def account_shell_stage(url):
    if not trusted_surface(url, {"account.formula1.com"}):
        return None
    parts = urlsplit(url)
    route, _, query = parts.fragment.partition("?")
    # F1 configures its OAuth redirect as /#//login/; the locale is intentionally absent.
    if parts.path not in {"", "/"} or route.rstrip("/") not in {"", "/en/login", "//login"}:
        return None
    params = parse_qs(parts.query + "&" + query)
    return "callback" if "code" in params or "state" in params else "login"


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
        options = {"server": f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")}
        if parts.username is not None:
            options["username"] = unquote(parts.username)
        if parts.password is not None:
            options["password"] = unquote(parts.password)
        return options
    except ValueError:
        raise AuthenticationError("The configured browser proxy URL is invalid.") from None


class BrowserFlow:
    def __init__(
        self, context, exchange, username, password, *, cancel=None, status=None, stall_timeout=15
    ):
        self.context = context
        self.exchange = exchange
        self.username = username
        self.password = password
        self.cancel = cancel
        self.status = status
        self.last_status = None
        self.opened_login = set()
        self.advanced = set()
        self.password_submitted = False
        self.exchanged = set()
        self.exchange_failed = False
        self.finished_requests = []
        self.token_exchange_started = False
        self.token_exchange_failed = False
        self.idle_shells = {}
        self.refreshed_shells = set()
        self.stall_timeout = stall_timeout
        context.on("request", self.note_request)
        context.on("requestfinished", self.note_finished_request)

    def note_request(self, request):
        if session_response_kind(request.url) == "oauth":
            self.token_exchange_started = True

    def note_finished_request(self, request):
        if session_response_kind(request.url):
            self.finished_requests.append(request)

    def emit(self, message):
        if message != self.last_status:
            self.last_status = message
            LOG.info(message)
            if self.status:
                self.status(message)

    def session_token(self, browser_error):
        from .auth import token_is_fresh

        # A completed OAuth response can arrive before the account site's cookie-writing code.
        requests, self.finished_requests = self.finished_requests, []
        for request in requests:
            try:
                response = request.response()
                if response is None:
                    continue
                kind = session_response_kind(response.url)
                if not 200 <= response.status < 300:
                    if kind == "oauth":
                        self.token_exchange_failed = True
                    continue
                data = response.json()
                if not isinstance(data, dict):
                    continue
                if kind == "oauth":
                    token = self.finish_subscription(access_subscription(data.get("access_token")))
                    if token:
                        return token
                elif kind == "entitlement":
                    result = data.get("resultObj")
                    token = result.get("entitlementToken") if isinstance(result, dict) else None
                    if isinstance(token, str) and token_is_fresh(token):
                        return token
            except (browser_error, ValueError):
                # Cookie/storage detection remains available if an observed response disappears.
                continue
        cookies = self.context.cookies(SESSION_URLS)
        for cookie in cookies:
            if cookie.get("name") == "entitlement_token":
                token = unquote(cookie.get("value", ""))
                if token_is_fresh(token):
                    return token
        for cookie in cookies:
            token = self.finish_subscription(subscription_cookie(cookie))
            if token:
                return token
        for page in self.context.pages:
            if page.is_closed() or not trusted_surface(page.url, ACCOUNT_HOSTS):
                continue
            try:
                stored = page.locator("html").evaluate(STORAGE_SESSIONS, timeout=1000)
                for item in stored:
                    token = self.finish_subscription(subscription_cookie(item))
                    if token:
                        return token
            except browser_error:
                # SSO navigation may replace the execution context during this read.
                continue
        return None

    def finish_subscription(self, subscription):
        from .auth import token_is_fresh

        if (
            not isinstance(subscription, str)
            or not token_is_fresh(subscription)
            or subscription in self.exchanged
        ):
            return None
        self.exchanged.add(subscription)
        self.emit("Finishing F1 TV account sign-in…")
        try:
            return self.exchange(subscription)
        except F1TVError:
            # The normal site can still issue its entitlement cookie after an SSO redirect.
            self.exchange_failed = True
            return None

    def recover_stalled_shell(self, page):
        stage = account_shell_stage(page.url)
        if stage is None or self.token_exchange_started:
            self.idle_shells.pop(page, None)
            return
        # Refresh only the account shell. Never interrupt a form, consent, or verification frame.
        controls = page.locator(
            'input:not([type="hidden"]):visible, textarea:visible, select:visible, '
            "form:visible, iframe:visible"
        )
        if (
            first_visible(controls) is not None
            or first_visible(page.get_by_text(VERIFICATION)) is not None
        ):
            self.idle_shells.pop(page, None)
            return
        now = time.monotonic()
        previous = self.idle_shells.get(page)
        if previous is None or previous[0] != page.url:
            self.idle_shells[page] = (page.url, now)
            return
        key = (page, stage)
        if (
            now - previous[1] < self.stall_timeout
            or key in self.refreshed_shells
            or self.token_exchange_started
        ):
            return
        self.refreshed_shells.add(key)
        self.emit("The F1 account page is stalled. Refreshing its loading screen once…")
        # A GET reload preserves the browser's cookies and PKCE state. Passwords are never replayed.
        page.reload(wait_until="commit", timeout=10000)
        self.idle_shells[page] = (page.url, time.monotonic())

    def accept_essential_cookies(self, frame):
        if not trusted_surface(frame.url, CONSENT_HOSTS):
            return False
        button = first_visible(frame.get_by_role("button", name=ESSENTIAL_COOKIES))
        if button is None:
            button = first_visible(frame.locator("#truste-consent-button:visible"))
        if button is not None:
            button.click(timeout=1500)
            return True
        return False

    def fill_account_form(self, frame):
        if (
            self.password_submitted
            or not (self.username or self.password)
            or not trusted_surface(frame.url, ACCOUNT_HOSTS)
        ):
            return
        key = (id(frame), frame.url)
        user = first_visible(
            frame.locator(
                'input[name="Login" i]:visible, input[name="username" i]:visible, '
                'input[name="email" i]:visible, input[type="email"]:visible, '
                'input[autocomplete="username"]:visible'
            )
        )
        secret = first_visible(frame.locator('input[type="password"]:visible'))
        current_user = user.input_value(timeout=1000) if user is not None else ""
        if (
            self.username
            and current_user
            and current_user.strip().casefold() != self.username.strip().casefold()
        ):
            self.emit("Complete sign-in in the browser window for the account you selected.")
            return
        if self.username and user is not None and not current_user:
            self.emit("Entering account details on the official F1 sign-in page…")
            user.fill(self.username, timeout=1500)
        if self.password and secret is not None and not secret.input_value(timeout=1000):
            secret.fill(self.password, timeout=1500)
        if secret is None and user is not None and self.username and key not in self.advanced:
            button = first_visible(frame.get_by_role("button", name=NEXT))
            if button is not None and button.is_enabled(timeout=1000):
                self.advanced.add(key)
                button.click(timeout=1500)
        elif secret is not None and self.username and self.password and not self.password_submitted:
            button = first_visible(frame.locator('button[type="submit"]:visible'))
            if button is None:
                button = first_visible(frame.get_by_role("button", name=SIGN_IN), last=True)
            if button is not None and button.is_enabled(timeout=1000):
                # Mark before clicking: a navigation timeout must never resubmit a password.
                self.password_submitted = True
                button.click(timeout=1500)
                self.emit("Waiting for sign-in. Complete any verification in the browser window.")

    def advance(self, page):
        for frame in page.frames:
            if self.accept_essential_cookies(frame):
                self.emit("Cookie choices saved. Waiting for F1 account sign-in…")
                return
        if urlsplit(page.url).hostname == "f1tv.formula1.com" and id(page) not in self.opened_login:
            button = first_visible(page.get_by_role("link", name=SIGN_IN))
            if button is None:
                button = first_visible(page.get_by_role("button", name=SIGN_IN))
            if button is not None:
                button.click(timeout=1500)
                self.opened_login.add(id(page))
                return
        for frame in page.frames:
            self.fill_account_form(frame)
        self.recover_stalled_shell(page)

    def wait(self, timeout, browser_error):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancel is not None and self.cancel.is_set():
                raise OperationCancelled()
            token = self.session_token(browser_error)
            if token:
                return token
            pages = [page for page in self.context.pages if not page.is_closed()]
            if not pages:
                raise AuthenticationError("The sign-in browser was closed before sign-in finished.")
            for page in pages:
                try:
                    self.advance(page)
                except browser_error:
                    # Consent and SSO navigation replace frames while they are being inspected.
                    # Keep the browser open so the next poll can use the replacement document.
                    continue
            # Playwright's sync API dispatches browser events while its own wait is running.
            # time.sleep would pause token-response listeners during the SSO callback.
            with suppress(browser_error):
                pages[0].wait_for_timeout(250)
        if self.exchange_failed:
            raise AuthenticationError(
                "Account sign-in completed, but F1 TV did not issue a playback session. "
                "Check your subscription and network access, then try again."
            )
        if self.token_exchange_failed:
            raise AuthenticationError(
                "F1 account sign-in could not finish its token exchange. "
                "Start a new browser sign-in and check network access if it happens again."
            )
        if self.refreshed_shells:
            raise AuthenticationError(
                "The F1 account page stayed on its loading screen after a refresh. "
                "Check browser network access, then start a new sign-in."
            )
        raise AuthenticationError(
            "Browser sign-in timed out. Complete cookie choices and account verification "
            "in the sign-in window, then try again."
        )


def browser_error_message(error):
    # Inspect diagnostics only for known categories; never expose URLs, cookies, or credentials.
    detail = str(error)
    if "ERR_CERT_" in detail or "certificate" in detail.lower():
        return (
            "The sign-in browser could not verify the website certificate. "
            "Check your browser or proxy certificate trust, then retry."
        )
    if "Executable doesn't exist" in detail:
        return "The sign-in browser is not installed. Install browser support, then retry."
    if "closed" in detail.lower():
        return "The sign-in browser closed before sign-in finished. Please try again."
    return (
        "Browser sign-in could not start or connect. "
        "Check browser setup, display, proxy, and network access."
    )


def browser_login(
    exchange,
    username=None,
    password=None,
    *,
    headless=False,
    executable=None,
    cancel=None,
    status=None,
    timeout=180,
):
    try:
        from playwright.sync_api import Error, sync_playwright
        from playwright.sync_api import TimeoutError as BrowserTimeout
    except ImportError:
        raise AuthenticationError(
            'Browser login requires: pip install "f1tv-dl[browser]"; '
            "then run: playwright install chromium"
        ) from None
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
                flow = BrowserFlow(
                    context, exchange, username, password, cancel=cancel, status=status
                )
                flow.emit("Opening the official F1 account sign-in page…")
                page = context.new_page()
                try:
                    page.goto(ACCOUNT_LOGIN_URL, wait_until="commit", timeout=10000)
                except BrowserTimeout:
                    if cancel is not None and cancel.is_set():
                        raise OperationCancelled() from None
                    flow.emit("The F1 account page did not respond. Retrying its address once…")
                    page.goto(ACCOUNT_LOGIN_URL, wait_until="commit", timeout=10000)
                return flow.wait(timeout, Error)
            finally:
                if browser is not None:
                    with suppress(Error):
                        browser.close()
    except Error as error:
        raise AuthenticationError(browser_error_message(error)) from None
