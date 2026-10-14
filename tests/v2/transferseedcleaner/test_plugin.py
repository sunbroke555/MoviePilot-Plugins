"""Exercise the V2 cleanup flow without a running MoviePilot or downloader."""

import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v2" / "transferseedcleaner"


@pytest.fixture
def plugin_module(monkeypatch):
    """Import the real plugin with only its host imports replaced."""
    def module(name, package=False, **attributes):
        stub = ModuleType(name)
        if package:
            stub.__path__ = []
        for key, value in attributes.items():
            setattr(stub, key, value)
        monkeypatch.setitem(sys.modules, name, stub)

    class PluginBase:
        def __init__(self):
            self._data = {}

        def get_data(self, key):
            return self._data.get(key)

        def save_data(self, key, value):
            self._data[key] = value

    class Event:
        def __init__(self, event_data):
            self.event_data = event_data

    class EventManager:
        def register(self, _event_type):
            return lambda callback: callback

    class IntervalTrigger:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class DownloaderHelper:
        service_type = "qbittorrent"
        downloader = SimpleNamespace(
            is_inactive=MagicMock(return_value=False),
            get_torrents=MagicMock(return_value=([SimpleNamespace(hash="abc123")], False)),
        )

        def get_configs(self):
            return {"qb": SimpleNamespace(type="qbittorrent", enabled=True)}

        def get_services(self):
            return {"qb": SimpleNamespace(type=self.service_type, instance=self.downloader)}

    module("apscheduler", package=True)
    module("apscheduler.triggers", package=True)
    module("apscheduler.triggers.interval", IntervalTrigger=IntervalTrigger)
    module("app", package=True)
    module("app.core", package=True)
    module("app.core.event", Event=Event, eventmanager=EventManager())
    module("app.core.config", settings=SimpleNamespace(RMT_MEDIAEXT={".mkv"}))
    module("app.db", package=True)
    module("app.db.downloadhistory_oper", DownloadHistoryOper=object)
    module("app.db.transferhistory_oper", TransferHistoryOper=object)
    module("app.helper", package=True)
    module("app.helper.downloader", DownloaderHelper=DownloaderHelper)
    module("app.log", logger=MagicMock())
    module("app.plugins", package=True, _PluginBase=PluginBase)
    module("app.schemas", package=True)
    module("app.schemas.types", EventType=SimpleNamespace(TransferComplete="TransferComplete"))

    package_name = "_transferseedcleaner_test_plugin"
    spec = importlib.util.spec_from_file_location(
        package_name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package_name, loaded)
    spec.loader.exec_module(loaded)
    yield loaded
    monkeypatch.delitem(sys.modules, f"{package_name}.safety", raising=False)


@pytest.fixture
def runtime(tmp_path, plugin_module):
    """Provide a complete single-file torrent and successful transfer history."""
    source = tmp_path / "downloads" / "Show" / "Show.mkv"
    destination = tmp_path / "library" / "Show.mkv"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b"downloaded video")
    destination.write_bytes(source.read_bytes())

    torrent = SimpleNamespace(
        hash="abc123", downloader="qb", progress=100,
        content_path=str(source.parent), save_path=str(source.parent)
    )
    chain = SimpleNamespace(
        list_torrents=MagicMock(return_value=[torrent]),
        torrent_files=MagicMock(return_value=[
            SimpleNamespace(name="Show.mkv", selected=True, priority=1)
        ]),
        remove_torrents=MagicMock(return_value=True),
    )
    download_oper = SimpleNamespace(
        get_by_hash=MagicMock(return_value=SimpleNamespace(
            downloader="qb", download_hash="abc123"
        )),
        get_files_by_hash=MagicMock(return_value=[SimpleNamespace(
            filepath="Show.mkv", fullpath=str(source), downloader="qb", state=1
        )]),
    )
    transfer_oper = SimpleNamespace(
        list_by_hash=MagicMock(return_value=[SimpleNamespace(
            id=1, src=str(source), dest=str(destination), downloader="qb",
            status=True, mode="copy"
        )]),
    )
    plugin_module.DownloadHistoryOper = lambda: download_oper
    plugin_module.TransferHistoryOper = lambda: transfer_oper
    plugin = plugin_module.TransferSeedCleaner()
    plugin.init_plugin({
        "enabled": True, "dry_run": False, "wait_minutes": 1, "downloaders": ["qb"]
    })
    plugin.chain = chain
    event = plugin_module.Event({
        "downloader": "qb",
        "download_hash": "abc123",
        "transferinfo": SimpleNamespace(success=True),
    })
    return plugin, chain, event


