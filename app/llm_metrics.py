import os
from typing import Any

from prometheus_client import Counter, Gauge


METRICS_SERVICE_NAME = os.getenv("METRICS_SERVICE_NAME", "backend")

LLM_REQUESTS_TOTAL = Counter(
    "llm_requests_total",
    "Total number of LLM requests.",
    ["service", "provider", "model", "operation"],
)
LLM_TOKENS_INPUT_TOTAL = Counter(
    "llm_tokens_input_total",
    "Total number of input (prompt) tokens used by LLM calls.",
    ["service", "provider", "model", "operation"],
)
LLM_TOKENS_OUTPUT_TOTAL = Counter(
    "llm_tokens_output_total",
    "Total number of output (completion) tokens used by LLM calls.",
    ["service", "provider", "model", "operation"],
)
LLM_TOKENS_REASONING_TOTAL = Counter(
    "llm_tokens_reasoning_total",
    "Total number of reasoning tokens used by LLM calls.",
    ["service", "provider", "model", "operation"],
)
LLM_METRICS_READY = Gauge(
    "llm_metrics_ready",
    "Whether LLM metrics instrumentation is initialized.",
    ["service"],
)

_DEFAULT_LABELS = {
    "service": METRICS_SERVICE_NAME,
    "provider": "unknown",
    "model": "unknown",
    "operation": "unknown",
}

LLM_REQUESTS_TOTAL.labels(**_DEFAULT_LABELS).inc(0)
LLM_TOKENS_INPUT_TOTAL.labels(**_DEFAULT_LABELS).inc(0)
LLM_TOKENS_OUTPUT_TOTAL.labels(**_DEFAULT_LABELS).inc(0)
LLM_TOKENS_REASONING_TOTAL.labels(**_DEFAULT_LABELS).inc(0)
LLM_METRICS_READY.labels(service=METRICS_SERVICE_NAME).set(1)


def _to_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _usage_value(obj: Any, attr: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def record_llm_usage(response: Any, provider: str, model: str, operation: str) -> None:
    usage = _usage_value(response, "usage")
    prompt_tokens = _to_int(_usage_value(usage, "prompt_tokens"))
    completion_tokens = _to_int(_usage_value(usage, "completion_tokens"))

    completion_details = _usage_value(usage, "completion_tokens_details")
    reasoning_tokens = _to_int(_usage_value(completion_details, "reasoning_tokens"))

    labels = {
        "service": METRICS_SERVICE_NAME,
        "provider": provider or "unknown",
        "model": model or "unknown",
        "operation": operation or "unknown",
    }
    LLM_REQUESTS_TOTAL.labels(**labels).inc()
    LLM_TOKENS_INPUT_TOTAL.labels(**labels).inc(prompt_tokens)
    LLM_TOKENS_OUTPUT_TOTAL.labels(**labels).inc(completion_tokens)
    LLM_TOKENS_REASONING_TOTAL.labels(**labels).inc(reasoning_tokens)
