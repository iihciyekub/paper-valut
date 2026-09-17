from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import keyring
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .core import VaultError, atomic_write, encode

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SERVICE = "papervault-cli"


def account_key(state):
    return hashlib.sha256(str(state.home).encode()).hexdigest()


def save_token(state, value):
    if state.config.get("token_store", "keyring") == "file":
        atomic_write(state.home / "token.json", value.encode())
    else:
        try:
            keyring.set_password(SERVICE, account_key(state), value)
        except (keyring.errors.KeyringError, RuntimeError):
            raise VaultError(
                "无法写入系统钥匙串。可显式使用 --token-store file 保存到权限为 600 的本机文件。", 4
            ) from None


def load_token(state):
    if state.config.get("token_store", "keyring") == "file":
        p = state.home / "token.json"
        return p.read_text() if p.exists() else None
    try:
        return keyring.get_password(SERVICE, account_key(state))
    except (keyring.errors.KeyringError, RuntimeError):
        raise VaultError("无法读取系统钥匙串。请解锁钥匙串或重新登录。", 4) from None


def install_client(state, path):
    data = json.loads(Path(path).expanduser().read_text())
    c = data.get("installed", {})
    if not c.get("client_id") or not c.get("client_secret"):
        raise VaultError("需要 Google Desktop app 类型的 OAuth 客户端 JSON。", 4)
    if c.get("auth_uri") not in {
        "https://accounts.google.com/o/oauth2/auth",
        "https://accounts.google.com/o/oauth2/v2/auth",
    }:
        raise VaultError("OAuth 授权端点不是官方 Google 地址。", 4)
    if c.get("token_uri") != "https://oauth2.googleapis.com/token":
        raise VaultError("OAuth token 端点不是官方 Google 地址。", 4)
    atomic_write(state.home / "client.json", encode(data))
    state.save()


def login(state, client=None, token_store="keyring", timeout=600, browser=True):
    if client:
        install_client(state, client)
    p = state.home / "client.json"
    if not p.exists():
        raise VaultError("请提供 --client Desktop OAuth 配置文件。", 4)
    state.config["token_store"] = token_store
    state.save()
    flow = InstalledAppFlow.from_client_secrets_file(str(p), SCOPES, autogenerate_code_verifier=True)
    # Authorization URL is necessary for the user but must not go to JSON stdout/log files.
    import contextlib

    with contextlib.redirect_stdout(sys.stderr):
        creds = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=browser,
            timeout_seconds=timeout,
            authorization_prompt_message="请在浏览器中登录 Google 并授权：\n{url}",
            success_message="PaperVault 授权完成，可以关闭此页面并返回终端。",
            access_type="offline",
            prompt="consent",
        )
    if not creds.refresh_token:
        raise VaultError("未获得刷新令牌，请重新登录并允许离线访问。", 4)
    save_token(state, creds.to_json())
    return {"authorized": True, "token_store": token_store, "scope": SCOPES}


def credentials(state):
    token = load_token(state)
    if not token:
        raise VaultError("尚未授权，请运行 pv auth login。", 4)
    creds = Credentials.from_authorized_user_info(json.loads(token), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError:
            raise VaultError("Google 授权失效，请运行 pv auth login 重新授权。", 4) from None
        except TransportError:
            raise VaultError("刷新 Google 令牌时网络不可用，请稍后重试。", 3) from None
        save_token(state, creds.to_json())
    return creds


def logout(state):
    if state.config.get("token_store", "keyring") == "file":
        (state.home / "token.json").unlink(missing_ok=True)
    else:
        try:
            keyring.delete_password(SERVICE, account_key(state))
        except keyring.errors.PasswordDeleteError:
            pass
    return {"local_token_removed": True, "google_grant_revoked": False}
