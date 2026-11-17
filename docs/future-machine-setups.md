# Proposal: Flavors as a reproducible environment layer

**Status: exploratory. Not implemented or a commitment to expand scope.**

## Idea

A flavor already describes how to recreate a customized Omarchy environment on top of upstream. It could become the environment layer used by a future machine-rebuilding tool, without becoming a whole-machine manager itself.

The recipe remains the source of truth. The composed Omarchy tree is a generated artifact, not something to back up and replay over a newer release.

## Division of responsibility

| Responsibility | Owner |
| --- | --- |
| Install Omarchy, packages, services, and hardware configuration | Machine provisioner |
| Compose desktop behavior, bindings, shell changes, carried patches, and declared user plugins | Flavors |
| Preserve personal overrides and their history | Dots or another dotfile manager |
| Recover documents and application data | Backup tool |
| Recover credentials and secrets | Separate secure recovery process |

These are responsibility boundaries, not a requirement for five applications. A standalone Flavors workflow should remain useful without a machine provisioner.

A provisioner would reference a flavor repository at a specific revision and invoke Flavors. It would not duplicate the flavor's transforms or maintain a second copy of its patch list.

## Two rebuild modes

### Follow upstream

Recreate the intended environment on a current, compatible Omarchy release. Reapply the recipe, detect drift, and report changes that need attention.

This is Flavors' existing direction: preserve intent while upstream keeps moving. It is not a promise of identical output across releases.

### Replay pinned inputs

Recreate a previously resolved environment using recorded inputs. A possible lockfile would identify:

- the exact Omarchy source or package artifact;
- the flavor revision and hashes of its recipe and assets;
- declared user plugin commits;
- the Flavors version used to compose it;
- relevant package/runtime versions, either directly or by reference to the provisioner's lock.

Updating would deliberately resolve new inputs and produce a new lock. A lockfile is not an archive: referenced artifacts must remain available, and exact replay also depends on the provisioner reproducing the relevant runtime. This would not promise bit-for-bit reproducibility of an entire machine.

## Reconstruction flow

1. The provisioner installs the selected Omarchy release and required dependencies.
2. It retrieves the pinned flavor recipe and assets.
3. Flavors composes the tree and returns its report.
4. Declared user plugins are synchronized in the user's context, preserving the existing third-party trust boundary.
5. Personal overrides and machine-specific configuration are restored through their owning tools.
6. Data and secrets are recovered separately.
7. The requested flavor and resulting environment are verified before the rebuild is declared complete.

Ordering matters: composition must precede operations that depend on the flavored tree. Personal overrides retain their existing precedence over flavor defaults. A provisioner and Flavors must not both claim ownership of the same plugin installation or configuration path.

## Success and user choice

A failed required transform followed by fallback to stock is a recovery outcome, **not** successful reproduction of the requested environment. Optional stale transforms and drift must remain visible; the caller must decide whether the result meets its acceptance policy.

Fresh-machine seeding and existing-machine reconciliation are different operations. Today's user plugin sync deliberately respects removals and independently installed plugins. Rebuilding a machine must not silently turn that behavior into forced convergence or erase personal choices.

A future integration should therefore make these intentions explicit:

- **Seed:** establish the declared environment on a fresh installation.
- **Reconcile:** compare an existing environment with the declaration, report differences, and apply only changes allowed by an explicit ownership and consent policy.

Neither mode should prune unrelated packages, plugins, or personal configuration by default.

## Smallest useful future step

Before building a new machine manager, prove one integration in a disposable VM:

1. Start from a known Omarchy release.
2. Retrieve an exact flavor revision.
3. Compose it and seed its declared user plugins.
4. Verify the report, selected desktop behavior, and plugin revisions.
5. Repeat from a fresh VM with the same inputs.
6. Exercise a required-transform failure and verify that the provisioner distinguishes stock fallback from a completed rebuild.

The likely Flavors-side additions are a resolved-input record, documented caller-facing status semantics, and a repeatable verification workflow. Package provisioning, backups, secrets, and general machine-state discovery remain outside this proposal.

## Open decisions

- What belongs in a Flavors lockfile versus the provisioner's lock?
- Which tool retains the artifacts required for later replay?
- What report states should block a rebuild, beyond failed required transforms?
- How should seed/reconcile intent and ownership be represented without overriding user choices?

## Proposed positioning

**Flavors describes and maintains the customized Omarchy environment. A machine provisioner recreates the system around it.**
