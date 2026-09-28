"""Fixed, nonblocking input-draft guard. It never decides provider turn completion."""
from __future__ import annotations

import json
import os
import socket
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .draft_observation import Observation, inspect_screen

WAIT_SECONDS = 180.0

# Local patch (ccb-team-kit): unattended agents. CCB_DRAFT_GUARD_UNATTENDED=1 is for panes no
# human types into (a team of agents). There a "nonempty" composer is a misread, not someone's
# draft, and the attended path (wait 180 s, then Ctrl-C, then wait forever for "empty") hung
# deliveries for 15-36 min; an "unknown" reading also reset that wait on every poll, so it never
# ended. Unattended: a nonempty reading is sent after a short confirmation, without Ctrl-C; a
# "busy" status row that has not changed for STALE_BUSY_SECONDS is not a turn in progress; every
# other unknown keeps waiting (a menu or dialog may own the keys) but the wait accumulates and is
# marked draft_guard_stuck:<reason> after STUCK_MARK_SECONDS, for a supervisor to see.
UNATTENDED_GRACE_SECONDS = 30.0
STALE_BUSY_SECONDS = 300.0
STUCK_MARK_SECONDS = 300.0
_BUSY_ROW = re.compile(r'^[✢✳✶✻✽·]\s+.*(?:…|\.\.\.)')
_ANSI_ANY = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def _unattended_env() -> bool:
    return os.environ.get('CCB_DRAFT_GUARD_UNATTENDED', '').strip().lower() in {'1', 'true', 'yes', 'on'}


_LAST_RECORD: dict = {}


