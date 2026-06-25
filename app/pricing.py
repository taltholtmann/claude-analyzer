"""
Rough cost estimation from token usage.

Prices are USD per 1M tokens and are approximate — they drift, so treat the
result as an estimate, not a bill. Cache reads bill at ~0.1x input; 5-minute
cache writes at ~1.25x input (Anthropic prompt-caching economics).
"""
from __future__ import annotations

# input / output USD per 1M tokens, keyed by a substring of the model id.
_PRICES = [
    ("claude-fable", 10.0, 50.0),
    ("claude-opus", 5.0, 25.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku", 1.0, 5.0),
]
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def _rate(models: list[str]) -> tuple[float, float] | None:
    """Return (input_per_mtok, output_per_mtok) for the first known model, or None."""
    for m in models:
        if not isinstance(m, str):
            continue
        for key, inp, out in _PRICES:
            if key in m:
                return inp, out
    return None


def estimate_cost(tokens: dict, models: list[str]) -> dict | None:
    """
    Estimate USD cost from a token-usage dict (input/output/cache_read/cache_write).
    Returns None when no model price is known (e.g. Codex/OpenAI models).
    """
    rate = _rate(models or [])
    if rate is None:
        return None
    inp, out = rate
    usd = (
        tokens.get("input", 0) / 1e6 * inp
        + tokens.get("cache_read", 0) / 1e6 * inp * CACHE_READ_FACTOR
        + tokens.get("cache_write", 0) / 1e6 * inp * CACHE_WRITE_FACTOR
        + tokens.get("output", 0) / 1e6 * out
    )
    return {"usd": round(usd, 4), "priced_model": next(m for m in models if _rate([m])),
            "note": "approximate — cache read 0.1x / write 1.25x input"}
