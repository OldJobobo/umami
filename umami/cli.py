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

from . import system, user
from .compose import REPORT_DIR, Composer
from .recipe import RecipeError, load

DEFAULT_SOURCE = "/usr/share/omarchy"
MARK = {"applied": "ok   ", "drift": "DRIFT", "stale": "STALE", "failed": "FAIL "}


def note_package(report: dict, source: str):
    """Composed onto the installed Omarchy, name its package: the tree's own
    version file is not bumped per release (4.0.0.alpha on every edge build)."""
    if Path(source).resolve() == Path(DEFAULT_SOURCE).resolve():
        report["package"] = report.get("package") or system.package_version()


def print_report(report: dict):
    base = report.get("package") or f"Omarchy {report['source_version'] or '?'}"
    print(f"flavor {report['flavor']} on {base} ({report['source']})")
    code = report.get("code") or []
    print(f"tier: code, trusted like a package ({len(code)} transforms run code)" if code
          else "tier: declarative (nothing in it runs as written)")
    for patch in report.get("builtin") or []:
        if patch["status"] != "applied":
            print(f"        note: built-in patch to {patch['file']} no longer applies ({patch['why']} may not hold)")
    for t in report["transforms"]:
        flags = [f for f, on in (("required", t["required"]), ("locked", t.get("locked"))) if on]
        print(f"  {MARK[t['status']]} {t['id']:<24} {t['type']}" + (f" ({', '.join(flags)})" if flags else ""))
        if t["detail"]:
            print(f"        {t['detail']}")
        for warning in t["warnings"]:
            print(f"        note: {warning}")
    locked = system.locked_items(report)
    if locked:
        print(f"locked by the flavor (users can't change these): {', '.join(locked)}")
    print(f"status: {report['status']}" + (f" (blocked by {', '.join(report['blocking'])})" if report["blocking"] else ""))


def files(root: Path) -> set:
    result = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if Path(dirpath) == root and REPORT_DIR in dirnames:
            dirnames.remove(REPORT_DIR)
        for name in filenames:
            result.add(str((Path(dirpath) / name).relative_to(root)))
    return result


def diff(source: Path, dest: Path, full: bool) -> str:
    """Added, removed and changed files (stock symlinks compared by content)."""
    before, after = files(source), files(dest)
    lines = [f"  added    {path}" for path in sorted(after - before)]
    lines += [f"  removed  {path}" for path in sorted(before - after)]
    changed = [path for path in sorted(before & after)
               if not filecmp.cmp(source / path, dest / path, shallow=False)]
    lines += [f"  changed  {path}" for path in changed]
    text = "\n".join(lines) + ("\n" if lines else "")
    if full:
        for path in sorted(after - before) + changed:
            old = (source / path).read_text(errors="replace").splitlines(keepends=True) if path in before else []
            new = (dest / path).read_text(errors="replace").splitlines(keepends=True)
            text += "".join(difflib.unified_diff(old, new, f"stock/{path}", f"flavor/{path}"))
        for path in sorted(before - after):
            text += f"--- stock/{path}\n+++ /dev/null (removed)\n"
    return text


def cmd_plan(args) -> int:
    recipe = load(args.recipe)
    with tempfile.TemporaryDirectory(prefix="umami-plan-") as scratch:
        dest = Path(scratch) / "tree"
        report = Composer(recipe, Path(args.source), dest).run()
        note_package(report, args.source)
        print_report(report)
        changes = diff(Path(args.source).resolve(), dest, args.diff)
        print("\nchanges against stock:" if changes else "\nno changes against stock")
        print(changes, end="")
        if args.keep:
            kept = Path(args.keep)
            subprocess.run(["cp", "-a", "--reflink=auto", str(dest), str(kept)], check=True)
            print(f"\ncomposed tree kept at {kept}")
    return 2 if report["status"] == "failed" else 0


def cmd_compose(args) -> int:
    recipe = load(args.recipe)
    report = Composer(recipe, Path(args.source), Path(args.out)).run()
    note_package(report, args.source)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 2 if report["status"] == "failed" else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="umami", description="Compose a flavor recipe onto stock Omarchy.")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="compose into a scratch directory and show what would change")
    plan.add_argument("recipe")
    plan.add_argument("--source", default=DEFAULT_SOURCE, help=f"Omarchy tree to compose onto (default {DEFAULT_SOURCE})")
    plan.add_argument("--diff", action="store_true", help="show the full unified diff, not just changed files")
    plan.add_argument("--keep", metavar="DIR", help="keep a copy of the composed tree")
    plan.set_defaults(func=cmd_plan)

    compose = sub.add_parser("compose", help="compose into a new directory")
    compose.add_argument("recipe")
    compose.add_argument("--out", required=True)
    compose.add_argument("--source", default=DEFAULT_SOURCE)
    compose.add_argument("--json", action="store_true")
    compose.set_defaults(func=cmd_compose)

    recompose = sub.add_parser("recompose", help="root: compose /etc/umami/recipe.toml onto stock into /var/lib/umami/tree")
    recompose.add_argument("--hook", action="store_true", help="called from the pacman hook: exit 0 even when the composition fails")
    recompose.set_defaults(func=lambda args: system.recompose(hook=args.hook))

    pick = sub.add_parser("flavor", help="root: install a flavor (a name, or a directory with recipe.toml), compose and enable it")
    pick.add_argument("flavor")
    pick.set_defaults(func=lambda args: system.flavor(args.flavor))

    user_sync = sub.add_parser("user-sync", help="as your user: install the flavor's plugins (runs at login)")
    user_sync.add_argument("--recipe", default=str(user.TREE_RECIPE), help="recipe to read (default: the composed tree's copy)")
    user_sync.add_argument("--dry-run", action="store_true", help="say what would be installed, change nothing")
    user_sync.add_argument("--review", action="store_true",
                           help="ask about plugins you haven't said yes or no to (opened for you at login)")
    user_sync.add_argument("--yes", action="store_true", help="say yes to those plugins without asking")
    user_sync.set_defaults(func=lambda args: user.sync(args.recipe, dry_run=args.dry_run,
                                                       interactive=args.review, assume_yes=args.yes))

    refresh = sub.add_parser("refresh", help="as your user, in your session: pick up a flavor switched live (umami flavor runs it)")
    refresh.set_defaults(func=lambda args: user.refresh())

    for name, help_text in (("enable", "root: point Omarchy at the composed tree (after reboot)"),
                            ("disable", "root: point Omarchy back at stock (after reboot)"),
                            ("status", "show what Omarchy runs from and the last composition report")):
        sub.add_parser(name, help=help_text).set_defaults(func=lambda args, name=name: getattr(system, name)())

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RecipeError as error:
        print(f"umami: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
