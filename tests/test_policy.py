import os

import pytest

from pubanon import _policy
from pubanon._policy import Membership, evaluate_membership, narrowest_offer, secret_like


def test_membership_consumes_iterator_paths_with_reusable_rules() -> None:
    paths = iter(('docs/a.md', 'docs/private.txt', 'docs/b.md', 'docs/private.md', 'other'))

    assert evaluate_membership(paths, {'docs/*'}, {'docs/private.*'}) == Membership(
        allowed=['docs/a.md', 'docs/b.md'],
        denied=['docs/private.txt', 'docs/private.md'],
        unmatched=['other'],
        reserved=[],
    )


def test_narrowest_offer_escapes_metacharacters_in_group_directory() -> None:
    paths = ['dir[1]/a.txt', 'dir[1]/b.txt']

    offers = narrowest_offer(paths)

    assert offers == [('dir[[]1]/**', paths)]
    assert evaluate_membership(paths, [offers[0][0]], []) == Membership(
        allowed=paths,
        denied=[],
        unmatched=[],
        reserved=[],
    )


def test_narrowest_offer_escapes_metacharacters_in_one_literal_path() -> None:
    path = 'README[1]?*.md'

    offers = narrowest_offer([path])

    assert offers == [('README[[]1][?][*].md', [path])]
    assert evaluate_membership([path], [offers[0][0]], []) == Membership(
        allowed=[path],
        denied=[],
        unmatched=[],
        reserved=[],
    )


def test_narrowest_offer_for_one_file_uses_exact_path() -> None:
    assert narrowest_offer(['docs/guide.md']) == [('docs/guide.md', ['docs/guide.md'])]


def test_narrowest_offer_groups_new_top_level_directory() -> None:
    assert narrowest_offer(['src/main.py', 'src/lib/helper.py', 'README.md']) == [
        ('src/**', ['src/main.py', 'src/lib/helper.py']),
        ('README.md', ['README.md']),
    ]


def test_narrowest_offer_guided_mode_exposes_nested_internal_groups() -> None:
    paths = [
        'docs/guide.md',
        'docs/internal/notes.md',
        'docs/superpowers/design.md',
        '.agents/instructions.md',
    ]

    offers = narrowest_offer(paths, guided=True)
    allow: list[str] = []
    deny: list[str] = []
    for pattern, admitted in offers:
        if admitted[0] in {'docs/guide.md', 'docs/superpowers/design.md'}:
            allow.append(pattern)
        else:
            deny.append(pattern)

    assert [admitted for _pattern, admitted in offers] == [
        ['docs/guide.md'],
        ['docs/internal/notes.md'],
        ['docs/superpowers/design.md'],
        ['.agents/instructions.md'],
    ]
    assert evaluate_membership(paths, allow, deny) == Membership(
        allowed=['docs/guide.md', 'docs/superpowers/design.md'],
        denied=['docs/internal/notes.md', '.agents/instructions.md'],
        unmatched=[],
        reserved=[],
    )


def test_operator_allow_precedes_unmatched_fallback() -> None:
    assert evaluate_membership(['public.txt', 'undecided.txt'], ['public.*'], []) == Membership(
        allowed=['public.txt'],
        denied=[],
        unmatched=['undecided.txt'],
        reserved=[],
    )


def test_operator_deny_wins_over_operator_allow() -> None:
    assert evaluate_membership(['private/key.txt'], ['**'], ['private/**']) == Membership(
        allowed=[],
        denied=['private/key.txt'],
        unmatched=[],
        reserved=[],
    )


def test_path_policy_full_grammar_is_platform_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    literal_path = 'Release[1].TXT'
    offers = narrowest_offer([literal_path])
    paths = [
        'Docs/Guide.md',
        'docs/a/b/guide.md',
        'docs/a/b/secret.txt',
        'build/output.txt',
        'build/a/output.txt',
        literal_path,
        'Secrets/ID_RSA',
        'nested/.pubanon.toml',
    ]
    allow = ['docs/*', 'build/**/output.txt', offers[0][0], 'Secrets/**', 'nested/**']
    deny = ['docs/**/secret.txt', 'secrets/**']
    expected = Membership(
        allowed=['docs/a/b/guide.md', 'build/a/output.txt', literal_path, 'Secrets/ID_RSA'],
        denied=['docs/a/b/secret.txt'],
        unmatched=['Docs/Guide.md', 'build/output.txt'],
        reserved=['nested/.pubanon.toml'],
    )

    def _identity(value: str) -> str:
        return value

    def _casefold(value: str) -> str:
        return value.casefold()

    assert offers == [('Release[[]1].TXT', [literal_path])]
    assert secret_like(['Secrets/ID_RSA'], []) == ['Secrets/ID_RSA']
    monkeypatch.setattr(os.path, 'normcase', _identity)
    assert evaluate_membership(paths, allow, deny) == expected

    monkeypatch.setattr(os.path, 'normcase', _casefold)
    assert evaluate_membership(paths, allow, deny) == expected
    assert secret_like(['Secrets/ID_RSA'], []) == ['Secrets/ID_RSA']


def test_reserved_deny_matches_at_any_depth() -> None:
    assert evaluate_membership(['nested/dir/.pubanon.toml'], [], []) == Membership(
        allowed=[],
        denied=[],
        unmatched=[],
        reserved=['nested/dir/.pubanon.toml'],
    )


@pytest.mark.parametrize('path', ['.pubanon.toml', '.PubAnon.toml', 'nested/.PUBANON.TOML'])
def test_reserved_deny_wins_over_operator_allow(path: str) -> None:
    assert evaluate_membership([path], ['**'], []) == Membership(
        allowed=[],
        denied=[],
        unmatched=[],
        reserved=[path],
    )


def test_secret_like_exact_exception_suppresses_flag() -> None:
    assert secret_like(['config/.env'], ['config/.env']) == []


def test_secret_like_flags_ecosystem_conventions_at_any_depth() -> None:
    paths = [
        'config/.env.production',
        'certs/site.pem',
        'keys/site.key',
        'ssh/id_rsa',
        'ssh/id_ed25519',
        'certs/site.p12',
        'certs/site.pfx',
        'certs/site.keystore',
        'config/credentials-prod.json',
        'home/.netrc',
        'packages/.npmrc',
        'packages/.pypirc',
        'nested/.gitmodules',
    ]

    assert secret_like(paths, []) == paths


def test_secret_like_flags_env_at_any_depth() -> None:
    assert secret_like(['one/two/three/.env'], []) == ['one/two/three/.env']


def test_secret_like_is_not_suppressed_by_broad_allow() -> None:
    path = 'config/.env.production'

    membership = evaluate_membership([path], ['**'], [])

    assert membership.allowed == [path]
    assert secret_like([path], []) == [path]


@pytest.mark.parametrize('path', ['id_RSA', 'sub/KEY.PEM', '.ENV', 'Credentials-x.JSON'])
def test_secret_like_matches_case_variants(path: str) -> None:
    assert _policy.secret_like([path], []) == [path]
