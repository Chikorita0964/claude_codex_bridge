from __future__ import annotations

import math
import os
import shutil
from time import monotonic_ns

from agents.models import RuntimeMode
from provider_core.registry import CORE_PROVIDER_NAMES, OPTIONAL_PROVIDER_NAMES, build_default_runtime_launcher_map


_PANE_BACKED_RUNTIME_PROVIDERS = frozenset(CORE_PROVIDER_NAMES + OPTIONAL_PROVIDER_NAMES)

# design I-3: 显式 [runtime.mux] backend = "herdr" 时允许通过 gate 的 provider
# allow-list。只收录有真实 Herdr 环境启动证据的 provider；新增 provider 前必须先
# 有 Herdr pane 实际运行验证（见 epic windows-native-herdr-ccb ITEM-1：Codex/Claude
# 已在 Herdr pane 运行并输出内容）。自动检测触发的 herdr 后端不经过此 allow-list。
_HERDR_NATIVE_VERIFIED_PROVIDERS = frozenset({'codex', 'claude'})


def runtime_launcher(provider: str):
    return build_default_runtime_launcher_map(include_optional=True).get(str(provider or '').strip().lower())


def ensure_agent_runtime(
    context,
    command,
    spec,
    plan,
    binding,
    *,
    runtime_launch_result_cls,
    binding_runtime_alive_fn,
    provider_executable_fn,
    cleanup_stale_tmux_binding_fn,
    launch_runtime_fn,
    resolve_agent_binding_fn,
    assigned_pane_id: str | None = None,
    assigned_pane_ref: dict[str, object] | None = None,
    namespace_ref: dict[str, object] | None = None,
    style_index: int = 0,
    tmux_socket_path: str | None = None,
    namespace_backend_impl: str | None = None,
    expected_pane_identity: dict[str, object] | None = None,
):
    launcher = _pane_backed_launcher(spec)
    if launcher is None:
        return runtime_launch_result_cls(launched=False, binding=binding)
    # design I-3: 仅当 config 显式声明 [runtime.mux] backend = "herdr" 时，
    # 未验证 provider fail-closed（allow-list 见 _HERDR_NATIVE_VERIFIED_PROVIDERS）。
    # 自动检测（HERDR_ENV / CCB_HERDR_SESSION）触发的 herdr 后端不触发此 gate。
    _explicit_herdr = (
        _is_herdr_runtime_launch(
            namespace_backend_impl=namespace_backend_impl,
            assigned_pane_ref=assigned_pane_ref,
        )
        and os.environ.get('CCB_RUNTIME_MUX_BACKEND', '').strip() == 'herdr'
    )
    if _explicit_herdr:
        gate_error = _herdr_explicit_gate_error(spec.provider)
        if gate_error is not None:
            raise RuntimeError(gate_error)
    if _binding_is_reusable(
        binding=binding,
        assigned_pane_id=assigned_pane_id,
        binding_runtime_alive_fn=binding_runtime_alive_fn,
    ):
        return runtime_launch_result_cls(launched=False, binding=binding)
    _require_runtime_launch_tools(
        spec.provider,
        provider_executable_fn=provider_executable_fn,
        require_tmux=not _is_herdr_runtime_launch(
            namespace_backend_impl=namespace_backend_impl,
            assigned_pane_ref=assigned_pane_ref,
        ),
    )
    cleanup_stale_tmux_binding_fn(binding)

    timings_ms: dict[str, float] = {}
    launch_started_ns = monotonic_ns()
    try:
        launch_kwargs = {
            'assigned_pane_id': assigned_pane_id,
            'style_index': style_index,
            'tmux_socket_path': tmux_socket_path,
        }
        if assigned_pane_ref is not None:
            launch_kwargs['assigned_pane_ref'] = assigned_pane_ref
        if namespace_ref is not None:
            launch_kwargs['namespace_ref'] = namespace_ref
        if namespace_backend_impl is not None:
            launch_kwargs['namespace_backend_impl'] = namespace_backend_impl
        if expected_pane_identity is not None:
            launch_kwargs['expected_pane_identity'] = expected_pane_identity
        launch_timings = launch_runtime_fn(
            context,
            command,
            spec,
            plan,
            launcher,
            **launch_kwargs,
        )
    except Exception as exc:
        launch_elapsed_ms = _elapsed_ms(launch_started_ns)
        _merge_launch_timings(
            timings_ms,
            getattr(exc, 'ccb_startup_timings_ms', None),
            enclosing_elapsed_ms=launch_elapsed_ms,
        )
        _attach_startup_timings(exc, timings_ms)
        raise
    launch_elapsed_ms = _elapsed_ms(launch_started_ns)
    _merge_launch_timings(
        timings_ms,
        launch_timings,
        enclosing_elapsed_ms=launch_elapsed_ms,
    )

    binding_started_ns = monotonic_ns()
    try:
        refreshed = _resolve_refreshed_binding(
            context=context,
            spec=spec,
            plan=plan,
            resolve_agent_binding_fn=resolve_agent_binding_fn,
        )
    except Exception as exc:
        _record_elapsed_ms(timings_ms, 'binding_resolve', binding_started_ns)
        _attach_startup_timings(exc, timings_ms)
        raise
    _record_elapsed_ms(timings_ms, 'binding_resolve', binding_started_ns)
    return _runtime_launch_result(
        runtime_launch_result_cls,
        launched=True,
        binding=refreshed,
        timings_ms=timings_ms,
    )


