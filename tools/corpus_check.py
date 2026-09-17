"""Sweep a directory of documents through the full pipeline and report what an
auditor would ask about: did every document parse and export, did any generated
assessment become passable by a fixed strategy, how many came back thinner than
requested, and does any source text look like an instruction aimed at an AI.

    python3 -m tools.corpus_check <dir> [--questions 5 8] [--json out.json]

Documents are treated strictly as data: nothing from them is printed except
titles and short excerpts of the worst cases. Use it on your own controlled
SOPs before a pilot; the numbers it prints are the same ones CLAUDE.md's
invariants are measured by.
"""
import argparse, collections, json, sys, tempfile, traceback
from pathlib import Path

from src.assessments import AssessmentGenerator, MedicalDeviceAssessmentGenerator
from src.generator import TrainingGenerator
from src.parser import SOPParser
from src.scorm_exporter import SCORMExporter

STRATEGIES = {
    "idx0": lambda q: 0,
    "idx1": lambda q: min(1, len(q.options) - 1),
    "idx2": lambda q: min(2, len(q.options) - 1),
    "idx3": lambda q: min(3, len(q.options) - 1),
    "last": lambda q: len(q.options) - 1,
    "longest": lambda q: max(range(len(q.options)), key=lambda i: len(str(q.options[i]))),
    "shortest": lambda q: min(range(len(q.options)), key=lambda i: len(str(q.options[i]))),
    "always_true": lambda q: 0 if q.type == "true_false" else None,
}
SUPPORTED = {".txt", ".md", ".pdf", ".docx"}


def _score(assessment, pick):
    total = sum(q.points for q in assessment.questions) or 1
    got = sum(q.points for q in assessment.questions if pick(q) == q.correct_answer)
    return 100.0 * got / total


def check_document(path, question_counts, export_dir):
    """Return a dict of measurements for one document; never raises."""
    rec = {"file": path.name, "ok": False}
    try:
        sop = SOPParser().parse(str(path))
        scan = getattr(sop, "injection_scan", None)
        rec.update(title=sop.title[:80], steps=len(sop.procedures),
                   warnings=len(sop.safety_warnings), definitions=len(sop.definitions),
                   has_purpose=bool(sop.purpose), has_scope=bool(sop.scope),
                   injection_risk=(scan or {}).get("risk"))
        module = TrainingGenerator().generate(sop)
        worst = (0.0, None, None, None)
        thin = 0
        for gen_cls in (AssessmentGenerator, MedicalDeviceAssessmentGenerator):
            for n in question_counts:
                a = gen_cls().generate(sop, num_questions=n)
                if len(a.questions) < n:
                    thin += 1
                for name, pick in STRATEGIES.items():
                    s = _score(a, pick)
                    if s > worst[0]:
                        worst = (s, name, gen_cls.__name__, a.passing_score)
        rec.update(worst_naive_pct=round(worst[0], 1), worst_strategy=worst[1],
                   worst_generator=worst[2], passing_score=worst[3],
                   gameable=worst[0] >= (worst[3] or 100), thin_assessments=thin)
        SCORMExporter().create_package(
            module, MedicalDeviceAssessmentGenerator().generate(sop, max(question_counts)),
            export_dir, "pkg_" + path.stem[:40])
        rec["ok"] = True
    except Exception as exc:  # noqa: BLE001 - a sweep must finish
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("directory")
    ap.add_argument("--questions", nargs="+", type=int, default=[5, 8])
    ap.add_argument("--json", help="write per-document records here")
    args = ap.parse_args(argv)

    paths = sorted(p for p in Path(args.directory).iterdir() if p.suffix.lower() in SUPPORTED)
    if not paths:
        print("no supported documents found", file=sys.stderr)
        return 2
    export_dir = tempfile.mkdtemp(prefix="corpus_check_")
    records = [check_document(p, args.questions, export_dir) for p in paths]

    ok = [r for r in records if r["ok"]]
    print(f"documents: {len(records)} | parsed+exported: {len(ok)} | errors: {len(records) - len(ok)}")
    if ok:
        steps = collections.Counter(min(r["steps"], 10) for r in ok)
        print("steps per document (10 = 10+):", dict(sorted(steps.items())))
        print(f"with purpose: {sum(r['has_purpose'] for r in ok)} | with scope: {sum(r['has_scope'] for r in ok)} | "
              f"total warnings: {sum(r['warnings'] for r in ok)} | total definitions: {sum(r['definitions'] for r in ok)}")
        gameable = [r for r in ok if r["gameable"]]
        print(f"GAMEABLE assessments (naive strategy >= pass mark): {len(gameable)}")
        print(f"thin assessments (fewer questions than requested): {sum(r['thin_assessments'] for r in ok)} "
              f"of {len(ok) * 2 * len(args.questions)}")
        risks = collections.Counter(r["injection_risk"] for r in ok)
        print("injection scan:", dict(risks))
        print("worst naive cases:")
        for r in sorted(ok, key=lambda r: -r["worst_naive_pct"])[:5]:
            print(f"  {r['worst_naive_pct']:5.1f}% {r['worst_strategy']:11s} pass={r['passing_score']} | {r['title'][:60]}")
    for r in records:
        if not r["ok"]:
            print("ERROR", r["file"], "|", r["error"])
    if args.json:
        Path(args.json).write_text(json.dumps(records, indent=2))
    return 1 if any(r.get("gameable") for r in ok) or len(ok) != len(records) else 0


if __name__ == "__main__":
    sys.exit(main())
