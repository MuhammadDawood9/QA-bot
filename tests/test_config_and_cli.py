import json
from pathlib import Path

import pytest

from src.config import check_settings, deep_merge, load_client, load_config, normalize_client_code
from src.main import main
from src.models import Issue

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_mistakes.srt"


@pytest.mark.parametrize("raw, expected", [
    ("2CS", "E2CS"),
    ("E2CS", "E2CS"),
    ("e8d", "E8D"),
    ("#E3AC", "E3AC"),
    (" 3ME ", "E3ME"),
    ("S_UP", "S_UP"),
    ("disc_Echo", "DISC_ECHO"),
    ("", None),
    (None, None),
])
def test_normalize_client_code(raw, expected):
    assert normalize_client_code(raw) == expected


def test_deep_merge_overrides_nested_values_only():
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2]}
    merged = deep_merge(base, {"nested": {"y": 3}, "list": [9]})
    assert merged == {"a": 1, "nested": {"x": 1, "y": 3}, "list": [9]}
    assert base["nested"]["y"] == 2


def test_client_override_reaches_check_settings(tmp_path):
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / "E9X.yaml").write_text(
        "client: E9X\noverrides:\n  too_short:\n    min_words: 3\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "paths:\n  clients_dir: clients\nchecks:\n  too_short:\n    min_words: 2\n    min_duration_sec: 0.5\n",
        encoding="utf-8")
    config = load_config(tmp_path / "config.yaml")
    client = load_client(config, "9X")
    assert check_settings(config, client, "too_short") == {"min_words": 3, "min_duration_sec": 0.5}


def test_unknown_client_warns_and_uses_defaults():
    config = load_config()
    with pytest.warns(UserWarning, match="No client file"):
        client = load_client(config, "E0ZZ")
    assert client == {"client": "E0ZZ", "overrides": {}}


def test_issue_validates_fields():
    with pytest.raises(ValueError):
        Issue(1, 0.0, "too_short", "urgent", "x", "msg")
    with pytest.raises(ValueError):
        Issue(1, 0.0, "too_short", "high", "x", "msg", confidence=1.5)


def test_cli_prints_issues(capsys):
    assert main(["--srt", str(FIXTURE), "--client", "E8D"]) == 0
    out = capsys.readouterr().out
    assert "client E8D | 20 captions" in out
    assert '"the"' in out and "too_short" in out


def test_cli_json(capsys):
    assert main(["--srt", str(FIXTURE), "--json"]) == 0
    issues = json.loads(capsys.readouterr().out)
    assert {"caption_id", "timestamp", "type", "severity", "original",
            "message", "suggestion", "confidence"} <= issues[0].keys()


def test_cli_missing_file(capsys):
    assert main(["--srt", "nope.srt"]) == 2
    assert "not found" in capsys.readouterr().err
