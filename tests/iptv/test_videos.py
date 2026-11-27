"""The sender PC's video folder (iptv 1-1, S-70, 20 GAP-195)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import modules.iptv.sender.videos as vf
from core.protocol.envelope import ZlError
from modules.iptv.sender.videos import VideoFolder, default_folder

REAL_DEFAULT = default_folder  # conftest puts every node's default at <data>/media


def made(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"ts")


def test_the_default_place_and_the_made_videos_moved_out_of_the_cache(tmp_path: Path) -> None:
    """2026-11-27 user ("35mbs파일 위치는 고정이야? 새로 설치하면 사용자가 못바꿔?"): the made
    videos were in the module cache (gone with a module removal, hours to make again)."""
    old = tmp_path / "data" / "modules" / "iptv" / "cache" / "generated"
    made(old, "ch1_UHD_35000000.ts", "ch1_UHD_35000000.json")
    v = VideoFolder(tmp_path / "data", tmp_path / "settings", old, platform="linux")
    assert v.folder == tmp_path / "data" / "media"  # Ubuntu: /var/lib/zeroloss/media as before
    assert sorted(p.name for p in v.generated.iterdir()) == [
        "ch1_UHD_35000000.json", "ch1_UHD_35000000.ts"]  # fmt: skip
    assert not any(old.iterdir())


def test_windows_uses_the_spec_folder_and_brings_the_movies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec said C:\\ZeroLoss\\Media; the code used <data>\\media."""
    monkeypatch.setattr(vf, "default_folder", REAL_DEFAULT)
    monkeypatch.setattr(vf, "WINDOWS_DEFAULT", tmp_path / "ZeroLoss" / "Media")
    made(tmp_path / "data" / "media", "movie.mp4")
    v = VideoFolder(tmp_path / "data", tmp_path / "settings", tmp_path / "none", platform="win32")
    assert v.folder == tmp_path / "ZeroLoss" / "Media" and (v.folder / "movie.mp4").is_file()


def test_s70_moves_the_made_videos_and_is_remembered(tmp_path: Path) -> None:
    v = VideoFolder(tmp_path / "data", tmp_path / "settings", tmp_path / "none", platform="linux")
    made(v.generated, "ch2_HD_10000000.ts")
    big = tmp_path / "big disk" / "zeroloss"
    got = v.set(str(big))
    assert got["folder"] == str(big) and got["moved"] == 1 and got["left"] == 0
    assert (big / "generated" / "ch2_HD_10000000.ts").is_file()
    saved = json.loads((tmp_path / "settings" / "videos.json").read_text(encoding="utf-8"))
    assert saved == {"folder": str(big)}
    again = VideoFolder(
        tmp_path / "data", tmp_path / "settings", tmp_path / "none", platform="linux"
    )
    assert again.folder == big  # the next start
    assert v.set("")["folder"] == str(tmp_path / "data" / "media")  # empty: the default
    with pytest.raises(ZlError) as e:
        v.set("relative/folder")
    assert e.value.code == "ZL-RUN-014" and "full path" in e.value.what


def test_a_folder_it_cannot_write_is_refused(tmp_path: Path) -> None:
    v = VideoFolder(tmp_path / "data", tmp_path / "settings", tmp_path / "none", platform="linux")
    blocker = tmp_path / "a file"
    blocker.write_bytes(b"")  # a folder cannot be made where a file is
    with pytest.raises(ZlError) as e:
        v.set(str(blocker / "videos"))
    assert e.value.code == "ZL-RUN-014" and "cannot write" in e.value.what
    assert v.folder == tmp_path / "data" / "media"  # unchanged


def test_a_saved_folder_that_is_gone_falls_back_to_the_default(tmp_path: Path) -> None:
    (tmp_path / "settings").mkdir()
    gone = tmp_path / "a file"
    gone.write_bytes(b"")
    (tmp_path / "settings" / "videos.json").write_text(json.dumps({"folder": str(gone / "x")}))
    v = VideoFolder(tmp_path / "data", tmp_path / "settings", tmp_path / "none", platform="linux")
    assert v.folder == tmp_path / "data" / "media"


def test_the_plan_sends_s70_to_the_sender() -> None:
    from modules.iptv.run import prepare_ahead, prepare_steps, start_steps, validate
    from tests.iptv.test_tablet_plan import plan, tab

    raw = plan(tab("tablet-1", 1, "A1"), {"device": "rx-pc", "ch": 1})
    raw["settings"] = {**raw.get("settings", {}), "S-70": " /data/zeroloss "}
    p = validate(raw)
    assert p.videos == "/data/zeroloss"
    senders, _ = start_steps(p)
    assert all(s.params["videos"] == "/data/zeroloss" for s in senders)
    assert prepare_steps(p)[0].params["videos"] == "/data/zeroloss"
    step = prepare_ahead({"quality": "HD", "sender": "tx", "videos": "/data/zeroloss"})
    assert step.params["videos"] == "/data/zeroloss"
    assert "videos" not in prepare_ahead({"quality": "HD", "sender": "tx"}).params
