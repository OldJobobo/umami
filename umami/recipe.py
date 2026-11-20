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
  drops: list = field(default_factory=list)
  suites: list = field(default_factory=list)
  defaults: dict = field(default_factory=dict)
  locked: dict = field(default_factory=dict)
  system: dict = field(default_factory=dict)
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
    unknown = set(raw) - {"id", "repo", "commit", "section", "index", "note", "setup", "teardown"}
    if unknown:
      raise RecipeError(f"{where} ({raw['id']}): unknown keys {', '.join(sorted(unknown))}")
    for step in ("setup", "teardown"):
      if step in raw and not (plain_text(raw[step]) and not raw[step].startswith("/")
                              and ".." not in Path(raw[step]).parts):
        raise RecipeError(f"{where} ({raw['id']}): {step} is a script inside the plugin, such as \"scripts/enable\"")
    if "teardown" in raw and "setup" not in raw:
      raise RecipeError(f"{where} ({raw['id']}): teardown undoes a setup, so it needs one")
    recipe.plugins.append(dict(raw))

  checked = check_packages(data.get("package", []))
  recipe.packages = [p for p in checked if not p.get("remove")]
  recipe.drops = [p["name"] for p in checked if p.get("remove")]
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
  for table in (recipe.defaults, recipe.locked):
    if "background" in table and not recipe.resolve_source(table["background"]).is_file():
      raise RecipeError(f"background {table['background']} is not a file in the recipe's directory")
  recipe.system = check_system(data.get("system", {}))
  return recipe


UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]*")
UNIT_KINDS = (".service", ".socket", ".timer", ".path")


