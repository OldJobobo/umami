"""Declarative and code transforms.

A flavor is declarative unless it says otherwise. Declarative transforms are
data the compositor checks and turns into configuration itself, the way
Omarchy treats a theme: appearance and layout settings, bindings to a short
list of actions, removing bar widgets and panels, and the [defaults], [locked] and
[[plugin]] tables (plugins go through Omarchy's own validation and the user's
consent). Nothing in them is run as written. [[package]] and [[suite]] are
declarative too: a package comes only from a pacman repository the machine
already trusts, and a suite runs that package's own installer, as the user,
after the user's yes.

plugin.hypr is declarative too: it loads a Hyprland Lua file shipped by one
of the recipe's own plugins, from that user's install, only if the user said
yes to the plugin. The code is the plugin's, which the user accepted with
Omarchy's warning, and it runs as that user inside Hyprland, never as root.
The flavor adds no code of its own.

Everything else runs code someone wrote: diffs, patches, replaced commands,
added files, raw Lua, a binding to an arbitrary shell command, and removing a
background service such as the lock screen. A recipe has to say
`code = true` in [flavor] to use them, and such a flavor is trusted like a
package, because the composed Omarchy also runs through sudo.
"""

import re

from .lua import literal

# Hyprland config sections a declarative lua.set may touch: how windows look
# and are laid out. Not input, monitors, binds, rules, environment, or exec.
LUA_SET_SECTIONS = {"general", "decoration", "animations", "dwindle", "master", "scrolling", "group", "misc"}
LUA_SET_PATH = re.compile(r"^[a-z_]+(\.[a-z0-9_]+)+$")

DIRECTIONS = {"l", "r", "u", "d"}
LAYOUT_MESSAGE = re.compile(r"^[a-z]+( [a-z0-9-]+)?$")
OMARCHY_COMMAND = re.compile(r"^omarchy-[a-z0-9-]+( [A-Za-z0-9._:=-]+)*$")
SHELL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLUGIN_FILE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*\.lua$")


def plugin_file_ok(path) -> bool:
  """A plain relative .lua path: no leading slash, no '..' or hidden parts."""
  return isinstance(path, str) and bool(PLUGIN_FILE.match(path)) and ".." not in path

# What a declarative removal may take away: things on screen, not services.
REMOVABLE_KINDS = {"bar-widget", "panel", "overlay", "menu"}

ACTION_HELP = ("action needs exactly one of: focus or swap (l, r, u, d), layout (a layout message), "
               "omarchy (an Omarchy command and plain arguments), menu, panel")


def check_action(action):
  """Raise ValueError unless `action` is one declarative binding action."""
  if not isinstance(action, dict) or len(action) != 1:
    raise ValueError(ACTION_HELP)
  (kind, value), = action.items()
  ok = isinstance(value, str) and {
      "focus": lambda v: v in DIRECTIONS,
      "swap": lambda v: v in DIRECTIONS,
      "layout": lambda v: bool(LAYOUT_MESSAGE.match(v)),
      "omarchy": lambda v: bool(OMARCHY_COMMAND.match(v)),
      "menu": lambda v: bool(SHELL_ID.match(v)),
      "panel": lambda v: bool(SHELL_ID.match(v)),
  }.get(kind, lambda v: False)(value)
  if not ok:
    raise ValueError(f"{ACTION_HELP}; got {kind} = {value!r}")


def action_lua(action) -> str:
  """The Lua for a checked declarative action, as o.bind takes it."""
  (kind, value), = action.items()
  if kind == "focus":
    return f"hl.dsp.focus({{ direction = {literal(value)} }})"
  if kind == "swap":
    return f"hl.dsp.window.swap({{ direction = {literal(value)} }})"
  if kind == "layout":
    return f"hl.dsp.layout({literal(value)})"
  if kind == "omarchy":
    return literal(value)
  return f"{{ {kind} = {literal(value)} }}"


def lua_set_is_declarative(path, value) -> bool:
  if not (isinstance(path, str) and LUA_SET_PATH.match(path) and path.split(".")[0] in LUA_SET_SECTIONS):
    return False
  scalar = lambda v: isinstance(v, (bool, int, float)) or (isinstance(v, str) and "\n" not in v)
  return scalar(value) or (isinstance(value, list) and all(scalar(v) for v in value))


def code_reason(ttype: str, options: dict):
  """Why a transform counts as code, or None if it is declarative.

  shell.plugins.remove is declarative here; the compositor refuses it in a
  declarative flavor when a plugin it removes is a service.
  """
  if ttype == "lua.set":
    if lua_set_is_declarative(options.get("path"), options.get("value")):
      return None
    return f"sets {options.get('path')!r}, outside {', '.join(sorted(LUA_SET_SECTIONS))} or not a plain value"
  if ttype in ("bind.add", "bind.replace"):
    if "action" in options:
      return None
    return "binds a key to raw Lua" if "lua" in options else "binds a key to a shell command"
  if ttype in ("bind.remove", "shell.plugins.remove", "plugin.hypr"):
    return None
  return {
      "lua": "adds raw Lua",
      "diff": "applies a diff to Omarchy's files",
      "patch": "patches an Omarchy file",
      "file.add": "adds a file to Omarchy",
      "file.remove": "removes a file from Omarchy",
      "command.wrap": "wraps an Omarchy command",
      "command.replace": "replaces an Omarchy command",
      "shell.plugins.add": "adds a first-party shell plugin",
      "shell.plugins.replace": "replaces a first-party shell plugin",
      "shell": "replaces the shell",
  }.get(ttype, f"{ttype} runs code")
