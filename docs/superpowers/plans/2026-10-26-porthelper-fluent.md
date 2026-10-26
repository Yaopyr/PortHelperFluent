# PortHelper Fluent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows 11-style Python/PyQt6 replacement for PortHelper 1.8.8 covering serial, TCP/UDP client, TCP server, USB HID, checksum, encoding conversion, image/dot-matrix tools and a C51 code wizard, delivered as one `PortHelperFluent.exe`.

**Architecture:** `core/` holds Qt-free algorithms; `services/` holds paths, settings, session logs and exports; `io/` holds serial, network and HID adapters; `ui/` holds Fluent pages and shared widgets. Pages receive services/controllers, never import each other. Serial and HID use worker threads with batched rendering; network uses Qt sockets.

**Tech Stack:** Python 3.13.9, PyQt6 6.11.0, PyQt6-Fluent-Widgets 1.11.2, pyserial 3.5, psutil 7.2.1, Pillow 12.3.0, numpy 2.3.4, hidapi 0.15.0, qrcode 8.2, pytest 9.1.1, PyInstaller 6.19.0.

**Spec:** `docs/superpowers/specs/2026-10-26-porthelper-fluent-design.md`

**Working branch:** `feat/porthelper-fluent`

---

## File Map

```
main.py
app/
  core/
    encoding.py          # hex/text/base helpers, codecs, seven-segment
    settings.py          # JSON settings with defaults and atomic save
    logging_setup.py     # rotating logs, crash reports, exception hooks
    checksum.py          # sums, parity, Intel HEX, TCP/IP, CRC presets/custom
    color.py             # RGB565 / RGB332 / RGB888
    audio_tools.py       # WAV/MP3 decode, C array generation
    gif_tools.py         # GIF frame extraction and BMP export
    qr_tools.py          # QR matrix/PNG generation
    imaging.py           # image -> C/BIN/LVGL with scan/color options
    fontmatrix.py        # text/BMP -> dot matrix -> bytes/C
    c51.py               # 8051 timer/baud registers, C and Keil project
  io/
    serial_worker.py     # pyserial transport + reader thread
    net_client.py        # QTcpSocket / QUdpSocket controller
    net_server.py        # QTcpServer controller
    hid_worker.py        # hidapi backend + reader thread
  services/
    paths.py             # AppData / portable paths
    session_log.py       # bounded records, counters, TXT/CSV export
    exports.py           # unique paths, write helpers, filename sanitising
    net_tools.py         # ping, DNS, adapter info
  ui/
    main_window.py       # FluentWindow, navigation, geometry, close confirm
    theme.py             # light/dark/accent/Mica
    qt_utils.py          # safe_slot, event-loop helper
    feedback.py          # InfoBar helpers
    widgets/
      cards.py            # section card helper
      status_chip.py      # connection status chip
      empty_state.py      # empty state
      file_picker.py      # path line edit + browse + drag/drop
      log_pane.py         # batched log display + session log
      hex_editor.py       # validated hex input
      matrix_view.py      # editable dot-matrix grid
    dialogs/
      session_dialog.py   # full session table + export
      text_info_dialog.py # ping result / adapter info
      multi_send_dialog.py# 8-slot multi-send editor
    pages/
      serial_page.py
      net_client_page.py
      net_server_page.py
      hid_page.py
      checksum_page.py
      image_page.py
      dotmatrix_page.py
      c51_page.py
      settings_page.py
      placeholder.py      # only used until a page task replaces it
      convert/
        __init__.py       # exports ConvertPage
        page.py           # segmented tool shell
        base_tool.py
        data_tool.py
        sevenseg_tool.py
        palette_tool.py
        gif_tool.py
        audio_tool.py
        qr_tool.py
tests/
  conftest.py
  test_paths.py
  test_settings.py
  test_encoding_basic.py
  test_session_log.py
  test_selftest.py
  test_widgets.py
  test_main_window.py
  test_serial_worker.py
  test_serial_page.py
  test_net_client.py
  test_net_server.py
  test_hid_worker.py
  test_hid_page.py
  test_checksum.py
  test_checksum_page.py
  test_convert_core.py
  test_convert_page.py
  test_imaging.py
  test_image_page.py
  test_fontmatrix.py
  test_dotmatrix_page.py
  test_c51.py
  test_c51_page.py
  test_settings_page.py
build/
  app.spec
  build.ps1
  make_icon.py
  icon.ico
requirements.txt
pytest.ini
```

---

## Task 1: Core Foundation

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `tests/conftest.py`
- Create: `app/__init__.py`, `app/core/__init__.py`, `app/io/__init__.py`, `app/services/__init__.py`, `app/ui/__init__.py`
- Create: `app/services/paths.py`, `app/services/exports.py`
- Create: `app/core/settings.py`, `app/core/logging_setup.py`, `app/core/encoding.py`
- Create: `app/services/session_log.py`
- Create: `main.py` (selftest + placeholder launch)
- Create tests: `tests/test_paths.py`, `tests/test_settings.py`, `tests/test_encoding_basic.py`, `tests/test_session_log.py`, `tests/test_selftest.py`
- Install: `hidapi==0.15.0`, `qrcode==8.2`

**Depends on:** nothing.

**Step 1: Install the two missing runtime packages and record versions**

```bash
python -m pip install "hidapi==0.15.0" "qrcode==8.2"
python -m pip show hidapi qrcode | grep -E "^(Name|Version)"
```

Expected: both packages import; `hid` and `qrcode` are available.

Step 2: Write `requirements.txt` with exactly:

```text
PyQt6==6.11.0
PyQt6-Fluent-Widgets==1.11.2
pyserial==3.5
psutil==7.2.1
Pillow==12.3.0
numpy==2.3.4
hidapi==0.15.0
qrcode==8.2
pytest==9.1.1
pyinstaller==6.19.0
```

Step 3: Write `pytest.ini`:

```ini
[pytest]
testpaths = tests
addopts = -q
filterwarnings =
    ignore::DeprecationWarning
```

Step 4: Write `tests/conftest.py` before any Qt import:

```python
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt6")
sys.dont_write_bytecode = True

import pytest


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
```

Step 5: Implement `app/services/paths.py`.

Public interface:

```python
APP_NAME = "PortHelperFluent"

@dataclass(frozen=True)
class AppPaths:
    exe_dir: Path
    data_dir: Path
    config_file: Path
    log_dir: Path
    portable: bool

def resolve_paths(exe_path: Path | None = None, appdata: Path | None = None) -> AppPaths
def ensure_dirs(paths: AppPaths) -> None
```

Rules:
- `exe_path` defaults to `Path(sys.executable)` when frozen, otherwise the repo root (three parents up from `paths.py`).
- Portable if `exe_dir / "portable.flag"` exists; data dir is `exe_dir / "data"`, otherwise `%APPDATA% / APP_NAME`.
- `config_file = data_dir / "config.json"`, `log_dir = data_dir / "logs"`.
- `ensure_dirs` creates data and log dirs.
- Never write to `Program Files` unless portable mode explicitly requested.

Tests in `tests/test_paths.py`: portable flag creates `exe/data`; non-portable uses injected appdata; config/log paths are correct; ensure_dirs creates both.

Step 6: Implement `app/services/exports.py`:

```python
def sanitize_name(name: str, fallback: str = "data") -> str
def unique_path(path: Path) -> Path       # appends _1, _2 ... before suffix
def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None
def write_bytes_atomic(path: Path, data: bytes) -> None
```

Tests: sanitize removes `<>:"/\\|?*` and control chars, keeps Chinese; unique_path avoids collisions; atomic write leaves no `.tmp` file.

Step 7: Implement `app/core/settings.py`.

```python
DEFAULT_SETTINGS: dict[str, Any] = {
    "app.version": "1.0.0",
    "ui.theme": "auto",
    "ui.accent": "#0A84FF",
    "ui.window_geometry": "",
    "ui.max_display_lines": 20000,
    "ui.auto_scroll": True,
    "ui.confirm_exit": True,
    "serial.port": "",
    "serial.baud": 115200,
    "serial.data_bits": 8,
    "serial.stop_bits": 1,
    "serial.parity": "none",
    "serial.flow": "none",
    "serial.encoding": "utf-8",
    "serial.hex_send": False,
    "serial.hex_display": False,
    "serial.timestamp": True,
    "net.host": "127.0.0.1",
    "net.port": 8080,
    "net.udp_port": 8080,
    "net.encoding": "utf-8",
    "server.port": 8080,
    "server.max_clients": 16,
    "hid.report_id": 0,
    "hid.report_length": 64,
    "checksum.encoding": "utf-8",
    "paths.last_export": "",
}

class Settings:
    def __init__(self, path: Path)
    def load(self) -> None
    def save(self) -> None
    def get(self, key: str, default=None) -> Any
    def set(self, key: str, value: Any) -> None
    def update(self, values: dict[str, Any]) -> None
    def reset(self) -> None
    def as_dict(self) -> dict[str, Any]
    @property
    def warning(self) -> str | None
```

