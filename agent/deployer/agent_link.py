"""The node on this PC, as the Deployer sees it (docs/06-install.md 11.2, 09 SYS-009).

The Deployer asks the node's ``/v1/health`` over HTTP (no Core code, L0). It never sends
a Controller boot header, so it can never look like a new Controller (20 GAP-49).
Services are restarted through the OS: systemd on Linux, the service manager on Windows.
Only short OS commands with a time limit are run here (12 4-3 rule 1: the Deployer, L0,
cannot use Core's process_runner); tests bring their own ``ServiceControl``.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import time
from typing import Any

from agent.deployer.modules import Agent

# no console window flashing for sc/taskkill (core.blocks.process_runner.NO_WINDOW; the
# deployer does not import core)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TEST_RUNNING = ("STARTING", "RUNNING", "STOPPING")  # node states in a test (INS-022)


class ServiceControl:
    def restart(self) -> None:
        """Stop hard (a hung node must not delay it) and start again."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError


class SystemdService(ServiceControl):
    def __init__(self, unit: str) -> None:
        self.unit = unit

    def restart(self) -> None:
        # KILL goes to the whole cgroup, so the node's players and senders go too (12 4-3)
        subprocess.run(
            ["systemctl", "kill", "--signal=KILL", self.unit],
            check=False,
            timeout=30,
            creationflags=NO_WINDOW,
        )
        subprocess.run(
            ["systemctl", "restart", self.unit], check=True, timeout=60, creationflags=NO_WINDOW
        )

    def graceful_restart(self) -> None:
        """A node that answers is stopped as it asks (its own clean-up: cards, time sync,
        players), the cgroup goes with it (KillMode=control-group); systemd still kills
        it after TimeoutStopSec. 2026-10-23 receiverPC: Update PCs SIGKILLed the node
        twice within 4 s, once per module, both while it was starting."""
        subprocess.run(
            ["systemctl", "restart", self.unit], check=True, timeout=120, creationflags=NO_WINDOW
        )

    def stop(self) -> None:
        subprocess.run(
            ["systemctl", "stop", self.unit], check=False, timeout=60, creationflags=NO_WINDOW
        )

    def start(self) -> None:
        subprocess.run(
            ["systemctl", "start", self.unit], check=False, timeout=60, creationflags=NO_WINDOW
        )


class WindowsService(ServiceControl):
    def __init__(self, name: str) -> None:
        self.name = name

    def restart(self) -> None:
        # the node's Job Object ends its children with it (12 4-3)
        subprocess.run(
            ["taskkill", "/F", "/FI", f"SERVICES eq {self.name}"],
            check=False,
            timeout=30,
            creationflags=NO_WINDOW,
        )
        subprocess.run(["sc", "start", self.name], check=False, timeout=60, creationflags=NO_WINDOW)

    def stop(self) -> None:
        subprocess.run(["sc", "stop", self.name], check=False, timeout=60, creationflags=NO_WINDOW)

    def start(self) -> None:
        subprocess.run(["sc", "start", self.name], check=False, timeout=60, creationflags=NO_WINDOW)


class AgentLink(Agent):
    def __init__(self, service: ServiceControl, port: int = 10992, host: str = "127.0.0.1",
                 come_back_s: float = 60.0) -> None:  # fmt: skip
        self.service = service
        self.port = port
        self.host = host
        self.come_back_s = come_back_s

    def health(self, timeout_s: float = 2.0) -> dict[str, Any] | None:
        """The node's ``/v1/health`` data, or None when it does not answer."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout_s)
        try:
            conn.request("GET", "/v1/health")
            body = json.loads(conn.getresponse().read())
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            conn.close()
        data = body.get("data") if isinstance(body, dict) and body.get("ok") else None
        return data if isinstance(data, dict) else None

    def busy(self) -> bool:
        h = self.health()
        return h is not None and h.get("state") in TEST_RUNNING

    def wait_answer(self, before: str | None = None) -> bool:
        """Until a node answers (with a boot ID other than ``before``), come_back_s."""
        end = time.monotonic() + self.come_back_s
        while time.monotonic() < end:
            h = self.health(timeout_s=1.0)
            if h and h.get("boot_id") != before:
                return True
            time.sleep(0.2)
        return False

    def wait_gone(self) -> bool:
        end = time.monotonic() + self.come_back_s
        while time.monotonic() < end:
            if self.health(timeout_s=1.0) is None:
                return True
            time.sleep(0.2)
        return False

    def restart(self) -> bool:
        """Restart and wait until a node with a new boot ID answers. A node that answers
        stops as it asks (graceful); a hung one is the watchdog's (SYS-009, hard)."""
        before = (self.health() or {}).get("boot_id")
        graceful = getattr(self.service, "graceful_restart", None)
        if before is not None and graceful is not None:
            graceful()
        else:
            self.service.restart()
        end = time.monotonic() + self.come_back_s
        while time.monotonic() < end:
            h = self.health(timeout_s=1.0)
            if h and h.get("boot_id") != before:
                return True
            time.sleep(0.2)
        return False
