"""OpenAI-compatible AI client (9Router, LiteLLM, Ollama, vLLM, ...).

ClipForge never talks to a specific vendor: everything goes through the
`openai` Python SDK pointed at the user's own `base_url`.

Two capabilities live here:

* `list_models()` — the Settings screen's "test connection" (`GET /v1/models`).
* `analyze_transcript()` — highlight detection: timestamped transcript in,
  scored `ViralClip` list out, with structured-output → `json_object` → lenient
  parsing fallbacks (PRD F-19) and model fallback on failure (F-19 AC3).

Error surfacing contract
------------------------
No exception may escape as a bare "unknown error". Every provider failure is
captured with its exception type, HTTP status and a redacted snippet of the
raw response, logged in full, and re-raised as `AIProviderError` carrying a
message that names the cause. `AIProviderError.detail` keeps the structured
bits (`status`, `error_type`, `raw`) for the UI/SSE layer.
"""

from __future__ import annotations

import json
import math
import random
import re
import threading
import time
from typing import Any, Callable

import httpx
from openai import OpenAI

from backend import config
from backend.schemas.analyze import ViralClip

ProgressFn = Callable[[float, str], None]

logger = config.logger

# Retryable HTTP statuses: rate limits, quota-ish, and server-side failures.
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}

# Never let a provider response echo the API key back into a log or the UI.
_SECRET_PATTERNS = (
    # (prefix, secret) — the prefix is kept so the message stays readable,
    # the secret is always masked, never echoed back.
    re.compile(r"(sk-)([A-Za-z0-9_\-]{4,})"),
    re.compile(r"((?:Bearer|bearer)\s+)([A-Za-z0-9_\-\.]{8,})"),
    re.compile(r"((?:api[_-]?key|x-api-key|api-key)[\"'\s:=]+)([A-Za-z0-9_\-\.]{8,})", re.I),
)

_RAW_SNIPPET_CHARS = 500


def _redact(text: str) -> str:
    """Mask anything that looks like a credential in provider output.

    Group 1 of every pattern is the *prefix* (e.g. ``sk-`` or ``Bearer ``); the
    secret itself is group 2 and is always replaced, never echoed back.
    """
    cleaned = text or ""
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(lambda m: f"{m.group(1)}{_mask(m.group(2))}", cleaned)
    return cleaned


def _mask(secret: str) -> str:
    """`sk-abc…xyz` — enough to correlate log lines, useless as a credential."""
    if not secret:
        return "***"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-2:]}***"


def _snippet(text: str, limit: int = _RAW_SNIPPET_CHARS) -> str:
    """A short, single-line, redacted excerpt of a raw provider response."""
    flat = " ".join((text or "").split())
    return _redact(flat[:limit])


def _describe_exception(exc: BaseException) -> dict[str, Any]:
    """Pull type / status / body out of any provider-raised exception.

    The OpenAI SDK raises distinct classes (APIStatusError, APIConnectionError,
    APITimeoutError, ...) and proxies add their own shapes. Everything is
    normalised here so no caller ever has to string-match blindly.
    """
    name = type(exc).__name__
    message = str(exc).strip() or name
    status: int | None = None
    body = ""

    response = getattr(exc, "response", None)
    request = getattr(exc, "request", None)
    if response is not None:
        status = getattr(response, "status_code", None) or status
        try:
            body = response.text or ""
        except Exception:  # noqa: BLE001 - body may not be buffered
            body = ""
    if status is None:
        status = getattr(exc, "status_code", None)

    # Prefer the server's own `error.message` when present.
    error_type = ""
    server_message = ""
    if body:
        try:
            parsed = json.loads(body)
            error = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
            if isinstance(error, dict):
                server_message = str(error.get("message") or "")
                error_type = str(error.get("type") or error.get("code") or "")
        except (json.JSONDecodeError, AttributeError):
            server_message = ""

    return {
        "type": name,
        "status": int(status) if isinstance(status, int) else None,
        "error_type": error_type,
        "message": _redact(server_message or message),
        "raw": _snippet(body or message),
        "url": str(getattr(request, "url", "") or "") if request is not None else "",
    }


