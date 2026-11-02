"""虚拟设备集合：所有硬件边界都在这里替换。"""

from __future__ import annotations

from .ble import VirtualBleBackend
from .hid import VirtualHidBackend
from .serial import VirtualSerialHub
from .spp import VirtualSppBackend


class VirtualDeviceHub:
    def __init__(self) -> None:
        self.serial = VirtualSerialHub()
        self.hid = VirtualHidBackend()
        self.ble = VirtualBleBackend()
        self.spp = VirtualSppBackend()

    def snapshot(self) -> dict:
        return {
            "serial": self.serial.snapshot(),
            "hid": self.hid.snapshot(),
            "ble": self.ble.snapshot(),
            "spp": self.spp.snapshot(),
        }
