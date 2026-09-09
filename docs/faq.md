# FAQ

## 1. What is the minimum assignment config?

At minimum, your config must include:

```toml
[grading]
rubric = "rubrics/example_rubric.toml"
system_prompt = "prompt/system.md"
provider = "ollama"
```

Use [data/example/config.toml](../data/example/config.toml) as the baseline.

## 2. What is uv and why do I need it?

[uv](https://docs.astral.sh/uv/) is a tool that installs Python and every
dependency TATA needs, so you never manage Python yourself. `uv sync` in the
project root sets everything up in one command.

Install uv one time only:

- Windows PowerShell: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

See README "Install" for the full walkthrough.

## 3. I lost my Canvas API token. What now?

Go to your Canvas site, open Settings (from your avatar menu), and under
**API Access Token** click to generate a new one. The token is shown only
once, so save the new one somewhere private. Then update
`CANVAS_ACCESS_TOKEN` in the project's `.env` file (in the TUI, Settings →
Canvas → Save .env does this for you).

## 4. Can I use a different LLM provider?

Yes. TATA is provider-agnostic: create a new provider in Library → Providers
(or edit `data/providers/<name>.toml` in your editor) and set `base_url`,
`model`, and `mode` for your provider. The bundled
[ollama.toml](../data/providers/ollama.toml) is the recommended first
provider — a local Qwen3.8-27B with no API key needed (`ollama serve` +
`ollama pull qwen3.8:latest`). The bundled
[deepseek_chat_tool.toml](../data/providers/deepseek_chat_tool.toml) is a
working DeepSeek example (needs `DEEPSEEK_API_KEY`). See
[config/provider.md](config/provider.md) for the full format.

## 5. Which paths are optional?

All fields under `[assignment]` are optional.

If omitted, defaults are:

- raw_dir -> `raw`
- processed_dir -> `processed`
- graded_dir -> `graded`
- logs_dir -> `logs`
- reference_file -> none (no reference file; set it to enable
  reference-based grading)

## 6. Do I need to create folders manually?

No. The pipeline now auto-creates assignment folders when running stages.

## 7. How do I validate an assignment config?

Run:

```bash
uv run cli validate -c data/my-assignment/config.toml
```

or with the short flag: `-c` is shorthand for `--config`.

`validate` loads the config with the same pydantic models used at runtime,
then checks the chain it would grade against:

- the rubric file exists and parses (criteria count is reported),
- every `system_prompt` file exists,
- the `[grading].provider` matches a provider in `data/providers/`,
- the reference file exists when `[assignment].reference_file` is set.

It prints one line per check and exits `1` when any check fails.

## 8. How can I speed up grading?

Set `grading.max_parallel_tasks` in config. Valid range is `1` to `10`.

```toml
[grading]
max_parallel_tasks = 10
```

## 9. What is the recommended stage order?

Use this order:

1. preprocess
1. plagiarism (optional but recommended)
1. grade
1. score
1. analyze (optional)

Or run all at once:

```bash
uv run cli preprocess -c data/my-assignment/config.toml
uv run cli plagiarism -c data/my-assignment/config.toml
uv run cli grade -c data/my-assignment/config.toml
uv run cli score -c data/my-assignment/config.toml
uv run cli analyze -c data/my-assignment/config.toml
```

## 10. Where are outputs written?

- Processed markdown: `processed/`
- Grading JSON: `graded/*.json`
- Score summaries: `scored/` (format-specific subfolders)
- Logs and checkpoint: `logs/`
- Plagiarism report and extracted files: `plagiarism/report.html`, `plagiarism/submissions/`, `plagiarism/template/`

## 11. Why do I get "All submissions already graded (cache hit)"?

The grading cache (`logs/grading.cache.json`, keyed by submission input
hashes) remembers which submissions were graded with unchanged inputs.

If you want to regrade from scratch, remove:

- `logs/grading.cache.json` (and `logs/grading.checkpoint.json` if present)
- old files in `graded/`

or re-run with `--force`. Then run grade again.

## 12. Can preprocessing accept multiple submission formats?

Yes. `processing.input_format` supports both a single value and a list.

Single format:

```toml
[processing]
input_format = "ipynb"
```

Multiple formats:

```toml
[processing]
input_format = ["ipynb", "html", "markdown"]
```

If omitted, preprocessing auto-detects from the first supported file in `raw/`.

## 13. Does reference answer have to be markdown?

No. Grade stage accepts reference files in:

- `.md`
- `.ipynb`
- `.html`

Set `[assignment].reference_file` to any of those formats. Non-markdown references are converted automatically during grading.

Recommended location is assignment root (for example `data/my-assignment/reference.ipynb`) so it is separate from student submissions.

## 14. How does plagiarism detection reduce boilerplate false positives?

Plagiarism stage uses `copydetect` with a template boilerplate source.

By default it expects `template.ipynb` in assignment root and extracts code into:

- `plagiarism/template/template.py`

Student code is extracted into:

- `plagiarism/submissions/*.py`

Then a report is generated at:

- `plagiarism/report.html`

And full pairwise comparison data is exported at:

- `plagiarism/all_pairs.json`

All paths can be customized via `[plagiarism]` config.

## 15. Does a high plagiarism score always mean a student cheated?

No. A high similarity score is a signal for manual review, not automatic proof of misconduct.

Common non-cheating causes include:

- Assignment prompts that are very constrained/straightforward
- Small solution space where many students produce near-identical code
- Shared starter structure or repetitive required steps

Recommended workflow:

1. Treat plagiarism results as triage candidates.
1. Compare highlighted regions for substantive logic overlap, not just scaffolding.
1. Check assignment context (difficulty, template rigidity, expected idioms) before conclusions.
1. Escalate only when evidence is consistent with policy.

## 16. Can I combine plagiarism results across all assignments into one report?

Yes. Use the aggregate helper script:

```bash
uv run cli plagiarism -c data/config.toml --aggregate \
	--output misc/plagiarism_summary.md
```

Tuning knobs live in the `[plagiarism]` config section instead of CLI
flags (allowed ranges are validated by
`src/shared/assignment_config.py`):

- `pairwise_alpha` / `individual_alpha` (default `0.01`, range 0–1) for
  stricter pair-level / student-level significance
- `score_floor` (default `0.001`, range 0–0.5) and `score_cap`
  (default `0.999`, range 0.5–1) to control logit clipping bounds

It reads `plagiarism/all_pairs.json` (full pair coverage).
It reports significant pairs and significant students as separate sections.
