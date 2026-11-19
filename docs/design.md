# Umami Flavors — Design

Draft, begun 2026-11-16. This is the design umami was built from, kept for its reasoning. Where it and the code differ (the YAML sketch, `umami apply`, stacking and the CI tier are not built), the [README](../README.md) describes what umami does today.

## Summary

A flavor is a recipe of changes that is re-applied automatically to whatever the newest Omarchy is. Users always run current upstream, with the flavor on top, and nobody forks, rebases or freezes a file.

Umami Flavors is an unofficial add-on, and its command is `umami`.

The key move is to express changes as **transforms**, not copies. An override copies an upstream file and freezes it, so upstream fixes to that file stop arriving. A transform is an instruction, for example "bind Super+Return to foot". It is re-run against each new upstream release, so every upstream change flows through and the modification rides on top.

The system has three parts:

1. **Recipe**: a declarative file listing the transforms.
2. **Compositor**: applies the recipe to the current upstream tree and produces a composed Omarchy tree.
3. **Delivery**: locally, a pacman hook recomposes on every Omarchy package update. For sharing, CI rebuilds the flavor on every upstream release and publishes it to a pacman repo.

When upstream changes something a transform depends on, that transform is reported as stale and skipped. The rest of the flavor still applies and the update still finishes.

## Problem

Every current way to customise Omarchy either freezes your changes against upstream or gets silently undone by it.

| Approach | What happens on update |
| --- | --- |
| Edit the packaged files in `/usr/share/omarchy` | Pacman overwrites them on the next upgrade, without warning |
| Edit a dev-linked git checkout | `omarchy update` runs `git pull --ff-only`, which fails once local commits or conflicting edits exist, and the update stops |
| Keep a fork and rebase | Works, but every upstream release means a manual rebase and conflict resolution |
| User overrides in `~/.config` | Updates proceed, but each overridden file is a frozen copy: upstream fixes to it never arrive |

The goal is a fifth option: always-latest Omarchy, with a modification layer that keeps tracking upstream, that never blocks an update, and that other people can install.

## Prior art

This pattern already works well elsewhere, which makes it a known shape rather than an experiment.

| Project | Base | How the layer is expressed | What we borrow |
| --- | --- | --- | --- |
| Universal Blue / Bazzite | Fedora Atomic images | A recipe applied on top of the upstream image, rebuilt automatically when upstream publishes | Automatic rebuild in CI, users get latest upstream plus the layer |
| Nix overlays | nixpkgs | Functions of the previous package set (`final: prev: …`) | A modification defined relative to upstream, not as a copy of it |
| Distro respins (Ubuntu flavours, Manjaro) | Ubuntu, Arch | A derived distribution with its own repo | A separate package repo users opt into |

These descriptions are from general knowledge, not from pages checked for this doc.

## Architecture

One recipe and one compositor feed two delivery tiers. The runtime tier changes what lives under `OMARCHY_PATH`. The package tier also reaches the files Omarchy installs at fixed system paths.

```
RUNTIME TIER · on the machine, at every omarchy update

  Stock Omarchy        ┐
  /usr/share/omarchy   │     Compositor           Composed tree          Your desktop
  updated by pacman    ├──▶  ALPM hook, as root ─▶ OMARCHY_PATH       ─▶  Hyprland, shell
                       │     checks each target    root-owned files       omarchy-* commands
  Flavor recipe        │          │                                            ▲
  layers, transforms   ┘          ▼                                            │
  + personal layer               Report                                        │ installs
                                 stale and drift                               │ the flavor
PACKAGE TIER · in CI, at every upstream release                                │
                                                                               │
  Omarchy release   ─▶  CI respin           ─▶  Flavor repo         ─▶  User machines
  omarchy-pkgs          transforms + tests      signed pacman repo      omarchy update
```

Every Omarchy update recomposes the flavor; nothing upstream is edited.

The stock package is never modified. Each update copies it, applies the recipe, and runs the desktop from the result. CI does the same for the files the runtime tier cannot reach.

### Runtime tier: the composed tree

