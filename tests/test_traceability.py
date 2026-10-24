"""docs/17-traceability.md must point at code and tests that really exist."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "docs" / "17-traceability.md"


def rows() -> list[list[str]]:
    out = []
    for line in TABLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and not line.startswith("|--") and not line.startswith("| 요구"):
            out.append([c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]])
    return out


def test_every_referenced_file_and_test_exists() -> None:
    problems = []
    for cells in rows():
        if len(cells) != 5:
            continue
        for ref in re.findall(r"`([^`]+)`", cells[3] + " " + cells[4]):
            path, _, func = ref.partition("::")
            if not (ROOT / path).is_file():
                problems.append(f"missing file {path}")
                continue
            if func:
                names = {
                    n.name
                    for n in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8")))
                    if isinstance(n, ast.FunctionDef)
                }
                if func not in names:
                    problems.append(f"missing test {ref}")
    assert not problems, problems


def test_done_rows_have_code_and_a_test() -> None:
    bad = [c[0] for c in rows() if len(c) == 5 and c[2].startswith("✅") and "::" not in c[4]]
    assert not bad, f"✅ rows need a test: {bad}"
