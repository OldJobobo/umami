"""Compose a flavor: copy an Omarchy tree, apply the recipe, write a report.

Every transform checks its target before acting. A transform whose target is
gone is reported *stale* and skipped; the rest of the flavor still applies. A
transform that applied but whose upstream counterpart changed, or that leaves
something unanswered, is reported *drift*. A required transform that cannot
apply fails the whole composition.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import ipc, lua, tiers
from .recipe import Recipe, RecipeError

REPORT_DIR = ".flavor"
WRAPPED_DIR = "bin/.flavor"
FLAVOR_LUA = "default/hypr/flavor.lua"
# Locked transforms: loaded at the end of toggles.lua, which Omarchy's
# hyprland.lua requires after the user's own files, so they win.
LOCKED_LUA = "default/hypr/flavor-locked.lua"
LOCKED_ENTRY = "default/hypr/toggles.lua"
# over_theme transforms: loaded right after the theme's Hyprland settings and
# before the user's own files, so they win over the theme but not the user.
OVER_THEME_LUA = "default/hypr/flavor-over-theme.lua"
THEME_ANCHOR = 'require_optional.module("omarchy.current.theme.hyprland")'
USER_SYNC = "/usr/lib/umami/bin/umami user-sync"
FLAVOR_PLUGINS = "shell/plugins/flavor"
PRIVILEGED_MARKER = "omarchy_security_require_source_root"
# flavor.lua goes after Omarchy's defaults and before the theme's and the
# user's overrides: before the theme line where there is one (4.5 and edge),
# else before software rendering.
LUA_HOOK_ANCHORS = ('require_optional.module("omarchy.current.theme.hyprland")', 'require("default.hypr.software-rendering")')


class Stale(Exception):
  """The transform's target is gone; skip it."""


@dataclass
class Result:
  id: str
  type: str
  required: bool
  status: str = "applied"  # applied | drift | stale | failed
  detail: str = ""
  warnings: list = field(default_factory=list)
  changed: list = field(default_factory=list)
  locked: bool = False
  over_theme: bool = False
  what: str = ""


def describe(t) -> str:
  """A transform in a few words, for people reading a report."""
  o = t.options
  short = lambda v, n=48: v if len(v) <= n else v[:n - 1] + "…"
  if t.type == "lua.set":
    return f"{o.get('path')} = {lua.literal(o.get('value'))}"
  if t.type == "bind.remove":
    return f"unbind {o.get('keys')}"
  if t.type in ("bind.add", "bind.replace"):
    return f"{o.get('keys')}: {short(str(o.get('description') or ''), 36)}"
  if t.type == "diff":
    sources = o.get("sources") or [o.get("source")]
    return short(str(sources[0])) + (f" (+{len(sources) - 1} fallback)" if len(sources) > 1 else "")
  if t.type == "patch":
    return f"edit {o.get('file')}"
  if t.type in ("file.add", "file.remove"):
    return f"{'add' if t.type == 'file.add' else 'remove'} {o.get('path')}"
  if t.type in ("command.wrap", "command.replace"):
    return f"{t.type.split('.')[1]} {o.get('command')}"
  if t.type == "shell.plugins.remove":
    return "remove " + ", ".join(o.get("ids") or [])
  if t.type == "plugin.hypr":
    return f"{o.get('plugin')}'s {o.get('file')}"
  if t.type == "lua":
    return short(str(o.get("source") or "inline Lua"))
  return short(str(o.get("source") or o.get("plugin") or ""))


def sha256(path: Path) -> str:
  return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_tree(source: Path, dest: Path):
  """Copy with real files, never symlinks out of the tree: Omarchy's
  privileged commands resolve their own path and refuse to run unless it is
  $OMARCHY_PATH/bin/<name>, and the packaged bin/ is all links to /usr/bin.
  Reflinks make the copy nearly free on btrfs."""
  subprocess.run(
      ["cp", "-rL", "--preserve=mode,timestamps", "--reflink=auto", f"{source}/.", str(dest)],
      check=True,
  )


