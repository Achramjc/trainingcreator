"""Content Aggregation Model checks for a generated SCORM package.

Two independent layers:

* :func:`xsd_errors` validates ``imsmanifest.xml`` against the official
  ADL/IMS schemas vendored in ``schemas/`` (see ``schemas/SOURCES.md``).
  Nothing is fetched at test time: the one remote import in the 2004 set
  (``http://www.w3.org/2001/xml.xsd``) is resolved to the vendored copy, and
  the parser is built with ``no_network=True`` so a silent fallback to the
  network is impossible.

* :func:`structural_errors` encodes the CAM rules an XSD cannot express: that
  ``identifierref`` resolves, that every declared ``<file>`` is really in the
  archive and every file in the archive is declared, that ``scormtype`` is
  spelled the way the requested version spells it, and that
  ``<schemaversion>`` carries the exact token the version defines.

Every failure is a sentence naming the element and what was expected, because
a conformance failure that reads "validation error" costs an afternoon.
"""

import zipfile
from pathlib import Path

from lxml import etree

SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"

NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"


class VersionSpec:
    """The binding one SCORM version defines."""

    def __init__(self, label, directory, cp_ns, adlcp_ns, scormtype,
                 schemaversion, extra_ns=()):
        self.label = label
        self.directory = directory
        self.cp_ns = cp_ns
        self.adlcp_ns = adlcp_ns
        #: local name of the scormtype attribute - the casing differs
        self.scormtype = scormtype
        self.schemaversion = schemaversion
        self.extra_ns = dict(extra_ns)

    @property
    def scormtype_attr(self):
        return "{%s}%s" % (self.adlcp_ns, self.scormtype)


SPECS = {
    "1.2": VersionSpec(
        label="SCORM 1.2",
        directory="scorm12",
        cp_ns="http://www.imsproject.org/xsd/imscp_rootv1p1p2",
        adlcp_ns="http://www.adlnet.org/xsd/adlcp_rootv1p2",
        scormtype="scormtype",
        schemaversion="1.2",
    ),
    "2004": VersionSpec(
        label="SCORM 2004 4th Edition",
        directory="scorm2004",
        cp_ns="http://www.imsglobal.org/xsd/imscp_v1p1",
        adlcp_ns="http://www.adlnet.org/xsd/adlcp_v1p3",
        scormtype="scormType",
        schemaversion="2004 4th Edition",
        extra_ns={
            "adlseq": "http://www.adlnet.org/xsd/adlseq_v1p3",
            "adlnav": "http://www.adlnet.org/xsd/adlnav_v1p3",
            "imsss": "http://www.imsglobal.org/xsd/imsss",
        },
    ),
}

#: Tokens SCORM defines for <schemaversion>.  "2004" alone is not one of them,
#: which is what this exporter used to emit.
VALID_SCHEMAVERSIONS = {
    "1.2": {"1.2"},
    "2004": {"2004 3rd Edition", "2004 4th Edition"},
}

#: The other version's namespaces, so a manifest that declares the wrong
#: binding is reported as that rather than as a wall of schema errors.
_OTHER_CP_NS = {
    "1.2": SPECS["2004"].cp_ns,
    "2004": SPECS["1.2"].cp_ns,
}


