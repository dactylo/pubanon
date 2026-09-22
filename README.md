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

uv is needed for these installation commands. If you use make, `make install` and `make reinstall`
run the same commands.

## Your first release

### Choose your publication identity

```sh
pubanon setup
```

Setup asks for the name and email to use in published Git history and the terms you want to keep
private. It verifies your GitHub CLI account and shows your answers before saving. Choose the name
and email you want readers to see: existing publication commits keep that identity. Use
`pubanon setup --edit` to change saved answers or `pubanon doctor` to check readiness.

### Select your project and files

Enter a private source repository with no Git remote:

```sh
cd /path/to/private-project
pubanon init
```

Follow the prompts to choose files, a GitHub destination, and repository settings. Choose `private`
visibility for this walkthrough so you can check the result on GitHub before making it public.
Review your choices and save. Pubanon stores its configuration outside the source repository.

### Prepare and review

Commit the changes you want to publish in your source repository, then run:

```sh
pubanon repo create
pubanon prepare
pubanon inspect
```

`prepare` saves the selected committed files as a local *snapshot*. Uncommitted changes are left
out. If the selected `pyproject.toml` declares a package version, Pubanon uses it for the release
tag; otherwise the snapshot is untagged. See [version tags](docs/guide.md#version-tags) to choose
an explicit version.

`inspect` shows the release details and a directory of read-only files to review. Builds or tests
that write files need a separate writable copy. Changes in either copy do not alter the snapshot.
To correct it, edit and commit in the source, then run `pubanon discard` and `pubanon prepare`
again.

### Publish and make it public

When you are satisfied, run:

```sh
pubanon publish
pubanon repo reveal
```

`publish` asks for confirmation before sending the saved snapshot to your private destination.
`repo reveal` checks the published history, settings, and any configured CI requirements before
asking to make the repository public. It also provides links to Actions logs and artifacts for you
to review; Pubanon does not scan their contents.

For the next release, commit your changes and repeat `prepare`, `inspect`, and `publish`. For a
versioned Python package, update and commit its version first. The
[publication guide](docs/guide.md#quick-path) also explains the `release VERSION` shortcut.

Still correcting the initial release privately? Commit your fixes, then use
`pubanon prepare --replace all`, `pubanon inspect`, and `pubanon publish` to replace the entire
published history while keeping the same version. To correct only the latest publication and keep
earlier history, use `pubanon prepare --replace`. See
[correcting published history](docs/guide.md#correcting-published-history) for the available
scopes.

After replacements, `pubanon clean` previews and removes superseded local snapshots with one
confirmation. It keeps current published snapshots and prepared work. See
[cleanup](docs/operations.md#cleanup) for other cleanup tasks.

## Where to go next

| If you want to...                                               | Read                                                                          |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Publish another release or choose a workflow                    | [Publication guide](docs/guide.md)                                            |
| Change the selected files, CI checks, or other project settings | [Project configuration](docs/guide.md#edit-an-existing-project)               |
| Run CI privately before publishing                              | [Private staging](docs/guide.md#private-first-ci)                             |
| Inspect an earlier release or recover its private source        | [Published history](docs/operations.md#published-history-and-source-recovery) |
| Correct a published release                                     | [History replacement](docs/guide.md#correcting-published-history)             |
| Recover from an interruption, clean up, or make backups         | [Operations guide](docs/operations.md)                                        |
| Check platform requirements                                     | [Platforms](docs/platforms.md)                                                |
| See changes by version                                          | [Changelog](CHANGELOG.md)                                                     |

Use `pubanon status` to see local state or `pubanon status --remote` to compare it with GitHub. If
a push outcome is unknown, follow the reported recovery command: `pubanon publish --resolve`.

## Development

Provide the tools in `[dependency-groups].dev` through your environment or package manager, then
run:

```sh
make check
```

`make format` applies the repository formatters. Tests run offline with temporary Git repositories
and a fake GitHub CLI.
