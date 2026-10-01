from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from completion.models import CompletionSourceKind, CompletionStatus
from provider_backends.claude.execution_runtime.hook_results import (
    capture_exact_hook_cancel_evidence,
    poll_exact_hook,
)
from provider_backends.claude.execution_runtime.hook_results_runtime import (
    load_strict_exact_hook_evidence,
)
from provider_execution.base import ProviderSubmission


def _submission() -> ProviderSubmission:
    return ProviderSubmission(
        job_id="job_1",
        agent_name="agent1",
        provider="claude",
        accepted_at="2026-04-06T00:00:00Z",
        ready_at="2026-04-06T00:00:00Z",
        source_kind=CompletionSourceKind.SESSION_EVENT_LOG,
        reply="",
        runtime_state={
            "completion_dir": "/tmp/completion",
            "next_seq": 7,
        },
    )


def _strict_submission() -> ProviderSubmission:
    return replace(
        _submission(),
        diagnostics={"workspace_path": "C:/work/demo"},
        runtime_state={
            "completion_dir": "/tmp/completion",
            "request_anchor": "job_1",
            "next_seq": 7,
            "prompt_sent": True,
            "session_path": r"C:\Users\demo\.claude\projects\session-1.jsonl",
        },
    )


def _submission_with_completion_dir(
    tmp_path: Path,
    *,
    accepted_at: str = "2026-04-06T00:00:00Z",
) -> ProviderSubmission:
    submission = _strict_submission()
    return replace(
        submission,
        accepted_at=accepted_at,
        runtime_state={
            **submission.runtime_state,
            "completion_dir": str(tmp_path / "completion"),
        },
    )


def _write_reserved_event(
    tmp_path: Path,
    *,
    session_id: str = "session-1",
    reply: str = "reply kept from an unanchored turn",
) -> str:
    from provider_hooks.artifacts import write_event

    # Pinned literal: the reserved-id contract is the hook's file-name class,
    # not an API the poller is allowed to depend on.
    req_id = f"reply_without_job__{session_id}__20260406T000000000000Z"
    event_path = write_event(
        provider="claude",
        completion_dir=tmp_path / "completion",
        agent_name="agent1",
        workspace_path=r"C:\work\demo",
        req_id=req_id,
        status="completed",
        reply=reply,
        session_id=session_id,
        hook_event_name="Stop",
        diagnostics={"reply_without_job": True},
    )
    payload = json.loads(event_path.read_text(encoding="utf-8"))
    payload["timestamp"] = "2026-04-06T00:01:00Z"
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    return req_id


def _strict_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": 1,
        "record_type": "provider_completion_hook",
        "provider": "claude",
        "agent_name": "agent1",
        "workspace_path": r"C:\work\demo",
        "req_id": "job_1",
        "reply": "completed reply",
        "timestamp": "2026-04-06T00:01:00Z",
        "status": "completed",
        "hook_event_name": "Stop",
        "session_id": "session-1",
    }
    event.update(overrides)
    return event


def test_poll_exact_hook_builds_failed_terminal_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "provider_backends.claude.execution_runtime.hook_results_runtime.load_event",
        lambda completion_dir, request_anchor: _strict_event(
            reply="bad gateway",
            status="failed",
            hook_event_name="completion.failed",
        ),
    )

    result = poll_exact_hook(_strict_submission(), now="2026-04-06T00:02:00Z")

    assert result is not None
    assert result.submission.reply == "bad gateway"
    assert result.submission.runtime_state["next_seq"] == 8
    assert result.decision is not None
    assert result.decision.status is CompletionStatus.FAILED
    assert result.decision.reason == "hook_stop_failure"
    assert result.items[0].payload["provider_turn_ref"] == "session-1"
    assert result.items[0].payload["status"] == "failed"


def test_poll_exact_hook_waits_for_late_final_before_empty_reply_incomplete(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "provider_backends.claude.execution_runtime.hook_results_runtime.load_event",
        lambda completion_dir, request_anchor: _strict_event(reply=""),
    )

    pending = poll_exact_hook(
        _strict_submission(),
        now="2026-04-06T00:02:00Z",
    )
    result = poll_exact_hook(
        _strict_submission(),
        now="2026-04-06T00:04:00Z",
    )

    assert pending is None
    assert result is not None
    assert result.submission.reply == ""
    assert result.submission.runtime_state["next_seq"] == 8
    assert result.decision is not None
    assert result.decision.status is CompletionStatus.INCOMPLETE
    assert result.decision.reason == "hook_stop_empty_reply"
    assert result.decision.diagnostics["empty_reply"] is True
    assert result.decision.diagnostics["error_type"] == "empty_provider_reply"
    assert result.decision.diagnostics["empty_hook_final_text_grace_elapsed"] is True
    assert result.decision.diagnostics["empty_hook_final_text_grace_s"] == 180.0
    assert "without assistant reply text" in result.decision.diagnostics["diagnosis"]
    assert result.items[0].payload["status"] == "incomplete"
    assert result.items[0].payload["empty_reply"] is True
    assert "without assistant reply text" in result.items[0].payload["text"]


