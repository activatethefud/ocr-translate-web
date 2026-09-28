from __future__ import annotations

import pytest

from ocrtran import pricing


def test_cost_known_model():
    c = pricing.cost_usd("deepseek-flash", 1_000_000, 1_000_000)
    assert c == pytest.approx(0.28 + 1.10, rel=1e-6)


def test_cost_unknown_model_is_zero():
    assert pricing.cost_usd("some-local-model", 10**6, 10**6) == 0.0


def test_price_prefix_match():
    assert pricing.price_for("gpt-4o-mini-2024") == pricing.PRICES["gpt-4o-mini"]


def test_estimate_scales_with_pages():
    a = pricing.estimate_for_pages("deepseek-flash", 5)
    b = pricing.estimate_for_pages("deepseek-flash", 10)
    assert b["est_cost_usd"] == pytest.approx(2 * a["est_cost_usd"], rel=1e-3)
    assert a["est_calls"] == 5


def test_estimate_verify_math_costs_more():
    a = pricing.estimate_for_pages("deepseek-flash", 10)
    b = pricing.estimate_for_pages("deepseek-flash", 10, verify_math=True)
    assert b["est_cost_usd"] > a["est_cost_usd"]
