from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from people_intel.deepseek_fallback import (
    DeepSeekDiffReviewer,
    DeepSeekPolicyError,
    DeepSeekResponseError,
    DeepSeekSettings,
)
from people_intel.light_tracker import ChangeDecision, FetchObservation, LightTracker


def key_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "deepseek-api-key"
    path.write_text("sk-synthetic-test-key-1234567890\n", encoding="utf-8")
    path.chmod(mode)
    return path


def settings(tmp_path: Path, **updates) -> DeepSeekSettings:
    values = {
        "enabled": True,
        "api_key_file": key_file(tmp_path),
        "audit_database": tmp_path / "audit.sqlite3",
        "max_calls_per_source_per_day": 1,
        "max_calls_per_person_per_week": 3,
        "max_calls_per_day": 10,
        "max_total_tokens_per_day": 10_000,
    }
    values.update(updates)
    return DeepSeekSettings(**values)


def ambiguous() -> ChangeDecision:
    return ChangeDecision(
        "ambiguous",
        0.4,
        "unresolved",
        modifications=[
            {
                "category": "content",
                "before": "I build robust learning systems.",
                "after": "I build reliable learning systems.",
                "cookie": "must-not-leave",
            }
        ],
    )


def context(**updates):
    value = {
        "person_key": "synthetic-person",
        "source_id": "synthetic-source",
        "source_kind": "homepage",
        "retrieval_mode": "direct",
        "health_status": "healthy",
        "identity_gate_passed": True,
        "quality_score": 0.95,
        "has_confirmed_baseline": True,
    }
    value.update(updates)
    return value


def response(decision: str = "meaningful_change") -> dict:
    body = {
        "decision": decision,
        "summary_zh": "公开简介中的稳定措辞发生编辑。",
        "confidence": 0.71,
        "needs_human_review": decision == "uncertain",
        "change_types": ["profile_edit"],
        "evidence_ids": ["E001"] if decision == "meaningful_change" else [],
    }
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": json.dumps(body, ensure_ascii=False)},
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "prompt_cache_hit_tokens": 30,
            "prompt_cache_miss_tokens": 90,
            "completion_tokens": 40,
            "total_tokens": 160,
        },
    }


def test_configuration_rejects_insecure_key_file(tmp_path):
    config = settings(tmp_path)
    config.api_key_file.chmod(0o644)
    assert any("0600" in item for item in config.validate_public())
    with pytest.raises(DeepSeekPolicyError):
        DeepSeekDiffReviewer(config, transport=lambda *_: response())


def test_configuration_rejects_raw_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-raw-environment-value-123456789")
    config = settings(tmp_path)
    assert any("raw DEEPSEEK_API_KEY" in item for item in config.validate_public())


def test_mode_0600_local_config_is_discovered_without_inline_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("PEOPLE_INTEL_DEEPSEEK_ENABLED", raising=False)
    key = key_file(tmp_path)
    config_path = tmp_path / "deepseek.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "api_key_file": str(key),
                "model": "deepseek-v4-flash",
                "audit_database": str(tmp_path / "audit.sqlite3"),
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    config = DeepSeekSettings.from_runtime(config_path)
    assert config.enabled is True
    assert config.model == "deepseek-v4-flash"
    assert config.configuration_source == "config_file"
    assert config.public_status()["use_cases"] == [
        "ambiguous_review",
        "confirmed_summary",
    ]


