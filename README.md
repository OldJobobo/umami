# omarchy-flavor

A flavor is a recipe of changes re-applied to whatever the newest Omarchy is. Changes are **transforms** (instructions that target something by meaning and check it still exists), not copies of upstream files, so every upstream update keeps flowing and the flavor rides on top.

Status: prototype. The compositor and dry run work; installing a composed tree as the live `OMARCHY_PATH` comes next.

## Use

```bash
bin/omarchy-flavor plan examples/jobo/recipe.toml          # compose into a scratch dir, show report + changed files
bin/omarchy-flavor plan examples/jobo/recipe.toml --diff   # with the full diff
bin/omarchy-flavor compose examples/jobo/recipe.toml --out DIR
```

`--source` picks the Omarchy tree to compose onto (default `/usr/share/omarchy`). Nothing outside the destination is written.

## Report

Every transform ends up as one of:

| Status | Meaning |
| --- | --- |
| `ok` | Applied |
| `DRIFT` | Applied, but upstream changed what it replaces, or a replacement leaves IPC unanswered |
| `STALE` | Target gone upstream; skipped, the rest of the flavor still applies |
| `FAIL` | Recipe error (bad source, path outside the tree); skipped |

A transform with `required = true` that ends stale or failed makes the whole composition `failed`. The report is written to `.flavor/report.json` in the composed tree, with a copy of the recipe.

## Transforms

| Type | Keys | Notes |
| --- | --- | --- |
| `file.add` | `path`, `source`, `mode`?, `upstream_sha256`? | Replacing an upstream file: record its hash to detect drift |
| `file.remove` | `path` | |
| `patch` | `file`, `anchor`, one of `insert_before` / `insert_after` / `replace_with` | Anchor must occur exactly once |
| `lua.set` | `path` (`general.gaps_out`), `value` | Emitted as `hl.config` in `default/hypr/flavor.lua` |
| `bind.add` / `bind.replace` | `keys`, `description`, `run` (command string or table like `{ launch = "foot" }`) or `lua` (raw dispatcher) | `bind.replace` needs the chord bound literally upstream |
| `bind.remove` | `keys` | |
| `command.wrap` | `command`, `before`?, `after`?, `after_on_failure`? | Original moves to `bin/.flavor/`; privileged entrypoints refuse wrapping |
| `command.replace` | `command`, `source`, `upstream_sha256`? | |
| `shell.plugins.add` | `source` (dir with `manifest.json`) | Installed under `shell/plugins/flavor/<id>/`, trusted as first-party |
| `shell.plugins.remove` | `ids` | Removes the manifest only; the code stays for anything importing it |
| `shell.plugins.replace` | `plugin`, `source` | Reports IPC functions the original answered that the replacement doesn't |
| `shell` | `source` (dir with `shell.qml`) | Whole-shell replacement, same IPC check |

Relative `source` paths resolve against the recipe's directory and cannot leave it.

## How it composes

- The source tree is copied with real files (`cp -rL --reflink=auto`): the package's `bin/` is symlinks into `/usr/bin`, and Omarchy's privileged commands refuse to run unless they resolve to `$OMARCHY_PATH/bin/<name>`.
- Hyprland transforms become one generated module, `default/hypr/flavor.lua`, loaded from `default/hypr/omarchy.lua` after Omarchy's defaults and before `~/.config/hypr` overrides. It calls the same APIs Omarchy does (`hl.config`, `o.rebind`, `hl.unbind`).

## Tests

```bash
python3 -I -m unittest discover -s tests -t .
```

Python 3.11+ standard library only (`tomllib`); stock Omarchy ships everything it needs.
