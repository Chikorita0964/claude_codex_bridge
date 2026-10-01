from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agents.models import AgentRuntime, AgentState, RuntimeBindingSource, normalize_runtime_binding_source
from ccbd.services.runtime_recovery_policy import (
    PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH,
    PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH,
)
from provider_runtime.pane_recovery_outcome import pane_recovery_outcome

from ..provider_runtime_facts import build_provider_runtime_facts, ensure_provider_pane, load_provider_session
from ..project_namespace_runtime.slot_replacement import (
    inject_project_slot_recovery_hints,
    relabel_project_slot_pane,
    resolve_project_slot_recovery_context,
)


def _workspace_path(runtime) -> str:
    return str(getattr(runtime, 'workspace_path', '') or '').strip()


def _attach_missing_session(*, attach_runtime_fn, agent_name: str, workspace_path: str, runtime) -> object:
    return attach_runtime_fn(
        agent_name=agent_name,
        workspace_path=workspace_path,
        backend_type=runtime.backend_type,
        pid=runtime.pid,
        runtime_ref=runtime.runtime_ref,
        session_ref=runtime.session_ref,
        health='session-missing',
        slot_key=getattr(runtime, 'slot_key', None) or agent_name,
        window_id=getattr(runtime, 'window_id', None),
        workspace_epoch=getattr(runtime, 'workspace_epoch', None),
        binding_source=runtime.binding_source,
    )


@dataclass(frozen=True)
class _PaneResolution:
    pane_id: str | None
    blocked_health: str | None = None
    blocked_detail: str | None = None
    pane_outcome: str | None = None


@dataclass(frozen=True)
class ProviderBindingRefresh:
    """A refresh and what its ``ensure_pane`` reported: ``pane_outcome`` is None when the call
    never reached a provider session, or when the session published no outcome."""

    runtime: AgentRuntime | None
    pane_outcome: str | None = None


def _session_recovery_block(session) -> tuple[str, str] | None:
    data = getattr(session, 'data', None)
    if not isinstance(data, dict):
        return None
    block = data.get('pane_recovery_block')
    if not isinstance(block, dict):
        return None
    reason = str(block.get('reason') or '').strip().lower()
    detail = str(block.get('detail') or '').strip()
    if not reason:
        return None
    block_pane_id = str(block.get('pane_id') or '').strip()
    session_pane_id = str(getattr(session, 'pane_id', '') or '').strip()
    if block_pane_id and session_pane_id and block_pane_id != session_pane_id:
        return None
    health = (
        PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH
        if reason == 'provider_auth_revoked'
        else PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH
    )
    if not detail:
        detail = f'Provider recovery is blocked: {reason}; repair authentication and remount'
    return health, detail


def _resolve_pane(session, *, recover: bool) -> _PaneResolution:
    pane_id = str(getattr(session, 'pane_id', '') or '').strip()
    if not recover:
        return _PaneResolution(pane_id or None)
    ok, pane_or_err = ensure_provider_pane(session)
    if not ok:
        block = _session_recovery_block(session)
        if block is not None:
            return _PaneResolution(None, blocked_health=block[0], blocked_detail=block[1])
        return _PaneResolution(None)
    return _PaneResolution(
        str(pane_or_err or '').strip() or None,
        pane_outcome=pane_recovery_outcome(session),
    )


def _patch_recovery_blocked_runtime(
    *,
    patch_runtime_state_fn,
    runtime,
    health: str,
    detail: str,
):
    if not callable(patch_runtime_state_fn):
        return runtime
    return patch_runtime_state_fn(
        runtime,
        state=AgentState.DEGRADED,
        health=health,
        active_pane_id=None,
        pane_state='dead',
        reconcile_state='blocked',
        last_failure_reason=detail,
        lifecycle_state='degraded',
    )


