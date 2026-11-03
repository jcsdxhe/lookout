# Upstream commit evidence

Open a tracked package and select **Upstream commits**. Each entry identifies a
change, the rule that found it and the Git history used for each release decision.
**Changed in candidate** selects changes whose inclusion evidence or revert
indications differ between the packaged source and Lookout's selected upgrade.

| Result | Evidence |
|---|---|
| Original commit | The target history contains the original SHA. |
| Equivalent patch | A target commit has the same stable Git patch ID. Follow the witness to inspect its diff. |
| Backport to review | A target commit names the original in a cherry-pick or backport message. Its diff requires review. |
| No inclusion evidence | The examined target range contains no original, equivalent patch or recognized reference. |
| Unknown inclusion | The source identity, target ref, comparable patch or common search baseline is unavailable. The entry gives the reason. |

**Revert clue** links to an explicit revert message reachable from that target,
within the search range. A revert can itself be reverted. Assess the resulting
tree before making a backport or cleanup decision. RPM patch application, build
options and runtime behavior require separate evidence.

## Configure and deploy

1. Use the [standard Lookout installation](deployment.md). Git is included in the application image.
2. Keep **commits** in the active configuration's existing list of enabled monitors.
3. Review the package repository, branch and baseline. The shipped catalog monitors
   libffi and zlib-ng. Configure another package in the [operator overrides](../config/README.md).
4. Restart the service after selecting the updated configuration. Allow the
   collector write access to the state directory and HTTPS access to GitHub.
   The Git transport honors the operator's existing `TRACKER_MONITOR_PROXY` setting.

The following policy shows the shipped libffi rule.

```toml
[libffi.monitors.commits]
repository = "https://github.com/libffi/libffi"
branch = "master"
baseline = "3d0ce1e6fcf19f853894862abcbac0ae78a7be60"
tag_pattern = "v{version}"
keywords = ["risc-v", "riscv"]
regex = ['\brv(?:32|64)\w*']
paths = ["src/riscv/*"]
```

The baseline is an exclusive, immutable lower bound. Choose a common ancestor
of the monitored branch and intended releases. The collector reads the branch
DAG, including merged branches. Keywords and Python regular expressions match
commit messages case-insensitively. Shell globs match case-sensitive repository-relative
paths and may span directories. Merge paths use the first-parent diff.
Any matching rule selects the commit, and each matched rule supplies a reason.

The current source comes from Lookout's corroborated Source0 SHA or archive
tag. An operator can bind an exact version to a full SHA or a qualified release
tag in the same policy, for example `releases = { "1.2" = "refs/tags/release-1.2" }`.
Use `source_repository` for a reviewed downstream Git tree when its release
history lives in a different public GitHub repository. A conflicting Source0
repository yields an unknown result. A floating branch cannot identify a release.
A pinned SHA must be reachable from a branch or tag fetched from the source repository.

Lookout's existing comparison policy selects the candidate. The monitor resolves
the reviewed tag pattern only when that policy supplies a fresh upgrade.
The package and candidate keep their own resolved SHAs. A missing tag remains unknown.

## Query saved results

Open `/packages/libffi#commits` or select the monitor in the package list.
The detail filters keep both release rows for each selected commit. The five
inclusion filters refer to the current release. `changed` and `reverted` inspect both scopes.

```sh
curl -G http://127.0.0.1:8080/api/v2/packages/libffi \
  --data-urlencode include=commits --data-urlencode commit_state=changed
```

Omit `commit_state` to return all saved findings. The supported values are
`original`, `equivalent`, `backport`, `no_evidence`, `unknown`, `changed` and `reverted`.
Each finding has a stable scope/SHA ID. Machine-readable fact codes include
`upstream_commit`, `match_reasons`, `target_ref`, `target_commit`, `inclusion`, `inclusion_reason`, `witness`, `patch_id`, `revert` and `comparison`.
The package list also exposes these outcomes as maintenance facets.

## Refresh and recover

The monitor participates in the ordinary monitor heartbeat. Its first fetch stores
a bare object database and SQLite index under `monitor-cache/git-history/`
beside the snapshot. Allow disk space for upstream history. Git transfers missing
objects on subsequent polls. Collection reuses committed metadata batches and
stable patch IDs. The discovery checkpoint advances after a complete scan.
Changed rules or rewritten history trigger a scan from the reviewed baseline.
Every check resolves the targets again, including moved tags.

