"""Local model backends, and the quirks that distinguish them.

The app talks OpenAI-compatible HTTP, which almost every local runtime now
speaks. What differs is not the wire format but the capabilities behind it,
and getting those wrong fails in confusing ways:

  * whether strict JSON-schema decoding is supported, and under which field
  * whether tool calling works
  * how the context window is set, and what it silently defaults to
  * whether the runtime pulls a model on demand or 404s

So each backend is described rather than assumed, and the app probes at
startup instead of trusting a config file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("agentfeed.providers")


@dataclass
class Provider:
    key: str
    label: str
    base_url: str
    #  How to get a decent context window. Both major runtimes default to
    #  something far too small and neither warns you.
    context_hint: str
    install: dict[str, str] = field(default_factory=dict)
    pull_hint: str = ""
    #  Filled in by probing rather than declared, because runtimes change.
    json_schema: bool | None = None
    tools: bool | None = None


PROVIDERS: dict[str, Provider] = {
    "ollama": Provider(
        key="ollama",
        label="Ollama",
        base_url="http://127.0.0.1:11434/v1",
        context_hint=(
            "Ollama defaults to a small context and ignores per-request "
            "overrides on the OpenAI endpoint. Set it for the server:\n"
            "    OLLAMA_CONTEXT_LENGTH=32768 ollama serve\n"
            "or bake it into a model:  ollama create <name> -f Modelfile"),
        install={
            "macos": "brew install ollama   # or https://ollama.com/download",
            "linux": "curl -fsSL https://ollama.com/install.sh | sh",
            "windows": "winget install Ollama.Ollama",
        },
        pull_hint="ollama pull qwen3:8b",
    ),
    "lmstudio": Provider(
        key="lmstudio",
        label="LM Studio",
        base_url="http://127.0.0.1:1234/v1",
        context_hint=(
            "LM Studio splits the loaded context across parallel slots, so "
            "`-c 8192 --parallel 4` gives each request only 2048 tokens:\n"
            "    lms load <model> -c 32768 --parallel 3"),
        install={
            "macos": "brew install --cask lm-studio",
            "linux": "https://lmstudio.ai/download",
            "windows": "winget install ElementLabs.LMStudio",
        },
        pull_hint="lms get qwen/qwen3.5-9b",
    ),
    "llamacpp": Provider(
        key="llamacpp",
        label="llama.cpp server",
        base_url="http://127.0.0.1:8080/v1",
        context_hint="Start with -c 32768.",
        install={"macos": "brew install llama.cpp",
                 "linux": "https://github.com/ggml-org/llama.cpp",
                 "windows": "https://github.com/ggml-org/llama.cpp/releases"},
        pull_hint="llama-server -hf <repo> -c 32768",
    ),
    #  NIM speaks the same OpenAI-compatible API as everything else here, so
    #  the app needed no code to support it -- only a name, so `doctor` can
    #  say which runtime it found instead of calling an H100 "vLLM".
    "nim": Provider(
        key="nim",
        label="NVIDIA NIM",
        base_url="http://127.0.0.1:8000/v1",
        context_hint=(
            "NIM sets the context from the model profile it selects. Pin one "
            "with NIM_MODEL_PROFILE, and give the container enough GPU "
            "memory that it does not fall back to a shorter profile."),
        install={
            "linux": ("docker run --gpus all -p 8000:8000 "
                      "nvcr.io/nim/qwen/qwen3-30b-a3b:latest"),
            "windows": "WSL2 + the same container",
        },
        pull_hint=("Any NIM container, or an NVIDIA-hosted endpoint:\n"
                   "    AGENTFEED_LLM_BASE_URL=https://integrate.api.nvidia.com/v1"),
    ),
    "vllm": Provider(
        key="vllm",
        label="vLLM",
        base_url="http://127.0.0.1:8000/v1",
        context_hint="Set --max-model-len when starting the server.",
        install={"linux": "pip install vllm"},
        pull_hint="vllm serve <model> --max-model-len 32768",
    ),
}

#  Probed in this order. Ollama first: it is the easiest thing to install on
#  all three platforms and needs no GUI.
#  NIM and vLLM share port 8000; whichever is actually running answers the
#  probe, and the model ids it returns say which one it was.
DETECT_ORDER = ("ollama", "lmstudio", "llamacpp", "nim", "vllm")


async def probe(base_url: str, timeout: float = 3.0) -> list[str]:
    """Model ids served at this endpoint, or [] if nothing answers."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{base_url.rstrip('/')}/models")
            if r.status_code != 200:
                return []
            return [m["id"] for m in r.json().get("data", [])]
    except Exception:  # noqa: BLE001 - a closed port is the normal case
        return []