class AIProviderError(RuntimeError):
    """Raised when the configured provider cannot be reached or rejects us."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}


def _provider_error(exc: BaseException, *, context: str = "") -> AIProviderError:
    """Build a fully-described `AIProviderError`, log it, and return it."""
    info = _describe_exception(exc)
    status = info["status"]
    prefix = f"{context}: " if context else ""

    # The HTTP status is the source of truth. A proxy can mislabel a 500 body as
    # `invalid_api_key` (seen with 9Router/OmniRoute), and reporting "API key
    # ditolak" for a server-side crash sends people chasing the wrong problem.
    auth_error = status == 401 or (status is None and info["error_type"] in {"invalid_api_key", "authentication_error"})

    if auth_error:
        message = (
            f"{prefix}API key ditolak provider (HTTP {status or 401} — {info['message'] or 'unauthorized'}). "
            "Periksa AI_API_KEY di .env atau di Settings."
        )
        code = 401
    elif status in _RETRYABLE_STATUS and status >= 500:
        message = (
            f"{prefix}Provider error di sisi server (HTTP {status} — {info['message'] or info['error_type']}). "
            "Ini bukan masalah konfigurasi lokal; percobaan ulang otomatis sudah dilakukan."
        )
        code = status
    elif status == 403:
        message = (
            f"{prefix}Akses ditolak provider (HTTP 403 — {info['message'] or 'forbidden'}). "
            "Kemungkinan kuota habis atau model tidak tersedia untuk akun ini."
        )
        code = 403
    elif status == 402:
        message = f"{prefix}Kuota/saldo provider habis (HTTP 402 — {info['message']})."
        code = 402
    elif status == 404:
        message = (
            f"{prefix}Endpoint tidak ditemukan (HTTP 404 — {info['message']}). "
            "Pastikan base URL berakhiran /v1."
        )
        code = 404
    elif status == 429:
        message = f"{prefix}Rate limit provider (HTTP 429 — {info['message']}). Coba lagi sebentar."
        code = 429
    elif status == 400:
        message = (
            f"{prefix}Permintaan ditolak provider (HTTP 400 — {info['message']}). "
            "Nama model mungkin tidak dikenal atau parameter tidak didukung."
        )
        code = 400
    elif "Timeout" in info["type"] or "timeout" in info["message"].lower():
        message = (
            f"{prefix}Provider tidak menjawab dalam {config.AI_TIMEOUT_SECONDS:.0f} detik "
            f"({info['type']}). Naikkan AI_TIMEOUT_SECONDS atau ganti model yang lebih cepat."
        )
        code = 504
    elif "Connect" in info["type"]:
        message = (
            f"{prefix}Tidak bisa terhubung ke provider di {config.AI_BASE_URL} "
            f"({info['message']}). Pastikan server provider berjalan dan base URL benar."
        )
        code = 502
    else:
        message = f"{prefix}{info['type']}: {info['message']}"
        code = status or 502

    logger.error(
        "AI provider call failed — context=%s type=%s status=%s error_type=%s raw=%s",
        context or "-",
        info["type"],
        info["status"],
        info["error_type"],
        info["raw"],
    )
    return AIProviderError(message, status_code=code, detail=info)


def _is_retryable(exc: BaseException) -> bool:
    """429 / 5xx / connection-reset / timeout are worth another attempt."""
    info = _describe_exception(exc)
    if info["status"] in _RETRYABLE_STATUS:
        return True
    return info["type"] in {
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "RateLimitError",
        "RemoteProtocolError",
        "ReadTimeout",
        "ConnectTimeout",
        "ReadError",
    }


def get_client(base_url: str | None = None, api_key: str | None = None) -> OpenAI:
    resolved_base = (base_url or config.AI_BASE_URL).rstrip("/")
    if not resolved_base:
        raise AIProviderError(
            "No AI base URL configured. Set AI_BASE_URL in .env or in Settings.",
            status_code=400,
        )
    # The SDK owns retries so it can retry per-attempt; we do our own backoff on
    # top for statuses the SDK does not consider retryable at our call sites.
    timeout = httpx.Timeout(
        config.AI_TIMEOUT_SECONDS,
        connect=config.AI_CONNECT_TIMEOUT_SECONDS,
    )
    return OpenAI(
        base_url=resolved_base,
        api_key=api_key or config.AI_API_KEY or "not-needed",
        timeout=timeout,
        max_retries=0,
    )


def list_models(
    base_url: str | None = None, api_key: str | None = None
) -> list[dict[str, Any]]:
    """Return the models exposed by the provider (`GET /v1/models`).

    Some OpenAI-compatible proxies (9Router/OmniRoute) gate `/v1/models` behind
    a management token while still answering `/v1/combos`. When the first call
    fails we try the combos endpoint before giving up, so "test connection"
    works on those providers too.
    """
    client = get_client(base_url, api_key)
    try:
        response = client.models.list()
        models = [
            {
                "id": model.id,
                "owned_by": getattr(model, "owned_by", "") or "",
                "created": getattr(model, "created", None),
            }
            for model in response.data
        ]
        if models:
            return sorted(models, key=lambda item: item["id"].lower())
        first_error: AIProviderError | None = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a friendly error
        first_error = _provider_error(exc, context="GET /models")
        models = []

    combos = _list_combos(base_url, api_key)
    if combos:
        return combos
    raise first_error or AIProviderError(
        "The provider returned an empty model list.", status_code=502
    )


def _list_combos(base_url: str | None, api_key: str | None) -> list[dict[str, Any]]:
    """Best-effort read of a proxy's `/v1/combos` catalogue (9Router/OmniRoute)."""
    if not config.AI_DISCOVER_COMBOS:
        return []
    resolved_base = (base_url or config.AI_BASE_URL).rstrip("/")
    if not resolved_base:
        return []
    url = f"{resolved_base}/combos"
    headers = {}
    key = api_key or config.AI_API_KEY
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = httpx.get(url, headers=headers, timeout=config.AI_CONNECT_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return []
        payload = response.json()
    except Exception:  # noqa: BLE001 - optional discovery only
        return []

    names: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key_name in ("name", "model"):
                candidate = value.get(key_name)
                if isinstance(candidate, str) and candidate.strip():
                    names.append(candidate.strip())
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    unique = sorted({name for name in names if name})
    logger.info("Discovered %d provider combos via %s", len(unique), url)
    return [{"id": name, "owned_by": "combo", "created": None} for name in unique]


# --- Highlight detection ------------------------------------------------------

_CLIP_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "clips"],
    "properties": {
        "summary": {"type": "string"},
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "start_time",
                    "end_time",
                    "hook_time",
                    "virality_score",
                    "key_quotes",
                    "transcript",
                    "title_suggestion",
                    "caption_suggestion",
                    "hashtag_suggestion",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                    "hook_time": {"type": "number"},
                    "virality_score": {"type": "integer", "minimum": 1, "maximum": 100},
                    "key_quotes": {"type": "array", "items": {"type": "string"}},
                    "transcript": {"type": "string"},
                    "title_suggestion": {"type": "string"},
                    "caption_suggestion": {"type": "string"},
                    "hashtag_suggestion": {"type": "string"},
                },
            },
        },
    },
}

