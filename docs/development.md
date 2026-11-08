# Development

Use this guide to set up a development checkout, run checks, and maintain dependencies and CI. For
installing the command, see the [README](../README.md#installation).

## Set up a development checkout

Provide Python 3.13 or newer, uv 0.13.6, Taplo 0.10.0, and shfmt 3.14.1 on `PATH`. The native tools
stay separate from the Python environment; their versions match CI.

```sh
make setup
make check
```

`make setup` creates `.venv` with the available `python`, syncs the exact packages and hashes in
`requirements-ci.txt`, and installs pubanon in editable mode using the locked build backend. Choose
another installed interpreter with `make setup PYTHON=/path/to/python`. Setup does not download
Python or change global packages; it syncs an existing environment in place.

Run setup again after changing `pyproject.toml` or the dependency locks. Checks refuse a missing or
outdated environment and never install packages. They use the environment's Python directly, so
activation is optional.

Use `make install` to install the command with `uv tool`, or `make reinstall` to replace an
existing installation.

## Run checks

| Target                  | What it does                                                     |
| ----------------------- | ---------------------------------------------------------------- |
| `make check`            | Runs quality checks and tests                                    |
| `make quality`          | Runs all five read-only checks below                             |
| `make format-check`     | Checks formatting and Ruff lint                                  |
| `make lock-check`       | Checks dependency locks without rewriting them                   |
| `make type-check`       | Runs Pyright for Linux, macOS, and Windows                       |
| `make whitespace-check` | Checks whitespace                                                |
| `make workflow-check`   | Runs zizmor offline on the GitHub Actions and starter workflows  |
| `make test`             | Runs tests serially unless `TEST_FLAGS` selects parallel workers |
| `make format`           | Applies formatting and lint fixes                                |

Inspect changes after `make format`. If it changes `pyproject.toml`, run `make setup` again before
checking. Tests run offline with temporary Git repositories and a fake GitHub CLI. CI uses the same
`make quality` target as local development.

## Select tests

Pass pytest options through `TEST_FLAGS`. For the parallel scheduling used in CI:

```sh
make test TEST_FLAGS='-n auto --dist worksteal --durations=15'
```

Use `-n 0` for a serial debugging run. Workers use the available CPU count; set
`PYTEST_XDIST_AUTO_NUM_WORKERS` when a runner or container sees more CPUs than its quota allows.

The `slow` marker covers expensive test functions and all their parameter variations. Classify a
new scenario by its measured cost. To use the shorter local macOS/Windows selection, include
`-m "not slow"` in `TEST_FLAGS`. Hosted CI runs every test regardless of the slow-test checkbox.

## Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs `quality` first, then the `test` jobs in
parallel. `package` starts after those jobs pass; `hosted-validation` reports the result of a
hosted run, including failures.

The test matrix covers Python 3.13 to 3.15 on Linux and Python 3.13 on macOS and Windows. All test
jobs have a one-hour hang guard, on local and hosted runners, in quick and full runs.

### Choose a run

CI uses GitHub-hosted runners unless a requested run in a private repository selects local runners.
Set `CI_LINUX_RUNNER`, `CI_MACOS_RUNNER`, and `CI_WINDOWS_RUNNER` in the repository's Actions
variables to select them. If a variable is absent, that operating system uses a hosted runner. All
other runs use hosted runners and run every test.

| Requested run                                              | pubanon                           | Actions > CI > Run workflow | Title               |
| ---------------------------------------------------------- | --------------------------------- | --------------------------- | ------------------- |
| Quick: local runners; omit slow tests on macOS and Windows | `pubanon stage push`              | hosted off, slow tests off  | `CI / local`        |
| Full: local runners; every test                            | `pubanon stage push --full-tests` | hosted off, slow tests on   | `CI / local (full)` |
| Hosted: GitHub-hosted runners; every test                  | `pubanon stage push --hosted`     | hosted on                   | `CI / hosted`       |

A staging repository sets `CI_RUN_ON_PUSH=false`. Its pushes appear as `CI / push skipped` and run
no jobs; request CI using the controls above. A newer requested run on a branch cancels an older
one. Push runs are never cancelled, because release automation needs the tagged commit's push run.

### Release automation

The [Start release workflow](../.github/workflows/release-start.yml) runs after each successful
push run of CI on `main`. When the commit carries its version tag, it starts the
[Release workflow](../.github/workflows/release.yml) on that tag. Release publishes CI's
distributions to PyPI, then publishes the version's draft release page.

To publish a tagged commit by hand, run Release from the Actions tab on its tag. If a release run
fails, rerun that run instead of starting a new one. Keep the publishing job's name,
`Publish to PyPI`: `release_checks` names it so `pubanon package watch` can find the release run.

## Update dependencies

`pyproject.toml` declares the dependency ranges used by published packages. The two lock files
serve development and CI in both staging and published repositories:

| File                    | Contents                                                                                                   | Used by                              |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| `requirements-ci.txt`   | Exact versions and hashes for runtime, development tools, and the build backend, including editable builds | Local development and quality checks |
| `requirements-test.txt` | Runtime, test, and build dependencies selected from the same pins                                          | Test jobs in a separate environment  |

After changing dependencies, regenerate both files with the uv version pinned in
[CI](../.github/workflows/ci.yml):

```sh
make lock-update
```

This retains compatible pins. To update to newer compatible versions deliberately:

```sh
make lock-update LOCK_FLAGS=--upgrade
```

Review both generated files, run `make setup` and `make check`, then commit the locks with the
dependency changes. `make lock-check`, also part of `make check`, checks that the locks are current
without rewriting them. Both lock commands resolve dependencies without installing them.

CI verifies hashes when installing the pinned tools and build backend. The package check installs
the built wheel in a clean environment containing only pubanon and its runtime dependencies,
constrained to the same versions.

### Markdown formatter tools

The Markdown formatter uses Taplo for TOML blocks and shfmt for shell blocks. Provide both on
`PATH` locally; test jobs do not need them. CI downloads the official Linux binaries for the
quality runner's architecture and verifies the SHA-256 checksums pinned alongside their versions in
the workflow.

uv excludes `mdformat-config`'s Taplo Python dependency because it lacks Linux and Windows ARM
wheels. The native executable supplies the formatter instead.
