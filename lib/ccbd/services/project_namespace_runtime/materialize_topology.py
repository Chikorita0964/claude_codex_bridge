from __future__ import annotations

import shlex
from types import SimpleNamespace
from typing import Any

from cli.services.tmux_ui import apply_project_tmux_ui
from agents.models import (
    AgentValidationError,
    layout_tool_alias_command,
    layout_tool_alias_label,
    normalize_agent_name,
    parse_layout_spec,
)
from terminal_runtime.placeholders import pane_placeholder_cmd
from terminal_runtime.tmux_panes_runtime.queries_runtime.service import is_tmux_query_unknown
from terminal_runtime.tmux_theme import tmux_theme_profile
from project_command_trust import require_project_command_approval
from ccbd.services.project_namespace_pane import (
    ProjectNamespacePaneRecord,
    inspect_project_namespace_pane,
)

from .backend import (
    apply_pane_identity,
    create_session,
    ensure_window,
    ensure_server_policy,
    rename_window,
    respawn_pane,
    select_window,
    session_window_target,
    split_pane,
    window_root_pane,
)
from .sidebar_helper import SIDEBAR_HELPER_ID_OPTION, sidebar_helper_fingerprint, sidebar_respawn_args


def refresh_topology_ui(context) -> None:
    if not callable(getattr(context.backend, '_tmux_run', None)):
        _sync_topology_sidebar_widths(None, context, topology_plan=getattr(context, 'topology_plan', None))
        return
    apply_project_tmux_ui(
        tmux_socket_path=context.desired_socket_path,
        tmux_session_name=context.desired_session_name,
        backend=context.backend,
    )
    _sync_topology_sidebar_widths(None, context, topology_plan=getattr(context, 'topology_plan', None))


def refresh_topology_ui_for_project(
    controller,
    context,
    *,
    topology_plan,
    timeout_s: float | None = None,
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    refresh_sidebar_helpers: bool = True,
    namespace_epoch: int | None = None,
) -> None:
    resolved_epoch = _authoritative_namespace_epoch(context, explicit=namespace_epoch)
    if refresh_sidebar_helpers:
        refresh_topology_sidebar_helpers(
            controller,
            context.backend,
            topology_plan=topology_plan,
            pane_records=pane_records,
            tmux_session_name=context.desired_session_name,
            namespace_epoch=resolved_epoch,
        )
    if callable(getattr(context.backend, '_tmux_run', None)):
        apply_project_tmux_ui(
            tmux_socket_path=context.desired_socket_path,
            ccbd_socket_path=str(controller._layout.ccbd_socket_path),
            tmux_session_name=context.desired_session_name,
            backend=context.backend,
        )
    _sync_topology_sidebar_widths(
        controller,
        context,
        topology_plan=topology_plan,
        timeout_s=timeout_s,
        pane_records=pane_records,
        namespace_epoch=resolved_epoch,
    )


def sync_topology_sidebar_widths(
    controller,
    backend,
    *,
    session_name: str,
    topology_plan,
    timeout_s: float | None = None,
    namespace_epoch: int | None = None,
) -> None:
    context = SimpleNamespace(backend=backend, desired_session_name=session_name)
    _sync_topology_sidebar_widths(
        controller,
        context,
        topology_plan=topology_plan,
        timeout_s=timeout_s,
        namespace_epoch=namespace_epoch,
    )


def materialize_topology(
    controller,
    context,
    *,
    topology_plan,
    epoch: int,
    terminal_size: tuple[int, int] | None = None,
    timeout_s: float | None = None,
) -> tuple[dict[str, str], str | None]:
    windows = tuple(getattr(topology_plan, 'windows', ()) or ())
    if not windows:
        return {}, None
    first_window = windows[0]
    if not context.session_is_alive:
        create_session(
            context.backend,
            session_name=context.desired_session_name,
            project_root=controller._layout.project_root,
            window_name=first_window.name,
            terminal_size=terminal_size,
            timeout_s=timeout_s,
        )
    else:
        ensure_window(
            context.backend,
            session_name=context.desired_session_name,
            window_name=first_window.name,
            project_root=controller._layout.project_root,
            select=False,
            timeout_s=timeout_s,
        )
    ensure_server_policy(context.backend, timeout_s=timeout_s)
    if callable(getattr(context.backend, '_tmux_run', None)):
        apply_project_tmux_ui(
            tmux_socket_path=context.desired_socket_path,
            ccbd_socket_path=str(controller._layout.ccbd_socket_path),
            tmux_session_name=context.desired_session_name,
            backend=context.backend,
        )
    _rename_legacy_workspace_if_needed(controller, context, first_window_name=first_window.name, timeout_s=timeout_s)

    agent_panes: dict[str, str] = {}
    cmd_pane: str | None = None
    for index, window in enumerate(windows):
        ensure_window(
            context.backend,
            session_name=context.desired_session_name,
            window_name=window.name,
            project_root=controller._layout.project_root,
            select=index == 0,
            timeout_s=timeout_s,
        )
        target = session_window_target(context.desired_session_name, window.name)
        root_pane = window_root_pane(context.backend, target_window=target, timeout_s=timeout_s)
        user_root = _materialize_sidebar(
            controller,
            context,
            window=window,
            root_pane=root_pane,
            epoch=epoch,
            timeout_s=timeout_s,
        )
        window_agent_panes, window_cmd_pane = _materialize_agent_layout(
            controller,
            context,
            window=window,
            user_root=user_root,
            epoch=epoch,
            timeout_s=timeout_s,
        )
        agent_panes.update(window_agent_panes)
        cmd_pane = cmd_pane or window_cmd_pane
        _materialize_tool_window(
            controller,
            context,
            window=window,
            user_root=user_root,
            epoch=epoch,
        )

    refresh_topology_ui_for_project(
        controller,
        context,
        topology_plan=topology_plan,
        timeout_s=timeout_s,
        refresh_sidebar_helpers=False,
        namespace_epoch=epoch,
    )
    select_window(
        context.backend,
        target=session_window_target(context.desired_session_name, topology_plan.entry_window),
    )
    return agent_panes, cmd_pane


