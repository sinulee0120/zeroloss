"""Packages to a node's Deployer (docs/06-install.md 11.5, 12; docs/02-data.md 15).

The Controller sends a package over 10991 and asks for the install, lists and removes
modules. It never does this during a test (INS-022). A Deployer that does not answer
is tried three more times, 1 s, 2 s and 4 s apart (09 0.3), then INS-014 (not a node
fault: the node may be fine). Upload and install are retried separately and the
install keeps one command ID, so a retry after a lost answer never installs twice.
A node package (.zlp, Linux) answers "started" at once: the install runs as its own job
on that PC, the Deployer itself restarts, and this waits on its health until the
update is done, went back, or failed (06 11.5 Linux 구현 규칙).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from controller.devices import DeviceList
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

FINAL = ("done", "rolled_back", "failed")  # 02 15 ``update.state`` (the Deployer's own list)

INSTALL_TIMEOUT_S = 180.0  # unpack + node restart (the Deployer waits up to 60 s for it)
RETRY_DELAYS_S = (1.0, 2.0, 4.0)  # INS-014 "retry 3 times", 09 0.3
UPDATE_WAIT_S = 900.0  # 06 11.5: a node update ends within 15 minutes
UPDATE_POLL_S = 2.0


class Deploy:
    def __init__(
        self, devices: DeviceList, boot_id: str, generation: int = 0,
        update_wait_s: float = UPDATE_WAIT_S, poll_s: float = UPDATE_POLL_S,
    ) -> None:  # fmt: skip
        self.devices = devices
        self.boot_id = boot_id
        self.generation = generation
        self.update_wait_s, self.poll_s = update_wait_s, poll_s

    def _client(self, node: str, host: str | None = None) -> NodeClient:
        dev = self.devices.get(node)
        return NodeClient(host or dev.host, dev.deployer_port, self.boot_id,
                          f"{dev.name} Deployer")  # fmt: skip

    def hosts(self, node: str) -> list[str]:
        """Where that PC's Deployer may answer: the address in use, then its cards as
        last seen (2026-10-17: the ProBook's node was off, its Deployer answered on its
        other card)."""
        dev = self.devices.get(node)
        cards = [str(c.get("ip")) for c in self.devices.cards.get(node) or [] if c.get("ip")]
        others = [h for h in cards if not h.startswith("127.")]  # a card's loopback is not it
        return [h for h in dict.fromkeys([dev.host, *others]) if h]

    def health(self, node: str) -> dict[str, Any]:
        return self._call(node, lambda c: c.get("/v1/deployer/health"))

    def install(self, node: str, package: Path) -> dict[str, Any]:
        if not package.is_file():
            raise ZlError("DSC-012", "Request rejected", detail=f"no package file {package}")

        got = self._call(node, lambda c: c.upload(f"/v1/deployer/incoming/{package.name}", package))
        cid = str(uuid.uuid4())
        r = self._call(
            node,
            lambda c: c.command(
                "/v1/deployer/install", self.generation, command_id=cid,
                timeout_s=INSTALL_TIMEOUT_S, package=got["name"],
            ),
        )  # fmt: skip
        if r.get("kind") == "node" and r.get("result") == "started":
            return self._wait_update(node, r)
        return r

    def _wait_update(self, node: str, started: dict[str, Any]) -> dict[str, Any]:
        """Until the Deployer's ``update`` record with this id is final. The Deployer is
        away while the job replaces it, so no answer is not an error here."""
        uid = started["update_id"]
        end = time.monotonic() + self.update_wait_s
        up: dict[str, Any] | None = None
        while time.monotonic() < end:
            try:
                up = self._client_health(node).get("update")
            except ZlError as e:
                if e.code not in ("ZL-DSC-014", "ZL-INS-014"):
                    raise
            if isinstance(up, dict) and up.get("id") == uid and up.get("state") in FINAL:
                break
            time.sleep(self.poll_s)
        else:
            raise ZlError(
                "INS-013", f"The update on {node} did not finish",
                f"No final state in {self.update_wait_s / 60:g} minutes"
                + (f" (last: {up.get('state')})" if isinstance(up, dict) else ""),
                "Look at the PC; if the node does not answer, install again over SSH.",
            )  # fmt: skip
        if up["state"] == "rolled_back":
            raise ZlError(
                "INS-028", f"Update failed on {node}", str(up.get("why") or ""),
                f"It is back on {up['from_build']}. Fix the cause, then update again.",
            )  # fmt: skip
        if up["state"] == "failed":
            raise ZlError(
                "INS-013", f"Update failed on {node} and could not go back",
                str(up.get("why") or ""),
                "Install again over SSH (zeroloss install linux ... --yes).",
            )  # fmt: skip
        return {"operation": "install", "kind": "node", "id": "agent", "build": up["to_build"],
                "from_build": up["from_build"], "result": "updated", "update": up}  # fmt: skip

    def _client_health(self, node: str) -> dict[str, Any]:
        client = self._client(node)
        try:
            result: dict[str, Any] = client.get("/v1/deployer/health", timeout_s=5.0)
            return result
        finally:
            client.close()

    def modules(self, node: str) -> dict[str, Any]:
        return self._call(node, lambda c: c.get("/v1/deployer/modules"))

    def remove(self, node: str, module_id: str, purge: bool, dry_run: bool) -> dict[str, Any]:
        cid = str(uuid.uuid4())
        return self._call(
            node,
            lambda c: c.command(
                "/v1/deployer/remove", self.generation, command_id=cid,
                timeout_s=INSTALL_TIMEOUT_S, kind="module", id=module_id, purge=purge,
                dry_run=dry_run,
            ),
        )  # fmt: skip

    def node(self, node: str, action: str) -> dict[str, Any]:
        """F11 fault screen: restart / stop / start the node service on that PC (02 15)."""
        cid = str(uuid.uuid4())
        return self._call(
            node,
            lambda c: c.command(
                "/v1/deployer/node", self.generation, command_id=cid,
                timeout_s=INSTALL_TIMEOUT_S, action=action,
            ),
        )  # fmt: skip

    def _call(self, node: str, fn: Any) -> dict[str, Any]:
        last: ZlError | None = None
        for delay in (*RETRY_DELAYS_S, None):
            for host in self.hosts(node):
                client = self._client(node, host)
                try:
                    result: dict[str, Any] = fn(client)
                    return result
                except ZlError as e:
                    if e.code != "ZL-DSC-014":
                        raise
                    last = e
                finally:
                    client.close()
            if delay is not None:
                time.sleep(delay)
        if last is None:
            what = f"The Deployer on {node} does not answer"
            raise ZlError("INS-014", what, "No address is known for that PC.", "Find Devices.")
        raise ZlError(
            "INS-014", f"The Deployer on {node} does not answer", last.why,
            "Windows: install the node again by hand. Linux: install again over SSH.",
            detail=f"{last.detail}; tried {len(RETRY_DELAYS_S) + 1} times",
        )  # fmt: skip
