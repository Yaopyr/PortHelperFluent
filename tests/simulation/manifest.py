"""功能清单：自动化场景、人工验收与文档明确不支持项。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    id: str
    page: str
    title: str
    kind: str = "auto"  # auto / manual / na
    existing_tests: tuple[str, ...] = ()
    note: str = ""


FEATURES: tuple[Feature, ...] = (
    Feature("SYS-01", "启动", "主窗口装配、11 页切换与 smoke_test", existing_tests=("tests/test_main_window.py",)),
    Feature("SYS-02", "启动", "退出确认 No/Yes 与后台线程清理", existing_tests=("tests/test_main_window.py",)),
    Feature("SYS-03", "启动", "--selftest 子进程自检", existing_tests=("tests/test_selftest.py",)),
    Feature("SYS-04", "启动", "--smoke-test 子进程冒烟", existing_tests=("tests/test_selftest.py",)),
    Feature("SYS-05", "启动", "损坏配置备份与默认值恢复", existing_tests=("tests/test_settings.py",)),
    Feature("SER-01", "串口", "端口枚举、参数与持久化", existing_tests=("tests/test_serial_page.py",)),
    Feature("SER-02", "串口", "打开/关闭串口与打开失败", existing_tests=("tests/test_serial_worker.py", "tests/test_serial_page.py")),
    Feature("SER-03", "串口", "文本/HEX/换行/编码发送与非法输入", existing_tests=("tests/test_serial_page.py",)),
    Feature("SER-04", "串口", "接收日志、暂停、时间戳、清空与计数", existing_tests=("tests/test_log_pane.py", "tests/test_serial_page.py")),
    Feature("SER-05", "串口", "DTR/RTS/BREAK 与 modem 状态", existing_tests=("tests/test_serial_worker.py", "tests/test_serial_page.py")),
    Feature("SER-06", "串口", "连续发送启停", existing_tests=("tests/test_serial_page.py",)),
    Feature("SER-07", "串口", "文件发送确认链、短写与取消", existing_tests=("tests/test_serial_worker.py",)),
    Feature("SER-08", "串口", "多项发送、会话导出与日志保存", existing_tests=("tests/test_serial_page.py", "tests/test_session_log.py")),
    Feature("BT-01", "蓝牙", "BLE 扫描、过滤、排序、收藏/重命名", existing_tests=("tests/test_bluetooth_page.py",)),
    Feature("BT-02", "蓝牙", "BLE 连接、MTU/RSSI、断开与自动连接", existing_tests=("tests/test_ble_worker.py", "tests/test_bluetooth_page.py")),
    Feature("BT-03", "蓝牙", "服务树、读取与 UUID 复制", existing_tests=("tests/test_bluetooth_page.py",)),
    Feature("BT-04", "蓝牙", "写入、通知、空通知与连续发送", existing_tests=("tests/test_ble_worker.py", "tests/test_bluetooth_page.py")),
    Feature("BT-05", "蓝牙", "MTU 分片与 BLE 文件发送", existing_tests=("tests/test_bluetooth_page.py",)),
    Feature("BT-06", "蓝牙", "SPP 扫描、过滤与配对状态", existing_tests=("tests/test_spp_worker.py",)),
    Feature("BT-07", "蓝牙", "SPP PIN/确认/拒绝/超时配对", existing_tests=("tests/test_spp_worker.py", "tests/test_bluetooth_page.py")),
    Feature("BT-08", "蓝牙", "RFCOMM/COM 回退收发、文件与会话", existing_tests=("tests/test_spp_worker.py", "tests/test_bluetooth_page.py")),
    Feature("NET-01", "网络客户端", "TCP 回环连接/收发/断开", existing_tests=("tests/test_net_client.py", "tests/test_net_client_page.py")),
    Feature("NET-02", "网络客户端", "TCP 错误路径与非法输入", existing_tests=("tests/test_net_client_page.py",)),
    Feature("NET-03", "网络客户端", "UDP 绑定/收发/域名异步解析", existing_tests=("tests/test_net_client.py",)),
    Feature("NET-04", "网络客户端", "Ping、域名解析与网卡信息", existing_tests=("tests/test_net_tools.py", "tests/test_net_client_page.py")),
    Feature("NET-05", "网络客户端", "文件/连续发送与会话导出", existing_tests=("tests/test_net_client_page.py", "tests/test_session_log.py")),
    Feature("SRV-01", "网络服务器", "监听、停止与实际端口", existing_tests=("tests/test_net_server.py", "tests/test_net_server_page.py")),
    Feature("SRV-02", "网络服务器", "客户端表、单发、广播与计数", existing_tests=("tests/test_net_server.py", "tests/test_net_server_page.py")),
    Feature("SRV-03", "网络服务器", "断开客户端、文件发送与会话", existing_tests=("tests/test_net_server_page.py",)),
    Feature("SRV-04", "网络服务器", "端口占用、最大客户端与空发送", existing_tests=("tests/test_net_server.py",)),
    Feature("HID-01", "USB HID", "枚举、搜索、VID/PID/Usage/接口", existing_tests=("tests/test_hid_worker.py", "tests/test_hid_page.py")),
    Feature("HID-02", "USB HID", "打开/关闭、报告 ID/长度与打开失败", existing_tests=("tests/test_hid_page.py",)),
    Feature("HID-03", "USB HID", "报告读写、补零/截断、拔出与连续发送", existing_tests=("tests/test_hid_worker.py", "tests/test_hid_page.py")),
    Feature("HID-04", "USB HID", "HID 文件发送与取消", existing_tests=("tests/test_hid_page.py",)),
    Feature("HID-05", "USB HID", "会话记录、计数、清空与错误恢复", existing_tests=("tests/test_hid_page.py",)),
    Feature("CHK-01", "校验计算", "16 种校验算法已知向量", existing_tests=("tests/test_checksum.py",)),
    Feature("CHK-02", "校验计算", "HEX/文本/编码与非法输入", existing_tests=("tests/test_checksum.py", "tests/test_checksum_page.py")),
    Feature("CHK-03", "校验计算", "源码预览与复制", existing_tests=("tests/test_checksum_page.py",)),
    Feature("CHK-04", "校验计算", "自定义 CRC 参数与计算", existing_tests=("tests/test_custom_crc.py",)),
    Feature("CHK-05", "校验计算", "文件流式、进度、取消与缺失文件", existing_tests=("tests/test_checksum_page.py",)),
    Feature("CNV-01", "编码转换", "2–36 进制、32/64 位与补码", existing_tests=("tests/test_convert_core.py", "tests/test_convert_page.py")),
    Feature("CNV-02", "编码转换", "Bin/Hex/文本互转与文件", existing_tests=("tests/test_encoding_basic.py", "tests/test_convert_page.py")),
    Feature("CNV-03", "编码转换", "七段数码管共阴/共阳", existing_tests=("tests/test_encoding_basic.py",)),
    Feature("CNV-04", "编码转换", "调色板 RGB565/RGB332", existing_tests=("tests/test_convert_core.py",)),
    Feature("CNV-05", "编码转换", "GIF 载入、播放与 BMP 导出", existing_tests=("tests/test_gif_tools.py",)),
    Feature("CNV-06", "编码转换", "WAV 载入与 C/H 导出", existing_tests=("tests/test_audio_tools.py",)),
    Feature("CNV-07", "编码转换", "二维码参数、预览与保存", existing_tests=("tests/test_qr_tools.py",)),
    Feature("CNV-08", "编码转换", "7 个子工具切换与持久化", existing_tests=("tests/test_convert_page.py",)),
    Feature("IMG-01", "图片取模", "单图/文件夹载入与预览", existing_tests=("tests/test_imaging.py", "tests/test_image_page.py")),
    Feature("IMG-02", "图片取模", "C/BIN/LVGL 格式、扫描/方向/位深/字节序", existing_tests=("tests/test_imaging.py",)),
    Feature("IMG-03", "图片取模", "缩放、阈值、反色、背景与比例锁定", existing_tests=("tests/test_image_page.py",)),
    Feature("IMG-04", "图片取模", "单张/批量导出与取消", existing_tests=("tests/test_image_page.py",)),
    Feature("IMG-05", "图片取模", "坏文件、空目录与参数持久化", existing_tests=("tests/test_image_page.py",)),
    Feature("DMX-01", "点阵生成", "文字矩阵、分页与参数", existing_tests=("tests/test_fontmatrix.py", "tests/test_dotmatrix_page.py")),
    Feature("DMX-02", "点阵生成", "BMP 载入、阈值与反色", existing_tests=("tests/test_fontmatrix.py",)),
    Feature("DMX-03", "点阵生成", "翻页、单元格编辑、重置与复制", existing_tests=("tests/test_dotmatrix_page.py",)),
    Feature("DMX-04", "点阵生成", "C/BIN 导出与文件夹批量", existing_tests=("tests/test_dotmatrix_page.py",)),
    Feature("DMX-05", "点阵生成", "字体回退、错误与参数持久化", existing_tests=("tests/test_dotmatrix_page.py",)),
    Feature("C51-01", "C51 向导", "默认配置、代码与警告", existing_tests=("tests/test_c51.py", "tests/test_c51_page.py")),
    Feature("C51-02", "C51 向导", "晶振、定时器 0/1/2 与端口", existing_tests=("tests/test_c51.py",)),
    Feature("C51-03", "C51 向导", "串口方式、波特率与 SMOD", existing_tests=("tests/test_c51.py",)),
    Feature("C51-04", "C51 向导", "中断、C 导出与 Keil 工程", existing_tests=("tests/test_c51_page.py",)),
    Feature("C51-05", "C51 向导", "不可写目录、复制与配置持久化", existing_tests=("tests/test_c51_page.py",)),
    Feature("SET-01", "设置", "主题与强调色", existing_tests=("tests/test_settings_page.py",)),
    Feature("SET-02", "设置", "行为设置与越界 clamp", existing_tests=("tests/test_settings_page.py",)),
    Feature("SET-03", "设置", "配置导出/重置/打开目录", existing_tests=("tests/test_settings_page.py",)),
    Feature("SET-04", "设置", "检查更新、最新/新版本/失败与自动检查", existing_tests=("tests/test_settings_page.py", "tests/test_updates.py")),
    Feature("SET-05", "设置", "下载进度、成功与失败", existing_tests=("tests/test_settings_page.py", "tests/test_update_install.py")),
    Feature("SET-06", "设置", "安装确认、不可替换与退出安装", existing_tests=("tests/test_settings_page.py", "tests/test_update_install.py")),
)

MANUAL_FEATURES: tuple[Feature, ...] = (
    Feature("MAN-01", "人工", "真实串口枚举/收发/DTR/RTS/BREAK", kind="manual", note="需要 USB 串口或回环线"),
    Feature("MAN-02", "人工", "真实 HID 枚举/报告读写/拔出恢复", kind="manual", note="需要标准 HID 设备"),
    Feature("MAN-03", "人工", "真实 BLE 扫描/连接/读写/通知/MTU", kind="manual", note="需要 BLE 透传模块"),
    Feature("MAN-04", "人工", "真实 SPP 配对/RFCOMM/系统 COM 回退", kind="manual", note="需要经典蓝牙模块"),
    Feature("MAN-05", "人工", "真实网络/防火墙/DNS/Ping/网卡", kind="manual", note="需要外部主机与网络"),
    Feature("MAN-06", "人工", "校验结果与独立工具对比", kind="manual", note="独立 CRC/Intel HEX 工具"),
    Feature("MAN-07", "人工", "图片输出在目标屏/查看器正确", kind="manual", note="真实显示设备"),
    Feature("MAN-08", "人工", "点阵输出在目标面板正确", kind="manual", note="真实 LED/OLED 面板"),
    Feature("MAN-09", "人工", "Keil 打开 M51Project.Uv2 并编译", kind="manual", note="需要 Keil uVision"),
    Feature("MAN-10", "人工", "真实更新下载/SHA-256/替换/重启", kind="manual", note="需发布 Release 与打包 exe"),
    Feature("MAN-11", "人工", "无 Python 环境 exe 自检/冒烟", kind="manual", note="干净 Windows 目录"),
    Feature("MAN-12", "人工", "WAV/MP3 实际播放与停止", kind="manual", note="需要声卡"),
    Feature("MAN-13", "人工", "浅深色/Mica/DPI/长文本视觉", kind="manual", note="真实桌面显示"),
    Feature("MAN-14", "人工", "便携目录与只读目录回退", kind="manual", note="需要可写性控制"),
)

NA_FEATURES: tuple[Feature, ...] = (
    Feature("NA-01", "范围外", "CH372/CH375（厂商 DLL）", kind="na"),
    Feature("NA-02", "范围外", "内核串口监视（32 位驱动）", kind="na"),
    Feature("NA-03", "范围外", "Keil 配色一键替换", kind="na"),
    Feature("NA-04", "范围外", "独立蓝牙助手程序", kind="na"),
)

AUTO_FEATURES = tuple(feature for feature in FEATURES if feature.kind == "auto")
BY_ID = {feature.id: feature for feature in FEATURES + MANUAL_FEATURES + NA_FEATURES}


def validate_manifest() -> None:
    ids = [feature.id for feature in FEATURES + MANUAL_FEATURES + NA_FEATURES]
    duplicates = {item for item in ids if ids.count(item) > 1}
    if duplicates:
        raise ValueError(f"manifest 中存在重复功能 ID：{sorted(duplicates)}")
