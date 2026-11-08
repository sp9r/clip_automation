"""Typed, secret-safe errors for publishing."""

from __future__ import annotations

from typing import Iterable


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text


class PublishingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        clip_id: str | None = None,
        platform: str = "youtube",
        retryable: bool = False,
        user_action: str | None = None,
        secrets: Iterable[str] = (),
    ):
        self.clip_id = clip_id
        self.platform = platform
        self.retryable = retryable
        self.user_action = user_action
        self.error_message = redact_text(message, secrets)
        super().__init__(self.error_message)

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__,
            "message": self.error_message,
            "clip_id": self.clip_id,
            "platform": self.platform,
            "retryable": self.retryable,
            "user_action": self.user_action,
        }


class CredentialsMissingError(PublishingError):
    pass


class AuthorizationError(PublishingError):
    pass


class InsufficientOAuthScopeError(AuthorizationError):
    """The persisted grant cannot satisfy the requested YouTube operations."""


class MetadataValidationError(PublishingError):
    pass


class UploadError(PublishingError):
    pass


class ThumbnailUploadError(PublishingError):
    pass


class ProcessingError(PublishingError):
    pass


class PublishPreparationError(PublishingError):
    pass
