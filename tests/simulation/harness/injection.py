"""在构造 MainWindow 之前注入所有硬件/系统边界；生产代码零改动。"""

from __future__ import annotations

from types import SimpleNamespace

from app.io.ble_worker import BleWorker
from app.io.serial_worker import SerialWorker
from app.io.spp_worker import SppWorker
from app.io.update_worker import UpdateCheckWorker, UpdateDownloadWorker
from app.services.net_tools import PingResult
from app.ui import main_window
from app.ui.pages import (
    bluetooth_page,
    dotmatrix_page,
    hid_page,
    net_client_page,
    net_server_page,
    serial_page,
    settings_page,
)

ADAPTERS = [
    {
        "name": "模拟网卡",
        "addresses": ["127.0.0.1", "192.0.2.10"],
        "netmask": "255.255.255.0",
        "gateway": "192.0.2.1",
        "mac": "00:11:22:33:44:55",
        "isup": True,
    }
]


def install_injections(monkeypatch, sim) -> None:
    hub = sim.hub

    # ---- 串口 ---------------------------------------------------------
    monkeypatch.setattr(serial_page, "list_ports", lambda: hub.serial.ports())
    monkeypatch.setattr(
        serial_page,
        "SerialWorker",
        lambda parent=None: SerialWorker(hub.serial.transport(), parent),
    )

    # ---- 蓝牙 ---------------------------------------------------------
    monkeypatch.setattr(bluetooth_page, "list_ports", lambda: hub.serial.ports())
    monkeypatch.setattr(bluetooth_page, "BleWorker", lambda: BleWorker(hub.ble))
    monkeypatch.setattr(
        bluetooth_page,
        "SppWorker",
        lambda: SppWorker(
            hub.spp,
            serial_factory=lambda: SerialWorker(hub.serial.transport()),
        ),
    )

    # ---- HID ----------------------------------------------------------
    monkeypatch.setattr(hid_page, "HidapiBackend", lambda: hub.hid)
    monkeypatch.setattr(hid_page, "list_devices", lambda: hub.hid.enumerate())

    # ---- 网络工具 -----------------------------------------------------
    monkeypatch.setattr(
        net_client_page,
        "ping_host",
        lambda host, count=4, timeout_s=15: PingResult(True, f"模拟 ping {host} 成功", host),
    )
    monkeypatch.setattr(
        net_client_page, "resolve_host", lambda host: ["127.0.0.1", "192.0.2.10"]
    )
    monkeypatch.setattr(net_client_page, "adapter_info", lambda: list(ADAPTERS))
    monkeypatch.setattr(net_server_page, "adapter_info", lambda: list(ADAPTERS))

    # ---- 点阵字体 -----------------------------------------------------
    monkeypatch.setattr(
        dotmatrix_page,
        "resolve_font_file",
        lambda family: str(sim.font_path) if sim.font_path is not None else None,
    )

    # ---- 更新 ---------------------------------------------------------
    monkeypatch.setattr(
        settings_page,
        "UpdateCheckWorker",
        lambda: UpdateCheckWorker(fetcher=sim.fetch_release),
    )
    monkeypatch.setattr(
        settings_page,
        "UpdateDownloadWorker",
        lambda release, work_dir: UpdateDownloadWorker(
            release, work_dir, runner=sim.download_release
        ),
    )
    monkeypatch.setattr(settings_page, "install_update", sim.record_install)
    monkeypatch.setattr(settings_page, "can_replace", sim.can_replace)
    monkeypatch.setattr(
        settings_page,
        "sys",
        SimpleNamespace(frozen=sim.frozen, executable=str(sim.fake_exe)),
    )
    monkeypatch.setattr(
        main_window,
        "UpdateCheckWorker",
        lambda: UpdateCheckWorker(fetcher=sim.fetch_release),
    )
    monkeypatch.setattr(main_window, "STARTUP_UPDATE_CHECK_DELAY_MS", 0)
    monkeypatch.setattr(main_window, "enable_window_mica", lambda _window: False)
