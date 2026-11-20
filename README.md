# Umami Flavors

Make Omarchy your own without forking it. A flavor is a recipe of changes: settings, key bindings, bar layout, plugins, even patches to Omarchy's own files. umami applies it to stock Omarchy, and applies it again after every Omarchy update, so you keep getting upstream's fixes and features with your changes on top.

Each change targets something by meaning, such as a setting's path, a key chord or a line of code, and checks that it is still there. When an update moves something a change depends on, umami skips that one change, tells you, and applies the rest.

umami is an unofficial add-on for Omarchy, and a prototype. It has been verified in a VM on Omarchy 4.5.0 stable (install, desktop, shell, updates, falling back to stock, uninstall), from the package, and across a switch from stable 4.5.0 to edge. It has not yet been tested across a stable release bump.

## Install

umami isn't in a package repository yet. Build the package from this checkout and install it:

```sh
packaging/build
sudo pacman -U packaging/out/umami-*.pkg.tar.zst
```

Then pick a flavor:

```sh
sudo umami flavor zen
```

From stock Omarchy, reboot to start the flavor. From one flavor to another, the switch happens live: Hyprland reloads and the shell restarts.

To take a flavor off the machine, run `sudo umami remove` and reboot. It points Omarchy back at stock and removes what the flavor added:

- in your session, your theme and each bar entry go back to what they were before the flavor, unless you've changed them since, and it asks whether to remove the plugins it added.
- the packages it installed, after listing them and any dependencies that would go with them, unless something else needs them. Packages you had before are never touched, and a shell suite's package stays while a user still has the suite.

The flavor stays available, so `sudo umami flavor <name>` installs it again. Other users get their theme, bar and plugin question with `umami user-sync --remove` while logged in (`--dry-run` shows what it would do). If Omarchy is dev-linked to a checkout, that link stays. The flavor's files stay until no session runs from them. Flavors installed before umami recorded packages and the old theme and bar keep those as they are.

To only switch back to stock and keep everything, run `sudo umami disable` and reboot. Removing the package (`sudo pacman -R umami`) does that first; after the reboot you can delete `/etc/umami` and `/var/lib/umami`.

umami won't switch while Omarchy is dev-linked to a checkout. Run `omarchy dev unlink` first.

## Flavors that come with it

| Flavor | What it is |
| --- | --- |
| [`zen`](examples/zen/recipe.toml) | Quiet and keyboard-first: the Vantablack theme, no animations, blur or shadows, square gapless tiles that share their borders like tmux panes, vim keys, a which-key guide, and a calmer bar |
| [`shibumi`](examples/shibumi/recipe.toml) | HANCORE's Shibumi Shell, installed as a package, with the Solitude theme and softly rounded windows to match |
| [`mac-switcher`](examples/mac-switcher/recipe.toml) | For people coming from macOS: a dock, Spotlight on Super+Space, Alt+Tab, a menu bar with the clock on the right, and a light theme |
| [`command-palette`](examples/command-palette/recipe.toml) | The Omarchy menu replaced by Keystroke, a Raycast-style palette, without changing any Omarchy file |
| [`company`](examples/company/recipe.toml) | Omarchy as an employer might hand it out: a theme put back at every login, keys and a setting users can't override, a widget that isn't there |
| [`server`](examples/server/recipe.toml) | For a machine that runs services: boots to a console instead of the desktop, never sleeps, runs SSH (keys only), Tailscale and Docker, turns off Bluetooth and printing, and installs tools for disks, temperatures, backups and the network. Start the desktop when a screen is plugged in, and it's a quiet one |

Each recipe's comments say what it changes and which keys move.

## Everyday use

```sh
umami status                     # which flavor runs, and how its last composition went
sudo umami flavor <name>         # switch to a flavor, or reapply one you edited
umami plan path/to/recipe.toml   # what a recipe would do, without changing anything
sudo umami remove                # take the flavor off: back to stock, with its packages, plugins, theme and bar
sudo umami disable               # back to stock from the next boot, keeping everything
sudo umami enable                # the flavor again from the next boot
```

