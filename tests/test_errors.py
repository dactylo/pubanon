from pubanon._errors import PubanonError


class TestRender:
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
        assert 'expected: anon-owner' in text
        assert 'actual: other-owner' in text
        assert 'recovery: fix [host]' in text

    def test_render_omits_absent_fields(self) -> None:
        assert PubanonError('boom').render() == 'error: boom'
