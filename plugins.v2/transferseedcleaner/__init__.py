"""Remove V2 downloader tasks only after their files have been safely organized."""

import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from apscheduler.triggers.interval import IntervalTrigger

from app.core.event import Event, eventmanager
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
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
    plugin_version = "1.1.0"
    plugin_author = "sunbroke555"
    author_url = "https://github.com/sunbroke555"
    plugin_config_prefix = "transferseedcleaner_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _dry_run = True
    _interval_minutes = 10
    _wait_minutes = 10
    _downloaders = frozenset()

    def init_plugin(self, config: dict = None):
        """Apply validated settings while keeping pending work across reloads."""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._dry_run = bool(config.get("dry_run", True))
        self._interval_minutes = self._bounded_int(config.get("interval_minutes"), 10, 1, 1440)
        self._wait_minutes = self._bounded_int(config.get("wait_minutes"), 10, 1, 10080)
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
        """This plugin does not expose an HTTP API."""
        return []

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
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "interval_minutes", "label": "复核周期（分钟）",
                                    "type": "number", "min": 1, "max": 1440,
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "wait_minutes", "label": "最后一次整理后等待（分钟）",
                                    "type": "number", "min": 1, "max": 10080,
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
            "downloaders": [],
            "interval_minutes": 10,
            "wait_minutes": 10,
        }

    def get_page(self) -> List[dict]:
        """Show queued work and recent cleanup decisions."""
        pending = self._load_pending()
        history = self.get_data("history") or []
        target_names = "、".join(sorted(self._downloaders)) or "未选择"
        page = [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": (f"待复核：{len(pending)} 个种子；"
                         f"模式：{'预览' if self._dry_run else '实际删除'}；"
                         f"允许清理的下载器：{target_names}。"),
            },
        }]
        for item in pending[-20:]:
            page.append({
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": f"{item['downloader']} / {item['hash']}：{item.get('reason') or '等待复核'}",
                },
            })
        for item in reversed(history[-20:]):
            page.append({
                "component": "VAlert",
                "props": {
                    "type": "success" if item.get("action") == "deleted" else "info",
                    "variant": "tonal",
                    "text": f"{item.get('time')} {item.get('downloader')} / {item.get('hash')}：{item.get('message')}",
                },
            })
        return page

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
        with self._pending_lock:
            pending = self._load_pending()
            current = next((item for item in pending if item["downloader"] == downloader
                            and item["hash"].lower() == download_hash.lower()), None)
            if current:
                current["last_event"] = time.time()
                current["reason"] = "等待整理流程结束"
            else:
                pending.append({
                    "downloader": downloader,
                    "hash": download_hash,
                    "last_event": time.time(),
                    "reason": "等待整理流程结束",
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
                        logger.info(f"整理后清理做种 {item['downloader']} / {item['hash']}：{reason}")
                    return

    def _record_deleted(self, item: dict):
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
                "action": "deleted",
                "message": "已删除下载器任务及下载文件",
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
        )

    def scan_pending(self):
        """Reconcile queued torrents and remove only candidates that pass every check."""
        if not self._enabled or not self._scan_lock.acquire(blocking=False):
            return
        try:
            pending = self._load_pending()
            if not pending:
                return
            selected_pending = []
            for item in pending:
                if item["downloader"] in self._downloaders:
                    selected_pending.append(item)
                else:
                    self._set_reason(item, "该下载器未选中，保留任务")
            if not selected_pending:
                return
            snapshot = self._snapshot_torrents()
            if snapshot is None:
                return
            all_torrents, service_types = snapshot
            download_oper = DownloadHistoryOper()
            transfer_oper = TransferHistoryOper()
            for item in selected_pending:
                if service_types.get(item["downloader"]) not in ("qbittorrent", "transmission"):
                    self._set_reason(item, "仅支持 qBittorrent 和 Transmission 下载任务")
                    continue
                if time.time() - float(item.get("last_event") or 0) < self._wait_minutes * 60:
                    self._set_reason(item, "等待最后一次整理后缓冲时间")
                    continue
                try:
                    result = self._inspect_pending(item, all_torrents, download_oper, transfer_oper)
                    if not result.ready:
                        self._set_reason(item, result.reason)
                        continue
                    if self._dry_run:
                        self._set_reason(item, "预览：符合删除条件")
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
                        if time.time() - float(current.get("last_event") or 0) < self._wait_minutes * 60:
                            continue
                        if self.chain.remove_torrents(
                            hashs=item["hash"], delete_file=True, downloader=item["downloader"]
                        ):
                            self._record_deleted(item)
                            logger.info(f"整理后清理做种：已删除 {item['downloader']} / {item['hash']} 及下载文件")
                        else:
                            self._set_reason(item, "下载器删除失败，等待下次重试")
                except Exception as error:
                    logger.error(f"整理后清理做种：复核 {item['downloader']} / {item['hash']} 失败：{error}")
                    self._set_reason(item, "复核异常，等待下次重试")
        finally:
            self._scan_lock.release()
