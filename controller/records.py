"""1-second record retrieval (docs/02-data.md 2-13 ``records``, 03 F15, 20 GAP-80).

One job: copy one run's 1-second record files (and channel change files, 7-2) from the
nodes into
``Reports/<run>/raw/<device>/``. A file already there with the same size is not fetched
again; each file is written as ``.part`` and renamed when complete. A node whose files
all came is told so (``/v1/records/fetched``) and removes its copy at its next start or
test (2026-10-31 user: "메모리 쌓이지 않게"). A node that does not answer is written
down, not a failure of the rest.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Any

from controller.devices import Device
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError
from core.records.recorder import DEVICE_NAME, record_run

FILE_TIMEOUT_S = 600.0  # one hour of one device (02 2-13)
UNREACHABLE = "ZL-DSC-014"


def _safe(device: str, name: str, run: str) -> bool:
    """Names from a node are checked before they become a path here."""
    got = record_run(name)
    ok_device = bool(DEVICE_NAME.match(device)) and set(device) != {"."}
    return ok_device and got is not None and got[0] == run


def collect(
    reports_dir: Path,
    run: str,
    devices: list[Device],
    boot_id: str,
    stopping: threading.Event | None = None,
    generation: int = 0,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    missing: dict[str, str] = {}
    root = reports_dir / run / "raw"
    for dev in devices:
        client = NodeClient(dev.host, dev.port, boot_id, dev.name)
        try:
            index = client.get("/v1/records/index", run=run)
            for f in index.get("files", []):
                if stopping is not None and stopping.is_set():
                    missing.setdefault(dev.name, "stopped: the Controller is closing")
                    break
                device, name, size = str(f.get("device")), str(f.get("name")), f.get("bytes")
                if not _safe(device, name, run):
                    missing.setdefault(dev.name, f"ZL-DSC-012 bad file name {device}/{name}")
                    continue
                dest = root / device / name
                row = {"device": device, "hour": f.get("hour"), "bytes": size}
                if dest.is_file() and dest.stat().st_size == size:
                    files.append({**row, "state": "had"})
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    client.download("/v1/records", dest, timeout_s=FILE_TIMEOUT_S, run=run,
                                    device=device, name=name)  # fmt: skip
                except ZlError as e:
                    missing.setdefault(dev.name, f"{e.code} {e.what}")
                    if e.code == UNREACHABLE:
                        break  # the node is gone: the rest of its files too
                    continue
                files.append({**row, "bytes": dest.stat().st_size, "state": "got"})
            if dev.name not in missing:  # all here: the node may remove its copy (2026-10-31)
                with contextlib.suppress(ZlError):  # an older node keeps it
                    client.command("/v1/records/fetched", generation, None, run=run)
        except ZlError as e:
            missing.setdefault(dev.name, f"{e.code} {e.what}")
        finally:
            client.close()
    return {"run": run, "files": files, "missing": missing}
