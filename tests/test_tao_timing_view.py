"""Exercise the standalone wallet viewer's timing controls."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_offline_timing_controls():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the viewer JavaScript")
    script = Path(__file__).with_name("wallet_timing_ui.cjs")
    subprocess.run([node, str(script)], check=True, timeout=30)
