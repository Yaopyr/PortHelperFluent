"""Task 8 核心测试：颜色编码、GIF、音频与二维码。"""

from __future__ import annotations

import array
import math
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.core import audio_tools
from app.core.audio_tools import (
    AudioData,
    audio_duration_s,
    audio_to_c,
    audio_to_h,
    load_audio,
)
from app.core.color import (
    decode_332,
    decode_565,
    hex_to_rgb,
    rgb_to_332,
    rgb_to_565,
    rgb_to_888,
    rgb_to_hex,
)
from app.core.gif_tools import export_frames, load_gif
from app.core.qr_tools import make_qr, save_qr


# ---- 颜色 -----------------------------------------------------------------


def test_color_known_565_values() -> None:
    assert rgb_to_565((255, 0, 0)) == 0xF800
    assert rgb_to_565((0, 255, 0)) == 0x07E0
    assert rgb_to_565((0, 0, 255)) == 0x001F
    assert rgb_to_565((0, 0, 0)) == 0x0000
    assert rgb_to_565((255, 255, 255)) == 0xFFFF
    assert decode_565(0xF800) == (255, 0, 0)
    assert decode_565(0x07E0) == (0, 255, 0)
    assert decode_565(0x001F) == (0, 0, 255)
    assert decode_565(0x0000) == (0, 0, 0)
    assert decode_565(0xFFFF) == (255, 255, 255)


def test_color_known_332_and_888_values() -> None:
    assert rgb_to_332((255, 0, 0)) == 0xE0
    assert rgb_to_332((0, 255, 0)) == 0x1C
    assert rgb_to_332((0, 0, 255)) == 0x03
    assert rgb_to_332((0, 0, 0)) == 0x00
    assert rgb_to_332((255, 255, 255)) == 0xFF
    assert decode_332(0xE0) == (255, 0, 0)
    assert decode_332(0x00) == (0, 0, 0)
    assert decode_332(0xFF) == (255, 255, 255)
    assert rgb_to_888((0x12, 0x34, 0x56)) == 0x123456
    assert rgb_to_888((255, 255, 255)) == 0xFFFFFF


def test_color_hex_parsing_and_formatting() -> None:
    assert hex_to_rgb("#FF0000") == (255, 0, 0)
    assert hex_to_rgb("ff0000") == (255, 0, 0)
    assert hex_to_rgb("#f00") == (255, 0, 0)
    assert hex_to_rgb("0x00FF80") == (0, 255, 128)
    assert rgb_to_hex((0x0A, 0x84, 0xFF)) == "#0A84FF"
    with pytest.raises(ValueError, match="颜色|格式"):
        hex_to_rgb("#12345")
    with pytest.raises(ValueError, match="颜色|格式"):
        hex_to_rgb("GGGGGG")
    with pytest.raises(ValueError, match="0–255|0-255"):
        rgb_to_hex((256, 0, 0))


def test_color_round_trip_within_quantization_error() -> None:
    for red in range(0, 256, 17):
        for green in range(0, 256, 15):
            for blue in range(0, 256, 19):
                code = rgb_to_565((red, green, blue))
                decoded = decode_565(code)
                assert rgb_to_565(decoded) == code
                assert abs(decoded[0] - red) <= 8
                assert abs(decoded[1] - green) <= 4
                assert abs(decoded[2] - blue) <= 8

                code332 = rgb_to_332((red, green, blue))
                decoded332 = decode_332(code332)
                assert rgb_to_332(decoded332) == code332
                assert abs(decoded332[0] - red) <= 32
                assert abs(decoded332[1] - green) <= 32
                assert abs(decoded332[2] - blue) <= 64


def test_color_rejects_out_of_range_codes() -> None:
    with pytest.raises(ValueError, match="RGB565"):
        decode_565(0x10000)
    with pytest.raises(ValueError, match="RGB332"):
        decode_332(0x100)
    with pytest.raises(ValueError, match="RGB332"):
        decode_332(-1)


