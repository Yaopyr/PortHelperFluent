"""音频读取与 C 数组生成：WAV 直接读取，MP3 通过本机 ffmpeg 解码。

核心层只依赖标准库；MP3 解码把 ffmpeg 的 WAV 输出放在内存中，不落临时
文件。缺少 ffmpeg 时给出明确中文提示，绝不静默失败。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

FFMPEG_TIMEOUT_S = 120
_SUPPORTED_SUFFIXES = frozenset({".wav", ".mp3"})
_C_KEYWORDS = frozenset(
    {
        "auto",
        "break",
        "case",
        "char",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extern",
        "float",
        "for",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "register",
        "restrict",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "struct",
        "switch",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
        "while",
    }
)


@dataclass(frozen=True)
class AudioData:
    """解码后的 PCM 音频：采样率、声道数、每采样字节数与原始采样字节。"""

    sample_rate: int
    channels: int
    sample_width: int
    frames: bytes


def load_audio(path: Path) -> AudioData:
    """读取 WAV 或 MP3 音频。

    WAV 使用 :mod:`wave` 直接读取，只接受未压缩 PCM；MP3 调用 PATH 中的
    ``ffmpeg`` 解码为 16 位 PCM WAV 后从内存读取。文件缺失、格式不支持或
    解码失败时抛出一句中文错误说明原因与下一步。
    """
    source = Path(path)
    if not source.is_file():
        raise ValueError(
            f"找不到音频文件：{source}。请确认路径是否正确、文件是否已被移动。"
        )
    suffix = source.suffix.casefold()
    if suffix not in _SUPPORTED_SUFFIXES:
        shown = suffix or "（无扩展名）"
        raise ValueError(
            f"不支持的音频格式：{shown}。仅支持 WAV 与 MP3；"
            "其他格式请先转换为 WAV。"
        )
    if suffix == ".mp3":
        return _load_mp3(source)
    return _load_wav(source)


def audio_duration_s(audio: AudioData) -> float:
    """返回音频时长（秒）；参数异常时返回 0.0。"""
    if (
        audio.sample_rate <= 0
        or audio.channels <= 0
        or audio.sample_width <= 0
    ):
        return 0.0
    frame_size = audio.channels * audio.sample_width
    sample_count = len(audio.frames) // frame_size
    return sample_count / audio.sample_rate


def audio_to_c(
    audio: AudioData, name: str, max_samples: int | None = None
) -> str:
    """生成包含音频元数据与 ``const unsigned char name[]`` 的 C 源文件文本。

    ``name`` 会被清洗为合法 C 标识符；``max_samples`` 限制每声道的采样点数
    （``None`` 表示完整导出），便于避免生成过大的源文件。
    """
    identifier = c_identifier(name)
    data, sample_count, total_samples = _limited_frames(audio, max_samples)
    truncated = sample_count < total_samples
    header = (
        f"/* 由 PortHelper Fluent 生成：音频数组 \"{identifier}\""
        f"{'（已按最大采样点数截断）' if truncated else ''} */\n"
        f"/* 采样率 {audio.sample_rate} Hz，声道 {audio.channels}，"
        f"采样位宽 {audio.sample_width} 字节（{audio.sample_width * 8} 位），"
        f"采样点数 {sample_count} */\n"
    )
    defines = (
        f"#define {identifier.upper()}_SAMPLE_RATE {audio.sample_rate}u\n"
        f"#define {identifier.upper()}_CHANNELS {audio.channels}u\n"
        f"#define {identifier.upper()}_SAMPLE_WIDTH {audio.sample_width}u\n"
        f"#define {identifier.upper()}_LENGTH {sample_count}u\n\n"
    )
    declarations = (
        f"const unsigned int {identifier}_sample_rate = {audio.sample_rate};\n"
        f"const unsigned int {identifier}_channels = {audio.channels};\n"
        f"const unsigned int {identifier}_sample_width = {audio.sample_width};\n"
        f"const unsigned int {identifier}_length = {sample_count};\n\n"
    )
    return header + defines + declarations + _c_array(identifier, data)


def audio_to_h(
    audio: AudioData, name: str, max_samples: int | None = None
) -> str:
    """生成与 :func:`audio_to_c` 配套的头文件文本（宏与 extern 声明）。"""
    identifier = c_identifier(name)
    _data, sample_count, total_samples = _limited_frames(audio, max_samples)
    guard = f"{identifier.upper()}_H"
    truncated = sample_count < total_samples
    return (
        f"/* 由 PortHelper Fluent 生成：音频数组 \"{identifier}\""
        f"{'（已按最大采样点数截断）' if truncated else ''} */\n"
        f"#ifndef {guard}\n"
        f"#define {guard}\n\n"
        f"#define {identifier.upper()}_SAMPLE_RATE {audio.sample_rate}u\n"
        f"#define {identifier.upper()}_CHANNELS {audio.channels}u\n"
        f"#define {identifier.upper()}_SAMPLE_WIDTH {audio.sample_width}u\n"
        f"#define {identifier.upper()}_LENGTH {sample_count}u\n\n"
        f"extern const unsigned int {identifier}_sample_rate;\n"
        f"extern const unsigned int {identifier}_channels;\n"
        f"extern const unsigned int {identifier}_sample_width;\n"
        f"extern const unsigned int {identifier}_length;\n"
        f"extern const unsigned char {identifier}[];\n\n"
        f"#endif /* {guard} */\n"
    )


def c_identifier(name: str, fallback: str = "audio") -> str:
    """把任意名称清洗为合法、简短的 C 标识符。"""
    text = re.sub(r"[^0-9A-Za-z_]", "_", str(name))
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"_{text}"
    if text in _C_KEYWORDS:
        text = f"{text}_"
    return text[:60]


def _load_wav(path: Path) -> AudioData:
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            compression = reader.getcomptype()
            if compression != "NONE":
                raise ValueError(
                    f"WAV 文件“{path}”使用压缩编码（{compression}），"
                    "仅支持未压缩 PCM WAV。请先用音频软件转换为 PCM WAV。"
                )
            data = reader.readframes(reader.getnframes())
    except wave.Error as exc:
        raise ValueError(
            f"无法读取 WAV 文件“{path}”（{exc}）。"
            "文件可能已损坏，或不是未压缩 PCM WAV。"
        ) from exc
    except EOFError as exc:
        raise ValueError(f"WAV 文件“{path}”数据不完整，无法读取。") from exc
    return _checked_audio(sample_rate, channels, sample_width, data, path)


def _load_mp3(path: Path) -> AudioData:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "未找到 ffmpeg，无法解码 MP3。请安装 ffmpeg 并把它加入 PATH，"
            "或改用 WAV 文件。"
        )
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "wav",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_S,
            check=False,
            creationflags=_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg 无法启动，可能已被移动或删除。请重新安装 ffmpeg 并确认它在 PATH 中。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"解码 MP3 超时（超过 {FFMPEG_TIMEOUT_S} 秒）：{path}。"
            "文件可能过大或已损坏。"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        detail = detail.splitlines()[-1] if detail else "未知错误"
        raise ValueError(
            f"ffmpeg 解码 MP3 失败：{path}（{detail}）。"
            "请确认文件是有效的 MP3，或改用 WAV 文件。"
        )
    if not completed.stdout:
        raise ValueError(f"ffmpeg 未输出音频数据：{path}。文件可能是纯元数据或已损坏。")
    try:
        with wave.open(io.BytesIO(completed.stdout), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            data = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(
            f"无法解析 ffmpeg 输出的 WAV 数据：{path}（{exc}）。"
        ) from exc
    return _checked_audio(sample_rate, channels, sample_width, data, path)


def _checked_audio(
    sample_rate: int,
    channels: int,
    sample_width: int,
    data: bytes,
    path: Path,
) -> AudioData:
    if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
        raise ValueError(f"音频参数无效：{path}（采样率/声道/位宽必须大于 0）。")
    if not data:
        raise ValueError(f"音频文件不包含采样数据：{path}。")
    return AudioData(
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=data,
    )


def _limited_frames(
    audio: AudioData, max_samples: int | None
) -> tuple[bytes, int, int]:
    if audio.channels <= 0 or audio.sample_width <= 0:
        raise ValueError("音频声道数或采样位宽无效。")
    frame_size = audio.channels * audio.sample_width
    total_samples = len(audio.frames) // frame_size
    if max_samples is None:
        sample_count = total_samples
    else:
        try:
            limit = int(max_samples)
        except (TypeError, ValueError) as exc:
            raise ValueError("最大采样点数必须是整数。") from exc
        if limit < 0:
            raise ValueError("最大采样点数不能为负数。")
        sample_count = min(total_samples, limit)
    byte_count = sample_count * frame_size
    return audio.frames[:byte_count], sample_count, total_samples


def _c_array(identifier: str, data: bytes) -> str:
    lines: list[str] = []
    if data:
        for start in range(0, len(data), 12):
            chunk = data[start : start + 12]
            values = ", ".join(f"0x{value:02X}" for value in chunk)
            lines.append(f"    {values}")
        body = ",\n".join(lines)
        array_text = f"const unsigned char {identifier}[] = {{\n{body}\n}};\n"
    else:
        array_text = f"const unsigned char {identifier}[] = {{ 0x00 }}; /* 空音频 */\n"
    return array_text


def _creation_flags() -> int:
    """Windows 下隐藏 ffmpeg 控制台窗口；其他平台为 0。"""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0
