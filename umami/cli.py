"""umami command line."""

import argparse
import difflib
import filecmp
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import system, ui, user
from .compose import REPORT_DIR, Composer
from .recipe import UmamiError, load

DEFAULT_SOURCE = "/usr/share/omarchy"
JSON_HELP = "print the full report as JSON, for scripts and agents"


def note_package(report: dict, source: str):
  """Composed onto the installed Omarchy, name its package: the tree's own
  version file is not bumped per release (4.0.0.alpha on every edge build)."""
  if Path(source).resolve() == Path(DEFAULT_SOURCE).resolve():
    report["package"] = report.get("package") or system.package_version()


def print_report(report: dict, changes: dict | None = None, recipe_dir: str = "", verbose: bool = False):
  """A composition report for people: what the flavor is, its changes, what
  else it does, and how it went, with the next command to run."""
  say = ui.say
  say(f"Flavor {report['flavor']}, {ui.tier(report)} · onto {ui.base(report)}")
  if report["transforms"]:
    say(f"Changes: {ui.applied_count(report)} {'apply' if len(report['transforms']) > 1 else 'applies'}")
    for line in ui.transform_lines(report["transforms"], verbose=verbose):
      say(line)
    notes = ui.hidden_notes(report["transforms"])
    if notes and not verbose:
      say(f"  ({ui.plural(notes, 'note')} on applied changes: --verbose shows them)")
  for line in ui.extras(report, system.locked_items(report)):
    say(line)
  if changes is not None:
    counts = [f"{len(changes[k])} {k}" for k in ("added", "removed", "changed") if changes[k]]
    say(f"Files against stock: {' · '.join(counts) or 'none'} (--files lists them, --diff shows the changes)")
  for line in ui.builtin_lines(report):
    say(line)
  say(ui.verdict(report))
  steps = ui.next_steps(report, recipe_dir or report["flavor"])
  for line in steps:
    say(line)
  if not steps and recipe_dir:
    say(f"Install it with: sudo umami flavor {recipe_dir}")


def files(root: Path) -> set:
  result = set()
  for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    if Path(dirpath) == root and REPORT_DIR in dirnames:
      dirnames.remove(REPORT_DIR)
    for name in filenames:
      result.add(str((Path(dirpath) / name).relative_to(root)))
  return result


def changed_files(source: Path, dest: Path) -> dict:
  """Added, removed and changed files (stock symlinks compared by content)."""
  before, after = files(source), files(dest)
  return {
      "added": sorted(after - before),
      "removed": sorted(before - after),
      "changed": [path for path in sorted(before & after)
                  if not filecmp.cmp(source / path, dest / path, shallow=False)],
  }


def file_list(changes: dict) -> str:
  return "".join(f"  {kind:<8} {path}\n" for kind in ("added", "removed", "changed") for path in changes[kind])


def unified(source: Path, dest: Path, changes: dict) -> str:
  text = ""
  for path in changes["added"] + changes["changed"]:
    old = (source / path).read_text(errors="replace").splitlines(keepends=True) if path not in changes["added"] else []
    new = (dest / path).read_text(errors="replace").splitlines(keepends=True)
    text += "".join(difflib.unified_diff(old, new, f"stock/{path}", f"flavor/{path}"))
  for path in changes["removed"]:
    text += f"--- stock/{path}\n+++ /dev/null (removed)\n"
  return text


def recipe_hint(path: str) -> str:
  """How to name this recipe to `sudo umami flavor`: its name when it sits
  in a flavors folder umami looks in, else its directory."""
  folder = Path(path).resolve().parent
  if folder.parent in [d.resolve() for d in (system.own_flavors(), system.EXAMPLES) if d]:
    return folder.name
  return str(folder)


def cmd_plan(args) -> int:
  recipe = load(args.recipe)
  source = Path(args.source).resolve()
  with tempfile.TemporaryDirectory(prefix="umami-plan-") as scratch:
    dest = Path(scratch) / "tree"
    report = Composer(recipe, source, dest).run()
    note_package(report, args.source)
    changes = changed_files(source, dest)
    if args.json:
      report["files"] = changes
      if args.diff:
        report["diff"] = unified(source, dest, changes)
      print(json.dumps(report, indent=2))
    else:
      print_report(report, changes, recipe_hint(args.recipe), args.verbose)
      if args.files or args.diff:
        print()
        print(file_list(changes), end="")
      if args.diff:
        print(unified(source, dest, changes), end="")
    if args.keep:
      kept = Path(args.keep)
      subprocess.run(["cp", "-a", "--reflink=auto", str(dest), str(kept)], check=True)
      if not args.json:
        ui.say(f"Composed tree kept at {kept}")
  return 2 if report["status"] == "failed" else 0


