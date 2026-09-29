from __future__ import annotations

import pytest

from app import db, learning


@pytest.fixture
def env(tmp_path):
    db.init_db(f"sqlite:///{tmp_path / 'learn.db'}")
    s = db.get_session()
    try:
        s.add(db.Document(id="d", filename="d.pdf", sha256="x", n_pages=1))
        s.commit()
    finally:
        s.close()
    learning.reset_cache()
    return tmp_path


def _usage(job_id: str, model: str, prompt: int, completion: int, calls: int) -> None:
    s = db.get_session()
    try:
        s.add(db.Job(id=job_id, document_id="d", status="done", model=model))
        s.add(
            db.Usage(
                job_id=job_id, model=model, prompt_tokens=prompt, completion_tokens=completion, calls=calls
            )
        )
        s.commit()
    finally:
        s.close()


def test_model_stats_aggregate(env):
    _usage("j1", "deepseek-flash", 100, 300, 2)
    _usage("j2", "deepseek-flash", 300, 500, 2)  # totals: 400/800 over 4 calls
    stats = learning.model_stats(refresh=True)
    assert stats["deepseek-flash"]["calls"] == 4
    assert stats["deepseek-flash"]["input_tokens_per_call"] == 100
    assert stats["deepseek-flash"]["output_tokens_per_call"] == 200


def test_model_stats_empty(env):
    assert learning.model_stats(refresh=True) == {}
    assert learning.learned_for("nope") is None


def test_learned_blend_shifts_estimate():
    from ocrtran.estimate import predict_cost

    base = predict_cost("deepseek-flash", 10)
    learned = {
        "calls": 500,  # plenty of data -> learned dominates
        "input_tokens_per_call": base["assumptions"]["input_tokens_per_call"],
        "output_tokens_per_call": base["assumptions"]["output_tokens_per_page"] * 3,
    }
    tuned = predict_cost("deepseek-flash", 10, learned=learned)
    assert tuned["est_cost_usd"] > base["est_cost_usd"]
    assert tuned["assumptions"]["learned_calls"] == 500


def test_learned_smoothing_ignores_tiny_samples():
    from ocrtran.estimate import predict_cost

    base = predict_cost("deepseek-flash", 10)
    tiny = predict_cost(
        "deepseek-flash",
        10,
        learned={
            "calls": 1,
            "input_tokens_per_call": base["assumptions"]["input_tokens_per_call"],
            "output_tokens_per_call": base["assumptions"]["output_tokens_per_page"] * 2,
        },
    )
    # one sample (2x) barely moves the estimate thanks to smoothing
    assert tiny["est_cost_usd"] < base["est_cost_usd"] * 1.2