# ---- GIF ------------------------------------------------------------------


def _write_test_gif(path: Path, durations=(100, 200)) -> None:
    first = Image.new("RGB", (24, 16), (255, 0, 0))
    second = Image.new("RGB", (24, 16), (0, 0, 255))
    first.save(
        path,
        save_all=True,
        append_images=[second],
        duration=list(durations),
        loop=0,
        disposal=2,
    )


def test_load_gif_frame_count_durations_and_size(tmp_path: Path) -> None:
    source = tmp_path / "anim.gif"
    _write_test_gif(source)

    frames, width, height = load_gif(source)

    assert (width, height) == (24, 16)
    assert len(frames) == 2
    assert [frame.index for frame in frames] == [0, 1]
    assert [frame.duration_ms for frame in frames] == [100, 200]
    assert all(frame.image.mode == "RGBA" for frame in frames)
    assert all(frame.image.size == (24, 16) for frame in frames)


def test_export_frames_naming_resize_and_no_temp_files(tmp_path: Path) -> None:
    source = tmp_path / "anim.gif"
    _write_test_gif(source)
    frames, _width, _height = load_gif(source)
    output = tmp_path / "out"

    written = export_frames(frames, output, "anim", width=12, height=8)

    assert [path.name for path in written] == ["anim_0001.bmp", "anim_0002.bmp"]
    assert all(path.is_file() for path in written)
    assert all(Image.open(path).size == (12, 8) for path in written)
    assert sorted(path.suffix for path in output.iterdir()) == [".bmp", ".bmp"]
    assert list(output.glob("*.tmp")) == []


def test_export_frames_single_dimension_keeps_aspect(tmp_path: Path) -> None:
    source = tmp_path / "anim.gif"
    _write_test_gif(source)
    frames, _width, _height = load_gif(source)

    written = export_frames(frames, tmp_path / "scaled", "anim", width=12)

    assert all(Image.open(path).size == (12, 8) for path in written)


def test_export_frames_cancel_and_empty(tmp_path: Path) -> None:
    source = tmp_path / "anim.gif"
    _write_test_gif(source)
    frames, _width, _height = load_gif(source)

    assert export_frames([], tmp_path / "empty", "anim") == []
    assert export_frames(frames, tmp_path / "cancelled", "anim", cancel=lambda: True) == []
    assert list((tmp_path / "cancelled").glob("*")) == []


def test_load_gif_rejects_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="找不到"):
        load_gif(tmp_path / "missing.gif")
    broken = tmp_path / "broken.gif"
    broken.write_bytes(b"not a gif")
    with pytest.raises(ValueError, match="GIF|图像"):
        load_gif(broken)


# ---- 音频 -----------------------------------------------------------------


def _write_test_wav(path: Path, seconds: float = 0.1, rate: int = 8000) -> bytes:
    count = int(seconds * rate)
    samples = array.array(
        "h",
        (int(12000 * math.sin(2 * math.pi * 440 * index / rate)) for index in range(count)),
    )
    data = samples.tobytes()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(data)
    return data


