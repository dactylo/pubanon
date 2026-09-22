# Pubanon operations guide

This guide helps you recover interrupted work, inspect or remove saved snapshots, repair local
history, and back up the private source. If the source, prepared release, configuration, account,
or GitHub repository no longer matches the saved state, Pubanon stops and reports what needs
attention. See the [operator guide](guide.md) for complete publication paths.

## Find the task

- [Recover an interrupted command](#retrying-an-interrupted-command)
- [Inspect a saved snapshot](#inspection)
- [Clean up local or staging data](#cleanup)
- [Verify a publication](#post-publication-verification)
- [Repair the local published-history copy](#repairing-the-local-history-copy)
- [Back up or restore the private source](#private-source-backups)
- [Disconnect an alternate Git object store](#disconnecting-an-alternate-object-store)
- [Check what a command changes](#what-commands-change)

## Recover first

- If Pubanon reports that no push was attempted, correct the reported problem and rerun the same
  command.
- If a push may have been sent but Pubanon could not verify the result, run
  `pubanon publish --resolve`. Do not resend or discard the snapshot first.
- If Pubanon verified the publication but could not finish its local records, correct the local
  write problem and run `pubanon publish --resolve`.
- For replacement conflicts, interrupted watches, reveal, staging, and other cases, follow the
  [detailed recovery steps](#retrying-an-interrupted-command).

## What commands change

`pubanon repo create` uses the visibility saved during setup and shows it before confirmation.
Optional `--private` or `--public` flags must match that setting; they do not override it.

The table lists changes to GitHub and the optional login session. Each command previews the target
and change before it runs.

| Command                                | External change                                                                                            | What stays unchanged                                                     |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `pubanon setup` (selected login only)  | Starts the GitHub CLI browser login you select; `gh` may store credentials and select HTTPS.               | No repository creation, push, visibility change, or `gh auth setup-git`. |
| `pubanon repo create`                  | Creates the configured empty repository with its saved visibility.                                         | No push or visibility change.                                            |
| `pubanon publish`                      | Atomically publishes the snapshot; an explicit replacement rewrites `main` and its planned tags.           | No repository creation, visibility change, staging, or package upload.   |
| `pubanon publish --sync`               | Pushes retained published history to an empty or exact-prefix repository.                                  | No new snapshot, imported foreign history, or tag movement.              |
| `pubanon release VERSION`              | Prepares, summarizes, and publishes a release; only the publish step changes GitHub.                       | No repository creation, reveal, staging, CI wait, or package upload.     |
| `pubanon run NAME [--version VERSION]` | Performs the GitHub changes named by the workflow stages.                                                  | No undeclared or later stage; `--yes` cannot add one.                    |
| `pubanon repo reveal`                  | Makes the configured private repository public after checks and acknowledgment.                            | No branch or tag push, and no package upload.                            |
| `pubanon stage push`                   | Creates the configured private staging repository on confirmed first use, then updates its staging branch. | No final-repository push, tag, public visibility, or package upload.     |
| `pubanon stage clean`                  | Deletes the recorded staging repository, or the configured one when no record exists.                      | Refuses the final repository; no change to local published history.      |

Apart from the selected GitHub CLI login described above, `setup`, `init`, `prepare`, `inspect`,
and `repo verify --repair-store` change local Pubanon configuration or data only. `publish` creates
a private source bookmark before pushing; `publish --resolve` can create one while recording an
older snapshot. `discard` and `delete` remove their owned local snapshot data and source bookmarks.
These operations leave source branches, ordinary tags, HEAD, index, and working files unchanged.
`doctor`, `status`, `repo verify`, `stage status`, `watch ci`, `watch package`, and
`publish --resolve` are read-only outside the machine. `doctor`, remote status and verification,
the watches, and `publish --resolve` may contact a service, but they do not mutate it. Pubanon
never publishes a package; the project's release workflow handles the upload.

## GitHub login during setup

Setup probes the current account with captured output. Pubanon never prints that raw output. If the
probe fails, setup does not assume that you are logged out: it offers retry, explicit login, or
cancel. Login runs this GitHub CLI operation without a shell:

```sh
gh auth login --hostname github.com --git-protocol https --web
```

Pubanon states that it is handing over the terminal, then starts it. During this one selected
session, `gh` owns the terminal, browser authorization, output, credential storage, and HTTPS
protocol selection. Pubanon does not collect a token, request insecure storage, upload SSH keys, or
run `gh auth setup-git`.

Setup verifies the account again after login, even when `gh` exits successfully, and saves Pubanon
configuration only after showing the exact identity, account, and every sensitive term. If setup
stops after `gh` completed authentication, that authentication and credential change cannot be
rolled back by Pubanon. Rerun `pubanon setup`; it will recheck the account and resume. If saving
succeeded but the final readiness check failed, the valid configuration remains saved and the rerun
checks it again. On a completed configuration, rerunning setup preserves the bytes but may repair
owner-only POSIX mode bits. It shows the public identity and performs readiness checks. Use
`pubanon setup --edit` to review and correct identity, numbered sensitive terms, or the expected
account. Use `pubanon doctor` when you need the same checks without a setup repair.

## Registry and draft recovery

The project registry records each canonical source path and its project name in JSON. If Pubanon
reports an invalid registry, inspect the file path in the error. Restore a known backup of that
file, or reconstruct the current JSON mapping from the known `source_path` and `name` in each saved
project configuration. Keep the damaged file until you have confirmed the replacement. Commands
that accept `--project NAME` can select an existing project explicitly while the registry is being
repaired. Pubanon does not silently reset a damaged registry.

Registry and guided-init draft saves replace a synced temporary file and then sync its parent
directory. A failure before replacement leaves the previous file intact. If the directory sync
fails after replacement, the error says the new file may have been saved; inspect the file before
retrying.

## Input and cancellation

For menus and yes/no questions, press the bracketed letter; no Enter is needed. For text fields,
type your answer and press Enter. Menu letters are case-insensitive. An answer Pubanon does not
recognize is explained and asked again; a typo never ends a command or discards earlier answers. A
blank answer takes the default shown in brackets; questions without a default are asked again.
Questions that publish, change visibility, or acknowledge exposure have no default.

Ctrl-C cancels without a traceback and returns status 130. End of input cancels with status 1 and
asks you to rerun in an interactive terminal. Choosing quit or cancel at a menu cancels like
Ctrl-C, with status 130. Cancellation reports what was and was not saved: setup writes nothing
until you save, and completed GitHub authentication stays in effect.

Pubanon never hides what you type and never masks what it shows you. Its privacy check runs on the
content about to be published, not on your terminal. Displayed text escapes terminal control
characters and backslashes so that output cannot alter your terminal.

## Inspection

Build and inspect the exact retained snapshot before publication:

```sh
pubanon prepare
pubanon inspect
```

`inspect` identifies the snapshot, summarizes its changes and checks, and shows the directory to
open for review. There is no Git metadata. On POSIX, its files and directories omit owner-write
permission. On Windows, its files have the read-only attribute, but the directories remain
writable. In either case, Pubanon verifies the directory against the saved snapshot before reuse.
To change what will be published, edit and commit in the source repository, then discard and
prepare again.

Copies live in `<project-data>/inspections/<full-commit-oid>`, so a different snapshot gets a
separate directory. `pubanon status` shows the number of preserved directories. Use
`pubanon inspect --verbose` to list their locations alongside a prepared snapshot. With no prepared
snapshot, `pubanon inspect` lists the directories directly. Open a listed directory in your editor
to review its files.

Before reusing a selected directory, Pubanon verifies its files, paths, symlink targets, and
executable flags against the saved snapshot. If they differ, inspection stops instead of showing
the changed files as the snapshot.

Use `pubanon inspect --refresh` to restore the selected directory from the saved snapshot. It
prints the exact location before replacing its current contents. `pubanon inspect --clean` lists
and removes all known project snapshot directories under `inspections/`.

After publication, `pubanon inspect --published v0.1.0` creates or reuses a directory for the saved
published revision without a prepared snapshot or source checkout. The directory has no Git
metadata. Its files are verified and protected as described above;
`pubanon inspect --published v0.1.0 --refresh` restores it from the saved commit. `inspect --clean`
also removes published copies, and a later `inspect --published` regenerates them when the
publication is still available locally. To browse normal Git commit history, use a separate clone
of the published host repository. The inspection directory is for reviewing the exact saved files.
Builds and tests that write files need a separate writable directory.

For a completed replacement, ordinary `inspect` materializes the resulting tip.
`pubanon inspect --rewritten v0.2.0` selects an intermediate corrected or replayed release by its
moved tag; a rewritten commit prefix also works. This selection is separate from `--published`,
which reads confirmed history. Staging sends the completed tip, not an intermediate inspection.

Use `pubanon status` to see local publication state and changes that affect a snapshot. Add
`--remote` to compare with the host. Before changing a repository on GitHub, Pubanon prints the
exact target and consequences before the command-specific confirmation. `release` and `run` build
and verify the snapshot before final publication approval. A decline preserves the snapshot and
snapshot directories for `pubanon inspect`; you still review each later GitHub change.
`publish --sync` shows the verified destination, published-history range and count, full commit
messages and metadata, and exact refs before it asks to proceed. It uses retained publication
history.

## Published history and source recovery

Prepared, published, and superseded snapshots share the same identity. Read-only snapshot
directories provide the saved files for review. `prepare` saves a snapshot locally and does not
create a source bookmark. Before `publish` pushes, Pubanon bookmarks the snapshot's original source
commit in its source Git repository. A published snapshot also has a private record and an archive
bookmark in the local sanitized Git store. These keep the exact published files and the original
source revision available locally even when a branch is rebased. The source bookmark stays on the
original commit; Pubanon does not infer a replacement from the rewritten branch.

Use `pubanon list` to list locally known snapshots. `pubanon show prepared` displays the saved
candidate; its commit prefix also selects it when unambiguous. For published snapshots, `show`
accepts `latest` for the local published main tip, an exact tag such as `v0.1.0`, or an unambiguous
publication commit prefix. A tag selects its current operational commit; only an absent tag falls
back to saved labels, where multiple matching records are ambiguous. Superseded snapshots have
state `superseded` because they are outside current published main's ancestry; select them by
commit prefix. `pubanon show v0.1.0 --verbose` gives the exact publication and source commit IDs,
saved destination, visibility when published, and local confirmation time. The first confirmation
is stored in UTC and displayed in your local time with seconds and a timezone; it is distinct from
the Git author date. The command provides a concrete `git -C ... worktree add --detach ...` command
for the original source revision when the source repository is available. Run that command yourself
to browse the complete private source revision; run its matching `git -C ... worktree remove ...`
command when finished. Pubanon does not create or remove source worktrees. A source worktree can
retain Git objects after a bookmark is removed.

A replayed release has a derived snapshot record: its original snapshot, known original source
input, correction commit, and explicit conflict resolutions. That source input is not an exact
source tree for the derived result; use `inspect --published` for the exact published files. When
available, each derived release gets its own source bookmark, independent of the original
publication's bookmark. Missing source-location information remains unavailable rather than being
inferred from your current configuration.

History and published inspection read local data; they do not check the host. `PRIVATE` or `PUBLIC`
in `show` means visibility when publication was confirmed. `pubanon status` may show a different
configured visibility, which is an intention. Use `pubanon status --remote` to check the host's
current visibility. Older published snapshots without a private record remain inspectable when
their Git objects are available, but their source and historical visibility are not invented.

The source bookmark protects the original commit and its Git ancestors from ordinary pruning. It is
visible in Git views that include all refs, such as `git log --all`, but is not an ordinary release
tag. Confirmed records have no automatic expiry. Retained ancestry may consume substantial disk
space. The bookmark is not a backup against loss of the source repository or its disk.
`pubanon clean` removes superseded local snapshots in one confirmed operation. For a specific
snapshot, `pubanon delete ID` previews and confirms deletion of its local entry, review directory,
private record, and preservation bookmarks. Use an ID from `pubanon list` to select an old
snapshot; a version tag selects its current publication. `--yes` can confirm either operation. The
snapshot stops appearing in `list` and cannot be selected through `show`, even if its commit
remains reachable through published Git history. The command does not change GitHub or run Git
garbage collection, so it does not promise immediate space savings. Git may retain source objects
through other refs, reflogs, or source worktrees.

`pubanon delete prepared` removes the selected prepared candidate and its review directories. An
unknown publication outcome must be resolved before ordinary deletion. Deleting an original
snapshot leaves a derived snapshot's independent source bookmark; deleting the derived snapshot
leaves the original's bookmark. The same source ancestry may remain protected by either. Pubanon
refuses deletion when the selected local data is needed by a retained replacement or rewrite;
finish it or discard unpublished work first.

## Retrying an interrupted command

The usual recovery is to correct the reported condition and rerun the same command. A rerun
re-verifies current facts and never silently selects new source content, rebuilds a retained
snapshot, changes a planned tag mapping, reuses a package version, or adds a stage. A retained
replacement may move or remove tags only as already reviewed in that snapshot.

- If `prepare` fails before retaining a snapshot, rerunning it makes a new, reported source
  selection.
- If replacement preparation pauses or is interrupted after retaining state, use `pubanon status`,
  then `pubanon prepare --continue`. An interrupted export is regenerated from saved objects and
  its partial files are not read as decisions. In the awaiting phase, edit `tree/` and declare
  every file decision in `resolutions.toml`; refusal preserves those edits. See the guide's
  [conflict procedure](guide.md#correcting-published-history).
- If `publish` fails or you decline before the push, the snapshot remains available. Correct the
  reported condition and rerun `publish`.
- If Pubanon cannot tell whether a sent push succeeded, run `pubanon publish --resolve`. Do not
  resend or discard first. Resolution reads the recorded destination and either records success or
  confirms that nothing changed.
- If Pubanon reports that publication was verified on the host but local finalization is
  incomplete, the publication has landed. Do not publish a new snapshot or discard the recovery
  record. Correct the reported local write failure, then run `pubanon publish --resolve`.
  Resolution safely reapplies local ref updates and removes the snapshot record last, after the
  remaining local writes succeed. Review directories remain on disk.
- Ctrl-C exits with status 130. Before the push, Pubanon reports that no push was attempted. An
  interruption during the push or before authoritative readback leaves the outcome unknown; run
  `pubanon publish --resolve`. If readback already confirmed publication, cancellation during local
  finalization reports that known success and retains the same resolution evidence. If cancellation
  arrives after the final record removal, Pubanon reports completed local finalization; resolution
  is no longer needed.
- If `release VERSION` stops, rerun the same command. It resumes only when the retained snapshot
  still has that version and passes local verification. Resolve any unknown outcome or incomplete
  local finalization first.
- If `run NAME` stops, rerun the same invocation. Completed publication is recognized by its exact
  version, and only the declared later stages run. If you edit the workflow declaration, the next
  invocation uses the new sequence. Review its printed stages before proceeding.
- If repository creation may be partial, run `pubanon repo verify`.
- If the host is an empty or exact-prefix restoration, run `pubanon publish --sync`.
- If staging fails, rerun `pubanon stage push` after correction or remove the staging repository
  with `pubanon stage clean`.
- If a watch reaches its time limit, follow its observed-state recovery and rerun the same watch.
  Pending checks need more time; missing checks need their configured name and workflow trigger
  checked; ambiguous checks need the multiple matches resolved; failed checks need inspection and a
  fixed or rerun workflow. The default is 1800 seconds; set another positive whole number with
  `--timeout SECONDS`.
- If a reveal stops after a possible visibility change, rerun `pubanon repo reveal`. Pubanon
  reports the current exposure and verifies the result; it does not promise rollback. If the
  project declares public visibility while the host remains private, first decide the intended
  visibility: make the host public outside Pubanon or set the project declaration back to private,
  then rerun reveal.

## Unattended releases

Review and acknowledge any new paths before unattended publication:

```sh
pubanon prepare
pubanon publish --yes
```

A first release needs no acknowledgment: the selection review in `init` already covered every file.
For later releases, `prepare` lists every file that was not in the last release and asks whether to
include them. The second command publishes the saved snapshot without a publication confirmation.

For unattended preparation, `pubanon prepare --acknowledge-new-paths` approves the new-path batch
in advance. Use it after reviewing the complete list. Add `--version` only if you want a tag and
there is no committed package version to infer; see [version tags](guide.md#version-tags).

`--yes` confirms only the changes selected by the command. It cannot supply identity or sensitive
terms, accept a changed source branch or tag, acknowledge new paths, approve first-use staging
creation, or accept the reveal disclosure. If a command needs one of these decisions, it stops and
tells you which interactive step to run.

## Cleanup

For routine local cleanup after replacing a publication, use `pubanon clean`. It previews all
superseded snapshots in the selected project and asks for one confirmation. It removes their local
records, review directories, and source recovery information, while keeping current published
snapshots and prepared work. If there are no superseded snapshots, there is nothing to remove.

| What you want to remove                              | Command                   |
| ---------------------------------------------------- | ------------------------- |
| Superseded local snapshots and their associated data | `pubanon clean`           |
| One selected local snapshot and its associated data  | `pubanon delete ID`       |
| Unfinished preparation, while keeping review copies  | `pubanon discard`         |
| All extracted review copies, while keeping snapshots | `pubanon inspect --clean` |
| Prepared work and its review copies together         | `pubanon delete prepared` |

`clean` and `delete` preview before asking for confirmation; `--yes` skips that confirmation. They
release Pubanon's own retention references as part of deleting a snapshot. They do not change your
source branches, tags, files, or worktrees, run Git garbage collection, or change GitHub. Git may
reclaim unreachable objects during later maintenance; cleanup does not promise immediate space
savings.

Cleanup checks every selected snapshot before deleting any. If one is needed by a retained
preparation or publication recovery, resolve that work first. A failure during deletion reports
what has completed; follow any recovery instruction, then rerun `clean` for remaining superseded
snapshots.

`discard` removes the retained snapshot, any paused rewrite state and conflict export, and reports
preserved snapshot directories and their cleanup command. It never changes published history,
confirmed records, or their archive bookmarks. If a push outcome is unknown, ordinary discard
refuses because the snapshot contains the evidence needed by `publish --resolve`. Use
`discard --force` only when you deliberately abandon that evidence after checking the host
yourself.

`inspect --clean` lists and removes all known project review directories and their contents. Later
inspection can recreate them from retained snapshots. `delete prepared` also removes the prepared
snapshot's review copies; `discard` leaves those copies available.

Remote staging is separate from local cleanup. Keep the private staging repository if you plan to
use it for future releases. When it is no longer useful, `pubanon stage clean` previews and
confirms its deletion from GitHub. It selects the complete recorded owner-and-repository pair when
a valid record exists, even after staging configuration drift. It uses the configured staging pair
only when no record exists, and refuses cleanup when the selected pair matches the configured final
repository, including case-only aliases.

Before deletion, the command shows the selected repository, its visibility, whether the selection
came from a saved record or configuration, and the current configured staging name. Declining
deletion preserves the selected repository and record. `--yes` may confirm only an otherwise
reviewable deletion. An absent-or-inaccessible response is uncertain and does not prove absence, so
no host deletion is attempted. When recorded evidence exists, Pubanon offers a separate interactive
abandonment choice. Declining, reaching end of input, or using `--yes` preserves that local
evidence. A successful delete request is followed by a readback of the selected destination before
Pubanon clears the record and reports deletion. This verifies the deletion response and readback;
it does not establish the content or lifetime of retained provider logs, artifacts, or other data.

`init --relocate` shows distinguishable old and new source roots before confirmation. Relocation
requires snapshots to be prepared again; existing publication history is unchanged.

## When a snapshot must be prepared again

A retained snapshot stays bound to the source commit, publication policy, publication identity,
destination, expected visibility, required checks, and product safety rules used to prepare it.
Changing one of those facts invalidates it. Pubanon refuses publication and tells you to run:

```sh
pubanon discard
pubanon prepare
```

Changes to the named source branch or tag do not alter the saved snapshot. Interactive publication
shows a warning, then the normal publication review and confirmation. Publishing uses the saved
snapshot. `--yes` still refuses a changed source reference. If the pinned commit itself is gone,
discard and prepare again. A damaged or inconsistent snapshot also fails closed.

Workflow declarations, staging settings, package-observation settings, and repository settings do
not replace a retained snapshot. Commands that use them still verify their own target before any
host change. Run `pubanon status` to see the current snapshot and the facts that would prevent its
publication.

## Version tags and replacement

For a package, ordinary `prepare` reads a literal version from the selected, committed root
`pyproject.toml` and records the corresponding tag; version `0.1.0` becomes `v0.1.0`. An explicit
`prepare --version 0.1.0` must match that committed version and does not override it. A project
without a literal committed version, including a non-package project, can pass `--version` to
request a tag or use plain `prepare` for an untagged publication. Publication makes the lightweight
tag point directly to the retained release commit. Ordinary publication never moves or reuses the
tag. If that version already exists, choose a new version or explicitly prepare a correction with
`pubanon prepare --replace v0.1.0`. Neither `publish --sync` nor an ordinary retry selects a
replacement.

Selected replacement moves every tag on the corrected and replayed commits. Bare `--replace`
selects `latest`, preserving earlier publications with no replay. `--replace all` starts a root and
removes every old tag except an intended tag reused on that new root. Preparation reads the
committed `--source` revision, defaulting to `HEAD`, not automatically the old source. For a tagged
selection, package metadata and an explicit version must match one of that release's moved tags. An
untagged selection or `all` uses the ordinary version rules. See
[correcting published history](guide.md#correcting-published-history) for the full preparation and
review procedure.

The final push checks the exact saved old refs and changes them atomically. Host protection rules
may reject it. A lost readback requires `publish --resolve`, which reads without another push and
uses retained transaction evidence to finish local records. Mixed or unexpected host state remains
indeterminate and requires investigation; resolution does not silently approve it.

Package versions and filenames are also immutable at the package service. If a published package
needs different files, fix the project and publish a new version. Pubanon observes package state
but never replaces or deletes a package. Rewriting Git history also leaves existing GitHub Release
assets and Actions logs and artifacts unchanged; review those separately. Existing clones and
caches may retain the superseded history.

## Post-publication verification

Use these read-only checks after publication:

```sh
pubanon repo verify
pubanon status --audit --remote
```

`repo verify` compares repository existence, visibility, branches, tags, and settings with the
declared and retained publication state. `repo verify --repair-store` first verifies the host, then
rebuilds the local published-history copy without changing the host.

`status --audit` applies the current publication policy to the local copy of published history.
Adding `--remote` first verifies that copy against the host. The audit reports identity-critical
matches as historical exposure and other matches as policy differences. It cannot undo content that
was already public. Findings include rule sources and entry numbers, byte-unit offsets, and full
historical witness commits where applicable. Byte ranges are zero-based and end-exclusive. Each
finding shows its first match; additional occurrences may exist. Findings show the exact terms and
locations before any acceptance decision. Acknowledging existing exposure neither removes published
content nor relaxes snapshot gates; declining leaves the saved digest unchanged.

If `required_checks` is configured, wait for CI on this release:

```sh
pubanon watch ci --version 0.1.0
```

If package observation is configured, wait for the release checks and the package:

```sh
pubanon watch package --version 0.1.0
```

`watch ci` waits for the configured checks on the exact selected published commit. `watch package`
first waits for the configured release checks on the exact tag commit, then checks the configured
package index and lists the published files. Package observation must be enabled in the project
configuration. Version metadata alone is incomplete until at least one distribution file exists;
reveal also refuses that state before changing visibility. Both watches print the selected target,
commit or version, checks, timeout, current phase, and Ctrl-C guidance before slow reads. They
report state changes and elapsed updates at least every 30 seconds between changes. Both are safe
to interrupt and rerun with the same exact selection.

Before making a private repository public, `repo reveal` verifies all branches and tags, required
CI, optional package state, declared settings, sanitized Git content, and identity metadata. It
separately inventories Actions runs and artifacts, saves the complete list with owner-only POSIX
permissions, and gives you exact run links plus the repository Actions page for manual review.
Pubanon does not scan Actions logs or artifact contents. After changing visibility, reveal verifies
visibility, branches, tags, and settings again.

When both the project declaration and host are already public, repeated reveal checks only current
visibility, published refs, and repository settings. Run `pubanon status --audit --remote` to apply
the current privacy rules to published Git history and compare that history with the host.

## Repairing the local history copy

A mismatch alone does not establish which history is correct. If the local history is correct,
review `pubanon publish --sync` to catch GitHub up. If GitHub history is correct and the local copy
is missing or damaged, rebuild it from GitHub. For outside changes, follow the guide
[outside-fix procedure](guide.md#accepting-an-outside-fix):

```sh
pubanon repo verify --repair-store
```

Repair uses the verified host history as the source for the local copy. It fetches the main branch
and all tags into temporary local references. It checks that commits are unsigned, match the
configured publication identity, and form a linear history, and that tags point into that history.
It then checks GitHub again to confirm the fetched references are still current. Invalid structure,
unexpected branches, or a mismatch at this stage leave the existing local branches and tags in
place. The verified host history may have advanced or been replaced; it does not have to extend the
surviving local copy.

Before replacement, repair shows every local ref it will add, replace, or remove and asks whether
to use GitHub history. Declining leaves the local and host refs unchanged and removes only
temporary repair data. Use `pubanon repo verify --repair-store --yes` to confirm the listed
replacement without another prompt. Ordinary `repo verify --yes` is a usage error. A no-op repair
asks no question and does not write managed refs. Repair prints escaped literal findings so you can
inspect them; `--yes` does not hide those findings.

After you confirm, repair checks the host again. It replaces local `main` and the exact host tag
set in one atomic transaction with checks that the old local refs have not changed. It then reads
the installed refs and checks the host again. If the host moves after installation, repair reports
that local refs may have changed and asks you to rerun verification. It does not roll back or claim
verified success. The host and private source are unchanged by repair. A late settings failure also
reports that local restoration already occurred.

Repair can restore history that contains content flagged by current policy so you can review it
locally. It reports those findings in full. Restoration does not acknowledge exposure or waive the
checks required for publication, synchronization, and reveal. Run `pubanon status --audit --remote`
to audit the local published history against current policy after matching its refs to the host.
These checks validate content and publication structure; matching identity metadata and references
do not prove historical origin or that Pubanon originally constructed every imported object.

## Private source backups

Back up the private source with a bundle, which is one file holding every branch, tag, and Pubanon
source bookmark:

```sh
git bundle create /path/to/backup.bundle --all
```

An offline copy made with `git clone --mirror` to a local or removable path is also acceptable. The
mirror's own `origin` entry points at the source; that is fine because the mirror is not the
repository Pubanon reads.

Restore every branch, tag, and Pubanon source bookmark without configuring a remote. Replace
`ORIGINAL_SOURCE_BRANCH` with the branch used by the private source; Pubanon's publication branch
is always `main`, but the source branch need not be.

```sh
git init -b restore-in-progress restored
cd restored
git fetch /path/to/backup.bundle 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*' 'refs/pubanon/publications/*:refs/pubanon/publications/*'
git checkout ORIGINAL_SOURCE_BRANCH
git remote
git branch --list
```

`git remote` must print nothing and `git branch --list` must show every branch from the backup. The
temporary initial branch never receives a commit, so it does not exist after the checkout. Do not
restore with `git clone`: it keeps secondary branches only as remote-tracking references, and
removing the remote afterward discards them.

If the restored path differs from the configured source path, name the project and relocate it:

```sh
pubanon init --project NAME --relocate
```

Without `--project`, `init` derives a name from the directory and would create a new project.
Relocation changes the source path, which is part of the snapshot's configuration fingerprint, so a
retained snapshot is refused with "publication configuration changed after prepare". Run
`pubanon discard`, then `pubanon prepare` again. Published history and the local published-history
store are unaffected.

Never push private history to any hosting service, by URL or otherwise, even to a private
repository. The absence of a configured remote does not make such a push compatible with Pubanon's
contract.

## Disconnecting an alternate object store

An alternates file means the source borrows Git objects from another repository. Removing that file
first can make branches and tags unreadable. Pubanon refuses this source and does not change it
automatically.

Stop other Git writers and run the following from the source repository. Keep the original object
store available until verification succeeds. The procedure records every local branch and tag, runs
`git repack -a` while borrowed objects remain available, temporarily renames the actual alternates
file, and verifies each recorded tip and all its reachable objects independently. It also runs
`git fsck --full`. Any failure restores the alternates file; only successful verification removes
the saved file. It does not prune packs or run garbage collection.

```sh
sh <<'SH'
set -eu
alternates=$(git rev-parse --git-path objects/info/alternates)
saved="${alternates}.saved"
test -f "$alternates"
test ! -e "$saved"
refs=$(mktemp)
objects=$(mktemp)
disconnected=false
trap 'if test "$disconnected" = true; then mv "$saved" "$alternates"; fi; rm -f "$refs" "$objects"' EXIT
trap 'exit 1' HUP INT TERM
git for-each-ref --format='%(refname) %(objectname)' refs/heads/ refs/tags/ > "$refs"
git repack -a
mv "$alternates" "$saved"
disconnected=true
while read -r ref oid; do
    test "$(git rev-parse --verify "$ref")" = "$oid"
    git cat-file -e "$oid"
    git rev-list --objects "$oid" > "$objects"
    while read -r object rest; do
        git cat-file -e "$object"
    done < "$objects"
done < "$refs"
git fsck --full
rm "$saved"
disconnected=false
SH
```

If interrupted after the rename by a crash that prevents the trap from running, restore the saved
file at the path printed by `git rev-parse --git-path objects/info/alternates` before retrying. Do
not delete the borrowed object store until independent verification succeeds.

## Residual risks

Pubanon proves that the host's reachable branches, tags, and Git objects match the sanitized
history it published. It cannot prove that forks, remnants of deleted Git references, or external
caches are absent. Deleting a repository or rewriting published history does not reliably erase
earlier exposure.

Published work also carries behavioral fingerprints, including push timing, release cadence, and
coding style. A determined investigator may correlate them. Pubanon warns about content it can
identify, but an operator can still authorize identifying content after a warning. Review both the
snapshot and the timing of publication with that limit in mind.

Content checks read selected file bytes as UTF-8 with surrogate escapes and as Latin-1. Pubanon
normalizes both text views and configured terms to NFC, then casefolds them for matching. UTF-16/32
text and compatibility-character substitutions, such as U+FF21 in place of ASCII `A`, are not
guaranteed matches. Inspect the selected files for content these views may miss.

Pubanon can rebuild its local published-history copy from a verified host repository. If both the
host repository and that local copy are lost, the old published history is gone. Pubanon cannot
reconstruct it from private history without creating a new sanitized release. The next publication
starts a new first release only after explicit operator acknowledgment.
