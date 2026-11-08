# Operations guide

This guide helps you recover interrupted work, inspect or remove saved snapshots, repair local
history, and back up the private source. If the source, prepared release, configuration, account,
or GitHub repository no longer matches the saved state, pubanon stops and reports what needs
attention. See the [publication guide](guide.md) for complete publication paths.

## Find the task

- [Recover an interrupted command](#retrying-an-interrupted-command)
- [Inspect a saved snapshot](#inspection)
- [Find a publication and its private source](#published-history-and-source-recovery)
- [Recover a host mismatch](#if-the-host-changed-outside-pubanon)
- [Clean up local or staging data](#cleanup)
- [Verify a publication](#post-publication-verification)
- [Repair the local published-history copy](#repairing-the-local-history-copy)
- [Back up or restore the private source](#private-source-backups)
- [Disconnect an alternate Git object store](#disconnecting-an-alternate-object-store)
- [Check what a command changes](#what-commands-change)

## Recover first

- If pubanon reports that no push was attempted, correct the reported problem and rerun the same
  command.
- If a push may have been sent but pubanon could not verify the result, run
  `pubanon publish --resolve`. Do not resend or discard the snapshot first.
- If pubanon verified the publication but could not finish its local records, correct the local
  write problem and run `pubanon publish --resolve`.
- If the code was published but its release page was not created or verified, run the reported
  `pubanon release create TAG` command. It succeeds without another write if the page now matches.
- For replacement conflicts, interrupted watches, reveal, staging, and other cases, follow the
  [detailed recovery steps](#retrying-an-interrupted-command).

## Retrying an interrupted command

The usual recovery is to correct the reported condition and rerun the same command. A rerun
re-verifies current facts and never silently selects new source content, rebuilds a retained
snapshot, changes a planned tag mapping, reuses a package version, or adds a stage. A retained
replacement may move or remove tags only as already reviewed in that snapshot.

pubanon repeats a GitHub read up to twice, after 2 and then 4 seconds, when GitHub answers it with
a server error (HTTP 500, 502, 503, or 504). It never repeats a change. When the error remains,
check [GitHub's status page](https://www.githubstatus.com/), wait, and rerun the command.

### Preparation

- If `prepare` fails before retaining a snapshot, rerunning it makes a new, reported source
  selection.
- If replacement preparation pauses or is interrupted after retaining state, use `pubanon status`,
  then `pubanon prepare --continue`. An interrupted export is regenerated from saved objects and
  its partial files are not read as decisions. In the awaiting phase, edit `tree/` and declare
  every file decision in `resolutions.toml`; refusal preserves those edits. See the guide's
  [conflict procedure](guide.md#correcting-published-history).

### Publication

- If `publish` fails or you decline before the push, the snapshot remains available. Correct the
  reported condition and rerun `publish`.
- If pubanon cannot tell whether a sent push succeeded, run `pubanon publish --resolve`. Do not
  resend or discard first. Resolution reads the recorded destination and either records success or
  confirms that nothing changed.
- If pubanon reports that publication was verified on the host but local finalization is
  incomplete, the publication has landed. Do not publish a new snapshot or discard the recovery
  record. Correct the reported local write failure, then run `pubanon publish --resolve`, which
  finishes the local records safely. Review copies remain on disk.
- Ctrl-C exits with status 130. If it arrives before the push, pubanon reports that nothing was
  pushed. If it arrives during the push, before pubanon has confirmed the result, the outcome is
  unknown: run `pubanon publish --resolve`. If the result was already confirmed, pubanon reports
  the success, and keeps what `publish --resolve` needs if local records were still being finished.

### Workflows and repository creation

- If `run NAME` stops, rerun the same invocation. Completed publication is recognized by its exact
  version, and only the declared later stages run. If you edit the workflow declaration, the next
  invocation uses the new sequence. Review its printed stages before proceeding.
- If repository creation may be partial, rerun `pubanon repo create`. It checks an existing empty
  repository without changing it and reports any configuration mismatch.
- If the host is an empty or exact-prefix restoration, run `pubanon repo restore --to remote`.

### Staging and requested CI

- If staging fails, check `pubanon stage status`, then use `stage repair` to reconnect tracking or
  `stage push` to send a new reviewed snapshot. `stage forget` only disconnects tracking; to
  publish without staging, run `pubanon init --publication-route direct` from the registered
  source. A repository that still has an old `stage` branch must be corrected on GitHub before
  repair.
- With `ci_workflow`, `pubanon stage push` requests quick CI on configured local runners. Add
  `--full-tests` for every test, `--hosted` for hosted CI, or both to pass both inputs. If a push
  succeeds but workflow registration or dispatch fails, the snapshot remains staged. Correct the
  reported cause and rerun `pubanon stage push` to request a new run. If a transport error or
  malformed response leaves the request outcome uncertain, inspect `pubanon stage status` first;
  rerunning requests another run and makes it the latest evidence. A failed or cancelled latest run
  blocks publication. Inspect its jobs, then request a new run; use `--hosted` when a required
  `hosted-validation` job was skipped by a local run.
- If `stage push` reports that required checks are not jobs in the connected workflow, correct them
  with `pubanon init --edit`, then run `pubanon discard` and `pubanon prepare`. If it reports that
  push CI is off in the staging repository and no CI workflow is connected, connect the workflow it
  names with `pubanon init --edit`, then run `pubanon discard` and `pubanon prepare`.
  Alternatively, delete `CI_RUN_ON_PUSH` from the staging repository on GitHub.

### Watches

- If a watch reaches its time limit, follow its observed-state recovery and rerun the same watch.
  Pending checks need more time; missing checks need their configured name and workflow trigger
  checked; ambiguous checks need the multiple matches resolved; failed checks need inspection and a
  fixed or rerun workflow. The default is 1800 seconds; set another positive whole number with
  `--timeout SECONDS`.

### Visibility changes

- If a reveal stops after a possible visibility change, rerun `pubanon repo reveal`. pubanon
  reports the current exposure and verifies the result; it does not promise rollback. If the
  project declares public visibility while the host remains private, first decide the intended
  visibility: make the host public outside pubanon or set the project declaration back to private,
  then rerun reveal.

If reveal cannot read the repository back immediately after changing visibility, it retries for up
to 3 minutes while GitHub applies the change. If it still cannot verify the result, wait a minute
and rerun reveal. The rerun records the repository as public without changing visibility again.

## Inspection

Build and inspect the exact retained snapshot before publication:

```sh
pubanon prepare
pubanon inspect
```

`inspect` identifies the snapshot, summarizes its changes and checks, and shows the review copy to
open. The copy has no Git metadata. On Linux and macOS its files and directories are read-only; on
Windows its files are read-only, but new files can still be added. To change what will be
published, edit and commit in the source repository, then discard and prepare again.

Review copies live in `<project-data>/inspections/<full-commit-oid>`, so a different snapshot gets
a separate copy. `pubanon status` shows the number of preserved copies. Use
`pubanon inspect --verbose` to list their locations alongside a prepared snapshot. With no prepared
snapshot, `pubanon inspect` lists the copies directly. Open a listed copy in your editor to review
its files.

Before reusing a directory, pubanon compares its files, paths, symlink targets, and executable
flags with the saved snapshot. If anything differs, inspection stops rather than present changed
files as the snapshot.

Use `pubanon inspect --refresh` to restore the selected directory from the saved snapshot. It
prints the exact location before replacing its current contents. `pubanon clean` lists all known
project review copies under `inspections/` and asks before removing them.

After publication, `pubanon inspect --published v0.1.0` creates or reuses a directory for the saved
published revision without a prepared snapshot or source checkout. The copy has no Git metadata.
Its files are verified and protected as described above;
`pubanon inspect --published v0.1.0 --refresh` restores it from the saved commit. `clean` also
removes published copies, and a later `inspect --published` regenerates them when the publication
is still available locally. To browse normal Git commit history, use a separate clone of the
published host repository. The review copy is for reviewing the exact saved files. Builds and tests
that write files need a separate writable directory.

For a completed replacement, ordinary `inspect` materializes the resulting tip.
`pubanon inspect --rewritten v0.2.0` selects an intermediate corrected or replayed release by its
moved tag; a rewritten commit prefix also works. This selection is separate from `--published`,
which reads confirmed history. Staging sends the completed tip, not an intermediate inspection.

Use `pubanon status` to see local publication state and anything that affects the snapshot; add
`--remote` to compare with GitHub. Every command that changes GitHub shows its exact target and
effect before asking. See [what commands change](#what-commands-change).

## Published history and source recovery

List saved snapshots, then select one to see its publication and private source details:

```sh
pubanon list
pubanon show v0.1.0 --verbose
```

`pubanon show prepared` displays the prepared snapshot; its commit prefix also selects it when
unambiguous. For published snapshots, `show` accepts `latest` for the most recent publication, a
tag such as `v0.1.0`, or an unambiguous commit prefix. A tag selects the commit it points to now.
If the tag no longer exists, pubanon looks for a saved record with that version and reports an
ambiguity when several match. Superseded snapshots are no longer part of the published history;
select them by commit prefix. `pubanon show v0.1.0 --verbose` gives the exact publication and
source commit IDs, saved destination, visibility when published, and local confirmation time.

The first confirmation is stored in UTC and displayed in your local time with seconds and a
timezone; it is distinct from the Git author date.

The command provides a concrete `git -C ... worktree add --detach ...` command for the original
source revision when the source repository is available. Run that command yourself to browse the
complete private source revision; run its matching `git -C ... worktree remove ...` command when
finished. pubanon does not create or remove source worktrees. A source worktree can retain Git
objects after a bookmark is removed.

### Find the same source contents after a rebase

Both `pubanon list --verbose` and `pubanon show VERSION --verbose` also search your current local
branch's history for commits with exactly the original source contents:

```text
Latest match on main: f6e5d4c3b2a1...
Number of matches:    1
```

The actual output uses full commit IDs. The latest match lets you inspect the same contents even
after a history rewrite changes commit IDs. Multiple commits can contain identical files, so the
output includes the number found. A completed search with no match shows `none` and `0`; an
unavailable source or history is reported as unavailable. This works whether source tags are
enabled or not, and compares complete source contents rather than the filtered publication.

### Understand source and visibility records

A replayed release has a derived snapshot record: its original snapshot, known original source
input, correction commit, and explicit conflict resolutions. That source input is not an exact
source tree for the derived result; use `inspect --published` for the exact published files. When
available, each derived release gets its own source bookmark, independent of the original
publication's bookmark. Missing source-location information remains unavailable rather than being
inferred from your current configuration.

History and published inspection read local data; they do not check GitHub. `private` or `public`
in `show` means visibility when publication was confirmed. `pubanon status` may show a different
configured visibility, which is an intention. Use `pubanon status --remote` to check the host's
current visibility. Older published snapshots without a private record remain inspectable when
their Git objects are available, but their source and historical visibility are not invented.

### Retention and bookmarks

Before `publish` pushes, pubanon bookmarks the snapshot's original source commit in your source
repository; `prepare` alone creates no bookmark. A published snapshot also has a private record and
a bookmark in pubanon's local published-history store. Together they keep the exact published files
and the original source revision available even after you rebase a branch. Neither the bookmark nor
the record changes when history is later rewritten.

The source bookmark protects the original commit and its Git ancestors from ordinary pruning. It is
visible in Git views that include all refs, such as `git log --all`, but is not an ordinary release
tag. Confirmed records have no automatic expiry. Retained ancestry may consume substantial disk
space. The bookmark is not a backup against loss of the source repository or its disk. To remove
snapshots and their bookmarks, see [cleanup](#cleanup).

### Local source tags

To see publications in your normal Git history, enable source tags during project setup or run:

```sh
pubanon init --source-tags
pubanon list
```

pubanon creates a lightweight tag such as `pubanon/v0.1.0` in the source repository after confirmed
publication. For an unversioned snapshot, it uses `pubanon/` followed by the full snapshot ID.
These source tags are separate from release tags in the published repository; pubanon does not push
them. You can inspect one directly:

```sh
git show pubanon/v0.1.0
```

`pubanon list` and `pubanon show` also create missing source tags and refresh existing ones. If the
original source has left your current branch's history after a rewrite, pubanon uses the latest
commit with identical contents. If no match exists, the tag keeps pointing to the original source.
A replacement publication updates the version's tag to its replacement source. For a replayed
release, the tag identifies its original source input; use `inspect --published` to see the
complete derived result.

New project initialization defaults to on. `pubanon init --no-source-tags` stops tag updates
without deleting existing tags. Snapshot cleanup also preserves these visible tags; remove unwanted
ones with `git tag -d pubanon/v0.1.0`. A conflicting tag is left unchanged with a warning, and a
tag update failure does not undo a completed publication. Correct the local problem and run
`pubanon list` to retry.

## Cleanup

For routine local cleanup, use `pubanon clean`. It previews superseded snapshots and all review
copies in the selected project, then asks for one confirmation. It removes the superseded
snapshots' local records and source recovery information and all review copies, while keeping
current published snapshots and prepared work. It also removes review copies when there are no
superseded snapshots.

| What you want to remove                             | Command             |
| --------------------------------------------------- | ------------------- |
| Superseded local snapshots and all review copies    | `pubanon clean`     |
| One selected local snapshot and its associated data | `pubanon delete ID` |
| Prepared work and its review copies                 | `pubanon discard`   |

### Remove a snapshot

`delete ID` removes one snapshot: its local entry, review copy, private record, and bookmarks.
Select an old snapshot by its ID from `pubanon list`; a version tag selects the current
publication. A deleted snapshot no longer appears in `list` or `show`, even if its commit is still
part of the published Git history. Deleting an original snapshot leaves a replayed release's own
bookmark in place, and the reverse, so the same source history may stay protected.

`clean` and `delete` preview before asking for confirmation; `--yes` skips that confirmation.
Snapshot deletion commands release pubanon's own retention references as part of deleting a
snapshot. They do not change your source branches, tags, files, or worktrees, run Git garbage
collection, or change GitHub. Git may reclaim unreachable objects during later maintenance; cleanup
does not promise immediate space savings.

Cleanup checks every selected snapshot before deleting any. If one is needed by a retained
preparation or publication recovery, resolve that work first. A failure during deletion reports
what has completed; follow any recovery instruction, then rerun `clean` for remaining superseded
snapshots.

`discard` removes the retained snapshot, its review copies, and any paused rewrite state and
conflict export. It never changes published history, confirmed records, or their archive bookmarks.
If a push outcome is unknown, ordinary discard refuses because the snapshot contains the evidence
needed by `publish --resolve`. Use `discard --force` only when you deliberately abandon that
evidence after checking the host yourself.

Later inspection can recreate removed review copies from retained snapshots.

### Forget a project

To remove a project's local pubanon data entirely, run `pubanon forget`. It previews the
registration, configuration file, and data paths, then asks for confirmation; `--yes` skips the
question. This removes snapshots, review copies, publication records, staging tracking, rewrite
state, init drafts, and the local published-history store. The same empty `project.lock` file and
its directory remain to exclude concurrent operations safely.

`forget` never contacts GitHub and keeps the source repository, including `pubanon/` tags and
`refs/pubanon/publications/*` bookmarks. Its preview identifies the final and staging repositories
that remain on GitHub, or shows `unknown` if configuration is missing or unreadable. An unresolved
publication outcome requires `pubanon publish --resolve`; unreadable snapshot or rewrite state also
blocks removal. `--force` abandons that evidence. Retained source worktrees always block removal,
even with `--force`; remove them using the displayed `git worktree remove` commands first.

If removal was interrupted, rerun `pubanon forget --project NAME` to remove what remains. After
forgetting, `pubanon init` in the source registers the project again, and
`pubanon repo restore --to local` rebuilds local published history from the host.

### Remove staging

The staging repository on GitHub is separate from local cleanup. When you no longer need it,
`pubanon stage delete` shows the repository and its visibility and asks before deleting it. It
selects the configured staging repository and refuses to delete the final repository. If local
tracking names a different repository, deletion stops; inspect that repository, and use
`stage forget` only if you mean to disconnect it locally. Neither command changes the project's
publication route.

Declining keeps the repository and its tracking, and `--yes` skips only the confirmation. If GitHub
reports the repository as missing or inaccessible, pubanon does not take that as proof that it is
gone and deletes nothing. After deleting, pubanon confirms that the repository is gone before
clearing tracking. GitHub may still keep its data for a while.

## When a snapshot must be prepared again

A retained snapshot stays bound to the source commit, publication policy, publication identity,
destination, expected visibility, required checks, `ci_workflow`, the expected GitHub account, and
pubanon's built-in safety rules used to prepare it. Changing one of those facts invalidates it.
pubanon refuses publication and tells you to run:

```sh
pubanon discard
pubanon prepare
```

Changes to the named source branch or tag do not alter the saved snapshot. Interactive publication
shows a warning, then the normal publication review and confirmation. Publishing uses the saved
snapshot. `--yes` still refuses a changed source reference. If the pinned commit itself is gone,
discard and prepare again. pubanon also refuses a damaged or inconsistent snapshot.

Publication routes, workflow declarations, staging settings, package-observation settings, and
repository settings do not replace a retained snapshot. Commands that use them still verify their
own target before any host change. Run `pubanon status` to see the current snapshot and the facts
that would prevent its publication.

## Version tags and replacement

The [version tag rules](guide.md#version-tags) and the
[replacement procedure](guide.md#correcting-published-history) are in the publication guide.
Publication points a lightweight tag at the release commit. Ordinary publication never moves or
reuses a tag: if the version already exists, choose a new version or prepare a correction with
`pubanon prepare --replace v0.1.0`. Neither `repo restore --to remote` nor a retry selects a
replacement.

A replacement moves every tag on the corrected and replayed commits; `--replace all` removes the
old tags except the intended tag on the new root. The final push checks that GitHub still matches
the saved published state and changes all refs in one update, and GitHub branch rules may reject
it. If the result is unknown, `publish --resolve` reads GitHub without pushing again and finishes
the local records. If GitHub shows an unexpected mix of old and new refs, resolution reports it for
you to investigate rather than accepting it.

Package versions cannot be replaced either: if a published package needs different files, publish a
new version. Replacing Git refs does not remove what GitHub retains, such as commits, activity,
Actions logs, and artifacts, or superseded history in existing clones and caches. A GitHub release
page and its assets are separate from the tag. `release create` and `release edit` manage a page's
title and notes, not its assets, and neither deletes pages.

## Post-publication verification

Use these checks after publication. Both are read-only, except that `audit --remote` offers to
delete Actions logs that match your terms and deletes nothing unless you confirm:

```sh
pubanon repo verify
pubanon audit --remote
```

`repo verify` compares repository existence, visibility, branches, tags, and settings with the
declared and retained publication state. `repo restore --to local` first verifies the host, then
rebuilds the local published-history copy without changing the host.

`audit` applies your current rules to the saved published history; `--remote` first checks that
history against GitHub. With `--remote`, audit then checks Actions run details, runners, and job
logs as reveal does, and names what it cannot check. Matches of your private terms are reported as
historical exposure, and matches of hygiene terms as policy differences.

Each finding names the rule and its entry number and, where relevant, the commit that contains it.
Text findings show where the match is, such as a file and line number, with the surrounding text;
binary content is located by byte range. Only the first match is shown; there may be more.

Before an acknowledgment, publish and restore show a summary that lists at most 20 rows per
category; `pubanon audit` lists every finding. Acknowledging an exposure neither removes published
content nor relaxes the checks on new snapshots; declining leaves the previous acknowledgment
unchanged.

If `ci_workflow` or `required_checks` is configured, wait for CI on this release:

```sh
pubanon repo watch --version 0.1.0
```

If package observation is configured, wait for the release checks and the package:

```sh
pubanon package watch --version 0.1.0
```

`repo watch` waits for the configured checks on the selected published commit. `package watch`
first waits for the release checks on the tagged commit, then checks the package index and lists
the published files; it needs [package observation](guide.md#package-observation) configured. Both
watches show what they are waiting for and report progress at least every 30 seconds. Both are safe
to interrupt and rerun with the same selection.

### Before making a repository public

Run `pubanon repo reveal` when you are ready to make a private repository public. Before asking, it
checks:

- Every branch and tag, declared repository settings, publication identity, and published Git
  content. Identity problems stop reveal. Content matching sensitive terms added after publication
  is listed for you to accept or decline.
- Required CI and optional package state. With `ci_workflow`, the latest push run for the release
  commit must pass, including required checks, as with `repo watch`.
- Every release page's tag, title, and notes, including drafts, against current sensitive terms.
  Any match stops reveal. A draft can become public later without passing reveal.
- Every Actions run's details, every job's runner, and every job log. This requires GitHub CLI
  2.97.0 or newer. Reveal stops while any run is still running.

Actions matches are grouped by run. For matching logs, choose delete, keep, or cancel. Deletion
asks again, cannot be undone, and keeps the runs and their results. It cannot remove matches in run
or runner details; delete those runs on GitHub, then rerun reveal. If the repository is already
public, deleting logs stops GitHub serving them but cannot undo earlier exposure.

Reveal saves the run and artifact list privately, then names what it cannot check: artifact
contents, Actions caches, job summaries, annotations, and earlier pushes on the Activity page. It
provides links for you to review them. Avoid starting workflows during reveal: its Actions check
uses several requests, and a run starting after the last read is not checked.

After changing visibility, reveal verifies visibility, branches, tags, and settings again. See
[visibility recovery](#visibility-changes) if it cannot verify the result. When it first records
the repository as public, including one GitHub already shows as public, it may offer to enable or
apply [immutable releases](guide.md#immutable-releases) if release pages are enabled or pages
already exist and the setting or eligible pages need action.

When both the project declaration and host are already public, repeated reveal checks only current
visibility, published refs, and repository settings. Run `pubanon audit --remote` to apply current
privacy rules to published Git history, compare it with the host, and check Actions history.

## If the host changed outside pubanon

When `publish` reports that the remote state differs from what the snapshot expects, or
`audit --remote` reports that host refs do not match local published state, pubanon shows the
expected and observed references. It does not know why they differ. Possible causes include a pull
request merged on the host, a push from another machine or configuration root, a change made in the
host's web interface, or a [recreated repository](guide.md#recreating-the-final-repository).

Ordinary publication does not remove commits from host history. Explicit
[publication replacement](guide.md#correcting-published-history) can rewrite the expected published
refs, but does not bypass a mismatch caused by outside changes. If you want the public branch back
at the last published state, restore it outside pubanon, using the last published commit reported
by `pubanon status --verbose`. After that, `pubanon status --remote` reports a match and
publication resumes. If the local published-history store is what is wrong,
`pubanon repo restore --to local` rebuilds it from a host that passes verification. Repair checks
history structure and identity, but cannot prove that pubanon originally created the commits.

## Repairing the local history copy

A mismatch alone does not establish which history is correct. If the local history is correct,
review `pubanon repo restore --to remote` to catch GitHub up. If GitHub history is correct and the
local copy is missing or damaged, rebuild it from GitHub. For outside changes, first review
[host-mismatch recovery](#if-the-host-changed-outside-pubanon):

```sh
pubanon repo restore --to local
```

`Local` means saved publication history, not the source checkout. Repair uses the verified host
history as the source for the local copy. It fetches the main branch and all tags into temporary
local references. It checks that commits are unsigned, match the configured publication identity,
and form a linear history, and that tags point into that history. It then checks GitHub again to
confirm the fetched references are still current. Invalid structure, unexpected branches, or a
mismatch at this stage leave the existing local branches and tags in place. The verified host
history may have advanced or been replaced; it does not have to extend the surviving local copy.

Before replacement, repair shows every local ref it will add, replace, or remove and asks whether
to use GitHub history. Declining leaves the local and host refs unchanged and removes only
temporary repair data. Use `pubanon repo restore --to local --yes` to confirm the listed
replacement without another prompt. If nothing needs to change, repair asks nothing and writes
nothing. It shows any content findings in full, even with `--yes`.

After you confirm, repair checks GitHub again, then replaces local `main` and the tags in a single
update that fails if the local copy changed in the meantime. It then checks the result and GitHub
once more. If GitHub moved after the update, repair says the local refs may have changed and asks
you to verify again; it does not roll back or report success. Repair never changes GitHub or your
private source. If a settings check fails at the end, repair reports that the local copy was
already restored.

Repair can restore history that your current rules flag, so you can review it locally. Restoring it
does not acknowledge the exposure or waive the checks for publishing, `repo restore --to remote`,
and reveal. Afterwards, run `pubanon audit --remote` to check the restored history against your
current rules. These checks confirm content and structure; matching names and refs cannot prove
that pubanon created every imported commit.

## Private source backups

Back up the private source with a bundle, which is one file holding every branch, tag, and pubanon
source bookmark:

```sh
git bundle create /path/to/backup.bundle --all
```

An offline copy made with `git clone --mirror` to a local or removable path is also acceptable. The
mirror's own `origin` entry points at the source; that is fine because the mirror is not the
repository pubanon reads.

Restore every branch, tag, and pubanon source bookmark without configuring a remote. Replace
`ORIGINAL_SOURCE_BRANCH` with the branch used by the private source; pubanon's publication branch
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

Relocation shows the old and new source roots before asking for confirmation.

Without `--project`, `init` derives a name from the directory and would create a new project.
Relocation changes the source path, which is part of the snapshot's configuration fingerprint, so a
retained snapshot is refused with "the configuration changed after the snapshot was prepared". Run
`pubanon discard`, then `pubanon prepare` again. Published history and the local published-history
store are unaffected.

Never push private history to any hosting service, by URL or otherwise, even to a private
repository. The absence of a configured remote does not make such a push compatible with pubanon's
contract.

## Disconnecting an alternate object store

An alternates file means the source borrows Git objects from another repository. Removing that file
first can make branches and tags unreadable. pubanon refuses this source and does not change it
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

## GitHub login during setup

If setup cannot verify your GitHub account, choose retry, login, or cancel. A failed check does not
necessarily mean you are logged out. If GitHub CLI cannot start, follow the reported installation
step first.

Choosing login hands the terminal and browser authorization to `gh`:

```sh
gh auth login --hostname github.com --git-protocol https --web
```

During that session, `gh` controls the output, credential storage, and HTTPS protocol selection.
pubanon does not collect a token, request insecure storage, upload SSH keys, or run
`gh auth setup-git`.

Setup checks the account again after login, even when `gh` reports success, and saves nothing until
it has shown you the identity, account, and every sensitive term. pubanon cannot undo a login that
`gh` completed; if setup stops afterwards, rerun `pubanon setup` to recheck the account and
continue. If saving succeeded but the final readiness check failed, the configuration stays saved
and the rerun checks it again. Rerunning setup on a complete configuration leaves its contents
unchanged, but on Linux and macOS it may restore owner-only file permissions. Use
`pubanon setup --edit` to correct the identity, sensitive terms, or expected account, and
`pubanon doctor` to run the checks without changing anything.

## Registry and draft recovery

The project registry records each canonical source path and its project name in JSON. If pubanon
reports an invalid registry, inspect the file path in the error. Restore a known backup of that
file, or reconstruct the current JSON mapping from the known `source_path` and `name` in each saved
project configuration. Keep the damaged file until you have confirmed the replacement. Commands
that accept `--project NAME` can select an existing project explicitly while the registry is being
repaired. pubanon does not silently reset a damaged registry.

If saving the registry or an `init` draft fails, the previous file is normally left intact. If the
error says the new file may have been saved, inspect the file before retrying.

## Input and cancellation

For menus and yes/no questions, press the bracketed letter; no Enter is needed. For text fields,
type your answer and press Enter. Menu letters are case-insensitive. An answer pubanon does not
recognize is explained and asked again; a typo never ends a command or discards earlier answers. A
blank answer takes the default shown in brackets; questions without a default are asked again.
Questions that publish, change visibility, or acknowledge exposure have no default.

Declining a confirmation reports `Canceled. Nothing was ...` and returns status 130. Choosing quit
or cancel at a menu and pressing Ctrl-C before a change starts also report `Canceled.` with status
130, without a traceback. Cancellation reports what was kept and how to retry when useful: setup
writes nothing until you save, and completed GitHub authentication stays in effect. End of input is
an error with status 1 and asks you to rerun in an interactive terminal.

An interruption during or after a change may leave an unknown or partial outcome. pubanon reports
what it knows and how to recover instead of claiming that nothing changed. In particular, if a
publication push may have been sent, follow `Next: pubanon publish --resolve` before retrying.

pubanon never hides what you type and never masks what it shows you. Its privacy check runs on the
content about to be published, not on your terminal. Displayed text escapes terminal control
characters and backslashes so that output cannot alter your terminal.

## Unattended releases

Review and acknowledge any new paths before unattended publication. For a staged project with
configured checks:

```sh
pubanon prepare
pubanon inspect
pubanon stage push
pubanon stage watch
pubanon publish --yes
```

Use the staging commands for [private-first CI](guide.md#private-first-ci), skipping the watch when
no CI is configured. On the [direct route](guide.md#direct-publication), skip both staging
commands. The first release needs no acknowledgment, because `init` already reviewed every file.
For later releases, `prepare` lists the files that are new since the last release and asks whether
to include them. `publish --yes` publishes without asking for confirmation, but a staged project
still verifies the staging repository and reads its checks.

For unattended preparation, `pubanon prepare --acknowledge-new-paths` approves the new-path batch
in advance. Use it after reviewing the complete list. Add `--version` only if you want a tag and
there is no committed package version to infer; see [version tags](guide.md#version-tags).

`--yes` confirms only the changes selected by the command. It cannot supply identity or sensitive
terms, accept a changed source branch or tag, acknowledge new paths, approve first-use staging
creation, accept sensitive terms in published history at reveal, or make the repository public. If
a command needs one of these decisions, it stops and tells you which interactive step to run.

## What commands change

These entries list changes to GitHub and the optional login session. Commands preview their target
and proposed changes before asking for approval.

### Setup and publication

- **`pubanon setup` (selected login only)**: Starts the GitHub CLI browser login you select; `gh`
  may store credentials and select HTTPS. No repository creation, push, visibility change, or
  `gh auth setup-git`.
- **`pubanon repo create`**: Creates the configured empty repository with its saved visibility, or
  checks an existing empty repository without changing it. No push or visibility change.
- **`pubanon publish`**: Atomically publishes the snapshot; an explicit replacement rewrites `main`
  and its planned tags. Updates the repository description first if it differs from the saved one.
  Creates a release page for a tagged version after publication when release pages are on or
  `--release-page` is given; with `draft = true` the page is a draft. No repository creation,
  visibility change, staging, or package upload.
- **`pubanon release create TAG`**: Creates a GitHub release page, a draft with `draft = true`, for
  a verified published tag after confirmation. No snapshot push, tag creation, or package upload.
- **`pubanon release edit TAG`**: Changes the selected page's title or notes after confirmation;
  `--refresh-notes` renders the notes from the changelog at the tag. Keeps the tag, status, assets,
  and other page settings.
- **`pubanon repo restore --to remote`**: Pushes retained published history to an empty or
  exact-prefix repository. No new snapshot, imported foreign history, or tag movement.
- **`pubanon run NAME [--version VERSION]`**: Performs the GitHub changes named by the workflow
  stages. A `publish` stage creates a release page under the same rule. No undeclared or later
  stage; `--yes` cannot add one.

### Visibility and privacy checks

- **`pubanon repo reveal`**: Makes the configured private repository public after checks and
  acknowledgment. Offers to delete Actions logs that match your terms, and may offer to enable
  immutable releases and apply them to existing pages, which permanently blocks reusing those
  versions. No branch or tag push, and no package upload.
- **`pubanon audit --remote`**: Offers to delete Actions logs that match your terms; deletes
  nothing unless you confirm. No push, visibility change, or other repository change.

### Private staging

- **`pubanon stage push`**: Creates the private staging repository with separate approval, or
  updates its `main` branch. With `ci_workflow`, it may create `CI_RUN_ON_PUSH=false` and request
  one CI run. No final publication or tag push.
- **`pubanon stage repair`**: May correct the private staging repository's default branch after
  approval. No staging push or final publication.
- **`pubanon stage forget`**: No external change; removes local staging tracking. Keeps the staging
  repository and publication route.
- **`pubanon stage delete`**: Deletes the selected private staging repository after its own preview
  and confirmation. Refuses the final repository; no change to local published history.

### Local changes and read-only commands

Apart from the optional GitHub login, `setup`, `init`, `prepare`, `inspect`, and
`repo restore --to local` change only pubanon's own configuration and data. Before pushing,
`publish` adds a private bookmark to the source commit, and `publish --resolve` may add one when it
records an older snapshot. `discard` and `delete` remove the bookmarks and local data they own. If
you enable [local source tags](#local-source-tags), publication, `list`, and `show` create or
refresh `pubanon/` tags. None of these commands change your source branches, HEAD, index, working
files, or other tags.

`doctor`, `status`, `repo verify`, `stage status`, `repo watch`, `package watch`, `release list`,
`release show`, and `publish --resolve` change nothing outside your machine, although several of
them read from GitHub or PyPI. `publish --resolve` reports a missing release page and its recovery
command without creating the page.

## Residual risks

pubanon proves that the host's reachable branches, tags, and Git objects match the sanitized
history it published. It cannot prove that forks, remnants of deleted Git references, or external
caches are absent. Deleting a repository or rewriting published history does not reliably erase
earlier exposure.

Published work also carries behavioral fingerprints, including push timing, release cadence, and
coding style. A determined investigator may correlate them. pubanon warns about content it can
identify, but an operator can still authorize identifying content after a warning. Review both the
snapshot and the timing of publication with that limit in mind.

Content checks can miss terms in UTF-16 or UTF-32 files, or terms written with look-alike
characters. See [how a release works](guide.md#how-a-release-works) for their limits, and inspect
the selected files for anything the checks may miss.

pubanon can rebuild its local published-history copy from a verified host repository. If both the
host repository and that local copy are lost, the old published history is gone. pubanon cannot
reconstruct it from private history without creating a new sanitized release. The next publication
starts a new first release only after your explicit acknowledgment.
