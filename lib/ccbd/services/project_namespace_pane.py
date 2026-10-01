from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from terminal_runtime.tmux_panes_runtime.queries_runtime.service import (
    TMUX_QUERY_UNKNOWN,
    TmuxQueryUnknown,
    is_tmux_query_unknown,
)
from terminal_runtime.tmux_readiness import (
    TmuxTransientServerUnavailable,
    is_tmux_transient_server_error_text,
    tmux_failure_detail,
)


@dataclass(frozen=True)
class ProjectNamespacePaneRecord:
    pane_id: str
    session_name: str | None = None
    window_id: str | None = None
    window_name: str | None = None
    pane_title: str | None = None
    role: str | None = None
    slot_key: str | None = None
    ccb_window: str | None = None
    sidebar_instance: str | None = None
    sidebar_helper_id: str | None = None
    agent_label: str | None = None
    label_style: str | None = None
    border_style: str | None = None
    active_border_style: str | None = None
    ccb_session_id: str | None = None
    window_width: int | None = None
    pane_width: int | None = None
    project_id: str | None = None
    managed_by: str | None = None
    namespace_epoch: int | None = None
    alive: bool = False

    @staticmethod
    def _matches_field(actual: str | None, expected: str, *, allow_missing: bool = False) -> bool:
        if allow_missing and actual is None:
            return True
        return str(actual or '').strip() == str(expected or '').strip()

    def matches(
        self,
        *,
        tmux_session_name: str,
        project_id: str,
        role: str,
        slot_key: str | None = None,
        managed_by: str | None = 'ccbd',
        window_id: str | None = None,
        window_name: str | None = None,
        namespace_epoch: int | None = None,
    ) -> bool:
        return self.mismatch_reason(
            tmux_session_name=tmux_session_name,
            project_id=project_id,
            role=role,
            slot_key=slot_key,
            managed_by=managed_by,
            window_id=window_id,
            window_name=window_name,
            namespace_epoch=namespace_epoch,
        ) is None

    def mismatch_reason(
        self,
        *,
        tmux_session_name: str,
        project_id: str,
        role: str,
        slot_key: str | None = None,
        managed_by: str | None = 'ccbd',
        window_id: str | None = None,
        window_name: str | None = None,
        namespace_epoch: int | None = None,
    ) -> str | None:
        if not self._matches_field(
            self.session_name,
            tmux_session_name or '',
            allow_missing=True,
        ):
            return 'tmux_session_mismatch'
        if not self._matches_field(self.project_id, project_id):
            return 'project_id_mismatch'
        if not self._matches_field(self.role, role):
            return 'pane_role_mismatch'
        if slot_key is not None and not self._matches_field(self.slot_key, slot_key):
            return 'pane_slot_mismatch'
        if window_name is not None and not self._matches_window_name(window_name):
            return 'logical_window_mismatch'
        if managed_by is not None and not self._matches_field(self.managed_by, managed_by):
            return 'pane_owner_mismatch'
        if window_id is not None and not self._matches_field(self.window_id, window_id):
            return 'tmux_window_id_mismatch'
        if namespace_epoch is not None and self.namespace_epoch != int(namespace_epoch):
            return 'namespace_epoch_mismatch'
        if not self.alive:
            return 'pane_not_alive'
        return None

    def _matches_window_name(self, expected: str) -> bool:
        if self.ccb_window is not None:
            return self._matches_field(self.ccb_window, expected)
        return self._matches_field(self.window_name, expected)

    def matches_authoritative_topology(
        self,
        *,
        tmux_session_name: str | None,
        project_id: str,
        role: str | None = None,
        slot_key: str | None = None,
        window_name: str | None = None,
        sidebar_instance: str | None = None,
        managed_by: str = 'ccbd',
        namespace_epoch: int | None = None,
        require_alive: bool = True,
    ) -> bool:
        """Return whether this live pane belongs to the current topology authority.

        Topology discovery is based on a server-wide ``list-panes -a`` snapshot,
        so pane user options alone are not sufficient: another session can carry
        an identical option set.  Session and epoch identity are therefore
        fail-closed requirements for authoritative matching.
        """

        if not str(self.pane_id or '').strip():
            return False
        if require_alive and not self.alive:
            return False
        expected_session = str(tmux_session_name or '').strip()
        if not expected_session or not self._matches_field(self.session_name, expected_session):
            return False
        if not self._matches_field(self.project_id, project_id):
            return False
        if managed_by and not self._matches_field(self.managed_by, managed_by):
            return False
        if role is not None and not self._matches_field(self.role, role):
            return False
        if slot_key is not None and not self._matches_field(self.slot_key, slot_key):
            return False
        if window_name is not None and not self._matches_window_name(window_name):
            return False
        if sidebar_instance is not None and not self._matches_field(
            self.sidebar_instance,
            sidebar_instance,
        ):
            return False
        if namespace_epoch is None or self.namespace_epoch != int(namespace_epoch):
            return False
        return True


