"""User layer: marketplace plugins a flavor lists, installed for each user.

Third-party plugins never go into the composed tree. A plugin there is
first-party, and the shell grants a first-party plugin every capability its
manifest declares. As a user plugin in ~/.config/omarchy/plugins it gets none
(a `clonedFrom` replacement inherits only what the stock part it replaces
has, which Omarchy decides). So each user gets them the way `omarchy plugin
add` installs them, through Omarchy's own validation and enable commands,
pinned to the commit the recipe names (for marketplace plugins, the commit
the marketplace validated).

Nothing third-party is installed without the user's yes. At login a plugin
the user hasn't decided on opens a terminal that shows Omarchy's own warning
and asks (`user-sync --review`); until then it is not installed. The repo URL
goes through `omarchy-git-url-check` before anything is cloned, as for
`omarchy plugin add`.

The state file records what this layer installed and what the user turned
down. A plugin the user removed or declined stays away, one the user
installed themselves is left alone, and only a plugin installed here follows
a new pin.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .recipe import load

TREE_RECIPE = Path("/var/lib/umami/tree/.flavor/recipe.toml")
SELF = "/usr/lib/umami/bin/umami"

# Omarchy's own words from omarchy plugin add.
WARNING = """\
Plugins run as arbitrary, unsandboxed code inside your long-lived
omarchy-shell process. Only add repos you trust, and review the code
before you enable it."""
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def plugins_dir() -> Path:
  return Path.home() / ".config/omarchy/plugins"


def state_file() -> Path:
  base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
  return Path(base) / "umami/plugins.json"


def cache_dir() -> Path:
  base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
  return Path(base) / "umami"


def load_state() -> dict:
  try:
    data = json.loads(state_file().read_text())
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def save_state(state: dict):
  path = state_file()
  path.parent.mkdir(parents=True, exist_ok=True)
  staged = path.with_suffix(".tmp")
  staged.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
  staged.replace(path)


class PluginError(Exception):
  pass


def run(command, **kwargs):
  return subprocess.run(command, capture_output=True, text=True, **kwargs)


# omarchy-shell gives a call 2s by default. Right after a login or a shell
# restart the shell can take longer than that to answer, so a plugin enable
# or a bar change reported "not responding" although it ran. A call that
# timed out may have run, so it can't be retried; it gets longer instead.
SHELL_CALL_TIMEOUT = "30s"


def run_shell_call(command):
  return run(command, env={**os.environ, "OMARCHY_SHELL_IPC_TIMEOUT": SHELL_CALL_TIMEOUT})


def fetch(plugin: dict) -> Path:
  """Clone `repo` at exactly `commit` into a staging folder, validated."""
  cache_dir().mkdir(parents=True, exist_ok=True)
  stage = Path(tempfile.mkdtemp(prefix=f"{plugin['id']}.", dir=cache_dir()))
  try:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    # As omarchy plugin add does: refuse a URL that names a git option or
    # a transport helper before cloning. No checker means no clone.
    try:
      check = run(["omarchy-git-url-check", plugin["repo"]])
    except FileNotFoundError as error:
      raise PluginError("omarchy-git-url-check is missing, so the URL can't be checked") from error
    if check.returncode != 0:
      raise PluginError("omarchy-git-url-check refused the URL: " + (check.stderr or check.stdout).strip()[:300])
    clone = run(["git", "clone", "-q", "--no-checkout", "--", plugin["repo"], str(stage)], env=env)
    if clone.returncode != 0:
      raise PluginError(f"cannot clone {plugin['repo']}: {clone.stderr.strip()}")
    checkout = run(["git", "-C", str(stage), "checkout", "-q", "--detach", plugin["commit"]], env=env)
    if checkout.returncode != 0:
      raise PluginError(f"commit {plugin['commit'][:12]} is not in {plugin['repo']}")
    head = run(["git", "-C", str(stage), "rev-parse", "HEAD"]).stdout.strip()
    if head != plugin["commit"]:
      raise PluginError(f"checked out {head[:12]}, not {plugin['commit'][:12]}")
    try:
      manifest = json.loads((stage / "manifest.json").read_text())
    except (OSError, ValueError) as error:
      raise PluginError(f"no readable manifest.json: {error}") from error
    if manifest.get("id") != plugin["id"]:
      raise PluginError(f"manifest id is {manifest.get('id')!r}, the recipe says {plugin['id']!r}")
    validate = run(["omarchy-plugin-validate", str(stage)])
    if validate.returncode != 0:
      raise PluginError("omarchy-plugin-validate refused it: " + (validate.stderr or validate.stdout).strip()[:300])
    return stage
  except BaseException:
    shutil.rmtree(stage, ignore_errors=True)
    raise


def wait_for_shell(timeout: float) -> bool:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if run(["omarchy-shell", "shell", "ping"]).returncode == 0:
      return True
    time.sleep(0.5)
  return False


def enable(plugin: dict):
  run_shell_call(["omarchy-shell", "shell", "rescanPlugins"])
  for _ in range(40):
    listed = run_shell_call(["omarchy-plugin-list", "--json"])
    try:
      if any(entry.get("id") == plugin["id"] for entry in json.loads(listed.stdout or "[]")):
        break
    except ValueError:
      pass
    time.sleep(0.05)
  command = ["omarchy-plugin-enable", plugin["id"]]
  if plugin.get("section"):
    command += ["--section", plugin["section"]]
  if plugin.get("index") is not None:
    command += ["--index", str(plugin["index"])]
  result = run_shell_call(command)
  if result.returncode != 0:
    raise PluginError("omarchy-plugin-enable failed: " + (result.stderr or result.stdout).strip()[:300])


def defaults_file() -> Path:
  return state_file().with_name("defaults.json")


# ---- settings Omarchy has a command for

# key: (the command that prints the current value, the command that sets it)
SETTINGS = {
    "font": (["omarchy-font-current"], ["omarchy-font-set"]),
    "background": (["omarchy-theme-bg-current"], ["omarchy-theme-bg-set"]),
    "terminal": (["omarchy-default-terminal"], ["omarchy-default-terminal"]),
    "browser": (["omarchy-default-browser"], ["omarchy-default-browser"]),
    "editor": (["omarchy-default-editor"], ["omarchy-default-editor"]),
    "text_size": (["omarchy-display-text-size"], ["omarchy-display-text-size"]),
    "power_profile": (["omarchy-powerprofiles-list", "--active-state"], ["omarchy-powerprofiles-set", "ac"]),
    "idle": (None, None),
}
# The theme sets a background, so a flavor's background comes after it.
SETTING_ORDER = ["font", "background", "terminal", "browser", "editor", "text_size", "power_profile", "idle"]


def setting_value(recipe, key: str):
  """The recipe's value as Omarchy takes it: a background is a path."""
  value = (recipe.defaults.get(key) if key in recipe.defaults else recipe.locked.get(key))
  return str(recipe.root / value) if key == "background" else value


