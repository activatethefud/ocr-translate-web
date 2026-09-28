"""Token usage -> USD, plus pre-run cost estimates.

Prices are USD per 1M tokens (input, output) and are deliberately simple /
configurable; unknown models cost 0 so nothing breaks.
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "deepseek-flash": (0.28, 1.10),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gemini-1.5-flash": (0.075, 0.30),
    "claude-3-5-sonnet": (3.00, 15.00),
}

# rough per-page token assumptions for estimating before a run
EST_INPUT_TOKENS = 1500
EST_OUTPUT_TOKENS = 1000
EST_BBOX_TOKENS = 200
EST_VERIFY_OUTPUT_TOKENS = 300


def price_for(model: str) -> tuple[float, float]:
    for key in sorted(PRICES, key=len, reverse=True):
        if model.startswith(key):
            return PRICES[key]
    return (0.0, 0.0)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = price_for(model)
    return prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout


def estimate_for_pages(
    model: str, pages: int, *, verify_math: bool = False, with_figures: bool = True
) -> dict:
    pin, pout = price_for(model)
    prompt = pages * EST_INPUT_TOKENS
    completion = pages * EST_OUTPUT_TOKENS
    if with_figures:
        prompt += pages * EST_BBOX_TOKENS
    if verify_math:
        prompt += pages * EST_INPUT_TOKENS
        completion += pages * EST_VERIFY_OUTPUT_TOKENS
    total = prompt / 1e6 * pin + completion / 1e6 * pout
    return {
        "model": model,
        "pages": pages,
        "est_calls": pages * (2 if verify_math else 1),
        "est_prompt_tokens": prompt,
        "est_completion_tokens": completion,
        "est_cost_usd": round(total, 4),
    }
