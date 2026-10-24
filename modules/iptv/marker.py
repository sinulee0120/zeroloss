"""Frame marker burnt into prepared videos (docs/modules/iptv.md 3.4, V12).

Bottom-left, one row of 22 square cells: white, black (start reference), channel
(2 bits), frame number (16 bits, most significant first), black, white (end reference).
1 = white, 0 = black. The frame number is the frame's index in the file (0 at the
start, again 0 where the file loops). Human-readable digits sit just above the row.
Reading never guesses: weak contrast or a wrong end reference gives None.
"""

from __future__ import annotations

from dataclasses import dataclass

CELLS = 22
CH_BITS, FRAME_BITS = 2, 16
FIRST_CH, FIRST_FRAME = 2, 4
CONTRAST_MIN = 64  # grey levels between the white and black reference cells
STRIP_CELL = 4  # 4.3 check: pixels per cell in the small marker strip ffmpeg makes
STRIP_W, STRIP_H = (CELLS + 2) * STRIP_CELL, 3 * STRIP_CELL


@dataclass(frozen=True)
class Marker:
    ch: int
    frame: int


def cell_px(height: int) -> int:
    """Cell size: an even number of pixels, about 1/72 of the height (UHD 30, HD 16)."""
    return 2 * round(height / 144)


def filter_graph(width: int, height: int, ch: int, title: str) -> str:
    """ffmpeg -filter_complex: [0:v] -> 30 fps, size, channel title, frame digits and the
    marker row -> [v]. ``geq`` computes each cell from the frame number N."""
    if not 0 <= ch < 1 << CH_BITS:
        raise ValueError("channel must fit in 2 bits")
    c = cell_px(height)
    i = f"trunc(X/{c})"
    lum = (
        f"255*if(eq({i},0),1,if(eq({i},1),0,"
        f"if(lt({i},{FIRST_FRAME}),mod(trunc({ch}/pow(2,{FIRST_FRAME - 1}-{i})),2),"
        f"if(lt({i},{FIRST_FRAME + FRAME_BITS}),"
        f"mod(trunc(N/pow(2,{FIRST_FRAME + FRAME_BITS - 1}-{i})),2),"
        f"if(eq({i},{CELLS - 2}),0,1)))))"
    )
    return (
        f"[0:v]fps=30,scale={width}:{height},"
        f"drawtext=text='{title}':fontsize={height // 15}:fontcolor=white"
        ":box=1:boxcolor=black@0.6:x=40:y=40,"
        f"drawtext=text='%{{frame_num}}':fontsize={2 * c}:fontcolor=white"
        f":box=1:boxcolor=black:x={c}:y={height - 5 * c}[v0];"
        f"color=c=black:s={CELLS * c}x{c}:r=30,format=gray,geq=lum='{lum}'[bar];"
        f"[v0][bar]overlay=x={c}:y={height - 2 * c}:shortest=1,format=yuv420p,"
        # 20 W-7 (GAP-77): best quality for the marker row, so an encoder never copies
        # it from a neighbour frame (a B-frame showed the next frame's number)
        f"addroi=x={c}:y={height - 2 * c}:w={CELLS * c}:h={c}:qoffset=-1[v]"
    )


def strip_filter(height: int) -> str:
    """ffmpeg ``-vf`` for the 4.3 check: the marker row of a ``height`` video, cut out
    and shrunk to ``STRIP_W`` x ``STRIP_H`` grey (every frame is read, cheaply)."""
    c = cell_px(height)
    return (
        f"crop={(CELLS + 2) * c}:{3 * c}:0:ih-{3 * c},"
        f"scale={STRIP_W}:{STRIP_H}:flags=area,format=gray"
    )


def read_strip(gray: bytes | memoryview) -> Marker | None:
    """Marker from one ``strip_filter`` picture (the row starts one cell in and down)."""
    if len(gray) < STRIP_W * STRIP_H:
        return None
    c = STRIP_CELL
    return _marker([_cell(gray, STRIP_W, STRIP_H, c * (1 + k), c, c) for k in range(CELLS)])


def read(
    gray: bytes | memoryview, width: int, height: int, source_height: int = 0
) -> Marker | None:
    """Marker from one 8-bit grey picture (``width`` x ``height``, row by row). A capture
    scaled from the video needs the video's height (``source_height``) for the cell size."""
    if len(gray) < width * height:
        return None
    scale = height / (source_height or height)
    c = cell_px(source_height or height) * scale
    if c < 3:  # below 3 px a cell centre cannot be told from its edges
        return None
    y0 = height - 2 * c
    return _marker([_cell(gray, width, height, c * (1 + k), y0, c) for k in range(CELLS)])


def _marker(values: list[float]) -> Marker | None:
    """Cell brightness -> marker; weak contrast or a wrong end reference is None."""
    white, black = values[0], values[1]
    if white - black < CONTRAST_MIN:
        return None
    mid = (white + black) / 2
    bits = [1 if v > mid else 0 for v in values]
    if bits[CELLS - 2 :] != [0, 1]:
        return None
    ch = _number(bits[FIRST_CH:FIRST_FRAME])
    frame = _number(bits[FIRST_FRAME : FIRST_FRAME + FRAME_BITS])
    return Marker(ch, frame)


def _cell(
    gray: bytes | memoryview, width: int, height: int, x0: float, y0: float, c: float
) -> float:
    """Mean of the middle half of one cell (edges blur in encoding and scaling)."""
    xs = range(int(x0 + c / 4), max(int(x0 + 3 * c / 4), int(x0 + c / 4) + 1))
    ys = range(int(y0 + c / 4), max(int(y0 + 3 * c / 4), int(y0 + c / 4) + 1))
    total = n = 0
    for y in ys:
        if not 0 <= y < height:
            continue
        row = y * width
        for x in xs:
            if 0 <= x < width:
                total += gray[row + x]
                n += 1
    return total / n if n else 0.0


def _number(bits: list[int]) -> int:
    value = 0
    for b in bits:
        value = value << 1 | b
    return value
