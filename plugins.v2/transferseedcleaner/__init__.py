"""Remove V2 downloader tasks only after their files have been safely organized."""

import threading
import time
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Dict, List, Tuple

from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends

from app.core.event import Event, eventmanager
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.db.user_oper import get_current_active_superuser
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.core.config import settings

from .safety import inspect_candidate


class TransferSeedCleaner(_PluginBase):
    """Queue transfer events, then verify the whole torrent before cleanup."""

    plugin_name = "整理后清理做种"
    plugin_desc = "确认整种已安全入库后，删除下载器任务和下载文件。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/clean.png"
    plugin_version = "1.3.2"
    plugin_author = "sunbroke555"
    author_url = "https://github.com/sunbroke555"
    plugin_config_prefix = "transferseedcleaner_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _dry_run = True
    _allow_missing_targets = False
    _interval_minutes = 10
    _downloaders = frozenset()
    _waiting_reason = "待检查"
    _waiting_reasons = frozenset({
        _waiting_reason, "等待整理流程结束", "等待最后一次整理后缓冲时间",
        "等待整理后缓冲时间", "已收到整理成功记录，等待定时复核",
    })

    def init_plugin(self, config: dict = None):
        """Apply validated settings while keeping pending work across reloads."""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._dry_run = bool(config.get("dry_run", True))
        self._allow_missing_targets = config.get("allow_missing_targets") is True
        self._interval_minutes = self._bounded_int(config.get("interval_minutes"), 10, 1, 1440)
        selected = config.get("downloaders") or []
        if isinstance(selected, str):
            selected = [selected]
        self._downloaders = frozenset(
            name.strip() for name in selected if isinstance(name, str) and name.strip()
        ) if isinstance(selected, (list, tuple, set)) else frozenset()
        if not hasattr(self, "_pending_lock"):
            self._pending_lock = threading.RLock()
        if not hasattr(self, "_scan_lock"):
            self._scan_lock = threading.Lock()

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        """Keep UI values within a safe range even if config is edited directly."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return min(max(parsed, minimum), maximum)

    def get_state(self) -> bool:
        """Show whether automatic reconciliation is enabled."""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """This plugin does not expose a remote command."""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """Expose the manual scan through MoviePilot's authenticated API."""
        return [{
            "path": "/run_now",
            "endpoint": self.run_now,
            "methods": ["POST"],
            "auth": "bear",
            "dependencies": [Depends(get_current_active_superuser)],
            "summary": "立即检查并清理待处理种子",
        }]

    def run_now(self) -> dict:
        """Use the same checks, preview mode and lock as the scheduled scan."""
        return self.scan_pending()

    def get_service(self) -> List[Dict[str, Any]]:
        """Ask MoviePilot to recheck queued torrents at a fixed interval."""
        if not self._enabled:
            return []
        return [{
            "id": f"{self.__class__.__name__}.scan_pending",
            "name": "整理后清理做种复核",
            "trigger": IntervalTrigger(minutes=self._interval_minutes),
            "func": self.scan_pending,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Present deletion and timing choices in MoviePilot's V2 form format."""
        try:
            downloader_options = [
                {"title": name, "value": name}
                for name, config in DownloaderHelper().get_configs().items()
                if getattr(config, "type", None) in ("qbittorrent", "transmission")
            ]
        except Exception as error:
            logger.warning(f"整理后清理做种：无法读取下载器选项：{error}")
            downloader_options = []
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VSelect", "props": {
                                    "model": "downloaders", "label": "允许清理的下载器",
                                    "items": downloader_options, "multiple": True,
                                    "chips": True, "clearable": True,
                                    "hint": "只清理选中的下载器；不选择则不会清理。",
                                    "persistent-hint": True,
                                }},
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VSwitch", "props": {
                                    "model": "enabled", "label": "启用插件",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VSwitch", "props": {
                                    "model": "dry_run", "label": "预览模式（不删除）",
                                }},
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VSwitch", "props": {
                                    "model": "allow_missing_targets", "label": "允许整理目标缺失时清理",
                                    "hint": "默认关闭。确认整理后的文件已搬走时可开启；若文件实际已丢失，也会按成功整理记录清理下载数据。",
                                    "persistent-hint": True,
                                }},
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "interval_minutes", "label": "自动清理间隔（分钟）",
                                    "type": "number", "min": 1, "max": 1440,
                                    "hint": "每隔多久检查一次待处理种子；满足条件时执行清理。",
                                    "persistent-hint": True,
                                }},
                            ]},
                        ],
                    },
                    {"component": "VAlert", "props": {
                        "type": "warning", "variant": "tonal",
                        "text": "关闭预览后，符合条件的种子任务和下载文件会一起删除。软链接、整理失败和共享数据会跳过。",
                    }},
                ],
            },
        ], {
            "enabled": False,
            "dry_run": True,
            "allow_missing_targets": False,
            "downloaders": [],
            "interval_minutes": 10,
        }

    def get_page(self) -> List[dict]:
        """Show flat title/subtitle lists and an authenticated run-now action."""
        pending = sorted(self._load_pending(), key=lambda item: float(item["last_event"]), reverse=True)
        history = list(reversed((self.get_data("history") or [])[-100:]))
        target_names = "、".join(sorted(self._downloaders)) or "未选择"
        for item in pending:
            self._refresh_pending_metadata(item)
        for item in history:
            item["name"] = item.get("name") or self._history_name(item)
            if "organized_names" not in item:
                item["organized_names"] = self._history_organized_names(item) or []
        running = self._scan_lock.locked()
        can_run = (self._enabled and not running
                   and any(item["downloader"] in self._downloaders for item in pending))
        page = [{
            "component": "div",
            "props": {"class": "d-flex align-center flex-wrap mb-3", "style": "gap: 12px;"},
            "content": [{
                "component": "VBtn",
                "props": {"color": "primary", "variant": "flat", "disabled": not can_run},
                "text": "立即执行",
                "events": {"click": {
                    "api": f"plugin/{self.__class__.__name__}/run_now",
                    "method": "post",
                    "params": {},
                }},
            }, {
                "component": "span", "props": {"class": "text-body-2 text-medium-emphasis"},
                "text": "正在检查待处理种子…" if running else (
                    "立即检查待处理列表；当前为预览模式，不会删除。" if self._dry_run
                    else "立即检查待处理列表，并清理符合条件的种子。"
                ),
            }],
        }, {
            "component": "div",
            "props": {"class": "text-body-2 text-medium-emphasis mb-3"},
            "text": (f"{'已启用' if self._enabled else '已停用'} · "
                     f"{'预览模式' if self._dry_run else '自动清理'} · "
                     f"每 {self._interval_minutes} 分钟检查 · 下载器：{target_names}"),
        }]
        last_run = self.get_data("last_run")
        if last_run:
            page.append({
                "component": "div", "props": {"class": "text-caption text-medium-emphasis mb-3"},
                "text": (f"最近执行：{last_run.get('time')} · {last_run.get('message')} · "
                         f"检查 {last_run.get('checked', 0)} 个 · 清理 {last_run.get('deleted', 0)} 个 · "
                         f"预览可清理 {last_run.get('ready', 0)} 个 · 保留 {last_run.get('retained', 0)} 个"),
            })
        page.extend([
            self._record_section("待处理", pending),
            self._record_section("清理记录", history, deleted=True),
        ])
        return page

    def _record_section(self, title: str, items: List[dict], deleted: bool = False) -> dict:
        rows = []
        for item in items:
            if rows:
                rows.append({"component": "div", "props": {
                    "role": "separator", "style": "border-top: 1px solid rgba(var(--v-theme-on-surface), 0.12);",
                }})
            rows.append(self._record_row(item, deleted))
        if not rows:
            content = [{"component": "div", "props": {"class": "text-body-2 text-medium-emphasis py-2"},
                        "text": "暂无清理记录" if deleted else "暂无待处理种子"}]
        else:
            content = [{
                "component": "VList",
                "props": {"density": "compact", "style": f"max-height: {360 if deleted else 480}px; overflow-y: auto;"},
                "content": rows,
            }]
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": [{"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": f"{title} · {len(items)}"},
                        {"component": "VCardText", "content": content}],
        }

    def _record_row(self, item: dict, deleted: bool) -> dict:
        wrap = "white-space: normal; overflow-wrap: anywhere; overflow: visible; text-overflow: clip; -webkit-line-clamp: unset;"
        names = item.get("organized_names") or []
        subtitle = (names[0] + (f" · 共 {len(names)} 个已整理文件" if len(names) > 1 else "")) if names else "暂无成功整理名称"
        status = "已清理" if deleted else self._pending_status(item)
        timestamp = item.get("time") if deleted else self._format_time(float(item["last_event"]))
        content = [{
            "component": "VListItemTitle", "props": {"class": "text-body-1 font-weight-medium", "style": wrap},
            "text": item.get("name") or "名称暂不可用",
        }, {
            "component": "VListItemSubtitle", "props": {"class": "mt-1", "style": wrap},
            "text": f"整理后：{subtitle}",
        }, {
            "component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2", "style": wrap},
            "text": f"{status} · {item['downloader']} · {timestamp}",
        }]
        if not deleted and status in ("未整理完成", "已跳过"):
            content.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis", "style": wrap},
                            "text": item.get("reason") or "等待检查结果"})
        details = [{"component": "summary", "text": "查看详情"},
                   {"component": "div", "props": {"style": wrap}, "text": f"Hash：{item['hash']}"}]
        if deleted:
            details.append({"component": "div", "text": item.get("message") or "已删除下载器任务及下载文件"})
            details.extend({"component": "div", "props": {"style": wrap}, "text": f"已忽略缺失目标：{path}"}
                           for path in item.get("ignored_missing_targets") or [])
        elif status == "可清理（预览）" and item.get("reason") != "预览：符合删除条件":
            details.append({"component": "div", "props": {"style": wrap}, "text": item["reason"]})
        if len(names) > 1:
            details.extend({"component": "div", "props": {"style": wrap}, "text": name} for name in names)
        content.append({"component": "details", "props": {"class": "text-caption text-medium-emphasis mt-1"}, "content": details})
        return {"component": "VListItem", "props": {"class": "px-0 py-3"}, "content": content}

    @staticmethod
    def _format_time(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def _pending_status(self, item: dict) -> str:
        """Translate current and legacy decisions into short user-facing states."""
        if not self._enabled:
            return "已暂停"
        if item["downloader"] not in self._downloaders:
            return "下载器未选中"
        reason = item.get("reason")
        if not reason or reason in self._waiting_reasons or reason == "该下载器未选中，保留任务":
            return "待检查"
        if reason.startswith("预览：符合删除条件"):
            return "可清理（预览）"
        if reason in ("仍有文件没有整理记录", "最新整理记录失败"):
            return "未整理完成"
        return "已跳过"

    @staticmethod
    def _history_name(item: dict) -> str:
        """Look up display metadata locally without treating it as safety evidence."""
        try:
            history = DownloadHistoryOper().get_by_hash(item["hash"])
            if getattr(history, "downloader", None) == item["downloader"]:
                name = getattr(history, "torrent_name", None)
                return name.strip() if isinstance(name, str) else ""
        except Exception as error:
            logger.debug(f"整理后清理做种：暂时无法读取种子名称：{error}")
        return ""

    @staticmethod
    def _history_organized_names(item: dict):
        """Use the latest successful destination per source as display metadata."""
        try:
            histories = TransferHistoryOper().list_by_hash(item["hash"]) or []
            latest = {}
            for history in histories:
                if getattr(history, "downloader", None) != item["downloader"]:
                    continue
                recorded_hash = getattr(history, "download_hash", None)
                if recorded_hash and str(recorded_hash).lower() != item["hash"].lower():
                    continue
                source = getattr(history, "src", None)
                if not source:
                    continue
                previous = latest.get(source)
                if previous is None or int(getattr(history, "id", 0) or 0) > int(getattr(previous, "id", 0) or 0):
                    latest[source] = history
            extensions = {str(ext).lower() for ext in settings.RMT_MEDIAEXT}
            destinations = set()
            for history in latest.values():
                destination = getattr(history, "dest", None)
                if getattr(history, "status", False) and isinstance(destination, str) and destination:
                    path = PurePosixPath(destination.replace("\\", "/"))
                    if path.suffix.lower() in extensions:
                        destinations.add(str(path))
            return [PurePosixPath(path).name for path in sorted(destinations)]
        except Exception as error:
            logger.debug(f"整理后清理做种：暂时无法读取整理后名称：{error}")
            return None

    def _refresh_pending_metadata(self, item: dict, torrents: List[Any] = None):
        """Merge display names without changing the event generation used for deletion."""
        torrent = self._matching_torrent(torrents, item) if torrents is not None else None
        # DownloaderTorrent.title is the original release name; .name is parsed media metadata.
        title = getattr(torrent, "title", None)
        name = title.strip() if isinstance(title, str) else ""
        name = name or item.get("name") or self._history_name(item)
        organized_names = self._history_organized_names(item)
        updates = {}
        if name:
            updates["name"] = name
        if organized_names is not None:
            updates["organized_names"] = organized_names
        with self._pending_lock:
            pending = self._load_pending()
            for current in pending:
                if (current["downloader"] == item["downloader"]
                        and current["hash"].lower() == item["hash"].lower()):
                    if current.get("last_event") != item.get("last_event"):
                        return
                    if any(current.get(key) != value for key, value in updates.items()):
                        current.update(updates)
                        self.save_data("pending", pending)
                    break
            item.update(updates)

    @staticmethod
    def _task_label(item: dict) -> str:
        return f"{item.get('name') or '名称暂不可用'}（{item['downloader']} / {item['hash']}）"

    def stop_service(self):
        """MoviePilot owns the interval service; no private thread remains."""
        pass

    def _load_pending(self) -> List[dict]:
        """Read valid queued torrent identities from plugin-owned storage."""
        data = self.get_data("pending") or []
        if not isinstance(data, list):
            return []
        pending = []
        for item in data:
            if not isinstance(item, dict) or not item.get("downloader") or not item.get("hash"):
                continue
            try:
                float(item.get("last_event"))
            except (TypeError, ValueError):
                continue
            pending.append(item)
        return pending

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """Remember the torrent; deleting in a per-file event would be premature."""
        if not self._enabled:
            return
        data = event.event_data or {}
        transferinfo = data.get("transferinfo")
        if not transferinfo or not getattr(transferinfo, "success", False):
            return
        downloader = str(data.get("downloader") or "").strip()
        download_hash = str(data.get("download_hash") or "").strip()
        if not downloader or downloader not in self._downloaders or not download_hash:
            return
        identity = {"downloader": downloader, "hash": download_hash}
        name = self._history_name(identity)
        organized_names = self._history_organized_names(identity)
        with self._pending_lock:
            pending = self._load_pending()
            current = next((item for item in pending if item["downloader"] == downloader
                            and item["hash"].lower() == download_hash.lower()), None)
            if current:
                current["last_event"] = time.time()
                current["reason"] = self._waiting_reason
                if name and not current.get("name"):
                    current["name"] = name
                if organized_names is not None:
                    current["organized_names"] = organized_names
            else:
                pending.append({
                    "downloader": downloader,
                    "hash": download_hash,
                    "name": name,
                    "organized_names": organized_names or [],
                    "last_event": time.time(),
                    "reason": self._waiting_reason,
                })
            self.save_data("pending", pending)

    def _set_reason(self, item: dict, reason: str):
        """Update a pending item's reason without losing a newer transfer event."""
        with self._pending_lock:
            pending = self._load_pending()
            for current in pending:
                if (current["downloader"] == item["downloader"]
                        and current["hash"].lower() == item["hash"].lower()
                        and current.get("last_event") == item.get("last_event")):
                    if current.get("reason") != reason:
                        current["reason"] = reason
                        self.save_data("pending", pending)
                        logger.info(f"整理后清理做种 {self._task_label(current)}：{reason}")
                    return

    def _record_deleted(self, item: dict, missing_targets=()):
        """Remove finished work and retain a short audit trail."""
        with self._pending_lock:
            pending = [
                current for current in self._load_pending()
                if not (current["downloader"] == item["downloader"]
                        and current["hash"].lower() == item["hash"].lower())
            ]
            self.save_data("pending", pending)
            history = self.get_data("history") or []
            history.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "downloader": item["downloader"],
                "hash": item["hash"],
                "name": item.get("name") or "",
                "organized_names": list(item.get("organized_names") or []),
                "action": "deleted",
                "message": "已删除下载器任务及下载文件" + (
                    f"；按设置忽略 {len(missing_targets)} 个缺失整理目标" if missing_targets else ""
                ),
                "ignored_missing_targets": list(missing_targets),
            })
            self.save_data("history", history[-100:])

    @staticmethod
    def _matching_torrent(torrents: List[Any], item: dict):
        """Require exactly one live task with the queued downloader and hash."""
        matches = [
            torrent for torrent in torrents
            if getattr(torrent, "downloader", None) == item["downloader"]
            and str(getattr(torrent, "hash", "")).lower() == item["hash"].lower()
        ]
        return matches[0] if len(matches) == 1 else None

    def _inventory_hashes(self):
        """Get a successful raw snapshot from every configured downloader."""
        helper = DownloaderHelper()
        configs = helper.get_configs()
        services = helper.get_services()
        if not configs:
            return None
        identities = set()
        service_types = {}
        for name in configs:
            service = services.get(name)
            if not service or service.instance.is_inactive():
                logger.warning(f"整理后清理做种：下载器 {name} 不可用，本轮不删除")
                return None
            try:
                torrents, error = service.instance.get_torrents()
            except Exception as exc:
                logger.warning(f"整理后清理做种：下载器 {name} 查询失败，本轮不删除：{exc}")
                return None
            if error or torrents is None:
                logger.warning(f"整理后清理做种：下载器 {name} 查询失败，本轮不删除")
                return None
            service_types[name] = service.type
            for torrent in torrents:
                raw_hash = (torrent.get("hash") if isinstance(torrent, dict)
                            else getattr(torrent, "hashString", None) or getattr(torrent, "hash", None))
                if not raw_hash:
                    logger.warning(f"整理后清理做种：下载器 {name} 返回无 Hash 任务，本轮不删除")
                    return None
                identities.add((name, str(raw_hash).lower()))
        return identities, service_types

    def _snapshot_torrents(self):
        """Require the downloader and MoviePilot views to agree before use."""
        snapshot = self._inventory_hashes()
        if snapshot is None:
            return None
        inventory, service_types = snapshot
        all_torrents = self.chain.list_torrents(include_all_tags=True)
        if all_torrents is None:
            logger.warning("整理后清理做种：查询下载器任务失败，本轮不删除")
            return None
        normalized = {
            (getattr(torrent, "downloader", None), str(getattr(torrent, "hash", "")).lower())
            for torrent in all_torrents
        }
        if inventory != normalized:
            logger.warning("整理后清理做种：下载器任务快照不一致，本轮不删除")
            return None
        return all_torrents, service_types

    def _inspect_pending(self, item, all_torrents, download_oper, transfer_oper):
        """Re-read file and history evidence for one queued torrent."""
        torrent = self._matching_torrent(all_torrents, item)
        torrent_files = self.chain.torrent_files(
            tid=item["hash"], downloader=item["downloader"]
        ) if torrent else None
        if torrent_files is not None and not isinstance(torrent_files, list):
            torrent_files = getattr(torrent_files, "data", None)
        return inspect_candidate(
            download_hash=item["hash"],
            downloader=item["downloader"],
            torrent=torrent,
            all_torrents=all_torrents,
            torrent_files=torrent_files,
            download_history=download_oper.get_by_hash(item["hash"]),
            download_files=download_oper.get_files_by_hash(item["hash"]),
            transfer_histories=transfer_oper.list_by_hash(item["hash"]),
            media_extensions=settings.RMT_MEDIAEXT,
            torrent_file_lookup=self.chain.torrent_files,
            allow_missing_targets=self._allow_missing_targets,
        )

    def scan_pending(self) -> dict:
        """Reconcile queued torrents and remove only candidates that pass every check."""
        result = {"success": True, "message": "检查完成", "checked": 0,
                  "deleted": 0, "ready": 0, "retained": 0}
        if not self._enabled:
            return dict(result, success=False, message="请先启用插件")
        if not self._downloaders:
            return dict(result, success=False, message="请先选择允许清理的下载器")
        if not self._scan_lock.acquire(blocking=False):
            return dict(result, success=False, message="正在检查中，请稍后查看结果")
        preview = self._dry_run
        try:
            pending = self._load_pending()
            result["retained"] = len(pending)
            if not pending:
                result["message"] = "暂无待处理种子"
                return result
            selected_pending = []
            for item in pending:
                if item["downloader"] in self._downloaders:
                    selected_pending.append(item)
                else:
                    self._set_reason(item, "该下载器未选中，保留任务")
            if not selected_pending:
                result["message"] = "所选下载器暂无待处理种子"
                return result
            snapshot = self._snapshot_torrents()
            if snapshot is None:
                result.update(success=False, message="无法核对下载器任务，本轮未执行清理")
                return result
            all_torrents, service_types = snapshot
            download_oper = DownloadHistoryOper()
            transfer_oper = TransferHistoryOper()
            for item in selected_pending:
                result["checked"] += 1
                self._refresh_pending_metadata(item, all_torrents)
                if service_types.get(item["downloader"]) not in ("qbittorrent", "transmission"):
                    self._set_reason(item, "仅支持 qBittorrent 和 Transmission 下载任务")
                    continue
                try:
                    inspection = self._inspect_pending(item, all_torrents, download_oper, transfer_oper)
                    if not inspection.ready:
                        self._set_reason(item, inspection.reason)
                        continue
                    if preview or self._dry_run:
                        result["ready"] += 1
                        self._set_reason(item, "预览：符合删除条件" + (
                            f"；{inspection.reason}" if inspection.missing_targets else ""
                        ))
                        continue
                    # Recheck live tasks and files immediately before the destructive call.
                    # Another torrent may have started using this data during the scan.
                    fresh_snapshot = self._snapshot_torrents()
                    if fresh_snapshot is None:
                        self._set_reason(item, "删除前无法重新核对下载器任务")
                        continue
                    fresh_torrents, fresh_types = fresh_snapshot
                    if fresh_types.get(item["downloader"]) not in ("qbittorrent", "transmission"):
                        self._set_reason(item, "下载器类型发生变化")
                        continue
                    fresh_result = self._inspect_pending(
                        item, fresh_torrents, download_oper, transfer_oper
                    )
                    if not fresh_result.ready:
                        self._set_reason(item, fresh_result.reason)
                        continue
                    # A later transfer event invalidates this scan's evidence.
                    with self._pending_lock:
                        current = next((entry for entry in self._load_pending()
                                        if entry["downloader"] == item["downloader"]
                                        and entry["hash"].lower() == item["hash"].lower()), None)
                        if not current or current.get("last_event") != item.get("last_event"):
                            continue
                        if item["downloader"] not in self._downloaders:
                            self._set_reason(item, "该下载器未选中，保留任务")
                            continue
                        if not self._enabled:
                            self._set_reason(item, "插件已停用，保留任务")
                            result.update(success=False, message="插件已停用，本轮检查已停止")
                            break
                        if preview or self._dry_run:
                            result["ready"] += 1
                            self._set_reason(item, "预览：符合删除条件" + (
                                f"；{fresh_result.reason}" if fresh_result.missing_targets else ""
                            ))
                            continue
                        if fresh_result.missing_targets and not self._allow_missing_targets:
                            self._set_reason(item, "允许整理目标缺失时清理已关闭，保留任务")
                            continue
                        if self.chain.remove_torrents(
                            hashs=item["hash"], delete_file=True, downloader=item["downloader"]
                        ):
                            result["deleted"] += 1
                            result["retained"] -= 1
                            self._record_deleted(item, missing_targets=fresh_result.missing_targets)
                            logger.info(f"整理后清理做种：已删除 {self._task_label(item)} 及下载文件" + (
                                f"；{fresh_result.reason}" if fresh_result.missing_targets else ""
                            ))
                        else:
                            self._set_reason(item, "下载器删除失败，等待下次重试")
                except Exception as error:
                    result.update(success=False, message="检查结束，部分任务复核异常")
                    logger.error(f"整理后清理做种：复核 {self._task_label(item)} 失败：{error}")
                    self._set_reason(item, "复核异常，等待下次重试")
            return result
        except Exception as error:
            logger.error(f"整理后清理做种：本轮检查异常：{error}")
            result.update(success=False, message="本轮检查异常，未完成的任务已保留")
            return result
        finally:
            result["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self.save_data("last_run", dict(result))
            except Exception as error:
                logger.error(f"整理后清理做种：保存本次执行结果失败：{error}")
            finally:
                self._scan_lock.release()
