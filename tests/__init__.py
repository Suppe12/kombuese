"""Automated tests for the HTTP API of Kombüse (Python standard library only, no pip packages).

Run everything from the repository root:

    python -m unittest discover tests -v

(`python -m unittest` without arguments works too.) Python 3.13 is the reference version, as in the Dockerfile.

How it works: every test class starts its own `app/server.py` as a subprocess on a free port, with a fresh temporary
data directory (KOMBUESE_DATA) and its own KOMBUESE_SECRET_KEY, waits for /api/version and stops the server
afterwards (see harness.py). The tests talk to the server only over HTTP and never import app modules, so renaming
the backend modules does not break them. No test reaches outside services (Bring!, Yazio, Anthropic, recipe sites):
error paths are tested, or a small fake server runs inside the test on 127.0.0.1.

Environment variables for the test run (all optional):
  KOMBUESE_TEST_PYTHON   interpreter for the server (default: the one running the tests)
  KOMBUESE_TEST_KEEP=1   keep the temporary data directories and print where they are
  KOMBUESE_TEST_LOG=1    print the server output of every test class at the end

The whole suite takes about 40 s. In a container without any network (docker run --network none) every server start
waits about 6 s for a reverse DNS lookup that http.server does when binding to 0.0.0.0, so it takes about 2 minutes.

Tests marked as expected failure document real bugs found by the suite; the note next to each says what is wrong.
"""