def _age_pending(plugin):
    """Move the event outside the intentional post-transfer grace period."""
    pending = plugin.get_data("pending")
    assert len(pending) == 1
    pending[0]["last_event"] = time.time() - 120


def test_transfer_event_only_queues_and_deduplicates(runtime):
    plugin, chain, event = runtime

    plugin.on_transfer_complete(event)
    plugin.on_transfer_complete(event)

    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("pending")[0]["hash"] == "abc123"
    chain.remove_torrents.assert_not_called()


def test_form_lists_configured_downloader_and_defaults_to_none(runtime):
    plugin, _, _ = runtime

    form, defaults = plugin.get_form()
    selects = []

    def find_selects(nodes):
        for node in nodes:
            if node.get("component") == "VSelect" and node.get("props", {}).get("model") == "downloaders":
                selects.append(node["props"])
            find_selects(node.get("content", []))

    find_selects(form)

    assert defaults["downloaders"] == []
    assert len(selects) == 1
    assert selects[0]["multiple"] is True
    assert selects[0]["items"] == [{"title": "qb", "value": "qb"}]


def test_preview_marks_ready_candidate_without_deleting(runtime):
    plugin, chain, event = runtime
    plugin._dry_run = True
    plugin.on_transfer_complete(event)
    _age_pending(plugin)

    plugin.scan_pending()

    chain.remove_torrents.assert_not_called()
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("pending")[0]["reason"] == "预览：符合删除条件"


def test_ready_candidate_removes_exact_task_and_data_once(runtime):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    _age_pending(plugin)

    plugin.scan_pending()
    plugin.scan_pending()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )
    assert plugin.get_data("pending") == []
    assert plugin.get_data("history")[-1]["action"] == "deleted"


def test_omitted_downloader_selection_keeps_existing_pending_task(runtime):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    _age_pending(plugin)
    plugin.init_plugin({"enabled": True, "dry_run": False, "wait_minutes": 1})

    plugin.scan_pending()

    assert len(plugin.get_data("pending")) == 1
    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()


def test_unselected_downloader_event_is_not_queued(runtime):
    plugin, chain, event = runtime
    plugin.init_plugin({"enabled": True, "dry_run": False, "downloaders": []})

    plugin.on_transfer_complete(event)

    assert plugin.get_data("pending") is None
    chain.remove_torrents.assert_not_called()


def test_only_selected_downloader_can_reach_delete(runtime, plugin_module, monkeypatch, tmp_path):
    plugin, chain, qb_event = runtime
    tr_hash = "def456"
    other_dir = tmp_path / "other-downloads"
    other_dir.mkdir()
    chain.list_torrents.return_value.append(SimpleNamespace(
        hash=tr_hash, downloader="tr", progress=100,
        content_path=str(other_dir), save_path=str(other_dir),
    ))
    qb_service = SimpleNamespace(
        type="qbittorrent", instance=plugin_module.DownloaderHelper.downloader,
    )
    tr_downloader = SimpleNamespace(
        is_inactive=MagicMock(return_value=False),
        get_torrents=MagicMock(return_value=([SimpleNamespace(hash=tr_hash)], False)),
    )
    tr_service = SimpleNamespace(type="transmission", instance=tr_downloader)
    configs = {
        "qb": SimpleNamespace(type="qbittorrent", enabled=True),
        "tr": SimpleNamespace(type="transmission", enabled=True),
    }
    services = {"qb": qb_service, "tr": tr_service}
    helper = SimpleNamespace(get_configs=lambda: configs, get_services=lambda: services)
    monkeypatch.setattr(plugin_module, "DownloaderHelper", lambda: helper)
    plugin.init_plugin({
        "enabled": True, "dry_run": False, "wait_minutes": 1,
        "downloaders": ["qb", "tr"],
    })
    plugin.on_transfer_complete(qb_event)
    plugin.on_transfer_complete(plugin_module.Event({
        "downloader": "tr", "download_hash": tr_hash,
        "transferinfo": SimpleNamespace(success=True),
    }))
    pending = plugin.get_data("pending")
    assert len(pending) == 2
    for item in pending:
        item["last_event"] = time.time() - 120
    plugin.init_plugin({
        "enabled": True, "dry_run": False, "wait_minutes": 1,
        "downloaders": ["qb"],
    })

    plugin.scan_pending()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )
    assert chain.torrent_files.call_count > 0
    assert all(call.kwargs["downloader"] == "qb" for call in chain.torrent_files.call_args_list)
    assert [item["downloader"] for item in plugin.get_data("pending")] == ["tr"]


