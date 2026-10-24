"""Test Wi-Fi (S-51) kept on the Controller (docs/02-data.md 2-7, 11 ``test-wifi.json``).

The password is encrypted with DPAPI on Windows (the service account's scope); the
Linux Controller (development only) keeps it in an owner-only file (0600, decided
2026-09-25). The password is never returned by ``public()``, never logged, never put
in an event, report or error text. Only ``secret()`` gives it, for the join command.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.protocol.envelope import ZlError

SECURITY = ("Open", "WPA2", "WPA3")  # 16 S-51
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WifiConfig:
    ssid: str
    security: str
    password: str  # "" for Open

    def __repr__(self) -> str:  # never shows the password (logs, tracebacks)
        return f"WifiConfig(ssid={self.ssid!r}, security={self.security!r}, password=***)"


def check(ssid: Any, security: Any, password: Any) -> WifiConfig:
    """16 S-51 allowed values. Raises ZlError RUN-014 (the texts never hold the password)."""
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise _bad("the Wi-Fi name (SSID) must be 1 to 32 bytes")
    if security not in SECURITY:
        raise _bad("security must be Open, WPA2 or WPA3")
    if security == "Open":
        if password not in (None, ""):
            raise _bad("an Open Wi-Fi has no password")
        return WifiConfig(ssid, security, "")
    if not isinstance(password, str) or not 8 <= len(password) <= 63:
        raise _bad("the password must be 8 to 63 characters")
    if not password.isprintable() or not password.isascii():
        raise _bad("the password must be printable ASCII (WPA passphrase)")
    return WifiConfig(ssid, security, password)


def _bad(rule: str) -> ZlError:
    return ZlError("RUN-014", f"S-51 Test Wi-Fi: {rule}", "The value is outside the table (16).")


class WifiStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def public(self) -> dict[str, Any]:
        w = self.secret()
        return {
            "ssid": w.ssid if w else None,
            "security": w.security if w else None,
            "has_password": bool(w and w.password),
        }

    def secret(self) -> WifiConfig | None:
        with self._lock:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                pw = d["password"]
                raw = base64.b64decode(pw["data"])
                text = _unprotect(raw) if pw["kind"] == "dpapi" else raw
                return WifiConfig(str(d["ssid"]), str(d["security"]), text.decode("utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise ZlError(
                    "SYS-008", "The saved test Wi-Fi cannot be read",
                    type(e).__name__, "Set it again (zeroloss wifi set).",
                ) from None  # fmt: skip

    def save(self, wifi: WifiConfig) -> dict[str, Any]:
        data = wifi.password.encode("utf-8")
        if sys.platform == "win32":
            pw = {"kind": "dpapi", "data": base64.b64encode(_protect(data)).decode()}
        else:
            pw = {"kind": "plain-0600", "data": base64.b64encode(data).decode()}
        doc = {"schema_version": SCHEMA_VERSION, "ssid": wifi.ssid, "security": wifi.security,
               "password": pw}  # fmt: skip
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            tmp.replace(self.path)
        return self.public()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self.path.unlink(missing_ok=True)
        return self.public()


class BandPasswords:
    """Passwords of the band networks picked in Devices (S-65), one per SSID, kept like
    the test Wi-Fi's (DPAPI on Windows). 2026-10-17 user: "암호가 있으면 암호도 넣게 해 ...
    옆에 칸 만들어 SSID 고르고 암호 넣게". ``ssids()`` says which have one; only
    ``secret()`` gives it, for the join of that network."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.path.read_text(encoding="utf-8")).get("passwords") or {})
        except (OSError, ValueError, AttributeError):
            return {}

    def ssids(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

    def secret(self, ssid: str) -> str | None:
        with self._lock:
            pw = self._read().get(ssid)
        if not isinstance(pw, dict):
            return None
        try:
            raw = base64.b64decode(pw["data"])
            return (_unprotect(raw) if pw["kind"] == "dpapi" else raw).decode("utf-8")
        except (OSError, ValueError, KeyError):
            return None

    def save(self, ssid: str, password: str) -> dict[str, Any]:
        """``password`` "" forgets it. Same rule as S-51: 8 to 63 printable ASCII."""
        if password:
            check(ssid, "WPA2", password)
        with self._lock:
            doc = self._read()
            if password:
                data = password.encode("utf-8")
                if sys.platform == "win32":
                    doc[ssid] = {"kind": "dpapi", "data": base64.b64encode(_protect(data)).decode()}
                else:
                    doc[ssid] = {"kind": "plain-0600", "data": base64.b64encode(data).decode()}
            else:
                doc.pop(ssid, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"schema_version": SCHEMA_VERSION, "passwords": doc}, f)
            tmp.replace(self.path)
        return {"ssid": ssid, "has_password": bool(password)}


# ---------------------------------------------------------------- Windows DPAPI

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _call(fn: Any, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):
            raise OSError("DPAPI call failed")  # 0x1 = CRYPTPROTECT_UI_FORBIDDEN
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def _protect(data: bytes) -> bytes:
        return _call(ctypes.windll.crypt32.CryptProtectData, data)

    def _unprotect(data: bytes) -> bytes:
        return _call(ctypes.windll.crypt32.CryptUnprotectData, data)

else:

    def _protect(data: bytes) -> bytes:
        raise OSError("DPAPI is Windows only")

    def _unprotect(data: bytes) -> bytes:
        raise OSError("DPAPI is Windows only")
