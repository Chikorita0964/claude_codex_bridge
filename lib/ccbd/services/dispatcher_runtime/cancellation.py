from __future__ import annotations

from dataclasses import replace

from agents.models import AgentState
from completion.models import CompletionConfidence, CompletionDecision, CompletionStatus

from ccbd.api_models import CancelReceipt, JobStatus, TargetKind

from .cancel_flags import cleanup_cancel_flags, clear_cancel_flag, write_cancel_flag
from .completion import build_terminal_state
from .finalization_runtime.artifacts import spill_terminal_reply_if_needed
from .finalization_runtime.message_bureau import record_message_bureau_cancellation
from .reply_delivery import resolve_reply_delivery_terminal


def cancel_job(dispatcher, job_id: str, *, record_reply: bool = True) -> CancelReceipt:
    current = dispatcher.get(job_id)
    if current is None:
        raise dispatcher._dispatch_error(f'unknown job: {job_id}')
    if current.status is JobStatus.CANCELLED:
        return _cancel_receipt(current)
    if current.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.INCOMPLETE}:
        raise dispatcher._dispatch_error(f'job is already terminal: {current.status.value}')

    cancelled_at = dispatcher._clock()
    marked = replace(current, cancel_requested_at=cancelled_at, updated_at=cancelled_at)
    dispatcher._append_job(marked)
    dispatcher._append_event(marked, 'job_cancel_requested', {'status': current.status.value}, timestamp=cancelled_at)
    if current.target_kind is TargetKind.AGENT and current.agent_name:
        # Retain the cooperative compatibility signal documented once in the
        # managed memory bundle. Runtime interruption below remains primary.
        write_cancel_flag(dispatcher._layout, current.agent_name, job_id)
        cleanup_cancel_flags(dispatcher._layout, current.agent_name)
    cancel_evidence = (
        _capture_cancel_evidence(dispatcher, job_id)
        if record_reply
        else None
    )
    composer_clear = (
        _cancel_and_clear_composer(dispatcher, job_id)
        if dispatcher._execution_service is not None
        else None
    )

    snapshot = dispatcher._snapshot_writer.load(job_id)
    snapshot_reply = str(
        getattr(getattr(snapshot, 'latest_decision', None), 'reply', '') or ''
    )
    reply = cancel_evidence.reply if cancel_evidence is not None else snapshot_reply
    return cancel_with_decision(
        dispatcher,
        marked,
        cancelled_at,
        reply,
        snapshot,
        record_reply=record_reply,
        cancel_evidence=cancel_evidence,
        composer_clear=composer_clear,
    )


