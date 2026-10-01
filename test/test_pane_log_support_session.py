from __future__ import annotations

from types import SimpleNamespace

import pytest

from provider_backends.pane_log_support import lifecycle
from provider_backends.pane_log_support import session as pane_session
from provider_runtime.pane_recovery_outcome import LIVE_OWNED, RESPAWN, pane_recovery_outcome
from terminal_runtime.mux_backend_contract import MuxCommandErrorV2


def test_compute_session_key_for_provider_uses_bound_project_id_and_scope(monkeypatch) -> None:
    monkeypatch.setattr(pane_session, 'compute_worktree_scope_id', lambda _path: 'scope-7')
    session = SimpleNamespace(data={'ccb_project_id': 'proj-1'}, work_dir='/tmp/demo')

    key = pane_session.compute_session_key_for_provider(
        session,
        provider='codex',
        instance='agent2',
    )

    assert key == 'codex:agent2:proj-1:scope-7'


def test_compute_session_key_for_provider_falls_back_to_unknown_project(monkeypatch) -> None:
    def _raise(_path):
        raise RuntimeError('no project')

    monkeypatch.setattr(pane_session, 'compute_ccb_project_id', _raise)
    monkeypatch.setattr(pane_session, 'compute_worktree_scope_id', lambda _path: 'scope-7')
    session = SimpleNamespace(data={}, work_dir='/tmp/demo')

    key = pane_session.compute_session_key_for_provider(session, provider='codex')

    assert key == 'codex:unknown:scope-7'


class _HerdrBackend:
    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.is_alive_calls: list[object] = []
        self.capture_calls: list[object] = []
        self.ensure_log_calls: list[object] = []
        self.respawn_calls: list[object] = []

    def is_alive(self, pane) -> bool:
        self.is_alive_calls.append(pane)
        return self.alive

    def capture_pane(self, pane, *, lines: int):
        self.capture_calls.append((pane, lines))
        return '', {'operation': 'capture_pane', 'backend_impl': 'herdr', 'pane_id': pane['pane_id']}

    def ensure_pane_log(self, pane) -> None:
        self.ensure_log_calls.append(pane)

    def respawn_pane(self, *args, **kwargs) -> None:
        self.respawn_calls.append((args, kwargs))
        raise AssertionError('Herdr ensure_pane must not use tmux rebound respawn')


class _UnsupportedHerdrBackend(_HerdrBackend):
    def is_alive(self, pane) -> bool:
        self.is_alive_calls.append(pane)
        raise MuxCommandErrorV2(
            category='unsupported',
            backend_impl='herdr',
            operation='capture_pane',
            detail='capture_pane capability is unsupported',
        )


def _herdr_session(tmp_path, backend):
    pane_ref = {
        'backend_impl': 'herdr',
        'pane_id': 'pane-1',
        'session_name': 'ccb-demo',
        'window_name': 'main',
        'agent_slug': 'agent1',
    }
    return pane_session.PaneLogProjectSessionBase(
        session_file=tmp_path / '.codex-session',
        data={
            'ccb_session_id': 'ccb-agent1-1',
            'agent_name': 'agent1',
            'terminal': 'mux',
            'backend_impl': 'herdr',
            'pane_id': 'pane-1',
            'pane_ref': pane_ref,
            'runtime_dir': str(tmp_path),
            'work_dir': str(tmp_path),
            'active': True,
        },
    ), pane_ref


def test_herdr_ensure_pane_uses_pane_ref_liveness_and_skips_tmux_ownership(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _HerdrBackend(alive=True)
    sess, pane_ref = _herdr_session(tmp_path, backend)
    sess.backend = lambda: backend  # type: ignore[method-assign]

    def _fail_tmux_ownership(*args, **kwargs):
        raise AssertionError('Herdr ensure_pane must not inspect tmux ownership')

    monkeypatch.setattr(lifecycle, 'inspect_tmux_pane_ownership', _fail_tmux_ownership)

    ok, pane = sess.ensure_pane()

    assert (ok, pane) == (True, 'pane-1')
    assert backend.is_alive_calls == [pane_ref]
    assert backend.ensure_log_calls == [pane_ref]
    assert backend.capture_calls == []
    assert backend.respawn_calls == []


class _SpawnableBackend:
    """A tmux-compatible backend whose one pane is either alive or gone."""

    def __init__(self, *, alive: bool) -> None:
        self.alive = alive
        self.respawned: list[str] = []

    def is_alive(self, pane) -> bool:
        return self.alive

    def pane_exists(self, pane) -> bool:
        return True

    def respawn_pane(self, pane, *, cmd: str, cwd: str | None = None, remain_on_exit: bool = True) -> None:
        self.respawned.append(str(pane))
        self.alive = True


def _tmux_session(tmp_path, backend, *, pane_id: str = '%1'):
    return SimpleNamespace(
        pane_id=pane_id,
        terminal='tmux',
        data={
            'agent_name': 'agent1',
            'terminal': 'tmux',
            'pane_id': pane_id,
            'start_cmd': 'codex --continue',
            'work_dir': str(tmp_path),
            'runtime_dir': str(tmp_path),
        },
        work_dir=str(tmp_path),
        runtime_dir=tmp_path,
        start_cmd='codex --continue',
        backend=lambda: backend,
        user_option_lookup=lambda: {},
        _write_back=lambda: None,
    )


def test_tmux_ensure_pane_publishes_live_owned_for_a_pane_it_leaves_alone(tmp_path) -> None:
    backend = _SpawnableBackend(alive=True)
    session = _tmux_session(tmp_path, backend)

    assert lifecycle.ensure_pane(
        session,
        now_str_fn=lambda: 'now',
        attach_pane_log_fn=lambda *args: None,
    ) == (True, '%1')
    assert backend.respawned == []
    assert pane_recovery_outcome(session) == LIVE_OWNED


def test_tmux_ensure_pane_publishes_respawn_for_the_pane_it_respawned(tmp_path) -> None:
    """The same pane id comes back, so the id cannot say whether anything was restarted - the
    outcome the recovery gates on must."""
    backend = _SpawnableBackend(alive=False)
    session = _tmux_session(tmp_path, backend)

    assert lifecycle.ensure_pane(
        session,
        now_str_fn=lambda: 'now',
        attach_pane_log_fn=lambda *args: None,
    ) == (True, '%1')
    assert backend.respawned == ['%1']
    assert session.pane_id == '%1'
    assert pane_recovery_outcome(session) == RESPAWN


def test_herdr_ensure_pane_reports_actionable_unsupported_liveness(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _UnsupportedHerdrBackend(alive=False)
    sess, pane_ref = _herdr_session(tmp_path, backend)
    sess.backend = lambda: backend  # type: ignore[method-assign]

    def _fail_tmux_ownership(*args, **kwargs):
        raise AssertionError('Herdr ensure_pane must not inspect tmux ownership')

    monkeypatch.setattr(lifecycle, 'inspect_tmux_pane_ownership', _fail_tmux_ownership)

    ok, detail = sess.ensure_pane()

    assert ok is False
    assert 'Herdr backend unsupported' in detail
    assert 'capture_pane capability is unsupported' in detail
    assert backend.is_alive_calls == [pane_ref]
    assert backend.ensure_log_calls == []
    assert backend.respawn_calls == []
