"""Live-system side of a flavor: recompose the tree and point Omarchy at it.

Everything here runs as root (from the pacman hook or through sudo) and reads
only root-owned inputs: the recipe in /etc/omarchy-flavor and the stock
package in /usr/share/omarchy.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .compose import Composer
from .recipe import load

STOCK = Path("/usr/share/omarchy")
RECIPE = Path("/etc/omarchy-flavor/recipe.toml")
STATE = Path("/var/lib/omarchy-flavor")
TREE = STATE / "tree"
REPORT = STATE / "report.json"
OMARCHY_CONF = Path("/etc/omarchy.conf")
SUDOERS = Path("/etc/sudoers.d/omarchy-dev-path")
SYSTEM_SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/bin"
CONF_MARKER = "# Written by omarchy-flavor"


def require_root():
    if os.geteuid() != 0:
        sys.exit("omarchy-flavor: this needs root (run it through sudo)")


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
    with tempfile.NamedTemporaryFile("w", dir="/etc/sudoers.d", prefix=".omarchy-flavor-", delete=False) as staged:
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
    return "\n".join(lines)


def package_version() -> str:
    """The installed Omarchy package, e.g. "omarchy 4.5.0-3". The tree's own
    version file is not bumped per release."""
    for name in ("omarchy-dev", "omarchy"):
        out = subprocess.run(["pacman", "-Q", name], capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    return ""


def recompose(hook: bool = False) -> int:
    """hook=True (from the pacman hook): report a failed composition loudly
    but exit 0, since pacman prints any nonzero hook as an error, and the
    package transaction itself did succeed."""
    require_root()
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

    if report["status"] == "failed":
        # The running session keeps the tree it has; from the next boot
        # Omarchy runs stock until the recipe is fixed.
        shutil.rmtree(new)
        point_at(STOCK)
        print("omarchy-flavor: FAILED: a required transform could not apply.")
        print("omarchy-flavor: Omarchy will run stock after the next reboot. Fix the recipe, then:")
        print("omarchy-flavor:   sudo omarchy-flavor recompose && sudo omarchy-flavor enable")
        return 0 if hook else 2

    git_mark(new)
    if TREE.exists():
        TREE.rename(old)
    new.rename(TREE)
    if old.exists():
        shutil.rmtree(old)
    return 0


def enable() -> int:
    require_root()
    if not TREE.is_dir():
        sys.exit("omarchy-flavor: no composed tree yet (run recompose first)")
    git_safe_directory()
    point_at(TREE)
    print(f"Omarchy will run from {TREE} after the next reboot.")
    return 0


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
