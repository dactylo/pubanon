# Platforms

Pubanon targets Linux, macOS, and Windows 10 or newer with native CPython 3.13 or newer. Windows
requires Git for Windows and the GitHub CLI on `PATH`. PowerShell and Git Bash can launch the same
native Python installation. WSL uses the Linux implementation.

## Files and permissions

Pubanon trusts the operator and the local machine. On Linux and macOS, POSIX mode bits restrict
private configuration and state directories and review copies to the owner. Review copy files and
directories omit owner-write permission to discourage accidental changes.

On Windows, Pubanon uses the ordinary read-only attribute for review copy files. Their directories
remain writable, so Windows still permits adding files. This protection is a review convenience,
not a security boundary. Pubanon compares file contents, paths, and symlink targets with the saved
snapshot before reusing the directory. Refresh and cleanup clear read-only attributes as needed.

Configuration and data use `.config/pubanon` and `.local/share/pubanon` beneath the home directory,
unless `XDG_CONFIG_HOME` or `XDG_DATA_HOME` overrides them. Linked parent directories are
supported. Cleanup does not follow links into unrelated directories.

## Symlinks and executable files

On Windows, snapshots containing symbolic links require Developer Mode or an account allowed to
create symlinks. Pubanon stops if it cannot reproduce a symlink; it does not replace the link with
an ordinary file.

Git executable bits remain part of the saved snapshot. Linux and macOS expose them through
filesystem permissions. Windows does not, but conflict resolution retains the saved executable mode
when a file is kept or renamed.

## Windows recovery notes

If deleting a local publication record is interrupted on Windows, a `.pubanon-delete-*.tmp` file
may remain in the same private directory. It is not a publication record; you can remove it after
Pubanon reports the interruption.