def existing_topology_agent_panes(
    controller,
    context,
    *,
    topology_plan,
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    namespace_epoch: int | None = None,
    include_dead: bool = False,
) -> dict[str, str]:
    resolved_epoch = _authoritative_namespace_epoch(context, explicit=namespace_epoch)
    resolved_session = _authoritative_tmux_session_name(context)
    agent_panes: dict[str, str] = {}
    for window in tuple(getattr(topology_plan, 'windows', ()) or ()):
        for agent_name in tuple(getattr(window, 'agent_names', ()) or ()):
            expected = {
                '@ccb_project_id': controller._project_id,
                '@ccb_role': 'agent',
                '@ccb_slot': str(agent_name),
                '@ccb_window': str(window.name),
                '@ccb_managed_by': 'ccbd',
            }
            matches = _matching_pane_ids(
                pane_records,
                context.backend,
                expected,
                tmux_session_name=resolved_session,
                namespace_epoch=resolved_epoch,
                require_alive=not include_dead,
            )
            if len(matches) == 1:
                agent_panes[str(agent_name)] = matches[0]
    return agent_panes


def existing_topology_cmd_pane(
    controller,
    context,
    *,
    topology_plan,
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    namespace_epoch: int | None = None,
) -> str | None:
    cmd_windows = _expected_cmd_windows(tuple(getattr(topology_plan, 'windows', ()) or ()))
    if len(cmd_windows) != 1:
        return None
    resolved_epoch = _authoritative_namespace_epoch(context, explicit=namespace_epoch)
    matches = _matching_pane_ids(
        pane_records,
        context.backend,
        {
            '@ccb_project_id': controller._project_id,
            '@ccb_role': 'cmd',
            '@ccb_slot': 'cmd',
            '@ccb_window': cmd_windows[0],
            '@ccb_managed_by': 'ccbd',
        },
        tmux_session_name=_authoritative_tmux_session_name(context),
        namespace_epoch=resolved_epoch,
    )
    return matches[0] if len(matches) == 1 else None


def topology_active_panes(
    controller,
    context,
    *,
    topology_plan,
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    namespace_epoch: int | None = None,
) -> tuple[str, ...]:
    resolved_epoch = _authoritative_namespace_epoch(context, explicit=namespace_epoch)
    resolved_session = _authoritative_tmux_session_name(context)
    topology_windows = tuple(getattr(topology_plan, 'windows', ()) or ())
    resolved_records = pane_records
    if resolved_records is None:
        candidate_ids = _list_panes_by_user_options(
            context.backend,
            {
                '@ccb_project_id': controller._project_id,
                '@ccb_managed_by': 'ccbd',
                '@ccb_namespace_epoch': str(resolved_epoch or ''),
            },
        )
        resolved_records = {}
        for pane_id in dict.fromkeys(candidate_ids):
            record = inspect_project_namespace_pane(context.backend, pane_id)
            # A timed-out probe is no record; treating it as one hides the pane from topology.
            if record is not None and not is_tmux_query_unknown(record):
                resolved_records[pane_id] = record
    expected: list[dict[str, str]] = []
    if bool(getattr(topology_plan, 'sidebar_enabled', False)):
        for window in topology_windows:
            window_name = str(window.name)
            expected.append(
                {
                    '@ccb_project_id': controller._project_id,
                    '@ccb_role': 'sidebar',
                    '@ccb_slot': f'sidebar:{window_name}',
                    '@ccb_window': window_name,
                    '@ccb_sidebar_instance': window_name,
                    '@ccb_managed_by': 'ccbd',
                }
            )
    for window_name in _expected_cmd_windows(topology_windows):
        expected.append(
            {
                '@ccb_project_id': controller._project_id,
                '@ccb_role': 'cmd',
                '@ccb_slot': 'cmd',
                '@ccb_window': window_name,
                '@ccb_managed_by': 'ccbd',
            }
        )
    for window in topology_windows:
        for agent_name in tuple(getattr(window, 'agent_names', ()) or ()):
            expected.append(
                {
                    '@ccb_project_id': controller._project_id,
                    '@ccb_role': 'agent',
                    '@ccb_slot': str(agent_name),
                    '@ccb_window': str(window.name),
                    '@ccb_managed_by': 'ccbd',
                }
            )
    for window_name, slot_key in sorted(_expected_tool_slots(topology_windows)):
        expected.append(
            {
                '@ccb_project_id': controller._project_id,
                '@ccb_role': 'tool',
                '@ccb_slot': slot_key,
                '@ccb_window': window_name,
                '@ccb_managed_by': 'ccbd',
            }
        )

    panes: list[str] = []
    for identity in expected:
        matches = _matching_pane_ids(
            resolved_records,
            context.backend,
            identity,
            tmux_session_name=resolved_session,
            namespace_epoch=resolved_epoch,
        )
        if len(matches) == 1:
            panes.append(matches[0])
    return tuple(dict.fromkeys(panes))


