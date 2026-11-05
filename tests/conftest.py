import os
import sys

import pytest

from seo import cli, geo
from tests.fakes import RecordingSession

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixture():
    def load(name):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            return fh.read()
    return load


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    """CLI runs with a fake browser, fake geocoder, no sleeps, temp cwd."""
    RecordingSession.created = []
    monkeypatch.setattr(cli, "Session", RecordingSession)
    for module in ("seo.browser", "seo.local.maps", "seo.local.grid"):
        monkeypatch.setattr(sys.modules[module].time, "sleep",
                            lambda s: None)
    calls = []

    def fake_geocode(place, opener=None):
        calls.append(place)
        if "paris" in place.lower():
            return (48.85, 2.35, "Paris, France",
                    "Paris,Ile-de-France,France")
        return (30.27, -97.74, "Austin, Texas, US",
                "Austin,Texas,United States")

    monkeypatch.setattr(geo, "geocode", fake_geocode)
    monkeypatch.chdir(tmp_path)
    return {"db": str(tmp_path / "t.sqlite"), "geocode_calls": calls}
