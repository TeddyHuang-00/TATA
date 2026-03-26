# TATA Hooks

Hooks are optional Python scripts mounted to lifecycle points via assignment config.

## Hook Contract

- Hook script path is resolved from the unified hooks directory (default: `hooks/` at repo root).
- Hook receives a JSON object on `stdin`.
- Hook must write a JSON object to `stdout`.
- Returning empty stdout means "no changes".
- Non-zero exit code fails the current stage.

Environment variables provided to hook:

- `TATA_HOOK_MOUNT_POINT`
- `TATA_HOOK_PROJECT_ROOT`

## Supported Mount Points

- `before_preprocess`
- `before_preprocess_file`
- `after_preprocess_file`
- `after_preprocess`
- `before_grade`
- `before_grade_submission`
- `after_grade_submission`
- `after_grade`
- `before_score`
- `after_score`
- `before_analyze`
- `after_analyze`

## Assignment Config Example

```toml
[hooks]
dir = "hooks"

[hooks.mounts]
before_grade_submission = "trim_grade_payload.py"
```

You can also attach multiple hooks to one lifecycle point.

```toml
[hooks]
dir = "hooks"

[hooks.mounts]
before_preprocess_file = [
  "extract_todo_code_snippets.py",
  "normalize_todo_style.py",
]
before_grade_submission = "trim_grade_payload.py"
```

Execution rules:

- Omit a mount point to run no hook at that lifecycle.
- Use a string for one hook.
- Use a list for multiple hooks.
- When a list is provided, hooks run sequentially in the listed order, each receiving the previous hook output.

Only hooks explicitly mounted in `hooks.mounts` are active.