class Composer:
  def __init__(self, recipe: Recipe, source: Path, dest: Path):
    self.recipe = recipe
    self.source = Path(source).resolve()
    self.dest = Path(dest).resolve()
    self.results = []
    self.lua_chunks = []
    self.lua_owners = []
    self.locked_chunks = []
    self.locked_owners = []
    self.over_theme_chunks = []
    self.over_theme_owners = []

  # ------------------------------------------------------------- helpers

  def inside(self, relative: str) -> Path:
    path = (self.dest / relative).resolve()
    if not path.is_relative_to(self.dest) or path == self.dest:
      raise RecipeError(f"path escapes the tree: {relative}")
    return path

  def rel(self, path: Path) -> str:
    return str(path.relative_to(self.dest))

  def command(self, name: str) -> Path:
    if not re.fullmatch(r"omarchy-[A-Za-z0-9._-]+", name):
      raise RecipeError(f"not an omarchy command name: {name!r}")
    path = self.dest / "bin" / name
    if not path.is_file():
      raise Stale(f"command {name} no longer exists upstream")
    return path

  def check_upstream_hash(self, path: Path, expected, result: Result):
    if not expected:
      result.warnings.append(f"no upstream_sha256 recorded for {self.rel(path)}; upstream changes to it won't be detected")
      return
    actual = sha256(self.source / self.rel(path))
    if actual != expected:
      result.status = "drift"
      result.warnings.append(f"{self.rel(path)} changed upstream (sha256 {actual[:12]}, recipe expects {expected[:12]})")

  # ----------------------------------------------------------- transforms

  def t_file_add(self, t, r):
    target = self.inside(t.get("path"))
    source = self.recipe.resolve_source(t.get("source"))
    if not source.is_file():
      raise RecipeError(f"source not found: {t.get('source')}")
    if target.exists():
      self.check_upstream_hash(target, t.get("upstream_sha256"), r)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if "mode" in t.options:
      target.chmod(int(str(t.get("mode")), 8))
    r.changed.append(self.rel(target))

  def t_file_remove(self, t, r):
    target = self.inside(t.get("path"))
    if not target.exists():
      raise Stale(f"{t.get('path')} no longer exists upstream")
    if target.is_dir():
      shutil.rmtree(target)
    else:
      target.unlink()
    r.changed.append(self.rel(target))

  def t_patch(self, t, r):
    target = self.inside(t.get("file"))
    if not target.is_file():
      raise Stale(f"{t.get('file')} no longer exists upstream")
    text = target.read_text()
    anchor = t.get("anchor")
    count = text.count(anchor)
    if count != 1:
      raise Stale(f"anchor found {count} times in {t.get('file')} (needs exactly 1)")
    if "insert_before" in t.options:
      text = text.replace(anchor, t.get("insert_before") + anchor)
    elif "insert_after" in t.options:
      text = text.replace(anchor, anchor + t.get("insert_after"))
    else:
      text = text.replace(anchor, t.get("replace_with"))
    target.write_text(text)
    r.changed.append(self.rel(target))

  def t_diff(self, t, r):
    """Apply a unified diff (`git diff` output, paths a/ and b/) to the
    tree. One that no longer applies cleanly is stale, as a whole: a
    change carried as a diff, such as an open pull request, is all or
    nothing.

    `sources` lists alternatives, tried in order, and the first that
    applies cleanly is used: the same change made for each Omarchy line
    it has to land on (the development branch and a stable release, say).
    """
    names = t.get("sources") or [t.get("source")]
    # The tree is not a repository; keep git from finding one above it.
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(self.dest.resolve().parent)}
    base = ["git", "apply", f"-p{int(t.get('strip', 1))}", "--whitespace=nowarn"]
    reasons = []
    for name in names:
      source = self.recipe.resolve_source(name)
      if not source.is_file():
        raise RecipeError(f"source not found: {name}")
      check = subprocess.run(base + ["--check", str(source)], cwd=self.dest, env=env, capture_output=True, text=True)
      if check.returncode != 0:
        first = next((line for line in check.stderr.splitlines() if line.startswith("error:")), check.stderr.strip())
        reasons.append(f"{name}: {first.removeprefix('error: ')}")
        continue
      stat = subprocess.run(base + ["--numstat", "-z", str(source)], cwd=self.dest, env=env, capture_output=True, text=True, check=True)
      subprocess.run(base + [str(source)], cwd=self.dest, env=env, capture_output=True, text=True, check=True)
      for entry in stat.stdout.split("\0"):
        if entry.strip():
          r.changed.append(entry.split("\t", 2)[-1])
      if len(names) > 1:
        r.warnings.append(f"applied {name}")
      return
    raise Stale("no longer applies: " + "; ".join(reasons))

  def t_lua(self, t, r):
    """Raw Lua for flavor.lua, for what has no structured transform (a
    plugin's own binding snippet, say). It runs in Hyprland's config
    after Omarchy's defaults; an error in it would stop the rest of the
    config, so wrap anything that can fail in pcall."""
    if "source" in t.options:
      source = self.recipe.resolve_source(t.get("source"))
      if not source.is_file():
        raise RecipeError(f"source not found: {t.get('source')}")
      code = source.read_text().strip("\n")
    else:
      code = str(t.get("code")).strip("\n")
    if not code:
      raise RecipeError("lua needs code")
    self.add_lua(t, "lua", code, r)

  def t_plugin_hypr(self, t, r):
    """Load a Hyprland Lua file one of the recipe's plugins ships, from
    the user's install of it (plugins are per user, never in the tree).
    A user who declined the plugin, or hasn't decided yet, has no such
    file, and nothing happens. pcall keeps an error in the plugin's file
    from stopping the rest of the config."""
    path = f"/.config/omarchy/plugins/{t.get('plugin')}/{t.get('file')}"
    code = (
        "do\n"
        f'  local file = (os.getenv("HOME") or "") .. {lua.literal(path)}\n'
        '  local handle = io.open(file, "r")\n'
        "  if handle then\n"
        "    handle:close()\n"
        "    pcall(dofile, file)\n"
        "  end\n"
        "end"
    )
    self.add_lua(t, f"{t.get('plugin')}'s {t.get('file')}, if installed", code, r)

  def t_lua_set(self, t, r):
    try:
      table = lua.nested(t.get("path"), t.get("value"))
      chunk = f"hl.config({lua.literal(table)})"
    except ValueError as error:
      raise RecipeError(str(error)) from error
    leaf = t.get("path").split(".")[-1]
    hypr = self.dest / "default/hypr"
    if not any(leaf in path.read_text(errors="replace") for path in hypr.rglob("*.lua")):
      r.warnings.append(f"{leaf} is not set anywhere in Omarchy's defaults; Hyprland validates it at load")
    self.add_lua(t, f"lua.set {t.get('path')}", chunk, r)

  def _dispatcher(self, t) -> str:
    if "action" in t.options:
      action = t.get("action")
      if "omarchy" in action:
        command = action["omarchy"].split()[0]
        if not (self.dest / "bin" / command).is_file():
          raise Stale(f"{command} is not an Omarchy command here")
      return tiers.action_lua(action)
    if "lua" in t.options:
      return t.get("lua")
    return lua.literal(t.get("run"))

  def t_bind_add(self, t, r):
    keys = t.get("keys")
    bound = lua.literal_bindings(self.dest / "default/hypr")
    if lua.normalize_keys(keys) in bound:
      r.warnings.append(f"{keys} is already bound in {bound[lua.normalize_keys(keys)]}; both bindings will fire (use bind.replace)")
    chunk = f"o.bind({lua.literal(keys)}, {lua.literal(t.get('description'))}, {self._dispatcher(t)})"
    self.add_lua(t, f"bind.add {keys}", chunk, r)

  def t_bind_replace(self, t, r):
    keys = t.get("keys")
    if lua.normalize_keys(keys) not in lua.literal_bindings(self.dest / "default/hypr"):
      raise Stale(f"{keys} is no longer bound in Omarchy's defaults")
    chunk = f"o.rebind({lua.literal(keys)}, {lua.literal(t.get('description'))}, {self._dispatcher(t)})"
    self.add_lua(t, f"bind.replace {keys}", chunk, r)

  def t_bind_remove(self, t, r):
    keys = t.get("keys")
    if lua.normalize_keys(keys) not in lua.literal_bindings(self.dest / "default/hypr"):
      if not t.get("optional", False):
        raise Stale(f"{keys} is no longer bound in Omarchy's defaults")
      # Bound only on some Omarchy versions; hl.unbind of a free key does nothing.
      r.warnings.append(f"{keys} is not bound in this Omarchy; unbinding it anyway")
    self.add_lua(t, f"bind.remove {keys}", f"hl.unbind({lua.literal(keys)})", r)

  def add_lua(self, t, label, chunk, r):
    label = " ".join(label.split())  # a comment line stays one line
    if t.locked:
      self.locked_chunks.append(f"-- [{t.id}] {label} (locked)\n{chunk}\n")
      self.locked_owners.append(r)
    elif t.over_theme:
      self.over_theme_chunks.append(f"-- [{t.id}] {label} (over the theme)\n{chunk}\n")
      self.over_theme_owners.append(r)
    else:
      self.lua_chunks.append(f"-- [{t.id}] {label}\n{chunk}\n")
      self.lua_owners.append(r)

  def t_command_wrap(self, t, r):
    name = t.get("command")
    target = self.command(name)
    original = target.read_text(errors="replace")
    if PRIVILEGED_MARKER in original:
      raise Stale(f"{name} is a privileged entrypoint that verifies its own path; it cannot be wrapped (use command.replace)")
    if not t.get("before") and not t.get("after"):
      raise RecipeError("command.wrap needs before and/or after")
    moved = self.dest / WRAPPED_DIR / name
    moved.parent.mkdir(parents=True, exist_ok=True)
    if moved.exists():
      raise RecipeError(f"{name} is already wrapped by another transform")
    target.rename(moved)
    # Keep the command's metadata so the router, help and menus still see it.
    metadata = "\n".join(line for line in original.splitlines() if line.startswith("# omarchy:"))
    # The original is found next to the wrapper, not through $OMARCHY_PATH,
    # which non-login shells may not have.
    upstream = f'"${{BASH_SOURCE[0]%/*}}/.flavor/{name}"'
    lines = ["#!/bin/bash", "", metadata, "", f"# Wrapped by umami ({self.recipe.name}, transform {t.id}).",
             f"# The upstream command is {WRAPPED_DIR}/{name}.", ""]
    if t.get("before"):
      lines += [t.get("before"), ""]
    if t.get("after"):
      lines += [f'{upstream} "$@"', "status=$?", ""]
      run_after = t.get("after_on_failure", False)
      lines += [t.get("after") if run_after else f"if (( status == 0 )); then\n  {t.get('after')}\nfi", "", "exit $status"]
    else:
      lines += [f'exec {upstream} "$@"']
    target.write_text("\n".join(lines) + "\n")
    target.chmod(0o755)
    r.changed += [self.rel(target), self.rel(moved)]

  def t_command_replace(self, t, r):
    target = self.command(t.get("command"))
    source = self.recipe.resolve_source(t.get("source"))
    if not source.is_file():
      raise RecipeError(f"source not found: {t.get('source')}")
    self.check_upstream_hash(target, t.get("upstream_sha256"), r)
    shutil.copy2(source, target)
    target.chmod(0o755)
    r.changed.append(self.rel(target))

  # ---------------------------------------------------------------- shell

  def plugin_manifests(self) -> dict:
    """Map plugin id -> manifest path for first-party plugins."""
    found = {}
    root = self.dest / "shell/plugins"
    for path in sorted(root.rglob("*.json")):
      if path.name != "manifest.json" and not path.name.endswith(".manifest.json"):
        continue
      try:
        manifest = json.loads(path.read_text())
      except (OSError, json.JSONDecodeError):
        continue
      if isinstance(manifest, dict) and manifest.get("id"):
        found.setdefault(manifest["id"], path)
    return found

  def references(self, plugin_id: str, exclude: Path) -> list:
    """Files outside the plugin that mention its id (callers to check)."""
    hits = []
    for folder in ("bin", "default", "shell"):
      for path in (self.dest / folder).rglob("*"):
        if not path.is_file() or path.is_relative_to(exclude) or path.stat().st_size > 2_000_000:
          continue
        try:
          if plugin_id in path.read_text(errors="strict"):
            hits.append(self.rel(path))
        except (UnicodeDecodeError, OSError):
          continue
    return hits

  def t_shell_plugins_remove(self, t, r):
    manifests = self.plugin_manifests()
    ids = t.get("ids")
    absent = [pid for pid in ids if pid not in manifests]
    if len(absent) == len(ids):
      raise Stale(f"none of these plugins exist upstream: {', '.join(absent)}")
    if absent:
      r.status = "drift"
      r.warnings.append(f"no longer upstream (skipped): {', '.join(absent)}")
    if not self.recipe.code:
      for pid in ids:
        if pid in absent:
          continue
        kinds = set(json.loads(manifests[pid].read_text()).get("kinds") or [])
        if kinds - tiers.REMOVABLE_KINDS:
          raise RecipeError(f"{pid} is a {', '.join(sorted(kinds - tiers.REMOVABLE_KINDS))}; removing it "
                            "runs without part of Omarchy, so it needs code = true in [flavor]")
    for pid in ids:
      if pid in absent:
        continue
      manifest = manifests[pid]
      # Removing only the manifest keeps the plugin undiscovered without
      # breaking anything that imports its QML.
      manifest.unlink()
      r.changed.append(self.rel(manifest))
      # So omarchy plugin enable can say why it is missing.
      removed = self.dest / REPORT_DIR / "removed"
      removed.parent.mkdir(exist_ok=True)
      with removed.open("a") as handle:
        handle.write(pid + "\n")
      callers = self.references(pid, manifest.parent)
      if callers:
        r.warnings.append(f"{pid} is still referenced by: {', '.join(callers[:8])}" + (" ..." if len(callers) > 8 else ""))

  def _read_manifest(self, source: Path) -> dict:
    manifest = source / "manifest.json"
    if not manifest.is_file():
      raise RecipeError(f"{source} has no manifest.json")
    try:
      data = json.loads(manifest.read_text())
    except json.JSONDecodeError as error:
      raise RecipeError(f"{manifest}: {error}") from error
    if not data.get("id"):
      raise RecipeError(f"{manifest} has no id")
    # The id names the plugin's directory in the tree.
    if not (isinstance(data["id"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", data["id"])):
      raise RecipeError(f"{manifest}: id {data['id']!r} is not a plugin id (letters, digits, . _ -)")
    return data

  def t_shell_plugins_add(self, t, r):
    source = self.recipe.resolve_source(t.get("source"))
    data = self._read_manifest(source)
    if data["id"] in self.plugin_manifests():
      raise RecipeError(f"plugin {data['id']} already exists upstream (use shell.plugins.replace)")
    target = self.dest / FLAVOR_PLUGINS / data["id"]
    shutil.copytree(source, target)
    r.changed.append(self.rel(target))
    r.warnings.append(f"{data['id']} runs as a first-party plugin with Omarchy's trusted host objects")

  def t_shell_plugins_replace(self, t, r):
    pid = t.get("plugin")
    manifests = self.plugin_manifests()
    if pid not in manifests:
      raise Stale(f"plugin {pid} no longer exists upstream")
    source = self.recipe.resolve_source(t.get("source"))
    data = self._read_manifest(source)
    if data["id"] != pid:
      raise RecipeError(f"replacement manifest id is {data['id']!r}, expected {pid!r}")
    old_manifest = manifests[pid]
    old_dir = old_manifest.parent
    gaps = ipc.missing(ipc.surface(old_dir), ipc.surface(source))
    # Keep the original's code (other QML may import it); only its manifest
    # goes, and the replacement takes the id.
    old_manifest.unlink()
    target = self.dest / FLAVOR_PLUGINS / pid
    shutil.copytree(source, target)
    r.changed += [self.rel(old_manifest), self.rel(target)]
    if gaps:
      r.status = "drift"
      r.warnings.append(f"replacement does not answer IPC the original did: {', '.join(gaps)}")

  def t_shell(self, t, r):
    source = self.recipe.resolve_source(t.get("source"))
    if not (source / "shell.qml").is_file():
      raise RecipeError(f"{t.get('source')} has no shell.qml")
    gaps = ipc.missing(ipc.surface(self.dest / "shell"), ipc.surface(source))
    shutil.rmtree(self.dest / "shell")
    shutil.copytree(source, self.dest / "shell")
    r.changed.append("shell")
    if gaps:
      r.status = "drift"
      r.warnings.append(f"replacement shell does not answer {len(gaps)} IPC calls: {', '.join(gaps[:12])}" + (" ..." if len(gaps) > 12 else ""))

  # ------------------------------------------------------------------ run

  def write_lua(self):
    self.write_locked_lua()
    self.write_over_theme_lua()
    # The user layer, for whoever logs in: the recipe's plugins, defaults
    # and locks, and tidying plugins an earlier flavor left behind. Always
    # present, so switching to a flavor without plugins still tidies.
    self.lua_chunks.append(
        "-- [user] user layer: the flavor's plugins and defaults for this user\n"
        'hl.on("hyprland.start", function()\n'
        f'  hl.exec_cmd("{USER_SYNC}")\n'
        "end)\n")
    module = self.dest / FLAVOR_LUA
    header = (f'-- Generated by umami for flavor "{self.recipe.name}". Do not edit.\n'
              "-- Runs after Omarchy's defaults and before your ~/.config/hypr overrides.\n\n")
    module.write_text(header + "\n".join(self.lua_chunks))
    entry = self.dest / "default/hypr/omarchy.lua"
    text = entry.read_text()
    anchor = next((a for a in LUA_HOOK_ANCHORS if text.count(a) == 1), None)
    if anchor and "require_optional" in text:
      text = text.replace(anchor, 'require_optional.module("default.hypr.flavor")\n' + anchor)
      where = f"before {anchor}"
    else:
      text += '\n-- umami\nrequire("default.hypr.flavor")\n'
      where = "at the end of omarchy.lua (anchor not found)"
    entry.write_text(text)
    for result in self.lua_owners:
      if result.status in ("applied", "drift"):
        result.changed += [FLAVOR_LUA, "default/hypr/omarchy.lua"]
    if "anchor not found" in where and self.lua_owners:
      self.lua_owners[0].warnings.append(f"flavor.lua is loaded {where}")

  def write_over_theme_lua(self):
    if not self.over_theme_chunks:
      return
    header = (f'-- Generated by umami for flavor "{self.recipe.name}". Do not edit.\n'
              "-- Over the theme: runs after the theme's settings and before your\n"
              "-- ~/.config/hypr files, so these win over the theme and you win over them.\n\n")
    (self.dest / OVER_THEME_LUA).write_text(header + "\n".join(self.over_theme_chunks))
    entry = self.dest / "default/hypr/omarchy.lua"
    text = entry.read_text() if entry.is_file() else ""
    changed = [OVER_THEME_LUA]
    if text.count(THEME_ANCHOR) == 1:
      entry.write_text(text.replace(THEME_ANCHOR, THEME_ANCHOR + '\nrequire_optional.module("default.hypr.flavor-over-theme")'))
      changed.append("default/hypr/omarchy.lua")
      warning = None
    else:
      # No theme line to follow: load with the flavor's other settings,
      # where a theme still wins. Reported, so it is not silently soft.
      self.lua_chunks.append('-- [over_theme] theme line not found\nrequire("default.hypr.flavor-over-theme")\n')
      warning = "the theme line was not found in omarchy.lua: these load before the theme and a theme still wins"
    for result in self.over_theme_owners:
      if result.status in ("applied", "drift"):
        result.changed += changed
        if warning:
          result.status = "drift"
          result.warnings.append(warning)

  def write_locked_lua(self):
    if not self.locked_chunks:
      return
    header = (f'-- Generated by umami for flavor "{self.recipe.name}". Do not edit.\n'
              "-- Locked by the flavor: runs after your ~/.config/hypr files, so these win.\n\n")
    (self.dest / LOCKED_LUA).write_text(header + "\n".join(self.locked_chunks))
    entry = self.dest / LOCKED_ENTRY
    changed = [LOCKED_LUA]
    if entry.is_file():
      entry.write_text(entry.read_text().rstrip("\n") + '\n\n-- umami: the flavor\'s locked settings\nrequire("default.hypr.flavor-locked")\n')
      changed.append(LOCKED_ENTRY)
      warning = None
    else:
      # No toggles.lua: load with the flavor's defaults, where the user's
      # config still wins. Reported, so the lock is not silently soft.
      self.lua_chunks.append('-- [locked] toggles.lua not found\nrequire("default.hypr.flavor-locked")\n')
      warning = f"{LOCKED_ENTRY} not found: locked settings load before your config and do not hold"
    for result in self.locked_owners:
      if result.status in ("applied", "drift"):
        result.changed += changed
        if warning:
          result.status = "drift"
          result.warnings.append(warning)

  # Stock Omarchy takes any OMARCHY_PATH but /usr/share/omarchy for a dev
  # checkout: switching channel would unlink the flavor, and the version
  # would read "dev". These teach the composed copy that the flavor's tree
  # is a package install too. They are the compositor's own (not part of a
  # recipe's tier), and one that stops applying is reported, never fatal.
  LIVE_TREE = "/var/lib/umami/tree"
  BUILTIN_PATCHES = [
      ("bin/omarchy-channel-set", "switching channel keeps the flavor",
       'if [[ -z $dev_checkout && $OMARCHY_PATH != "/usr/share/omarchy" ]]; then',
       'if [[ -z $dev_checkout && $OMARCHY_PATH != "/usr/share/omarchy" && $OMARCHY_PATH != "{tree}" ]]; then'),
      # Every non-dev switch unlinks, which would write stock into
      # /etc/omarchy.conf and drop the flavor at the next reboot.
      ("bin/omarchy-channel-set", "switching channel doesn't point Omarchy back at stock",
       '  omarchy-dev-unlink --no-reboot\n',
       '  [[ $OMARCHY_PATH == "{tree}" ]] || omarchy-dev-unlink --no-reboot\n'),
      # The hook has just recomposed the tree onto the new channel's
      # Omarchy, so the update that follows runs from it.
      ("bin/omarchy-channel-set", "the update after a switch runs from the flavor",
       '  export OMARCHY_PATH=/usr/share/omarchy\n',
       '  [[ $OMARCHY_PATH == "{tree}" ]] || export OMARCHY_PATH=/usr/share/omarchy\n'),
      ("bin/omarchy-channel-current", "the channel reads stable or edge, not dev",
       'if [[ $OMARCHY_PATH != "/usr/share/omarchy" ]]; then',
       'if [[ $OMARCHY_PATH != "/usr/share/omarchy" && $OMARCHY_PATH != "{tree}" ]]; then'),
      ("bin/omarchy-version", "the version reads the package's, not dev",
       'if [[ $omarchy_path != "/usr/share/omarchy" ]]; then',
       'if [[ $omarchy_path != "/usr/share/omarchy" && $omarchy_path != "{tree}" ]]; then'),
      ("bin/omarchy-version-branch", "no dev branch is reported",
       '[[ $omarchy_path != "/usr/share/omarchy" ]] || exit 1',
       '[[ $omarchy_path != "/usr/share/omarchy" && $omarchy_path != "{tree}" ]] || exit 1'),
      ("bin/omarchy-update-dev", "omarchy update doesn't treat the tree as a checkout",
       '[[ $OMARCHY_PATH != "/usr/share/omarchy" ]] || exit 0',
       '[[ $OMARCHY_PATH != "/usr/share/omarchy" && $OMARCHY_PATH != "{tree}" ]] || exit 0'),
      ("bin/omarchy-plugin-enable", "a plugin the flavor removed says so",
       "  fail \"plugin '$id' is not known; run: omarchy-shell shell rescanPlugins\"",
       "  grep -qxF -- \"$id\" \"{tree}/.flavor/removed\" 2>/dev/null &&\n"
       "    fail \"plugin '$id' is not available: this machine's flavor removes it (see: umami status)\"\n"
       "  fail \"plugin '$id' is not known; run: omarchy-shell shell rescanPlugins\""),
  ]

  def builtin_patches(self) -> list:
    """An Omarchy that reads OMARCHY_PATH_TYPE (omacom/omarchy#19538) already
    treats the tree as a package install, so its scripts need no patch."""
    results = []
    for rel, why, old, new in self.BUILTIN_PATCHES:
      path = self.dest / rel
      text = path.read_text(errors="replace") if path.is_file() else ""
      if "OMARCHY_PATH_TYPE" in text:
        results.append({"file": rel, "why": why, "status": "upstream"})
      elif text.count(old) == 1:
        path.write_text(text.replace(old, new.format(tree=self.LIVE_TREE)))
        results.append({"file": rel, "why": why, "status": "applied"})
      else:
        results.append({"file": rel, "why": why, "status": "stale"})
    return results

  def run(self) -> dict:
    if self.dest.exists() and any(self.dest.iterdir()):
      raise RecipeError(f"destination is not empty: {self.dest}")
    self.dest.mkdir(parents=True, exist_ok=True)
    copy_tree(self.source, self.dest)

    for t in self.recipe.transforms:
      r = Result(t.id, t.type, t.required, locked=t.locked, over_theme=t.over_theme, what=describe(t))
      handler = getattr(self, "t_" + t.type.replace(".", "_"))
      try:
        handler(t, r)
      except Stale as error:
        r.status, r.detail = "stale", str(error)
      except (RecipeError, OSError, ValueError) as error:
        r.status, r.detail = "failed", str(error)
      self.results.append(r)
    self.write_lua()
    builtin = self.builtin_patches()

    blocking = [r for r in self.results if r.required and r.status in ("stale", "failed")]
    if blocking:
      status = "failed"
    elif any(r.status != "applied" for r in self.results):
      status = "degraded"
    else:
      status = "ok"

    version = self.source / "version"
    report = {
        "flavor": self.recipe.name,
        "code": [f"{tid}: {reason}" for tid, reason in self.recipe.code_transforms],
        "recipe": str(self.recipe.path),
        "source": str(self.source),
        "source_version": version.read_text().strip() if version.is_file() else "",
        "composed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "blocking": [r.id for r in blocking],
        "transforms": [r.__dict__ for r in self.results],
        "locked": self.recipe.locked,
        "defaults": self.recipe.defaults,
        "plugins": [p["id"] for p in self.recipe.plugins],
        "packages": [p["name"] for p in self.recipe.packages],
        "drops": self.recipe.drops,
        "system": self.recipe.system,
        "suites": [{"id": x["id"], "command": x["command"], "package": x["package"]} for x in self.recipe.suites],
        "builtin": builtin,
    }
    report_dir = self.dest / REPORT_DIR
    report_dir.mkdir(exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    shutil.copy2(self.recipe.path, report_dir / "recipe.toml")
    return report