def test_capture_cancel_evidence_accepts_exact_hook_with_windows_session_path(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "provider_backends.claude.execution_runtime.hook_results_runtime.load_event",
        lambda completion_dir, request_anchor: _strict_event(),
    )

    decision = capture_exact_hook_cancel_evidence(
        _strict_submission(),
        now="2026-04-06T00:02:00Z",
    )

    assert decision is not None
    assert decision.reply == "completed reply"
    assert decision.diagnostics["cancel_reply_salvaged"] is True
    assert decision.diagnostics["cancel_reply_source"] == "exact_hook_artifact"


def test_normal_hook_poll_rejects_completion_from_another_claude_session(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "provider_backends.claude.execution_runtime.hook_results_runtime.load_event",
        lambda completion_dir, request_anchor: _strict_event(session_id="old-session"),
    )

    result = poll_exact_hook(
        _strict_submission(),
        now="2026-04-06T00:02:00Z",
    )

    assert result is None


@pytest.mark.parametrize(
    ("event_overrides", "runtime_overrides", "diagnostic_overrides", "require_reply"),
    [
        ({"schema_version": 2}, {}, {}, False),
        ({"record_type": "other"}, {}, {}, False),
        ({"req_id": "other"}, {}, {}, False),
        ({"status": "unknown"}, {}, {}, False),
        ({"session_id": None}, {}, {}, False),
        ({}, {"session_path": ""}, {}, False),
        ({"provider": "gemini"}, {}, {}, False),
        ({"agent_name": "other"}, {}, {}, False),
        ({"workspace_path": "/different"}, {}, {}, False),
        ({"timestamp": "2026-04-05T23:59:59Z"}, {}, {}, False),
        ({"timestamp": "2026-04-06T00:03:00Z"}, {}, {}, False),
        ({"reply": ""}, {}, {}, True),
        ({}, {"prompt_sent": False}, {}, False),
        ({}, {}, {"workspace_path": ""}, False),
    ],
)
def test_strict_hook_evidence_fails_closed_when_identity_proof_is_missing(
    monkeypatch,
    event_overrides,
    runtime_overrides,
    diagnostic_overrides,
    require_reply,
) -> None:
    event = _strict_event(**event_overrides)
    submission = _strict_submission()
    submission = replace(
        submission,
        diagnostics={**dict(submission.diagnostics or {}), **diagnostic_overrides},
        runtime_state={**submission.runtime_state, **runtime_overrides},
    )
    monkeypatch.setattr(
        "provider_backends.claude.execution_runtime.hook_results_runtime.load_event",
        lambda completion_dir, request_anchor: event,
    )

    evidence = load_strict_exact_hook_evidence(
        submission,
        now="2026-04-06T00:02:00Z",
        require_reply=require_reply,
    )

    assert evidence is None


def test_poll_exact_hook_attaches_kept_reply_from_turn_outside_any_job(
    tmp_path: Path,
) -> None:
    req_id = _write_reserved_event(tmp_path)

    result = poll_exact_hook(
        _submission_with_completion_dir(tmp_path),
        now="2026-04-06T00:02:00Z",
    )

    assert result is not None
    assert result.submission.reply == "reply kept from an unanchored turn"
    assert result.decision is not None
    assert result.decision.status is CompletionStatus.COMPLETED
    assert result.decision.reply == "reply kept from an unanchored turn"
    assert result.decision.diagnostics["completion_source"] == "hook_artifact"
    assert result.decision.diagnostics["reply_without_job"] is True
    assert result.decision.diagnostics["reply_without_job_req_id"] == req_id
    assert result.items[0].payload["reply"] == "reply kept from an unanchored turn"


def test_poll_exact_hook_does_not_attach_kept_reply_from_another_session(
    tmp_path: Path,
) -> None:
    _write_reserved_event(tmp_path, session_id="other-session")

    assert (
        poll_exact_hook(
            _submission_with_completion_dir(tmp_path),
            now="2026-04-06T00:02:00Z",
        )
        is None
    )


def test_poll_exact_hook_does_not_attach_kept_reply_that_predates_the_submission(
    tmp_path: Path,
) -> None:
    _write_reserved_event(tmp_path)

    assert (
        poll_exact_hook(
            _submission_with_completion_dir(tmp_path, accepted_at="2026-04-06T00:01:30Z"),
            now="2026-04-06T00:02:00Z",
        )
        is None
    )
