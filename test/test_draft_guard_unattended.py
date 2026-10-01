"""Local patch (ccb-team-kit): the unattended draft guard (CCB_DRAFT_GUARD_UNATTENDED=1)."""
from __future__ import annotations

import json

import pytest

from provider_execution import draft_guard
from provider_execution.draft_guard import (DraftGuard, STALE_BUSY_SECONDS, STUCK_MARK_SECONDS,
                                            UNATTENDED_GRACE_SECONDS)
from provider_execution.draft_observation import Observation, inspect_screen

BORDER = '─' * 40


def claude_screen(*, busy_row: str = '', draft: str = '') -> dict:
    lines = ['● earlier output', busy_row, BORDER, '❯ ' + draft, BORDER, '  ⏵⏵ bypass permissions on']
    return {'text': '\n'.join(lines), 'cursor_x': 2 + len(draft), 'cursor_y': 3, 'binding': 'pane'}


OWN_ANCHOR = 'job-1'
OWN_PROMPT = 'CCB_REQ_ID: job-1\n\nRun the verification and report back.'


class Target:
    """Scripted readings; clear() must never be called in unattended mode."""

    provider = 'claude'

    def __init__(self, state='nonempty', reason='claude_draft', content=''):
        self.state, self.reason, self.binding, self.clears = state, reason, 'pane-1', 0
        self.content = content

    def observe(self):
        return Observation(self.state, self.binding, self.reason, self.content)

    def clear(self, observation):
        self.clears += 1


class ScreenTarget:
    """Real inspect_screen over a synthetic Claude pane."""
    provider = 'claude'
    pane_id = '%1'

    def __init__(self, screen):
        self._screen = screen

    def observe(self):
        return inspect_screen('claude', self._screen, binding='gen:pane')

    def clear(self, observation):
        raise AssertionError('unattended guard must not clear')


@pytest.fixture(autouse=True)
def log_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv('CCB_DRAFT_GUARD_LOG', str(tmp_path / 'draft-guard.jsonl'))
    monkeypatch.setattr(draft_guard, '_LAST_RECORD', {})  # the one-per-minute limit is per process
    return tmp_path / 'draft-guard.jsonl'


def guard_at(now):
    return DraftGuard(clock=lambda: now[0], unattended=True,
                      job_anchor=OWN_ANCHOR, job_prompt=OWN_PROMPT)


def test_default_stays_attended(monkeypatch):
    monkeypatch.delenv('CCB_DRAFT_GUARD_UNATTENDED', raising=False)
    assert DraftGuard().unattended is False
    monkeypatch.setenv('CCB_DRAFT_GUARD_UNATTENDED', '1')
    assert DraftGuard().unattended is True


def test_empty_sends_at_once():
    now = [0.0]
    assert guard_at(now).allows(Target('empty', 'claude_blank'))


def test_nonempty_is_sent_after_the_grace_without_ctrl_c():
    now, target = [0.0], Target(content=OWN_PROMPT)  # the box holds this job's own prompt
    guard = guard_at(now)
    assert not guard.allows(target) and guard.reason == 'unattended_confirming'
    now[0] = UNATTENDED_GRACE_SECONDS - 0.1
    assert not guard.allows(target)
    now[0] = UNATTENDED_GRACE_SECONDS
    assert guard.allows(target) and guard.reason == 'unattended_send:claude_draft'
    assert target.clears == 0


def test_a_foreign_composer_is_never_pasted_over():
    # A leftover prompt from another (e.g. cancelled) job: pasting on top merges two
    # anchors into one user prompt, so the guard waits instead and marks it for the watch.
    now, target = [0.0], Target(content='first task body for the cancelled job')
    guard = guard_at(now)
    for instant in (0, UNATTENDED_GRACE_SECONDS - 0.1):
        now[0] = instant
        assert not guard.allows(target)
    now[0] = UNATTENDED_GRACE_SECONDS
    assert not guard.allows(target) and guard.reason == 'draft_guard_foreign_text'
    now[0] = STUCK_MARK_SECONDS
    assert not guard.allows(target) and guard.reason == 'draft_guard_stuck:claude_draft'
    assert target.clears == 0  # unattended mode never sends a clear key


