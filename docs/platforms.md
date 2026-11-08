# Platforms

## Requirements

All platforms require Python 3.13 or newer, Git 2.41.0 or newer, and GitHub CLI (`gh`) 2.97.0 or
newer on `PATH`.

| Platform            | Installation requirements                       |
| ------------------- | ----------------------------------------------- |
| Linux               | Python, Git, and GitHub CLI                     |
| macOS               | Python, Git, and GitHub CLI                     |
| Windows 10 or newer | Native CPython, Git for Windows, and GitHub CLI |

PowerShell and Git Bash can launch the same native Windows Python installation. WSL uses the Linux
implementation.

## Files and permissions

Configuration and data live in `.config/pubanon` and `.local/share/pubanon` beneath your home
directory. Set `XDG_CONFIG_HOME` or `XDG_DATA_HOME` to use different roots. Linked parent
directories are supported; cleanup does not follow links into unrelated directories.

pubanon trusts you and your local machine. Review copies discourage accidental edits:

| Platform | Configuration and state                                             | Review copies                                                                              |
| -------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Linux    | POSIX mode bits restrict private files and directories to the owner | Owner-only files and directories omit owner-write permission                               |
| macOS    | POSIX mode bits restrict private files and directories to the owner | Owner-only files and directories omit owner-write permission                               |
| Windows  | Use your account's filesystem access controls                       | Files have the read-only attribute; directories remain writable, so new files can be added |

A review copy's write protection is a convenience, not a security boundary. Before reusing a copy,
pubanon compares its contents, paths, and symlink targets with the saved snapshot. Refresh and
cleanup clear Windows read-only attributes as needed.

## Symlinks and executable files

On Windows, snapshots containing symlinks need Developer Mode or an account allowed to create
symlinks. pubanon stops if it cannot reproduce a symlink; it does not substitute an ordinary file.

Git executable bits stay in the saved snapshot. Linux and macOS expose them through filesystem
permissions. Windows does not, but conflict resolution retains the saved executable mode when a
file is kept or renamed.

## Displayed paths

File labels use forward slashes on every platform, including Windows under Git Bash or PowerShell.
Special Windows namespace paths keep their original spelling. Literal backslashes in POSIX
filenames are escaped for display. Paths beneath your home directory use `~/` in labels; suggested
commands use the paths and quoting needed to run them. Display formatting does not change stored
paths or filesystem operations.

## Windows recovery notes

If deleting a local publication record is interrupted, a `.pubanon-delete-*.tmp` file may remain in
the same private directory. It is not a publication record; you can remove it after pubanon reports
the interruption.