Omarchy already resolves almost everything through `OMARCHY_PATH`: `bin/`, `default/`, `shell/`, `themes/`, `applications/`, `config/` and `migrations/`. `/etc/omarchy.conf` sets that path, and `omarchy dev link` uses it today to point the system at a git checkout. A flavor points it at a **composed tree** instead.

1. The stock `omarchy` package keeps updating normally through pacman. No git is involved.
2. An ALPM hook fires inside the same transaction whenever `omarchy` or `omarchy-dev` changes.
3. The hook runs the compositor as root: copy `/usr/share/omarchy` into a staging directory, apply the recipe, then atomically swap it into place (for example `/var/lib/umami/tree`).
4. Omarchy runs from the composed tree. `PATH` and the sudo `secure_path` drop-in point at its `bin/`, the same way dev-link sets them up.

The tree is root-owned and built by root, so this is safer than dev-link, which runs user-writable code as root.

### Package tier: the CI respin

Some Omarchy files never pass through `OMARCHY_PATH`: `/etc` drop-ins, systemd units, ALPM hooks, `/etc/skel`, and everything `omarchy-settings` installs. For transforms that target those, CI builds derived packages:

- CI watches the `omarchy-pkgs` releases.
- On each release, it applies the recipe's system transforms to the upstream package sources and builds `<flavor>-system`, which depends on the matching `omarchy` version.
- The flavor's runtime part ships in the same repo as `<flavor>-runtime`: the recipe, the compositor and the ALPM hook.
- Omarchy's own `test/` suite runs against the composed result before anything is published.

Local-only flavors can skip the package tier entirely, and use systemd and `/etc` drop-ins for the few system-level changes they need.

## Recipe format and transforms

A recipe is a list of transforms, and each transform targets something by meaning and checks that its target still exists before it acts.

```yaml
flavor: mine
base: omarchy >= 4.0
layers: [umami-core]        # flavors can stack

transforms:
  - id: gaps
    lua.set: { module: default.hypr.looknfeel, key: general.gaps_out, value: 4 }
  - id: terminal-key
    bind.replace: { keys: SUPER+RETURN, run: foot }
  - id: thpm-on-theme
    command.wrap: { cmd: omarchy-theme-set, after: "thpm sync" }
  - id: packages
    packages.add: [foot, helix]
    packages.remove: [alacritty]
  - id: shell
    shell.plugins:
      add: [io.github.oldjobobo.thpm]
      remove: [omarchy.weather, omarchy.dock]
      replace: { omarchy.bar: ./shell/my-bar }
```

| Transform | Acts on | Checks before acting | Tier |
| --- | --- | --- | --- |
| `file.add` / `file.remove` | Any file in the tree | Path exists (for remove) | Runtime |
| `lua.set` / `lua.remove` | A Hyprland setting in a `default.hypr.*` module | Module and key exist | Runtime |
| `bind.add` / `bind.replace` / `bind.remove` | A keybinding | The binding exists (replace, remove) | Runtime |
| `command.wrap` | An `omarchy-*` command: run before or after it, or rewrite its arguments, then call the original | Command exists | Runtime |
| `command.replace` | An `omarchy-*` command, fully replaced | Command exists, and its upstream hash matches the one recorded | Runtime |
| `json.merge` / `toml.merge` | Structured config and theme files | File parses | Runtime |
| `template.add` | A theme template in `default/themed/` | None | Runtime |
| `packages.add` / `packages.remove` | Omarchy's install package lists and migrations | The list exists | Runtime |
| `shell.plugins.add` / `.remove` | Plugins in the composed `shell/plugins/` | The manifest validates; nothing else depends on a removed plugin's IPC | Runtime |
| `shell.plugins.replace` | A built-in plugin's slot (bar, menu, OSD, lock, dock, …), filled by the flavor's own QML component | The replacement implements the IPC functions the original exposed | Runtime |
| `shell` | The whole `shell/` directory | The replacement implements the shell IPC that Omarchy calls | Runtime |
| `migration.add` | A flavor migration, run once per user after the stock migrations | None | Runtime |
| `systemd.dropin` / `etc.dropin` | A unit or `/etc` file Omarchy installs | Target exists | Package |
| `patch` | Anything else (shell scripts, the rare QML line) | The anchor lines match exactly once | Either |

