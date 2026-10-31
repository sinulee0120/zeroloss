"""Results web on TCP 10993 (docs/02-data.md 12, 07 7, 16 S-44, 09 CTL-006, 20 GAP-86).

One job: show the reports and the running test to a browser, read only, with the
standard library only. ``/`` lists the reports, ``/live`` shows the running test's
device rows and 5-minute cells, ``/reports/<folder>/…`` serves a report folder's own
files (never outside it, never hidden files or zips). Who may look is S-44: Off,
This PC only, or Same subnet (the Controller PC's own networks). Bytes sent during a
test are counted for the report (07 7: viewing over the tested Wi-Fi mixes in).
"""

from __future__ import annotations

import html
import ipaddress
import json
import mimetypes
import threading
from collections.abc import Callable, Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from controller import live_page
from core.protocol.http_server import OwnPortServer
from core.ui_schema.design import COLORS as DESIGN
from core.ui_schema.design import ICONS  # one place for the shapes (11-ui 1.7)

WEB_PORT = 10993
ACCESS = ("Off", "This PC only", "Same subnet")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".json": "application/json", ".csv": "text/csv; charset=utf-8",
         ".pdf": "application/pdf", ".png": "image/png", ".log": "text/plain; charset=utf-8",
         ".jsonl": "text/plain; charset=utf-8"}  # fmt: skip
COLORS = {k: DESIGN[k] for k in ("green", "yellow", "red", "purple", "grey")}  # one place

WIRED_ONLY = ("Viewing during a test: use a wired PC (Wi-Fi viewing mixes into the test, "
              "K-22).")  # fmt: skip


def _value(item: dict[str, Any]) -> str:
    v = item.get("value")
    return "" if v is None else f"{v}{item.get('unit') or ''}"


