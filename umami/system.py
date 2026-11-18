"""Live-system side of a flavor: recompose the tree and point Omarchy at it.

Everything here runs as root (from the pacman hook or through sudo) and reads
only root-owned inputs: the recipe in /etc/umami and the stock
package in /usr/share/omarchy.
"""

import json
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ui
from .compose import Composer
from .recipe import UmamiError, load

STOCK = Path("/usr/share/omarchy")
RECIPE = Path("/etc/umami/recipe.toml")
STATE = Path("/var/lib/umami")
TREE = STATE / "tree"
REPORT = STATE / "report.json"
OMARCHY_CONF = Path("/etc/omarchy.conf")
SUDOERS = Path("/etc/sudoers.d/omarchy-dev-path")
SYSTEM_SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/bin"
CONF_MARKER = "# Written by umami"
SELF = "/usr/lib/umami/bin/umami"


def require_root():
    if os.geteuid() != 0:
        raise UmamiError("this needs root. Run it with sudo.")


def git_mark(tree: Path):
    """omarchy-update-dev runs `git pull` in any non-stock OMARCHY_PATH and
    aborts the update if it isn't a git checkout. A repo with no upstream
    branch makes it print "skip" instead. (Replaced by a flavor-mode marker
    once Omarchy supports one.)"""
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / ".git" / "info" / "exclude").write_text("*\n")


def git_safe_directory():
    """The tree is root-owned, and git refuses a repo owned by someone else
    ("dubious ownership"), which would also abort the update."""
    existing = subprocess.run(["git", "config", "--system", "--get-all", "safe.directory"],
                              capture_output=True, text=True).stdout.split()
    if str(TREE) not in existing:
        subprocess.run(["git", "config", "--system", "--add", "safe.directory", str(TREE)], check=True)


def point_at(path: Path):
    """Write /etc/omarchy.conf and the sudo secure_path drop-in, in the same
    form `omarchy dev link` writes them. Takes effect for the session after a
    reboot; sudo's drop-in applies at once."""
    OMARCHY_CONF.write_text(f'{CONF_MARKER}\nexport OMARCHY_PATH="{path}"\n')
    if path == STOCK:
        SUDOERS.unlink(missing_ok=True)
        return
    with tempfile.NamedTemporaryFile("w", dir="/etc/sudoers.d", prefix=".umami-", delete=False) as staged:
        staged.write(f'Defaults secure_path="{path}/bin:{SYSTEM_SECURE_PATH}"\n')
    try:
        subprocess.run(["visudo", "-cqf", staged.name], check=True)
        os.chmod(staged.name, 0o440)
        os.replace(staged.name, SUDOERS)
    finally:
        Path(staged.name).unlink(missing_ok=True)


def summary(report: dict) -> str:
    """For pacman's output and logs, where umami's lines sit among others."""
    return ui.one_line(report)


def locked_items(report: dict) -> list:
    """What the flavor locks, so a missing feature reads as its decision."""
    items = [t["id"] for t in report["transforms"] if t.get("locked")]
    table = report.get("locked") or {}
    if table.get("theme"):
        items.append(f"theme {table['theme']}")
    if table.get("bar"):
        items.append("bar layout")
    return items