_SYSTEM_PROMPT = """You are ClipForge's highlight analyst. You receive a timestamped transcript of a long video \
and must select the moments with the highest short-form (TikTok / Reels / Shorts) potential.

Rules:
- Prefer self-contained moments: a strong hook in the first 3 seconds, tension or curiosity, a punchline, \
a surprising claim, a concrete tip, or an emotional peak. Skip intros, sponsor reads, housekeeping and outros.
- start_time and end_time are absolute seconds (numbers) inside the analysed window. \
end_time - start_time should be close to the requested clip length.
- hook_time is the absolute second inside the clip where the hook lands (usually 0-4s after start_time).
- virality_score is an integer 1-100. Be discriminative: 90+ only for genuinely strong moments.
- title: written in the ORIGINAL language of the transcript (zero translation), max 70 characters, \
no first-person pronouns ("I", "saya", "aku"), no clickbait emoji.
- key_quotes: 1-3 short verbatim quotes from the clip.
- transcript: the verbatim excerpt covered by the clip.
- title_suggestion, caption_suggestion, hashtag_suggestion: platform-ready copy in the ORIGINAL language. \
hashtags are space separated and start with '#'.
- Return clips sorted by virality_score descending, without overlapping each other.
- summary: 1-2 sentences in the ORIGINAL language describing what the video is about and why these \
moments were chosen. Never leave it empty.
- Never switch language: summary, titles, hooks, captions and quotes must be written in the language \
named under "Output language" in the user message. Do not translate the quotes away from the transcript.
- Answer with JSON only, matching the provided schema. No markdown, no commentary."""