async def detect(preferred: str = "") -> tuple[Provider | None, list[str]]:
    """Find a running backend. Returns (provider, model ids)."""
    order = ([preferred] if preferred in PROVIDERS else []) + [
        k for k in DETECT_ORDER if k != preferred]
    for key in order:
        p = PROVIDERS[key]
        models = await probe(p.base_url)
        if models:
            log.info("using %s at %s (%d models)", p.label, p.base_url, len(models))
            return p, models
    return None, []


async def capabilities(base_url: str, model: str) -> dict[str, Any]:
    """Find out what this backend actually supports, by asking it.

    Structured output is the one that matters: enrichment is built on
    schema-constrained decoding, and a runtime that quietly ignores the
    field returns prose where JSON was expected.
    """
    import json as _json

    out: dict[str, Any] = {"json_schema": False, "tools": False,
                           "reasoning": False, "notes": []}
    schema = {"type": "object",
              "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}

    async with httpx.AsyncClient(timeout=180.0) as c:
        async def ask(extra: dict[str, Any]) -> tuple[int, str, int]:
            body = {"model": model, "temperature": 0,
                    # Generous: a reasoning model spends most of a small
                    # budget thinking and returns empty content, which reads
                    # as "schema unsupported" when it means the opposite.
                    "max_tokens": 700,
                    "messages": [{"role": "user",
                                  "content": "Reply with {\"ok\": true}"}],
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": "Probe", "strict": True, "schema": schema}}}
            body.update(extra)
            r = await c.post(f"{base_url.rstrip('/')}/chat/completions", json=body)
            if r.status_code != 200:
                return r.status_code, r.text[:200], 0
            d = r.json()
            reasoned = (d.get("usage", {}).get("completion_tokens_details") or {}
                        ).get("reasoning_tokens", 0)
            return 200, d["choices"][0]["message"].get("content") or "", reasoned

        try:
            code, content, reasoned = await ask({})
            if code == 200 and not content.strip() and reasoned:
                out["reasoning"] = True
                out["notes"].append(
                    f"reasoning model ({reasoned} thinking tokens); retried "
                    f"with reasoning disabled")
                code, content, _ = await ask({"reasoning_effort": "none"})
            if code != 200:
                out["notes"].append(f"json_schema rejected: HTTP {code}")
            else:
                _json.loads(content)
                out["json_schema"] = True
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"json_schema unusable: {type(exc).__name__}")

        # Ask something the model cannot answer from memory, tell it to use
        # the tool, and leave room for the call. A terse weather question with
        # a small budget gets answered directly, which reads as "tools
        # unsupported" when the runtime supports them perfectly well.
        # Whether a model calls a tool is a sampled decision, so one refusal
        # proves nothing. Two attempts, the second insisting; declaring a
        # capable model incapable sends people to the wrong fix.
        tool_def = [{"type": "function", "function": {
            "name": "search_items",
            "description": "Search the news corpus. Call this for any "
                           "question about what has happened.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "search query"},
                "days": {"type": "integer"}},
                "required": ["text"]}}}]
        attempts = [
            [{"role": "user",
              "content": "What was published about NVIDIA this week?"}],
            [{"role": "system",
              "content": "You must call a tool before answering. Never "
                         "answer from memory."},
             {"role": "user",
              "content": "What was published about NVIDIA this week?"}],
        ]
        for i, msgs in enumerate(attempts):
            try:
                r = await c.post(f"{base_url.rstrip('/')}/chat/completions", json={
                    "model": model, "max_tokens": 400, "temperature": 0,
                    "messages": msgs, "tools": tool_def, "tool_choice": "auto"})
                if r.status_code != 200:
                    out["notes"].append(f"tools rejected: HTTP {r.status_code}")
                    break
                if r.json()["choices"][0]["message"].get("tool_calls"):
                    out["tools"] = True
                    break
            except Exception as exc:  # noqa: BLE001
                out["notes"].append(f"tools unusable: {type(exc).__name__}")
                break
        else:
            out["notes"].append(
                "the model answered instead of calling a tool, twice; a chat "
                "agent would be unreliable on it")
    return out


def install_hint(provider_key: str = "ollama") -> str:
    import platform
    p = PROVIDERS.get(provider_key)
    if not p:
        return ""
    system = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(
        platform.system(), "linux")
    return p.install.get(system, next(iter(p.install.values()), ""))