class Traffic:
    """Requests and bytes sent while a test runs, per run (02 10 ``results_web``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, int]] = {}

    def count(self, run: str | None, sent: int) -> None:
        if not run:
            return
        with self._lock:
            c = self._runs.setdefault(run, {"requests": 0, "bytes": 0})
            c["requests"] += 1
            c["bytes"] += sent

    def take(self, run: str) -> dict[str, int]:
        with self._lock:
            return self._runs.pop(run, {"requests": 0, "bytes": 0})


def allowed(client: str, mode: str, networks: Iterable[str]) -> bool:
    """S-44: Off = nobody, This PC only = loopback, Same subnet = loopback or an
    address inside one of this PC's networks (``192.168.1.0/24`` strings)."""
    if mode == "Off":
        return False
    try:
        ip = ipaddress.ip_address(client.split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback or (ip.version == 6 and ip.ipv4_mapped and ip.ipv4_mapped.is_loopback):
        return True
    if mode != "Same subnet":
        return False
    v4 = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip
    for net in networks:
        try:
            if v4 in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


class ResultsWeb:
    def __init__(
        self,
        reports_dir: Path,
        listing: Callable[[], dict[str, Any]],
        live: Callable[[], dict[str, Any]],
        access: Callable[[], str],
        networks: Callable[[], list[str]],
        running: Callable[[], str | None],
        traffic: Traffic,
        host: str = "0.0.0.0",
        port: int = WEB_PORT,
        live_data: Callable[[], dict[str, Any]] | None = None,
        guide: Callable[[], str | None] | None = None,
    ) -> None:
        self.reports_dir = reports_dir
        self._guide = guide  # the install and use guide (the screen's Help, 08 2)
        self._listing, self._live, self._access = listing, live, access
        self._live_data = live_data  # live monitor (02 12 /live) (GAP-119)
        self._networks, self._running, self.traffic = networks, running, traffic
        self._host, self._port = host, port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> None:
        """Opens the port; an OSError (in use, CTL-006) goes to the caller."""
        self._server = OwnPortServer((self._host, self._port), self._handler())
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.2}, name="results-web",
                                        daemon=True)  # fmt: skip
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---------------------------------------------------------------- pages

    def page(self, path: str) -> tuple[int, str, bytes]:
        """(status, content type, body) for one GET path."""
        if path in ("", "/"):
            return 200, TYPES[".html"], self._index().encode()
        if path == "/live":
            if self._live_data is not None:  # the live monitor (GAP-119)
                return 200, TYPES[".html"], live_page.page().encode()
            return 200, TYPES[".html"], self._live_page().encode()
        if path == "/guide" and self._guide is not None:
            text = self._guide()
            if text is not None:
                return 200, TYPES[".html"], text.encode()
        if path == "/live.json" and self._live_data is not None:
            body = json.dumps(self._live_data(), ensure_ascii=False, default=str)
            return 200, "application/json; charset=utf-8", body.encode()
        if path.startswith("/reports/"):
            f = self._file(path.removeprefix("/reports/"))
            if f is not None:
                kind = TYPES.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "text/plain"
                return 200, kind, f.read_bytes()
        return 404, TYPES[".html"], b"<!doctype html><p>Not found</p>"

    def _file(self, rel: str) -> Path | None:
        parts = [p for p in unquote(rel).split("/") if p]
        if not parts or any(p.startswith(".") or p == ".." for p in parts):
            return None
        root = self.reports_dir.resolve()
        f = (root / Path(*parts)).resolve()
        if root not in f.parents or not f.is_file() or f.suffix == ".zip":
            return None  # outside the report folders, missing, or a diagnostics file
        return f

    def _head(self, title: str, refresh: bool = False) -> str:
        meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
        note = f"<p><strong>{html.escape(WIRED_ONLY)}</strong></p>" if self._running() else ""
        return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>{meta}"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                f"<title>{html.escape(title)}</title><style>body{{font-family:system-ui,sans-serif;"
                "margin:16px;color:#1b1b1b;background:#fafafa}td,th{padding:6px 8px;"
                "border-bottom:1px solid #ddd;text-align:left}.c{display:inline-block;width:12px;"
                "height:12px;margin:1px;border-radius:2px;color:#fff;font-size:9px;"
                "text-align:center;line-height:12px}</style></head><body>"
                f"<h1>{html.escape(title)}</h1>{note}")  # fmt: skip

    def _index(self) -> str:
        e = html.escape
        d = self._listing()
        rows = "".join(
            f"<tr><td>{e(str(r['started']))}</td><td>{e(str(r['run']))}</td>"
            f"<td>{e(str(r.get('test_type') or ''))} {e(str(r.get('quality') or ''))}</td>"
            f"<td>{e(str(r.get('verdict') or r.get('why') or ''))}</td><td>"
            + (f"<a href='/reports/{e(str(r['run']))}/report.html'>Report</a> "
               f"<a href='/reports/{e(str(r['run']))}/report.pdf'>PDF</a>"
               if r.get("verdict") else "")
            + "</td></tr>"
            for r in d.get("reports") or []
        )  # fmt: skip
        rows = rows or "<tr><td colspan=5>No reports yet</td></tr>"
        return (self._head("ZeroLoss reports") + "<p><a href='/live'>The test now</a></p>"
                "<table><tr><th>Started</th><th>Run</th><th>Test</th><th>Result</th><th></th></tr>"
                f"{rows}</table></body></html>")  # fmt: skip

    def _live_page(self) -> str:
        e = html.escape
        run = self._live().get("run") or {}
        body = f"<p>State: {e(str(run.get('state')))} · run {e(str(run.get('run_id') or '-'))}</p>"
        for name, dev in (run.get("devices") or {}).items():
            row = dev.get("row") or {}
            items = " · ".join(
                f"{i.get('label')} {_value(i)}" for i in row.get("items") or [])  # fmt: skip
            color = str(row.get("color") or "grey")
            body += (f"<p><span style='color:{COLORS.get(color, '#616161')}'>{ICONS.get(color, '')}"
                     f"</span> <strong>{e(str(name))}</strong> {e(items)}</p>")  # fmt: skip
        for name, cells in (run.get("cells") or {}).items():
            spans = "".join(
                f"<span class='c' style='background:{COLORS.get(str(c.get('color')), '#616161')}'>"
                f"{ICONS.get(str(c.get('color')), '')}</span>"
                for c in cells
            )
            body += f"<p>{e(str(name))} {spans}</p>"
        return self._head("ZeroLoss - the test now", refresh=True) + body + (
            "<p><a href='/'>Reports</a></p></body></html>")  # fmt: skip

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        web = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ZeroLossWeb"

            def log_message(self, *args: Any) -> None:  # quiet
                return

            def do_GET(self) -> None:
                if not allowed(self.client_address[0], web._access(), web._networks()):
                    code, kind, body = 403, TYPES[".html"], b"<!doctype html><p>Not allowed</p>"
                else:
                    try:
                        code, kind, body = web.page(urlparse(self.path).path)
                    except Exception:  # a broken page never stops the web
                        code, kind, body = 500, TYPES[".html"], b"<!doctype html><p>Error</p>"
                try:
                    self.send_response(HTTPStatus(code))
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    return
                web.traffic.count(web._running(), len(body))

            def do_POST(self) -> None:
                self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)  # read only (02 12)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler
