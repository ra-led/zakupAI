"""Unified LLM transport for zakupAI.

Replaces the two pre-existing chat-completion clients (``app/llm_openai.py``
and ``app/services/llm_client.py``). Both used the OpenAI Python SDK against
different ``base_url``\\s — one against api.openai.com directly, the other
against openrouter.ai. They diverged on:

  * which env vars they read (``OPENAI_*`` vs ``OPENROUTER_*``);
  * sync vs async (sync OpenAI vs AsyncOpenAI);
  * whether they recorded usage (only the OpenAI one did).

This module collapses them into a single transport that:

  * Reads ``LLM_*`` env vars by default with full backward compatibility for
    the old names — existing prod ``.env`` files keep working.
  * Lets every call site swap the model (and even the provider) per task
    via ``LLM_MODEL_<TASK>`` / ``LLM_BASE_URL_<TASK>`` env vars, no code
    changes needed.
  * Exposes both sync and async chat-completion entrypoints. The async
    variant is just the sync one offloaded to a thread, since the OpenAI SDK
    doesn't have a real async client for our use cases — and we want one
    code path to keep auditing simple.
  * Always records usage through ``app.usage_tracking.record_usage`` so the
    admin dashboard sees every call regardless of which task issued it.

Per-task overrides
------------------

Each call passes a ``task`` string. Resolution priority:

  LLM_MODEL_<TASK_UPPER>      → LLM_MODEL → OPENAI_MODEL → OPENROUTER_MODEL → built-in default
  LLM_BASE_URL_<TASK_UPPER>   → LLM_BASE_URL → OPENAI_BASE_URL → OPENROUTER_BASE_URL → openai default
  LLM_API_KEY_<TASK_UPPER>    → LLM_API_KEY → OPENAI_API_KEY → OPENROUTER_API_KEY

So a typical zakupAI ``.env`` only needs ``LLM_API_KEY`` and ``LLM_MODEL``.
Power users can route ``compare_characteristics`` to a stronger model
without touching anything else by setting
``LLM_MODEL_COMPARE_CHARACTERISTICS=anthropic/claude-3.5-sonnet``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, OpenAI

from ..usage_tracking import record_usage, save_trace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Built-in last-resort defaults if neither LLM_* nor legacy env vars are set.
# Picked to match the historical behavior of llm_openai.py / llm_client.py:
#  - openrouter base url so the same key works for OpenAI / Anthropic / Gemini /
#    open-source models without code changes;
#  - gemini-flash as the cheap default (matches the old llm_client default).
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "google/gemini-2.0-flash-001"

# Default per-call timeout. Lots extraction is the longest job we run today
# (~120 s on big TZs); search-queries are usually under 30 s. Individual
# call sites may pass their own timeout.
_DEFAULT_TIMEOUT = 180.0

# When True, full request/response is saved to LLMTrace table for debugging.
_TRACE_ENABLED = os.getenv("LLM_TRACE_ENABLED", "false").lower() == "true"

# Transient-failure retry policy. Triggered on 429 (rate limit), 5xx gateway
# errors, and raw connection drops. Under 10 concurrent supplier_search
# pipelines a naive client would lose half the requests to bursts; the
# backoff lets OpenRouter/Perplexity/etc. drain their queue before we retry.
_RETRY_MAX_ATTEMPTS = int(os.getenv("LLM_RETRY_MAX_ATTEMPTS", "3"))
_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY_S", "1.0"))
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, APIConnectionError):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None) if response is not None else None
    return status_code in _RETRYABLE_STATUS


# ---------------------------------------------------------------------------
# Config resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    """Resolved per-task LLM endpoint config."""

    task: str
    base_url: Optional[str]
    api_key: str
    model: str

    def __repr__(self) -> str:  # don't leak the api key in logs
        return f"LLMConfig(task={self.task!r}, base_url={self.base_url!r}, model={self.model!r})"


def _env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Return the first env var that is set and non-empty, else default."""
    for name in names:
        val = os.getenv(name)
        if val is not None and val.strip() != "":
            return val
    return default


def _task_env_suffix(task: str) -> str:
    """Convert a task name like 'kp_items_extraction' to 'KP_ITEMS_EXTRACTION'."""
    return task.upper().replace("-", "_").replace(".", "_")


def resolve_config(task: str) -> LLMConfig:
    """Compute the LLM endpoint to use for a given task name.

    See module docstring for the resolution chain.
    """
    suffix = _task_env_suffix(task)

    base_url = _env_first(
        f"LLM_BASE_URL_{suffix}",
        "LLM_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENROUTER_BASE_URL",
        default=_DEFAULT_BASE_URL,
    )
    api_key = _env_first(
        f"LLM_API_KEY_{suffix}",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    )
    if not api_key:
        raise RuntimeError(
            f"No LLM API key configured for task '{task}'. "
            f"Set LLM_API_KEY (or task-specific LLM_API_KEY_{suffix})."
        )
    model = _env_first(
        f"LLM_MODEL_{suffix}",
        "LLM_MODEL",
        "OPENAI_MODEL",
        "OPENROUTER_MODEL",
        default=_DEFAULT_MODEL,
    )

    return LLMConfig(task=task, base_url=base_url, api_key=api_key, model=model)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _build_client(cfg: LLMConfig, timeout: float) -> OpenAI:
    """Build a fresh OpenAI SDK client. We don't cache because clients are
    cheap to build and per-call timeouts vary."""
    kwargs: Dict[str, Any] = {
        "api_key": cfg.api_key,
        "timeout": timeout,
        "default_headers": {
            # OpenRouter uses these for accounting; harmless to other providers.
            "HTTP-Referer": "https://zakupai.app",
            "X-Title": "ZakupAI",
        },
    }
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return OpenAI(**kwargs)


# ---------------------------------------------------------------------------
# Usage-tracking helper
# ---------------------------------------------------------------------------


def _channel_for(cfg: LLMConfig) -> str:
    """Best-effort label for the LLMUsage.channel column.

    The dashboard groups spend by channel, so we want a stable, recognizable
    name. Heuristic: pick a substring of the base_url, fall back to 'llm'.
    """
    if cfg.base_url:
        host = cfg.base_url.lower()
        if "openrouter" in host:
            return "openrouter"
        if "openai" in host:
            return "openai"
        if "anthropic" in host:
            return "anthropic"
    return "llm"


# ---------------------------------------------------------------------------
# Retry wrapper — handles 429/5xx/connection drops with exponential backoff
# ---------------------------------------------------------------------------


def _call_with_retry(client: OpenAI, request_kwargs: Dict[str, Any], *, task: str) -> Any:
    attempt = 0
    while True:
        attempt += 1
        try:
            return client.chat.completions.create(**request_kwargs)
        except Exception as exc:  # noqa: BLE001
            if attempt >= _RETRY_MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "[llm:%s] retryable error (attempt %d/%d), sleeping %.1fs: %s",
                task, attempt, _RETRY_MAX_ATTEMPTS, delay, exc,
            )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Sync chat-completion entrypoint
# ---------------------------------------------------------------------------


def chat_completion(
    messages: List[Dict[str, Any]],
    *,
    task: str,
    response_format: Optional[Dict[str, Any]] = None,
    max_completion_tokens: Optional[int] = None,
    extra_body: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Any:
    """Run one chat-completion call and record usage.

    Returns the parsed OpenAI SDK response object (same as
    ``client.chat.completions.create``). The caller pulls
    ``response.choices[0].message.content`` out of it.

    On any failure, a single ``record_usage(success=False)`` row is written
    and the exception is re-raised so the caller can decide what to do.
    """
    cfg = resolve_config(task)
    client = _build_client(cfg, timeout=timeout)

    # OpenRouter returns spend info only when extra_body usage.include is set.
    # Other providers tolerate the extra field harmlessly.
    body = dict(extra_body) if extra_body else {}
    body.setdefault("usage", {"include": True})

    request_kwargs: Dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "extra_body": body,
    }
    if response_format is not None:
        request_kwargs["response_format"] = response_format
    if max_completion_tokens is not None:
        request_kwargs["max_completion_tokens"] = max_completion_tokens

    channel = _channel_for(cfg)
    t0 = time.monotonic()
    try:
        response = _call_with_retry(client, request_kwargs, task=task)
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("[llm:%s] request failed: %s", task, exc)
        record_usage(
            channel=channel,
            operation=task,
            model=cfg.model,
            success=False,
            error_message=str(exc)[:500],
            duration_ms=duration_ms,
            **(usage_ctx or {}),
        )
        raise

    duration_ms = int((time.monotonic() - t0) * 1000)
    usage_id = record_usage(
        channel=channel,
        operation=task,
        model=cfg.model,
        response=response,
        duration_ms=duration_ms,
        **(usage_ctx or {}),
    )

    if _TRACE_ENABLED and usage_id:
        resp_content = None
        if response.choices:
            resp_content = getattr(response.choices[0].message, "content", None)
        save_trace(
            usage_id=usage_id,
            request_messages=messages,
            response_content=resp_content,
            duration_ms=duration_ms,
        )

    return response