class _VendoredResolver(etree.Resolver):
    """Resolve a schema's remote imports to the vendored copy next to it."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def resolve(self, url, pubid, context):
        local = self.directory / url.rsplit("/", 1)[-1]
        if local.exists():
            return self.resolve_filename(str(local), context)
        return None


_SCHEMA_CACHE = {}


def schemas_available(version):
    """True when the official XSDs for *version* are vendored in this tree."""
    return (SCHEMA_ROOT / SPECS[version].directory / "_all.xsd").exists()


def load_schema(version):
    """Build (and cache) the ``lxml`` XMLSchema for *version*, offline."""
    if version in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[version]
    directory = SCHEMA_ROOT / SPECS[version].directory
    parser = etree.XMLParser(no_network=True)
    parser.resolvers.add(_VendoredResolver(directory))
    document = etree.parse(str(directory / "_all.xsd"), parser)
    schema = etree.XMLSchema(document)
    _SCHEMA_CACHE[version] = schema
    return schema


def xsd_errors(manifest_bytes, version):
    """Validate against the official schemas. Returns a list of messages."""
    schema = load_schema(version)
    try:
        document = etree.fromstring(manifest_bytes)
    except etree.XMLSyntaxError as exc:
        return ["imsmanifest.xml is not well-formed XML: {0}".format(exc)]
    if schema.validate(document):
        return []
    return ["line {0}: {1}".format(entry.line, entry.message)
            for entry in schema.error_log]


# ---------------------------------------------------------------------------
# Structural rules
# ---------------------------------------------------------------------------
def _local(element):
    return etree.QName(element).localname


def _text(element):
    return (element.text or "").strip()


def structural_errors(manifest_bytes, version, package_files):
    """Check the CAM rules an XSD cannot express.

    Args:
        manifest_bytes: the raw ``imsmanifest.xml``.
        version: ``"1.2"`` or ``"2004"``.
        package_files: every file in the package, as archive-relative
            POSIX paths (``imsmanifest.xml`` included).

    Returns:
        A list of human-readable problems; empty means conformant.
    """
    spec = SPECS[version]
    problems = []
    package_files = set(package_files)

    try:
        root = etree.fromstring(manifest_bytes)
    except etree.XMLSyntaxError as exc:
        return ["imsmanifest.xml is not well-formed XML: {0}".format(exc)]

    # -- root element and binding -------------------------------------------
    if _local(root) != "manifest":
        problems.append(
            "root element is <{0}>, expected <manifest>".format(_local(root)))
    root_ns = etree.QName(root).namespace
    if root_ns != spec.cp_ns:
        if root_ns == _OTHER_CP_NS[version]:
            problems.append(
                "<manifest> is bound to {0}, which is the OTHER SCORM "
                "version's content-packaging namespace; {1} requires {2}"
                .format(root_ns, spec.label, spec.cp_ns))
        else:
            problems.append(
                "<manifest> namespace is {0!r}, {1} requires {2!r}"
                .format(root_ns, spec.label, spec.cp_ns))
    if not (root.get("identifier") or "").strip():
        problems.append("<manifest> has no identifier attribute")
    if not (root.get("version") or "").strip():
        problems.append("<manifest> has no version attribute")

    declared = set(root.nsmap.values())
    if spec.adlcp_ns not in declared:
        problems.append(
            "the ADL CP namespace {0} is not declared on <manifest>"
            .format(spec.adlcp_ns))
    for prefix, uri in spec.extra_ns.items():
        if uri not in declared:
            problems.append(
                "{0} requires the {1} namespace {2}; it is not declared "
                "on <manifest>".format(spec.label, prefix, uri))

    schema_location = root.get("{%s}schemaLocation" % NS_XSI, "")
    if not schema_location:
        problems.append("<manifest> carries no xsi:schemaLocation")
    elif spec.cp_ns not in schema_location.split():
        problems.append(
            "xsi:schemaLocation does not map {0}".format(spec.cp_ns))

    def find(parent, name):
        return parent.findall("{%s}%s" % (spec.cp_ns, name))

    def find_one(parent, name):
        found = find(parent, name)
        return found[0] if found else None

    # -- metadata -----------------------------------------------------------
    metadata = find_one(root, "metadata")
    if metadata is None:
        problems.append("<manifest> has no <metadata> element")
    else:
        schema_element = find_one(metadata, "schema")
        if schema_element is None or _text(schema_element) != "ADL SCORM":
            problems.append(
                "<metadata><schema> must be exactly 'ADL SCORM', found {0!r}"
                .format(None if schema_element is None
                        else _text(schema_element)))
        version_element = find_one(metadata, "schemaversion")
        found_version = (None if version_element is None
                         else _text(version_element))
        if found_version not in VALID_SCHEMAVERSIONS[version]:
            problems.append(
                "<metadata><schemaversion> is {0!r}; {1} requires one of {2}"
                .format(found_version, spec.label,
                        sorted(VALID_SCHEMAVERSIONS[version])))
        elif found_version != spec.schemaversion:
            problems.append(
                "<metadata><schemaversion> is {0!r}; this harness targets "
                "{1!r}".format(found_version, spec.schemaversion))

    # -- organizations ------------------------------------------------------
    organizations = find_one(root, "organizations")
    if organizations is None:
        problems.append("<manifest> has no <organizations> element")
        organization_list = []
        default = None
    else:
        organization_list = find(organizations, "organization")
        default = organizations.get("default")
        if not default:
            problems.append("<organizations> has no default attribute")
        elif default not in {o.get("identifier") for o in organization_list}:
            problems.append(
                "<organizations default={0!r}> does not name any "
                "<organization>".format(default))
        if not organization_list:
            problems.append("<organizations> contains no <organization>")

    # -- identifiers --------------------------------------------------------
    identifiers = []
    for element in root.iter():
        value = element.get("identifier")
        if value is not None and _local(element) in (
                "organization", "item", "resource"):
            identifiers.append((_local(element), value))
    seen = {}
    for kind, value in identifiers:
        if value in seen:
            problems.append(
                "identifier {0!r} is used by more than one element "
                "(<{1}> and <{2}>); identifiers must be unique in a manifest"
                .format(value, seen[value], kind))
        else:
            seen[value] = kind

    # -- resources ----------------------------------------------------------
    resources_element = find_one(root, "resources")
    resources = find(resources_element, "resource") if resources_element is not None else []
    if resources_element is None:
        problems.append("<manifest> has no <resources> element")
    resource_by_id = {}
    for resource in resources:
        identifier = resource.get("identifier")
        if not identifier:
            problems.append("a <resource> has no identifier attribute")
            continue
        resource_by_id[identifier] = resource

    # -- items --------------------------------------------------------------
    item_count = 0
    launchable = set()
    for organization in organization_list:
        for item in organization.iter("{%s}item" % spec.cp_ns):
            item_count += 1
            identifier = item.get("identifier") or "<no identifier>"
            title = find_one(item, "title")
            if title is None or not _text(title):
                problems.append(
                    "item {0} has no non-empty <title>".format(identifier))
            reference = item.get("identifierref")
            if reference is None:
                continue
            if reference not in resource_by_id:
                problems.append(
                    "item {0} has identifierref={1!r}, which matches no "
                    "<resource identifier>; the LMS has nothing to launch"
                    .format(identifier, reference))
            else:
                launchable.add(reference)
    if not item_count:
        problems.append("no <item> elements: the organization is empty")

    # -- scormtype, href and files -----------------------------------------
    wrong_case = "{%s}%s" % (
        spec.adlcp_ns, "scormType" if spec.scormtype == "scormtype" else "scormtype")
    declared_files = set()
    sco_count = 0

    for resource in resources:
        identifier = resource.get("identifier") or "<no identifier>"
        if resource.get("type") != "webcontent":
            problems.append(
                "resource {0} has type={1!r}; SCORM requires 'webcontent'"
                .format(identifier, resource.get("type")))

        scormtype = resource.get(spec.scormtype_attr)
        if resource.get(wrong_case) is not None and scormtype is None:
            problems.append(
                "resource {0} uses adlcp:{1}; {2} spells it adlcp:{3}"
                .format(identifier, etree.QName(wrong_case).localname,
                        spec.label, spec.scormtype))
        elif scormtype is None:
            problems.append(
                "resource {0} has no adlcp:{1} attribute"
                .format(identifier, spec.scormtype))
        elif scormtype not in ("sco", "asset"):
            problems.append(
                "resource {0} has adlcp:{1}={2!r}; the only values are "
                "'sco' and 'asset'".format(identifier, spec.scormtype,
                                           scormtype))

        href = resource.get("href")
        if scormtype == "sco":
            sco_count += 1
            if not href:
                problems.append(
                    "resource {0} is a SCO with no href: there is nothing "
                    "for the LMS to launch".format(identifier))
        if identifier in launchable and not href:
            problems.append(
                "resource {0} is referenced by an item but has no href"
                .format(identifier))

        own_files = set()
        for element in resource.findall("{%s}file" % spec.cp_ns):
            file_href = element.get("href")
            if not file_href:
                problems.append(
                    "resource {0} has a <file> with no href".format(identifier))
                continue
            own_files.add(file_href)
            declared_files.add(file_href)
            for bad, why in (("\\", "backslashes"), ("..", "parent segments")):
                if bad in file_href:
                    problems.append(
                        "resource {0} declares <file href={1!r}> containing "
                        "{2}; hrefs must be relative POSIX paths inside the "
                        "package".format(identifier, file_href, why))
            if file_href.startswith("/") or "://" in file_href:
                problems.append(
                    "resource {0} declares <file href={1!r}>; hrefs must be "
                    "relative to the package root".format(identifier, file_href))
            if file_href not in package_files:
                problems.append(
                    "resource {0} declares <file href={1!r}> but that file is "
                    "not in the package".format(identifier, file_href))

        if href:
            if href not in package_files:
                problems.append(
                    "resource {0} has href={1!r} but that file is not in the "
                    "package".format(identifier, href))
            if href not in own_files:
                problems.append(
                    "resource {0} has href={1!r} but does not declare it as "
                    "one of its <file> elements".format(identifier, href))

    if not sco_count:
        problems.append(
            "no resource is adlcp:{0}='sco'; a package with no SCO reports "
            "nothing to the LMS".format(spec.scormtype))

    undeclared = sorted(
        name for name in package_files
        if name != "imsmanifest.xml" and name not in declared_files)
    if undeclared:
        problems.append(
            "these files are in the package but declared by no <resource>, "
            "so an LMS that deploys only what the manifest declares will drop "
            "them: {0}".format(", ".join(undeclared)))

    # -- version-specific vocabulary ---------------------------------------
    used_namespaces = set()
    for element in root.iter():
        namespace = etree.QName(element).namespace
        if namespace:
            used_namespaces.add(namespace)
    if version == "1.2":
        forbidden = used_namespaces & {
            SPECS["2004"].cp_ns, SPECS["2004"].adlcp_ns,
            "http://www.imsglobal.org/xsd/imsss",
            "http://www.adlnet.org/xsd/adlseq_v1p3",
            "http://www.adlnet.org/xsd/adlnav_v1p3",
        }
        if forbidden:
            problems.append(
                "a SCORM 1.2 manifest uses SCORM 2004 vocabulary: {0}"
                .format(", ".join(sorted(forbidden))))
    else:
        forbidden = used_namespaces & {SPECS["1.2"].cp_ns, SPECS["1.2"].adlcp_ns}
        if forbidden:
            problems.append(
                "a SCORM 2004 manifest uses SCORM 1.2 vocabulary: {0}"
                .format(", ".join(sorted(forbidden))))

    return problems


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------
def package_files_from_dir(package_dir):
    package_dir = Path(package_dir)
    return sorted(
        str(path.relative_to(package_dir).as_posix())
        for path in package_dir.rglob("*") if path.is_file())


def package_files_from_zip(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        return sorted(name for name in archive.namelist()
                      if not name.endswith("/"))


def check_directory(package_dir, version):
    """All problems with the package in *package_dir*, XSD first."""
    package_dir = Path(package_dir)
    manifest_bytes = (package_dir / "imsmanifest.xml").read_bytes()
    problems = []
    if schemas_available(version):
        problems += ["XSD: " + message
                     for message in xsd_errors(manifest_bytes, version)]
    problems += structural_errors(manifest_bytes, version,
                                  package_files_from_dir(package_dir))
    return problems
