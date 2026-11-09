import os

from hypothesis import settings

# Local runs stay fast; CI tries more random cases to search harder for counterexamples.
settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=1000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def pep_library(tmp_path_factory):
    """One library of all bundled PEPs, built once per test run. Tests must not change it."""
    from evfind.ingest import ingest_peps
    from evfind.store import Library

    path = tmp_path_factory.mktemp("peps") / "peps.db"
    with Library.open(path, create=True) as library:
        ingest_peps(library)
    return path