def get_setting(key: str):
  if key == "idle":
    for path in (Path.home() / ".config/omarchy/shell.json",
                 Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "config/omarchy/shell.json"):
      try:
        idle = json.loads(path.read_text()).get("idle")
      except (OSError, ValueError, AttributeError):
        continue
      if isinstance(idle, dict):
        return idle
    return {}
  try:
    out = run(SETTINGS[key][0]).stdout
  except OSError:
    return ""
  if key == "text_size":  # "Text size: 12 (default) px"
    first = out.splitlines()[0] if out else ""
    return "reset" if "default" in first else "".join(c for c in first if c.isdigit())
  if key == "power_profile":
    return next((line.split()[0] for line in out.splitlines() if line.split()[1:] == ["1"]), "")
  return out.strip()


def same_setting(key: str, current, value) -> bool:
  if key == "idle":
    return isinstance(current, dict) and all(current.get(k) == v for k, v in value.items())
  return str(current).strip().lower() == str(value).strip().lower()


def set_setting(key: str, value):
  if key == "idle":
    program = ".idle = ((.idle // {}) + $v)"
    result = run(["bash", "-c", 'source omarchy-shell-config && commit "$1" --argjson v "$2"', "umami",
                  program, json.dumps(value)])
  else:
    result = run_shell_call([*SETTINGS[key][1], str(value)])
  if result.returncode != 0:
    raise PluginError(f"{key} {value}: " + (result.stderr or result.stdout).strip()[:300])


def describe(key: str, value) -> str:
  if key == "idle":
    return "idle " + ", ".join(f"{k} {'off' if v == 0 else f'{v}s'}" for k, v in sorted(value.items()))
  if key == "background":
    return f"background {Path(str(value)).name}"
  return f"{key.replace('_', ' ')} {value}"


# ---- launchers: [defaults.apps]

def applications_dir() -> Path:
  base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
  return Path(base) / "applications"


def system_launcher(name: str):
  dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
  return next((Path(d) / "applications" / f"{name}.desktop" for d in dirs
               if (Path(d) / "applications" / f"{name}.desktop").is_file()), None)


def hidden(text: str) -> str:
  """The same desktop entry with NoDisplay=true: launchers leave it out,
  and it still opens the files it handles."""
  out, main = [], False
  for line in text.splitlines():
    if line.strip().startswith("["):
      main = line.strip() == "[Desktop Entry]"
      out.append(line)
      if main:
        out.append("NoDisplay=true")
      continue
    if not (main and line.split("=", 1)[0].strip() == "NoDisplay"):
      out.append(line)
  return "\n".join(out) + "\n"


def hide_launcher(name: str):
  """Returns the record for a hidden launcher, or None when there is none."""
  mine = applications_dir() / f"{name}.desktop"
  if mine.is_file():
    before = mine.read_text()
  else:
    source = system_launcher(name)
    if not source:
      return None
    before = None
    mine.parent.mkdir(parents=True, exist_ok=True)
  wrote = hidden(before if before is not None else source.read_text())
  mine.write_text(wrote)
  return {"before": before, "wrote": wrote}


def unhide_launcher(name: str, record: dict) -> bool:
  """Put a launcher back, unless it changed since (the user's own edit)."""
  mine = applications_dir() / f"{name}.desktop"
  if not mine.is_file() or mine.read_text() != record["wrote"]:
    return False
  if record.get("before") is None:
    mine.unlink()
  else:
    mine.write_text(record["before"])
  return True


