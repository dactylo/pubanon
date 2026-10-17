# pubanon operator guide

For installation and your first publication, start with the
[README walkthrough](../README.md#your-first-release). Use this guide for later releases, project
settings, and named workflows. Commands select the project registered to your current source
directory; use `--project NAME` when working elsewhere.

| If you want to...                            | Start here                                                          |
| -------------------------------------------- | ------------------------------------------------------------------- |
| Publish committed changes                    | [Quick path](#quick-path)                                           |
| Configure, create, or recover a release page | [GitHub release pages](#github-release-pages)                       |
| Review each publication step                 | [Step-by-step publication](#step-by-step-publication)               |
| Choose or update a version tag               | [Version tags](#version-tags)                                       |
| Set up another project                       | [Initialize a project](#initialize-a-project)                       |
| Remove a project from this machine           | [`pubanon forget`](operations.md#cleanup)                           |
| Change files, CI checks, or project settings | [Edit an existing project](#edit-an-existing-project)               |
| Understand snapshots and content checks      | [How a release works](#how-a-release-works)                         |
| Publish directly to a public repository      | [Direct publication](#direct-publication)                           |
| Test on GitHub before the final publication  | [Private-first CI](#private-first-ci)                               |
| Automate a repeatable sequence               | [Reusable workflows](#reusable-workflows)                           |
| Correct an earlier publication               | [Correcting published history](#correcting-published-history)       |
| Delete and recreate the final repository     | [Recreating the final repository](#recreating-the-final-repository) |
| Incorporate a pull request                   | [Accepting an outside fix](#accepting-an-outside-fix)               |

For interruptions, cleanup, backups, or damaged local history, use the
[operations guide](operations.md).

## Quick path

For a staged release with configured checks, commit the changes you want to publish, then run:

```sh
pubanon prepare
pubanon inspect
pubanon stage push
pubanon stage watch
pubanon repo create
pubanon publish
pubanon repo watch
pubanon repo reveal
```

`prepare` saves a local snapshot of the selected committed files, and `inspect` shows its details
and a read-only copy of the files to review. `stage push` sends that exact snapshot to a private
staging repository, and `stage watch` waits for its configured checks. `repo create` creates the
final repository or verifies a matching empty repository you created beforehand. `publish` asks for
confirmation, then sends the snapshot there and, if release pages are on, creates one for a tagged
version. `repo watch` waits for the final repository's checks, and `repo reveal` makes the
repository public after a review.

Leave out the commands that don't apply:

- Skip `stage watch` when neither `ci_workflow` nor checks are configured, and review the staging
  repository yourself. Skip `repo watch` in the same way.
- Skip `repo create` after the first publication, and `repo reveal` when it is already public or
  should stay private.
- On the `direct` route, skip both `stage` commands and go from inspection to publication.

Publication leaves your source branches, checkout, and files unchanged. Optional
[local source tags](operations.md#local-source-tags) mark releases in your Git history.

Interactive `prepare` shows the included and excluded files and asks whether to continue, review,
or quit. Review walks through the file groups again with include, exclude, customize, and back
choices, as in [`init`](#initialize-a-project). A revised selection is saved to the project
configuration only when you continue; quit leaves it unchanged. Global exclusions always apply.
`--yes` skips this review but cannot answer unresolved file choices or approve files that are new
since the last release.

If your project has a committed package version, update and commit it before preparing; pubanon
reads it automatically. Otherwise, choose a version tag with `pubanon prepare --version 0.2.0`. See
[version tags](#version-tags) for the rules. For a repeatable sequence, declare a named
[workflow](#reusable-workflows).

## GitHub release pages

A release page announces a published version on GitHub. Release pages are off unless you turn them
on: `pubanon init` asks whether to create one for each published version, and `pubanon init --edit`
changes that answer later. Answer draft to have your release workflow publish each page after
uploading the package. When pages are on, pubanon creates the page right after a tagged version is
published and shows it in the publish preview first. Untagged publications have no page.

A page can notify watchers and start workflows that run on new releases, including package uploads.
On the staged route with required checks, the page is created after staging checks pass. On the
direct route, or without checks, it is created before any CI runs. To create a page only after the
final repository's CI passes, leave release pages off and run `pubanon release create` after
`pubanon repo watch`. A draft notifies no one and starts no release workflows until it is
published; see [Publish pages after the package upload](#publish-pages-after-the-package-upload).

By default, the page title is the tag, such as `v0.2.0`, and the notes are one line linking to
`CHANGELOG.md` at that tag in the final repository. If the published snapshot has no root
`CHANGELOG.md`, the notes are empty and the preview shows how to link a changelog elsewhere.

### Change the defaults

Add a `[release_page]` table to your [project configuration](#edit-an-existing-project). `init`
saves `enabled` and `draft`; the other keys change the page itself. Each example is independent:

```toml
[release_page]
enabled = true
```

```toml
[release_page]
enabled = true
draft = true
```

```toml
[release_page]
changelog = "docs/CHANGES.md"
```

```toml
[release_page]
title = "Version {version}"
notes = """
See the [changelog]({changelog_url}).

Install with `pip install example=={version}`.
"""
```

`title` and `notes` are templates. `{tag}` is the tag, `{version}` is the tag without its leading
`v`, and notes can also use `{changelog_url}`. Write `{{` and `}}` for literal braces, and
`notes = ""` for empty notes. Without the table, `enabled` and `draft` are `false`, `title` is
`"{tag}"`, and the notes link to `CHANGELOG.md` when it exists. A `changelog` you set must be a
file in the published snapshot whenever the notes link to it.

### Publish pages after the package upload

With `draft = true`, every page pubanon creates is a draft: in `publish`, in a `run` publish stage,
with `--release-page`, and in `release create`. Only people with write access to the repository can
see a draft. Your release workflow publishes it after uploading the package, so the page and the
package appear together. pubanon never publishes or deletes a page, and `release edit` keeps a
page's status. Its results mark a page that is still a draft with `(draft)`.

The starter workflows [release-start.yml](examples/release-start.yml) and
[release.yml](examples/release.yml) do this for a Python package on PyPI. Copy both to
`.github/workflows/` beside the [starter CI workflow](examples/ci.yml), and follow their comments
to add a PyPI Trusted Publisher. After each successful push run of CI on `main` in a public
repository, the start workflow checks whether the commit carries the `vX.Y.Z` tag for the version
in `pyproject.toml`. If it does, it starts the release workflow on that tag, which builds and
uploads the package and then publishes the tag's draft. The page becomes GitHub's latest release
unless a published, non-prerelease page has a higher version. Commits without the tag start
nothing. Because every release run starts on its tag, its checks, such as `Publish to PyPI`, appear
on the tagged commit, where `pubanon package watch` looks for them.

Running the release workflow again for the same version is safe: files already on PyPI are skipped,
and a waiting draft is published. To publish a tagged commit by hand, run the release workflow from
the Actions tab on its tag. If a release run fails, rerun that run rather than starting a new one:
a new run leaves the failed run's skipped checks on the tagged commit, and `pubanon package watch`
refuses skipped checks.

To approve each upload, open the repository's settings on GitHub, then Environments, then `pypi`,
and add required reviewers. The release workflow then waits for approval before uploading, and the
page appears after the approved upload. Later release runs wait until you approve or reject the
waiting one. Release runs start on tags, so if the environment limits deployments to selected
branches and tags, allow your version tags.

A draft created after the release workflow finished stays a draft. This happens when page creation
failed and `release create` or a `run` rerun finished it later, or when you add a page for an
earlier version. The workflow's summary then says it found no page. Rerun the workflow, or publish
the page on GitHub. The next publication's preview also warns about earlier versions whose pages
are still drafts.

A page published by the workflow does not start other workflows that listen for release events,
because GitHub starts no workflows for changes made with a workflow's own token. pubanon checks the
tag, title, and notes of every page it creates, but not files a workflow attaches to a page.

### For one publication

```sh
pubanon publish --release-page
pubanon publish --no-release-page
pubanon publish --release-notes-file notes.md
```

The first creates a page for this publication even when release pages are off. The second publishes
without a page. The third uses your own notes for this publication, sent exactly as written, and
also creates a page when release pages are off. All three work with `pubanon run NAME` when the
workflow includes `publish`. When a tagged publication finishes without a page, pubanon prints the
`release create` command that adds one.

### Add or fix a page later

```sh
pubanon release create v0.2.0
pubanon release show v0.2.0
pubanon release edit v0.2.0 --title "Version 0.2.0" --notes-file revised-notes.md
```

`release create` uses your configured title and notes unless you pass `--title` or `--notes-file`,
and it works even when `enabled = false`. With `draft = true`, it creates a draft. If a matching
page already exists, it reports the page and changes nothing. If a different page exists, it shows
the differences and points to `release show` and `release edit`. `edit` changes only what you pass;
an empty notes file clears the notes. `pubanon release list` shows up to 30 pages; `--limit`
changes the count.

### If something goes wrong

If the code is published but the page is not, pubanon says so and prints the `release create`
command that finishes it. That command is safe to run whether or not the page now exists. Rerunning
a `run` workflow also creates a missing page before continuing when release pages are on or the
rerun passes `--release-page`. With release pages on, `publish --resolve` reports whether a
completed publication has its page, without creating one. If you are unsure whether a
`release edit` took effect, check with `release show` before editing again.

### Immutable releases

GitHub's immutable releases setting keeps each published release's tag and attached files fixed and
signs a record of the release, which `gh release verify` checks. Titles and notes stay editable.
pubanon cannot replace published history that moves the tag of an immutable release. A deleted
release's version can never be used again, even in a recreated repository.

When `repo reveal` first records the final repository as public, whether reveal made it public or
GitHub already did, pubanon considers immutable releases if release pages are enabled or the
repository already has a page. After the visibility check, it asks to enable a setting that is off
and not enforced by the owner, or to convert eligible pages if the setting is on and any remain
mutable. You may still need to correct published history or recreate the repository before then.
Answering yes turns the setting on if needed and converts existing non-draft release pages that
have titles, including pages created while the repository was private. Pages without titles stay
mutable; draft pages become immutable when published. Later runs of reveal on a repository already
recorded as public do not ask again.

To turn the setting on later, use the repository's settings on GitHub, or run:

```sh
gh api -X PUT repos/OWNER/REPO/immutable-releases
```

Turning on the setting through GitHub or the command alone does not change existing pages. To make
an existing page immutable, edit it without changing its content. These commands send the page's
current title back unchanged:

```sh
id=$(gh api repos/OWNER/REPO/releases/tags/v0.1.0 --jq .id)
gh api repos/OWNER/REPO/releases/$id --jq '{name}' | gh api -X PATCH repos/OWNER/REPO/releases/$id --input -
```

A page without a title cannot be converted without changing it. Give it a title with
`gh release edit v0.1.0 --title v0.1.0` to make it immutable. If pubanon reports that turning the
setting on did not finish, check whether the setting is on and enable it if needed. Then use the
PATCH commands above for listed pages with titles, or give an untitled page a title.

### Details

- pubanon previews every page change and checks the tag, title, and notes against your content
  rules; `--yes` skips only the confirmation. It also checks that the tag belongs to your published
  history and points to the same commit on GitHub.
- A page can notify GitHub watchers and trigger workflows that listen for release events. It is
  created while the final repository's CI may still be running, so it does not certify CI or a
  package upload. A draft published by the
  [starter release workflows](#publish-pages-after-the-package-upload) appears only after both.
- Backfilling an older version never makes it GitHub's latest release when a higher version already
  has a published, non-prerelease page. Otherwise GitHub chooses the latest release.
- A replacement that moves several version tags gets no automatic page. pubanon prints a
  `release create` command for each tag, and `--release-page` and `--release-notes-file` need a
  single version tag.
- `--release-page`, `--no-release-page`, and `--release-notes-file` cannot be combined with each
  other, do not change your configuration, and do not work with `publish --resolve`. A relative
  notes path starts at the working directory, and `-` does not mean standard input. A rendered
  notes template ends with one line break; a notes file is sent without template expansion or
  line-break changes.

## Step-by-step publication

For a new project, complete [setup](#setup-and-configuration) and
[initialization](#initialize-a-project), choosing `private` visibility. Create the final
destination before publication if it does not already exist.

### Prepare committed changes

From the source repository, check your working tree and commit the changes you want to publish.
Then run:

```sh
pubanon prepare
```

Preparation shows the selected source revision and short commit ID (the full ID with `--verbose`).
It counts staged, unstaged, and untracked changes so you can see what was left out; only committed
bytes enter the snapshot. `--source REV` selects another committed revision instead of `HEAD`.
Preparation stays local and does not contact GitHub. The commit message comes from
[commit message settings](#commit-messages) or the built-in default; `--message` overrides it for
one preparation.

### Inspect the saved files

```sh
pubanon inspect
```

The report shows the author and committer, complete commit message, intended tag, destination, and
expected visibility. It summarizes changes from the last publication, lists up to 20 changed paths,
and links to a complete local file report. Use `--verbose` for full source and publication commit
IDs and tree details.

Review the copy printed by `inspect`. Its files are read-only and contain no Git metadata. Builds
or tests that write files need a separate writable copy. Changes in either copy do not alter the
saved snapshot. To correct the release, edit and commit in the source, then discard and prepare
again. `discard` removes that preparation's review copies; see
[inspection](operations.md#inspection) for refreshing copies kept with other snapshots.

Inspection rechecks saved Git objects. It reports the content checks performed during preparation
and any changed configuration; it does not check current GitHub state.

### Test privately and publish

On a staged project, run `pubanon stage push`, then `pubanon stage watch` if checks are configured.
If staging reveals a problem, commit the fix in the source, run `pubanon discard`, prepare and
inspect again, and push the new candidate. The staging repository is reused for later candidates.
See [private-first CI](#private-first-ci).

Before the first final upload, consider that GitHub may retain commits, activity, Actions logs, and
artifacts after refs are replaced. Staging lets you check the exact snapshot before the final
repository receives it. Create the final repository if absent, then publish:

```sh
pubanon repo create
pubanon publish
```

Review the destination and snapshot, then confirm publication. If the selected source branch or tag
has changed, pubanon warns before confirmation. You can publish the saved snapshot or decline and
prepare again to include newer changes. Declining keeps the snapshot. `--yes` refuses a changed
source reference. For a tagged version, the preview also shows the release page pubanon will create
after the code is published, or why it will keep or skip one.

Before pushing, pubanon adds a private bookmark to the original source commit so a later rebase
does not lose it. See [source recovery](operations.md#published-history-and-source-recovery) for
retrieval, retention, and cleanup.

### Check CI and make the repository public

If you configured `ci_workflow` or `required_checks`, wait for CI:

```sh
pubanon repo watch
```

By default, `repo watch` checks the latest published commit; `--version 0.1.0` selects that
release's commit by tag. Without `ci_workflow`, if a branch push and a tag push both run the same
required check, every check run must pass. With `ci_workflow`, pubanon judges the latest matching
push run for the exact selected commit on `main`.

When ready to make a private repository public, run:

```sh
pubanon repo reveal
```

Reveal checks the published history, repository settings, required CI, and any configured
[package observation](#package-observation). It re-checks the tag, title, and notes of every
release page, including drafts, under the current sensitive terms and refuses if any page matches.
A draft can become public later without passing reveal. If published history contains terms you
added after publishing it, reveal lists them and asks whether to make them public; see
[Sensitive terms in published history](#sensitive-terms-in-published-history).

Reveal also checks your Actions history with the same terms: the details of every workflow run, the
runner of every job, and every job log. It needs GitHub CLI 2.97.0 or newer, and it stops while any
run is still running. If anything matches, reveal lists the matches by run. For matching logs, it
offers to delete the logs of those runs, keep them, or cancel. Deleting asks again, cannot be
undone, and keeps the runs and their results. Deleting logs cannot remove matching run or runner
details; to remove those details, delete the run on GitHub, then rerun reveal.

`pubanon audit --remote` runs the same check on a repository that is already public, such as one
revealed before pubanon 0.5.0, and offers to delete matching logs.

Reveal cannot check artifact contents, Actions caches, job summaries, annotations, or earlier
pushes listed on the Activity page. It names what it could not check, with links to review it, and
then asks to make the repository public. Its Actions check uses several requests rather than an
atomic snapshot: a run that starts after the last check is not checked, so avoid starting workflows
during reveal. If reveal cannot read the repository back right after the visibility change, it
retries for up to 3 minutes, because GitHub may still be applying the change. If it still cannot,
wait a minute and rerun it; the rerun records the repository as public without changing it again.
On the first reveal that records the repository as public, it may offer to enable or apply
[immutable releases](#immutable-releases) if release pages are enabled or pages already exist and
the setting or eligible pages need action. A repository already recorded as public needs no reveal
for later releases. See
[post-publication verification](operations.md#post-publication-verification) for more checks.

## Version tags

For a Python package with a literal `[project].version` in the selected, committed root
`pyproject.toml`, plain `pubanon prepare` uses that version automatically. For example,
`version = "0.1.0"` produces the tag `v0.1.0`. The value must have strict `X.Y.Z` form.

To release a new version, update and commit the package version first. pubanon does not increment
it or edit your files. An explicit `--version` must match the committed value; it cannot override
it. An uncommitted version edit does not count, and `--source REV` uses the value at that revision.
Preparation shows the intended tag and refuses a tag that is already published.

For projects without a static package version, including dynamically calculated versions, use
`--version` when you want a tag:

```sh
pubanon prepare --version 0.1.0
```

Plain `prepare` leaves these publications untagged. pubanon does not execute package code to
discover versions.

## How a release works

A *snapshot* contains the selected files saved for publication. pubanon selects files from one
source commit using your allow and deny rules, checks their contents, and creates a new commit
under your publication identity. "Sanitized" means selected and checked; pubanon does not rewrite
secrets out of included files.

Content checks look for your configured terms in every selected file, ignoring case and treating
equivalent Unicode spellings of a character as the same. They read each file as UTF-8 and as
Latin-1 text. A term may be missed in a UTF-16 or UTF-32 file, or when it is written with
look-alike characters such as full-width letters. Review the selected files with these limits in
mind.

The same snapshot has a state throughout its lifecycle:

| State        | Meaning                                                           |
| ------------ | ----------------------------------------------------------------- |
| `prepared`   | Saved locally for review and publication                          |
| `incomplete` | Replacement preparation needs continuation or conflict resolution |
| `unresolved` | pubanon could not confirm whether publication completed           |
| `published`  | Part of the current published history                             |
| `superseded` | Previously published, then replaced by a history correction       |

For `incomplete` or `unresolved` snapshots, pubanon explains the problem and gives the recovery
command. Opening a review copy does not approve publication.

`pubanon status` shows the current project, its prepared snapshot, and its last published snapshot.
`pubanon list` shows known snapshots with their versions, states, and publication times; add
`--verbose` for identifiers and provenance. Published and superseded snapshots stay in `list` while
their saved history is available. To remove snapshots or review copies, see
[cleanup](operations.md#cleanup).

```text
private source repository
        |  prepare: select files, check them, and save a snapshot locally
        v
prepared snapshot
        |  publish: send the exact snapshot to the destination repository
        v
private or public GitHub repository
```

`prepare` changes neither the source nor GitHub, so you can inspect the snapshot before publishing
it. Each project has at most one prepared snapshot at a time. pubanon checks content before and
after creating the commit, verifies the saved Git objects, and pushes the branch and any version
tag together, so either both arrive or neither does.

## Setup and configuration

Run guided setup before initializing your first project:

```sh
pubanon setup
```

Setup asks for the publication name and email, sensitive terms one per line until you enter a blank
line, and the default publication route for new projects. The built-in route is `staged`; choose
`direct` to publish without private staging. It verifies the current GitHub CLI account and shows
it before saving. If authentication cannot be verified, choose retry, login, or cancel. Login hands
the terminal and browser authorization to `gh`.

Review the identity, account, and every term, then choose save, edit, or cancel. Editing preserves
your other answers and lets you replace or remove terms by number. It asks you to confirm
surrounding whitespace in an edited term. Nothing is written until you save.

To change saved answers, use `pubanon setup --edit`. To check readiness without editing, use
`pubanon doctor`. Rerunning ordinary setup preserves your answers and checks readiness; on Linux
and macOS, it also restores owner-only permissions on the configuration files. GitHub
authentication completed by `gh` remains in effect if setup later stops. See
[login recovery](operations.md#github-login-during-setup).

When pubanon saves a configuration file, it rewrites the whole file, so hand-written comments and
layout may be lost. The global `config.toml` is saved by guided setup and `setup --edit`, which
preserves a hand-edited `[commit_message]` table. A project's file is saved when `init` creates the
project, changes its route, source-tag setting, or self-publication setting, or relocates its
source; when `init --edit` saves changes; when you accept a revised file selection in `prepare`;
when you acknowledge a historical exposure during publication; and when `repo reveal` records that
the repository is public. `doctor` never writes configuration, and neither do setup or `init` when
nothing changes.

Identity changes affect every project using this configuration. Existing publication commits keep
their identity unless you [replace their history](#publication-identity). Changes to sensitive
terms or the expected account require retained snapshots to be prepared again.

Configuration and publication data live outside the source repository:

| Location                               | Contents                                                                   |
| -------------------------------------- | -------------------------------------------------------------------------- |
| `~/.config/pubanon/config.toml`        | Publication identity, sensitive terms, GitHub account, and commit messages |
| `~/.config/pubanon/projects/NAME.toml` | Settings for the named project                                             |
| `~/.local/share/pubanon/`              | Project registry, snapshots, publication records, and local Git history    |

`XDG_CONFIG_HOME` and `XDG_DATA_HOME` change these roots. The local Git store at
`~/.local/share/pubanon/projects/NAME/store.git` holds the exact published snapshots and history;
it is separate from your private source repository. See [platforms](platforms.md) for file
permissions and Windows behavior.

To write the global configuration yourself, use this form. `sensitive_terms` needs at least one
term, and `default_publication_route` is `staged` or `direct`. Run `pubanon doctor` afterwards to
validate it.

```toml
anon_name = "Example Author"
anon_email = "author@example.com"
sensitive_terms = ["private-name"]
default_publication_route = "staged"

[accounts]
github = "example-account"
```

### Commit messages

Add an optional `[commit_message]` table to the global `config.toml` to set defaults for every
project:

```toml
[commit_message]
tagged = "Release {tag}"
untagged = "Update published files"
```

A project can override either key in its own configuration. This example changes only tagged
publications for one project:

```toml
[commit_message]
tagged = "Version {version}"
```

For each key, pubanon uses `prepare --message` when given, then the project setting, then the
global setting, then the built-in default. The built-in messages are `release: {tag}` for a tagged
publication and `publication update` for an untagged one. A project setting only `tagged` still
uses the global `untagged` value. These settings apply to new preparations; replacements keep their
existing message unless you pass `--message`.

`tagged` accepts `{tag}` (such as `v0.2.0`) and `{version}` (such as `0.2.0`). `untagged` accepts
no fields. Use `{{` and `}}` for literal braces. Messages may span several lines for a body or
trailers. pubanon removes trailing line breaks from a configured template before writing the
commit; other whitespace remains. Edit the table by hand, then run `pubanon doctor` to validate the
global configuration. `setup --edit` keeps the table when saving other changes.

### Global exclusions

To keep files or directories out of every project, run `pubanon setup --edit` and choose **Files**
or **Directories**. You can also add the optional top-level fields to the global configuration,
above `[accounts]`:

```toml
ignored_files = ["LOCAL.md", "notes/private.txt"]
ignored_directories = ["docs/private", "notes/confidential"]
```

Entries are literal paths, not glob patterns, and ignore case. A bare filename matches at any
depth; a path containing `/` matches that exact path from the source root. A directory entry covers
everything beneath it, with or without a trailing slash. Exclusions override project includes,
apply to existing and new projects, and leave your source files untouched. Changing either list
requires prepared snapshots to be prepared again; published history is unchanged.

Exclusions don't stop content checks on the files that remain. To block references to private names
or paths in published text, including build configuration, add them to `sensitive_terms`.

### Initialize a project

From a private source repository with no Git remote, run:

```sh
pubanon init
```

Choose which files to publish, the GitHub destination, expected visibility, repository settings,
publication route, and CI checks. The route starts from your global default and is saved for this
project; later changes to that default affect only new projects. The final review also shows the
derived private staging destination. Both publication and staging use `main`; your private source
branch can have any name.

Initialization first asks about paths whose names suggest credential files. A name match is a
warning, not proof of a secret; you choose whether to include each file. Included files still pass
the content checks. The preview groups included files by directory and shows exclusions with their
reasons; exclusions take precedence over includes. Subdirectories of `docs/` and the `.agents/`
directory appear as separate groups. Files named `.pubanon.toml` are always excluded because that
name is reserved for pubanon configuration.

Choose continue to accept the selection or review to adjust it. Review walks through the undecided
files one group at a time. Include or exclude a group as offered, or choose customize to type your
own pattern, such as `tests/fixtures/**`, and include or exclude the files it matches. Each choice
reports how many files it decided. Because exclusions take precedence, pubanon lists any already
included files that an exclusion would also remove and asks before excluding them. Back undoes the
preceding choice, including a credential-like filename decision. The complete list of filenames,
without their contents, is saved locally. `--verbose` adds the include and exclude patterns, the
first two matches for each include pattern, and the path where the list was saved.

Set the repository description and whether issues, pull requests, discussions, projects, and the
wiki are enabled. Pull requests and discussions default to off. pubanon publishes from the private
source and does not merge on GitHub. These settings are saved under `[settings]` and checked before
reveal.

Initialization finds CI check names in the committed workflows selected for publication, expanding
static matrix jobs into individual names. The checks you choose are the results pubanon waits for;
your workflows still decide which jobs GitHub runs.

On the staged route, initialization also connects a CI workflow when exactly one selected workflow
qualifies: it declares boolean `workflow_dispatch` inputs named `hosted` and `full_tests`, and it
runs on every branch push. pubanon saves it as `ci_workflow`, lists only its jobs, and requests its
runs from `stage push`. When several workflows qualify, none is connected; choose one with
`pubanon init --edit`. See [Request CI from stage push](#request-ci-from-stage-push).

pubanon flags checks that might not run: those from workflows without a branch-push trigger, with
branch or path filters, or with a job condition. **all listed** is always offered. It is the
default when every job was resolved and either a workflow is connected or nothing is flagged;
otherwise, choose explicitly. A manual release job, for example, usually doesn't belong.

Discovery can't evaluate every GitHub expression. Checks from unsupported expressions and reusable
workflows are marked unresolved, so enter their names yourself: separate names with commas and
quote a name that contains one, such as `quality, "test (ubuntu-24.04, 3.13)"`. An empty list means
there are no checks to wait for; on the staged route, review the staging repository yourself.

Initialization also asks whether to create a release page for each published version: yes, draft,
or no. The answer starts as no. Answer draft when your release workflow publishes each page after
uploading the package. The prompt explains when pages are created on your route. See
[GitHub release pages](#github-release-pages).

Initialization also offers local source tags, on by default for new projects. They show a marker
such as `pubanon/v0.1.0` beside the source commit in your usual Git viewer. Choose no to leave them
off. The choice is saved per project; existing settings and resumed answers are preserved.

The final review lets you save, edit, or cancel. An invalid answer returns you to that field
without losing earlier choices. When publishing pubanon's own source, pass `--self-publication` so
its name is not treated as a hygiene term; your configured terms and exclusions still apply.
Rerunning `pubanon init --self-publication` enables it for an existing project. To turn it off, set
`self_publication = false` in the project's configuration file.

Each accepted answer is saved in a setup draft. After quitting or pressing Ctrl-C, rerun the
displayed command and choose Resume. Start over discards the draft; quitting the resume menu keeps
it. If the source commit changed, initialization keeps your choices and shows the new selection for
review. The project configuration is written only when you choose Save at the final review.

### Edit an existing project

Run `pubanon init --edit` from the registered source to review the project's saved settings. Choose
**edit** to change the expected visibility, repository description and features, route, CI workflow
and required checks, release pages, or source tags, then **save**. Nothing is written until you
save, and saving without changes leaves the file untouched. Changing the expected visibility, the
CI workflow, or required checks invalidates a prepared snapshot, and pubanon warns when you do.

The **checks** section first shows the connected CI workflow. Keep it, connect a qualifying
workflow from the selected files, or choose none; then choose the checks for the result. On the
direct route, pubanon suggests none.

To change which files are included, run `pubanon prepare` and choose **Review** at the selection
prompt. If a snapshot is already prepared, discard it first with `pubanon discard`. After accepting
the new selection, preparation checks and saves the snapshot for inspection.

For other settings, such as the destination, workflows, package observation, page titles and notes,
and commit messages, open `~/.config/pubanon/projects/NAME.toml` in your editor, using the
configured XDG location if different. To find the exact path, run `pubanon init --verbose` from the
registered source directory. For an existing project, this prints the path and refreshes
registration; it does not reopen the setup questions.

`pubanon init --publication-route staged` or `--publication-route direct` changes only the route
without prompts. This preserves prepared snapshots, staging tracking, and publication history.
Rerunning `pubanon init` without the flag keeps the project's route.

Use `pubanon init --source-tags` to enable local source tags, or `pubanon init --no-source-tags` to
stop creating and updating them. Existing tags remain when disabled. Run `pubanon list` after
enabling the option to mark already published sources. These changes preserve prepared snapshots.

Update the existing fields for the change you want. For example, these are top-level fields, above
the first table heading such as `[settings]`:

```toml
allow = ["src/*", "pyproject.toml", "README.md", "LICENSE"]
deny = ["docs/private/*"]
required_checks = ["quality", "hosted-validation"]
ci_workflow = "ci.yml"
publication_route = "staged"
source_tags = false
```

Keep the other saved fields. `publication_route` is required and accepts `staged` or `direct`.
Changing it does not rebuild or change a prepared snapshot. `allow` and `deny` use case-sensitive
patterns against the whole relative path, with forward slashes on every platform. `*` can match
across directory separators: `src/*` includes files in nested source directories. Deny patterns
take precedence. pubanon's reserved `.pubanon.toml` filename stays excluded.

The optional `ci_workflow` names one `.yml` or `.yaml` file under `.github/workflows/`, such as
`ci.yml`; `pubanon init` and `pubanon init --edit` set it for you. The filename must start with an
ASCII letter, digit, underscore, or hyphen; later characters may also be periods. When set, pubanon
reads the workflow's requested runs and uses `required_checks` as exact job names; changing
`ci_workflow` requires a fresh `prepare`. Without it, the existing check-run behavior applies.

The staging repository is named after the destination with `-stage` appended; set the optional
top-level `stage_repo` to choose another name. Add [named workflows](#reusable-workflows) and
[package observation](#package-observation) as their own TOML tables. Edit the existing
`[settings]` table for repository features and description; `default_branch` must remain `"main"`.
For example:

```toml
[settings]
description = "Public project"
default_branch = "main"
issues = true
wiki = false
projects = false
pull_requests = false
discussions = false
```

Both `pull_requests` and `discussions` are required in existing project configurations. Set each to
`true` or `false` when updating a configuration that does not yet have them.

`publish` shows the configured description in its preview. After the publication checks and your
confirmation, it updates the final repository's description if needed and verifies the result
before uploading code. This also applies to repositories created outside pubanon. An empty
description clears the existing description. If the update fails or is interrupted, no code is
uploaded and the prepared snapshot is kept for retry with `pubanon publish`. The description may
already have changed; it also remains changed if the later code upload fails.

The other repository settings are checked against the final repository before reveal. A new staging
repository starts with the configured settings, but later edits are not copied to it.

CI runs separately in the staging and final repositories. Staging receives `main` without tags, so
tag-only workflows don't run there. Keep deployment and package-upload triggers limited to the
repository that should run them.

Use the optional [`[release_page]` table](#change-the-defaults) to change the page created for a
tagged publication. `init` and `init --edit` save only its `enabled` and `draft` keys. Use the
optional [per-project `[commit_message]` table](#commit-messages) to override either global message
default for new preparations.

After saving, run `pubanon status` to check the project and any retained snapshot. Changes to file
selection, publication identity, destination, required checks, or `ci_workflow` require a fresh
snapshot. When instructed, discard the old snapshot and prepare again, then inspect the selected
files. See [when to prepare again](operations.md#when-a-snapshot-must-be-prepared-again) for the
complete rules. Use `pubanon setup --edit` for global identity, sensitive terms, exclusions, and
account changes.

## Publication identity

The global configuration holds one publication identity: the name and email pubanon uses as author
and committer of every publication commit. It may be an alias or a real name. pubanon does not
check which, and it does not sign commits or request a host-verified badge.

The identity must not appear in `sensitive_terms` or in a project's `extra_terms`. During setup,
`doctor` checks it against global `sensitive_terms` and machine-private paths. For a project,
`prepare` checks it against `sensitive_terms`, `extra_terms`, private paths, and hygiene terms. A
refusal names every matched rule source without printing the matched term.

Every published commit is audited against the configured identity. A changed name or email blocks
ordinary publication, `repo restore` in either direction, and reveal. Restore the original
identity, or follow [correcting published history](#correcting-published-history) and use
`pubanon prepare --replace all` to make an explicit full replacement under the changed identity.
The replacement does not erase old content from existing clones or caches. Choose an email you can
keep, such as a host no-reply address.

One configuration root holds one identity. To publish some projects anonymously and others under a
real name, use separate configuration and data roots. In a POSIX shell:

```sh
XDG_CONFIG_HOME=~/.config/pubanon-public XDG_DATA_HOME=~/.local/share/pubanon-public pubanon setup
```

Each root keeps its own global configuration, project registry, and published-history store. Under
a real name, the [residual risks](operations.md#residual-risks) still apply to every configured
sensitive term; they no longer apply to the identity itself.

## Direct publication

Select the `direct` route to publish without private staging. The route is separate from the final
repository's visibility: for this example, choose `direct` and `public` visibility during `init`.
`repo create` uses the saved visibility.

```sh
pubanon init --publication-route direct
pubanon repo create
pubanon prepare
pubanon inspect
pubanon publish
```

`publish` needs an existing final repository; it neither creates one nor changes its visibility.
The same `init` command switches an existing project to `direct`; leftover staging tracking does
not then require staging.

## Private-first CI

Select the `staged` route to test the exact candidate in a separate private repository before the
final upload. How pubanon waits for CI depends on your setup:

| Setup        | You provide                                           | What `stage push` and `stage watch` do                                              |
| ------------ | ----------------------------------------------------- | ----------------------------------------------------------------------------------- |
| No CI        | Nothing                                               | `stage push` sends the snapshot; review the staging repository yourself             |
| Push CI      | Any workflow that runs on push, and `required_checks` | The push starts CI; the watch waits for the named checks                            |
| Requested CI | A workflow that `init` connects as `ci_workflow`      | `stage push` requests a run; the watch and `publish` judge the latest requested run |

Choose the CI workflow and checks during `pubanon init`, or later with `pubanon init --edit`. To
name the staging repository yourself, set `stage_repo` in your
[project configuration](#edit-an-existing-project). Then run:

```sh
pubanon prepare
pubanon inspect
pubanon stage push
pubanon stage watch
pubanon publish
```

The first `stage push` asks before creating the private staging repository, and `--yes` cannot
approve that. Later pushes update its `main` branch and never send tags. `stage status` reports the
staged snapshot's current check results without waiting; `stage watch` waits for them. `publish`
reads the results once for the exact staged commit, and pending or failed checks stop it. Without
`ci_workflow`, an empty `required_checks` list means you can skip the watch and review staging
yourself: pubanon still verifies the staged commit but does not report CI success. The final
repository needs its own checks, because staging results stay in the staging repository.

### Request CI from stage push

A connected workflow lets each `stage push` start exactly one run. The workflow must:

- declare boolean `workflow_dispatch` inputs named `hosted` and `full_tests`;
- skip push runs when the repository variable `CI_RUN_ON_PUSH` is `false`, so a stage push does not
  start a second run; and
- run on every branch push, because `repo watch` and `repo reveal` judge its push run in the final
  repository.

To start from scratch, copy the [starter workflow](examples/ci.yml) to `.github/workflows/ci.yml`,
replace its marked test step with your project's test commands, and commit. The placeholder step
fails until you replace it. For a new project, `pubanon init` then selects the file and connects
the workflow in one pass.

For a registered project, the workflow must be in the file selection before `pubanon init --edit`
can offer it. Run `pubanon prepare`, after `pubanon discard` if a snapshot is prepared, and publish
the new workflow file when asked. Then run `pubanon init --edit`, connect the workflow and choose
its checks in the **checks** section, and save. The connection changes the snapshot's settings, so
run `pubanon discard` and `pubanon prepare` again before staging.

A plain `pubanon stage push` requests a quick run on configured local runners, with slow tests
omitted where the workflow defines that mode. Use `pubanon stage push --full-tests` to request
every test on configured local runners, or `pubanon stage push --hosted` to request a complete run
on GitHub-hosted runners. The options can be combined; the workflow receives both `full_tests=true`
and `hosted=true` and decides how to route that run. Without a configured local runner, the
workflow may use a hosted runner. The options require `ci_workflow`.

For a connected project, pubanon creates `CI_RUN_ON_PUSH=false` in the staging repository when
needed, pushes the exact prepared commit, and requests one run of the connected workflow. The
starter workflow's `hosted-validation` job runs only for hosted runs and passes only when every
test job passed. Requiring it makes publication wait for a hosted pass; leave it out if a passing
quick run should be enough.

`stage status`, `stage watch`, and `publish` use the requested runs for the staged commit. pubanon
publishes only when the most recent requested run for that exact commit passed the required jobs. A
newer run replaces an older pass as evidence. For example, a quick local run after a hosted pass
blocks publication if `hosted-validation` is required, because the quick run skips that job.
Request `pubanon stage push --hosted` again and wait for it to pass. `repo watch` applies the same
latest-run rule to push CI in the final repository.

`stage push` refuses two setups that cannot pass. With a connected workflow, each required check
must name one of its jobs; correct the list with `pubanon init --edit`. Without one, when only
workflows that declare pubanon's inputs define the required checks and the staging repository's
`CI_RUN_ON_PUSH` is `false`, no push run can start them; connect the workflow with
`pubanon init --edit`, or delete the variable. Connecting a workflow or changing checks needs a new
snapshot.

Checks may take time to appear. The watches report progress until a timeout; Ctrl-C stops waiting
without changing the snapshot or GitHub, and rerunning the same watch continues. See
[watch recovery](operations.md#retrying-an-interrupted-command) for missing, ambiguous, or failed
checks and timeout options.

Keep the staging repository for later releases. `stage repair` reconnects local tracking when it
recognizes the repository's `main` branch; a repository that still has an old `stage` branch must
be corrected on GitHub first. `stage forget` removes local tracking but keeps the repository and
the route, so a staged project must push or repair staging again before publishing. To stop
staging, switch to `direct` with `pubanon init --publication-route direct`. `stage delete` deletes
the staging repository. See [cleanup](operations.md#cleanup) for details.

### Use your own runners

By default, every run uses GitHub-hosted runners, which use Actions minutes in a private staging
repository. To run quick and full-test runs on your own machines,
[add self-hosted runners](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
to the staging repository and give each a label. Then set the staging repository's variables
`CI_LINUX_RUNNER`, `CI_MACOS_RUNNER`, and `CI_WINDOWS_RUNNER` to those labels. The starter workflow
routes only requested runs in a private repository to self-hosted runners; `--hosted` runs and
every run in the final repository stay on GitHub-hosted runners.

## Reusable workflows

Named workflows let you test privately, review the results, and then publish the tested snapshot.
Add them to your [project configuration](#edit-an-existing-project), with `required_checks` or
`ci_workflow` set for the CI waits:

```toml
[workflow.stage]
stages = ["prepare", "inspect", "stage push", "stage watch"]

[workflow.ship]
stages = ["publish", "repo watch"]
```

A workflow uses each stage at most once, in this order: `prepare`, `inspect`, `stage push`,
`stage watch`, `publish`, `repo watch`, `repo reveal`, and `package watch`. The names `stage` and
`ship` are your choice, and you can define several workflows.

First, prepare the candidate, send it to staging, and wait for CI. Preparation reads the version
from your committed `pyproject.toml` when there is one:

```sh
pubanon run stage
```

After staging CI succeeds and you have reviewed the result, publish that candidate and wait for the
final repository's CI:

```sh
pubanon run ship
```

`ship` publishes the same snapshot you tested, with the candidate's version, or without a tag if it
has none. With one version tag and release pages on, it creates a release page; the preview shows
when a page will be created, kept, or skipped. For a project without a committed package version,
pass `--version` to `run stage` to get a tag. A workflow containing `publish` needs a prepared
snapshot or an explicit `--version`: after publication, `pubanon run ship --version 0.1.0` can
create a missing page and resume the remaining checks for that release when release pages are on or
the rerun passes `--release-page` or `--release-notes-file`.

`run` first prints its stages and every change it could make on GitHub, including a release page
when its `publish` stage may create one. Creating the staging repository, pushing to it,
publishing, and revealing each keep their own approval. Before publication, the preview shows the
full commit IDs, author and committer, message, file summary, destination, visibility, and refs.
Declining keeps the snapshot and its review copy; run `pubanon inspect` to review it by hand, then
follow its next step.

With `--yes`, stages continue after their previews without asking, and the inspection stage does
not pause for you to review files. For a hands-on review, end the workflow at `inspect` or run the
commands separately. `--yes` confirms only the declared stages. It cannot answer policy or new-path
decisions, accept a changed source branch or tag, approve creating the staging repository,
acknowledge a historical exposure, accept sensitive terms in published history at reveal, or make
the repository public.

## Non-package workflows

pubanon can publish any supported Git source snapshot. A project without a package can omit the
`[package]` table and still publish Git history, wait for CI, and reveal a repository.

Use plain `pubanon prepare` for an untagged snapshot or [choose a version tag](#version-tags)
explicitly. Configure `required_checks` to use `pubanon repo watch`. A
[named workflow](#reusable-workflows) can end after CI; package observation is optional.

## Package observation

Use package observation when your release workflow uploads to PyPI and you want to confirm that the
files arrived. pubanon only observes; it never uploads packages. Add this table to your
[project configuration](#edit-an-existing-project):

```toml
[package]
index = "pypi"
name = "public-package"
release_checks = ["Publish to PyPI"]
```

Set `name` to the exact PyPI distribution name. `release_checks` lists at least one check that
gates the package upload, by its job or check name rather than the workflow title.
`pubanon package watch` looks at PyPI only after every release check succeeds. Without `--version`,
it selects the highest `X.Y.Z` release tag, even if a newer commit has a lower version; `--version`
selects that exact release. A version with no uploaded files is incomplete: the watch keeps
waiting, and `repo reveal` refuses to make the repository public until files appear. When the files
arrive, the watch lists them. If the watch reports a check from the
[starter release workflows](#publish-pages-after-the-package-upload) as skipped, see that section.

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

After preparation, run `pubanon inspect` and follow its route-aware next step. A staged project
must stage the resulting tip and satisfy configured checks before `pubanon publish`; a direct
project can publish after inspection. Preparation uses your current committed source by default and
does not change GitHub. The summary explains which history will be replaced. These commands have
the same meaning for private and public destinations.

For a versioned package, commit a new version before preparing an ordinary new release. If its tag
already exists, plain `prepare` stops and explains how to select a replacement instead.

While iterating privately toward an initial release, commit each correction and repeat:

```sh
pubanon prepare --replace all --message "Initial release."
pubanon inspect
pubanon stage push
pubanon stage watch
pubanon publish
```

Adjust the staging commands for your route and checks as in the [quick path](#quick-path).

Each successful publication starts the published history over with one commit and the intended
version tag, if any. You can keep the same package version while correcting that initial release.
Your private development history stays intact.

Once others use a public repository, usually publish a new release instead of replacing history.
Ordinary publication and `repo restore --to remote` do not rewrite published history. Replacement
can be used for private or public repositories, but may disrupt existing clones and consumers.
Existing clones, caches, and host-retained objects may still contain the old content. Moved Git
tags do not replace GitHub Release assets, Actions logs or artifacts, or packages already uploaded
by your release workflow. To start a private repository over without them, see
[Recreating the final repository](#recreating-the-final-repository).

GitHub refuses to move or delete the tag of an [immutable release](#immutable-releases), so
`publish` stops before pushing a replacement that would.

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
`action = "unresolved"` entry. pubanon refuses missing required files, invalid actions, occupied
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
complete files report contains changes omitted from a bounded terminal preview. Review copies are
read-only and match the selected saved snapshot. On the staged route, `pubanon stage push` sends
the resulting tip only, and publication requires private staging and configured checks for that
same tip. Follow inspection's next step before the publication commands below.

```sh
pubanon publish
pubanon repo watch --version 0.3.0
```

In the example, `0.3.0` stands for the last replayed release; use your actual moved tag.
Publication shows the destination, which releases are corrected and replayed, the ref changes, and
a warning about public history, then asks once. It pushes the result in a single update that fails
if GitHub no longer matches the saved published state. GitHub branch rules may still refuse the
rewrite, and pubanon cannot detect them in advance. A successful publication says nothing about CI,
so watch the published version next. If the push outcome is unknown, run
`pubanon publish --resolve`, which checks GitHub without pushing again. `run` refuses a prepared
replacement, so finish it with the individual commands.

`list` shows superseded snapshots with state `superseded`. They remain locally inspectable by
commit prefix while preserved. A derived release's original source revision is an input to replay,
not an exact private-source copy of its resulting published tree. See the operations guide for
[source recovery](operations.md#published-history-and-source-recovery) and
[cleanup](operations.md#cleanup).

## Existing destinations and restored history

The final repository may already exist before your first publication, as long as it is empty.
Follow the [quick path](#quick-path), including `repo create`. For a pre-created repository, that
command checks its identity, visibility, settings, and absence of branches and tags without
changing GitHub. It then suggests `prepare`, or the next step for an already prepared snapshot. If
settings differ, correct them on GitHub or in the saved configuration and rerun `repo create`. The
default branch is checked after publication because an empty repository has no branch yet.

Before the first release, `publish` checks emptiness again. It refuses a repository with existing
branches or tags rather than importing, replacing, or overwriting them. `repo verify` is for
checking previously published history; it is not needed to connect a pre-created empty repository.

For a recreated or restored repository, retained local published history may be sent to a host that
is empty or an exact prefix of that history:

```sh
pubanon repo restore --to remote
```

It checks that GitHub is empty or strictly behind, then sends the saved published history. It does
not use or change a prepared snapshot. If that snapshot is a correction, see
[Recreating the final repository](#recreating-the-final-repository).

Run `pubanon audit` to check your saved published history, including the author and committer names
and emails, against your current terms. `pubanon audit --remote` first checks that history against
GitHub. If a name becomes sensitive after publication, `audit` reports the exposure, `publish` and
`repo restore --to remote` require you to acknowledge it, and `repo reveal` lists it and asks
before making it public. `--yes` cannot answer either question. See
[Sensitive terms in published history](#sensitive-terms-in-published-history).

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
pubanon repo restore --to local
```

Here, `local` means saved publication history, not the source checkout. Use restoration only when
GitHub has the history you want to keep locally. It previews the local changes and asks before
replacing them. Matching history structure and identity cannot prove that pubanon originally
created it. See [history repair](operations.md#repairing-the-local-history-copy) for verification,
partial-success recovery, and limits.

## Recreating the final repository

Replacing published history does not erase it from GitHub: the old commits can stay reachable, and
their CI runs stay listed. While the final repository is private, you can delete and recreate it to
start over without them.

A recreated repository cannot reuse the version of a release page that was immutable, so restoring
that tag fails.

Publish any prepared correction first, so the saved history you restore is the corrected one:

```sh
pubanon publish
gh repo delete OWNER/REPO
pubanon repo create
pubanon repo restore --to remote
pubanon release create v0.1.0
```

`gh repo delete` asks you to confirm; you can also delete the repository in its GitHub settings.
`repo create` applies the settings saved in your project. Restoring sends branches and tags only,
so run `release create` for each release page you want back, and set up again anything you
configured only on GitHub, such as secrets or branch rules.

### If you already recreated it

A prepared correction expects GitHub to still have the history it corrects, so `publish` stops when
the new repository is empty. Restore the saved history, then publish the correction:

```sh
pubanon repo restore --to remote
pubanon publish
```

This briefly puts the history you are correcting into the new repository, and CI may run on it. If
the history you are correcting contains terms you have since added, the restore asks you to
acknowledge them. To remove that history from the final repository, recreate it once more now that
the correction is published:

```sh
gh repo delete OWNER/REPO
pubanon repo create
pubanon repo restore --to remote
pubanon release create v0.1.0
```

## Sensitive terms in published history

A term you add after publishing can match commits already in the final repository. New snapshots
cannot contain it, but those earlier commits still do.

`publish` and `repo restore --to remote` summarize the matches and ask you to acknowledge them
once; later runs for a private repository remind you in one line. `pubanon audit` lists every
finding and shows its first match in context; additional occurrences may exist. `repo reveal` lists
the findings again and asks before making them public. `--yes` answers none of these questions.

The same terms can appear in Actions logs. `pubanon audit --remote` checks the details of every
workflow run, the runner of every job, and every job log, lists matches by run, and offers to
delete the logs of matching runs. Deleting asks again and cannot be undone. When the repository is
already public, deleting stops GitHub from serving the logs, but copies made meanwhile remain.

| Situation                               | Choice                                            | Cost                                                             |
| --------------------------------------- | ------------------------------------------------- | ---------------------------------------------------------------- |
| Private; the matches can be public      | Keep the history and answer yes at reveal         | The matches become public with the repository                    |
| Private; the matches must not be public | Correct the history, then recreate the repository | Release pages, Actions history, and Activity are lost; tags move |
| Already public                          | Replace history only if new clones matter         | Existing clones, forks, and caches keep the old history          |

Immutable release pages can block these corrections: a replacement cannot move or remove an
affected page's tag, and a recreated repository cannot reuse that page's version.

Correcting is cheapest early, before more releases and release pages build on the affected history.
To correct from the first affected release, here `v0.1.0`:

1. Find its original source with `pubanon show v0.1.0 --verbose`.

2. Commit the correction on top of that source revision, keeping the same package version.

3. Prepare and publish the correction. Later releases are replayed on top of it; see
   [Correcting published history](#correcting-published-history).

   ```sh
   pubanon prepare --replace v0.1.0 --source REV
   pubanon inspect
   pubanon publish
   ```

   On the staged route, stage and watch first, as inspection's next step shows.

4. Save each release page's notes to a file before deleting anything. `pubanon release show` is for
   reading, not backup: it adds metadata and escapes the text. Use GitHub's CLI instead:

   ```sh
   gh release view v0.1.0 --repo OWNER/REPO --json body --jq .body >notes-v0.1.0.md
   ```

5. Delete and recreate the final repository and restore the corrected history as in
   [Recreating the final repository](#recreating-the-final-repository). Recreate each page from its
   saved notes, adding `--title` if you had changed the title:

   ```sh
   pubanon release create v0.1.0 --notes-file notes-v0.1.0.md
   ```

Recreating starts a fresh repository with corrected history. It does not prove that the old content
is erased: GitHub can restore some deleted repositories for 90 days, and copies may remain in
clones, forks, caches, and package indexes. Packages already uploaded keep their contents.

## Accepting an outside fix

Incorporate outside changes in the private source and publish a new release through pubanon.
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

Release through pubanon as usual. The public commit carries the publication identity. Credit the
contributor in the controlled message with the same trailer:

```sh
pubanon prepare --version 0.2.0 --message "release: v0.2.0

Co-authored-by: Contributor Name <contributor@example.com>"
pubanon publish
```

pubanon checks the release message for sensitive terms too. It refuses a contributor trailer that
matches one.

Close the pull request with an explanation:

```sh
gh pr close --repo OWNER/REPO NUMBER --comment "Merged privately and released in v0.2.0. This repository publishes verified releases and does not merge on the host."
```

A `CONTRIBUTING.md` stating that policy is ordinary project content.

### If the host changed outside pubanon

When `publish` reports that the remote state differs from what the snapshot expects, or
`audit --remote` reports that host refs do not match local published state, pubanon shows the
expected and observed references. It does not know why they differ. Possible causes include a pull
request merged on the host, a push from another machine or configuration root, a change made in the
host's web interface, or a [recreated repository](#recreating-the-final-repository).

Ordinary publication does not remove commits from host history. Explicit
[publication replacement](#correcting-published-history) can rewrite the expected published refs,
but does not bypass a mismatch caused by outside changes. If you want the public branch back at the
last published state, restore it outside pubanon, using the last published commit reported by
`pubanon status --verbose`. After that, `pubanon status --remote` reports a match and publication
resumes. If the local published-history store is what is wrong, `pubanon repo restore --to local`
rebuilds it from a host that passes verification. Repair checks history structure and identity, but
cannot prove that pubanon originally created the commits.

## Reading the terminal interface

Output gives the context, the facts needed to judge the result, one result line, and a `Next:`
command when there is another step. Short results need no heading. Previews show what you are about
to change; warnings explain consequences such as replaced history or checks not performed.

Headings are bold cyan, field labels are bold, successful results are green, warnings and
cancellations are yellow, and errors are bold red. Questions are bold and choices and commands are
cyan. Secondary notes are dim. Messages use words rather than status symbols and remain clear
without color. `--color auto` (the default) enables styling on terminals and respects `NO_COLOR`;
redirected output stays plain. `--color always` forces styling, including in pipes, and overrides
`NO_COLOR`. `--color never` disables color and bolding.

Use `-v` or `--verbose` for diagnostic detail: full commit IDs, Git references, internal paths, and
comparisons of saved and observed state. It adds to the normal output; warnings, unknown outcomes,
and recovery commands remain visible without it. Presentation options work before or after the
command. Commit IDs normally use 12 characters. Publication timestamps use local time with seconds
and a timezone abbreviation. Displayed paths under your home directory start with `~`; paths in
commands to copy and run stay absolute.

Declining a confirmation reports `Canceled.` and returns status 130, as do quitting a menu and
pressing Ctrl-C before a change starts. If a change may already have happened, pubanon reports the
known outcome and the recovery step. See
[input and cancellation](operations.md#input-and-cancellation).

Inside a registered source repository or its subdirectories, commands select that project
automatically. Use `-p NAME` (or `--project NAME`) before or after a command when working elsewhere
or selecting another project.

## Disconnecting an alternate object store

If pubanon reports that your source borrows Git objects from another repository, follow the
[disconnection procedure](operations.md#disconnecting-an-alternate-object-store). It copies and
verifies the borrowed objects before removing the connection. Keep the original object store
available until verification succeeds.
