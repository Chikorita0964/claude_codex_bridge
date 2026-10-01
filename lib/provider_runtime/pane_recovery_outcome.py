"""Which action a recovery's ``ensure_pane`` call took, published on the session object.

Local patch (ccb-team-kit). ``ensure_pane`` keeps its ``(ok, pane_or_error)`` contract for its many
callers, so the action it took is recorded as a transient attribute on the session instead of a
third tuple item, and read back by the recovery layer: ``live_owned`` means the pane was already
alive and owned and the call changed nothing, ``respawn`` means the same pane was respawned in
place, ``replacement`` means a new pane was created. A session that never publishes an outcome
reads as ``None``, which callers must treat as "no signal, nothing is known" rather than as a
no-op: the attribute is transient on purpose, nothing of this reaches the session file.
"""
from __future__ import annotations

LIVE_OWNED = 'live_owned'
RESPAWN = 'respawn'
REPLACEMENT = 'replacement'
OUTCOMES = frozenset({LIVE_OWNED, RESPAWN, REPLACEMENT})
_OUTCOME_ATTR = 'pane_recovery_outcome'


def publish_pane_recovery_outcome(session, outcome: str | None) -> None:
    """Record ``outcome`` on ``session``; a session that refuses the attribute is left alone."""
    try:
        setattr(session, _OUTCOME_ATTR, outcome)
    except Exception:
        return


def pane_recovery_outcome(session) -> str | None:
    """The outcome the last ``ensure_pane`` published, or None when there is no signal."""
    outcome = str(getattr(session, _OUTCOME_ATTR, '') or '').strip()
    return outcome if outcome in OUTCOMES else None


__all__ = [
    'LIVE_OWNED',
    'OUTCOMES',
    'REPLACEMENT',
    'RESPAWN',
    'pane_recovery_outcome',
    'publish_pane_recovery_outcome',
]
