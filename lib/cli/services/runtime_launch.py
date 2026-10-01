from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import math
import os
from pathlib import Path
import shutil
import subprocess

from agents.models import AgentSpec
from cli.context import CliContext
from cli.models import ParsedStartCommand
from provider_core.contracts import ProviderRuntimeLauncher
from provider_core.runtime_shared import (
    provider_executable as resolve_provider_executable,
    provider_start_parts as resolve_provider_start_parts,
)
from runtime_observability import record_startup_operation, startup_operation_scope
from terminal_runtime import TmuxBackend, get_backend as get_terminal_backend
from workspace.models import WorkspacePlan

from .provider_hooks import prepare_provider_workspace, provider_workspace_path_for_prepare
from .provider_binding import AgentBinding, resolve_agent_binding
from .role_command_policy import role_command_policy_for_spec, role_command_policy_requires_enforcement
from .runtime_launch_runtime import (
    best_effort_kill_tmux_pane as _best_effort_kill_tmux_pane_impl,
    binding_runtime_alive as _binding_runtime_alive_impl,
    cleanup_stale_tmux_binding as _cleanup_stale_tmux_binding_impl,
    create_detached_tmux_pane as _create_detached_tmux_pane_impl,
    ensure_agent_runtime as _ensure_agent_runtime_impl,
    launch_session_id as _launch_session_id_impl,
    launch_runtime as _launch_runtime_impl,
    pane_meets_minimum_size as _pane_meets_minimum_size_impl,
    pane_title_marker as _pane_title_marker_impl,
    runtime_launcher as _runtime_launcher_impl,
    session_filename as _session_filename_impl,
    write_session_file as _write_session_file_impl,
)


@dataclass(frozen=True)
class RuntimeLaunchResult:
    launched: bool
    binding: AgentBinding | None
    timings_ms: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if self.timings_ms is None:
            return
        object.__setattr__(self, 'timings_ms', _clean_runtime_launch_timings(self.timings_ms))


def _runtime_launcher(provider: str) -> ProviderRuntimeLauncher | None:
    return _runtime_launcher_impl(provider)


def ensure_agent_runtime(
    context: CliContext,
    command: ParsedStartCommand,
    spec: AgentSpec,
    plan: WorkspacePlan,
    binding: AgentBinding | None,
    *,
    assigned_pane_id: str | None = None,
    assigned_pane_ref: dict[str, object] | None = None,
    namespace_ref: dict[str, object] | None = None,
    style_index: int = 0,
    tmux_socket_path: str | None = None,
    namespace_backend_impl: str | None = None,
    provider_prepared: bool = False,
    effective_command: ParsedStartCommand | None = None,
    expected_pane_identity: dict[str, object] | None = None,
) -> RuntimeLaunchResult:
    launcher = _runtime_launcher(spec.provider)
    runtime_dir = context.paths.agent_provider_runtime_dir(spec.name, spec.provider)
    launch_command = (
        effective_command
        if effective_command is not None
        else effective_start_command(command, spec)
    )
    if not provider_prepared:
        provider_workspace_path = provider_workspace_path_for_prepare(
            command=launch_command,
            spec=spec,
            plan=plan,
            runtime_dir=runtime_dir,
            launcher=launcher,
        )
        record_startup_operation('provider_prepare_attempt_count')
        with startup_operation_scope('provider_prepare'):
            prepare_provider_workspace(
                layout=context.paths,
                spec=spec,
                workspace_path=provider_workspace_path,
                completion_dir=runtime_dir / 'completion',
                agent_name=spec.name,
                auto_permission=launch_command.auto_permission,
            )
        record_startup_operation('provider_prepare_count')
    return _ensure_agent_runtime_impl(
        context,
        launch_command,
        spec,
        plan,
        binding,
        runtime_launch_result_cls=RuntimeLaunchResult,
        binding_runtime_alive_fn=_binding_runtime_alive,
        provider_executable_fn=_provider_executable,
        cleanup_stale_tmux_binding_fn=_cleanup_stale_tmux_binding,
        launch_runtime_fn=_launch_runtime,
        resolve_agent_binding_fn=resolve_agent_binding,
        assigned_pane_id=assigned_pane_id,
        assigned_pane_ref=assigned_pane_ref,
        namespace_ref=namespace_ref,
        style_index=style_index,
        tmux_socket_path=tmux_socket_path,
        namespace_backend_impl=namespace_backend_impl,
        expected_pane_identity=expected_pane_identity,
    )


def effective_start_command(command: ParsedStartCommand, spec: AgentSpec) -> ParsedStartCommand:
    """Resolve the single command used for provider preparation and launch."""
    policy = role_command_policy_for_spec(spec)
    if role_command_policy_requires_enforcement(policy) and command.auto_permission:
        return replace(command, auto_permission=False)
    return command


def _launch_runtime(
    context: CliContext,
    command: ParsedStartCommand,
    spec: AgentSpec,
    plan: WorkspacePlan,
    launcher: ProviderRuntimeLauncher,
    *,
    assigned_pane_id: str | None = None,
    assigned_pane_ref: dict[str, object] | None = None,
    namespace_ref: dict[str, object] | None = None,
    style_index: int = 0,
    tmux_socket_path: str | None = None,
    namespace_backend_impl: str | None = None,
    expected_pane_identity: dict[str, object] | None = None,
) -> dict[str, float]:
    return _launch_runtime_impl(
        context,
        command,
        spec,
        plan,
        launcher,
        backend_factory=_runtime_backend_factory(
            namespace_backend_impl=namespace_backend_impl,
            namespace_ref=namespace_ref,
        ),
        pane_title_marker_fn=_pane_title_marker,
        launch_session_id_fn=_launch_session_id,
        create_detached_tmux_pane_fn=_create_detached_tmux_pane,
        pane_meets_minimum_size_fn=_pane_meets_minimum_size,
        best_effort_kill_tmux_pane_fn=_best_effort_kill_tmux_pane,
        write_session_file_fn=_write_session_file,
        assigned_pane_id=assigned_pane_id,
        assigned_pane_ref=assigned_pane_ref,
        namespace_ref=namespace_ref,
        style_index=style_index,
        tmux_socket_path=tmux_socket_path,
        namespace_backend_impl=namespace_backend_impl,
        allow_detached_fallback=tmux_socket_path is None,
        expected_pane_identity=expected_pane_identity,
    )


