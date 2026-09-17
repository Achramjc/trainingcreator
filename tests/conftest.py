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
