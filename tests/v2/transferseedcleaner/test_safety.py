"""Destructive cleanup checks using only local files and downloader snapshots."""

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SAFETY_PATH = Path(__file__).resolve().parents[3] / "plugins.v2" / "transferseedcleaner" / "safety.py"
SPEC = importlib.util.spec_from_file_location("transferseedcleaner_safety_tests", SAFETY_PATH)
assert SPEC and SPEC.loader
safety = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = safety
SPEC.loader.exec_module(safety)


def _candidate(tmp_path, names=("Season 1/E01.mkv",)):
    """Build a complete torrent whose videos have independent library copies."""
    content = tmp_path / "downloads" / "Show"
    library = tmp_path / "library"
    torrent = {
        "hash": "ABC123",
        "downloader": "qb",
        "progress": 100,
        "content_path": str(content),
        "save_path": str(content),
    }
    arguments = {
        "download_hash": "abc123",
        "downloader": "qb",
        "torrent": torrent,
        "all_torrents": [torrent],
        "torrent_files": [],
        "download_history": {"downloader": "qb", "download_hash": "abc123"},
        "download_files": [],
        "transfer_histories": [],
        "media_extensions": {".mkv", ".mp4"},
    }
    for index, name in enumerate(names, 1):
        source = content / name
        destination = library / name
        source.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"episode {index}".encode())
        destination.write_bytes(source.read_bytes())
        arguments["torrent_files"].append({"name": name, "selected": True, "priority": 1})
        arguments["download_files"].append({
            "filepath": name,
            "fullpath": str(source),
            "downloader": "qb",
            "state": 1,
        })
        arguments["transfer_histories"].append({
            "id": index,
            "src": str(source),
            "dest": str(destination),
            "downloader": "qb",
            "status": True,
            "mode": "copy",
        })
    return arguments


def _inspect(arguments):
    return safety.inspect_candidate(**arguments)


@pytest.mark.parametrize("mode", ["copy", "link"])
def test_all_files_safely_organized_are_ready(tmp_path, mode):
    arguments = _candidate(tmp_path, names=("Season 1/E01.mkv", "Season 1/E02.mkv"))
    for history in arguments["transfer_histories"]:
        history["mode"] = mode
        if mode == "link":
            destination = Path(history["dest"])
            destination.unlink()
            os.link(history["src"], destination)

    result = _inspect(arguments)

    assert result.ready, result.reason


def test_multifile_v2_download_records_use_content_directory_as_savepath(tmp_path):
    arguments = _candidate(tmp_path, names=("E01.mkv", "E02.mkv"))
    content = Path(arguments["torrent"]["content_path"])
    arguments["torrent"]["save_path"] = str(content.parent)
    for torrent_file, download_file in zip(
        arguments["torrent_files"], arguments["download_files"]
    ):
        filename = download_file["filepath"]
        torrent_file["name"] = f"Show/{filename}"
        download_file["savepath"] = str(content)
        assert download_file["fullpath"] == str(content / filename)

    result = _inspect(arguments)

    assert result.ready, result.reason


def test_first_completed_episode_does_not_clear_multifile_torrent(tmp_path):
    arguments = _candidate(tmp_path, names=("Season 1/E01.mkv", "Season 1/E02.mkv"))
    arguments["transfer_histories"].pop()

    result = _inspect(arguments)

    assert not result.ready
    assert "没有整理记录" in result.reason


def test_latest_failed_transfer_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    failed = {**arguments["transfer_histories"][0], "id": 2, "status": False}
    arguments["transfer_histories"].append(failed)

    result = _inspect(arguments)

    assert not result.ready
    assert "失败" in result.reason


def test_library_symlink_to_download_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    history = arguments["transfer_histories"][0]
    destination = Path(history["dest"])
    destination.unlink()
    destination.symlink_to(history["src"])

    result = _inspect(arguments)

    assert not result.ready
    assert "软链接" in result.reason


def test_missing_library_target_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    Path(arguments["transfer_histories"][0]["dest"]).unlink()

    result = _inspect(arguments)

    assert not result.ready
    assert "整理目标缺失" in result.reason


@pytest.mark.parametrize("progress", [99.9, 0, "unknown"])
def test_unfinished_or_unknown_download_progress_blocks_cleanup(tmp_path, progress):
    arguments = _candidate(tmp_path)
    arguments["torrent"]["progress"] = progress

    result = _inspect(arguments)

    assert not result.ready
    assert "进度" in result.reason or "尚未完成下载" in result.reason


