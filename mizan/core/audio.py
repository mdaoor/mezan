"""WAV/ffmpeg helpers. Segment audio is cut on the fly from normalized.wav by byte offset
(PCM16 @16 kHz mono = 32,000 bytes/s); there are no per-segment files."""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SR = 16000
BYTES_PER_S = SR * 2


@dataclass
class WavInfo:
    sample_rate: int
    channels: int
    sampwidth: int
    data_offset: int
    data_len: int

    @property
    def duration_s(self) -> float:
        return self.data_len / (self.sample_rate * self.channels * self.sampwidth)


def parse_wav(path: Path) -> WavInfo:
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a RIFF/WAVE file")
        fmt = None
        while True:
            h = f.read(8)
            if len(h) < 8:
                raise ValueError("no data chunk")
            cid, clen = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"fmt ":
                _, ch, sr, _, _, bps = struct.unpack("<HHIIHH", f.read(16))
                fmt = (sr, ch, bps // 8)
                f.seek(clen - 16, 1)
            elif cid == b"data":
                off = f.tell()
                n = min(clen, size - off)  # tolerate streamed headers (0xFFFFFFFF)
                if fmt is None:
                    raise ValueError("data before fmt")
                return WavInfo(fmt[0], fmt[1], fmt[2], off, n)
            else:
                f.seek(clen + (clen & 1), 1)


def wav_header(data_len: int, sr: int = SR, channels: int = 1, sampwidth: int = 2) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, channels, sr, sr * channels * sampwidth, channels * sampwidth, sampwidth * 8)
        + b"data" + struct.pack("<I", data_len))


def read_pcm(path: Path, info: WavInfo, start_s: float, end_s: float) -> bytes:
    frame = info.channels * info.sampwidth
    a = max(0, int(start_s * info.sample_rate)) * frame
    b = min(info.data_len, int(math.ceil(end_s * info.sample_rate)) * frame)
    if b <= a:
        return b""
    with open(path, "rb") as f:
        f.seek(info.data_offset + a)
        return f.read(b - a)


def cut_wav(path: Path, info: WavInfo, start_s: float, end_s: float) -> bytes:
    pcm = read_pcm(path, info, start_s, end_s)
    return wav_header(len(pcm), info.sample_rate, info.channels, info.sampwidth) + pcm


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_peaks(path: Path, info: WavInfo, per_s: int = 20) -> dict:
    mm = np.memmap(path, dtype="<i2", mode="r", offset=info.data_offset,
                   shape=(info.data_len // (2 * info.channels) * info.channels,))
    x = mm[:: info.channels] if info.channels > 1 else mm
    block = info.sample_rate // per_s
    n = len(x) // block
    if n == 0:
        peaks = [float(np.abs(x).max()) / 32768.0] if len(x) else [0.0]
    else:
        peaks = (np.abs(x[: n * block].reshape(n, block)).max(axis=1) / 32768.0).round(4).tolist()
    return {"sample_rate": info.sample_rate, "peaks_per_s": per_s, "duration_s": info.duration_s, "peaks": peaks}


async def _run(*args: str) -> tuple[int, bytes]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    return p.returncode, err


async def transcode_to_wav16k(src: Path, dst: Path) -> None:
    rc, err = await _run("ffmpeg", "-nostdin", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR),
                         "-c:a", "pcm_s16le", "-f", "wav", str(dst))
    if rc != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {err.decode(errors='replace')[-400:]}")


async def make_preview_opus(src_wav: Path, dst: Path) -> bool:
    rc, _ = await _run("ffmpeg", "-nostdin", "-y", "-i", str(src_wav), "-c:a", "libopus", "-b:a", "32k", str(dst))
    return rc == 0


def synth_wav(duration_s: float, freq: float = 0.0, amp: float = 0.3) -> bytes:
    """Silence (freq=0) or a sine tone; used for smoke tests when no real segment is available."""
    n = int(duration_s * SR)
    t = np.arange(n) / SR
    x = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype("<i2") if freq else np.zeros(n, "<i2")
    return wav_header(n * 2) + x.tobytes()


def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    return wav_header(len(pcm)) + pcm


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()
