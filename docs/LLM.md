# The grounded LLM enhancement layer

Optional. **Off by default.** Everything in this document describes a layer that
sits *between* generation and export and can be removed without changing a byte
of the deterministic pipeline.

## What it is, honestly

The deterministic pipeline produces training content by slicing the source SOP.
It is accurate and traceable, and it reads like it: learning objectives say
`Perform Step 3: Notify Supervision`, and wrong answers on the quiz are other
sentences from elsewhere in the document. A subject-matter expert will accept
the *facts* and rewrite the *prose*.

This layer asks Claude to do that rewriting — objectives, section summaries,
distractors — and then **mechanically checks every sentence it gets back against
a cited line of the source document**. Anything that fails the check is thrown
away and the deterministic text is kept. Every proposal, accepted or rejected,
is recorded with its reason in an enhancement report meant to be read by the
SME and, if it comes to it, an auditor.

What this is: **acceleration with a human in the loop.**
What this is not: automation of a regulated judgment.

> The SME review and approval step remains mandatory. Surviving the grounding
> check is not approval; it only means the sentence is traceable to a line of
> the SOP. Nothing generated here may reach a learner without a named human
> approving it. (GOAL.md, acceptance criterion 6 and Risk 2.)

## Enabling it

```bash
pip install -r requirements.txt -r requirements-llm.txt

export TRAINING_CREATOR_LLM=anthropic          # default: off
export ANTHROPIC_API_KEY=sk-ant-...            # or run: ant auth login
export TRAINING_CREATOR_LLM_MODEL=claude-opus-5   # default

python3 -m src.cli -i examples/sample_sop.txt -o /tmp/out -f scorm1.2 --llm
```

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `TRAINING_CREATOR_LLM` | `off`, `anthropic` | `off` | Which backend to use. Anything else is treated as `off` and noted in the report. |
| `TRAINING_CREATOR_LLM_MODEL` | model id | `claude-opus-5` | Model for all three calls. |
| `ANTHROPIC_API_KEY` | key | unset | Only its *presence* is read, and only so the report can say a credential was missing. The value is never logged or serialised. An `ant auth login` profile works instead. |
| `TRAINING_CREATOR_LLM_LIVE_TESTS` | `1` | unset | Test-suite only: opts in to the single live smoke test. Requires the key too. |

The CLI flag `--llm` / `--no-llm` overrides the environment for one run.
`--llm` with no backend configured promotes it to `anthropic`, so the flag
never silently does nothing. The flag also writes
`enhancement_report.json` next to the output package.

The `anthropic` SDK is imported lazily, inside `AnthropicProvider`. With the
SDK absent the layer reports "SDK not installed" and the deterministic content
ships unchanged — the package, the CLI, the web app and the whole test suite
run either way. No test in this repository makes a network call except the live
smoke test, which is skipped unless both environment variables above are set.

## What it does

Three model calls per document.

**1. Learning objectives.** Each deterministic objective is offered for rewrite
as a Bloom's-aligned, observable objective with a cited `[start, end]` line
span. Accepted rewrites replace the original; rejected ones leave the original
in place. Nothing is added or renumbered.

**2. Section summaries.** A 2–4 sentence plain-language summary per training
section, each sentence cited separately. Accepted summaries are injected at the
top of the section as `<p class="summary">…</p>`, HTML-escaped with
`html.escape` — model output is treated as exactly as untrusted as document
text (CLAUDE.md invariant). **One unsupported sentence rejects the whole
summary**: a paragraph shown to a learner in a regulated course is either fully
supported or it is not shown.

**3. Distractors.** Replacements for weak wrong answers, on multiple-choice
questions only. The correct option is never changed; true/false questions are
not touched. After any replacement the assessment's integrity machinery is
re-run — see below.

## What the grounding check rejects

`src/llm/grounding.py`. Six cheap, explainable tests a reviewer can re-run by
eye — deliberately not a second model grading the first, because a model
judging a model is the black box QA/RA will not accept. Read the next section
too: knowing what these tests *cannot* see is part of using them honestly.

For a generated **claim** (`verify_claim`):

1. **The citation must resolve.** The `[start, end]` span must be real
   1-indexed line numbers inside the document the model was shown. An
   out-of-range or malformed span rejects the item outright.
2. **No invented numbers.** Every digit-bearing token in the sentence — a
   count, a duration, a limit, a form number, a radio channel — must appear in
   the cited excerpt (the cited lines ±1 line of context). A summary that says
   "within 15 minutes" where the SOP says 30 is rejected. This is the
   highest-value test in the file: a hallucinated figure in a controlled
   procedure is the failure mode that ends the company in this market.
