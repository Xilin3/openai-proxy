"""ChatGPT session loaded from the local Codex login."""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AuthError(RuntimeError):
    pass


@dataclass(repr=False)
class ChatGPTSession:
    access_token: str = field(repr=False)
    account_id: str
    account_user_id: str
    expires_at: int

    @property
    def expired(self) -> bool:
        return self.expires_at <= int(time.time()) + 30

    def __repr__(self) -> str:
        return (
            "ChatGPTSession(account_id={!r}, account_user_id={!r}, expires_at={!r})".format(
                self.account_id, self.account_user_id, self.expires_at
            )
        )


def _b64json(segment: str) -> dict[str, Any]:
    padded = segment + "=" * ((4 - len(segment) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        value = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthError("access_token 的 JWT payload 无效") from exc
    if not isinstance(value, dict):
        raise AuthError("JWT payload is not an object")
    return value


def load_session(path: Path | None = None) -> ChatGPTSession:
    if path is not None:
        auth_path = path
    else:
        codex_home = os.environ.get("CODEX_HOME")
        auth_path = (Path(codex_home) if codex_home else Path.home() / ".codex") / "auth.json"
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuthError(f"找不到 Codex 登录态：{auth_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthError(f"无法读取 Codex 登录态：{auth_path}") from exc

    tokens = raw.get("tokens") if isinstance(raw, dict) else None
    if not isinstance(tokens, dict):
        raise AuthError("auth.json 里没有 tokens")
    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not isinstance(access_token, str) or access_token.count(".") < 2:
        raise AuthError("auth.json 里没有可用的 access_token")
    if not isinstance(account_id, str) or not account_id:
        raise AuthError("auth.json 里没有 account_id")

    try:
        payload = _b64json(access_token.split(".")[1])
    except AuthError:
        raise
    except (IndexError, ValueError) as exc:
        raise AuthError("access_token 的 JWT 格式无效") from exc
    auth_claims = payload.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    claim_account = auth_claims.get("chatgpt_account_id")
    if isinstance(claim_account, str) and claim_account and claim_account != account_id:
        account_id = claim_account
    account_user_id = auth_claims.get("chatgpt_account_user_id")
    if not isinstance(account_user_id, str):
        account_user_id = ""
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        expires_at = 0
    session = ChatGPTSession(
        access_token=access_token,
        account_id=account_id,
        account_user_id=account_user_id,
        expires_at=expires_at,
    )
    if session.expired:
        raise AuthError("ChatGPT access_token 已过期，先在本机 ChatGPT 里刷新登录")
    return session
