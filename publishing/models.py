"""Platform-neutral publish metadata and state helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import MetadataValidationError


PRIVACY_STATUSES = {"private", "unlisted", "public"}
DEFAULT_CATEGORY_ID = "22"
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500


@dataclass(frozen=True)
class PublishMetadata:
    title: str
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    privacy_status: str = "private"
    category_id: str = DEFAULT_CATEGORY_ID
    made_for_kids: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "privacy_status": self.privacy_status,
            "category_id": self.category_id,
        }
        if self.made_for_kids is not None:
            data["made_for_kids"] = self.made_for_kids
        return data


def validate_metadata(value: Mapping[str, Any] | PublishMetadata) -> PublishMetadata:
    if isinstance(value, PublishMetadata):
        metadata = value
    elif isinstance(value, Mapping):
        tags = value.get("tags", [])
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        if not isinstance(tags, (list, tuple)):
            raise MetadataValidationError("tags must be an array of strings")
        metadata = PublishMetadata(
            title=str(value.get("title", "")),
            description=str(value.get("description", "")),
            tags=tuple(str(item) for item in tags),
            privacy_status=str(value.get("privacy_status", "private")),
            category_id=str(value.get("category_id", DEFAULT_CATEGORY_ID)),
            made_for_kids=value.get("made_for_kids"),
        )
    else:
        raise MetadataValidationError("metadata must be an object")

    if not metadata.title.strip():
        raise MetadataValidationError("title must not be empty")
    if len(metadata.title) > MAX_TITLE_CHARS or "<" in metadata.title or ">" in metadata.title:
        raise MetadataValidationError("title must be <=100 characters and must not contain < or >")
    if len(metadata.description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise MetadataValidationError("description must be <=5000 UTF-8 bytes")
    if "<" in metadata.description or ">" in metadata.description:
        raise MetadataValidationError("description must not contain < or >")
    if metadata.privacy_status not in PRIVACY_STATUSES:
        raise MetadataValidationError("privacy_status must be private, unlisted, or public")
    if not metadata.category_id.strip():
        raise MetadataValidationError("category_id must not be empty")
    if any(not tag.strip() for tag in metadata.tags):
        raise MetadataValidationError("tags must contain non-empty strings")
    encoded_tags = ",".join(metadata.tags).encode("utf-8")
    if len(encoded_tags) > MAX_TAGS_CHARS:
        raise MetadataValidationError("combined tags must be <=500 UTF-8 bytes")
    if metadata.made_for_kids is not None and not isinstance(metadata.made_for_kids, bool):
        raise MetadataValidationError("made_for_kids must be boolean when provided")
    return metadata
