"""Read only the current default tmux composer; unknown layouts fail closed."""
from __future__ import annotations

import re
from dataclasses import dataclass

_SGR = re.compile(r'\x1b\[([0-9;]*)m')
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_BORDER = re.compile(r'^─{8,}\s*$')


@dataclass(frozen=True)
class Observation:
    state: str
    binding: str
    reason: str
    # Local patch (ccb-team-kit): the visible composer text, so a caller can prove
    # whether the box holds this job's own prompt before clearing or pasting.
    # Empty when the provider never exposes contents (OMP) or the layout is unknown.
    content: str = ''


def _styled_lines(text: str) -> list[list[tuple[str, bool, bool]]]:
    lines = [[]]
    dim = inverse = False
    pos = 0
    while pos < len(text):
        match = _SGR.match(text, pos)
        if match:
            codes = [int(value or 0) for value in match.group(1).split(';')]
            index = 0
            while index < len(codes):
                code = codes[index]
                if code == 0:
                    dim = inverse = False
                elif code == 2:
                    dim = True
                elif code == 22:
                    dim = False
                elif code == 7:
                    inverse = True
                elif code == 27:
                    inverse = False
                elif code in {38, 48, 58} and index+1 < len(codes):
                    index += 4 if codes[index+1] == 2 else 2
                index += 1
            pos = match.end()
            continue
        other = _ANSI.match(text, pos)
        if other:
            pos = other.end()
            continue
        char = text[pos]
        if char == '\n':
            lines.append([])
        else:
            lines[-1].append((char, dim, inverse))
        pos += 1
    return lines


def inspect_screen(provider: str, screen: dict, *, binding: str) -> Observation:
    styled = _styled_lines(screen['text'])
    lines = [''.join(char for char, _, _ in line) for line in styled]
    cursor_x, cursor_y = screen['cursor_x'], screen['cursor_y']
    def result(state, reason, content=''):
        return Observation(state, binding, reason, content)
    if provider == 'codex':
        arrows = [i for i, line in enumerate(lines) if line.startswith('›') and i <= cursor_y]
        if not arrows:
            return result('unknown', 'composer_missing')
        top = arrows[-1]
        # The phrase alone also occurs in ordinary answers and user drafts.
        # Only the native animated status row outside the composer vetoes send.
        # Join its continuation rows for narrow terminals.
        if any(re.match(r'^[•◦]\s', line) and re.search(
                r'\([^)]*esc to interrupt', ' '.join(lines[i:min(i+3, top)]), re.I)
               for i, line in enumerate(lines[:top])):
            return result('unknown', 'provider_busy')
        # Default main composer has a status footer below the cursor. Selection
        # menus use the same arrow; their confirmation footer is not accepted.
        footer = _codex_footer(lines, styled, cursor_y)
        if footer is None:
            return result('unknown', 'composer_layout_unknown')
        if _editor_mode_in_footer(lines[footer:]):
            return result('unknown', 'unsupported_editor_mode')
        content = lines[top][2:]
        draft = '\n'.join([content, *lines[top+1:footer]])
        # Styled terminal rows may retain right-hand padding. This is still
        # the fixed placeholder at its initial cursor, not a typed draft.
        if content.rstrip(' ') == 'Ask Codex to do anything' and cursor_y == top and cursor_x == 2:
            if all(not line.strip() for line in lines[top+1:footer]):
                return result('empty', 'codex_placeholder')
        if any(line.strip() for line in [content, *lines[top+1:footer]]) or cursor_y != top or cursor_x > 2:
            return result('nonempty', 'codex_draft', draft)
        return result('unknown', 'codex_no_placeholder')
    if provider != 'claude':
        return result('unknown', 'unsupported_provider')
    borders = [i for i, line in enumerate(lines) if _BORDER.fullmatch(line)]
    pairs = [(a, b) for a, b in zip(borders, borders[1:]) if a < cursor_y < b]
    if not pairs:
        return result('unknown', 'composer_layout_unknown')
    top, bottom = pairs[-1]
    if not lines[top+1].startswith('❯'):
        return result('unknown', 'composer_not_focused')
    # Claude may show elapsed time/tokens without an "esc to interrupt" hint.
    # Its animated flower + ellipsis is distinct from completed "Worked for"
    # status and normal assistant bullets. Never inspect the draft for status.
    if any(re.match(r'^[✢✳✶✻✽·]\s+.*(?:…|\.\.\.)', line) for line in lines[:top]):
        return result('unknown', 'provider_busy')
    if _editor_mode_in_footer(lines[bottom+1:]):
        return result('unknown', 'unsupported_editor_mode')
    draft = '\n'.join([lines[top+1][1:], *lines[top+2:bottom]]).strip()
    content = styled[top+1][2:] + [cell for row in styled[top+2:bottom] for cell in row]
    visible = [cell for cell in content if not cell[0].isspace()]
    if not visible:
        if bottom == top+2 and cursor_y == top+1 and cursor_x == 2:
            return result('empty', 'claude_blank')
        return result('nonempty', 'claude_whitespace_draft', draft)
    # A virtual cursor may render the first ghost character in inverse video.
    # Require the rest to be dim, and at least one actual dim glyph.
    ghost = all(dim or (index == 0 and inverse) for index, (_, dim, inverse) in enumerate(visible))
    if ghost and any(dim for _, dim, _ in visible) and cursor_x == 2 and cursor_y == top+1:
        if 'Press up to edit queued messages' in lines[top+1]:
            return result('unknown', 'provider_native_queue_pending')
        return result('empty', 'claude_ghost')
    return result('nonempty', 'claude_draft', draft)


def _codex_footer(lines, styled, cursor_y: int) -> int | None:
    # The status bar is the last nonblank row, separated from the editor by
    # a blank row. Its configurable labels (including model names) are opaque.
    footer = next((i for i in range(len(lines)-1, cursor_y, -1) if lines[i].strip()), None)
    if footer is None or footer <= cursor_y+1 or lines[footer-1].strip():
        return None
    row = lines[footer]
    if not row.startswith('  '):
        return None
    if re.match(r'^  (?:\d+% [Cc]ontext\b|\? for shortcuts\b)', row):
        return footer
    # Custom status bars separate fields with a dim middle dot. Require the
    # rendering attribute as well as spacing; ordinary draft prose is not a
    # status bar just because it contains a dot or a model-like word.
    if any(char == '·' and dim and 0 < i < len(row)-1
           and row[i-1:i+2] == ' · '
           for i, (char, dim, _) in enumerate(styled[footer])):
        return footer
    return None


def _editor_mode_in_footer(lines: list[str]) -> bool:
    return any(re.search(r'\b(?:INSERT|NORMAL|VISUAL)\b', line) for line in lines)
