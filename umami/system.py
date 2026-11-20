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
PACKAGES = STATE / "packages.json"
REMOVED = STATE / "removed"
SYSTEM = STATE / "system.json"
DROPPED = STATE / "dropped.json"
NO_SLEEP = {
    Path("/etc/systemd/logind.conf.d/90-umami-no-sleep.conf"):
        "[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n"
        "HandleSuspendKey=ignore\nHandleHibernateKey=ignore\nIdleAction=ignore\n",
    # Omarchy's own file: while it exists, the system menu leaves out Suspend
    # and Hibernate (omarchy-sleep-disabled).
    Path("/etc/systemd/sleep.conf.d/omarchy-sleep-disabled.conf"):
        "[Sleep]\nAllowSuspend=no\nAllowHibernation=no\nAllowSuspendThenHibernate=no\nAllowHybridSleep=no\n",
}
# sshd takes the first value it reads, so this sorts before the others.
SSHD_KEYS_ONLY = Path("/etc/ssh/sshd_config.d/05-umami-keys-only.conf")
OMARCHY_CONF = Path("/etc/omarchy.conf")
SUDOERS = Path("/etc/sudoers.d/omarchy-dev-path")
PROC = Path("/proc")
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
  reboot; sudo's drop-in applies at once. A flavor's tree is also marked
  OMARCHY_PATH_TYPE=flavor, so an Omarchy that knows the type (omacom/omarchy#19538)
  treats it as a package install, not a dev checkout."""
  if path == STOCK:
    OMARCHY_CONF.write_text(f'{CONF_MARKER}\nexport OMARCHY_PATH="{path}"\n')
    SUDOERS.unlink(missing_ok=True)
    return
  OMARCHY_CONF.write_text(f'{CONF_MARKER}\nexport OMARCHY_PATH="{path}"\nexport OMARCHY_PATH_TYPE="flavor"\n')
  with tempfile.NamedTemporaryFile("w", dir="/etc/sudoers.d", prefix=".umami-", delete=False) as staged:
    staged.write(f'Defaults secure_path="{path}/bin:{SYSTEM_SECURE_PATH}"\n')
  try:
    subprocess.run(["visudo", "-cqf", staged.name], check=True)
    os.chmod(staged.name, 0o440)
    os.replace(staged.name, SUDOERS)
  finally:
    Path(staged.name).unlink(missing_ok=True)


def configured_path() -> str:
  """Where /etc/omarchy.conf points Omarchy from the next boot."""
  if OMARCHY_CONF.is_file():
    for line in OMARCHY_CONF.read_text().splitlines():
      if line.startswith("export OMARCHY_PATH="):
        return line.split("=", 1)[1].split()[0].strip('"').rstrip("/") or str(STOCK)
  return str(STOCK)


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
  items += ui.setting_parts(table)
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
      clean_removed_tree()
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
  REMOVED.unlink(missing_ok=True)  # chosen again: no longer waiting to be deleted
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


def flavor(flavor: str, assume_yes: bool = False) -> int:
  """Install a flavor: a name (your own in ~/.config/umami/flavors, then
  the package's), or a recipe directory. Copies it to /etc/umami,
  composes, and enables it."""
  require_root()
  source = find_flavor(flavor, [d for d in (own_flavors(), EXAMPLES) if d])
  recipe = load(source / "recipe.toml")  # refuses a broken recipe before anything changes

  current = configured_path()
  if current not in (str(STOCK), str(TREE)):
    raise UmamiError(f"Omarchy is dev-linked to {current}. Run omarchy dev unlink first.")

  ui.say(f"Flavor {recipe.name}, " + ("runs code, trusted like a package" if recipe.code
                                      else "declarative: nothing in it runs as written"))
  install_packages(recipe.packages, recipe.name)  # before anything changes: a failure leaves the current flavor
  check_units(recipe.system)
  staged = RECIPE.parent.with_name(RECIPE.parent.name + ".new")
  if staged.exists():
    shutil.rmtree(staged)
  shutil.copytree(source, staged, symlinks=False)
  REMOVED.unlink(missing_ok=True)
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
  for line in apply_system(recipe.system):
    print(line)
  if channel_note(recipe.system):
    print(channel_note(recipe.system))
  drop_stock(recipe.drops, recipe.name, assume_yes)
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


def installed_packages() -> dict:
  """The packages flavors installed, {name: flavor}. A package the machine
  had already is never recorded, so removing a flavor leaves it."""
  try:
    record = json.loads(PACKAGES.read_text())
  except (OSError, ValueError):
    return {}
  return record if isinstance(record, dict) else {}


def save_packages(record: dict):
  if record:
    STATE.mkdir(parents=True, exist_ok=True)
    PACKAGES.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
  else:
    PACKAGES.unlink(missing_ok=True)


def install_packages(packages: list, flavor: str = ""):
  """[[package]]: install what is missing, from a repository pacman already
  trusts, with Omarchy's own helper, and record what was installed. Packages
  stay when another flavor takes over; `umami remove` takes them away."""
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
  save_packages({**installed_packages(), **{name: flavor for name in missing}})


# ---- stock apps a flavor leaves out

def dropped_packages() -> dict:
  try:
    record = json.loads(DROPPED.read_text())
  except (OSError, ValueError):
    return {}
  return record if isinstance(record, dict) else {}


def save_dropped(record: dict):
  if record:
    STATE.mkdir(parents=True, exist_ok=True)
    DROPPED.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
  else:
    DROPPED.unlink(missing_ok=True)


def reinstall(names: list) -> bool:
  if not names:
    return True
  ui.say(f"Reinstalling {', '.join(names)}, which the flavor had removed")
  if subprocess.run(["omarchy-pkg-add", *names]).returncode != 0:
    ui.say(f"Reinstalling them failed; try again with: sudo umami remove, or omarchy-pkg-add {' '.join(names)}")
    return False
  return True


def drop_stock(names: list, flavor: str, assume_yes: bool):
  """Remove the apps Omarchy installed that a flavor leaves out, and put
  back the ones an earlier flavor removed that this one keeps. Omarchy's
  updates don't reinstall a removed app, so this lasts; umami records each
  so `umami remove` reinstalls it. Only an app installed on its own goes,
  never one something else needs."""
  record = dropped_packages()
  back = sorted(n for n in record if n not in names)
  if reinstall(back):
    record = {n: f for n, f in record.items() if n not in back}
    save_dropped(record)
  todo = []
  for name in names:
    if name in record or subprocess.run(["pacman", "-Qq", "--", name], capture_output=True).returncode != 0:
      continue
    if subprocess.run(["pacman", "-Qqe", "--", name], capture_output=True).returncode != 0:
      ui.say(f"Keeping {name}: it was installed for another package, not on its own.")
    elif required_by(name):
      ui.say(f"Keeping {name}: {', '.join(sorted(required_by(name)))} needs it.")
    else:
      todo.append(name)
  if not todo:
    return
  extra = sorted(set(removal_set(todo)) - set(todo))
  ui.say(f"Apps {flavor} leaves out: {', '.join(todo)}" + (f" (with {', '.join(extra)})" if extra else ""))
  ui.say("umami remove reinstalls them.")
  if not (assume_yes or confirm("Remove them?")):
    ui.say(f"Kept them. Remove them later with: sudo umami flavor {flavor} --yes")
    return
  if subprocess.run(["omarchy-pkg-drop", *todo]).returncode != 0:
    raise UmamiError(f"removing {', '.join(todo)} failed.")
  save_dropped({**record, **{n: flavor for n in todo}})


# ---- [system]: the machine around Omarchy

def systemctl(*args) -> subprocess.CompletedProcess:
  return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def load_system() -> dict:
  try:
    record = json.loads(SYSTEM.read_text())
  except (OSError, ValueError):
    return {}
  return record if isinstance(record, dict) else {}


def save_system(record: dict):
  if record:
    STATE.mkdir(parents=True, exist_ok=True)
    SYSTEM.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
  else:
    SYSTEM.unlink(missing_ok=True)


def channel_note(system: dict) -> str:
  """A flavor's channel is checked, not switched: a switch updates the whole
  system, which is the user's call. Asked as stock, where the answer is the
  installed packages' channel rather than "dev"."""
  want = system.get("channel")
  if not want:
    return ""
  out = subprocess.run([str(STOCK / "bin/omarchy-channel-current")], capture_output=True, text=True,
                       env={**os.environ, "OMARCHY_PATH": str(STOCK), "PATH": f"{STOCK}/bin:{SYSTEM_SECURE_PATH}"})
  current = out.stdout.strip() or "unknown"
  if current == want:
    return ""
  return f"  note      this machine is on {current}, and the flavor expects {want}: omarchy channel set {want} (it updates the whole system)"


def regular_users() -> list:
  """The people on this machine: accounts with a login shell, not services."""
  return sorted(e.pw_name for e in pwd.getpwall()
                if 1000 <= e.pw_uid < 60000 and not e.pw_shell.endswith(("nologin", "false")))


def check_units(system: dict):
  """Before anything changes: every service and group the flavor names
  exists (a [[package]] may have just installed it), and so does its
  timezone."""
  for group in system.get("groups", []):
    if subprocess.run(["getent", "group", group], capture_output=True).returncode != 0:
      raise UmamiError(f"[system] names group {group}, which this machine doesn't have (add its [[package]]).")
  if system.get("timezone") and not (Path("/usr/share/zoneinfo") / system["timezone"]).is_file():
    raise UmamiError(f"[system] timezone {system['timezone']} isn't a zone this machine knows.")
  for units in (system.get("services") or {}).values():
    for unit in units:
      if systemctl("cat", "--", unit).returncode != 0:
        raise UmamiError(f"[system] names {unit}, which isn't installed (add its [[package]]).")


def keys_only(record: dict) -> list:
  """sshd for a flavor takes keys only, as omarchy-setup-security-sshd sets
  it up, and its port opens rate-limited. Checked with sshd itself before it
  listens: a config it ignores would leave passwords on."""
  lines = []
  subprocess.run(["ssh-keygen", "-A"], capture_output=True)
  SSHD_KEYS_ONLY.parent.mkdir(parents=True, exist_ok=True)
  SSHD_KEYS_ONLY.write_text("# Written by umami for the flavor's sshd; umami remove deletes it.\n"
                            "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n")
  effective = subprocess.run(["sshd", "-T"], capture_output=True, text=True)
  settings = {line.lower() for line in effective.stdout.splitlines()}
  if effective.returncode != 0 or not {"passwordauthentication no", "kbdinteractiveauthentication no"} <= settings:
    SSHD_KEYS_ONLY.unlink()
    raise UmamiError("sshd would still accept passwords (another sshd config overrides umami's), so it stays off.")
  record["ssh"] = {"keys_only": True}
  if shutil.which("ufw"):
    if "22/tcp" not in subprocess.run(["ufw", "status"], capture_output=True, text=True).stdout:
      subprocess.run(["ufw", "limit", "22/tcp", "comment", "umami-sshd"], capture_output=True)
      subprocess.run(["ufw", "reload"], capture_output=True)
      record["ssh"]["ufw"] = True
      lines.append("  opened    port 22 in the firewall, rate limited")
  if not any((Path(e.pw_dir) / ".ssh/authorized_keys").is_file() for e in pwd.getpwall() if e.pw_uid >= 1000):
    lines.append("  note      no SSH key is authorized yet, and passwords are off: omarchy-setup-security-sshd adds one")
  return lines


def undo_ssh(entry: dict):
  SSHD_KEYS_ONLY.unlink(missing_ok=True)
  if systemctl("is-active", "--quiet", "sshd.service").returncode == 0:
    systemctl("reload", "sshd.service")
  if entry.get("ufw"):
    subprocess.run(["ufw", "delete", "limit", "22/tcp"], capture_output=True)
    subprocess.run(["ufw", "reload"], capture_output=True)


def apply_system(system: dict) -> list:
  """Bring the machine to a flavor's [system]. What the last flavor set and
  this one doesn't want is undone first; what was there before any flavor
  stays recorded, so `umami remove` puts it back. A service a flavor turned
  on is only disabled, not stopped: stopping sshd or tailscaled could cut
  off the session doing it, so it stops at the next boot."""
  record, lines = load_system(), []
  services = record.setdefault("services", {})
  wanted = {unit: action for action, units in (system.get("services") or {}).items() for unit in units}

  for unit, entry in list(services.items()):
    if wanted.get(unit) == entry["action"]:
      continue
    if entry["action"] == "enable" and entry["was"] != "enabled":
      systemctl("disable", "--", unit)
      lines.append(f"  disabled  {unit.removesuffix('.service')} (stops at the next boot)")
    elif entry["action"] == "disable" and entry["was"] == "enabled":
      systemctl("enable", "--now", "--", unit)
      lines.append(f"  enabled   {unit.removesuffix('.service')} again")
    del services[unit]
    save_system(record)
  if record.get("ssh") and wanted.get("sshd.service") != "enable":
    undo_ssh(record.pop("ssh"))
    lines.append("  removed   sshd's keys-only setting")
  if record.get("boot") and system.get("boot") != "console":
    systemctl("set-default", record.pop("boot"))
    lines.append("  restored  booting to the desktop")
  if record.get("no_sleep") and system.get("sleep") is not False:
    created = record.pop("no_sleep")
    for path in NO_SLEEP if created is True else map(Path, created):  # True: from before it was a list
      path.unlink(missing_ok=True)
    lines.append("  restored  sleep")
  wanted_groups = set(system.get("groups", []))
  kept = []
  for user, group in record.get("groups", []):
    if group in wanted_groups:
      kept.append([user, group])
    else:
      subprocess.run(["gpasswd", "-d", user, group], capture_output=True)
      lines.append(f"  removed   {user} from {group}")
  record["groups"] = kept
  if record.get("timezone") and not system.get("timezone"):
    was = record.pop("timezone")
    subprocess.run(["timedatectl", "set-timezone", was], capture_output=True)
    lines.append(f"  restored  timezone {was}")
  save_system(record)

  for unit, action in wanted.items():
    if unit in services:
      continue
    was = systemctl("is-enabled", "--", unit).stdout.strip() or "unknown"
    if unit == "sshd.service" and action == "enable" and not record.get("ssh"):
      lines += keys_only(record)  # before it listens
      save_system(record)
    result = systemctl(action, "--now", "--", unit)
    if result.returncode != 0:
      raise UmamiError(f"systemctl {action} {unit} failed: " + (result.stderr or result.stdout).strip()[:300])
    services[unit] = {"action": action, "was": was}
    save_system(record)
    lines.append(f"  {action + 'd':<9} {unit.removesuffix('.service')}")
  if system.get("boot") == "console" and not record.get("boot"):
    record["boot"] = systemctl("get-default").stdout.strip() or "graphical.target"
    systemctl("set-default", "multi-user.target")
    save_system(record)
    lines.append("  boot      to a console from the next boot (sudo systemctl start sddm for the desktop)")
  if system.get("sleep") is False and not record.get("no_sleep"):
    created = []
    for path, text in NO_SLEEP.items():
      if path.exists():  # Omarchy's own, on hardware that can't sleep: never deleted
        continue
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text("# Written by umami: the flavor never sleeps. umami remove deletes it.\n" + text)
      created.append(str(path))
    record["no_sleep"] = created
    save_system(record)
    lines.append("  sleep     off: no suspend on idle, lid or key, and none in the menu (the lid from the next boot)")
  have = {tuple(pair) for pair in record["groups"]}
  for group in system.get("groups", []):
    members = subprocess.run(["getent", "group", group], capture_output=True, text=True).stdout.strip().split(":")[-1]
    for user in regular_users():
      if (user, group) in have or user in members.split(","):
        continue
      if subprocess.run(["gpasswd", "-a", user, group], capture_output=True).returncode == 0:
        record["groups"].append([user, group])
        save_system(record)
        lines.append(f"  added     {user} to {group} (from their next login)")
  if system.get("timezone") and not record.get("timezone"):
    was = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True, text=True).stdout.strip()
    if was != system["timezone"]:
      record["timezone"] = was or "UTC"
      subprocess.run(["timedatectl", "set-timezone", system["timezone"]], capture_output=True)
      save_system(record)
      lines.append(f"  timezone  {system['timezone']}")
  if not record["groups"]:
    record.pop("groups")
  if not services:
    record.pop("services", None)
  save_system(record)
  return lines


def user_env(uid: int) -> dict:
  return {"XDG_RUNTIME_DIR": f"/run/user/{uid}", "PATH": SYSTEM_SECURE_PATH, "HOME": pwd.getpwuid(uid).pw_dir}


def hypr_session():
  """(user, uid, Hyprland signature) of the Hyprland session of the user
  running sudo, whatever it runs from."""
  name = os.environ.get("SUDO_USER")
  if not name or name == "root":
    return None
  try:
    uid = pwd.getpwnam(name).pw_uid
  except KeyError:
    return None
  hypr = Path(f"/run/user/{uid}/hypr")
  signatures = sorted(hypr.iterdir(), key=lambda d: d.stat().st_mtime) if hypr.is_dir() else []
  return (name, uid, signatures[-1].name) if signatures else None


def runs_tree(name: str, uid: int) -> bool:
  env = subprocess.run(["runuser", "-u", name, "--", "env", *[f"{k}={v}" for k, v in user_env(uid).items()],
                        "systemctl", "--user", "show-environment"], capture_output=True, text=True)
  return f"OMARCHY_PATH={TREE}" in env.stdout.splitlines()


def live_session():
  """The sudo user's session, when it already runs from the composed tree.
  Switching from one flavor to another then needs no reboot: every part of
  the session points at the same path, and only what is in it changes. From
  stock it does."""
  session = hypr_session()
  return session if session and runs_tree(*session[:2]) else None


def tree_in_use() -> bool:
  """Whether anything runs from the tree. Every process of a session on it
  carries OMARCHY_PATH=<tree>, whoever is logged in and however, so this
  sees sessions the user running sudo doesn't own."""
  needle = f"OMARCHY_PATH={TREE}".encode()
  for proc in PROC.iterdir() if PROC.is_dir() else []:
    if not proc.name.isdigit() or int(proc.name) in (os.getpid(), os.getppid()):
      continue
    try:
      if needle in (proc / "environ").read_bytes().split(b"\0"):
        return True
    except OSError:
      continue
  return False


def session_env(name: str, uid: int) -> dict:
  """The user's session environment (display, Hyprland, D-Bus, PATH), so a
  command run from sudo's terminal reaches their desktop."""
  out = subprocess.run(["runuser", "-u", name, "--", "env", *[f"{k}={v}" for k, v in user_env(uid).items()],
                        "systemctl", "--user", "show-environment"], capture_output=True, text=True).stdout
  env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
  return {**user_env(uid), **env, "HOME": pwd.getpwuid(uid).pw_dir}


def run_in_session(name: str, uid: int, signature: str, command: str) -> bool:
  env = {**user_env(uid), "HYPRLAND_INSTANCE_SIGNATURE": signature}
  result = subprocess.run(["runuser", "-u", name, "--", "env", *[f"{k}={v}" for k, v in env.items()],
                           "hyprctl", "dispatch", f"hl.dsp.exec_cmd([[{command}]])"], capture_output=True, text=True)
  return result.returncode == 0 and "ok" in result.stdout


def switch_live(name: str, uid: int, signature: str) -> bool:
  """Have the user's own session pick the new flavor up (umami refresh)."""
  return run_in_session(name, uid, signature, f"{SELF} refresh")


def disable() -> int:
  require_root()
  point_at(STOCK)
  print("After the next reboot, Omarchy runs stock, without a flavor.")
  return 0


def boot_id() -> str:
  try:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
  except OSError:
    return ""


def clean_removed_tree():
  """Delete the tree of a removed flavor once nothing can run from it: after
  the reboot that follows `umami remove`, while no session uses it and
  Omarchy isn't pointed at it again."""
  if not (REMOVED.is_file() and TREE.exists()) or REMOVED.read_text().strip() == boot_id():
    return False
  if configured_path() == str(TREE):
    REMOVED.unlink()
    return False
  if tree_in_use():
    return False
  shutil.rmtree(TREE)
  REMOVED.unlink()
  subprocess.run(["git", "config", "--system", "--unset-all", "safe.directory", str(TREE)], capture_output=True)
  return True


def required_by(name: str) -> set:
  out = subprocess.run(["pacman", "-Qi", "--", name], capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C"}).stdout
  for line in out.splitlines():
    if line.startswith("Required By"):
      value = line.split(":", 1)[1].split()
      return set() if value == ["None"] else set(value)
  return set()


def confirm(prompt: str) -> bool:
  if shutil.which("gum") and os.isatty(0):
    return subprocess.run(["gum", "confirm", "--default=false", prompt]).returncode == 0
  try:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
  except EOFError:
    return False


def suite_users() -> dict:
  """{package: [users]} for the shell suites users still have installed. A
  suite's uninstaller is its package's command, so the package stays until
  each of them has answered."""
  holders = {}
  for entry in pwd.getpwall():
    if entry.pw_uid < 1000 or entry.pw_uid == 65534:
      continue
    try:
      state = json.loads((Path(entry.pw_dir) / ".local/state/umami/plugins.json").read_text())
    except (OSError, ValueError):
      continue
    for key, known in state.items() if isinstance(state, dict) else []:
      if key.startswith("suite:") and isinstance(known, dict) and known.get("package"):
        holders.setdefault(known["package"], []).append(entry.pw_name)
  return holders


def removal_set(names: list) -> list:
  """Everything pacman removes with these: -Rns also takes dependencies
  nothing else needs, which the question has to show. (--nosave and
  --print can't go together; it lists the same packages.)"""
  out = subprocess.run(["pacman", "-Rsp", "--print-format", "%n", "--", *names], capture_output=True, text=True)
  if out.returncode != 0:
    raise UmamiError(f"pacman can't remove {', '.join(names)}: " + (out.stderr or out.stdout).strip()[:300])
  return out.stdout.split()


def drop_packages(assume_yes: bool):
  """Remove the packages flavors installed, except ones something else on
  the machine still needs or a user's suite still runs from."""
  record = installed_packages()
  installed = {name for name in record
               if subprocess.run(["pacman", "-Q", "--", name], capture_output=True).returncode == 0}
  present = set(installed)
  suites = suite_users()
  for name in sorted(present & set(suites)):
    ui.say(f"Keeping package {name}: {', '.join(suites[name])} still has its suite "
           "(umami user-sync --remove asks them; then run sudo umami remove again).")
  present -= set(suites)
  needs = {name: required_by(name) for name in present}
  drop = set(present)
  while True:  # a package only the others in the list need can go with them
    kept = {name for name in drop if needs[name] - drop}
    if not kept:
      break
    drop -= kept
  for name in sorted(present - drop):
    ui.say(f"Keeping package {name}: {', '.join(sorted(needs[name] - drop))} needs it.")
  if drop:
    extra = sorted(set(removal_set(sorted(drop))) - drop)
    ui.say(f"Packages the flavor installed: {', '.join(sorted(drop))}")
    if extra:
      ui.say(f"With them, packages only they needed: {', '.join(extra)}")
    if not (assume_yes or confirm("Remove them?")):
      ui.say("Kept them. Remove them later with: sudo umami remove")
      return
    if subprocess.run(["omarchy-pkg-drop", *sorted(drop)]).returncode != 0:
      raise UmamiError(f"removing {', '.join(sorted(drop))} failed.")
  save_packages({name: flavor for name, flavor in record.items() if name in installed - drop})


def remove(assume_yes: bool = False) -> int:
  """Take the flavor off the machine: Omarchy back to stock, the packages it
  installed, and in the sudo user's session, its plugins (asked) and the
  theme and bar it set (put back). The flavor stays available to install
  again."""
  require_root()
  cleaned = clean_removed_tree()
  name = load(RECIPE).name if RECIPE.is_file() else ""
  if not (name or installed_packages() or SYSTEM.is_file() or DROPPED.is_file()
          or (TREE.exists() and not REMOVED.is_file())):
    if cleaned:
      ui.say("Deleted the removed flavor's files.")
      return 0
    raise UmamiError("no flavor is installed.")
  if name:
    ui.say(f"Removing flavor {name}.")

  # Your half first, here, while the flavor's packages (a suite's own
  # uninstaller among them) are still installed.
  session = hypr_session()
  if session:
    user, uid, _ = session
    flags = ["--yes"] if assume_yes else []
    subprocess.run(["runuser", "-u", user, "--", "env", *[f"{k}={v}" for k, v in session_env(user, uid).items()],
                    SELF, "user-sync", "--remove", *flags])
  else:
    ui.say("For each user, while logged in: umami user-sync --remove puts back the theme and bar and asks about the plugins.")
  for line in apply_system({}):  # before the packages its services come from
    print(line)
  if reinstall(sorted(dropped_packages())):
    save_dropped({})
  drop_packages(assume_yes)

  current = configured_path()
  linked = current not in (str(STOCK), str(TREE))
  if not linked:
    point_at(STOCK)
  else:
    ui.say(f"Omarchy is dev-linked to {current}; that stays as it is.")
  if RECIPE.parent.exists():
    shutil.rmtree(RECIPE.parent)
  REPORT.unlink(missing_ok=True)
  if TREE.exists() and tree_in_use():
    REMOVED.write_text(boot_id() + "\n")
    ui.say("A session still runs the flavor. Reboot to return to stock Omarchy.")
    ui.say("After the reboot, its files go at the next Omarchy update, or now with: sudo umami remove")
  else:
    if TREE.exists():
      shutil.rmtree(TREE)
      subprocess.run(["git", "config", "--system", "--unset-all", "safe.directory", str(TREE)], capture_output=True)
    REMOVED.unlink(missing_ok=True)
    ui.say("Omarchy runs stock from the next boot." if name and not linked else "Done.")
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
  configured = configured_path()
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