@pytest.mark.parametrize("other_path", ["same", "parent", "child"])
def test_shared_directory_with_separate_files_allows_cleanup(tmp_path, other_path):
    arguments = _candidate(tmp_path)
    content = Path(arguments["torrent"]["content_path"])
    paths = {"same": content, "parent": content.parent, "child": content / "other"}
    root = paths[other_path]
    other_file = root / "OtherShow.mkv"
    other_file.parent.mkdir(parents=True, exist_ok=True)
    other_file.write_bytes(b"another torrent's video")
    arguments["all_torrents"].append({
        "hash": "OTHER",
        "downloader": "qb",
        "content_path": str(root),
        "save_path": str(root),
    })
    lookup = Mock(return_value=SimpleNamespace(data=[{"name": other_file.name}]))
    arguments["torrent_file_lookup"] = lookup

    result = _inspect(arguments)

    assert result.ready, result.reason
    lookup.assert_called_once_with(tid="other", downloader="qb")
    assert other_file.read_bytes() == b"another torrent's video"


@pytest.mark.parametrize("other_path", ["same", "parent", "child", "symlink"])
def test_shared_file_blocks_cleanup_and_identifies_conflicting_torrent(tmp_path, other_path):
    arguments = _candidate(tmp_path)
    content = Path(arguments["torrent"]["content_path"])
    source = Path(arguments["download_files"][0]["fullpath"])
    alias = tmp_path / "downloads-alias"
    alias.symlink_to(content, target_is_directory=True)
    paths = {"same": content, "parent": content.parent, "child": source.parent, "symlink": alias}
    root = paths[other_path]
    other_file = alias / source.relative_to(content) if other_path == "symlink" else source
    arguments["all_torrents"].append({
        "hash": "OTHER", "title": "Other.Show.Full.Torrent.Name", "downloader": "qb",
        "content_path": str(root), "save_path": str(root),
    })
    arguments["torrent_file_lookup"] = Mock(return_value=[{
        "name": str(other_file.relative_to(root)), "selected": False, "priority": 0,
    }])

    result = _inspect(arguments)

    assert not result.ready
    assert "共用下载数据" in result.reason
    assert "Other.Show.Full.Torrent.Name" in result.reason
    assert str(source) in result.reason
    assert str(other_file) in result.reason


def test_shared_inode_in_same_directory_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    source = Path(arguments["download_files"][0]["fullpath"])
    other_file = source.with_name("OtherName.mkv")
    os.link(source, other_file)
    arguments["all_torrents"].append({
        "hash": "OTHER", "downloader": "qb", "title": "Other torrent",
        "content_path": str(source.parent), "save_path": str(source.parent),
    })
    arguments["torrent_file_lookup"] = Mock(return_value=[{"name": other_file.name}])

    result = _inspect(arguments)

    assert not result.ready
    assert "共用下载数据" in result.reason
    assert str(other_file) in result.reason


@pytest.mark.parametrize("failure", [
    "no_lookup", "none", "empty", "exception", "escape", "outside_content", "duplicate",
])
def test_uncertain_shared_directory_file_inventory_blocks_cleanup(tmp_path, failure):
    arguments = _candidate(tmp_path)
    content = Path(arguments["torrent"]["content_path"])
    arguments["all_torrents"].append({
        "hash": "OTHER", "downloader": "qb", "title": "Unverified torrent",
        "content_path": str(content), "save_path": str(content.parent),
    })
    lookup = Mock(return_value=[{"name": "Show/Other.mkv"}])
    arguments["torrent_file_lookup"] = lookup
    if failure == "no_lookup":
        arguments.pop("torrent_file_lookup")
    elif failure == "exception":
        lookup.side_effect = RuntimeError("downloader unavailable")
    else:
        lookup.return_value = {
            "none": None,
            "empty": [],
            "escape": [{"name": "../Other.mkv"}],
            "outside_content": [{"name": "Elsewhere/Other.mkv"}],
            "duplicate": [{"name": "Show/Other.mkv"}, {"name": "Show/Other.mkv"}],
        }[failure]

    result = _inspect(arguments)

    assert not result.ready
    assert "无法核对共享目录种子的文件清单" in result.reason
    assert "Unverified torrent" in result.reason


def test_separate_content_paths_do_not_require_other_file_queries(tmp_path):
    arguments = _candidate(tmp_path)
    arguments["all_torrents"].append({
        "hash": "OTHER", "downloader": "qb", "content_path": str(tmp_path / "other"),
    })
    lookup = Mock(side_effect=AssertionError("unnecessary downloader request"))
    arguments["torrent_file_lookup"] = lookup

    result = _inspect(arguments)

    assert result.ready, result.reason
    lookup.assert_not_called()


