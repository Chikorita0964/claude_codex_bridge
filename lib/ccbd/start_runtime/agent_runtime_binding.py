from __future__ import annotations

import math
from time import monotonic_ns

from .binding_runtime.common import (
    binding_pane_id,
    is_pane_runtime_ref,
    runtime_ref_backend,
    runtime_ref_pane_id,
)
from .agent_runtime_models import RuntimeBindingState


def resolve_runtime_binding_state(
    *,
    context,
    command,
    agent_name: str,
    spec,
    plan,
    binding,
    raw_binding,
    stale_binding: bool,
    assigned_pane_id: str | None,
    assigned_pane_ref: dict[str, object] | None = None,
    namespace_ref: dict[str, object] | None = None,
    style_index: int,
    project_id: str,
    tmux_socket_path: str | None,
    namespace_backend_impl: str | None = None,
    namespace_epoch: int | None,
    window_name: str | None = None,
    ensure_agent_runtime_fn,
    launch_binding_hint_fn,
    relabel_project_namespace_pane_fn,
    same_tmux_socket_path_fn,
    provider_prepared: bool = False,
    effective_command=None,
    reused_pane_identity_current: bool = False,
    timings_ms: dict[str, float] | None = None,
) -> RuntimeBindingState:
    stage_started_ns = monotonic_ns()
    try:
        binding, agent_action, launch_timings_ms = launch_or_reuse_binding(
            context=context,
            command=command,
            spec=spec,
            plan=plan,
            binding=binding,
            raw_binding=raw_binding,
            stale_binding=stale_binding,
            assigned_pane_id=assigned_pane_id,
            assigned_pane_ref=assigned_pane_ref,
            namespace_ref=namespace_ref,
            style_index=style_index,
            tmux_socket_path=tmux_socket_path,
            namespace_backend_impl=namespace_backend_impl,
            window_name=window_name,
            namespace_epoch=namespace_epoch,
            ensure_agent_runtime_fn=ensure_agent_runtime_fn,
            launch_binding_hint_fn=launch_binding_hint_fn,
            provider_prepared=provider_prepared,
            effective_command=effective_command,
        )
    except Exception as exc:
        _merge_launch_timings(
            timings_ms,
            getattr(exc, 'ccb_startup_timings_ms', None),
            enclosing_elapsed_ms=_elapsed_ms(stage_started_ns),
        )
        _attach_startup_timings(exc, timings_ms)
        raise
    _merge_launch_timings(
        timings_ms,
        launch_timings_ms,
        enclosing_elapsed_ms=_elapsed_ms(stage_started_ns),
    )

    stage_started_ns = monotonic_ns()
    actions_taken: list[str] = []
    if not (agent_action == 'attached' and reused_pane_identity_current):
        actions_taken.extend(
            relabel_runtime_pane(
                binding=binding,
                agent_name=agent_name,
                project_id=project_id,
                style_index=style_index,
                tmux_socket_path=tmux_socket_path,
                namespace_epoch=namespace_epoch,
                window_name=window_name,
                relabel_project_namespace_pane_fn=relabel_project_namespace_pane_fn,
            )
        )
    runtime_ref, session_ref, health, lifecycle_state, agent_action = runtime_status(
        binding=binding,
        stale_binding=stale_binding,
        agent_name=agent_name,
        agent_action=agent_action,
        actions_taken=actions_taken,
    )
    socket_name, runtime_pane_id, project_socket_active_pane_id = runtime_pane_facts(
        binding=binding,
        runtime_ref=runtime_ref,
        tmux_socket_path=tmux_socket_path,
        same_tmux_socket_path_fn=same_tmux_socket_path_fn,
    )
    _record_elapsed_ms(timings_ms, 'pane_and_runtime_facts', stage_started_ns)
    return RuntimeBindingState(
        binding=binding,
        agent_action=agent_action,
        actions_taken=tuple(actions_taken),
        runtime_ref=runtime_ref,
        session_ref=session_ref,
        health=health,
        lifecycle_state=lifecycle_state,
        socket_name=socket_name,
        runtime_pane_id=runtime_pane_id,
        project_socket_active_pane_id=project_socket_active_pane_id,
    )


def launch_or_reuse_binding(
    *,
    context,
    command,
    spec,
    plan,
    binding,
    raw_binding,
    stale_binding: bool,
    assigned_pane_id: str | None,
    assigned_pane_ref: dict[str, object] | None = None,
    namespace_ref: dict[str, object] | None = None,
    style_index: int,
    tmux_socket_path: str | None,
    namespace_backend_impl: str | None = None,
    window_name: str | None = None,
    namespace_epoch: int | None = None,
    ensure_agent_runtime_fn,
    launch_binding_hint_fn,
    provider_prepared: bool = False,
    effective_command=None,
):
    if binding is not None and not _is_herdr_assigned_pane_ref(assigned_pane_ref):
        return binding, 'attached', {}

    launch_kwargs = dict(
        assigned_pane_id=assigned_pane_id,
        style_index=style_index,
        tmux_socket_path=tmux_socket_path,
        provider_prepared=provider_prepared,
    )
    if assigned_pane_id is not None and namespace_epoch is not None:
        # A namespace assignment is captured when the topology is materialized; if the
        # namespace is rebuilt before the launch, the pane id can now name another
        # agent's pane.  State the identity the pane must still carry so the launcher
        # can refuse a foreign respawn (see tmux_panes.require_assigned_pane_identity).
        launch_kwargs['expected_pane_identity'] = {
            'slot': str(getattr(spec, 'name', '') or '').strip(),
            'window': window_name,
            'epoch': int(namespace_epoch),
        }
    if assigned_pane_ref is not None:
        launch_kwargs['assigned_pane_ref'] = assigned_pane_ref
    if namespace_ref is not None:
        launch_kwargs['namespace_ref'] = namespace_ref
    if namespace_backend_impl is not None:
        launch_kwargs['namespace_backend_impl'] = namespace_backend_impl
    if effective_command is not None:
        launch_kwargs['effective_command'] = effective_command
    launch = ensure_agent_runtime_fn(
        context,
        command,
        spec,
        plan,
        launch_binding_hint_fn(
            binding=binding,
            raw_binding=raw_binding,
            stale_binding=stale_binding,
            assigned_pane_id=assigned_pane_id,
            tmux_socket_path=tmux_socket_path,
        ),
        **launch_kwargs,
    )
    binding = launch.binding
    launch_timings_ms = getattr(launch, 'timings_ms', None)
    if stale_binding and launch.launched:
        return binding, 'relaunched', launch_timings_ms
    if launch.launched:
        return binding, 'launched', launch_timings_ms
    return binding, 'attached', launch_timings_ms


