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
import sys
import tempfile
from pathlib import Path

from .compose import Composer
from .recipe import load

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
        sys.exit("umami: this needs root (run it through sudo)")


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
    counts = {}
    for t in report["transforms"]:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    parts = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    lines = [f"flavor {report['flavor']}: {report['status']} ({parts or 'no transforms'})"]
    for t in report["transforms"]:
        if t["status"] != "applied":
            lines.append(f"  {t['status']}: {t['id']}: {t['detail'] or '; '.join(t['warnings'])}")
    locked = locked_items(report)
    if locked:
        lines.append(f"locked by the flavor: {', '.join(locked)}")
    return "\n".join(lines)


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
                            "notify-send", "-a", "umami", "-u", urgency, title, body],
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
        sys.exit(f"umami: no flavor picked yet ({RECIPE} is missing); try: sudo umami flavor zen")
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
        print("umami: FAILED: a required transform could not apply.")
        print("umami: Omarchy will run stock after the next reboot. Fix the recipe, then:")
        print("umami:   sudo umami recompose && sudo umami enable")
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
        sys.exit("umami: no composed tree yet (run recompose first)")
    git_safe_directory()
    point_at(TREE)
    if not quiet:
        print(f"Omarchy will run from {TREE} after the next reboot.")
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
    sys.exit(f"umami: no flavor {flavor!r}" + (f" (try: {', '.join(names)})" if names else ""))


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
                    sys.exit(f"umami: Omarchy is dev-linked to {current}. Run omarchy dev unlink first.")

    print(f"flavor {recipe.name}: " + ("runs code, trusted like a package" if recipe.code else "declarative"))
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
    enable(quiet=bool(live))
    if live and switch_live(*live):
        print(f"Switched live for {live[0]}: Hyprland and the shell reload now, without a reboot.")
    else:
        print(f"Omarchy will run from {TREE} after the next reboot.")
        print("Reboot to start running the flavor.")
    return 0


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
    print("Omarchy will run stock after the next reboot.")
    return 0


def status() -> int:
    configured = ""
    if OMARCHY_CONF.is_file():
        for line in OMARCHY_CONF.read_text().splitlines():
            if line.startswith("export OMARCHY_PATH="):
                configured = line.split("=", 1)[1].strip('"')
    print(f"configured: OMARCHY_PATH={configured or STOCK}")
    print(f"this shell: OMARCHY_PATH={os.environ.get('OMARCHY_PATH', '<unset>')}")
    if REPORT.is_file():
        report = json.loads(REPORT.read_text())
        print(f"composed:   {report['composed_at']} from {report.get('package') or report['source_version']}")
        print(summary(report))
    else:
        print("no composition report yet")
    return 0