def test_local_config_rejects_inline_api_key(tmp_path):
    config_path = tmp_path / "deepseek.json"
    config_path.write_text(
        json.dumps({"enabled": True, "api_key": "sk-must-not-be-inline-123456"}),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    with pytest.raises(ValueError, match="inline secrets"):
        DeepSeekSettings.from_config_file(config_path)


@pytest.mark.parametrize(
    ("decision", "overrides"),
    [
        (ChangeDecision("changed", 1, "done"), {}),
        (ambiguous(), {"health_status": "authwall"}),
        (ambiguous(), {"identity_gate_passed": False}),
        (ambiguous(), {"quality_score": 0.5}),
        (ambiguous(), {"source_kind": "github"}),
        (ambiguous(), {"retrieval_mode": "search_index"}),
        (ambiguous(), {"has_confirmed_baseline": False}),
    ],
)
def test_eligibility_gate_blocks_deterministic_or_untrusted_cases(
    decision, overrides, tmp_path
):
    calls = {"count": 0}

    def transport(*_):
        calls["count"] += 1
        return response()

    reviewer = DeepSeekDiffReviewer(settings(tmp_path), transport=transport)
    allowed, _ = DeepSeekDiffReviewer.eligibility(
        decision, context(**overrides)
    )
    assert not allowed
    with pytest.raises(DeepSeekPolicyError, match="ineligible"):
        reviewer.review(decision, context=context(**overrides))
    assert calls["count"] == 0
    with sqlite3.connect(tmp_path / "audit.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM deepseek_audit").fetchone()[0] == 0


def test_valid_call_is_minimized_validated_audited_and_advisory(tmp_path):
    captured = {}

    def transport(payload, headers, timeout):
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout"] = timeout
        return response()

    reviewer = DeepSeekDiffReviewer(settings(tmp_path), transport=transport)
    decision = reviewer.review(ambiguous(), context=context())
    assert decision.status == "changed"
    assert "仅建议" in decision.summary
    assert decision.reviewer.endswith(":advisory")
    serialized = json.dumps(captured["payload"], ensure_ascii=False)
    assert "must-not-leave" not in serialized
    assert "synthetic-person" not in serialized
    assert "sk-synthetic-test-key" not in serialized
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert "user_id" not in captured["payload"]
    assert "user" not in captured["payload"]
    assert captured["headers"]["Authorization"].endswith(
        "sk-synthetic-test-key-1234567890"
    )

    raw_audit = (tmp_path / "audit.sqlite3").read_bytes()
    assert b"sk-synthetic-test-key" not in raw_audit
    assert b"I build robust" not in raw_audit
    with sqlite3.connect(tmp_path / "audit.sqlite3") as connection:
        row = connection.execute(
            """SELECT status,decision,total_tokens,request_hash,response_hash
               FROM deepseek_audit"""
        ).fetchone()
    assert row[:3] == ("completed", "meaningful_change", 160)
    assert len(row[3]) == len(row[4]) == 64


def test_confirmed_change_can_use_deepseek_for_summary_without_redecision(tmp_path):
    captured = {}

    def transport(payload, *_):
        captured["payload"] = payload
        return response()

    reviewer = DeepSeekDiffReviewer(settings(tmp_path), transport=transport)
    decision = ChangeDecision(
        "changed",
        0.91,
        "deterministically confirmed",
        additions=[
            {
                "category": "publication",
                "text": "New Paper · ICML 2026",
                "stable_id": "paper:123",
            }
        ],
        confirmation_count=2,
        confirmations_required=2,
    )
    result = reviewer.summarize_confirmed(
        decision,
        context=context(change_confirmed=True),
    )
    assert result.status == "changed"
    assert result.score == 0.91
    assert result.confirmation_count == result.confirmations_required == 2
    assert result.summary.startswith("已确认变化摘要：")
    assert result.reviewer.endswith(":confirmed-summary")
    system = captured["payload"]["messages"][0]["content"]
    assert "不得重新裁决" in system
    with sqlite3.connect(tmp_path / "audit.sqlite3") as connection:
        purpose = connection.execute(
            "SELECT purpose FROM deepseek_audit"
        ).fetchone()[0]
    assert purpose == "confirmed_summary"
    usage = reviewer.usage_snapshot()
    assert usage["confirmed_summary_calls_last_24h"] == 1
    assert usage["ambiguous_review_calls_last_24h"] == 0
    assert usage["prompt_cache_hit_tokens_last_24h"] == 30
    assert usage["prompt_cache_miss_tokens_last_24h"] == 90


def test_summary_gate_requires_deterministic_confirmation(tmp_path):
    reviewer = DeepSeekDiffReviewer(settings(tmp_path), transport=lambda *_: response())
    decision = ChangeDecision(
        "changed",
        0.8,
        "candidate only",
        additions=[{"category": "position", "text": "New role"}],
    )
    with pytest.raises(DeepSeekPolicyError, match="ineligible summary"):
        reviewer.summarize_confirmed(
            decision,
            context=context(change_confirmed=False),
        )


def test_bad_json_mode_output_fails_closed_and_audits_only_error_type(tmp_path):
    invalid = {
        "choices": [{"finish_reason": "stop", "message": {"content": ""}}],
        "usage": {},
    }
    reviewer = DeepSeekDiffReviewer(
        settings(tmp_path), transport=lambda *_: invalid
    )
    with pytest.raises(DeepSeekResponseError):
        reviewer.review(ambiguous(), context=context())
    with sqlite3.connect(tmp_path / "audit.sqlite3") as connection:
        row = connection.execute(
            "SELECT status,decision,error_type FROM deepseek_audit"
        ).fetchone()
    assert row == ("failed", "uncertain", "DeepSeekResponseError")


def test_missing_usage_fails_closed_instead_of_underreporting_cost(tmp_path):
    invalid = response()
    invalid["usage"] = {}
    reviewer = DeepSeekDiffReviewer(
        settings(tmp_path), transport=lambda *_: invalid
    )
    with pytest.raises(DeepSeekResponseError, match="usage"):
        reviewer.review(ambiguous(), context=context())
    with sqlite3.connect(tmp_path / "audit.sqlite3") as connection:
        row = connection.execute(
            "SELECT status,total_tokens,error_type FROM deepseek_audit"
        ).fetchone()
    assert row == ("failed", 0, "DeepSeekResponseError")


def test_per_source_budget_prevents_second_same_day_call(tmp_path):
    reviewer = DeepSeekDiffReviewer(
        settings(tmp_path), transport=lambda *_: response("noise")
    )
    first = reviewer.review(ambiguous(), context=context())
    assert first.status == "noise"
    with pytest.raises(DeepSeekPolicyError, match="per-source"):
        reviewer.review(ambiguous(), context=context())


def test_reserved_token_ceiling_blocks_concurrent_budget_overshoot(tmp_path):
    config = settings(
        tmp_path,
        max_calls_per_source_per_day=10,
        max_calls_per_person_per_week=10,
        max_total_tokens_per_day=1_000,
    )
    first = DeepSeekDiffReviewer(config, transport=lambda *_: response())
    second = DeepSeekDiffReviewer(config, transport=lambda *_: response())
    first._reserve("pi_person_1", "pi_source_1", "test", token_ceiling=600)
    with pytest.raises(DeepSeekPolicyError, match="daily token"):
        second._reserve(
            "pi_person_2",
            "pi_source_2",
            "test",
            token_ceiling=600,
        )


def test_light_tracker_keeps_model_suggestion_as_unconfirmed_candidate(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    try:
        source = tracker.add_person(
            "Synthetic Person",
            urls=["https://synthetic.invalid/profile"],
        )["sources"][0]
        first = tracker.start_run("baseline")
        baseline = (
            "<h1>Synthetic Person</h1><main>"
            "<p>I build robust learning systems for science.</p></main>"
        )
        assert tracker.observe(
            first, source["source_id"], FetchObservation(body=baseline)
        ).status == "baseline"
        tracker.complete_run(first)
        confirmed_before = tracker.db.execute(
            "SELECT semantic_hash FROM sources WHERE source_id=?",
            (source["source_id"],),
        ).fetchone()["semantic_hash"]

        reviewer = DeepSeekDiffReviewer(
            settings(tmp_path), transport=lambda *_: response()
        )
        second = tracker.start_run("changed")
        edited = baseline.replace("robust", "reliable")
        decision = tracker.observe(
            second,
            source["source_id"],
            FetchObservation(body=edited),
            reviewer=reviewer,
        )
        tracker.complete_run(second)
        confirmed_after = tracker.db.execute(
            "SELECT semantic_hash FROM sources WHERE source_id=?",
            (source["source_id"],),
        ).fetchone()["semantic_hash"]
        assert decision.status == "candidate"
        assert decision.confirmation_count == 1
        assert decision.confirmations_required == 2
        assert confirmed_before == confirmed_after
    finally:
        tracker.close()