def test_load_wav_duration_and_c_output(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    raw = _write_test_wav(source)

    audio = load_audio(source)

    assert audio.sample_rate == 8000
    assert audio.channels == 1
    assert audio.sample_width == 2
    assert audio.frames == raw
    assert audio_duration_s(audio) == pytest.approx(0.1, abs=1e-6)

    source_text = audio_to_c(audio, "tone")
    assert "8000" in source_text
    assert "tone_length = 800;" in source_text
    assert "const unsigned char tone[]" in source_text
    assert source_text.count("0x") == 1600
    assert audio_to_c(audio, "tone", max_samples=100).count("0x") == 200
    assert "tone_length = 100;" in audio_to_c(audio, "tone", max_samples=100)

    header_text = audio_to_h(audio, "tone")
    assert "tone_length" in header_text
    assert "extern const unsigned char tone[]" in header_text
    assert "#ifndef TONE_H" in header_text


def test_audio_name_is_sanitized_and_empty_audio_rejected(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _write_test_wav(source)
    audio = load_audio(source)
    assert "const unsigned char my_audio[]" in audio_to_c(audio, "my-audio")
    assert "const unsigned char audio[]" in audio_to_c(audio, "中文")

    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
    with pytest.raises(ValueError, match="不包含|空"):
        load_audio(empty)


def test_audio_rejects_unsupported_format_and_broken_wav(tmp_path: Path) -> None:
    ogg = tmp_path / "sound.ogg"
    ogg.write_bytes(b"OggS")
    with pytest.raises(ValueError, match="不支持"):
        load_audio(ogg)

    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav file")
    with pytest.raises(ValueError, match="WAV"):
        load_audio(broken)

    with pytest.raises(ValueError, match="找不到"):
        load_audio(tmp_path / "missing.wav")


def test_mp3_without_ffmpeg_raises_chinese_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "tone.mp3"
    source.write_bytes(b"ID3")
    monkeypatch.setattr(audio_tools.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="ffmpeg"):
        load_audio(source)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
def test_load_mp3_via_ffmpeg(tmp_path: Path) -> None:
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path)
    mp3_path = tmp_path / "tone.mp3"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(mp3_path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert mp3_path.stat().st_size > 0

    audio = load_audio(mp3_path)

    assert audio.sample_rate == 8000
    assert audio.channels == 1
    assert audio.sample_width == 2
    assert 0.05 < audio_duration_s(audio) < 0.5
    assert "const unsigned char tone[]" in audio_to_c(audio, "tone")


def test_audio_duration_handles_invalid_parameters() -> None:
    assert audio_duration_s(AudioData(0, 1, 2, b"\x00\x00")) == 0.0
    assert audio_duration_s(AudioData(8000, 1, 2, b"")) == 0.0


# ---- 二维码 ---------------------------------------------------------------


def test_make_qr_square_border_and_colors() -> None:
    image = make_qr("hello", box_size=4, border=2, fg="#FF0000", bg="#0000FF")

    assert image.mode == "RGBA"
    assert image.width == image.height
    assert image.width % 4 == 0
    assert image.width >= (21 + 2 * 2) * 4
    assert image.getpixel((0, 0)) == (0, 0, 255, 255)
    assert image.getpixel((2 * 4, 2 * 4)) == (255, 0, 0, 255)


@pytest.mark.parametrize("ec", ["L", "M", "Q", "H"])
def test_make_qr_supports_all_error_correction_levels(ec: str) -> None:
    image = make_qr("PortHelper Fluent", ec=ec, box_size=2, border=1)
    assert image.width == image.height
    assert image.width > 0


def test_make_qr_rejects_empty_text_and_bad_parameters() -> None:
    for text in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="不能为空"):
            make_qr(text)
    with pytest.raises(ValueError, match="纠错"):
        make_qr("x", ec="X")
    with pytest.raises(ValueError, match="模块尺寸"):
        make_qr("x", box_size=0)
    with pytest.raises(ValueError, match="边框"):
        make_qr("x", border=-1)
    with pytest.raises(ValueError, match="颜色|格式"):
        make_qr("x", fg="red")


def test_save_qr_png_and_bmp(tmp_path: Path) -> None:
    image = make_qr("保存测试", box_size=3, border=2)

    png_path = tmp_path / "qr.png"
    bmp_path = tmp_path / "qr.bmp"
    save_qr(image, png_path)
    save_qr(image, bmp_path)

    assert png_path.is_file()
    assert bmp_path.is_file()
    with Image.open(png_path) as loaded:
        assert loaded.size == image.size
        assert loaded.mode == "RGBA"
    with Image.open(bmp_path) as loaded:
        assert loaded.size == image.size
        assert loaded.mode == "RGB"

    with pytest.raises(ValueError, match="不支持"):
        save_qr(image, tmp_path / "qr.jpg")