def apply_apps(apps: dict, mine: dict, applied: dict, dry_run: bool) -> list:
  lines = []
  done = mine.setdefault("apps", {"hidden": {}, "added": {}})
  for name in apps.get("hide", []):
    if name in done["hidden"]:
      continue
    if dry_run:
      lines.append(f"  would hide {name}")
      continue
    record = hide_launcher(name)
    if record:
      done["hidden"][name] = record
      lines.append(f"  hid       {name}")
  for kind, installer in (("webapps", "omarchy-webapp-install"), ("tuis", "omarchy-tui-install")):
    for entry in apps.get(kind, []):
      name = entry["name"]
      if name in done["added"]:
        continue
      if (applications_dir() / f"{name}.desktop").exists():
        lines.append(f"  skipped   {name}: you already have a launcher by that name")
        done["added"][name] = None  # yours: never removed
        continue
      if dry_run:
        lines.append(f"  would add {name}")
        continue
      args = [entry["url"], entry["icon"]] if kind == "webapps" else [entry["command"], entry["style"], entry["icon"]]
      result = run([installer, name, *args])
      if result.returncode != 0:
        lines.append(f"  FAILED    adding {name}: " + (result.stderr or result.stdout).strip()[:200])
        continue
      done["added"][name] = kind
      lines.append(f"  added     {name}")
  if not dry_run:
    save_defaults(applied)
  return lines


def restore_apps(done: dict) -> tuple:
  """(lines, what failed or is left to do)."""
  lines, left = [], {"hidden": {}, "added": {}}
  for name, record in done.get("hidden", {}).items():
    if unhide_launcher(name, record):
      lines.append(f"  restored  {name} in the launcher")
    else:
      lines.append(f"  kept      {name}: you changed its launcher since")
  for name, kind in done.get("added", {}).items():
    if not kind or not (applications_dir() / f"{name}.desktop").exists():
      continue
    remover = "omarchy-webapp-remove" if kind == "webapps" else "omarchy-tui-remove"
    result = run([remover, name])
    if result.returncode == 0:
      lines.append(f"  removed   {name}")
    else:
      left["added"][name] = kind
      lines.append(f"  FAILED    removing {name}: " + (result.stderr or result.stdout).strip()[:200])
  return lines, (left if left["added"] else None)


def apply_defaults(recipe, dry_run=False, shell_timeout=60.0, waiting=False) -> list:
  """Apply the recipe's [defaults] once per user, then its [locked] table.

  A default is applied the first time this user logs in on the flavor, and
  again only where the flavor changes it. A theme the user picks afterwards
  is theirs and stays. The bar is tracked entry by entry, so a flavor that
  changes one widget moves only that one, not the rest of the user's layout.
  """
  try:
    applied = json.loads(defaults_file().read_text())
  except (OSError, ValueError):
    applied = {}
  if not isinstance(applied, dict):
    applied = {}
  mine = applied.setdefault(recipe.name, {})
  # Earlier versions stored the whole bar table once it was applied.
  if isinstance(mine.get("bar"), dict):
    mine["bar_done"] = sorted({command_key(c) for c in bar_commands(mine.pop("bar"))})
  lines = []
  # The theme brings its own background: what umami remove puts back is the
  # one from before either changed.
  if not dry_run and "background" in recipe.defaults and "background" not in mine.get("settings", {}):
    mine.setdefault("before", {}).setdefault("background", get_setting("background"))
  theme = recipe.defaults.get("theme")
  if theme and mine.get("theme") != theme:
    if dry_run:
      lines.append(f"  would set theme {theme}")
    else:
      # What `umami remove` puts back, while it is still the flavor's.
      mine.setdefault("before_theme", current_theme())
      mine["applied_at"] = time.time()
      set_theme(theme)
      mine["theme"] = theme
      save_defaults(applied)
      lines.append(f"  set       theme {theme}")

  bar = recipe.defaults.get("bar")
  if bar:
    commands = bar_commands(bar)
    done = set(mine.get("bar_done", []))
    todo = [c for c in commands if command_key(c) not in done]
    if todo and waiting:
      # The layout can place the flavor's plugins; wait until they're decided.
      lines.append("  waiting   bar layout, until the plugins are decided")
    elif todo:
      todo = without_absent(recipe, todo, dry_run)
      if dry_run:
        lines += [f"  would run {' '.join(c)}" for c in todo]
      elif todo:
        mine.setdefault("before_bar", bar_layout())
        mine["applied_at"] = time.time()
        skipped = run_bar(todo, shell_timeout)
        done |= {command_key(c) for c in todo}
        current = {command_key(c) for c in commands}
        mine["bar_done"] = sorted(done & current)
        save_defaults(applied)
        lines.append("  set       bar layout" + ("" if len(todo) == len(commands) else f" ({len(todo)} changed)"))
        lines += skipped

  settings = mine.setdefault("settings", {})
  for key in SETTING_ORDER:
    if key not in recipe.defaults:
      continue
    value = setting_value(recipe, key)
    if settings.get(key) == value:
      continue
    if dry_run:
      lines.append(f"  would set {describe(key, value)}")
      continue
    mine.setdefault("before", {}).setdefault(key, get_setting(key))
    mine["applied_at"] = time.time()
    try:
      set_setting(key, value)
    except PluginError as error:
      lines.append(f"  FAILED    {error}")
      continue
    settings[key] = value
    save_defaults(applied)
    lines.append(f"  set       {describe(key, value)}")
  if recipe.defaults.get("apps"):
    lines += apply_apps(recipe.defaults["apps"], mine, applied, dry_run)
  return lines + apply_locked(recipe, dry_run, shell_timeout, waiting)


