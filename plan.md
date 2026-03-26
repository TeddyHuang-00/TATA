# Plan: Unified Grading Pipeline Framework

## TL;DR

Rewrite the scattered assignment-specific grading scripts across `ref/` into a centralized, configuration-driven framework in `TATA/`. Each assignment will define its structure via TOML configs (rubric, system prompt, processing rules), while the unified framework handles grading orchestration, LLM calls, and scoring. Pilot validation using Lab-Probability.

## Decisions

- **Scope**: All four pipeline stages (preprocessing, grading, rubric, post-processing)
- **Config Format**: TOML files for rubric definitions and settings per assignment
- **System Prompts**: Separate markdown files (e.g., `TATA/prompt/lab-probability.md`)
- **Backwards Compat**: Keep old scripts in `ref/` as-is; build new framework independently in `TATA/`
- **Pilot**: Lab-Probability (medium complexity, well-documented, good reference in `ref/`)

## Architecture Overview

```
TATA/
├── main.py                          ← Unified orchestrator (entry point)
├── provider.py                      ✅ DONE (config-based provider management)
├── rubric.py                        ✅ PARTIALLY DONE (Criterion framework ready)
├── grading.py                       ← NEW: LLM grading loop (reads markdown, calls API, saves JSON)
├── processing.py                    ← NEW: Unified preprocessing (auto-detect format, dispatch to converters)
├── scoring.py                       ← NEW: Generic post-processing (rubric JSON → scores + summaries)
├── config/
│   ├── provider.toml               ✅ DONE
│   └── processor.toml              ← NEW: Preprocessing rules (indent, extensions, sed patterns)
├── prompt/
│   ├── system.md                   ✅ EXISTS (base feedback guidelines)
│   ├── lab-probability.md          ← NEW: Lab-Probability-specific instructions
│   ├── lab-decision-tree.md        ← Future
│   └── ...
├── rubrics/
│   ├── example_rubric.toml         ✅ EXISTS (template)
│   └── lab-probability.toml        ← NEW: Lab-Probability rubric definition
├── assignments/
│   ├── lab-probability/
│   │   ├── config.toml             ← Assignment settings (paths, input_format, etc.)
│   │   ├── raw/                    ← Input files (student + reference notebooks)
│   │   ├── processed/              ← Converted markdown files
│   │   ├── graded/                 ← JSON + summary markdown outputs
│   │   └── logs/                   ← Grading logs and checkpoints
│   ├── lab-decision-tree/
│   │   └── ...
│   └── ...
└── pyproject.toml                  ✅ EXISTS
```

## Implementation Steps

### Phase 1: Framework Foundation (Parallel 1a + 1b)

**1a. Extend `rubric.py` with Pydantic model generation** _(1-2 hours)_

- Current state: Loads TOML rubrics into `RubricDefinition(Criterion[])`
- Need: Convert RubricDefinition to a dynamic Pydantic model for grading API response parsing
  - Create `generate_grading_model(rubric_def: RubricDefinition) -> type[BaseModel]`
  - Creates BaseModel with fields matching each criterion (e.g., `horror_movie_question: Ternary`, `feedback: str`)
  - This model becomes the response schema for instructor API
- _Verify_: Write test that:
  1. Loads example_rubric.toml
  2. Generates model
  3. Tests validation with mock JSON matching the rubric

**1b. Create `grading.py` with unified grading orchestrator** _(2-3 hours)_

- Read processed markdown files from folder
- For each file:
  1. Read file content + reference answer
  2. Load assignment config (rubric path, system prompt path)
  3. Load rubric → generate Pydantic model
  4. Load system prompt markdown
  5. Call instructor API with:
     - System: assignment-specific system prompt
     - User: "Reference: {reference_md}\n\nStudent Answer: {student_md}"
     - Response model: dynamically generated from rubric
  6. Write response to `graded/{filename}.json`
  7. Track checkpoints (resume failed gradings)
- Key function: `grade_assignment(assignment_config: Path) -> None`
- _Verify_: Run on Lab-Probability with mock LLM (return dummy ratings)

### Phase 2: Preprocessing Unification (depends on Phase 1 for integration)

**2. Create `processing.py`** _(2-3 hours)_

- Auto-detect input format (ipynb vs HTML vs markdown)
- Route to appropriate converter:
  - Jupyter notebooks: `jupyter nbconvert --to markdown --template basic`
  - HTML: `pandoc HTML to markdown`
  - Mark down: copy as-is
- Apply sanitization (from old ref/ patterns):
  - Remove base64 images
  - Clean filenames (spaces, special chars)
  - Run mdformat for consistency
- Create assignment structure under `TATA/assignments/{assignment_name}/`
- Store processing config in `processor.toml` (indent levels, filters, etc.)
- Key function: `preprocess_assignment(assignment_name: str, raw_input_path: Path) -> None`
- _Verify_: Process a sample Lab-Probability notebook manually, compare output to ref/Lab-Probability/processed/

### Phase 3: Post-Processing & Scoring (depends on Phase 1 for rubric)

**3. Create `scoring.py`** _(1.5-2 hours)_

- Read `graded/{filename}.json` (grading response)
- Map ratings to scores using rubric's `pts` and `grading` scheme:
  - Binary: correct=full pts, incorrect=0
  - Ternary (STANDARD): correct=full, partial=50%, incorrect=0
  - Ternary (STRICT): correct=full, partial=0, incorrect=0
  - Likert + custom scales: use Criterion's custom_scale if provided
- Aggregate scores across all criteria
- Generate human-readable markdown summary:

  ```
  ## Grading Summary: submission_abc123.ipynb

  ### Criterion 1: Horror Movie DataFrame Creation (8/8 pts)
  **Rating**: CORRECT
  **Feedback**: [...feedback from LLM...]

  ### Total Score: 88/100
  ```