`patch` is the escape hatch. It works everywhere, but it is the transform most likely to go stale, so a good recipe uses it last.

`command.wrap` is preferred over `command.replace` because the wrapper keeps calling the current upstream command. A replaced command freezes, so the compositor records its upstream hash and reports any upstream change to it as drift.

### The shell

A flavor changes the shell by swapping parts, never by patching QML or editing its configuration. Omarchy launches it with `quickshell -p "$OMARCHY_PATH/shell"` and discovers built-in plugins in `$OMARCHY_PATH/shell/plugins`, and the composed tree owns both directories.

| Level | Recipe | Result |
| --- | --- | --- |
| Plugin set | `shell.plugins.add` / `.remove` | Plugins added or built-ins left out entirely, not merely disabled |
| Component swap | `shell.plugins.replace` | The flavor's own QML component fills a built-in's slot. All 26 built-ins, the bar included, are plugins with manifests |
| Wholesale | `shell` | The flavor provides the whole shell |

Everything a flavor does not swap keeps receiving upstream updates.

- **IPC contract.** The rest of Omarchy drives the shell over IPC: 95 `omarchy-shell` calls in `bin/` alone, plus keybindings and migrations. The compositor lists the IPC functions the upstream part exposes and reports any that the replacement lacks. This is the shell's form of drift detection.
- **Trust.** Built-in plugins receive Omarchy's trusted host objects, while third-party plugins get capability-scoped facades. Plugins a flavor places in `shell/plugins` run as first-party code, fully trusted. Installing a flavor already means trusting its root-run compositor, so this adds no new trust, but flavor authors and users should know it.

## Trust: declarative first

A flavor is declarative unless its recipe says `code = true` in `[flavor]`. A declarative flavor is data the compositor checks and turns into configuration itself, the way Omarchy treats a theme. Nothing in it runs as written. `umami plan` prints which tier a recipe is in, and why.

`plugin.hypr` is declarative too. It loads a Hyprland Lua file that one of the recipe's own plugins ships, from that user's install, and only if the user accepted the plugin. The code is the plugin's, it runs as the user inside Hyprland, never as root, and the flavor adds none of its own. Fathom's Alt+Tab is the first use, which is why the Mac flavor no longer needs `code = true`.

| Tier | Transforms | Trusted like |
| --- | --- | --- |
| Declarative | `lua.set` on look-and-layout sections (general, decoration, animations, dwindle, master, scrolling, group, misc) with plain values · `bind.remove` · `bind.add`/`bind.replace` to a checked action (focus, swap, layout, an `omarchy-*` command with plain arguments, a menu, a panel) · removing bar widgets, panels, overlays and menus · `[defaults]` · `[[plugin]]` | A theme |
| Code | `diff`, `patch`, `file.add`/`file.remove`, `command.wrap`/`command.replace`, raw `lua`, binds to shell commands or Lua, adding or replacing shell plugins, replacing the shell, removing a service such as the lock screen | A package |

The line sits there because the composed tree also runs through sudo: a code transform is root code. A declarative flavor cannot add a command, a file or a service, and cannot take away the lock screen, polkit or idle.

### The user layer

Third-party shell plugins and first-login defaults are per user, so they never enter the root-owned tree. `umami user-sync` runs at Hyprland start, as the user.

- **Plugins need consent.** An undecided plugin opens a floating terminal with Omarchy's own plugin warning and a `gum confirm` that defaults to No. A No is remembered. Before cloning, `omarchy-git-url-check` vets the URL; if the checker is missing, the plugin is refused. Repos are pinned to a commit.
- **Defaults apply once.** `[defaults]` sets the theme and the bar layout (remove, move and set widgets through `omarchy-plugin-disable` and `omarchy-bar`). Each value is applied once per user, recorded in `~/.local/state/umami/defaults.json`, so later changes the user makes stick. The bar waits until every plugin is decided.

### Defaults and locks

