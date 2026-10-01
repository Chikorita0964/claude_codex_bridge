from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.services.runtime_launch_runtime import tmux_panes


class FakeBackend:
    def __init__(self, *, socket_path: str | None = None, socket_name: str | None = None, returncode: int = 0) -> None:
        self.socket_path = socket_path
        self.socket_name = socket_name
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []
        self.respawned: list[tuple[str, str, str | None]] = []

    def _tmux_run(self, argv, **kwargs):
        del kwargs
        self.calls.append(tuple(argv))
        stdout = '%7\n' if tuple(argv[:1]) == ('list-panes',) else ''
        return subprocess.CompletedProcess(args=argv, returncode=self.returncode, stdout=stdout, stderr='')

    def respawn_pane(self, pane_id: str, *, cmd: str, cwd: str | None = None, remain_on_exit: bool = True) -> None:
        del remain_on_exit
        self.respawned.append((pane_id, cmd, cwd))


def _pane_identity_line(
    *,
    pane_id: str = '%7',
    slot: str = 'worker1',
    window: str = 'pair1',
    epoch: int = 7,
    dead: str = '0',
) -> str:
    """One line in the 21-field format `inspect_project_namespace_pane` asks tmux for."""
    fields = (
        pane_id,
        'ccb-demo',
        '@1',
        window,
        dead,
        'agent',
        slot,
        window,
        '',
        '',
        'proj-1',
        'ccbd',
        str(epoch),
        '',
        slot,
        '',
        '',
        '',
        '',
        '',
        '',
    )
    return '\t'.join(fields) + '\n'


class PaneIdentityBackend:
    """Fake backend whose display-message answers the pane identity probe."""

    def __init__(self, *, line: str | None, returncode: int = 0) -> None:
        self.line = line
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []
        self.respawned: list[tuple[str, str, str | None]] = []

    def _tmux_run(self, argv, **kwargs):
        del kwargs
        self.calls.append(tuple(argv))
        stdout = (self.line or '') if self.returncode == 0 else ''
        return subprocess.CompletedProcess(args=argv, returncode=self.returncode, stdout=stdout, stderr='')

    def respawn_pane(self, pane_id: str, *, cmd: str, cwd: str | None = None, remain_on_exit: bool = True) -> None:
        del remain_on_exit
        self.respawned.append((pane_id, cmd, cwd))


def _launch_assigned_pane(backend, tmp_path: Path, *, expected_pane_identity=None) -> str:
    return tmux_panes.launch_pane(
        backend,
        spec_name='worker1',
        assigned_pane_id='%7',
        start_cmd='codex',
        run_cwd=tmp_path,
        create_detached_tmux_pane_fn=lambda *args, **kwargs: pytest.fail('unexpected detached pane'),
        pane_meets_minimum_size_fn=lambda *args, **kwargs: True,
        best_effort_kill_tmux_pane_fn=lambda *args, **kwargs: None,
        allow_detached_fallback=False,
        expected_pane_identity=expected_pane_identity,
    )


def test_launch_pane_respawns_assigned_pane_whose_identity_matches(tmp_path: Path) -> None:
    backend = PaneIdentityBackend(
        line=_pane_identity_line(slot='worker1', window='pair1', epoch=7),
    )

    pane_id = _launch_assigned_pane(
        backend,
        tmp_path,
        expected_pane_identity={'slot': 'worker1', 'window': 'pair1', 'epoch': 7},
    )

    assert pane_id == '%7'
    assert backend.respawned == [('%7', 'codex', str(tmp_path))]


def test_launch_pane_refuses_assigned_pane_owned_by_another_agent(tmp_path: Path) -> None:
    backend = PaneIdentityBackend(
        line=_pane_identity_line(slot='worker2', window='pair2', epoch=7),
    )

    with pytest.raises(RuntimeError, match='no longer belongs to it'):
        _launch_assigned_pane(
            backend,
            tmp_path,
            expected_pane_identity={'slot': 'worker1', 'window': 'pair1', 'epoch': 7},
        )

    assert backend.respawned == []


def test_launch_pane_refuses_assigned_pane_from_an_older_namespace_epoch(tmp_path: Path) -> None:
    backend = PaneIdentityBackend(
        line=_pane_identity_line(slot='worker1', window='pair1', epoch=6),
    )

    with pytest.raises(RuntimeError, match='namespace_epoch=6 expected 7'):
        _launch_assigned_pane(
            backend,
            tmp_path,
            expected_pane_identity={'slot': 'worker1', 'window': 'pair1', 'epoch': 7},
        )

    assert backend.respawned == []


