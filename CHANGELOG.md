# Changelog

## 0.2.1 (2026-09-29)

### Added

- Optional `ci_workflow` project setting. When set, `stage push` requests a run of that workflow
  after pushing; see the guide for the workflow inputs it requires.
- `stage push --full-tests` and `--hosted` to request complete or hosted CI.
- With `ci_workflow`, `stage status`, `stage watch`, and `publish` judge only the latest requested
  run for the exact staged commit, which must pass the required jobs. `repo watch` applies the same
  rule to the final repository's CI.

### Changed

- `publish` and `repo restore --to remote` show previously published exposures grouped as in
  `audit`, with rule labels and highlighted matches.

## 0.2.0 (2026-09-25)

### Breaking changes

- Add a publication route, `staged` or `direct`, to your configuration: `default_publication_route`
  in the global configuration and `publication_route` in each project. Configurations without them
  no longer load.

- Staging repositories now use a `main` branch. If yours still has a `stage` branch, rename it to
  `main` on GitHub, make it the default branch, and run `pubanon stage repair`.

- Several commands moved under the thing they act on. The old spellings no longer work, including
  in saved `run` workflows:

  | Old                          | New                        |
  | ---------------------------- | -------------------------- |
  | `watch ci --staged`          | `stage watch`              |
  | `watch ci`                   | `repo watch`               |
  | `watch package`              | `package watch`            |
  | `stage clean`                | `stage delete`             |
  | `status --audit [--remote]`  | `audit [--remote]`         |
  | `inspect --clean`            | `clean`                    |
  | `delete prepared`            | `discard`                  |
  | `publish --sync`             | `repo restore --to remote` |
  | `repo verify --repair-store` | `repo restore --to local`  |

- `release VERSION` is removed. Use the publication steps for your route or a named `run` workflow.

- `repo create` uses your configured visibility instead of `--public` or `--private`.

- Add `pull_requests` and `discussions` to each project's `[settings]` table, with `true` or
  `false` values. Configurations without either setting no longer load.

### Added

- GitHub release pages: `release create`, `edit`, `list`, and `show`.
- File and directory exclusions that apply to every project.
- A review of included and excluded files during `prepare`.
- `stage repair` and `stage forget`, for fixing or dropping local tracking of a staging repository.
- `forget` removes a project's local Pubanon data after showing what it will remove.
- `pull_requests` and `discussions` settings control those GitHub repository features.
- Optional local source tags that mark published versions in your Git history.

### Changed

- Shorten terminal output, with diagnostic detail under `--verbose`. Declining a confirmation
  reports `Canceled.` with status 130.
- New projects default to private staging. Staging repositories can be reused across releases, and
  configured checks must pass before publication.
- Publication workflows take the version from the prepared snapshot.
- `prepare` and `audit` show blocked text with its file and line number.
- `discard` also removes the prepared snapshot's review copies, and `clean` removes all review
  copies along with superseded snapshots.
- `init` asks you to confirm CI checks that might not run for a publication.

### Fixed

- `setup` now saves the GitHub account you confirm.

## 0.1.0 (2026-09-20)

Initial release.
