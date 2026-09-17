"""SCORM Content Aggregation Model conformance of the generated manifest.

Every assertion here is about ``imsmanifest.xml``: the document an LMS reads
before it will accept the package at all.  The matrix is both SCORM versions x
both assessment generators x both shipped SOP fixtures, because the manifest is
built from the training module and a fixture with a different section count is
a different manifest.

The negative tests at the bottom are the ones that make the rest mean
something: they feed the checker manifests with the defects this exporter used
to ship and assert that it says so.
"""

import zipfile

import pytest
from lxml import etree

from tests.conformance import manifest_checks as mc
from tests.conformance.conftest import MATRIX, MATRIX_IDS

NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"


@pytest.fixture(params=MATRIX, ids=MATRIX_IDS)
def package(request, package_factory):
    return package_factory(*request.param)


# ---------------------------------------------------------------------------
# The official schemas
# ---------------------------------------------------------------------------
def test_official_xsds_are_vendored():
    """If this fails the harness silently degrades to structural checks only."""
    for version in ("1.2", "2004"):
        assert mc.schemas_available(version), (
            "no vendored XSDs for SCORM {0}; see "
            "tests/conformance/schemas/SOURCES.md".format(version))


def test_schemas_build_without_network():
    """The schema set must resolve entirely from vendored files.

    ``imscp_v1p1.xsd`` imports ``http://www.w3.org/2001/xml.xsd``.  The parser
    is built with ``no_network=True``, so if the local resolver ever stops
    matching, this raises rather than quietly reaching for the internet.
    """
    for version in ("1.2", "2004"):
        assert mc.load_schema(version) is not None


def test_manifest_validates_against_the_official_xsd(package):
    manifest_bytes = (package.dir / "imsmanifest.xml").read_bytes()
    errors = mc.xsd_errors(manifest_bytes, package.version)
    assert not errors, "SCORM {0} XSD validation failed:\n  {1}".format(
        package.version, "\n  ".join(errors))


# ---------------------------------------------------------------------------
# The CAM rules an XSD cannot express
# ---------------------------------------------------------------------------
def test_manifest_passes_the_structural_checks(package):
    problems = mc.check_directory(package.dir, package.version)
    assert not problems, "SCORM {0} ({1}/{2}) manifest problems:\n  {3}".format(
        package.version, package.generator, package.fixture,
        "\n  ".join(problems))


def test_the_zip_and_the_directory_agree(package):
    """The checks run on the directory; the LMS is handed the zip."""
    manifest_bytes = (package.dir / "imsmanifest.xml").read_bytes()
    problems = mc.structural_errors(
        manifest_bytes, package.version,
        mc.package_files_from_zip(package.zip_path))
    assert not problems, "\n  ".join(problems)

    with zipfile.ZipFile(package.zip_path) as archive:
        assert "imsmanifest.xml" in archive.namelist(), (
            "the manifest must be at the archive root, not in a subfolder")


def test_the_binding_is_the_requested_version(package):
    """The namespaces, not just the version string, have to change."""
    spec = mc.SPECS[package.version]
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())

    assert etree.QName(root).namespace == spec.cp_ns
    declared = set(root.nsmap.values())
    assert spec.adlcp_ns in declared
    for uri in spec.extra_ns.values():
        assert uri in declared, uri

    other = mc.SPECS["2004" if package.version == "1.2" else "1.2"]
    assert other.cp_ns not in declared, (
        "the other SCORM version's content-packaging namespace is declared")
    assert other.adlcp_ns not in declared


def test_schemaversion_token_is_exact(package):
    spec = mc.SPECS[package.version]
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())
    element = root.find("{%s}metadata/{%s}schemaversion" % (spec.cp_ns, spec.cp_ns))
    assert element is not None
    assert element.text == spec.schemaversion
    assert element.text in mc.VALID_SCHEMAVERSIONS[package.version]


def test_scormtype_casing_matches_the_version(package):
    """1.2 says ``adlcp:scormtype``; 2004 says ``adlcp:scormType``."""
    spec = mc.SPECS[package.version]
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())
    resources = root.findall("{%s}resources/{%s}resource" % (spec.cp_ns, spec.cp_ns))
    assert resources

    wrong = "scormType" if spec.scormtype == "scormtype" else "scormtype"
    for resource in resources:
        assert resource.get(spec.scormtype_attr) in ("sco", "asset"), (
            resource.get("identifier"))
        assert resource.get("{%s}%s" % (spec.adlcp_ns, wrong)) is None


