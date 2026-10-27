"""校验与 CRC 算法：纯 Python、可流式累加、不依赖 Qt。

CRC 参数采用 Rocksoft/RevEng 模型（width、poly、init、refin、refout、xorout），
同时支持反射与非反射多项式；所有结果按位宽掩码。累加器只保留少量状态，因此
文件校验可以按块读取，不需要把整个文件载入内存。

`source_text(name)` 返回本程序对应算法的独立 Python 实现，供用户移植参考，
不是原 PortHelper 程序的源码。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CRC_PRESETS",
    "ChecksumResult",
    "CrcAccumulator",
    "CrcParams",
    "IntelHexAccumulator",
    "ParityAccumulator",
    "RESULT_NAMES",
    "SumAccumulator",
    "TcpIpAccumulator",
    "XorAccumulator",
    "compute_all",
    "create_accumulators",
    "format_value",
    "source_text",
]

_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BIT_COUNTS = tuple(bin(value).count("1") for value in range(256))


@dataclass(frozen=True)
class CrcParams:
    """一个 CRC 变体的完整参数（Rocksoft 模型）。"""

    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int

    def __post_init__(self) -> None:
        if self.width not in (8, 16, 32):
            raise ValueError(f"CRC 位宽只支持 8/16/32，收到 {self.width}")
        limit = (1 << self.width) - 1
        for field_name in ("poly", "init", "xorout"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"CRC 参数 {field_name} 必须是整数")
            if not 0 <= value <= limit:
                raise ValueError(
                    f"CRC 参数 {field_name}=0x{value:X} 超出 {self.width} 位范围"
                )


# 预设顺序即界面结果表的显示顺序。
CRC_PRESETS: dict[str, CrcParams] = {
    "CRC-8/MAXIM": CrcParams(8, 0x31, 0x00, True, True, 0x00),
    "CRC-8/SAE-J1850": CrcParams(8, 0x1D, 0xFF, False, False, 0xFF),
    "CRC-8/BLUETOOTH": CrcParams(8, 0xA7, 0x00, True, True, 0x00),
    "CRC-16/MODBUS": CrcParams(16, 0x8005, 0xFFFF, True, True, 0x0000),
    "CRC-16/CCITT-FALSE": CrcParams(16, 0x1021, 0xFFFF, False, False, 0x0000),
    "CRC-16/XMODEM": CrcParams(16, 0x1021, 0x0000, False, False, 0x0000),
    "CRC-16/KERMIT": CrcParams(16, 0x1021, 0x0000, True, True, 0x0000),
    "CRC-32/ISO-HDLC": CrcParams(32, 0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF),
}

RESULT_NAMES: tuple[str, ...] = (
    "Sum-8",
    "Sum-16",
    "Sum-32",
    "XOR-8 (BCC)",
    "Parity-Odd",
    "Parity-Even",
    "Intel HEX",
    "TCP/IP",
    *CRC_PRESETS,
)


@dataclass(frozen=True)
class ChecksumResult:
    """一项校验结果；value 已按 width 掩码，未做大小端重排。"""

    name: str
    value: int
    width: int


def _as_bytes(data: bytes | bytearray | memoryview) -> bytes:
    return data if isinstance(data, bytes) else bytes(data)


def _mask_for(width: int) -> int:
    return (1 << width) - 1


def _reflect(value: int, width: int) -> int:
    """按位宽反转二进制位。"""
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def _crc_table(params: CrcParams, reflected: bool) -> tuple[int, ...]:
    """为一次 CRC 计算生成 256 项表；表随累加器创建，避免全局缓存被改坏。"""
    mask = _mask_for(params.width)
    table: list[int] = []
    if reflected:
        poly = _reflect(params.poly, params.width)
        for index in range(256):
            crc = index
            for _ in range(8):
                crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
            table.append(crc & mask)
    else:
        top_bit = 1 << (params.width - 1)
        for index in range(256):
            crc = index << (params.width - 8)
            for _ in range(8):
                if crc & top_bit:
                    crc = ((crc << 1) ^ params.poly) & mask
                else:
                    crc = (crc << 1) & mask
            table.append(crc)
    return tuple(table)


class CrcAccumulator:
    """流式 CRC：一次初始化后可以连续 `update`，`result()` 可重复调用。"""

    def __init__(self, params: CrcParams, name: str | None = None) -> None:
        if not isinstance(params, CrcParams):
            raise TypeError("params 必须是 CrcParams")
        self.params = params
        self.width = params.width
        self.name = name or f"CRC-{params.width}/自定义"
        self._mask = _mask_for(params.width)
        self._reflected = bool(params.refin)
        self._table = _crc_table(params, self._reflected)
        self.reset()

    def reset(self) -> None:
        # 反射算法使用右移寄存器，内部寄存器是规范寄存器（init）的位反转。
        init = self.params.init
        self._register = _reflect(init, self.width) if self._reflected else init

    def update(self, data: bytes | bytearray | memoryview) -> None:
        chunk = _as_bytes(data)
        if not chunk:
            return
        table = self._table
        register = self._register
        if self._reflected:
            for byte in chunk:
                register = (register >> 8) ^ table[(register ^ byte) & 0xFF]
        else:
            mask = self._mask
            shift = self.width - 8
            for byte in chunk:
                register = ((register << 8) & mask) ^ table[((register >> shift) ^ byte) & 0xFF]
        self._register = register & self._mask

    def result(self) -> int:
        if self._reflected:
            # 先还原成规范（MSB-first）寄存器，再按 refout 决定是否反转输出。
            crc = _reflect(self._register, self.width)
        else:
            crc = self._register
        if self.params.refout:
            crc = _reflect(crc, self.width)
        return (crc ^ self.params.xorout) & self._mask


class SumAccumulator:
    """累加和：所有字节求和后按 8/16/32 位取模。"""

    def __init__(self, width: int = 8) -> None:
        if width not in (8, 16, 32):
            raise ValueError(f"累加和只支持 8/16/32 位，收到 {width}")
        self.width = width
        self.name = f"Sum-{width}"
        self._mask = _mask_for(width)
        self.reset()

    def reset(self) -> None:
        self._total = 0

    def update(self, data: bytes | bytearray | memoryview) -> None:
        self._total += sum(_as_bytes(data))

    def result(self) -> int:
        return self._total & self._mask


class XorAccumulator:
    """XOR-8 / BCC：逐字节异或，结果为 8 位。"""

    width = 8
    name = "XOR-8 (BCC)"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._value = 0

    def update(self, data: bytes | bytearray | memoryview) -> None:
        value = self._value
        for byte in _as_bytes(data):
            value ^= byte
        self._value = value

    def result(self) -> int:
        return self._value & 0xFF


class ParityAccumulator:
    """奇/偶校验位：结果为 1 位。

    `odd` 表示校验位使整个数据（含校验位）中 1 的个数为奇数；
    `even` 表示 1 的个数为偶数。
    """

    width = 1
    MODES = ("odd", "even")

    def __init__(self, mode: str = "even") -> None:
        if mode not in self.MODES:
            raise ValueError(f"校验方式只支持 odd/even，收到 {mode}")
        self.mode = mode
        self.name = f"Parity-{mode.capitalize()}"
        self.reset()

    def reset(self) -> None:
        self._ones = 0

    def update(self, data: bytes | bytearray | memoryview) -> None:
        chunk = _as_bytes(data)
        self._ones += sum(_BIT_COUNTS[byte] for byte in chunk)

    def result(self) -> int:
        parity = self._ones & 1
        return parity if self.mode == "even" else parity ^ 1


class IntelHexAccumulator:
    """Intel HEX 记录校验和：所有字节之和的二进制补码（8 位）。"""

    width = 8
    name = "Intel HEX"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._total = 0

    def update(self, data: bytes | bytearray | memoryview) -> None:
        self._total += sum(_as_bytes(data))

    def result(self) -> int:
        return (-self._total) & 0xFF


class TcpIpAccumulator:
    """TCP/IP 校验和：按大端 16 位字求和、折叠进位后取反码。"""

    width = 16
    name = "TCP/IP"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._total = 0
        self._pending: int | None = None

    def update(self, data: bytes | bytearray | memoryview) -> None:
        chunk = _as_bytes(data)
        if not chunk:
            return
        offset = 0
        if self._pending is not None:
            self._total += (self._pending << 8) | chunk[0]
            self._pending = None
            offset = 1
        end = len(chunk) - ((len(chunk) - offset) % 2)
        total = self._total
        for index in range(offset, end, 2):
            total += (chunk[index] << 8) | chunk[index + 1]
        self._total = total
        if end < len(chunk):
            self._pending = chunk[-1]
        self._fold()

    def _fold(self) -> None:
        total = self._total
        while total > 0xFFFF:
            total = (total & 0xFFFF) + (total >> 16)
        self._total = total

    def result(self) -> int:
        total = self._total
        if self._pending is not None:
            total += self._pending << 8
        while total > 0xFFFF:
            total = (total & 0xFFFF) + (total >> 16)
        return (~total) & 0xFFFF


def create_accumulators() -> list[object]:
    """按结果表顺序创建一组全新的累加器，供一次性或流式计算共用。"""
    accumulators: list[object] = [
        SumAccumulator(8),
        SumAccumulator(16),
        SumAccumulator(32),
        XorAccumulator(),
        ParityAccumulator("odd"),
        ParityAccumulator("even"),
        IntelHexAccumulator(),
        TcpIpAccumulator(),
    ]
    accumulators.extend(
        CrcAccumulator(params, name) for name, params in CRC_PRESETS.items()
    )
    return accumulators


def compute_all(data: bytes, invert: bool = False) -> list[ChecksumResult]:
    """一次性计算全部校验项；`invert` 按各项位宽执行 `~value & mask`。"""
    chunk = _as_bytes(data)
    results: list[ChecksumResult] = []
    for accumulator in create_accumulators():
        accumulator.update(chunk)
        value = accumulator.result()
        if invert:
            value = ~value & _mask_for(accumulator.width)
        results.append(
            ChecksumResult(accumulator.name, value, accumulator.width)
        )
    return results


def _format_in_base(number: int, base: int) -> str:
    if number == 0:
        return "0"
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, base)
        digits.append(_DIGITS[remainder])
    return "".join(reversed(digits))


def format_value(value: int, width: int, base: int = 16) -> str:
    """按位宽掩码后格式化：二进制/八进制/十六进制补足位数，十进制不补零。"""
    if not 2 <= base <= 36:
        raise ValueError(f"进制必须在 2–36 之间，收到 {base}")
    if width <= 0:
        raise ValueError(f"位宽必须为正数，收到 {width}")
    number = int(value) & _mask_for(width)
    if base == 10:
        return str(number)
    text = _format_in_base(number, base)
    if base == 2:
        padded = width
    elif base == 8:
        padded = (width + 2) // 3
    elif base == 16:
        padded = (width + 3) // 4
    else:
        padded = 0
    return text.rjust(padded, "0")


def _sum_source(width: int) -> str:
    mask = _mask_for(width)
    digits = width // 4
    return f'''def sum_{width}(data: bytes) -> int:
    """{width} 位累加和：所有字节相加后按 {width} 位取模。"""
    return sum(data) & 0x{mask:0{digits}X}
'''


def _parity_source(mode: str) -> str:
    odd = "True" if mode == "odd" else "False"
    return f'''def parity_{mode}(data: bytes) -> int:
    """返回校验位，使数据（含校验位）中 1 的个数为{"奇" if mode == "odd" else "偶"}数。"""
    ones = sum(bin(byte).count("1") for byte in data)
    parity = ones & 1
    return parity ^ 1 if {odd} else parity
'''


def _crc_source(name: str, params: CrcParams) -> str:
    function = name.lower().replace("-", "_").replace("/", "_")
    digits = params.width // 4
    mask = _mask_for(params.width)
    return f'''def {function}(data: bytes) -> int:
    """{name}：width={params.width}，refin={params.refin}，refout={params.refout}。"""
    width = {params.width}
    poly = 0x{params.poly:0{digits}X}
    init = 0x{params.init:0{digits}X}
    xorout = 0x{params.xorout:0{digits}X}
    refin = {params.refin}
    refout = {params.refout}
    mask = 0x{mask:0{digits}X}
    top_bit = 1 << (width - 1)
    crc = init
    for byte in data:
        if refin:
            byte = int(f"{{byte:08b}}"[::-1], 2)
        crc ^= (byte << (width - 8)) & mask
        for _ in range(8):
            if crc & top_bit:
                crc = ((crc << 1) ^ poly) & mask
            else:
                crc = (crc << 1) & mask
    if refout:
        crc = int(f"{{crc:0{{width}}b}}"[::-1], 2)
    return (crc ^ xorout) & mask
'''


_SOURCE_TEXT: dict[str, str] = {
    "Sum-8": _sum_source(8),
    "Sum-16": _sum_source(16),
    "Sum-32": _sum_source(32),
    "XOR-8 (BCC)": '''def xor_8_bcc(data: bytes) -> int:
    """XOR-8 / BCC：逐字节异或。"""
    value = 0
    for byte in data:
        value ^= byte
    return value & 0xFF
''',
    "Parity-Odd": _parity_source("odd"),
    "Parity-Even": _parity_source("even"),
    "Intel HEX": '''def intel_hex_checksum(data: bytes) -> int:
    """Intel HEX：所有字节之和的二进制补码，使整条记录求和为 0。"""
    return (-sum(data)) & 0xFF
''',
    "TCP/IP": '''def tcp_ip_checksum(data: bytes) -> int:
    """TCP/IP：大端 16 位字求和、折叠进位后取反码；奇数长度补 0x00。"""
    if len(data) % 2:
        data = data + b"\\x00"
    total = 0
    for index in range(0, len(data), 2):
        total += (data[index] << 8) | data[index + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF
''',
}
_SOURCE_TEXT.update(
    {name: _crc_source(name, params) for name, params in CRC_PRESETS.items()}
)


def source_text(name: str) -> str:
    """返回指定校验算法的 Python 实现片段；未知名称抛出 KeyError。"""
    try:
        return _SOURCE_TEXT[name]
    except KeyError:
        raise KeyError(f"没有算法“{name}”的源码示例") from None
