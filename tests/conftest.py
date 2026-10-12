import pytest


def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true",
                     help="also run slow tests that use ffmpeg and the real Whisper model")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip = pytest.mark.skip(reason="slow: needs ffmpeg and the Whisper model; run with --run-slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_real_api_calls(monkeypatch):
    """Tests must never call the real Anthropic API (it costs money), even on a machine where
    ANTHROPIC_API_KEY is set. Tests that exercise the API use a fake client instead."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_slow_video_passes(monkeypatch):
    """CLI tests give fake video files, so the slow passes over the picture (on-screen text OCR
    and color measurement) are stubbed out. Their own tests call them directly."""
    monkeypatch.setattr("src.engine.read_screen_text", lambda *a, **kw: [])
    monkeypatch.setattr("src.engine.read_color", lambda *a, **kw: [])
    monkeypatch.setattr("src.engine.read_video_once", lambda *a, **kw: [])
