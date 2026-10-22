"""Read KiCad library tables and packed/unpacked symbols without GUI dependencies."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Callable


_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+')


def _atom(value: str) -> str:
    if not value.startswith('"'):
        return value
    return re.sub(r'\\(.)', lambda m: {'n': '\n', 'r': '\r', 't': '\t'}.get(m[1], m[1]), value[1:-1])


def expressions(text: str) -> list:
    """Parse balanced S-expressions, preserving escaped string values."""
    root: list = []
    stack = [root]
    for match in _TOKEN.finditer(text):
        token = match[0]
        if token == '(':
            child: list = []
            stack[-1].append(child)
            stack.append(child)
        elif token == ')':
            if len(stack) == 1:
                raise ValueError('Unbalanced KiCad expression')
            stack.pop()
        else:
            stack[-1].append(_atom(token))
    if len(stack) != 1:
        raise ValueError('Unbalanced KiCad expression')
    return root


def read_library_table(path: str, expand: Callable[[str], str], _seen=None) -> dict[str, dict[str, str]]:
    """Flatten Table entries; preserve nicknames and ignore disabled rows/cycles.

    Callers merge project tables before global tables with setdefault, matching
    KiCad's project nickname precedence. Hidden base libraries remain available.
    """
    table = Path(path).resolve()
    seen = set() if _seen is None else _seen
    if table in seen:
        return {}
    seen.add(table)
    try:
        roots = expressions(table.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if len(roots) != 1 or not roots[0] or roots[0][0] not in ('sym_lib_table', 'fp_lib_table'):
        return {}
    libs = {}
    for row in roots[0][1:]:
        if not isinstance(row, list) or not row or row[0] != 'lib':
            continue
        fields = {field[0]: field[1:] for field in row[1:] if isinstance(field, list) and field}
        if 'disabled' in fields:
            continue
        def field(key):
            return str((fields.get(key) or [''])[0])
        name, kind, uri = field('name'), field('type'), expand(field('uri'))
        if not name or not uri:
            continue
        if not Path(uri).is_absolute():
            uri = str(table.parent / uri)
        if kind.casefold() == 'table':
            for nick, entry in read_library_table(uri, expand, seen).items():
                libs.setdefault(nick, entry)
        else:
            libs.setdefault(name, {'type': kind, 'uri': uri, 'descr': field('descr')})
    return libs


def symbol_files(path: str | Path) -> list[Path]:
    library = Path(path)
    if library.is_dir():
        return sorted(library.glob('*.kicad_sym'))
    return [library] if library.is_file() else []


def symbol_entries(path: str | Path):
    """Yield top-level symbols, including multi-symbol files in unpacked dirs."""
    for file in symbol_files(path):
        try:
            roots = expressions(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if len(roots) != 1 or not roots[0] or roots[0][0] != 'kicad_symbol_lib':
            continue
        for node in roots[0][1:]:
            if isinstance(node, list) and len(node) > 1 and node[0] == 'symbol':
                yield str(node[1]), node


def symbol_names(path: str | Path, nickname: str) -> list[str]:
    return sorted({f'{nickname}:{name}' for name, _ in symbol_entries(path)})


def symbol_metadata(path: str | Path, nickname: str) -> dict[str, tuple[str, str]]:
    result = {}
    for name, node in symbol_entries(path):
        props = {p[1]: p[2] for p in node[2:]
                 if isinstance(p, list) and len(p) > 2 and p[0] == 'property'}
        result[f'{nickname}:{name}'] = (props.get('Description', ''), props.get('Datasheet', ''))
    return result
