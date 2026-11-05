"""Pinned Taostats detail pages never imply wallet-list coverage."""
import json

import pytest


def page(data, extra=""):
    flight = json.dumps(["$", "$La0", None, {"data": data}],
                        separators=(",", ":"))
    return extra + "<script>self.__next_f.push([1,%s])</script>" % (
        json.dumps(flight))


def record(success=True):
    return {"id": "9344821-0027", "block_number": 9344821, "index": 27,
            "full_name": "SubtensorModule.transfer_stake", "success": success,
            "signer_address": "walletA", "timestamp": "2026-10-23T07:44:00Z",
            "call_args": {"alpha_amount": 100000000000751,
                          "destination_coldkey": "walletB",
                          "origin_netuid": 23, "destination_netuid": 23}}


def event_page(eid):
    header = ("<div><p>StakeRemoved</p><div><p>#<!-- -->%s</p>"
              "<p>14 days ago</p><p>23 Oct 2026, 07:44:00 (UTC)</p>"
              "</div></div>" % eid)
    return page(["walletA", "hotkey", 700301006213, 100000000000751, 23, 0],
                header)


class Session:
    def __init__(self, pages):
        self.pages = pages
        self.visits = []

    def goto(self, url, check_google):
        self.visits.append(url)

    def html(self):
        return self.pages[self.visits[-1]]

    def pause(self):
        pass


def test_successful_transfer_parameters_and_separate_known_event():
    from seo.sources.tao_extrinsic import fetch_evidence
    base = "https://taostats.io"
    s = Session({base + "/extrinsic/9344821-0027": page(record()),
                 base + "/event/9344821-0198": event_page("9344821-0198")})
    row = fetch_evidence(s, "9344821-0027", (198,))
    assert row["status"] == "observed" and row["success"] is True
    assert row["call_args"]["alpha_amount"] == 100000000000751
    event, = row["requested_block_events"]
    assert event["status"] == "observed"
    assert event["stake"]["alpha"] == "100000.000000751"
    assert event["stake"]["tao"] == "700.301006213"
    assert event["event_id"] == "9344821-0198"


def test_failed_call_is_reported_with_its_on_chain_failure():
    from seo.sources.tao_extrinsic import fetch_evidence
    url = "https://taostats.io/extrinsic/9344821-0027"
    row = fetch_evidence(Session({url: page(record(False))}), "9344821-0027")
    assert row["status"] == "observed" and row["success"] is False


def test_wrong_record_does_not_confirm_requested_extrinsic():
    from seo.sources.tao_extrinsic import fetch_evidence
    url = "https://taostats.io/extrinsic/999-0001"
    s = Session({url: page(record())})
    row = fetch_evidence(s, "999-0001", (198,))
    assert row["status"] == "unavailable"
    assert "success" not in row and s.visits == [url]


def test_wrong_event_keeps_call_but_marks_evidence_partial():
    from seo.sources.tao_extrinsic import fetch_evidence
    url = "https://taostats.io/extrinsic/9344821-0027"
    s = Session({url: page(record()),
                 "https://taostats.io/event/9344821-0198":
                 event_page("9344821-0010")})
    row = fetch_evidence(s, "9344821-0027", (198,))
    assert row["status"] == "partial" and row["success"] is True
    assert row["requested_block_events"][0]["status"] == "unavailable"


def test_targets_are_bounded_numeric_ids():
    from seo.sources.tao_extrinsic import parse_target
    assert parse_target("9344821-27:198,199,198") == (
        "9344821-0027", (198, 199))
    assert parse_target("9333063-0013") == ("9333063-0013", ())
    for bad in ("https://other.test", "9-2:1/2", "9-2:", "9--2",
                "9-2:1,2,3,4,5,6,7,8,9"):
        with pytest.raises(ValueError):
            parse_target(bad)


def test_batch_move_requires_matching_signer_subnet_quantity_and_inert_calls():
    from seo.sources.tao_extrinsic import confirms_reallocation
    move = {"origin_netuid": 24, "destination_netuid": 24,
            "alpha_amount": 14011228945196}
    calls = [{"SubtensorModule": {"move_stake": move}},
             {"System": {"remark_with_event": {"remark": [1]}}}]
    proof = dict(call="Utility.batch_all", status="observed", success=True,
                 call_args={"calls": calls}, signer="holder")
    sell = dict(src="holder", netuid=24, alpha=14011.228945196)
    assert confirms_reallocation(proof, [sell])
    assert not confirms_reallocation(proof, [dict(sell, src="other")])
    assert not confirms_reallocation(proof, [dict(sell, netuid=23)])
    assert not confirms_reallocation(proof, [dict(sell, alpha=14012)])
    assert not confirms_reallocation(dict(proof, success=False), [sell])
    calls.append({"SubtensorModule": {"remove_stake": move}})
    assert not confirms_reallocation(proof, [sell])