def _runtime_backend_factory(
    *,
    namespace_backend_impl: str | None,
    namespace_ref: dict[str, object] | None,
):
    if str(namespace_backend_impl or '').strip() != 'herdr':
        return TmuxBackend
    if not isinstance(namespace_ref, dict):
        return TmuxBackend

    def factory(**kwargs):
        del kwargs
        backend = get_terminal_backend('herdr')
        if backend is None:
            raise RuntimeError('Herdr backend is not available for provider runtime launch')
        setattr(backend, '_ccb_project_namespace_ref', dict(namespace_ref))
        return backend

    return factory


def _clean_runtime_launch_timings(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    timings: dict[str, float] = {}
    for key, raw_value in value.items():
        try:
            parsed = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(parsed) or parsed < 0:
            continue
        timings[str(key)] = parsed
    return timings


def _write_session_file(
    *,
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    run_cwd: Path,
    pane_id: str,
    tmux_socket_name: str | None,
    tmux_socket_path: str | None,
    pane_title_marker: str,
    start_cmd: str,
    launch_session_id: str,
    provider_payload: dict[str, object],
    backend_family: str = 'tmux-family',
    backend_impl: str = 'tmux',
    namespace_ref: dict[str, object] | None = None,
    pane_ref: dict[str, object] | None = None,
) -> Path:
    return _write_session_file_impl(
        context=context,
        spec=spec,
        plan=plan,
        runtime_dir=runtime_dir,
        run_cwd=run_cwd,
        pane_id=pane_id,
        tmux_socket_name=tmux_socket_name,
        tmux_socket_path=tmux_socket_path,
        pane_title_marker=pane_title_marker,
        start_cmd=start_cmd,
        launch_session_id=launch_session_id,
        provider_payload=provider_payload,
        backend_family=backend_family,
        backend_impl=backend_impl,
        namespace_ref=namespace_ref,
        pane_ref=pane_ref,
    )


def _launch_session_id(agent_name: str) -> str:
    return _launch_session_id_impl(agent_name)


def _session_filename(spec: AgentSpec) -> str:
    return _session_filename_impl(spec)


def _provider_executable(provider: str) -> str:
    return resolve_provider_executable(provider)


def _provider_start_parts(provider: str) -> list[str]:
    return resolve_provider_start_parts(provider)


def _pane_title_marker(context: CliContext, spec: AgentSpec) -> str:
    return _pane_title_marker_impl(context, spec)


def _binding_runtime_alive(binding: AgentBinding) -> bool:
    return _binding_runtime_alive_impl(
        binding,
        tmux_backend_cls=TmuxBackend,
        herdr_liveness_check_fn=_herdr_liveness_check,
    )


def _herdr_liveness_check(binding) -> bool:
    """通过 herdr IPC 探测 pane 是否存活（capture_pane）。

    mux: runtime_ref 的实际存活性检测 —— 替代 liveness.py 中无条件 return True。
    """
    try:
        from terminal_runtime.api import get_backend_for_session

        session_data = dict(getattr(binding, 'session_ref', None) or {})
        if not session_data:
            return True  # 无 session 数据，假设存活（避免误杀）
        backend = get_backend_for_session(session_data)
        if backend is None:
            return True
        pane_id = str(getattr(binding, 'pane_id', '') or '')
        if not pane_id:
            return True
        # 使用 herdr IPC 探测：capture_pane 成功 → 存活，异常 → 死亡
        backend.capture_pane(pane_id, lines=1)
        return True
    except Exception:
        return False


def _cleanup_stale_tmux_binding(binding: AgentBinding | None) -> None:
    _cleanup_stale_tmux_binding_impl(
        binding,
        tmux_backend_cls=TmuxBackend,
        kill_tmux_pane_fn=_best_effort_kill_tmux_pane,
    )


def _inside_tmux() -> bool:
    return bool((os.environ.get('TMUX') or os.environ.get('TMUX_PANE') or '').strip())


def _prepare_detached_tmux_server(backend: TmuxBackend) -> None:
    from .runtime_launch_runtime import prepare_detached_tmux_server as _prepare_detached_tmux_server_impl

    _prepare_detached_tmux_server_impl(backend)


def _create_detached_tmux_pane(backend: TmuxBackend, *, cmd: str, cwd: Path, session_name: str) -> str:
    return _create_detached_tmux_pane_impl(backend, cmd=cmd, cwd=cwd, session_name=session_name)


def _pane_meets_minimum_size(backend: TmuxBackend, pane_id: str, *, min_width: int = 20, min_height: int = 8) -> bool:
    return _pane_meets_minimum_size_impl(
        backend,
        pane_id,
        min_width=min_width,
        min_height=min_height,
    )


def _best_effort_kill_tmux_pane(backend: TmuxBackend, pane_id: str) -> None:
    _best_effort_kill_tmux_pane_impl(backend, pane_id)
