# Distribution

## Self-updating standalone executables built by the runner package

**Id:** 0437158b-02ed-4cc8-a08a-74d00a998ee4
**Type:** decision
**Type:** workaround
**Status:** active
**Evidence:** confirmed
**Source:** maintainer statement, 2026-10-22; commit a898137 (PR #4)
**Revisit when:** on-prem installs get a managed update channel, or pycrucible gains a supported way to fetch the latest version at launch

The `runner/` package is built with pycrucible into per-OS launcher executables (Linux, macOS, Windows) by the `build` workflow. Each launch resolves the latest Croupier from git, so branch computers update themselves without anyone touching them.

**Reason:** supporting on-prem installs by hand is a hassle, and a reliable way to update the software was needed. pycrucible is used with a deliberate hack to get this: `delete_after_run = true` and `extract_to_temp = true` in `runner/pyproject.toml` discard the extracted environment after each run, so the next start re-resolves the `croupier` git dependency and its latest dependencies.

**Consequence:** every start can pick up new code and new dependency versions; there is no pinning or rollback built in, and a broken commit on the tracked branch reaches branch computers on their next launch.

**Rejected alternative:** alternatives to this approach were not recorded; unknown.
