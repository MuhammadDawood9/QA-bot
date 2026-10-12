import csv
import datetime as dt
import shutil
from pathlib import Path

import pytest

from src.main import main
from src.models import Caption, Issue
from src.report import builder, screenshots
from src.report.builder import ReportInfo, build_report
from tests.helpers import CONFIG

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_VIDEO = ROOT / "data" / "samples" / "synthetic_sample.mp4"
WHEN = dt.datetime(2026, 10, 12, 9, 30, 0)

CAPTIONS = [
    Caption(1, 0.0, 2.0, "Welcome back."),
    Caption(2, 2.1, 4.1, "We need to to talk"),
    Caption(3, 4.2, 6.0, "Profit | loss *really*"),
    Caption(4, 6.1, 8.0, "Thanks."),
]
ISSUES = [
    Issue(2, 2.1, "repeated_word", "medium", "We need to to talk", 'Repeated word: "to to".',
          'Remove one: "We need to talk"', 0.8),
    Issue(2, 2.1, "extra_word", "high", "We need to to talk",
          'Extra word "to" (repeated): caption says "need to to", audio sounds like "need to".',
          'Audio: "We need to talk"', 1.0),
    Issue(3, 4.2, "spelling", "low", "Profit | loss *really*", "Odd | text", "", 0.5),
]


def settings(tmp_path, **kw):
    return {"output_dir": tmp_path / "reports", "formats": ["md"], "include_screenshots": True,
            "screenshot_at": "middle", "screenshot_width": 640, "write_csv": True, **kw}


def info(**kw):
    return ReportInfo(srt=Path("clip.srt"), client="E3JB", rules_version="2026-10-12",
                      checks=["Spelling: ran", "Punctuation (Anthropic API): skipped (no API key)"],
                      generated=WHEN, **kw)


def build(tmp_path, issues=ISSUES, video=None, ffmpeg=None, **kw):
    written = build_report(CAPTIONS, issues, info(video=video), settings(tmp_path, **kw), ffmpeg)
    report = written[0]
    return report, report.read_text(encoding="utf-8")


def test_report_folder_and_files(tmp_path):
    report, _ = build(tmp_path)
    assert report == tmp_path / "reports" / "clip_20261012-093000" / "report.md"
    assert (report.parent / "issues.csv").is_file()
    assert not (report.parent / "screenshots").exists()           # no video, no screenshots
    again, _ = build(tmp_path)
    assert again.parent.name == "clip_20261012-093000-2"          # never overwrites


def test_header_and_summary(tmp_path):
    _, text = build(tmp_path)
    assert text.startswith("# QC report: clip.srt\n")
    assert "- **Video:** none (caption text checks only)" in text
    assert "- **Captions:** clip.srt, 4 captions" in text
    assert "- **Client:** E3JB" in text and "- **Generated:** 2026-10-12 09:30" in text
    assert "- **Rules version:** 2026-10-12" in text
    assert "  - Punctuation (Anthropic API): skipped (no API key)" in text
    assert "**3 issues on 2 of 4 captions.**" in text
    assert "| Spelling |  |  | 1 | 1 |" in text
    assert "| Extra word (audio) | 1 |  |  | 1 |" in text
    assert "| **All** | **1** | **1** | **1** | **3** |" in text


def test_issues_on_one_caption_share_one_section(tmp_path):
    _, text = build(tmp_path)
    assert text.count("### 00:02.100, caption #2") == 1
    section = text.split("### 00:02.100, caption #2")[1].split("###")[0]
    rows = [line for line in section.splitlines() if line.startswith("| ") and "Problem" not in line]
    assert [r.split(" | ")[1] for r in rows] == ["high", "medium"]       # worst first
    assert "| [00:02.100](#caption-2) | #2 We need to to talk | Extra word (audio), Repeated word | high |" in text


def test_markdown_special_characters_are_escaped(tmp_path):
    _, text = build(tmp_path)
    assert "> Profit \\| loss \\*really\\*" in text
    assert "Odd \\| text" in text


def test_appendix_marks_flagged_captions(tmp_path):
    _, text = build(tmp_path)
    appendix = text.split("## Appendix: all captions")[1]
    assert "| 00:00.000 | 1 | Welcome back. | |" in appendix
    assert "| 00:02.100 | [2](#caption-2) | **We need to to talk** | Extra word (audio), Repeated word |" in appendix


