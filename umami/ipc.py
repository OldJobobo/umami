"""Static extraction of the shell's IPC surface from QML.

Omarchy declares IPC with `ShellIpc { target: ...; function name(...) }`
(or a raw `IpcHandler`). A replacement plugin or shell has to answer the same
calls, or the commands and keybindings that drive it break silently.
"""

import re
from pathlib import Path

_BLOCK_START = re.compile(r"\b(?:ShellIpc|IpcHandler)\s*\{")
_TARGET = re.compile(r"^\s*target\s*:\s*(.+?)\s*$", re.MULTILINE)
_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _blocks(text: str):
    for match in _BLOCK_START.finditer(text):
        depth, start = 0, match.end() - 1
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    yield text[start + 1:index]
                    break


def surface(root: Path) -> dict:
    """Map each IPC target (as written: a string literal or an expression)
    to the set of functions it answers."""
    result = {}
    for path in sorted(root.rglob("*.qml")):
        if path.name == "ShellIpc.qml":
            continue  # the component definition, not a use of it
        for body in _blocks(path.read_text(errors="replace")):
            target = _TARGET.search(body)
            name = target.group(1).strip().strip('"') if target else "<no target>"
            functions = set(_FUNCTION.findall(body))
            if functions:
                result.setdefault(name, set()).update(functions)
    return result


def missing(original: dict, replacement: dict) -> list:
    """Functions the original answers that the replacement does not."""
    gaps = []
    for target, functions in sorted(original.items()):
        absent = sorted(functions - replacement.get(target, set()))
        gaps.extend(f"{target}.{name}" for name in absent)
    return gaps
