"""Small JSON API server on the Python standard library (docs/12-architecture.md 1-1).

- HTTP/1.1 keep-alive (NAT connection tracking stays alive, docs/02-data.md 3).
- Every response is the common envelope. Unknown path, bad JSON or a too large body
  -> DSC-012. A handler bug -> SYS-008, published as an event; never a silent failure.
- ``command`` routes go through the node's CommandGuard (once-only, generation) and
  report a Controller restart (new ``X-ZL-Controller-Boot``) before running (CTL-008).
- Handlers run on the connection's thread: keep them short, hand long work to the
  node's single work queue.
- ``stream`` routes send Server-Sent Events (docs/02-data.md 2, port 10994): each item
  the generator yields is one ``data:`` line; ``None`` sends a keep-alive comment; ``IDLE``
  sends nothing but checks that the client is still there. When the client goes away
  the generator is closed, so it can unsubscribe.
"""

from __future__ import annotations

import contextlib
import json
import select
import socket
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.events.bus import Event, EventBus, utc_now
from core.protocol.envelope import BOOT_HEADER, CommandGuard, Request, ZlError, fail, ok

MAX_BODY_BYTES = 1 << 20
IDLE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class Query:
    params: dict[str, str]
    client_ip: str
    local_ip: str = ""  # this node's address the request came in on (07 3 same subnet)

    def int(self, name: str, default: int | None = None) -> int:
        raw = self.params.get(name)
        if raw is None:
            if default is None:
                raise _bad(f"query parameter {name} is required")
            return default
        try:
            return int(raw)
        except ValueError:
            raise _bad(f"query parameter {name} must be an integer") from None


GetHandler = Callable[[Query], Mapping[str, Any] | None]
CommandHandler = Callable[[Request], Mapping[str, Any] | None]
# A report, not a command (02 2-12): no command_id memory, no generation; repeating it
# gives the same result. Local screens only.
ReportHandler = Callable[[dict[str, Any]], Mapping[str, Any] | None]


class _Idle:
    """Yielded by a stream to let the server check the client between items."""


IDLE = _Idle()
StreamHandler = Callable[[Query], Iterator[Mapping[str, Any] | _Idle | None]]
# A file answer (02 4 ``GET /v1/logs``): the handler makes a new file, the server sends
# it as it is and removes it afterwards. Errors still go back as the JSON envelope.
FileHandler = Callable[[Query], Path]


def _bad(detail: str) -> ZlError:
    return ZlError(
        "DSC-012", "Request rejected", "The request is not in the expected form.", "", detail
    )