def test_failed_removal_keeps_candidate_for_retry(runtime):
    plugin, chain, event = runtime
    chain.remove_torrents.side_effect = [False, True]
    plugin.on_transfer_complete(event)
    _age_pending(plugin)

    plugin.scan_pending()

    assert len(plugin.get_data("pending")) == 1
    assert "重试" in plugin.get_data("pending")[0]["reason"]
    assert plugin.get_data("history") is None

    plugin.scan_pending()

    assert chain.remove_torrents.call_count == 2
    assert plugin.get_data("pending") == []
    assert plugin.get_data("history")[-1]["action"] == "deleted"


@pytest.mark.parametrize("failure", ["error_flag", "exception"])
def test_downloader_inventory_failure_prevents_deletion(runtime, plugin_module, failure):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    _age_pending(plugin)
    downloader = plugin_module.DownloaderHelper.downloader
    if failure == "error_flag":
        downloader.get_torrents.return_value = ([], True)
    else:
        downloader.get_torrents.side_effect = RuntimeError("downloader unavailable")

    plugin.scan_pending()

    downloader.get_torrents.assert_called_once_with()
    chain.list_torrents.assert_not_called()
    chain.remove_torrents.assert_not_called()
    assert len(plugin.get_data("pending")) == 1


@pytest.mark.parametrize("difference", ["raw_has_extra", "normalized_has_extra"])
def test_mismatched_torrent_snapshots_prevent_deletion(runtime, plugin_module, difference):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    _age_pending(plugin)
    if difference == "raw_has_extra":
        plugin_module.DownloaderHelper.downloader.get_torrents.return_value = (
            [SimpleNamespace(hash="abc123"), SimpleNamespace(hash="other")], False
        )
    else:
        chain.list_torrents.return_value.append(
            SimpleNamespace(hash="other", downloader="qb")
        )

    plugin.scan_pending()

    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()
    assert len(plugin.get_data("pending")) == 1


