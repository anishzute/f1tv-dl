"""Errors that can be safely displayed without exposing credentials."""


class F1TVError(Exception):
    """An actionable application error."""


class APIError(F1TVError):
    def __init__(self, operation: str, status: int | None = None):
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{operation} failed{suffix}.")


class AuthenticationError(F1TVError):
    pass


class MediaError(F1TVError):
    pass


class DRMError(MediaError):
    def __init__(self):
        super().__init__(
            "This stream uses DRM and cannot be downloaded by this tool. "
            "Use the official F1 TV player."
        )
