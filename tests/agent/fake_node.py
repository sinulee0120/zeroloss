"""A stand-in for the node's /v1/health (Deployer tests): state, boot ID and whether
it answers at all are set by the test; restarting it gives a new boot ID."""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent.deployer.agent_link import ServiceControl


class FakeNode(ServiceControl):
    def __init__(self) -> None:
        self.state = "IDLE"
        self.boot_id = str(uuid.uuid4())
        self.device_id = str(uuid.uuid4())
        self.answering = True
        self.restarts = 0
        self.build = "20260925.1200"
        node = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_GET(self) -> None:
                if not node.answering:
                    time.sleep(3)  # hung: the Deployer's 2 s limit runs out
                    return
                body = json.dumps({"ok": True, "data": {
                    "build": node.build, "state": node.state, "boot_id": node.boot_id,
                    "device_id": node.device_id, "role": "agent", "deployer_api": 1,
                }}).encode()  # fmt: skip
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def restart(self) -> None:
        self.restarts += 1
        self.boot_id = str(uuid.uuid4())
        self.state = "IDLE"
        self.answering = True

    def stop(self) -> None:
        self.answering = False

    def start(self) -> None:
        self.boot_id = str(uuid.uuid4())
        self.answering = True

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