def apply_locked(recipe, dry_run=False, shell_timeout=60.0, waiting=False) -> list:
  """[locked]: the flavor's theme and bar, put back at every login."""
  lines = []
  theme = recipe.locked.get("theme")
  if theme:
    try:
      current = run(["omarchy-theme-current"]).stdout.strip()
    except OSError:
      current = ""
    norm = lambda name: name.strip().lower().replace("-", " ")
    if norm(current) != norm(theme):
      if dry_run:
        lines.append(f"  would set theme {theme} (locked)")
      else:
        set_theme(theme)
        lines.append(f"  locked    theme {theme}")
  for key in SETTING_ORDER:
    if key not in recipe.locked:
      continue
    value = setting_value(recipe, key)
    if same_setting(key, get_setting(key), value):
      continue
    if dry_run:
      lines.append(f"  would set {describe(key, value)} (locked)")
      continue
    try:
      set_setting(key, value)
      lines.append(f"  locked    {describe(key, value)}")
    except PluginError as error:
      lines.append(f"  FAILED    {error}")
  bar = recipe.locked.get("bar")
  if bar and waiting:
    lines.append("  waiting   locked bar layout, until the plugins are decided")
  elif bar:
    commands = without_absent(recipe, bar_commands(bar), dry_run)
    if dry_run:
      lines += [f"  would run {' '.join(c)} (locked)" for c in commands]
    elif commands:
      skipped = run_bar(commands, shell_timeout)
      lines.append("  locked    bar layout")
      lines += skipped
  return lines


def command_key(command: list) -> str:
  return json.dumps(command)


def current_theme() -> str:
  try:
    return run(["omarchy-theme-current"]).stdout.strip()
  except OSError:
    return ""


def bar_layout() -> dict:
  """The user's bar layout, or Omarchy's default when they have none."""
  for path in (Path.home() / ".config/omarchy/shell.json",
               Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "config/omarchy/shell.json"):
    try:
      layout = json.loads(path.read_text())["bar"]["layout"]
    except (OSError, ValueError, KeyError, TypeError):
      continue
    if isinstance(layout, dict):
      return layout
  return {}


def placement(layout: dict, wid: str):
  """(section, index, entry) of a widget in a bar layout, or None."""
  for section, entries in layout.items():
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
      if isinstance(entry, dict) and entry.get("id") == wid:
        return section, index, entry
  return None


def restore_commands(done: list, before: dict, now: dict) -> tuple:
  """Omarchy's commands that undo a flavor's bar entries, each only where
  the bar is still as the flavor left it; and the entries left alone."""
  pairs, changed = restore_pairs(done, before, now)
  return [undo for undo, _ in pairs], changed


def restore_pairs(done: list, before: dict, now: dict) -> tuple:
  """([(undo command, the flavor's command it undoes)], changed widgets)."""
  undo, changed = [], []
  for command in done:
    wid = command[2] if command[0] == "omarchy-bar" else command[1]
    was, is_ = placement(before, wid), placement(now, wid)
    if command[0] == "omarchy-plugin-disable":
      if was and not is_:
        undo.append((["omarchy-plugin-enable", wid, "--section", was[0], "--index", str(was[1])], command))
      elif was:
        changed.append(wid)
    elif command[1] == "put":
      if is_ and not was:
        undo.append((["omarchy-plugin-disable", wid], command))
    elif command[1] == "move":
      if was and is_ and is_[0] == command[4]:
        undo.append((["omarchy-bar", "move", wid, "--section", was[0], "--index", str(was[1])], command))
      elif was:
        changed.append(wid)
    elif command[1] == "set":
      key, value = command[3], command[4]
      if was and is_ and str(is_[2].get(key)) == value and key in was[2]:
        undo.append((["omarchy-bar", "set", wid, key, str(was[2][key])], command))
      elif was and key in was[2]:
        changed.append(wid)
  # Taken off first, then put back in the order they stood, so each lands
  # at its old index, then their options.
  def order(pair):
    c = pair[0]
    if c[0] == "omarchy-plugin-disable":
      return (0, 0)
    return (2, 0) if c[1] == "set" else (1, int(c[-1]))
  return sorted(undo, key=order), changed


