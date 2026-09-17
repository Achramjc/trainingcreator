"""
Sample-SOP gallery loader.

Reads the catalog at examples/gallery/index.json and resolves each entry's
document path relative to the repository root. This is the only module that
knows the on-disk layout of the gallery; callers (the Flask blueprint, the
tests) go through ``list_samples`` / ``get_sample`` / ``sample_path`` rather
than touching examples/gallery/ directly, so a path is never built from
untrusted input.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
GALLERY_DIR = REPO_ROOT / "examples" / "gallery"
CATALOG_PATH = GALLERY_DIR / "index.json"

#: Sample ids are used to build filesystem paths (via the catalog) and URL
#: segments, so they are restricted to a conservative charset up front -
#: independent of whatever happens to be in the catalog file.
_ID_RE = re.compile(r"^[a-z0-9_]+$")


class UnknownSampleError(KeyError):
    """Raised by ``sample_path`` for an id that isn't in the catalog."""


def _load_catalog() -> List[Dict]:
    """Read and parse examples/gallery/index.json.

    Not cached: the catalog is small, read rarely (once per gallery listing
    or generate request), and re-reading means an edit to index.json is
    picked up without restarting the process.
    """
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def list_samples() -> List[Dict]:
    """Return the gallery catalog, one dict per sample, paths resolved.

    Each entry carries everything in index.json plus a ``resolved_path``
    (an absolute ``Path``) resolved against the repository root. Callers
    that expose this over an API (see src/samples_routes.py) should strip
    filesystem paths before sending it to a client.
    """
    entries = []
    for entry in _load_catalog():
        resolved = dict(entry)
        resolved["resolved_path"] = REPO_ROOT / entry["path"]
        entries.append(resolved)
    return entries


def get_sample(sample_id: str) -> Optional[Dict]:
    """Return the catalog entry for ``sample_id``, or None if it doesn't exist.

    Does not validate ``sample_id`` against ``_ID_RE`` -- an id that isn't in
    the catalog simply isn't found, whatever shape it is. Use ``sample_path``
    when the id is going to be used to reach the filesystem.
    """
    for entry in list_samples():
        if entry["id"] == sample_id:
            return entry
    return None


def sample_path(sample_id: str) -> Path:
    """Return the resolved document path for ``sample_id``.

    Strict by design: ``sample_id`` must match ``_ID_RE`` AND be present in
    the catalog. This is the only function in this module that should ever
    be used to turn a caller-supplied id into a filesystem path - it never
    joins untrusted input onto a directory itself, it only looks up a path
    the catalog already named.

    Raises:
        ValueError: ``sample_id`` doesn't match the allowed id charset.
        UnknownSampleError: ``sample_id`` is well-formed but not in the
            catalog.
    """
    if not isinstance(sample_id, str) or not _ID_RE.match(sample_id):
        raise ValueError(f"invalid sample id: {sample_id!r}")

    entry = get_sample(sample_id)
    if entry is None:
        raise UnknownSampleError(sample_id)

    return entry["resolved_path"]