def _attach_healthy_runtime(
    *,
    attach_runtime_fn,
    agent_name: str,
    workspace_path: str,
    runtime,
    provider: str,
    facts,
    active_pane_id: str | None,
    slot_key: str | None,
    window_id: str | None,
    workspace_epoch: int | None,
) -> object:
    return attach_runtime_fn(
        agent_name=agent_name,
        workspace_path=workspace_path,
        backend_type=runtime.backend_type,
        pid=runtime.pid,
        runtime_ref=facts.runtime_ref or runtime.runtime_ref,
        session_ref=facts.session_ref or runtime.session_ref,
        health='healthy',
        provider=provider,
        runtime_root=facts.runtime_root,
        runtime_pid=facts.runtime_pid,
        terminal_backend=facts.terminal_backend,
        provider_runtime_backend_ref=facts.provider_runtime_backend_ref,
        namespace_ref=facts.namespace_ref,
        pane_ref=facts.pane_ref,
        namespace_restore_token_present=facts.namespace_restore_token_present,
        herdr_auto_restore_mode=facts.herdr_auto_restore_mode,
        herdr_agent_state_ref=facts.herdr_agent_state_ref,
        pane_id=facts.pane_id,
        active_pane_id=active_pane_id,
        pane_title_marker=facts.pane_title_marker,
        pane_state=facts.pane_state,
        tmux_socket_name=facts.tmux_socket_name,
        tmux_socket_path=facts.tmux_socket_path,
        session_file=facts.session_file,
        session_id=facts.session_id,
        slot_key=slot_key,
        window_id=window_id,
        workspace_epoch=workspace_epoch,
        binding_source=runtime.binding_source,
    )


def refresh_provider_binding(
    *,
    layout,
    registry,
    session_bindings,
    attach_runtime_fn,
    patch_runtime_state_fn=None,
    agent_name: str,
    recover: bool = False,
):
    runtime = registry.get(agent_name)
    if runtime is None:
        return ProviderBindingRefresh(None)
    if normalize_runtime_binding_source(runtime.binding_source) is RuntimeBindingSource.EXTERNAL_ATTACH:
        return ProviderBindingRefresh(runtime)
    workspace_path = _workspace_path(runtime)
    if not workspace_path:
        return ProviderBindingRefresh(runtime)
    spec = registry.spec_for(agent_name)
    binding = session_bindings.get(spec.provider)
    if binding is None:
        return ProviderBindingRefresh(runtime)

    replacement_context = (
        resolve_project_slot_recovery_context(
            layout=layout,
            config=getattr(registry, '_config', None),
            runtime=runtime,
            agent_name=agent_name,
        )
        if recover
        else None
    )
    session = load_provider_session(binding, Path(workspace_path), agent_name)
    if session is None:
        return ProviderBindingRefresh(
            _attach_missing_session(
                attach_runtime_fn=attach_runtime_fn,
                agent_name=agent_name,
                workspace_path=workspace_path,
                runtime=runtime,
            )
        )

    inject_project_slot_recovery_hints(session, replacement_context)
    pane_resolution = _resolve_pane(session, recover=recover)
    if recover and pane_resolution.blocked_health is not None:
        return ProviderBindingRefresh(
            _patch_recovery_blocked_runtime(
                patch_runtime_state_fn=patch_runtime_state_fn,
                runtime=runtime,
                health=pane_resolution.blocked_health,
                detail=str(pane_resolution.blocked_detail or pane_resolution.blocked_health),
            )
        )
    pane_id = pane_resolution.pane_id
    if recover and pane_id is None:
        return ProviderBindingRefresh(runtime)
    if pane_id is not None:
        relabel_project_slot_pane(
            pane_id=pane_id,
            context=replacement_context,
            session_id=_session_ccb_session_id(session),
        )
    facts = build_provider_runtime_facts(
        session,
        binding=binding,
        provider=spec.provider,
        pane_id_override=pane_id,
    )
    return ProviderBindingRefresh(
        _attach_healthy_runtime(
            attach_runtime_fn=attach_runtime_fn,
            agent_name=agent_name,
            workspace_path=workspace_path,
            runtime=runtime,
            provider=spec.provider,
            facts=facts,
            active_pane_id=pane_id,
            slot_key=(replacement_context.slot_key if replacement_context is not None else getattr(runtime, 'slot_key', None) or agent_name),
            window_id=(replacement_context.workspace_window_id if replacement_context is not None else getattr(runtime, 'window_id', None)),
            workspace_epoch=(
                replacement_context.workspace_epoch if replacement_context is not None else getattr(runtime, 'workspace_epoch', None)
            ),
        ),
        pane_outcome=pane_resolution.pane_outcome,
    )


def _session_ccb_session_id(session) -> str | None:
    text = str(getattr(session, 'ccb_session_id', '') or '').strip()
    if text:
        return text
    data = getattr(session, 'data', None)
    if isinstance(data, dict):
        text = str(data.get('ccb_session_id') or '').strip()
        if text:
            return text
    return None
