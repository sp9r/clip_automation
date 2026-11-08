"""Desktop OAuth credentials with loopback + PKCE and safe token storage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import AuthorizationError, CredentialsMissingError, InsufficientOAuthScopeError


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEFAULT_YOUTUBE_SCOPES = (YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE)


def required_youtube_scopes(*, include_readonly: bool = True) -> tuple[str, ...]:
    """Return scopes for the standard upload + owner processing-status flow."""
    return DEFAULT_YOUTUBE_SCOPES if include_readonly else (YOUTUBE_UPLOAD_SCOPE,)


def _scope_set(credentials, info: dict | None = None) -> set[str]:
    if info:
        if "scopes" in info or "scope" in info:
            value = info.get("scopes", info.get("scope"))
            if isinstance(value, str):
                return {item for item in value.split() if item}
            if isinstance(value, (list, tuple, set)):
                return {str(item) for item in value if item}
            return set()
    granted = getattr(credentials, "granted_scopes", None)
    if granted is not None:
        return {str(item) for item in granted}
    scopes = getattr(credentials, "scopes", None)
    if scopes:
        return {str(item) for item in scopes}
    return set()


def ensure_required_scopes(credentials, required: tuple[str, ...], *, granted_info: dict | None = None):
    granted = _scope_set(credentials, granted_info)
    missing = [scope for scope in required if scope not in granted]
    if missing:
        raise InsufficientOAuthScopeError(
            "YouTube OAuth grant is missing required scopes: " + ", ".join(missing) + "; authをやり直してください",
            user_action="upload + processing status用のscopeでauthをやり直してください。",
        )
    return credentials


@dataclass(frozen=True)
class CredentialPaths:
    client_secrets: Path
    token_file: Path

    @classmethod
    def from_env(cls, project_root: str | Path | None = None) -> "CredentialPaths":
        root = Path(project_root or Path(__file__).resolve().parents[1])
        client = Path(os.environ.get("SPECTRA_YOUTUBE_CLIENT_SECRETS", root / "data" / "credentials" / "youtube_client.json"))
        token = Path(os.environ.get("SPECTRA_YOUTUBE_TOKEN_FILE", root / "data" / "tokens" / "youtube.json"))
        return cls(client.expanduser().resolve(), token.expanduser().resolve())


def credentials_files_available(paths: CredentialPaths | None = None) -> bool:
    selected = paths or CredentialPaths.from_env()
    if not selected.token_file.is_file():
        return False
    try:
        data = json.loads(selected.token_file.read_text(encoding="utf-8"))
        return isinstance(data, dict) and bool(data.get("refresh_token") or data.get("access_token"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _save_token(credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    os.replace(temporary, path)


def authorize_youtube(
    paths: CredentialPaths | None = None,
    *,
    include_readonly: bool = True,
    open_browser: bool = True,
):
    selected = paths or CredentialPaths.from_env()
    if not selected.client_secrets.is_file():
        raise CredentialsMissingError(
            f"YouTube client secrets are missing: {selected.client_secrets}",
            user_action="SPECTRA_YOUTUBE_CLIENT_SECRETSまたはdata/credentials/youtube_client.jsonを設定してください。",
        )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as error:
        raise AuthorizationError(
            "google-auth-oauthlib is not installed",
            user_action="requirements.txtのGoogle OAuth依存をインストールしてください。",
        ) from error
    scopes = list(required_youtube_scopes(include_readonly=include_readonly))
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(selected.client_secrets), scopes=scopes, autogenerate_code_verifier=True
        )
        credentials = flow.run_local_server(
            host="127.0.0.1", port=0, open_browser=open_browser, authorization_prompt_message="{url}"
        )
        ensure_required_scopes(credentials, tuple(scopes))
        _save_token(credentials, selected.token_file)
        return credentials
    except InsufficientOAuthScopeError:
        raise
    except Exception as error:
        raise AuthorizationError("YouTube OAuth authorization failed", user_action="ブラウザ認証を再実行してください。") from error


def load_youtube_credentials(
    paths: CredentialPaths | None = None,
    *,
    include_readonly: bool = True,
    authorize_if_missing: bool = False,
):
    selected = paths or CredentialPaths.from_env()
    scopes = required_youtube_scopes(include_readonly=include_readonly)
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as error:
        raise AuthorizationError(
            "Google authentication dependencies are not installed",
            user_action="requirements.txtのGoogle依存をインストールしてください。",
        ) from error
    credentials = None
    if selected.token_file.is_file():
        try:
            info = json.loads(selected.token_file.read_text(encoding="utf-8"))
            if not isinstance(info, dict):
                raise ValueError("token file must contain an object")
            credentials = Credentials.from_authorized_user_info(info)
            ensure_required_scopes(credentials, scopes, granted_info=info)
        except (OSError, ValueError) as error:
            raise AuthorizationError("YouTube token file is invalid") from error
    if credentials and credentials.valid:
        return credentials
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save_token(credentials, selected.token_file)
            return credentials
        except Exception as error:
            raise AuthorizationError("YouTube token refresh failed", user_action="authを再実行してください。") from error
    if authorize_if_missing:
        return authorize_youtube(selected, include_readonly=include_readonly)
    raise CredentialsMissingError(
        "YouTube credentials are not available",
        user_action="python upload.py auth を実行してください。",
    )


def refresh_if_needed(credentials, request=None):
    """Small dependency-free seam used by tests and by credential adapters."""
    if getattr(credentials, "expired", False) and getattr(credentials, "refresh_token", None):
        credentials.refresh(request)
    return credentials