Rules: merge defaults with loaded JSON; preserve unknown keys; corrupt JSON is renamed to `config.bad-YYYYmmdd-HHMMSS.json`, defaults restored, `warning` set; save writes a temp file then `replace`; use `ensure_ascii=False, indent=2`.

Tests: defaults, set/get, save/reload, corrupt backup and warning, reset, unknown key preserved.

Step 8: Implement `app/core/logging_setup.py`.

```python
LOGGER_NAME = "porthelper"

def get_logger() -> logging.Logger
def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger
def write_crash_report(log_dir: Path, exc_type, exc, tb) -> Path
def install_exception_hooks(log_dir: Path, on_error=None) -> None
```

Use `RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")`, remove duplicate handlers on the `porthelper` logger, format `%(asctime)s %(levelname)s %(name)s: %(message)s`. `write_crash_report` writes `crash-YYYYmmdd-HHMMSS.txt` with traceback. `install_exception_hooks` sets `sys.excepthook` and `threading.excepthook`, writes a crash report, logs, and calls `on_error(message)` if provided.

Step 9: Implement basic `app/core/encoding.py`.

```python
CODEC_CHOICES = ("ansi", "gbk", "utf-8")

def resolve_codec(name: str) -> str       # ansi -> locale.getpreferredencoding(False)
def encode_text(text: str, codec: str) -> bytes
def decode_bytes(data: bytes, codec: str, errors: str = "replace") -> str
def parse_hex(text: str) -> bytes         # ValueError with Chinese message
def to_hex(data: bytes, sep: str = " ") -> str
```

`parse_hex` accepts `0x`, `\x`, spaces, tabs, newlines, commas, semicolons; rejects odd length and non-hex characters; error messages must name the offending character.

Tests: `parse_hex("0x01 02,0A") == b"\x01\x02\x0a"`; odd length raises; `decode_bytes(b"\xc4\xe3", "gbk") == "你"`; `resolve_codec("ansi")` is non-empty.

Step 10: Implement `app/services/session_log.py`.

```python
@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    direction: str          # "rx", "tx", "info"
    source: str
    data: bytes

class SessionLog:
    def __init__(self, max_bytes: int = 8 * 1024 * 1024, max_records: int = 100_000)
    def add(self, record: LogRecord) -> None
    def add_bytes(self, data: bytes, direction: str, source: str = "") -> LogRecord
    def clear(self) -> None
    def records(self) -> tuple[LogRecord, ...]
    def total_bytes(self, direction: str | None = None) -> int
    def export_txt(self, path: Path, codec: str = "utf-8", hex_mode: bool = False) -> None
    def export_csv(self, path: Path, codec: str = "utf-8", hex_mode: bool = False) -> None
```

Rules: drop oldest records when either cap is exceeded; counters keep lifetime totals; export TXT lines `[HH:MM:ss.zzz] [RX] source: payload`; CSV header `time,direction,source,length,hex,text`, encoding `utf-8-sig` on disk. Tests: cap drops old records but counters grow; export contains Chinese; CSV has BOM.

Step 11: Implement `main.py` with selftest and a temporary placeholder window.

CLI:
- `PortHelperFluent.exe --selftest --report <path>`: import checks, version, PyQt6 binding check, paths writable, write JSON `{ok, errors, python, qt, qfluentwidgets, version}`; exit 0 on success, 1 on failure.
- Normal launch: create QApplication, set `HighDpiScaleFactorRoundingPolicy.PassThrough`, set app name/version, `setup_logging`, `install_exception_hooks`, resolve paths, load settings, create `MainWindow` if available; before Task 2 exists, use a minimal `FluentWindow` with a `SubtitleLabel` saying the shell is being built. Task 2 replaces this placeholder with the real main window.
- Wrap startup in try/except; on failure write crash report and show `QMessageBox.critical` with a Chinese message.

Binding check must be:

```python
from PyQt6.QtWidgets import QWidget
from qfluentwidgets import FluentWindow
if not issubclass(FluentWindow, QWidget):
    raise RuntimeError("qfluentwidgets 当前不是 PyQt6 版本")
```