class OwnPortServer(ThreadingHTTPServer):
    """A port only this process listens on. On Windows the standard server's
    SO_REUSEADDR lets a second process bind the same port and take requests (2026-10-08
    GAP-117: two quick double-clicks ran two Controllers on 10994); there the port is
    taken with SO_EXCLUSIVEADDRUSE, so a second one fails at once. Linux SO_REUSEADDR
    never allows two listeners and stays (a restart may reuse a port in TIME_WAIT)."""

    if sys.platform == "win32":
        allow_reuse_address = False

        def server_bind(self) -> None:
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                getattr(socket, "SO_EXCLUSIVEADDRUSE"),  # noqa: B009 (Windows only)
                1,
            )
            super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that went away (a window closed, a page reloaded) is not an error:
        one short line instead of a traceback. 2026-10-08 GAP-120: 700 lines of these
        hid the service log of the run the user had to kill."""
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            print(f"{utc_now()} client {client_address[0]} went away ({type(err).__name__})",
                  file=sys.stderr, flush=True)  # fmt: skip
            return
        print(f"{utc_now()} request from {client_address[0]} failed:", file=sys.stderr, flush=True)
        super().handle_error(request, client_address)


class ApiServer:
    def __init__(
        self,
        host: str,
        port: int,
        guard: CommandGuard,
        bus: EventBus,
        on_controller_restart: Callable[[], None] | None = None,
        on_request: Callable[[], None] | None = None,
    ) -> None:
        self._gets: dict[str, GetHandler] = {}
        self._commands: dict[str, CommandHandler] = {}
        self._reports: dict[str, ReportHandler] = {}
        self._streams: dict[str, StreamHandler] = {}
        self._files: dict[str, FileHandler] = {}
        self._guard = guard
        self._bus = bus
        self._on_restart = on_controller_restart
        self._on_request = on_request
        self._server = OwnPortServer((host, port), self._handler_class())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None
        self._open: set[socket.socket] = set()  # live connections, closed on stop()
        self._open_lock = threading.Lock()
        self._stopping = False

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def get(self, path: str, fn: GetHandler) -> None:
        self._gets[path] = fn

    def command(self, path: str, fn: CommandHandler) -> None:
        self._commands[path] = fn

    def report(self, path: str, fn: ReportHandler) -> None:
        self._reports[path] = fn

    def stream(self, path: str, fn: StreamHandler) -> None:
        self._streams[path] = fn

    def file(self, path: str, fn: FileHandler) -> None:
        self._files[path] = fn

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("server already started")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},  # stop() returns within 0.1 s
            name=f"api-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop accepting, close every open connection (a stopped node must never answer
        on a kept-alive connection) and wait for the serving thread."""
        self._stopping = True
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(5)
            self._thread = None
        self._server.server_close()
        with self._open_lock:
            conns, self._open = list(self._open), set()
        for conn in conns:
            with contextlib.suppress(OSError):  # already gone
                conn.shutdown(socket.SHUT_RDWR)

    # ---------------------------------------------------------------- dispatch

    def _dispatch_get(
        self, path: str, query: Query, boot_id: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        fn = self._gets.get(path)
        if fn is None:
            return self._not_found(path)
        self._check_boot(boot_id)  # every request carries it, GET too (02 3, 20 GAP-49)
        return 200, ok(fn(query))

    def _check_boot(self, boot_id: str | None) -> None:
        """Only the Controller-facing API (it has the restart hook) tracks boot IDs;
        a local screen or CLI with its own ID must never look like a new Controller."""
        if self._on_restart is not None and self._guard.controller_boot(boot_id):
            self._on_restart()

    def _dispatch_post(
        self, path: str, body: bytes, boot_id: str | None
    ) -> tuple[int, dict[str, Any]]:
        fn = self._commands.get(path)
        rep = self._reports.get(path)
        if fn is None and rep is None:
            return self._not_found(path)
        try:
            decoded = json.loads(body or b"{}")
        except ValueError:
            raise _bad("body is not valid JSON") from None
        if rep is not None:
            params = decoded.get("params") if isinstance(decoded, dict) else None
            if not isinstance(params, dict):
                raise _bad("params must be an object")
            return 200, ok(rep(params))
        assert fn is not None
        req = Request.parse(decoded)
        self._check_boot(boot_id)
        return 200, self._guard.run(req, fn)

    def _not_found(self, path: str) -> tuple[int, dict[str, Any]]:
        return 404, fail(_bad(f"no such path: {path}"))

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = IDLE_TIMEOUT_S
            server_version = "ZeroLoss"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                with api._open_lock:
                    api._open.add(self.connection)

            def finish(self) -> None:
                with api._open_lock:
                    api._open.discard(self.connection)
                super().finish()

            def handle_one_request(self) -> None:
                if api._stopping:
                    self.close_connection = True
                    return
                super().handle_one_request()

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                query = Query(
                    {k: v[-1] for k, v in parse_qs(url.query).items()},
                    self.client_address[0],
                    str(self.connection.getsockname()[0]),
                )
                stream = api._streams.get(url.path)
                if stream is not None:
                    self._stream(stream, query)
                    return
                boot = self.headers.get(BOOT_HEADER)
                file = api._files.get(url.path)
                if file is not None:
                    self._file(file, query, boot)
                    return
                self._answer(lambda: api._dispatch_get(url.path, query, boot))

            def _file(self, fn: FileHandler, query: Query, boot: str | None) -> None:
                made: list[Path] = []

                def work() -> tuple[int, dict[str, Any]]:
                    api._check_boot(boot)
                    made.append(fn(query))
                    return 200, {}

                if api._on_request is not None:
                    api._on_request()
                status, payload = self._run(work)
                if not made:
                    self._send(status, payload)
                    return
                path = made[0]
                try:
                    self.send_response(HTTPStatus.OK)
                    kind = "application/x-ndjson" if path.suffix == ".jsonl" else "application/zip"
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(path.stat().st_size))
                    self.end_headers()
                    with path.open("rb") as f:
                        while block := f.read(1 << 20):
                            self.wfile.write(block)
                except OSError:
                    self.close_connection = True  # the client went away mid-file
                finally:
                    path.unlink(missing_ok=True)

            def _stream(self, fn: StreamHandler, query: Query) -> None:
                if api._on_request is not None:
                    api._on_request()
                items = fn(query)
                self.close_connection = True
                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for item in items:
                        if isinstance(item, _Idle):
                            if self._client_closed():
                                return
                            continue
                        if item is None:
                            chunk = b": keep-alive\n\n"
                        else:
                            text = json.dumps(item, separators=(",", ":"), ensure_ascii=False)
                            chunk = f"data: {text}\n\n".encode()
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    pass  # the client went away; closing the generator unsubscribes it
                finally:
                    close = getattr(items, "close", None)
                    if close is not None:
                        close()

            def do_POST(self) -> None:
                url = urlsplit(self.path)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY_BYTES:
                    self.close_connection = True
                    self._send(413, fail(_bad(f"body size {length} not accepted")))
                    return
                body = self.rfile.read(length)
                boot = self.headers.get(BOOT_HEADER)
                self._answer(lambda: api._dispatch_post(url.path, body, boot))

            def _client_closed(self) -> bool:
                conn: socket.socket = self.connection
                readable, _, _ = select.select([conn], [], [], 0)
                if not readable:
                    return False
                try:
                    return conn.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    return True

            def _answer(self, work: Callable[[], tuple[int, dict[str, Any]]]) -> None:
                if api._on_request is not None:
                    api._on_request()
                self._send(*self._run(work))

            def _run(
                self, work: Callable[[], tuple[int, dict[str, Any]]]
            ) -> tuple[int, dict[str, Any]]:
                try:
                    status, payload = work()
                except ZlError as e:
                    status, payload = 400, fail(e)
                except Exception as e:  # a handler bug: report, never hide
                    err = ZlError(
                        "SYS-008",
                        "Internal error",
                        "ZeroLoss hit an unexpected problem.",
                        "Please send the logs.",
                        detail=repr(e),
                    )
                    api._bus.publish(Event(err.code, "error", f"{self.path}: {e!r}"))
                    status, payload = 500, fail(err)
                return status, payload

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
                self.send_response(HTTPStatus(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass  # access lines would flood the log once a second; errors are events

        return Handler
