import pytest

from cinemafabric.fabric import machines
from cinemafabric.fabric.backends.gpuworker import GpuWorkerBackend
from cinemafabric.fabric.backends.ollama import OllamaBackend
from cinemafabric.fabric.registry import Registry
from cinemafabric.fabric.types import TaskKind


@pytest.mark.parametrize(
    ("size", "name", "quality"),
    [("30.5B", "", 2), ("8.0B", "", 1), ("", "muse-glimmer:30b-mlx", 2), ("", "llama3.1:8b", 1), ("1.5B", "", 1), ("", "x", 1)],
)
def test_guess_quality(size, name, quality):
    assert machines.guess_quality(size, name) == quality


def test_meta_and_guard_detection():
    assert machines.is_meta("muse-glimmer:30b-mlx") and machines.is_meta("llama3.1:8b")
    assert not machines.is_meta("qwen2.5:7b", "qwen2")
    assert machines.is_guard("llama-guard3:1b")


def test_build_guard_offers_moderation():
    spec = machines.MachineSpec(id="g", label="Guard", kind="ollama", base_url="http://h:1", model="llama-guard3:1b", quality=1)
    backend = machines.build(spec)
    assert isinstance(backend, OllamaBackend) and backend.offers[0].kind == TaskKind.MODERATE
    assert backend.source == "added"


def test_build_gpu_worker_needs_known_caps():
    ok = machines.build(machines.MachineSpec(id="w", label="W", kind="gpu-worker", base_url="http://h:1", caps=["tts", "nope"]))
    assert isinstance(ok, GpuWorkerBackend) and [o.kind for o in ok.offers] == [TaskKind.TTS]
    with pytest.raises(ValueError):
        machines.build(machines.MachineSpec(id="w", label="W", kind="gpu-worker", base_url="http://h:1", caps=["nope"]))


def test_validate_url():
    assert machines.validate_url(" http://192.168.0.5:11434/ ") == "http://192.168.0.5:11434"
    with pytest.raises(ValueError):
        machines.validate_url("file:///etc/passwd")


def test_store_round_trip(tmp_path):
    store = machines.MachineStore(tmp_path / "f.db")
    spec = machines.MachineSpec(id="mac", label="Mac", kind="ollama", base_url="http://h:1", model="m", quality=2, vision=True)
    store.save(spec)
    assert store.all() == [spec]
    assert store.delete("mac") and not store.delete("mac")


async def test_registry_add_keeps_builtins_last_and_remove():
    cloud = machines.build(machines.MachineSpec(id="a", label="A", kind="gpu-worker", base_url="http://127.0.0.1:9", caps=["tts"]))
    cloud.source = "builtin"
    registry = Registry([cloud])
    added = machines.build(machines.MachineSpec(id="b", label="B", kind="gpu-worker", base_url="http://127.0.0.1:9", caps=["music"]))
    await registry.add(added)
    assert [b.id for b in registry.backends] == ["b", "a"]
    await registry.remove("b")
    assert "b" not in registry.state and [b.id for b in registry.backends] == ["a"]


def test_sam31_reply_decodes_into_a_full_frame_mask():
    """A real SAM 3.1 reply (two people, box-local one-bit masks) decoded with Meta's meta-sam-parser."""
    import json
    from pathlib import Path

    import numpy as np

    from cinemafabric.fabric.backends.muse import sam_text_to_mask

    fixtures = Path(__file__).parent / "fixtures"
    text = (fixtures / "sam31_two_people.txt").read_text(encoding="utf-8")
    expected = json.loads((fixtures / "sam31_two_people.json").read_text())
    size = tuple(expected["size"])
    mask, box, count = sam_text_to_mask(text, size)
    assert count == expected["objects"] == 2
    assert box == expected["box"]
    assert mask.size == size
    assert np.asarray(mask).mean() / 255 == pytest.approx(expected["coverage"], abs=0.002)
    arr = np.asarray(mask)
    assert arr[:, : box[0]].max() == 0 and arr[:, box[2] :].max() == 0
    assert sam_text_to_mask(text, size, min_confidence=0.999)[2] == 0
    assert sam_text_to_mask("", size) == (None, None, 0)
