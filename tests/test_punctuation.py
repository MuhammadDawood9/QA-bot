"""Punctuation check tests. The Anthropic API is always faked here: no network, no cost."""

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from src.checks import punctuation
from src.checks.context import CheckContext
from src.config import load_yaml
from src.inputs.srt_loader import load_srt
from src.main import main, run_checks
from src.models import PUNCTUATION, Caption
from src.utils.glossary import Glossary
from src.utils.llm import FALLBACK_BETA, ClaudeClient, LLMError, MissingApiKeyError, Usage
from tests.helpers import CONFIG, captions, context as make_context

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SETTINGS = {**CONFIG["checks"]["punctuation"], "enabled": True}

# What a careful reviewer would answer for tests/fixtures/punctuation_mistakes.srt.
# Each correction is a real comment from data/examples.csv.
ANSWERS = {
    "and there it was.": ("and there it was", "No full stop mid-thought.", 0.9),               # E3RK 0:03
    "it was pretty bad. That": ("it was pretty bad that", "No full stop or capital mid-thought.", 0.85),  # E3RK 0:25
    "was when it started": ("was when it started.", "Maybe a full stop.", 0.4),                # low confidence
    "it was recreational then it got worse": ("it was recreational, then it got worse", "Comma after recreational.", 0.7),  # E3RK 0:11
    "his speech was slow slurred and heavy": ("his speech was slow, slurred and heavy", "Comma before slurred.", 0.75),  # E3JB 01:55:09
    "all that weight on his shoulders back": ("all that weight on his shoulder's back", "Possessive apostrophe.", 0.65),  # E2CS 00:00:35:12
    "I'm bringing sexy, back": ("I'm bringing sexy back", "No comma between verb and object.", 0.95),  # E8D 0:13
    "Why does that matter.": ("Why does that matter?", "It is a question.", 0.9),                # E3ME 00:01:03:11
    "that's where we come from": ("that's where we come from.", "Full stop at the end of the sentence.", 0.8),  # E8D 0:27
}
EXPECTED = {1: "and there it was", 3: "it was pretty bad that", 5: "it was recreational, then it got worse",
            6: "his speech was slow, slurred and heavy", 7: "all that weight on his shoulder's back",
            8: "I'm bringing sexy back", 9: "Why does that matter?", 10: "that's where we come from."}

_TARGET = re.compile(r"^>> #(\d+) \[[^\]]+\] (.*?)   <- TARGET$", re.MULTILINE)


def answer_key(system, user, schema):
    """Fake model: answer every TARGET in the request from ANSWERS."""
    reviews = []
    for target_id, text in _TARGET.findall(user):
        fix, why, conf = ANSWERS.get(text, (text, "", 0.95))
        reviews.append({"target_id": int(target_id), "has_issue": fix != text, "issue_type": "punctuation",
                        "original": text, "suggestion": fix, "explanation": why, "confidence": conf})
    return json.dumps({"reviews": reviews})


class FakeLLM:
    model = "fake-model"
    effort = "low"

    def __init__(self, responder=answer_key):
        self.responder = responder
        self.calls = []
        self.usage = Usage()

    def complete_json(self, system, user, schema):
        self.calls.append({"system": system, "user": user, "schema": schema})
        self.usage.requests += 1
        result = self.responder(system, user, schema)
        if isinstance(result, Exception):
            raise result
        return result

    def targets(self, call_index):
        return [int(t) for t, _ in _TARGET.findall(self.calls[call_index]["user"])]


def ctx(tmp_path, llm=None, **kw):
    return CheckContext(llm=llm, llm_cache_file=tmp_path / "cache.json", **kw)


def run(caps, tmp_path, llm, settings=None, **kw):
    context = ctx(tmp_path, llm, **kw)
    return punctuation.check(caps, {**SETTINGS, **(settings or {})}, context), context


# --- windows and prompt ---------------------------------------------------------------

def test_windows_are_target_plus_two_before_and_two_after():
    caps = load_srt(FIXTURES / "sample_mistakes.srt")
    windows = {w.target.id: w for w in punctuation.build_windows(caps)}
    assert [c.id for c in windows[1].before] == [] and [c.id for c in windows[1].after] == [2, 3]
    assert [c.id for c in windows[10].before] == [8, 9] and [c.id for c in windows[10].after] == [11, 12]
    assert [c.id for c in windows[20].after] == []
    rendered = windows[4].render(1)
    assert rendered.splitlines() == [
        "WINDOW 1: review ONLY caption #4",
        "   #2 [00:02.450 - 00:04.000] and number one is,",
        "   #3 [00:04.050 - 00:05.600] i should be careful here",
        ">> #4 [00:05.650 - 00:05.780] the   <- TARGET",
        "   #5 [00:05.830 - 00:07.600] market moves on its own.",
        "   #6 [00:07.650 - 00:08.250] same",
    ]


