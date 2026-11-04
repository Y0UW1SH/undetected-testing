import os

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixture():
    def load(name):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            return fh.read()
    return load