Your own flavors go in `~/.config/umami/flavors/<name>/recipe.toml`, with any files the recipe uses beside it. `sudo umami flavor <name>` looks there first, then in the flavors that come with umami (`/usr/share/umami/flavors`). A path to a recipe directory works too. The flavor that runs is the copy made in `/etc/umami` when you switched, so after editing a recipe, run `sudo umami flavor <name>` again.

When you switch from one flavor to another, its packages and your theme and bar stay; `umami remove` is what takes them away. Plugins the old flavor installed that the new one doesn't use are offered for removal once. Keep them and they become yours. Plugins you installed yourself are never touched.

## After an Omarchy update

You don't need to do anything. When `omarchy update` upgrades Omarchy, a pacman hook composes your flavor onto the new version as part of the same update. Each logged-in user then gets a desktop notice saying how it went.

Most of the time every change applies again. When one doesn't:

- A **stale** change is one whose target Omarchy changed. It is skipped, and the rest of the flavor applies. Your desktop keeps working, without that one change.
- A change marked `required = true` that can't apply stops the flavor. The running session is left alone, and from the next boot Omarchy runs stock until the recipe is fixed.

### Fixing a stale change

1. See which change it is and why:

   ```sh
   umami status
   ```

   Each stale change is listed with the file and line that no longer matched.

2. Decide what it still needs:
   - **Omarchy has it now**, for example the pull request a `diff` carried was merged: delete the transform from the recipe.
   - **You don't want it any more**: delete it from the recipe.
   - **You still want it**: bring it up to date with the new Omarchy. For a `diff`, rebase the branch it came from onto current Omarchy and write the diff out again. If the recipe also runs on an older Omarchy, keep the old diff as a later entry in `sources`, since the first one that applies wins. For a `patch`, update its anchor to the new text. For `bind.replace` or `bind.remove`, check what the key is bound to now.

3. Check the changed recipe. This changes nothing on the system:

   ```sh
   umami plan path/to/recipe.toml
   ```

   It should end with `Everything applies.`

4. Apply it:

   ```sh
   sudo umami flavor <name>
   ```

A flavor in `~/.config/umami/flavors` is yours to edit. A flavor that comes with umami is fixed by a new umami release, and until then the rest of it keeps working.

## Writing a flavor

A recipe is a TOML file. This one starts each user on Tokyo Night, tightens the gaps around windows and frees the weather key:

```toml
[flavor]
name = "mine"

[defaults]
theme = "Tokyo Night"

[[transform]]
id = "tight-gaps"
type = "lua.set"
path = "general.gaps_out"
value = 4

[[transform]]
id = "no-weather-key"
type = "bind.remove"
keys = "SUPER + CTRL + ALT + W"
```

Save it as `~/.config/umami/flavors/mine/recipe.toml`, check it with `umami plan ~/.config/umami/flavors/mine/recipe.toml`, and start it with `sudo umami flavor mine`.

### Declarative flavors, and flavors that run code

A flavor is declarative unless its `[flavor]` table says `code = true`. A declarative recipe is data: umami checks it and turns it into configuration itself, the way Omarchy treats a theme, and nothing in it runs as written. Declarative recipes can use:

- `lua.set` on how windows look and are laid out (`general`, `decoration`, `animations`, `dwindle`, `master`, `scrolling`, `group`, `misc`) and how the keyboard, touchpad and cursor behave (`input`, `cursor`), with plain values. Your own `~/.config/hypr/input.lua` loads after the flavor and wins
- `bind.remove`, and `bind.add` or `bind.replace` with an `action`: `focus` or `swap` (`l`, `r`, `u`, `d`), `layout` (a layout message), `omarchy` (an Omarchy command with plain arguments), `menu` or `panel`
- `shell.plugins.remove` of bar widgets, panels, overlays and menus (not services such as the lock screen)
- `plugin.hypr`, which loads a Hyprland Lua file that one of the recipe's own plugins ships (`plugin = "<id>"`, `file = "hypr/x.lua"`), from the user's install of it. It loads only if the user said yes to the plugin, and it runs as that user inside Hyprland, never as root. The code is the plugin's, not the flavor's.
- `[defaults]`, `[locked]`, `[system]`, `[[plugin]]`, `[[package]]` and `[[suite]]`

