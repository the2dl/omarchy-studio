"""Suite-wide fixtures.

Deliberately thin. There was no conftest at all until the suite was caught doing
something to the machine running it, and that is the bar for adding to this file:
something every test needs to be prevented from doing, not shared convenience.
"""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _never_open_a_window_on_the_user():
    """The suite must not reach out of the process and put a window on screen.

    `reveal()` opens the user's file manager when an export finishes, which is the
    point of the feature and exactly wrong inside a test. A full run left a Flea
    window sitting on workspace 2, with an orphaned `xdg-open` behind it that was
    still running long after the run had finished.

    Autouse and session-scoped, so no individual test has to remember -- the export
    tests that trigger this are not obviously about revealing anything, which is why
    it went unnoticed.
    """
    os.environ["OMARCHY_STUDIO_NO_REVEAL"] = "1"
    yield
    os.environ.pop("OMARCHY_STUDIO_NO_REVEAL", None)
