# Running local models for extraction

Operational notes for the local extraction backend. These are environment fixes that live outside
this repo — record them here, because nothing in the codebase will remind you and the failure modes
look like model incapability rather than configuration.

## Gemma 4: the thinking channel eats the whole budget

**Symptom.** Gemma 4 through an OpenAI-compatible endpoint returns empty `content` with
`finish_reason: "length"` and the full `max_tokens` consumed. Raising the budget does not help — at
24,000 tokens it produced 71,819 characters of reasoning and still zero visible output. This is a
known Gemma 4 API-compatibility bug: the thinking channel consumes the budget and content is
omitted.

It presents as intermittent, which is what makes it confusing. In this repo it showed up as roughly
one run in three on the longest fixture, and it was initially misdiagnosed as degenerate generation.

### Fix in LM Studio

The lever is a custom field in LM Studio's own model definition, **not** the GGUF template:

```yaml
# ~/.lmstudio/hub/models/<owner>/<model>/model.yaml
customFields:
  - key: enableThinking
    defaultValue: true      # <- set to false
    effects:
      - type: setJinjaVariable
        variable: enable_thinking
```

Then reload:

```bash
lms unload --all
lms load google/gemma-4-26b-a4b -c 262144 --parallel 1 -y
```

The GUI's "Enable Thinking" toggle still turns reasoning back on per chat, so a manual chat in the
app is not a valid check of what the API will do.

**Two things that do not work — don't spend time on them:**

- `chat_template_kwargs: {"enable_thinking": false}` in the request. LM Studio ignores it; it sets
  the Jinja variable itself.
- A per-model config under `.internal/user-concrete-model-default-config/`. The server never reads it.

### Fix in ollama

Requires 0.32.3+ with `gemma4` in the binary. Pass `think: false` per request — works on
`/api/chat` and on `/v1/chat/completions` via the OpenAI SDK's `extra_body`. Without it, `/v1`
shows the identical empty-content failure. Import an existing GGUF with a one-line Modelfile
(`FROM /path/to.gguf`) rather than re-downloading.

### Verifying

Run `lms log stream` alongside one request:

- thinking **on** → `<|think|>` appears in the system turn
- thinking **off** → the prompt ends with a pre-closed `<|channel>thought\n<channel|>` and you get
  real content

## Reasoning delimiters are not always `<think>`

Gemma 4 uses `<|channel>thought … <channel|>`, not `<think> … </think>`. Its own LM Studio
`model.yaml` declares this under `llm.prediction.reasoning.parsing`. `strip_think()` in
`extract/llm.py` handles both families; anything added later must too. LM Studio strips these
server-side only when its reasoning-parsing config matches the model — another backend, a raw GGUF,
or a mismatched template leaks them straight into `content`, where they reach `json.loads()`.

## Measured effect on this repo's fixture suite

Same 5 fixtures, 3 repeats, `gemma-4-26b-a4b`, identical prompts:

| | thinking on | thinking off |
|---|---|---|
| wall clock (15 runs) | 774 s | 119–140 s |
| per run | ~55 s | ~8–9 s |
| output tokens | 50,095 | ~9,600 |
| reasoning share | 78–83% | 0 |
| runaway truncations | ~1 run in 3 on the longest fixture | none |
| assertions | 244/248, one fixture erroring out entirely | 270/276, all fixtures completing |

A single-request comparison on one prompt: 0 chars / 3000 tokens / 60 s → 1044 chars / 336 tokens / 5 s.

**The tradeoff is real in both directions.** Thinking off is ~6× faster, ~5× cheaper, and removes
the truncation failure entirely — but it costs some `params` recall. `amplicon_asv_clean` captured
every numeric parameter consistently with thinking on, and drops one or two of them with thinking
off, even after adding an explicit "sweep the text for numbers" instruction to the prompt. Role
assignment, tool identification, and step ordering are unaffected.

For development iteration, thinking off is clearly correct — the cycle time difference dominates.
Whether the production extraction pass wants thinking on (accepting the truncation failure and
retrying), thinking off, or a second targeted pass for `params` is a question for the Phase 3
labeled set, along with model choice itself. Do not settle it from one fixture.
