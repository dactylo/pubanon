# Development

## Local environment

Provide Python 3.13 or newer, uv 0.12.13, Taplo 0.10.0, and shfmt 3.14.1 on `PATH`. These native
tools remain separate from the Python environment; use the same versions as CI when checking
publication readiness.

```sh
make setup
make check
```

`make setup` creates `.venv` with the available `python`, syncs the exact packages and hashes in
`requirements-ci.txt`, and installs Pubanon in editable mode using the locked build backend. To
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

The manual [Publish package workflow](../.github/workflows/release.yml) uploads this repository's
package to PyPI after checking its tag and CI result. Its publishing job is named
`Publish to PyPI`.

Quality checks run first, followed by the test matrix. Packaging starts only after every test job
passes. A failure prevents later stages from starting, on both hosted and local runners.

The repository's CI uses GitHub-hosted runners by default. For a private repository, Actions
variables `CI_LINUX_RUNNER`, `CI_MACOS_RUNNER`, and `CI_WINDOWS_RUNNER` can select local runners
for ordinary branch pushes. If a variable is absent, that operating system uses a hosted runner.
Public repositories, pull requests, and workflow dispatches with the hosted option enabled use
hosted runners for every job. The conditional `hosted-validation` job summarizes a complete hosted
run.

CI runs tests in parallel using pytest-xdist. Hosted runners and local Linux runners run the full
suite. By default, local macOS and Windows runners omit tests marked `slow` and have a 20-minute
job limit. These expensive integration scenarios remain part of every hosted validation run.

To run the full suite on local runners, open Actions > CI > Run workflow, disable the hosted
option, and enable **Include slow tests on local runners** (`full_tests`). This requires no runner
variable or required-check changes. The run appears as `CI / development (full)` and uses longer
timeouts for Windows and macOS. Enabling the hosted option always runs every test, regardless of
the slow-test checkbox.

To run the full suite with the same scheduling in a terminal:

```sh
make test TEST_FLAGS='-n auto --dist worksteal --durations=15'
```

For the shorter local macOS/Windows selection, include `-m "not slow"` in `TEST_FLAGS`. The marker
applies to expensive test functions, including all their parameter variations. Classification comes
from measured test cost; adding a scenario does not automatically make it slow.

Workers are selected from the available CPU count. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` when a
runner has a CPU quota that differs from its visible CPU count, such as a Docker container. Use
`-n 0` for a serial run when debugging. Plain `make test` also remains serial.

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
dependency changes. `make lock` checks that they are current without rewriting them; `make check`
includes that check. Lock generation and checking resolve dependencies but do not install them.

CI verifies hashes when installing the pinned tools and build backend. The package check installs
the built wheel in a clean environment, using the same version constraints for its runtime
dependencies. That environment contains only the package and its runtime dependencies.

The Markdown formatter uses Taplo for TOML code blocks and shfmt for shell code blocks. CI
downloads their official Linux binaries for the quality runner's architecture and verifies pinned
SHA-256 checksums. The versions and checksums live in the workflow. uv excludes `mdformat-config`'s
Taplo Python dependency because that distribution lacks Linux and Windows ARM wheels; the native
executable supplies the formatter instead. For local Markdown formatting, provide `taplo` and
`shfmt` on `PATH`. Test jobs do not need them.
