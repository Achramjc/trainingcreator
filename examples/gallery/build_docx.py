"""
Regenerate the .docx versions of two gallery SOPs.

Run with:  python3 examples/gallery/build_docx.py

Two gallery documents are also shipped as .docx (alongside their .txt source)
so the DOCX parser path (src/parser.py:_parse_docx) is exercised by the
sample gallery, not just by tests/fixtures/*.docx. Each line of the .txt
source becomes one paragraph in the .docx file, which is exactly what
SOPParser._parse_docx reconstructs (one line per paragraph, joined with
"\n"), so the two files parse to the same structured content.

This script has no side effects beyond (re)writing the .docx files named
below; it is not imported by the app or the tests, only run by hand when the
source .txt files change.
"""

from pathlib import Path

from docx import Document

GALLERY_DIR = Path(__file__).resolve().parent

# (source .txt, output .docx) pairs. Keep this in sync with index.json's
# "format": "docx" entries.
DOCX_SOURCES = [
    ("md_device_history_record.txt", "md_device_history_record.docx"),
    ("pharma_cleaning_validation.txt", "pharma_cleaning_validation.docx"),
]


def build_docx(txt_name: str, docx_name: str) -> None:
    src_path = GALLERY_DIR / txt_name
    out_path = GALLERY_DIR / docx_name

    text = src_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    # A trailing newline in the source produces one trailing empty line from
    # split("\n"); drop it so the .docx doesn't end with an extra blank
    # paragraph the .txt doesn't have.
    if lines and lines[-1] == "":
        lines = lines[:-1]

    document = Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(str(out_path))
    print(f"wrote {out_path} ({len(lines)} paragraphs)")


def main() -> None:
    for txt_name, docx_name in DOCX_SOURCES:
        build_docx(txt_name, docx_name)


if __name__ == "__main__":
    main()
