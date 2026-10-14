"""File-level checks before removing a MoviePilot V2 download task and its data."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Tuple


@dataclass(frozen=True)
class CheckResult:
    """Result of a fail-closed cleanup check."""

    ready: bool
    reason: str
    missing_targets: Tuple[str, ...] = ()


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


def _file_map(save_path: str, files: Any) -> dict:
    """Resolve a downloader's complete file list without accepting escaping paths."""
    if not isinstance(files, (list, tuple)):
        files = getattr(files, "data", None)
    if not save_path or not files or not isinstance(files, (list, tuple)):
        raise ValueError("无法取得种子文件列表")
    resolved = {}
    for file in files:
        name = _relative(_value(file, "name"))
        if (not name or name.startswith("/") or ":" in name.split("/")[0]
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("种子文件路径无效")
        fullpath = os.path.normpath(os.path.join(save_path, name))
        if not _inside(save_path, fullpath) or fullpath in resolved:
            raise ValueError("种子文件路径无效")
        resolved[fullpath] = (name, file)
    return resolved


def _task_label(torrent: Any) -> str:
    name = _value(torrent, "title") or _value(torrent, "name") or _value(torrent, "hash") or "名称未知"
    return f"{name}（{_value(torrent, 'downloader')} / {_value(torrent, 'hash')}）"


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
    torrent_file_lookup: Optional[Callable[..., Any]] = None,
    allow_missing_targets: bool = False,
) -> CheckResult:
    """Validate every transfer, allowing absent targets only with explicit opt-in.

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
    media_exts = {str(ext).lower() for ext in media_extensions}
    try:
        listed_by_path = _file_map(save_path, torrent_files)
    except ValueError as error:
        return CheckResult(False, str(error))
    listed_by_name = {name: (file, path) for path, (name, file) in listed_by_path.items()}
    content_directory_ids = set()
    candidate_realpaths = {}
    candidate_file_ids = {}
    content_is_directory = os.path.isdir(content_path)
    directory_root = content_path if content_is_directory else os.path.dirname(content_path)
    try:
        # The downloader deletes its own file list, not every file under save_path.
        # Inspect only those paths; unrelated files in the shared directory are retained.
        for source in listed_by_path:
            if not _inside(content_path, source) or (not content_is_directory and source != content_path):
                return CheckResult(False, "种子内容路径与文件清单不一致")
            current = Path(source)
            while _inside(save_path, str(current)):
                if os.path.islink(current):
                    return CheckResult(False, "种子文件路径包含软链接")
                if str(current) != source and current.exists():
                    if not current.is_dir():
                        return CheckResult(False, "种子文件父路径不是目录")
                    if _inside(directory_root, str(current)):
                        info = current.stat()
                        content_directory_ids.add((info.st_dev, info.st_ino))
                if str(current) == save_path:
                    break
                current = current.parent
            if os.path.lexists(source):
                if not os.path.isfile(source):
                    return CheckResult(False, "种子文件清单包含未知文件类型")
                info = os.stat(source)
                candidate_file_ids[(info.st_dev, info.st_ino)] = source
            candidate_realpaths[os.path.realpath(source)] = source
    except OSError:
        return CheckResult(False, "无法核对种子文件路径")

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
            return CheckResult(False, f"其他种子的内容路径未知：{_task_label(other)}")
        overlapping = (_overlaps(content_path, other_path)
                       or _overlaps(os.path.realpath(content_path), os.path.realpath(other_path)))
        if not overlapping:
            try:
                overlapping = os.path.samefile(content_path, other_path)
            except OSError:
                pass
        if not overlapping:
            continue
        try:
            if not other_hash or not other_downloader or torrent_file_lookup is None:
                raise ValueError("缺少文件清单查询信息")
            other_save_path = _path(_value(other, "save_path"))
            other_files = _file_map(other_save_path, torrent_file_lookup(
                tid=other_hash, downloader=other_downloader
            ))
            if not _inside(other_save_path, other_path) or any(
                    not _inside(other_path, path) for path in other_files):
                raise ValueError("内容路径与文件清单不一致")
            for path in other_files:
                conflict = candidate_realpaths.get(os.path.realpath(path))
                if not conflict and os.path.exists(path):
                    info = os.stat(path)
                    conflict = candidate_file_ids.get((info.st_dev, info.st_ino))
                if conflict:
                    return CheckResult(False, f"其他种子共用下载数据：{_task_label(other)}；冲突文件：{conflict}；对方文件：{path}")
        except Exception:
            return CheckResult(False, f"无法核对共享目录种子的文件清单：{_task_label(other)}；内容路径：{other_path}")

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
    missing_targets = []
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
        if not destination:
            return CheckResult(False, "整理目标路径无效")
        try:
            destination_stat = os.lstat(destination)
        except FileNotFoundError:
            if not allow_missing_targets:
                return CheckResult(False, f"整理目标缺失：{destination}")
            destination_stat = None
        except OSError:
            return CheckResult(False, f"整理目标不可访问：{destination}")
        if destination_stat is not None:
            if stat.S_ISLNK(destination_stat.st_mode):
                return CheckResult(False, f"整理目标为软链接：{destination}")
            if not stat.S_ISREG(destination_stat.st_mode):
                return CheckResult(False, f"整理目标不是普通文件：{destination}")
        if (_overlaps(content_path, destination)
                or _overlaps(os.path.realpath(content_path), os.path.realpath(destination))
                or os.path.realpath(source) == os.path.realpath(destination)):
            return CheckResult(False, "整理目标仍依赖下载数据")
        try:
            # A bind mount can alias a download directory without changing realpath.
            for parent in Path(destination).parents:
                try:
                    parent_stat = os.stat(parent)
                except FileNotFoundError:
                    if destination_stat is None:
                        continue
                    raise
                if (parent_stat.st_dev, parent_stat.st_ino) in content_directory_ids:
                    return CheckResult(False, "整理目标仍依赖下载数据")
            source_size = os.path.getsize(source)
            if source_size <= 0:
                return CheckResult(False, "下载源文件为空")
            if destination_stat is None:
                # Explicit opt-in: successful history is required, but the moved
                # library copy cannot be verified at its former location.
                missing_targets.append(destination)
                continue
            if _value(latest, "mode") == "copy" and os.path.samefile(source, destination):
                return CheckResult(False, "复制目标与下载源仍为同一文件")
            destination_size = os.path.getsize(destination)
            if source_size != destination_size:
                return CheckResult(False, "整理目标与源文件大小不一致")
            if _value(latest, "mode") == "link":
                if not os.path.samefile(source, destination):
                    return CheckResult(False, "硬链接整理目标与源文件不是同一文件")
                if os.stat(source).st_nlink < 2:
                    return CheckResult(False, "硬链接整理目标缺少独立目录项")
        except OSError:
            return CheckResult(False, "无法核对整理目标与源文件")
    if missing_targets:
        return CheckResult(True, "已按设置忽略整理目标缺失：" + "；".join(missing_targets), tuple(missing_targets))
    return CheckResult(True, "所有已下载媒体文件均已安全整理")