def inspect_project_namespace_pane(backend, pane_id: str) -> ProjectNamespacePaneRecord | TmuxQueryUnknown | None:
    """Describe a pane; TMUX_QUERY_UNKNOWN when every probe timed out before answering."""
    pane_text = str(pane_id or '').strip()
    if not pane_text:
        return None
    details = _describe_pane_via_tmux(backend, pane_text) if pane_text.startswith('%') else None
    if details is None or is_tmux_query_unknown(details):
        # The second describe is a different probe (its own retries live in the pane service).
        details = _describe_pane_via_backend(backend, pane_text)
    if is_tmux_query_unknown(details):
        return TMUX_QUERY_UNKNOWN
    if details is None:
        return None
    return ProjectNamespacePaneRecord(
        pane_id=pane_text,
        session_name=_clean(details.get('session_name')),
        window_id=_clean(details.get('window_id')),
        window_name=_clean(details.get('window_name')),
        pane_title=_clean(details.get('pane_title')),
        role=_clean(details.get('@ccb_role')),
        slot_key=_clean(details.get('@ccb_slot')),
        ccb_window=_clean(details.get('@ccb_window')),
        sidebar_instance=_clean(details.get('@ccb_sidebar_instance')),
        sidebar_helper_id=_clean(details.get('@ccb_sidebar_helper_id')),
        agent_label=_clean(details.get('@ccb_agent')),
        label_style=_clean(details.get('@ccb_label_style')),
        border_style=_clean(details.get('@ccb_border_style')),
        active_border_style=_clean(details.get('@ccb_active_border_style')),
        ccb_session_id=_clean(details.get('@ccb_session_id')),
        window_width=_clean_int(details.get('window_width')),
        pane_width=_clean_int(details.get('pane_width')),
        project_id=_clean(details.get('@ccb_project_id')),
        managed_by=_clean(details.get('@ccb_managed_by')),
        namespace_epoch=_clean_int(details.get('@ccb_namespace_epoch')),
        alive=_pane_alive(details),
    )


def snapshot_project_namespace_panes(backend) -> dict[str, ProjectNamespacePaneRecord] | None:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return None
    fmt = '\t'.join(
        (
            '#{pane_id}',
            '#{session_name}',
            '#{window_id}',
            '#{window_name}',
            '#{pane_dead}',
            '#{@ccb_role}',
            '#{@ccb_slot}',
            '#{@ccb_window}',
            '#{@ccb_sidebar_instance}',
            '#{@ccb_sidebar_helper_id}',
            '#{@ccb_project_id}',
            '#{@ccb_managed_by}',
            '#{@ccb_namespace_epoch}',
            '#{pane_title}',
            '#{@ccb_agent}',
            '#{@ccb_label_style}',
            '#{@ccb_border_style}',
            '#{@ccb_active_border_style}',
            '#{@ccb_session_id}',
            '#{window_width}',
            '#{pane_width}',
        )
    )
    try:
        cp = runner(
            ['list-panes', '-a', '-F', fmt],
            capture=True,
            check=False,
            timeout=0.5,
        )
    except Exception:
        return None
    if getattr(cp, 'returncode', 1) != 0:
        return None
    records: dict[str, ProjectNamespacePaneRecord] = {}
    for line in (getattr(cp, 'stdout', '') or '').splitlines():
        details = _decode_tmux_pane_description(line)
        if details is None:
            continue
        pane_id = str(details.get('pane_id') or '').strip()
        if not pane_id.startswith('%'):
            continue
        records[pane_id] = ProjectNamespacePaneRecord(
            pane_id=pane_id,
            session_name=_clean(details.get('session_name')),
            window_id=_clean(details.get('window_id')),
            window_name=_clean(details.get('window_name')),
            pane_title=_clean(details.get('pane_title')),
            role=_clean(details.get('@ccb_role')),
            slot_key=_clean(details.get('@ccb_slot')),
            ccb_window=_clean(details.get('@ccb_window')),
            sidebar_instance=_clean(details.get('@ccb_sidebar_instance')),
            sidebar_helper_id=_clean(details.get('@ccb_sidebar_helper_id')),
            agent_label=_clean(details.get('@ccb_agent')),
            label_style=_clean(details.get('@ccb_label_style')),
            border_style=_clean(details.get('@ccb_border_style')),
            active_border_style=_clean(details.get('@ccb_active_border_style')),
            ccb_session_id=_clean(details.get('@ccb_session_id')),
            window_width=_clean_int(details.get('window_width')),
            pane_width=_clean_int(details.get('pane_width')),
            project_id=_clean(details.get('@ccb_project_id')),
            managed_by=_clean(details.get('@ccb_managed_by')),
            namespace_epoch=_clean_int(details.get('@ccb_namespace_epoch')),
            alive=_pane_alive(details),
        )
    return records


