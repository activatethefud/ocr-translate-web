from __future__ import annotations

from ocrtran.estimate import analyze_document, estimate_image_tokens, predict_cost


def test_image_tokens_increase_with_dpi_and_cap():
    lo = estimate_image_tokens(612, 792, 72, 1800)
    hi = estimate_image_tokens(612, 792, 150, 1800)
    assert lo < hi
    capped = estimate_image_tokens(612, 792, 400, 1000)
    uncapped = estimate_image_tokens(612, 792, 400, 0)
    assert capped < uncapped
    assert estimate_image_tokens(0, 0, 150, 1800) > 0  # sensible default


def test_predict_scales_with_pages():
    a = predict_cost("deepseek-flash", 10)
    b = predict_cost("deepseek-flash", 20)
    assert b["est_cost_usd"] > a["est_cost_usd"]
    assert b["pages"] == 20


def test_predict_figure_and_verify_add_cost():
    base = predict_cost("deepseek-flash", 20, figure_mode="off", verify_math=False, figure_fraction=0.3)
    tight = predict_cost("deepseek-flash", 20, figure_mode="tight", figure_fraction=0.3)
    judge = predict_cost("deepseek-flash", 20, figure_mode="judge", figure_fraction=0.3)
    verify = predict_cost("deepseek-flash", 20, figure_mode="off", verify_math=True, formula_fraction=0.5)
    assert base["est_cost_usd"] < tight["est_cost_usd"] < judge["est_cost_usd"]
    assert verify["est_cost_usd"] > base["est_cost_usd"]


def test_predict_range_and_breakdown_consistency():
    r = predict_cost("deepseek-flash", 12)
    assert r["est_cost_low"] <= r["est_cost_usd"] <= r["est_cost_high"]
    assert abs(sum(r["breakdown"].values()) - r["est_cost_usd"]) < 0.01


def test_predict_rolling_glossary_and_chunks():
    plain = predict_cost("deepseek-flash", 100, chunk_size=25, rolling_glossary=False)
    rolling = predict_cost("deepseek-flash", 100, chunk_size=25, rolling_glossary=True)
    assert plain["breakdown"].get("rolling_glossary") is None
    assert rolling["breakdown"]["rolling_glossary"] > 0


def test_predict_target_script_changes_output_tokens():
    latin = predict_cost("deepseek-flash", 10, target_lang="English")
    cjk = predict_cost("deepseek-flash", 10, target_lang="Chinese (Simplified)")
    assert latin["est_completion_tokens"] != cjk["est_completion_tokens"]
    assert cjk["assumptions"]["target_script"] == "cjk"


def test_predict_glossary_and_instructions_add_prompt_tokens():
    base = predict_cost("deepseek-flash", 10)
    more = predict_cost("deepseek-flash", 10, glossary_terms=50, extra_chars=2000)
    assert more["est_prompt_tokens"] > base["est_prompt_tokens"]


def test_unknown_model_is_free():
    r = predict_cost("local-model", 10)
    assert r["est_cost_usd"] == 0.0


def test_analyze_document_text(tiny_pdf):
    sig = analyze_document(tiny_pdf)
    assert sig["n_pages"] == 2
    assert (round(sig["page_w"]), round(sig["page_h"])) == (300, 400)
    assert sig["avg_chars_per_page"] > 0
    assert 0.0 <= sig["formula_fraction"] <= 1.0
    assert 0.0 <= sig["figure_fraction"] <= 1.0
    assert sig["kind"] == "text"


def test_analyze_document_detects_figures(tmp_path):
    import fitz

    pdf = tmp_path / "fig.pdf"
    d = fitz.open()
    page = d.new_page(width=300, height=300)
    for i in range(40):  # lots of vector ops -> looks like a figure
        page.draw_line((10, 10 + i), (200, 10 + i))
    d.save(pdf)
    d.close()
    sig = analyze_document(pdf)
    assert sig["figure_fraction"] == 1.0


def test_estimate_has_eta():
    r = predict_cost("deepseek-flash", 10)
    assert r["est_seconds"] > 0
    faster = predict_cost("deepseek-flash", 10, concurrency=8)
    assert faster["est_seconds"] < r["est_seconds"]
