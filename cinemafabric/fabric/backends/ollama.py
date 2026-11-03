"""Ollama backend: a Meta open-weight model served by Ollama on one machine/GPU."""

import re

import httpx

from ...profile import ollama_licence
from ...util import extract_json
from ..types import TaskKind
from .base import Backend, BackendError, ExecResult, Offer


class OllamaBackend(Backend):
    def __init__(
        self,
        id: str,
        label: str,
        location: str,
        device: str,
        base_url: str,
        model: str,
        quality: int,
        vision: bool,
        typical_s: float,
        role: str = "text",
        secret: str | None = None,
    ):
        self.id = id
        self.label = label
        self.location = location
        self.device = device
        self.model = model
        self.role = role
        self.max_concurrency = 1
        kind = TaskKind.MODERATE if role == "guard" else TaskKind.TEXT
        self.offers = [Offer(kind, model, 2 if role == "guard" else quality, vision=vision, typical_s=typical_s,
                             licence=ollama_licence(model))]
        self.capabilities: list[str] | None = None
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(900.0, connect=5.0),
                                         headers={"X-Worker-Secret": secret} if secret else None)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def probe(self) -> tuple[bool, str]:
        try:
            r = await self._client.get("/api/tags", timeout=4.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return False, f"Ollama unreachable ({e.__class__.__name__})"
        names = {m.get("name", "") for m in r.json().get("models", [])}
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in names:
            return False, f"model {self.model} not pulled"
        await self._learn_capabilities()
        try:
            ps = await self._client.get("/api/ps", timeout=4.0)
            loaded = any(m.get("name") == wanted for m in ps.json().get("models", []))
        except (httpx.HTTPError, ValueError):
            loaded = False
        return True, "model loaded in memory" if loaded else "ready (model will load on first call)"

    async def _learn_capabilities(self) -> None:
        if self.capabilities is not None:
            return
        try:
            show = await self._client.post("/api/show", json={"model": self.model}, timeout=10.0)
            self.capabilities = show.json().get("capabilities") or []
        except (httpx.HTTPError, ValueError):
            pass

    async def _moderate(self, payload: dict) -> ExecResult:
        """Llama Guard: classify a user prompt, or (with context) a model's reply to it."""
        if payload.get("context"):
            messages = [{"role": "user", "content": payload["context"]}, {"role": "assistant", "content": payload["text"]}]
        else:
            messages = [{"role": "user", "content": payload["text"]}]
        body = {"model": self.model, "messages": messages, "stream": False, "options": {"temperature": 0, "num_predict": 20}}
        try:
            r = await self._client.post("/api/chat", json=body)
        except httpx.HTTPError as e:
            raise BackendError(f"Ollama transport error: {e.__class__.__name__}") from e
        if r.status_code != 200:
            raise BackendError(f"Ollama HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        verdict = r.json().get("message", {}).get("content", "").strip()
        if not verdict.lower().startswith(("safe", "unsafe")):
            raise BackendError(f"unexpected Llama Guard reply: {verdict[:80]}", retryable=False)
        categories = re.findall(r"S\d+", verdict) if verdict.lower().startswith("unsafe") else []
        return ExecResult({"safe": not categories and verdict.lower().startswith("safe"), "categories": categories, "raw": verdict})

    async def execute(self, offer: Offer, payload: dict) -> ExecResult:
        if offer.kind == TaskKind.MODERATE:
            return await self._moderate(payload)
        messages = []
        if payload.get("system"):
            messages.append({"role": "system", "content": payload["system"]})
        user = {"role": "user", "content": payload["prompt"]}
        if payload.get("images"):
            user["images"] = payload["images"]
        messages.append(user)
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": float(payload.get("temperature", 0.8)),
                "num_predict": int(payload.get("max_tokens", 2048)),
                "num_ctx": 16384,
            },
        }
        if payload.get("json_schema"):
            body["format"] = payload["json_schema"]["schema"]
            await self._learn_capabilities()
            if self.capabilities and "thinking" in self.capabilities:
                body["think"] = False
        try:
            r = await self._client.post("/api/chat", json=body)
        except httpx.HTTPError as e:
            raise BackendError(f"Ollama transport error: {e.__class__.__name__}") from e
        if r.status_code != 200:
            raise BackendError(f"Ollama HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        data = r.json()
        text = data.get("message", {}).get("content", "")
        output = {"text": text}
        if payload.get("json_schema"):
            try:
                output["json"] = extract_json(text, expect_object=payload["json_schema"]["schema"].get("type") == "object")
            except ValueError as e:
                raise BackendError(str(e), retryable=False) from e
        usage = {"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)}
        return ExecResult(output, 0.0, usage)
