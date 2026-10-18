# Changelog

## 0.7.0 (2026-10-18)

### Added

- `release edit TAG --refresh-notes` replaces a page's notes with the ones pubanon would create
  now, from the changelog at that tag. Use it to add changelog sections to pages that already
  exist.
- Notes templates can use `{changelog_section}` to place the version's section. A template that
  uses it refuses the page when the section is missing, which stops `publish` before anything is
  pushed.
- Set `changelog_heading` under `[release_page]` when your changelog headings put other text before
  the version, such as `changelog_heading = "## Version {version}"`.
- `init` reports which versions it recognizes in your changelog and asks whether to copy their
  sections into release notes.

### Changed

- By default, release page notes contain the version's section of `CHANGELOG.md`, followed by a
  link to the whole file. When pubanon finds no section, the notes only link to the file and the
  preview says why. Set `changelog_section = false` under `[release_page]` to keep link-only notes.

## 0.6.0 (2026-10-16)

### Added

- Release pages can start as drafts that your release workflow publishes after it uploads the
  package, so the page and the package appear together. Answer `draft` when `init` asks about
  release pages, or set `draft = true` under `[release_page]`.
- Starter workflows `release-start.yml` and `release.yml` in `docs/examples/` upload a Python
  package to PyPI after CI passes on its tagged commit, then publish the version's draft page. See
  "Publish pages after the package upload" in the guide.

### Changed

- `repo reveal` also checks draft release pages for sensitive terms, because a release workflow can
  publish them later.

## 0.5.0 (2026-10-13)

### Breaking changes

- Upgrade GitHub CLI to 2.97.0 or newer. `repo reveal` and `audit --remote` need it to read Actions
  logs and stop with older versions.

### Added

- `repo reveal` offers to turn on GitHub's immutable releases when it first records a repository as
  public, and to make the existing release pages immutable.

### Changed

- When published history contains terms you added to `sensitive_terms` later, `repo reveal` lists
  them and asks whether to make them public instead of refusing. `publish` and `audit` tell you in
  advance.
- `publish` refuses to move or remove the tag of an immutable release page.

### Fixed

- `repo reveal` waits up to 3 minutes for GitHub to apply the visibility change instead of
  reporting drift.
- Commands retry a GitHub read up to twice after a server error (HTTP 500, 502, 503, or 504)
  instead of stopping. Requests that change GitHub are never retried.
- Git and GitHub CLI errors show multiline output on separate lines instead of joining it with
  `\x0a`.

### Security

- `repo reveal` checks workflow run details, job runners, and job logs for sensitive terms before
  making a repository public, and offers to delete matching logs.
- Run `audit --remote` on repositories revealed before 0.5.0. It checks their Actions history the
  same way and offers to delete matching logs; it stops while a workflow run is still in progress.

## 0.4.1 (2026-10-11)

### Changed

- In file selection for `init` and `prepare`, customize asks whether to include or exclude your
  pattern, and each choice reports how many files it decided.

### Fixed

- `publish` updates the final repository's description to match `[settings]`, including in
  repositories created outside pubanon, and shows the description in its preview. If you edited the
  description on GitHub, copy it into `[settings]` before publishing, or `publish` replaces it.
- File selection lists the already included files that an exclusion would also remove, and asks
  before excluding them.

## 0.4.0 (2026-10-07)

### Added

- On the staged route, `init` connects a CI workflow as `ci_workflow` when exactly one selected
  workflow qualifies, and lists only that workflow's jobs as checks. A workflow qualifies when it
  declares boolean `workflow_dispatch` inputs named `hosted` and `full_tests` and runs on every
  branch push.
- `init --edit` can connect, change, or disconnect the CI workflow in its **checks** section.
- A starter CI workflow, linked from the guide, that `init` can connect. It can run requested quick
  and full-test runs on your own runners.

### Changed

- `init` always offers **all listed** for required checks. It is the default when no check is
  unresolved and either a workflow is connected or no check is flagged.
- `init` turns on local source tags by default for new projects. Existing projects keep their
  setting.
- `repo create` verifies a matching empty repository that you created beforehand, without changing
  it, instead of refusing it.
- `stage push` refuses required checks that cannot pass: names that are not jobs in the connected
  workflow, or checks that no push can start because `CI_RUN_ON_PUSH` is `false` in the staging
  repository.

## 0.3.0 (2026-10-06)

### Added

- Optional release pages for tagged publications. `init` asks whether to create them; when they are
  on, `publish` and `run` show the page in the preview and create it after publishing. Otherwise, a
  tagged publication prints the `release create` command that adds one later.
- `--release-page`, `--no-release-page`, and `--release-notes-file` on `publish` and `run` choose
  for one publication without changing the project setting.
- An optional `[release_page]` table changes a page's title, notes, or changelog file. By default
  the title is the tag and the notes link to the published `CHANGELOG.md`.
- `pubanon init --edit` changes a project's visibility, route, source tags, release pages, required
  checks, or repository settings after setup.
- Optional `[commit_message]` tables in the global or project configuration set the messages for
  tagged and untagged publications. `prepare --message` still takes precedence.

### Changed

- `release create` uses your configured or default title and notes when you omit `--notes-file`,
  and reports an existing matching page instead of failing.
- With `ci_workflow` set, `repo reveal` requires that workflow's latest push run for the published
  commit to pass, as `repo watch` does.
- On Windows, human-readable output shows paths with forward slashes.

### Fixed

- `release create` and `release edit` no longer report an uncertain outcome when GitHub is slow to
  show the change.
- `release create` for an older version no longer makes it GitHub's latest release when a newer
  version already has a page.
- `clean` no longer deletes current publication records when the local published history is
  missing; it refuses and suggests `repo restore --to local`.
- After the final repository is deleted and recreated, `publish`, `status --remote`, and
  `repo verify` point you to `repo restore --to remote`.
- `run --verbose` shows the full publication preview, as `publish --verbose` does.

### Security

- `repo reveal` checks the tag, title, and notes of every non-draft release page against your
  current sensitive terms, and refuses to make the repository public while any page matches.

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
