"""Device list (docs/02-data.md 11-0): names in plans resolve here.

P2 fills it by hand (``zeroloss devices add``); P3 discovery fills the same list.
A device is added only after its node answers ``/v1/health``; ``device_id`` keeps the
same device recognisable when its IP changes.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from core.events.bus import utc_now
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

SCHEMA_VERSION = 1
DEPLOYER_PORT = 10991
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    port: int
    device_id: str
    role: str
    added_utc: str
    # Highest wired link speed seen per NIC IP (02 11-0): the "usual" speed that a
    # lower link is compared with (NET-014, 05 1.8)
    best_link_mbps: dict[str, int] = field(default_factory=dict)
    deployer_port: int = DEPLOYER_PORT  # 02 11-0 / 15 (older files without it: 10991)


class DeviceList:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._devices: dict[str, Device] = {}
        self._lock = threading.RLock()  # discovery (a thread) and requests both write
        self.unreadable: str | None = None  # SYS-011: what was kept aside, and why
        self.cards: dict[str, list[dict[str, Any]]] = {}  # last cards seen per device
        # 2026-10-17: kept on disk too -- after a restart the ProBook (its node off) was
        # known only at 10.0.0.1 and Reset All could not reach its Deployer at its
        # other address (192.168.0.27)
        self._cards_path = path.with_name("cards.json")
        try:
            got = json.loads(self._cards_path.read_text(encoding="utf-8"))
            self.cards = {str(k): [dict(x) for x in v if isinstance(x, dict)]
                          for k, v in got.items() if isinstance(v, list)}  # fmt: skip
        except (OSError, ValueError, AttributeError):
            self.cards = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                version = data.get("schema_version")
                devices = [Device(**d) for d in data.get("devices", [])]
            except (ValueError, TypeError, AttributeError) as e:
                # a broken file must not keep the Controller from starting (GAP-117)
                kept = path.with_name(f"{path.name}.unreadable-{utc_now()[:19].replace(':', '')}")
                path.replace(kept)
                self.unreadable = f"{path.name} could not be read ({e}); kept as {kept.name}"
                return
            if version != SCHEMA_VERSION:
                raise ZlError(
                    "INS-025",
                    f"{path.name} format {version} cannot be used",
                    detail=str(path),
                )
            for dev in devices:
                self._devices[dev.name] = dev

    def all(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: d.name)

    def get(self, name_or_id: str) -> Device:
        for d in self._devices.values():
            if name_or_id in (d.name, d.device_id):
                return d
        raise ZlError(
            "DSC-001",
            f"No device named {name_or_id}",
            "It is not in the device list.",
            "Add it with 'zeroloss devices add <name> <IP>'.",
        )

    def add(
        self, name: str, host: str, port: int, boot_id: str, deployer_port: int = DEPLOYER_PORT
    ) -> Device:
        """Ask the node who it is, then save. Replaces an entry with the same device_id."""
        if not NAME.match(name):
            raise ZlError(
                "DSC-012", f"Device name {name!r} is not allowed", "Use letters, digits, - _ ."
            )
        client = NodeClient(host, port, boot_id, name)
        try:
            health = client.get("/v1/health")
        finally:
            client.close()
        dev = Device(
            name=name,
            host=host,
            port=port,
            device_id=str(health["device_id"]),
            role=str(health.get("role", "")),
            added_utc=utc_now(),
            deployer_port=deployer_port,
        )
        # GAP-155: a name keeps its PC — adding "t460-rx" at the sender's address must not
        # turn the t460-rx entry into the sender (renaming the same PC is fine)
        for other in self._devices.values():
            if other.name == name and other.device_id != dev.device_id:
                raise ZlError("DSC-016", f"Another PC answers at {host}, not {name}",
                              f"{name} is a different PC (device ID {other.device_id[:8]}).",
                              f"Use the address {name} shows under My IP, or remove {name} "
                              "first to add this PC.")  # fmt: skip
        for other in list(self._devices.values()):
            if other.device_id == dev.device_id or other.name == name:
                if other.device_id == dev.device_id:  # the same PC at a new address (GAP-153)
                    dev = replace(dev, best_link_mbps=dict(other.best_link_mbps))
                del self._devices[other.name]
        self._devices[name] = dev
        self._save()
        return dev

    def move(self, device_id: str, host: str, port: int) -> tuple[Device, str] | None:
        """06 10.2 (GAP-73 ④): the same device answered at another address (e.g. the
        sender PC moved from the router LAN to its WAN port) -> keep the new address.
        Returns the updated device and its old address, or None when nothing changed."""
        with self._lock:
            dev = next((d for d in self._devices.values() if d.device_id == device_id), None)
            if dev is None or (dev.host, dev.port) == (host, port):
                return None
            self._devices[dev.name] = replace(dev, host=host, port=port)
            self._save()
            return self._devices[dev.name], f"{dev.host}:{dev.port}"

    def remove(self, name: str) -> None:
        dev = self.get(name)
        del self._devices[dev.name]
        self._save()

    def note_links(self, name: str, links: list[dict[str, Any]]) -> bool:
        """Remember each NIC's highest link speed; saves only when it changes. The cards
        themselves are kept in memory: the screen's Card list between tests (GAP-121)."""
        dev = self._devices.get(name)
        if dev is None:
            return False
        cards = [dict(x) for x in links if isinstance(x, dict)]
        ips = [(c.get("nic"), c.get("ip")) for c in cards]
        if ips != [(c.get("nic"), c.get("ip")) for c in self.cards.get(name) or []]:
            self.cards[name] = cards
            self._save_cards()  # the addresses changed: keep them for the next start
        self.cards[name] = cards
        changed = False
        for x in links:
            ip, mbps = x.get("ip"), x.get("link_mbps")
            if (
                isinstance(ip, str)
                and isinstance(mbps, int)
                and mbps > dev.best_link_mbps.get(ip, 0)
            ):
                dev.best_link_mbps[ip] = mbps
                changed = True
        if changed:
            self._save()
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "devices": [asdict(d) for d in self.all()]}

    def _save_cards(self) -> None:
        try:
            tmp = self._cards_path.with_name(self._cards_path.name + ".tmp")
            tmp.write_text(json.dumps(self.cards, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._cards_path)
        except OSError:
            pass  # the screen still works from memory

    def _save(self) -> None:
        with self._lock:
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)  # never a half-written file (02-data 11)
