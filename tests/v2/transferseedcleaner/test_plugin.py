"""Exercise the V2 cleanup flow without a running MoviePilot or downloader."""

import copy
import importlib.util
import inspect
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    module("fastapi", Depends=lambda dependency: SimpleNamespace(dependency=dependency))
    module("app", package=True)
    module("app.core", package=True)
    module("app.core.event", Event=Event, eventmanager=EventManager())
    module("app.core.config", settings=SimpleNamespace(RMT_MEDIAEXT={".mkv"}))
    module("app.db", package=True)
    module("app.db.downloadhistory_oper", DownloadHistoryOper=object)
    module("app.db.transferhistory_oper", TransferHistoryOper=object)
    module("app.db.user_oper", get_current_active_superuser=lambda: None)
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
        "enabled": True, "dry_run": False, "downloaders": ["qb"]
    })
    plugin.chain = chain
    event = plugin_module.Event({
        "downloader": "qb",
        "download_hash": "abc123",
        "transferinfo": SimpleNamespace(success=True),
    })
    return plugin, chain, event


def _nodes(nodes, component=None):
    """Traverse the MoviePilot component tree without relying on page positions."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if component is None or node.get("component") == component:
            yield node
        content = node.get("content", [])
        if isinstance(content, list):
            yield from _nodes(content, component)


def _page_text(page):
    return "\n".join(
        str(node.get("text", "")) + " " + str(node.get("props", {}).get("text", ""))
        for node in _nodes(page)
    )


def _component_text(page, component):
    return [
        str(node.get("text", node.get("props", {}).get("text", "")))
        for node in _nodes(page, component)
    ]


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

    plugin.scan_pending()

    chain.remove_torrents.assert_not_called()
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("pending")[0]["reason"] == "预览：符合删除条件"


def test_missing_target_setting_is_off_by_default_and_requires_true(runtime):
    plugin, _, _ = runtime
    form, defaults = plugin.get_form()
    switches = [node["props"] for node in _nodes(form, "VSwitch")]
    assert defaults["allow_missing_targets"] is False
    assert plugin._allow_missing_targets is False
    setting = next(node for node in switches if node["model"] == "allow_missing_targets")
    assert setting["label"] == "允许整理目标缺失时清理"
    assert "已丢失" in setting["hint"]
    plugin.init_plugin({"allow_missing_targets": "false"})
    assert plugin._allow_missing_targets is False


@pytest.mark.parametrize("allow_missing", [False, True])
@pytest.mark.parametrize("preview", [False, True])
def test_missing_target_option_controls_cleanup_and_is_recorded(runtime, plugin_module, allow_missing, preview):
    plugin, chain, event = runtime
    destination = Path(plugin_module.TransferHistoryOper().list_by_hash.return_value[0].dest)
    destination.rename(destination.with_name("MovedShow.mkv"))
    plugin.init_plugin({
        "enabled": True, "dry_run": preview, "downloaders": ["qb"],
        "allow_missing_targets": allow_missing,
    })
    plugin.on_transfer_complete(event)

    result = plugin.run_now()

    if allow_missing and not preview:
        chain.remove_torrents.assert_called_once_with(hashs="abc123", delete_file=True, downloader="qb")
        assert result["deleted"] == 1
        record = plugin.get_data("history")[-1]
        assert record["ignored_missing_targets"] == [str(destination)]
        assert "忽略 1 个缺失整理目标" in record["message"]
        assert str(destination) in _page_text(plugin.get_page())
    else:
        chain.remove_torrents.assert_not_called()
        reason = plugin.get_data("pending")[0]["reason"]
        if allow_missing:
            assert result["ready"] == 1
            assert "已按设置忽略整理目标缺失" in reason
            assert "可清理（预览）" in _page_text(plugin.get_page())
            assert str(destination) in _page_text(plugin.get_page())
        else:
            assert result["ready"] == 0
            assert "整理目标缺失" in reason
        assert result["deleted"] == 0


def test_missing_target_option_is_rechecked_immediately_before_deletion(runtime, plugin_module, monkeypatch):
    plugin, chain, event = runtime
    Path(plugin_module.TransferHistoryOper().list_by_hash.return_value[0].dest).unlink()
    plugin.init_plugin({
        "enabled": True, "dry_run": False, "downloaders": ["qb"], "allow_missing_targets": True,
    })
    plugin.on_transfer_complete(event)
    original = plugin._inspect_pending
    calls = 0

    def disable_after_final_inspection(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            assert result.ready and result.missing_targets
            plugin.init_plugin({"enabled": True, "dry_run": False, "downloaders": ["qb"]})
        return result

    monkeypatch.setattr(plugin, "_inspect_pending", disable_after_final_inspection)
    result = plugin.run_now()

    assert calls == 2
    assert result["deleted"] == 0
    assert "清理已关闭" in plugin.get_data("pending")[0]["reason"]
    chain.remove_torrents.assert_not_called()


def test_ready_candidate_removes_exact_task_and_data_once(runtime):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)

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
        "enabled": True, "dry_run": False,
        "downloaders": ["qb", "tr"],
    })
    plugin.on_transfer_complete(qb_event)
    plugin.on_transfer_complete(plugin_module.Event({
        "downloader": "tr", "download_hash": tr_hash,
        "transferinfo": SimpleNamespace(success=True),
    }))
    pending = plugin.get_data("pending")
    assert len(pending) == 2
    plugin.init_plugin({
        "enabled": True, "dry_run": False,
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

    plugin.scan_pending()

    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()
    assert len(plugin.get_data("pending")) == 1
    assert "仅支持 qBittorrent 和 Transmission" in plugin.get_data("pending")[0]["reason"]


def test_shared_directory_with_independent_files_cleans_only_candidate(runtime, plugin_module):
    plugin, chain, event = runtime
    candidate = chain.list_torrents.return_value[0]
    source = Path(candidate.save_path) / "Show.mkv"
    other_file = source.with_name("OtherShow.mkv")
    other_file.write_bytes(b"other torrent data")
    destination = Path(plugin_module.TransferHistoryOper().list_by_hash.return_value[0].dest)
    chain.list_torrents.return_value.append(SimpleNamespace(
        hash="other", downloader="qb", progress=100, title="OtherShow",
        content_path=candidate.content_path, save_path=candidate.save_path,
    ))
    plugin_module.DownloaderHelper.downloader.get_torrents.return_value = (
        [SimpleNamespace(hash="abc123"), SimpleNamespace(hash="other")], False
    )
    chain.torrent_files.side_effect = lambda tid, downloader: [
        SimpleNamespace(name="Show.mkv" if tid == "abc123" else "OtherShow.mkv", selected=True)
    ]

    def remove_candidate(**kwargs):
        assert kwargs == {"hashs": "abc123", "delete_file": True, "downloader": "qb"}
        source.unlink()
        return True

    chain.remove_torrents.side_effect = remove_candidate
    plugin.on_transfer_complete(event)

    plugin.scan_pending()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )
    assert not source.exists()
    assert other_file.read_bytes() == b"other torrent data"
    assert destination.read_bytes() == b"downloaded video"
    assert plugin.get_data("pending") == []
    assert plugin.get_data("history")[-1]["action"] == "deleted"
    assert [call.kwargs["tid"] for call in chain.torrent_files.call_args_list].count("other") == 2


def test_shared_directory_file_list_is_refetched_before_deletion(runtime, plugin_module):
    plugin, chain, event = runtime
    candidate = chain.list_torrents.return_value[0]
    chain.list_torrents.return_value.append(SimpleNamespace(
        hash="other", downloader="qb", progress=100, title="Newly shared torrent",
        content_path=candidate.content_path, save_path=candidate.save_path,
    ))
    plugin_module.DownloaderHelper.downloader.get_torrents.return_value = (
        [SimpleNamespace(hash="abc123"), SimpleNamespace(hash="other")], False
    )
    other_requests = 0

    def file_list(tid, downloader):
        nonlocal other_requests
        name = "Show.mkv"
        if tid == "other":
            other_requests += 1
            if other_requests == 1:
                name = "OtherShow.mkv"
        return [SimpleNamespace(name=name, selected=True)]

    chain.torrent_files.side_effect = file_list
    plugin.on_transfer_complete(event)

    plugin.scan_pending()

    assert other_requests == 2
    chain.remove_torrents.assert_not_called()
    reason = plugin.get_data("pending")[0]["reason"]
    assert "共用下载数据" in reason
    assert "Newly shared torrent" in reason
    assert "Show.mkv" in reason


def test_new_overlapping_torrent_after_first_inspection_blocks_removal(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
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


def test_form_exposes_only_automatic_cleanup_interval_and_uses_it_for_service(runtime):
    plugin, _, _ = runtime
    plugin.init_plugin({"enabled": True, "interval_minutes": 7, "wait_minutes": 60})

    form, defaults = plugin.get_form()
    fields = [node["props"] for node in _nodes(form, "VTextField")]

    assert [(field["model"], field["label"]) for field in fields] == [
        ("interval_minutes", "自动清理间隔（分钟）")
    ]
    assert defaults["interval_minutes"] == 10
    assert "wait_minutes" not in defaults
    assert plugin.get_service()[0]["trigger"].kwargs == {"minutes": 7}


def test_legacy_wait_minutes_does_not_delay_fresh_event_until_next_scan(runtime):
    plugin, chain, event = runtime
    plugin.init_plugin({
        "enabled": True, "dry_run": False, "downloaders": ["qb"], "wait_minutes": 1440,
    })

    plugin.on_transfer_complete(event)
    chain.remove_torrents.assert_not_called()
    plugin.scan_pending()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )


def test_new_transfer_event_during_inspection_invalidates_scan(runtime, plugin_module, monkeypatch):
    plugin, chain, event = runtime
    # Real plugin storage serializes records: the scan and a new event read separate objects.
    monkeypatch.setattr(plugin, "get_data", lambda key: copy.deepcopy(plugin._data.get(key)))
    monkeypatch.setattr(plugin, "save_data", lambda key, value: plugin._data.update({key: copy.deepcopy(value)}))
    current = {"time": 1_700_000_000.0}
    monkeypatch.setattr(plugin_module.time, "time", lambda: current["time"])
    plugin.on_transfer_complete(event)
    original_inspect = plugin_module.inspect_candidate
    received_new_event = False

    def inspect_then_receive_another_event(**arguments):
        nonlocal received_new_event
        result = original_inspect(**arguments)
        if not received_new_event:
            assert result.ready, result.reason
            received_new_event = True
            current["time"] += 1
            plugin.on_transfer_complete(event)
        return result

    monkeypatch.setattr(plugin_module, "inspect_candidate", inspect_then_receive_another_event)
    plugin.scan_pending()

    assert received_new_event
    assert plugin.get_data("pending")[0]["last_event"] == current["time"]
    chain.remove_torrents.assert_not_called()

    plugin.scan_pending()
    chain.remove_torrents.assert_called_once()


def test_multifile_torrent_waits_until_every_file_is_organized(runtime, plugin_module):
    plugin, chain, event = runtime
    first_record = plugin_module.TransferHistoryOper().list_by_hash.return_value[0]
    second_source = Path(first_record.src).with_name("Show.S01E02.mkv")
    second_source.write_bytes(b"second episode")
    chain.torrent_files.return_value.append(SimpleNamespace(
        name=second_source.name, selected=True, priority=1,
    ))
    plugin_module.DownloadHistoryOper().get_files_by_hash.return_value.append(SimpleNamespace(
        filepath=second_source.name, fullpath=str(second_source), downloader="qb", state=1,
    ))
    plugin.on_transfer_complete(event)

    plugin.scan_pending()

    chain.remove_torrents.assert_not_called()
    assert plugin.get_data("pending")[0]["reason"] == "仍有文件没有整理记录"
    assert "未整理完成" in _page_text(plugin.get_page())

    second_destination = Path(first_record.dest).with_name(second_source.name)
    second_destination.write_bytes(second_source.read_bytes())
    plugin_module.TransferHistoryOper().list_by_hash.return_value.append(SimpleNamespace(
        id=2, src=str(second_source), dest=str(second_destination), downloader="qb",
        status=True, mode="copy",
    ))
    plugin.on_transfer_complete(event)
    plugin.scan_pending()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )


def _add_old_unselected_episode(chain, plugin_module):
    """Keep the old full-pack history after qBittorrent skips an absent episode."""
    download_oper = plugin_module.DownloadHistoryOper()
    first_file = download_oper.get_files_by_hash.return_value[0]
    skipped_source = Path(first_file.fullpath).with_name("Show.S01E02.mkv")
    assert not skipped_source.exists()
    # qBittorrent uses priority instead of a separate selected field.
    skipped_file = SimpleNamespace(name=skipped_source.name, priority=0, progress=0)
    chain.torrent_files.return_value.append(skipped_file)
    download_oper.get_files_by_hash.return_value.append(SimpleNamespace(
        filepath=skipped_source.name, fullpath=str(skipped_source), downloader="qb", state=0,
    ))
    return skipped_file


@pytest.mark.parametrize("entrypoint", ["scan_pending", "run_now"])
@pytest.mark.parametrize("preview", [False, True])
def test_single_selected_episode_cleans_up_despite_old_unselected_file_record(
    runtime, plugin_module, entrypoint, preview
):
    plugin, chain, event = runtime
    _add_old_unselected_episode(chain, plugin_module)
    plugin._dry_run = preview
    plugin.on_transfer_complete(event)

    result = getattr(plugin, entrypoint)()

    assert result["success"] is True
    assert result["checked"] == 1
    assert result["deleted"] == (0 if preview else 1)
    assert result["ready"] == (1 if preview else 0)
    assert result["retained"] == (1 if preview else 0)
    if preview:
        chain.remove_torrents.assert_not_called()
        assert plugin.get_data("pending")[0]["reason"] == "预览：符合删除条件"
        assert not plugin.get_data("history")
    else:
        chain.remove_torrents.assert_called_once_with(
            hashs="abc123", delete_file=True, downloader="qb"
        )
        assert plugin.get_data("pending") == []
        assert plugin.get_data("history")[0]["action"] == "deleted"


@pytest.mark.parametrize("entrypoint", ["scan_pending", "run_now"])
@pytest.mark.parametrize("stage", ["snapshot", "file_list"])
def test_reselected_episode_before_delete_retains_old_full_pack(
    runtime, plugin_module, entrypoint, stage
):
    plugin, chain, event = runtime
    skipped_file = _add_old_unselected_episode(chain, plugin_module)
    query = chain.list_torrents if stage == "snapshot" else chain.torrent_files

    def reselect_before_second_query(*args, **kwargs):
        if query.call_count == 2:
            skipped_file.priority = 1
        return query.return_value

    query.side_effect = reselect_before_second_query
    plugin.on_transfer_complete(event)

    result = getattr(plugin, entrypoint)()

    assert query.call_count == 2
    assert skipped_file.priority == 1
    assert result["success"] is True
    assert result["checked"] == 1
    assert result["deleted"] == 0
    assert result["ready"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    assert not plugin.get_data("history")
    chain.remove_torrents.assert_not_called()


def test_transfer_event_records_full_history_torrent_name_instead_of_file_name(runtime, plugin_module):
    plugin, chain, event = runtime
    history = plugin_module.DownloadHistoryOper().get_by_hash.return_value
    history.torrent_name = "Show.S01.1080p.Complete.Pack"
    event.event_data["name"] = "Show.S01E01.mkv"

    plugin.on_transfer_complete(event)

    assert plugin.get_data("pending")[0]["name"] == history.torrent_name
    assert history.torrent_name in _component_text(plugin.get_page(), "VListItemTitle")
    chain.remove_torrents.assert_not_called()


def test_page_backfills_legacy_name_without_resetting_event_or_querying_downloaders(runtime, plugin_module):
    plugin, chain, _ = runtime
    last_event = 1_700_000_000
    history = plugin_module.DownloadHistoryOper().get_by_hash.return_value
    history.torrent_name = "Show.S01.Complete.Pack"
    plugin.save_data("pending", [{
        "downloader": "qb", "hash": "abc123", "last_event": last_event,
        "reason": "等待整理流程结束",
    }])

    page = plugin.get_page()
    text = _page_text(page)

    assert history.torrent_name in _component_text(page, "VListItemTitle")
    assert "qb" in text and "abc123" in text
    assert datetime.fromtimestamp(last_event).strftime("%Y-%m-%d %H:%M:%S") in text
    assert plugin.get_data("pending")[0]["name"] == history.torrent_name
    assert plugin.get_data("pending")[0]["last_event"] == last_event
    chain.list_torrents.assert_not_called()
    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()
    plugin_module.DownloaderHelper.downloader.get_torrents.assert_not_called()


@pytest.mark.parametrize("reason", [
    "等待整理流程结束", "等待最后一次整理后缓冲时间", "等待整理后缓冲时间",
    "已收到整理成功记录，等待定时复核",
])
def test_legacy_wait_reason_displays_pending_without_countdown(runtime, reason):
    plugin, chain, _ = runtime
    plugin.save_data("pending", [{
        "downloader": "qb", "hash": "abc123", "name": "Show.Complete.Pack",
        "last_event": time.time(), "reason": reason,
    }])

    text = _page_text(plugin.get_page())

    assert "待检查" in text
    assert reason not in text
    assert "最早可复核" not in text
    assert "缓冲" not in text
    assert "剩余约" not in text
    chain.remove_torrents.assert_not_called()


def test_scan_caches_raw_torrent_title_and_preserves_both_names_in_deletion_history(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin._dry_run = True
    plugin_module.DownloadHistoryOper().get_by_hash.return_value.torrent_name = "History.Old.Pack"
    record = plugin_module.TransferHistoryOper().list_by_hash.return_value[0]
    final_name = "示例剧 - S01E01 - 第一集.mkv"
    final_destination = Path(record.dest).with_name(final_name)
    Path(record.dest).rename(final_destination)
    record.dest = str(final_destination)
    torrent = chain.list_torrents.return_value[0]
    torrent.title = "Show.S01.Complete.1080p.WEB-DL.Group"
    torrent.name = "Show"
    plugin.on_transfer_complete(event)

    plugin.scan_pending()

    pending = plugin.get_data("pending")[0]
    assert pending["name"] == torrent.title
    assert pending["organized_names"] == [final_name]
    assert torrent.title in _component_text(plugin.get_page(), "VListItemTitle")
    assert "整理后：" + final_name in _component_text(plugin.get_page(), "VListItemSubtitle")
    assert "可清理（预览）" in _page_text(plugin.get_page())
    chain.remove_torrents.assert_not_called()

    plugin._dry_run = False
    plugin.scan_pending()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )
    assert plugin.get_data("pending") == []
    history = plugin.get_data("history")[-1]
    assert history["name"] == torrent.title
    assert history["organized_names"] == [final_name]
    assert torrent.title in _component_text(plugin.get_page(), "VListItemTitle")
    assert "整理后：" + final_name in _component_text(plugin.get_page(), "VListItemSubtitle")


@pytest.mark.parametrize("unavailable", ["missing", "different_downloader", "lookup_failure"])
def test_unavailable_or_mismatched_history_name_keeps_hash_visible(runtime, plugin_module, unavailable):
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
    page = plugin.get_page()
    text = _page_text(page)

    assert "名称暂不可用" in _component_text(page, "VListItemTitle")
    assert "qb" in text and "abc123" in text
    assert "Wrong.Downloaders.Pack" not in str(page)
    assert "Only.One.Episode.mkv" not in str(page)
    chain.list_torrents.assert_not_called()
    chain.remove_torrents.assert_not_called()


def test_subtitle_uses_deduplicated_final_filenames_for_multiple_episodes(runtime, plugin_module):
    plugin, _, event = runtime
    records = plugin_module.TransferHistoryOper().list_by_hash.return_value
    records[:] = [
        SimpleNamespace(id=1, src="/source/E01.mkv", dest="/library/剧集 - S01E01.mkv",
                        downloader="qb", status=True, download_hash="abc123"),
        SimpleNamespace(id=2, src="/source/E02.mkv", dest="/library/剧集 - S01E02.mkv",
                        downloader="qb", status=True, download_hash="ABC123"),
        SimpleNamespace(id=3, src="/source/E01.mkv", dest="/library/剧集 - S01E01.mkv",
                        downloader="qb", status=True, download_hash="abc123"),
    ]
    plugin.on_transfer_complete(event)

    page = plugin.get_page()

    names = plugin.get_data("pending")[0]["organized_names"]
    assert set(names) == {"剧集 - S01E01.mkv", "剧集 - S01E02.mkv"}
    assert len(names) == 2
    assert any(text.startswith("整理后：") and "共 2 个已整理文件" in text
               for text in _component_text(page, "VListItemSubtitle"))


def test_organized_names_exclude_other_task_and_latest_failed_transfer(runtime, plugin_module):
    plugin, _, event = runtime
    records = plugin_module.TransferHistoryOper().list_by_hash.return_value
    records[:] = [
        SimpleNamespace(id=1, src="/source/E01.mkv", dest="/library/stale-success.mkv",
                        downloader="qb", status=True, download_hash="abc123"),
        SimpleNamespace(id=2, src="/source/E01.mkv", dest="/library/latest-failure.mkv",
                        downloader="qb", status=False, download_hash="abc123"),
        SimpleNamespace(id=3, src="/source/E02.mkv", dest="/library/wrong-downloader.mkv",
                        downloader="other-qb", status=True, download_hash="abc123"),
        SimpleNamespace(id=4, src="/source/E03.mkv", dest="/library/wrong-hash.mkv",
                        downloader="qb", status=True, download_hash="another-hash"),
    ]
    plugin.on_transfer_complete(event)

    page = plugin.get_page()

    assert plugin.get_data("pending")[0]["organized_names"] == []
    assert "暂无成功整理名称" in _page_text(page)
    for name in ["stale-success", "latest-failure", "wrong-downloader", "wrong-hash"]:
        assert name not in str(page)


def test_successful_empty_name_lookup_clears_stale_cache_but_exception_preserves_it(runtime, plugin_module):
    plugin, _, event = runtime
    plugin.on_transfer_complete(event)
    plugin.get_page()
    assert plugin.get_data("pending")[0]["organized_names"] == ["Show.mkv"]
    lookup = plugin_module.TransferHistoryOper().list_by_hash
    lookup.side_effect = RuntimeError("history temporarily unavailable")

    page = plugin.get_page()
    assert "整理后：Show.mkv" in _component_text(page, "VListItemSubtitle")
    assert plugin.get_data("pending")[0]["organized_names"] == ["Show.mkv"]

    lookup.side_effect = None
    lookup.return_value = []
    page = plugin.get_page()
    assert plugin.get_data("pending")[0]["organized_names"] == []
    assert "暂无成功整理名称" in _page_text(page)


def test_record_page_shows_every_row_in_direct_neutral_lists_without_groups(runtime):
    plugin, _, _ = runtime
    plugin.save_data("pending", [{
        "downloader": "qb", "hash": f"pending-{index}", "name": f"Complete.Torrent.Name.{index}",
        "last_event": time.time(), "reason": "待检查",
    } for index in range(12)])
    plugin.save_data("history", [{
        "downloader": "qb", "hash": f"history-{index}", "name": f"Deleted.Torrent.Name.{index}",
        "time": "2026-10-14 12:00:00", "action": "deleted", "message": "已删除下载器任务及下载文件",
        "organized_names": [f"剧集 - S01E{index + 1:02}.mkv"],
    } for index in range(8)])

    page = plugin.get_page()

    assert not list(_nodes(page, "VAlert"))
    lists = list(_nodes(page, "VList"))
    assert len(lists) == 2
    assert sorted(len(list(_nodes([record_list], "VListItem"))) for record_list in lists) == [8, 12]
    titles = _component_text(page, "VListItemTitle")
    assert len(titles) == 20
    assert set(titles) == {
        *(f"Complete.Torrent.Name.{index}" for index in range(12)),
        *(f"Deleted.Torrent.Name.{index}" for index in range(8)),
    }
    assert not any(str(node.get("component", "")).startswith("VExpansionPanel") for node in _nodes(page))
    assert "第 1 组" not in _page_text(page)


def _run_button(plugin):
    buttons = [node for node in _nodes(plugin.get_page(), "VBtn")
               if node.get("text", node.get("props", {}).get("text")) == "立即执行"]
    assert len(buttons) == 1
    return buttons[0]


def test_run_button_calls_only_the_manual_post_endpoint(runtime):
    plugin, _, event = runtime
    plugin.on_transfer_complete(event)

    button = _run_button(plugin)
    click = button["events"]["click"]
    endpoints = [endpoint for endpoint in plugin.get_api() if endpoint["path"] == "/run_now"]

    assert button.get("props", {}).get("disabled", False) is False
    assert click["api"] == "plugin/TransferSeedCleaner/run_now"
    assert click["method"].lower() == "post"
    assert "apikey" not in str(button).lower()
    assert len(endpoints) == 1
    assert [method.upper() for method in endpoints[0]["methods"]] == ["POST"]
    assert endpoints[0]["endpoint"] == plugin.run_now
    assert endpoints[0]["auth"] == "bear"
    assert [dependency.dependency for dependency in endpoints[0]["dependencies"]] == [
        sys.modules["app.db.user_oper"].get_current_active_superuser
    ]
    assert not inspect.signature(plugin.run_now).parameters


@pytest.mark.parametrize("unavailable", ["disabled", "no_selection", "empty", "unselected", "busy"])
def test_manual_button_is_disabled_when_it_cannot_run(runtime, unavailable):
    plugin, _, event = runtime
    if unavailable != "empty":
        plugin.on_transfer_complete(event)
    if unavailable == "disabled":
        plugin._enabled = False
    elif unavailable == "no_selection":
        plugin._downloaders = frozenset()
    elif unavailable == "unselected":
        plugin._downloaders = frozenset({"another-downloader"})
    elif unavailable == "busy":
        plugin._scan_lock.acquire()

    try:
        assert _run_button(plugin)["props"]["disabled"] is True
    finally:
        if unavailable == "busy":
            plugin._scan_lock.release()


def test_manual_run_deletes_immediately_and_saves_visible_summary(runtime):
    plugin, chain, event = runtime
    plugin._interval_minutes = 1440
    plugin.on_transfer_complete(event)

    result = plugin.run_now()

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )
    assert result["success"] is True
    assert result["checked"] == 1
    assert result["deleted"] == 1
    assert result["retained"] == 0
    assert result["message"]
    last_run = plugin.get_data("last_run")
    assert last_run["checked"] == 1 and last_run["deleted"] == 1
    assert last_run["time"]
    assert last_run["message"] in _page_text(plugin.get_page())


def test_manual_run_respects_preview_and_reports_ready_without_deleting(runtime):
    plugin, chain, event = runtime
    plugin._dry_run = True
    plugin.on_transfer_complete(event)

    result = plugin.run_now()

    assert result["success"] is True
    assert result["checked"] == 1
    assert result["ready"] == 1
    assert result["deleted"] == 0
    assert result["retained"] == 1
    assert plugin._dry_run is True
    assert plugin.get_data("pending")[0]["reason"] == "预览：符合删除条件"
    chain.remove_torrents.assert_not_called()


def test_manual_run_retains_unorganized_files(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin_module.TransferHistoryOper().list_by_hash.return_value = []
    plugin.on_transfer_complete(event)

    result = plugin.run_now()

    assert result["success"] is True
    assert result["checked"] == 1
    assert result["ready"] == 0
    assert result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    chain.remove_torrents.assert_not_called()


@pytest.mark.parametrize("unavailable", ["disabled", "no_selection"])
def test_manual_run_does_not_bypass_disabled_or_downloader_configuration(runtime, unavailable):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    previous_run = {"time": "2026-10-14 12:00:00", "message": "previous scan"}
    plugin.save_data("last_run", previous_run.copy())
    if unavailable == "disabled":
        plugin._enabled = False
    else:
        plugin._downloaders = frozenset()

    result = plugin.run_now()

    assert result["success"] is False
    assert result["checked"] == 0
    assert result["deleted"] == 0
    assert result["message"]
    assert plugin.get_data("last_run") == previous_run
    assert len(plugin.get_data("pending")) == 1
    chain.list_torrents.assert_not_called()
    chain.remove_torrents.assert_not_called()


def test_manual_run_with_no_pending_work_records_empty_summary(runtime):
    plugin, chain, _ = runtime

    result = plugin.run_now()

    assert result["success"] is True
    assert all(result[key] == 0 for key in ("checked", "deleted", "ready", "retained"))
    assert result["message"]
    assert plugin.get_data("last_run")["time"]
    assert plugin.get_data("last_run")["message"] == result["message"]
    chain.list_torrents.assert_not_called()
    chain.remove_torrents.assert_not_called()


def test_manual_run_reports_downloader_failure_and_preserves_pending(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    plugin_module.DownloaderHelper.downloader.get_torrents.return_value = ([], True)

    result = plugin.run_now()

    assert result["success"] is False
    assert result["checked"] == 0
    assert result["deleted"] == 0
    assert result["retained"] == 1
    assert result["message"]
    assert plugin.get_data("last_run")["success"] is False
    assert plugin.get_data("last_run")["message"] == result["message"]
    assert len(plugin.get_data("pending")) == 1
    chain.remove_torrents.assert_not_called()


@pytest.mark.parametrize("first,second", [("scan_pending", "run_now"), ("run_now", "scan_pending")])
def test_manual_and_automatic_scans_share_lock_and_delete_only_once(runtime, monkeypatch, first, second):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    entered_scan = threading.Event()
    release_scan = threading.Event()
    original_snapshot = plugin._snapshot_torrents

    def blocked_snapshot():
        entered_scan.set()
        assert release_scan.wait(timeout=5), "test did not release running scan"
        return original_snapshot()

    monkeypatch.setattr(plugin, "_snapshot_torrents", blocked_snapshot)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(getattr(plugin, first))
        try:
            assert entered_scan.wait(timeout=5), "first scan never reached inventory check"
            result = getattr(plugin, second)()
            assert result["success"] is False
            assert result["checked"] == 0 and result["deleted"] == 0
            assert result["message"]
            assert plugin.get_data("last_run") is None
            chain.remove_torrents.assert_not_called()
        finally:
            release_scan.set()
        assert running.result(timeout=5)["success"] is True

    chain.remove_torrents.assert_called_once_with(
        hashs="abc123", delete_file=True, downloader="qb"
    )
    assert plugin.get_data("pending") == []
    assert plugin.get_data("last_run")["deleted"] == 1


@pytest.mark.parametrize("stage", ["snapshot", "inspection"])
@pytest.mark.parametrize("change", ["disabled", "preview"])
def test_manual_run_rechecks_config_after_final_safety_checks(runtime, monkeypatch, stage, change):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    method = "_snapshot_torrents" if stage == "snapshot" else "_inspect_pending"
    original = getattr(plugin, method)
    calls = 0

    def reconfigure_after_second_check(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            plugin.init_plugin({
                "enabled": change != "disabled",
                "dry_run": change == "preview",
                "downloaders": ["qb"],
            })
        return result

    monkeypatch.setattr(plugin, method, reconfigure_after_second_check)

    result = plugin.run_now()

    assert calls == 2
    assert result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    chain.remove_torrents.assert_not_called()


def test_preview_run_cannot_begin_deleting_if_preview_is_disabled_mid_scan(runtime, monkeypatch):
    plugin, chain, event = runtime
    plugin._dry_run = True
    plugin.on_transfer_complete(event)
    original = plugin._inspect_pending

    def turn_off_preview_after_inspection(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.ready
        plugin.init_plugin({"enabled": True, "dry_run": False, "downloaders": ["qb"]})
        return result

    monkeypatch.setattr(plugin, "_inspect_pending", turn_off_preview_after_inspection)

    result = plugin.run_now()

    assert plugin._dry_run is False
    assert result["ready"] == 1
    assert result["deleted"] == 0
    assert result["retained"] == 1
    assert plugin.get_data("pending")[0]["reason"] == "预览：符合删除条件"
    chain.remove_torrents.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["scan_pending", "run_now"])
@pytest.mark.parametrize("preview", [False, True])
def test_confirmed_missing_torrent_leaves_pending_without_deleting_files(
    runtime, plugin_module, entrypoint, preview
):
    plugin, chain, event = runtime
    plugin._dry_run = preview
    history = plugin_module.DownloadHistoryOper().get_by_hash.return_value
    history.torrent_name = "Show.S01.Complete.1080p"
    transfer = plugin_module.TransferHistoryOper().list_by_hash.return_value[0]
    source, destination = Path(transfer.src), Path(transfer.dest)
    plugin.on_transfer_complete(event)
    plugin_module.DownloaderHelper.downloader.get_torrents.return_value = ([], False)
    chain.list_torrents.return_value = []

    result = getattr(plugin, entrypoint)()

    assert result["success"] is True
    assert result["checked"] == 1
    assert result["removed"] == 1
    assert result["deleted"] == result["ready"] == result["retained"] == 0
    assert plugin.get_data("pending") == []
    assert plugin.get_data("last_run")["removed"] == 1
    record = plugin.get_data("history")[-1]
    assert record["action"] == "removed"
    assert record["message"] == "下载器任务已不存在，已移出待处理（未删除文件）"
    assert record["name"] == history.torrent_name
    assert record["organized_names"] == [destination.name]
    assert record["downloader"] == "qb" and record["hash"] == "abc123"
    assert record["message"] in _page_text(plugin.get_page())
    assert plugin_module.DownloaderHelper.downloader.get_torrents.call_count == 2
    assert chain.list_torrents.call_count == 2
    chain.torrent_files.assert_not_called()
    chain.remove_torrents.assert_not_called()
    assert source.read_bytes() == destination.read_bytes() == b"downloaded video"

    repeated = getattr(plugin, entrypoint)()
    assert repeated["removed"] == repeated["deleted"] == repeated["checked"] == 0
    assert len(plugin.get_data("history")) == 1


def test_missing_and_live_tasks_have_separate_removal_and_deletion_counts(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(plugin_module.Event({
        "downloader": "qb", "download_hash": "already-removed",
        "transferinfo": SimpleNamespace(success=True),
    }))
    plugin.on_transfer_complete(event)

    result = plugin.run_now()

    assert result["checked"] == 2
    assert result["removed"] == result["deleted"] == 1
    assert result["retained"] == 0
    assert plugin.get_data("pending") == []
    assert [item["action"] for item in plugin.get_data("history")] == ["removed", "deleted"]
    chain.remove_torrents.assert_called_once_with(hashs="abc123", delete_file=True, downloader="qb")


@pytest.mark.parametrize("unavailable", [
    "inactive", "error_flag", "exception", "no_response", "no_config",
    "no_service", "unsupported", "raw_has_task", "normalized_has_task",
])
def test_missing_task_is_retained_unless_downloader_absence_is_reliable(
    runtime, plugin_module, monkeypatch, unavailable
):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    existing_torrent = chain.list_torrents.return_value[0]
    downloader = plugin_module.DownloaderHelper.downloader
    downloader.get_torrents.return_value = ([], False)
    chain.list_torrents.return_value = []
    if unavailable == "inactive":
        downloader.is_inactive.return_value = True
    elif unavailable == "error_flag":
        downloader.get_torrents.return_value = ([], True)
    elif unavailable == "exception":
        downloader.get_torrents.side_effect = RuntimeError("downloader unavailable")
    elif unavailable == "no_response":
        downloader.get_torrents.return_value = (None, False)
    elif unavailable == "no_config":
        monkeypatch.setattr(plugin_module.DownloaderHelper, "get_configs", lambda self: {})
    elif unavailable == "no_service":
        monkeypatch.setattr(plugin_module.DownloaderHelper, "get_services", lambda self: {})
    elif unavailable == "unsupported":
        plugin_module.DownloaderHelper.service_type = "rtorrent"
    elif unavailable == "raw_has_task":
        downloader.get_torrents.return_value = ([SimpleNamespace(hash="abc123")], False)
    else:
        chain.list_torrents.return_value = [existing_torrent]

    result = plugin.run_now()

    assert result["removed"] == result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("history") is None
    chain.remove_torrents.assert_not_called()


def test_duplicate_matching_torrents_are_not_mistaken_for_absence(runtime):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    chain.list_torrents.return_value.append(copy.copy(chain.list_torrents.return_value[0]))

    result = plugin.run_now()

    assert result["removed"] == result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("history") is None
    chain.remove_torrents.assert_not_called()


def test_missing_task_that_reappears_during_confirmation_is_retained(runtime, plugin_module):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    existing_torrent = chain.list_torrents.return_value[0]
    downloader = plugin_module.DownloaderHelper.downloader
    downloader.get_torrents.side_effect = [([], False), ([SimpleNamespace(hash="abc123")], False)]
    chain.list_torrents.side_effect = [[], [existing_torrent]]

    result = plugin.run_now()

    assert downloader.get_torrents.call_count == 2
    assert result["removed"] == result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("history") is None
    chain.remove_torrents.assert_not_called()


@pytest.mark.parametrize("failure", ["error_flag", "exception", "mismatch"])
def test_missing_task_is_retained_if_confirmation_fails(runtime, plugin_module, failure):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    existing_torrent = chain.list_torrents.return_value[0]
    downloader = plugin_module.DownloaderHelper.downloader
    chain.list_torrents.return_value = []
    if failure == "error_flag":
        downloader.get_torrents.side_effect = [([], False), ([], True)]
    elif failure == "exception":
        downloader.get_torrents.side_effect = [([], False), RuntimeError("connection lost")]
    else:
        downloader.get_torrents.return_value = ([], False)
        chain.list_torrents.side_effect = [[], [existing_torrent]]

    result = plugin.run_now()

    assert downloader.get_torrents.call_count == 2
    assert result["removed"] == result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("history") is None
    chain.remove_torrents.assert_not_called()


def test_new_transfer_event_during_missing_task_confirmation_preserves_pending(
    runtime, plugin_module, monkeypatch
):
    plugin, chain, event = runtime
    monkeypatch.setattr(plugin_module.time, "time", lambda: 100.0)
    plugin.on_transfer_complete(event)
    downloader = plugin_module.DownloaderHelper.downloader
    downloader.get_torrents.return_value = ([], False)
    chain.list_torrents.return_value = []
    original_snapshot = plugin._snapshot_torrents
    snapshots = 0

    def snapshot_with_new_event():
        nonlocal snapshots
        snapshot = original_snapshot()
        snapshots += 1
        if snapshots == 2:
            monkeypatch.setattr(plugin_module.time, "time", lambda: 200.0)
            plugin.on_transfer_complete(event)
        return snapshot

    monkeypatch.setattr(plugin, "_snapshot_torrents", snapshot_with_new_event)

    result = plugin.run_now()

    assert snapshots == 2
    assert result["removed"] == result["deleted"] == 0
    assert result["retained"] == 1
    pending = plugin.get_data("pending")
    assert len(pending) == 1 and pending[0]["last_event"] == 200.0
    assert pending[0]["reason"] == plugin._waiting_reason
    assert plugin.get_data("history") is None
    chain.remove_torrents.assert_not_called()


@pytest.mark.parametrize("change", ["disabled", "unselected"])
def test_missing_task_removal_rechecks_enabled_and_selection_after_confirmation(
    runtime, plugin_module, monkeypatch, change
):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    plugin_module.DownloaderHelper.downloader.get_torrents.return_value = ([], False)
    chain.list_torrents.return_value = []
    original_snapshot = plugin._snapshot_torrents
    snapshots = 0

    def snapshot_with_reconfiguration():
        nonlocal snapshots
        snapshot = original_snapshot()
        snapshots += 1
        if snapshots == 2:
            plugin.init_plugin({
                "enabled": change != "disabled", "dry_run": False,
                "downloaders": [] if change == "unselected" else ["qb"],
            })
        return snapshot

    monkeypatch.setattr(plugin, "_snapshot_torrents", snapshot_with_reconfiguration)

    result = plugin.run_now()

    assert snapshots == 2
    assert result["removed"] == result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("history") is None
    chain.remove_torrents.assert_not_called()


def test_missing_task_identity_includes_downloader(runtime, plugin_module, monkeypatch):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    other_torrent = copy.copy(chain.list_torrents.return_value[0])
    other_torrent.downloader = "other-qb"
    chain.list_torrents.return_value = [other_torrent]
    downloader = plugin_module.DownloaderHelper.downloader
    downloader.get_torrents.return_value = ([], False)
    other_downloader = SimpleNamespace(
        is_inactive=MagicMock(return_value=False),
        get_torrents=MagicMock(return_value=([SimpleNamespace(hash="abc123")], False)),
    )
    helper = SimpleNamespace(
        get_configs=lambda: {
            "qb": SimpleNamespace(type="qbittorrent", enabled=True),
            "other-qb": SimpleNamespace(type="qbittorrent", enabled=True),
        },
        get_services=lambda: {
            "qb": SimpleNamespace(type="qbittorrent", instance=downloader),
            "other-qb": SimpleNamespace(type="qbittorrent", instance=other_downloader),
        },
    )
    monkeypatch.setattr(plugin_module, "DownloaderHelper", lambda: helper)

    result = plugin.run_now()

    assert result["removed"] == 1 and result["deleted"] == 0
    assert plugin.get_data("pending") == []
    assert plugin.get_data("history")[-1]["downloader"] == "qb"
    chain.remove_torrents.assert_not_called()


def test_pending_for_removed_config_is_retained_when_other_downloaders_are_healthy(
    runtime, plugin_module, monkeypatch
):
    plugin, chain, event = runtime
    plugin.on_transfer_complete(event)
    chain.list_torrents.return_value = []
    other_downloader = SimpleNamespace(
        is_inactive=MagicMock(return_value=False),
        get_torrents=MagicMock(return_value=([], False)),
    )
    helper = SimpleNamespace(
        get_configs=lambda: {"other-qb": SimpleNamespace(type="qbittorrent", enabled=True)},
        get_services=lambda: {"other-qb": SimpleNamespace(type="qbittorrent", instance=other_downloader)},
    )
    monkeypatch.setattr(plugin_module, "DownloaderHelper", lambda: helper)

    result = plugin.run_now()

    assert result["removed"] == result["deleted"] == 0
    assert result["retained"] == 1
    assert len(plugin.get_data("pending")) == 1
    assert plugin.get_data("history") is None
    chain.remove_torrents.assert_not_called()
