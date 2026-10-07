import tomllib
from pathlib import Path

import pubanon


def test_pubanon_is_the_only_supported_identity() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text())
    assert pubanon.__all__ == []
    assert project['project']['name'] == 'pubanon'
    assert project['project']['scripts'] == {'pubanon': 'pubanon._cli:main'}
    assert project['tool']['hatch']['build']['targets']['wheel']['packages'] == ['src/pubanon']
