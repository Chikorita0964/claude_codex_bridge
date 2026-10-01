"""Local patch (ccb-team-kit): a tmux describe that times out is unknown, not a foreign pane.

A probe that never answered is not a fact about the pane. The daemon recovered live panes on
2026-09-27..10-01 because a slow `display-message` read as a missing or foreign pane, and six such
alarms open the recovery circuit that holds the agent's mailbox. These tests drive the real
TmuxBackend with a `_tmux_run` that raises `subprocess.TimeoutExpired`.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from ccbd.services.health_assessment import provider_pane as provider_pane_module
from ccbd.services.health_assessment.provider_pane import assess_provider_pane
from ccbd.services.health_assessment.tmux_runtime.state import tmux_pane_state
from ccbd.services.health_monitor_runtime.provider import provider_pane_health
from terminal_runtime.tmux_backend import TmuxBackend

PANE_ID = '%5'
DESCRIBE_STDOUT = f'{PANE_ID}\tagent1\t0\tagent1\tproj-1\n'


def _completed(*, stdout: str = '', returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=['tmux'], returncode=returncode, stdout=stdout, stderr='')


def _timed_out_backend(*, describes_before_answer: int):
    """A real TmuxBackend; its first N describes time out, then (if any) it answers."""
    remaining = {'describes': describes_before_answer}
    describe_timeouts: list[float | None] = []

    def tmux_run(args, *, capture=False, timeout=None, **kwargs):
        if args[0] == 'list-panes':
            # An answered list that does not contain the pane; only the old code reaches it.
            return _completed(stdout='')
        fmt = str(args[-1]) if args else ''
        if '#{pane_title}' in fmt:
            if remaining['describes'] > 0:
                remaining['describes'] -= 1
                describe_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(args, timeout)
            return _completed(stdout=DESCRIBE_STDOUT)
        if '#{pane_dead}' in fmt:
            return _completed(stdout='0\n')
        if '#{pane_id}' in fmt:
            return _completed(stdout=f'{PANE_ID}\n')
        return _completed(returncode=1)

    backend = TmuxBackend(socket_name='ccb-timeout-test')
    backend._tmux_run = tmux_run
    return backend, describe_timeouts


def _session(backend) -> SimpleNamespace:
    return SimpleNamespace(
        terminal='tmux',
        pane_id=PANE_ID,
        backend=lambda: backend,
        data={'agent_name': 'agent1', 'ccb_project_id': 'proj-1'},
    )


def test_existence_that_keeps_timing_out_is_unknown_never_missing() -> None:
    backend = TmuxBackend(socket_name='ccb-timeout-test')

    def tmux_run(args, *, capture=False, timeout=None, **kwargs):
        raise subprocess.TimeoutExpired(args, timeout)

    backend._tmux_run = tmux_run

    assert tmux_pane_state(_session(backend), backend, PANE_ID) == 'unknown'


def test_describe_that_keeps_timing_out_is_unknown_never_foreign() -> None:
    backend, describe_timeouts = _timed_out_backend(describes_before_answer=2)

    assert tmux_pane_state(_session(backend), backend, PANE_ID) == 'unknown'
    assert describe_timeouts == [0.5, 3.0]


def test_describe_that_answers_on_the_retry_returns_the_live_pane() -> None:
    backend, describe_timeouts = _timed_out_backend(describes_before_answer=1)

    assert tmux_pane_state(_session(backend), backend, PANE_ID) == 'alive'
    assert describe_timeouts == [0.5]


def test_provider_pane_health_keeps_the_stored_health_when_the_describe_times_out(monkeypatch) -> None:
    backend, _describe_timeouts = _timed_out_backend(describes_before_answer=2)
    session = _session(backend)
    monkeypatch.setattr(provider_pane_module, 'load_provider_session', lambda *args, **kwargs: session)

    def fail_degrade(*args, **kwargs):
        raise AssertionError('a timed-out probe must not degrade the runtime')

    monitor = SimpleNamespace(
        _assess_provider_pane=lambda **kwargs: assess_provider_pane(**kwargs),
        _registry=SimpleNamespace(spec_for=lambda agent_name: SimpleNamespace(provider='codex')),
        _session_bindings={'codex': SimpleNamespace()},
        _namespace_state_store=None,
        _mark_degraded=fail_degrade,
    )
    runtime = SimpleNamespace(
        agent_name='agent1',
        runtime_ref=f'tmux:{PANE_ID}',
        workspace_path='/tmp/workspace',
        health='pane-missing',
        pane_state='missing',
    )

    assert provider_pane_health(monitor, runtime) == 'pane-missing'
    assert runtime.health == 'pane-missing'
    assert runtime.pane_state == 'missing'