def test_window_size_comes_from_settings():
    caps = captions(*[f"caption number {i}" for i in range(1, 8)])
    [w] = [w for w in punctuation.build_windows(caps, before=1, after=3) if w.target.id == 4]
    assert [c.id for c in w.before] == [3] and [c.id for c in w.after] == [5, 6, 7]


def test_symbol_only_captions_are_not_sent():
    caps = captions("Here we go.", "♪", "Next part now.")
    assert [w.target.id for w in punctuation.build_windows(caps)] == [1, 3]


def test_system_prompt_has_rules_client_rules_examples_and_glossary():
    rules = load_yaml(ROOT / "rules.yaml")["rules"]
    examples = [
        {"client": "E3RK", "category": "punctuation", "editor_wrote": "", "should_be": "",
         "comment_text": 'no full stop after "there it was"'},
        {"client": "E2CS", "category": "punctuation", "editor_wrote": "shoulders", "should_be": "shoulder's",
         "comment_text": 'it should be "shoulder\'s" not "shoulders"'},
        {"client": "E8D", "category": "punctuation", "editor_wrote": "realM", "should_be": "",
         "comment_text": '"realM" m should be small'},                       # capitals: filtered out
        {"client": "E2CI", "category": "spelling", "editor_wrote": "hourse", "should_be": "hours",
         "comment_text": "spelling mistake"},                                  # not punctuation: filtered out
    ]
    context = CheckContext(glossary=Glossary.from_lines(["=Mark McGwire", "Danja"]), rules=rules,
                           examples=examples, client={"client": "E3RK", "notes": "Flowing sentences: fewer full stops."})
    system = punctuation.build_system_prompt(context, SETTINGS)

    assert system.startswith("You are a QC reviewer for video captions.")
    assert "- No full stop mid-thought when the sentence continues; no capital after it." in system
    assert "- Do not put a comma after every phrase/line." in system
    assert '"I" is always capital' not in system                                # only Full stops / Commas
    assert "- E3RK: Flowing sentences: fewer full stops." in system
    assert '- [E3RK] reviewer: "no full stop after "there it was""' in system
    assert '- [E2CS] "shoulders" -> "shoulder\'s" reviewer:' in system
    assert "realM" not in system and "hourse" not in system
    assert "GLOSSARY (never flag these terms): Mark McGwire, Danja" in system
    assert "Respond with ONLY JSON" in system


def test_prompt_is_identical_across_requests_so_it_can_be_cached(tmp_path):
    llm = FakeLLM()
    run(captions(*[f"line {i} of the talk" for i in range(25)]), tmp_path, llm, {"windows_per_request": 10})
    assert len(llm.calls) == 3
    assert len({c["system"] for c in llm.calls}) == 1


# --- end to end with a fake model -----------------------------------------------------

def test_real_punctuation_mistakes_are_reported(tmp_path):
    caps = load_srt(FIXTURES / "punctuation_mistakes.srt")
    issues, _ = run(caps, tmp_path, FakeLLM())
    found = {i.caption_id: i for i in issues}
    assert set(found) == set(EXPECTED) | {4}                      # 4 is the low-confidence answer
    for cap_id, fix in EXPECTED.items():
        assert found[cap_id].suggestion == fix
        assert found[cap_id].type == PUNCTUATION
    assert found[8].severity == "medium" and found[7].severity == "low"
    assert found[8].message == "No comma between verb and object."


def test_min_confidence_from_config_drops_unsure_answers(tmp_path):
    caps = load_srt(FIXTURES / "punctuation_mistakes.srt")
    context = make_context()
    context.llm = FakeLLM()
    context.llm_cache_file = tmp_path / "cache.json"
    for name in ("too_short", "spelling", "capitalization", "spacing"):
        context.settings[name] = {**context.settings[name], "enabled": False}
    issues = run_checks(caps, CONFIG, {}, context)
    assert {i.caption_id for i in issues} == set(EXPECTED)        # 0.4 < min_confidence 0.6


def test_unchanged_suggestion_is_not_an_issue(tmp_path):
    def says_issue_but_no_change(system, user, schema):
        return json.dumps({"reviews": [{"target_id": int(t), "has_issue": True, "issue_type": "punctuation",
                                        "original": x, "suggestion": f"  {x} ", "explanation": "?",
                                        "confidence": 0.9} for t, x in _TARGET.findall(user)]})
    issues, _ = run(captions("one two three"), tmp_path, FakeLLM(says_issue_but_no_change))
    assert issues == []


