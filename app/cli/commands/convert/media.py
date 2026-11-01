"""``convert`` 的媒体子命令：``gif``、``audio``、``qr``。

- ``gif``：GIF 拆帧，每帧导出为 ``<stem>_0001.bmp``；
- ``audio``：WAV（PCM）或 MP3（需要本机 ffmpeg）→ ``<name>.c`` 与 ``<name>.h``；
- ``qr``：文本 → PNG/BMP 二维码。

解码、拆帧与编码全部来自 :mod:`app.core.gif_tools`、:mod:`app.core.audio_tools`
与 :mod:`app.core.qr_tools`（与 GUI 同一套实现），本模块只做参数映射、错误码
转换与结果组装。

这些核心模块会连带加载 Pillow/qrcode（MP3 还要调用 ffmpeg），因此一律在处理器
内部局部导入：``import app.cli.main`` 只构建解析器，不会把媒体依赖带进 CLI 进程。
解析器里的允许集合同样硬编码，避免构造解析器时导入核心模块。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.cli.errors import EXIT_OPERATION, CliError
from app.cli.output import add_output_options
from app.services.exports import sanitize_name, write_text_atomic

__all__ = ["register", "run_audio", "run_gif", "run_qr"]

# 与 ``app.core.qr_tools.EC_LEVELS`` 的键保持一致（硬编码以保持导入轻量）。
EC_LEVELS = ("L", "M", "Q", "H")


def register(subparsers) -> None:
    """把 ``gif``/``audio``/``qr`` 三个媒体子命令挂到 ``convert`` 的子命令解析器上。"""
    _register_gif(subparsers)
    _register_audio(subparsers)
    _register_qr(subparsers)


# ---------------------------------------------------------------------------
# gif
# ---------------------------------------------------------------------------


def _register_gif(subparsers) -> None:
    parser = subparsers.add_parser(
        "gif",
        help="GIF 拆帧：每一帧导出为一个 BMP。",
        description="读取 GIF 的所有帧，按 --stem 命名导出为 BMP；可用 --width/--height 缩放。",
    )
    parser.add_argument("--file", required=True, metavar="GIF", help="输入 GIF 文件路径。")
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="输出目录，不存在时自动创建。",
    )
    parser.add_argument(
        "--width",
        type=int,
        metavar="N",
        help="输出宽度（像素）；只给宽度时按原图比例缩放。",
    )
    parser.add_argument(
        "--height",
        type=int,
        metavar="N",
        help="输出高度（像素）；只给高度时按原图比例缩放。",
    )
    parser.add_argument(
        "--stem",
        metavar="NAME",
        help="输出文件名前缀，默认取 GIF 文件名。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_gif, command="convert.gif")


def run_gif(args) -> dict[str, Any]:
    """拆出所有帧并写出 BMP，返回 ``frames``/``width``/``height``/``files``。"""
    from app.core.gif_tools import export_frames, load_gif

    source = Path(getattr(args, "file"))
    out_dir = Path(getattr(args, "out_dir"))
    stem = str(getattr(args, "stem", None) or source.stem or "frame")
    try:
        frames, width, height = load_gif(source)
    except ValueError as exc:
        raise _load_error(source, exc) from exc
    try:
        files = export_frames(
            frames,
            out_dir,
            stem,
            getattr(args, "width", None),
            getattr(args, "height", None),
        )
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    except OSError as exc:
        # ``export_frames`` 的 OSError 已点名目录与原因，直接沿用。
        raise CliError("permission_denied", str(exc), EXIT_OPERATION) from exc
    return {
        "frames": len(frames),
        "width": width,
        "height": height,
        "files": [str(path) for path in files],
    }


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


def _register_audio(subparsers) -> None:
    parser = subparsers.add_parser(
        "audio",
        help="音频转 C 数组：WAV/MP3 导出 .c 与 .h。",
        description="读取 WAV（未压缩 PCM）或 MP3（需要本机 ffmpeg），"
        "写出 <name>.c 与 <name>.h。",
    )
    parser.add_argument(
        "--file",
        required=True,
        metavar="AUDIO",
        help="输入音频路径（.wav 或 .mp3）。",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="输出目录，不存在时自动创建。",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="输出文件名主名与 C 数组名，默认取输入文件名。",
    )
    parser.add_argument(
        "--max-samples",
        dest="max_samples",
        type=int,
        metavar="N",
        help="每声道最多导出的采样点数（默认导出全部）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_audio, command="convert.audio")


def run_audio(args) -> dict[str, Any]:
    """解码音频并写出 C/H，返回采样参数、时长与写出的文件。"""
    from app.core.audio_tools import audio_duration_s, audio_to_c, audio_to_h, load_audio

    source = Path(getattr(args, "file"))
    out_dir = Path(getattr(args, "out_dir"))
    name = str(getattr(args, "name", None) or source.stem or "audio")
    max_samples = getattr(args, "max_samples", None)
    try:
        audio = load_audio(source)
    except ValueError as exc:
        raise _load_error(source, exc) from exc
    except RuntimeError as exc:
        # MP3 需要本机 ffmpeg：缺失属于当前环境不支持，不是输入内容问题。
        raise CliError("unsupported", str(exc), EXIT_OPERATION) from exc
    try:
        c_text = audio_to_c(audio, name, max_samples)
        h_text = audio_to_h(audio, name, max_samples)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc

    base = sanitize_name(name, fallback="audio")
    for target, text in ((out_dir / f"{base}.c", c_text), (out_dir / f"{base}.h", h_text)):
        try:
            write_text_atomic(target, text)
        except OSError as exc:
            raise CliError(
                "permission_denied",
                f"写出音频文件失败：{target}（{exc}）。请检查输出目录与写入权限。",
                EXIT_OPERATION,
            ) from exc
    return {
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "sample_width": audio.sample_width,
        "duration_s": audio_duration_s(audio),
        "files": [str(out_dir / f"{base}.c"), str(out_dir / f"{base}.h")],
    }


# ---------------------------------------------------------------------------
# qr
# ---------------------------------------------------------------------------


def _register_qr(subparsers) -> None:
    parser = subparsers.add_parser(
        "qr",
        help="二维码生成：输出 PNG 或 BMP。",
        description="按文本生成二维码；--out 必须使用 .png 或 .bmp 扩展名。",
    )
    parser.add_argument("--text", required=True, metavar="TEXT", help="要编码的文本。")
    parser.add_argument(
        "--out",
        required=True,
        metavar="PATH",
        help="输出图片路径，必须是 .png 或 .bmp。",
    )
    parser.add_argument(
        "--ec",
        choices=EC_LEVELS,
        default="M",
        metavar="LEVEL",
        help="纠错等级 L/M/Q/H（默认 M）。",
    )
    parser.add_argument(
        "--box-size",
        dest="box_size",
        type=int,
        default=8,
        metavar="N",
        help="每个模块的像素数（1–64，默认 8）。",
    )
    parser.add_argument(
        "--border",
        type=int,
        default=4,
        metavar="N",
        help="静区宽度（模块数，0–32，默认 4）。",
    )
    parser.add_argument(
        "--fg",
        default="#000000",
        metavar="#RRGGBB",
        help="前景色（默认 #000000）。",
    )
    parser.add_argument(
        "--bg",
        default="#FFFFFF",
        metavar="#RRGGBB",
        help="背景色（默认 #FFFFFF）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_qr, command="convert.qr")


def run_qr(args) -> dict[str, Any]:
    """生成二维码并保存，返回 ``text``/``size``/``files``。"""
    from app.core.qr_tools import make_qr, save_qr

    text = str(getattr(args, "text") or "")
    out = Path(getattr(args, "out"))
    try:
        image = make_qr(
            text,
            getattr(args, "ec", "M"),
            getattr(args, "box_size", 8),
            getattr(args, "border", 4),
            getattr(args, "fg", "#000000"),
            getattr(args, "bg", "#FFFFFF"),
        )
        save_qr(image, out)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    except OSError as exc:
        # ``save_qr`` 的 OSError 已点名目标路径与原因，直接沿用。
        raise CliError("permission_denied", str(exc), EXIT_OPERATION) from exc
    return {
        "text": text,
        "size": {"width": image.width, "height": image.height},
        "files": [str(out)],
    }


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def _load_error(source: Path, exc: ValueError) -> CliError:
    """读输入失败：文件不存在用 ``not_found``，其余（格式/内容）用 ``invalid_input``。

    两类都保留核心层的中文原文案，Agent 能直接拿到“路径或格式哪里不对”。
    """
    code = "not_found" if not source.is_file() else "invalid_input"
    return CliError(code, str(exc), EXIT_OPERATION)
