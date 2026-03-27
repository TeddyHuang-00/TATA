# Rubric Config Format

This guide explains how to structure rubric TOML files without schema validation.

## Where this file sits in the workflow

Rubric is the grading contract for the `grade` and `score` stages:

- `grade` uses rubric criteria to build dynamic grading response schema.
- `score` converts ratings to numeric points using each criterion's grading settings.

A malformed rubric can break grading or produce incorrect score weighting.

## File location

Recommended: `rubrics/<name>.toml`

## Top-level structure

Use repeated `[[criterion]]` blocks.

```toml
[[criterion]]
name = "TODO 1"
desc = "What to evaluate"
pts = 10
rating = "ternary"
grading = "standard"
```

## Required fields per criterion

- `name` (string): unique criterion name.
- `desc` (string): grading guidance text used by the model.
- `pts` (number): point weight for this criterion.
- `rating` (enum string): rating scale.
- `grading` (enum string): score conversion strategy.

## Accepted values

## `rating`

- `binary`
- `ternary`
- `likert`

## `grading`

- `standard`
- `strict`
- `round up`
- `custom`

## Optional field

- `custom_scale` (list[number]) only when `grading = "custom"`.

## How `rating` and `grading` affect scoring

- `rating` chooses the label set the model can return.
- `grading` controls how those labels map to numeric points.

Practical behavior:

- `standard`: partial levels receive partial points.
- `strict`: only highest rating gets full points.
- `round up`: like standard but rounded to integer points.
- `custom`: uses your explicit `custom_scale` mapping.

## Rules for `custom_scale`

When `grading = "custom"`:

- `custom_scale` is required.
- Length must match `rating` size:
  - `binary` -> 2 values
  - `ternary` -> 3 values
  - `likert` -> 5 values
- Values must be non-decreasing.

Example:

```toml
[[criterion]]
name = "Model selection"
desc = "Chooses and justifies the best model"
pts = 15
rating = "ternary"
grading = "custom"
custom_scale = [0, 8, 15]
```

## Workflow-oriented writing tips

- `desc` should be concrete enough to distinguish correct/partial/incorrect behavior.
- Keep each criterion focused on one competency to reduce ambiguous model judgments.
- Use higher `pts` for algorithmic complexity, lower `pts` for simple API calls.
- Ensure total `pts` matches your grading policy (commonly 100).

## Common mistakes

- Duplicate or ambiguous criterion names.
- `grading = "custom"` without `custom_scale`.
- Wrong `custom_scale` length for selected rating.
- Non-monotonic `custom_scale` values.
- Vague descriptions that do not define observable evidence.
