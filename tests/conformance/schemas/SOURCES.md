# Vendored SCORM CAM schemas — provenance

These are third-party XML Schema files, vendored so `tests/conformance/` can
validate a generated `imsmanifest.xml` **offline**. They are not part of the
shipped product: nothing under `src/` reads them, and they are never copied
into a generated package. They are test data.

Every file below is byte-for-byte as fetched — none has been edited. The one
remote import in the set (`imscp_v1p1.xsd` imports
`http://www.w3.org/2001/xml.xsd`) is redirected to the vendored `xml.xsd` by a
resolver in `manifest_checks.py`, and the parser runs with `no_network=True`,
so a test run never reaches the network.

`_all.xsd` in each directory is **ours**, not ADL's: a two-line wrapper that
imports the namespaces a manifest mixes, so `lxml.etree.XMLSchema` can validate
the whole document in one pass.

Fetched 2026-09-17.

## SCORM 2004 4th Edition — `scorm2004/`

Source: <https://github.com/adlnet/SCORM-2004-4ed-Test-Suite>, path
`software_development/xml/xsd/`, branch `master`. This is ADL's own 4th Edition
conformance test suite — the normative distribution of these bindings.

| File | SHA-256 |
|---|---|
| `imscp_v1p1.xsd` | `37d9a70b8b9b0303cdee1af5839fe8b32c2724c39d8124e714af650da4a08cdb` |
| `adlcp_v1p3.xsd` | `87cefec31b160a1ecc9ffd1475bee6cb0e2d71b2bcd76ba1a08df0ac90cf8f7d` |
| `adlseq_v1p3.xsd` | `162b5ca35a3fc43107efe0f78f5216c76f6cad8e13921c8332b7b82db7c4ae08` |
| `adlnav_v1p3.xsd` | `1fc2678e9acaee07ba8b0da93caa5f52da5167f2999c4adbd0a4ad74c15c8bfb` |
| `imsss_v1p0.xsd` | `53565d8b23842d5b667beffa4ed100e3e06cdd778b60b85efc38e7a9a1f52a90` |
| `imsss_v1p0auxresource.xsd` | `c0dd16d707f37c4d26484d7c233eea6d9ccbb8bad643040359f4b4f2944c057a` |
| `imsss_v1p0control.xsd` | `c8564177ca7b1c241d6ca893652f6946eb91fee450dce8607ba2189ba88e7969` |
| `imsss_v1p0delivery.xsd` | `58d4f343e3302e2986a641b066a3f0b30bd895b62d358a184cb5c3455427392c` |
| `imsss_v1p0limit.xsd` | `debda0a7fe2a1ce6e063383c4efc9fbd5f2341355c85d66062742657be2418a9` |
| `imsss_v1p0objective.xsd` | `776e091df47e934432c78703e552482e83165f75c9bf89c6320ee035ecd7ae69` |
| `imsss_v1p0random.xsd` | `3442393c455d8a984853c1c7cdf1bf6ccf4d0c58452c0e39b6c0aab65339f597` |
| `imsss_v1p0rollup.xsd` | `85100e4c7ba8eff77102d73d2ce265ddd803d13191a1d2b81bf3bf8bb1eac554` |
| `imsss_v1p0seqrule.xsd` | `b8fbd362e2408d318ae97cc70a7d3a8a536db29150b7ef7b355f394d06027023` |
| `imsss_v1p0util.xsd` | `b7234f3f62ede3f8dc6cd35053805ad021b4642b54f34b7960945981be95a438` |
| `xml.xsd` | `95907dad3dea7debdd26bddeab77e3946b95370329994beefa74253be105c3e2` |

Not vendored: the IEEE LOM schema set (`lom.xsd` and its `unique`/`vocab`/
`extend` imports). The generated manifest carries no LOM metadata, so nothing
in it needs those bindings.

## SCORM 1.2 — `scorm12/`

ADL's SCORM 1.2 test suite is not on GitHub and `adlnet.gov` / `imsglobal.org`
are not reachable from this environment (the proxy returns 403). The files were
taken instead from long-standing open-source mirrors, and each was fetched from
**three independent repositories and compared byte-for-byte** before being
vendored — the three copies are identical, which is the check that they are the
unmodified ADL/IMS originals rather than one project's local patch.

| File | SHA-256 | Mirrors compared |
|---|---|---|
| `imscp_rootv1p1p2.xsd` | `e45606348d7f517874d70ec102796526a63a85e54a4a1db5e6a5944449674d31` | `adaptlearning/adapt-contrib-spoor` `scorm/1.2/`, `atutor/ATutor` `mods/_core/imscp/include/`, `tylershumaker/s1000d-scorm` `xsd_12/` |
| `adlcp_rootv1p2.xsd` | `166397a1f52585caac857228cf2b10085a5d07c0612a3d55cb3ed108ce8b028a` | the same three |
| `ims_xml.xsd` | `9fdd37742066d2c1324427eebb716f14aabe73d0168dd7632fc494bd7e1b388c` | `adaptlearning/adapt-contrib-spoor` and `tylershumaker/s1000d-scorm` are identical once whitespace is normalised; `atutor/ATutor` carries a shortened copy. The vendored file is the two-way-agreeing one. |

`imscp_rootv1p1p2.xsd` and `adlcp_rootv1p2.xsd` are **byte-identical** across
all three mirrors (identical blob hashes), which is the strongest evidence
available here that they are unmodified originals. `ims_xml.xsd` declares only
`xml:lang`, `xml:base` and `xml:link`; it carries no SCORM vocabulary, so a
mirror disagreement there cannot weaken or loosen a conformance check.

Not vendored: `imsmd_rootv1p2p2.xsd` (IMS metadata 1.2). It is not on any of
the mirrors, and the generated manifest declares no `imsmd` elements — the
`<metadata>` block carries only `<schema>` and `<schemaversion>`, both of which
are in the content-packaging namespace. If IMS metadata is ever emitted, this
schema must be vendored too and the check in `manifest_checks.py` extended.

## Copyright and licence

The schemas carry their originators' notices in-file:

- `imscp_rootv1p1p2.xsd`, `imscp_v1p1.xsd`, `imsss_*.xsd` — "Copyright (c) 2001
  IMS GLC, Inc." (IMS Global Learning Consortium, now 1EdTech). IMS publishes
  its bindings for implementers to use in building and testing conformant
  systems.
- `adlcp_*.xsd`, `adlseq_v1p3.xsd`, `adlnav_v1p3.xsd` — Advanced Distributed
  Learning (ADL) Initiative, U.S. Department of Defense. ADL distributes SCORM
  and its bindings for unrestricted use under the SCORM licence, which permits
  reproduction and use provided the ADL notices are retained. Works of the U.S.
  federal government are not subject to domestic copyright.
- `xml.xsd` / `ims_xml.xsd` — the W3C `xml:` namespace attribute bindings, W3C
  Software and Document Notice and Licence.

No notice has been stripped from any file. Neither ADL nor IMS/1EdTech endorses
this project, and passing the checks in `tests/conformance/` is **not** an ADL
certification: see `docs/SCORM_CONFORMANCE.md` for what these tests do and do
not establish.
