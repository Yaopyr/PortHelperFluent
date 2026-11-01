"""``convert`` 媒体子命令：gif、audio、qr。

GIF 拆帧、音频解码与二维码生成本身由 ``tests/test_convert_core.py`` /
``tests/test_convert_extra_core.py`` 覆盖；本文件只验证 CLI 契约：参数映射、
输出文件命名与格式、``data`` 字段形状、错误码与退出码，以及媒体路径不把
Pillow/qrcode/PyQt6 带进 ``import app.cli.main``。
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]

GIF_DATA_KEYS = {"frames", "width", "height", "size", "files"}
AUDIO_DATA_KEYS = {
    "sample_rate",
    "channels",
    "sample_width",
    "duration_s",
    "files",
}
QR_DATA_KEYS = {"text", "size", "files"}


def _image_size(size: tuple[int, int]) -> dict[str, int]:
    """Pillow 的 ``(宽, 高)`` 转成 ``data.size`` 的字段形状。"""
    return {"width": size[0], "height": size[1]}


def _write_gif(path: Path, *, size: tuple[int, int] = (4, 4), frames: int = 2) -> Path:
    """写一个多帧 GIF：每帧纯色，避免 Pillow 把相同帧合并。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    colors = [(255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 255, 0)]
    images = [Image.new("RGB", size, colors[index % len(colors)]) for index in range(frames)]
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=100,
        loop=0,
    )
    return path