def topology_recreate_reason(
    controller,
    context,
    *,
    topology_plan,
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    namespace_epoch: int | None = None,
) -> str | None:
    resolved_epoch = _authoritative_namespace_epoch(context, explicit=namespace_epoch)
    resolved_session = _authoritative_tmux_session_name(context)
    if context.current is not None:
        current_workspace = str(getattr(context.current, 'workspace_window_name', '') or '').strip()
        if current_workspace and current_workspace != context.desired_workspace_window_name:
            return 'topology_workspace_changed'

    windows = tuple(getattr(topology_plan, 'windows', ()) or ())
    if pane_records is not None:
        observed_windows = {
            str(record.window_name or '').strip()
            for record in pane_records.values()
            if str(record.session_name or '').strip() == context.desired_session_name
            and str(record.project_id or '').strip() == controller._project_id
            and str(record.managed_by or '').strip() == 'ccbd'
            and record.namespace_epoch == resolved_epoch
        }
        for window in windows:
            if str(window.name) not in observed_windows:
                return f'topology_window_missing:{window.name}'
    else:
        for window in windows:
            if _find_window(context, str(window.name)) is None:
                return f'topology_window_missing:{window.name}'

    expected_agents = {
        str(agent_name)
        for window in windows
        for agent_name in tuple(getattr(window, 'agent_names', ()) or ())
    }
    if set(
        existing_topology_agent_panes(
            controller,
            context,
            topology_plan=topology_plan,
            pane_records=pane_records,
            namespace_epoch=resolved_epoch,
            include_dead=True,
        )
    ) != expected_agents:
        return 'topology_agent_panes_changed'

    if bool(getattr(topology_plan, 'sidebar_enabled', False)):
        for window in windows:
            matches = _matching_pane_ids(
                pane_records,
                context.backend,
                {
                    '@ccb_project_id': controller._project_id,
                    '@ccb_role': 'sidebar',
                    '@ccb_slot': f'sidebar:{window.name}',
                    '@ccb_window': str(window.name),
                    '@ccb_sidebar_instance': str(window.name),
                    '@ccb_managed_by': 'ccbd',
                },
                tmux_session_name=resolved_session,
                namespace_epoch=resolved_epoch,
            )
            if len(matches) != 1:
                return 'topology_sidebar_panes_changed'
    for window_name in _expected_cmd_windows(windows):
        matches = _matching_pane_ids(
            pane_records,
            context.backend,
            {
                '@ccb_project_id': controller._project_id,
                '@ccb_role': 'cmd',
                '@ccb_slot': 'cmd',
                '@ccb_window': window_name,
                '@ccb_managed_by': 'ccbd',
            },
            tmux_session_name=resolved_session,
            namespace_epoch=resolved_epoch,
        )
        if len(matches) != 1:
            return 'topology_cmd_panes_changed'
    expected_tools = _expected_tool_slots(windows)
    for window_name, slot_key in expected_tools:
        matches = _matching_pane_ids(
            pane_records,
            context.backend,
            {
                '@ccb_project_id': controller._project_id,
                '@ccb_role': 'tool',
                '@ccb_slot': slot_key,
                '@ccb_window': window_name,
                '@ccb_managed_by': 'ccbd',
            },
            tmux_session_name=resolved_session,
            namespace_epoch=resolved_epoch,
        )
        if len(matches) != 1:
            return 'topology_tool_panes_changed'
    return None


def _rename_legacy_workspace_if_needed(controller, context, *, first_window_name: str, timeout_s: float | None) -> None:
    legacy_name = str(getattr(controller._layout, 'ccbd_tmux_workspace_window_name', '') or '').strip()
    if context.current is not None:
        legacy_name = str(getattr(context.current, 'workspace_window_name', '') or '').strip() or legacy_name
    first_name = str(first_window_name or '').strip()
    if not legacy_name or not first_name or legacy_name == first_name:
        return
    legacy = ensure_target = None
    try:
        from .backend import find_window

        legacy = find_window(
            context.backend,
            session_name=context.desired_session_name,
            window_name=legacy_name,
            timeout_s=timeout_s,
        )
        ensure_target = find_window(
            context.backend,
            session_name=context.desired_session_name,
            window_name=first_name,
            timeout_s=timeout_s,
        )
    except Exception:
        return
    if legacy is None or ensure_target is not None:
        return
    rename_window(
        context.backend,
        target=session_window_target(context.desired_session_name, legacy.window_id or legacy_name),
        new_name=first_name,
        timeout_s=timeout_s,
    )


