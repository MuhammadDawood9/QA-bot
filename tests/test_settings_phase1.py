"""Phase 1 of the floor rollout: the paid API check off by default, color thresholds in color.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.app.choices import CHECK_TOGGLES, visible_toggles
from src.config import color_settings, load_config
from src.engine import QCOptions, build_context, check_status_lines
from src.models import Issue
from src.video.color import FrameStats, Shot, color_issues

ROOT = Path(__file__).resolve().parent.parent


def test_punctuation_is_off_by_default_and_hidden_from_the_menu():
    config = load_config()
    assert config["checks"]["punctuation"]["enabled"] is False
    assert "punctuation" not in {k for k, _ in visible_toggles(config)}
    on = {**config, "checks": {**config["checks"], "punctuation": {"enabled": True}}}
    assert [k for k, _ in visible_toggles(on)] == [k for k, _ in CHECK_TOGGLES]


def test_report_status_lines_leave_out_the_disabled_api_check():
    config = load_config()
    lines = check_status_lines(build_context(config, {}), None, QCOptions())
    assert not any("Punctuation" in line or "Anthropic" in line for line in lines)
    assert any(line.startswith("Spelling") for line in lines)


def test_every_color_threshold_lives_in_color_yaml():
    data = yaml.safe_load((ROOT / "color.yaml").read_text(encoding="utf-8"))
    config = load_config()
    assert config["color"] == {**config["color"], **data["defaults"]}
    for key in ("clipped_white", "overexposed_high_factor", "cast_medium_factor", "flat_luma_range",
                "face_min_score", "skip"):
        assert key in data["defaults"]
    assert "color" not in yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def _config_with_color_file(tmp_path: Path, clients: dict) -> dict:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"paths": {"color_file": "color.yaml"}}), encoding="utf-8")
    (tmp_path / "color.yaml").write_text(yaml.safe_dump({"defaults": {"cast_strength": 6, "skip": []},
                                                         "clients": clients}), encoding="utf-8")
    return load_config(tmp_path / "config.yaml")


def test_per_client_color_overrides_and_skip_list(tmp_path):
    config = _config_with_color_file(tmp_path, {"3AC": {"skip": ["saturation"], "cast_strength": 9}})
    assert color_settings(config, {"client": "E3AC"}) == {"cast_strength": 9, "skip": ["saturation"]}
    assert color_settings(config, {"client": "E8D"}) == {"cast_strength": 6, "skip": []}
    # an older color: block in the client's own file still applies, last
    assert color_settings(config, {"client": "E3AC", "color": {"skip": []}})["skip"] == []


def test_skip_list_from_color_yaml_hides_that_type(tmp_path):
    config = _config_with_color_file(tmp_path, {"E3AC": {"skip": ["saturation"]}})
    grey = Shot(number=1, start=0.0, end=3.0, frames=[
        FrameStats(t=1.0, luma_median=120, black_level=10, white_level=230, clipped_white=0.0, clipped_black=0.0,
                   chroma=2.0, cast_a=0.0, cast_b=0.0)])          # a well-exposed but almost grey shot
    everyone = color_issues([grey], {**load_config()["color"]})
    assert [i.type for i in everyone] == ["saturation"] and isinstance(everyone[0], Issue)
    settings = {**load_config()["color"], **color_settings(config, {"client": "E3AC"})}
    assert color_issues([grey], settings) == []
