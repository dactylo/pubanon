# Pubanon operator guide

For installation and your first publication, start with the
[README walkthrough](../README.md#your-first-release). Use this guide for later releases, project
settings, and optional workflows. Commands select the project registered to your current source
directory; use `--project NAME` when working elsewhere.

| If you want to...                            | Start here                                                    |
| -------------------------------------------- | ------------------------------------------------------------- |
| Publish committed changes                    | [Quick path](#quick-path)                                     |
| Review each publication step                 | [Step-by-step publication](#step-by-step-publication)         |
| Choose or update a version tag               | [Version tags](#version-tags)                                 |
| Set up another project                       | [Initialize a project](#initialize-a-project)                 |
| Change files, CI checks, or project settings | [Edit an existing project](#edit-an-existing-project)         |
| Understand snapshots and content checks      | [How a release works](#how-a-release-works)                   |
| Publish directly to a public repository      | [Direct publication](#direct-publication)                     |
| Test on GitHub before the final publication  | [Private-first CI](#private-first-ci)                         |
| Automate a repeatable sequence               | [Reusable workflows](#reusable-workflows)                     |
| Correct an earlier publication               | [Correcting published history](#correcting-published-history) |
| Incorporate a pull request                   | [Accepting an outside fix](#accepting-an-outside-fix)         |

For interruptions, cleanup, backups, or damaged local history, use the
[operations guide](operations.md).

## Quick path

After the first release, commit the changes you want to publish, then run:

```sh
pubanon prepare
pubanon inspect
pubanon publish
```

`prepare` saves a local snapshot of the selected committed files. `inspect` shows the release
details and a directory of read-only files for review. `publish` asks for confirmation before
sending that saved snapshot to GitHub. Your source branches, ordinary tags, checkout, and files
stay unchanged.

If your project has a committed package version, update it before preparing the next release.
Pubanon uses that version automatically. See [version tags](#version-tags) for details and for
projects without a package version.

For a versioned release, `pubanon release VERSION` combines preparation, an inspection summary, and
publication with confirmation. For example, after committing package version `0.2.0`:

```sh
pubanon release 0.2.0
```

The shortcut does not pause for file review. Use the separate commands above when you want to
inspect files or run tests. Rerunning the shortcut resumes the retained snapshot only when its
version and current checks still match. If new files need your decision, it asks about the complete
list. `--yes` cannot answer that question; see
[unattended releases](operations.md#unattended-releases).

## Step-by-step publication

For a new project, complete [setup](#setup-and-configuration) and
[initialization](#initialize-a-project), choosing `private` visibility. Create the destination
once:

```sh
pubanon repo create
```

### Prepare committed changes

From the source repository, check your working tree and commit the changes you want to publish.
Then run:

```sh
pubanon prepare
```

Preparation shows the selected source revision and full commit ID. It counts staged, unstaged, and
untracked changes so you can see what was left out; only committed bytes enter the snapshot.
`--source REV` selects another committed revision instead of `HEAD`. Preparation stays local and
does not contact GitHub.

### Inspect the saved files

```sh
pubanon inspect
```

The report shows the author and committer, complete commit message, intended tag, destination, and
expected visibility. It summarizes changes from the last publication, lists up to 20 changed paths,
and links to a complete local file report. Use `--verbose` for full source and publication commit
IDs and tree details.

Review the directory printed by `inspect`. Its files are read-only and contain no Git metadata.
Builds or tests that write files need a separate writable copy. Changes in either copy do not alter
the saved snapshot. To correct the release, edit and commit in the source, then discard and prepare
again. See [inspection](operations.md#inspection) for refreshing or removing review directories.

Inspection rechecks saved Git objects. It reports the content checks performed during preparation
and any changed configuration; it does not check current GitHub state.

### Publish

```sh
pubanon publish
```

Review the destination and snapshot, then confirm publication. If the selected source branch or tag
has changed, Pubanon warns before confirmation. You can publish the saved snapshot or decline and
prepare again to include newer changes. Declining keeps the snapshot. `--yes` refuses a changed
source reference.

Before pushing, Pubanon adds a private bookmark to the original source commit so a later rebase
does not lose it. See [source recovery](operations.md#published-history-and-source-recovery) for
retrieval, retention, and cleanup.

### Check CI and make the repository public

If you configured `required_checks`, wait for them on the latest published snapshot:

```sh
pubanon watch ci
```

Use `--version 0.1.0` to select that published version instead. When a branch and tag trigger
separate runs of the same check, all matching runs must succeed. For CI before publication, insert
the [private staging steps](#private-first-ci) between inspection and publication.

When ready to make a private destination public, run:

```sh
pubanon repo reveal
```

Reveal checks published Git history, repository settings, required CI, and any configured
[package observation](#package-observation). It lists Actions runs and artifacts and supplies links
for your review. Pubanon does not scan logs or artifact contents. After your separate disclosure
acknowledgment and confirmation, it changes visibility and checks the resulting repository again.

Already public repositories need no reveal step for later releases. See
[post-publication verification](operations.md#post-publication-verification) for additional checks.

## Version tags

For a Python package with a literal `[project].version` in the selected, committed root
`pyproject.toml`, plain `pubanon prepare` uses that version automatically. For example,
`version = "0.1.0"` produces the tag `v0.1.0`. The value must have strict `X.Y.Z` form.

To release a new version, update and commit the package version first. Pubanon does not increment
it or edit your files. An explicit `--version` must match the committed value; it cannot override
it. An uncommitted version edit does not count, and `--source REV` uses the value at that revision.
Preparation shows the intended tag and refuses a tag that is already published.

For projects without a static package version, including dynamically calculated versions, use
`--version` when you want a tag:

```sh
pubanon prepare --version 0.1.0
```

Plain `prepare` leaves these publications untagged. Pubanon does not execute package code to
discover versions.

## How a release works

A *snapshot* contains the selected files saved for publication. Pubanon selects files from one
source commit using your allow and deny rules, checks their contents, and creates a new commit
under your publication identity. "Sanitized" means selected and checked; Pubanon does not rewrite
secrets out of included files.

Content checks read selected file bytes as UTF-8 with surrogate escapes and as Latin-1. Pubanon
normalizes both text views and configured terms to NFC, then casefolds them for matching. UTF-16/32
text and compatibility-character substitutions, such as U+FF21 in place of ASCII `A`, are not
guaranteed matches. Review the selected files with these limits in mind.

The same snapshot has a state throughout its lifecycle:

| State        | Meaning                                                           |
| ------------ | ----------------------------------------------------------------- |
| `prepared`   | Saved locally for review and publication                          |
| `incomplete` | Replacement preparation needs continuation or conflict resolution |
| `unresolved` | Pubanon could not confirm whether publication completed           |
| `published`  | Part of the current published history                             |
| `superseded` | Previously published, then replaced by a history correction       |

For `incomplete` or `unresolved` snapshots, the explanation and recovery command appear below the
table. Follow that command to continue preparation or resolve publication. Opening an inspection
directory does not approve publication.

A review directory contains read-only files matching the saved snapshot. Pubanon verifies existing
directories before reusing them. To change the snapshot, edit and commit in your source repository,
then discard and prepare again. `discard` removes the prepared snapshot; `inspect --clean` removes
snapshot directories. Published and superseded snapshots remain in `list` while their saved history
is available.

`pubanon status` shows the current project, its prepared snapshot, and its last published snapshot.
`pubanon list` lists known snapshots in a table with their versions, states, and publication times.
Use `--verbose` for additional identifiers and provenance.

After replacing publications, `pubanon clean` previews the superseded local snapshots and asks once
before removing them, their review directories, and their source recovery information. It keeps
current published snapshots and prepared work. Use `pubanon delete ID` when you want to remove a
particular local snapshot instead. Neither command changes GitHub. See
[cleanup](operations.md#cleanup) for the distinction between deleting a snapshot, discarding
prepared work, and removing review copies.

```text
private source repository
        |  prepare: select files, check them, and save a snapshot locally
        v
prepared snapshot
        |  publish: send the exact snapshot to the destination repository
        v
private or public GitHub repository
```

`prepare` changes neither the source nor GitHub. You can stop to inspect the snapshot before
publishing it. If the destination is private, `pubanon repo reveal` checks it and asks for separate
confirmation before making it public.

Pubanon retains one prepared snapshot per project at a time. It checks content before and after
creating the commit, verifies the saved Git objects, and pushes the branch and any version tag
together in one atomic update.

## Setup and configuration

Run guided setup before initializing your first project:

```sh
pubanon setup
```

Setup asks for the publication name and email, then sensitive terms one per line until you enter a
blank line. It verifies the current GitHub CLI account and shows it before saving. If
authentication cannot be verified, choose retry, login, or cancel. Login hands the terminal and
browser authorization to `gh`.

Review the identity, account, and every term, then choose save, edit, or cancel. Editing preserves
your other answers and lets you replace or remove terms by number. It asks you to confirm
surrounding whitespace in an edited term. Nothing is written until you save.

To change saved answers, use `pubanon setup --edit`. To check readiness without editing, use
`pubanon doctor`. Rerunning ordinary setup preserves your answers and checks readiness; on POSIX,
it also restores owner-only configuration-file permissions. GitHub authentication completed by `gh`
remains in effect if setup later stops. See
[login recovery](operations.md#github-login-during-setup).

Identity changes affect every project using this configuration. Existing publication commits keep
their identity unless you [replace their history](#publication-identity). Changes to sensitive
terms or the expected account require retained snapshots to be prepared again.

Configuration and publication data live outside the source repository:

| Location                               | Contents                                                                |
| -------------------------------------- | ----------------------------------------------------------------------- |
| `~/.config/pubanon/config.toml`        | Publication identity, sensitive terms, and GitHub account               |
| `~/.config/pubanon/projects/NAME.toml` | Settings for the named project                                          |
| `~/.local/share/pubanon/`              | Project registry, snapshots, publication records, and local Git history |

Pubanon honors `XDG_CONFIG_HOME` and `XDG_DATA_HOME`. See [platforms](platforms.md) for filesystem
permissions and Windows behavior. To write the global TOML yourself, set `anon_name`, `anon_email`,
and `sensitive_terms`, plus `github` under `[accounts]`. Run `setup` or `doctor` to validate it.

### Initialize a project

From a private source repository with no Git remote, run:

```sh
pubanon init
```

Choose which files to publish, the GitHub destination, expected visibility, and repository
settings. The publication branch is always `main`; your private source branch can have any name.

Initialization first asks about paths whose names suggest credential files. A name match is a
warning, not proof of a secret; you choose whether to include each file. Included files still pass
the content checks. The preview groups included files by directory and shows exclusions with their
reasons. Exclusions take precedence over includes. Files named `.pubanon.toml` are always excluded
because that name is reserved for Pubanon configuration.

Choose continue to accept the selection or review to adjust it. Back undoes the preceding
include/exclude choice, including a credential-like filename decision. The updated preview also
offers Back. `--verbose` adds include/exclude patterns and the first two matches per include
pattern. You can save the complete filename list at the displayed local path; it contains names,
not file contents.

Initialization discovers CI check names from the committed workflows selected for publication.
Choose All listed or Select to choose by number. This controls which results Pubanon waits for; the
workflow still controls which jobs GitHub runs. Static matrix jobs expand to individual names.
Unsupported expressions and reusable workflows are marked unresolved so you can enter their names
yourself. Separate manual names with commas and quote a name containing a comma, such as
`quality, "tests (ubuntu-24.04, 3.13)"`. An empty list makes CI waiting optional.

Set the repository description and whether issues, the wiki, and projects are enabled. These
settings are saved under `[settings]` and checked before reveal. The final review lets you save,
edit, or cancel. Invalid answers return to the affected field without losing earlier choices.
Directories such as `docs/internal/`, `docs/superpowers/`, and `.agents/` appear as separate
selection groups when present. For Pubanon's own source, pass `--self-publication` so its name is
not treated as a hygiene term.

Each accepted answer is saved in a setup draft. After quitting or pressing Ctrl-C, rerun the
displayed command and choose Resume. Start over discards the draft; quitting the resume menu keeps
it. If the source commit changed, initialization keeps your choices and shows the new selection for
review. The project configuration is written only when you choose Save at the final review.

### Edit an existing project

Open `~/.config/pubanon/projects/NAME.toml` in your editor, using the configured XDG location if
different. To find the exact path, run `pubanon init` from the registered source directory. For an
existing project, this prints the path and refreshes registration; it does not reopen the setup
questions.

Update the existing fields for the change you want. For example, these are top-level fields, above
the first table heading such as `[settings]`:

```toml
allow = ["src/*", "pyproject.toml", "README.md", "LICENSE"]
deny = ["docs/internal/*"]
required_checks = ["quality"]
```

Keep the other saved fields. `allow` and `deny` use case-sensitive patterns against the whole
relative path, with forward slashes on every platform. `*` can match across directory separators:
`src/*` includes files in nested source directories. Deny patterns take precedence. Pubanon's
reserved `.pubanon.toml` filename stays excluded.

Private staging defaults to a repository named after the destination with `-stage` appended. To
choose another name, set the optional top-level `stage_repo` value. Add
[named workflows](#reusable-workflows) and [package observation](#package-observation) as their own
TOML tables. Edit the existing `[settings]` table for repository features and description;
`default_branch` must remain `"main"`. Pubanon checks declared settings against GitHub before
reveal; editing this file does not update GitHub settings.

After saving, run `pubanon status` to check the project and any retained snapshot. Changes to file
selection, publication identity, destination, or required checks require a fresh snapshot. When
instructed, discard the old snapshot and prepare again, then inspect the selected files. See
[when to prepare again](operations.md#when-a-snapshot-must-be-prepared-again) for the complete
rules. Use `pubanon setup --edit` for global identity, sensitive terms, and account changes.

## Publication identity

The global configuration holds one publication identity: the name and email Pubanon uses as author
and committer of every publication commit. It may be an alias or a real name. Pubanon does not
check which, and it does not sign commits or request a host-verified badge.

The identity must not appear in `sensitive_terms` or in a project's `extra_terms`. During setup,
`doctor` checks it against global `sensitive_terms` and machine-private paths. For a project,
`prepare` checks it against `sensitive_terms`, `extra_terms`, private paths, and hygiene terms. A
refusal names every matched rule source without printing the matched term.

Every published commit is audited against the configured identity. A changed name or email blocks
ordinary publication, synchronization, store repair, and reveal. Restore the original identity, or
follow [correcting published history](#correcting-published-history) and use
`pubanon prepare --replace all` to make an explicit full replacement under the changed identity.
The replacement does not erase old content from existing clones or caches. Choose an email you can
keep, such as a host no-reply address.

One configuration root holds one identity. To publish some projects anonymously and others under a
real name, use separate configuration and data roots. In a POSIX shell:

```sh
XDG_CONFIG_HOME=~/.config/pubanon-public XDG_DATA_HOME=~/.local/share/pubanon-public pubanon setup
```

Each root keeps its own global configuration, project registry, and published-history store. Under
a real name, the residual risks in the operations guide still apply to every configured sensitive
term; they no longer apply to the identity itself.

## Direct publication

Private staging is optional. If local evidence is sufficient, create the final repository as public
and publish directly. During `init`, choose `public` when asked for expected visibility;
`repo create --public` must match that saved choice.

```sh
pubanon init
pubanon repo create --public
pubanon prepare
pubanon inspect
pubanon publish
```

For a versioned release, you may replace the last three commands with `pubanon release VERSION`. It
requires a version matching any committed package version. `publish` and `release` use the existing
final repository; they do not create it, change visibility, or add staging.

## Private-first CI

Use a separate private staging repository to run CI before publishing to the final destination. Set
`required_checks` and, if needed, `stage_repo` in your
[project configuration](#edit-an-existing-project). Then run:

```sh
pubanon prepare
pubanon inspect
pubanon stage push
pubanon watch ci --staged
pubanon publish
```

On first use, `stage push` asks before it creates the private staging repository; `--yes` cannot
make that creation decision. Later pushes may replace only the staging branch and never send tags.
`stage status` checks the staged snapshot and reports its current CI results without waiting.
`watch ci --staged` waits for the configured checks on that snapshot. The final repository still
needs its own checks because staging results belong to the separate staging repository. Pubanon
proves that the released tree matches the staged snapshot.

Checks may take time to appear. The watches report progress and wait within a shared timeout;
Ctrl-C stops the wait without changing the snapshot or GitHub. Rerun the same watch to continue.
See [watch recovery](operations.md#retrying-an-interrupted-command) for missing, ambiguous, or
failed checks and timeout options.

Delete the staging repository when it is no longer useful:

```sh
pubanon stage clean
```

The command previews the selected repository and asks before deleting it. See
[cleanup](operations.md#cleanup) if the configuration changed or GitHub cannot confirm whether the
repository still exists.

## Reusable workflows

Add a named sequence to your [project configuration](#edit-an-existing-project). Set
`required_checks` before including `watch ci`:

```toml
[workflow.ship]
stages = ["prepare", "inspect", "publish", "watch ci"]
```

Run it with an explicit publication version matching any committed package version:

```sh
pubanon run ship --version 0.1.0
```

A declared workflow prints its stage outline first. It prepares and verifies the retained snapshot
before asking for publication approval. The final preview shows its full commit IDs, actual author
and committer, complete message, file summary, verified destination and visibility, and exact refs.
A public destination means the snapshot becomes publicly accessible. Declining retains the snapshot
and snapshot directory; run `pubanon inspect` for hands-on review, then publish when ready. Staging
creation, staging push, publication, and reveal each keep their own concrete approval.

With `--yes`, selected stages continue automatically after their previews. The inspection stage
creates or preserves the snapshot-specific copy but does not pause for editing or testing. For
hands-on review in that mode, end the workflow at `inspect` or run `prepare`, `inspect`, and
`publish` separately. First-use staging creation, source drift, historical-exposure acknowledgment,
and reveal disclosure are not covered by `--yes`.

Review directories remain available after publication or cancellation. See
[inspection](operations.md#inspection) for checking, refreshing, or cleaning them.

A workflow may contain each of these stages at most once and in this order: `prepare`, `inspect`,
`stage push`, `watch ci --staged`, `publish`, `watch ci`, `repo reveal`, and `watch package`. `run`
prints the resolved stages and every possible change to GitHub before it starts. `--yes` can
confirm only that declared sequence. It cannot answer policy, new-path, source-drift, first-use
staging, or reveal-disclosure decisions.

## Non-package workflows

Pubanon can publish any supported Git source snapshot. A project without a package can omit the
`[package]` table and still publish Git history, wait for CI, and reveal a repository.

Use plain `pubanon prepare` for an untagged snapshot or [choose a version tag](#version-tags)
explicitly. Configure `required_checks` to use `pubanon watch ci`. A
[named workflow](#reusable-workflows) can end after CI; package observation is optional.

## Package observation

Use package observation when your release workflow uploads to PyPI and you want to check that the
files arrived. Pubanon observes the upload; it never uploads a package itself. Add this table to
your [project configuration](#edit-an-existing-project):

```toml
[package]
index = "pypi"
name = "public-package"
release_checks = ["Publish package"]
```

Use the exact PyPI distribution name for `name`. `release_checks` must contain at least one exact
host check name that gates the project's package release. `pubanon watch package` observes PyPI
only after every configured release check succeeds. Without `--version`, it selects the numerically
greatest strict `X.Y.Z` release tag, regardless of commit recency or the published `main` tip. With
`--version`, it selects that exact release tag and commit. Version metadata without distribution
files remains incomplete: the watch continues, and `repo reveal` refuses to change visibility until
files appear. A successful observation lists the actual files. Pubanon never uploads a package.

## Correcting published history

Use replacement when you need to correct an existing publication. Choose the command for your
situation:

| What you want                                                | Preparation command                |
| ------------------------------------------------------------ | ---------------------------------- |
| Publish for the first time                                   | `pubanon prepare`                  |
| Publish a new release                                        | `pubanon prepare`                  |
| Correct the latest publication while keeping earlier history | `pubanon prepare --replace`        |
| Correct an older release and rebuild subsequent publications | `pubanon prepare --replace v0.1.0` |
| Start the published history over entirely                    | `pubanon prepare --replace all`    |

After preparation, run `pubanon inspect`, then `pubanon publish`. Preparation uses your current
committed source by default and does not change GitHub. The summary explains which history will be
replaced. These commands have the same meaning for private and public destinations.

For a versioned package, commit a new version before preparing an ordinary new release. If its tag
already exists, plain `prepare` stops and explains how to select a replacement instead.

While iterating privately toward an initial release, commit each correction and repeat:

```sh
pubanon prepare --replace all --message "Initial release."
pubanon inspect
pubanon publish
```

Each successful publication starts the published history over with one commit and the intended
version tag, if any. You can keep the same package version while correcting that initial release.
Your private development history stays intact.

Once others use a public repository, usually publish a new release instead of replacing history.
Ordinary publication and `publish --sync` do not rewrite published history. Replacement can be used
for private or public repositories, but may disrupt existing clones and consumers. Existing clones,
caches, and host-retained objects may still contain the old content. Moved Git tags do not replace
GitHub Release assets, Actions logs or artifacts, or packages already uploaded by your release
workflow.

First commit the intended correction in your private source. Select that committed revision when
preparing:

```sh
pubanon prepare --replace v0.1.0 --source REV
pubanon inspect
```

`REV` is the private source commit or ref containing the corrected snapshot. `--source` defaults to
`HEAD`; selecting an older publication does **not** automatically select its old private source.
Use `pubanon show v0.1.0 --verbose` to locate that original input if you need it. Uncommitted edits
are excluded. When the selected publication is tagged, an inferred package version or explicit
`--version` must match one of its moved tags. With one tag, an omitted version defaults to it; with
several, the report lists them all. Commit the matching package version in the correction snapshot;
`--version` does not override conflicting committed metadata. For an untagged selection or
`--replace all`, the ordinary version rules determine the intended tag, if any.

Choose the scope deliberately:

- `--replace v0.1.0` or an unambiguous published commit prefix corrects that release and replays
  every later publication in order. Earlier commits stay unchanged. Every tag on a rewritten commit
  moves to its replacement, including multiple tags on one commit; earlier tags stay put.
- `--replace` defaults to `latest`. Both forms correct the current published tip, with no
  descendants to replay, and preserve earlier publications.
- `--replace all` creates a new root from the selected source snapshot and replays nothing. It
  removes the old tag set and creates the intended version tag, if any. An old tag with that same
  name moves to the new root. This is the clean-slate option.

Selection uses local published history and must be reachable from its current `main`. Preparation
does not contact GitHub. A version selector follows its current tag; use a commit prefix to select
a superseded snapshot for history inspection. Only one correction can be retained at a time.

Replay preserves each later publication's change relative to its predecessor. It can stop on a
conflict. `prepare` and `status` name the affected release, conflict groups, directory, and
`resolutions.toml`. The directory contains `tree/` for editable result files and numbered
`versions/` slots for available base, ours, and theirs inputs. The manifest records each slot's
original path and modes. Edit result files under `tree/`, not `versions/`.

Every actionable file path in the manifest needs an explicit decision:

- Keep the result at that path with `action = "keep"`.
- Remove the path with `action = "delete"`.
- Move it by replacing the `action` field with `rename = "new/path"` and putting the intended
  result at `tree/new/path`.

Conflicts can group related paths, including files within a directory conflict. Decide every listed
file; do not add new manifest paths. Editing a file alone does not resolve an
`action = "unresolved"` entry. Pubanon refuses missing required files, invalid actions, occupied
rename targets, and remaining conflict markers in checked text entries. It ignores and reports
edits outside the declared paths. Resolved and replayed results still pass the publication gates.

```sh
pubanon prepare --continue
pubanon status
```

Repeat resolution and continuation until the rewrite is complete. If export was interrupted,
continuation regenerates the incomplete export from saved Git objects without accepting its edits.
Once the state is awaiting resolution, continuation preserves your edits on refusal. Do not change
source or publication settings to amend a retained correction; discard and prepare again instead.

The default inspection is the resulting tip. To inspect an intermediate rewritten release, select
its moved tag or rewritten commit prefix:

```sh
pubanon inspect
pubanon inspect --rewritten v0.2.0
```

The report lists old-to-new commits, per-release changes, moved tags, and recorded resolutions; its
complete files report contains changes omitted from a bounded terminal preview. Inspection copies
are read-only and match the selected saved snapshot. If you use staging, `pubanon stage push` sends
the resulting tip only, and publication requires staging evidence for that same tip.

```sh
pubanon publish
pubanon watch ci --version 0.3.0
```

The CI example selects the final replayed release's version; use your actual moved tag. Publication
reviews the destination, correction and replay mapping, ref changes, and public-history warning
before one confirmation. It sends the frozen result with exact old-ref checks in one atomic push.
GitHub rules may refuse the rewrite; Pubanon does not promise to detect host policy in advance.
Local publication success does not imply CI success. Observe the exact published version next. If
the push outcome is unknown, run `pubanon publish --resolve`; it checks the saved destination
without pushing again. `release` and `run` refuse retained replacements so that you finish through
the explicit inspection and publication steps.

`list` shows superseded snapshots with state `superseded`. They remain locally inspectable by
commit prefix while preserved. A derived release's original source revision is an input to replay,
not an exact private-source copy of its resulting published tree. See the operations guide for
[source recovery](operations.md#published-history-and-source-recovery) and
[separate cleanup choices](operations.md#cleanup).

## Existing destinations and restored history

For a first publication, the configured host repository may already exist when it is empty. Prepare
and publish normally:

```sh
pubanon prepare
pubanon inspect
pubanon publish
```

The `publish` preflight verifies that the repository is empty before sending the first release. It
refuses a nonempty repository with unrelated commits, branches, or tags rather than importing,
replacing, or force-updating that history.

For a recreated or restored repository, retained local published history may be sent to a host that
is empty or an exact prefix of that history:

```sh
pubanon publish --sync
```

`publish --sync` verifies that the host is empty or strictly behind, then sends the verified
retained public history. It consumes no prepared snapshot.

Historical auditing uses current terms, including for the publication author and committer name and
email. If an alias becomes sensitive after publication, `status --audit` reports the exposure,
`repo reveal` refuses it, and synchronization requires explicit historical-exposure acknowledgment.
`--yes` alone cannot supply that acknowledgment.

Findings identify the matched term and its location so you can correct it. Accepting a historical
exposure does not remove published content or allow those terms in new snapshots. See
[post-publication verification](operations.md#post-publication-verification) for audit details.

Use `repo verify` when the host should already match the retained publication state exactly:

```sh
pubanon repo verify
```

If the local published-history copy is missing or damaged and the host still has its main branch,
rebuild the local copy from the verified host:

```sh
pubanon repo verify --repair-store
```

Use repair only when GitHub has the history you want to keep locally. It previews the local changes
and asks before replacing them. Matching history structure and identity cannot prove that Pubanon
originally created it. See [history repair](operations.md#repairing-the-local-history-copy) for
verification, partial-success recovery, and limits.

## Accepting an outside fix

Incorporate outside changes in the private source and publish a new release through Pubanon.
Merging a pull request on GitHub changes the expected remote history and blocks the next
publication.

The private source has no remote, so `gh` cannot infer the repository. Name it explicitly and save
the change as a unified diff to a path outside the source repository, so the patch file itself does
not appear as an untracked change:

```sh
gh pr diff --repo OWNER/REPO NUMBER >/path/outside/the/source/fix.patch
```

Review the patch. Then, from a clean working tree, stage it, review what is staged, and commit:

```sh
git status --porcelain
git apply --index /path/outside/the/source/fix.patch
git diff --cached
git commit -m "fix: <summary>" -m "Co-authored-by: Contributor Name <contributor@example.com>"
```

The first command must print nothing. `--index` stages every change in the patch, including added
and deleted files, so the ordinary commit captures all of it. Private history carries whatever
authorship you choose; it is never published.

Release through Pubanon as usual. The public commit carries the publication identity. Credit the
contributor in the controlled message with the same trailer:

```sh
pubanon prepare --version 0.2.0 --message "release: v0.2.0

Co-authored-by: Contributor Name <contributor@example.com>"
pubanon publish
```

Pubanon checks the release message for sensitive terms too. It refuses a contributor trailer that
matches one.

Close the pull request with an explanation:

```sh
gh pr close --repo OWNER/REPO NUMBER --comment "Merged privately and released in v0.2.0. This repository publishes verified releases and does not merge on the host."
```

A `CONTRIBUTING.md` stating that policy is ordinary project content.

### If the host changed outside Pubanon

When `publish` reports that the remote state differs from what the snapshot expects, or
`status --audit --remote` reports that host refs do not match local published state, Pubanon shows
the expected and observed references. It does not know why they differ. Possible causes include a
pull request merged on the host, a push from another machine or configuration root, or a change
made in the host's web interface.

Ordinary publication does not remove commits from host history. Explicit
[publication replacement](#correcting-published-history) can rewrite the expected published refs,
but does not bypass a mismatch caused by outside changes. If you want the public branch back at the
last published state, restore it outside Pubanon, using the last published commit reported by
`pubanon status`. After that, `pubanon status --remote` reports a match and publication resumes. If
the local published-history store is what is wrong, `pubanon repo verify --repair-store` rebuilds
it from a host that passes verification. Repair checks history structure and identity, but cannot
prove that Pubanon originally created the commits.

## Reading the terminal interface

Sections are separated by blank lines and bold cyan headings. Questions are bold, choices are cyan,
successful results are green, warnings and cancellations are yellow, and errors are red. Labels and
messages also explain their meaning without color. `--color auto` (the default) enables styling on
terminals and respects `NO_COLOR`; redirected output stays plain. `--color always` forces styling,
including in pipes, and overrides `NO_COLOR`. `--color never` disables color and bolding.

Use `-v` or `--verbose` for additional snapshot IDs, source revisions, and local paths. Normal
output still includes the state, relevant warnings, and next action. Presentation options work
before or after the command. Publication timestamps use local time with seconds and a timezone.

Inside a registered source repository or its subdirectories, commands select that project
automatically. Use `-p NAME` (or `--project NAME`) before or after a command when working elsewhere
or selecting another project.

## Disconnecting an alternate object store

If Pubanon reports that your source borrows Git objects from another repository, follow the
[disconnection procedure](operations.md#disconnecting-an-alternate-object-store). It copies and
verifies the borrowed objects before removing the connection. Keep the original object store
available until verification succeeds.
