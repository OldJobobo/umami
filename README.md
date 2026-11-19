# Umami Flavors

An unofficial add-on for [Omarchy](https://omarchy.org). The Omarchy name is kept for official Omarchy features; if flavors are adopted upstream, they can take an Omarchy name then. The command is `umami`.

A flavor is a recipe of changes re-applied to whatever the newest Omarchy is. Changes are **transforms** (instructions that target something by meaning and check it still exists), not copies of upstream files, so every upstream update keeps flowing and the flavor rides on top.

Status: prototype. The dry run works, and a flavor runs live: verified in a VM on stock Omarchy 4.5.0 stable (desktop, shell, update, pacman hook, required-transform fallback, uninstall). Also verified from the package (`sudo umami flavor zen`), and across a real Omarchy version change: a switch from stable 4.5.0 to edge, with the pacman hook recomposing the flavor. Not yet tested across a stable release bump.

## Install on a machine (prototype)

```bash
./install.sh examples/zen       # compositor -> /usr/lib/umami, recipe -> /etc/umami,
                                # pacman hook, compose, point Omarchy at the tree; reboot after
./uninstall.sh                  # back to stock from the next boot
```

Refuses to install while Omarchy is dev-linked to a checkout (`omarchy dev unlink` first). Once installed:

```bash
/usr/lib/umami/bin/umami status               # what Omarchy runs from + last report
sudo /usr/lib/umami/bin/umami recompose       # after editing /etc/umami
sudo /usr/lib/umami/bin/umami enable|disable  # flavor or stock, from the next boot
```

What happens on an update:

1. `omarchy update` upgrades the Omarchy packages.
2. The pacman hook (`/etc/pacman.d/hooks/90-umami.hook`) recomposes stock + recipe into `/var/lib/umami/tree.new` as root, then swaps it into `tree`, inside the transaction, so migrations run from the composed tree. Each logged-in user then gets a desktop notice saying how it went: every change reapplied, the ones that no longer apply, or that the flavor failed.
3. A stale transform is reported in pacman's output and skipped. A required transform that can't apply leaves the running session alone and points `/etc/omarchy.conf` back at stock for the next boot.

The tree carries a `.git` with no upstream so `omarchy-update-dev` skips it instead of aborting the update, and is registered as a system git `safe.directory` because it is root-owned. Both go away once Omarchy has a flavor mode.

## Dry run

```bash
bin/umami plan examples/zen/recipe.toml            # compose into a scratch dir and say what the flavor does
bin/umami plan examples/zen/recipe.toml --files    # also list the files that differ from stock
bin/umami plan examples/zen/recipe.toml --diff     # and show the changes themselves
bin/umami compose examples/zen/recipe.toml --out DIR
```

`--source` picks the Omarchy tree to compose onto (default `/usr/share/omarchy`). Nothing outside the destination is written.

## Output

umami writes for people by default: plain sentences saying what the flavor is, its changes, what else it does, how it went, and the next command to run. Problems come with their reasons. `-v` shows each change's type and informational notes. Nothing is coloured except the `Error:` label of a failure on a terminal.

```
Flavor shibumi, declarative: nothing in it runs as written · onto omarchy-dev 4.0.0.r8031.ge6aec97-1
Changes: all 8 changes apply
  soft-corners       decoration.rounding = 6
  room-between       general.gaps_in = 6
  room-around        general.gaps_out = 12
  ...
  (2 notes on applied changes: --verbose shows them)
At each user's first login, theirs to change afterwards: theme Solitude
Installed with the flavor, each user asked before a plugin or suite: packages shibumi-shell · plugin suites shibumi (shibumi-shell's installer)
Files against stock: 1 added · 7 changed (--files lists them, --diff shows the changes)
Everything applies.
Install it with: sudo umami flavor examples/shibumi
```

Scripts and agents use `--json` on `plan`, `compose` and `status`: the complete report and nothing else, with each change's `status`, `what`, `detail` and `warnings`, the files against stock for `plan` (and the unified diff with `--diff`), and for `status` where Omarchy runs from now (`this_session`) and after the next boot (`next_boot`). The exit code is 2 when a required change fails, 1 when umami can't do what was asked (said on stderr as `Error: ...`), else 0. In pacman's output, umami writes one `umami:` line, plus one per problem.

## Report

Every change ends up as one of:

| Status | Meaning |
| --- | --- |
| `applied` | Applied |
| `drift` | Applied, but upstream changed what it replaces, or a replacement leaves IPC unanswered: check it |
| `stale` | Omarchy changed what it edits; skipped, the rest of the flavor still applies |
| `failed` | Recipe error (bad source, path outside the tree); skipped |

A change with `required = true` that ends stale or failed makes the whole composition `failed`. The report is written to `.flavor/report.json` in the composed tree, with a copy of the recipe.

### When a transform goes stale

An Omarchy update can change a file a transform edits, so the transform no longer fits. The update still goes through: umami skips that transform, applies the rest, and the pacman output and the desktop notice name it. Your desktop keeps working, without that one change.

1. See which transform it is and why:

   ```sh
   umami status
   ```

   Each stale transform is listed with the file and line that no longer matched.

2. Decide what it still needs:
   - **Upstream now has it** (the pull request a `diff` carried was merged): delete the transform from the recipe.
   - **You don't want it any more**: delete it from the recipe.
   - **You still want it**: bring it up to date with the new Omarchy. For a `diff`, rebase the branch it came from onto current Omarchy and write the diff out again. Keep the old one as a later entry in `sources` if the recipe also runs on an older Omarchy, since the first that applies wins. For a `patch`, update its anchor to the new text. For `bind.replace` or `bind.remove`, check what the key is bound to now.

3. Check the changed recipe; this changes nothing on the system:

   ```sh
   umami plan path/to/recipe.toml
   ```

   Every transform should be `ok`.

4. Apply it:

   ```sh
   sudo umami flavor path/to/recipe.toml   # or the flavor's name
   ```

A flavor in `~/.config/umami/flavors` is yours to edit. A packaged flavor is fixed by a new umami release; until then, the rest of it keeps working.

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
| `shell.plugins.remove` | `ids` | A lock: removes the manifest, so the plugin can't be turned back on (the code stays for anything importing it). To only switch a widget off, use `[defaults.bar] remove` |
| `shell.plugins.replace` | `plugin`, `source` | Reports IPC functions the original answered that the replacement doesn't |
| `shell` | `source` (dir with `shell.qml`) | Whole-shell replacement, same IPC check |

Relative `source` paths resolve against the recipe's directory and cannot leave it.

## Declarative first

A flavor is declarative unless its `[flavor]` table says `code = true`. Declarative transforms are data that umami checks and turns into configuration itself, the way Omarchy treats a theme; nothing in them runs as written:

- `lua.set` on how windows look and are laid out (`general`, `decoration`, `animations`, `dwindle`, `master`, `scrolling`, `group`, `misc`), with plain values
- `bind.remove`, and `bind.add`/`bind.replace` with an `action`: `focus` or `swap` (`l`, `r`, `u`, `d`), `layout` (a layout message), `omarchy` (an Omarchy command with plain arguments), `menu` or `panel`
- `shell.plugins.remove` of bar widgets, panels, overlays and menus (not services such as the lock screen)
- `plugin.hypr`: load a Hyprland Lua file that one of the recipe's own plugins ships (`plugin = "<id>"`, `file = "hypr/x.lua"`), from the user's install of it. It loads only if the user said yes to the plugin, runs as that user inside Hyprland, never as root, and the code is the plugin's, not the flavor's
- `[defaults]`, `[[plugin]]`, `[[package]]` and `[[suite]]`

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

Plugins from the marketplace never go into the composed tree. A plugin there is first-party, and the shell grants a first-party plugin every capability its manifest declares; as a user plugin it gets none (a `clonedFrom` replacement inherits only what the stock part it replaces has). So `umami user-sync` installs them for each user, as `omarchy plugin add` would. Nothing is installed without the user's yes: at login, plugins they haven't decided on open a terminal with Omarchy's own plugin warning and a Yes/No question (`user-sync --review`), and a no is remembered. Then it checks the URL with `omarchy-git-url-check`, clones the repo, checks out exactly the pinned commit, checks the manifest id, runs `omarchy-plugin-validate`, moves it into `~/.config/omarchy/plugins`, and enables it with the placement on first install. `flavor.lua` runs it at login. Its state file (`~/.local/state/umami/plugins.json`) means a plugin you remove stays removed, one you installed yourself is left alone, and a new pin updates only what it installed.

## Packages and plugin suites

```toml
[[package]]
name = "shibumi-shell"            # from a pacman repository this machine already uses

[[suite]]
id = "shibumi"
package = "shibumi-shell"         # one of this recipe's [[package]]s
command = "shibumi-shell"         # must belong to that package
install = ["install", "--yes"]
update = ["update", "--yes"]      # when the package version changes
uninstall = ["uninstall", "--yes"] # offered when a flavor without it takes over
```

Some shells and plugin sets ship as a pacman package with their own installer, which stages many plugins at once. `sudo umami flavor` installs a recipe's `[[package]]`s before it changes anything else, and only from a repository in `pacman.conf`, which pacman checks signatures for; a name that no repository has fails the switch and leaves the current flavor as it was. Never the AUR, a file or a URL. Packages stay when another flavor takes over, since other software may need them.

A `[[suite]]` is that package's installer, run by `umami user-sync` as each user. It is asked about together with the flavor's plugins, under Omarchy's plugin warning, and nothing runs before a yes. The command has to be owned by the named package (`pacman -Qo`), so the code that runs is the package's; the recipe only names its steps, as plain arguments. After a package update changes the version, the next login runs `update`. Switching to a flavor without the suite offers its `uninstall` once, like leftover plugins. Both tables are declarative.

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

The bar layout works the same way, through Omarchy's own commands; a layout the user makes afterwards stays. Each entry is tracked on its own, so when a new release of the flavor changes one entry, only that entry is applied again. A widget that a release of Omarchy doesn't have is skipped.

## Switching flavors

Your own flavors go in `~/.config/umami/flavors/<name>/recipe.toml` (with any files the recipe points at beside it), and `sudo umami flavor <name>` finds them by name, before the package's own flavors in `/usr/share/umami/flavors`. Edit one there, and `sudo umami flavor <name>` again picks up the change: the flavor that runs is the copy in `/etc/umami` made at the switch. A path to a recipe directory works too.

`sudo umami flavor <name>` from stock needs a reboot: it changes `OMARCHY_PATH`, which every part of the session reads once at login. From one flavor to another it doesn't: the path stays `/var/lib/umami/tree` and only what is in it changes. So when your session already runs a flavor, `umami flavor` swaps the tree and has your session run `umami refresh`: Hyprland reloads, the shell restarts, and the user layer runs as at login.

Plugins the old flavor installed that the new one doesn't use are offered for removal once, in the same terminal as the new flavor's plugins. Keep them and they become yours; plugins you installed yourself are never touched. A widget the flavor removed answers `omarchy plugin enable` with "this machine's flavor removes it" instead of "not known".

## Defaults and locks

Everything above is a default: where the flavor starts, which the user can change and keep. A flavor can also lock things, for a company that wants certain things simply not available:

```toml
[locked]                  # put back at every login
theme = "Tokyo Night"

[[transform]]             # loaded after the user's ~/.config/hypr files, so it wins
id = "no-screen-recording"
type = "bind.remove"
keys = "ALT + PRINT"
locked = true
```

| | Default | Locked |
| --- | --- | --- |
| Widgets and panels | `[defaults.bar] remove`: switched off once, can be turned back on | `shell.plugins.remove`: taken out of the tree, nothing to turn on |
| Theme and bar | `[defaults]`: once | `[locked]`: at every login |
| Settings and keys | before `~/.config/hypr`: the user wins | `locked = true` (on `lua.set`, `lua`, `bind.*`, `plugin.hypr`): after it, the flavor wins |

In between, `over_theme = true` (on `lua.set` and `lua`) makes a setting win over the theme while the user still wins over it. A flavor's settings normally load before the theme's, so a theme that sets its own border size or gaps replaces the flavor's; an over_theme setting goes in `default/hypr/flavor-over-theme.lua`, loaded right after the theme's settings and before the user's `~/.config/hypr` files. Zen uses it for its seamless borders, so they hold under any theme:

```toml
[[transform]]
id = "seamless-gaps-in"
type = "lua.set"
path = "general.gaps_in"
value = -1
over_theme = true
```

If Omarchy's `omarchy.lua` has no theme line to follow, the setting loads with the flavor's others and `umami plan` reports that a theme still wins.

Locked transforms go in `default/hypr/flavor-locked.lua`, loaded at the end of `default/hypr/toggles.lua`, which Omarchy's `hyprland.lua` requires after the user's own files. `umami plan` and `umami status` list what is locked. A lock holds against Omarchy's settings, bar, menus and commands; it is not a security boundary, since anyone with sudo can switch the flavor off. For that, give people accounts without admin rights.

## Examples

| Flavor | Shows |
| --- | --- |
| [`examples/zen`](examples/zen/recipe.toml) | Defaults only: settings, a quieter bar the user can change back, many binding changes, two marketplace plugins |
| [`examples/shibumi`](examples/shibumi/recipe.toml) | A whole shell from a package: HANCORE's Shibumi Shell through `[[package]]` and `[[suite]]`, with a matching window look and theme |
| [`examples/company`](examples/company/recipe.toml) | Locks: a theme put back at every login, keys and a setting the user can't override, a widget that isn't there |
| [`examples/mac-switcher`](examples/mac-switcher/recipe.toml) | Declarative: four marketplace plugins wired to keys (one loads its own Hyprland file through `plugin.hypr`, one replaces the lock screen), a Mac-style bar and look, a light theme |
| [`examples/command-palette`](examples/command-palette/recipe.toml) | Replacing the Omarchy menu with a marketplace plugin, no Omarchy file changed |

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
