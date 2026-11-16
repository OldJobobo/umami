"""omarchy-flavor command line."""

import argparse
import difflib
import filecmp
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import system
from .compose import REPORT_DIR, Composer
from .recipe import RecipeError, load

DEFAULT_SOURCE = "/usr/share/omarchy"
MARK = {"applied": "ok   ", "drift": "DRIFT", "stale": "STALE", "failed": "FAIL "}


def print_report(report: dict):
    print(f"flavor {report['flavor']} on Omarchy {report['source_version'] or '?'} ({report['source']})")
    for t in report["transforms"]:
        print(f"  {MARK[t['status']]} {t['id']:<24} {t['type']}" + (" (required)" if t["required"] else ""))
        if t["detail"]:
            print(f"        {t['detail']}")
        for warning in t["warnings"]:
            print(f"        note: {warning}")
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
    with tempfile.TemporaryDirectory(prefix="omarchy-flavor-plan-") as scratch:
        dest = Path(scratch) / "tree"
        report = Composer(recipe, Path(args.source), dest).run()
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
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 2 if report["status"] == "failed" else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy-flavor", description="Compose a flavor recipe onto stock Omarchy.")
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

    recompose = sub.add_parser("recompose", help="root: compose /etc/omarchy-flavor/recipe.toml onto stock into /var/lib/omarchy-flavor/tree")
    recompose.add_argument("--hook", action="store_true", help="called from the pacman hook: exit 0 even when the composition fails")
    recompose.set_defaults(func=lambda args: system.recompose(hook=args.hook))

    for name, help_text in (("enable", "root: point Omarchy at the composed tree (after reboot)"),
                            ("disable", "root: point Omarchy back at stock (after reboot)"),
                            ("status", "show what Omarchy runs from and the last composition report")):
        sub.add_parser(name, help=help_text).set_defaults(func=lambda args, name=name: getattr(system, name)())

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RecipeError as error:
        print(f"omarchy-flavor: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
