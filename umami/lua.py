"""Small helpers for emitting and inspecting Omarchy's Hyprland Lua."""

import re
from pathlib import Path

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BIND_CALL = re.compile(r'\bo\.(?:bind|rebind|bind_toggle|bind_hold)\(\s*"([^"]+)"')


def literal(value) -> str:
    """Render a TOML value as a Lua literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, list):
        return "{ " + ", ".join(literal(item) for item in value) + " }"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            name = key if _IDENT.match(key) else f"[{literal(key)}]"
            parts.append(f"{name} = {literal(item)}")
        return "{ " + ", ".join(parts) + " }"
    raise ValueError(f"cannot render {type(value).__name__} as Lua")


def nested(path: str, value) -> dict:
    """general.gaps_out = 4  ->  {"general": {"gaps_out": 4}}"""
    keys = path.split(".")
    if not all(keys):
        raise ValueError(f"bad config path: {path!r}")
    result = value
    for key in reversed(keys):
        result = {key: result}
    return result


def normalize_keys(keys: str) -> str:
    return " + ".join(part.strip().upper() for part in keys.split("+"))


def literal_bindings(hypr_dir: Path) -> dict:
    """Map normalized key chords to the file that binds them literally.

    Chords built at runtime (loops, string concatenation) are invisible here,
    so a transform targeting one cannot be checked and is reported stale.
    """
    found = {}
    for path in sorted(hypr_dir.rglob("*.lua")):
        for match in _BIND_CALL.finditer(path.read_text(errors="replace")):
            found.setdefault(normalize_keys(match.group(1)), path.relative_to(hypr_dir.parent.parent))
    return found