A recipe says how strongly each thing it sets should hold. Most flavors only need defaults: the flavor's starting point, which the user can change and keep, as Omarchy's own rule is that your config beats its defaults. Some need locks: a company flavor where certain things are simply not available.

|  | Default | Locked |
| --- | --- | --- |
| Meaning | Where the flavor starts; the user's change stays | The flavor's rule; the user can't turn it back on through Omarchy |
| Widgets and panels | `[defaults.bar] remove` hides them once with `omarchy plugin disable`; the user can enable them again | `shell.plugins.remove` takes them out of the composed tree, so there is nothing to enable |
| Theme and bar | `[defaults]`, applied once at first login | `[locked]`, applied again at every login |
| Hyprland settings and keys | Loaded before `~/.config/hypr`, so the user's config wins | `locked = true` on the transform: loaded after the user's files, so the flavor wins |

A default is re-applied only where the flavor changes it. The bar is tracked entry by entry, so a new Zen release that moves one widget moves only that widget, and leaves the rest of the user's layout alone.

Locked transforms go in `default/hypr/flavor-locked.lua`, which the composed `default/hypr/toggles.lua` loads at its end. Omarchy's `hyprland.lua` requires `toggles` after the user's `monitors`, `input`, `bindings`, `looknfeel` and `autostart`, on 4.5 and edge alike, so a locked setting or key wins over all of them. Lines a user adds below `toggles` in their own `hyprland.lua` still run after it.

Locked is not a security boundary. It makes things unavailable through Omarchy's settings, bar, menus and commands; anyone with sudo can switch the flavor off. A real lockdown is users without admin rights, which is how companies run machines already; then the root-owned tree and recipe are out of their reach. `umami status` names each locked item, so a locked feature reads as a decision, not a fault.

### Examples

| Flavor | Tier | What it shows |
| --- | --- | --- |
| zen | Declarative | Vantablack, no animations, blur or shadows, vim focus keys, which-key, and a minimal bar: time then date on the left, workspaces centred, then indicators, tray and Pocket on the right, with the rest behind Pocket and the update notice kept out |
| command-palette | Declarative | A Raycast-style palette (Keystroke) that takes over the Omarchy menu everywhere, plus Key Promoter; plugins only, no transforms |
| mac-switcher | Declarative | A dock, Spotlight on Super+Space (the Omarchy menu moves to Super+Alt+Space), Fathom's Alt+Tab loaded through plugin.hypr, Lock Explorer, the clock on the right of the bar, rounded windows with shadow and blur, and the White theme |
| shibumi | Declarative | HANCORE's Shibumi Shell through `[[package]]` and `[[suite]]`, with the Solitude theme and a matching window look |
| company | Declarative | Locks: a theme put back at every login, keys and a setting users can't override, a widget that isn't there |

## Update flow and failure handling

The flavor recomposes inside the package transaction, so the stock migrations already run against the composed tree, and a stale transform never stops the update.

Where the flavor steps into `omarchy update`:

1. `omarchy-update-dev`: skipped in flavor mode (upstream change 1).
2. `omarchy-update-system-pkgs` upgrades `omarchy` through pacman.
3. **Flavor ALPM hook** (post-transaction): recompose, write a report, swap the tree.
4. `omarchy-migrate` runs the stock migrations from the composed tree.
5. **Flavor migrations** run, in the order the recipe lists them.
6. `omarchy-hook post-update`: the flavor's hook shows the report if anything went stale.
7. Shell restart, boot verification and the reboot prompt continue unchanged.

The existing `post-update` hook runs after migrations, so it is too late to recompose. The ALPM hook is the only point that runs before them.

| Situation | Behaviour |
| --- | --- |
| Transform target still exists | Applied |
| Target gone or anchor not found | Transform marked **stale** and skipped. The rest of the flavor applies and the update finishes |
| Fully replaced command, file or shell part changed upstream | Applied, and reported as **drift** with the upstream diff or the missing IPC functions |
| A transform marked `required: true` goes stale, or the compositor crashes | The running session keeps the previous composed tree until reboot. Its files are copies, so the upgrade leaves them intact. `/etc/omarchy.conf` is pointed at stock `/usr/share/omarchy`, the update ends with "reboot required", and the flavor stays off until the recipe is fixed |