def test_own_prompt_tail_is_still_sent_after_the_grace():
    now, target = [0.0], Target(content=OWN_PROMPT[-24:])
    guard = guard_at(now)
    assert not guard.allows(target) and guard.reason == 'unattended_confirming'
    now[0] = UNATTENDED_GRACE_SECONDS
    assert guard.allows(target) and guard.reason == 'unattended_send:claude_draft'


def test_a_provider_that_hides_composer_text_keeps_the_grace_send():
    # The OMP editor bridge never returns draft contents, so nothing can be claimed
    # or refused there; the unattended grace send stays as it was.
    now, target = [0.0], Target(content='')
    target.provider = 'omp'
    guard = guard_at(now)
    assert not guard.allows(target)
    now[0] = UNATTENDED_GRACE_SECONDS
    assert guard.allows(target) and guard.reason == 'unattended_send:claude_draft'


def test_unknown_does_not_restart_the_wait():
    now, target = [0.0], Target(content=OWN_PROMPT)
    guard = guard_at(now)
    assert not guard.allows(target)
    now[0], target.state, target.reason = 10, 'unknown', 'composer_layout_unknown'
    assert not guard.allows(target)
    now[0], target.state, target.reason = UNATTENDED_GRACE_SECONDS, 'nonempty', 'claude_draft'
    assert guard.allows(target)  # the wait counted from the first reading, through the unknown one


def test_other_unknown_never_sends_but_is_marked_stuck():
    now, target = [0.0], Target('unknown', 'composer_not_focused')
    guard = guard_at(now)
    for instant in (0, 60, STUCK_MARK_SECONDS - 1):
        now[0] = instant
        assert not guard.allows(target)
        assert guard.reason == 'composer_not_focused'
    now[0] = STUCK_MARK_SECONDS
    assert not guard.allows(target)
    assert guard.reason == 'draft_guard_stuck:composer_not_focused'
    assert target.clears == 0


def test_a_moving_busy_row_is_a_turn_in_progress():
    now = [0.0]
    guard = guard_at(now)
    for i, instant in enumerate(range(0, int(STALE_BUSY_SECONDS) * 3, 60)):
        now[0] = instant
        target = ScreenTarget(claude_screen(busy_row=f'✻ Processing… ({i}m 0s · ↓ {i}k tokens)'))
        assert not guard.allows(target)
        assert guard.reason == 'provider_busy'


def test_a_frozen_busy_row_is_not_a_turn():
    now = [0.0]
    guard = guard_at(now)
    target = ScreenTarget(claude_screen(busy_row='✻ Processing… (3m 1s · ↓ 12k tokens)'))
    for instant in (0, 60, STALE_BUSY_SECONDS - 1):
        now[0] = instant
        assert not guard.allows(target)
    now[0] = STALE_BUSY_SECONDS
    assert guard.allows(target)  # the composer under the stale row reads empty


def test_readings_are_recorded_for_diagnosis(log_to_tmp):
    now = [0.0]
    guard = guard_at(now)
    guard.allows(ScreenTarget(claude_screen(busy_row='✻ Processing… (1m 0s)')))
    rec = json.loads(log_to_tmp.read_text().splitlines()[0])
    assert rec['state'] == 'unknown' and rec['reason'] == 'provider_busy'
    assert rec['cursor'] == [2, 3] and '❯ ' in rec['bottom']


def test_the_unattended_flag_reaches_ccbd():
    # ccbd gets an allowlisted env; a flag it drops would leave the patch switched off.
    from runtime_env.control_plane import control_plane_env
    env = control_plane_env(environ={'CCB_DRAFT_GUARD_UNATTENDED': '1', 'PATH': '/usr/bin'})
    assert env.get('CCB_DRAFT_GUARD_UNATTENDED') == '1'
