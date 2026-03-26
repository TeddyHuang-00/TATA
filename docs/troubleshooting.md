# Troubleshooting

## 1. Invalid assignment config

Symptom:

- Error says config is invalid or required fields are missing.

Fix:

1. Start from [assignments/example/config.toml](../assignments/example/config.toml)
2. Ensure `[grading]` has all required keys:
   - `rubric`
   - `system_prompt`
   - `provider`
3. Validate paths for rubric and prompt files.

## 2. Invalid TOML syntax

Symptom:

- Error indicates TOML parsing failed.

Fix:

- Check for missing quotes, commas, or section headers.
- Regenerate schemas with `--stage schema` and use editor schema validation.

## 3. No supported files found in raw/

Symptom:

- Preprocess reports no supported files.

Fix:

1. Place files in `raw/`
2. Use supported extensions: `.ipynb`, `.html`, `.md`
3. If needed, set `[processing].input_format`

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
2. Or set `assignment.reference_file` explicitly

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
- Example: `prompt/lab.md`

## 8. No graded files found in graded/

Symptom:

- Score stage cannot find any grading JSON files.

Fix:

1. Run grade stage first
2. Check provider credentials
3. Review `logs/grading.errors.log` if present

## 9. Provider authentication errors

Symptom:

- API request fails with auth/401 errors.

Fix:

1. Set required environment variable in `.env`
2. Check provider entry in [config/provider.toml](../config/provider.toml)
3. Re-run grade stage

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
2. Remove stale files in `graded/`
3. Run grade again