def cmd_compose(args) -> int:
  recipe = load(args.recipe)
  report = Composer(recipe, Path(args.source), Path(args.out)).run()
  note_package(report, args.source)
  if args.json:
    print(json.dumps(report, indent=2))
  else:
    print_report(report, verbose=args.verbose)
    ui.say(f"Composed into {args.out}")
  return 2 if report["status"] == "failed" else 0


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(prog="umami", description="Compose a flavor recipe onto stock Omarchy.")
  sub = parser.add_subparsers(dest="command", required=True)

  plan = sub.add_parser("plan", help="compose into a scratch directory and show what would change")
  plan.add_argument("recipe")
  plan.add_argument("--source", default=DEFAULT_SOURCE, help=f"Omarchy tree to compose onto (default {DEFAULT_SOURCE})")
  plan.add_argument("--files", action="store_true", help="list the files that differ from stock")
  plan.add_argument("--diff", action="store_true", help="show the full unified diff against stock")
  plan.add_argument("--json", action="store_true", help=JSON_HELP)
  plan.add_argument("-v", "--verbose", action="store_true", help="show each change's type and informational notes")
  plan.add_argument("--keep", metavar="DIR", help="keep a copy of the composed tree")
  plan.set_defaults(func=cmd_plan)

  compose = sub.add_parser("compose", help="compose into a new directory")
  compose.add_argument("recipe")
  compose.add_argument("--out", required=True)
  compose.add_argument("--source", default=DEFAULT_SOURCE)
  compose.add_argument("--json", action="store_true", help=JSON_HELP)
  compose.add_argument("-v", "--verbose", action="store_true", help="show each change's type and informational notes")
  compose.set_defaults(func=cmd_compose)

  recompose = sub.add_parser("recompose", help="root: compose /etc/umami/recipe.toml onto stock into /var/lib/umami/tree")
  recompose.add_argument("--hook", action="store_true", help="called from the pacman hook: exit 0 even when the composition fails")
  recompose.set_defaults(func=lambda args: system.recompose(hook=args.hook))

  pick = sub.add_parser("flavor", help="root: install a flavor (a name, or a directory with recipe.toml), compose and enable it")
  pick.add_argument("flavor")
  pick.set_defaults(func=lambda args: system.flavor(args.flavor))

  remove = sub.add_parser("remove", help="root: remove the flavor: back to stock, its packages, plugins, theme and bar")
  remove.add_argument("--yes", action="store_true", help="remove its packages without asking")
  remove.set_defaults(func=lambda args: system.remove(assume_yes=args.yes))

  user_sync = sub.add_parser("user-sync", help="as your user: install the flavor's plugins (runs at login)")
  user_sync.add_argument("--recipe", default=str(user.TREE_RECIPE), help="recipe to read (default: the composed tree's copy)")
  user_sync.add_argument("--dry-run", action="store_true", help="say what would be installed, change nothing")
  user_sync.add_argument("--review", action="store_true",
                         help="ask about plugins you haven't said yes or no to (opened for you at login)")
  user_sync.add_argument("--yes", action="store_true", help="say yes to those plugins without asking")
  user_sync.add_argument("--remove", action="store_true",
                         help="after umami remove: put back your theme and bar, and ask about the flavor's plugins")
  user_sync.set_defaults(func=lambda args: user.remove(interactive=args.review, assume_yes=args.yes, dry_run=args.dry_run) if args.remove
                         else user.sync(args.recipe, dry_run=args.dry_run, interactive=args.review, assume_yes=args.yes))

  refresh = sub.add_parser("refresh", help="as your user, in your session: pick up a flavor switched live (umami flavor runs it)")
  refresh.set_defaults(func=lambda args: user.refresh())

  for name, help_text in (("enable", "root: point Omarchy at the composed tree (after reboot)"),
                          ("disable", "root: point Omarchy back at stock (after reboot)")):
    sub.add_parser(name, help=help_text).set_defaults(func=lambda args, name=name: getattr(system, name)())

  status = sub.add_parser("status", help="show which flavor runs, and how its last composition went")
  status.add_argument("--json", action="store_true", help=JSON_HELP)
  status.set_defaults(func=lambda args: system.status(as_json=args.json))

  args = parser.parse_args(argv)
  try:
    return args.func(args)
  except UmamiError as error:
    ui.error(str(error))
    return 1
  except KeyboardInterrupt:
    return 130
  except BrokenPipeError:
    # A reader like head stopped early: nothing is wrong, and Python mustn't
    # complain again while flushing on exit.
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


if __name__ == "__main__":
  sys.exit(main())