def _materialize_sidebar(
    controller,
    context,
    *,
    window,
    root_pane: str,
    epoch: int,
    timeout_s: float | None,
) -> str:
    sidebar = getattr(window, 'sidebar', None)
    if sidebar is None:
        return root_pane
    if getattr(sidebar, 'position', 'left') == 'right':
        sidebar_pane = split_pane(
            context.backend,
            target=root_pane,
            direction='right',
            percent=_sidebar_pane_percent_for_sidebar(
                sidebar.width,
                pane_width=_pane_width_cells(context.backend, root_pane),
            ),
            project_root=controller._layout.project_root,
            timeout_s=timeout_s,
        )
        _respawn_sidebar(context.backend, sidebar_pane, sidebar.launch_args, cwd=str(controller._layout.project_root))
        apply_pane_identity(
            context.backend,
            pane_id=sidebar_pane,
            title='sidebar',
            agent_label='sidebar',
            project_id=controller._project_id,
            role='sidebar',
            slot_key=f'sidebar:{window.name}',
            window_name=window.name,
            sidebar_instance=window.name,
            namespace_epoch=epoch,
            managed_by='ccbd',
        )
        _record_sidebar_helper_identity(context.backend, sidebar_pane)
        return root_pane
    user_root = split_pane(
        context.backend,
        target=root_pane,
        direction='right',
        percent=_user_pane_percent_for_sidebar(
            sidebar.width,
            pane_width=_pane_width_cells(context.backend, root_pane),
        ),
        project_root=controller._layout.project_root,
        timeout_s=timeout_s,
    )
    _respawn_sidebar(context.backend, root_pane, sidebar.launch_args, cwd=str(controller._layout.project_root))
    apply_pane_identity(
        context.backend,
        pane_id=root_pane,
        title='sidebar',
        agent_label='sidebar',
        project_id=controller._project_id,
        role='sidebar',
        slot_key=f'sidebar:{window.name}',
        window_name=window.name,
        sidebar_instance=window.name,
        namespace_epoch=epoch,
        managed_by='ccbd',
    )
    _record_sidebar_helper_identity(context.backend, root_pane)
    return user_root


def refresh_topology_sidebar_helpers(
    controller,
    backend,
    *,
    topology_plan,
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    tmux_session_name: str | None = None,
    namespace_epoch: int | None = None,
) -> tuple[str, ...]:
    desired_identity = sidebar_helper_fingerprint()
    if not desired_identity:
        return ()
    refreshed: list[str] = []
    for window in tuple(getattr(topology_plan, 'windows', ()) or ()):
        sidebar = getattr(window, 'sidebar', None)
        if sidebar is None:
            continue
        matches = _matching_pane_ids(
            pane_records,
            backend,
            {
                '@ccb_project_id': controller._project_id,
                '@ccb_role': 'sidebar',
                '@ccb_slot': f'sidebar:{window.name}',
                '@ccb_window': str(window.name),
                '@ccb_sidebar_instance': str(window.name),
                '@ccb_managed_by': 'ccbd',
            },
            tmux_session_name=tmux_session_name,
            namespace_epoch=namespace_epoch,
        )
        if len(matches) != 1:
            continue
        pane_id = matches[0]
        record = pane_records.get(pane_id) if pane_records is not None else None
        current_identity = (
            str(getattr(record, 'sidebar_helper_id', '') or '').strip()
            if record is not None
            else _pane_option(backend, pane_id, SIDEBAR_HELPER_ID_OPTION)
        )
        if current_identity == desired_identity:
            continue
        _respawn_sidebar(
            backend,
            pane_id,
            tuple(getattr(sidebar, 'launch_args', ()) or ()),
            cwd=str(controller._layout.project_root),
        )
        _record_sidebar_helper_identity(backend, pane_id, identity=desired_identity)
        refreshed.append(pane_id)
    return tuple(refreshed)


def _record_sidebar_helper_identity(backend, pane_id: str, *, identity: str | None = None) -> None:
    resolved = identity or sidebar_helper_fingerprint()
    if not resolved:
        return
    setter = getattr(backend, 'set_pane_user_option', None)
    if not callable(setter):
        return
    setter(pane_id, SIDEBAR_HELPER_ID_OPTION, resolved)