3. **The wording must come from the source.** After NFKC normalisation,
   case-folding, punctuation stripping and stopword removal, at least
   **70%** (`MIN_CONTENT_WORD_OVERLAP`) of the sentence's content words must
   appear in the cited excerpt — *or* every capitalised and defined term in
   the sentence must. A plausible-sounding addition ("operators must wear
   insulated gloves") scores near zero and is rejected.
4. **At most 3 unsupported content words** (`MAX_UNSUPPORTED_CONTENT_WORDS`),
   whatever the ratio says. A ratio scales with sentence length — a long
   sentence can carry several invented words and still look well-grounded — so
   this bounds the absolute amount of unsupported material. The defined-terms
   alternative in rule 3 does **not** waive it: packing a sentence with the
   document's own named terms must not buy the right to four invented words.
5. **No added steps.** A clause introduced by *and, then, also, before, after,
   additionally, furthermore, plus, next, afterwards, subsequently* whose
   content words are **all** absent from the excerpt is treated as an invented
   action, condition or actor, and rejected with those words named in the
   reason. A clause with even one supported word is an elaboration, not an
   addition, and passes.
6. **Shape.** Non-empty, and at most 300 characters (objectives: 200).

Rules 4 and 5 exist because of a real adversarial finding, not a hypothetical.
At the original 50% bar,

> *"Press the red E-STOP button and then call the fire department."*

cited against the step it half-quotes was **accepted**: the faithfully quoted
first half paid for the invented second half, and the output was an instruction
to call the fire department in a procedure that says no such thing — carrying a
citation that looked legitimate. Lexical grounding cannot tell that from a
paraphrase, so the check **fails closed**. A false rejection only keeps the
deterministic original; a false acceptance puts an invented instruction into
regulated training.

The cost of failing closed is that fluent paraphrases are rejected too:

> *"Hit the emergency stop control closest to you; all moving equipment loses
> power within five seconds."*

is rejected (58% overlap, 5 unsupported words) even though it is *true*. That
is the intended behaviour, not a bug to tune away — the prompts ask for the
document's own wording, and the deterministic text is a perfectly good
fallback. Note also that "five seconds" would sail past the numbers rule, since
the document writes "5 seconds": the wording rules are not optional decoration
on top of rule 2.

For a proposed **distractor** (`verify_distractor`):

- It must be distinguishable from the correct answer, on the same terms
  `src/assessments.pick_distractors` already uses: not empty, not a duplicate,
  not containing or contained by the correct answer, and below 88% similarity
  to it. A learner must not be able to argue two options were both right.
- **It must be false with respect to this document.** If the SOP asserts it
  anywhere — verbatim, near-verbatim (≥88% similar to some document sentence),
  or in the same words with the same polarity — it is rejected. A "distractor"
  the document actually states is not a hard question, it is a broken one.
  The polarity clause is what lets a genuine contradiction through: *"Never
  restart until an inspection is documented"* (asserted → rejected) and
  *"Always restart immediately without an inspection"* (contradicted →
  accepted) share most of their words and differ only in polarity.

## What the check cannot catch

Read this section before deciding how much to trust the layer. The grounding
check is **lexical**. It compares words, and only words, against the lines the
model cited. It has no model of meaning, and the following get past it:

- **An invented clause assembled from words that do appear in the cited lines.**
  This is the important one. The added-step rule fires only when *every*
  content word of the added clause is missing from the excerpt. A sentence like
  *"Press the red emergency stop button and then notify the Line Supervisor"*,
  cited against Step 2, passes — "notify", "line" and "supervisor" all appear
  elsewhere within the cited window — even though Step 2 says nothing about
  notifying anyone. Recombining the document's own vocabulary into an
  instruction the document does not give is a failure mode this check cannot
  see by construction.
- **A relationship reversed between supported terms.** "The Safety Officer
  completes Form MS-101" uses only words the document contains; that the
  document assigns the form to the Line Supervisor is a semantic fact, not a
  lexical one. The polarity test covers negation and modality ("never" vs
  "always", "must" vs "may"); it does not cover swapped actors or objects.
- **A number that is correct in the excerpt but wrong in context** — the rule
  checks that each figure *appears* in the cited lines, not that it is attached
  to the right thing.
- **A claim that is true of the wrong step.** The citation is checked for
  resolution and support; nothing checks that the model cited the step it was
  actually asked to summarise.
- **Omission.** A summary that leaves out the one warning that matters is
  perfectly grounded and perfectly dangerous.

This is why the framing in this document is not modesty. **The check is a
filter against the worst and most common failures, not a proof of correctness.
It is why SME approval remains mandatory, and why the layer is off by default.**
A reviewer reading `enhancement_report.json` is looking at machine-assisted
drafts with their citations attached, so that checking them is fast — they are
not looking at verified content.

If you want a stronger guarantee than this, the honest answer is to keep the
layer off: the deterministic pipeline's output is already fully traceable, and
it is what ships when every proposal is rejected.

## M0's invariants are re-proved, not assumed

Swapping distractors changes the options' length profile, and "always click the
longest option" is a strategy M0 defeated by *shaping* those lengths. So after
any accepted distractor change, `enhance_assessment`:

1. puts each question's correct option back at index 0 (canonical form),
2. re-runs `src.assessments._assign_answer_positions` — the private helper is
   imported deliberately rather than reimplemented, because the naive-learner
   invariant is *defined by* that function's joint offset search, and a second
   copy of it here would be the first thing to drift,
3. re-scores every fixed strategy a non-reader can execute: the
   position-dependent ones from `naive_strategies` / `strategy_pick`, plus
   "always the longest", "always the shortest" and "always True", which
   `assessments` deliberately leaves out of its layout search because they can
   only be fixed by shaping option text — exactly what a distractor swap
   changes.

If any strategy would now reach the assessment's own passing score, **all**
distractor changes are reverted, the deterministic assessment is returned
unchanged, and the report says which strategy and what it would have scored.
`tests/test_llm.py` asserts both halves: that a well-shaped replacement keeps
the naive learner below the pass mark on both fixtures and both generators, and
that a set of short replacements that re-opens the length tell is rolled back.

The answer key is unaffected: `to_learner_dict()` still carries only the salt
and the answer hash.

## Never mutates its inputs

`enhance_module` and `enhance_assessment` deep-copy their inputs and return new
objects. With the layer off, a `NullProvider`, no provider, a provider that
raises, an API error, a refusal, or a malformed payload, they return the input
objects themselves — `to_dict()` compares equal — plus a report saying why. The
deterministic build stays byte-identical for the same SOP title and version.

A provider failure is never an exception. `AnthropicProvider` catches
`AuthenticationError`, `RateLimitError`, `APIStatusError` (distinguishing
`status_code >= 500`) and `APIConnectionError`, most specific first, and turns
each into a recorded reason. `stop_reason == "refusal"` is treated as "no
enhancement" and recorded with its category.

## Cost notes

Three calls per document, all sharing a cached prefix:

- `system` block 1 is the byte-stable instruction prompt;
- `system` block 2 is the SOP with a `L0042: ` line-number gutter;
- both carry `cache_control: {"type": "ephemeral"}`.

Call one writes that prefix to cache; calls two and three read it at roughly a
tenth of the input price. For a 30-page SOP the document dominates the input
tokens, so the cache is most of the bill. `response.usage.input_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens` and `output_tokens` are
logged per call into the report, and the CLI prints the totals — if
`cache_read_input_tokens` is zero across calls two and three, something has been
interpolated into the prefix and the caching is gone. **Nothing per-run may
enter either system block**: no timestamps, no document title in the
instructions, no question counts. `tests/test_llm.py` asserts the prefix is
byte-identical across runs.

Other cost levers: the layer is per-document, not per-section, so cost scales
with document count rather than module size; `max_tokens` is 16000
(non-streaming) because a truncated JSON response is a wasted call, not a
cheaper one; and the whole layer is optional, so a customer who does not want
to pay for it gets the deterministic output with no degradation in
traceability.

## Reading the report

`enhancement_report.json` (CLI) or `EnhancementReport.to_dict()`:

| Key | Meaning |
|---|---|
| `disclosure` | The standing statement that this is a machine-assisted draft requiring SME approval. |
| `accepted_count` / `rejected_count` / `counts_by_task` | Totals, and per task (`objectives`, `section_summaries`, `distractors`). |
| `items[]` | Every proposal: task, target, accepted, **reason**, original text, proposed text, cited span, and the check's diagnostics (overlap achieved, excerpt used). |
| `calls[]` | Per model call: ok, refused, error, token usage. |
| `token_usage` | Summed input / cache-read / output tokens. |
| `notes[]` | Configuration notes and the post-enhancement naive-learner re-check result. |
| `config` | Backend, model, and the thresholds actually applied (`min_content_word_overlap`, `max_unsupported_content_words`, `max_sentence_chars`). **Never the API key.** |

The rejection reasons are the point. "These numbers do not appear in the cited
lines: 15" is a sentence an SME can act on, and it is the evidence that the
check is doing work rather than rubber-stamping.

## Wiring it in

```python
from src.llm import LLMConfig, build_provider, enhance_module, enhance_assessment

config = LLMConfig.from_env()            # or .with_enabled(True) for a UI toggle
provider = build_provider(config)        # NullProvider when off

module, module_report = enhance_module(module, sop_content, provider, config)
assessment, assessment_report = enhance_assessment(
    assessment, sop_content, provider, config)
```

Reuse one `provider` across both calls so all three requests share the prompt
cache. `merge_reports("package", [module_report, assessment_report], config)`
folds them into the single report the CLI writes.

## Files

| File | Contents |
|---|---|
| `src/llm/config.py` | `LLMConfig.from_env()`, environment contract, thresholds. |
| `src/llm/provider.py` | `Provider` protocol, `ProviderResult`, `AnthropicProvider` (lazy SDK import), `FakeProvider`, `NullProvider`, `build_provider`. |
| `src/llm/prompts.py` | Byte-stable system prompt, the three task prompts, line-numbered document rendering, JSON schemas. |
| `src/llm/grounding.py` | `verify_claim`, `verify_distractor`, `document_asserts`, span resolution. |
| `src/llm/enhance.py` | `enhance_module`, `enhance_assessment`, `EnhancementReport`, the naive-learner re-check. |
| `tests/test_llm.py` | All of the above, `FakeProvider` only, plus one opt-in live smoke test. |
| `requirements-llm.txt` | The optional `anthropic` pin. |