Everything else runs code someone wrote: diffs, patches, replaced or wrapped commands, added or removed files, raw Lua, a binding that runs a shell command or raw Lua, and removing a service. A recipe that uses any of these must say `code = true`, and `umami plan` says so on its first line. Omarchy also runs through sudo, so trust a flavor that runs code the way you'd trust a package: install it from a signed package, never from a bare URL.

### Defaults: where users start

```toml
[defaults]
theme = "Vantablack"

[defaults.bar]
remove = ["omarchy.menu"]                                         # omarchy plugin disable
add = [{ id = "omarchy.tailscale", section = "right" }]           # omarchy bar put
move = [{ id = "omarchy.clock", section = "left", index = 0 }]    # omarchy bar move
set = [{ id = "omarchy.clock", key = "format", value = "HH:mm" }] # omarchy bar set
```

Defaults live in each user's own settings. umami applies them once, at the user's first login on the flavor, through Omarchy's own commands, and again only if a new version of the flavor changes a value. A theme or bar layout the user picks afterwards stays. Each bar entry is tracked on its own, so a changed entry is applied again without the others, and a widget this Omarchy doesn't have is skipped.

### Locks: what users can't change

A flavor can also lock things, for a company that wants some features simply not there:

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
| Widgets and panels | `[defaults.bar] remove`: switched off once, and can be turned back on | `shell.plugins.remove`: taken out of Omarchy, with nothing to turn back on |
| Theme and bar | `[defaults]`: applied once | `[locked]`: put back at every login |
| Settings and keys | loaded before `~/.config/hypr`, so the user wins | `locked = true` on `lua.set`, `lua`, `bind.*` or `plugin.hypr`: loaded after it, so the flavor wins |

`umami plan` and `umami status` list what is locked, so a missing feature reads as a decision rather than a fault. A widget the flavor removed answers `omarchy plugin enable` with "this machine's flavor removes it". A lock holds against Omarchy's settings, bar, menus and commands. It is not a security boundary: anyone with sudo can switch the flavor off. For that, give people accounts without admin rights.

### Over the theme

A flavor's settings normally load before the theme's, so a theme that sets its own border size or gaps replaces the flavor's. With `over_theme = true` (on `lua.set` and `lua`), a setting loads right after the theme's and still before the user's `~/.config/hypr` files: it wins over the theme, and the user still wins over it. Zen uses it so its seamless borders hold under any theme:

```toml
[[transform]]
id = "seamless-gaps-in"
type = "lua.set"
path = "general.gaps_in"
value = -1
over_theme = true
```

If Omarchy's `omarchy.lua` has no theme line to follow, the setting loads with the flavor's others, and `umami plan` says that a theme still wins.

### Plugins

```toml
[[plugin]]
id = "evindor.keystroke"
repo = "https://github.com/evindor/keystroke"
commit = "0c74f2dd7466f25faf2cda4a39697f237afbe774"   # the full commit the marketplace validated
section = "left"                                     # optional bar placement
index = 0
```

Marketplace plugins are installed for each user, the way `omarchy plugin add` would install them, and never into the composed Omarchy. There they would count as first-party, and the shell grants a first-party plugin every capability its manifest asks for. As user plugins they get none.

Nothing is installed without the user's yes. At login, plugins the user hasn't decided on open a terminal with Omarchy's own plugin warning and a Yes/No question, and a no is remembered. After a yes, umami checks the URL with `omarchy-git-url-check`, clones the repository at exactly the pinned commit, checks the manifest's id, runs `omarchy-plugin-validate`, moves the plugin into `~/.config/omarchy/plugins`, and enables it with its placement. A plugin you remove stays removed, one you installed yourself is left alone, and a new pin updates only what umami installed.

### Packages and plugin suites

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

Some shells and plugin sets ship as a pacman package with their own installer. `sudo umami flavor` installs a recipe's packages before it changes anything else, and only from a repository in `pacman.conf`, whose signatures pacman checks. umami never installs from the AUR, a file or a URL. A package no repository has stops the switch and leaves the current flavor as it was. Packages stay when another flavor takes over, since other software may need them.

A `[[suite]]` runs that package's installer as each user. It is asked about together with the flavor's plugins, under Omarchy's plugin warning, and nothing runs before a yes. The command must belong to the named package (`pacman -Qo`), so the code that runs is the package's, and the recipe only names its steps. When a package update changes the version, the next login runs `update`. Switching to a flavor without the suite offers its `uninstall` once.