def _materialize_agent_layout(
    controller,
    context,
    *,
    window,
    user_root: str,
    epoch: int,
    timeout_s: float | None,
) -> tuple[dict[str, str], str | None]:
    if str(getattr(window, 'kind', '') or '') == 'tool':
        return {}, None
    layout = parse_layout_spec(window.user_layout)
    agent_names = tuple(str(name) for name in getattr(window, 'agent_names', ()) or ())
    tool_names = set(str(name) for name in tuple(getattr(window, 'tool_names', ()) or ()))
    style_index_by_agent = {name: index for index, name in enumerate(agent_names)}
    agent_panes: dict[str, str] = {}
    cmd_pane: str | None = None

    def assign_leaf(item: str, pane_id: str) -> None:
        nonlocal cmd_pane
        if item == 'cmd':
            apply_pane_identity(
                context.backend,
                pane_id=pane_id,
                title='cmd',
                agent_label='cmd',
                project_id=controller._project_id,
                is_cmd=True,
                role='cmd',
                slot_key='cmd',
                window_name=window.name,
                namespace_epoch=epoch,
                managed_by='ccbd',
            )
            cmd_pane = pane_id
            return
        item_tool = str(item or '').strip().lower()
        if item_tool in tool_names:
            _materialize_tool_pane(
                controller,
                context,
                pane_id=pane_id,
                tool_name=item_tool,
                command=layout_tool_alias_command(item_tool),
                label=layout_tool_alias_label(item_tool),
                window_name=window.name,
                order_index=int(getattr(window, 'order', 0) or 0),
                epoch=epoch,
                project_command_field=False,
            )
            return
        # 用 normalize_agent_name 统一 agent 标识（layout leaf 可能保留原始大小写如
        # Main_Code，而 CCB 内部 config key / topology agent_names 是 normalized
        # main_code）。否则 pane token 与提取/匹配用大小写敏感比较，main_code 会
        # 拿不到 pane → provider_runtime_deferred_on_herdr（2026-08-06 采集暴露）。
        try:
            agent_name = normalize_agent_name(item)
        except AgentValidationError:
            # 非法 agent 名（罕见）：保留原始名，避免物化中断
            agent_name = item
        agent_panes[agent_name] = pane_id
        apply_pane_identity(
            context.backend,
            pane_id=pane_id,
            title=item,
            agent_label=agent_name,
            project_id=controller._project_id,
            order_index=style_index_by_agent.get(agent_name),
            role='agent',
            slot_key=agent_name,
            window_name=window.name,
            namespace_epoch=epoch,
            managed_by='ccbd',
        )

    _materialize_layout(
        controller,
        context,
        parent_pane_id=user_root,
        node=layout,
        assign_leaf=assign_leaf,
        timeout_s=timeout_s,
    )
    return agent_panes, cmd_pane


def _materialize_tool_window(
    controller,
    context,
    *,
    window,
    user_root: str,
    epoch: int,
) -> None:
    if str(getattr(window, 'kind', '') or '') != 'tool':
        return
    command = str(getattr(window, 'command', '') or '').strip() or pane_placeholder_cmd()
    _materialize_tool_pane(
        controller,
        context,
        pane_id=user_root,
        tool_name=window.name,
        command=command,
        label=str(getattr(window, 'label', None) or window.name),
        window_name=window.name,
        order_index=int(getattr(window, 'order', 0) or 0),
        epoch=epoch,
        project_command_field=True,
    )


def _materialize_tool_pane(
    controller,
    context,
    *,
    pane_id: str,
    tool_name: str,
    command: str,
    label: str,
    window_name: str,
    order_index: int,
    epoch: int,
    project_command_field: bool,
) -> None:
    command = str(command or '').strip() or pane_placeholder_cmd()
    if project_command_field:
        require_project_command_approval(
            controller._layout.project_root,
            field_path=f'tool_windows.{str(tool_name).strip().lower()}.command',
            field_value=command,
        )
    respawn_pane(
        context.backend,
        pane_id=pane_id,
        command=command,
        cwd=str(controller._layout.project_root),
    )
    apply_pane_identity(
        context.backend,
        pane_id=pane_id,
        title=label,
        agent_label=label,
        project_id=controller._project_id,
        order_index=order_index,
        role='tool',
        slot_key=f'tool:{tool_name}',
        window_name=window_name,
        namespace_epoch=epoch,
        managed_by='ccbd',
    )


def _expected_tool_slots(windows: tuple[object, ...]) -> set[tuple[str, str]]:
    expected: set[tuple[str, str]] = set()
    for window in windows:
        window_name = str(getattr(window, 'name', '') or '')
        if str(getattr(window, 'kind', '') or '') == 'tool':
            expected.add((window_name, f'tool:{window_name}'))
        for tool_name in tuple(getattr(window, 'tool_names', ()) or ()):
            expected.add((window_name, f'tool:{tool_name}'))
    return expected


def _expected_cmd_windows(windows: tuple[object, ...]) -> tuple[str, ...]:
    expected: list[str] = []
    for window in windows:
        if str(getattr(window, 'kind', '') or '') == 'tool':
            continue
        layout_text = str(getattr(window, 'user_layout', '') or '').strip()
        if not layout_text:
            continue
        layout = parse_layout_spec(layout_text)
        if any(str(leaf.name or '').strip().lower() == 'cmd' for leaf in layout.iter_leaves()):
            expected.append(str(window.name))
    return tuple(expected)


