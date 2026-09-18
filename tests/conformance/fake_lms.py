"""A recording fake LMS, and the HTTP server that hosts it.

The point of this file is that the SCO is never handed its API.  The host page
publishes ``API`` (SCORM 1.2) or ``API_1484_11`` (SCORM 2004) on its own
``window`` and then loads the package two frames down, so ``scorm_api.js`` has
to walk ``window.parent`` to find it - which is the path a real LMS puts a SCO
on, and the path that silently fails when the wrapper looks for the wrong
object.

Everything the SCO calls is recorded: the function name, the arguments, the
JavaScript ``typeof`` each argument, and the return value.  The data model is
a dictionary with the initial values the specification requires, so a SCO that
reads before it writes sees what an LMS would give it.

The pages are served over ``http://127.0.0.1`` rather than ``file://`` so that
relative asset paths (``styles.css``, ``scorm_api.js``) resolve exactly as they
do in a deployed package.
"""

import contextlib
import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path

#: Initial data-model values, per SCORM version.  A learner who has never
#: touched the SCO gets these, and the wrapper's "don't clobber a recorded
#: pass" logic is only meaningful against them.
INITIAL_VALUES = {
    "1.2": {
        "cmi.core.lesson_status": "not attempted",
        "cmi.core.student_id": "learner-1",
        "cmi.core.student_name": "Test, Learner",
        "cmi.core.credit": "credit",
        "cmi.core.entry": "ab-initio",
        "cmi.core.lesson_mode": "normal",
    },
    "2004": {
        "cmi.completion_status": "unknown",
        "cmi.success_status": "unknown",
        "cmi.learner_id": "learner-1",
        "cmi.learner_name": "Test, Learner",
        "cmi.credit": "credit",
        "cmi.entry": "ab-initio",
        "cmi.mode": "normal",
    },
}

#: The function names each API surface exposes.
API_METHODS = {
    "1.2": ("LMSInitialize", "LMSFinish", "LMSGetValue", "LMSSetValue",
            "LMSCommit", "LMSGetLastError", "LMSGetErrorString",
            "LMSGetDiagnostic"),
    "2004": ("Initialize", "Terminate", "GetValue", "SetValue", "Commit",
             "GetLastError", "GetErrorString", "GetDiagnostic"),
}

API_OBJECT_NAME = {"1.2": "API", "2004": "API_1484_11"}

_LMS_SCRIPT = """
/* A recording SCORM run-time service.  Not a conformant LMS - it is a
 * microphone.  It answers the calls a SCO makes and writes down every one. */
(function () {
    var VERSION = %(version_json)s;
    var OBJECT_NAME = %(object_name_json)s;
    var METHODS = %(methods_json)s;

    window.__scormCalls = [];
    window.__scormModel = %(initial_json)s;
    window.__scormState = { initialized: false, terminated: false,
                            commits: 0, lastError: "0" };

    function record(name, args, ret) {
        window.__scormCalls.push({
            fn: name,
            args: Array.prototype.slice.call(args),
            argTypes: Array.prototype.slice.call(args).map(function (a) {
                return typeof a;
            }),
            ret: ret
        });
        return ret;
    }

    var handlers = {};

    handlers.initialize = function (args) {
        if (window.__scormState.initialized) {
            window.__scormState.lastError = "101";
            return "false";
        }
        window.__scormState.initialized = true;
        window.__scormState.lastError = "0";
        return "true";
    };

    handlers.terminate = function (args) {
        if (!window.__scormState.initialized || window.__scormState.terminated) {
            window.__scormState.lastError = "112";
            return "false";
        }
        window.__scormState.terminated = true;
        window.__scormState.lastError = "0";
        return "true";
    };

    handlers.getValue = function (args) {
        if (!window.__scormState.initialized || window.__scormState.terminated) {
            window.__scormState.lastError = "122";
            return "";
        }
        window.__scormState.lastError = "0";
        var key = args[0];
        return Object.prototype.hasOwnProperty.call(window.__scormModel, key)
            ? window.__scormModel[key] : "";
    };

    handlers.setValue = function (args) {
        if (!window.__scormState.initialized || window.__scormState.terminated) {
            window.__scormState.lastError = "132";
            return "false";
        }
        window.__scormState.lastError = "0";
        window.__scormModel[args[0]] = args[1];
        return "true";
    };

    handlers.commit = function (args) {
        if (!window.__scormState.initialized || window.__scormState.terminated) {
            window.__scormState.lastError = "142";
            return "false";
        }
        window.__scormState.commits += 1;
        window.__scormState.lastError = "0";
        return "true";
    };

    handlers.lastError = function () { return window.__scormState.lastError; };
    handlers.errorString = function () { return ""; };
    handlers.diagnostic = function () { return ""; };

    var byIndex = [handlers.initialize, handlers.terminate, handlers.getValue,
                   handlers.setValue, handlers.commit, handlers.lastError,
                   handlers.errorString, handlers.diagnostic];

    var api = {};
    METHODS.forEach(function (name, index) {
        var handler = byIndex[index];
        api[name] = function () {
            return record(name, arguments, handler(arguments));
        };
    });
    window[OBJECT_NAME] = api;
})();
"""

