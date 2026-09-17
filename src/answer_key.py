"""
answer_key.py - deterministic, client-verifiable answer keys for static packages.

WHY THIS MODULE EXISTS
----------------------
A SCORM package is a bag of static files (HTML/JS/XML) that an LMS unzips and
serves.  There is no server of ours in the loop at run time, so *some*
representation of the answer key has to travel to the learner's browser or the
package could not be scored at all.  The question is only what that
representation reveals.

What this module ships to the learner, per question:

    salt         32 hex chars, derived deterministically from the document
    answer_hash  sha256( salt + "|" + normalise(correct_option_text) )

What it does NOT ship: the correct option index, the correct option text as a
flagged field, or the explanation.  Viewing source no longer hands the learner
the key.

HONEST LIMITS (do not oversell this)
------------------------------------
* A learner with browser dev tools can hash all 2-4 rendered options against the
  published salt and find which one matches.  That is a few seconds of work for
  someone who knows how.  The salt stops precomputed/rainbow lookups and stops
  the key being *readable*; it does not stop brute force over a tiny candidate
  set.  That is inherent to static content and cannot be fixed here.
* So this raises the bar from "view source" (trivial, accidental) to "write a
  script against crypto.subtle" (deliberate, requires intent).  It is not
  tamper-proof scoring.
* The real fix is server- or LMS-verified scoring: submit the chosen option, let
  the grader hold the key.  That is milestone M2.  Until then a training record
  produced by this package is completion evidence under an honour system, and
  customers should be told so.

NORMALISATION
-------------
The hash covers *normalised option text*, not the raw string, so that trivial
rendering differences (whitespace, punctuation, HTML entity round-trips, NFC vs
NFKC forms) cannot break verification in a browser.  The Python and JavaScript
normalisers below must stay behaviourally identical:

    1. Unicode NFKC
    2. lowercase
    3. every character that is not a Unicode letter or number becomes a space
       (Python: str.isalnum() == \\p{L} u \\p{N}; JS: /[^\\p{L}\\p{N}]+/gu)
    4. collapse runs of spaces, strip

tests/test_scorm.py re-implements this independently and cross-checks the
shipped JavaScript with node when node is available.
"""

import hashlib
import unicodedata
from typing import List

__all__ = [
    "normalize_option_text",
    "document_key",
    "question_digest",
    "make_salt",
    "answer_hash",
    "seed_from_digest",
    "CLIENT_VERIFIER_JS",
]

SALT_LENGTH = 32


def normalize_option_text(text: str) -> str:
    """Normalise an option string for hashing.

    Mirrors normalizeOptionText() in CLIENT_VERIFIER_JS - change both or neither.
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = s.lower()
    out: List[str] = []
    pending_space = False
    for ch in s:
        if ch.isalnum():
            if pending_space and out:
                out.append(" ")
            out.append(ch)
            pending_space = False
        else:
            pending_space = True
    return "".join(out)


def document_key(title: str, version: str) -> str:
    """Stable digest of the source document's identity.

    Reproducible builds matter for validation: the same SOP title + revision must
    always produce the same package, so every seed used by the generator descends
    from this value rather than from wall-clock time or os.urandom.
    """
    material = "{0}|{1}".format((title or "").strip(), (version or "").strip())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def question_digest(doc_key: str, question_id: str) -> str:
    """Stable per-question digest: sha256(doc_key | question_id)."""
    return hashlib.sha256(
        "{0}|{1}".format(doc_key, question_id).encode("utf-8")
    ).hexdigest()


def make_salt(doc_key: str, question_id: str) -> str:
    """Per-question salt.

    Public (it ships in the package), deterministic, and distinct per question so
    that cracking one answer tells an attacker nothing about the next.
    """
    return hashlib.sha256(
        "{0}|{1}|salt".format(doc_key, question_id).encode("utf-8")
    ).hexdigest()[:SALT_LENGTH]


def answer_hash(salt: str, option_text: str) -> str:
    """sha256(salt + '|' + normalise(option_text)) as 64 lowercase hex chars."""
    material = "{0}|{1}".format(salt, normalize_option_text(option_text))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def seed_from_digest(digest: str) -> int:
    """Turn a hex digest into a seed for random.Random()."""
    return int(digest[:16], 16)


# ---------------------------------------------------------------------------
# Client-side verifier.  Inlined into assessment.html by the SCORM exporter, so
# the package file layout is unchanged.
#
# crypto.subtle is used when it exists, but it is only exposed in a *secure
# context*.  SCORM content is routinely opened from file:// (QA review, SME
# preview, offline LMS players) and from plain http:// intranet LMS hosts, where
# window.crypto.subtle is undefined.  A package that silently fails to score in
# those cases is worse than one that is a few milliseconds slower, so a compact
# pure-JS SHA-256 is carried as a fallback.  Both paths must agree with
# answer_hash() above; the test suite checks that with node.
# ---------------------------------------------------------------------------
CLIENT_VERIFIER_JS = r"""
/* Answer verification helpers.
 *
 * The package ships, per question, a public salt and
 * sha256(salt + "|" + normalise(right option text)).  It does NOT ship the
 * answer index, the answer text as such, or the SME rationale.
 *
 * Known limit, stated plainly: anyone who opens dev tools can hash the 2-4
 * rendered options against the published salt and find the match.  This stops
 * "view source", not a determined learner.  Server-verified scoring is the real
 * fix and is not possible inside a static SCORM package.
 */
