"""Offline checks for the AI scoring stage (highlight scoring).

Everything here is mocked or offline, so the suite runs without a provider,
without a network and without touching the real `.env`. It pins the failure
modes that made analysis abort with an unhelpful message:

1. no model configured  → auto-discovery kicks in (was: "No AI model configured")
2. slow/timeout model   → configurable timeout + retryable backoff
3. HTTP 429             → retried, then a clear rate-limit message
4. wrong API key (401)  → clear auth message + status 401
5. empty / fenced / truncated / trailing-comma JSON → parsed or explained
6. long transcript      → chunked, per-chunk progress reported
7. one bad chunk        → the rest still score (partial result)
8. total model failure  → labelled local heuristic fallback, never silent

Run: backend/.venv/bin/python -m backend.scripts.ai_service_test
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

from backend import config
from backend.services import ai_service
from backend.services.ai_service import AIProviderError

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(f"{name} {detail}")


# --- fakes -------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """Records calls and replays a scripted list of outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.prompts: list[str] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        self.prompts.append(str(kwargs.get("messages", [{}])[-1].get("content", "")))
        if not self.outcomes:
            raise AssertionError("FakeCompletions ran out of scripted outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


class _FakeModels:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def list(self) -> Any:
        class _Item:
            def __init__(self, id_: str) -> None:
                self.id = id_
                self.owned_by = "fake"
                self.created = None

        class _Resp:
            def __init__(self, items: list[Any]) -> None:
                self.data = items

        return _Resp([_Item(name) for name in self.ids])


class _FakeClient:
    def __init__(self, outcomes: list[Any], model_ids: list[str] | None = None) -> None:
        self.chat = self  # client.chat.completions.create
        self.completions = _FakeCompletions(outcomes)
        self.models = _FakeModels(model_ids or [])


def _status_error(status: int, message: str, code: str = "invalid_api_key") -> Exception:
    """Build an exception shaped like the OpenAI SDK's APIStatusError."""
    from openai import APIStatusError
    import httpx

    request = httpx.Request("POST", "http://localhost:20128/v1/chat/completions")
    response = httpx.Response(status, json={"error": {"message": message, "type": code, "code": code}}, request=request)
    return APIStatusError(message, response=response, body=None)


def _clip_payload(count: int = 1) -> str:
    clips = []
    for i in range(count):
        clips.append(
            {
                "title": f"Momen {i + 1}",
                "start_time": i * 30.0,
                "end_time": i * 30.0 + 30.0,
                "hook_time": i * 30.0,
                "virality_score": 80 - i,
                "key_quotes": ["ini kutipan"],
                "transcript": "ini transkrip",
                "title_suggestion": "judul",
                "caption_suggestion": "caption",
                "hashtag_suggestion": "#a #b",
            }
        )
    return json.dumps({"summary": "Ringkasan video.", "clips": clips})


SEGMENTS = [
    {"start": float(i * 5), "end": float(i * 5 + 5), "text": f"Kalimat ke-{i} tentang topik menarik."}
    for i in range(6)
]


# --- 1. model auto-discovery -------------------------------------------------


def test_model_discovery() -> None:
    print("1. Model discovery when AI_MODEL is empty")
    original = config.AI_MODEL
    try:
        config.AI_MODEL = ""
        config.AI_AUTO_DISCOVER_MODEL = True
        ai_service.list_models = lambda *a, **k: [  # type: ignore[assignment]
            {"id": "free-stack", "owned_by": "", "created": None},
            {"id": "cbai/glm-5.3", "owned_by": "", "created": None},
        ]
        candidates = ai_service._model_candidates(None)
        check("discovers models instead of raising", bool(candidates), str(candidates))
        check("prefers router alias 'free-stack'", candidates[0] == "free-stack", str(candidates))
    finally:
        config.AI_MODEL = original


# --- 2. error surfacing ------------------------------------------------------


def test_error_surfacing() -> None:
    print("2. Error surfacing (no bare 'unknown error')")
    cases = [
        (_status_error(401, "Invalid API key", "invalid_api_key"), 401, "401"),
        (_status_error(429, "Rate limit reached", "rate_limit_error"), 429, "429"),
        (_status_error(400, "Model 'nope' not found", "bad_request"), 400, "400"),
        (_status_error(404, "Unknown API route", "not_found"), 404, "404"),
    ]
    for exc, expected_status, marker in cases:
        error = ai_service._provider_error(exc, context="test")
        check(
            f"HTTP {expected_status} mapped to status_code",
            error.status_code == expected_status,
            f"got {error.status_code}",
        )
        check(f"HTTP {expected_status} message mentions cause", marker in error.message, error.message)
        check(f"HTTP {expected_status} keeps raw detail", bool(error.detail.get("raw")), str(error.detail))

    # A generic exception must still name its type.
    error = ai_service._provider_error(RuntimeError("boom"), context="test")
    check("generic error names the type", "RuntimeError" in error.message, error.message)
    check("generic error has no empty message", bool(error.message.strip()), error.message)

    # API keys must never leak into a message or detail. The fake key is built
    # at runtime so this file never contains a literal that looks like one.
    fake_key = "sk-" + ("s3cr3tvalu3" * 3)
    leak = _status_error(401, f"Bad key {fake_key}", "invalid_api_key")
    error = ai_service._provider_error(leak, context="test")
    check("API key redacted from message", fake_key not in error.message, error.message)
    check("API key redacted from raw", fake_key not in str(error.detail), str(error.detail))
    check("message keeps a masked marker", "***" in error.message, error.message)


def test_empty_response_message() -> None:
    print("3. Empty response is explained, never 'unknown error'")
    try:
        ai_service._parse_json("")
        check("empty response raises", False, "no exception")
    except AIProviderError as exc:
        check("empty response mentions 'empty'", "empty" in exc.message.lower(), exc.message)


# --- 4. tolerant JSON parsing ------------------------------------------------


def test_parse_json() -> None:
    print("4. Tolerant JSON parsing")
    good = _clip_payload(1)

    cases = {
        "plain object": good,
        "fenced json": f"```json\n{good}\n```",
        "prose around": f"Here you go:\n{good}\nHope that helps!",
        "trailing comma": good.replace('"hashtag_suggestion": "#a #b"', '"hashtag_suggestion": "#a #b",'),
        "bare array": json.dumps(json.loads(good)["clips"]),
        "keyed moments": json.dumps({"summary": "s", "moments": json.loads(good)["clips"]}),
    }
    for name, text in cases.items():
        try:
            payload = ai_service._parse_json(text)
            ok = len(payload.get("clips") or []) == 1
        except Exception as exc:  # noqa: BLE001
            ok = False
            payload = {"error": str(exc)}
        check(f"parses {name}", ok, str(payload)[:160])

    # Truncated mid-object: salvage the first complete clip rather than failing.
    two = _clip_payload(2)
    truncated = two[: two.rfind("}]") + 1]
    try:
        payload = ai_service._parse_json(truncated)
        check("salvages truncated JSON", len(payload.get("clips") or []) >= 1, str(payload)[:160])
    except AIProviderError as exc:
        check("truncated JSON error explains itself", "JSON" in exc.message, exc.message)

    # Garbage must still raise a descriptive error.
    try:
        ai_service._parse_json("I cannot help with that request.")
        check("garbage raises", False, "no exception")
    except AIProviderError as exc:
        check("garbage error quotes the response", "JSON" in exc.message, exc.message)


# --- 5. retry + backoff ------------------------------------------------------


def test_retry_backoff() -> None:
    print("5. Retry with exponential backoff")
    original_retries, original_base = config.AI_MAX_RETRIES, config.AI_RETRY_BASE_SECONDS
    try:
        config.AI_MAX_RETRIES = 2
        config.AI_RETRY_BASE_SECONDS = 0.01
        client = _FakeClient([_status_error(429, "Rate limit"), _clip_payload(1)])
        started = time.time()
        content, model = ai_service._create_with_retry(client, "m", [{"role": "user", "content": "p"}], None)
        elapsed = time.time() - started
        check("retries a 429 then succeeds", bool(content), content[:60])
        check("waited between attempts", elapsed >= 0.01, f"{elapsed:.3f}s")
        check("made 2 attempts", len(client.completions.calls) == 2, str(len(client.completions.calls)))

        # A non-retryable 401 must NOT be retried.
        client = _FakeClient([_status_error(401, "Invalid API key")])
        try:
            ai_service._create_with_retry(client, "m", [{"role": "user", "content": "p"}], None)
            check("401 raises", False, "no exception")
        except AIProviderError as exc:
            check("401 is not retried", len(client.completions.calls) == 1, str(len(client.completions.calls)))
            check("401 status preserved", exc.status_code == 401, str(exc.status_code))
    finally:
        config.AI_MAX_RETRIES, config.AI_RETRY_BASE_SECONDS = original_retries, original_base


def test_timeout_is_retryable_and_explained() -> None:
    print("6. Timeout is retryable and explained")
    from openai import APITimeoutError
    import httpx

    request = httpx.Request("POST", "http://localhost:20128/v1/chat/completions")
    exc = APITimeoutError(request=request)
    check("timeout is retryable", ai_service._is_retryable(exc))
    error = ai_service._provider_error(exc, context="test")
    check("timeout message mentions the timeout value", "detik" in error.message, error.message)
    check("timeout status is 504", error.status_code == 504, str(error.status_code))


def test_connect_failure_is_retryable() -> None:
    print("7. Connection failure is retryable and explained")
    from openai import APIConnectionError
    import httpx

    request = httpx.Request("POST", "http://localhost:9999/v1/chat/completions")
    exc = APIConnectionError(request=request)
    check("connection error is retryable", ai_service._is_retryable(exc))
    error = ai_service._provider_error(exc, context="test")
    check("connection message names the base URL", config.AI_BASE_URL in error.message, error.message)


# --- 6. chunking + progress --------------------------------------------------


def test_chunking_and_progress() -> None:
    print("8. Long transcript is chunked with per-chunk progress")
    original_chunk, original_model = config.ANALYZE_CHUNK_CHARS, config.AI_MODEL
    original_get_client = ai_service.get_client
    try:
        config.AI_MODEL = "fake-model"
        config.ANALYZE_CHUNK_CHARS = 200  # force several chunks
        big = [
            {"start": float(i * 4), "end": float(i * 4 + 4), "text": "teks yang cukup panjang " * 4}
            for i in range(40)
        ]
        client = _FakeClient([_clip_payload(2)] * 12)
        ai_service.get_client = lambda *a, **k: client  # type: ignore[assignment]

        seen: list[str] = []
        out = ai_service.analyze_transcript(
            big,
            duration=160.0,
            target_seconds=30,
            clip_count=4,
            language="id",
            on_progress=lambda pct, msg: seen.append(msg),
        )
        chunks = out["chunks"]
        check("splits into multiple chunks", chunks > 1, f"chunks={chunks}")
        check(
            "progress labels each chunk",
            any("1/" in m and str(chunks) in m for m in seen),
            str(seen[:3]),
        )
        check("returns clips", len(out["clips"]) > 0, str(len(out["clips"])))
    finally:
        config.ANALYZE_CHUNK_CHARS = original_chunk
        config.AI_MODEL = original_model
        ai_service.get_client = original_get_client  # type: ignore[assignment]


def test_partial_chunk_failure() -> None:
    print("9. One bad chunk does not kill the run")
    original_chunk, original_model = config.ANALYZE_CHUNK_CHARS, config.AI_MODEL
    original_get_client = ai_service.get_client
    original_retries, original_base = config.AI_MAX_RETRIES, config.AI_RETRY_BASE_SECONDS
    try:
        config.AI_MODEL = "fake-model"
        config.ANALYZE_CHUNK_CHARS = 200
        config.AI_MAX_RETRIES = 0
        config.AI_RETRY_BASE_SECONDS = 0.01
        big = [
            {"start": float(i * 4), "end": float(i * 4 + 4), "text": "teks yang cukup panjang " * 4}
            for i in range(40)
        ]
        # First chunk: hard failure. Rest: fine.
        client = _FakeClient([_status_error(429, "Rate limit")] + [_clip_payload(2)] * 11)
        ai_service.get_client = lambda *a, **k: client  # type: ignore[assignment]

        out = ai_service.analyze_transcript(
            big, duration=160.0, target_seconds=30, clip_count=4, language="id"
        )
        check("still returns clips", len(out["clips"]) > 0, str(len(out["clips"])))
        check("marked partial", bool(out.get("partial")), str(out.get("warnings")))
        check("warning names the failing chunk", any("chunk 1/" in w for w in out.get("warnings", [])), str(out.get("warnings")))
    finally:
        config.ANALYZE_CHUNK_CHARS = original_chunk
        config.AI_MODEL = original_model
        config.AI_MAX_RETRIES = original_retries
        config.AI_RETRY_BASE_SECONDS = original_base
        ai_service.get_client = original_get_client  # type: ignore[assignment]


# --- 7. heuristic fallback ---------------------------------------------------


def test_heuristic_fallback() -> None:
    print("10. Total model failure falls back to a labelled local heuristic")
    original_model, original_heuristic = config.AI_MODEL, config.AI_FALLBACK_HEURISTIC
    original_get_client = ai_service.get_client
    original_retries = config.AI_MAX_RETRIES
    try:
        config.AI_MODEL = "fake-model"
        config.AI_FALLBACK_HEURISTIC = True
        config.AI_MAX_RETRIES = 0
        client = _FakeClient([_status_error(500, "Server exploded")])
        ai_service.get_client = lambda *a, **k: client  # type: ignore[assignment]

        out = ai_service.analyze_transcript(
            SEGMENTS, duration=30.0, target_seconds=15, clip_count=3, language="id"
        )
        check("fallback flag is set", out.get("fallback") is True, str(out.get("model")))
        check("model is labelled local-heuristic", out["model"] == "local-heuristic", out["model"])
        check("summary explains the fallback", "fallback" in out["summary"].lower(), out["summary"][:120])
        check("produced clips", len(out["clips"]) > 0, str(len(out["clips"])))
        check(
            "scores are clamped 1..100",
            all(1 <= c["virality_score"] <= 100 for c in out["clips"]),
            str([c["virality_score"] for c in out["clips"]]),
        )
    finally:
        config.AI_MODEL = original_model
        config.AI_FALLBACK_HEURISTIC = original_heuristic
        config.AI_MAX_RETRIES = original_retries
        ai_service.get_client = original_get_client  # type: ignore[assignment]


def test_fallback_can_be_disabled() -> None:
    print("11. Fallback can be disabled (then the real error surfaces)")
    original_model, original_heuristic = config.AI_MODEL, config.AI_FALLBACK_HEURISTIC
    original_get_client = ai_service.get_client
    original_retries = config.AI_MAX_RETRIES
    try:
        config.AI_MODEL = "fake-model"
        config.AI_FALLBACK_HEURISTIC = False
        config.AI_MAX_RETRIES = 0
        client = _FakeClient([_status_error(500, "Server exploded")])
        ai_service.get_client = lambda *a, **k: client  # type: ignore[assignment]
        try:
            ai_service.analyze_transcript(
                SEGMENTS, duration=30.0, target_seconds=15, clip_count=3, language="id"
            )
            check("raises when fallback disabled", False, "no exception")
        except AIProviderError as exc:
            check("error names the cause", "500" in exc.message or "Server" in exc.message, exc.message)
            check("error status is 500", exc.status_code == 500, str(exc.status_code))
    finally:
        config.AI_MODEL = original_model
        config.AI_FALLBACK_HEURISTIC = original_heuristic
        config.AI_MAX_RETRIES = original_retries
        ai_service.get_client = original_get_client  # type: ignore[assignment]


# --- 8. clip coercion --------------------------------------------------------


def test_coerce_clip() -> None:
    print("12. Clip validation clamps and rejects junk")
    bad = {"start_time": 10, "end_time": 5, "virality_score": 500}
    check("rejects end<=start", ai_service._coerce_clip(bad, 100.0) is None)

    clamped = ai_service._coerce_clip(
        {"start_time": 5, "end_time": 20, "virality_score": 500, "key_quotes": "satu"}, 30.0
    )
    check("clamps score to 100", clamped is not None and clamped.virality_score == 100, str(clamped))
    check("wraps a string quote into a list", clamped is not None and clamped.key_quotes == ["satu"], str(clamped))

    negative = ai_service._coerce_clip(
        {"start_time": -50, "end_time": 20, "virality_score": -5}, 30.0
    )
    check("clamps negative start", negative is not None and negative.start_time >= 0.0, str(negative))
    check("clamps score to >=1", negative is not None and negative.virality_score >= 1, str(negative))
    check("rejects non-dict", ai_service._coerce_clip("nope", 30.0) is None)


def test_timestamp_formats() -> None:
    """Models render timestamps inconsistently; every form must survive."""
    print("14. Timestamp formats (regression: real e2e returned '21:03')")
    cases = [
        (126, 126.0),
        (126.5, 126.5),
        ("126", 126.0),
        ("126.5", 126.5),
        ("2:06", 126.0),
        ("21:03", 1263.0),
        ("1:02:06", 3726.0),
    ]
    for value, expected in cases:
        got = ai_service._parse_time(value)
        check(f"parses {value!r}", got == expected, f"got {got}, want {expected}")

    for bad in (None, "", "abc", float("nan"), "1:2:x"):
        got = ai_service._parse_time(bad)
        check(f"rejects {bad!r}", got is None, f"got {got}")

    # The exact response that failed chunk 4/9 in the 55-minute e2e run:
    # an unquoted mm:ss timestamp, which is not valid JSON on its own.
    broken = '{"clips": [{"start_time": 21:03, "end_time": 21:33, "virality_score": 92, "title": "Respons"}]}'
    try:
        payload = ai_service._parse_json(broken)
        check("repairs unquoted mm:ss JSON", len(payload.get("clips") or []) == 1, str(payload)[:160])
        clip = ai_service._coerce_clip(payload["clips"][0], 3284.0)
        check("mm:ss clip gets real seconds", clip is not None and clip.start_time == 1263.0, str(clip))
    except AIProviderError as exc:
        check("repairs unquoted mm:ss JSON", False, exc.message)


def test_short_clip_is_widened() -> None:
    """A 1-second 'highlight' must not reach the timeline (seen in e2e)."""
    print("15. Too-short clips are widened to a usable window")
    raw = {"start_time": 35.33, "end_time": 36.33, "virality_score": 90, "title": "Rumus 50/30/30"}
    clip = ai_service._coerce_clip(raw, duration=3284.0, target_seconds=30.0)
    span = (clip.end_time - clip.start_time) if clip else 0.0
    check("clip survives", clip is not None, str(clip))
    check("widened to >= 15s", span >= 15.0, f"{span:.2f}s")
    check("start preserved as hook", clip is not None and abs(clip.start_time - 35.33) < 0.01, str(clip.start_time))

    # Without a target it must still never be shorter than 1 s (existing rule).
    clip = ai_service._coerce_clip(raw, duration=3284.0)
    check("no target keeps >=1s", clip is not None and clip.end_time - clip.start_time >= 1.0, str(clip))

    # Near the end of the video the window is clamped inside the duration.
    raw = {"start_time": 3283.0, "end_time": 3283.5, "virality_score": 90}
    clip = ai_service._coerce_clip(raw, duration=3284.0, target_seconds=30.0)
    check("clamped to duration", clip is not None and clip.end_time <= 3284.0, str(clip.end_time))


def test_cancel_stops_early() -> None:
    print("13. Cancel stops the run")
    original_get_client = ai_service.get_client
    try:
        ai_service.get_client = lambda *a, **k: _FakeClient([_clip_payload(1)])  # type: ignore[assignment]
        cancel = threading.Event()
        cancel.set()
        try:
            ai_service.analyze_transcript(
                SEGMENTS, duration=30.0, target_seconds=15, clip_count=2, cancel=cancel
            )
            check("cancel raises", False, "no exception")
        except AIProviderError as exc:
            check("cancel message is clear", "cancel" in exc.message.lower(), exc.message)
    finally:
        ai_service.get_client = original_get_client  # type: ignore[assignment]


def main() -> int:
    print("AI scoring stage — offline checks\n")
    for test in (
        test_model_discovery,
        test_error_surfacing,
        test_empty_response_message,
        test_parse_json,
        test_retry_backoff,
        test_timeout_is_retryable_and_explained,
        test_connect_failure_is_retryable,
        test_chunking_and_progress,
        test_partial_chunk_failure,
        test_heuristic_fallback,
        test_fallback_can_be_disabled,
        test_coerce_clip,
        test_cancel_stops_early,
        test_timestamp_formats,
        test_short_clip_is_widened,
    ):
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            print(f"  ERROR in {test.__name__}: {type(exc).__name__}: {exc}")
            FAILURES.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")
        print()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for item in FAILURES:
            print(" -", item)
        return 1
    print("All AI scoring checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
