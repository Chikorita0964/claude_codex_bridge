from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from storage.atomic import atomic_write_json

SCHEMA_VERSION = 1

# Events for turns that resolve to no CCB request anchor are written under a
# reserved id so they survive until ccbd can attach them to the agent's active
# submission. The prefix marks the id class; the suffix keeps session identity
# and write time visible in the file name.
RESERVED_REQ_ID_PREFIX = 'reply_without_job__'


def event_path(completion_dir: Path | str, req_id: str) -> Path:
    return Path(completion_dir).expanduser() / 'events' / f'{req_id}.json'


def reserved_req_id(session_id: str | None, *, timestamp: datetime | None = None) -> str:
    """A filesystem-safe event id for a reply whose turn has no job anchor."""
    moment = timestamp or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    session = re.sub(r'[^A-Za-z0-9_.-]', '-', str(session_id or '').strip()).strip('.-')
    return f'{RESERVED_REQ_ID_PREFIX}{session or "unknown-session"}__{stamp}'


def is_reserved_req_id(req_id: str | None) -> bool:
    return str(req_id or '').strip().startswith(RESERVED_REQ_ID_PREFIX)


def iter_reserved_events(completion_dir: Path | str) -> tuple[dict[str, Any], ...]:
    """Every kept reply event written for a turn outside any CCB job."""
    directory = Path(completion_dir).expanduser() / 'events'
    try:
        paths = sorted(directory.glob(f'{RESERVED_REQ_ID_PREFIX}*.json'))
    except OSError:
        return ()
    events: list[dict[str, Any]] = []
    for path in paths:
        payload = _load_json_dict(path)
        if payload is None or not is_reserved_req_id(payload.get('req_id')):
            continue
        events.append(payload)
    return tuple(events)


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _normalized_req_id(req_id: str) -> str:
    return str(req_id or '').strip()


def _event_matches_req_id(payload: dict[str, Any], req_id: str) -> bool:
    return _normalized_req_id(payload.get('req_id')) == _normalized_req_id(req_id)


def _normalized_optional(value: str | None) -> str | None:
    normalized = str(value or '').strip()
    return normalized or None


def _normalized_payload(
    *,
    provider: str,
    agent_name: str,
    workspace_path: str,
    req_id: str,
    status: str,
    reply: str,
    session_id: str | None,
    hook_event_name: str | None,
    transcript_path: str | None,
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'record_type': 'provider_completion_hook',
        'provider': str(provider or '').strip().lower(),
        'agent_name': str(agent_name or '').strip(),
        'workspace_path': str(workspace_path or '').strip(),
        'req_id': _normalized_req_id(req_id),
        'status': str(status or '').strip().lower(),
        'reply': str(reply or ''),
        'session_id': _normalized_optional(session_id),
        'hook_event_name': _normalized_optional(hook_event_name),
        'transcript_path': _normalized_optional(transcript_path),
        'diagnostics': dict(diagnostics or {}),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


def load_event(completion_dir: Path | str, req_id: str) -> dict[str, Any] | None:
    path = event_path(completion_dir, req_id)
    if not path.exists():
        return None
    payload = _load_json_dict(path)
    if payload is None:
        return None
    if not _event_matches_req_id(payload, req_id):
        return None
    return payload


def write_event(
    *,
    provider: str,
    completion_dir: Path | str,
    agent_name: str,
    workspace_path: str,
    req_id: str,
    status: str,
    reply: str,
    session_id: str | None = None,
    hook_event_name: str | None = None,
    transcript_path: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> Path:
    payload = _event_payload(
        provider=provider,
        agent_name=agent_name,
        workspace_path=workspace_path,
        req_id=req_id,
        status=status,
        reply=reply,
        session_id=session_id,
        hook_event_name=hook_event_name,
        transcript_path=transcript_path,
        diagnostics=diagnostics,
    )
    path = event_path(completion_dir, req_id)
    atomic_write_json(path, payload)
    return path


def _event_payload(
    *,
    provider: str,
    agent_name: str,
    workspace_path: str,
    req_id: str,
    status: str,
    reply: str,
    session_id: str | None,
    hook_event_name: str | None,
    transcript_path: str | None,
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    return _normalized_payload(
        provider=provider,
        agent_name=agent_name,
        workspace_path=workspace_path,
        req_id=req_id,
        status=status,
        reply=reply,
        session_id=session_id,
        hook_event_name=hook_event_name,
        transcript_path=transcript_path,
        diagnostics=diagnostics,
    )


__all__ = [
    'RESERVED_REQ_ID_PREFIX',
    'SCHEMA_VERSION',
    'event_path',
    'is_reserved_req_id',
    'iter_reserved_events',
    'load_event',
    'reserved_req_id',
    'write_event',
]
