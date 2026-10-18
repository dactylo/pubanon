from pathlib import Path

import pytest

from pubanon import _changelog
from pubanon._changelog import Lookup, NearMiss, Recognition

_CHANGELOG = Path(__file__).parents[1] / 'CHANGELOG.md'
_SEMANTIC_RELEASE = (
    '# Changelog\n'
    '# [0.7.0](https://example.invalid/compare) (2026-10-20)\n'
    '### Features\n'
    '* a\n'
    '## [0.6.1](https://example.invalid/compare) (2026-10-10)\n'
    '### Bug Fixes\n'
    '* b\n'
    '# [0.6.0](https://example.invalid/compare) (2026-10-01)\n'
    '* c\n'
)


def _section(text: str, *, version: str = '0.7.0', template: str | None = None) -> Lookup:
    return _changelog.find_section(text.encode(), version, template)


def test_a_fence_closes_only_with_the_same_character_at_least_as_long() -> None:
    text = '## 0.7.0\n~~~~\n## 0.6.0\n~~~\n```\nstill code\n~~~~\n- a\n'
    assert _section(text) == Lookup('~~~~\n## 0.6.0\n~~~\n```\nstill code\n~~~~\n- a')


def test_a_near_miss_suggests_a_template() -> None:
    assert _section('# Changelog\n\n## Version 0.7.0 (2026-10-20)\n- a\n') == Lookup(
        None,
        'no heading',
        near_misses=(NearMiss(3, '## Version 0.7.0 (2026-10-20)'),),
        suggestion='## Version {version}',
    )


def test_a_template_suffix_follows_the_version_directly() -> None:
    text = '## 0.7.0-2026-10-20\n- a\n## 0.6.0-2026-10-01\n- b\n'
    assert _section(text, template='## {version}-') == Lookup('- a')
    assert _changelog.recognize(text.encode(), '## {version}-').versions == ('0.7.0', '0.6.0')


def test_a_version_inside_a_longer_word_is_not_a_near_miss() -> None:
    assert _section('## mytoolv0.7.0\n## 10.7.0\n').near_misses == ()


def test_an_empty_section_gives_no_section() -> None:
    assert _section('## 0.7.0\n\n   \n## 0.6.0\n- b\n') == Lookup(None, 'empty section')


def test_an_unclosed_fence_runs_to_the_end_of_the_file() -> None:
    assert _section('```\n## 0.7.0\n- a\n').reason == 'no heading'


def test_backticks_after_an_opening_are_inline_code_not_a_fence() -> None:
    assert _section('```inline``` text\n## 0.7.0\n- a\n') == Lookup('- a')


@pytest.mark.parametrize(
    'heading',
    [
        '## 0.7.0 (2026-10-20)',
        '## [0.7.0] - 2026-10-20',
        '## 0.7.0',
        '## [0.7.0](https://github.com/o/r/compare/v0.6.0...v0.7.0) (2026-10-20)',
        '# [0.7.0](https://github.com/o/r/compare/v0.6.0...v0.7.0) (2026-10-20)',
        '## v0.7.0 - 2026-10-20',
        '## 0.7.0, 2026-10-20',
    ],
)
def test_builtin_rule_matches_common_formats(heading: str) -> None:
    assert _section(f'# Changelog\n\n{heading}\n\n- Added a thing.\n') == Lookup(
        '- Added a thing.'
    )


@pytest.mark.parametrize(
    'heading',
    [
        '## 0.7.0-rc.1',
        '## 0.7.01',
        '## 0.7.0.1',
        '## [Unreleased]',
        '### 0.7.0',
        '## Upgrading from 0.7.0',
    ],
)
def test_builtin_rule_rejects_other_headings(heading: str) -> None:
    assert _section(f'{heading}\n\n- Added a thing.\n').reason == 'no heading'


def test_categories_under_level_one_versions_stay_in_the_section() -> None:
    text = '# 0.7.0\n## Added\n- a\n## 1. Breaking changes\n- b\n# 0.6.0\n- c\n'
    assert _section(text) == Lookup('## Added\n- a\n## 1. Breaking changes\n- b')


def test_code_blocks_hide_headings() -> None:
    text = '## 0.7.0\n```md\n## 0.6.0\n```\n- a\n## 0.6.0\n- b\n'
    assert _section(text) == Lookup('```md\n## 0.6.0\n```\n- a')


@pytest.mark.parametrize(
    ('template', 'heading'),
    [
        ('## Version {version}', '## Version 0.7.0 (2026-10-20)'),
        ('### {version}', '### 0.7.0'),
        ('## mytool v{version}', '## mytool v0.7.0'),
        ('## Version {version}', '##  Version\t0.7.0'),
        ('## {{beta}} {version}', '## {beta} 0.7.0'),
        ('## [{version}]', '## [0.7.0] - 2026-10-20'),
    ],
)
def test_heading_template_matches(template: str, heading: str) -> None:
    assert _section(f'{heading}\n- a\n', template=template) == Lookup('- a')


@pytest.mark.parametrize('heading', ['## Version 0.7.01', '## Version 0.7.0-rc.1'])
def test_heading_template_requires_the_version_to_end(heading: str) -> None:
    assert _section(f'{heading}\n- a\n', template='## Version {version}').reason == 'no heading'


def test_line_endings_and_byte_order_mark_are_normalized() -> None:
    data = b'\xef\xbb\xbf## 0.7.0\r\n\r\n- a\r\n## 0.6.0\r- b'
    assert _changelog.find_section(data, '0.7.0') == Lookup('- a')