def test_no_issues(tmp_path):
    _, text = build(tmp_path, issues=[])
    assert "No issues found." in text and "## Details" not in text


def test_csv_for_excel_and_clickup(tmp_path):
    report, _ = build(tmp_path)
    raw = (report.parent / "issues.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                         # Excel reads it as UTF-8
    with (report.parent / "issues.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["type"] for r in rows] == ["extra_word", "repeated_word", "spelling"]
    assert rows[0]["timestamp"] == "00:02.100" and rows[0]["seconds"] == "2.100"
    assert rows[2]["caption"] == "Profit | loss *really*"            # raw text, not Markdown


def test_csv_can_be_switched_off(tmp_path):
    report, _ = build(tmp_path, write_csv=False)
    assert not (report.parent / "issues.csv").exists()


@pytest.fixture
def fake_frames(monkeypatch):
    taken = []

    def fake_grab(ffmpeg, video, seconds, out, width=640):
        taken.append(round(seconds, 3))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return seconds < 5                                         # the frame for caption 3 "fails"
    monkeypatch.setattr(builder, "grab_frame", fake_grab)
    return taken


def test_screenshots_at_the_middle_of_each_flagged_caption(tmp_path, fake_frames):
    report, text = build(tmp_path, video=Path("clip.mp4"), ffmpeg="ffmpeg")
    assert fake_frames == [3.1, 5.1]
    assert "![Frame at 00:03.100](screenshots/00003100_caption2.jpg)" in text
    assert "*(no screenshot: ffmpeg could not get a frame at this time)*" in text
    assert text.startswith("# QC report: clip.mp4")


def test_screenshots_at_caption_start(tmp_path, fake_frames):
    build(tmp_path, video=Path("clip.mp4"), ffmpeg="ffmpeg", screenshot_at="start")
    assert fake_frames == [2.1, 4.2]


def test_screenshots_can_be_switched_off(tmp_path, fake_frames):
    _, text = build(tmp_path, video=Path("clip.mp4"), ffmpeg="ffmpeg", include_screenshots=False)
    assert fake_frames == [] and "![" not in text


def test_grab_frame_uses_an_argument_list(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        from PIL import Image
        Image.new("RGB", (8, 8)).save(tmp_path / "f.jpg", "JPEG")
        return type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(screenshots.subprocess, "run", fake_run)
    assert screenshots.grab_frame("ffmpeg", Path("my clip.mp4"), 3.1, tmp_path / "f.jpg", 480)
    cmd = seen["cmd"]
    assert cmd[cmd.index("-ss") + 1] == "3.100" and cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-i") + 1] == "my clip.mp4" and "scale=480:-2" in cmd


def test_grab_frame_failure_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(screenshots.subprocess, "run",
                        lambda cmd, **kw: type("R", (), {"returncode": 1, "stderr": "seek past end"})())
    assert screenshots.grab_frame("ffmpeg", Path("v.mp4"), 999, tmp_path / "f.jpg") is False


def test_cli_report_flag(monkeypatch, tmp_path, capsys):
    config = {**CONFIG, "report": {**CONFIG["report"], "output_dir": str(tmp_path / "out")}}
    monkeypatch.setattr("src.engine.load_config", lambda path=None: config)
    assert main(["--srt", str(FIXTURES / "sample_mistakes.srt"), "--client", "E8D", "--report"]) == 0
    err = capsys.readouterr().err
    [folder] = (tmp_path / "out").iterdir()
    text = (folder / "report.md").read_text(encoding="utf-8")
    docx = next(folder.glob('*.docx'))
    assert docx.name.startswith('sample_mistakes_QC_') and docx.name.endswith('.docx')
    assert f"report: {folder / 'report.md'}" in err and f"report: {docx}" in err
    assert "# QC report: sample_mistakes.srt" in text and "- **Client:** E8D" in text
    assert "  - Captions vs audio (Whisper): not run (no video)" in text
    assert "  - Punctuation (Anthropic API): skipped (no API key)" in text


@pytest.mark.slow
def test_real_screenshot_from_the_sample_video(tmp_path):
    if not SAMPLE_VIDEO.exists() or not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg and data/samples/synthetic_sample.mp4 (python scripts/make_sample_video.py)")
    out = tmp_path / "frame.jpg"
    assert screenshots.grab_frame(shutil.which("ffmpeg"), SAMPLE_VIDEO, 6.9, out)
    assert out.read_bytes()[:2] == b"\xff\xd8"                    # a JPEG