# ---------------------------------------------------------------------------
# Sync convenience wrappers
# ---------------------------------------------------------------------------


def chat_text(
    messages: List[Dict[str, Any]],
    *,
    task: str,
    response_format: Optional[Dict[str, Any]] = None,
    max_completion_tokens: Optional[int] = None,
    extra_body: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> str:
    """Convenience wrapper that returns the assistant text directly."""
    response = chat_completion(
        messages,
        task=task,
        response_format=response_format,
        max_completion_tokens=max_completion_tokens,
        extra_body=extra_body,
        timeout=timeout,
        usage_ctx=usage_ctx,
    )
    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise RuntimeError(f"Empty response from LLM (task={task})")
    return content


def chat_json(
    messages: List[Dict[str, Any]],
    *,
    task: str,
    json_schema: Optional[Dict[str, Any]] = None,
    max_completion_tokens: Optional[int] = None,
    extra_body: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a chat completion that must return valid JSON.

    If a ``json_schema`` is given, it's enforced with
    ``response_format=json_schema``. Otherwise the call asks for
    ``response_format=json_object`` (looser, but works on more models).

    Returns the parsed dict. Raises if the model returns invalid JSON.
    """
    if json_schema is not None:
        response_format = {"type": "json_schema", "json_schema": json_schema}
    else:
        response_format = {"type": "json_object"}

    text = chat_text(
        messages,
        task=task,
        response_format=response_format,
        max_completion_tokens=max_completion_tokens,
        extra_body=extra_body,
        timeout=timeout,
        usage_ctx=usage_ctx,
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("[llm:%s] returned invalid JSON: %s; raw_tail=%r", task, exc, text[-500:])
        raise RuntimeError(f"LLM (task={task}) returned invalid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Async wrappers — offload to a worker thread so the event loop stays free
# ---------------------------------------------------------------------------


async def achat_completion(
    messages: List[Dict[str, Any]],
    *,
    task: str,
    response_format: Optional[Dict[str, Any]] = None,
    max_completion_tokens: Optional[int] = None,
    extra_body: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: chat_completion(
            messages,
            task=task,
            response_format=response_format,
            max_completion_tokens=max_completion_tokens,
            extra_body=extra_body,
            timeout=timeout,
            usage_ctx=usage_ctx,
        ),
    )


async def achat_text(
    messages: List[Dict[str, Any]],
    *,
    task: str,
    response_format: Optional[Dict[str, Any]] = None,
    max_completion_tokens: Optional[int] = None,
    extra_body: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: chat_text(
            messages,
            task=task,
            response_format=response_format,
            max_completion_tokens=max_completion_tokens,
            extra_body=extra_body,
            timeout=timeout,
            usage_ctx=usage_ctx,
        ),
    )


async def achat_json(
    messages: List[Dict[str, Any]],
    *,
    task: str,
    json_schema: Optional[Dict[str, Any]] = None,
    max_completion_tokens: Optional[int] = None,
    extra_body: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: chat_json(
            messages,
            task=task,
            json_schema=json_schema,
            max_completion_tokens=max_completion_tokens,
            extra_body=extra_body,
            timeout=timeout,
            usage_ctx=usage_ctx,
        ),
    )


# ---------------------------------------------------------------------------
# Error sanitization — strip provider/model info before showing to users
# ---------------------------------------------------------------------------

import re as _re

_PROVIDER_PATTERNS = _re.compile(
    r"(https?://\S+|"
    r"openrouter\.ai\S*|mistral\.ai\S*|openai\.com\S*|"
    r"api\.anthropic\.com\S*|generativelanguage\.googleapis\S*|"
    r"[a-z0-9_-]+/[a-z0-9._:-]+(?:-\d+)?)",  # model slugs like google/gemini-2.0-flash-001
    _re.IGNORECASE,
)

_GENERIC_LLM_ERROR = "Ошибка при обработке запроса AI. Попробуйте повторить позже."


def sanitize_llm_error(exc: Exception) -> str:
    """Return a user-safe error string with provider/model details stripped."""
    raw = str(exc)
    if _PROVIDER_PATTERNS.search(raw):
        return _GENERIC_LLM_ERROR
    return raw[:300]
