from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from terminal_runtime.shell_launch import (
    herdr_respawn_command,
    resolve_sh_executable,
    sh_quote,
)

from .tmux_panes import launch_pane


def _resolve_sh_exe() -> str | None:
    """Windows 下 PowerShell pane 无 `sh` 命令；解析 Git Bash 的 sh.exe 完整路径。"""
    return resolve_sh_executable()


def _herdr_launch_command(start_cmd: str, run_cwd: Path, spec_name: str) -> list[str]:
    """构造 herdr respawn 的启动命令 argv：PowerShell 调 sh.exe 执行 bash 脚本。

    herdr pane 的 shell 是 PowerShell，codex 启动命令是 bash 语法（export/codex）。
    直接注入会触发 PowerShell ReadLine/Parser 错误（无 sh 命令、嵌套引号解析）。
    共享实现见 terminal_runtime.shell_launch.herdr_respawn_command。
    """
    return herdr_respawn_command(start_cmd, run_cwd, spec_name)


def _sh_quote(value: str) -> str:
    return sh_quote(value)


def launch_runtime_pane(
    backend,
    *,
    spec_name: str,
    assigned_pane_id: str | None,
    assigned_pane_ref: Mapping[str, object] | None,
    start_cmd: str,
    run_cwd: Path,
    create_detached_tmux_pane_fn,
    pane_meets_minimum_size_fn,
    best_effort_kill_tmux_pane_fn,
    allow_detached_fallback: bool,
    expected_pane_identity: Mapping[str, object] | None = None,
):
    if _is_herdr_pane_ref(assigned_pane_ref):
        pane_ref = dict(assigned_pane_ref or {})
        backend.respawn_pane(
            pane_ref,
            command=_herdr_launch_command(start_cmd, run_cwd, spec_name),
            cwd=str(run_cwd),
            env={},
        )
        _best_effort_capture_pane(backend, pane_ref)
        # 方向 A：respawn 后回填 pane 前台进程 pid，供 runtime_pid 使用。
        # codex/claude 经 sh.exe 启动进 pane，CCB 不经 provider session 跟踪其
        # pid；用 herdr `pane process-info` 拿前台进程，best-effort 不阻塞。
        _best_effort_pane_runtime_pid(backend, pane_ref)
        return pane_ref
    return launch_pane(
        backend,
        spec_name=spec_name,
        assigned_pane_id=assigned_pane_id,
        start_cmd=start_cmd,
        run_cwd=run_cwd,
        create_detached_tmux_pane_fn=create_detached_tmux_pane_fn,
        pane_meets_minimum_size_fn=pane_meets_minimum_size_fn,
        best_effort_kill_tmux_pane_fn=best_effort_kill_tmux_pane_fn,
        allow_detached_fallback=allow_detached_fallback,
        expected_pane_identity=expected_pane_identity,
    )


def pane_runtime_id(pane) -> str:
    if isinstance(pane, Mapping):
        return str(pane.get('pane_id') or '').strip()
    return str(pane or '').strip()


def _is_herdr_pane_ref(value) -> bool:
    return isinstance(value, Mapping) and str(value.get('backend_impl') or '').strip() == 'herdr'


def _best_effort_capture_pane(backend, pane_ref: Mapping[str, object]) -> None:
    capture = getattr(backend, 'capture_pane', None)
    if not callable(capture):
        return
    try:
        capture(dict(pane_ref), lines=1)
    except Exception:
        return


def _best_effort_pane_runtime_pid(backend, pane_ref: Mapping[str, object]) -> None:
    """Best-effort: query the pane's foreground process pid and record it.

    Pane-backed (herdr) agents respawn the provider CLI into the pane; CCB does
    not track its pid via a provider session.  ``pane process-info`` exposes the
    foreground pid — record it on the pane ref so downstream can backfill
    ``runtime_pid``.  Never raises; a transient failure simply leaves pid unset.
    """
    process_info = getattr(backend, 'pane_process_info', None)
    if not callable(process_info):
        return
    try:
        info = process_info(dict(pane_ref))
    except Exception:
        return
    if not isinstance(info, dict):
        return
    pid = info.get('foreground_pid')
    if pid:
        try:
            pane_ref['runtime_pid'] = int(pid)
        except (TypeError, ValueError):
            return


__all__ = [
    'launch_runtime_pane',
    'pane_runtime_id',
]