def _is_herdr_assigned_pane_ref(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return str(value.get('backend_impl') or '').strip() == 'herdr'


def relabel_runtime_pane(
    *,
    binding,
    agent_name: str,
    project_id: str,
    style_index: int,
    tmux_socket_path: str | None,
    namespace_epoch: int | None,
    window_name: str | None = None,
    relabel_project_namespace_pane_fn,
) -> tuple[str, ...]:
    if binding is None:
        return ()
    relabeled_pane = relabel_project_namespace_pane_fn(
        binding=binding,
        agent_name=agent_name,
        project_id=project_id,
        style_index=style_index,
        tmux_socket_path=tmux_socket_path,
        namespace_epoch=namespace_epoch,
        window_name=window_name,
    )
    if relabeled_pane is None:
        return ()
    return (f'relabel_runtime_pane:{agent_name}:{relabeled_pane}',)


def runtime_status(
    *,
    binding,
    stale_binding: bool,
    agent_name: str,
    agent_action: str,
    actions_taken: list[str],
) -> tuple[str | None, str | None, str, str, str]:
    if binding is None and stale_binding:
        actions_taken.append(f'degraded_stale_binding:{agent_name}')
        return '', '', 'degraded', 'degraded', 'degraded'

    runtime_ref = binding.runtime_ref if binding else None
    session_ref = binding.session_ref if binding else None
    actions_taken.extend(runtime_action_markers(agent_name=agent_name, agent_action=agent_action))
    return runtime_ref, session_ref, 'healthy', 'idle', agent_action


def runtime_action_markers(*, agent_name: str, agent_action: str) -> tuple[str, ...]:
    mapping = {
        'attached': f'reuse_binding:{agent_name}',
        'launched': f'launch_runtime:{agent_name}',
        'relaunched': f'relaunch_runtime:{agent_name}',
    }
    marker = mapping.get(agent_action)
    return (marker,) if marker else ()


def runtime_pane_facts(
    *,
    binding,
    runtime_ref: str | None,
    tmux_socket_path: str | None,
    same_tmux_socket_path_fn,
) -> tuple[str | None, str | None, str | None]:
    if not runtime_ref or not is_pane_runtime_ref(runtime_ref) or binding is None:
        return None, None, None
    runtime_pane_id = binding_pane_id(binding) or runtime_ref_pane_id(runtime_ref)
    if runtime_pane_id is None:
        return None, None, None
    socket_name = binding.tmux_socket_path or binding.tmux_socket_name
    project_socket_active_pane_id = None
    if (
        runtime_ref_backend(runtime_ref) == 'tmux'
        and same_tmux_socket_path_fn(getattr(binding, 'tmux_socket_path', None), tmux_socket_path)
    ):
        project_socket_active_pane_id = runtime_pane_id
    return socket_name, runtime_pane_id, project_socket_active_pane_id


def _record_elapsed_ms(
    timings_ms: dict[str, float] | None,
    field_name: str,
    started_ns: int,
) -> None:
    if timings_ms is None:
        return
    elapsed_ms = _elapsed_ms(started_ns)
    timings_ms[field_name] = timings_ms.get(field_name, 0.0) + elapsed_ms


def _merge_launch_timings(
    timings_ms: dict[str, float] | None,
    value: object,
    *,
    enclosing_elapsed_ms: float,
) -> None:
    if timings_ms is None:
        return
    clean: dict[str, float] = {}
    if isinstance(value, dict):
        for key, raw_value in value.items():
            try:
                parsed = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(parsed) or parsed < 0:
                continue
            clean[str(key)] = parsed
    measured_ms = sum(clean.values())
    if measured_ms > enclosing_elapsed_ms:
        clean = {}
        measured_ms = 0.0
    for field_name, elapsed_ms in clean.items():
        timings_ms[field_name] = timings_ms.get(field_name, 0.0) + elapsed_ms
    timings_ms['unattributed'] = timings_ms.get('unattributed', 0.0) + max(
        0.0,
        enclosing_elapsed_ms - measured_ms,
    )


def _elapsed_ms(started_ns: int) -> float:
    return max(0.0, (monotonic_ns() - started_ns) / 1_000_000)


def _attach_startup_timings(
    exc: Exception,
    timings_ms: dict[str, float] | None,
) -> None:
    if timings_ms is None:
        return
    try:
        setattr(exc, 'ccb_startup_timings_ms', dict(timings_ms))
    except Exception:
        return


__all__ = ['resolve_runtime_binding_state']
