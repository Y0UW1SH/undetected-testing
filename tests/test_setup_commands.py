"""doctor / init / report: the first-run experience, offline."""
import json

import pytest

from seo import doctor, models, report, storage
from tests.fakes import RecordingSession, run


@pytest.fixture(autouse=True)
def deps_present(monkeypatch):
    """Doctor results must not depend on the machine running the tests."""
    monkeypatch.setattr(doctor, "_seleniumbase_version", lambda: "4.57.0")
    monkeypatch.setattr(doctor, "_bs4_available", lambda: True)
    monkeypatch.setattr(doctor, "_tkinter_available", lambda: True)


def test_doctor_checks_without_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_chrome_path", lambda: "/usr/bin/chrome")
    results = doctor.checks(str(tmp_path / "missing.json"),
                            str(tmp_path / "data"))
    names = [r["name"] for r in results]
    assert names[:3] == ["Python", "SeleniumBase", "BeautifulSoup"]
    by = {r["name"]: r for r in results}
    assert by["Python"]["status"] == doctor.PASS
    assert by["SeleniumBase"]["detail"] == "4.57.0"
    assert by["Chrome"]["status"] == doctor.PASS
    assert by["Data directory"]["status"] == doctor.PASS
    assert by["Config"]["status"] == doctor.WARN
    assert "seo init" in by["Config"]["fix"]
    text = doctor.render(results)
    assert "Python" in text and "->" in text        # a fix line is shown
    monkeypatch.setattr(doctor, "_seleniumbase_version", lambda: None)
    results = doctor.checks(str(tmp_path / "x.json"), str(tmp_path / "d"))
    sb = [r for r in results if r["name"] == "SeleniumBase"][0]
    assert sb["status"] == doctor.FAIL and "pip install" in sb["fix"]


def test_doctor_flags_missing_tkinter_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor, "_tkinter_available", lambda: False)
    results = doctor.checks(str(tmp_path / "x.json"), str(tmp_path / "d"))
    tk = [r for r in results if r["name"] == "tkinter"][0]
    assert tk["status"] == doctor.FAIL and "python3-tk" in tk["fix"]


def test_doctor_browser_check_uses_session(monkeypatch):
    class OkSession(RecordingSession):
        def title(self):
            return "Web Testing Page"

    res = doctor.browser_check(OkSession, headless=True)
    assert res["status"] == doctor.PASS
    assert OkSession.created[-1].headless is True

    class BadSession(RecordingSession):
        def open(self):
            raise RuntimeError("no chrome")

    res = doctor.browser_check(BadSession)
    assert res["status"] == doctor.FAIL and "no chrome" in res["detail"]


def test_doctor_command_exit_codes(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_chrome_path", lambda: "/usr/bin/chrome")
    assert run(fake_env, "doctor") == 0
    out = capsys.readouterr().out
    assert "All required checks passed" in out
    monkeypatch.setattr(doctor, "_chrome_path", lambda: None)
    assert run(fake_env, "doctor") == 1
    assert "FAIL  Chrome" in capsys.readouterr().out


def test_init_copies_example_or_builds_from_args(fake_env, capsys):
    assert run(fake_env, "init", "--config", "cfg.json") == 0
    with open("cfg.json") as fh:
        cfg = json.load(fh)
    assert isinstance(cfg, list) and cfg[0]["location"] == "Austin, TX"
    assert run(fake_env, "init", "--config", "cfg.json") == 1  # exists
    assert run(fake_env, "init", "--config", "cfg.json", "--force",
               "--location", "Boise, ID", "--serp", "plumber",
               "--serp", "drain cleaning", "--local", "plumber",
               "--target", "Joe's Plumbing", "--grid-size", "3") == 0
    with open("cfg.json") as fh:
        cfg = json.load(fh)
    assert cfg == [{
        "location": "Boise, ID", "device": "desktop",
        "serp": ["plumber", "drain cleaning"], "local": ["plumber"],
        "grid": [{"query": "plumber", "target": "Joe's Plumbing",
                  "size": 3, "spacing_km": 1.0}]}]
    # the generated config runs end to end through batch
    assert run(fake_env, "batch", "--config", "cfg.json") == 0
    assert "Next: `seo report`" in capsys.readouterr().out


def test_batch_without_config_explains(fake_env, capsys):
    assert run(fake_env, "batch", "--config", "nope.json") == 2
    assert "seo init" in capsys.readouterr().err


def test_report_renders_everything(fake_env, capsys):
    store = storage.Storage(fake_env["db"])
    html = report.render(store)
    assert "No data yet" in html
    store.close()
    run(fake_env, "serp", "emergency plumber", "--location", "Austin, TX")
    run(fake_env, "local", "plumber", "--lat", "30.27", "--lng", "-97.74")
    run(fake_env, "grid", "plumber", "--target", "Joe's Plumbing",
        "--lat", "30.27", "--lng", "-97.74", "--size", "3",
        "--out", "data/grid_joes_plumbing.html")
    store = storage.Storage(fake_env["db"])
    store.save([models.NapRecord("Joe's", "Yelp", "https://yelp.example",
                                 "Joe's", "5125550100", None, True, False)])
    store.save([models.CanaryRun("q", "ok"), models.CanaryRun("q2",
                                                              "challenge")])
    store.close()
    assert run(fake_env, "report", "--out", "data/report.html") == 0
    html = open("data/report.html", encoding="utf-8").read()
    assert "<title>SEO report</title>" in html
    assert "Google web search" in html and "joesplumbing.example" in html
    assert "Local pack" in html and "Austin Drain Pros" in html
    assert "Google Maps listings" in html and "+15125550100" not in html
    assert "Local rank grids" in html and "heatmap" in html
    assert "3x3 grid, found in 9/9 cells" in html
    assert "NAP consistency" in html and ">yes<" in html and ">no<" in html
    assert "Canary" in html and "1 challenged (50%)" in html
    assert "Report written to" in capsys.readouterr().out