Each discovery increment and target search supports up to 20,000 commits.
A larger interval fails the check so the operator can review the baseline.
The snapshot exposes up to 500 matched changes in topological order and reports
the total. Original-commit membership is exact. Patch, backport and revert
searches cover target ancestors outside the baseline's history.

Failures use the runner's retry schedule. Open **Checks** for the failed scope,
attempt time and last successful observation. Retained evidence keeps its timestamp.
The cache can be rebuilt from the configured repositories. The application
snapshot remains the source for page and API reads.

From the source checkout, inspect a snapshot copy with the same configuration.
The single-package checker uses a temporary Git cache.

```sh
PYTHONPATH=backend python -m tracker.monitors explain libffi --monitor commits \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
PYTHONPATH=backend python -m tracker.monitors check libffi --monitor commits \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
```

## Reproduce the evidence offline

Use the [development environment](../CONTRIBUTING.md#run-checks). Run from the checkout root.

```sh
(cd backend && python -m tests.commit_history_sample)
(cd backend && python -m pytest -q tests/monitors/test_upstream_commits.py)
```

The sample creates a temporary Git repository with two releases and fixed commit
dates. It prints the same evidence contract the collector publishes.

| Sample change | Current 1.0 | Candidate 2.0 |
|---|---|---|
| Add RISC-V support | Original | Original |
| Use vector instructions | Equivalent patch with a revert clue | Original |
| RV64 ABI adjustment | Backport reference | Original |
| RISC-V scheduling | No inclusion evidence | Original |

The tests also exercise a changed rule, an interrupted scan, a rewritten branch,
a moved or deleted tag, failed refresh retention and recovery. The API and page
checks read a published SQLite snapshot with the Git transport disabled.

Package matching lives in `monitors/commits/rules.py`.
For a new inclusion method, extend `analysis.py` and add a Git graph that distinguishes it from existing methods.
Bump the adapter version when evidence interpretation changes. Bump `DISCOVERY_VERSION`
when new matching semantics require rescanning previously visited commits.

## Representative upstream checks

On 2026-11-03 both public Lookout package records reported the current release
as the latest release. The table records the collector output at the linked
branch heads, using the shipped baselines. The offline sample above exercises a candidate upgrade.

| Packaged Source0 | Monitored head | Matched | Original | No evidence in range | Unknown |
|---|---|---:|---:|---:|---:|
| [libffi 3.8.0](https://github.com/openRuyi-Project/openRuyi/blob/4da642a2893fdcd7f8161c114ec860cc95fc2990/SPECS/libffi/libffi.spec) | [master d02afc2](https://github.com/libffi/libffi/commit/d02afc25e8ea3aac9e78e227748e1fd7c9f14c82) | 16 | 11 | 3 | 2 |
| [zlib-ng 2.3.3](https://github.com/openRuyi-Project/openRuyi/blob/9363148d76bae519bd62ccc6b815e1a871b63067/SPECS/zlib-ng/zlib-ng.spec) | [develop 9d1d182](https://github.com/zlib-ng/zlib-ng/commit/9d1d1826c061da2c0dd6e4b5f402df942dabe738) | 52 | 12 | 40 | 0 |

The libffi void-return fix matched both its message and `src/riscv/ffi.c`.
The search found no inclusion evidence in v3.8.0. Its merge commit has no
single-parent patch ID and remains unknown. [Original change](https://github.com/libffi/libffi/commit/5fdd801e466a1ac28106e4ce9a3cb596c267d73d) · [Merge](https://github.com/libffi/libffi/commit/a8c5a70e18288a36a9119b74f59456fcf86c6bca)

The zlib-ng Chorba change matched a RISC-V source path. Git confirms the original
commit is reachable from 2.3.3. [Original change](https://github.com/zlib-ng/zlib-ng/commit/8003f57828f7310aaa035519bfa17c93b5621977) · [Release commit](https://github.com/zlib-ng/zlib-ng/commit/12731092979c6d07f42da27da673a9f6c7b13586)