The report lives at `/var/lib/umami/report.json`, and `umami status` prints it, for example: `stale: terminal-key (SUPER+RETURN no longer bound in 4.1)`.

## Distribution and stacking

Installing a flavor means adding one pacman repo and one package. From then on, `omarchy update` delivers both upstream Omarchy and the flavor.

```bash
# once
umami add https://flavors.example/mine   # adds the repo + signing key
sudo pacman -S mine-flavor                # runtime (+ system, if the flavor has one)
# from then on
omarchy update
```

- **Versioning.** A flavor release names the Omarchy range it was tested against. Out of range, the flavor still applies, and the report notes that it is untested on this version.
- **Stacking.** A recipe can list `layers`: other flavors applied first, in order. A shared base flavor (for example, a community core), then a gaming flavor, then personal tweaks. Later layers win when two transforms touch the same target, and the report names every such conflict.
- **Personal layer.** The last layer can be a plain local recipe, with no repo at all. You edit `~/.config/omarchy/flavor.yaml`, and `umami apply` copies it to root-owned `/etc/umami/` after a sudo prompt, because the compositor never reads a home directory. A flavor is machine-wide; per-user taste stays in ordinary `~/.config` overrides.
- **Uninstall.** Removing the package deletes the hook and the composed tree, and points `/etc/omarchy.conf` back at stock Omarchy. User files the flavor's migrations changed stay as they are, the same way stock migrations behave.

**Today's package.** Before any repo or CI, the flavor ships as one pacman package, `umami`, built from the checkout by `packaging/build`. It holds the compositor in `/usr/lib/umami`, the ALPM hook in `/usr/share/libalpm/hooks`, and the shareable flavors in `/usr/share/umami/flavors`. `sudo umami flavor zen` (or a directory with a `recipe.toml`) copies the recipe to root-owned `/etc/umami`, composes, and enables it; it refuses while Omarchy is dev-linked elsewhere. The hook does nothing until a flavor is picked, and removing the package points Omarchy back at stock first.

## Findings from the Omarchy code

Most of Omarchy already follows `OMARCHY_PATH`, so the runtime tier needs no new mechanism, only care in a few places. Checked against an `omacom/omarchy` checkout and the installed `omarchy-dev 4.0.0.r7852.g663aff7`.

**Works as is**

- Hyprland loads its defaults through `$OMARCHY_PATH/default/hypr/bootstrap.lua`, and the shell, menus, themes and migrations all resolve through `OMARCHY_PATH`.
- In dev mode, `env-bootstrap` puts `$OMARCHY_PATH/bin` at the front of `PATH`, and `omarchy dev link` writes a sudoers `secure_path` drop-in. A flavor reuses both.
- `omarchy-update-available` skips its git check when the tree has no upstream branch, and still reports package updates.
- Omarchy invokes its own commands by name almost everywhere, including about 140 calls in the Hyprland defaults and 135 in shell QML. Commands in the composed `bin/` therefore take effect everywhere `PATH` is used.

**Constraints the design must respect**

| Constraint | Source | Consequence |
| --- | --- | --- |
| Privileged commands resolve symlinks and require the result to be `$OMARCHY_PATH/bin/<name>` | `omarchy_security_require_source_root` in `bin/omarchy-security-functions` | The composed tree holds real files (copies or hardlinks), never symlinks into `/usr/share/omarchy` |
| Migrations run before the `post-update` hook | `bin/omarchy-update` | Recompose in an ALPM post-transaction hook |
| `OMARCHY_PATH` is read at session start | `default/bash/env-bootstrap` | Switching a flavor on or off takes effect after a reboot, as dev-link does today |
| A few paths are pinned to stock on purpose | `omarchy-apply-lock`, `omarchy-provision-hardware`, factory reset, `omarchy-upgrade-to-quattro` | The lock screen, first-boot provisioning and recovery always run stock code. The design keeps it that way |

**Out of reach of the runtime tier**