Step 12: Run the tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_paths.py tests/test_settings.py tests/test_encoding_basic.py tests/test_session_log.py tests/test_selftest.py -v
```

Expected: all pass; selftest reports `ok: true`.

Step 13: Commit.

```bash
git add requirements.txt pytest.ini app main.py tests
git commit -m "feat: add core foundation, settings, logging, session log and selftest"
```

---

## Task 2: Fluent UI Shell and Shared Widgets

**Files:**
- Create: `app/ui/theme.py`, `app/ui/qt_utils.py`, `app/ui/feedback.py`
- Create: `app/ui/widgets/__init__.py`, `cards.py`, `status_chip.py`, `empty_state.py`, `file_picker.py`, `log_pane.py`, `hex_editor.py`
- Create: `app/ui/dialogs/__init__.py`, `session_dialog.py`
- Create: `app/ui/pages/placeholder.py`, and placeholders `serial_page.py`, `net_client_page.py`, `net_server_page.py`, `hid_page.py`, `checksum_page.py`, `image_page.py`, `dotmatrix_page.py`, `c51_page.py`, `settings_page.py`, `convert/__init__.py`
- Create: `app/ui/main_window.py`
- Modify: `main.py` (real smoke test, real MainWindow)
- Create tests: `tests/test_widgets.py`, `tests/test_main_window.py`
- Create: `build/app.spec` minimal (optional onedir build)

**Depends on:** Task 1.

Step 1: Implement `app/ui/feedback.py` with `show_info`, `show_success`, `show_warning`, `show_error` using qfluentwidgets `InfoBar` (`InfoBarPosition.TOP_RIGHT`, 3 s info, 5 s error). If the parent is not visible, fall back to a non-blocking `QMessageBox` only for errors; info calls no-op. Tests may monkeypatch `InfoBar` and assert the call.

Step 2: Implement `app/ui/qt_utils.py`:

```python
def safe_slot(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logging.getLogger("porthelper").exception("slot failed")
            parent = args[0] if args and isinstance(args[0], QWidget) else None
            show_error(parent, "操作失败", readable_message(exc))
            return None
    return wrapper

def process_events_until(predicate, timeout_ms: int = 2000) -> bool
```

`readable_message` maps `PermissionError`, `FileNotFoundError`, `serial.SerialException`, `OSError` to Chinese sentences. `process_events_until` is used by Qt tests.

Step 3: Implement `app/ui/theme.py`:

```python
def apply_theme(settings) -> None      # auto/light/dark + accent
def enable_mica(window) -> bool        # returns False when unavailable
```

Map `auto` to `Theme.AUTO`, `light` to `Theme.LIGHT`, `dark` to `Theme.DARK`; call `setTheme` and `setThemeColor`. `enable_mica` checks Windows build >= 22000, calls `window.setMicaEffectEnabled(True)` in try/except, logs and returns False on failure.

Step 4: Implement shared widgets:

- `cards.py`: `def section_card(title: str, subtitle: str | None = None) -> tuple[CardWidget, QVBoxLayout]`; card margins 16, spacing 10, a `SubtitleLabel` title and optional `CaptionLabel` subtitle.
- `status_chip.py`: `StatusChip(QWidget)` with `set_state(state: str, text: str)`; states `idle`, `ok`, `busy`, `error`; colored dot via stylesheet, accessible text, fixed height 28.
- `empty_state.py`: `EmptyState(QWidget)` with `FluentIcon.INFO`, title, hint, centered.
- `file_picker.py`: `FilePicker(QWidget)` with line edit, browse tool button, optional save mode, `path()`, `set_path()`, `pathChanged(str)`; supports drag/drop of exactly one file.
- `hex_editor.py`: `HexLineEdit(QLineEdit)` with `hex_bytes()`, `is_valid()`, `set_hex_text()`; validates on text change using `parse_hex`; invalid state sets a red border and tooltip; `Ctrl+Enter` emits `submit`.
- `log_pane.py`: `LogPane(QWidget)` API:
  ```python
  def __init__(self, title: str, *, show_direction: bool = True, parent=None)
  def append_bytes(self, data: bytes, direction: str, source: str = "") -> None
  def append_info(self, text: str) -> None
  def set_hex_mode(self, enabled: bool) -> None
  def set_timestamp_visible(self, enabled: bool) -> None
  def set_encoding(self, codec: str) -> None
  def set_paused(self, paused: bool) -> None
  def clear(self) -> None
  def save_txt(self, path) -> None
  def save_csv(self, path) -> None
  def session(self) -> SessionLog
  ```
  Internal: a `SessionLog`, a pending record deque, a 33 ms QTimer that renders batches into a read-only `QPlainTextEdit` with `setMaximumBlockCount(settings max lines)`, auto-scroll toggle, pause switch, HEX/timestamp toggles, save/copy/clear buttons. Paused stops rendering but continues recording/counting. Changing hex mode re-renders the last 2000 records. `append_info` adds an info line and does not count as RX/TX.

Step 5: Implement `app/ui/dialogs/session_dialog.py`: a `QDialog` showing a snapshot table from a `SessionLog` (time, direction, source, length, hex, text), with `导出 CSV` and `导出 TXT`; use `QTableWidget` with row caps (last 5000) and a label explaining the cap.

Step 6: Create placeholder pages. Each file defines a page class inheriting `PlaceholderPage`, with the exact class names used by the main window: `SerialPage`, `NetClientPage`, `NetServerPage`, `HidPage`, `ChecksumPage`, `ImagePage`, `DotMatrixPage`, `C51Page`, `SettingsPage`, and `ConvertPage` in `app/ui/pages/convert/__init__.py`. Each sets `objectName` to a stable route key (`serial`, `net-client`, `net-server`, `hid`, `checksum`, `convert`, `image`, `dotmatrix`, `c51`, `settings`).

Step 7: Implement `app/ui/main_window.py`.

- `MainWindow(FluentWindow)` receives `Settings` and `AppPaths`.
- Window title `PortHelper Fluent 1.0.0`; minimum 1180×760; default 1280×820; restore `ui.window_geometry` from base64; on close save it.
- Navigation:
  - header `通信`, then Serial, Net client, Net server, HID.
  - header `工具`, then Checksum, Convert, Image, Dot matrix, C51.
  - header `其他`, then Settings.
  - Use `navigationInterface.addItemHeader` and `addSubInterface(page, icon, title)` in that order.
  - Icons: `CONNECT`, `GLOBE`, `IOT`, `DEVELOPER_TOOLS`, `CODE`, `PHOTO`, `FONT`, `SETTING`; use a helper that falls back to `FluentIcon.APPLICATION` for missing names.
- Set the first page (serial) as current; disable the navigation search line if present.
- `enable_mica(self)` in try/except; never fail startup.
- Close confirmation only when a page reports an active connection/listener; expose `has_active_work()` on pages, default False. On confirm, stop workers and save settings.
- Add `smoke_test()` method returning True after construction.

Step 8: Update `main.py`: add `--smoke-test` (create app/window, show, `QTimer.singleShot(1500, app.quit)`, exec, return 0), and import the real `MainWindow`.

Step 9: Create `build/app.spec` minimal onedir spec with `Analysis(['main.py'])`, `COLLECT`, name `PortHelperFluent`, `console=False`, and a placeholder icon path only if it exists. This is a smoke build; Task 12 finalises onefile.

Step 10: Tests.

`tests/test_widgets.py`:
- `LogPane` append RX/HEX, assert text contains `01 02`, counters == 2; pause then append, assert display unchanged but session count grows; clear resets display, session records empty.
- `FilePicker.set_path` emits signal; `HexLineEdit` rejects `"0G"` and accepts `"0A ff"`; `StatusChip.set_state("error", "失败")` exposes text.
- `test_main_window.py`: instantiate `MainWindow`, assert 10 pages in `stackedWidget`, nav route keys present, switching works, `smoke_test()` is True.

Step 11: Run tests and a smoke launch.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_widgets.py tests/test_main_window.py -v
python main.py --smoke-test
```

Expected: tests pass; smoke exits 0 within ~4 s.

Step 12: Commit.

```bash
git add app/ui main.py build tests
git commit -m "feat: add Fluent shell, navigation, shared widgets and smoke test"
```

---

## Task 3: Serial Debugger

**Files:**
- Create: `app/io/serial_worker.py`
- Create: `app/ui/dialogs/multi_send_dialog.py`
- Replace: `app/ui/pages/serial_page.py`
- Tests: `tests/test_serial_worker.py`, `tests/test_serial_page.py`

**Depends on:** Tasks 1-2.

Step 1: Define transport and worker interfaces in `app/io/serial_worker.py`:

```python
@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    serial_number: str | None

@dataclass(frozen=True)
class SerialConfig:
    port: str
    baud: int = 115200
    bytesize: int = 8
    stopbits: float = 1
    parity: str = "none"       # none/odd/even/mark/space
    flow: str = "none"         # none/rtscts/xonxoff
    encoding: str = "utf-8"

class SerialTransport(Protocol):
    def open(self, config: SerialConfig) -> None: ...
    def close(self) -> None: ...
    def read(self, size: int, timeout: float) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def set_dtr(self, value: bool) -> None: ...
    def set_rts(self, value: bool) -> None: ...
    def send_break(self, duration: float = 0.25) -> None: ...
    def modem_status(self) -> dict[str, bool]: ...

def list_ports() -> list[PortInfo]
class PyserialTransport: ...
class SerialWorker(QThread):
    opened = pyqtSignal()
    closed = pyqtSignal()
    data_received = pyqtSignal(bytes)
    error = pyqtSignal(str)
    modem_changed = pyqtSignal(dict)
    def __init__(self, transport: SerialTransport | None = None, parent=None)
    def open_port(self, config: SerialConfig) -> None
    def close_port(self) -> None
    def send(self, data: bytes) -> None
    def set_dtr(self, value: bool) -> None
    def set_rts(self, value: bool) -> None
    def send_break(self) -> None
    def is_open(self) -> bool
```

Rules: worker owns a `queue.Queue` for writes, a stop event, a 50 ms read timeout; drain writes before each read; emit modem status at most every 100 ms; on any error emit `error`, close, emit `closed`; `close_port` joins the thread within 2 s. `PyserialTransport` maps parity/flow to pyserial constants; `modem_status` returns `cts/dsr/ri/cd` with getattr fallback False.

Step 2: Write `tests/test_serial_worker.py` with a `FakeTransport` that feeds `read()` from a queue and records writes. Test:
- `open_port` emits `opened` and worker open.
- `send(b"AB")` writes `AB` and emits no error.
- fake read `b"hello"` emits `data_received`.
- fake read raises `OSError` -> `error` + `closed`.
- `close_port` closes and joins.
- `list_ports()` returns a list (may be empty) without raising.

Use `QCoreApplication` from the `qapp` fixture and `process_events_until`.

Step 3: Implement the serial page. Required controls:
- Port combo + refresh + port info line; manual COM entry allowed.
- Baud editable combo (300…921600), data bits, stop bits, parity, flow, encoding, open/close, status chip.
- DTR, RTS switches, BREAK button, CTS/DSR/RING/RLSD read-only chips.
- RX `LogPane` with HEX/timestamp/pause/clear/save; TX `QPlainTextEdit` with HEX checkbox, append CRLF/LF/CR/none, send button, repeat checkbox + interval spin, send file button, counters + reset, multi-send button, session record button.
- `Ctrl+Enter` send, `Ctrl+K` clear RX, `F5` refresh ports.
- Restore and persist serial settings via `Settings`.
- Errors via `show_error`; never raise from slots.
- `has_active_work()` returns `worker.is_open()`.

Step 4: Implement `app/ui/dialogs/multi_send_dialog.py`:
- 8 rows: enabled checkbox, HEX checkbox, line edit, send button, repeat checkbox + interval; save/load JSON; validate hex before send.
- API `MultiSendDialog(worker_provider, parent)` where `worker_provider` returns the current worker; expose `to_dict()`/`from_dict()` for tests.

Step 5: Tests `tests/test_serial_page.py`:
- Inject `FakeTransport` via `SerialPage(settings, worker_factory=...)`.
- Fill port/baud, open, feed RX, assert LogPane text and counters.
- Send text/HEX; invalid HEX shows an error and sends nothing.
- Toggle timestamp/HEX and assert display; pause/resume; clear.
- `has_active_work()` true after open.

Step 6: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_serial_worker.py tests/test_serial_page.py -v
```

Step 7: Commit.

```bash
git add app/io/serial_worker.py app/ui/pages/serial_page.py app/ui/dialogs/multi_send_dialog.py tests/test_serial_worker.py tests/test_serial_page.py
git commit -m "feat: add serial transport, worker and Fluent serial debugger"
```

---

## Task 4: Network Client and Server

**Files:**
- Create: `app/io/net_client.py`, `app/io/net_server.py`, `app/services/net_tools.py`
- Create: `app/ui/dialogs/text_info_dialog.py`
- Replace: `app/ui/pages/net_client_page.py`, `app/ui/pages/net_server_page.py`
- Tests: `tests/test_net_client.py`, `tests/test_net_server.py`, `tests/test_net_tools.py`

**Depends on:** Tasks 1-3.

Step 1: Implement `app/io/net_client.py`:

```python
class NetClientController(QObject):
    tcp_state_changed = pyqtSignal(str)      # disconnected/connecting/connected
    udp_state_changed = pyqtSignal(str)      # closed/bound
    tcp_data = pyqtSignal(bytes)
    udp_data = pyqtSignal(bytes)
    error = pyqtSignal(str)
    def connect_tcp(self, host: str, port: int, local_port: int = 0) -> None
    def disconnect_tcp(self) -> None
    def bind_udp(self, local_port: int) -> None
    def close_udp(self) -> None
    def send_tcp(self, data: bytes) -> bool
    def send_udp(self, data: bytes, host: str, port: int) -> bool
    def is_tcp_connected(self) -> bool
    def is_udp_bound(self) -> bool
```

Use `QTcpSocket`/`QUdpSocket`; handle `connected`, `disconnected`, `errorOccurred`, `readyRead`; write returns bool and emits error on -1. No threads.

Step 2: Implement `app/io/net_server.py`:

```python
@dataclass(frozen=True)
class ClientInfo:
    client_id: str
    address: str
    port: int
    connected_at: datetime
    rx_bytes: int
    tx_bytes: int

class NetServerController(QObject):
    started = pyqtSignal(int)
    stopped = pyqtSignal()
    client_connected = pyqtSignal(str)
    client_disconnected = pyqtSignal(str)
    data_received = pyqtSignal(str, bytes)
    error = pyqtSignal(str)
    def start(self, host: str, port: int, max_clients: int = 16) -> None
    def stop(self) -> None
    def send_to(self, client_id: str, data: bytes) -> bool
    def broadcast(self, data: bytes) -> int
    def clients(self) -> list[ClientInfo]
```

Track per-client buffers and counters; reject/closes clients beyond max with an error signal; clean up on stop.

Step 3: Implement `app/services/net_tools.py`:

```python
@dataclass(frozen=True)
class PingResult:
    ok: bool
    output: str
    host: str

def ping_host(host: str, count: int = 4, timeout_s: int = 15) -> PingResult
def resolve_host(host: str) -> list[str]
def adapter_info() -> list[dict]   # name, addresses, netmask, gateway, mac, isup
```

`ping_host` uses `subprocess.run(["ping", "-n", str(count), host], capture_output=True, timeout=timeout_s, creationflags=CREATE_NO_WINDOW)`; decode with `locale.getpreferredencoding(False)` and `errors="replace"`; return raw output. `resolve_host` uses `socket.getaddrinfo`, deduplicated, with a Chinese error message on failure. `adapter_info` uses psutil.net_if_addrs/net_if_stats.

Step 4: Implement `app/ui/dialogs/text_info_dialog.py`: read-only `QPlainTextEdit`, title, copy-all button, close; used for ping output, DNS results and adapter info.

Step 5: Implement `app/ui/pages/net_client_page.py`:
- Mode segmented control TCP/UDP.
- Host `LineEdit` + resolve button; destination port; TCP local port; UDP local port; connect/open and disconnect/close buttons; status chip.
- Two `LogPane`s (TCP and UDP) or one pane switched by a segmented control; persist counters.
- Send card: text editor, HEX checkbox, encoding, TCP send/UDP send, repeat + interval, file send, clear, counters, session record.
- Assist buttons: Ping (background `QThreadPool`), 域名转 IP, 网卡信息, 会话记录; show results in `TextInfoDialog`.
- Persist host/ports/encoding; errors via feedback; `Ctrl+Enter` sends for the active mode.

Step 6: Implement `app/ui/pages/net_server_page.py`:
- Host combo (`0.0.0.0` + local addresses), port, max clients spin, start/stop, status chip.
- Client `QTableWidget` (IP:port, connected time, RX, TX); selection enables send-to-selected; disconnect button.
- RX `LogPane` with source prefixes; send card with broadcast checkbox, HEX, repeat, file, counters, session record.
- On client connect/disconnect update table; on error show info; `has_active_work()` true while listening or clients exist.

Step 7: Tests:
- `test_net_client.py`: local echo `QTcpServer`; connect, send, receive; disconnect; connect refused -> error; UDP echo; port in use.
- `test_net_server.py`: start on port 0 (use `serverPort()` after start or allow port 0 and emit actual), connect two `QTcpSocket` clients, receive data with source id, send-to-selected, broadcast, disconnect, max-client limit.
- `test_net_tools.py`: resolve `localhost` returns 127.0.0.1/::1; ping 127.0.0.1 returns a result (ok may be False in restricted environments, but output non-empty); adapter_info returns a list.

Step 8: Page tests offscreen with local servers; assert status text, log text, counters, errors. Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_net_client.py tests/test_net_server.py tests/test_net_tools.py -v
```

Step 9: Commit.

```bash
git add app/io/net_client.py app/io/net_server.py app/services/net_tools.py app/ui/dialogs/text_info_dialog.py app/ui/pages/net_client_page.py app/ui/pages/net_server_page.py tests/test_net_client.py tests/test_net_server.py tests/test_net_tools.py
git commit -m "feat: add TCP/UDP client, TCP server and Fluent network pages"
```

---

## Task 5: USB HID

**Files:**
- Create: `app/io/hid_worker.py`
- Replace: `app/ui/pages/hid_page.py`
- Tests: `tests/test_hid_worker.py`, `tests/test_hid_page.py`

**Depends on:** Tasks 1-4.

Step 1: Implement backend and worker:

```python
@dataclass(frozen=True)
class HidDeviceInfo:
    path: bytes
    vendor_id: int
    product_id: int
    product: str
    manufacturer: str
    serial: str
    usage_page: int
    usage: int
    interface: int

class HidBackend(Protocol):
    def enumerate(self) -> list[HidDeviceInfo]: ...
    def open(self, path: bytes) -> object: ...
    def write(self, handle, data: bytes) -> int: ...
    def read(self, handle, size: int, timeout_ms: int) -> bytes: ...
    def close(self, handle) -> None: ...

def list_devices() -> list[HidDeviceInfo]
class HidapiBackend: ...
class HidWorker(QThread):
    opened = pyqtSignal(str)
    closed = pyqtSignal()
    data_received = pyqtSignal(bytes)
    error = pyqtSignal(str)
    def __init__(self, backend: HidBackend | None = None, parent=None)
    def open_device(self, info: HidDeviceInfo) -> None
    def close_device(self) -> None
    def send(self, data: bytes, report_id: int = 0, report_length: int = 64) -> None
    def is_open(self) -> bool
```

Rules: prepend report ID only when `report_id != 0`; pad/truncate to `report_length`; read with 50 ms timeout; on read error emit `error` and close; join on close. `hidapi` import must be lazy so enumeration failure shows an empty list, not an import crash.

Step 2: Tests with `FakeHidBackend`: enumerate two devices, filter; open/close; send prepends ID/pads; read emits data; error closes. Real enumeration test asserts a list, not non-empty.

Step 3: Implement the HID page:
- Device table with columns VID/PID, product, manufacturer, serial, usage, interface; search box; refresh; open/close; status chip.
- Report ID spin 0-255, report length spin 1-1024 default 64; explanation that 0 means no report ID.
- RX `LogPane`, TX editor, HEX, repeat, file send, counters, clear/save/session.
- Info bar: "本程序支持标准 HID；CH372 需要厂商 DLL，不在支持范围".
- Device removal auto-closes; persist report settings.
- `has_active_work()` returns worker open.

Step 4: Tests offscreen with fake backend: enumerate, select/open, feed RX, send padded report, error path.

Step 5: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_hid_worker.py tests/test_hid_page.py -v
```

Step 6: Commit.

```bash
git add app/io/hid_worker.py app/ui/pages/hid_page.py tests/test_hid_worker.py tests/test_hid_page.py
git commit -m "feat: add standard USB HID debugger with worker and Fluent page"
```

---

## Task 6: Checksum Tool

**Files:**
- Create: `app/core/checksum.py`
- Replace: `app/ui/pages/checksum_page.py`
- Tests: `tests/test_checksum.py`, `tests/test_checksum_page.py`

**Depends on:** Tasks 1-2.

Step 1: Implement `app/core/checksum.py` with streaming accumulators:

```python
@dataclass(frozen=True)
class CrcParams:
    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int

CRC_PRESETS: dict[str, CrcParams]

@dataclass(frozen=True)
class ChecksumResult:
    name: str
    value: int
    width: int

class SumAccumulator: ...      # 8/16/32
class XorAccumulator: ...
class ParityAccumulator: ...
class IntelHexAccumulator: ...
class TcpIpAccumulator: ...
class CrcAccumulator: ...
def compute_all(data: bytes, invert: bool = False) -> list[ChecksumResult]
def format_value(value: int, width: int, base: int = 16) -> str
def source_text(name: str) -> str
```

Presets: `CRC-8/MAXIM` (width 8, poly 0x31, init 0, refin/refout true, xorout 0), `CRC-8/SAE-J1850` (0x1D, 0xFF, false, false, 0xFF), `CRC-8/BLUETOOTH` (0xA7, 0x00, true, true, 0x00), `CRC-16/MODBUS` (0x8005, 0xFFFF, true, true, 0), `CRC-16/CCITT-FALSE` (0x1021, 0xFFFF, false, false, 0), `CRC-16/XMODEM` (0x1021, 0x0000, false, false, 0), `CRC-16/KERMIT` (0x1021, 0x0000, true, true, 0), `CRC-32/ISO-HDLC` (0x04C11DB7, 0xFFFFFFFF, true, true, 0xFFFFFFFF). Implement reflected and non-reflected CRC with a 256-entry table per call; mask to width. `compute_all` also computes Sum8/16/32, XOR-8, odd/even parity, Intel HEX checksum, TCP/IP checksum. Invert applies `~value & mask` per width.

`source_text(name)` returns the actual Python body for that accumulator from a module-level mapping; the page displays it read-only. Do not claim it is the original program's source.

Step 2: Tests:
- Known vectors: `crc("123456789", preset)` for all presets equals published check values.
- Streaming vs one-shot for random 64 KiB with chunk sizes 1, 7, 4096.
- Sum/XOR/parity/Intel HEX/TCP-IP vectors.
- Invert behavior.
- `source_text` non-empty for every result name.

Step 3: Implement `app/ui/pages/checksum_page.py`:
- Input mode segmented: 文本 / HEX / 文件.
- Text editor or HEX editor or `FilePicker`; encoding selector for text.
- Results table with name, hex, dec, bin, copy button; invert toggle; byte order toggle for 16/32-bit; compute button auto-updates on input.
- File mode: background `QThreadPool` task with progress bar and cancel; stream chunks via accumulators.
- Source preview: bottom `QPlainTextEdit` showing `source_text(selected_name)`, monospace, read-only; a combo to choose the algorithm.
- Tests: text "123456789" -> CRC32 `CBF43926`; HEX input; invert; file mode with a temp file; source preview contains `def` or the algorithm name.

Step 4: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_checksum.py tests/test_checksum_page.py -v
```

Step 5: Commit.

```bash
git add app/core/checksum.py app/ui/pages/checksum_page.py tests/test_checksum.py tests/test_checksum_page.py
git commit -m "feat: add streaming checksum engine and Fluent checksum page"
```

---

## Task 7: Encoding Conversion Shell and First Three Tools

**Files:**
- Modify: `app/core/encoding.py` (add base/seven-segment/file helpers)
- Create: `app/ui/pages/convert/__init__.py`, `page.py`, `base_tool.py`, `data_tool.py`, `sevenseg_tool.py`
- Tests: `tests/test_convert_core.py`, `tests/test_convert_page.py`

**Depends on:** Tasks 1-2.

Step 1: Extend `app/core/encoding.py`:

```python
def parse_int(text: str, base: int, bits: int = 32, signed: bool = False) -> int
def format_int(value: int, base: int, bits: int = 32, signed: bool = False) -> str
def base_outputs(text: str, from_base: int, bits: int = 32, signed: bool = False) -> dict[int, str]
SEVEN_SEG_COMMON_CATHODE: dict[str, int]
def seven_segment_code(char: str, common_cathode: bool = True) -> int
def seven_segment_segments(code: int, common_cathode: bool = True) -> list[str]
def bin_file_to_hex_text(data: bytes) -> str
def hex_text_to_bytes(text: str) -> bytes
def hex_text_to_text(text: str, codec: str) -> str
def text_to_hex_text(data: bytes) -> str
```

Rules: `parse_int` rejects empty/overflow with Chinese messages; `format_int` masks to bits; signed uses two's complement; base outputs are binary/octal/decimal/hex uppercase; seven-segment bits `a=0x01 ... g=0x40, dp=0x80`; common anode is bitwise inverted; `hex_text_to_bytes` uses `parse_hex`; `hex_text_to_text` decodes bytes with the chosen codec.

Step 2: Tests for each function, including `0xFF` signed 8-bit -> -1, overflow rejection, seven-segment 0/9/A/F, Bin→Hex→Text round-trip.

Step 3: Create the conversion page shell:
- `ConvertPage(QWidget)` with a `SegmentedWidget`/`Pivot` of tool names and a `QStackedWidget`.
- Tool widgets are separate classes; each exposes `title`, a `QWidget` body and `set_settings(settings)`/`save_settings(settings)` where needed.
- First three tools:
  - `BaseTool`: input, from-base combo 2-36, bits 32/64, signed toggle, four read-only output fields with copy buttons, live conversion.
  - `DataTool`: three operations (Bin→Text, Hex→Text, Hex→Bin) with `FilePicker` input/output, encoding combo, convert button, progress, preview first 256 bytes/chars, errors via feedback.
  - `SevenSegTool`: digit input 0-9/A-F, segment toggle buttons a-g/dp, common cathode/anode outputs in hex/bin, live sync both directions.
- Page registers the tools and restores the last selected tool from settings (`convert.last_tool`).

Step 4: Tests:
- Core tests as above.
- Page tests: instantiate ConvertPage, switch tools, base conversion updates outputs, invalid input shows error, SevenSeg toggles update code, DataTool conversion with temp files writes expected bytes.
- Ensure the page does not create modal dialogs during tests; use direct methods for file conversion.

Step 5: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_convert_core.py tests/test_convert_page.py -v
```

Step 6: Commit.

```bash
git add app/core/encoding.py app/ui/pages/convert tests/test_convert_core.py tests/test_convert_page.py
git commit -m "feat: add base, binary/hex/text and seven-segment conversion tools"
```

---

## Task 8: Palette, GIF, Audio and QR Conversion Tools

**Files:**
- Create: `app/core/color.py`, `app/core/gif_tools.py`, `app/core/audio_tools.py`, `app/core/qr_tools.py`
- Create: `app/ui/pages/convert/palette_tool.py`, `gif_tool.py`, `audio_tool.py`, `qr_tool.py`
- Modify: `app/ui/pages/convert/page.py` to register the four tools
- Tests: `tests/test_convert_extra_core.py`, extend `tests/test_convert_page.py`

**Depends on:** Task 7.

Step 1: Implement `app/core/color.py`:

```python
def hex_to_rgb(text: str) -> tuple[int, int, int]
def rgb_to_hex(rgb: tuple[int, int, int]) -> str
def rgb_to_565(rgb) -> int          # rrrrrggggggbbbbb
def rgb_to_332(rgb) -> int          # rrrgggbb
def rgb_to_888(rgb) -> int
def decode_565(value: int) -> tuple[int, int, int]
def decode_332(value: int) -> tuple[int, int, int]
```

Tests: known colors (red `0xF800`, green `0x07E0`, blue `0x001F`, black, white), round-trip within quantization error.

Step 2: Implement `app/core/gif_tools.py`:

```python
@dataclass(frozen=True)
class GifFrame:
    index: int
    image: Image.Image
    duration_ms: int

def load_gif(path: Path) -> tuple[list[GifFrame], int, int]
def export_frames(frames, output_dir: Path, stem: str, width: int | None = None, height: int | None = None, progress=None, cancel=None) -> list[Path]
```

Tests: create a 2-frame GIF with Pillow, load, assert frame count/durations, export BMP files, resize, naming `stem_0001.bmp`.

Step 3: Implement `app/core/audio_tools.py`:

```python
@dataclass(frozen=True)
class AudioData:
    sample_rate: int
    channels: int
    sample_width: int
    frames: bytes

def load_audio(path: Path) -> AudioData           # WAV via wave; MP3 via ffmpeg to temp WAV
def audio_to_c(audio: AudioData, name: str, max_samples: int | None = None) -> str
def audio_duration_s(audio: AudioData) -> float
```

WAV: use `wave` module; only PCM (raise Chinese error for unsupported compression). MP3: locate `ffmpeg` on PATH, run `ffmpeg -v error -i input -f wav -acodec pcm_s16le -` with a timeout and capture stdout into a `BytesIO`, then read with `wave`. If ffmpeg is missing, raise a clear error. C output includes a header comment, sample rate, channels, sample width, length and a `const unsigned char name[]`; cap `max_samples` to avoid gigantic files. Tests: generate 0.1 s 8 kHz mono 16-bit WAV, load, duration, C output contains length and array name; MP3 test skipped if ffmpeg absent.

Step 4: Implement `app/core/qr_tools.py`:

```python
def make_qr(text: str, ec: str = "M", box_size: int = 8, border: int = 4, fg: str = "#000000", bg: str = "#FFFFFF") -> Image.Image
def save_qr(image, path: Path) -> None
```

Use `qrcode.QRCode(version=None, error_correction=..., box_size=..., border=...)`; fill and make image; convert to RGBA and apply fg/bg colors. Tests: non-empty text produces square image with expected border; empty text raises a Chinese error; save PNG/BMP.

Step 5: Implement the four tool widgets:
- `PaletteTool`: color picker + hex/RGB inputs, swatch, outputs 24-bit/565/332/888 with copy; updates live.
- `GifTool`: file picker, frame list/preview with timer playback, speed slider, width/height, export button, progress, output directory.
- `AudioTool`: file picker, format info, play/stop (`winsound` for WAV, MCI for MP3 via ctypes; keep in UI layer), convert to C, save `.c` and `.h`, progress.
- `QrTool`: text, EC combo, size, border, fg/bg color buttons, preview label, save PNG/BMP.
- Register in `ConvertPage`; persist selected tool and last paths.

Step 6: Tests:
- Core color/GIF/audio/QR as above.
- Page tests with temp files: QR generates preview; palette conversion; GIF frame count; audio conversion writes C file (skip MP3).
- Do not assert audio playback.

Step 7: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_convert_extra_core.py tests/test_convert_page.py -v
```

Step 8: Commit.

```bash
git add app/core app/ui/pages/convert tests
git commit -m "feat: add palette, GIF, audio and QR conversion tools"
```

---

## Task 9: Image to Array Tool

**Files:**
- Create: `app/core/imaging.py`
- Replace: `app/ui/pages/image_page.py`
- Tests: `tests/test_imaging.py`, `tests/test_image_page.py`

**Depends on:** Tasks 1-2.

Step 1: Implement `app/core/imaging.py`:

```python
@dataclass(frozen=True)
class ImageParams:
    output_type: str = "c"       # c/bin/lvgl8/lvgl9
    scan: str = "horizontal"     # horizontal/vertical
    direction: str = "ltr"       # ltr/rtl/ttb/btt
    color_depth: int = 16        # 1/8/16/24/32
    byte_order: str = "little"
    width: int | None = None
    height: int | None = None
    background: tuple[int, int, int] = (0, 0, 0)
    invert: bool = False

@dataclass(frozen=True)
class ConversionResult:
    data: bytes
    width: int
    height: int
    preview: Image.Image
    text: str | None

def load_image(path: Path) -> Image.Image
def convert_image(image: Image.Image, params: ImageParams, name: str = "image") -> ConversionResult
def convert_file(path: Path, params: ImageParams, output_dir: Path) -> list[Path]
def batch_convert(paths: list[Path], params: ImageParams, output_dir: Path, progress=None, cancel=None) -> dict[str, list[Path]]
```

Rules:
- Resize to params width/height (nearest) if provided; preserve aspect if only one dimension is given? Keep it simple: if both None use original; if one None compute from aspect; UI provides a "lock ratio" option.
- 1-bit: grayscale threshold 128, invert optional; pack 8 pixels per byte, MSB first in scan order; pad last byte with 0.
- 8-bit: RGB332.
- 16-bit: RGB565; bytes in requested endianness.
- 24-bit: RGB888.
- 32-bit: RGBA8888, alpha 255.
- Scan horizontal: rows, then direction ltr/rtl; vertical: columns, then ttb/btt.
- C output: header comment `/* name WxH depth scan direction endian */`, `const unsigned char name[] = { ... };`, array name sanitised, 12 values per line.
- BIN: raw data only.
- LVGL8/9: produce a C file with `lv_img_dsc_t` header and data; LVGL8 uses 32-bit header (cf, w, h), LVGL9 uses `lv_image_dsc_t`; include a `#if`-free simple struct text. Do not require LVGL headers to generate.
- Batch: per-file output names sanitised; collisions use `unique_path`.

Step 2: Tests:
- Create a 2×2 image with known colors; verify 565/332/888 bytes, scan directions, endianness, 1-bit packing, vertical transpose.
- Convert C output contains dimensions and array name; BIN length matches.
- Batch two files writes two outputs; collision gets suffix.
- Preview size matches output.

Step 3: Implement the page:
- Input single/folder `FilePicker` or a folder picker; list of files for batch.
- Preview labels for original and output (scaled, keep aspect); size/depth labels.
- Parameter card: output type, scan, direction, depth, endianness, width/height + lock ratio, background color, invert.
- Buttons: convert/save, batch convert, open output dir; progress bar; code preview read-only; errors.
- Persist parameters; `has_active_work()` false.
- Tests: load a temp PNG, set params, convert, assert preview and text; batch writes files; invalid path shows error; no modal dialogs.

Step 4: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_imaging.py tests/test_image_page.py -v
```

Step 5: Commit.

```bash
git add app/core/imaging.py app/ui/pages/image_page.py tests/test_imaging.py tests/test_image_page.py
git commit -m "feat: add image to C/BIN/LVGL converter with batch export"
```

---

## Task 10: Dot Matrix Generator

**Files:**
- Create: `app/core/fontmatrix.py`, `app/ui/widgets/matrix_view.py`
- Replace: `app/ui/pages/dotmatrix_page.py`
- Tests: `tests/test_fontmatrix.py`, `tests/test_dotmatrix_page.py`

**Depends on:** Tasks 1-2.

Step 1: Implement `app/core/fontmatrix.py`:

```python
@dataclass(frozen=True)
class FontParams:
    font_path: str | None = None
    size: int = 16
    bold: bool = False
    italic: bool = False
    threshold: int = 128
    invert: bool = False
    scan: str = "horizontal"      # horizontal/vertical
    direction: str = "ltr"        # ltr/rtl/ttb/btt
    bit_order: str = "msb"        # msb/lsb
    page_chars: int = 8

@dataclass
class Matrix:
    rows: int
    cols: int
    cells: list[list[bool]]

def matrix_from_text(text: str, params: FontParams) -> list[Matrix]
def matrix_from_image(path: Path, params: FontParams) -> Matrix
def matrix_to_bytes(matrix: Matrix, params: FontParams) -> bytes
def matrices_to_bytes(matrices: list[Matrix], params: FontParams) -> bytes
def matrix_to_c(matrices: list[Matrix], params: FontParams, name: str) -> str
def paginate(matrix: Matrix, page_chars: int) -> list[Matrix]
```

Rules: use Pillow `ImageFont.truetype(font_path, size)`; if `font_path` is None use `ImageFont.load_default()`; bold via `stroke_width=1`; italic via affine shear (x + y*0.2). Render text on a white background, threshold to black/white, invert. Page horizontally by `page_chars` columns of the rendered text bitmap (or by characters? Keep it simple: split the rendered bitmap into vertical pages of `page_chars * size` columns, overlapping no; tests verify page count). Scan directions as in imaging; pack MSB/LSB per byte; pad. C output includes width/height, scan, direction, bit order and page count.

Step 2: Tests:
- Synthetic 2×8 matrix -> known packed bytes for all four directions and MSB/LSB.
- Render "A" at 16 px with default font -> non-empty, dimensions > 0.
- Image BMP threshold/invert.
- Paginate a 32×64 matrix into pages.
- C output contains array name and dimensions.

Step 3: Implement `MatrixView(QWidget)`:
- `set_matrix(matrix)`, `matrix()`, `set_zoom(int)`, `cellToggled(row, col)`; paint white/black cells + grid; mouse click toggles; mouse wheel zoom; minimum 200×200; page label handled by the page.
- Tests: set a matrix, toggle a cell via direct method, assert matrix changed; `sizeHint` positive.

Step 4: Implement the page:
- Text/BMP mode; text editor, font family combo (`QFontComboBox`), size, bold/italic; BMP `FilePicker` + threshold + invert.
- Parameters card: scan, direction, bit order, page chars, dot/background colors, grid, zoom.
- MatrixView preview with page navigation (prev/next/label); click-to-edit; reset from text; export C/BIN; BMP folder batch.
- Persist parameters; errors; `Ctrl+S` export.
- Tests: render text, page navigation, toggle cell changes export, save C/BIN to temp, batch BMP folder.

Step 5: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_fontmatrix.py tests/test_dotmatrix_page.py -v
```

Step 6: Commit.

```bash
git add app/core/fontmatrix.py app/ui/widgets/matrix_view.py app/ui/pages/dotmatrix_page.py tests/test_fontmatrix.py tests/test_dotmatrix_page.py
git commit -m "feat: add editable dot matrix generator with C/BIN and BMP batch export"
```

---

## Task 11: C51 Code Wizard

**Files:**
- Create: `app/core/c51.py`
- Replace: `app/ui/pages/c51_page.py`
- Tests: `tests/test_c51.py`, `tests/test_c51_page.py`

**Depends on:** Tasks 1-2.

Step 1: Implement `app/core/c51.py`:

```python
@dataclass
class TimerConfig:
    mode: int = 1                # 0,1,2,3
    enabled: bool = False
    gate: bool = False
    time_us: int = 1000
    reload: int | None = None    # optional direct reload

@dataclass
class SerialConfig:
    mode: int = 1                # 0..3
    baud: int = 9600
    ren: bool = True
    sm2: bool = False
    tb8: bool = False
    rb8: bool = False
    smod: bool = False
    generator: str = "t1"        # t1/t2

@dataclass
class InterruptConfig:
    ea: bool = True
    ex0: bool = False
    et0: bool = False
    ex1: bool = False
    et1: bool = False
    es: bool = False
    et2: bool = False
    high_priority: set[str] = field(default_factory=set)
    ext0_edge: bool = False
    ext1_edge: bool = False

@dataclass
class C51Config:
    crystal_mhz: float = 12.0
    double_speed: bool = False   # 6T mode
    ports: dict[str, int] = field(default_factory=lambda: {"P0": 0, "P1": 0, "P2": 0, "P3": 0})
    timers: list[TimerConfig] = field(default_factory=lambda: [TimerConfig(), TimerConfig(), TimerConfig()])
    serial: SerialConfig = field(default_factory=SerialConfig)
    interrupts: InterruptConfig = field(default_factory=InterruptConfig)
    power_mode: str = "normal"   # normal/idle/powerdown
    delay_us: int = 0

@dataclass(frozen=True)
class TimerResult:
    th: int
    tl: int
    counts: int
    warnings: list[str]

def timer_reload(cfg: C51Config, index: int) -> TimerResult
def baud_reload(cfg: C51Config) -> dict[str, int | float | list[str]]
def build_registers(cfg: C51Config) -> dict[str, int]
def build_warnings(cfg: C51Config) -> list[str]
def generate_c(cfg: C51Config) -> str
def generate_keil_project(cfg: C51Config, output_dir: Path) -> list[Path]
```

Formulas:
- Machine cycle machine cycles per second = crystal_mhz * 1e6 / (6 if double_speed else 12).
- Timer mode max counts: mode 0 = 8192 (13-bit), mode 1 = 65536, mode 2 = 256, mode 3 = 256 per half.
- For mode 0/1: counts = round(time_us * 1e-6 * machine_cycles); reload = max_counts - counts; TH/TL split; mode 0 use 13-bit split (TH = reload >> 5, TL = reload & 0x1F); mode 2 TL = reload & 0xFF, TH = same; mode 3 TL0/TH0 separate.
- Baud:
  - mode 0: baud = crystal_mhz * 1e6 / 12; no reload.
  - mode 2: baud = (2 if smod else 1) * crystal_mhz * 1e6 / 64.
  - mode 1/3 with T1 mode 2: reload = 256 - round(crystal_mhz*1e6 / (12 * 32 * baud) * (2 if smod else 1)); actual = crystal*1e6/(12*32*(256-reload)) * (2 if smod else 0.5?) Use the standard formula consistently and report error %.
  - mode 1/3 with T2: reload16 = 65536 - round(crystal_mhz*1e6/(32*baud)); actual = crystal*1e6/(32*(65536-reload16)).
  - Warn if reload outside 0..255/0..65535, error > 5%, mode conflicts, timer used as baud generator and enabled as a timer, SM2 in mode 0/1, etc.
- Registers: TMOD bits (T1 M1/M0/C/T/GATE, T0 ...), TCON (TF1/TR1/TF0/TR0/IE1/IT1/IE0/IT0), SCON (SM0/SM1/SM2/REN/TB8/RB8/TI/RI), PCON (SMOD), IE (EA/ES/ET1/EX1/ET0/EX0/ET2), IP (PS/PT1/PX1/PT0/PX0/PT2), TH/TL/RCAP2.
- `generate_c`: include `<reg51.h>` or `<reg52.h>` (reg52 if timer2 used), port initialisation, `TMOD=...;`, `TH0=...; TL0=...;`, `SCON=...;`, `PCON=...;`, `IE=...; IP=...;`, `TR0=1;` etc., interrupt service functions for enabled sources, `void main(void)` with `while(1);`. Comments in Chinese. Escape numbers as hex.
- `generate_keil_project`: write `Main.c` (generated C), `M51Project.Uv2` (uVision2 text with target, file group, `Main.c`), `M51Project.Opt` (minimal options), `STARTUP.A51` (own minimal startup with `CSEG AT 0`, `LJMP STARTUP1`, `ORG 0x30`, `STARTUP1:`, `RETI` stubs for interrupts, `END`). Add comments that this is a minimal self-written startup. Return paths.

Step 2: Tests:
- Timer at 12 MHz, mode 1, 50 ms => TH/TL `0x3C/0xB0` (counts 50000, reload 15536 = 0x3CB0).
- Timer at 12 MHz, mode 2, 250 µs => TL/TH 0x06 (counts 250, reload 6).
- Baud 9600 at 11.0592 MHz, T1, SMOD=0 => TH1 0xFD (reload 253), actual 9600.
- Baud 115200 at 11.0592 MHz with SMOD=1 => error reported and warning.
- Registers bitfields for a known config.
- C output contains `TMOD =`, `TH0 =`, `void main`, and enabled ISR names.
- Keil export creates 4 files; `Main.c` matches generated C; `.Uv2` contains `Main.c`.

Step 3: Implement the page:
- Left column: crystal, double speed, power mode, delay.
- Port grid P0-P3 (8 checkboxes each) with hex labels; clicking updates values.
- Interrupts card: EA/EX0/ET0/EX1/ET1/ES/ET2, priority combos, edge/level for external interrupts.
- Timers card: three sub-cards with mode combo, gate, run, time µs or reload, computed TH/TL labels, warnings.
- Serial card: mode, baud, REN/SM2/TB8/RB8/SMOD, generator T1/T2, computed actual baud/error.
- Warning `LogPane` or read-only text; code preview `QPlainTextEdit` with simple C highlighting (QSyntaxHighlighter for keywords/comments/numbers) or monospace plain text; buttons: generate, copy, export C, export Keil project, browse.
- Persist config; `Ctrl+S` export; errors via feedback.
- Tests: set 12 MHz/50ms, generate, assert TH0/TL0 in preview; export to temp, assert files; warnings for overflow; port checkbox updates hex.

Step 4: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_c51.py tests/test_c51_page.py -v
```

Step 5: Commit.

```bash
git add app/core/c51.py app/ui/pages/c51_page.py tests/test_c51.py tests/test_c51_page.py
git commit -m "feat: add 8051 timer/baud wizard with C and Keil project export"
```

---

## Task 12: Settings, About, Icon and Polish

**Files:**
- Replace: `app/ui/pages/settings_page.py`
- Modify: `app/ui/main_window.py` (close confirmation, icon, geometry, status)
- Create: `build/make_icon.py`, `build/icon.ico`
- Modify: `main.py` (set window icon)
- Tests: `tests/test_settings_page.py`, extend `tests/test_main_window.py`

**Depends on:** Tasks 1-11.

Step 1: Implement the settings page:
- Appearance card: theme combo (跟随系统/浅色/深色), accent color button + hex label; apply immediately via `apply_theme`.
- Behaviour card: default encoding combo, max display lines spin 1000-100000, auto-scroll switch, confirm-exit switch, log font size spin 9-24.
- Paths card: config path, log path, buttons open folder, export config, reset config (confirmation dialog).
- About card: app name/version, "功能参考 PortHelper V1.8.8（温子祺），独立 Python 复刻", unsupported list (蓝牙、内核串口监视、CH372、在线更新、Keil 配色替换), dependencies/licenses summary, REA evidence note.
- Tests: toggle theme changes `isDarkTheme`; accent updates setting; reset asks for confirmation (monkeypatch `QMessageBox.question` to Yes); open-folder buttons call `QDesktopServices.openUrl` (monkeypatch and assert).
- No external URLs in the About page.

Step 2: Polish main window:
- Use a custom icon from `build/icon.ico`; if missing, generate a fallback in code.
- Restore geometry; save on close; confirm exit if `ui.confirm_exit` and any page's `has_active_work()`.
- Add a footer/status label? Optional; skip to avoid layout churn.
- Ensure nav group headers render; keyboard shortcuts work; first page serial.
- Extend `tests/test_main_window.py`: close confirmation with a fake active page (monkeypatch), geometry save/restore, icon not null.

Step 3: Create `build/make_icon.py` using Pillow:
- 256×256 rounded square with vertical blue gradient, white plug/terminal glyph and a small waveform; save 16/24/32/48/64/128/256 ICO.
- Run `python build/make_icon.py`; commit `build/icon.ico` and the script.
- Do not use original app assets.

Step 4: Run tests.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_settings_page.py tests/test_main_window.py -v
python build/make_icon.py
```

Step 5: Commit.

```bash
git add app/ui/pages/settings_page.py app/ui/main_window.py main.py build/make_icon.py build/icon.ico tests
git commit -m "feat: add settings/about pages, app icon and window polish"
```

---

## Task 13: Packaging, Full Verification and Delivery

**Files:**
- Modify: `build/app.spec` (onefile, excludes, data collection, icon, version info)
- Create: `build/build.ps1`
- Modify: `main.py` if selftest/smoke need packaging fixes
- Create: `build/version_info.txt` (generated or static)
- No new docs.

**Depends on:** Tasks 1-12.

Step 1: Write `build/app.spec`:
- `Analysis(['main.py'], pathex=[repo_root], datas=collect_data_files('qfluentwidgets'), binaries=collect_dynamic_libs('hid'), hiddenimports=['hid'], excludes=['PyQt5','PyQt6.QtWebEngineCore','PyQt6.QtWebEngineWidgets','PyQt6.QtQuick','PyQt6.QtQml','PyQt6.Qt3DCore','PyQt6.QtMultimedia','tkinter','matplotlib','scipy','pandas'])`.
- `EXE(... onefile=True? In spec, `EXE(pyz, a.scripts, a.binaries, a.datas, ..., name='PortHelperFluent', console=False, icon='build/icon.ico', version='build/version_info.txt')` with `COLLECT` omitted for onefile. For onefile, use `EXE` with all binaries/datas and no COLLECT; PyInstaller onefile is triggered by `--onefile` or spec structure. Use `EXE(pyz, a.scripts, a.binaries, a.datas, [], name=..., debug=False, strip=False, upx=False, console=False, icon=..., version=...)`.
- Exclude `analysis`, `docs`, `tests`, `.git` by not including them.
- Add runtime hook? Not needed.

Step 2: Write `build/version_info.txt` with `VSVersionInfo` for version 1.0.0, product `PortHelper Fluent`, company `Independent reimplementation`, description in Chinese.

Step 3: Write `build/build.ps1`:
- `$ErrorActionPreference = "Stop"`, set location repo root.
- Clean `build_work` and `dist`.
- Run `python -m pytest` full suite; abort on failure.
- Run `python build/make_icon.py`.
- Run `python -m PyInstaller --noconfirm --clean --distpath dist --workpath build_work build/app.spec`.
- Compute SHA-256 of `dist/PortHelperFluent.exe` and write `dist/SHA256.txt`; write `dist/build.log` with versions and test summary.
- Verify `--selftest` and `--smoke-test` using the built exe from a temp directory; abort on non-zero.
- Print size and SHA-256.

Step 4: Add a packaged self-test:
- `--selftest --report <path>` must work in the windowed exe (no console). It writes JSON and exits 0/1. The report includes whether `hid`, `qrcode`, qfluentwidgets resources and icon are present.
- `--smoke-test` creates the real MainWindow, shows it, quits after 1.5 s; return 0. Do not rely on screenshots.
- If `sys.frozen`, log paths under AppData to avoid writing next to the exe.

Step 5: Run the full test suite.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -v
```

Expected: all tests pass. If a test is flaky due to timing, fix the test or code, do not skip silently.

Step 6: Build the exe.

```powershell
powershell -ExecutionPolicy Bypass -File build/build.ps1
```

Expected: `dist/PortHelperFluent.exe` exists; selftest and smoke pass; SHA-256 file written.

Step 7: Run the packaged exe from a temp directory:

```bash
mkdir -p /tmp/porthelper-delivery && cp dist/PortHelperFluent.exe /tmp/porthelper-delivery/ && cd /tmp/porthelper-delivery && ./PortHelperFluent.exe --selftest --report selftest.json && ./PortHelperFluent.exe --smoke-test
```

Expected: both exit 0; `selftest.json` shows `"ok": true`. The app must not depend on the repo directory.

Step 8: Inspect the packaged archive for excluded content:

```bash
python -m PyInstaller.utils.cliutils.archive_viewer dist/PortHelperFluent.exe 2>/dev/null | head -40 || true
```

Confirm no `analysis/`, `docs/`, `tests/`, `.git`, PyQt5 or QtWebEngine entries. If the archive viewer is unavailable, inspect the spec/warn file and PyInstaller build log.

Step 9: Final code review.

- Dispatch the final code reviewer subagent over the whole branch vs `master` (or the initial commit). It must check spec coverage, tests, performance/cancellation, thread cleanup, single-exe packaging, and no copied assets.
- Fix Critical/Important issues with a focused implementer subagent, then re-review.

Step 10: Final commit and delivery report.

```bash
git add build main.py
git commit -m "build: package single-file PortHelper Fluent exe and verify delivery"
git log --oneline -5
```

Report to the user:
- files and exe path;
- test count/result;
- selftest/smoke result;
- SHA-256 and size;
- unsupported features;
- manual hardware checklist (serial device, HID device, TCP/UDP peer, Keil project open, image/font export).

---

## Plan Self-Review

**Spec coverage:**
- Serial 4.1 -> Task 3.
- Network client 4.2 -> Task 4.
- Network server 4.3 -> Task 4.
- USB HID 4.4 -> Task 5.
- Checksum 4.5 -> Task 6.
- Encoding conversion 4.6 -> Tasks 7 and 8.
- Image 4.7 -> Task 9.
- Dot matrix 4.8 -> Task 10.
- C51 4.9 -> Task 11.
- Settings/About 4.10 -> Task 12.
- Architecture, concurrency, config, errors, tests, packaging, attribution -> Tasks 1, 2, 12, 13.
- Excluded Bluetooth/kernel monitor/CH372/auto-update/Keil colour replacement -> not implemented; About page documents them.

**Placeholder scan:** no TBD/TODO; every task names files, interfaces, commands and expected results.

**Type consistency:**
- `SessionLog.add_bytes`, `LogPane.append_bytes`, `SerialWorker.data_received`, `NetClientController.tcp_data`, `NetServerController.data_received`, `HidWorker.data_received` all use `bytes`.
- `Settings.get/set` flat dotted keys are used consistently.
- Page class names and route keys match the main window.
- C51 `TimerConfig`/`SerialConfig` names differ from Task 3's `SerialConfig` for the serial port; they live in different modules and must be imported with aliases where both are needed. The C51 page imports only `app.core.c51`.
- `ImageParams` and `FontParams` both use `scan`/`direction` strings with the same vocabulary.
- `ChecksumResult.width` is the bit width; `format_value` takes bits, not bytes.

**Known integration points to watch:**
- `LogPane` must be resizable and not steal keyboard focus from send editors; set focus policy on buttons appropriately.
- `MainWindow` group header order must match the page insertion order; write a test for route keys.
- HID page must not import `hid` at module import time in a way that fails when the DLL is missing; use lazy import and show an error state.
- PyInstaller onefile must include `qrcode` and `hidapi` hidden imports; verify in Task 13.