### The machine: `[system]`

```toml
[system]
boot = "console"                  # a text login; sudo systemctl start sddm starts the desktop
sleep = false                     # never suspend: not on idle, the lid or a key

[system.services]
enable = ["sshd", "tailscaled", "docker"]   # enabled and started
disable = ["bluetooth", "cups"]             # disabled and stopped
```

`sudo umami flavor` applies these after the flavor composes, and records what each was before. A service must already be installed, by Omarchy or one of the recipe's `[[package]]`s, or the switch stops before anything changes. `sleep = false` writes drop-ins to `/etc/systemd/logind.conf.d` and `/etc/systemd/sleep.conf.d`; the lid setting takes effect from the next boot.

Enabling `sshd` turns password logins off first, the way `omarchy-setup-security-sshd` does, and checks with `sshd -T` that sshd really applies it; if another config overrides it, sshd stays off. Port 22 opens in the firewall, rate limited. Authorize a key with `omarchy-setup-security-sshd`.

Switching to another flavor undoes what it doesn't also set, and `umami remove` undoes all of it: each service goes back to how it was, the boot target and sleep come back, and umami's sshd and firewall settings go. A service the flavor started is disabled but not stopped, so an SSH or Tailscale session running the removal isn't cut off; it stops at the next boot.

### Transforms

| Type | Keys | Notes |
| --- | --- | --- |
| `lua.set` | `path` (such as `general.gaps_out`), `value` | Written as `hl.config` in `default/hypr/flavor.lua` |
| `lua` | `code`, or `source` (a `.lua` file in the recipe) | Raw Lua for `flavor.lua`, for what no other transform covers, such as a plugin's own binding snippet. Guard anything that can fail |
| `bind.add` / `bind.replace` | `keys`, `description`, and `run` (a command string, or a table such as `{ launch = "foot" }`), `lua` (a raw dispatcher) or `action` | `bind.replace` needs the chord bound literally upstream |
| `bind.remove` | `keys` | |
| `file.add` | `path`, `source`, `mode`?, `upstream_sha256`? | When replacing an upstream file, record its hash to detect drift |
| `file.remove` | `path` | |
| `patch` | `file`, `anchor`, and one of `insert_before`, `insert_after` or `replace_with` | The anchor must occur exactly once |
| `diff` | `source`, or `sources` (tried in order; the first that applies wins), `strip`? | A `git diff`, applied all or nothing. For carrying an open pull request, with a version for each Omarchy line it lands on |
| `command.wrap` | `command`, `before`?, `after`?, `after_on_failure`? | The original moves to `bin/.flavor/`. Privileged commands can't be wrapped |
| `command.replace` | `command`, `source`, `upstream_sha256`? | |
| `shell.plugins.add` | `source` (a directory with `manifest.json`) | Installed under `shell/plugins/flavor/<id>/` and trusted as first-party |
| `shell.plugins.remove` | `ids` | A lock: the plugin can't be turned back on. To only switch a widget off, use `[defaults.bar] remove` |
| `shell.plugins.replace` | `plugin`, `source` | Reports IPC functions the original answered that the replacement doesn't |
| `shell` | `source` (a directory with `shell.qml`) | Replaces the whole shell, with the same IPC check |
| `plugin.hypr` | `plugin`, `file` | Loads a Hyprland file one of the recipe's plugins ships |

Every transform takes an `id`, and optionally `required = true` (the flavor fails without it), `locked = true` or `over_theme = true`. A relative `source` path is resolved from the recipe's directory and can't leave it.

Each transform ends up with one of four statuses:

| Status | Meaning |
| --- | --- |
| `applied` | Applied |
| `drift` | Applied, but Omarchy changed what it replaces, or a replacement leaves an IPC function unanswered. Check it |
| `stale` | Omarchy changed what it edits. Skipped, and the rest of the flavor still applies |
| `failed` | A mistake in the recipe, such as a missing source or a path outside Omarchy. Skipped |

## Output for scripts and agents

umami writes plain sentences for people, ending with the next command to run. `-v` adds each change's type and any notes.

