"""
Tests for the sample-SOP gallery: the loader (src/samples.py), the parsing
quality of each gallery document, its round-trip through the full pipeline,
and the Flask blueprint (src/samples_routes.py).
"""

import io
import json
from pathlib import Path

import pytest

from src.assessments import AssessmentGenerator, MedicalDeviceAssessmentGenerator
from src.generator import TrainingGenerator
from src.parser import SOPParser
from src.samples import (
    REPO_ROOT,
    UnknownSampleError,
    get_sample,
    list_samples,
    sample_path,
)
from src.scorm_exporter import SCORMExporter

CATALOG = list_samples()
CATALOG_IDS = tuple(entry["id"] for entry in CATALOG)

# Register the blueprint on the shared Flask app if app.py hasn't done so
# itself yet (the coordinator wires this in with one line after this stream
# merges). This MUST happen at import time, during pytest's collection
# phase, rather than inside a fixture: Flask refuses `register_blueprint`
# once the app has handled its first request, and some other test module in
# the same run may dispatch a request before a fixture would otherwise run.
# Collection (importing every test module) always completes before any test
# function executes, so registering here is safe regardless of test order.
import app as _app_module  # noqa: E402  (see comment above)

if "samples" not in _app_module.app.blueprints:
    from src.samples_routes import samples_bp

    _app_module.app.register_blueprint(samples_bp)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def test_catalog_is_not_empty():
    assert len(CATALOG) == 6, "expected the six gallery regimes"
    assert len(set(CATALOG_IDS)) == len(CATALOG_IDS), "duplicate sample id"


def test_list_samples_resolves_paths_under_the_repo():
    for entry in CATALOG:
        path = entry["resolved_path"]
        assert path.exists(), f"{entry['id']}: {path} does not exist"
        assert REPO_ROOT in path.parents or path.parent == REPO_ROOT


def test_get_sample_returns_none_for_unknown_id():
    assert get_sample("does_not_exist") is None


@pytest.mark.parametrize("sample_id", CATALOG_IDS)
def test_sample_path_resolves_known_ids(sample_id):
    path = sample_path(sample_id)
    assert path.exists()
    assert path.is_file()


@pytest.mark.parametrize("bad_id", (
    "../x",
    "../../etc/passwd",
    "sample;rm",
    "sample rm -rf",
    "sample/../x",
    "sample.txt",
    "SAMPLE",
    "",
    "sample id",
))
def test_sample_path_rejects_malformed_ids(bad_id):
    with pytest.raises(ValueError):
        sample_path(bad_id)


def test_sample_path_rejects_well_formed_but_unknown_id():
    with pytest.raises(UnknownSampleError):
        sample_path("not_a_real_sample")


def test_sample_path_never_escapes_the_gallery_directory():
    """Even a well-formed-looking id that isn't cataloged must not resolve."""
    gallery_dir = REPO_ROOT / "examples" / "gallery"
    for bad_id in ("etc_passwd", "index_json", "build_docx_py"):
        with pytest.raises(UnknownSampleError):
            sample_path(bad_id)
    # Sanity: the real ids really do live under examples/gallery.
    for sample_id in CATALOG_IDS:
        assert sample_path(sample_id).parent == gallery_dir


# ---------------------------------------------------------------------------
# Parsing quality of every gallery document
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entry", CATALOG, ids=CATALOG_IDS)
def test_gallery_document_parses_richly(entry):
    document = SOPParser().parse(str(entry["resolved_path"]))

    assert document.purpose, f"{entry['id']}: no purpose extracted"
    assert document.scope, f"{entry['id']}: no scope extracted"
    assert len(document.definitions) >= 3, (
        f"{entry['id']}: only {len(document.definitions)} definitions")

    assert len(document.safety_warnings) >= entry["warnings_expected"], (
        f"{entry['id']}: only {len(document.safety_warnings)} warnings, "
        f"expected >= {entry['warnings_expected']}")

    assert len(document.procedures) >= entry["steps_expected"], (
        f"{entry['id']}: only {len(document.procedures)} procedures, "
        f"expected >= {entry['steps_expected']}")

    for procedure in document.procedures:
        assert procedure.get("body"), (
            f"{entry['id']}: step {procedure.get('step_number')} has an empty body")


# ---------------------------------------------------------------------------
# Full pipeline round-trip: generate -> assess (5 and 8 questions) -> SCORM
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entry", CATALOG, ids=CATALOG_IDS)
@pytest.mark.parametrize("num_questions", (5, 8))
def test_gallery_document_round_trips_the_pipeline(tmp_path, entry, num_questions):
    document = SOPParser().parse(str(entry["resolved_path"]))

    training_module = TrainingGenerator().generate(document)
    assert training_module.sections

    assessment = MedicalDeviceAssessmentGenerator().generate(
        document, num_questions=num_questions)
    assert len(assessment.questions) == num_questions

    package_name = "gallery_{0}_{1}".format(entry["id"], num_questions)
    zip_path = SCORMExporter(scorm_version="1.2").create_package(
        training_module, assessment, str(tmp_path), package_name)
    assert Path(zip_path).exists()


