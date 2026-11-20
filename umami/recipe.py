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
    "plugin.hypr": ["plugin", "file"],
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


# Transforms that may say `locked = true`: loaded after the user's own
# Hyprland files, so the flavor's setting or key wins. shell.plugins.remove is
# always locked: it takes the plugin out of the tree.
LOCKABLE = {"lua.set", "lua", "bind.add", "bind.replace", "bind.remove", "plugin.hypr", "shell.plugins.remove"}

# Transforms that may say `over_theme = true`: loaded right after the theme's
# Hyprland settings, so the flavor's look wins over the theme's while the
# user's own Hyprland files still win over the flavor.
OVER_THEME = {"lua.set", "lua"}


class UmamiError(Exception):
  """Something umami can't do, said in a sentence; main reports it once."""


class RecipeError(UmamiError):
  pass


@dataclass
class Transform:
  id: str
  type: str
  required: bool
  options: dict
  locked: bool = False
  over_theme: bool = False

  def get(self, key, default=None):
    return self.options.get(key, default)


@dataclass
class Recipe:
  name: str
  path: Path
  omarchy: str = ""
  transforms: list = field(default_factory=list)
  plugins: list = field(default_factory=list)
  packages: list = field(default_factory=list)
  suites: list = field(default_factory=list)
  defaults: dict = field(default_factory=dict)
  locked: dict = field(default_factory=dict)
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



def plain_text(value) -> bool:
  """A non-empty, one-line string: names and ids end up in Lua comments and
  command lines, where a line break would start new code."""
  return isinstance(value, str) and bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)

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
  if not plain_text(name):
    raise RecipeError("[flavor] name must be one line of text")

  recipe = Recipe(name=name, path=path, omarchy=meta.get("omarchy", ""))
  seen = set()
  for index, raw in enumerate(data.get("transform", [])):
    where = f"transform #{index + 1}"
    tid = raw.get("id")
    ttype = raw.get("type")
    if not tid or not ttype:
      raise RecipeError(f"{where}: needs id and type")
    if not plain_text(tid):
      raise RecipeError(f"{where}: id must be one line of text")
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
    if ttype == "plugin.hypr" and not tiers.plugin_file_ok(raw.get("file")):
      raise RecipeError(f"{where} ({tid}): plugin.hypr file must be a relative .lua path inside the plugin, "
                        f"got {raw.get('file')!r}")
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
    locked = raw.get("locked", False)
    if not isinstance(locked, bool):
      raise RecipeError(f"{where} ({tid}): locked must be true or false")
    if locked and ttype not in LOCKABLE:
      raise RecipeError(f"{where} ({tid}): {ttype} cannot be locked (only {', '.join(sorted(LOCKABLE))})")
    over_theme = raw.get("over_theme", False)
    if not isinstance(over_theme, bool):
      raise RecipeError(f"{where} ({tid}): over_theme must be true or false")
    if over_theme and ttype not in OVER_THEME:
      raise RecipeError(f"{where} ({tid}): {ttype} cannot be over_theme (only {', '.join(sorted(OVER_THEME))})")
    if over_theme and locked:
      raise RecipeError(f"{where} ({tid}): locked already wins over the theme; drop over_theme")
    options = {k: v for k, v in raw.items() if k not in ("id", "type", "required", "locked", "over_theme")}
    recipe.transforms.append(Transform(tid, ttype, bool(raw.get("required", False)), options,
                                       locked or ttype == "shell.plugins.remove", over_theme))
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

  recipe.packages = check_packages(data.get("package", []))
  recipe.suites = check_suites(data.get("suite", []), {p["name"] for p in recipe.packages}, seen_plugins)

  # plugin.hypr loads a file from a plugin the user consents to, so the
  # plugin must be one of this recipe's own.
  for t in recipe.transforms:
    if t.type == "plugin.hypr" and t.get("plugin") not in seen_plugins:
      raise RecipeError(f"{t.id}: plugin.hypr names {t.get('plugin')!r}, which is not a [[plugin]] of this recipe")

  # Per-user settings: [defaults] once, at a user's first login on the
  # flavor (the user's later choice stays); [locked] at every login.
  recipe.defaults = check_user_table("defaults", data.get("defaults", {}))
  recipe.locked = check_user_table("locked", data.get("locked", {}))
  return recipe


def check_user_table(name: str, table) -> dict:
  """[defaults] or [locked]: a theme and a bar layout."""
  if not isinstance(table, dict):
    raise RecipeError(f"[{name}] must be a table")
  unknown = set(table) - {"theme", "bar"}
  if unknown:
    raise RecipeError(f"[{name}]: unknown keys {', '.join(sorted(unknown))}")
  if "theme" in table and not (isinstance(table["theme"], str) and table["theme"].strip()):
    raise RecipeError(f"[{name}] theme must be a theme name")
  if "bar" in table:
    check_bar_defaults(table["bar"], name)
  return dict(table)


