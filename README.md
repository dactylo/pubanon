# pubanon

pubanon publishes selected files from a private Git repository to GitHub under a separate name and
email. Each release creates one publication commit without copying your private commit history.
Choose the files, review the prepared release, and confirm publication.

pubanon checks the selected content for configured sensitive terms. It does not rewrite secrets out
of files. You control what you publish; the
[content matching limits](docs/guide.md#how-a-release-works) explain what the checks can detect.

## Installation

You need Python 3.13 or newer, Git 2.41.0 or newer, and GitHub CLI (`gh`) 2.97.0 or newer. pubanon
targets Linux, macOS, and native Windows. See [platform requirements](docs/platforms.md) for
details.

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

This walkthrough uses private staging to check a release before publishing it to a private final
repository, then making that repository public. For other routes and CI setups, see the
[publication guide](docs/guide.md).

### Choose your publication identity

```sh
pubanon setup
```

Choose the name and email for published Git history, the terms you want to keep private, and the
`staged` publication route. Setup checks your GitHub CLI account and shows your answers before
saving.

### Select your project and files

From a private source repository with no Git remote, run:

```sh
pubanon init
```

Choose the files and GitHub destination, `staged` publication, and `private` visibility. Review the
repository settings and CI checks, then save. pubanon keeps its configuration outside your source
repository. See [project setup](docs/guide.md#initialize-a-project) for the individual choices.

### Prepare and test

Commit the changes you want to publish, including the package version when you have one:

```sh
pubanon prepare
pubanon inspect
pubanon stage push
pubanon stage watch
```

`prepare` saves the selected committed files as a local snapshot. `inspect` shows its details and a
read-only review copy. `stage push` sends that exact snapshot to a private staging repository;
`stage watch` waits for the configured CI checks. Review the files and results before continuing.
See [version tags](docs/guide.md#version-tags) and
[private staging](docs/guide.md#private-first-ci) for other version and CI choices.

### Publish and make it public

```sh
pubanon repo create
pubanon publish
pubanon repo watch
pubanon repo reveal
```

`repo create` creates or checks the final private repository. `publish` previews the release and
asks before uploading it. `repo watch` waits for that repository's CI. Finally, `repo reveal`
checks the publication and asks before making it public. Read its findings and
[verification limits](docs/operations.md#post-publication-verification) before confirming.

For the next release, commit your changes and repeat the preparation, staging, publication, and CI
steps. You no longer need `repo create` or `repo reveal`. See
[release pages](docs/guide.md#github-release-pages) to add a GitHub announcement for each version.

## Where to go next

| If you want to...                                               | Read                                                                          |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Publish another release or choose a workflow                    | [Publication guide](docs/guide.md)                                            |
| Publish without private staging                                 | [Direct publication](docs/guide.md#direct-publication)                        |
| Configure, create, or recover a GitHub release page             | [Release pages](docs/guide.md#github-release-pages)                           |
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

## License

pubanon is available under the [MIT License](LICENSE).