# ---------------------------------------------------------------------------
# DOCX versions parse to the same step count as their .txt originals
# ---------------------------------------------------------------------------
DOCX_PAIRS = tuple(
    entry for entry in CATALOG
    if (entry["resolved_path"].parent / (entry["resolved_path"].stem + ".docx")).exists()
)


def test_docx_pairs_exist():
    """Sanity: at least two gallery documents ship a .docx twin, per the spec."""
    assert len(DOCX_PAIRS) >= 2


@pytest.mark.parametrize("entry", DOCX_PAIRS, ids=[e["id"] for e in DOCX_PAIRS])
def test_docx_version_matches_txt_step_count(entry):
    txt_path = entry["resolved_path"]
    docx_path = txt_path.parent / (txt_path.stem + ".docx")

    parser = SOPParser()
    txt_doc = parser.parse(str(txt_path))
    docx_doc = parser.parse(str(docx_path))

    assert len(docx_doc.procedures) == len(txt_doc.procedures)
    assert len(docx_doc.safety_warnings) == len(txt_doc.safety_warnings)
    assert len(docx_doc.definitions) == len(txt_doc.definitions)
    for txt_proc, docx_proc in zip(txt_doc.procedures, docx_doc.procedures):
        assert docx_proc["body"] == txt_proc["body"]


# ---------------------------------------------------------------------------
# Blueprint: GET /api/samples, POST /api/samples/<id>/generate
# ---------------------------------------------------------------------------
@pytest.fixture
def samples_client(app):
    """The shared `app` fixture (tests/conftest.py); the samples blueprint is
    registered at module import time above, once, for the whole test run."""
    return app.test_client()


def test_catalog_route_returns_200_with_no_paths(samples_client):
    resp = samples_client.get("/api/samples")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "samples" in payload
    assert len(payload["samples"]) == len(CATALOG)
    for entry in payload["samples"]:
        assert "path" not in entry
        assert "resolved_path" not in entry
        assert {"id", "title", "regime", "standard", "description"} <= set(entry)


@pytest.mark.parametrize("sample_id", CATALOG_IDS)
def test_generate_route_success(samples_client, sample_id):
    resp = samples_client.post(
        f"/api/samples/{sample_id}/generate",
        data={
            "num_questions": "5",
            "passing_score": "80",
            "scorm_version": "1.2",
            "output_format": "scorm",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["success"] is True
    assert "download_url" in payload
    assert "review_url" in payload

    download_resp = samples_client.get(payload["download_url"])
    assert download_resp.status_code == 200


def test_generate_route_unknown_id_is_404(samples_client):
    resp = samples_client.post(
        "/api/samples/does_not_exist/generate",
        data={"num_questions": "5", "passing_score": "80",
              "scorm_version": "1.2", "output_format": "scorm"},
    )
    assert resp.status_code == 404


def test_generate_route_path_traversal_id_is_404(samples_client):
    resp = samples_client.post(
        "/api/samples/..%2f..%2fetc%2fpasswd/generate",
        data={"num_questions": "5", "passing_score": "80",
              "scorm_version": "1.2", "output_format": "scorm"},
    )
    assert resp.status_code in (404, 400)


@pytest.mark.parametrize("bad_form,expected_status", (
    ({"num_questions": "not-a-number"}, 400),
    ({"num_questions": "9999"}, 400),
    ({"passing_score": "10"}, 400),
    ({"output_format": "xapi"}, 400),
    ({"scorm_version": "9.9"}, 400),
))
def test_generate_route_bad_params_is_400(samples_client, bad_form, expected_status):
    form = {
        "num_questions": "5",
        "passing_score": "80",
        "scorm_version": "1.2",
        "output_format": "scorm",
    }
    form.update(bad_form)
    resp = samples_client.post(
        f"/api/samples/{CATALOG_IDS[0]}/generate", data=form)
    assert resp.status_code == expected_status


def test_generate_route_copies_the_sample_rather_than_pointing_at_it(samples_client, app):
    """The gallery source file must still exist after generation - only the
    upload-dir *copy* is cleaned up, exactly like a real upload."""
    sample_id = CATALOG_IDS[0]
    original = sample_path(sample_id)
    original_bytes = original.read_bytes()

    resp = samples_client.post(
        f"/api/samples/{sample_id}/generate",
        data={"num_questions": "5", "passing_score": "80",
              "scorm_version": "1.2", "output_format": "scorm"},
    )
    assert resp.status_code == 200
    assert original.exists()
    assert original.read_bytes() == original_bytes

    # No leftover copy in the upload folder (retention behaves like a real upload).
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    leftovers = list(upload_root.rglob(original.name))
    assert not leftovers, f"sample copy not cleaned up: {leftovers}"
