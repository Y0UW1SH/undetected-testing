"""Test sampling positive subnet positions from the Holders tab."""

A, B, C, D, E = ["5" + ch * 47 for ch in "ABCDE"]


def row(address, rank, balance="1000", netuid=23):
    return {"netuid": netuid, "subnet_rank": rank,
            "coldkey": {"ss58": address}, "hotkey": {"ss58": "5" + "Z" * 47},
            "balance": balance}


def test_sample_keeps_positive_coldkeys_and_spreads_observed_ranks():
    from seo.sources.tao_holders import fetch_holders
    rows = [row(D, 4), row(B, 2), row(E, 5), row(A, 1), row(C, 3),
            row(A, 6), row("5" + "F" * 47, 7, "0"), row("...", 9),
            row("5" + "G" * 47, 8, netuid=99), None]
    tab = Session({1: state(1, 1, rows)})
    assert fetch_holders(tab, 23, limit=3) == ([A, C, E], "partial")
    assert fetch_holders(tab, 23, limit=1) == ([C], "partial")


class Session:
    def __init__(self, states):
        self.sb = self
        self.states = states
        self.page = 1

    def goto(self, url, check_google=False):
        self.page = 1

    def evaluate(self, js):
        from seo.sources import tao_holders as source
        if js == source.SELECT_HOLDERS:
            return True
        if "const target =" in js:
            self.page = int(js.split("const target = ", 1)[1].split(";")[0])
            return True
        return self.states[self.page]

    def pause(self):
        pass


def state(number, total, records):
    return {"status": "success", "data": {"data": records, "pagination": {
        "current_page": number, "total_pages": total, "per_page": 10,
        "total_items": total * 10}}}


def test_two_pages_include_lower_ranked_holders():
    from seo.sources.tao_holders import fetch_holders
    s = Session({1: state(1, 17, [row(A, 1)]),
                 13: state(13, 17, [row(E, 125, "100")])})
    assert fetch_holders(s, 23, limit=6, pages=2) == ([A, E], "partial")


def test_failed_holder_query_does_not_claim_empty_list():
    from seo.sources.tao_holders import fetch_holders
    assert fetch_holders(Session({1: {"status": "error"}}), 23) == (
        [], "unavailable")
    empty = {"status": "success", "data": {"data": [], "pagination": {
        "current_page": 1, "total_pages": 0, "per_page": 10,
        "total_items": 0}}}
    assert fetch_holders(Session({1: empty}), 23) == ([], "complete")


def test_malformed_positive_holder_cannot_claim_complete_coverage():
    from seo.sources.tao_holders import fetch_holders
    response = state(1, 1, [row(A, 1), row("invalid", 2)])
    response["data"]["pagination"]["total_items"] = 2
    assert fetch_holders(Session({1: response}), 23) == ([A], "partial")


def test_jump_field_uses_visible_numeric_page_input():
    from seo.sources.tao_holders import fetch_holders

    class JumpSession(Session):
        target = None

        def evaluate(self, js):
            if "const target =" in js:
                self.target = int(js.split("const target = ", 1)[1]
                                  .split(";")[0])
                return "jump"
            if "HTMLInputElement.prototype" in js:
                return True
            if "new KeyboardEvent" in js:
                self.page = self.target
                return True
            return super().evaluate(js)

    tab = JumpSession({1: state(1, 17, [row(A, 1)]),
                       13: state(13, 17, [row(E, 125)])})
    assert fetch_holders(tab, 23, limit=6, pages=2) == ([A, E], "partial")


def test_pending_visible_verification_retried_once_before_reading(monkeypatch):
    from seo.sources import tao_holders as h

    class Verified(Session):
        clicks = 0

        def click_with_offset(self, selector, x, y, scroll):
            assert (selector, x, y, scroll) == ("#cf-turnstile", 20, 32, False)
            self.clicks += 1

        def evaluate(self, js):
            if js == h.SELECT_HOLDERS or self.clicks:
                return super().evaluate(js)
            return {"status": "pending", "diagnostic": {
                "query_found": True, "grid_present": True,
                "control_visible": True, "widget_completed": False}}

    monkeypatch.setattr(h.time, "sleep", lambda _: None)
    tab = Verified({1: state(1, 1, [row(A, 1)])})
    assert h.fetch_holders(tab, 23, limit=2) == ([A], "partial")
    assert tab.clicks == 1


def test_unhydrated_first_click_is_retried_when_grid_never_activated(
        monkeypatch):
    from seo.sources import tao_holders as h

    class Hydration(Session):
        clicks = 0

        def evaluate(self, js):
            if js == h.SELECT_HOLDERS:
                self.clicks += 1
                return True
            if self.clicks == 1:
                return {"diagnostic": {"grid_present": False,
                                       "query_found": False}}
            return super().evaluate(js)

    monkeypatch.setattr(h.time, "sleep", lambda _: None)
    tab = Hydration({1: state(1, 1, [row(A, 1)])})
    assert h.fetch_holders(tab, 23, limit=1) == ([A], "partial")
    assert tab.clicks == 2


def test_persistent_verification_has_diagnostic_not_empty_success(
        monkeypatch, capsys):
    from seo.sources import tao_holders as h

    class Stalled(Session):
        clicks = 0

        def click_with_offset(self, *args, **kwargs):
            self.clicks += 1

    monkeypatch.setattr(h.time, "sleep", lambda _: None)
    tab = Stalled({1: {"status": "pending", "diagnostic": {
        "query_found": True, "grid_present": True,
        "widget_present": True, "control_visible": True,
        "widget_completed": False, "cells": 0}}})
    assert h.fetch_holders(tab, 23) == ([], "unavailable")
    assert tab.clicks == 1
    assert "verification" in capsys.readouterr().out
