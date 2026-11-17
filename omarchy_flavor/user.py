"""User layer: marketplace plugins a flavor lists, installed for each user.

Third-party plugins never go into the composed tree. A plugin there is
first-party, and the shell grants a first-party plugin every capability its
manifest declares. As a user plugin in ~/.config/omarchy/plugins it gets none
(a `clonedFrom` replacement inherits only what the stock part it replaces
has, which Omarchy decides). So each user gets them the way `omarchy plugin
add` installs them, through Omarchy's own validation and enable commands,
pinned to the commit the recipe names (for marketplace plugins, the commit
the marketplace validated).

The state file records what this layer installed. A plugin the user removed
stays removed, one the user installed themselves is left alone, and only a
plugin installed here follows a new pin.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .recipe import load

TREE_RECIPE = Path("/var/lib/omarchy-flavor/tree/.flavor/recipe.toml")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def plugins_dir() -> Path:
    return Path.home() / ".config/omarchy/plugins"


def state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "omarchy-flavor/plugins.json"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "omarchy-flavor"


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


def fetch(plugin: dict) -> Path:
    """Clone `repo` at exactly `commit` into a staging folder, validated."""
    cache_dir().mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{plugin['id']}.", dir=cache_dir()))
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
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
    run(["omarchy-shell", "shell", "rescanPlugins"])
    for _ in range(40):
        listed = run(["omarchy-plugin-list", "--json"])
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
    result = run(command)
    if result.returncode != 0:
        raise PluginError("omarchy-plugin-enable failed: " + (result.stderr or result.stdout).strip()[:300])


def defaults_file() -> Path:
    return state_file().with_name("defaults.json")


def apply_defaults(recipe, dry_run=False, shell_timeout=60.0) -> list:
    """Apply the recipe's [defaults] once per user, flavor and value.

    A default is applied the first time this user logs in on the flavor, and
    again only if the flavor changes it. A theme the user picks afterwards is
    theirs and stays.
    """
    try:
        applied = json.loads(defaults_file().read_text())
    except (OSError, ValueError):
        applied = {}
    if not isinstance(applied, dict):
        applied = {}
    mine = applied.setdefault(recipe.name, {})
    lines = []
    theme = recipe.defaults.get("theme")
    if theme and mine.get("theme") != theme:
        if dry_run:
            lines.append(f"  would set theme {theme}")
        else:
            result = run(["omarchy-theme-set", theme])
            if result.returncode != 0:
                raise PluginError(f"theme {theme}: " + (result.stderr or result.stdout).strip()[:300])
            mine["theme"] = theme
            save_defaults(applied)
            lines.append(f"  set       theme {theme}")

    bar = recipe.defaults.get("bar")
    if bar and mine.get("bar") != bar:
        commands = bar_commands(bar)
        if dry_run:
            lines += [f"  would run {' '.join(c)}" for c in commands]
        else:
            if not wait_for_shell(shell_timeout):
                raise PluginError("bar layout: the shell is not running (run omarchy-flavor user-sync again)")
            for command in commands:
                result = run(command)
                if result.returncode != 0:
                    raise PluginError(f"bar layout: {' '.join(command)}: " + (result.stderr or result.stdout).strip()[:300])
            mine["bar"] = bar
            save_defaults(applied)
            lines.append("  set       bar layout")
    return lines


def bar_commands(bar: dict) -> list:
    """Omarchy's own commands for a [defaults.bar] table, in order."""
    commands = [["omarchy-plugin-disable", wid] for wid in bar.get("remove", [])]
    for entry in bar.get("move", []):
        command = ["omarchy-bar", "move", entry["id"], "--section", entry["section"]]
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


def sync(recipe_path=TREE_RECIPE, dry_run=False, shell_timeout=60.0) -> int:
    recipe = load(recipe_path)
    if not recipe.plugins and not recipe.defaults:
        return 0
    state = load_state()
    lines, failed, installed = [], 0, []
    shell_ready = None

    for plugin in recipe.plugins:
        pid = plugin["id"]
        target = plugins_dir() / pid
        known = state.get(pid)
        present = target.exists() or target.is_symlink()
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
                run(["omarchy-shell", "shell", "rescanPlugins"])
            installed.append(pid)
            lines.append(f"  {action + 'ed' if action == 'install' else 'updated':<9} {pid} @ {plugin['commit'][:12]}")
        except PluginError as error:
            failed += 1
            lines.append(f"  FAILED    {pid}: {error}")

    try:
        lines += apply_defaults(recipe, dry_run, shell_timeout)
    except PluginError as error:
        failed += 1
        lines.append(f"  FAILED    {error}")

    print(f"flavor {recipe.name}: user layer")
    print("\n".join(lines))
    if (installed or failed) and not dry_run and shutil.which("notify-send"):
        body = (f"Installed or updated: {', '.join(installed)}. " if installed else "") + (f"{failed} failed; run omarchy-flavor user-sync to see why." if failed else "")
        run(["notify-send", "-a", "omarchy-flavor", f"Flavor {recipe.name}", body.strip()])
    return 1 if failed else 0
