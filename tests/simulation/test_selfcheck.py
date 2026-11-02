"""负控制：故意注入故障，确认场景会失败而不是空转通过。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.ui.pages import checksum_page

from .scenarios.bluetooth import bt_04_write_notify_and_repeat
from .scenarios.serial import ser_03_send_text_hex_newline_encoding
from .scenarios.tools import chk_01_known_vectors

pytestmark = pytest.mark.harness


def test_serial_scenario_detects_lost_write(sim) -> None:
    sim.hub.serial.device("COM7").silent_write = True
    with pytest.raises(AssertionError):
        ser_03_send_text_hex_newline_encoding(sim)


def test_ble_scenario_detects_suppressed_notification(sim) -> None:
    sim.hub.ble.suppress_notifications = True
    with pytest.raises(AssertionError):
        bt_04_write_notify_and_repeat(sim)


def test_checksum_scenario_detects_wrong_result(sim) -> None:
    real = checksum_page.compute_all

    def corrupt(data):
        return [replace(result, value=result.value ^ 1) for result in real(data)]

    sim.monkeypatch.setattr(checksum_page, "compute_all", corrupt)
    with pytest.raises(AssertionError):
        chk_01_known_vectors(sim)