def restore_defaults(shell_timeout: float = 60.0, dry_run: bool = False) -> list:
  """Put back the theme and bar each flavor changed, latest flavor first,
  where they are still as the flavor left them; then forget the flavors.
  What fails is remembered, so running it again retries just that."""
  try:
    applied = json.loads(defaults_file().read_text())
  except (OSError, ValueError):
    return []
  if not isinstance(applied, dict):
    return []
  norm = lambda name: name.strip().lower().replace("-", " ")
  lines, remaining = [], {}
  for name, mine in sorted(applied.items(), key=lambda item: -(item[1].get("applied_at") or 0)):
    left = {**mine, "bar_done": [], "settings": {}}
    left.pop("theme", None)
    left.pop("apps", None)
    if mine.get("theme") and mine.get("before_theme"):
      if norm(current_theme()) == norm(mine["theme"]):
        if dry_run:
          lines.append(f"  would restore theme {mine['before_theme']}")
        else:
          try:
            set_theme(mine["before_theme"])
            lines.append(f"  restored  theme {mine['before_theme']}")
          except PluginError as error:
            left["theme"] = mine["theme"]
            lines.append(f"  FAILED    {error}")
      else:
        lines.append(f"  kept      theme {current_theme()}: you changed it after {name} set it")
    done = [json.loads(key) for key in mine.get("bar_done", [])]
    if done and "before_bar" not in mine:
      lines.append(f"  kept      bar layout: nothing was saved from before {name}")
    elif done:
      pairs, changed = restore_pairs(done, mine["before_bar"], bar_layout())
      if pairs and dry_run:
        lines += [f"  would run {' '.join(command)}" for command, _ in pairs]
      elif pairs:
        if not wait_for_shell(shell_timeout):
          raise PluginError("bar layout: the shell is not running (run umami user-sync --remove again)")
        for command, source in pairs:
          result = run_shell_call(command)
          if result.returncode != 0:
            left["bar_done"].append(command_key(source))
            lines.append(f"  FAILED    {' '.join(command)}: " + (result.stderr or result.stdout).strip()[:200])
        if len(left["bar_done"]) < len(pairs):
          lines.append("  restored  bar layout")
      lines += [f"  kept      {wid}: you changed it after {name} did" for wid in sorted(set(changed))]
    for key, value in (mine.get("settings") or {}).items():
      before = (mine.get("before") or {}).get(key)
      if before in (None, "", {}):
        continue
      if not same_setting(key, get_setting(key), value):
        lines.append(f"  kept      {key.replace('_', ' ')}: you changed it after {name} set it")
      elif dry_run:
        lines.append(f"  would restore {describe(key, before)}")
      else:
        try:
          set_setting(key, before)
          lines.append(f"  restored  {describe(key, before)}")
        except PluginError as error:
          left["settings"][key] = value
          lines.append(f"  FAILED    {error}")
    if mine.get("apps") and dry_run:
      lines += [f"  would restore {n} in the launcher" for n in mine["apps"].get("hidden", {})]
      lines += [f"  would remove {n}" for n, kind in mine["apps"].get("added", {}).items() if kind]
    elif mine.get("apps"):
      app_lines, apps_left = restore_apps(mine["apps"])
      lines += app_lines
      if apps_left:
        left["apps"] = apps_left
    if left.get("theme") or left["bar_done"] or left["settings"] or left.get("apps"):
      remaining[name] = left
  if dry_run:
    return lines
  if remaining:
    save_defaults(remaining)
    lines.append(f"  run {SELF} user-sync --remove again to retry what failed")
  else:
    defaults_file().unlink(missing_ok=True)
  return lines


def set_theme(theme: str):
  result = run_shell_call(["omarchy-theme-set", theme])
  if result.returncode != 0:
    raise PluginError(f"theme {theme}: " + (result.stderr or result.stdout).strip()[:300])


def without_absent(recipe, commands: list, dry_run: bool) -> list:
  """Leave out the parts about a flavor plugin that isn't installed."""
  absent = set() if dry_run else {p["id"] for p in recipe.plugins if not (plugins_dir() / p["id"]).exists()}
  return [c for c in commands if not set(c) & absent]


def run_bar(commands: list, shell_timeout: float) -> list:
  """Run the bar commands; returns the lines for the widgets skipped."""
  if not wait_for_shell(shell_timeout):
    raise PluginError("bar layout: the shell is not running (run umami user-sync again)")
  skipped = []
  for command in commands:
    result = run_shell_call(command)
    # A widget this Omarchy doesn't have is already off the bar.
    if result.returncode != 0 and command[0] == "omarchy-plugin-disable" and "is not known" in result.stderr:
      continue
    # A widget that isn't on this user's bar (taken off, or replaced by
    # one of their own) has nothing to move or set; the rest still applies.
    if result.returncode != 0 and command[0] == "omarchy-bar" and "could not find widget" in result.stderr:
      skipped.append(f"  skipped   {' '.join(command[1:3])}: not on your bar")
      continue
    if result.returncode != 0:
      raise PluginError(f"bar layout: {' '.join(command)}: " + (result.stderr or result.stdout).strip()[:300])
  return skipped


def bar_commands(bar: dict) -> list:
  """Omarchy's own commands for a [defaults.bar] table, in order."""
  commands = [["omarchy-plugin-disable", wid] for wid in bar.get("remove", [])]
  # put leaves a widget that is already on the bar where it is.
  for verb, kind in (("put", "add"), ("move", "move")):
    for entry in bar.get(kind, []):
      command = ["omarchy-bar", verb, entry["id"], "--section", entry["section"]]
      if "index" in entry:
        command += ["--index", str(entry["index"])]
      commands.append(command)
  for entry in bar.get("set", []):
    commands.append(["omarchy-bar", "set", entry["id"], entry["key"], entry["value"]])
  return commands


def save_defaults(applied: dict):
  path = defaults_file()
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(applied, indent=2, sort_keys=True) + "\n")