BAR_SECTIONS = {"left", "centerBegin", "center", "centerEnd", "tray", "right"}


def check_bar_defaults(bar, name="defaults"):
  """[defaults.bar] or [locked.bar]: widgets to take off the bar, add, move, and configure."""
  if not isinstance(bar, dict):
    raise RecipeError(f"[{name}.bar] must be a table")
  unknown = set(bar) - {"remove", "add", "move", "set"}
  if unknown:
    raise RecipeError(f"[{name}.bar]: unknown keys {', '.join(sorted(unknown))}")
  remove = bar.get("remove", [])
  if not (isinstance(remove, list) and all(isinstance(i, str) and i for i in remove)):
    raise RecipeError(f"[{name}.bar] remove must be a list of widget ids")
  for kind in ("add", "move"):
    for entry in bar.get(kind, []):
      if not isinstance(entry, dict) or set(entry) - {"id", "section", "index"} or not plain_text(entry.get("id")) \
              or entry.get("section") not in BAR_SECTIONS:
        raise RecipeError(f"[{name}.bar] {kind} entries need id and a section ({', '.join(sorted(BAR_SECTIONS))}), optional index")
      if "index" in entry and not (type(entry["index"]) is int and entry["index"] >= 0):
        raise RecipeError(f"[{name}.bar] {kind} index must be a whole number")
  for entry in bar.get("set", []):
    if not isinstance(entry, dict) or set(entry) != {"id", "key", "value"} or not isinstance(entry["value"], str) \
            or not (plain_text(entry["id"]) and plain_text(entry["key"])):
      raise RecipeError(f"[{name}.bar] set entries need id, key and a string value")


PACKAGE_NAME = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]*$")
COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SUITE_ARG = re.compile(r"^-{0,2}[A-Za-z0-9][A-Za-z0-9._=-]*$")


def check_packages(raw_list) -> list:
  """[[package]]: a package from a pacman repository this machine already
  trusts (signed, in pacman.conf), installed by `umami flavor`. Never the
  AUR, a file or a URL: only a name, which pacman resolves."""
  if not isinstance(raw_list, list):
    raise RecipeError("[[package]] must be a list of tables")
  packages, seen = [], set()
  for index, raw in enumerate(raw_list):
    where = f"package #{index + 1}"
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not PACKAGE_NAME.match(raw["name"]):
      raise RecipeError(f"{where}: needs a package name")
    unknown = set(raw) - {"name", "note"}
    if unknown:
      raise RecipeError(f"{where} ({raw['name']}): unknown keys {', '.join(sorted(unknown))}")
    if raw["name"] in seen:
      raise RecipeError(f"{where}: duplicate package {raw['name']!r}")
    seen.add(raw["name"])
    packages.append(dict(raw))
  return packages


def check_suites(raw_list, packages: set, plugins: set) -> list:
  """[[suite]]: a package's own installer for a set of shell plugins, run as
  each user after they say yes. The command must belong to one of the
  recipe's [[package]]s, so the code that runs is the package's; the flavor
  only names its steps (install, update after the package changes,
  uninstall when a flavor without it takes over), as plain arguments."""
  if not isinstance(raw_list, list):
    raise RecipeError("[[suite]] must be a list of tables")
  suites, seen = [], set()
  for index, raw in enumerate(raw_list):
    where = f"suite #{index + 1}"
    if not isinstance(raw, dict):
      raise RecipeError(f"{where}: must be a table")
    missing = [k for k in ("id", "package", "command", "install", "update", "uninstall") if k not in raw]
    if missing:
      raise RecipeError(f"{where}: needs {', '.join(missing)}")
    sid = raw["id"]
    if not (isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", sid)):
      raise RecipeError(f"{where}: bad suite id {sid!r}")
    if sid in seen or sid in plugins:
      raise RecipeError(f"{where}: duplicate id {sid!r}")
    seen.add(sid)
    unknown = set(raw) - {"id", "package", "command", "install", "update", "uninstall", "note"}
    if unknown:
      raise RecipeError(f"{where} ({sid}): unknown keys {', '.join(sorted(unknown))}")
    if raw["package"] not in packages:
      raise RecipeError(f"{where} ({sid}): package {raw['package']!r} is not a [[package]] of this recipe")
    if not (isinstance(raw["command"], str) and COMMAND_NAME.match(raw["command"])):
      raise RecipeError(f"{where} ({sid}): command must be a bare command name")
    for step in ("install", "update", "uninstall"):
      args = raw[step]
      if not (isinstance(args, list) and args and all(isinstance(a, str) and SUITE_ARG.match(a) for a in args)):
        raise RecipeError(f"{where} ({sid}): {step} must be a non-empty list of plain arguments")
    suites.append(dict(raw))
  return suites