def _write_wav(
    path: Path,
    *,
    sample_count: int = 8,
    rate: int = 8000,
    channels: int = 1,
    width: int = 2,
) -> Path:
    """写一个未压缩 PCM WAV；默认 8 个 16 位单声道采样点。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * sample_count * channels)
    return path


# ---------------------------------------------------------------------------
# gif
# ---------------------------------------------------------------------------


def test_gif_exports_frames(cli, tmp_path) -> None:
    source = _write_gif(tmp_path / "anim.gif", size=(4, 4), frames=2)
    out_dir = tmp_path / "frames"
    payload = cli(["convert", "gif", "--file", str(source), "--out-dir", str(out_dir)])
    assert payload["exit_code"] == 0
    assert payload["command"] == "convert.gif"

    data = payload["data"]
    assert set(data) == GIF_DATA_KEYS
    assert data["frames"] == 2
    assert data["width"] == 4 and data["height"] == 4
    # 未缩放时 size 与源尺寸一致，但它是“实际写出的 BMP 尺寸”。
    assert data["size"] == {"width": 4, "height": 4}
    assert [Path(path).name for path in data["files"]] == ["anim_0001.bmp", "anim_0002.bmp"]
    for path in data["files"]:
        assert Path(path).suffix == ".bmp"
        assert Path(path).is_file()


def test_gif_stem_and_scaled_size(cli, tmp_path) -> None:
    source = _write_gif(tmp_path / "anim.gif", size=(8, 4), frames=2)
    out_dir = tmp_path / "frames"
    data = cli(
        [
            "convert",
            "gif",
            "--file",
            str(source),
            "--out-dir",
            str(out_dir),
            "--stem",
            "renamed",
            "--width",
            "4",
        ]
    )["data"]
    assert [Path(path).name for path in data["files"]] == [
        "renamed_0001.bmp",
        "renamed_0002.bmp",
    ]
    # 缩放后的实际输出尺寸必须出现在 data 里，Agent 不必再打开 BMP 才知道大小。
    assert data["size"] == {"width": 4, "height": 2}
    # 源尺寸仍是 GIF 原始尺寸。
    assert data["width"] == 8 and data["height"] == 4
    with Image.open(data["files"][0]) as image:
        # 只给宽度时按原图比例缩放：8×4 → 4×2。
        assert image.size == (4, 2)
        assert _image_size(image.size) == data["size"]


def test_gif_missing_file_is_not_found(cli, tmp_path) -> None:
    payload = cli(
        ["convert", "gif", "--file", str(tmp_path / "nope.gif"), "--out-dir", str(tmp_path)]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"
    assert "找不到 GIF 文件" in payload["error"]["message"]


def test_gif_invalid_dimension_is_invalid_input(cli, tmp_path) -> None:
    source = _write_gif(tmp_path / "anim.gif", size=(8, 4), frames=1)
    payload = cli(
        ["convert", "gif", "--file", str(source), "--out-dir", str(tmp_path), "--width", "0"]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"


def test_gif_write_failure_is_permission_denied(cli, tmp_path) -> None:
    source = _write_gif(tmp_path / "anim.gif", size=(4, 4), frames=1)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    payload = cli(
        ["convert", "gif", "--file", str(source), "--out-dir", str(blocked)]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "permission_denied"


def test_gif_requires_out_dir(cli, tmp_path) -> None:
    source = _write_gif(tmp_path / "anim.gif", size=(4, 4), frames=1)
    payload = cli(["convert", "gif", "--file", str(source)])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


def test_wav_exports_c_and_h(cli, tmp_path) -> None:
    source = _write_wav(tmp_path / "tone.wav", sample_count=8, rate=8000, width=2)
    out_dir = tmp_path / "out"
    payload = cli(
        [
            "convert",
            "audio",
            "--file",
            str(source),
            "--out-dir",
            str(out_dir),
            "--name",
            "tone",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "convert.audio"

    data = payload["data"]
    assert set(data) == AUDIO_DATA_KEYS
    assert data["sample_rate"] == 8000
    assert data["channels"] == 1
    assert data["sample_width"] == 2
    assert data["duration_s"] == pytest.approx(8 / 8000)

    c_path, h_path = out_dir / "tone.c", out_dir / "tone.h"
    assert data["files"] == [str(c_path), str(h_path)]
    c_text = c_path.read_text(encoding="utf-8")
    h_text = h_path.read_text(encoding="utf-8")
    assert "#define TONE_SAMPLE_RATE 8000u" in c_text
    assert "#define TONE_LENGTH 8u" in c_text
    assert "const unsigned char tone[]" in c_text
    assert "#define TONE_SAMPLE_RATE 8000u" in h_text
    assert "extern const unsigned char tone[]" in h_text


def test_wav_default_name_and_max_samples(cli, tmp_path) -> None:
    source = _write_wav(tmp_path / "voice.wav", sample_count=8, rate=8000, width=2)
    out_dir = tmp_path / "out"
    data = cli(
        [
            "convert",
            "audio",
            "--file",
            str(source),
            "--out-dir",
            str(out_dir),
            "--max-samples",
            "4",
        ]
    )["data"]
    assert data["files"] == [str(out_dir / "voice.c"), str(out_dir / "voice.h")]
    # 截断后宏取实际导出的采样点数，而不是原始总数。
    assert "#define VOICE_LENGTH 4u" in (out_dir / "voice.c").read_text(encoding="utf-8")


def test_audio_name_strips_known_suffixes(cli, tmp_path) -> None:
    """``--name tone.wav`` 与 image/dotmatrix 的约定一致：去掉后缀，不生成 tone.wav.c。"""
    source = _write_wav(tmp_path / "voice.wav", sample_count=8, rate=8000, width=2)
    out_dir = tmp_path / "out"
    data = cli(
        [
            "convert",
            "audio",
            "--file",
            str(source),
            "--out-dir",
            str(out_dir),
            "--name",
            "tone.wav",
        ]
    )["data"]
    assert data["files"] == [str(out_dir / "tone.c"), str(out_dir / "tone.h")]
    assert not (out_dir / "tone.wav.c").exists()
    assert not (out_dir / "tone.wav.h").exists()
    # 文件名主名与 C 标识符保持一致（tone，而不是 tone_wav）。
    c_text = (out_dir / "tone.c").read_text(encoding="utf-8")
    assert "#define TONE_SAMPLE_RATE 8000u" in c_text
    assert "const unsigned char tone[]" in c_text


def test_unsupported_audio_returns_invalid_input(cli, tmp_path) -> None:
    source = tmp_path / "noise.ogg"
    source.write_bytes(b"not really audio")
    payload = cli(
        ["convert", "audio", "--file", str(source), "--out-dir", str(tmp_path / "out")]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"
    assert "不支持的音频格式" in payload["error"]["message"]


def test_mp3_without_ffmpeg_is_unsupported(cli, tmp_path, monkeypatch) -> None:
    """MP3 需要本机 ffmpeg：缺失说明当前环境不支持，不是音频内容非法。"""
    import shutil

    source = tmp_path / "voice.mp3"
    source.write_bytes(b"ID3 fake mp3")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: None)
    payload = cli(
        ["convert", "audio", "--file", str(source), "--out-dir", str(tmp_path / "out")]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "unsupported"
    assert "ffmpeg" in payload["error"]["message"]


def test_audio_missing_file_is_not_found(cli, tmp_path) -> None:
    payload = cli(
        ["convert", "audio", "--file", str(tmp_path / "nope.wav"), "--out-dir", str(tmp_path)]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"
    assert "找不到音频文件" in payload["error"]["message"]


def test_audio_write_failure_is_permission_denied(cli, tmp_path) -> None:
    source = _write_wav(tmp_path / "tone.wav", sample_count=8, rate=8000, width=2)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    payload = cli(
        ["convert", "audio", "--file", str(source), "--out-dir", str(blocked)]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "permission_denied"


def test_audio_requires_out_dir(cli, tmp_path) -> None:
    source = _write_wav(tmp_path / "tone.wav", sample_count=8, rate=8000, width=2)
    payload = cli(["convert", "audio", "--file", str(source)])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# qr
# ---------------------------------------------------------------------------


def test_qr_writes_png(cli, tmp_path) -> None:
    out = tmp_path / "qr.png"
    payload = cli(["convert", "qr", "--text", "hello", "--out", str(out)])
    assert payload["exit_code"] == 0
    assert payload["command"] == "convert.qr"

    data = payload["data"]
    assert set(data) == QR_DATA_KEYS
    assert data["text"] == "hello"
    assert data["files"] == [str(out)]
    assert out.is_file()
    with Image.open(out) as image:
        assert data["size"] == {"width": image.width, "height": image.height}
        assert image.width == image.height > 0


def test_qr_bmp_with_colors_and_options(cli, tmp_path) -> None:
    out = tmp_path / "qr.bmp"
    payload = cli(
        [
            "convert",
            "qr",
            "--text",
            "hello",
            "--out",
            str(out),
            "--ec",
            "H",
            "--box-size",
            "4",
            "--border",
            "2",
            "--fg",
            "#FF0000",
            "--bg",
            "#00FF00",
        ]
    )
    assert payload["exit_code"] == 0
    assert out.suffix == ".bmp" and out.is_file()
    with Image.open(out) as image:
        assert _image_size(image.size) == payload["data"]["size"]
        # 静区是背景色，因此左上角像素一定是 --bg。
        assert image.convert("RGB").getpixel((0, 0)) == (0, 255, 0)


def test_qr_errors(cli, tmp_path) -> None:
    # 空文本、扩展名不是 .png/.bmp 都按输入内容非法处理。
    payload = cli(["convert", "qr", "--text", "", "--out", str(tmp_path / "qr.png")])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    payload = cli(["convert", "qr", "--text", "hello", "--out", str(tmp_path / "qr.jpg")])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"
    assert "不支持的图片格式" in payload["error"]["message"]

    # 缺少 --text/--out 与非法 --ec 由 argparse 报用法错误。
    payload = cli(["convert", "qr", "--out", str(tmp_path / "qr.png")])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["convert", "qr", "--text", "hello"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(
        ["convert", "qr", "--text", "hello", "--out", str(tmp_path / "qr.png"), "--ec", "X"]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


def test_qr_write_failure_is_permission_denied(cli, tmp_path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    payload = cli(["convert", "qr", "--text", "hello", "--out", str(blocked / "qr.png")])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# 导入契约
# ---------------------------------------------------------------------------


def test_media_commands_import_without_heavy_deps() -> None:
    """媒体命令的解析器构建不能把 Pillow/qrcode/numpy/PyQt6 带进 CLI 进程。"""
    code = (
        "import sys\n"
        "import app.cli.main\n"
        "heavy = [name for name in ('PIL', 'qrcode', 'numpy', 'PyQt6') "
        "if name in sys.modules]\n"
        "print(','.join(heavy) or 'none')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "none"
