import os

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: hits the real Gemini Live API (RUN_INTEGRATION=1 to enable)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration test — set RUN_INTEGRATION=1 to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