# --- batching and caching -------------------------------------------------------------

def test_windows_are_batched_per_request(tmp_path):
    caps = captions(*[f"line {i} of the talk" for i in range(1, 21)])
    llm = FakeLLM()
    run(caps, tmp_path / "a", llm, {"windows_per_request": 10})
    assert [llm.targets(i) for i in range(len(llm.calls))] == [list(range(1, 11)), list(range(11, 21))]
    llm = FakeLLM()
    run(caps, tmp_path / "b", llm, {"windows_per_request": 7})
    assert len(llm.calls) == 3


def test_rerun_is_answered_from_cache(tmp_path):
    caps = load_srt(FIXTURES / "punctuation_mistakes.srt")
    first, _ = run(caps, tmp_path, FakeLLM())
    llm = FakeLLM()
    second, context = run(caps, tmp_path, llm)
    assert llm.calls == [] and second == first
    assert context.stats["punctuation"]["cached"] == 11


def test_changing_one_caption_only_resends_the_windows_that_contain_it(tmp_path):
    caps = load_srt(FIXTURES / "punctuation_mistakes.srt")
    run(caps, tmp_path, FakeLLM())
    edited = [Caption(c.id, c.start, c.end, "it was recreational, then it got worse") if c.id == 5 else c
              for c in caps]
    llm = FakeLLM()
    issues, _ = run(edited, tmp_path, llm)
    assert llm.targets(0) == [3, 4, 5, 6, 7]                         # caption 5 and its neighbours' windows
    assert 5 not in {i.caption_id for i in issues}


def test_cache_depends_on_model_and_prompt(tmp_path):
    caps = captions("one two three")
    run(caps, tmp_path, FakeLLM())
    other_model = FakeLLM()
    other_model.model = "another-model"
    run(caps, tmp_path, other_model)
    assert len(other_model.calls) == 1
    other_client = FakeLLM()
    run(caps, tmp_path, other_client, client={"client": "E8D", "notes": "different rules"})
    assert len(other_client.calls) == 1


# --- failures are logged and skipped --------------------------------------------------

def test_unreadable_reply_is_skipped_logged_and_retried_next_run(tmp_path, caplog):
    caps = captions(*[f"line {i} of the talk" for i in range(1, 5)])
    replies = iter(["this is not JSON", None])
    def flaky(system, user, schema):
        reply = next(replies)
        return reply if reply is not None else answer_key(system, user, schema)
    llm = FakeLLM(flaky)
    with caplog.at_level(logging.WARNING, logger="qcbot.punctuation"):
        issues, context = run(caps, tmp_path, llm, {"windows_per_request": 2})
    assert issues == [] and len(llm.calls) == 2
    assert "captions #1-#2 skipped" in caplog.text
    assert context.stats["punctuation"]["failed"] == 2
    retry = FakeLLM()
    run(caps, tmp_path, retry, {"windows_per_request": 2})
    assert retry.targets(0) == [1, 2]                              # failures were not cached


@pytest.mark.parametrize("error", [LLMError("the model declined the request (category: cyber)"),
                                   LLMError("the reply was cut off at max_tokens")])
def test_refusal_or_cut_off_reply_is_skipped(tmp_path, caplog, error):
    with caplog.at_level(logging.WARNING, logger="qcbot.punctuation"):
        issues, _ = run(captions("one two three"), tmp_path, FakeLLM(lambda *a: error))
    assert issues == [] and str(error) in caplog.text


def test_api_errors_skip_the_batch_but_auth_errors_stop(tmp_path, caplog):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.InternalServerError("overloaded", response=httpx2.Response(529, request=request), body=None)
    replies = iter([overloaded, None])
    llm = FakeLLM(lambda s, u, sc: next(replies) or answer_key(s, u, sc))
    caps = load_srt(FIXTURES / "punctuation_mistakes.srt")
    issues, _ = run(caps, tmp_path / "a", llm, {"windows_per_request": 6})
    assert len(llm.calls) == 2 and {i.caption_id for i in issues} == {7, 8, 9, 10}

    bad_key = anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=request), body=None)
    llm = FakeLLM(lambda *a: bad_key)
    with caplog.at_level(logging.WARNING, logger="qcbot.punctuation"):
        issues, context = run(caps, tmp_path / "b", llm, {"windows_per_request": 2})
    assert len(llm.calls) == 1 and issues == []
    assert "punctuation check stopped" in caplog.text
    assert context.stats["punctuation"]["failed"] == 11