def same_tmux_socket_path(left: str | None, right: str | None) -> bool:
    left_text = str(left or '').strip()
    right_text = str(right or '').strip()
    if not left_text or not right_text:
        return False
    try:
        return Path(left_text).expanduser().resolve() == Path(right_text).expanduser().resolve()
    except Exception:
        return left_text == right_text


def backend_socket_matches(backend, tmux_socket_path: str) -> bool:
    backend_socket_path = str(getattr(backend, '_socket_path', '') or '').strip()
    if not backend_socket_path:
        return False
    return same_tmux_socket_path(backend_socket_path, tmux_socket_path)


def _describe_pane_via_tmux(backend, pane_id: str) -> dict[str, str] | TmuxQueryUnknown | None:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return None
    try:
        cp = runner(
            [
                'display-message',
                '-p',
                '-t',
                pane_id,
                '\t'.join(
                    (
                        '#{pane_id}',
                        '#{session_name}',
                        '#{window_id}',
                        '#{window_name}',
                        '#{pane_dead}',
                        '#{@ccb_role}',
                        '#{@ccb_slot}',
                        '#{@ccb_window}',
                        '#{@ccb_sidebar_instance}',
                        '#{@ccb_sidebar_helper_id}',
                        '#{@ccb_project_id}',
                        '#{@ccb_managed_by}',
                        '#{@ccb_namespace_epoch}',
                        '#{pane_title}',
                        '#{@ccb_agent}',
                        '#{@ccb_label_style}',
                        '#{@ccb_border_style}',
                        '#{@ccb_active_border_style}',
                        '#{@ccb_session_id}',
                        '#{window_width}',
                        '#{pane_width}',
                    )
                ),
            ],
            capture=True,
            check=False,
            timeout=0.5,
        )
    except subprocess.TimeoutExpired:
        # Local patch (ccb-team-kit): tmux did not answer, so this describe says nothing about
        # namespace membership; the backend-describe fallback is a separate probe.
        return TMUX_QUERY_UNKNOWN
    except Exception:
        return None
    if getattr(cp, 'returncode', 1) != 0:
        detail = tmux_failure_detail(
            cp,
            [
                'display-message',
                '-p',
                '-t',
                pane_id,
            ],
        )
        if is_tmux_transient_server_error_text(detail):
            raise TmuxTransientServerUnavailable(detail)
        return None
    line = ((getattr(cp, 'stdout', '') or '').splitlines() or [''])[0]
    return _decode_tmux_pane_description(line)