def check_system(table) -> dict:
  """[system]: the machine around Omarchy. Data like the rest: umami turns
  each key into systemd's own settings, records what was there, and
  `umami remove` puts it back."""
  if not isinstance(table, dict):
    raise RecipeError("[system] must be a table")
  unknown = set(table) - {"boot", "sleep", "services", "groups", "timezone", "channel"}
  if unknown:
    raise RecipeError(f"[system]: unknown keys {', '.join(sorted(unknown))}")
  system = {}
  if "boot" in table:
    if table["boot"] not in ("console", "desktop"):
      raise RecipeError('[system] boot is "console" or "desktop"')
    if table["boot"] == "console":
      system["boot"] = "console"
  if "sleep" in table:
    if not isinstance(table["sleep"], bool):
      raise RecipeError("[system] sleep is true or false")
    if not table["sleep"]:
      system["sleep"] = False
  groups = table.get("groups", [])
  if not (isinstance(groups, list) and all(isinstance(g, str) and re.fullmatch(r"[a-z_][a-z0-9_-]*", g) for g in groups)):
    raise RecipeError("[system] groups is a list of group names, such as \"docker\"")
  if groups:
    system["groups"] = list(groups)
  if "timezone" in table:
    tz = table["timezone"]
    if not (isinstance(tz, str) and re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", tz)):
      raise RecipeError('[system] timezone is a zone such as "Europe/Amsterdam"')
    system["timezone"] = tz
  if "channel" in table:
    if table["channel"] not in ("stable", "rc", "edge"):
      raise RecipeError('[system] channel is "stable", "rc" or "edge"')
    system["channel"] = table["channel"]
  services = table.get("services", {})
  if not isinstance(services, dict) or set(services) - {"enable", "disable"}:
    raise RecipeError("[system] services has enable and disable lists")
  seen = set()
  for action in ("enable", "disable"):
    names = services.get(action, [])
    if not (isinstance(names, list) and all(isinstance(n, str) and UNIT_NAME.fullmatch(n) for n in names)):
      raise RecipeError(f"[system] services.{action} must be a list of systemd unit names")
    units = [n if n.endswith(UNIT_KINDS) else n + ".service" for n in names]
    if seen & set(units):
      raise RecipeError(f"[system] services: {', '.join(sorted(seen & set(units)))} is both enabled and disabled")
    seen |= set(units)
    if units:
      system.setdefault("services", {})[action] = units
  return system


SETTING_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
POWER_PROFILES = ("power-saver", "balanced", "performance")


def check_user_table(name: str, table) -> dict:
  """[defaults] or [locked]: a user's theme, bar, and the settings Omarchy
  has a command for. [defaults] also takes [defaults.apps]."""
  if not isinstance(table, dict):
    raise RecipeError(f"[{name}] must be a table")
  keys = {"theme", "bar", "font", "background", "terminal", "browser", "editor", "text_size", "idle", "power_profile"}
  unknown = set(table) - keys - ({"apps"} if name == "defaults" else set())
  if unknown:
    raise RecipeError(f"[{name}]: unknown keys {', '.join(sorted(unknown))}")
  if "theme" in table and not (isinstance(table["theme"], str) and table["theme"].strip()):
    raise RecipeError(f"[{name}] theme must be a theme name")
  if "bar" in table:
    check_bar_defaults(table["bar"], name)
  if "font" in table and not plain_text(table["font"]):
    raise RecipeError(f"[{name}] font must be a font name, as omarchy font list shows it")
  if "background" in table and not (plain_text(table["background"]) and not table["background"].startswith("/")):
    raise RecipeError(f"[{name}] background must be an image or video in the recipe's directory")
  for key in ("terminal", "browser", "editor"):
    if key in table and not (isinstance(table[key], str) and SETTING_NAME.fullmatch(table[key])):
      raise RecipeError(f"[{name}] {key} must be a name, as omarchy default {key} takes it")
  if "text_size" in table and not (type(table["text_size"]) is int and 8 <= table["text_size"] <= 40):
    raise RecipeError(f"[{name}] text_size must be a size in pixels, 8 to 40")
  if "power_profile" in table and table["power_profile"] not in POWER_PROFILES:
    raise RecipeError(f"[{name}] power_profile is one of {', '.join(POWER_PROFILES)}")
  if "idle" in table:
    idle = table["idle"]
    if not (isinstance(idle, dict) and idle and not set(idle) - {"screensaver", "lock"}
            and all(type(v) is int and v >= 0 for v in idle.values())):
      raise RecipeError(f"[{name}.idle] has screensaver and lock, in seconds (0 turns it off)")
  if "apps" in table:
    check_apps(table["apps"])
  return dict(table)


def check_apps(apps):
  """[defaults.apps]: launchers. hide takes entries out of the app launcher;
  webapps and tuis add them, with Omarchy's own installers."""
  if not isinstance(apps, dict) or set(apps) - {"hide", "webapps", "tuis"}:
    raise RecipeError("[defaults.apps] has hide, webapps and tuis")
  launcher = lambda v: plain_text(v) and "/" not in v
  hide = apps.get("hide", [])
  if not (isinstance(hide, list) and all(launcher(n) for n in hide)):
    raise RecipeError("[defaults.apps] hide is a list of launcher names, such as \"YouTube\"")
  for entry in apps.get("webapps", []):
    if not (isinstance(entry, dict) and set(entry) == {"name", "url", "icon"} and launcher(entry["name"])
            and plain_text(entry["url"]) and entry["url"].startswith("https://") and plain_text(entry["icon"])):
      raise RecipeError("[defaults.apps] webapps entries need name, an https url and icon")
  for entry in apps.get("tuis", []):
    if not (isinstance(entry, dict) and set(entry) == {"name", "command", "style", "icon"} and launcher(entry["name"])
            and plain_text(entry["command"]) and entry["style"] in ("float", "tile") and plain_text(entry["icon"])):
      raise RecipeError("[defaults.apps] tuis entries need name, command, style (float or tile) and icon")


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
  AUR, a file or a URL: only a name, which pacman resolves. With
  remove = true, an app Omarchy installed that the flavor leaves out."""
  if not isinstance(raw_list, list):
    raise RecipeError("[[package]] must be a list of tables")
  packages, seen = [], set()
  for index, raw in enumerate(raw_list):
    where = f"package #{index + 1}"
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not PACKAGE_NAME.match(raw["name"]):
      raise RecipeError(f"{where}: needs a package name")
    unknown = set(raw) - {"name", "note", "remove"}
    if unknown:
      raise RecipeError(f"{where} ({raw['name']}): unknown keys {', '.join(sorted(unknown))}")
    if raw["name"] in seen:
      raise RecipeError(f"{where}: duplicate package {raw['name']!r}")
    if not isinstance(raw.get("remove", False), bool):
      raise RecipeError(f"{where} ({raw['name']}): remove is true or false")
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
