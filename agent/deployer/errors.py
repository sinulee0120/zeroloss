"""The Deployer's own error (L0 uses no Core code, docs/12-architecture.md 1-1).

Same fields and envelope as Core's errors (docs/02-data.md 3): code, what, why, fix,
detail; codes come from docs/09-exceptions.md.
"""

from __future__ import annotations

from typing import Any


class DeployError(Exception):
    def __init__(
        self, code: str, what: str, why: str = "", fix: str = "", detail: str = ""
    ) -> None:
        super().__init__(f"{code}: {what}")
        self.code = code if code.startswith("ZL-") else f"ZL-{code}"
        self.what, self.why, self.fix, self.detail = what, why, fix, detail

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "what": self.what, "why": self.why, "fix": self.fix,
                "detail": self.detail}  # fmt: skip


def damaged(detail: str) -> DeployError:
    return DeployError(
        "INS-006",
        "Package damaged",
        "Its files do not match the package's hash list.",
        "Build or download the package again.",
        detail,
    )
