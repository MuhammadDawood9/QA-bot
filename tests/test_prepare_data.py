import importlib.util
from pathlib import Path

import pytest
import yaml

from src.utils import system

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "data" / "QC_Review_Analysis_Jun-Oct_2026.xlsx"

_spec = importlib.util.spec_from_file_location("prepare_data", ROOT / "scripts" / "prepare_data.py")
prepare_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prepare_data)


def test_client_overrides_survive_regeneration(tmp_path):
    rows = [{"client_code": "8D", "project (from task name)": "Danja", "client-specific rule": "old"}]
    prepare_data.build_client_files(rows, tmp_path)
    path = tmp_path / "E8D.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["overrides"] = {"too_short": {"min_words": 3}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    rows[0]["client-specific rule"] = "new"
    prepare_data.build_client_files(rows, tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data == {"client": "E8D", "project": "Danja", "notes": "new",
                    "overrides": {"too_short": {"min_words": 3}}}


def test_examples_get_client_from_task_name_and_seconds():
    rows = [{"task_name": "#2CS Episode 316", "client": "", "video_timestamp": "00:00:14:32",
             "category": "too_short_caption"}]
    [example], problems = prepare_data.build_examples(rows, 30)
    assert example["client"] == "E2CS"
    assert example["start_sec"] == "14.533"      # frame 32 means a 60 fps timeline
    assert problems == []


def test_unreadable_timestamp_is_reported_not_fatal():
    rows = [{"task_name": "#E3HW x", "client": "E3HW", "video_timestamp": "35-36"}]
    [example], problems = prepare_data.build_examples(rows, 30)
    assert example["start_sec"] == "" and len(problems) == 1


def test_exceptions_parse_client_and_timestamp():
    rows = [{"client": "E2CS", "what you accepted": '13:28 "voices overlapped" (flag withdrawn)'},
            {"client": "(comment)", "what you accepted": '"all good now"'}]
    data = yaml.safe_load(prepare_data.build_exceptions(rows, "x.xlsx"))
    assert data["exceptions"] == [
        {"client": "E2CS", "timestamp": "13:28", "note": '"voices overlapped" (flag withdrawn)'},
        {"client": None, "note": "all good now"},
    ]


def test_rule_examples_are_split_per_client():
    rows = [{"area": "Commas", "rule": "r",
             "example": 'E3RK: comma after "recreational"; E3JB: comma before "slurred"'}]
    data = yaml.safe_load(prepare_data.build_rules(rows, "x.xlsx"))
    assert data["rules"][0]["id"] == "commas_1"
    assert data["rules"][0]["examples"] == ['E3RK: comma after "recreational"',
                                           'E3JB: comma before "slurred"']


@pytest.mark.skipif(not XLSX.exists(), reason="spreadsheet not in data/")
def test_full_run_on_real_spreadsheet(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "paths:\n  rules_file: rules.yaml\n  clients_dir: clients\n  glossary_file: glossary.txt\n"
        "  examples_file: examples.csv\n  exceptions_file: exceptions.yaml\ntimecode:\n  fps: 30\n",
        encoding="utf-8")
    assert prepare_data.main(["--xlsx", str(XLSX), "--config", str(config)]) == 0
    rules = yaml.safe_load((tmp_path / "rules.yaml").read_text(encoding="utf-8"))
    assert len(rules["rules"]) == 17
    assert len(list((tmp_path / "clients").glob("*.yaml"))) == 10
    lines = (tmp_path / "examples.csv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 60                        # header + 59 examples
    assert (tmp_path / "glossary.txt").read_text(encoding="utf-8").startswith("# glossary.txt")


def test_missing_ffmpeg_explains_install_on_both_systems(monkeypatch):
    monkeypatch.setattr(system.shutil, "which", lambda name: None)
    with pytest.raises(system.MissingToolError) as err:
        system.find_ffmpeg()
    assert "winget install ffmpeg" in str(err.value)
    assert "brew install ffmpeg" in str(err.value)