def ask(recipe, pending: list, suites: list = ()) -> bool:
  """Show Omarchy's plugin warning and the plugins, and ask once for all."""
  if pending:
    print(f"The {recipe.name} flavor adds these plugins from the Omarchy marketplace:\n")
  for plugin in pending:
    print(f"  {plugin['id']}")
    if plugin.get("note"):
      print(f"      {plugin['note']}")
    print(f"      {plugin['repo']} @ {plugin['commit'][:12]}\n")
  if suites:
    print(f"The {recipe.name} flavor installs {'this plugin suite' if len(suites) == 1 else 'these plugin suites'} "
          "into your shell, with the package's own installer:\n")
  for suite in suites:
    print(f"  {suite['id']}")
    if suite.get("note"):
      print(f"      {suite['note']}")
    print(f"      {suite['command']} {' '.join(suite['install'])}  (from the {suite['package']} package)\n")
  print("⚠️  " + WARNING.replace("\n", "\n  ") + "\n")
  count = len(pending) + len(suites)
  prompt = f"Add {'it' if count == 1 else 'them'}?"
  if shutil.which("gum") and os.isatty(0):
    return run_tty(["gum", "confirm", "--default=false", prompt]) == 0
  try:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
  except EOFError:
    return False


def leftovers(recipe, state: dict) -> list:
  """Plugins a flavor installed that this flavor doesn't use: left over from
  the previous flavor, or dropped by a new release. Ones the user installed
  themselves, or chose to keep, are never listed."""
  wanted = {p["id"] for p in recipe.plugins}
  plugins = [pid for pid, known in state.items()
             if isinstance(known, dict) and known.get("commit") and not known.get("kept")
             and pid not in wanted and (plugins_dir() / pid).exists()]
  wanted_suites = {suite_key(s) for s in recipe.suites}
  suites = [key for key, known in state.items()
            if key.startswith("suite:") and isinstance(known, dict) and known.get("version")
            and not known.get("kept") and key not in wanted_suites]
  return sorted(plugins) + sorted(suites)


def suite_key(suite: dict) -> str:
  return f"suite:{suite['id']}"


def label(key: str, state: dict) -> str:
  known = state.get(key) or {}
  return f"{key[6:]} (the {known.get('package')} suite)" if key.startswith("suite:") else key


def ask_leftovers(recipe, state: dict, ids: list) -> bool:
  flavors = sorted({state[pid].get("flavor") or "an earlier" for pid in ids} - {recipe.name})
  origin = f"the {', '.join(flavors)} flavor" if flavors else "an earlier release of this flavor"
  suites = any(pid.startswith("suite:") for pid in ids)
  what = ("this plugin suite" if suites else "this plugin") if len(ids) == 1 else "these"
  print(f"{origin[0].upper() + origin[1:]} added {what}, which {recipe.name} doesn't use:\n")
  for pid in ids:
    print(f"  {label(pid, state)}")
  print("\nPlugins you installed yourself are not touched. Keep them, and they become yours.\n")
  prompt = f"Remove {'it' if len(ids) == 1 else 'them'}?"
  if shutil.which("gum") and os.isatty(0):
    return run_tty(["gum", "confirm", "--default=false", prompt]) == 0
  try:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
  except EOFError:
    return False


def tidy(recipe, state: dict, ids: list, dry_run: bool, interactive: bool, assume_yes: bool, ask=None) -> list:
  """Offer to remove the leftover plugins, once; the answer is remembered."""
  if not ids:
    return []
  if dry_run or assume_yes or not interactive:
    return [f"  leftover  {label(pid, state)}: from an earlier flavor (user-sync --review asks)" for pid in ids]
  lines = []
  if (ask or ask_leftovers)(recipe, state, ids):
    for pid in ids:
      if pid.startswith("suite:"):
        known = state[pid]
        if not Path(known["command"]).exists():
          lines.append(f"  FAILED    removing {label(pid, state)}: {known['command']} is gone; "
                       f"reinstall the {known.get('package')} package to uninstall it")
          continue
        result = run_shell_call([known["command"], *known["uninstall"]])
      else:
        result = run_shell_call(["omarchy-plugin-remove", pid, "--yes"])
      if result.returncode == 0:
        name = label(pid, state)
        state.pop(pid, None)
        lines.append(f"  removed   {name}")
      else:
        lines.append(f"  FAILED    removing {pid}: " + (result.stderr or result.stdout).strip()[:200])
  else:
    for pid in ids:
      state[pid]["kept"] = True
      lines.append(f"  kept      {label(pid, state)}: yours now")
  save_state(state)
  return lines


def package_version(name: str) -> str:
  """The installed version of a pacman package, or "" when it isn't."""
  out = run(["pacman", "-Q", "--", name])
  parts = out.stdout.split()
  return parts[1] if out.returncode == 0 and len(parts) == 2 else ""


SUITE_PATH = "/usr/bin:/usr/local/bin"


def suite_command(suite: dict) -> str:
  """The suite's command, only if its package owns it: the code that runs
  is the package's, which pacman installed from a trusted repository."""
  path = shutil.which(suite["command"], path=SUITE_PATH)
  if not path:
    raise PluginError(f"{suite['command']} is not installed")
  owner = run(["pacman", "-Qqo", "--", path]).stdout.strip()
  if owner != suite["package"]:
    raise PluginError(f"{path} belongs to {owner or 'no package'}, not {suite['package']}")
  return path


