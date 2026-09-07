"""Thin client for LM Studio's OpenAI-compatible server.

Deliberately not LangChain. Everything AgentFeed needs from an LLM is three
calls -- chat, chat-with-tools, embed -- and local models are fussy enough
that owning the retry/repair logic directly is worth more than a framework.
Swapping to Ollama or mlx_lm.server is a matter of changing llm_base_url.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Sequence

import httpx
from pydantic import BaseModel
from typing import TypeVar

T = TypeVar("T", bound=BaseModel)

from .config import settings
from .db import get_setting
from .providers import PROVIDERS, detect

log = logging.getLogger("agentfeed.llm")

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

# LM Studio's wording when a model will not fit in memory.
_LOAD_FAIL = re.compile(
    r"failed to load model|insufficient system resources|out of memory"
    r"|model loading was stopped", re.IGNORECASE)


def _is_load_failure(body: str) -> bool:
    return bool(_LOAD_FAIL.search(body or ""))


def _ran_out_of_room(exc: Exception) -> bool:
    """Was the JSON cut off mid-token rather than semantically wrong?"""
    msg = str(exc)
    return "EOF while parsing" in msg or "Invalid JSON" in msg


class LLMUnavailable(RuntimeError):
    """LM Studio is not reachable or has no model loaded."""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }


def strip_reasoning(text: str) -> str:
    """Qwen thinking models emit <think> blocks; the instruct ones don't."""
    return _THINK.sub("", text or "").strip()


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic JSON schema acceptable to strict grammar decoding.

    Inlines $defs, forbids extra keys, and marks every property required --
    strict mode rejects optional fields, so optionality is expressed as
    nullable types plus sensible defaults on the Python side.
    """
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return walk(json.loads(json.dumps(defs.get(name, {}))))
        node = {k: walk(v) for k, v in node.items()}
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        return node

    return walk(schema)


#  Which backend we settled on, for health reporting and error messages.
ACTIVE_PROVIDER: Any = None


class LLM:
    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or settings.llm_base_url
                         or PROVIDERS["ollama"].base_url).rstrip("/")
        self.model = model or settings.active_chat_model
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def client(self) -> httpx.AsyncClient:
        # A cached httpx client is bound to the loop that created it. The CLI
        # calls asyncio.run() more than once per process, so rebind rather
        # than handing back a client whose loop has closed.
        loop = asyncio.get_running_loop()
        if self._client is not None and self._loop is not loop:
            self._client = None
        if self._client is None:
            self._loop = loop
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=_headers(),
                timeout=httpx.Timeout(settings.llm_timeout, connect=10.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except RuntimeError:
                pass  # loop already gone; nothing to release
            self._client = None
            self._loop = None

    # --- introspection ---------------------------------------------------

    async def models(self) -> list[str]:
        try:
            c = await self.client()
            r = await c.get("/models")
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
            from .providers import install_hint
            raise LLMUnavailable(
                f"No local model server is running. Tried {self.base_url}.\n"
                f"The quickest fix is Ollama:\n"
                f"    {install_hint('ollama')}\n"
                f"    ollama serve\n"
                f"    ollama pull qwen3:8b\n"
                f"[{exc}]") from exc

    async def loaded_context(self) -> int | None:
        """The context window the runtime actually loaded the model with.

        Not exposed by the OpenAI-compatible surface, and it matters more
        than anything else here: a model whose card advertises 262k may be
        serving 8k, and prompts sized against the card get truncated or
        rejected. Each runtime hides it somewhere different, so ask each in
        its own dialect.
        """
        base = self.base_url.rsplit("/v1", 1)[0]
        c = await self.client()

        # LM Studio: native models endpoint carries the loaded length, and
        # parallel slots divide it further.
        try:
            r = await c.get(f"{base}/api/v0/models", timeout=6.0)
            if r.status_code == 200:
                for m in r.json().get("data", []):
                    if m.get("id") == self.model and m.get("state") == "loaded":
                        return (m.get("loaded_context_length")
                                or m.get("max_context_length"))
        except Exception:  # noqa: BLE001
            pass

        # Ollama: /api/ps reports what is resident, including the context it
        # was loaded with — which reflects OLLAMA_CONTEXT_LENGTH.
        try:
            r = await c.get(f"{base}/api/ps", timeout=6.0)
            if r.status_code == 200:
                for m in r.json().get("models", []):
                    if m.get("model", "").startswith(self.model.split(":")[0]):
                        ctx = m.get("context_length")
                        if ctx:
                            return int(ctx)
        except Exception:  # noqa: BLE001
            pass

        # Ollama fallback: /api/show gives the model's trained maximum. That
        # is an upper bound, not what is served, so cap it conservatively --
        # claiming 262k when the server allows 4k is the failure this whole
        # method exists to prevent.
        try:
            r = await c.post(f"{base}/api/show", json={"model": self.model},
                             timeout=8.0)
            if r.status_code == 200:
                info = r.json().get("model_info") or {}
                for k, v in info.items():
                    if k.endswith(".context_length") and isinstance(v, int):
                        return min(int(v), 8192)
        except Exception:  # noqa: BLE001
            pass
        return None

    async def loaded_models(self) -> list[str]:
        """Models LM Studio currently has in memory."""
        try:
            base = self.base_url.rsplit("/v1", 1)[0]
            c = await self.client()
            r = await c.get(f"{base}/api/v0/models", timeout=10.0)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])
                    if m.get("state") == "loaded"
                    and m.get("type") != "embeddings"]
        except Exception:  # noqa: BLE001
            return []

    async def health(self) -> dict[str, Any]:
        try:
            ids = await self.models()
        except LLMUnavailable as exc:
            return {"ok": False, "error": str(exc), "models": []}
        # Report what we will actually use, not what the profile prefers --
        # the resolver may have bound to a different loaded model.
        await resolve_models()
        want_chat = resolved_chat_model()
        want_embed = resolved_embed_model()
        provider = ACTIVE_PROVIDER
        return {
            "ok": True,
            "provider": provider.key if provider else "custom",
            "provider_label": provider.label if provider else self.base_url,
            "context_hint": provider.context_hint if provider else "",
            "models": ids,
            "chat_model": want_chat,
            "chat_loaded": any(want_chat in m for m in ids),
            "embed_model": want_embed,
            "embed_loaded": any(want_embed in m for m in ids),
            "base_url": self.base_url,
            "profile": settings.model_profile.label,
        }

    # --- completion ------------------------------------------------------

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1600,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        effort = (reasoning_effort if reasoning_effort is not None
                  else effort_for(payload["model"]))
        if effort:
            payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        c = await self.client()
        last: Exception | None = None
        fell_back: str | None = None
        for attempt in range(4):
            try:
                r = await c.post("/chat/completions", json=payload)
                if r.status_code == 400 and "reasoning" in r.text.lower() \
                        and "reasoning_effort" in payload:
                    # Model does not accept the parameter; drop it and retry
                    # rather than failing over something optional.
                    payload.pop("reasoning_effort", None)
                    continue
                if r.status_code == 400 and _is_load_failure(r.text):
                    # LM Studio refused to load the requested model, almost
                    # always because another large one is already resident and
                    # the pair will not fit. Falling back to something already
                    # in memory is far better than failing the user's question.
                    loaded = await self.loaded_models()
                    alt = next((m for m in loaded if m != payload["model"]), None)
                    if alt and fell_back is None:
                        log.warning("could not load %s; using loaded model %s",
                                    payload["model"], alt)
                        payload["model"] = alt
                        fell_back = alt
                        continue
                    raise LLMUnavailable(
                        f"LM Studio could not load '{payload['model']}'. It is "
                        f"probably too large to sit alongside the other model "
                        f"you selected — on a machine this size only one big "
                        f"model fits at a time. In Status → Local models, set "
                        f"'Assistant & briefs' to 'same as workhorse', or pick "
                        f"a model that is already loaded"
                        + (f" ({', '.join(loaded)})" if loaded else "") + ".")
                if 400 <= r.status_code < 500:
                    # Usually our fault (bad schema, oversized context) but
                    # LM Studio also 400s briefly while it swaps a model in.
                    # Give it one slow retry, then report the body verbatim --
                    # that text is the only place the real reason appears.
                    if attempt == 0:
                        await asyncio.sleep(4.0)
                        continue
                    raise LLMUnavailable(
                        f"LM Studio rejected the request ({r.status_code}): "
                        f"{r.text[:600]}")
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"server {r.status_code}: {r.text[:300]}",
                        request=r.request, response=r,
                    )
                r.raise_for_status()
                body = r.json()
                msg = body["choices"][0]["message"]

                # Reasoning models think before answering, and given a budget
                # sized for the answer alone they spend all of it thinking and
                # return empty content -- which reads as a broken model rather
                # than a misconfigured one.
                #
                # Each runtime signals this differently, and relying on any
                # one of them misses the others: LM Studio reports
                # reasoning_tokens in usage; Ollama puts the thinking in a
                # separate `reasoning` field and reports nothing in usage; and
                # when the budget simply ran out mid-thought, the only clue is
                # finish_reason. So treat any of the three as the same thing.
                usage = body.get("usage") or {}
                reasoned = (usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens", 0)
                had_reasoning_field = bool(
                    (msg.get("reasoning") or msg.get("reasoning_content") or "").strip())
                ran_out = body["choices"][0].get("finish_reason") == "length"
                empty = not (msg.get("content") or "").strip()

                if (empty and not msg.get("tool_calls")
                        and (reasoned or had_reasoning_field or ran_out)
                        and payload.get("reasoning_effort") != "none"):
                    why = ("reasoning_tokens" if reasoned
                           else "reasoning field" if had_reasoning_field
                           else "budget exhausted")
                    log.info("empty answer (%s); retrying with reasoning "
                             "disabled", why)
                    payload["reasoning_effort"] = "none"
                    continue

                if fell_back:
                    msg["_fell_back_to"] = fell_back
                return msg
            except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
                last = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        raise LLMUnavailable(f"chat failed after 3 attempts: {last}")

    async def text(self, messages: Sequence[dict[str, Any]], **kw: Any) -> str:
        msg = await self.chat(messages, **kw)
        return strip_reasoning(msg.get("content") or "")

    async def structured(
        self,
        messages: Sequence[dict[str, Any]],
        schema_model: type[T],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1600,
        model: str | None = None,
    ) -> T:
        """Constrained decoding against a Pydantic model.

        LM Studio compiles the schema to a grammar, so the token stream
        cannot leave the schema. The retry below is for semantic failures
        (a model that emits `null` for a required list), not syntax.
        """
        rf = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_model.__name__,
                "strict": True,
                "schema": strictify(schema_model.model_json_schema()),
            },
        }
        msgs = list(messages)
        last: Exception | None = None
        budget = max_tokens
        for attempt in range(3):
            raw = await self.text(
                msgs, temperature=temperature, max_tokens=budget,
                response_format=rf, model=model,
            )
            try:
                return schema_model.model_validate_json(raw)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if _ran_out_of_room(exc):
                    #  Not a semantic failure: the grammar was still emitting
                    #  valid JSON when the budget ended, so the reply is
                    #  unparseable through no fault of the model's. Asking it
                    #  to "correct" a truncation just truncates again --
                    #  what it needs is room. Seen on every schema in this
                    #  app at one time or another.
                    budget = int(budget * 2)
                    log.info("structured reply hit the token ceiling; "
                             "retrying with %d", budget)
                    msgs = list(messages)
                    continue
                msgs = list(messages) + [
                    {"role": "assistant", "content": raw[:2000]},
                    {"role": "user", "content":
                     f"That did not validate: {exc}. Return corrected JSON only."},
                ]
        raise ValueError(f"structured output failed: {last}")

    # --- embeddings ------------------------------------------------------

    async def embed(self, texts: Sequence[str], *, model: str | None = None
                    ) -> list[list[float]]:
        if not texts:
            return []
        c = await self.client()
        r = await c.post("/embeddings", json={
            "model": model or resolved_embed_model(),
            "input": list(texts),
        })
        if r.status_code == 404 or r.status_code >= 400:
            raise LLMUnavailable(
                f"Embedding model '{resolved_embed_model()}' not served "
                f"by LM Studio ({r.status_code}). Load it, or AgentFeed falls "
                f"back to keyword-only search. [{r.text[:200]}]"
            )
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]


_resolved: dict[str, str] = {}


def _pick(available: list[str], prefer: list[str], explicit: str | None,
          exclude_embeddings: bool) -> str | None:
    """First preference actually served, else any plausible model."""
    if explicit:
        return explicit
    lowered = [(m, m.lower()) for m in available]
    for want in prefer:
        w = want.lower()
        for m, ml in lowered:
            if ml == w or ml.endswith("/" + w) or w in ml:
                return m
    pool = [m for m, ml in lowered
            if ("embed" in ml) is not exclude_embeddings]
    return pool[0] if pool else None


async def ensure_backend(force: bool = False) -> Any:
    """Find a running model server, unless one was pinned.

    Auto-detection rather than configuration: the common failure is a
    perfectly good runtime on a port the app was not told about, which
    presents as "no model available" and sends people to the wrong docs.
    """
    global ACTIVE_PROVIDER
    if ACTIVE_PROVIDER is not None and not force:
        return ACTIVE_PROVIDER
    if not settings.llm_base_url:
        #  A server the person connected to from the welcome page. Env wins
        #  when set; otherwise what they chose last time is what they get.
        try:
            from .db import get_setting
            settings.llm_base_url = get_setting("llm_base_url", "") or ""
        except Exception:  # noqa: BLE001 - no database yet is fine
            pass
    if settings.llm_base_url:
        # Explicitly pinned: honour it, and label it if we recognise the port.
        llm = get_llm()
        llm.base_url = settings.llm_base_url.rstrip("/")
        ACTIVE_PROVIDER = next(
            (p for p in PROVIDERS.values()
             if p.base_url.rstrip("/") == llm.base_url), None)
        return ACTIVE_PROVIDER
    provider, _models = await detect(settings.llm_provider)
    if provider is not None:
        ACTIVE_PROVIDER = provider
        get_llm().base_url = provider.base_url.rstrip("/")
    return ACTIVE_PROVIDER


async def resolve_models(force: bool = False) -> dict[str, str]:
    """Bind models, preferring what the user chose in the GUI.

    Order of authority: the app_settings table (the model picker), then the
    AGENTFEED_* environment override, then the hardware profile's preference
    list, then whatever LM Studio happens to be serving.

    Two roles, because they have opposite requirements. `chat` is the
    workhorse -- it runs over every fetched item, so throughput dominates.
    `assistant` answers questions and writes the brief a handful of times a
    day, so a slower, stronger model costs almost nothing there.
    """
    if _resolved and not force:
        return _resolved
    await ensure_backend(force)
    llm = get_llm()
    available = await llm.models()
    prof = settings.model_profile

    chosen_chat = get_setting("chat_model") or settings.chat_model
    chosen_asst = get_setting("assistant_model") or settings.assistant_model
    chosen_embed = get_setting("embed_model") or settings.embed_model

    chat = _pick(available, prof.chat_prefer, chosen_chat, True)
    embed = _pick(available, prof.embed_prefer, chosen_embed, False)
    assistant = (_pick(available, prof.chat_prefer, chosen_asst, True)
                 if chosen_asst else chat)

    _resolved.clear()
    _resolved.update({
        "chat": chat or "",
        "assistant": assistant or chat or "",
        "embed": embed or "",
        "available": ", ".join(available),
    })
    if chat:
        llm.model = chat
    return _resolved


def effort_for(model: str) -> str:
    """Reasoning effort for a given model, as chosen in the GUI."""
    if model and model == _resolved.get("assistant") \
            and model != _resolved.get("chat"):
        return get_setting("assistant_reasoning_effort",
                           settings.assistant_reasoning_effort)
    return get_setting("reasoning_effort", settings.reasoning_effort)


def resolved_assistant_model() -> str:
    return _resolved.get("assistant") or resolved_chat_model()


async def plan_prompt_budget() -> dict[str, int]:
    """Decide how much article text to send per enrichment call.

    Derived from the context LM Studio actually loaded, divided by our own
    concurrency, because parallel slots split the KV cache between them.
    Falls back to the profile default when LM Studio will not say.
    """
    prof = settings.model_profile
    fallback = {"words": prof.enrich_word_budget, "ctx": 0, "per_request": 0,
                "concurrency": prof.enrich_concurrency,
                "source": "profile-default"}
    ctx = await get_llm().loaded_context()
    if not ctx:
        return fallback

    # LM Studio reverts to its default context whenever it reloads a model
    # (a TTL expiry, or juggling two models), which silently cut the budget
    # to 270 words more than once. Rather than truncate articles, drop our
    # own concurrency until each request gets a usable share -- slower, but
    # the model still sees the whole article.
    overhead = 1300 + 900          # system + vocabulary block, then output
    target_words = 1200            # below this, prefer fewer parallel calls
    concurrency = max(1, prof.enrich_concurrency)
    while concurrency > 1:
        per = int(ctx / concurrency)
        if int((per - overhead) * 0.85 * 0.6) >= target_words:
            break
        concurrency -= 1

    per_request = int(ctx / concurrency)
    usable = int((per_request - overhead) * 0.85)
    if usable < 250:
        # Too tight to be useful -- keep a floor and let the caller warn.
        return {"words": 250, "ctx": ctx, "per_request": per_request,
                "concurrency": concurrency, "source": "context-limited"}
    # ~0.6 words per token is conservative for mixed-language scientific text.
    words = max(250, min(prof.enrich_word_budget * 3, int(usable * 0.6)))
    return {"words": words, "ctx": ctx, "per_request": per_request,
            "concurrency": concurrency, "source": "runtime-context"}


def resolved_chat_model() -> str:
    # Before the first resolve, fall back through the same order of authority
    # the resolver uses -- the saved choice outranks the profile default.
    return (_resolved.get("chat") or get_setting("chat_model")
            or settings.active_chat_model)


def resolved_embed_model() -> str:
    return (_resolved.get("embed") or get_setting("embed_model")
            or settings.active_embed_model)


_shared: LLM | None = None


def get_llm() -> LLM:
    global _shared
    if _shared is None:
        _shared = LLM()
    return _shared
