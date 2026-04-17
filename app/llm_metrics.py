from typing import Any

from prometheus_client import Counter

LLM_TOKENS_INPUT_TOTAL = Counter(
    "llm_tokens_input_total",
    "Total number of input (prompt) tokens used by LLM calls.",
)
LLM_TOKENS_COMPLETION_TOTAL = Counter(
    "llm_tokens_completion_total",
    "Total number of completion tokens used by LLM calls.",
)
LLM_TOKENS_REASONING_TOTAL = Counter(
    "llm_tokens_reasoning_total",
    "Total number of reasoning tokens used by LLM calls.",
)


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
    _ = (provider, model, operation)
    usage = _usage_value(response, "usage")
    prompt_tokens = _to_int(_usage_value(usage, "prompt_tokens"))
    completion_tokens = _to_int(_usage_value(usage, "completion_tokens"))

    completion_details = _usage_value(usage, "completion_tokens_details")
    reasoning_tokens = _to_int(_usage_value(completion_details, "reasoning_tokens"))

    LLM_TOKENS_INPUT_TOTAL.inc(prompt_tokens)
    LLM_TOKENS_COMPLETION_TOTAL.inc(completion_tokens)
    LLM_TOKENS_REASONING_TOTAL.inc(reasoning_tokens)
