"""Steam sign-in through IAuthenticationService over HTTPS (stdlib only).

The same flow the Steam client uses, with platform_type = SteamClient so the
refresh token is accepted by a CM logon (the downloader worker uses it as
``access_token``). Two ways in:

* QR: BeginAuthSessionViaQR -> show challenge_url as a QR code -> the Steam
  mobile app scans it -> PollAuthSessionStatus returns the tokens;
* credentials: RSA-encrypted password -> BeginAuthSessionViaCredentials ->
  a Steam Guard code (UpdateAuthSessionWithSteamGuardCode) or a tap in the
  mobile app -> poll.

The refresh token (valid ~200 days) is stored in auth.json, mode 0600; the
password is never stored.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .state import data_dir

API = "https://api.steampowered.com/IAuthenticationService/{method}/v1"
PLATFORM_STEAM_CLIENT = 1
PERSISTENT = 1
DEVICE_NAME = f"Steam Downgrader ({socket.gethostname()})"

# EAuthSessionGuardType
GUARD_NONE, GUARD_EMAIL_CODE, GUARD_DEVICE_CODE, GUARD_DEVICE_CONFIRM, GUARD_EMAIL_CONFIRM = 1, 2, 3, 4, 5

ERESULT_OK, ERESULT_INVALID_PASSWORD, ERESULT_FILE_NOT_FOUND, ERESULT_EXPIRED = 1, 5, 9, 27
ERESULT_RATE_LIMIT, ERESULT_TWOFACTOR_MISMATCH = 84, 88


class AuthError(RuntimeError):
    def __init__(self, message: str, eresult: int = 0):
        super().__init__(message)
        self.eresult = eresult


def _call(method: str, fields: dict, get: bool = False) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    url = API.format(method=method)
    if get:
        req = urllib.request.Request(f"{url}?{data.decode()}", headers={"User-Agent": "steam-downgrader"})
    else:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "steam-downgrader"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            eresult = int(r.headers.get("X-eresult", ERESULT_OK))
            body = json.load(r).get("response", {})
    except urllib.error.HTTPError as e:
        raise AuthError(f"Steam ответил HTTP {e.code}", int(e.headers.get("X-eresult", 0) or 0)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AuthError(f"Нет связи со Steam: {e}") from e
    if eresult != ERESULT_OK:
        raise AuthError(_eresult_text(eresult), eresult)
    return body


def _eresult_text(e: int) -> str:
    return {
        ERESULT_INVALID_PASSWORD: "Неверный логин или пароль",
        ERESULT_FILE_NOT_FOUND: "Сессия входа истекла",
        ERESULT_EXPIRED: "Сессия входа истекла",
        ERESULT_RATE_LIMIT: "Слишком много попыток входа — подождите несколько минут",
        ERESULT_TWOFACTOR_MISMATCH: "Неверный код Steam Guard",
        65: "Неверный код Steam Guard",
    }.get(e, f"Ошибка Steam (EResult {e})")


# --- RSA (PKCS#1 v1.5) -----------------------------------------------------------


def rsa_encrypt(message: bytes, mod_hex: str, exp_hex: str) -> str:
    n, e = int(mod_hex, 16), int(exp_hex, 16)
    k = (n.bit_length() + 7) // 8
    if len(message) > k - 11:
        raise AuthError("Слишком длинный пароль")
    pad = b""
    while len(pad) < k - 3 - len(message):
        pad += bytes(b for b in secrets.token_bytes(k) if b)  # non-zero padding
    block = b"\x00\x02" + pad[: k - 3 - len(message)] + b"\x00" + message
    c = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(c.to_bytes(k, "big")).decode()


# --- sessions ----------------------------------------------------------------------


@dataclass
class PollResult:
    refresh_token: str = ""
    account_name: str = ""
    new_challenge_url: str = ""
    remote_interaction: bool = False

    @property
    def done(self) -> bool:
        return bool(self.refresh_token)


class _Session:
    client_id = ""
    request_id = ""
    steamid = ""
    interval = 5.0

    def poll(self) -> PollResult:
        r = _call("PollAuthSessionStatus", {"client_id": self.client_id, "request_id": self.request_id})
        if r.get("new_client_id"):
            self.client_id = str(r["new_client_id"])
        return PollResult(
            refresh_token=r.get("refresh_token", ""),
            account_name=r.get("account_name", ""),
            new_challenge_url=r.get("new_challenge_url", ""),
            remote_interaction=bool(r.get("had_remote_interaction")),
        )


class QrSession(_Session):
    def start(self) -> str:
        r = _call("BeginAuthSessionViaQR", {"device_friendly_name": DEVICE_NAME, "platform_type": PLATFORM_STEAM_CLIENT})
        self.client_id, self.request_id = str(r["client_id"]), r["request_id"]
        self.interval = float(r.get("interval", 5))
        return r["challenge_url"]


class CredentialsSession(_Session):
    allowed: list[int]

    def start(self, username: str, password: str) -> list[int]:
        key = _call("GetPasswordRSAPublicKey", {"account_name": username}, get=True)
        r = _call("BeginAuthSessionViaCredentials", {
            "device_friendly_name": DEVICE_NAME,
            "account_name": username,
            "encrypted_password": rsa_encrypt(password.encode(), key["publickey_mod"], key["publickey_exp"]),
            "encryption_timestamp": key["timestamp"],
            "remember_login": "1",
            "platform_type": PLATFORM_STEAM_CLIENT,
            "persistence": PERSISTENT,
            "website_id": "Client",
        })
        if "client_id" not in r:
            raise AuthError("Неверный логин или пароль", ERESULT_INVALID_PASSWORD)
        self.client_id, self.request_id = str(r["client_id"]), r["request_id"]
        self.steamid = str(r.get("steamid", ""))
        self.interval = float(r.get("interval", 5))
        self.allowed = [int(c.get("confirmation_type", 0)) for c in r.get("allowed_confirmations", [])]
        return self.allowed

    def submit_code(self, code: str, code_type: int) -> None:
        _call("UpdateAuthSessionWithSteamGuardCode", {
            "client_id": self.client_id,
            "steamid": self.steamid,
            "code": code.strip(),
            "code_type": code_type,
        })


# --- token storage -------------------------------------------------------------------


def _token_path():
    return data_dir() / "auth.json"


@dataclass
class Token:
    account_name: str
    refresh_token: str

    @property
    def expires(self) -> int:
        """exp claim of the JWT, 0 if unreadable."""
        try:
            payload = self.refresh_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
        except (IndexError, ValueError):
            return 0

    @property
    def valid(self) -> bool:
        exp = self.expires
        return bool(self.refresh_token) and (exp == 0 or exp > time.time() + 60)


def load_token() -> Token | None:
    try:
        d = json.loads(_token_path().read_text())
        t = Token(d["account_name"], d["refresh_token"])
    except (OSError, ValueError, KeyError):
        return None
    return t if t.valid else None


def save_token(account_name: str, refresh_token: str) -> None:
    p = _token_path()
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"account_name": account_name, "refresh_token": refresh_token}, f)
    os.replace(tmp, p)


def clear_token() -> None:
    _token_path().unlink(missing_ok=True)
