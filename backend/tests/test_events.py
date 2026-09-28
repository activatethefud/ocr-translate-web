from __future__ import annotations

import pytest

from ocrtran.events import Canceled, CancelToken, Event, emit


def test_cancel_token_lifecycle():
    token = CancelToken()
    assert token.canceled is False
    token.check()  # no raise
    token.cancel()
    assert token.canceled is True
    with pytest.raises(Canceled):
        token.check()


def test_event_defaults():
    e = Event("ocr", "ok")
    assert e.stage == "ocr" and e.status == "ok"
    assert e.page is None and e.index == 0 and e.total == 0
    assert e.data == {}


def test_emit_without_callback_is_noop():
    emit(None, Event("ocr", "ok"))  # must not raise


def test_emit_forwards_event():
    seen = []
    emit(seen.append, Event("build", "ok", page=2))
    assert seen[0].page == 2