def _decode_tmux_pane_description(line: str) -> dict[str, str] | None:
    parts = line.split('\t')
    if len(parts) == 7:
        return {
            'pane_id': parts[0].strip(),
            'session_name': parts[1].strip(),
            'window_id': '',
            'window_name': '',
            'pane_dead': parts[2].strip(),
            '@ccb_role': parts[3].strip(),
            '@ccb_slot': parts[4].strip(),
            '@ccb_window': '',
            '@ccb_project_id': parts[5].strip(),
            '@ccb_managed_by': parts[6].strip(),
            '@ccb_namespace_epoch': '',
        }
    if len(parts) == 10:
        return {
            'pane_id': parts[0].strip(),
            'session_name': parts[1].strip(),
            'window_id': parts[2].strip(),
            'window_name': parts[3].strip(),
            'pane_dead': parts[4].strip(),
            '@ccb_role': parts[5].strip(),
            '@ccb_slot': parts[6].strip(),
            '@ccb_window': '',
            '@ccb_project_id': parts[7].strip(),
            '@ccb_managed_by': parts[8].strip(),
            '@ccb_namespace_epoch': parts[9].strip(),
        }
    if len(parts) == 11:
        return {
            'pane_id': parts[0].strip(),
            'session_name': parts[1].strip(),
            'window_id': parts[2].strip(),
            'window_name': parts[3].strip(),
            'pane_dead': parts[4].strip(),
            '@ccb_role': parts[5].strip(),
            '@ccb_slot': parts[6].strip(),
            '@ccb_window': parts[7].strip(),
            '@ccb_sidebar_instance': '',
            '@ccb_project_id': parts[8].strip(),
            '@ccb_managed_by': parts[9].strip(),
            '@ccb_namespace_epoch': parts[10].strip(),
        }
    if len(parts) == 12:
        return {
            'pane_id': parts[0].strip(),
            'session_name': parts[1].strip(),
            'window_id': parts[2].strip(),
            'window_name': parts[3].strip(),
            'pane_dead': parts[4].strip(),
            '@ccb_role': parts[5].strip(),
            '@ccb_slot': parts[6].strip(),
            '@ccb_window': parts[7].strip(),
            '@ccb_sidebar_instance': parts[8].strip(),
            '@ccb_sidebar_helper_id': '',
            '@ccb_project_id': parts[9].strip(),
            '@ccb_managed_by': parts[10].strip(),
            '@ccb_namespace_epoch': parts[11].strip(),
        }
    if len(parts) == 13:
        return {
            'pane_id': parts[0].strip(),
            'session_name': parts[1].strip(),
            'window_id': parts[2].strip(),
            'window_name': parts[3].strip(),
            'pane_dead': parts[4].strip(),
            '@ccb_role': parts[5].strip(),
            '@ccb_slot': parts[6].strip(),
            '@ccb_window': parts[7].strip(),
            '@ccb_sidebar_instance': parts[8].strip(),
            '@ccb_sidebar_helper_id': parts[9].strip(),
            '@ccb_project_id': parts[10].strip(),
            '@ccb_managed_by': parts[11].strip(),
            '@ccb_namespace_epoch': parts[12].strip(),
        }
    if len(parts) not in {19, 21}:
        return None
    result = {
        'pane_id': parts[0].strip(),
        'session_name': parts[1].strip(),
        'window_id': parts[2].strip(),
        'window_name': parts[3].strip(),
        'pane_dead': parts[4].strip(),
        '@ccb_role': parts[5].strip(),
        '@ccb_slot': parts[6].strip(),
        '@ccb_window': parts[7].strip(),
        '@ccb_sidebar_instance': parts[8].strip(),
        '@ccb_sidebar_helper_id': parts[9].strip(),
        '@ccb_project_id': parts[10].strip(),
        '@ccb_managed_by': parts[11].strip(),
        '@ccb_namespace_epoch': parts[12].strip(),
        'pane_title': parts[13].strip(),
        '@ccb_agent': parts[14].strip(),
        '@ccb_label_style': parts[15].strip(),
        '@ccb_border_style': parts[16].strip(),
        '@ccb_active_border_style': parts[17].strip(),
        '@ccb_session_id': parts[18].strip(),
    }
    if len(parts) == 21:
        result['window_width'] = parts[19].strip()
        result['pane_width'] = parts[20].strip()
    return result


def _describe_pane_via_backend(backend, pane_id: str) -> dict[str, str] | TmuxQueryUnknown | None:
    descriptor = getattr(backend, 'describe_pane', None)
    if not callable(descriptor):
        return None
    try:
        described = descriptor(
            pane_id,
            user_options=(
                '@ccb_role',
                '@ccb_slot',
                '@ccb_window',
                '@ccb_sidebar_instance',
                '@ccb_sidebar_helper_id',
                '@ccb_agent',
                '@ccb_label_style',
                '@ccb_border_style',
                '@ccb_active_border_style',
                '@ccb_session_id',
                '@ccb_project_id',
                '@ccb_managed_by',
                '@ccb_namespace_epoch',
            ),
        )
    except subprocess.TimeoutExpired:
        return TMUX_QUERY_UNKNOWN
    except Exception:
        return None
    if is_tmux_query_unknown(described):
        return described
    if not isinstance(described, dict):
        return None
    result = _stringify_details(described)
    if 'session_name' not in result:
        result['session_name'] = ''
    return result


def _stringify_details(described: dict[object, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in described.items()}


def _clean(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _clean_int(value: object) -> int | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _pane_alive(details: dict[str, str]) -> bool:
    return str(details.get('pane_dead') or '').strip() in {'', '0', 'false', 'False'}


__all__ = [
    'ProjectNamespacePaneRecord',
    'backend_socket_matches',
    'inspect_project_namespace_pane',
    'same_tmux_socket_path',
    'snapshot_project_namespace_panes',
]
