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
    theme = recipe.defaults.get("theme")
    if theme and mine.get("theme") != theme:
        if dry_run:
            lines.append(f"  would set theme {theme}")
        else:
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
                run_bar(todo, shell_timeout)
                done |= {command_key(c) for c in todo}
                current = {command_key(c) for c in commands}
                mine["bar_done"] = sorted(done & current)
                save_defaults(applied)
                lines.append("  set       bar layout" + ("" if len(todo) == len(commands) else f" ({len(todo)} changed)"))
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
    bar = recipe.locked.get("bar")
    if bar and waiting:
        lines.append("  waiting   locked bar layout, until the plugins are decided")
    elif bar:
        commands = without_absent(recipe, bar_commands(bar), dry_run)
        if dry_run:
            lines += [f"  would run {' '.join(c)} (locked)" for c in commands]
        elif commands:
            run_bar(commands, shell_timeout)
            lines.append("  locked    bar layout")
    return lines


def command_key(command: list) -> str:
    return json.dumps(command)


def set_theme(theme: str):
    result = run(["omarchy-theme-set", theme])
    if result.returncode != 0:
        raise PluginError(f"theme {theme}: " + (result.stderr or result.stdout).strip()[:300])


def without_absent(recipe, commands: list, dry_run: bool) -> list:
    """Leave out the parts about a flavor plugin that isn't installed."""
    absent = set() if dry_run else {p["id"] for p in recipe.plugins if not (plugins_dir() / p["id"]).exists()}
    return [c for c in commands if not set(c) & absent]


def run_bar(commands: list, shell_timeout: float):
    if not wait_for_shell(shell_timeout):
        raise PluginError("bar layout: the shell is not running (run umami user-sync again)")
    for command in commands:
        result = run(command)
        # A widget this Omarchy doesn't have is already off the bar.
        if result.returncode != 0 and command[0] == "omarchy-plugin-disable" and "is not known" in result.stderr:
            continue
        if result.returncode != 0:
            raise PluginError(f"bar layout: {' '.join(command)}: " + (result.stderr or result.stdout).strip()[:300])


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


def ask(recipe, pending: list) -> bool:
    """Show Omarchy's plugin warning and the plugins, and ask once for all."""
    print(f"The {recipe.name} flavor adds these plugins from the Omarchy marketplace:\n")
    for plugin in pending:
        print(f"  {plugin['id']}")
        if plugin.get("note"):
            print(f"      {plugin['note']}")
        print(f"      {plugin['repo']} @ {plugin['commit'][:12]}\n")
    print("⚠️  " + WARNING.replace("\n", "\n  ") + "\n")
    prompt = f"Add {'this plugin' if len(pending) == 1 else 'these plugins'}?"
    if shutil.which("gum") and os.isatty(0):
        return run_tty(["gum", "confirm", "--default=false", prompt]) == 0
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run_tty(command) -> int:
    return subprocess.run(command).returncode


def open_review():
    """At login: open a terminal that asks about the undecided plugins."""
    launcher = "omarchy-launch-floating-terminal-with-presentation"
    if shutil.which(launcher):
        # Detached: the terminal stays up until the user answers, and the
        # rest of this sync (the theme, say) shouldn't wait for that.
        subprocess.Popen([launcher, f"{SELF} user-sync --review"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    return False


def sync(recipe_path=TREE_RECIPE, dry_run=False, shell_timeout=60.0, interactive=False, assume_yes=False) -> int:
    recipe = load(recipe_path)
    if not recipe.plugins and not recipe.defaults and not recipe.locked:
        return 0
    state = load_state()
    lines, failed, installed = [], 0, []
    shell_ready = None

    # The plugins nobody has said yes or no to yet.
    pending = [p for p in recipe.plugins if not state.get(p["id"])
               and not ((plugins_dir() / p["id"]).exists() or (plugins_dir() / p["id"]).is_symlink())]
    deferred = set()
    if pending and not dry_run and not assume_yes:
        if interactive:
            if not ask(recipe, pending):
                for plugin in pending:
                    state[plugin["id"]] = {"declined": True, "flavor": recipe.name}
                save_state(state)
        else:
            deferred = {p["id"] for p in pending}
            opened = open_review()
            lines.append("  waiting   for your OK: " + ", ".join(sorted(deferred))
                         + ("" if opened else f" (run {SELF} user-sync --review)"))

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
                run(["omarchy-shell", "shell", "rescanPlugins"])
            installed.append(pid)
            lines.append(f"  {action + 'ed' if action == 'install' else 'updated':<9} {pid} @ {plugin['commit'][:12]}")
        except PluginError as error:
            failed += 1
            lines.append(f"  FAILED    {pid}: {error}")

    try:
        lines += apply_defaults(recipe, dry_run, shell_timeout, waiting=bool(deferred))
    except PluginError as error:
        failed += 1
        lines.append(f"  FAILED    {error}")

    print(f"flavor {recipe.name}: user layer")
    print("\n".join(lines))
    if (installed or failed) and not dry_run and shutil.which("notify-send"):
        body = (f"Installed or updated: {', '.join(installed)}. " if installed else "") + (f"{failed} failed; run umami user-sync to see why." if failed else "")
        run(["notify-send", "-a", "umami", f"Flavor {recipe.name}", body.strip()])
    return 1 if failed else 0