```
Flavor shibumi, declarative: nothing in it runs as written · onto omarchy-dev 4.0.0.r8031.ge6aec97-1
Changes: all 8 changes apply
  soft-corners       decoration.rounding = 6
  room-between       general.gaps_in = 6
  room-around        general.gaps_out = 12
  ...
At each user's first login, theirs to change afterwards: theme Solitude
Installed with the flavor, each user asked before a plugin or suite: packages shibumi-shell · plugin suites shibumi (shibumi-shell's installer)
Files against stock: 1 added · 7 changed (--files lists them, --diff shows the changes)
Everything applies.
Install it with: sudo umami flavor examples/shibumi
```

Scripts and agents should use `--json` on `plan`, `compose` and `status`. It prints the complete report and nothing else: each change's `status`, `what`, `detail` and `warnings`; for `plan`, the files that differ from stock (and the unified diff with `--diff`); for `status`, where Omarchy runs from now (`this_session`) and after the next boot (`next_boot`).

The exit code is 2 when a required change fails, 1 when umami can't do what was asked (with `Error: ...` on stderr), and 0 otherwise. In pacman's output, umami writes one `umami:` line, plus one for each problem.

## How it works

- umami copies stock Omarchy from `/usr/share/omarchy` with real files (`cp -rL --reflink=auto`), applies the recipe, and swaps the result into `/var/lib/umami/tree`. It points `/etc/omarchy.conf` and sudo's `secure_path` at that tree, and marks it `OMARCHY_PATH_TYPE=flavor` so Omarchy treats it as a package install, not a dev checkout. The package's `bin/` is symlinks into `/usr/bin`, and Omarchy's privileged commands refuse to run unless they resolve to `$OMARCHY_PATH/bin/<name>`, hence the real copy.
- Hyprland changes become one generated module, `default/hypr/flavor.lua`, loaded from `default/hypr/omarchy.lua` after Omarchy's defaults and before the theme and `~/.config/hypr`. It calls the same APIs Omarchy does (`hl.config`, `o.rebind`, `hl.unbind`). `over_theme` settings go in `flavor-over-theme.lua`, right after the theme. Locked ones go in `flavor-locked.lua`, at the end of `toggles.lua`, which loads after the user's files.
- The pacman hook (`90-umami.hook`) recomposes into `tree.new` inside the update transaction and then swaps it in, so Omarchy's migrations run from the composed tree.
- `umami user-sync` runs as each user at login: defaults, locks, plugins and suites.
- Switching from stock changes `OMARCHY_PATH`, which the session reads once at login, so it needs a reboot. Between flavors the path stays the same and only the tree changes, so `umami flavor` swaps it and has your session run `umami refresh`: Hyprland reloads, the shell restarts, and the user layer runs as at login.
- The tree carries a `.git` with no upstream, so `omarchy-update-dev` skips it instead of stopping the update. It is registered as a system git `safe.directory` because it is root-owned. Both go away once Omarchy has a flavor mode.
- The last composition's report is `/var/lib/umami/report.json`, which `umami status` reads. The tree keeps its own copy in `.flavor/`, beside the recipe it was composed from.

## Development

Try a recipe from a checkout without installing anything:

```sh
bin/umami plan examples/zen/recipe.toml            # what the flavor does
bin/umami plan examples/zen/recipe.toml --files    # and the files that differ from stock
bin/umami plan examples/zen/recipe.toml --diff     # and the changes themselves
bin/umami compose examples/zen/recipe.toml --out DIR
```

`--source` picks the Omarchy tree to compose onto (default `/usr/share/omarchy`). Nothing outside the destination is written.

Run the tests:

```sh
python3 -I -m unittest discover -s tests -t .
```

umami needs Python 3.11 or later and only its standard library, which stock Omarchy already has.

`./install.sh <recipe-dir>` and `./uninstall.sh` are the older prototype installer, kept for VM testing without a package.

[The design](docs/design.md) explains why umami works the way it does: the problem, prior art, the architecture, the trust model and the decisions taken. [Flavors as a reproducible environment layer](docs/future-machine-setups.md) is an exploratory sketch of how a machine provisioner could use a pinned flavor to rebuild a setup. It is not implemented or planned.

## License

MIT
