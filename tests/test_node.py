from cinemafabric import profile as prof
from cinemafabric.fabric.backends.base import Offer
from cinemafabric.fabric.cache import ResultCache
from cinemafabric.fabric.ledger import Ledger
from cinemafabric.fabric.registry import Registry
from cinemafabric.fabric.router import Router
from cinemafabric.fabric.types import TaskKind, TaskRequest
from cinemafabric.node.discovery import Device, advertise_host, plan

from .test_fabric import FakeBackend

ALL = {"meta-model-api", "apache-2.0", "mit", "llama", "sam", "cc-by-nc-4.0", "builtin"}


def test_merge_overrides_nested_keys():
    merged = prof._merge({"a": {"x": 1, "y": 2}, "b": 1}, {"a": {"y": 3}, "c": 4})
    assert merged == {"a": {"x": 1, "y": 3}, "b": 1, "c": 4}


def test_ollama_licence_and_family():
    assert prof.ollama_licence("llama3.1:8b") == "llama"
    assert prof.ollama_licence("muse-glimmer:30b") == "apache-2.0"
    assert prof.ollama_licence("something-else:7b") == "unknown"


def test_plan_spreads_capabilities_by_free_memory():
    big = Device("cuda:0", "cuda", "GPU A", 24, 20)
    small = Device("cuda:1", "cuda", "GPU B", 8, 4)
    tiny = Device("cuda:2", "cuda", "GPU C", 4, 1)
    planned = plan([big, small, tiny], ALL, reserve_gb=1.0)
    assert set(big.caps) == {"tts", "music", "segment", "watermark", "depth"}
    assert set(small.caps) == {"tts", "depth", "watermark"}
    assert tiny not in planned


def test_plan_respects_licences_and_device_kinds():
    gpu = Device("cuda:0", "cuda", "GPU", 24, 20)
    plan([gpu], ALL - {"cc-by-nc-4.0"}, reserve_gb=1.0)
    assert set(gpu.caps) == {"segment", "watermark", "depth"}
    cpu = Device("cpu", "cpu", "CPU", 0, 0)
    plan([cpu], ALL, reserve_gb=1.0)
    assert "music" not in cpu.caps and "tts" in cpu.caps


def test_plan_can_be_limited_to_some_capabilities():
    gpu = Device("cuda:0", "cuda", "GPU", 24, 20)
    plan([gpu], ALL, reserve_gb=1.0, only=["tts"])
    assert gpu.caps == ["tts"]


def test_advertise_host_for_local_fabric():
    assert advertise_host("http://127.0.0.1:8100") == "127.0.0.1"


async def test_router_skips_disallowed_licences(tmp_path):
    nc = FakeBackend("nc", [Offer(TaskKind.MUSIC, "nc-music", 2, licence="cc-by-nc-4.0")])
    ok = FakeBackend("ok", [Offer(TaskKind.MUSIC, "free-music", 1, licence="builtin")])
    registry = Registry([nc, ok])
    await registry.probe_all()
    router = Router(registry, Ledger(tmp_path / "f.db", None, None, False), ResultCache(tmp_path / "c"), ALL - {"cc-by-nc-4.0"})
    result = await router.run(TaskRequest(kind=TaskKind.MUSIC, payload={"prompt": "x"}, cache=False))
    assert result.decision.worker_id == "ok"
    assert "licence cc-by-nc-4.0 not allowed" in result.decision.reason


def test_ollama_memory_is_kept_free_on_the_largest_gpu():
    from cinemafabric.node.discovery import ollama_reserve

    a, b = Device("cuda:0", "cuda", "A", 12, 11), Device("cuda:1", "cuda", "B", 8, 7)
    held = ollama_reserve([a, b], [4.9, 1.6])
    assert held == {"cuda:0": 1.8}
    plan([a, b], ALL, reserve_gb=1.5, extra_reserve=held)
    assert "segment" in b.caps and "music" in a.caps
    assert ollama_reserve([Device("cpu", "cpu", "CPU", 0, 0)], [4.9]) == {}


def test_heavy_work_goes_to_the_strongest_gpu_with_ollama_room_left():
    from cinemafabric.node.discovery import ollama_reserve

    strong = Device("cuda:0", "cuda", "RTX 5070", 12.0, 11.6)
    weak = Device("cuda:1", "cuda", "RTX 3070 Ti", 8.0, 7.0)
    held = ollama_reserve([strong, weak], [4.7, 1.6])
    assert held == {"cuda:0": 1.8}
    plan([strong, weak], ALL, reserve_gb=1.5, extra_reserve=held)
    assert {"depth", "music"} <= set(strong.caps) and "segment" in weak.caps
    assert "tts" in strong.caps and "tts" in weak.caps
    assert {"depth", "music"} <= set(weak.caps), "the other GPU is on call for heavy work"


def test_depth_input_keeps_small_frames_and_shrinks_large_ones_to_whole_patches():
    from PIL import Image

    from cinemafabric.worker.app import depth_input

    small = Image.new("RGB", (640, 360))
    assert depth_input(small) is small
    resized = depth_input(Image.new("RGB", (2016, 1152)))
    assert max(resized.size) <= 1344 and resized.width % 14 == 0 and resized.height % 14 == 0


def test_a_backup_model_is_kept_on_call_when_a_gpu_is_short_of_room():
    strong = Device("cuda:0", "cuda", "RTX 5070", 12.0, 11.6)
    weak = Device("cuda:1", "cuda", "RTX 3070 Ti", 8.0, 5.0)
    plan([strong, weak], ALL, reserve_gb=1.5, extra_reserve={"cuda:0": 1.8})
    assert "segment" in strong.caps or "segment" in weak.caps


def test_every_job_survives_either_gpu_going_offline():
    strong = Device("cuda:0", "cuda", "RTX 5070", 12.0, 11.6)
    weak = Device("cuda:1", "cuda", "RTX 3070 Ti", 8.0, 7.0)
    plan([strong, weak], ALL, reserve_gb=1.5, extra_reserve={"cuda:0": 1.8})
    assert set(strong.caps) == set(weak.caps) == {"depth", "music", "segment", "tts", "watermark"}


def test_a_node_leaves_out_models_it_is_told_to_ignore(monkeypatch):
    from cinemafabric.fabric import machines
    from cinemafabric.node.__main__ import ollama_specs

    async def inspect(kind, url):
        names = ("llama3:8b", "llama3.1:8b")
        return {"models": [{"name": n, "family": "llama", "quality": 1, "vision": False} for n in names]}

    monkeypatch.setattr(machines, "inspect", inspect)
    url = "http://127.0.0.1:11434"
    assert [s["model"] for s in ollama_specs(url, ["llama"], "127.0.0.1", True)] == ["llama3:8b", "llama3.1:8b"]
    assert [s["model"] for s in ollama_specs(url, ["llama"], "127.0.0.1", True, ("llama3:8b",))] == ["llama3.1:8b"]