def test_torrent_file_count_disagrees_with_download_records(tmp_path):
    arguments = _candidate(tmp_path)
    arguments["torrent_files"].append({"name": "Season 1/E02.mkv", "selected": True})

    result = _inspect(arguments)

    assert not result.ready
    assert "数量不一致" in result.reason


@pytest.mark.parametrize("uncertain", ["no_inventory", "no_snapshot", "unknown_other_path"])
def test_uncertain_downloader_inventory_blocks_cleanup(tmp_path, uncertain):
    arguments = _candidate(tmp_path)
    if uncertain == "no_inventory":
        arguments["torrent_files"] = []
    elif uncertain == "no_snapshot":
        arguments["all_torrents"] = []
    else:
        arguments["all_torrents"].append({"hash": "OTHER", "downloader": "qb"})

    result = _inspect(arguments)

    assert not result.ready


def test_duplicate_basenames_without_relative_paths_are_ambiguous(tmp_path):
    arguments = _candidate(tmp_path, names=("Season 1/E01.mkv", "Season 2/E01.mkv"))
    for file in arguments["download_files"]:
        file["filepath"] = "E01.mkv"

    result = _inspect(arguments)

    assert not result.ready
    assert "无法逐一对应" in result.reason


def test_parent_directory_escape_in_torrent_filename_is_rejected(tmp_path):
    arguments = _candidate(tmp_path)
    arguments["torrent_files"][0]["name"] = "../Season 1/E01.mkv"

    result = _inspect(arguments)

    assert not result.ready
    assert "路径无效" in result.reason


def test_absolute_torrent_filename_is_rejected(tmp_path):
    arguments = _candidate(tmp_path)
    arguments["torrent_files"][0]["name"] = arguments["download_files"][0]["fullpath"]

    result = _inspect(arguments)

    assert not result.ready
    assert "路径无效" in result.reason


def test_unrelated_extra_file_in_shared_directory_does_not_block_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    extra = Path(arguments["torrent"]["content_path"]) / "unregistered.nfo"
    extra.write_bytes(b"torrent sidecar")

    result = _inspect(arguments)

    assert result.ready, result.reason
    assert extra.read_bytes() == b"torrent sidecar"


def test_symlink_in_candidate_parent_path_still_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    content = Path(arguments["torrent"]["content_path"])
    season = content / "Season 1"
    moved = tmp_path / "original-season"
    season.rename(moved)
    season.symlink_to(moved, target_is_directory=True)

    result = _inspect(arguments)

    assert not result.ready
    assert "软链接" in result.reason


def test_copy_with_different_target_size_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    Path(arguments["transfer_histories"][0]["dest"]).write_bytes(b"x")

    result = _inspect(arguments)

    assert not result.ready
    assert "大小不一致" in result.reason


def test_claimed_hardlink_that_is_only_a_copy_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    arguments["transfer_histories"][0]["mode"] = "link"

    result = _inspect(arguments)

    assert not result.ready
    assert "不是同一文件" in result.reason


def test_claimed_copy_that_is_source_hardlink_blocks_cleanup(tmp_path):
    arguments = _candidate(tmp_path)
    history = arguments["transfer_histories"][0]
    destination = Path(history["dest"])
    destination.unlink()
    os.link(history["src"], destination)
    assert destination != Path(history["src"])
    assert os.path.samefile(history["src"], destination)

    result = _inspect(arguments)

    assert not result.ready
    assert "同一文件" in result.reason or "依赖下载数据" in result.reason


def test_download_record_source_must_match_torrent_file_location(tmp_path):
    arguments = _candidate(tmp_path)
    original = Path(arguments["download_files"][0]["fullpath"])
    wrong_source = tmp_path / "elsewhere" / original.name
    wrong_source.parent.mkdir(parents=True)
    wrong_source.write_bytes(original.read_bytes())
    arguments["download_files"][0]["fullpath"] = str(wrong_source)
    arguments["transfer_histories"][0]["src"] = str(wrong_source)

    result = _inspect(arguments)

    assert not result.ready
    assert "种子文件与下载源路径不一致" in result.reason


def test_copy_target_under_symlinked_parent_inside_torrent_is_rejected(tmp_path):
    arguments = _candidate(tmp_path, names=("Season 1/E01.mkv", "Season 1/E02.mkv"))
    content = Path(arguments["torrent"]["content_path"])
    library_alias = tmp_path / "library-alias"
    library_alias.symlink_to(content, target_is_directory=True)
    # E01's claimed library copy resolves to E02's source through the linked parent.
    # It has the same size, so only the resolved path check detects the dependency.
    arguments["transfer_histories"][0]["dest"] = str(library_alias / "Season 1/E02.mkv")

    result = _inspect(arguments)

    assert not result.ready
    assert "仍依赖下载数据" in result.reason