def _get_specified_percent(node: Any) -> int | None:
    if node.kind == 'leaf':
        assert node.leaf is not None
        return node.leaf.percent
    for leaf in node.iter_leaves():
        if getattr(leaf, 'percent', None) is not None:
            return leaf.percent
    return None


def _materialize_layout(
    controller,
    context,
    *,
    parent_pane_id: str,
    node: Any,
    assign_leaf,
    timeout_s: float | None,
) -> None:
    if node.kind == 'leaf':
        assert node.leaf is not None
        assign_leaf(node.leaf.name, parent_pane_id)
        return

    assert node.left is not None
    assert node.right is not None

    right_pct = _get_specified_percent(node.right)
    left_pct = _get_specified_percent(node.left)
    if right_pct is not None:
        percent = max(1, min(99, right_pct))
    elif left_pct is not None:
        percent = max(1, min(99, 100 - left_pct))
    else:
        total = max(1, node.leaf_count)
        right_count = max(1, node.right.leaf_count)
        percent = max(1, min(99, round((right_count * 100) / total)))

    direction = 'right' if node.kind == 'horizontal' else 'bottom'
    new_pane_id = split_pane(
        context.backend,
        target=parent_pane_id,
        direction=direction,
        percent=percent,
        project_root=controller._layout.project_root,
        timeout_s=timeout_s,
    )
    _materialize_layout(
        controller,
        context,
        parent_pane_id=parent_pane_id,
        node=node.left,
        assign_leaf=assign_leaf,
        timeout_s=timeout_s,
    )
    _materialize_layout(
        controller,
        context,
        parent_pane_id=new_pane_id,
        node=node.right,
        assign_leaf=assign_leaf,
        timeout_s=timeout_s,
    )


def _find_window(context, window_name: str):
    try:
        from .backend import find_window

        return find_window(
            context.backend,
            session_name=context.desired_session_name,
            window_name=window_name,
            timeout_s=0.0,
        )
    except Exception:
        return None


def _sync_topology_sidebar_widths(
    controller,
    context,
    *,
    topology_plan,
    timeout_s: float | None = None,
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    namespace_epoch: int | None = None,
) -> None:
    if topology_plan is None or not bool(getattr(topology_plan, 'sidebar_enabled', False)):
        return
    width_by_window = {
        str(window.name): getattr(window.sidebar, 'width', '15%')
        for window in tuple(getattr(topology_plan, 'windows', ()) or ())
        if getattr(window, 'sidebar', None) is not None
    }
    if not width_by_window:
        return
    project_id = (
        str(getattr(controller, '_project_id', '') or '').strip()
        if controller is not None
        else ''
    )
    width_override = _session_sidebar_width_override(context.backend, context.desired_session_name)
    _set_session_sidebar_sync_guard(context.backend, context.desired_session_name, enabled=True)
    try:
        for record in _list_sidebar_geometry_records(
            context.backend,
            session_name=context.desired_session_name,
            project_id=project_id,
            pane_records=pane_records,
            namespace_epoch=namespace_epoch,
        ):
            configured_width = width_override or width_by_window.get(record['sidebar_instance'])
            if configured_width is None:
                continue
            window_width = _positive_int(record.get('window_width'))
            if window_width <= 0:
                continue
            target_width = _sidebar_width_cells(configured_width, window_width)
            if target_width <= 0 or target_width == _positive_int(record.get('pane_width')):
                continue
            _resize_pane_width(context.backend, record['pane_id'], target_width, timeout_s=timeout_s)
    finally:
        _set_session_sidebar_sync_guard(context.backend, context.desired_session_name, enabled=False)


def _list_sidebar_geometry_records(
    backend,
    *,
    session_name: str,
    project_id: str = '',
    pane_records: dict[str, ProjectNamespacePaneRecord] | None = None,
    namespace_epoch: int | None = None,
) -> list[dict[str, str]]:
    if pane_records is not None:
        records: list[dict[str, str]] = []
        for pane_id, record in pane_records.items():
            if project_id and not record.matches_authoritative_topology(
                tmux_session_name=session_name,
                project_id=project_id,
                role='sidebar',
                namespace_epoch=namespace_epoch,
            ):
                continue
            if not project_id and (
                str(record.session_name or '').strip() != session_name
                or record.role != 'sidebar'
                or record.managed_by != 'ccbd'
                or not record.alive
            ):
                continue
            sidebar_instance = str(record.sidebar_instance or '').strip()
            if not pane_id.startswith('%') or not sidebar_instance:
                continue
            records.append(
                {
                    'pane_id': pane_id,
                    'window_width': str(record.window_width or ''),
                    'pane_width': str(record.pane_width or ''),
                    'sidebar_instance': sidebar_instance,
                }
            )
        return records
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return []
    fmt = '\t'.join(
        [
            '#{session_name}',
            '#{pane_id}',
            '#{window_width}',
            '#{pane_width}',
            '#{@ccb_project_id}',
            '#{@ccb_role}',
            '#{@ccb_sidebar_instance}',
            '#{@ccb_managed_by}',
        ]
    )
    try:
        cp = runner(['list-panes', '-a', '-F', fmt], capture=True, check=False, timeout=0.5)
    except Exception:
        return []
    if getattr(cp, 'returncode', 1) != 0:
        return []
    records: list[dict[str, str]] = []
    for line in (getattr(cp, 'stdout', '') or '').splitlines():
        parts = [part.strip() for part in line.split('\t')]
        if len(parts) != 8:
            continue
        (
            pane_session,
            pane_id,
            window_width,
            pane_width,
            pane_project_id,
            role,
            sidebar_instance,
            managed_by,
        ) = parts
        if pane_session != session_name or role != 'sidebar' or managed_by != 'ccbd':
            continue
        if project_id and pane_project_id != project_id:
            continue
        if not pane_id.startswith('%') or not sidebar_instance:
            continue
        records.append(
            {
                'pane_id': pane_id,
                'window_width': window_width,
                'pane_width': pane_width,
                'sidebar_instance': sidebar_instance,
            }
        )
    return records