def test_every_item_resolves_to_a_launchable_resource(package):
    spec = mc.SPECS[package.version]
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())
    resources = {
        resource.get("identifier"): resource
        for resource in root.findall(
            "{%s}resources/{%s}resource" % (spec.cp_ns, spec.cp_ns))}

    items = root.findall(
        "{%s}organizations/{%s}organization/{%s}item"
        % (spec.cp_ns, spec.cp_ns, spec.cp_ns))
    # one content page per section, plus the assessment
    assert len(items) == len(package.training.sections) + 1
    assert items[-1].get("identifier") == "ITEM-ASSESSMENT"

    for item in items:
        reference = item.get("identifierref")
        assert reference in resources, item.get("identifier")
        resource = resources[reference]
        href = resource.get("href")
        assert href, reference
        assert (package.dir / href).exists(), href
        assert resource.get(spec.scormtype_attr) == "sco"


def test_every_page_declares_the_assets_it_loads(package):
    """A resource that omits ``scorm_api.js`` launches a SCO that cannot talk
    to the LMS at all, because ``initializeSCORM`` is undefined."""
    spec = mc.SPECS[package.version]
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())

    for resource in root.findall(
            "{%s}resources/{%s}resource" % (spec.cp_ns, spec.cp_ns)):
        href = resource.get("href")
        if not href:
            continue
        declared = {element.get("href")
                    for element in resource.findall("{%s}file" % spec.cp_ns)}
        page = (package.dir / href).read_text(encoding="utf-8")
        assert href in declared
        for asset in ("styles.css", "scorm_api.js"):
            assert asset in page, "{0} no longer loads {1}".format(href, asset)
            assert asset in declared, (
                "resource {0} loads {1} but does not declare it"
                .format(resource.get("identifier"), asset))


def test_no_file_in_the_package_is_left_undeclared(package):
    declared = set()
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())
    for element in root.iter("{%s}file" % mc.SPECS[package.version].cp_ns):
        declared.add(element.get("href"))
    present = set(mc.package_files_from_dir(package.dir)) - {"imsmanifest.xml"}
    assert present <= declared, sorted(present - declared)


def test_the_pass_mark_is_expressed_the_way_the_version_expresses_it(package):
    """SCORM 1.2: ``<adlcp:masteryscore>``.  SCORM 2004: a primary objective
    satisfied by a normalised measure."""
    spec = mc.SPECS[package.version]
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())
    item = root.find(
        "{%s}organizations/{%s}organization/{%s}item[@identifier='ITEM-ASSESSMENT']"
        % (spec.cp_ns, spec.cp_ns, spec.cp_ns))
    assert item is not None

    if package.version == "1.2":
        mastery = item.find("{%s}masteryscore" % spec.adlcp_ns)
        assert mastery is not None, "no <adlcp:masteryscore> on the assessment"
        assert mastery.text == str(package.passing_score)
    else:
        imsss = spec.extra_ns["imsss"]
        measure = item.find(
            "{%s}sequencing/{%s}objectives/{%s}primaryObjective/"
            "{%s}minNormalizedMeasure" % (imsss, imsss, imsss, imsss))
        assert measure is not None, "no primary objective on the assessment"
        assert abs(float(measure.text) - package.passing_score / 100.0) < 1e-9
        assert 0.0 <= float(measure.text) <= 1.0


def test_2004_declares_sequencing_on_the_organization(package):
    if package.version != "2004":
        pytest.skip("SCORM 1.2 has no sequencing")
    spec = mc.SPECS["2004"]
    imsss = spec.extra_ns["imsss"]
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())
    control = root.find(
        "{%s}organizations/{%s}organization/{%s}sequencing/{%s}controlMode"
        % (spec.cp_ns, spec.cp_ns, imsss, imsss))
    assert control is not None
    assert control.get("choice") == "true"
    assert control.get("flow") == "true"


