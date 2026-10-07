"""Rebuild the local published-history store from the host."""

import os
import re
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    assemble_rules,
    derived_private_terms,
)
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host
from pubanon._remote._host import Destination, RemoteRefs
from pubanon._run import OID_HEX, run_git
from pubanon._snapshots import _audit
from pubanon._snapshots._candidate import STATE_UNKNOWN_OUTCOME, published_refs
from pubanon._snapshots._records import load_candidate
from pubanon._snapshots._store import store_path
from pubanon._terminal import confirm, escape, heading, output, style


class RepairResult(NamedTuple):
    """Record restored local refs and their current-policy history audit.

    Attributes:
        refs: Actual installed main and exact tag set, freshly matched to the
            complete host observation before return. No extra branches exist.
        audit: Current content findings over all restored history. Structural
            checks passed; findings do not imply exposure acknowledgment or
            prove historical provenance.
    """

    refs: RemoteRefs
    audit: _audit.HistoryAudit


def _clear_repair_refs(store: Path) -> None:
    """Delete only refs in the private repair namespace."""
    output = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'for-each-ref',
            '--format=%(refname)',
            'refs/pubanon/repair/',
        ],
        cwd=store,
    ).stdout
    if output:
        commands = b''.join(b'delete ' + ref + b'\n' for ref in output.splitlines())
        run_git(
            ['--git-dir', str(store.absolute()), 'update-ref', '--no-deref', '--stdin'],
            cwd=store,
            input_bytes=commands,
        )


def _confirm_repair(before: RemoteRefs, fetched: RemoteRefs, *, yes: bool) -> None:
    """Review every changed local ref before authorizing host-directed replacement."""
    old_refs = {'refs/heads/main': before.main} | {
        f'refs/tags/{name}': oid for name, oid in before.tags.items()
    }
    new_refs = {'refs/heads/main': fetched.main} | {
        f'refs/tags/{name}': oid for name, oid in fetched.tags.items()
    }
    changes = {
        ref: f'{old_refs.get(ref) or "absent"} -> {new_refs.get(ref) or "absent"}'
        for ref in sorted(old_refs.keys() | new_refs.keys())
        if old_refs.get(ref) != new_refs.get(ref)
    }
    heading('Replace local publication history with verified GitHub history:')
    for label, value in changes.items():
        output(f'{style(escape(label) + ":")} {escape(value)}')
    output('The host repository and source repository will not change.')
    if not yes and not confirm('Replace local publication refs with verified GitHub refs?'):
        raise OperationCanceled('Canceled. Nothing was restored.')


def _install_repair_refs(
    store: Path,
    before: RemoteRefs,
    fetched: RemoteRefs,
) -> None:
    """Replace managed refs atomically only if their original values remain."""
    old = {f'refs/tags/{name}': oid for name, oid in before.tags.items()}
    if before.main is not None:
        old['refs/heads/main'] = before.main
    new = {f'refs/tags/{name}': oid for name, oid in fetched.tags.items()}
    if fetched.main is None:
        raise PubanonError('fetched repair main is absent')
    new['refs/heads/main'] = fetched.main
    commands = ['start']
    for ref, oid in new.items():
        if ref in old:
            commands.append(f'update {ref} {oid} {old[ref]}')
        else:
            commands.append(f'create {ref} {oid}')
    commands.extend(f'delete {ref} {oid}' for ref, oid in old.items() if ref not in new)
    commands.extend(('prepare', 'commit', ''))
    run_git(
        ['--git-dir', str(store.absolute()), 'update-ref', '--no-deref', '--stdin'],
        cwd=store,
        input_bytes='\n'.join(commands).encode('utf-8', 'surrogateescape'),
    )


def _repair_refs(store: Path) -> RemoteRefs:
    """Read and validate fetched main and direct tags in the repair namespace."""
    output = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'for-each-ref',
            '--format=%(objectname) %(refname)',
            'refs/pubanon/repair/',
        ],
        cwd=store,
    ).stdout
    main = None
    tags: dict[str, str] = {}
    for line in output.splitlines():
        match = re.fullmatch(
            rb'(' + OID_HEX.encode() + rb') refs/pubanon/repair/(main|tags/(.+))', line
        )
        if match is None:
            raise PubanonError('Git returned malformed repair refs')
        oid = match[1].decode('ascii')
        if match[2] == b'main':
            if main is not None:
                raise PubanonError('Git returned duplicate repair main')
            main = oid
        else:
            try:
                name = match[3].decode('utf-8')
            except UnicodeDecodeError as exc:
                raise PubanonError('Git returned malformed repair tag text') from exc
            if name in tags:
                raise PubanonError('Git returned duplicate repair tag')
            tags[name] = oid
    if main is None:
        raise PubanonError('fetched repair main is absent')
    return RemoteRefs(main, tags)


