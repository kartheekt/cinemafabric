"""Small encoding helpers shared by the fabric, studio and GPU worker."""

import base64
import io
import json
import re
from typing import Any

import numpy as np
from scipy.io import wavfile


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def data_url(b64: str) -> str:
    return f"data:{sniff_mime(b64d(b64))};base64,{b64}"


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    wavfile.write(buf, sample_rate, pcm)
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    sample_rate, pcm = wavfile.read(io.BytesIO(data))
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    if pcm.dtype == np.int16:
        samples = pcm.astype(np.float32) / 32768.0
    elif pcm.dtype == np.int32:
        samples = pcm.astype(np.float32) / 2147483648.0
    else:
        samples = pcm.astype(np.float32)
    return samples, int(sample_rate)


def extract_json(text: str, expect_object: bool = False) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    candidates = [text]
    pairs = (("{", "}"),) if expect_object else (("{", "}"), ("[", "]"))
    for open_ch, close_ch in pairs:
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if expect_object and not isinstance(value, dict):
            continue
        return value
    raise ValueError("model reply did not contain valid JSON" + (" object" if expect_object else ""))
