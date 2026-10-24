"""A small PDF writer with the standard library only (03 F16 implementation rules, 20
GAP-86): text in the built-in Helvetica fonts and filled rectangles, A4 pages, enough
for ``report.pdf``. No external tools, so the Controller service stays dependency-free.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

A4 = (595.0, 842.0)  # points
MARGIN = 42.0
# Helvetica widths (1/1000 em) for the printable ASCII range, for line wrapping
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
      556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
      1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
      667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
      333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
      556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]  # fmt: skip
ASCII = {"\u2014": "-", "\u2013": "-", "\u2192": "->", "\u2265": ">=", "\u2264": "<=",
         "\u00b1": "+-", "\u2026": "...", "\u2714": "OK", "\u2716": "X", "\u26a0": "!",
         "\u25c6": "*", "\u25cb": "o", "\u2212": "-", "\u00d7": "x"}  # fmt: skip


def plain(text: str) -> str:
    """Text the built-in fonts can show (WinAnsi, latin-1); others become close ASCII."""
    out = "".join(ASCII.get(c, c) for c in str(text))
    return out.encode("latin-1", "replace").decode("latin-1")


def width(text: str, size: float) -> float:
    return sum(_W[ord(c) - 32] if 32 <= ord(c) < 127 else 556 for c in plain(text)) * size / 1000


def wrap(text: str, size: float, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in plain(text).split("\n"):
        line = ""
        for word in para.split(" "):
            test = f"{line} {word}" if line else word
            if width(test, size) <= max_w or not line:
                line = test
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _esc(text: str) -> str:
    return plain(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _rgb(color: str) -> str:
    c = color.lstrip("#")
    return " ".join(f"{int(c[i : i + 2], 16) / 255:.3f}" for i in (0, 2, 4))


@dataclass
class Pdf:
    """Pages of drawing commands; ``y`` counts from the top of the page."""

    pages: list[list[str]] = field(default_factory=lambda: [[]])
    y: float = MARGIN

    @property
    def usable(self) -> float:
        return A4[0] - 2 * MARGIN

    def new_page(self) -> None:
        self.pages.append([])
        self.y = MARGIN

    def room(self, h: float) -> None:
        if self.y + h > A4[1] - MARGIN:
            self.new_page()

    def text(self, x: float, y: float, s: str, size: float = 10, bold: bool = False,
             color: str = "#1b1b1b") -> None:  # fmt: skip
        font = "F2" if bold else "F1"
        self.pages[-1].append(f"BT {_rgb(color)} rg /{font} {size} Tf {x:.2f} "
                              f"{A4[1] - y:.2f} Td ({_esc(s)}) Tj ET")  # fmt: skip

    def rect(self, x: float, y: float, w: float, h: float, color: str) -> None:
        self.pages[-1].append(f"{_rgb(color)} rg {x:.2f} {A4[1] - y - h:.2f} {w:.2f} {h:.2f} re f")

    def para(self, s: str, size: float = 10, bold: bool = False, color: str = "#1b1b1b",
             indent: float = 0.0) -> None:  # fmt: skip
        """Wrapped lines at the cursor, moving it down; a new page when full."""
        for line in wrap(s, size, self.usable - indent):
            self.room(size * 1.4)
            self.text(MARGIN + indent, self.y + size, line, size, bold, color)
            self.y += size * 1.4

    def gap(self, h: float) -> None:
        self.y += h

    def save(self) -> bytes:
        objs: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"",  # pages, filled below
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>",
        ]
        kids = []
        for cmds in self.pages:
            data = zlib.compress("\n".join(cmds).encode("latin-1"))
            objs.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data)
                        + data + b"\nendstream")  # fmt: skip
            content = len(objs)
            objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
                        b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
                        % (A4[0], A4[1], content))  # fmt: skip
            kids.append(len(objs))
        objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
            b" ".join(b"%d 0 R" % k for k in kids), len(kids))  # fmt: skip
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for n, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objs) + 1, xref)  # fmt: skip
        return bytes(out)