_HOST_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Fake LMS (%(version)s)</title>
<script>
%(script)s
</script>
</head>
<body>
<h1>Fake LMS</h1>
<iframe id="shell" name="shell" src="shell.html" width="1000" height="800"></iframe>
</body>
</html>
"""

#: An intermediate frame that publishes NO API, so the SCO has to climb two
#: levels.  A wrapper that only checks its immediate parent fails here.
_SHELL_PAGE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Course shell</title></head>
<body style="margin:0">
<iframe id="sco" name="sco" src="%(sco_src)s" width="1000" height="760"></iframe>
</body>
</html>
"""

#: A host page with no API anywhere: the "learner opened the file outside an
#: LMS" case.  The SCO must still score and render.
_NO_LMS_PAGE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>No LMS</title></head>
<body style="margin:0">
<iframe id="sco" name="sco" src="%(sco_src)s" width="1000" height="760"></iframe>
</body>
</html>
"""


def write_host(root, sco_src, version=None):
    """Write the host pages into *root* and return the entry page's name.

    Args:
        root: directory served by :func:`serve`.
        sco_src: the SCO's path relative to *root*, e.g.
            ``"package/assessment.html"``.
        version: ``"1.2"``, ``"2004"``, or ``None`` for the no-LMS case.
    """
    root = Path(root)
    if version is None:
        (root / "lms.html").write_text(
            _NO_LMS_PAGE % {"sco_src": sco_src}, encoding="utf-8")
        return "lms.html"

    script = _LMS_SCRIPT % {
        "version_json": json.dumps(version),
        "object_name_json": json.dumps(API_OBJECT_NAME[version]),
        "methods_json": json.dumps(list(API_METHODS[version])),
        "initial_json": json.dumps(INITIAL_VALUES[version]),
    }
    (root / "lms.html").write_text(
        _HOST_PAGE % {"version": version, "script": script}, encoding="utf-8")
    (root / "shell.html").write_text(
        _SHELL_PAGE % {"sco_src": sco_src}, encoding="utf-8")
    return "lms.html"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - silence the test output
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def serve(root):
    """Serve *root* over http://127.0.0.1:<port> for the life of the block."""
    handler = functools.partial(_QuietHandler, directory=str(root))
    with _Server(("127.0.0.1", 0), handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:{0}".format(httpd.server_address[1])
        finally:
            httpd.shutdown()
            thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Reading the recording back
# ---------------------------------------------------------------------------
class Recording:
    """The calls a SCO made, in order, with a few convenience views."""

    def __init__(self, calls, model, state):
        self.calls = calls
        self.model = model
        self.state = state

    def __repr__(self):
        return "Recording({0} calls)".format(len(self.calls))

    def names(self):
        return [call["fn"] for call in self.calls]

    def of(self, *names):
        return [call for call in self.calls if call["fn"] in names]

    def sets(self):
        return self.of("LMSSetValue", "SetValue")

    def set_calls_for(self, element):
        return [call for call in self.sets() if call["args"][0] == element]

    def last_set(self, element, default=None):
        matching = self.set_calls_for(element)
        return matching[-1]["args"][1] if matching else default

    # -- cmi.interactions ---------------------------------------------------
    #
    # Indexed collections arrive as flat element names
    # ("cmi.interactions.3.result"), because that is literally what a SCO puts
    # on the wire.  The service stores them verbatim - it does NOT validate
    # them, so a passing test here means "the package wrote this", never "a
    # real LMS would accept this".  The format rules are asserted separately.

    def written_keys(self):
        """Every data-model element the SCO wrote, as a set."""
        return {call["args"][0] for call in self.sets()}

    def interaction_writes(self):
        """The cmi.interactions.N.* SetValue calls, in call order."""
        return [call for call in self.sets()
                if call["args"][0].startswith("cmi.interactions.")]

    def interactions(self):
        """``{n: {field: value}}`` for every interaction the SCO wrote.

        ``field`` keeps everything after ``cmi.interactions.<n>.``, so a
        sub-collection element such as ``objectives.0.id`` stays visible rather
        than being flattened away.  The last write wins, as it would in an LMS.
        """
        collected = {}
        for call in self.interaction_writes():
            element, value = call["args"][0], call["args"][1]
            parts = element.split(".", 3)   # cmi | interactions | n | field
            if len(parts) != 4 or not parts[2].isdigit() or not parts[3]:
                continue
            collected.setdefault(int(parts[2]), {})[parts[3]] = value
        return collected

    def interaction_indices(self):
        return sorted(self.interactions())

    def index_of(self, *names):
        """Index of the first call to any of *names*, or ``None``."""
        for index, call in enumerate(self.calls):
            if call["fn"] in names:
                return index
        return None

    def last_index_of(self, *names):
        found = None
        for index, call in enumerate(self.calls):
            if call["fn"] in names:
                found = index
        return found

    def pretty(self):
        return "\n".join(
            "{0:>3}  {1}({2}) -> {3!r}".format(
                index, call["fn"],
                ", ".join(repr(a) for a in call["args"]), call["ret"])
            for index, call in enumerate(self.calls))


def read_recording(page):
    """Pull the recording out of the Playwright page hosting the fake LMS."""
    return Recording(
        page.evaluate("window.__scormCalls"),
        page.evaluate("window.__scormModel"),
        page.evaluate("window.__scormState"),
    )
