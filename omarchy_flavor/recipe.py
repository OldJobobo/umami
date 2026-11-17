"""Load and validate a flavor recipe (TOML)."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
        if ttype in ("bind.add", "bind.replace") and "run" not in raw and "lua" not in raw:
            raise RecipeError(f"{where} ({tid}): {ttype} needs run or lua")
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
    return recipe
