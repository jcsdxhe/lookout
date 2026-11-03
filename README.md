# openRuyi Lookout

Read-only package tracking for [openRuyi](https://openruyi.cn): RPM source versions,
upstream releases, OBS builds and maintenance evidence.
Collectors save observations in SQLite snapshots.
Page and API requests read these snapshots. They do not query providers.

| Task | Guide |
|---|---|
| Install, configure ports, back up or upgrade | [Deployment](docs/deployment.md) |
| Add a package or correct its upstream identity | [Configuration](config/README.md) |
| Change code and run checks | [Contributing](CONTRIBUTING.md) |
| Add a monitor or provider backend | [Monitor porting](docs/monitor-porting.md) |
| Track architecture changes and compare source releases | [Upstream commit evidence](docs/upstream-commits.md) |
| Understand consistency and security boundaries | [Design](docs/design.md) |
| Report an accessibility barrier | [Accessibility](ACCESSIBILITY.md) |

Confined SPEC parsing requires a Linux container with native RPM bindings and Landlock ABI 6+. Keep configuration and persistent data on separate mounts.

The running site's `/api` page provides query examples. `/openapi.json` defines
parameters and responses. Failed checks retain previous evidence and its observation time.
Upstream security matches do not evaluate local patches or the exploitability of distributed RPMs.

[MulanPSL-2.0](LICENSE).