def _pane_backed_launcher(spec):
    if spec.runtime_mode is not RuntimeMode.PANE_BACKED:
        return None
    if spec.provider not in _PANE_BACKED_RUNTIME_PROVIDERS:
        return None
    return runtime_launcher(spec.provider)


def _binding_is_reusable(
    *,
    binding,
    assigned_pane_id: str | None,
    binding_runtime_alive_fn,
) -> bool:
    if assigned_pane_id is not None or binding is None:
        return False
    if not binding.runtime_ref or not binding.session_ref:
        return False
    return bool(binding_runtime_alive_fn(binding))


def _require_runtime_launch_tools(
    provider: str,
    *,
    provider_executable_fn,
    require_tmux: bool = True,
) -> None:
    if require_tmux and shutil.which('tmux') is None:
        raise RuntimeError(f'tmux is required for pane-backed {provider} launch')
    if shutil.which(provider_executable_fn(provider)) is None:
        raise RuntimeError(f'{provider} executable not found in PATH')


def _is_herdr_runtime_launch(
    *,
    namespace_backend_impl: str | None,
    assigned_pane_ref,
) -> bool:
    """判断当前启动是否为 herdr 后端。

    优先 ``namespace_backend_impl``，其次 ``assigned_pane_ref['backend_impl']``。
    与 ``tmux_backend._is_herdr_launch`` 保持一致的检测逻辑。
    """
    if str(namespace_backend_impl or '').strip() == 'herdr':
        return True
    if isinstance(assigned_pane_ref, dict):
        return str(assigned_pane_ref.get('backend_impl') or '').strip() == 'herdr'
    return False


def _herdr_explicit_gate_error(provider: str) -> str | None:
    """显式 herdr + 未验证 provider → 返回 fail-closed 错误消息；通过返回 None。

    仅作用于显式 opt-in（``[runtime.mux] backend = "herdr"``），自动检测的
    herdr 后端不经过此 gate（design I-3）。allow-list 见
    ``_HERDR_NATIVE_VERIFIED_PROVIDERS``。
    """
    key = str(provider or '').strip().lower()
    if key in _HERDR_NATIVE_VERIFIED_PROVIDERS:
        return None
    verified = ', '.join(sorted(_HERDR_NATIVE_VERIFIED_PROVIDERS))
    return (
        f'provider {provider!r} does not support herdr-native launch; '
        f'only {verified} are verified for explicit herdr runtime. '
        f'remove [runtime.mux] backend = "herdr" from config to use tmux, '
        f'or switch to a verified provider'
    )


def _resolve_refreshed_binding(*, context, spec, plan, resolve_agent_binding_fn):
    refreshed = resolve_agent_binding_fn(
        provider=spec.provider,
        agent_name=spec.name,
        workspace_path=plan.workspace_path,
        project_root=context.project.project_root,
    )
    if refreshed is not None:
        return refreshed
    raise RuntimeError(
        f'failed to resolve usable binding for {spec.name} after {spec.provider} launch'
    )


def _runtime_launch_result(
    result_cls,
    *,
    launched: bool,
    binding,
    timings_ms: dict[str, float],
):
    try:
        return result_cls(
            launched=launched,
            binding=binding,
            timings_ms=dict(timings_ms),
        )
    except TypeError as exc:
        # Test doubles and third-party callers may still expose the historical
        # two-field result constructor. Only fall back for the additive field.
        if 'timings_ms' not in str(exc):
            raise
        return result_cls(launched=launched, binding=binding)


def _merge_launch_timings(
    target: dict[str, float],
    value: object,
    *,
    enclosing_elapsed_ms: float,
) -> None:
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
        target[field_name] = target.get(field_name, 0.0) + elapsed_ms
    residual_ms = max(0.0, enclosing_elapsed_ms - measured_ms)
    target['unattributed'] = target.get('unattributed', 0.0) + residual_ms


def _record_elapsed_ms(timings_ms: dict[str, float], field_name: str, started_ns: int) -> None:
    timings_ms[field_name] = timings_ms.get(field_name, 0.0) + _elapsed_ms(started_ns)


def _elapsed_ms(started_ns: int) -> float:
    return max(0.0, (monotonic_ns() - started_ns) / 1_000_000)


def _attach_startup_timings(exc: Exception, timings_ms: dict[str, float]) -> None:
    try:
        setattr(exc, 'ccb_startup_timings_ms', dict(timings_ms))
    except Exception:
        return


__all__ = ['ensure_agent_runtime', 'runtime_launcher']