def test_link_definitions_inside_the_section_are_kept() -> None:
    text = '## [0.7.0] - 2026-10-20\n- a\n\n[0.7.0]: https://example.invalid/v0.7.0\n'
    assert _section(text) == Lookup('- a\n\n[0.7.0]: https://example.invalid/v0.7.0')


def test_near_misses_are_limited_to_three_in_file_order() -> None:
    lookup = _section('## a 0.7.0\n## b 0.7.0\n## c 0.7.0\n## d 0.7.0\n')
    assert [miss.line for miss in lookup.near_misses] == [1, 2, 3]


def test_pubanon_changelog_has_a_section_for_every_version() -> None:
    data = _CHANGELOG.read_bytes()
    versions = _changelog.recognize(data).versions
    assert len(versions) == data.count(b'\n## ')
    for version in versions:
        assert _changelog.find_section(data, version).section


def test_recognize_lists_distinct_versions_newest_first() -> None:
    data = (
        b'# Changelog\n## 0.2.0 (2026-01-02)\n- b\n## 0.10.0 (2026-01-03)\n- c\n'
        b'## 0.2.0\n## [Unreleased]\n## 0.1.0-rc.1\n## 0.1.0\n'
    )
    assert _changelog.recognize(data) == Recognition(('0.10.0', '0.2.0', '0.1.0'))


@pytest.mark.parametrize('template', [None, '## Version {version}'])
@pytest.mark.parametrize('version', ['01.2.3', '1.02.3', '1.2.03'])
def test_recognize_rejects_leading_zero_components(template: str | None, version: str) -> None:
    heading = (template or '## {version}').format(version=version)
    assert _changelog.recognize(f'{heading}\n- a\n'.encode(), template) == Recognition(())


def test_recognize_reports_text_that_is_not_utf8() -> None:
    assert _changelog.recognize(b'\xff') == Recognition((), readable=False)


@pytest.mark.parametrize('template', [None, '## Version {version}'])
def test_recognize_retains_zero_components(template: str | None) -> None:
    heading = template or '## {version}'
    data = '\n'.join(heading.format(version=v) for v in ('0.0.0', '0.1.0', '1.0.0'))
    assert _changelog.recognize(data.encode(), template) == Recognition(
        ('1.0.0', '0.1.0', '0.0.0')
    )


def test_recognize_uses_the_heading_template() -> None:
    data = b'## Version 0.6.0\n## Version 0.5.0\n'
    assert _changelog.recognize(data, '## Version {version}').versions == ('0.6.0', '0.5.0')


def test_recognize_without_matches_gives_hints() -> None:
    assert _changelog.recognize(b'# Changelog\n## Version 0.6.0\n- a\n') == Recognition(
        (), near_misses=(NearMiss(2, '## Version 0.6.0'),), suggestion='## Version {version}'
    )


@pytest.mark.parametrize('template', [None, '### Release {version}'])
def test_recognize_without_matches_hints_only_at_canonical_versions(
    template: str | None,
) -> None:
    data = b'## Version 01.2.3\n## Version 1.02.3\n## Version 1.2.03\n## Version 0.0.0\n'
    assert _changelog.recognize(data, template) == Recognition(
        (), near_misses=(NearMiss(4, '## Version 0.0.0'),), suggestion='## Version {version}'
    )


@pytest.mark.parametrize(
    ('version', 'section'),
    [('0.7.0', '### Features\n* a'), ('0.6.1', '### Bug Fixes\n* b'), ('0.6.0', '* c')],
)
def test_sections_end_at_the_next_version_heading_at_any_level(version: str, section: str) -> None:
    assert _section(_SEMANTIC_RELEASE, version=version) == Lookup(section)


@pytest.mark.parametrize(
    ('word', 'ends'),
    [
        ('0.6.1', True),
        ('1.0.0-rc.1', True),
        ('1.0.0+build.5', True),
        ('0.7.0rc1', True),
        ('2.1.302', True),
        ('1.2', True),
        ('0.6\u03b2', True),
        ('1.2_beta', False),
        ('1.', False),
        ('1', False),
        ('Added', False),
        ('Unreleased', False),
    ],
)
def test_only_version_like_words_end_a_level_one_section(word: str, *, ends: bool) -> None:
    text = f'# 0.7.0\n- a\n## {word} notes\n- b\n'
    assert _section(text) == Lookup('- a' if ends else f'- a\n## {word} notes\n- b')


def test_several_matching_headings_give_no_section() -> None:
    assert _section('## 0.7.0\n- a\n## 0.7.0\n- b\n') == Lookup(
        None, 'several headings', lines=(1, 3)
    )


@pytest.mark.parametrize(
    ('heading', 'suggestion'),
    [
        ("## What's new in 0.7.0", "## What's new in {version}"),
        ('## {beta} 0.7.0', '## {{beta}} {version}'),
        ('## mytool v0.7.0', '## mytool v{version}'),
        ('## 2026-10-20 (0.7.0)', None),
    ],
)
def test_suggestions_double_braces_and_skip_digits_before_the_version(
    heading: str, suggestion: str | None
) -> None:
    lookup = _section(f'{heading}\n- a\n')
    assert lookup.near_misses == (NearMiss(1, heading),)
    assert lookup.suggestion == suggestion


def test_text_that_is_not_utf8_has_no_section() -> None:
    assert _changelog.find_section(b'## 0.7.0\n\xff\n', '0.7.0') == Lookup(None, 'not UTF-8')