def _resize_pane_width(backend, pane_id: str, width: int, *, timeout_s: float | None = None) -> None:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return
    try:
        runner(
            ['resize-pane', '-t', pane_id, '-x', str(max(1, int(width)))],
            check=False,
            capture=True,
            timeout=timeout_s,
        )
    except Exception:
        return


def _sidebar_width_cells(width: object, window_width: int) -> int:
    usable_width = max(1, int(window_width or 0))
    target = _sidebar_width_target_cells(width, usable_width)
    min_user_width = 10 if usable_width > 20 else 1
    max_width = max(1, usable_width - min_user_width)
    return max(1, min(max_width, int(target)))


def _sidebar_width_target_cells(width: object, window_width: int) -> int:
    text = str(width or '').strip()
    if text.endswith('%'):
        return round(max(1, int(window_width or 0)) * (_sidebar_percent(text) / 100.0))
    try:
        return int(text)
    except Exception:
        return round(max(1, int(window_width or 0)) * 0.15)


def _pane_width_cells(backend, pane_id: str) -> int:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return 0
    try:
        cp = runner(
            ['display-message', '-p', '-t', pane_id, '#{pane_width}'],
            capture=True,
            check=False,
            timeout=0.5,
        )
    except Exception:
        return 0
    if getattr(cp, 'returncode', 1) != 0:
        return 0
    return _positive_int(((getattr(cp, 'stdout', '') or '').splitlines() or [''])[0])


def _session_sidebar_width_override(backend, session_name: str) -> int:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return 0
    try:
        cp = runner(
            ['show-option', '-qv', '-t', session_name, '@ccb_sidebar_width_cells'],
            capture=True,
            check=False,
            timeout=0.5,
        )
    except Exception:
        return 0
    if getattr(cp, 'returncode', 1) != 0:
        return 0
    return _positive_int(((getattr(cp, 'stdout', '') or '').splitlines() or [''])[0])


def _set_session_sidebar_sync_guard(backend, session_name: str, *, enabled: bool) -> None:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return
    args = (
        ['set-option', '-t', session_name, '@ccb_sidebar_sync_guard', '1']
        if enabled
        else ['set-option', '-u', '-t', session_name, '@ccb_sidebar_sync_guard']
    )
    try:
        runner(args, capture=True, check=False, timeout=0.5)
    except Exception:
        return


def _positive_int(value: object) -> int:
    try:
        parsed = int(str(value or '').strip())
    except Exception:
        return 0
    return max(0, parsed)


def _pane_option(backend, pane_id: str, option_name: str) -> str:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return ''
    try:
        cp = runner(
            ['display-message', '-p', '-t', pane_id, f'#{{{option_name}}}'],
            capture=True,
            check=False,
            timeout=0.5,
        )
    except Exception:
        return ''
    if getattr(cp, 'returncode', 1) != 0:
        return ''
    return ((getattr(cp, 'stdout', '') or '').splitlines() or [''])[0].strip()


def _list_panes_by_user_options(backend, expected: dict[str, str]) -> list[str]:
    lister = getattr(backend, 'list_panes_by_user_options', None)
    if callable(lister):
        try:
            return list(lister(expected))
        except Exception:
            return []
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return []
    options = list(expected)
    fmt = '\t'.join(['#{pane_id}', *(f'#{{{option}}}' for option in options)])
    try:
        cp = runner(['list-panes', '-a', '-F', fmt], capture=True, check=False, timeout=0.5)
    except Exception:
        return []
    if getattr(cp, 'returncode', 1) != 0:
        return []
    matches: list[str] = []
    for line in (getattr(cp, 'stdout', '') or '').splitlines():
        parts = line.split('\t')
        if len(parts) != len(options) + 1:
            continue
        pane_id = parts[0].strip()
        if not pane_id.startswith('%'):
            continue
        if all((parts[index + 1] or '').strip() == expected[option] for index, option in enumerate(options)):
            matches.append(pane_id)
    return matches


