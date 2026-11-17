"""Load and validate a flavor recipe (TOML)."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import tiers

# Transform type -> keys it requires (beyond id/type/required).
REQUIRED_KEYS = {
    "file.add": ["path", "source"],
    "file.remove": ["path"],
    "patch": ["file", "anchor"],
    "diff": [],
    "lua.set": ["path", "value"],
    "lua": [],
    "bind.add": ["keys", "description"],
    "bind.replace": ["keys", "description"],
    "bind.remove": ["keys"],
    "command.wrap": ["command"],
    "command.replace": ["command", "source"],
    "shell.plugins.add": ["source"],
    "shell.plugins.remove": ["ids"],
    "shell.plugins.replace": ["plugin", "source"],
    "shell": ["source"],
}


class RecipeError(Exception):
    pass


@dataclass
class Transform:
    id: str
    type: str
    required: bool
    options: dict

    def get(self, key, default=None):
        return self.options.get(key, default)


@dataclass
class Recipe:
    name: str
    path: Path
    omarchy: str = ""
    transforms: list = field(default_factory=list)
    plugins: list = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    code: bool = False
    code_transforms: list = field(default_factory=list)

    @property
    def root(self) -> Path:
        """Directory that relative `source` paths resolve against."""
        return self.path.parent

    def resolve_source(self, source: str) -> Path:
        resolved = (self.root / source).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise RecipeError(f"source escapes the recipe directory: {source}")
        return resolved


def load(path) -> Recipe:
    path = Path(path).resolve()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RecipeError(f"cannot read recipe {path}: {error}") from error

    meta = data.get("flavor", {})
    name = meta.get("name")
    if not name:
        raise RecipeError("recipe needs [flavor] name = \"...\"")

    recipe = Recipe(name=name, path=path, omarchy=meta.get("omarchy", ""))
    seen = set()
    for index, raw in enumerate(data.get("transform", [])):
        where = f"transform #{index + 1}"
        tid = raw.get("id")
        ttype = raw.get("type")
        if not tid or not ttype:
            raise RecipeError(f"{where}: needs id and type")
        if tid in seen:
            raise RecipeError(f"{where}: duplicate id {tid!r}")
        seen.add(tid)
        if ttype not in REQUIRED_KEYS:
            raise RecipeError(f"{where} ({tid}): unknown type {ttype!r}")
        missing = [key for key in REQUIRED_KEYS[ttype] if key not in raw]
        if missing:
            raise RecipeError(f"{where} ({tid}): {ttype} needs {', '.join(missing)}")
        if ttype in ("bind.add", "bind.replace"):
            if sum(k in raw for k in ("action", "run", "lua")) != 1:
                raise RecipeError(f"{where} ({tid}): {ttype} needs exactly one of action, run, lua")
            if "action" in raw:
                try:
                    tiers.check_action(raw["action"])
                except ValueError as error:
                    raise RecipeError(f"{where} ({tid}): {error}") from error
        if ttype == "lua" and ("code" in raw) == ("source" in raw):
            raise RecipeError(f"{where} ({tid}): lua needs exactly one of code, source")
        if ttype == "diff":
            sources = raw.get("sources")
            if ("source" in raw) == (sources is not None):
                raise RecipeError(f"{where} ({tid}): diff needs exactly one of source, sources")
            if sources is not None and (not isinstance(sources, list) or not sources or not all(isinstance(x, str) for x in sources)):
                raise RecipeError(f"{where} ({tid}): sources must be a non-empty list of paths")
        if ttype == "patch" and sum(k in raw for k in ("insert_before", "insert_after", "replace_with")) != 1:
            raise RecipeError(f"{where} ({tid}): patch needs exactly one of insert_before, insert_after, replace_with")
        options = {k: v for k, v in raw.items() if k not in ("id", "type", "required")}
        recipe.transforms.append(Transform(tid, ttype, bool(raw.get("required", False)), options))
        reason = tiers.code_reason(ttype, options)
        if reason:
            recipe.code_transforms.append((tid, reason))

    # Declarative unless the recipe says otherwise (see tiers.py).
    code = meta.get("code", False)
    if not isinstance(code, bool):
        raise RecipeError("[flavor] code must be true or false")
    recipe.code = code
    if recipe.code_transforms and not code:
        listed = "; ".join(f"{tid} {reason}" for tid, reason in recipe.code_transforms)
        raise RecipeError(f"these transforms run code: {listed}. A flavor that runs code is trusted like a "
                          "package; add code = true to [flavor] to allow it")
    seen_plugins = set()
    for index, raw in enumerate(data.get("plugin", [])):
        where = f"plugin #{index + 1}"
        if not isinstance(raw, dict):
            raise RecipeError(f"{where}: must be a table")
        missing = [key for key in ("id", "repo", "commit") if not raw.get(key)]
        if missing:
            raise RecipeError(f"{where}: needs {', '.join(missing)}")
        if not re.fullmatch(r"[0-9a-f]{40}", str(raw["commit"])):
            raise RecipeError(f"{where} ({raw['id']}): commit must be a full 40-character sha, so the pin is exact")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(raw["id"])):
            raise RecipeError(f"{where}: bad plugin id {raw['id']!r}")
        if raw["id"] in seen_plugins:
            raise RecipeError(f"{where}: duplicate plugin {raw['id']!r}")
        seen_plugins.add(raw["id"])
        unknown = set(raw) - {"id", "repo", "commit", "section", "index", "note"}
        if unknown:
            raise RecipeError(f"{where} ({raw['id']}): unknown keys {', '.join(sorted(unknown))}")
        recipe.plugins.append(dict(raw))

    # Per-user settings applied once, at a user's first login on the flavor.
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise RecipeError("[defaults] must be a table")
    unknown = set(defaults) - {"theme", "bar"}
    if unknown:
        raise RecipeError(f"[defaults]: unknown keys {', '.join(sorted(unknown))}")
    if "theme" in defaults and not (isinstance(defaults["theme"], str) and defaults["theme"].strip()):
        raise RecipeError("[defaults] theme must be a theme name")
    if "bar" in defaults:
        check_bar_defaults(defaults["bar"])
    recipe.defaults = dict(defaults)
    return recipe


BAR_SECTIONS = {"left", "centerBegin", "center", "centerEnd", "tray", "right"}


def check_bar_defaults(bar):
    """[defaults.bar]: widgets to take off the bar, move, and configure."""
    if not isinstance(bar, dict):
        raise RecipeError("[defaults.bar] must be a table")
    unknown = set(bar) - {"remove", "move", "set"}
    if unknown:
        raise RecipeError(f"[defaults.bar]: unknown keys {', '.join(sorted(unknown))}")
    remove = bar.get("remove", [])
    if not (isinstance(remove, list) and all(isinstance(i, str) and i for i in remove)):
        raise RecipeError("[defaults.bar] remove must be a list of widget ids")
    for entry in bar.get("move", []):
        if not isinstance(entry, dict) or set(entry) - {"id", "section", "index"} or not entry.get("id") \
                or entry.get("section") not in BAR_SECTIONS:
            raise RecipeError(f"[defaults.bar] move entries need id and a section ({', '.join(sorted(BAR_SECTIONS))}), optional index")
        if "index" in entry and not (isinstance(entry["index"], int) and entry["index"] >= 0):
            raise RecipeError("[defaults.bar] move index must be a whole number")
    for entry in bar.get("set", []):
        if not isinstance(entry, dict) or set(entry) != {"id", "key", "value"} or not isinstance(entry["value"], str):
            raise RecipeError("[defaults.bar] set entries need id, key and a string value")