def test_rtorrent_candidate_is_retained_without_deleting_directory(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin_module.DownloaderHelper.service_type = "rtorrent"
    plugin.on_transfer_complete(event)
    _age_pending(plugin)

    plugin.scan_pending()

    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()
    assert len(plugin.get_data("pending")) == 1
    assert "仅支持 qBittorrent 和 Transmission" in plugin.get_data("pending")[0]["reason"]


def test_new_overlapping_torrent_after_first_inspection_blocks_removal(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    _age_pending(plugin)
    original_inspect = plugin_module.inspect_candidate
    downloader = plugin_module.DownloaderHelper.downloader
    first_inspection = False

    def inspect_then_add_cross_seed(**arguments):
        nonlocal first_inspection
        result = original_inspect(**arguments)
        if not first_inspection:
            assert result.ready, result.reason
            first_inspection = True
            candidate = chain.list_torrents.return_value[0]
            chain.list_torrents.return_value.append(SimpleNamespace(
                hash="cross-seed", downloader="qb", progress=100,
                content_path=candidate.content_path, save_path=candidate.save_path,
            ))
            downloader.get_torrents.return_value = (
                [SimpleNamespace(hash="abc123"), SimpleNamespace(hash="cross-seed")], False
            )
        return result

    plugin_module.inspect_candidate = inspect_then_add_cross_seed

    plugin.scan_pending()

    assert first_inspection
    chain.remove_torrents.assert_not_called()
    assert len(plugin.get_data("pending")) == 1
    reason = plugin.get_data("pending")[0]["reason"]
    assert "共用" in reason or "变化" in reason or "不一致" in reason


def test_transfer_event_records_full_history_torrent_name_instead_of_file_name(runtime, plugin_module):
    plugin, chain, event = runtime
    history = plugin_module.DownloadHistoryOper().get_by_hash.return_value
    history.torrent_name = "Show.S01.1080p.Complete.Pack"
    event.event_data["name"] = "Show.S01E01.mkv"

    plugin.on_transfer_complete(event)

    assert plugin.get_data("pending")[0]["name"] == history.torrent_name
    assert plugin.get_page()[1]["props"]["title"] == history.torrent_name
    chain.remove_torrents.assert_not_called()


def test_page_backfills_legacy_name_without_resetting_wait_or_querying_downloaders(
    runtime, plugin_module, monkeypatch
):
    plugin, chain, _ = runtime
    last_event = 1_700_000_000
    monkeypatch.setattr(plugin_module.time, "time", lambda: last_event + 20)
    history = plugin_module.DownloadHistoryOper().get_by_hash.return_value
    history.torrent_name = "Show.S01.Complete.Pack"
    plugin.save_data("pending", [{
        "downloader": "qb", "hash": "abc123", "last_event": last_event,
        "reason": "等待整理流程结束",
    }])

    props = plugin.get_page()[1]["props"]

    assert props["title"] == history.torrent_name
    assert "qb" in props["text"] and "abc123" in props["text"]
    assert datetime.fromtimestamp(last_event).strftime("%Y-%m-%d %H:%M:%S") in props["text"]
    assert datetime.fromtimestamp(last_event + 60).strftime("%Y-%m-%d %H:%M:%S") in props["text"]
    assert plugin.get_data("pending")[0]["name"] == history.torrent_name
    assert plugin.get_data("pending")[0]["last_event"] == last_event
    chain.list_torrents.assert_not_called()
    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()
    plugin_module.DownloaderHelper.downloader.get_torrents.assert_not_called()


@pytest.mark.parametrize("reason", ["等待整理流程结束", "等待最后一次整理后缓冲时间"])
def test_page_wait_status_advances_when_grace_expires_without_a_scan(
    runtime, plugin_module, monkeypatch, reason
):
    plugin, chain, _ = runtime
    last_event = 1_700_000_000
    current = {"time": last_event + 20}
    monkeypatch.setattr(plugin_module.time, "time", lambda: current["time"])
    plugin.save_data("pending", [{
        "downloader": "qb", "hash": "abc123", "name": "Show.Complete.Pack",
        "last_event": last_event, "reason": reason,
    }])

    waiting = plugin.get_page()[1]["props"]["text"]
    assert "约 1 分钟" in waiting
    assert "等待时间已满" not in waiting

    current["time"] = last_event + 61
    ready = plugin.get_page()[1]["props"]["text"]
    assert "等待时间已满，待下一轮复核" in ready
    assert plugin.get_data("pending")[0]["last_event"] == last_event
    chain.remove_torrents.assert_not_called()


def test_another_successful_file_event_restarts_the_torrents_grace_period(
    runtime, plugin_module, monkeypatch
):
    plugin, chain, event = runtime
    start = 1_700_000_000
    current = {"time": start}
    monkeypatch.setattr(plugin_module.time, "time", lambda: current["time"])
    plugin.on_transfer_complete(event)

    current["time"] = start + 40
    plugin.on_transfer_complete(event)
    current["time"] = start + 61
    props = plugin.get_page()[1]["props"]

    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("pending")[0]["last_event"] == start + 40
    assert "约 1 分钟" in props["text"]
    assert "等待时间已满" not in props["text"]
    chain.remove_torrents.assert_not_called()


def test_scan_caches_raw_torrent_title_during_wait_and_preserves_it_in_deletion_history(
    runtime, plugin_module, monkeypatch
):
    plugin, chain, event = runtime
    start = 1_700_000_000
    current = {"time": start}
    monkeypatch.setattr(plugin_module.time, "time", lambda: current["time"])
    plugin_module.DownloadHistoryOper().get_by_hash.return_value.torrent_name = "History.Old.Pack"
    torrent = chain.list_torrents.return_value[0]
    torrent.title = "Show.S01.Complete.1080p.WEB-DL.Group"
    torrent.name = "Show"
    plugin.on_transfer_complete(event)

    plugin.scan_pending()

    assert plugin.get_data("pending")[0]["name"] == torrent.title
    assert plugin.get_data("pending")[0]["last_event"] == start
    assert plugin.get_page()[1]["props"]["title"] == torrent.title
    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()

    current["time"] = start + 61
    plugin.scan_pending()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )
    assert plugin.get_data("pending") == []
    assert plugin.get_data("history")[-1]["name"] == torrent.title
    assert plugin.get_page()[1]["props"]["title"] == torrent.title


@pytest.mark.parametrize("unavailable", ["missing", "different_downloader", "lookup_failure"])
def test_unavailable_or_mismatched_history_name_keeps_hash_visible(
    runtime, plugin_module, unavailable
):
    plugin, chain, event = runtime
    lookup = plugin_module.DownloadHistoryOper().get_by_hash
    if unavailable == "missing":
        lookup.return_value = None
    elif unavailable == "different_downloader":
        lookup.return_value = SimpleNamespace(
            downloader="another-qb", torrent_name="Wrong.Downloaders.Pack"
        )
    else:
        lookup.side_effect = RuntimeError("history temporarily unavailable")
    event.event_data["name"] = "Only.One.Episode.mkv"

    plugin.on_transfer_complete(event)
    props = plugin.get_page()[1]["props"]

    assert props["title"] == "名称暂不可用"
    assert "qb" in props["text"] and "abc123" in props["text"]
    assert "Wrong.Downloaders.Pack" not in str(props)
    assert "Only.One.Episode.mkv" not in str(props)
    chain.list_torrents.assert_not_called()
    chain.remove_torrents.assert_not_called()
