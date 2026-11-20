"""How umami talks to people: reports as short, plain sentences.

Facts on a line are joined with " · ", and the next command to run is written
out in full. Scripts and agents read --json instead of parsing any of this.
"""

import datetime
import re
import sys

# What each transform status means to someone reading it.
SAYS = {
    "applied": "applied",
    "drift": "applied, but check it",
    "stale": "skipped, Omarchy changed what it edits",
    "failed": "failed, the recipe has a mistake",
}
PATCH_FAILED = re.compile(r"^(?P<diff>.+?): patch failed: (?P<file>.+?):(?P<line>\d+)$")


def say(message: str = ""):
  print(message, flush=True)


def error(message: str):
  """On a terminal the error ends other output, so it gets a line of space
  and a red label to stand apart from it."""
  if sys.stderr.isatty():
    print(f"\n\033[1;31mError:\033[0m {message}", file=sys.stderr)
  else:
    print(f"Error: {message}", file=sys.stderr)


def plural(n: int, word: str) -> str:
  return f"{n} {word if n == 1 else word + 's'}"


def problems(report: dict) -> list:
  return [t for t in report["transforms"] if t["status"] != "applied"]


def applied_count(report: dict) -> str:
  """'all 8 changes', '10 of 11 changes', 'the 1 change'."""
  total = len(report["transforms"])
  applied = sum(1 for t in report["transforms"] if t["status"] == "applied")
  if applied == total:
    return "the 1 change" if total == 1 else f"all {total} changes"
  return f"{applied} of {plural(total, 'change')}"


def verdict(report: dict, done: bool = False) -> str:
  """The whole composition in a sentence; done: it already ran."""
  off = problems(report)
  if report["status"] == "failed":
    names = ", ".join(report.get("blocking") or []) or "a required change"
    return f"Not applied: {names} could not apply, and the flavor needs it."
  if not report["transforms"]:
    return "Nothing to change."
  if not off:
    return "Everything applied." if done else "Everything applies."
  skipped = sum(1 for t in off if t["status"] in ("stale", "failed"))
  parts = [plural(skipped, "change") + " skipped"] if skipped else []
  if len(off) > skipped:
    parts.append(plural(len(off) - skipped, "change") + " to check")
  return f"Works, with {' and '.join(parts)}."


def tier(report: dict) -> str:
  code = report.get("code") or []
  if code:
    return f"runs code ({plural(len(code), 'transform')}), trusted like a package"
  return "declarative: nothing in it runs as written"


def base(report: dict) -> str:
  return report.get("package") or f"Omarchy {report.get('source_version') or '?'}"


def plainer(reason: str) -> str:
  """'x.diff: patch failed: a/b.json:46' as a sentence."""
  m = PATCH_FAILED.match(reason)
  if m:
    return f"{m['diff']} doesn't fit {m['file']} near line {m['line']}"
  return reason


def detail_lines(t: dict, verbose: bool = False) -> list:
  """A transform's detail, one reason a line. Stale diffs say 'no longer
  applies: A: patch failed: x; B: ...', and the status already says the first
  part. Notes on an applied transform are for the curious: --verbose."""
  detail = t.get("detail") or ""
  if detail.startswith("no longer applies: "):
    detail = detail[len("no longer applies: "):]
  lines = [plainer(part.strip()) for part in detail.split("; ") if part.strip()]
  if verbose or t["status"] != "applied":
    lines += [f"note: {w}" for w in t.get("warnings") or []]
  return lines


def flags(t: dict) -> list:
  return [f for f, on in (("required", t.get("required")), ("locked", t.get("locked")),
                          ("over the theme", t.get("over_theme"))) if on]


def transform_lines(transforms: list, verbose: bool = False) -> list:
  width = min(max((len(t["id"]) for t in transforms), default=0), 34)
  lines = []
  for t in transforms:
    facts = [t.get("what") or t["type"], *flags(t), *([t["type"]] if verbose else [])]
    if t["status"] != "applied":
      facts.insert(0, SAYS[t["status"]])
    lines.append(f"  {t['id']:<{width}}  {' · '.join(facts)}")
    lines += [f"    {line}" for line in detail_lines(t, verbose)]
  return lines