def cancel_with_decision(
    dispatcher,
    current,
    cancelled_at: str,
    reply: str,
    snapshot,
    *,
    record_reply: bool = True,
    cancel_evidence: CompletionDecision | None = None,
    composer_clear: dict[str, object] | None = None,
) -> CancelReceipt:
    diagnostics = _cancel_diagnostics(cancel_evidence, composer_clear)
    decision = CompletionDecision(
        terminal=True,
        status=CompletionStatus.CANCELLED,
        reason='cancel_info',
        confidence=CompletionConfidence.DEGRADED,
        reply=reply,
        anchor_seen=(
            snapshot.state.anchor_seen
            if snapshot
            else bool(cancel_evidence.anchor_seen if cancel_evidence else False)
        ),
        reply_started=bool(reply) or (snapshot.state.reply_started if snapshot else False),
        reply_stable=(
            bool(cancel_evidence.reply_stable)
            if cancel_evidence is not None
            else (snapshot.state.reply_stable if snapshot else False)
        ),
        provider_turn_ref=(
            cancel_evidence.provider_turn_ref
            if cancel_evidence is not None
            else (snapshot.state.provider_turn_ref if snapshot else None)
        ),
        source_cursor=(
            cancel_evidence.source_cursor
            if cancel_evidence is not None
            else (snapshot.state.latest_cursor if snapshot else None)
        ),
        finished_at=cancelled_at,
        diagnostics=diagnostics,
    )
    with dispatcher._chain_transition_lock:
        latest = dispatcher.get(current.job_id)
        if latest is None:
            raise dispatcher._dispatch_error(f'unknown job: {current.job_id}')
        if latest.status is JobStatus.CANCELLED:
            return _cancel_receipt(latest)
        if latest.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.INCOMPLETE}:
            if current.target_kind is TargetKind.AGENT and current.agent_name:
                clear_cancel_flag(dispatcher._layout, current.agent_name, current.job_id)
            raise dispatcher._dispatch_error(f'job is already terminal: {latest.status.value}')
        current = latest
        decision = spill_terminal_reply_if_needed(
            dispatcher,
            current,
            decision,
            finished_at=cancelled_at,
        )
        if dispatcher._completion_tracker is not None:
            dispatcher._completion_tracker.finish(current.job_id)
        dispatcher._snapshot_writer.write_completion(
            job_id=current.job_id,
            agent_name=current.agent_name,
            profile_family=dispatcher._profile_family_for_job(current),
            state=build_terminal_state(decision, snapshot.state if snapshot else None),
            decision=decision,
            updated_at=cancelled_at,
        )
        dispatcher._append_event(current, 'completion_terminal', decision.to_record(), timestamp=cancelled_at)
        terminal = replace(
            current,
            status=JobStatus.CANCELLED,
            terminal_decision=decision.to_record(),
            updated_at=cancelled_at,
            cancel_requested_at=cancelled_at,
        )
        dispatcher._append_job(terminal)
        dispatcher._append_event(
            terminal,
            'job_cancelled',
            {'status': JobStatus.CANCELLED.value},
            timestamp=cancelled_at,
        )
        dispatcher._state.remove_queued_for(current.target_kind, current.target_name, current.job_id)
        dispatcher._state.clear_active_for(current.target_kind, current.target_name, job_id=current.job_id)
        if dispatcher._message_bureau is not None:
            record_message_bureau_cancellation(
                dispatcher,
                terminal,
                decision,
                finished_at=cancelled_at,
                record_reply=record_reply,
            )
    if current.target_kind is TargetKind.AGENT:
        dispatcher._sync_runtime(current.agent_name, state=AgentState.IDLE)
    resolve_reply_delivery_terminal(dispatcher, terminal, finished_at=cancelled_at)
    return _cancel_receipt(terminal)


def _capture_cancel_evidence(dispatcher, job_id: str) -> CompletionDecision | None:
    execution_service = dispatcher._execution_service
    capture = getattr(execution_service, 'capture_cancel_evidence', None)
    if not callable(capture):
        return None
    try:
        evidence = capture(job_id)
    except Exception:
        return None
    if not isinstance(evidence, CompletionDecision):
        return None
    if not evidence.terminal or not str(evidence.reply or '').strip():
        return None
    return evidence


def _cancel_and_clear_composer(dispatcher, job_id: str) -> dict[str, object] | None:
    """Local patch: cancel the running submission and report what the composer held.

    The execution service clears text the interrupted job left in the composer and
    returns the readback evidence; a provider without a draft guard simply cancels.
    """
    result = dispatcher._execution_service.cancel(job_id)
    return dict(result) if isinstance(result, dict) and result else None


def _cancel_diagnostics(
    cancel_evidence: CompletionDecision | None,
    composer_clear: dict[str, object] | None = None,
) -> dict[str, object]:
    diagnostics: dict[str, object] = {'cancel_requested': True}
    if composer_clear:
        diagnostics.update(composer_clear)
    if cancel_evidence is None:
        return diagnostics
    evidence_diagnostics = dict(cancel_evidence.diagnostics or {})
    diagnostics.update(
        {
            'cancel_reply_salvaged': True,
            'cancel_reply_source': str(
                evidence_diagnostics.get('cancel_reply_source')
                or evidence_diagnostics.get('completion_source')
                or 'provider_terminal_evidence'
            ),
            'captured_completion_status': cancel_evidence.status.value,
            'captured_completion_reason': cancel_evidence.reason,
            'captured_completion_finished_at': cancel_evidence.finished_at,
        }
    )
    for key in ('completion_source', 'completion_fallback_source', 'hook_event_name'):
        value = evidence_diagnostics.get(key)
        if value is not None:
            diagnostics[key] = value
    return diagnostics


def _cancel_receipt(terminal) -> CancelReceipt:
    return CancelReceipt(
        job_id=terminal.job_id,
        agent_name=terminal.agent_name,
        target_kind=terminal.target_kind,
        target_name=terminal.target_name,
        provider_instance=terminal.provider_instance,
        status=JobStatus.CANCELLED,
        cancelled_at=terminal.updated_at,
    )
__all__ = ['cancel_job', 'cancel_with_decision']