def analyze_transcript(
    segments: list[dict[str, Any]],
    *,
    duration: float,
    target_seconds: int,
    clip_count: int,
    custom_prompt: str | None = None,
    language: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Ask the model for the most viral moments and return validated clips."""
    if not segments:
        raise AIProviderError(
            "There is no transcript to analyse — the video may contain no speech.",
            status_code=422,
        )

    client = get_client(base_url, api_key)
    models = _model_candidates(model, base_url=base_url, api_key=api_key)
    chunks = _chunk_segments(segments, config.ANALYZE_CHUNK_CHARS)
    per_chunk = max(2, math.ceil(clip_count / max(1, len(chunks))))
    logger.info(
        "Scoring %d transcript segments in %d chunk(s), models=%s", len(segments), len(chunks), models
    )

    collected: list[ViralClip] = []
    summaries: list[str] = []
    used_model = models[0]
    failures: list[str] = []
    last_status: int | None = None

    for index, chunk in enumerate(chunks):
        if cancel is not None and cancel.is_set():
            raise AIProviderError("Analysis cancelled", status_code=499)
        if on_progress:
            base = index / len(chunks) * 100.0
            label = (
                f"Scoring highlights with AI ({index + 1}/{len(chunks)})"
                if len(chunks) > 1
                else "Scoring highlights with AI"
            )
            on_progress(base, label)

        prompt = _build_user_prompt(
            chunk,
            duration=duration,
            target_seconds=target_seconds,
            clip_count=per_chunk,
            custom_prompt=custom_prompt,
            language=language,
            part=(index + 1, len(chunks)),
        )
        try:
            text, model_used = _request_analysis(client, models, prompt)
            used_model = model_used
            payload = _parse_json(text)
        except AIProviderError as exc:
            # One bad chunk must not kill a 40-chunk run: remember and continue,
            # so a transient 429 on chunk 3/8 still yields clips from the rest.
            logger.warning("Chunk %d/%d failed: %s", index + 1, len(chunks), exc.message)
            failures.append(f"chunk {index + 1}/{len(chunks)}: {exc.message}")
            last_status = exc.status_code or last_status
            continue

        summary = str(payload.get("summary") or "").strip()
        if summary:
            summaries.append(summary)
        kept_here = 0
        for raw in payload.get("clips") or []:
            clip = _coerce_clip(raw, duration, target_seconds=target_seconds)
            if clip is not None:
                collected.append(clip)
                kept_here += 1
        logger.info("Chunk %d/%d -> %d usable clip(s)", index + 1, len(chunks), kept_here)

    if on_progress:
        on_progress(99.0, "Ranking clips")

    clips = _rank_and_dedupe(collected, clip_count)
    if not clips:
        if failures and config.AI_FALLBACK_HEURISTIC:
            return _heuristic_fallback(
                segments,
                duration=duration,
                target_seconds=target_seconds,
                clip_count=clip_count,
                reason="; ".join(failures),
            )
        raise AIProviderError(
            "The model returned no usable clips."
            + (f" ({'; '.join(failures)})" if failures else "")
            + " Try a larger model or a different prompt.",
            status_code=last_status or 502,
            detail={"failures": failures},
        )

    result = {
        "summary": _merge_summaries(summaries),
        "clips": [clip.model_dump() for clip in clips],
        "model": used_model,
        "chunks": len(chunks),
    }
    if failures:
        result["warnings"] = failures
        result["partial"] = True
    return result


def _model_candidates(
    model: str | None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[str]:
    """Configured model first, then fallbacks, then auto-discovery.

    Leaving `AI_MODEL` empty used to abort the whole analysis with "No AI model
    configured" even though the provider had usable models. We now ask the
    provider what it serves before giving up.
    """
    primary = (model or config.AI_MODEL or "").strip()
    candidates: list[str] = [primary] if primary else []
    candidates.extend(m for m in config.AI_FALLBACK_MODELS if m not in candidates)

    if not candidates and config.AI_AUTO_DISCOVER_MODEL:
        discovered = _discover_models(base_url, api_key)
        if discovered:
            logger.info("No AI_MODEL set — auto-discovered %d model(s)", len(discovered))
            return discovered[: config.AI_DISCOVER_LIMIT]

    if not candidates:
        raise AIProviderError(
            "No AI model configured and the provider did not advertise any. "
            "Pick one in Settings (it lists the models your provider exposes) or set AI_MODEL in .env.",
            status_code=400,
        )
    return candidates


def _discover_models(base_url: str | None, api_key: str | None) -> list[str]:
    """Model ids to try when nothing is configured; never raises."""
    try:
        models = list_models(base_url, api_key)
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        logger.warning("Model auto-discovery failed: %s", exc)
        return []
    # "auto" is a router alias that works on 9Router-style proxies; prefer it
    # when present, otherwise fall back to the sorted model list.
    ids = [item["id"] for item in models if item.get("id")]
    alias = [name for name in ("auto", "free-stack") if name in ids]
    return alias + [name for name in ids if name not in alias]


def check_model(
    base_url: str | None, api_key: str | None, model: str
) -> tuple[bool, str]:
    """Send one tiny completion to prove the model really answers.

    `list_models` succeeding only proves the endpoint is up; routers often
    advertise models that have no upstream credentials ("No active credentials
    for provider: openai"). This makes the Settings test meaningful.
    """
    client = get_client(base_url, api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            # Reasoning models spend tokens before emitting any text, so a small
            # budget would return finish_reason='length' with empty content and
            # get misreported as a broken model. 64 covers the reasoning step.
            max_tokens=64,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, _provider_error(exc, context=f"model check '{model}'").message

    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        # An empty answer with finish_reason='length' means the model works but
        # burned its budget on reasoning — treat it as healthy, not as a failure.
        if getattr(choice, "finish_reason", None) == "length":
            logger.warning(
                "Model '%s' returned no text before the token limit (reasoning model); "
                "treating the probe as successful.",
                model,
            )
            return True, ""
        return False, (
            f"Model '{model}' answered with an empty message. "
            "It may be a reasoning model — try a model that returns text directly."
        )
    return True, ""


def _request_analysis(
    client: OpenAI, models: list[str], prompt: str
) -> tuple[str, str]:
    """Call the provider, trying each model and each response_format dialect."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    formats: list[dict[str, Any] | None] = [
        {
            "type": "json_schema",
            "json_schema": {
                "name": "video_analysis",
                "schema": _CLIP_JSON_SCHEMA,
                "strict": True,
            },
        },
        {"type": "json_object"},
        None,
    ]

    last_error: AIProviderError | None = None
    for candidate in models:
        for response_format in formats:
            try:
                content, used = _create_with_retry(client, candidate, messages, response_format)
                if content:
                    return content, used
                last_error = AIProviderError(
                    f"Model '{candidate}' returned an empty answer "
                    f"(response_format={_format_name(response_format)}).",
                    status_code=502,
                )
            except AIProviderError as exc:
                if getattr(exc, "format_rejected", False):
                    logger.info(
                        "Model %s rejects response_format=%s — trying the next dialect",
                        candidate,
                        _format_name(response_format),
                    )
                    last_error = exc
                    continue  # provider does not speak this dialect
                last_error = exc
                break  # model-level failure: move to the next model

    if last_error is not None:
        raise AIProviderError(
            f"Every configured model failed ({', '.join(models)}). "
            f"Last error: {last_error.message}",
            status_code=last_error.status_code,
            detail=last_error.detail,
        )
    raise AIProviderError(
        f"No response from any configured model ({', '.join(models)}).", status_code=502
    )


def _format_name(response_format: dict[str, Any] | None) -> str:
    if response_format is None:
        return "none"
    return str(response_format.get("type") or "?")


def _create_with_retry(
    client: OpenAI,
    candidate: str,
    messages: list[dict[str, str]],
    response_format: dict[str, Any] | None,
) -> tuple[str, str]:
    """One chat completion with exponential backoff on 429 / 5xx / timeout."""
    attempts = max(1, config.AI_MAX_RETRIES + 1)
    kwargs: dict[str, Any] = {
        "model": candidate,
        "messages": messages,
        "temperature": config.AI_TEMPERATURE,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    for attempt in range(attempts):
        try:
            response = client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            content = (choice.message.content or "").strip()
            if not content and getattr(choice, "finish_reason", None) == "length":
                # A reasoning model can spend the whole default budget thinking
                # and emit nothing. Retry with a bigger budget before giving up.
                logger.warning(
                    "Model '%s' hit the token limit without emitting text "
                    "(finish_reason=length); retrying with a larger budget.",
                    candidate,
                )
                kwargs["max_tokens"] = max(
                    int(kwargs.get("max_tokens") or 0) * 2, config.AI_MIN_OUTPUT_TOKENS
                )
                if attempt < attempts - 1:
                    continue
            return content, candidate
        except Exception as exc:  # noqa: BLE001 - provider errors vary wildly
            error = _provider_error(
                exc, context=f"chat/completions model={candidate} format={_format_name(response_format)}"
            )
            if _is_format_rejection(exc) and response_format is not None:
                error.format_rejected = True  # type: ignore[attr-defined]
                raise error from exc
            if attempt < attempts - 1 and _is_retryable(exc):
                delay = config.AI_RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Retryable provider error (attempt %d/%d) — retrying in %.1fs: %s",
                    attempt + 1,
                    attempts,
                    delay,
                    error.message,
                )
                time.sleep(delay)
                continue
            raise error from exc
    return "", candidate


def _is_format_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status == 400 and (
        "response_format" in text or "json_schema" in text or "unsupported" in text
    ):
        return True
    return any(
        marker in text
        for marker in (
            "response_format",
            "json_schema is not supported",
            "does not support json_schema",
            "unsupported parameter",
            "unknown parameter",
        )
    )


def _build_user_prompt(
    chunk: list[dict[str, Any]],
    *,
    duration: float,
    target_seconds: int,
    clip_count: int,
    custom_prompt: str | None,
    language: str | None,
    part: tuple[int, int],
) -> str:
    first = float(chunk[0]["start"])
    last = float(chunk[-1]["end"])
    target = "the model's own judgement (auto)" if target_seconds <= 0 else f"{target_seconds} seconds"
    lines = "\n".join(
        f"[{_mmss(float(segment['start']))}] {str(segment['text']).strip()}" for segment in chunk
    )
    language_hint = _language_name(language)
    extra = f"\nFocus requested by the user: {custom_prompt.strip()}" if custom_prompt else ""
    return (
        f"Video duration: {_mmss(duration)} ({duration:.1f}s total).\n"
        f"Transcript window: {_mmss(first)} → {_mmss(last)}"
        f"{f' (part {part[0]} of {part[1]})' if part[1] > 1 else ''}.\n"
        f"Target clip length: {target}.\n"
        f"Return exactly {clip_count} clips.\n"
        f"Output language: {language_hint}.\n"
        f"{extra}\n\n"
        f"Transcript:\n{lines}\n\n"
        "Return the JSON object now."
    )


_LANGUAGE_NAMES = {
    "en": "English",
    "id": "Indonesian",
    "ms": "Malay",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "nl": "Dutch",
    "ru": "Russian",
    "uk": "Ukrainian",
    "pl": "Polish",
    "tr": "Turkish",
    "ar": "Arabic",
    "fa": "Persian",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "ur": "Urdu",
    "th": "Thai",
    "vi": "Vietnamese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "tl": "Tagalog",
}


def _language_name(code: str | None) -> str:
    """Human-readable name for a Whisper language code, with a safe fallback."""
    key = (code or "").strip().lower()
    if not key:
        return "the original language of the transcript"
    return _LANGUAGE_NAMES.get(key, f"the original language of the transcript ({key})")


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _chunk_segments(
    segments: list[dict[str, Any]], max_chars: int
) -> list[list[dict[str, Any]]]:
    """Split the transcript into prompt-sized windows with one line of overlap."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for segment in segments:
        text = str(segment.get("text") or "")
        if current and size + len(text) > max_chars:
            chunks.append(current)
            current = [current[-1]]  # overlap keeps a sentence that straddles the cut
            size = len(str(current[0].get("text") or ""))
        current.append(segment)
        size += len(text)
    if current:
        chunks.append(current)
    return chunks


def _parse_json(text: str) -> dict[str, Any]:
    """Tolerant JSON parsing: fences, prose, trailing commas, truncation.

    Providers differ in how literally they follow the schema, so a bare array of
    clips (or an object keyed `moments`/`highlights`) is accepted and normalised.
    A response that is cut off mid-object is salvaged by closing the open
    brackets rather than failing the whole chunk.
    """
    if not (text or "").strip():
        raise AIProviderError(
            "The model returned an empty response, so nothing could be parsed. "
            "Try a different model or a shorter transcript window.",
            status_code=502,
        )

    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()

    for candidate in _json_candidates(cleaned):
        for attempt in _repair_variants(candidate):
            try:
                return _normalize_payload(json.loads(attempt))
            except (json.JSONDecodeError, ValueError):
                continue

    raise AIProviderError(
        f"The model did not return valid JSON "
        f"(response started: {_snippet(text, 120)!r}). "
        "Try a model with better JSON support or reduce ANALYZE_CHUNK_CHARS.",
        status_code=502,
    )


def _json_candidates(text: str) -> list[str]:
    """Plausible JSON substrings, most complete first."""
    out: list[str] = [text]
    start_obj, end_obj = text.find("{"), text.rfind("}")
    if start_obj != -1 and end_obj > start_obj:
        out.append(text[start_obj : end_obj + 1])
    start_arr, end_arr = text.find("["), text.rfind("]")
    if start_arr != -1 and end_arr > start_arr:
        out.append(text[start_arr : end_arr + 1])
    return out


def _repair_variants(text: str) -> list[str]:
    """Cheap `json.loads` fixes: bare timestamps, trailing commas, bracket closing."""
    variants = [text]
    # Models sometimes emit `"start_time": 21:03` (unquoted mm:ss), which is not
    # valid JSON and used to cost us the whole chunk. Quote the timestamp.
    quoted = re.sub(
        r'("(?:start_time|end_time|hook_time)"\s*:\s*)(\d{1,3}:\d{2}(?::\d{2})?)(\s*[,}\]])',
        r'\1"\2"\3',
        text,
    )
    if quoted != text:
        variants.append(quoted)
        text = quoted
    no_trailing = re.sub(r",\s*([}\]])", r"\1", text)
    if no_trailing not in variants:
        variants.append(no_trailing)
    # Salvage a truncated object/array by closing whatever is still open.
    base = no_trailing
    closers: list[str] = []
    in_string = False
    escaped = False
    for char in base:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            closers.append("}" if char == "{" else "]")
        elif char in "}]" and closers:
            closers.pop()
    if closers:
        trimmed = base.rstrip()
        if trimmed and trimmed[-1] not in ",{[":
            variants.append(base + "".join(reversed(closers)))
    return variants


def _normalize_payload(value: Any) -> dict[str, Any]:
    """Coerce whatever the model returned into `{summary, clips: [...]}`.

    Only dicts/lists that actually contain clip-shaped entries survive; anything
    else degrades to an empty result instead of raising, so one malformed chunk
    never aborts the run.
    """
    if isinstance(value, list):
        return {"summary": "", "clips": [item for item in value if isinstance(item, dict)]}
    if not isinstance(value, dict):
        return {"summary": "", "clips": []}
    payload: dict[str, Any] = dict(value)
    if not isinstance(payload.get("clips"), list):
        for key in ("moments", "highlights", "results", "data", "segments", "clips_list"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                payload["clips"] = candidate
                break
        else:
            payload["clips"] = []
    payload["clips"] = [item for item in payload["clips"] if isinstance(item, dict)]
    if not isinstance(payload.get("summary"), str):
        payload["summary"] = ""
    return payload


def _parse_time(value: Any) -> float | None:
    """Parse a timestamp that a model may render in several ways.

    Models are inconsistent here and it silently drops whole chunks:
    ``126``, ``126.5``, ``"126"``, ``"2:06"`` (m:ss), ``"1:02:06"`` (h:mm:ss)
    and ``"21:03"`` (mm:ss, seen from GLM via the router) all occur.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else None  # reject NaN

    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if number == number else None
    except ValueError:
        pass

    parts = text.split(":")
    if 2 <= len(parts) <= 3 and all(part.strip().lstrip("-").isdigit() for part in parts):
        try:
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + float(part)
            return seconds
        except ValueError:
            return None
    return None


def _coerce_clip(raw: Any, duration: float, target_seconds: float | None = None) -> ViralClip | None:
    """Validate one clip; drop it (rather than failing the run) when unusable."""
    if not isinstance(raw, dict):
        return None
    data = dict(raw)
    data.setdefault("title", "")

    start = _parse_time(data.get("start_time"))
    end = _parse_time(data.get("end_time"))
    if start is None or end is None or end <= start:
        return None

    data["start_time"] = max(0.0, min(start, max(0.0, duration - 1)))
    data["end_time"] = max(data["start_time"] + 1.0, min(end, duration or end))

    # A one-second clip is useless on a 9:16 timeline. When the model returns a
    # point-in-time highlight instead of a window, widen it to the requested
    # length, clamped to the video, keeping the model's start as the hook.
    if target_seconds:
        minimum = max(3.0, float(target_seconds) * 0.5)
        span = data["end_time"] - data["start_time"]
        if span < minimum:
            data["end_time"] = min(duration or data["start_time"] + minimum, data["start_time"] + float(target_seconds))
            if data["end_time"] - data["start_time"] < minimum:
                data["start_time"] = max(0.0, data["end_time"] - float(target_seconds))

    hook = _parse_time(data.get("hook_time"))
    data["hook_time"] = (
        max(data["start_time"], min(hook, data["end_time"]))
        if hook is not None
        else data["start_time"]
    )

    score = data.get("virality_score", 50)
    try:
        data["virality_score"] = int(round(float(score)))
    except (TypeError, ValueError):
        data["virality_score"] = 50
    data["virality_score"] = max(1, min(100, data["virality_score"]))

    quotes = data.get("key_quotes")
    if isinstance(quotes, str):
        data["key_quotes"] = [quotes]
    elif not isinstance(quotes, list):
        data["key_quotes"] = []
    else:
        data["key_quotes"] = [str(item) for item in quotes][:3]

    for key in ("title", "transcript", "title_suggestion", "caption_suggestion", "hashtag_suggestion"):
        if not isinstance(data.get(key), str):
            data[key] = "" if data.get(key) is None else str(data.get(key))

    try:
        return ViralClip.model_validate(data)
    except Exception:  # noqa: BLE001 - one bad clip must not kill the run
        return None


def _rank_and_dedupe(clips: list[ViralClip], limit: int) -> list[ViralClip]:
    """Sort by score and drop clips that overlap an already-kept clip."""
    ordered = sorted(clips, key=lambda clip: clip.virality_score, reverse=True)
    kept: list[ViralClip] = []
    for clip in ordered:
        if any(_overlaps(clip, existing) for existing in kept):
            continue
        kept.append(clip)
        if len(kept) >= max(1, min(limit, config.ANALYZE_MAX_CLIPS)):
            break
    return kept


def _overlaps(a: ViralClip, b: ViralClip, threshold: float = 0.25) -> bool:
    overlap = min(a.end_time, b.end_time) - max(a.start_time, b.start_time)
    if overlap <= 0:
        return False
    shortest = min(a.end_time - a.start_time, b.end_time - b.start_time) or 1.0
    return overlap / shortest >= threshold


def _merge_summaries(summaries: list[str]) -> str:
    unique: list[str] = []
    for summary in summaries:
        cleaned = " ".join(summary.split())
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    merged = " ".join(unique)
    return merged[:1200]


def _friendly_error(exc: Exception) -> str:
    """Kept for backwards compatibility; delegates to the structured describer."""
    return _provider_error(exc).message


# --- Local heuristic fallback -------------------------------------------------
#
# Used only when every model attempt failed. It is deliberately simple and
# explainable: dense speech + short pauses + sentence boundaries score higher.
# The caller is told `fallback=True` so the UI can label the result.

_HEURISTIC_FILLERS = {
    "uh", "um", "umm", "eee", "hmm", "like", "you", "know", "gitu", "kan", "ya",
    "jadi", "eee", "apa", "namanya",
}


def _heuristic_fallback(
    segments: list[dict[str, Any]],
    *,
    duration: float,
    target_seconds: int,
    clip_count: int,
    reason: str,
) -> dict[str, Any]:
    """Score windows locally when the model is unusable; never raises."""
    logger.warning("AI scoring failed for every model — using local heuristic fallback (%s)", reason)

    usable = [
        segment
        for segment in segments
        if str(segment.get("text") or "").strip() and float(segment.get("end", 0)) > float(segment.get("start", 0))
    ]
    if not usable:
        raise AIProviderError(
            f"No transcript content to score locally either ({reason}).", status_code=422
        )

    target = float(target_seconds) if target_seconds and target_seconds > 0 else 30.0
    target = max(config.ANALYZE_MIN_CLIP_SECONDS, min(target, 90.0))
    windows = _sliding_windows(usable, target)

    scored: list[tuple[float, list[dict[str, Any]]]] = []
    for window in windows:
        scored.append((_window_score(window), window))
    scored.sort(key=lambda item: item[0], reverse=True)

    clips: list[ViralClip] = []
    taken: list[tuple[float, float]] = []
    for score, window in scored:
        start = float(window[0]["start"])
        end = float(window[-1]["end"])
        if any(start < t_end and end > t_start for t_start, t_end in taken):
            continue
        taken.append((start, end))
        text = " ".join(str(item["text"]).strip() for item in window).strip()
        clips.append(
            ViralClip(
                title=_first_sentence(text)[:70] or f"Highlight {len(clips) + 1}",
                start_time=round(start, 2),
                end_time=round(min(end, duration or end), 2),
                hook_time=round(start, 2),
                virality_score=max(1, min(100, int(round(score)))),
                key_quotes=[_first_sentence(text)[:120]] if text else [],
                transcript=text[:800],
                title_suggestion=_first_sentence(text)[:70],
                caption_suggestion=text[:140],
                hashtag_suggestion="",
            )
        )
        if len(clips) >= max(1, min(clip_count, config.ANALYZE_MAX_CLIPS)):
            break

    if not clips:
        raise AIProviderError(
            f"Local fallback could not build a clip either ({reason}).", status_code=422
        )

    return {
        "summary": (
            "Skor dihitung lokal (fallback heuristik) karena model AI tidak dapat dipakai: "
            f"{reason}. Urutan berdasarkan kepadatan bicara dan jeda."
        ),
        "clips": [clip.model_dump() for clip in clips],
        "model": "local-heuristic",
        "chunks": 0,
        "fallback": True,
        "warnings": [reason],
    }


def _sliding_windows(
    segments: list[dict[str, Any]], target: float
) -> list[list[dict[str, Any]]]:
    """Every contiguous run of segments covering roughly `target` seconds."""
    windows: list[list[dict[str, Any]]] = []
    start_index = 0
    while start_index < len(segments):
        window: list[dict[str, Any]] = []
        end_index = start_index
        while end_index < len(segments):
            window.append(segments[end_index])
            span = float(segments[end_index]["end"]) - float(segments[start_index]["start"])
            end_index += 1
            if span >= target:
                break
        if len(window) >= 2:
            windows.append(window)
        start_index += max(1, len(window) // 2)  # 50% stride keeps overlaps cheap
    return windows or [segments]


def _window_score(window: list[dict[str, Any]]) -> float:
    """0-100 score from speech density, pause structure and opening strength."""
    text = " ".join(str(item.get("text") or "") for item in window).lower()
    words = [word for word in re.findall(r"[a-zA-ZÀ-ÿ']+", text)]
    if not words:
        return 1.0
    span = max(0.001, float(window[-1]["end"]) - float(window[0]["start"]))
    density = len(words) / span  # words per second; conversational ≈ 2.5-4.5

    pauses = [
        float(window[i + 1]["start"]) - float(window[i]["end"])
        for i in range(len(window) - 1)
    ]
    long_pauses = sum(1 for pause in pauses if pause > 0.8)
    pause_penalty = min(18.0, long_pauses * 6.0)

    fillers = sum(1 for word in words if word in _HEURISTIC_FILLERS)
    filler_penalty = min(12.0, (fillers / max(1, len(words))) * 100.0)

    opening = " ".join(str(item.get("text") or "") for item in window[:2]).strip()
    hook_bonus = 8.0 if opening and opening[0].isupper() else 0.0
    if "?" in opening:
        hook_bonus += 5.0  # questions hook viewers

    score = 45.0 + (density - 3.0) * 12.0 + hook_bonus - pause_penalty - filler_penalty
    return max(1.0, min(100.0, score))


def _first_sentence(text: str) -> str:
    """First sentence-ish chunk, trimmed for a title."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    match = re.match(r"(.{10,120}?[.!?])\s", cleaned)
    if match:
        return match.group(1)
    return cleaned[:90]