def test_launch_pane_refuses_assigned_pane_that_is_gone(tmp_path: Path) -> None:
    backend = PaneIdentityBackend(line=None, returncode=1)

    with pytest.raises(RuntimeError, match='is gone'):
        _launch_assigned_pane(
            backend,
            tmp_path,
            expected_pane_identity={'slot': 'worker1', 'window': 'pair1', 'epoch': 7},
        )

    assert backend.respawned == []


def test_launch_pane_refuses_assigned_pane_that_does_not_answer_the_probe(tmp_path: Path) -> None:
    class SilentBackend:
        def __init__(self) -> None:
            self.respawned: list[object] = []

        def describe_pane(self, pane_id, *, user_options=()):
            del user_options
            raise subprocess.TimeoutExpired(cmd=['tmux', 'display-message'], timeout=0.5)

        def respawn_pane(self, pane_id, *, cmd, cwd=None, remain_on_exit=True) -> None:
            del cmd, cwd, remain_on_exit
            self.respawned.append(pane_id)

    backend = SilentBackend()

    with pytest.raises(RuntimeError, match='did not answer an identity probe'):
        _launch_assigned_pane(
            backend,
            tmp_path,
            expected_pane_identity={'slot': 'worker1', 'window': 'pair1', 'epoch': 7},
        )

    assert backend.respawned == []


def test_launch_pane_skips_the_identity_probe_without_expectations(tmp_path: Path) -> None:
    backend = FakeBackend()

    pane_id = _launch_assigned_pane(backend, tmp_path)

    assert pane_id == '%7'
    assert backend.respawned == [('%7', 'codex', str(tmp_path))]
    assert not any(call[:1] == ('display-message',) for call in backend.calls)


def setup_function() -> None:
    tmux_panes._PREPARED_DETACHED_TMUX_SERVER_KEYS.clear()


def test_prepare_detached_tmux_server_reuses_same_socket_and_environment(monkeypatch) -> None:
    monkeypatch.setenv('DISPLAY', ':1')
    backend = FakeBackend(socket_path='/tmp/ccb.sock')

    tmux_panes.prepare_detached_tmux_server(backend)
    first_count = len(backend.calls)
    tmux_panes.prepare_detached_tmux_server(backend)

    assert first_count > 0
    assert len(backend.calls) == first_count
    assert ('start-server',) not in backend.calls


def test_prepare_detached_tmux_server_does_not_share_different_sockets(monkeypatch) -> None:
    monkeypatch.setenv('DISPLAY', ':1')
    first = FakeBackend(socket_path='/tmp/ccb-a.sock')
    second = FakeBackend(socket_path='/tmp/ccb-b.sock')

    tmux_panes.prepare_detached_tmux_server(first)
    tmux_panes.prepare_detached_tmux_server(second)

    assert first.calls
    assert second.calls


def test_prepare_detached_tmux_server_refreshes_when_environment_changes(monkeypatch) -> None:
    backend = FakeBackend(socket_path='/tmp/ccb.sock')
    monkeypatch.setenv('DISPLAY', ':1')
    tmux_panes.prepare_detached_tmux_server(backend)
    first_count = len(backend.calls)

    monkeypatch.setenv('DISPLAY', ':2')
    tmux_panes.prepare_detached_tmux_server(backend)

    assert len(backend.calls) > first_count


def test_prepare_detached_tmux_server_retries_after_failed_prepare(monkeypatch) -> None:
    monkeypatch.setenv('DISPLAY', ':1')
    backend = FakeBackend(socket_path='/tmp/ccb.sock', returncode=1)

    tmux_panes.prepare_detached_tmux_server(backend)
    first_count = len(backend.calls)
    tmux_panes.prepare_detached_tmux_server(backend)

    assert first_count > 0
    assert len(backend.calls) == first_count * 2


def test_create_detached_tmux_pane_creates_session_before_server_policy(tmp_path: Path) -> None:
    backend = FakeBackend(socket_path='/tmp/ccb.sock')

    pane_id = tmux_panes.create_detached_tmux_pane(
        backend,
        cmd='codex',
        cwd=tmp_path,
        session_name='ccb-agent1',
    )

    assert pane_id == '%7'
    assert backend.calls[0][:1] == ('new-session',)
    assert ('start-server',) not in backend.calls
    policy_index = backend.calls.index(('set-option', '-g', 'destroy-unattached', 'off'))
    assert policy_index > 0
    assert backend.respawned == [('%7', 'codex', str(tmp_path))]
