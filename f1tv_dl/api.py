"""F1 TV metadata and entitled playback API."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import BASE_URL, Settings
from .errors import APIError, AuthenticationError, DRMError, F1TVError
from .http import validate_media_url
from .models import Content, ContentRef
from .web import WebClient, request_id, validate_playback_profile


@dataclass(frozen=True)
class Playback:
    url: str
    stream_type: str


def result_object(payload):
    if payload.get("resultCode") not in (None, "OK", "SUCCESS", 0, "0", 200, "200"):
        raise F1TVError(
            "F1 TV refused the request. Check your subscription, region, and content availability."
        )
    result = payload.get("resultObj")
    if not isinstance(result, dict):
        raise F1TVError("F1 TV returned an unexpected response.")
    return result


def playback_parameters(content, channel, *, web_session):
    """Keep the advertised request route separate from the returned TME feed ID."""
    params = {"contentId": content.ref.id}
    selected = channel.channel_id if channel is not None else None
    if web_session:
        params["player"] = "player_tm"
        if channel is not None and channel.web_channel_id is not None:
            selected = channel.web_channel_id
    # INTERNATIONAL has a feed ID for TME selection, but its advertised playback URL
    # deliberately omits channelId. Sending that feed ID to PLAY can return HTTP 404.
    if channel is not None and channel.channel_id is not None:
        params["channelId"] = selected
    return params, selected


def request_playback(http, content, channel, *, headers, web_session, platform=None):
    params, selected = playback_parameters(content, channel, web_session=web_session)
    platform = platform or ("WEB_HLS" if web_session else "BIG_SCREEN_HLS")
    version = "3.0" if web_session else "2.0"
    url = BASE_URL + f"/{version}/R/ENG/{platform}/ALL/CONTENT/PLAY"
    try:
        payload = http.get_json(
            url,
            params=params,
            headers=headers,
            operation="Playback authorization",
        )
    except APIError as error:
        # The recorded browser resolves all feeds through the advertised default feed.
        # Some servers do not expose the content-level route. Retry only that missing
        # route, retaining INTERNATIONAL's selected ID when reading the returned feeds.
        default = content.select_channel()
        fallback = default.web_channel_id if default is not None else None
        if not (
            error.status == 404
            and web_session
            and "channelId" not in params
            and selected is not None
            and fallback is not None
            and str(fallback) != str(selected)
        ):
            raise
        payload = http.get_json(
            url,
            params={**params, "channelId": fallback},
            headers={**headers, "correlationid": request_id()},
            operation="Playback authorization",
        )
    return payload, selected


def select_playback_url(result, selected):
    """Use the requested Tiled Media feed even when a top-level default URL is present."""
    tme = result.get("tme")
    feeds = tme.get("feeds", []) if isinstance(tme, dict) else []
    selected = selected if selected is not None else result.get("channelId")
    for feed in feeds if isinstance(feeds, list) else []:
        metadata = feed.get("metadata") if isinstance(feed, dict) else None
        if (
            selected is not None
            and isinstance(metadata, dict)
            and str(metadata.get("channel_id")) == str(selected)
        ):
            return feed.get("url")
    returned = result.get("channelId")
    if feeds or (selected is not None and returned is not None and str(selected) != str(returned)):
        raise F1TVError("F1 TV returned no manifest for the selected feed.")
    return result.get("url")


class F1TVClient:
    def __init__(self, http, settings: Settings | None = None):
        self.http = http
        self.settings = settings or Settings()

    def content(self, url: str) -> Content:
        ref = ContentRef.parse(url)
        entitlement = self.settings.entitlement
        payload = self.http.get_json(
            BASE_URL + f"/3.0/R/ENG/BIG_SCREEN_HLS/ALL/CONTENT/VIDEO/{ref.id}/{entitlement}/2",
            params={
                "contentId": ref.id,
                "entitlement": entitlement,
                "homeCountry": self.settings.country,
            },
            operation="Content lookup",
        )
        result_object(payload)
        return Content.from_response(ref, payload)

    def playback(self, content: Content, channel, sessions, *, profile="web") -> Playback:
        validate_playback_profile(profile)
        token = sessions.get()
        for attempt in range(2):
            subscription = getattr(sessions, "subscription_token", None)
            web_session = isinstance(subscription, str) and bool(subscription)
            if profile == "android-tv" and not web_session:
                raise AuthenticationError(
                    "Android TV playback requires Browser sign-in. Sign in on the Account tab, "
                    "or use --login-method browser --no-cache in the CLI."
                )
            headers = {"entitlementToken": token}
            if web_session:
                client = getattr(sessions, "web_client", None)
                headers.update(
                    (client if isinstance(client, WebClient) else WebClient.create()).headers(
                        profile
                    )
                )
                headers["ascendontoken"] = subscription
            try:
                payload, selected = request_playback(
                    self.http,
                    content,
                    channel,
                    headers=headers,
                    web_session=web_session,
                )
                break
            except APIError as error:
                if error.status == 401 and attempt == 0:
                    token = sessions.refresh()
                    continue
                if error.status == 403:
                    raise F1TVError(
                        "F1 TV denied playback (HTTP 403). Check your subscription, region, "
                        "and session; the service may also require browser verification."
                    ) from None
                raise
        result = result_object(payload)
        if result.get("drmType") or result.get("drmToken") or result.get("isDrmProtected"):
            raise DRMError()
        url = select_playback_url(result, selected) if web_session else result.get("url")
        if not isinstance(url, str) or not url:
            raise F1TVError("F1 TV did not return a playback URL.")
        kind = str(result.get("streamType") or "").upper()
        if web_session and kind.endswith("_CMAF"):
            path = urlsplit(url).path.lower()
            kind = "HLS" if path.endswith(".m3u8") else "DASH" if path.endswith(".mpd") else kind
        if kind and kind not in {"HLS", "DASH"}:
            raise F1TVError(f"Unsupported stream type: {kind}.")
        return Playback(validate_media_url(url), kind)
