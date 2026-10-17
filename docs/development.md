# Development

## Local environment

Provide Python 3.13 or newer, uv 0.12.23, Taplo 0.10.0, and shfmt 3.14.1 on `PATH`. These native
tools remain separate from the Python environment; use the same versions as CI when checking
publication readiness.

```sh
make setup
make check
```

Use `make install` to install the command-line tool with `uv tool`, or `make reinstall` to replace
an existing installation. `make format-check`, `make lock-check`, `make type-check`, and
`make whitespace-check` run the corresponding read-only checks; `make quality` combines them.

`make setup` creates `.venv` with the available `python`, syncs the exact packages and hashes in
`requirements-ci.txt`, and installs pubanon in editable mode using the locked build backend. To
choose an installed interpreter when creating the environment, use
`make setup PYTHON=/path/to/python`. Setup does not download Python or change global packages.
Existing environments are synced in place.

Run setup again after changing `pyproject.toml` or the dependency locks. Make targets refuse a
missing or outdated environment and use its Python directly, so activation is optional. Checks
never install or update packages; `make setup` does that, which keeps the environment aligned with
CI.

`make quality` runs formatting, lint, lock, three-platform type, and whitespace checks. CI uses
this same target. `make check` also runs tests; `make format` applies formatting changes. If
formatting changes `pyproject.toml`, run setup again before checking. Tests run offline, using
temporary Git repositories and a fake GitHub CLI.

## Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs `quality` first. The `test` jobs then run in
parallel. `package` starts only after those jobs pass; `hosted-validation` reports the outcome of a
hosted run, including failures.

CI uses GitHub-hosted runners unless a run explicitly requests local runners. In a private
repository, the Actions variables `CI_LINUX_RUNNER`, `CI_MACOS_RUNNER`, and `CI_WINDOWS_RUNNER`
select local runners for runs started from Actions > CI > Run workflow with the hosted option
disabled, including runs requested by `pubanon stage push`. If a variable is absent, that operating
system uses a hosted runner. Pushes, pull requests, public repositories, and runs with the hosted
option enabled use hosted runners for every job and run every test.

A staging repository sets the Actions variable `CI_RUN_ON_PUSH` to `false`. Its pushes then appear
as `CI / push skipped` and run no jobs; CI runs only on request. A newer requested run on a branch
cancels an older one. Push runs are never cancelled, because the Release workflow needs the tagged
commit's push run.

| Requested run                                                 | pubanon                           | Actions > CI > Run workflow | Title               |
| ------------------------------------------------------------- | --------------------------------- | --------------------------- | ------------------- |
| Quick: local runners, slow tests omitted on macOS and Windows | `pubanon stage push`              | hosted off, slow tests off  | `CI / local`        |
| Full: local runners, every test                               | `pubanon stage push --full-tests` | hosted off, slow tests on   | `CI / local (full)` |
| Hosted: GitHub-hosted runners, every test                     | `pubanon stage push --hosted`     | hosted on                   | `CI / hosted`       |

The `test` matrix covers Python 3.13 to 3.15 on Linux and Python 3.13 on macOS and Windows. All
test jobs have a one-hour hang guard, on local and hosted runners, in quick and full runs.

CI runs tests in parallel with pytest-xdist. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` when a runner's
visible CPU count exceeds its CPU quota. For the same scheduling locally, run:

```sh
make test TEST_FLAGS='-n auto --dist worksteal --durations=15'
```

The [Start release workflow](../.github/workflows/release-start.yml) runs after each successful
push run of CI on `main`. When that commit carries its version tag, it starts the
[Release workflow](../.github/workflows/release.yml) on the tag, which publishes CI's distributions
to PyPI and then the version's draft release page. To publish a tagged commit by hand, run the
Release workflow from the Actions tab on its tag. If a release run fails, rerun that run instead of
starting a new one.

## Dependency updates

`pyproject.toml` declares dependency ranges. `requirements-ci.txt` records the exact Python
dependency versions and hashes used by CI: runtime dependencies, development tools, and the build
backend, including editable-build support. `requirements-test.txt` selects the runtime, test, and
build dependencies from those same pins. Quality checks use the full development set; test jobs use
the smaller test set in a separate virtual environment. Local development and CI, in both the
staging and published repositories, use the same files. Published packages keep the ranges declared
in `pyproject.toml`.

After changing dependencies, regenerate both files with the uv version pinned in
[the CI workflow](../.github/workflows/ci.yml):

```sh
make lock-update
```

This retains existing compatible pins. To update to newer compatible versions deliberately:

```sh
make lock-update LOCK_FLAGS=--upgrade
```

Review both generated files, run `make setup` and `make check`, then commit the locks with the
dependency changes. `make lock-check` checks that they are current without rewriting them;
`make check` includes that check. Lock generation and checking resolve dependencies but do not
install them.

CI verifies hashes when installing the pinned tools and build backend. The package check installs
the built wheel in a clean environment, using the same version constraints for its runtime
dependencies. That environment contains only the package and its runtime dependencies.

The Markdown formatter uses Taplo for TOML code blocks and shfmt for shell code blocks. CI
downloads their official Linux binaries for the quality runner's architecture and verifies pinned
SHA-256 checksums. The versions and checksums live in the workflow. uv excludes `mdformat-config`'s
Taplo Python dependency because that distribution lacks Linux and Windows ARM wheels; the native
executable supplies the formatter instead. For local Markdown formatting, provide `taplo` and
`shfmt` on `PATH`. Test jobs do not need them.

## Test selection

The `slow` marker covers expensive test functions and all their parameter variations. A new
scenario is not automatically slow; classification comes from measured test cost. To run the
shorter local macOS/Windows selection, include `-m "not slow"` in `TEST_FLAGS`. Workers use the
available CPU count; a container's visible count can exceed its CPU quota. Use `-n 0` for a serial
debugging run. Plain `make test` remains serial. The hosted option runs every test regardless of
the slow-test checkbox. The release workflow's publishing job is named `Publish to PyPI`.
