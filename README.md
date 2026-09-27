# pubanon

Pubanon publishes selected files from a private Git repository to GitHub under a separate name and
email. Each release creates one publication commit without copying your private commit history.
Choose the files, review the prepared release, and confirm publication.

Pubanon checks the selected content for configured sensitive terms. It does not rewrite secrets out
of files. You control what you publish; the
[content matching limits](docs/guide.md#how-a-release-works) explain what the checks can detect.

## Installation

You need Python 3.13 or newer, Git 2.41.0 or newer, and the GitHub CLI (`gh`). Pubanon targets
Linux, macOS, and native Windows. See [platform requirements](docs/platforms.md) for details.

From a checkout of this repository, install the command with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install .
```

After updating the checkout, update the installed command:

```sh
uv tool install --reinstall .
```

`make install` and `make reinstall` run the same commands.

## Your first release

### Choose your publication identity

```sh
pubanon setup
```

Setup asks for the name and email to use in published Git history, the terms you want to keep
private, and the default publication route for new projects. The built-in route is `staged`; choose
`direct` if you intend to publish without private staging. Setup verifies your GitHub CLI account
and shows your answers before saving. Choose the name and email you want readers to see: existing
publication commits keep that identity. Use `pubanon setup --edit` to change saved answers or
`pubanon doctor` to check readiness.

### Select your project and files

Enter a private source repository with no Git remote:

```sh
cd /path/to/private-project
pubanon init
```

Follow the prompts to choose files, a GitHub destination, a publication route, and repository
settings. Choose the `staged` route and `private` visibility for this walkthrough so you can test
before the final upload and check the result on GitHub before making it public. Review your choices
and save. Pubanon stores its configuration outside the source repository.

### Prepare and review

Commit the changes you want to publish in your source repository, then run:

```sh
pubanon prepare
pubanon inspect
pubanon stage push
pubanon stage watch
```

`prepare` saves the selected committed files as a local *snapshot*; uncommitted changes are left
out. If the selected `pyproject.toml` declares a package version, Pubanon uses it for the release
tag; otherwise the snapshot is untagged. See [version tags](docs/guide.md#version-tags) to choose
an explicit version.

`inspect` shows the release details and a read-only review copy of the files. To run builds or
tests, copy the files somewhere writable first. To change the snapshot, edit and commit in the
source, then run `pubanon discard` and `pubanon prepare` again.

`stage push` sends that exact snapshot to a separate private GitHub repository, asking before it
creates the repository the first time. Staging never changes your source branches, and the same
staging repository is reused for later releases. `stage watch` waits for its configured checks; if
you haven't configured any, skip it and review the staging repository yourself.

### Publish and make it public

When you are satisfied, run:

```sh
pubanon repo create
pubanon publish
pubanon repo watch
pubanon repo reveal
```

`repo create` creates the final repository, still private; skip it if the repository already
exists. Before the first upload, keep in mind that GitHub can retain old commits, activity, Actions
logs, and artifacts even after history is replaced. Staging lets you test before anything reaches
the final repository.

`publish` asks for confirmation, then sends the snapshot to the final repository. On the `staged`
route, it first checks that the staged snapshot passed its configured checks. `repo watch` waits
for the final repository's own checks; skip it if none are configured. `repo reveal` checks the
published history and settings, links the repository's Activity and Actions pages for you to
review, and asks before making the repository public. Pubanon does not scan logs or artifact
contents.

For the next release, commit your changes, updating the package version if you have one, and repeat
these steps without `repo create` and `repo reveal`.

## Where to go next

| If you want to...                                               | Read                                                                          |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Publish another release or choose a workflow                    | [Publication guide](docs/guide.md)                                            |
| Publish without private staging                                 | [Direct publication](docs/guide.md#direct-publication)                        |
| Add or edit a GitHub release page for a published tag           | [Release pages](docs/guide.md#github-release-pages)                           |
| Change the selected files, CI checks, or other project settings | [Project configuration](docs/guide.md#edit-an-existing-project)               |
| Run CI privately before publishing                              | [Private staging](docs/guide.md#private-first-ci)                             |
| Inspect an earlier release or recover its private source        | [Published history](docs/operations.md#published-history-and-source-recovery) |
| Fix a candidate that failed in staging                          | [Test privately](docs/guide.md#test-privately-and-publish)                    |
| Correct a published release                                     | [History replacement](docs/guide.md#correcting-published-history)             |
| Recover from an interruption, clean up, or make backups         | [Operations guide](docs/operations.md)                                        |
| Check platform requirements                                     | [Platforms](docs/platforms.md)                                                |
| See changes by version                                          | [Changelog](CHANGELOG.md)                                                     |

Use `pubanon status` to see local state or `pubanon status --remote` to compare it with GitHub. If
a push outcome is unknown, follow the reported recovery command: `pubanon publish --resolve`.

## Development

The [development guide](docs/development.md) lists the required tools. With them installed, run:

```sh
make setup
make check
```