var AnswerKey = (function () {
    "use strict";

    var NON_ALNUM;
    var UNICODE_CLASSES_OK = true;
    try {
        NON_ALNUM = new RegExp("[^\\p{L}\\p{N}]+", "gu");
    } catch (e) {
        /* Pre-ES2018 engine: fall back to ASCII-only classing.  Identical
         * results for ASCII source documents, divergent for accented text, so
         * expose a flag rather than fail silently. */
        NON_ALNUM = /[^0-9a-z]+/g;
        UNICODE_CLASSES_OK = false;
    }

    function normalizeOptionText(text) {
        var s = (text === null || text === undefined) ? "" : String(text);
        if (typeof s.normalize === "function") {
            s = s.normalize("NFKC");
        }
        s = s.toLowerCase();
        return s.replace(NON_ALNUM, " ").replace(/^ +| +$/g, "");
    }

    function utf8Bytes(str) {
        var out = [];
        for (var i = 0; i < str.length; i++) {
            var c = str.charCodeAt(i);
            if (c < 0x80) {
                out.push(c);
            } else if (c < 0x800) {
                out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
            } else if (c >= 0xd800 && c <= 0xdbff && i + 1 < str.length) {
                var c2 = str.charCodeAt(i + 1);
                if (c2 >= 0xdc00 && c2 <= 0xdfff) {
                    var cp = 0x10000 + ((c - 0xd800) << 10) + (c2 - 0xdc00);
                    out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63),
                             0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
                    i++;
                    continue;
                }
                out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
            } else {
                out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
            }
        }
        return new Uint8Array(out);
    }

    var K = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
        0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
        0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
        0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
        0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    ];

    function rotr(x, n) {
        return ((x >>> n) | (x << (32 - n))) >>> 0;
    }

    function sha256Fallback(bytes) {
        var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
        var l = bytes.length;
        var padded = new Uint8Array(((((l + 8) >> 6) + 1) << 6));
        padded.set(bytes);
        padded[l] = 0x80;
        var dv = new DataView(padded.buffer);
        dv.setUint32(padded.length - 8, Math.floor(l / 536870912) >>> 0);
        dv.setUint32(padded.length - 4, (l * 8) >>> 0);

        var w = new Uint32Array(64);
        for (var i = 0; i < padded.length; i += 64) {
            var j;
            for (j = 0; j < 16; j++) {
                w[j] = dv.getUint32(i + j * 4);
            }
            for (j = 16; j < 64; j++) {
                var x = w[j - 15], y = w[j - 2];
                var s0 = (rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3)) >>> 0;
                var s1 = (rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10)) >>> 0;
                w[j] = (w[j - 16] + s0 + w[j - 7] + s1) >>> 0;
            }
            var a = H[0], b = H[1], c = H[2], d = H[3];
            var e = H[4], f = H[5], g = H[6], h = H[7];
            for (j = 0; j < 64; j++) {
                var S1 = (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) >>> 0;
                var ch = ((e & f) ^ ((~e) & g)) >>> 0;
                var t1 = (h + S1 + ch + K[j] + w[j]) >>> 0;
                var S0 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) >>> 0;
                var maj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
                var t2 = (S0 + maj) >>> 0;
                h = g; g = f; f = e; e = (d + t1) >>> 0;
                d = c; c = b; b = a; a = (t1 + t2) >>> 0;
            }
            H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
            H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
            H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
            H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
        }
        var hex = "";
        for (var k = 0; k < 8; k++) {
            hex += ("00000000" + H[k].toString(16)).slice(-8);
        }
        return hex;
    }

    function toHex(buffer) {
        var view = new Uint8Array(buffer);
        var hex = "";
        for (var i = 0; i < view.length; i++) {
            hex += ("0" + view[i].toString(16)).slice(-2);
        }
        return hex;
    }

    /* Callback style rather than Promise style, so the page also works in older
     * LMS-embedded browsers that lack Promise. */
    function sha256Hex(str, cb) {
        var bytes = utf8Bytes(str);
        var subtle = null;
        try {
            subtle = (typeof crypto !== "undefined" && crypto && crypto.subtle) ||
                     (typeof msCrypto !== "undefined" && msCrypto && msCrypto.subtle) ||
                     null;
        } catch (e) {
            subtle = null;
        }
        if (subtle && typeof subtle.digest === "function") {
            try {
                var result = subtle.digest("SHA-256", bytes);
                if (result && typeof result.then === "function") {
                    result.then(function (buf) { cb(toHex(buf)); },
                                function () { cb(sha256Fallback(bytes)); });
                    return;
                }
                /* Legacy CryptoOperation (IE11): just use the fallback. */
            } catch (e2) { /* fall through */ }
        }
        cb(sha256Fallback(bytes));
    }

    function verify(salt, optionText, expectedHash, cb) {
        sha256Hex(salt + "|" + normalizeOptionText(optionText), function (h) {
            cb(h === expectedHash);
        });
    }

    return {
        normalize: normalizeOptionText,
        sha256Hex: sha256Hex,
        verify: verify,
        unicodeClassesSupported: UNICODE_CLASSES_OK
    };
})();

if (typeof module !== "undefined" && module.exports) {
    module.exports = AnswerKey;   /* node, for the parity test only */
}
"""