def sync_suite(recipe, suite, state, dry_run, interactive, shell_ready, shell_timeout):
  """Install a suite once the user said yes, and update it when its package
  changes. Returns (line, ok, shell_ready): ok is True after a change, None
  when there was nothing to do, False on failure."""
  sid, key = suite["id"], suite_key(suite)
  known = state.get(key)
  if known and known.get("declined"):
    return f"  skipped   {sid}: you said no", None, shell_ready
  version = package_version(suite["package"])
  if not version:
    return (f"  FAILED    {sid}: the {suite['package']} package is not installed "
            f"(sudo umami flavor {recipe.name} installs it)"), False, shell_ready
  if known and known.get("version") == version:
    return f"  ok        {sid} ({suite['package']} {version})", None, shell_ready
  action = "update" if known else "install"
  if dry_run:
    return f"  would {action:<7} {sid} ({suite['package']} {version})", None, shell_ready
  try:
    command = [suite_command(suite), *suite[action]]
    if shell_ready is None:
      shell_ready = wait_for_shell(shell_timeout)
    if not shell_ready:
      raise PluginError("the shell is not running (run umami user-sync again)")
    env = {**os.environ, "OMARCHY_SHELL_IPC_TIMEOUT": SHELL_CALL_TIMEOUT}
    if interactive:
      # In the review terminal: the installer's own output is shown.
      code, why = subprocess.run(command, env=env).returncode, "see its output above"
    else:
      result = run(command, env=env)
      code, why = result.returncode, (result.stderr or result.stdout).strip()[-300:]
    if code != 0:
      raise PluginError(f"{' '.join(command)} failed: {why}")
  except PluginError as error:
    return f"  FAILED    {sid}: {error}", False, shell_ready
  state[key] = {"suite": True, "package": suite["package"], "version": version, "command": command[0],
                "uninstall": suite["uninstall"], "flavor": recipe.name}
  save_state(state)
  return f"  {action + 'ed' if action == 'install' else 'updated':<9} {sid} ({suite['package']} {version})", True, shell_ready


def run_tty(command) -> int:
  return subprocess.run(command).returncode


