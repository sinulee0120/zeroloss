"""12 1장 ``media_import`` (20 GAP-109): the Deployer brings what a module asks for from
the desktop users' folders — a link that never changes the person's file, a copy only
with room, follows changes and removals, never touches a person's own file, removes
exactly what it brought. Runs on a temporary root, never on real home folders."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.media_import import RECORD, MediaImport, asks
from modules.iptv.sender.sources import VIDEO_EXT

ASK = {"from": "Videos", "into": "media", "ext": ["mp4", "mkv", "ts"]}


def setup_root(tmp: Path, ask: dict[str, Any] = ASK) -> tuple[Path, Path, Path]:
    root, data, mods = tmp / "root", tmp / "root" / "var/lib/zeroloss", tmp / "mods"
    (root / "home" / "test" / "Videos").mkdir(parents=True)
    data.mkdir(parents=True)
    (mods / "iptv").mkdir(parents=True)
    (mods / "iptv" / "module.json").write_text(json.dumps({"id": "iptv", "media_import": ask}))
    return root, data, mods


def importer(root: Path, data: Path, mods: Path, **kw: Any) -> tuple[MediaImport, list[Any]]:
    owned: list[Any] = []
    logs: list[Any] = []
    m = MediaImport(root, data, mods, owned.append, lambda lv, t: logs.append((lv, t)), **kw)
    m.owned, m.logs = owned, logs  # type: ignore[attr-defined]
    return m, logs


def test_iptv_declares_its_videos_with_its_own_extensions() -> None:
    repo = Path(__file__).resolve().parents[2]
    got = [a for a in asks(repo / "modules") if a.module == "iptv"]
    assert got and got[0].src == "Videos" and got[0].into == "media"
    assert {"." + e for e in got[0].ext} == set(VIDEO_EXT)  # the S-48 list (iptv 1-1)


def test_a_readable_movie_is_linked_and_the_persons_file_is_untouched(tmp_path: Path) -> None:
    root, data, mods = setup_root(tmp_path)
    movie = root / "home/test/Videos/movie.mp4"
    movie.write_bytes(b"x" * 1000)
    (root / "home/test/Videos/notes.txt").write_text("not a video")
    (root / "home/test/Videos/.hidden.mp4").write_bytes(b"h")
    before = movie.stat()
    m, _ = importer(root, data, mods)
    assert m.run() == ["link media/movie.mp4"]
    dst = data / "media" / "movie.mp4"
    assert os.path.samefile(dst, movie)  # a link: no copy, no extra space
    after = movie.stat()
    assert (after.st_mode, after.st_uid) == (before.st_mode, before.st_uid)
    assert dst not in m.owned  # type: ignore[attr-defined]  # a link is never chowned
    assert sorted(p.name for p in (data / "media").iterdir()) == [RECORD, "movie.mp4"]
    assert m.run() == []  # nothing changed: nothing to do


def test_changed_and_gone_originals_are_followed(tmp_path: Path) -> None:
    root, data, mods = setup_root(tmp_path)
    movie = root / "home/test/Videos/movie.mp4"
    movie.write_bytes(b"x" * 1000)
    m, _ = importer(root, data, mods)
    m.run()
    movie.unlink()  # a new file with the same name
    movie.write_bytes(b"y" * 2000)
    os.utime(movie, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert m.run() == ["link media/movie.mp4"]
    assert (data / "media" / "movie.mp4").read_bytes() == b"y" * 2000
    movie.unlink()
    assert m.run() == ["removed media/movie.mp4"]
    assert not (data / "media" / "movie.mp4").exists()


def test_a_persons_own_file_in_media_is_never_touched(tmp_path: Path) -> None:
    root, data, mods = setup_root(tmp_path)
    (data / "media").mkdir()
    mine = data / "media" / "movie.mp4"
    mine.write_bytes(b"mine")
    (root / "home/test/Videos/movie.mp4").write_bytes(b"theirs")
    m, _ = importer(root, data, mods)
    assert m.run() == []
    assert mine.read_bytes() == b"mine"
    assert m.remove() == [] and mine.read_bytes() == b"mine"


def test_no_link_means_a_copy_only_with_room(tmp_path: Path, monkeypatch: Any) -> None:
    root, data, mods = setup_root(tmp_path)
    (root / "home/test/Videos/movie.ts").write_bytes(b"z" * 500)

    def no_link(a: Any, b: Any) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_link)
    m, logs = importer(root, data, mods, free=lambda p: 100)
    assert m.run() == [] and any("no room" in t for _, t in logs)
    m, _ = importer(root, data, mods, free=lambda p: 10**12)
    assert m.run() == ["copy media/movie.ts"]
    dst = data / "media" / "movie.ts"
    assert dst.read_bytes() == b"z" * 500 and not os.path.samefile(
        dst, root / "home/test/Videos/movie.ts"
    )
    # a copy belongs to the node account before it gets its name (never seen half-owned)
    assert dst.with_name(".movie.ts.part") in m.owned  # type: ignore[attr-defined]


def test_removal_takes_exactly_what_was_brought(tmp_path: Path) -> None:
    root, data, mods = setup_root(tmp_path)
    (root / "home/test/Videos/a.mp4").write_bytes(b"a")
    (root / "home/test/Videos/b.mkv").write_bytes(b"b")
    m, _ = importer(root, data, mods)
    m.run()
    (data / "media" / "mine.mp4").write_bytes(b"mine")
    assert m.remove("other-module") == [] and (data / "media" / "a.mp4").exists()
    assert m.remove("iptv") == []
    assert sorted(p.name for p in (data / "media").iterdir()) == ["mine.mp4"]
    assert (root / "home/test/Videos/a.mp4").read_bytes() == b"a"  # the originals stay


@pytest.mark.parametrize(
    "ask",
    [
        {"from": "../etc", "into": "media", "ext": ["mp4"]},
        {"from": "Videos", "into": "/var", "ext": ["mp4"]},
        {"from": "Videos", "into": "media", "ext": []},
        {"from": "Videos", "into": "media", "ext": ["../x"]},
        "Videos",
    ],
)
def test_a_bad_declaration_is_skipped(tmp_path: Path, ask: Any) -> None:
    _, _, mods = setup_root(tmp_path, ask)
    assert asks(mods) == []


def test_the_deployer_imports_only_while_the_node_is_idle(tmp_path: Path) -> None:
    from agent.deployer.agent_link import AgentLink
    from agent.deployer.modules import Layout
    from agent.deployer.server import Deployer
    from tests.agent.fake_node import FakeNode

    root, data, mods = setup_root(tmp_path)
    (root / "home/test/Videos/movie.mp4").write_bytes(b"x")
    node = FakeNode()
    node.state = "RUNNING"
    d = Deployer(Layout(data, mods), AgentLink(node, node.port, come_back_s=5), "127.0.0.1", 0,
                 build="20261003.1824", watch_s=5.0, poll_s=0.1, media_root=root)  # fmt: skip
    d.start()
    try:
        time.sleep(0.5)
        assert not (data / "media" / "movie.mp4").exists()  # never during a test
        node.state = "IDLE"
        end = time.monotonic() + 5
        while time.monotonic() < end and not (data / "media" / "movie.mp4").exists():
            time.sleep(0.05)
        assert (data / "media" / "movie.mp4").exists()
    finally:
        d.stop()
        node.close()