- Six systemd units call `/usr/bin/omarchy-*` by absolute path (`crash-watch`, `hw-recover-internal-monitor`, `hw-wait-camera-access`, `migrate-notify`, `system-sleep-monitor`, `tailscale-receive`). These need `systemd.dropin`.
- `/etc` drop-ins, ALPM hooks, `/etc/skel` and everything from `omarchy-settings`. These belong to the package tier.

Eight shell actions call `omarchyPath + "/bin/..."` directly (clipboard, emoji insert, notifications, reminders, the theme switcher). They are not a problem here: they resolve through the composed tree, so they pick up the flavor's commands.

## Upstream asks

One small PR to `omacom/omarchy` is needed: an `OMARCHY_PATH_TYPE` line in `/etc/omarchy.conf`, beside the `OMARCHY_PATH` line it describes. Today Omarchy guesses the kind of path: `/usr/share/omarchy` is the package and anything else is a git checkout. A composed tree is neither.

```sh
export OMARCHY_PATH="/var/lib/umami/tree"
export OMARCHY_PATH_TYPE="flavor"   # package, dev or flavor
```

`omarchy dev link` would write `dev` and unlink would write `package`, so scripts check the type instead of comparing the path. Unset means today's guess, so existing installs and dev links keep working. The flavor's name stays out of Omarchy; umami's report records it.

| Script | Today, with a non-stock path | With `OMARCHY_PATH_TYPE=flavor` |
| --- | --- | --- |
| `bin/omarchy-update-dev` | Exits 1 if the tree is not a git checkout, which aborts `omarchy update` | Exits 0: the package upgrade already recomposed the tree |
| `bin/omarchy-channel-set` | Treats the switch as "leaving dev" and resets `/etc/omarchy.conf`, so the flavor is silently dropped | Keeps the path and its type across channel switches |
| `bin/omarchy-channel-current` | Reports `dev` | Reports the package channel |

Until that lands, the flavor works without it. The compositor applies seven **built-in patches** to its own copy of these scripts, so they treat the composed tree as a package install: `omarchy-channel-set` keeps the flavor across a channel switch (three patches: no "leaving dev" reset, no unlink back to stock, and the update after the switch runs from the tree), `omarchy-channel-current`, `omarchy-version` and `omarchy-version-branch` report stable or edge instead of dev, and `omarchy-update-dev` skips the tree. Each is an exact one-line match on stable 4.5.0 and edge. One that stops matching is reported by `plan` and never fails the composition, and the `OMARCHY_PATH_TYPE` PR would make all seven unnecessary.

It is easier to argue for the PR with a working prototype behind it, so the PR comes after the prototype.

## Prototype plan

The prototype proves the runtime tier locally, with no upstream change and no package repo. The package tier and CI come once that works.

