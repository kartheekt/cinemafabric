"""Meta Model API list prices (USD, https://dev.meta.ai/docs/pricing-rate-limits) and cost estimators."""

import base64
import struct

TEXT_PRICES: dict[str, tuple[float, float, float]] = {
    "muse-wonder-1.0": (4.00, 0.20, 20.00),
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.2": (1.25, 0.15, 4.25),
    "muse-spark-1.1": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.002, 0.20),
    "muse-spark-1.2-contributor": (0.10, 0.002, 0.20),
}
DEFAULT_TEXT_PRICE = (1.25, 0.15, 4.25)

IMAGE_PRICE = 0.01
SAM_IMAGE_PRICE = 2.50 / 1000
TRANSCRIBE_PER_HOUR = 0.18
IMAGE_INPUT_TOKENS = 1100
REASONING_HEADROOM = {"minimal": 1000, "low": 2500, "medium": 6000, "high": 12000}
DEFAULT_HEADROOM = 4000


def text_cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """input_tokens includes cached_tokens, as the API reports them; cached ones are billed at the cached rate."""
    pin, pcached, pout = TEXT_PRICES.get(model, DEFAULT_TEXT_PRICE)
    cached = min(cached_tokens, input_tokens)
    return (input_tokens - cached) / 1e6 * pin + cached / 1e6 * pcached + output_tokens / 1e6 * pout


def output_cap(payload: dict) -> int:
    """The max_tokens actually sent: the answer budget plus room for reasoning."""
    effort = payload.get("reasoning_effort", "low")
    return int(payload.get("max_tokens", 2048)) + REASONING_HEADROOM.get(effort, DEFAULT_HEADROOM)


def estimate_text_tokens(payload: dict) -> tuple[int, int]:
    """Worst case, so a reservation always covers what the call can cost."""
    chars = len(payload.get("system", "")) + len(payload.get("prompt", ""))
    schema = payload.get("json_schema")
    if schema:
        chars += len(str(schema))
    input_tokens = chars // 3 + IMAGE_INPUT_TOKENS * len(payload.get("images", []))
    return input_tokens, output_cap(payload)


def wav_seconds(audio_b64: str) -> float:
    """Length of a base64 WAV from its header and size, without decoding the samples."""
    size = len(audio_b64) * 3 // 4
    try:
        head = base64.b64decode(audio_b64[:64])
        channels, rate = struct.unpack_from("<HI", head, 22)
        bits = struct.unpack_from("<H", head, 34)[0]
        frame_rate = rate * channels * bits / 8 if head[:4] == b"RIFF" else 0
    except (ValueError, struct.error):
        frame_rate = 0
    return max(size - 44, 0) / (frame_rate or 8000)


def estimate_text_cost(model: str, payload: dict) -> float:
    return text_cost(model, *estimate_text_tokens(payload))