def test_parse_reviews_is_defensive(caplog):
    good = {"target_id": 1, "has_issue": True, "issue_type": "punctuation", "original": "a",
            "suggestion": "a.", "explanation": "x", "confidence": 1.7}
    fenced = "```json\n" + json.dumps({"reviews": [good]}) + "\n```"
    assert punctuation.parse_reviews(fenced, {1})[1]["confidence"] == 1.0
    with caplog.at_level(logging.WARNING, logger="qcbot.punctuation"):
        out = punctuation.parse_reviews(json.dumps({"reviews": [
            {**good, "target_id": 9},                 # not asked about
            {**good, "has_issue": "yes"},             # wrong type
            {"suggestion": "no id"},                  # missing fields
        ]}), {1})
    assert out == {} and caplog.text.count("skipped") == 3
    for bad in ["not json", "[]", '{"other": 1}']:
        with pytest.raises(ValueError):
            punctuation.parse_reviews(bad, {1})


def test_missing_api_key_skips_with_instructions(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="qcbot.punctuation"):
        issues, context = run(captions("one two three"), tmp_path, llm=None)
    assert issues == []
    assert "ANTHROPIC_API_KEY is not set" in caplog.text
    assert "brew" not in caplog.text and "setx ANTHROPIC_API_KEY" in caplog.text and "~/.zshrc" in caplog.text
    assert context.stats["punctuation"]["skipped"] == "no API key"


def test_disabled():
    assert punctuation.check(captions("a b c"), {**SETTINGS, "enabled": False}, CheckContext(llm=FakeLLM())) == []


# --- the API client (with a fake Anthropic SDK client) ---------------------------------

def fake_sdk(stop_reason="end_turn", text='{"reviews": []}', category=None):
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(
            stop_reason=stop_reason,
            stop_details=SimpleNamespace(category=category) if category else None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                                  cache_creation_input_tokens=1000, cache_read_input_tokens=0),
        )
    sdk = SimpleNamespace(messages=SimpleNamespace(create=create),
                          beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    return sdk, sent


def test_request_shape():
    sdk, sent = fake_sdk()
    client = ClaudeClient.from_settings(SETTINGS)
    client.client = sdk
    assert client.complete_json("SYSTEM", "USER", punctuation.REVIEW_SCHEMA) == '{"reviews": []}'
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert sent["messages"] == [{"role": "user", "content": "USER"}]
    assert sent["output_config"] == {"effort": "low",
                                     "format": {"type": "json_schema", "schema": punctuation.REVIEW_SCHEMA}}
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    assert "thinking" not in sent and "temperature" not in sent
    assert client.usage.requests == 1 and client.usage.cache_write_tokens == 1000


def test_fallbacks_can_be_switched_off():
    sdk, sent = fake_sdk()
    client = ClaudeClient.from_settings({**SETTINGS, "fallbacks": None})
    client.client = sdk
    client.complete_json("S", "U", {})
    assert "betas" not in sent and "fallbacks" not in sent


@pytest.mark.parametrize("stop_reason, category, message", [
    ("refusal", "cyber", "declined the request (category: cyber)"),
    ("max_tokens", None, "cut off at max_tokens"),
])
def test_refusal_and_truncation_raise(stop_reason, category, message):
    sdk, _ = fake_sdk(stop_reason=stop_reason, category=category)
    client = ClaudeClient(model="m", client=sdk)
    with pytest.raises(LLMError, match=re.escape(message)):
        client.complete_json("S", "U", {})


def test_api_key_comes_only_from_the_environment(monkeypatch):
    with pytest.raises(MissingApiKeyError):
        ClaudeClient(model="m")._api()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    api = ClaudeClient(model="m", max_retries=5)._api()          # builds the SDK client; sends nothing
    assert api.api_key == "sk-ant-test-not-a-real-key" and api.max_retries == 5


def test_cost_estimate():
    usage = Usage(requests=2, input_tokens=1_000_000, output_tokens=100_000,
                  cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert usage.cost(SETTINGS["price_per_mtok"]) == pytest.approx(2.0 + 1.0 + 2.5 + 0.2)


# --- command line ---------------------------------------------------------------------

def test_cli_without_key_still_runs_other_checks(capsys):
    assert main(["--srt", str(FIXTURES / "sample_mistakes.srt")]) == 0
    captured = capsys.readouterr()
    assert "too_short" in captured.out
    assert "skipped: no API key" in captured.err


def test_cli_no_llm_flag(capsys):
    assert main(["--srt", str(FIXTURES / "sample_mistakes.srt"), "--no-llm"]) == 0
    assert "punctuation" not in capsys.readouterr().err
