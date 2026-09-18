"""Shared pytest fixtures for the Flask app and CLI tests."""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as app_module  # noqa: E402  (import after sys.path tweak)

FIXED_TEST_SECRET_KEY = "test-fixed-secret-key-for-pytest-only"


@pytest.fixture
def app(tmp_path):
    """The Flask app, reconfigured to use tmp_path for uploads/outputs."""
    application = app_module.app
    application.config["TESTING"] = True
    application.config["SECRET_KEY"] = FIXED_TEST_SECRET_KEY
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    application.config["OUTPUT_FOLDER"] = str(tmp_path / "outputs")
    application.config["DOWNLOAD_TTL_SECONDS"] = 24 * 60 * 60
    os.makedirs(application.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(application.config["OUTPUT_FOLDER"], exist_ok=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_sop_path():
    return str(REPO_ROOT / "examples" / "sample_sop.txt")


# ---------------------------------------------------------------------------
# Shared Playwright driver, for every real-browser test (tests/test_scorm.py
# and tests/conformance/). ``sync_playwright()`` owns an event loop for as
# long as it is open, and a second one on the same thread while one is open
# raises - so there must be exactly one, opened once for the whole session
# and shared by every test that needs a browser. Skips (never installs a
# browser) rather than fails when Playwright for Python isn't present.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def playwright_instance():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright for Python is not installed")
    with sync_playwright() as playwright:
        yield playwright


def _launch_chromium(playwright):
    """Launch Chromium, or skip.

    Never installs anything: if the Playwright package and the browser build
    on disk disagree (a pinned image can carry one and expect another) fall
    back to whatever Chromium binary is already present under
    PLAYWRIGHT_BROWSERS_PATH.
    """
    try:
        return playwright.chromium.launch()
    except Exception:
        pass

    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    candidates = sorted(root.glob("chromium_headless_shell-*/chrome-linux/headless_shell"))
    candidates += sorted(root.glob("chromium-*/chrome-linux/chrome"))
    for candidate in candidates:
        try:
            return playwright.chromium.launch(executable_path=str(candidate))
        except Exception:
            continue
    pytest.skip("no usable Chromium build is installed")


@pytest.fixture
def chromium(playwright_instance):
    """A headless Chromium on top of the session's shared Playwright driver.

    Function-scoped: each test gets its own browser process (cheap relative
    to the driver itself) and no test can leak state into another's.
    """
    browser = _launch_chromium(playwright_instance)
    try:
        yield browser
    finally:
        browser.close()
