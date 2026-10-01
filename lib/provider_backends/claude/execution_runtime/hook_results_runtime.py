from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ccbd.system import parse_utc_timestamp
from completion.models import (
    CompletionConfidence,
    CompletionCursor,
    CompletionDecision,
    CompletionItemKind,
    CompletionStatus,
)
from provider_execution.base import ProviderPollResult, ProviderSubmission
from provider_execution.common import build_item, request_anchor_from_runtime_state
from provider_hooks.artifacts import is_reserved_req_id, iter_reserved_events, load_event


@dataclass(frozen=True)
class HookPollContext:
    completion_dir: str
    request_anchor: str
    next_seq: int


@dataclass(frozen=True)
class ExactHookEvidence:
    context: HookPollContext
    event: dict[str, object]
    event_at: datetime


_EMPTY_HOOK_FINAL_TEXT_GRACE_S = 180.0


def poll_exact_hook(submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
    evidence = load_strict_exact_hook_evidence(
        submission,
        now=now,
        allow_unattributed=True,
    )
    if evidence is None:
        return None
    event = evidence.event
    status = hook_status(event)
    reply = hook_reply(event)
    extra_diagnostics: dict[str, object] | None = None
    if not reply and status in {CompletionStatus.COMPLETED, CompletionStatus.INCOMPLETE}:
        observed_at = _parse_timestamp(now)
        if observed_at is None:
            return None
        age_s = (observed_at - evidence.event_at).total_seconds()
        if age_s < _EMPTY_HOOK_FINAL_TEXT_GRACE_S:
            return None
        extra_diagnostics = {
            "empty_hook_final_text_grace_elapsed": True,
            "empty_hook_final_text_grace_s": _EMPTY_HOOK_FINAL_TEXT_GRACE_S,
            "empty_hook_age_s": age_s,
        }
    if is_reserved_req_id(str(event.get("req_id") or "")):
        extra_diagnostics = {
            **dict(extra_diagnostics or {}),
            "reply_without_job": True,
            "reply_without_job_req_id": str(event.get("req_id") or ""),
        }
    return poll_hook_event(
        submission,
        context=evidence.context,
        event=event,
        now=now,
        extra_diagnostics=extra_diagnostics,
    )


def poll_hook_event(
    submission: ProviderSubmission,
    *,
    context: HookPollContext,
    event: dict[str, object],
    now: str,
    extra_diagnostics: dict[str, object] | None = None,
) -> ProviderPollResult:
    reply = hook_reply(event)
    status = hook_status(event)
    diagnostics = hook_diagnostics(event, extra=extra_diagnostics)
    status, diagnostics = normalize_stalled_reply_status(
        status,
        diagnostics,
        reply=reply,
    )
    status, diagnostics = normalize_empty_reply_status(status, diagnostics, reply=reply)
    provider_turn_ref = hook_provider_turn_ref(event, request_anchor=context.request_anchor)
    cursor_path = hook_cursor_path(context)
    item = build_hook_item(
        submission,
        event=event,
        context=context,
        reply=reply,
        status=status,
        diagnostics=diagnostics,
        provider_turn_ref=provider_turn_ref,
        cursor_path=cursor_path,
        now=now,
    )
    decision = build_hook_decision(
        submission,
        event=event,
        context=context,
        reply=reply,
        status=status,
        diagnostics=diagnostics,
        provider_turn_ref=provider_turn_ref,
        cursor_path=cursor_path,
        now=now,
    )
    updated = advance_submission(submission, reply=reply, next_seq=context.next_seq + 1)
    return ProviderPollResult(submission=updated, items=(item,), decision=decision)


def load_strict_exact_hook_evidence(
    submission: ProviderSubmission,
    *,
    now: str | None = None,
    require_reply: bool = False,
    allow_unattributed: bool = False,
) -> ExactHookEvidence | None:
    """Load independently attributable hook evidence for every terminal path.

    Prompt activation is necessary but does not prove that a hook artifact
    belongs to the current managed agent/session. Normal polling, recovery,
    and cancellation all fail closed unless provider, agent, workspace,
    request time, and Claude session identity agree.

    ``allow_unattributed`` additionally accepts a kept reply from a turn that
    resolves to no CCB job (the finish hook's reserved-id events). Because
    such a reply has no request anchor to bind to, it is only attached while
    the submission is active: provider, agent, workspace, Claude session, and
    the accepted-at..now window must all still agree.
    """
    if not bool(submission.runtime_state.get("prompt_sent", False)):
        return None
    context = hook_poll_context(submission)
    if context is None:
        return None
    event = load_event(context.completion_dir, context.request_anchor)
    unattributed = False
    if not event and allow_unattributed:
        event = _load_unattributed_hook_event(submission, context=context, now=now)
        unattributed = event is not None
    if not event:
        return None
    if unattributed:
        if not is_reserved_req_id(str(event.get("req_id") or "")):
            return None
    elif str(event.get("req_id") or "").strip() != context.request_anchor:
        return None
    if not hook_event_matches_submission(submission, event):
        return None
    try:
        hook_status(event)
    except (TypeError, ValueError):
        return None
    event_at = _parse_timestamp(event.get("timestamp"))
    accepted_at = _parse_timestamp(submission.accepted_at)
    if event_at is None or accepted_at is None or event_at < accepted_at:
        return None
    if now is not None:
        observed_at = _parse_timestamp(now)
        if observed_at is None or event_at > observed_at:
            return None
    if require_reply and not hook_reply(event):
        return None
    return ExactHookEvidence(context=context, event=event, event_at=event_at)


def _load_unattributed_hook_event(
    submission: ProviderSubmission,
    *,
    context: HookPollContext,
    now: str | None,
) -> dict[str, object] | None:
    """The newest kept reply event that belongs to this active submission.

    Reserved-id events carry no job anchor; identity is the Claude session
    plus the submission window, so an event that predates the submission
    (or comes from another session) can never attach to it. When several
    events land inside one window the newest turn end wins.
    """
    accepted_at = _parse_timestamp(submission.accepted_at)
    observed_at = _parse_timestamp(now) if now is not None else None
    candidates: list[tuple[datetime, dict[str, object]]] = []
    for event in iter_reserved_events(context.completion_dir):
        if not hook_event_matches_submission(submission, event):
            continue
        try:
            hook_status(event)
        except (TypeError, ValueError):
            continue
        event_at = _parse_timestamp(event.get("timestamp"))
        if event_at is None or accepted_at is None or event_at < accepted_at:
            continue
        if now is not None and (observed_at is None or event_at > observed_at):
            continue
        candidates.append((event_at, event))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def capture_exact_hook_cancel_evidence(
    submission: ProviderSubmission,
    *,
    now: str,
) -> CompletionDecision | None:
    evidence = load_strict_exact_hook_evidence(
        submission,
        now=now,
        require_reply=True,
    )
    if evidence is None:
        return None
    result = poll_hook_event(
        submission,
        context=evidence.context,
        event=evidence.event,
        now=now,
        extra_diagnostics={
            "cancel_reply_salvaged": True,
            "cancel_reply_source": "exact_hook_artifact",
            "completion_fallback_source": "cancel_exact_hook_artifact",
        },
    )
    return result.decision


def hook_event_matches_submission(
    submission: ProviderSubmission,
    event: dict[str, object],
) -> bool:
    if event.get("schema_version") != 1:
        return False
    if str(event.get("record_type") or "").strip() != "provider_completion_hook":
        return False
    if str(event.get("provider") or "").strip().lower() != submission.provider.strip().lower():
        return False
    if str(event.get("agent_name") or "").strip() != submission.agent_name.strip():
        return False
    expected_workspace = str((submission.diagnostics or {}).get("workspace_path") or "").strip()
    recorded_workspace = str(event.get("workspace_path") or "").strip()
    if not expected_workspace or not recorded_workspace:
        return False
    if _normalized_path_text(recorded_workspace) != _normalized_path_text(expected_workspace):
        return False
    hook_session = str(event.get("session_id") or "").strip()
    tracked_session = _tracked_session_id(submission)
    return bool(hook_session and tracked_session and hook_session == tracked_session)


def _tracked_session_id(submission: ProviderSubmission) -> str:
    tracked_path = str(submission.runtime_state.get("session_path") or "").strip()
    if not tracked_path:
        return ""
    return _normalized_path_text(tracked_path).rsplit("/", 1)[-1].removesuffix(".jsonl")


def _normalized_path_text(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parse_utc_timestamp(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def hook_poll_context(submission: ProviderSubmission) -> HookPollContext | None:
    completion_dir = str(submission.runtime_state.get("completion_dir") or "").strip()
    request_anchor = request_anchor_from_runtime_state(submission.runtime_state, fallback=submission.job_id)
    next_seq = int(submission.runtime_state.get("next_seq", 1))
    if not completion_dir or not request_anchor:
        return None
    return HookPollContext(
        completion_dir=completion_dir,
        request_anchor=request_anchor,
        next_seq=next_seq,
    )


def hook_reply(event: dict[str, object]) -> str:
    return str(event.get("reply") or "").strip()


def hook_status(event: dict[str, object]) -> CompletionStatus:
    return CompletionStatus(str(event.get("status") or CompletionStatus.COMPLETED.value))


def normalize_empty_reply_status(
    status: CompletionStatus,
    diagnostics: dict[str, object],
    *,
    reply: str,
) -> tuple[CompletionStatus, dict[str, object]]:
    if reply or status not in {CompletionStatus.COMPLETED, CompletionStatus.INCOMPLETE}:
        return status, diagnostics
    normalized = dict(diagnostics)
    normalized.setdefault("reason", "hook_stop_empty_reply")
    normalized.setdefault("empty_reply", True)
    normalized.setdefault("error_type", "empty_provider_reply")
    normalized.setdefault(
        "message",
        "Provider completion hook fired without assistant reply text; inspect "
        "the provider transcript, pane state, and authentication/API output.",
    )
    normalized.setdefault("diagnosis", normalized["message"])
    return CompletionStatus.INCOMPLETE, normalized


def normalize_stalled_reply_status(
    status: CompletionStatus,
    diagnostics: dict[str, object],
    *,
    reply: str,
) -> tuple[CompletionStatus, dict[str, object]]:
    first_line = reply.strip().splitlines()[0].strip().casefold() if reply.strip() else ""
    if not first_line.startswith("api error: response stalled mid-stream"):
        return status, diagnostics
    normalized = dict(diagnostics)
    normalized["reason"] = "claude_response_stalled_mid_stream"
    normalized.setdefault("error_type", "provider_api_error")
    normalized.setdefault("error_message", reply)
    normalized.setdefault("message", reply)
    normalized.setdefault("diagnosis", reply)
    normalized["response_incomplete"] = True
    return CompletionStatus.FAILED, normalized


def hook_provider_turn_ref(event: dict[str, object], *, request_anchor: str) -> str:
    return str(event.get("session_id") or request_anchor)


def hook_cursor_path(context: HookPollContext) -> str:
    return str(Path(context.completion_dir) / "events" / f"{context.request_anchor}.json")


def hook_timestamp(event: dict[str, object], *, now: str) -> str:
    return str(event.get("timestamp") or now)


def hook_item_payload(
    *,
    event: dict[str, object],
    reply: str,
    request_anchor: str,
    provider_turn_ref: str,
    status: CompletionStatus,
    diagnostics: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "reply": reply,
        "text": reply,
        "turn_id": request_anchor,
        "provider_turn_ref": provider_turn_ref,
        "completion_source": "hook_artifact",
        "hook_event_name": event.get("hook_event_name"),
        "status": status.value,
    }
    if not payload["text"]:
        fallback_text = fallback_payload_text(diagnostics)
        if fallback_text:
            payload["text"] = fallback_text
    for key, value in diagnostics.items():
        if value is None or key in payload:
            continue
        payload[key] = value
    return payload


def hook_diagnostics(
    event: dict[str, object],
    *,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    diagnostics = dict(event.get("diagnostics") or {})
    diagnostics.setdefault("completion_source", "hook_artifact")
    diagnostics.setdefault("hook_event_name", event.get("hook_event_name"))
    diagnostics.update(dict(extra or {}))
    return diagnostics


def fallback_payload_text(diagnostics: dict[str, object]) -> str:
    for key in ("text", "error_message", "message", "error", "diagnosis"):
        text = str(diagnostics.get(key) or "").strip()
        if text:
            return text
    return ""


def hook_reason(status: CompletionStatus, diagnostics: dict[str, object]) -> str:
    explicit_reason = str(diagnostics.get("reason") or "").strip().lower()
    if explicit_reason:
        return explicit_reason
    if status is CompletionStatus.FAILED:
        return "hook_stop_failure"
    if status is CompletionStatus.CANCELLED:
        return "hook_stop_cancelled"
    if status is CompletionStatus.INCOMPLETE:
        return "hook_stop_incomplete"
    return "hook_stop"


def build_hook_item(
    submission: ProviderSubmission,
    *,
    event: dict[str, object],
    context: HookPollContext,
    reply: str,
    status: CompletionStatus,
    diagnostics: dict[str, object],
    provider_turn_ref: str,
    cursor_path: str,
    now: str,
):
    return build_item(
        submission,
        kind=CompletionItemKind.ASSISTANT_FINAL,
        timestamp=hook_timestamp(event, now=now),
        seq=context.next_seq,
        payload=hook_item_payload(
            event=event,
            reply=reply,
            request_anchor=context.request_anchor,
            provider_turn_ref=provider_turn_ref,
            status=status,
            diagnostics=diagnostics,
        ),
        cursor_kwargs={"opaque_cursor": cursor_path},
    )


def build_hook_decision(
    submission: ProviderSubmission,
    *,
    event: dict[str, object],
    context: HookPollContext,
    reply: str,
    status: CompletionStatus,
    diagnostics: dict[str, object],
    provider_turn_ref: str,
    cursor_path: str,
    now: str,
) -> CompletionDecision:
    timestamp = hook_timestamp(event, now=now)
    return CompletionDecision(
        terminal=True,
        status=status,
        reason=hook_reason(status, diagnostics),
        confidence=CompletionConfidence.EXACT,
        reply=reply,
        anchor_seen=bool(submission.runtime_state.get("anchor_seen", False)),
        reply_started=bool(reply),
        reply_stable=bool(reply),
        provider_turn_ref=provider_turn_ref,
        source_cursor=CompletionCursor(
            source_kind=submission.source_kind,
            opaque_cursor=cursor_path,
            event_seq=context.next_seq,
            updated_at=timestamp,
        ),
        finished_at=timestamp,
        diagnostics=diagnostics,
    )


def advance_submission(submission: ProviderSubmission, *, reply: str, next_seq: int) -> ProviderSubmission:
    return replace(
        submission,
        reply=reply,
        runtime_state={**submission.runtime_state, "next_seq": next_seq},
    )


__all__ = [
    "ExactHookEvidence",
    "capture_exact_hook_cancel_evidence",
    "hook_event_matches_submission",
    "hook_poll_context",
    "load_strict_exact_hook_evidence",
    "poll_exact_hook",
    "poll_hook_event",
]
