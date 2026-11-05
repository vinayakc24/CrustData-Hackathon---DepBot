"""Unit-test isolation (owner: Member A).

Engine unit tests drive Conan-manifest repositories with stand-in stages; they
must not depend on a Conan installation. The real tooling preflight
(``environment_problems``) is therefore stubbed for unit tests, except tests
marked ``@pytest.mark.real_tooling``, which exercise the real check.
"""

import pytest

from depbot.package_managers.conan import ConanAdapter


@pytest.fixture(autouse=True)
def _no_conan_preflight(request, monkeypatch):
    if request.node.get_closest_marker("real_tooling") is None:
        monkeypatch.setattr(ConanAdapter, "environment_problems", lambda self: [])