def hidden_notes(transforms: list) -> int:
  return sum(len(t.get("warnings") or []) for t in transforms if t["status"] == "applied")


def when(iso: str) -> str:
  """'Nov 18, 10:48' in local time, from the report's UTC timestamp."""
  try:
    moment = datetime.datetime.fromisoformat(iso).astimezone()
  except (TypeError, ValueError):
    return iso or "?"
  return moment.strftime("%b %-d, %H:%M")


def bar_summary(bar: dict) -> str:
  parts = []
  if bar.get("remove"):
    parts.append(f"{plural(len(bar['remove']), 'widget')} off")
  if bar.get("add"):
    parts.append(f"{len(bar['add'])} added")
  if bar.get("move"):
    parts.append(f"{len(bar['move'])} moved")
  if bar.get("set"):
    parts.append(plural(len(bar["set"]), "setting"))
  return ", ".join(parts)


def suite_text(suite) -> str:
  if isinstance(suite, str):  # a report from before suites were structured
    return suite
  source = suite["package"] if suite["command"] == suite["package"] else f"{suite['command']} from {suite['package']}"
  return f"{suite['id']} ({source}'s installer)"


def extras(report: dict, locked: list) -> list:
  """What the flavor does besides its changes, a line each."""
  defaults = report.get("defaults") or {}
  bar = bar_summary(defaults.get("bar") or {})
  first_login = [f for f in (defaults.get("theme") and f"theme {defaults['theme']}",
                             bar and f"bar: {bar}",
                             report.get("plugins") and "plugins " + ", ".join(report["plugins"])) if f]
  installs = [f for f in (report.get("packages") and "packages " + ", ".join(report["packages"]),
                          report.get("suites") and "plugin suites " + ", ".join(suite_text(x) for x in report["suites"])) if f]
  over = [t["id"] for t in report["transforms"] if t.get("over_theme")]
  machine = system_parts(report.get("system") or {})
  lines = []
  if first_login:
    lines.append(f"At each user's first login, theirs to change afterwards: {' · '.join(first_login)}")
  if installs:
    lines.append(f"Installed with the flavor, each user asked before a plugin or suite: {' · '.join(installs)}")
  if machine:
    lines.append(f"On the machine, undone by umami remove: {' · '.join(machine)}")
  if over:
    lines.append(f"Over any theme, though users can still change them: {', '.join(over)}")
  if locked:
    lines.append(f"Locked, users can't change: {', '.join(locked)}")
  return lines


def system_parts(system: dict) -> list:
  short = lambda units: ", ".join(u.removesuffix(".service") for u in units)
  services = system.get("services") or {}
  return [f for f in (system.get("boot") == "console" and "boots to a console (sudo systemctl start sddm for the desktop)",
                      system.get("sleep") is False and "never sleeps",
                      services.get("enable") and "on: " + short(services["enable"]),
                      services.get("disable") and "off: " + short(services["disable"])) if f]


def builtin_lines(report: dict) -> list:
  """umami's own edits to Omarchy that no longer fit, a line a file."""
  by_file = {}
  for p in report.get("builtin") or []:
    if p["status"] == "stale":
      by_file.setdefault(p["file"], []).append(p["why"])
  return [f"umami's own edit to {file} no longer fits, so this may not hold: {'; '.join(whys)}"
          for file, whys in by_file.items()]


def next_steps(report: dict, flavor: str) -> list:
  """What to do about the report's problems, if anything."""
  off = problems(report)
  if any(t["status"] in ("stale", "failed") for t in off):
    return ["To fix it, update or remove it in the recipe and check with: umami plan <recipe>",
            f"Then apply it with: sudo umami flavor {flavor}"]
  if off:
    return ["These still applied; the notes say what to look at."]
  return []


def one_line(report: dict) -> str:
  """For pacman's output and logs: one line, plus a line per problem."""
  head = f"umami: flavor {report['flavor']}: "
  if report["status"] == "failed":
    sentence = verdict(report)
    lines = [head + sentence[0].lower() + sentence[1:]]
  else:
    lines = [head + applied_count(report) + " applied"]
  for t in problems(report):
    lines.append(f"umami:   {t['id']}: {SAYS[t['status']]}")
  if problems(report):
    lines.append("umami:   details and how to fix: umami status")
  return "\n".join(lines)
