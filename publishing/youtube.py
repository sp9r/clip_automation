"""YouTube platform adapter using the official Google Python client lazily."""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any, Callable

from job_control import PipelineCancelled

from .credentials import CredentialPaths, credentials_files_available, load_youtube_credentials
from .errors import CredentialsMissingError, ProcessingError, ThumbnailUploadError, UploadError, redact_text


RETRIABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class Publisher:
    platform = "unknown"
    secret_values: tuple[str, ...] = ()

    def credentials_available(self) -> bool:
        return False

    def publish_video(self, video_path: Path, metadata: dict[str, Any], **kwargs) -> dict[str, Any]:
        raise NotImplementedError

    def set_thumbnail(self, video_id: str, thumbnail_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def get_status(self, video_id: str) -> dict[str, Any]:
        raise NotImplementedError


def _status_code(error: BaseException) -> int | None:
    response = getattr(error, "resp", None)
    value = getattr(response, "status", None) or getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class YouTubePublisher(Publisher):
    platform = "youtube"

    def __init__(
        self,
        *,
        service=None,
        credentials=None,
        credential_paths: CredentialPaths | None = None,
        include_readonly: bool = True,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ):
        self.service = service
        self.credentials = credentials
        self.credential_paths = credential_paths or CredentialPaths.from_env()
        self.include_readonly = include_readonly
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep
        self._log = log or (lambda _message: None)
        self.secret_values = tuple(
            str(value)
            for value in (
                getattr(credentials, "token", None) if credentials is not None else None,
                getattr(credentials, "refresh_token", None) if credentials is not None else None,
                getattr(credentials, "client_secret", None) if credentials is not None else None,
            )
            if value
        )

    def credentials_available(self) -> bool:
        return self.credentials is not None or credentials_files_available(self.credential_paths)

    def _service(self):
        if self.service is not None:
            return self.service
        if self.credentials is None:
            self.credentials = load_youtube_credentials(
                self.credential_paths, include_readonly=self.include_readonly, authorize_if_missing=False
            )
        try:
            from googleapiclient.discovery import build
        except ImportError as error:
            raise CredentialsMissingError("google-api-python-client is not installed") from error
        self.service = build("youtube", "v3", credentials=self.credentials, cache_discovery=False)
        return self.service

    def _execute_with_retry(self, request, *, operation: str):
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                return request.execute()
            except Exception as error:
                last_error = error
                status = _status_code(error)
                if status not in RETRIABLE_STATUS_CODES or attempt >= self.max_retries:
                    break
                delay = min(60.0, 2 ** attempt)
                self._log(f"{operation}: retry {attempt + 1}/{self.max_retries}")
                self._sleep(delay)
        raise last_error

    def publish_video(self, video_path: Path, metadata: dict[str, Any], **_kwargs) -> dict[str, Any]:
        if not self.credentials_available() and self.service is None:
            raise CredentialsMissingError("YouTube credentials are not available")
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise UploadError(f"video file is missing or empty: {video_path}")
        try:
            from googleapiclient.http import MediaFileUpload
            media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
            body = {
                "snippet": {
                    "title": metadata["title"],
                    "description": metadata["description"],
                    "tags": metadata["tags"],
                    "categoryId": metadata["category_id"],
                },
                "status": {"privacyStatus": metadata["privacy_status"]},
            }
            if metadata.get("made_for_kids") is not None:
                body["status"]["selfDeclaredMadeForKids"] = metadata["made_for_kids"]
            request = self._service().videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            last_error = None
            retry_count = 0
            cancel_event = _kwargs.get("cancel_event")
            while response is None:
                # The Google client may block inside one HTTP request; cancellation
                # is therefore checked at resumable chunk boundaries. A successful
                # chunk is progress, not a retry attempt, regardless of its count.
                if cancel_event is not None and cancel_event.is_set():
                    raise PipelineCancelled("publishing was canceled")
                try:
                    _status, response = request.next_chunk()
                    retry_count = 0
                    if cancel_event is not None and cancel_event.is_set():
                        raise PipelineCancelled("publishing was canceled")
                except PipelineCancelled:
                    raise
                except Exception as error:
                    last_error = error
                    status = _status_code(error)
                    if status not in RETRIABLE_STATUS_CODES:
                        raise
                    retry_count += 1
                    if retry_count > self.max_retries:
                        raise
                    self._log(f"upload: retry {retry_count}/{self.max_retries}")
                    self._sleep(min(60.0, 2 ** (retry_count - 1)))
            if not response or not response.get("id"):
                raise last_error or RuntimeError("YouTube upload returned no video ID")
            video_id = str(response["id"])
            return {"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}
        except (PipelineCancelled, UploadError):
            raise
        except Exception as error:
            raise UploadError(
                f"YouTube upload failed: {redact_text(error, self.secret_values)}",
                retryable=_status_code(error) in RETRIABLE_STATUS_CODES,
                secrets=self.secret_values,
            ) from error

    def set_thumbnail(self, video_id: str, thumbnail_path: Path) -> dict[str, Any]:
        try:
            from googleapiclient.http import MediaFileUpload
            mime = mimetypes.guess_type(thumbnail_path.name)[0] or "image/jpeg"
            request = self._service().thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(str(thumbnail_path), mimetype=mime, resumable=False)
            )
            return self._execute_with_retry(request, operation="thumbnail")
        except Exception as error:
            raise ThumbnailUploadError(
                f"YouTube thumbnail upload failed: {redact_text(error, self.secret_values)}",
                retryable=_status_code(error) in RETRIABLE_STATUS_CODES,
                secrets=self.secret_values,
            ) from error

    def get_status(self, video_id: str) -> dict[str, Any]:
        try:
            request = self._service().videos().list(part="processingDetails,status,snippet", id=video_id)
            response = self._execute_with_retry(request, operation="processing status")
            items = response.get("items", [])
            if not items:
                raise ProcessingError("YouTube returned no video status")
            item = items[0]
            processing = item.get("processingDetails", {})
            status = processing.get("processingStatus") or item.get("status", {}).get("uploadStatus")
            return {
                "processing_status": status or "unknown",
                "channel_id": item.get("snippet", {}).get("channelId"),
                "channel_title": item.get("snippet", {}).get("channelTitle"),
                "raw": {"processingStatus": status or "unknown"},
            }
        except ProcessingError:
            raise
        except Exception as error:
            raise ProcessingError(
                f"YouTube processing status failed: {redact_text(error, self.secret_values)}",
                retryable=_status_code(error) in RETRIABLE_STATUS_CODES,
                secrets=self.secret_values,
            ) from error
