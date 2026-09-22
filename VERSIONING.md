# Versioning

Baton follows [Semantic Versioning](https://semver.org/) since 1.0.0
(2026-08-30), the release that declared the package Production/Stable.

## What the version number covers

Baton is a CLI application, so its public API is everything a caller or a
profile can depend on, not its Python imports:

- **Command-line syntax**: command names, flags, and whether a flag takes a
  value.
- **Exit codes**: the shared contract in `src/baton/exits.py`.
- **`--json` payloads**: key names, shapes, and value types. Adding a key is
  compatible; removing, renaming, or retyping one is not. Human-readable
  output is not API, which is what `--json` is for.
- **Profile schema** (`baton.yaml`): new keys may arrive; existing keys keep
  their name, meaning, and optionality.
- **State files** under `state/` and the job records Baton reads back.
- **Database columns the packaged defaults map automatically**, because a
  mapped-but-missing column decides whether commands run at all.

## Choosing the level

| Level | When it applies |
|---|---|
| MAJOR | Something a correct caller or profile can do stops working: a flag removed or newly required to take a value, an exit code changing meaning, a JSON key removed or retyped, a config key renamed or newly required, a state format Baton can no longer read. |
| MINOR | New commands, flags, config keys, JSON keys, and optional behaviours; everything that existed keeps working as before. |
| PATCH | Bug fixes and documentation. No new command, flag, config key, or JSON key. |

A release that mixes these takes the highest level that applies.

## Two house rules on top of semver

- **A migration that must run before the new version works is breaking.**
  A database column the packaged defaults map qualifies when commands fail
  until the `ALTER` runs, which is a MAJOR under this rule. Additive schema
  a profile adopts at its own pace (a new table, an optional column nothing
  maps by default) stays a MINOR, with the steps written under
  **Upgrading** in the changelog.
- **Pin exact versions.** Deployments install `studio-baton[google]==x.y.z`
  and move deliberately after reading the changelog; version ranges are not
  supported and never were.

## History before this document

The 0.x series was initial development, where semver's `0.y.z` allowance
applies. Three post-1.0 releases predate this document and break it: 1.0.2
and 1.0.3 carried features and CLI/JSON contract changes in a patch, and
1.1.2 shipped a new config key in a patch. The numbers stay as released;
rewriting tags serves nobody. The rules above bind from the release after
1.9.0 (2026-09-22). Two earlier minors, 1.3.0 and 1.5.0, shipped
required-migration schema; the house rule above makes that pattern a MAJOR
from now on.

## Release mechanics

Tag `vx.y.z` and push. The Release workflow builds and publishes to PyPI,
and every tag carries a dated CHANGELOG entry and a GitHub release.

How a release reaches the studio's running gateway (which pins move where,
and the image build) is machine-local: `RUNTIME.md` beside this file in the
working tree, gitignored, because it names a private deployment. Public
documentation stops at this file.
