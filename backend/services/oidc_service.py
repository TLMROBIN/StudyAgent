from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from jose import jwt
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.models.user import User
from backend.services.auth_service import auth_service


class OidcAuthError(ValueError):
    pass


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class OidcService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.settings.oidc_issuer.rstrip('/')}/protocol/openid-connect/auth"

    @property
    def token_endpoint(self) -> str:
        return f"{self.settings.oidc_issuer.rstrip('/')}/protocol/openid-connect/token"

    @property
    def jwks_uri(self) -> str:
        return f"{self.settings.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"

    def create_login_state(self) -> dict[str, str]:
        verifier = secrets.token_urlsafe(48)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.settings.oidc_client_id,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "scope": self.settings.oidc_scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return {
            "state": state,
            "code_verifier": verifier,
            "authorization_url": f"{self.authorization_endpoint}?{urlencode(params)}",
        }

    def exchange_code_for_claims(self, code: str, code_verifier: str) -> dict:
        data = {
            "grant_type": "authorization_code",
            "client_id": self.settings.oidc_client_id,
            "code": code,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "code_verifier": code_verifier,
        }
        if self.settings.oidc_client_secret:
            data["client_secret"] = self.settings.oidc_client_secret
        with httpx.Client(timeout=10) as client:
            token_response = client.post(self.token_endpoint, data=data)
            token_response.raise_for_status()
            token_payload = token_response.json()
            jwks_response = client.get(self.jwks_uri)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
        id_token = token_payload.get("id_token")
        if not id_token:
            raise OidcAuthError("OIDC token response did not include id_token")
        try:
            return jwt.decode(
                id_token,
                jwks,
                algorithms=["RS256"],
                audience=self.settings.oidc_client_id,
                issuer=self.settings.oidc_issuer.rstrip("/"),
            )
        except Exception as exc:  # jose raises several concrete JWT errors.
            raise OidcAuthError("OIDC id_token validation failed") from exc

    def issue_local_tokens_for_claims(self, db: Session, claims: dict) -> dict[str, str | int | bool]:
        username = claims.get("preferred_username") or claims.get("email", "").split("@", 1)[0]
        subject = claims.get("sub")
        if not username and not subject:
            raise OidcAuthError("OIDC claims did not include a usable username")

        filters = []
        if username:
            filters.append(User.username == username)
        if subject:
            filters.append(User.student_no == subject)
        user = db.scalar(select(User).where(or_(*filters)).limit(1))
        if not user or not user.is_active:
            raise OidcAuthError("OIDC user is not bound to an active StudyAgent account")

        tokens = auth_service.issue_token_pair(user)
        tokens["role"] = user.role.value
        return tokens


oidc_service = OidcService()