def check_store_repair_candidate(paths: Paths, project: str) -> None:
    """Refuse store repair while any candidate is retained.

    Args:
        paths: Derived pubanon paths.
        project: Registered project name.

    Raises:
        PubanonError: A retained candidate requires recovery before repair.
    """
    record = load_candidate(paths, project)
    if record is None:
        return
    if record.state == STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'cannot repair the store while a prepared snapshot is retained',
            recovery='pubanon publish --resolve',
        )
    raise PubanonError(
        'cannot repair the store while a prepared snapshot is retained; '
        'a later pubanon prepare rebuilds the discarded snapshot',
        recovery='pubanon discard, then pubanon repo restore --to local',
    )


def rebuild_store(
    paths: Paths,
    configs: tuple[GlobalConfig, ProjectConfig],
    dest: Destination,
    *,
    yes: bool,
) -> RepairResult:
    """Validate host history and atomically replace advisory main and tags.

    The caller holds the project lock and verifies the destination. Fetched
    objects are audited in private temporary refs before installation. Content
    findings may be restored locally without acknowledgment; invalid structure
    is refused. Host advancement needs no ancestry relationship with local
    refs.

    Args:
        paths: Derived pubanon paths.
        configs: Current global configuration and project whose local refs will
            be replaced.
        dest: Destination already verified by the host adapter.

        yes: Preauthorize ordinary replacement confirmation only.

    Returns:
        Actual installed refs matched to a fresh complete host observation,
        together with current-policy findings over their entire history.

    Raises:
        PubanonError: Candidate recovery is required, imported history is
            invalid, refs race, or a read or write fails. Failures after atomic
            installation explicitly state that local refs may have changed;
            no rollback or provenance guarantee is provided.
    """
    g, p = configs
    check_store_repair_candidate(paths, p.name)
    before = RemoteRefs(*published_refs(paths, p.name))
    store = store_path(paths, p.name)
    try:
        _clear_repair_refs(store)
        _host.fetch(
            dest,
            store,
            [
                '+refs/heads/main:refs/pubanon/repair/main',
                '+refs/tags/*:refs/pubanon/repair/tags/*',
            ],
        )
        fetched = _repair_refs(store)
        if fetched.main is None:
            raise PubanonError('fetched repair main is absent')
        rules = assemble_rules(g, p, derived_private_terms(paths, p, os.environ))
        commits = _audit.published_commits(paths, p.name, new=fetched.main)
        audit = _audit.audit_history(
            paths,
            p.name,
            commits,
            fetched.tags,
            rules,
            f'{g.anon_name} <{g.anon_email}>',
            tip=fetched.main,
        )

        if audit.identity_problems:
            raise PubanonError(
                'host history has invalid publication structure; managed refs were not replaced',
                fact=escape(', '.join(audit.identity_problems)),
            )
        if fetched != _host.ls_remote(dest, paths, p.name):
            raise PubanonError(
                'host refs changed or contain unexpected branches; managed refs were not replaced',
                recovery='pubanon repo restore --to local',
            )
        if before != fetched:
            _confirm_repair(before, fetched, yes=yes)
            if fetched != _host.ls_remote(dest, paths, p.name):
                raise PubanonError(
                    'host refs changed during consent; managed refs were not replaced',
                    recovery='pubanon repo restore --to local',
                )
            _install_repair_refs(store, before, fetched)
        try:
            actual = RemoteRefs(*published_refs(paths, p.name))
            observed = _host.ls_remote(dest, paths, p.name)
        except PubanonError as exc:
            raise type(exc)(
                'repair could not verify its result; local refs may have changed',
                fact=exc.render(),
                recovery='pubanon repo restore --to local',
            ) from exc
        if actual != fetched or actual != observed:
            raise PubanonError(
                'installed refs no longer match verified host refs; local refs may have changed',
                recovery='pubanon repo restore --to local',
            )
        return RepairResult(actual, audit)
    finally:
        # Best-effort cleanup must not hide the primary repair outcome.
        with suppress(PubanonError):
            _clear_repair_refs(store)
