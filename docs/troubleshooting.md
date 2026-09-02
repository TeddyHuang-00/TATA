# Troubleshooting

## 1. Invalid assignment config

Symptom:

- Error says config is invalid or required fields are missing.

Fix:

1. Start from [data/example/config.toml](../data/example/config.toml)
1. Ensure `[grading]` has all required keys:
   - `rubric`
   - `system_prompt`
   - `provider`
1. Validate paths for rubric and prompt files.

## 2. Invalid TOML syntax

Symptom:

- Error indicates TOML parsing failed.

Fix:

- Check for missing quotes, commas, or section headers.
- Run `cli validate -c <config>` for guidance on the failing field.

## 3. No supported files found in raw/

Symptom:

- Preprocess reports no supported files.

Fix:

1. Place files in `raw/`
1. Use supported extensions: `.ipynb`, `.html`, `.md`
1. If needed, set `[processing].input_format`

## 4. No files found for input_format

Symptom:

- Preprocess says no files found for the configured format.

Fix:

- Match file extensions with `processing.input_format`
- Or remove `input_format` to enable auto-detection

## 5. Reference file not found

Symptom:

- Grade stage fails with missing reference file.

Fix:

1. Create `reference.md` in assignment root (or use `reference.ipynb`/`reference.html`)
1. Or set `assignment.reference_file` explicitly
1. Or omit `reference_file` entirely for rubric-only grading (no reference answer needed)

## 6. Rubric file not found

Symptom:

- Grade or score stage fails with missing rubric.

Fix:

- Set `grading.rubric` to an existing TOML file in the repository
- Example: `rubrics/example_rubric.toml`

## 7. System prompt file not found

Symptom:

- Grade stage fails with missing prompt file.

Fix:

- Set `grading.system_prompt` to an existing markdown file
- Example: `prompt/system.md`

## 8. No graded files found in graded/

Symptom:

- Score stage cannot find any grading JSON files.

Fix:

1. Run grade stage first
1. Check provider credentials
1. Review `logs/grading.errors.log` if present

## 9. Provider authentication errors

Symptom:

- API request fails with auth/401 errors.

Fix:

1. Set required environment variable in `.env`
1. Check provider entry in [data/providers](../data/providers)
1. Re-run grade stage

## 10. Slow grading throughput

Symptom:

- Grading is slower than expected.

Fix:

- Increase `grading.max_parallel_tasks` (up to `10`)
- Ensure provider and network capacity can sustain parallel requests

## 11. Unexpected old results or skipped grading

Symptom:

- Grade stage skips files unexpectedly.

Fix:

1. Remove `logs/grading.checkpoint.json`
1. Remove stale files in `graded/`
1. Run grade again

## 12. Template file not found for plagiarism stage

Symptom:

- Plagiarism stage fails with missing template file error.

Fix:

1. Create `template.ipynb` in assignment root
1. Or set `[plagiarism].template_file` to the correct path in config

## 13. Plagiarism stage finds no submissions

Symptom:

- Stage reports no submission files found in `raw/`.

Fix:

1. Ensure student `.ipynb` files are under `raw/`
1. If you also want `.py` files, set `[plagiarism].include_python_files = true`
1. Re-run `plagiarism`

## 14. Plagiarism report generated but scores seem too noisy

Symptom:

- Report has many false positives from shared scaffolding.

Fix:

1. Make sure `template.ipynb` contains assignment boilerplate/common starter code
1. Confirm it is correctly configured as `[plagiarism].template_file`
1. Tune `[plagiarism].display_threshold` as needed

## 15. Reference TODO/instruction mismatch review is inconsistent

Symptom:

- You are unsure whether reference TODO implementations strictly match instructions.

Fix:

Run the rule-based audit helper and manually review flagged TODOs:

```bash
uv run misc/reference_mismatch_audit.py \
   --notebook data/my-assignment/reference.ipynb
```
