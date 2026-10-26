"""F1 TV metadata and entitled playback API."""

from dataclasses import dataclass

from .config import BASE_URL, Settings
from .errors import APIError, DRMError, F1TVError
from .http import validate_media_url
from .models import Content, ContentRef


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

    def playback(self, content: Content, channel, sessions) -> Playback:
        params = {"contentId": content.ref.id}
        if channel is not None and channel.channel_id is not None:
            params["channelId"] = channel.channel_id
        token = sessions.get()
        for attempt in range(2):
            try:
                payload = self.http.get_json(
                    BASE_URL + "/2.0/R/ENG/BIG_SCREEN_HLS/ALL/CONTENT/PLAY",
                    params=params,
                    headers={"entitlementToken": token},
                    operation="Playback authorization",
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
        url = result.get("url")
        if not isinstance(url, str) or not url:
            raise F1TVError("F1 TV did not return a playback URL.")
        kind = str(result.get("streamType") or "").upper()
        if kind and kind not in {"HLS", "DASH"}:
            raise F1TVError(f"Unsupported stream type: {kind}.")
        return Playback(validate_media_url(url), kind)
