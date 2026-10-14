"""Pure checks used before removing a MoviePilot V2 download task and its data."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class CheckResult:
    """Result of a fail-closed cleanup check."""

    ready: bool
    reason: str


def _value(item: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a MoviePilot model or a downloader file dict."""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _path(value: Any) -> Optional[str]:
    """Normalize an absolute local path without requiring it to exist."""
    if not value:
        return None
    path = os.path.normpath(os.fspath(value))
    return path if os.path.isabs(path) else None


def _relative(value: Any) -> str:
    """Normalize a torrent-relative filename for comparison with download history."""
    name = str(value or "").replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def _overlaps(first: str, second: str) -> bool:
    """Detect identical or nested content paths used by two torrents."""
    try:
        common = os.path.commonpath((first, second))
    except ValueError:
        return False
    return common in (first, second)


def _inside(root: str, path: str) -> bool:
    """Return whether a path is the content root itself or a child of it."""
    try:
        return os.path.commonpath((root, path)) == root
    except ValueError:
        return False


def inspect_candidate(
    *,
    download_hash: str,
    downloader: str,
    torrent: Any,
    all_torrents: Iterable[Any],
    torrent_files: Iterable[Any],
    download_history: Any,
    download_files: Iterable[Any],
    transfer_histories: Iterable[Any],
    media_extensions: Iterable[str],
) -> CheckResult:
    """Require complete, independently stored output for every registered file.

    MoviePilot sends ``TransferComplete`` once per video, so an event or the
    torrent's "已整理" tag alone cannot establish that a multi-file torrent is safe.
    """
    expected_hash = str(download_hash or "").lower()
    if not expected_hash or not downloader:
        return CheckResult(False, "缺少下载器或种子 Hash")
    if not download_history or _value(download_history, "downloader") != downloader:
        return CheckResult(False, "找不到匹配的 MoviePilot 下载历史")
    if not torrent or str(_value(torrent, "hash", "")).lower() != expected_hash:
        return CheckResult(False, "下载器中未找到对应任务")
    if _value(torrent, "downloader") != downloader:
        return CheckResult(False, "下载任务所属下载器不匹配")
    try:
        if float(_value(torrent, "progress") or 0) < 100:
            return CheckResult(False, "种子尚未完成下载")
    except (TypeError, ValueError):
        return CheckResult(False, "无法读取种子下载进度")

    content_path = _path(_value(torrent, "content_path") or _value(torrent, "path"))
    save_path = _path(_value(torrent, "save_path"))
    if (not content_path or not save_path or content_path == os.path.sep
            or os.path.islink(content_path) or not os.path.exists(content_path)
            or not _inside(save_path, content_path)):
        return CheckResult(False, "无法确认种子内容路径")
    others = list(all_torrents or [])
    if not others:
        return CheckResult(False, "无法核对下载器中的其他任务")
    for other in others:
        other_hash = str(_value(other, "hash", "")).lower()
        other_downloader = _value(other, "downloader")
        if other_hash == expected_hash and other_downloader == downloader:
            continue
        other_path = _path(_value(other, "content_path") or _value(other, "path"))
        if not other_path:
            return CheckResult(False, "其他种子的内容路径未知")
        if (_overlaps(content_path, other_path)
                or _overlaps(os.path.realpath(content_path), os.path.realpath(other_path))):
            return CheckResult(False, "其他种子共用下载数据")

    media_exts = {str(ext).lower() for ext in media_extensions}
    listed = list(torrent_files or [])
    if not listed:
        return CheckResult(False, "无法取得种子文件列表")
    listed_by_name = {}
    listed_by_path = {}
    for file in listed:
        name = _relative(_value(file, "name"))
        if (not name or name.startswith("/") or ":" in name.split("/")[0]
                or any(part in ("", ".", "..") for part in name.split("/"))
                or name in listed_by_name):
            return CheckResult(False, "种子文件路径无效")
        fullpath = os.path.normpath(os.path.join(save_path, name))
        if not _inside(save_path, fullpath) or fullpath in listed_by_path:
            return CheckResult(False, "种子文件路径无效")
        listed_by_name[name] = (file, fullpath)
        listed_by_path[fullpath] = (name, file)

    content_directory_ids = set()
    if os.path.isdir(content_path):
        actual_files = set()

        def raise_walk_error(error):
            """Make an unreadable directory fail the inventory check."""
            raise error

        try:
            for directory, dirs, files in os.walk(content_path, onerror=raise_walk_error):
                directory_stat = os.stat(directory)
                content_directory_ids.add((directory_stat.st_dev, directory_stat.st_ino))
                if any(os.path.islink(os.path.join(directory, entry)) for entry in dirs + files):
                    return CheckResult(False, "种子目录包含软链接")
                for filename in files:
                    actual = os.path.normpath(os.path.join(directory, filename))
                    if not os.path.isfile(actual):
                        return CheckResult(False, "种子目录包含未知文件类型")
                    actual_files.add(actual)
        except OSError:
            return CheckResult(False, "无法完整读取种子内容目录")
        listed_files = {
            path for _, path in listed_by_name.values() if os.path.isfile(path)
        }
        if actual_files != listed_files:
            return CheckResult(False, "种子目录包含未登记文件或文件路径不一致")
    else:
        if content_path not in {path for _, path in listed_by_name.values()}:
            return CheckResult(False, "种子内容文件与清单不一致")
        try:
            parent_stat = os.stat(os.path.dirname(content_path))
            content_directory_ids.add((parent_stat.st_dev, parent_stat.st_ino))
        except OSError:
            return CheckResult(False, "无法核对种子内容目录")

    torrent_videos = {
        path for name, (file, path) in listed_by_name.items()
        if Path(name).suffix.lower() in media_exts
        and (os.path.isfile(path)
             or (_value(file, "selected", True) is not False
                 and _value(file, "priority", 1) != 0))
    }
    if not torrent_videos:
        return CheckResult(False, "种子中没有可核对的已下载媒体文件")
    registered = list(download_files or [])
    if not registered:
        return CheckResult(False, "没有 MoviePilot 下载文件记录")
    registered_by_path = {}
    registered_relative_paths = set()
    for file in registered:
        if _value(file, "downloader") != downloader or _value(file, "state") != 1:
            return CheckResult(False, "下载文件记录不完整或所属下载器不匹配")
        name = _relative(_value(file, "filepath"))
        source = _path(_value(file, "fullpath"))
        raw_save_path = _value(file, "savepath")
        recorded_save_path = _path(raw_save_path)
        if (not name or name.startswith("/") or ":" in name.split("/")[0]
                or any(part in ("", ".", "..") for part in name.split("/"))
                or not source or source in registered_by_path
                or (raw_save_path and not recorded_save_path)
                or (recorded_save_path, name) in registered_relative_paths):
            return CheckResult(False, "无法逐一对应种子文件与下载记录")
        if source not in listed_by_path:
            return CheckResult(False, "种子文件与下载源路径不一致")
        if (recorded_save_path
                and os.path.normpath(os.path.join(recorded_save_path, name)) != source):
            return CheckResult(False, "种子文件与下载源路径不一致")
        registered_by_path[source] = file
        registered_relative_paths.add((recorded_save_path, name))
    registered_videos = {
        path for path in registered_by_path if Path(path).suffix.lower() in media_exts
    }
    if registered_videos != torrent_videos:
        return CheckResult(False, "种子媒体文件与 MoviePilot 下载记录数量不一致")

    histories = list(transfer_histories or [])
    for file in registered:
        source = _path(_value(file, "fullpath"))
        if not source or not os.path.isfile(source):
            return CheckResult(False, "下载源文件缺失或不可访问")
        if not _inside(save_path, source):
            return CheckResult(False, "下载源文件不在种子内容路径内")
        matching = [
            history for history in histories
            if _value(history, "downloader") == downloader
            and _path(_value(history, "src")) == source
        ]
        if not matching:
            return CheckResult(False, "仍有文件没有整理记录")
        latest = max(matching, key=lambda item: int(_value(item, "id", 0) or 0))
        if not _value(latest, "status", False):
            return CheckResult(False, "最新整理记录失败")
        if _value(latest, "mode") not in ("copy", "link"):
            return CheckResult(False, "整理模式不是复制或硬链接")
        destination = _path(_value(latest, "dest"))
        if not destination or os.path.islink(destination) or not os.path.isfile(destination):
            return CheckResult(False, "整理目标缺失、不可访问或为软链接")
        if (_overlaps(content_path, destination)
                or _overlaps(os.path.realpath(content_path), os.path.realpath(destination))
                or os.path.realpath(source) == os.path.realpath(destination)):
            return CheckResult(False, "整理目标仍依赖下载数据")
        try:
            # A bind mount can alias a download directory without changing realpath.
            for parent in Path(destination).parents:
                parent_stat = os.stat(parent)
                if (parent_stat.st_dev, parent_stat.st_ino) in content_directory_ids:
                    return CheckResult(False, "整理目标仍依赖下载数据")
            if _value(latest, "mode") == "copy" and os.path.samefile(source, destination):
                return CheckResult(False, "复制目标与下载源仍为同一文件")
            source_size = os.path.getsize(source)
            destination_size = os.path.getsize(destination)
            if source_size <= 0 or source_size != destination_size:
                return CheckResult(False, "整理目标与源文件大小不一致")
            if _value(latest, "mode") == "link":
                if not os.path.samefile(source, destination):
                    return CheckResult(False, "硬链接整理目标与源文件不是同一文件")
                if os.stat(source).st_nlink < 2:
                    return CheckResult(False, "硬链接整理目标缺少独立目录项")
        except OSError:
            return CheckResult(False, "无法核对整理目标与源文件")
    return CheckResult(True, "所有已下载媒体文件均已安全整理")