def test_schema_location_maps_every_declared_namespace(package):
    root = etree.fromstring((package.dir / "imsmanifest.xml").read_bytes())
    location = root.get("{%s}schemaLocation" % NS_XSI, "").split()
    assert location and len(location) % 2 == 0
    mapped = set(location[0::2])
    spec = mc.SPECS[package.version]
    assert spec.cp_ns in mapped
    assert spec.adlcp_ns in mapped
    for uri in spec.extra_ns.values():
        assert uri in mapped, uri


# ---------------------------------------------------------------------------
# The checker itself must be able to fail
# ---------------------------------------------------------------------------
SCORM12_MANIFEST_WITH = """<?xml version="1.0" encoding="UTF-8"?>
<manifest xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2"
          xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://www.imsproject.org/xsd/imscp_rootv1p1p2 imscp_rootv1p1p2.xsd"
          identifier="M" version="1.0">
  <metadata><schema>ADL SCORM</schema><schemaversion>%(schemaversion)s</schemaversion></metadata>
  <organizations default="ORG">
    <organization identifier="ORG">
      <title>T</title>
      <item identifier="I1" identifierref="%(ref)s"><title>Page</title></item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="R1" type="webcontent" adlcp:%(scormtype)s="sco" href="a.html">
      <file href="a.html"/>%(extra_file)s
    </resource>
  </resources>
</manifest>
"""


def _manifest_12(schemaversion="1.2", ref="R1", scormtype="scormtype",
                 extra_file=""):
    return (SCORM12_MANIFEST_WITH % {
        "schemaversion": schemaversion, "ref": ref,
        "scormtype": scormtype, "extra_file": extra_file}).encode("utf-8")


def _has(problems, fragment):
    return any(fragment in problem for problem in problems)


def test_checker_accepts_a_minimal_good_12_manifest():
    problems = mc.structural_errors(
        _manifest_12(), "1.2", {"imsmanifest.xml", "a.html"})
    assert not problems, problems


def test_checker_rejects_the_old_2004_schemaversion():
    """The exact defect this exporter shipped: a 1.2 manifest with '2004'."""
    problems = mc.structural_errors(
        _manifest_12(schemaversion="2004"), "1.2", {"imsmanifest.xml", "a.html"})
    assert _has(problems, "schemaversion"), problems


def test_checker_rejects_a_12_binding_asked_for_as_2004():
    problems = mc.structural_errors(
        _manifest_12(schemaversion="2004"), "2004",
        {"imsmanifest.xml", "a.html"})
    assert _has(problems, "OTHER SCORM version"), problems


def test_checker_rejects_a_dangling_identifierref():
    problems = mc.structural_errors(
        _manifest_12(ref="NOPE"), "1.2", {"imsmanifest.xml", "a.html"})
    assert _has(problems, "matches no <resource identifier>"), problems


def test_checker_rejects_the_wrong_scormtype_casing():
    problems = mc.structural_errors(
        _manifest_12(scormtype="scormType"), "1.2",
        {"imsmanifest.xml", "a.html"})
    assert _has(problems, "spells it adlcp:scormtype"), problems


def test_checker_rejects_a_file_missing_from_the_package():
    problems = mc.structural_errors(
        _manifest_12(extra_file='<file href="missing.js"/>'), "1.2",
        {"imsmanifest.xml", "a.html"})
    assert _has(problems, "not in the package"), problems


def test_checker_rejects_a_file_present_but_undeclared():
    """The exporter's old defect: styles.css and scorm_api.js in the zip,
    declared by nothing."""
    problems = mc.structural_errors(
        _manifest_12(), "1.2",
        {"imsmanifest.xml", "a.html", "scorm_api.js", "styles.css"})
    assert _has(problems, "declared by no <resource>"), problems
    assert _has(problems, "scorm_api.js"), problems


def test_checker_rejects_2004_vocabulary_in_a_12_manifest():
    manifest = _manifest_12().replace(
        b'<title>Page</title>',
        b'<title>Page</title><imsss:sequencing '
        b'xmlns:imsss="http://www.imsglobal.org/xsd/imsss"/>')
    problems = mc.structural_errors(
        manifest, "1.2", {"imsmanifest.xml", "a.html"})
    assert _has(problems, "SCORM 2004 vocabulary"), problems


def test_xsd_layer_rejects_a_broken_manifest():
    """A structurally plausible manifest the schema still refuses."""
    broken = _manifest_12().replace(b'type="webcontent"', b'')
    assert mc.xsd_errors(broken, "1.2")
