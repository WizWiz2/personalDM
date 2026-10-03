from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from jwt import PyJWK

from app.config import settings
from app.services.security import decrypt_secret, encrypt_secret


class ChatGPTAuthError(RuntimeError):
    """Raised when Sign in with ChatGPT cannot establish usable plan credentials."""


@dataclass(frozen=True)
class _AuthAttempt:
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    return_url: str
    client_id: str
    expected_subject: str | None
    expires_at: float


class ChatGPTAuthService:
    """Local OAuth runtime for ChatGPT plan usage.

    Tokens never enter browser storage or campaign rows. One encrypted local profile is
    enough for PersonalDM's current single-user desktop runtime; the issued client ID and
    stable host ID are reused on later authorization attempts.
    """

    ISSUER = "https://auth.openai.com"
    AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
    TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
    JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
    DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
    RESOURCE = "https://api.openai.com/v1"
    MODELS_URL = f"{RESOURCE}/models"
    DYNAMIC_CLIENT_ID = "dynamic_agent_client"
    REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
    SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    CALLBACK_PATH = "/api/runtime/providers/chatgpt/callback"
    APP_NAME = "PersonalDM"

    _attempts: dict[str, _AuthAttempt] = {}
    _attempt_lock = threading.Lock()
    _refresh_lock = threading.Lock()

    def __init__(self) -> None:
        self.storage_dir = Path(settings.DATA_DIR) / "chatgpt"
        self.host_file = self.storage_dir / "chatgpt-host.json"
        self.profile_file = self.storage_dir / "profile.enc"

    @staticmethod
    def _http_client(timeout: float) -> httpx.Client:
        return httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        )

    @staticmethod
    def _write_private_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _host_id(self) -> str:
        if self.host_file.is_file():
            try:
                payload = json.loads(self.host_file.read_text(encoding="utf-8"))
                value = str(payload.get("ext_agent_host_id") or "")
                if value.startswith("urn:uuid:"):
                    return value
            except (OSError, ValueError, TypeError):
                pass
        value = f"urn:uuid:{uuid.uuid4()}"
        self._write_private_text(
            self.host_file,
            json.dumps({"ext_agent_host_id": value}, ensure_ascii=False),
        )
        return value

    def _load_profile(self) -> dict | None:
        if not self.profile_file.is_file():
            return None
        try:
            encrypted = self.profile_file.read_text(encoding="utf-8").strip()
            payload = json.loads(decrypt_secret(encrypted))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ChatGPTAuthError("Сохранённая сессия ChatGPT повреждена") from exc
        if not isinstance(payload, dict):
            raise ChatGPTAuthError("Сохранённая сессия ChatGPT имеет неверный формат")
        return payload

    def _save_profile(self, profile: dict) -> None:
        encrypted = encrypt_secret(json.dumps(profile, ensure_ascii=False))
        self._write_private_text(self.profile_file, encrypted)

    @staticmethod
    def _validate_return_url(return_url: str | None) -> str:
        if not return_url:
            return "http://127.0.0.1/#/settings"
        parsed = urlparse(return_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or not parsed.netloc
        ):
            raise ChatGPTAuthError("OAuth return URL должен указывать на локальный PersonalDM")
        return return_url

    @classmethod
    def _validate_redirect_uri(cls, redirect_uri: str) -> None:
        parsed = urlparse(redirect_uri)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path != cls.CALLBACK_PATH
            or not parsed.port
        ):
            raise ChatGPTAuthError(
                "ChatGPT OAuth callback должен быть http://127.0.0.1:<port>"
                + cls.CALLBACK_PATH
            )

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def start_sign_in(self, *, redirect_uri: str, return_url: str | None = None) -> str:
        self._validate_redirect_uri(redirect_uri)
        safe_return_url = self._validate_return_url(return_url)
        profile = self._load_profile()
        client_id = (
            str(profile.get("client_id"))
            if profile and profile.get("client_id")
            else self.DYNAMIC_CLIENT_ID
        )
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier, challenge = self._pkce_pair()
        attempt = _AuthAttempt(
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            return_url=safe_return_url,
            client_id=client_id,
            expected_subject=str(profile.get("subject")) if profile and profile.get("subject") else None,
            expires_at=time.time() + 600,
        )
        with self._attempt_lock:
            self._attempts[state] = attempt
            expired = [
                key for key, item in self._attempts.items()
                if item.expires_at <= time.time()
            ]
            for key in expired:
                self._attempts.pop(key, None)

        params = {
            "client_id": client_id,
            "ext_agent_host_id": self._host_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": self.SCOPES,
            "resource": self.RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if client_id == self.DYNAMIC_CLIENT_ID:
            params["agent_name_hint"] = self.APP_NAME
        elif profile:
            if profile.get("id_token"):
                params["id_token_hint"] = str(profile["id_token"])
            if profile.get("email"):
                params["login_hint"] = str(profile["email"])
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    @classmethod
    def _consume_attempt(cls, state: str) -> _AuthAttempt:
        with cls._attempt_lock:
            attempt = cls._attempts.pop(state, None)
        if attempt is None or attempt.expires_at <= time.time():
            raise ChatGPTAuthError("Сессия входа ChatGPT истекла или уже была использована")
        return attempt

    @classmethod
    def _verified_identity(
        cls,
        id_token: str,
        *,
        client_id: str,
        nonce: str,
    ) -> dict:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise ChatGPTAuthError("OpenAI вернул некорректный ID token") from exc
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ChatGPTAuthError("Неподдерживаемая подпись ID token")

        try:
            with cls._http_client(10.0) as client:
                response = client.get(cls.JWKS_URL)
                response.raise_for_status()
            keys = response.json().get("keys", [])
            jwk_data = next(
                item for item in keys
                if isinstance(item, dict) and item.get("kid") == header["kid"]
            )
            signing_key = PyJWK.from_dict(jwk_data, algorithm="RS256").key
            claims = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=cls.ISSUER,
                leeway=5,
                options={"require": ["sub", "exp", "iat"]},
            )
        except (httpx.HTTPError, StopIteration, ValueError, TypeError, jwt.PyJWTError) as exc:
            raise ChatGPTAuthError("Не удалось проверить подпись OpenAI ID token") from exc

        if claims.get("nonce") != nonce:
            raise ChatGPTAuthError("Nonce в ChatGPT OAuth callback не совпал")
        if not claims.get("sub"):
            raise ChatGPTAuthError("OpenAI ID token не содержит subject")
        return claims

    def complete_sign_in(
        self,
        *,
        state: str,
        code: str | None,
        issued_client_id: str | None,
        error: str | None = None,
    ) -> str:
        attempt = self._consume_attempt(state)
        if error:
            raise ChatGPTAuthError(
                "Вход через ChatGPT отменён" if error == "access_denied"
                else f"ChatGPT OAuth завершился ошибкой: {error}"
            )
        if not code:
            raise ChatGPTAuthError("ChatGPT OAuth callback не содержит authorization code")

        if attempt.client_id == self.DYNAMIC_CLIENT_ID:
            if not issued_client_id or issued_client_id == self.DYNAMIC_CLIENT_ID:
                raise ChatGPTAuthError("OpenAI не вернул выданный client_id")
            client_id = issued_client_id
        else:
            client_id = attempt.client_id
            if issued_client_id and issued_client_id != client_id:
                raise ChatGPTAuthError("OpenAI вернул неожиданный client_id")

        try:
            with self._http_client(20.0) as client:
                response = client.post(
                    self.TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": code,
                        "code_verifier": attempt.code_verifier,
                        "redirect_uri": attempt.redirect_uri,
                        "resource": self.RESOURCE,
                    },
                    headers={"Accept": "application/json"},
                )
            if response.status_code != 200:
                raise ChatGPTAuthError(
                    f"OpenAI token exchange вернул HTTP {response.status_code}"
                )
            token_data = response.json()
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError("Не удалось обменять ChatGPT authorization code") from exc

        access_token = str(token_data.get("access_token") or "")
        refresh_token = str(token_data.get("refresh_token") or "")
        id_token = str(token_data.get("id_token") or "")
        if not access_token or not refresh_token or not id_token:
            raise ChatGPTAuthError("OpenAI вернул неполный набор OAuth credentials")

        claims = self._verified_identity(
            id_token,
            client_id=client_id,
            nonce=attempt.nonce,
        )
        subject = str(claims["sub"])
        if attempt.expected_subject and subject != attempt.expected_subject:
            raise ChatGPTAuthError("ChatGPT вернул другой аккаунт для существующей регистрации")

        scopes = str(token_data.get("scope") or "").split()
        if self.REQUIRED_SCOPE not in scopes:
            raise ChatGPTAuthError(
                "Доступ к лимитам ChatGPT не разрешён для этой регистрации"
            )

        profile = {
            "email": claims.get("email"),
            "name": claims.get("name"),
            "issuer": self.ISSUER,
            "subject": subject,
            "client_id": client_id,
            "ext_agent_host_id": self._host_id(),
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": token_data.get("token_type", "Bearer"),
            "expires_in": int(token_data.get("expires_in") or 3600),
            "earliest_refresh_at": token_data.get("earliest_refresh_at"),
            "scopes": scopes,
            "saved_at": time.time(),
        }
        self._save_profile(profile)
        return attempt.return_url

    def connection_summary(self) -> dict:
        profile = self._load_profile()
        if not profile:
            return {
                "connected": False,
                "email": None,
                "name": None,
                "plan_usage_enabled": False,
            }
        scopes = set(profile.get("scopes") or [])
        connected = bool(profile.get("access_token") and profile.get("refresh_token"))
        return {
            "connected": connected,
            "email": profile.get("email"),
            "name": profile.get("name"),
            "plan_usage_enabled": connected and self.REQUIRED_SCOPE in scopes,
        }

    def disconnect(self) -> bool:
        """End the renewable session, then retain only the local registration mapping.

        OpenAI recommends revoking the refresh token before local sign-out. The issued
        client ID and verified account mapping are intentionally kept so a later sign-in
        can reuse the registration instead of creating a fresh dynamic client.
        """
        profile = self._load_profile()
        if not profile:
            return True

        refresh_token = str(profile.get("refresh_token") or "")
        client_id = str(profile.get("client_id") or "")
        revocation_confirmed = not refresh_token
        if refresh_token and client_id:
            try:
                with self._http_client(10.0) as client:
                    discovery = client.get(self.DISCOVERY_URL)
                    discovery.raise_for_status()
                revocation_endpoint = str(
                    discovery.json().get("revocation_endpoint") or ""
                )
                if revocation_endpoint:
                    with self._http_client(10.0) as client:
                        response = client.post(
                            revocation_endpoint,
                            data={
                                "token": refresh_token,
                                "token_type_hint": "refresh_token",
                                "client_id": client_id,
                            },
                            headers={"Accept": "application/json"},
                        )
                    revocation_confirmed = response.status_code == 200
            except (httpx.HTTPError, ValueError, TypeError):
                revocation_confirmed = False

        retained = {
            key: profile.get(key)
            for key in (
                "email",
                "name",
                "issuer",
                "subject",
                "client_id",
                "ext_agent_host_id",
            )
            if profile.get(key) is not None
        }
        if retained.get("client_id") and retained.get("subject"):
            self._save_profile(retained)
        else:
            try:
                self.profile_file.unlink()
            except FileNotFoundError:
                pass
        return revocation_confirmed

    @staticmethod
    def _expires_at(profile: dict) -> float:
        return float(profile.get("saved_at") or 0) + int(profile.get("expires_in") or 0)

    def _refresh(self, profile: dict) -> dict:
        refresh_token = str(profile.get("refresh_token") or "")
        client_id = str(profile.get("client_id") or "")
        if not refresh_token or not client_id:
            raise ChatGPTAuthError("ChatGPT OAuth session нужно подключить заново")
        try:
            with self._http_client(20.0) as client:
                response = client.post(
                    self.TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "resource": self.RESOURCE,
                    },
                    headers={"Accept": "application/json"},
                )
            if response.status_code != 200:
                raise ChatGPTAuthError(
                    f"Обновление ChatGPT OAuth token вернуло HTTP {response.status_code}"
                )
            data = response.json()
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError("Не удалось обновить ChatGPT OAuth token") from exc

        access_token = str(data.get("access_token") or "")
        replacement_refresh = str(data.get("refresh_token") or "")
        if not access_token or not replacement_refresh:
            raise ChatGPTAuthError("OpenAI не вернул обновлённые OAuth credentials")
        scopes = str(data.get("scope") or " ".join(profile.get("scopes") or [])).split()
        if self.REQUIRED_SCOPE not in scopes:
            raise ChatGPTAuthError("ChatGPT plan usage больше не разрешён")

        updated = {
            **profile,
            "access_token": access_token,
            "refresh_token": replacement_refresh,
            "id_token": data.get("id_token") or profile.get("id_token"),
            "token_type": data.get("token_type", profile.get("token_type", "Bearer")),
            "expires_in": int(data.get("expires_in") or 3600),
            "earliest_refresh_at": data.get("earliest_refresh_at"),
            "scopes": scopes,
            "saved_at": time.time(),
        }
        self._save_profile(updated)
        return updated

    def get_access_token(self) -> str:
        profile = self._load_profile()
        if not profile:
            raise ChatGPTAuthError("ChatGPT не подключён")
        scopes = set(profile.get("scopes") or [])
        if self.REQUIRED_SCOPE not in scopes:
            raise ChatGPTAuthError("ChatGPT plan usage не разрешён")
        if self._expires_at(profile) <= time.time() + 120:
            with self._refresh_lock:
                latest = self._load_profile()
                if latest is None:
                    raise ChatGPTAuthError("ChatGPT не подключён")
                if self._expires_at(latest) <= time.time() + 120:
                    profile = self._refresh(latest)
                else:
                    profile = latest
        token = str(profile.get("access_token") or "")
        if not token:
            raise ChatGPTAuthError("ChatGPT OAuth access token отсутствует")
        return token

    def list_models(self) -> list[dict[str, str]]:
        token = self.get_access_token()
        try:
            with self._http_client(15.0) as client:
                response = client.get(
                    self.MODELS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code != 200:
                raise ChatGPTAuthError(
                    f"OpenAI models endpoint вернул HTTP {response.status_code}"
                )
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError("Не удалось получить модели ChatGPT") from exc

        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raise ChatGPTAuthError("OpenAI models endpoint вернул неожиданный формат")

        models: list[dict[str, str]] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if item.get("visibility") != "list":
                continue
            slug = str(item.get("slug") or item.get("id") or "").strip()
            if not slug:
                continue
            models.append(
                {
                    "slug": slug,
                    "display_name": str(item.get("display_name") or slug),
                }
            )
        return models