def package_version() -> str:
    """The installed Omarchy package, e.g. "omarchy 4.5.0-3". The tree's own
    version file is not bumped per release."""
    for name in ("omarchy-dev", "omarchy"):
        out = subprocess.run(["pacman", "-Q", name], capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    return ""


def update_notice(report: dict) -> tuple:
    """(title, body, urgency) for the desktop notice after an update
    recomposes the flavor. The update's own window scrolls umami's line away."""
    name, version = report["flavor"], report.get("package") or "the new Omarchy"
    count = len(report["transforms"])
    off = [t for t in report["transforms"] if t["status"] != "applied"]
    if report["status"] == "failed":
        blocking = [t["id"] for t in off if t.get("required", True) and t["status"] in ("stale", "failed")]
        return (f"Flavor {name} could not be reapplied",
                f"{', '.join(blocking) or 'A required change'} no longer applies to {version}. "
                "Omarchy runs stock after the next reboot. See: umami status", "critical")
    if off:
        return (f"Flavor {name} reapplied, with changes",
                f"On {version}: {count - len(off)} of {count} changes applied; "
                + "; ".join(f"{t['id']} {t['status']}" for t in off) + ". See: umami status", "normal")
    return (f"Flavor {name} reapplied", f"All {count} changes applied to {version}.", "normal")


def notify_sessions(title: str, body: str, urgency: str = "normal"):
    """A desktop notice in every logged-in user's session. Best effort: a
    machine with nobody logged in, or no notification daemon, gets none."""
    run_dir = Path("/run/user")
    for entry in sorted(run_dir.iterdir()) if run_dir.is_dir() else []:
        if not entry.name.isdigit() or int(entry.name) < 1000 or not (entry / "bus").exists():
            continue
        uid = int(entry.name)
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            continue
        env = {**user_env(uid), "DBUS_SESSION_BUS_ADDRESS": f"unix:path={entry}/bus"}
        try:
            subprocess.run(["runuser", "-u", name, "--", "env", *[f"{k}={v}" for k, v in env.items()],
                            "omarchy-notification-send", "--app-name", "umami", "-u", urgency, title, body],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


def recompose(hook: bool = False) -> int:
    """hook=True (from the pacman hook): report a failed composition loudly
    but exit 0, since pacman prints any nonzero hook as an error, and the
    package transaction itself did succeed."""
    require_root()
    if not RECIPE.is_file():
        if hook:
            return 0  # the package is installed, but no flavor has been picked
        raise UmamiError(f"no flavor picked yet ({RECIPE} is missing). Pick one with: sudo umami flavor zen")
    STATE.mkdir(parents=True, exist_ok=True)
    new = STATE / "tree.new"
    old = STATE / "tree.old"
    for leftover in (new, old):
        if leftover.exists():
            shutil.rmtree(leftover)

    report = Composer(load(RECIPE), STOCK, new).run()
    report["package"] = package_version()
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(summary(report))
    if hook:
        notify_sessions(*update_notice(report))

    if report["status"] == "failed":
        # The running session keeps the tree it has; from the next boot
        # Omarchy runs stock until the recipe is fixed.
        shutil.rmtree(new)
        point_at(STOCK)
        print("umami: this session keeps running as it is; after the next reboot Omarchy runs stock.")
        print("umami: fix the recipe (umami status shows why), then: sudo umami flavor <name>")
        return 0 if hook else 2

    git_mark(new)
    if TREE.exists():
        TREE.rename(old)
    new.rename(TREE)
    if old.exists():
        shutil.rmtree(old)
    return 0


def enable(quiet: bool = False) -> int:
    require_root()
    if not TREE.is_dir():
        raise UmamiError("there is no composed flavor yet. Pick one with: sudo umami flavor <name>")
    git_safe_directory()
    point_at(TREE)
    if not quiet:
        print(f"After the next reboot, Omarchy runs the flavor ({TREE}).")
    return 0


EXAMPLES = Path("/usr/share/umami/flavors")


def own_flavors() -> Path | None:
    """~/.config/umami/flavors of the user running sudo: where your own
    flavors live, found by name like the package's."""
    name = os.environ.get("SUDO_USER") or os.environ.get("USER")
    try:
        return Path(pwd.getpwnam(name).pw_dir) / ".config/umami/flavors" if name else None
    except KeyError:
        return None


def find_flavor(flavor: str, dirs: list[Path]) -> Path:
    """A recipe directory given as a path, or a flavor name looked up in
    dirs, the first that has it winning."""
    if (Path(flavor) / "recipe.toml").is_file():
        return Path(flavor)
    for d in dirs:
        if (d / flavor / "recipe.toml").is_file():
            return d / flavor
    names = sorted({p.name for d in dirs if d.is_dir() for p in d.iterdir() if (p / "recipe.toml").is_file()})
    raise UmamiError(f"no flavor {flavor!r}" + (f". Try: {', '.join(names)}" if names else ""))


def flavor(flavor: str) -> int:
    """Install a flavor: a name (your own in ~/.config/umami/flavors, then
    the package's), or a recipe directory. Copies it to /etc/umami,
    composes, and enables it."""
    require_root()
    source = find_flavor(flavor, [d for d in (own_flavors(), EXAMPLES) if d])
    recipe = load(source / "recipe.toml")  # refuses a broken recipe before anything changes

    if OMARCHY_CONF.is_file() and CONF_MARKER not in OMARCHY_CONF.read_text():
        for line in OMARCHY_CONF.read_text().splitlines():
            if line.startswith("export OMARCHY_PATH="):
                current = line.split("=", 1)[1].strip('"')
                if current.rstrip("/") != str(STOCK):
                    raise UmamiError(f"Omarchy is dev-linked to {current}. Run omarchy dev unlink first.")

    ui.say(f"Flavor {recipe.name}, " + ("runs code, trusted like a package" if recipe.code
                                        else "declarative: nothing in it runs as written"))
    install_packages(recipe.packages)  # before anything changes: a failure leaves the current flavor
    staged = RECIPE.parent.with_name(RECIPE.parent.name + ".new")
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source, staged, symlinks=False)
    for path in [staged, *staged.rglob("*")]:
        os.chown(path, 0, 0)
        path.chmod(path.stat().st_mode & ~0o022)
    if RECIPE.parent.exists():
        shutil.rmtree(RECIPE.parent)
    staged.rename(RECIPE.parent)

    live = live_session()  # before anything changes: is this session already on a flavor?
    code = recompose()
    if code != 0:
        return code
    enable(quiet=True)  # what happens next is said below
    if live and switch_live(*live):
        ui.say(f"{recipe.name} is running. Hyprland and the shell are reloading.")
    else:
        ui.say(f"Reboot to run {recipe.name}.")
    return 0


def package_repo(name: str) -> str:
    """The sync repository pacman would install `name` from, or "" if none
    of this machine's (signed, configured) repositories has it."""
    out = subprocess.run(["pacman", "-Si", "--", name], capture_output=True, text=True,
                         env={**os.environ, "LC_ALL": "C"})
    for line in out.stdout.splitlines():
        if line.startswith("Repository"):
            return line.split(":", 1)[1].strip()
    return ""


def install_packages(packages: list):
    """[[package]]: install what is missing, from a repository pacman already
    trusts, with Omarchy's own helper. Packages stay when another flavor takes
    over: other software may need them, and pacman is the place to remove them."""
    missing = [p["name"] for p in packages
               if subprocess.run(["pacman", "-Q", "--", p["name"]], capture_output=True).returncode != 0]
    if not missing:
        return
    for name in missing:
        repo = package_repo(name)
        if not repo:
            raise UmamiError(f"package {name} is in none of this machine's pacman repositories, "
                             "and a flavor installs only from those, never the AUR.")
        ui.say(f"Installing package {name} from the {repo} repository")
    if subprocess.run(["omarchy-pkg-add", *missing]).returncode != 0:
        raise UmamiError(f"installing {', '.join(missing)} failed. The current flavor is unchanged.")


def user_env(uid: int) -> dict:
    return {"XDG_RUNTIME_DIR": f"/run/user/{uid}", "PATH": SYSTEM_SECURE_PATH, "HOME": pwd.getpwuid(uid).pw_dir}


def live_session():
    """(user, uid, Hyprland signature) when the user running sudo has a
    session that already runs from the composed tree. Switching from one
    flavor to another then needs no reboot: every part of the session points
    at the same path, and only what is in it changes. From stock it does."""
    name = os.environ.get("SUDO_USER")
    if not name or name == "root":
        return None
    try:
        uid = pwd.getpwnam(name).pw_uid
    except KeyError:
        return None
    env = subprocess.run(["runuser", "-u", name, "--", "env", *[f"{k}={v}" for k, v in user_env(uid).items()],
                          "systemctl", "--user", "show-environment"], capture_output=True, text=True)
    if f"OMARCHY_PATH={TREE}" not in env.stdout.splitlines():
        return None
    hypr = Path(f"/run/user/{uid}/hypr")
    signatures = sorted(hypr.iterdir(), key=lambda d: d.stat().st_mtime) if hypr.is_dir() else []
    return (name, uid, signatures[-1].name) if signatures else None


def switch_live(name: str, uid: int, signature: str) -> bool:
    """Have the user's own session pick the new flavor up (umami refresh)."""
    env = {**user_env(uid), "HYPRLAND_INSTANCE_SIGNATURE": signature}
    command = f"hl.dsp.exec_cmd([[{SELF} refresh]])"
    result = subprocess.run(["runuser", "-u", name, "--", "env", *[f"{k}={v}" for k, v in env.items()],
                             "hyprctl", "dispatch", command], capture_output=True, text=True)
    return result.returncode == 0 and "ok" in result.stdout


def disable() -> int:
    require_root()
    point_at(STOCK)
    print("After the next reboot, Omarchy runs stock, without a flavor.")
    return 0


def omarchy_path_label(path: str) -> str:
    if not path:
        return "unknown"
    if path.rstrip("/") == str(TREE):
        return "the flavor"
    if path.rstrip("/") == str(STOCK):
        return "stock Omarchy, no flavor"
    return f"{path} (a dev checkout)"


def status(as_json: bool = False) -> int:
    configured = ""
    if OMARCHY_CONF.is_file():
        for line in OMARCHY_CONF.read_text().splitlines():
            if line.startswith("export OMARCHY_PATH="):
                configured = line.split("=", 1)[1].strip('"')
    configured = configured or str(STOCK)
    session = os.environ.get("OMARCHY_PATH", "")
    report = json.loads(REPORT.read_text()) if REPORT.is_file() else None
    if as_json:
        print(json.dumps({"next_boot": configured, "this_session": session or None, "report": report}, indent=2))
        return 0

    if not report:
        ui.say("No flavor yet. Pick one with: sudo umami flavor <name>")
        return 0
    ui.say(f"Flavor {report['flavor']}, {ui.tier(report)}")
    now, boot = omarchy_path_label(session), omarchy_path_label(configured)
    if not session:
        ui.say(f"The next boot runs {boot} (this shell doesn't say what the session runs)")
    elif session.rstrip("/") == configured.rstrip("/"):
        ui.say(f"This session runs {now}, and so does the next boot")
    else:
        ui.say(f"This session runs {now}; from the next boot, {boot}")
    ui.say(f"Composed {ui.when(report['composed_at'])} onto {ui.base(report)}: {ui.applied_count(report)} applied")
    for line in ui.transform_lines(ui.problems(report)):
        ui.say(line)
    for line in ui.builtin_lines(report):
        ui.say(line)
    ui.say(ui.verdict(report, done=True))
    for line in ui.next_steps(report, report["flavor"]):
        ui.say(line)
    return 0
