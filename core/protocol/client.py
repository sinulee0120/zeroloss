"""Client for node APIs (docs/02-data.md 3): the Controller side of every control link.

- Keep-alive connection per node (NAT connection tracking stays alive).
- Every request carries the Controller boot ID header; commands carry run ID, a new
  command ID and the current generation (once-only execution on the node).
- Every wait has a limit (docs/09-exceptions.md 0.4: 5 s for control commands,
  1 s for the heartbeat). A failure raises ZlError with a code, never hangs.
- A retry of a command reuses the same command ID, so the node runs it once.
- A node that does not take the connection is tried from this PC's other addresses
  (source_pick, GAP-153: Windows with two default routes).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from core.protocol import source_pick
from core.protocol.envelope import BOOT_HEADER, ZlError

COMMAND_TIMEOUT_S = 5.0
HEARTBEAT_TIMEOUT_S = 1.0


class NodeClient:
    def __init__(self, host: str, port: int, boot_id: str, name: str = "") -> None:
        self.host = host
        self.port = port
        self.boot_id = boot_id
        self.name = name or f"{host}:{port}"
        self._conn: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()  # one request at a time per connection

    def get(self, path: str, timeout_s: float = COMMAND_TIMEOUT_S, **query: Any) -> dict[str, Any]:
        full = f"{path}?{urlencode(query)}" if query else path
        return self._request("GET", full, None, timeout_s)

    def command(
        self,
        path: str,
        generation: int,
        run_id: str | None = None,
        command_id: str | None = None,
        timeout_s: float = COMMAND_TIMEOUT_S,
        **params: Any,
    ) -> dict[str, Any]:
        body = {
            "run_id": run_id,
            "command_id": command_id or str(uuid.uuid4()),
            "generation": generation,
            "params": params,
        }
        return self._request("POST", path, body, timeout_s)

    def upload(self, path: str, file: Path, timeout_s: float = 600.0) -> dict[str, Any]:
        """PUT a file as it is (Deployer ``incoming``, 02 15): streamed, with its SHA-256
        in ``X-ZL-SHA256`` so the receiver can refuse a damaged copy (INS-006)."""
        digest = hashlib.sha256()
        with file.open("rb") as f:
            while block := f.read(1 << 20):
                digest.update(block)
        headers = {
            BOOT_HEADER: self.boot_id,
            "Content-Length": str(file.stat().st_size),
            "X-ZL-SHA256": digest.hexdigest(),
        }
        with self._lock:
            self._drop()  # a fresh connection for a long body
            try:
                conn = self._connection(timeout_s)
                with file.open("rb") as body:
                    conn.request("PUT", path, body=body, headers=headers)
                    raw = conn.getresponse().read()
            except (OSError, http.client.HTTPException) as e:
                self._drop()
                raise self._unreachable(str(e)) from e
            self._drop()
        return self._open_envelope(raw)

    def download(self, path: str, dest: Path, timeout_s: float = 120.0, **query: Any) -> Path:
        """GET a file answer (02 4 ``GET /v1/logs``) into ``dest``: written as ``.part``
        and renamed when complete. A JSON reply is the node's error (raised as ZlError)."""
        full = f"{path}?{urlencode(query)}" if query else path
        part = dest.with_name(dest.name + ".part")
        with self._lock:
            self._drop()  # a fresh connection for a long answer
            try:
                conn = self._connection(timeout_s)
                conn.request("GET", full, headers={BOOT_HEADER: self.boot_id})
                resp = conn.getresponse()
                if resp.getheader("Content-Type", "").startswith("application/json"):
                    raw = resp.read()
                    self._drop()
                    self._open_envelope(raw)  # raises the node's error
                    raise ZlError("DSC-012", f"{self.name} sent no file", detail=full)
                with part.open("wb") as f:
                    while block := resp.read(1 << 20):
                        f.write(block)
                if resp.length not in (None, 0):
                    raise self._unreachable("the file was cut off")
            except (OSError, http.client.HTTPException) as e:
                part.unlink(missing_ok=True)
                self._drop()
                raise self._unreachable(str(e)) from e
            except ZlError:
                part.unlink(missing_ok=True)
                self._drop()
                raise
            self._drop()
        part.replace(dest)
        return dest

    def close(self) -> None:
        with self._lock:
            self._drop()

    # ---------------------------------------------------------------- helpers

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None, timeout_s: float
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {BOOT_HEADER: self.boot_id, "Content-Type": "application/json"}
        with self._lock:
            for attempt in (1, 2):  # a stale keep-alive connection gets one fresh retry
                try:
                    conn = self._connection(timeout_s)
                    conn.request(method, path, body=data, headers=headers)
                    resp = conn.getresponse()
                    raw = resp.read()
                    break
                # Windows says ConnectionAbortedError (WinError 10053) for the same stale
                # connection: 2026-10-09/10 the screen's Stop failed on it twice (GAP-150)
                except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):  # fmt: skip
                    self._drop()
                    if attempt == 2:
                        raise self._unreachable("connection closed") from None
                except TimeoutError:
                    self._drop()
                    raise self._unreachable(f"no answer in {timeout_s:g} s") from None
                except OSError as e:
                    self._drop()
                    raise self._unreachable(str(e)) from e
        return self._open_envelope(raw)

    def _open_envelope(self, raw: bytes) -> dict[str, Any]:
        try:
            envelope = json.loads(raw)
        except ValueError:
            raise ZlError(
                "DSC-012",
                f"{self.name} sent a reply that is not JSON",
                detail=raw[:200].decode("utf-8", "replace"),
            ) from None
        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            raise ZlError(
                str(err.get("code", "ZL-SYS-008")),
                str(err.get("what", "Request failed")),
                str(err.get("why", "")),
                str(err.get("fix", "")),
                str(err.get("detail", "")),
            )
        data_out: dict[str, Any] = envelope.get("data") or {}
        return data_out

    def _connection(self, timeout_s: float) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = self._connect(timeout_s)
        self._conn.timeout = timeout_s
        if self._conn.sock is not None:
            self._conn.sock.settimeout(timeout_s)
        return self._conn

    def _connect(self, timeout_s: float) -> http.client.HTTPConnection:
        """A new connection, from the local address that reached this node before
        (GAP-153). When it is refused or times out, the other local addresses are tried
        once; OSError when none connects."""
        source = source_pick.source_for(self.host, self.port)
        try:
            return self._open(source, timeout_s)
        except OSError:
            if source is not None:
                source_pick.forget(self.host, self.port)  # the plain route next time
            other = source_pick.find(self.host, self.port)
            if other is None or other == source:
                raise
            return self._open(other, timeout_s)

    def _open(self, source: str | None, timeout_s: float) -> http.client.HTTPConnection:
        bind = (source, 0) if source else None
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout_s,
                                          source_address=bind)  # fmt: skip
        try:
            conn.connect()
        except OSError:
            conn.close()
            raise
        return conn

    def _drop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _unreachable(self, why: str) -> ZlError:
        return ZlError(
            "DSC-014",
            f"Cannot reach {self.name}",
            why,
            "Check the PC, its network cable and that the ZeroLoss service runs.",
            detail=f"{self.host}:{self.port}",
        )