- Key function: `score_submission(rubric_def: RubricDefinition, grading_response: dict) -> tuple[float, str]`
- _Verify_: Use mock gradings (from ref/Lab-Probability/graded/\*.json pattern), verify score calculations match ref/summary.py

### Phase 4: Integration & Configuration (depends on all previous phases)

**4. Create assignment config pattern** _(1 hour)_

- Template: `TATA/assignments/lab-probability/config.toml`

  ```toml
  [assignment]
  name = "Lab-Probability"
  input_format = "ipynb"  # or "html", "markdown"
  reference_file = "reference.ipynb"

  [processing]
  # Inherit from processor.toml, override if needed
  indent_level = 4

  [grading]
  rubric = "rubrics/lab-probability.toml"
  system_prompt = "prompt/lab-probability.md"
  provider = "deepseek_reasoner"  # from provider.toml

  metadata.title = "CS 4/5356 Lab: Probability"
  ```

- Create `rubrics/lab-probability.toml` (convert from ref/Lab-Probability/rubric.py)
- Create `prompt/lab-probability.md` (convert from ref/Lab-Probability/grading.py SYSTEM string)

**5. Implement unified `main.py` orchestrator** _(1.5 hours)_

- Entry point: `python -m TATA main.py --assignment lab-probability --stage [preprocess|grade|score|all]`
- Stages:
  1. `--stage preprocess`: Run Phase 2
  2. `--stage grade`: Run Phase 1b
  3. `--stage score`: Run Phase 3
  4. `--stage all`: Run all sequentially
- Load providers, rubrics, configs via central registry pattern
- Provide progress logging and error recovery

**6. Create migration helper scripts** _(1 hour)_

- `migrate_rubric.py`: Convert ref/Lab-Probability/rubric.py → TATA/rubrics/lab-probability.toml
- `migrate_prompt.py`: Extract SYSTEM string from ref/Lab-Probability/grading.py → TATA/prompt/lab-probability.md
- Use these as templates for future migrations

### Phase 5: Validation & Documentation (depends on Phase 4)

**7. Validate with Lab-Probability** _(3-4 hours)_

- Preprocess raw notebooks → check processed/\*.md matches ref/Lab-Probability/processed/
- Grade with actual LLM → spot-check graded/\*.json looks reasonable
- Score submissions → verify totals match manual calculation
- Compare outputs with ref/ to detect discrepancies
- _Verification Checklist_:
  - [ ] Preprocessing: 5 sample notebooks converted; filename/content format matches ref
  - [ ] Grading: LLM produces ratings matching expected rubric structure
  - [ ] Scoring: At least 2 submissions scored; totals between 0-100 as expected
  - [ ] No data loss: All 13 criteria present in JSON; no missing feedback fields
  - [ ] Checkpoint recovery: Kill process mid-grade, resume, verify no reprocessing

**8. Documentation** _(1 hour)_

- README: Framework architecture, config schema, usage guide
- Examples: Annotated Lab-Probability config showing all options
- Migration guide: Step-by-step for converting other assignments

## Relevant Files & Patterns

### To Reuse (Stable)

- **provider.py**: Sophisticated TOML config with env var interpolation → use as-is
- **rubric.py Criterion framework**: Flexible rating/grading/custom_scale logic → extend with model generation
- **ref/Lab-Probability/rubric.py**: 13 criteria definitions + PTS_MAP → convert to `lab-probability.toml`
- **ref/Lab-Probability/grading.py** SYSTEM string: Assignment-specific instructions → `prompt/lab-probability.md`
- **ref/Lab-Probability/summary.py**: Score aggregation logic (correct/partial/incorrect mapping) → implement in scoring.py

### New Files to Create

- `grading.py`: Orchestrator for LLM evaluation
- `processing.py`: Unified preprocessing dispatcher
- `scoring.py`: Post-processing & score aggregation
- `assignments/lab-probability/config.toml`: Assignment-specific settings
- `rubrics/lab-probability.toml`: Lab-Probability rubric definition
- `prompt/lab-probability.md`: Lab-Probability system prompt

### To Extend

- **rubric.py**: Add `generate_grading_model()` function
- **main.py**: Replace skeleton with full orchestrator
- **config/processor.toml**: Define preprocessing rules (NEW file)

## Verification Checklist (Per Phase)

1. **Phase 1a**: Rubric model generation produces valid Pydantic class; JSON deserialization works
2. **Phase 1b**: Mock grading produces saved JSON with all criteria populated; error handling logs issues
3. **Phase 2**: Processed markdown from sample notebook matches ref/Lab-Probability format
4. **Phase 3**: Scores calculated from mock JSON match manual expectations; markdown summaries readable
5. **Phase 4**: Main.py properly routes stages; configs load without errors; provider injection works
6. **Phase 5**: End-to-end Lab-Probability workflow (raw → processed → graded → scored) completes successfully

## Further Considerations

1. **Error Handling Strategy**: How verbose should logs be? Should failures in one submission stop the batch or continue?
   - Recommendation: Log individual submission errors; continue processing batch; summarize failures at end

2. **Extensibility for Other Input Formats**: Current plan assumes .ipynb is primary. Activity-2-1 uses HTML forms. Should preprocessing detect format automatically or require config?
   - Recommendation: Auto-detect via file extension; fall back to config hint if ambiguous

3. **Provider Switching**: Some assignments may need different models (e.g., strict grading vs. reasoning). Should config.toml allow per-criterion provider overrides?
   - Recommendation: Start with one provider per assignment; add per-criterion override in Phase 5+ only if needed
