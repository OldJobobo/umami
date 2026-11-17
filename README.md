# omarchy-flavor

A flavor is a recipe of changes re-applied to whatever the newest Omarchy is. Changes are **transforms** (instructions that target something by meaning and check it still exists), not copies of upstream files, so every upstream update keeps flowing and the flavor rides on top.

Status: prototype. The dry run works, and a flavor runs live: verified in a VM on stock Omarchy 4.5.0 stable (desktop, shell, update, pacman hook, required-transform fallback, uninstall). Not yet tested across a real Omarchy version bump.

## Install on a machine (prototype)

```bash
./install.sh examples/jobo      # compositor -> /usr/lib/omarchy-flavor, recipe -> /etc/omarchy-flavor,
                                # pacman hook, compose, point Omarchy at the tree; reboot after
./uninstall.sh                  # back to stock from the next boot
```

Refuses to install while Omarchy is dev-linked to a checkout (`omarchy dev unlink` first). Once installed:

```bash
/usr/lib/omarchy-flavor/bin/omarchy-flavor status               # what Omarchy runs from + last report
sudo /usr/lib/omarchy-flavor/bin/omarchy-flavor recompose       # after editing /etc/omarchy-flavor
sudo /usr/lib/omarchy-flavor/bin/omarchy-flavor enable|disable  # flavor or stock, from the next boot
```

What happens on an update:

1. `omarchy update` upgrades the Omarchy packages.
2. The pacman hook (`/etc/pacman.d/hooks/90-omarchy-flavor.hook`) recomposes stock + recipe into `/var/lib/omarchy-flavor/tree.new` as root, then swaps it into `tree`, inside the transaction, so migrations run from the composed tree.
3. A stale transform is reported in pacman's output and skipped. A required transform that can't apply leaves the running session alone and points `/etc/omarchy.conf` back at stock for the next boot.

The tree carries a `.git` with no upstream so `omarchy-update-dev` skips it instead of aborting the update, and is registered as a system git `safe.directory` because it is root-owned. Both go away once Omarchy has a flavor mode.

## Dry run

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
| `diff` | `source`, or `sources` (tried in order, first that applies wins), `strip`? | A `git diff`, applied all or nothing; one that no longer applies is stale. For carrying an open pull request, with a version per Omarchy line it lands on |
| `lua` | `code` or `source` (a `.lua` file in the recipe) | Raw Lua for `flavor.lua`, for what has no structured transform, such as a plugin's own binding snippet. Guard anything that can fail |
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

## Declarative first

A flavor is declarative unless its `[flavor]` table says `code = true`. Declarative transforms are data that omarchy-flavor checks and turns into configuration itself, the way Omarchy treats a theme; nothing in them runs as written:

- `lua.set` on how windows look and are laid out (`general`, `decoration`, `animations`, `dwindle`, `master`, `scrolling`, `group`, `misc`), with plain values
- `bind.remove`, and `bind.add`/`bind.replace` with an `action`: `focus` or `swap` (`l`, `r`, `u`, `d`), `layout` (a layout message), `omarchy` (an Omarchy command with plain arguments), `menu` or `panel`
- `shell.plugins.remove` of bar widgets, panels, overlays and menus (not services such as the lock screen)
- `[defaults]` and `[[plugin]]`

Everything else runs code someone wrote: diffs, patches, replaced or wrapped commands, added or removed files, raw Lua, a binding to `run` a shell command or to raw `lua`, removing a service. Such a recipe must say `code = true`, and `plan` prints its tier. The composed Omarchy also runs through sudo, so a flavor that runs code is trusted like a package: install it from a signed package, never from a bare URL.

## Plugins (the user layer)

```toml
[[plugin]]
id = "evindor.keystroke"
repo = "https://github.com/evindor/keystroke"
commit = "0c74f2dd7466f25faf2cda4a39697f237afbe774"   # full sha: the marketplace's validated commit
section = "left"                                     # optional bar placement
index = 0
```

Plugins from the marketplace never go into the composed tree. A plugin there is first-party, and the shell grants a first-party plugin every capability its manifest declares; as a user plugin it gets none (a `clonedFrom` replacement inherits only what the stock part it replaces has). So `omarchy-flavor user-sync` installs them for each user, as `omarchy plugin add` would. Nothing is installed without the user's yes: at login, plugins they haven't decided on open a terminal with Omarchy's own plugin warning and a Yes/No question (`user-sync --review`), and a no is remembered. Then it checks the URL with `omarchy-git-url-check`, clones the repo, checks out exactly the pinned commit, checks the manifest id, runs `omarchy-plugin-validate`, moves it into `~/.config/omarchy/plugins`, and enables it with the placement on first install. `flavor.lua` runs it at login. Its state file (`~/.local/state/omarchy-flavor/plugins.json`) means a plugin you remove stays removed, one you installed yourself is left alone, and a new pin updates only what it installed.

## Defaults

```toml
[defaults]
theme = "Vantablack"
```

Settings that live in each user's own state, applied by `user-sync` once, at the user's first login on the flavor, and again only if the flavor changes the value. A theme the user picks afterwards stays.

```toml
[defaults.bar]
remove = ["omarchy.menu"]                                    # omarchy plugin disable
move = [{ id = "omarchy.clock", section = "left", index = 0 }]   # omarchy bar move
set = [{ id = "omarchy.clock", key = "format", value = "HH:mm" }] # omarchy bar set
```

The bar layout works the same way, once, through Omarchy's own commands; a layout the user makes afterwards stays.

## Examples

| Flavor | Shows |
| --- | --- |
| [`examples/zen`](examples/zen/recipe.toml) | Settings, plugin removal, many binding changes, raw Lua, two marketplace plugins |
| [`examples/mac-switcher`](examples/mac-switcher/recipe.toml) | Four marketplace plugins wired to keys, one of them replacing the lock screen |
| [`examples/command-palette`](examples/command-palette/recipe.toml) | Replacing the Omarchy menu with a marketplace plugin, no Omarchy file changed |
| [`examples/jobo`](examples/jobo/recipe.toml) | Carrying open pull requests as diffs, a version per Omarchy line |

## How it composes

- The source tree is copied with real files (`cp -rL --reflink=auto`): the package's `bin/` is symlinks into `/usr/bin`, and Omarchy's privileged commands refuse to run unless they resolve to `$OMARCHY_PATH/bin/<name>`.
- Hyprland transforms become one generated module, `default/hypr/flavor.lua`, loaded from `default/hypr/omarchy.lua` after Omarchy's defaults and before `~/.config/hypr` overrides. It calls the same APIs Omarchy does (`hl.config`, `o.rebind`, `hl.unbind`).

## Possible future direction

[Flavors as a reproducible environment layer](docs/future-machine-setups.md) sketches how a machine provisioner could use a pinned flavor to recreate a setup. This is exploratory, not implemented or committed scope.

## Tests

```bash
python3 -I -m unittest discover -s tests -t .
```

Python 3.11+ standard library only (`tomllib`); stock Omarchy ships everything it needs.