- [x] **Test VM.** Built 2026-11-16: a frozen stock baseline, Omarchy 4.5.0-3 stable.
- [x] **Recipe and compositor.** Define the recipe schema and build the compositor for `file.add`, `lua.set`, `bind.replace`, `command.wrap` and `patch`, each with its target check and a JSON report.
- [x] **Dry run.** `umami plan` composes into a scratch directory and prints a diff against stock, without touching the live system.
- [x] **First real transform.** Start with the local agents tweak in the dev checkout (bin/omarchy-agent-usage-update and shell/plugins/agents/collectors.py). Then port other changes you actually run today, from `~/.config/hypr` or a post-update hook, into a recipe, and confirm the composed tree behaves the same. Done: the agents usage collector limit is an `agents-collector-limit` transform in a personal flavor.
- [x] **Go live.** Add the ALPM hook, the root-owned tree, and the git-without-upstream workaround, then point `/etc/omarchy.conf` at the tree, in a VM running stock Omarchy.
- [ ] **Survive an update.** Run a real `omarchy update`, confirm the flavor recomposes before migrations, and plant a stale transform on purpose to prove the update finishes. Done (2026-11-16): omarchy update in flavor mode, the hook firing inside a pacman transaction, stale and required-stale transforms, the fallback to stock and back, and uninstall. Done (2026-11-17): a real version change, switching from stable 4.5.0 to edge (omarchy-dev r7969). The hook recomposed Zen inside that transaction, and it was still running after the reboot. Still pending: a stable release bump.
- [ ] **Examples and the user layer.** Zen, Command palette and Mac switcher, plugin consent and first-login defaults, checked in the VM on 2026-11-16. All three re-checked from the umami package on 2026-11-17, in fresh VM runs: Zen, Mac switcher (Spotlight, Fathom's Alt+Tab, Lock Explorer) and Command palette (Super+Space opens Keystroke).
- [x] **Standalone package.** Built and tested in a fresh VM (2026-11-17): `pacman -U` the umami 0.1.0 package, `sudo umami flavor zen`, reboot. Omarchy ran from the composed tree, version and channel read stable, and consent, theme and bar applied at login.
- [x] **Defaults and locks.** Built and checked in the VM on 4.5.0 stable (2026-11-17). Company flavor: the locked theme came back at the next login after the user picked Nord, and a user's `rounding = 12` and Alt+Print binding lost to the locks, while their unlocked `gaps_out = 20` applied. Zen: the world clock, weather and agents widgets are hidden, not removed; turning the world clock back on worked and it stayed on through the next login. The bar default is tracked entry by entry. On a second machine since 2026-11-17, running Zen on edge.
- [x] **Move the author's main machine onto the flavor.** Once a real update survives in the VM: decide together, then run omarchy dev unlink, install the flavor, and reboot. The update's snapper snapshot is the rollback point. Omarchy PR testing moves to the VM or a temporary re-link. Done: it runs a personal flavor from the composed tree, with open Omarchy pull requests carried as `diff` transforms.
- [ ] **Upstream PR.** Propose `OMARCHY_PATH_TYPE`, with the prototype as evidence.
- [ ] **Package tier.** Deferred until someone else wants the flavor: the CI respin, the repo and stacking. Path when it comes: a pacman package on a GitHub release first, then a submission to the Omarchy package repo (OPR) if it catches on.

## Decisions

- **Dev-link clash.** The author's main machine was dev-linked to an Omarchy checkout for PR work, and a flavor needs that same `OMARCHY_PATH` slot. **Decided:** the prototype runs in a VM on stock Omarchy, which is also what real users run. A git checkout as the base can come later, as a developer feature.
- **Personal recipe trust.** The compositor runs as root, but a personal layer lives in a user's home directory. **Decided:** root reads only a root-owned copy in `/etc/umami/`, installed by `umami apply`, and a flavor is machine-wide (see Distribution and stacking).
- **Shell.** **Decided:** flavors change the shell by swapping plugins, components or the whole shell, checked against its IPC contract, not by patching QML (see The shell).
- **Fallback timing.** When a required transform goes stale, falling back to stock only takes effect after a reboot. **Decided:** the session keeps the previous composed tree until reboot, and the update ends with "reboot required".
- **Upstream.** **Decided:** build first. Upstream hears about it with the `OMARCHY_PATH_TYPE` PR, once the prototype works.
- **Audience.** **Decided:** a personal flavor first. The package tier (CI, signed repo, stacking) is deferred until someone else wants to use it.
- **Existing customisations.** **Decided:** current plugins, hooks and Hyprland overrides in `~/.config` stay where they are. The flavor holds only what changes Omarchy itself.
- **Name.** **Decided:** a recipe is a "flavor". The project is **Umami Flavors** and its command is `umami` (2026-11-17).
- **Recipe format and compositor.** **Decided:** recipes are TOML, the format Omarchy already uses, and the compositor is Python using only its standard library, which reads TOML natively. Stock Omarchy needs nothing extra. The YAML example above shows the idea; the README has the real syntax.
- **Security.** **Decided:** flavors are declarative by default, in line with Omarchy's no-code-in-themes stance. Code transforms need `code = true` and are trusted like a package. Third-party plugins stay out of the root tree and go through consent and `omarchy-git-url-check`.
- **Standalone.** **Decided:** the flavor deploys without the upstream PR, through built-in patches; the PR stays the clean fix.
