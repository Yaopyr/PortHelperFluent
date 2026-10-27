"""C51 向导页：8051 定时器/波特率配置、寄存器预览与 C/Keil 工程导出。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QRegularExpression
from PyQt6.QtGui import (QColor, QFont, QKeySequence, QShortcut, QSyntaxHighlighter, QTextCharFormat)
from PyQt6.QtWidgets import (QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                             QScrollArea, QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CheckBox, ComboBox, DoubleSpinBox,
                            FluentIcon, LineEdit, PlainTextEdit, PrimaryPushButton, PushButton,
                            SpinBox, SwitchButton, TitleLabel, ToolButton)

from app.core.c51 import (HIGH_PRIORITY_KEYS, PORT_NAMES, C51Config, InterruptConfig,
                          SerialConfig, TimerConfig, baud_reload, build_registers,
                          build_warnings, generate_c, generate_keil_project, timer_reload)
from app.core.logging_setup import get_logger
from app.services.exports import write_text_atomic
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card

SETTINGS_PREFIX = "c51."
ERROR_COLOR = "#C42B1C"
ERROR_COLOR_DARK = "#FF99A4"
MAX_TIMER_US = 10_000_000
SERIAL_MODE_ITEMS = (("方式 0（同步移位）", 0), ("方式 1（8 位 UART）", 1),
                     ("方式 2（9 位 UART）", 2), ("方式 3（9 位 UART）", 3))
GENERATOR_ITEMS = (("定时器 1", "t1"), ("定时器 2", "t2"))
POWER_ITEMS = (("正常", "normal"), ("空闲", "idle"), ("掉电", "powerdown"))
PRIORITY_ITEMS = (("低优先级", "low"), ("高优先级", "high"))
TRIGGER_ITEMS = (("低电平触发", "level"), ("下降沿触发", "edge"))
INTERRUPT_ITEMS = (("ea", "EA 总中断"), ("ex0", "EX0 外部中断 0"), ("et0", "ET0 定时器 0"),
                   ("ex1", "EX1 外部中断 1"), ("et1", "ET1 定时器 1"), ("es", "ES 串口"),
                   ("et2", "ET2 定时器 2"))
TIMER_MODE_ITEMS = {
    0: (("方式 0（13 位）", 0), ("方式 1（16 位）", 1),
        ("方式 2（8 位自动重装）", 2), ("方式 3（双 8 位）", 3)),
    1: (("方式 0（13 位）", 0), ("方式 1（16 位）", 1), ("方式 2（8 位自动重装）", 2)),
    2: (("16 位自动重装", 1),),
}
C_KEYWORDS = (
    "auto", "bit", "break", "case", "char", "code", "const", "continue", "data",
    "default", "do", "else", "enum", "extern", "float", "for", "goto", "idata",
    "if", "inline", "int", "interrupt", "long", "pdata", "reentrant", "register",
    "return", "sbit", "sfr", "short", "signed", "sizeof", "static", "struct",
    "switch", "typedef", "union", "unsigned", "using", "void", "volatile",
    "while", "xdata",
)


class C51Highlighter(QSyntaxHighlighter):
    """极简 C51 语法高亮：关键字、数字、字符串、预处理与注释。"""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        keyword = QTextCharFormat()
        keyword.setForeground(QColor("#0A84FF"))
        keyword.setFontWeight(QFont.Weight.DemiBold)
        for word in C_KEYWORDS:
            self._rules.append((QRegularExpression(rf"\b{word}\b"), keyword))
        number, string, preprocessor = QTextCharFormat(), QTextCharFormat(), QTextCharFormat()
        number.setForeground(QColor("#E8A33D"))
        string.setForeground(QColor("#CE9178"))
        preprocessor.setForeground(QColor("#C586C0"))
        self._comment_format = QTextCharFormat()
        self._comment_format.setForeground(QColor("#4CAF50"))
        self._comment_format.setFontItalic(True)
        self._rules += [
            (QRegularExpression(r"\b(0[xX][0-9A-Fa-f]+|\d+)[uUlL]*\b"), number),
            (QRegularExpression(r'"[^"\\]*(?:\\.[^"\\]*)*"'), string),
            (QRegularExpression(r"^\s*#.*$"), preprocessor),
            (QRegularExpression(r"//[^\n]*"), self._comment_format),
        ]
        self._comment_start = QRegularExpression(r"/\*")
        self._comment_end = QRegularExpression(r"\*/")

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt 虚函数名
        for pattern, fmt in self._rules:
            iterator = pattern.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)
        self.setCurrentBlockState(0)
        start = 0 if self.previousBlockState() == 1 else -1
        if start < 0:
            match = self._comment_start.match(text)
            start = match.capturedStart() if match.hasMatch() else -1
        while start >= 0:
            end_match = self._comment_end.match(text, start)
            if not end_match.hasMatch():
                self.setCurrentBlockState(1)
                self.setFormat(start, len(text) - start, self._comment_format)
                break
            end = end_match.capturedEnd()
            self.setFormat(start, end - start, self._comment_format)
            next_match = self._comment_start.match(text, end)
            start = next_match.capturedStart() if next_match.hasMatch() else -1


class C51Page(QWidget):
    """C51 向导工作台；计算、生成与导出方法可在测试中直接调用。"""

    title = "C51 向导"

    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(parent)
        self.setObjectName("c51")
        self.settings, self.paths, self.extra_options = settings, paths, dict(kwargs)
        self._loading = True
        self.timer_mode_combos: list[ComboBox] = []
        self.timer_gate_checks: list[CheckBox] = []
        self.timer_run_checks: list[CheckBox] = []
        self.timer_time_spins: list[SpinBox] = []
        self.timer_reload_checks: list[CheckBox] = []
        self.timer_reload_spins: list[SpinBox] = []
        self.timer_labels: list[CaptionLabel] = []
        self.timer_warning_labels: list[CaptionLabel] = []
        self.interrupt_checks: dict[str, CheckBox] = {}
        self.priority_combos: dict[str, ComboBox] = {}
        self.port_checks: dict[str, list[CheckBox]] = {}
        self.port_hex_labels: dict[str, BodyLabel] = {}
        self._build_ui()
        self._restore_settings()
        self._loading = False
        self.refresh()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(12)
        self.title_label = TitleLabel("C51 向导", self)
        self.subtitle_label = CaptionLabel(
            "配置 8051 定时器、串口与中断，自动换算寄存器并导出 C 代码或 Keil 工程", self)
        self.subtitle_label.setWordWrap(True)
        outer.addWidget(self.title_label)
        outer.addWidget(self.subtitle_label)

        body = QHBoxLayout()
        body.setSpacing(12)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        content = QWidget(scroll)
        scroll.setWidget(content)
        left = QVBoxLayout(content)
        left.setContentsMargins(0, 0, 8, 0)
        left.setSpacing(12)
        left.addWidget(self._build_system_card())
        left.addWidget(self._build_ports_card())
        left.addWidget(self._build_interrupt_card())
        left.addWidget(self._build_timers_card())
        left.addWidget(self._build_serial_card())
        left.addStretch(1)
        body.addWidget(scroll, 3)

        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_code_card(), 1)
        right.addWidget(self._build_warning_card())
        body.addLayout(right, 2)
        outer.addLayout(body, 1)

        self._connect_signals()

    def _build_system_card(self) -> QWidget:
        card, layout = section_card("系统时钟与功耗",
                                    "晶振频率决定机器周期；6T 模式（双倍速）的机器周期是 12T 的一半。")
        self.crystal_spin = DoubleSpinBox(card)
        self.crystal_spin.setRange(0.1, 100.0)
        self.crystal_spin.setDecimals(4)
        self.crystal_spin.setSingleStep(0.1)
        self.crystal_spin.setValue(12.0)
        self.crystal_spin.setSuffix(" MHz")
        self.crystal_spin.setFixedWidth(170)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(BodyLabel("晶振", card))
        row.addWidget(self.crystal_spin)
        self.preset_buttons: list[PushButton] = []
        self.crystal_presets = (11.0592, 12.0, 24.0)
        for value in self.crystal_presets:
            button = PushButton(f"{value:g} MHz", card)
            button.setFixedWidth(100)
            button.setToolTip(f"使用 {value:g} MHz 晶振")
            row.addWidget(button)
            self.preset_buttons.append(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.double_speed_switch = SwitchButton(card)
        self.power_combo = _combo(card, POWER_ITEMS, 140)
        self.delay_spin = _spin(card, 0, MAX_TIMER_US, 0, 170, suffix=" µs")
        layout.addLayout(
            _control_row(
                card,
                [
                    ("6T 倍速", self.double_speed_switch),
                    ("功耗模式", self.power_combo),
                    ("软件延时", self.delay_spin),
                ],
            )
        )
        hint = CaptionLabel(
            "6T 倍速时机器周期更短，同样的定时时间需要更多计数；掉电模式会停止晶振。", card
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return card

    def _build_ports_card(self) -> QWidget:
        card, layout = section_card("端口初值",
                                    "勾选 P0–P3 的引脚，生成代码会在 main 开头写入对应的十六进制值。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(2)
        grid.setVerticalSpacing(2)
        for bit in range(8):
            label = CaptionLabel(str(bit), card)
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(label, 0, bit + 1)
        grid.addWidget(CaptionLabel("HEX", card), 0, 9)
        for row, name in enumerate(PORT_NAMES, start=1):
            grid.addWidget(BodyLabel(name, card), row, 0)
            checks: list[CheckBox] = []
            for bit in range(8):
                check = CheckBox(card)
                check.setFixedWidth(26)
                check.setToolTip(f"{name}.{bit}")
                grid.addWidget(check, row, bit + 1)
                checks.append(check)
            self.port_checks[name] = checks
            hex_label = BodyLabel("0x00", card)
            hex_label.setFont(_mono_font())
            hex_label.setMinimumWidth(48)
            grid.addWidget(hex_label, row, 9)
            self.port_hex_labels[name] = hex_label
        grid.setColumnStretch(9, 1)
        layout.addLayout(grid)
        return card

    def _build_interrupt_card(self) -> QWidget:
        card, layout = section_card("中断",
                                    "EA 为总开关；每个中断可单独选择高/低优先级，外部中断可选择触发方式。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        for row, (key, label) in enumerate(INTERRUPT_ITEMS):
            check = CheckBox(label, card)
            check.setMinimumWidth(180)
            self.interrupt_checks[key] = check
            grid.addWidget(check, row, 0)
            if key == "ea":
                grid.addWidget(CaptionLabel("总开关（无独立优先级）", card), row, 1, 1, 2)
                continue
            priority = _combo(card, PRIORITY_ITEMS, 120)
            self.priority_combos[key] = priority
            grid.addWidget(priority, row, 1)
            if key in ("ex0", "ex1"):
                trigger = _combo(card, TRIGGER_ITEMS, 140)
                setattr(self, f"{key}_trigger_combo", trigger)
                grid.addWidget(trigger, row, 2)
            else:
                grid.addWidget(CaptionLabel("", card), row, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)
        self.ext0_trigger_combo = self.ex0_trigger_combo
        self.ext1_trigger_combo = self.ex1_trigger_combo
        hint = CaptionLabel(
            "ET2 需要 8052 内核（reg52.h）；外部中断的电平触发会一直保持请求，注意及时处理。",
            card,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return card

    def _build_timers_card(self) -> QWidget:
        card, layout = section_card("定时器 0 / 1 / 2",
                                    "定时时间按当前晶振换算为机器周期；重载值越界或方式受限时会在下方给出警告。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        for index in range(3):
            grid.addWidget(self._build_timer_block(card, index), 0, index)
        layout.addLayout(grid)
        return card

    def _build_timer_block(self, parent: QWidget, index: int) -> QWidget:
        block = QWidget(parent)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        title = BodyLabel(f"定时器 {index}", block)
        title.setStyleSheet("font-weight: 600;")
        layout.addWidget(title)
        mode = _combo(block, TIMER_MODE_ITEMS[index], 150)
        layout.addWidget(mode)
        self.timer_mode_combos.append(mode)
        checks = QHBoxLayout()
        checks.setSpacing(8)
        gate = CheckBox("GATE", block)
        gate.setToolTip("门控：由 INT0/INT1 引脚电平与 TRx 共同控制启动")
        run = CheckBox("运行", block)
        run.setToolTip("生成代码中置位 TRx")
        if index == 2:
            gate.setEnabled(False)
            mode.setEnabled(False)
            gate.setToolTip("定时器 2 没有 GATE 位")
        checks.addWidget(gate)
        checks.addWidget(run)
        checks.addStretch(1)
        layout.addLayout(checks)
        self.timer_gate_checks.append(gate)
        self.timer_run_checks.append(run)
        layout.addWidget(CaptionLabel("定时时间（µs）", block))
        time_spin = _spin(block, 0, MAX_TIMER_US, 1000, 150)
        time_spin.setSingleStep(10)
        layout.addWidget(time_spin)
        self.timer_time_spins.append(time_spin)
        reload_check = CheckBox("直接指定重载值", block)
        layout.addWidget(reload_check)
        self.timer_reload_checks.append(reload_check)
        reload_spin = _spin(block, 0, 0xFFFF, 0, 150)
        reload_spin.setEnabled(False)
        layout.addWidget(reload_spin)
        self.timer_reload_spins.append(reload_spin)
        value_label = CaptionLabel("TH/TL 尚未计算", block)
        value_label.setWordWrap(True)
        value_label.setMinimumHeight(34)
        layout.addWidget(value_label)
        self.timer_labels.append(value_label)
        warning_label = CaptionLabel("", block)
        warning_label.setWordWrap(True)
        warning_label.setTextColor(QColor(ERROR_COLOR), QColor(ERROR_COLOR_DARK))
        warning_label.setVisible(False)
        layout.addWidget(warning_label)
        self.timer_warning_labels.append(warning_label)
        layout.addStretch(1)
        return block

    def _build_serial_card(self) -> QWidget:
        card, layout = section_card("串口与波特率",
                                    "方式 1/3 使用定时器 1 或定时器 2 产生波特率；实际波特率与误差会实时显示。")
        self.serial_mode_combo = _combo(card, SERIAL_MODE_ITEMS, 170)
        self.baud_spin = _spin(card, 300, 4_000_000, 9600, 150)
        self.baud_spin.setSingleStep(100)
        self.generator_combo = _combo(card, GENERATOR_ITEMS, 120)
        layout.addLayout(
            _control_row(
                card,
                [
                    ("方式", self.serial_mode_combo),
                    ("波特率 (bps)", self.baud_spin),
                    ("发生器", self.generator_combo),
                ],
            )
        )
        self.ren_check = CheckBox("REN", card)
        self.sm2_check = CheckBox("SM2", card)
        self.tb8_check = CheckBox("TB8", card)
        self.rb8_check = CheckBox("RB8", card)
        self.smod_check = CheckBox("SMOD", card)
        options = QHBoxLayout()
        options.setSpacing(14)
        for check, tip in (
            (self.ren_check, "允许接收"),
            (self.sm2_check, "多机通信控制位，仅方式 2/3 有效"),
            (self.tb8_check, "发送第 9 位，仅方式 2/3 有效"),
            (self.rb8_check, "接收第 9 位，仅方式 2/3 有效"),
            (self.smod_check, "波特率倍频位"),
        ):
            check.setToolTip(tip)
            options.addWidget(check)
        options.addStretch(1)
        layout.addLayout(options)
        self.baud_label = CaptionLabel("波特率尚未计算", card)
        self.baud_label.setWordWrap(True)
        layout.addWidget(self.baud_label)
        return card

    def _build_code_card(self) -> QWidget:
        card, layout = section_card("C 代码预览",
                                    "生成结果包含寄存器设置、中断服务函数与 main 框架；注释为中文，可直接粘贴到 Keil。")
        header = QHBoxLayout()
        header.addWidget(BodyLabel("输出预览", card))
        header.addStretch(1)
        self.copy_button = PushButton("复制", card)
        header.addWidget(self.copy_button)
        layout.addLayout(header)
        self.code_preview = PlainTextEdit(card)
        self.code_preview.setReadOnly(True)
        self.code_preview.setFont(_mono_font())
        self.code_preview.setMinimumHeight(220)
        self.code_preview.setPlaceholderText("点击“生成代码”后在此预览 C 源文件")
        layout.addWidget(self.code_preview, 1)
        self.highlighter = C51Highlighter(self.code_preview.document())
        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        folder_row.addWidget(BodyLabel("输出目录", card))
        self.output_dir_edit = LineEdit(card)
        self.output_dir_edit.setPlaceholderText("留空则导出时再选择目录")
        self.output_dir_edit.setClearButtonEnabled(True)
        folder_row.addWidget(self.output_dir_edit, 1)
        self.output_dir_button = ToolButton(card)
        self.output_dir_button.setIcon(FluentIcon.FOLDER)
        self.output_dir_button.setToolTip("选择 C 文件或 Keil 工程的输出目录")
        folder_row.addWidget(self.output_dir_button)
        layout.addLayout(folder_row)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.generate_button = PrimaryPushButton("生成代码", card)
        self.export_c_button = PushButton("导出 C", card)
        self.export_keil_button = PushButton("导出 Keil 工程", card)
        actions.addWidget(self.generate_button)
        actions.addWidget(self.export_c_button)
        actions.addWidget(self.export_keil_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.status_label = CaptionLabel("修改参数后会自动刷新代码与警告。", card)
        self.status_label.setWordWrap(True)
        self.error_label = CaptionLabel("", card)
        self.error_label.setWordWrap(True)
        self.error_label.setTextColor(QColor(ERROR_COLOR), QColor(ERROR_COLOR_DARK))
        self.error_label.setVisible(False)
        layout.addWidget(self.status_label)
        layout.addWidget(self.error_label)
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        return card

    def _build_warning_card(self) -> QWidget:
        card, layout = section_card("检查与警告",
                                    "定时器越界、波特率误差、方式冲突与建议会在这里汇总。")
        self.warning_count_label = CaptionLabel("", card)
        layout.addWidget(self.warning_count_label)
        self.warning_view = PlainTextEdit(card)
        self.warning_view.setReadOnly(True)
        self.warning_view.setMinimumHeight(70)
        self.warning_view.setMaximumHeight(150)
        self.warning_view.setPlaceholderText("尚未发现警告")
        layout.addWidget(self.warning_view)
        return card

    def _connect_signals(self) -> None:
        for button, value in zip(self.preset_buttons, self.crystal_presets):
            button.clicked.connect(
                lambda _checked=False, preset=value: self._on_preset_clicked(preset)
            )
        for signal in (
            self.crystal_spin.valueChanged, self.delay_spin.valueChanged,
            self.baud_spin.valueChanged, self.double_speed_switch.checkedChanged,
            self.ren_check.toggled, self.sm2_check.toggled, self.tb8_check.toggled,
            self.rb8_check.toggled, self.smod_check.toggled,
        ):
            signal.connect(self._on_changed)
        for combo in (
            self.power_combo, self.serial_mode_combo, self.generator_combo,
            self.ext0_trigger_combo, self.ext1_trigger_combo,
            *self.timer_mode_combos, *self.priority_combos.values(),
        ):
            combo.currentIndexChanged.connect(self._on_changed)
        for check in (
            *self.interrupt_checks.values(), *self.timer_gate_checks,
            *self.timer_run_checks, *self.timer_reload_checks,
            *(check for checks in self.port_checks.values() for check in checks),
        ):
            check.toggled.connect(self._on_changed)
        for spin in (*self.timer_time_spins, *self.timer_reload_spins):
            spin.valueChanged.connect(self._on_changed)
        for signal, slot in (
            (self.output_dir_edit.textChanged, self._on_output_dir_changed),
            (self.output_dir_button.clicked, self._on_browse_clicked),
            (self.generate_button.clicked, self._on_generate_clicked),
            (self.copy_button.clicked, self._on_copy_clicked),
            (self.export_c_button.clicked, self._on_export_c_clicked),
            (self.export_keil_button.clicked, self._on_export_keil_clicked),
            (self.save_shortcut.activated, self._on_save_shortcut),
        ):
            signal.connect(slot)

    # ---- 配置 ---------------------------------------------------------

    def current_config(self) -> C51Config:
        serial = SerialConfig(
            mode=int(_combo_data(self.serial_mode_combo, 1)),
            baud=int(self.baud_spin.value()),
            ren=self.ren_check.isChecked(), sm2=self.sm2_check.isChecked(),
            tb8=self.tb8_check.isChecked(), rb8=self.rb8_check.isChecked(),
            smod=self.smod_check.isChecked(),
            generator=str(_combo_data(self.generator_combo, "t1")),
        )
        timers = [
            TimerConfig(
                mode=int(_combo_data(self.timer_mode_combos[index], 1)),
                enabled=self.timer_run_checks[index].isChecked(),
                gate=self.timer_gate_checks[index].isChecked(),
                time_us=int(self.timer_time_spins[index].value()),
                reload=int(self.timer_reload_spins[index].value())
                if self.timer_reload_checks[index].isChecked() else None,
            )
            for index in range(3)
        ]
        interrupts = InterruptConfig(
            **{key: check.isChecked() for key, check in self.interrupt_checks.items()},
            high_priority={
                key
                for key in HIGH_PRIORITY_KEYS
                if self.interrupt_checks[key].isChecked()
                and _combo_data(self.priority_combos[key], "low") == "high"
            },
            ext0_edge=_combo_data(self.ext0_trigger_combo, "level") == "edge",
            ext1_edge=_combo_data(self.ext1_trigger_combo, "level") == "edge",
        )
        return C51Config(
            crystal_mhz=round(float(self.crystal_spin.value()), 4),
            double_speed=self.double_speed_switch.isChecked(),
            ports={name: self.port_value(name) for name in PORT_NAMES},
            timers=timers,
            serial=serial,
            interrupts=interrupts,
            power_mode=str(_combo_data(self.power_combo, "normal")),
            delay_us=int(self.delay_spin.value()),
        )

    config = current_config

    def set_config(self, cfg: C51Config) -> None:
        """把配置写回控件；外部调用时会刷新代码预览。"""
        previous = self._loading
        self._loading = True
        try:
            self.crystal_spin.setValue(max(0.1, min(100.0, float(cfg.crystal_mhz))))
            self.double_speed_switch.setChecked(bool(cfg.double_speed))
            _set_combo_data(self.power_combo, cfg.power_mode)
            self.delay_spin.setValue(max(0, min(MAX_TIMER_US, int(cfg.delay_us))))
            for name in PORT_NAMES:
                self.set_port_value(name, cfg.ports.get(name, 0))
            for index in range(3):
                timer = cfg.timers[index] if index < len(cfg.timers) else TimerConfig()
                _set_combo_data(self.timer_mode_combos[index], int(timer.mode))
                self.timer_gate_checks[index].setChecked(bool(timer.gate))
                self.timer_run_checks[index].setChecked(bool(timer.enabled))
                self.timer_time_spins[index].setValue(max(0, min(MAX_TIMER_US, int(timer.time_us))))
                self.timer_reload_checks[index].setChecked(timer.reload is not None)
                self.timer_reload_spins[index].setValue(
                    max(0, min(0xFFFF, int(timer.reload))) if timer.reload is not None else 0)
            serial = cfg.serial
            _set_combo_data(self.serial_mode_combo, int(serial.mode))
            self.baud_spin.setValue(max(300, min(4_000_000, int(serial.baud))))
            self.ren_check.setChecked(bool(serial.ren))
            self.sm2_check.setChecked(bool(serial.sm2))
            self.tb8_check.setChecked(bool(serial.tb8))
            self.rb8_check.setChecked(bool(serial.rb8))
            self.smod_check.setChecked(bool(serial.smod))
            _set_combo_data(self.generator_combo, serial.generator)
            interrupts = cfg.interrupts
            for key, check in self.interrupt_checks.items():
                check.setChecked(bool(getattr(interrupts, key)))
            for key, combo in self.priority_combos.items():
                _set_combo_data(combo, "high" if key in interrupts.high_priority else "low")
            _set_combo_data(self.ext0_trigger_combo, "edge" if interrupts.ext0_edge else "level")
            _set_combo_data(self.ext1_trigger_combo, "edge" if interrupts.ext1_edge else "level")
        finally:
            self._loading = previous
        self._sync_port_labels()
        if not previous:
            self.refresh()

    def set_port_value(self, name: str, value: int) -> None:
        checks = self.port_checks.get(name)
        if checks is None:
            raise ValueError(f"未知端口 {name!r}")
        value = int(value) & 0xFF
        for bit, check in enumerate(checks):
            blocked = check.blockSignals(True)
            try:
                check.setChecked(bool(value & (1 << bit)))
            finally:
                check.blockSignals(blocked)
        self._sync_port_labels()

    def port_value(self, name: str) -> int:
        checks = self.port_checks.get(name)
        if checks is None:
            raise ValueError(f"未知端口 {name!r}")
        return sum(1 << bit for bit, check in enumerate(checks) if check.isChecked())

    # ---- 配置持久化 ---------------------------------------------------

    def save_settings(self) -> None:
        if self.settings is None or self._loading:
            return
        try:
            cfg = self.current_config()
            values = {f"{SETTINGS_PREFIX}{key}": value for key, value in cfg.to_dict().items()}
            values[SETTINGS_PREFIX + "output_dir"] = self.output_dir_edit.text().strip()
            self.settings.update(values)
            self.settings.save()
        except Exception:
            get_logger().exception("保存 C51 向导参数失败")

    def _restore_settings(self) -> None:
        if self.settings is None:
            self.set_config(C51Config())
            return
        raw = {}
        for key in ("crystal_mhz", "double_speed", "ports", "timers", "serial",
                    "interrupts", "power_mode", "delay_us"):
            value = self.settings.get(SETTINGS_PREFIX + key, None)
            if value is not None:
                raw[key] = value
        try:
            cfg = C51Config.from_dict(raw)
        except Exception:
            get_logger().exception("读取 C51 向导参数失败，使用默认配置")
            cfg = C51Config()
        self.set_config(cfg)
        self.output_dir_edit.setText(str(self.settings.get(SETTINGS_PREFIX + "output_dir", "") or ""))

    # ---- 计算与预览 ---------------------------------------------------

    def refresh(self) -> None:
        """刷新计算标签、警告与代码预览；任何错误只写入内联提示。"""
        try:
            self.generate()
        except Exception as exc:
            get_logger().exception("刷新 C51 向导失败")
            self._set_error(str(exc).strip() or type(exc).__name__)

    def generate(self) -> str:
        cfg = self.current_config()
        warnings = self._refresh_computed(cfg)
        return self._generate_from(cfg, warnings)

    def _generate_from(self, cfg: C51Config, warnings: list[str]) -> str:
        text = generate_c(cfg)
        self.code_preview.setPlainText(text)
        suffix = f"，{len(warnings)} 条警告。" if warnings else "。"
        self.status_label.setText(f"已生成 {len(text.splitlines())} 行 C 代码{suffix}")
        self._set_error("")
        return text

    def _refresh_computed(self, cfg: C51Config | None = None) -> list[str]:
        cfg = cfg or self.current_config()
        self._sync_enabled_states()
        self._sync_port_labels()
        registers = build_registers(cfg)
        self._refresh_timer_labels(cfg, registers)
        self._refresh_baud_label(cfg)
        warnings = build_warnings(cfg)
        if warnings:
            self.warning_view.setPlainText(
                "\n".join(f"{index}. {text}" for index, text in enumerate(warnings, 1))
            )
            self.warning_count_label.setText(f"共 {len(warnings)} 条警告")
        else:
            self.warning_view.setPlainText("未发现冲突或越界配置。")
            self.warning_count_label.setText("未发现警告")
        return warnings

    def _refresh_timer_labels(
        self, cfg: C51Config, registers: dict[str, int]
    ) -> None:
        serial = cfg.serial
        uses_t1 = serial.mode in (1, 3) and serial.generator == "t1"
        uses_t2 = serial.mode in (1, 3) and serial.generator == "t2"
        cycle_us = _machine_cycle_us(cfg)
        for index in range(3):
            result = timer_reload(cfg, index)
            th = registers.get(f"TH{index}", result.th)
            tl = registers.get(f"TL{index}", result.tl)
            if index == 2:
                text = f"TH2=0x{th:02X} TL2=0x{tl:02X}"
                if "RCAP2H" in registers:
                    text += (
                        f" RCAP2H=0x{registers['RCAP2H']:02X}"
                        f" RCAP2L=0x{registers['RCAP2L']:02X}"
                    )
                if uses_t2:
                    text += " · 串口波特率发生器"
                else:
                    text += f" · {result.counts} 计数"
                    if not cfg.timers[2].enabled:
                        text += "（未运行）"
            elif index == 1 and uses_t1:
                text = f"TH1=0x{th:02X} TL1=0x{tl:02X} · 串口 T1 波特率"
            else:
                duration = result.counts * cycle_us
                text = (
                    f"TH{index}=0x{th:02X} TL{index}=0x{tl:02X} · "
                    f"{result.counts} 计数 · {_format_duration(duration)}"
                )
                if not cfg.timers[index].enabled:
                    text += "（未运行）"
            self.timer_labels[index].setText(text)
            warnings = [] if (index == 1 and uses_t1) or (index == 2 and uses_t2) else result.warnings
            self.timer_warning_labels[index].setText("；".join(warnings))
            self.timer_warning_labels[index].setVisible(bool(warnings))

    def _refresh_baud_label(self, cfg: C51Config) -> None:
        info = baud_reload(cfg)
        mode = int(info["mode"])
        if mode in (1, 3):
            if cfg.serial.generator == "t2":
                detail = (
                    f"RCAP2H=0x{int(info['rcap2h']):02X} "
                    f"RCAP2L=0x{int(info['rcap2l']):02X}"
                )
            else:
                detail = f"TH1=0x{int(info['th']):02X} TL1=0x{int(info['tl']):02X}"
        elif mode == 0:
            detail = "方式 0 波特率固定为 fosc/12"
        else:
            detail = "方式 2 波特率由 fosc 与 SMOD 固定"
        self.baud_label.setText(
            f"目标 {int(info['requested'])} bps，实际约 {int(info['baud'])} bps"
            f"（误差 {float(info['error_percent']):.2f}%）· {detail}"
        )
        bad = bool(info["warnings"]) or float(info["error_percent"]) > 5.0
        if bad:
            self.baud_label.setTextColor(QColor(ERROR_COLOR), QColor(ERROR_COLOR_DARK))
        else:
            self.baud_label.setTextColor()

    def _sync_enabled_states(self) -> None:
        for key in HIGH_PRIORITY_KEYS:
            self.priority_combos[key].setEnabled(
                self.interrupt_checks[key].isChecked()
            )
        self.ext0_trigger_combo.setEnabled(self.interrupt_checks["ex0"].isChecked())
        self.ext1_trigger_combo.setEnabled(self.interrupt_checks["ex1"].isChecked())
        mode = int(_combo_data(self.serial_mode_combo, 1))
        self.generator_combo.setEnabled(mode in (1, 3))
        ninth_bit = mode in (2, 3)
        self.tb8_check.setEnabled(ninth_bit)
        self.rb8_check.setEnabled(ninth_bit)
        self.smod_check.setEnabled(mode != 0)
        for index in range(3):
            direct = self.timer_reload_checks[index].isChecked()
            self.timer_reload_spins[index].setEnabled(direct)
            self.timer_time_spins[index].setEnabled(not direct)
        self.timer_mode_combos[2].setEnabled(False)
        self.timer_gate_checks[2].setEnabled(False)

    def _sync_port_labels(self) -> None:
        for name in PORT_NAMES:
            self.port_hex_labels[name].setText(f"0x{self.port_value(name):02X}")

    # ---- 导出 ---------------------------------------------------------

    def output_dir(self) -> Path | None:
        text = self.output_dir_edit.text().strip()
        return Path(text) if text else None

    def set_output_dir(self, directory: str | Path) -> None:
        self.output_dir_edit.setText(str(directory))

    def copy_code(self) -> str:
        text = self.generate()
        QApplication.clipboard().setText(text)
        return text

    def export_c(
        self,
        target: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> Path:
        text = self.generate()
        if target is not None:
            path = Path(target)
        else:
            directory = Path(output_dir) if output_dir is not None else self.output_dir()
            if directory is None:
                error = ValueError("请先选择输出目录，或在“输出目录”中填写保存位置。")
                self._set_error(str(error))
                raise error
            path = directory / "Main.c"
        if not path.suffix:
            path = path.with_suffix(".c")
        try:
            write_text_atomic(path, text)
        except OSError as exc:
            message = (
                f"写入文件失败：{path}（{exc}）。请检查磁盘空间、目录权限或文件是否被占用。"
            )
            self._set_error(message)
            raise OSError(message) from exc
        self._set_error("")
        self.status_label.setText(f"已导出 {path.name} 到 {path.parent}。")
        self.save_settings()
        return path

    def export_keil(self, output_dir: str | Path | None = None) -> list[Path]:
        directory = Path(output_dir) if output_dir is not None else self.output_dir()
        if directory is None:
            error = ValueError("请先选择 Keil 工程输出目录。")
            self._set_error(str(error))
            raise error
        cfg = self.current_config()
        self.generate()
        try:
            paths = generate_keil_project(cfg, directory)
        except OSError as exc:
            message = (
                f"写入 Keil 工程失败：{directory}（{exc}）。"
                "请检查磁盘空间、目录权限或文件是否被占用。"
            )
            self._set_error(message)
            raise OSError(message) from exc
        self._set_error("")
        self.status_label.setText(
            f"已导出 Keil 工程到 {directory}：Main.c、M51Project.Uv2、"
            "M51Project.Opt、STARTUP.A51。"
        )
        self.save_settings()
        return paths

    # ---- 事件 ---------------------------------------------------------

    @safe_slot
    def _on_preset_clicked(self, value: float) -> None:
        self.crystal_spin.setValue(float(value))

    @safe_slot
    def _on_changed(self, *_args) -> None:
        if self._loading:
            return
        self.save_settings()
        self.refresh()

    @safe_slot
    def _on_output_dir_changed(self, _text: str = "") -> None:
        if not self._loading:
            self.save_settings()

    @safe_slot
    def _on_browse_clicked(self, *_args) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 C51 输出目录", str(self.output_dir() or Path.home())
        )
        if selected:
            self.output_dir_edit.setText(selected)

    @safe_slot
    def _on_generate_clicked(self, *_args) -> None:
        self.generate()

    @safe_slot
    def _on_copy_clicked(self, *_args) -> None:
        try:
            text = self.copy_code()
        except Exception as exc:
            show_error(self, "复制失败", str(exc))
            return
        show_success(self, "已复制", f"已复制 {len(text.splitlines())} 行 C 代码到剪贴板。")

    @safe_slot
    def _on_export_c_clicked(self, *_args) -> None:
        default = Path(self.output_dir() or Path.home()) / "Main.c"
        path, _selected = QFileDialog.getSaveFileName(
            self, "导出 C 代码", str(default), "C 源文件 (*.c);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            written = self.export_c(target=path)
        except Exception as exc:
            show_error(self, "导出失败", str(exc))
            return
        show_success(self, "导出完成", f"已保存：{written}")

    @safe_slot
    def _on_export_keil_clicked(self, *_args) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 Keil 工程目录", str(self.output_dir() or Path.home())
        )
        if not selected:
            return
        self.output_dir_edit.setText(selected)
        try:
            paths = self.export_keil(selected)
        except Exception as exc:
            show_error(self, "导出失败", str(exc))
            return
        show_success(
            self,
            "导出完成",
            f"已写出 {len(paths)} 个文件到 {Path(selected)}。",
        )

    @safe_slot
    def _on_save_shortcut(self, *_args) -> None:
        if self.output_dir() is None:
            self._on_export_c_clicked()
            return
        try:
            written = self.export_c()
        except Exception as exc:
            show_error(self, "导出失败", str(exc))
            return
        show_success(self, "导出完成", f"已保存：{written}")

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        """C51 向导不保持连接或后台线程。"""
        return False

    def stop_workers(self) -> None:
        """没有后台任务需要停止。"""
        return None

    def closeEvent(self, event) -> None:
        self.save_settings()
        super().closeEvent(event)

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))


# ---- 小工具 -----------------------------------------------------------


def _combo(parent: QWidget, items, width: int) -> ComboBox:
    combo = ComboBox(parent)
    for label, value in items:
        combo.addItem(label, userData=value)
    combo.setFixedWidth(width)
    return combo


def _spin(
    parent: QWidget, low: int, high: int, value: int, width: int = 150,
    suffix: str = "",
) -> SpinBox:
    spin = SpinBox(parent)
    spin.setRange(low, high)
    spin.setValue(value)
    spin.setFixedWidth(width)
    if suffix:
        spin.setSuffix(suffix)
    return spin


def _set_combo_data(combo: ComboBox, value) -> bool:
    index = combo.findData(value)
    if index < 0:
        return False
    blocked = combo.blockSignals(True)
    try:
        combo.setCurrentIndex(index)
    finally:
        combo.blockSignals(blocked)
    return True


def _combo_data(combo: ComboBox, default):
    data = combo.currentData()
    return default if data is None else data


def _control_row(parent: QWidget, items: list[tuple[str, QWidget]]) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    for title, widget in items:
        if title:
            row.addWidget(BodyLabel(title, parent))
        row.addWidget(widget)
        row.addSpacing(8)
    row.addStretch(1)
    return row


def _machine_cycle_us(cfg: C51Config) -> float:
    crystal = max(0.0, float(cfg.crystal_mhz))
    if crystal <= 0:
        return 0.0
    return (6.0 if cfg.double_speed else 12.0) / crystal


def _format_duration(us: float) -> str:
    if us >= 1000:
        return f"{us / 1000.0:.3f} ms"
    return f"{us:.1f} us"


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
    return font
