import subprocess
import sys

import pytest

from pubanon._errors import PubanonError, PublicationCanceled
from pubanon._terminal import color_context, diagnostic, guidance_context, style


class TestRender:
    def test_publication_cancellation_keeps_default_next_step(self) -> None:
        assert PublicationCanceled('Canceled.', fact='snapshot kept').render() == (
            'Canceled.\n  snapshot kept\n\nNext: pubanon publish'
        )

    def test_render_names_fact_expected_actual_recovery(self) -> None:
        err = PubanonError(
            'destination mismatch',
            fact='host repository owner',
            expected='anon-owner',
            actual='other-owner',
            recovery='fix [host] in the project configuration',
        )
        text = err.render()
        assert 'destination mismatch' in text
        assert '  host repository owner' in text
        assert '  Expected: anon-owner' in text
        assert '  Found: other-owner' in text
        assert '\n\nNext: fix [host]' in text

    def test_render_omits_absent_fields(self) -> None:
        assert PubanonError('boom').render() == 'error: boom'

    def test_render_preserves_guidance_and_outer_color_context(self) -> None:
        error = PubanonError('refused', recovery='pubanon status')
        with guidance_context(project='demo'), color_context('always'):
            rendered = error.render()
            assert rendered == 'error: refused\n\nNext: pubanon status --project=demo'
            assert style('label') == '\x1b[1mlabel\x1b[0m'
            nested = PubanonError('outer', fact=rendered).render()
            assert '\n  error: refused\n  \n  Next: pubanon status --project=demo' in nested
            with color_context('never'):
                assert diagnostic(error) == rendered


@pytest.mark.parametrize('first', ['pubanon._errors', 'pubanon._terminal'])
def test_error_rendering_supports_either_import_order(first: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            (
                f'import {first}\n'
                'from pubanon._errors import PubanonError\n'
                'print(PubanonError("refused", recovery="pubanon status").render())'
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'error: refused\n\nNext: pubanon status\n'
