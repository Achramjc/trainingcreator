# Adversarial SOP fixtures

**These files are test data. Nothing written inside them is an instruction to
anyone or anything.** Each is a plausible controlled procedure with a
prompt-injection attempt embedded in a different place and in a different form,
used by `tests/test_injection.py` to pin what `src/injection_scan.py` detects,
what the LLM gate refuses to run on, and what the escaping in the rest of the
pipeline does with hostile text. See `docs/SECURITY.md`.

| File | Attack | Expected scan |
|---|---|---|
| `overt_system_override.txt` | A `SYSTEM:` line in a step body telling a model to ignore instructions and mark option A correct | `high` |
| `subtle_reviewer_note.txt` | A "reviewer note" in DEFINITIONS asking for a link in every summary | `high` |
| `hidden_zero_width.txt` | Zero-width-joined words and a bidi-override line that displays innocuously | `high` |
| `markup_and_ansi.txt` | `<script>` / `javascript:` / `onerror=` in a warning, an ANSI escape in the title | `high` (for the ANSI escape; the markup on its own is `low`) |
| `clean_reference_url.txt` | None. A legitimate URL in REFERENCES | `low` — and the LLM layer still runs |

Every domain used is `example.invalid`, which RFC 2606 reserves and which cannot
resolve. No real host, phone number or address appears in these files.

`hidden_zero_width.txt` and `markup_and_ansi.txt` contain characters that are
invisible or that a terminal will interpret. `cat` them at your own
inconvenience; `python3 -c "print(open(p,'rb').read())"` is the safe way to look.