def record_observation(target, observation: Observation) -> None:
    """Local patch: log every non-empty composer reading (rate-limited per pane and reading) with
    the bottom of the screen and the cursor, so a hang can be diagnosed afterwards. Never raises."""
    try:
        screen = getattr(target, '_screen', None)
        if observation.state == 'empty' or not screen:  # only real pane readings
            return
        key = (observation.binding, observation.state, observation.reason)
        now = time.time()
        if now - _LAST_RECORD.get(key, 0.0) < 60.0:
            return
        _LAST_RECORD[key] = now
        lines = _ANSI_ANY.sub('', str(screen.get('text') or '')).split('\n')
        path = Path(os.environ.get('CCB_DRAFT_GUARD_LOG') or Path.home() / '.cache' / 'ccb' / 'draft-guard.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_suffix('.jsonl.1'))
        rec = {'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'provider': getattr(target, 'provider', ''),
               'pane': getattr(target, 'pane_id', ''), 'binding': observation.binding,
               'state': observation.state, 'reason': observation.reason,
               'cursor': [screen.get('cursor_x'), screen.get('cursor_y')], 'rows': len(lines),
               'bottom': lines[-14:]}
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


@dataclass
class DraftGuard:
    clock: Callable[[], float] = time.monotonic
    binding: str = ''
    since: float | None = None
    clear_attempted: bool = False
    reason: str = 'unobserved'
    unattended: bool = field(default_factory=_unattended_env)
    blocked_since: float | None = None
    busy_row: str = ''
    busy_since: float | None = None

    def reset(self, reason: str) -> None:
        self.since = None
        self.reason = reason

    def allows(self, target: 'DraftTarget') -> bool:
        if self.unattended:
            return self._allows_unattended(target)
        observation = target.observe()
        record_observation(target, observation)
        if observation.state not in {'empty', 'nonempty'}:
            self.reset(observation.reason)
            return False
        if self.binding != observation.binding:
            self.binding = observation.binding
            self.since = None
            self.clear_attempted = False
        self.reason = observation.reason
        if observation.state == 'empty':
            self.since = None
            self.clear_attempted = False
            return True
        if observation.state != 'nonempty':
            self.reset(observation.reason)
            return False
        if self.clear_attempted:
            self.reason = 'clear_unconfirmed'
            return False
        if self.since is None:
            self.since = self.clock()
        if self.clock() - self.since < WAIT_SECONDS:
            self.reason = 'waiting_for_draft'
            return False
        # Recheck at the destructive boundary, including pane generation/busy.
        fresh = target.observe()
        if fresh.binding != self.binding or fresh.state != 'nonempty':
            self.reset('draft_changed_before_clear')
            return False
        self.clear_attempted = True
        try:
            target.clear(fresh)
        except Exception:
            self.reason = 'clear_failed'
            return False
        # Rendering is asynchronous. Subsequent normal polls may confirm empty;
        # never send another clear just because the first readback is delayed.
        after = target.observe()
        if after.binding == self.binding and after.state == 'empty':
            self.since = None
            self.clear_attempted = False
            self.reason = 'cleared'
            return True
        self.reason = 'clear_unconfirmed'
        return False

    def _allows_unattended(self, target) -> bool:
        observation = target.observe()
        record_observation(target, observation)
        now = self.clock()
        if self.binding != observation.binding:
            self.binding = observation.binding
            self.blocked_since = self.busy_since = None
            self.busy_row = ''
        if observation.state == 'empty':
            self.blocked_since = self.busy_since = None
            self.busy_row = ''
            self.reason = observation.reason
            return True
        if observation.state == 'unknown' and observation.reason == 'provider_busy':
            row = _busy_row(target)
            if row != self.busy_row:
                self.busy_row, self.busy_since = row, now
            if self.busy_since is not None and now - self.busy_since >= STALE_BUSY_SECONDS:
                # the status row has not moved: an old row left on screen, not a turn in progress
                second = _inspect_without_busy_rows(target, observation.binding)
                if second is not None and second.state in {'empty', 'nonempty'}:
                    observation = second
            if observation.reason == 'provider_busy':
                self.blocked_since = None
                self.reason = 'provider_busy'
                return False
        if self.blocked_since is None:
            self.blocked_since = now
        waited = now - self.blocked_since
        if observation.state == 'empty':
            self.blocked_since = None
            self.reason = observation.reason
            return True
        if observation.state == 'nonempty':
            if waited >= UNATTENDED_GRACE_SECONDS:
                self.blocked_since = None
                self.reason = f'unattended_send:{observation.reason}'
                return True
            self.reason = 'unattended_confirming'
            return False
        self.reason = (f'draft_guard_stuck:{observation.reason}' if waited >= STUCK_MARK_SECONDS
                       else observation.reason)
        return False


def _busy_row(target) -> str:
    screen = getattr(target, '_screen', None) or {}
    lines = _ANSI_ANY.sub('', str(screen.get('text') or '')).split('\n')
    rows = [line for line in lines if _BUSY_ROW.match(line)]
    return rows[-1] if rows else ''


def _inspect_without_busy_rows(target, binding: str) -> Observation | None:
    screen = getattr(target, '_screen', None)
    provider = getattr(target, 'provider', '')
    if not screen or not provider:
        return None
    text = '\n'.join('' if _BUSY_ROW.match(_ANSI_ANY.sub('', line)) else line
                     for line in str(screen['text']).split('\n'))
    return inspect_screen(provider, dict(screen, text=text), binding=binding)


@dataclass
class DraftTarget:
    provider: str
    backend: object
    pane_id: str
    generation: str = ''
    editor_socket: str = ''
    actor: str = ''
    launch_id: str = ''
    _native: dict = field(default_factory=dict, repr=False)

    def observe(self) -> Observation:
        try:
            screen = self.backend.capture_composer(self.pane_id)
            self._screen = screen  # local patch: kept for diagnostics and the unattended guard
            binding = f'{self.generation}:{screen["binding"]}'
            if screen.get('blocked'):
                return Observation('unknown', binding, 'pane_unavailable_or_in_mode')
            if self.provider != 'omp':
                return inspect_screen(self.provider, screen, binding=binding)
            # The native editor API reads the underlying buffer even when a
            # modal owns keyboard focus. v1 qualifies the default Status Band
            # surface; alternate layouts remain unknown rather than sending
            # keys into a picker. Text emptiness itself still comes from OMP.
            lines = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', screen['text']).splitlines()
            editors = [i for i, line in enumerate(lines) if re.match(r'^╰─(?: |$)', line)]
            if not editors or screen['cursor_y'] < editors[-1]:
                return Observation('unknown', binding, 'omp_editor_not_focused')
            data = self._editor_request('inspect')
            self._native = data
            binding += ':' + str(data.get('runtime_instance_id', '')) + ':' + str(data.get('session_id', ''))
            state = data.get('state')
            if state not in {'empty', 'nonempty'}:
                state = 'unknown'
            return Observation(state, binding, str(data.get('reason') or state))
        except (ConnectionRefusedError, FileNotFoundError):
            return Observation('unknown', self.generation, 'composer_bridge_unavailable')
        except Exception:
            return Observation('unknown', self.generation, 'composer_unavailable')

    def _editor_request(self, operation: str, **extra) -> dict:
        if not self.editor_socket:
            raise ValueError('OMP editor bridge unavailable')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(self.editor_socket)
            client.sendall((json.dumps({'operation': operation, 'actor': self.actor,
                                      'launch_session_id': self.launch_id, **extra})+'\n').encode())
            response = b''
            while b'\n' not in response and len(response) < 4096:
                part = client.recv(4096)
                if not part:
                    break
                response += part
            return json.loads(response)

    def clear(self, observation: Observation) -> None:
        if self.provider == 'omp':
            self._editor_request('clear', runtime_instance_id=self._native.get('runtime_instance_id'),
                                 session_id=self._native.get('session_id'))
        elif self.backend.send_key(self.pane_id, 'C-c') is False:
            raise RuntimeError('clear key failed')


def guarded_backend(backend: object) -> bool:
    return callable(getattr(backend, 'capture_composer', None))


def target_for_state(provider: str, state: dict) -> DraftTarget:
    return DraftTarget(provider, state.get('backend'), str(state.get('pane_id') or ''),
                       str(state.get('draft_guard_generation') or state.get('launch_session_id') or ''),
                       str(state.get('draft_guard_socket') or ''), str(state.get('actor') or ''),
                       str(state.get('launch_session_id') or ''))


def allow_submission_send(provider: str, state: dict) -> bool:
    if not state.get('draft_guard_enabled'):
        return True
    guard = state.get('_draft_guard')
    if not isinstance(guard, DraftGuard):
        guard = DraftGuard()
        state['_draft_guard'] = guard
    allowed = guard.allows(target_for_state(provider, state))
    state['draft_guard_pending'] = not allowed
    state['draft_guard_reason'] = guard.reason
    return allowed


def guarded_send(state: dict, sender) -> None:
    """An ambiguous terminal write is never retried as an unsent prompt."""
    state['prompt_sent'] = True
    state['draft_guard_pending'] = False
    try:
        sender()
    except Exception as exc:
        state['draft_guard_send_unknown'] = True
        state['draft_guard_send_error'] = type(exc).__name__


def send_unknown_result(submission, *, now):
    from completion.models import CompletionDecision, CompletionStatus, CompletionConfidence
    from .base import ProviderPollResult
    if not submission.runtime_state.get('draft_guard_send_unknown'):
        return None
    return ProviderPollResult(submission=submission, decision=CompletionDecision(
        terminal=True, status=CompletionStatus.FAILED, reason='draft_guard_send_unknown',
        confidence=CompletionConfidence.DEGRADED, reply='', anchor_seen=False,
        reply_started=False, reply_stable=False, provider_turn_ref=None,
        source_cursor=None, finished_at=now,
        diagnostics={'error_type': 'terminal_send_unknown', 'automatic_retry': False},
    ))


def initial_guard_state(provider: str, backend: object, data: dict) -> dict:
    enabled = guarded_backend(backend)
    # An older OMP launcher has no bridge. Do not silently infer native support.
    if provider == 'omp':
        enabled = enabled and data.get('omp_draft_guard_version') == 1
    return {'draft_guard_enabled': enabled,
            'draft_guard_generation': str(data.get('ccb_session_id') or ''),
            'draft_guard_socket': str(data.get('omp_draft_guard_socket') or ''),
            'draft_guard_reason': 'pending' if enabled else 'unprotected_transport'}


def resolve_job_target(job, context) -> DraftTarget | None:
    """Resolve a current managed binding without activating/restarting a pane."""
    if job.provider not in {'codex', 'claude', 'omp'} or context is None:
        return None
    if context.backend_type in {'headless', 'pty-backed'}:
        return None
    if not context.workspace_path:
        raise ValueError('missing workspace')
    from importlib import import_module
    from provider_core.instance_resolution import named_agent_instance
    from terminal_runtime import get_backend_for_session, get_pane_id_from_session

    finder = import_module(f'provider_backends.{job.provider}.session').find_project_session_file
    name = str(getattr(job, 'provider_instance', None) or job.agent_name or job.provider)
    session_file = finder(Path(context.workspace_path), instance=named_agent_instance(name, primary_agent=job.provider))
    if session_file is None:
        raise ValueError('missing managed binding')
    # Session loaders may migrate layouts or write bindings. Pre-claim polling
    # must remain read-only, including while a human is editing the composer.
    data = json.loads(session_file.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('invalid managed binding')
    backend = get_backend_for_session(data)
    config = initial_guard_state(job.provider, backend, data)
    if not config['draft_guard_enabled']:
        return None
    pane = get_pane_id_from_session(data)
    if not pane:
        raise ValueError('missing pane')
    return DraftTarget(job.provider, backend, pane, config['draft_guard_generation'],
                       config['draft_guard_socket'], job.agent_name,
                       str(data.get('ccb_session_id') or ''))