def _matching_pane_ids(
    pane_records: dict[str, ProjectNamespacePaneRecord] | None,
    backend,
    expected: dict[str, str],
    *,
    tmux_session_name: str | None,
    namespace_epoch: int | None,
    require_alive: bool = True,
) -> list[str]:
    project_id = str(expected.get('@ccb_project_id', '') or '').strip()
    resolved_session = str(tmux_session_name or '').strip()
    if not project_id or not resolved_session or namespace_epoch is None:
        return []
    candidate_expected = dict(expected)
    if namespace_epoch is not None:
        candidate_expected['@ccb_namespace_epoch'] = str(namespace_epoch)
    if pane_records is None:
        candidate_ids = _list_panes_by_user_options(backend, candidate_expected)
        candidates = (
            (pane_id, inspect_project_namespace_pane(backend, pane_id))
            for pane_id in candidate_ids
        )
    else:
        candidates = pane_records.items()
    matches: list[str] = []
    for pane_id, record in candidates:
        if record is None or is_tmux_query_unknown(record):
            continue
        if not record.matches_authoritative_topology(
            tmux_session_name=resolved_session,
            project_id=project_id,
            role=expected.get('@ccb_role'),
            slot_key=expected.get('@ccb_slot'),
            window_name=expected.get('@ccb_window'),
            sidebar_instance=expected.get('@ccb_sidebar_instance'),
            managed_by=str(expected.get('@ccb_managed_by', 'ccbd') or 'ccbd'),
            namespace_epoch=namespace_epoch,
            require_alive=require_alive,
        ):
            continue
        if all(
            option
            in {
                '@ccb_project_id',
                '@ccb_role',
                '@ccb_slot',
                '@ccb_window',
                '@ccb_sidebar_instance',
                '@ccb_managed_by',
                '@ccb_namespace_epoch',
            }
            or _pane_record_option(record, option) == value
            for option, value in expected.items()
        ):
            matches.append(pane_id)
    return matches


def _authoritative_namespace_epoch(context, *, explicit: int | None = None) -> int | None:
    candidate = explicit
    if candidate is None:
        candidate = getattr(getattr(context, 'current', None), 'namespace_epoch', None)
    try:
        value = int(candidate)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _authoritative_tmux_session_name(context) -> str | None:
    candidate = getattr(context, 'desired_session_name', None)
    if candidate is None:
        candidate = getattr(getattr(context, 'current', None), 'tmux_session_name', None)
    value = str(candidate or '').strip()
    return value or None


def _pane_record_option(record: ProjectNamespacePaneRecord, option: str) -> str:
    field_names = {
        '@ccb_project_id': 'project_id',
        '@ccb_role': 'role',
        '@ccb_slot': 'slot_key',
        '@ccb_window': 'ccb_window',
        '@ccb_sidebar_instance': 'sidebar_instance',
        '@ccb_managed_by': 'managed_by',
        '@ccb_namespace_epoch': 'namespace_epoch',
    }
    field_name = field_names.get(option)
    if field_name is None:
        return ''
    return str(getattr(record, field_name, None) or '').strip()


def _sidebar_percent(width: object) -> int:
    text = str(width or '').strip()
    if text.endswith('%'):
        text = text[:-1]
    try:
        value = int(text)
    except Exception:
        return 15
    return max(1, min(90, value))


def _user_pane_percent_for_sidebar(width: object, pane_width: int = 0) -> int:
    if pane_width > 0:
        sidebar_cells = _sidebar_width_cells(width, pane_width)
        user_cells = max(1, int(pane_width) - sidebar_cells)
        return max(1, min(99, round((user_cells * 100) / int(pane_width))))
    return max(10, min(99, 100 - _sidebar_percent(width)))


def _sidebar_pane_percent_for_sidebar(width: object, pane_width: int = 0) -> int:
    if pane_width > 0:
        sidebar_cells = _sidebar_width_cells(width, pane_width)
        return max(1, min(99, round((sidebar_cells * 100) / int(pane_width))))
    return _sidebar_percent(width)


def _respawn_sidebar(backend, pane_id: str, launch_args: tuple[str, ...], *, cwd: str) -> None:
    args = sidebar_respawn_args(tuple(launch_args or ()))
    command = ' '.join(shlex.quote(str(part)) for part in args) if args else pane_placeholder_cmd()
    command = f'CCB_SIDEBAR_THEME_PROFILE={shlex.quote(tmux_theme_profile())} {command}'
    respawn_pane(backend, pane_id=pane_id, command=command, cwd=cwd)


__all__ = [
    'existing_topology_cmd_pane',
    'existing_topology_agent_panes',
    'materialize_topology',
    'refresh_topology_sidebar_helpers',
    'refresh_topology_ui',
    'refresh_topology_ui_for_project',
    'sync_topology_sidebar_widths',
    'topology_active_panes',
    'topology_recreate_reason',
]