def open_review(flags: str = "--review"):
  """At login: open a terminal that asks about the undecided plugins."""
  launcher = "omarchy-launch-floating-terminal-with-presentation"
  if shutil.which(launcher):
    # Detached: the terminal stays up until the user answers, and the
    # rest of this sync (the theme, say) shouldn't wait for that.
    subprocess.Popen([launcher, f"{SELF} user-sync {flags}"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True
  return False


class NoFlavor:
  name, plugins, suites = "", [], []


def ask_removal(recipe, state: dict, ids: list) -> bool:
  flavors = sorted({state[pid].get("flavor") or "an earlier" for pid in ids})
  what = "this" if len(ids) == 1 else "these"
  print(f"The flavor is removed. The {', '.join(flavors)} flavor added {what}:\n")
  for pid in ids:
    print(f"  {label(pid, state)}")
  print("\nPlugins you installed yourself, or chose to keep, are not touched.\n")
  prompt = f"Remove {'it' if len(ids) == 1 else 'them'}?"
  if shutil.which("gum") and os.isatty(0):
    return run_tty(["gum", "confirm", "--default=false", prompt]) == 0
  try:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
  except EOFError:
    return False


def remove(interactive: bool = False, assume_yes: bool = False, dry_run: bool = False,
           shell_timeout: float = 60.0) -> int:
  """After `umami remove`, as each user: put back the theme and bar the
  flavors set, and ask about the plugins they added. Asked right here in a
  terminal (sudo umami remove's own), else in a terminal opened for it."""
  lines = [] if interactive else restore_defaults(shell_timeout, dry_run=dry_run)
  state = load_state()
  ids = leftovers(NoFlavor, state)
  if dry_run:
    lines += tidy(NoFlavor, state, ids, dry_run=True, interactive=False, assume_yes=False)
    print("removing the flavor would" + (":" if lines else " change nothing"))
    for line in lines:
      print(line)
    return 0
  if ids and not interactive and not assume_yes and not sys.stdin.isatty():
    opened = open_review("--remove --review")
    lines.append("  waiting   for your answer about the flavor's plugins"
                 + ("" if opened else f" (run {SELF} user-sync --remove --review)"))
  elif ids:
    ask = (lambda *_: True) if assume_yes else ask_removal
    lines += tidy(NoFlavor, state, ids, dry_run=False, interactive=True, assume_yes=False, ask=ask)
  print("flavor removed" + (":" if lines else ""))
  for line in lines:
    print(line)
  return 0


def refresh() -> int:
  """Run inside the user's session after a live flavor switch: reload
  Hyprland and the shell from the new tree, then do what login does."""
  reload = run(["hyprctl", "reload"])
  shell = run(["omarchy-restart-shell"])
  if reload.returncode != 0 or shell.returncode != 0:
    why = (shell.stderr or reload.stderr or "").strip()[:200]
    if shutil.which("omarchy-notification-send"):
      run(["omarchy-notification-send", "--app-name", "umami", "-u", "normal", "Flavor switched", f"Log out and back in to finish. {why}".strip()])
    return 1
  wait_for_shell(30)
  code = sync()
  if shutil.which("omarchy-notification-send"):
    run(["omarchy-notification-send", "--app-name", "umami", "-u", "normal", f"Flavor {load(TREE_RECIPE).name}", "Switched without a reboot."])
  return code


def sync(recipe_path=TREE_RECIPE, dry_run=False, shell_timeout=60.0, interactive=False, assume_yes=False) -> int:
  recipe = load(recipe_path)
  state = load_state()
  left = leftovers(recipe, state)
  if not recipe.plugins and not recipe.suites and not recipe.defaults and not recipe.locked and not left:
    return 0
  lines, failed, installed = [], 0, []
  shell_ready = None

  # The plugins nobody has said yes or no to yet.
  pending = [p for p in recipe.plugins if not state.get(p["id"])
             and not ((plugins_dir() / p["id"]).exists() or (plugins_dir() / p["id"]).is_symlink())]
  pending_suites = [x for x in recipe.suites if not state.get(suite_key(x))]
  deferred = set()
  if left and not pending and not pending_suites and not dry_run and not assume_yes and not interactive:
    # Nothing to install, but something to tidy: ask in the same terminal.
    opened = open_review()
    lines.append("  waiting   for your answer about plugins from an earlier flavor"
                 + ("" if opened else f" (run {SELF} user-sync --review)"))
    left = []
  if (pending or pending_suites) and not dry_run and not assume_yes:
    if interactive:
      if not ask(recipe, pending, pending_suites):
        for plugin in pending:
          state[plugin["id"]] = {"declined": True, "flavor": recipe.name}
        for suite in pending_suites:
          state[suite_key(suite)] = {"declined": True, "flavor": recipe.name}
        save_state(state)
    else:
      deferred = {p["id"] for p in pending} | {suite_key(x) for x in pending_suites}
      left = []  # the review terminal asks about these too
      opened = open_review()
      lines.append("  waiting   for your OK: " + ", ".join(sorted(d.removeprefix("suite:") for d in deferred))
                   + ("" if opened else f" (run {SELF} user-sync --review)"))

  for suite in recipe.suites:
    key = suite_key(suite)
    if key in deferred:
      continue
    line, ok, shell_ready = sync_suite(recipe, suite, state, dry_run, interactive, shell_ready, shell_timeout)
    lines.append(line)
    if ok is False:
      failed += 1
    elif ok:
      installed.append(suite["id"])

  for plugin in recipe.plugins:
    pid = plugin["id"]
    target = plugins_dir() / pid
    known = state.get(pid)
    present = target.exists() or target.is_symlink()
    if pid in deferred:
      continue
    if known and known.get("declined"):
      lines.append(f"  skipped   {pid}: you said no (omarchy plugin add it yourself if you change your mind)")
      continue
    if known and not present:
      lines.append(f"  skipped   {pid}: you removed it")
      continue
    if not known and present:
      lines.append(f"  yours     {pid}: already installed, left alone")
      continue
    if known and known.get("commit") == plugin["commit"]:
      lines.append(f"  ok        {pid} @ {plugin['commit'][:12]}")
      continue
    action = "update" if known else "install"
    if dry_run:
      lines.append(f"  would {action:<7} {pid} @ {plugin['commit'][:12]}")
      continue
    try:
      stage = fetch(plugin)
      target.parent.mkdir(parents=True, exist_ok=True)
      if action == "update":
        old = target.with_name(f".{pid}.flavor-old")
        shutil.rmtree(old, ignore_errors=True)
        target.rename(old)
        stage.rename(target)
        shutil.rmtree(old, ignore_errors=True)
      else:
        stage.rename(target)
      state[pid] = {"commit": plugin["commit"], "repo": plugin["repo"], "flavor": recipe.name}
      save_state(state)
      if action == "install":
        if shell_ready is None:
          shell_ready = wait_for_shell(shell_timeout)
        if not shell_ready:
          raise PluginError("installed, but the shell is not running to enable it (run user-sync again, or omarchy plugin enable)")
        enable(plugin)
      else:
        run_shell_call(["omarchy-shell", "shell", "rescanPlugins"])
      installed.append(pid)
      lines.append(f"  {action + 'ed' if action == 'install' else 'updated':<9} {pid} @ {plugin['commit'][:12]}")
    except PluginError as error:
      failed += 1
      lines.append(f"  FAILED    {pid}: {error}")

  lines += tidy(recipe, state, left, dry_run, interactive, assume_yes)

  try:
    lines += apply_defaults(recipe, dry_run, shell_timeout, waiting=bool(deferred))
  except PluginError as error:
    failed += 1
    lines.append(f"  FAILED    {error}")

  print(f"flavor {recipe.name}: user layer")
  print("\n".join(lines) if lines else "  nothing to do")
  if (installed or failed) and not dry_run and shutil.which("omarchy-notification-send"):
    body = (f"Installed or updated: {', '.join(installed)}. " if installed else "") + (f"{failed} failed; run umami user-sync to see why." if failed else "")
    run(["omarchy-notification-send", "--app-name", "umami", "-u", "normal", f"Flavor {recipe.name}", body.strip()])
  return 1 if failed else 0
