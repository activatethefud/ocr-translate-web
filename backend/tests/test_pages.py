from __future__ import annotations

import pytest

from ocrtran.pages import PageSpecError, parse_page_spec


def test_all():
    assert parse_page_spec("all", 5) == [1, 2, 3, 4, 5]
    assert parse_page_spec("", 3) == [1, 2, 3]
    assert parse_page_spec(None, 2) == [1, 2]


def test_single_and_list():
    assert parse_page_spec("3", 5) == [3]
    assert parse_page_spec("2,4,1", 5) == [1, 2, 4]


def test_ranges():
    assert parse_page_spec("1-3", 5) == [1, 2, 3]
    assert parse_page_spec("2,4,7-9", 10) == [2, 4, 7, 8, 9]
    assert parse_page_spec("3-", 5) == [3, 4, 5]
    assert parse_page_spec("-2", 5) == [1, 2]


def test_out_of_range_clamped():
    assert parse_page_spec("1-99", 3) == [1, 2, 3]


def test_invalid():
    with pytest.raises(PageSpecError):
        parse_page_spec("abc", 5)
    with pytest.raises(PageSpecError):
        parse_page_spec("9", 3)  # selects nothing


def test_dedup():
    assert parse_page_spec("1,1,2,2,1", 5) == [1, 2]


def test_whitespace_tolerated():
    assert parse_page_spec(" 1 , 2 - 4 ", 5) == [1, 2, 3, 4]


def test_reversed_range_sorted():
    assert parse_page_spec("4-2", 5) == [2, 3, 4]


def test_bare_dash_is_all():
    assert parse_page_spec("-", 3) == [1, 2, 3]


def test_star_is_all():
    assert parse_page_spec("*", 3) == [1, 2, 3]


def test_zero_selects_nothing():
    with pytest.raises(PageSpecError):
        parse_page_spec("0", 3)


def test_bad_range():
    with pytest.raises(PageSpecError):
        parse_page_spec("a-b", 3)
