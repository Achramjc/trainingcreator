"""SCORM conformance harness.

Two layers, both run in CI:

``manifest_checks``
    Validates ``imsmanifest.xml`` against the official ADL/IMS XSDs vendored
    under ``schemas/``, and then against the Content Aggregation Model rules
    an XSD cannot express (identifier resolution, files present in the
    archive, ``scormtype`` casing per version, the exact ``schemaversion``
    token).

``fake_lms``
    A recording LMS - a host page that publishes ``API`` (SCORM 1.2) or
    ``API_1484_11`` (SCORM 2004) on ``window`` and loads the package's page
    two frames down, so the SCO has to find the API by walking its ancestors,
    which is the path a real LMS puts it on.  Every call, argument and return
    value is recorded and asserted against the data model the version defines.

What this does not do is in ``docs/SCORM_CONFORMANCE.md``.
"""
