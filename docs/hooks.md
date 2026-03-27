# Hooks Configuration and Lifecycle

This guide explains how hooks work end-to-end, what payload they receive, what they should return, and how to wire them in assignment config.

## What hooks are for

Hooks let you inject lightweight Python scripts into stage lifecycle events without modifying core pipeline code.

Common use cases:

- normalize text before grading
- inject assignment-specific metadata
- trim noisy fields from model output
- export custom audit metadata after stage completion

## Runtime contract

Every hook script follows the same contract:

1. Input is a JSON object from stdin.
2. Output must be a JSON object to stdout.
3. Exit code 0 means success.
4. Non-zero exit code fails the current stage.
5. Empty stdout means no changes (original payload is kept).

Environment variables available to every hook:

- TATA_HOOK_MOUNT_POINT
- TATA_HOOK_PROJECT_ROOT

## How to configure hooks in assignment config

```toml
[hooks]
dir = "hooks"

[hooks.mounts]
before_preprocess_file = "extract_todo_code_snippets.py"
before_grade_submission = [
  "trim_grade_payload.py",
  "my_grade_input_patch.py",
]
after_grade_submission = "my_grade_output_tag.py"
```

Behavior notes:

- hooks.dir is resolved from repository root.
- a mount value can be one script path or a list of script paths.
- when a list is used, scripts run in order and each receives the previous script output.

## Lifecycle map

Current mount points:

- before_preprocess
- before_preprocess_file
- after_preprocess_file
- after_preprocess
- before_grade
- before_grade_submission
- after_grade_submission
- after_grade
- before_score
- after_score
- before_analyze
- after_analyze
- before_plagiarism
- after_plagiarism

## Payload shapes by mount point

The payload is always a JSON object. Below are the fields sent by each lifecycle point.

## Preprocess

before_preprocess:

- assignment_config
- raw_dir
- processed_dir
- configured_formats

before_preprocess_file:

- assignment_config
- input_file
- output_file
- input_format

Important: this hook can modify input_file/output_file/input_format.

after_preprocess_file:

- assignment_config
- input_file
- output_file
- input_format
- success
- error (only on failure)

after_preprocess:

- assignment_config
- raw_dir
- processed_dir
- processed_count
- failed_count

## Grade

before_grade:

- assignment_config
- submission_count
- processed_dir
- graded_dir

before_grade_submission:

- assignment_config
- submission_name
- submission_path
- student_text
- reference_text
- system_prompt

Important: this hook can modify student_text/reference_text/system_prompt.

after_grade_submission:

- assignment_config
- submission_name
- submission_path
- result_json
- error

Important: when error is null, this hook can modify result_json.

after_grade:

- assignment_config
- done_count
- error_count
- graded_dir
- errors_log

## Score

before_score:

- assignment_config
- graded_dir
- graded_count

after_score:

- assignment_config
- graded_dir
- scored_count
- error_count

## Analyze

before_analyze:

- assignment_config

after_analyze:

- assignment_config
- json_output
- md_output

## Plagiarism

before_plagiarism:

- assignment_config
- raw_dir
- output_dir
- template_file

after_plagiarism:

- assignment_config
- report_file
- full_pairs_file
- submissions_dir
- template_dir
- success_count
- error_count

## Hook function IO examples (naive)

## Example 1: pass-through hook

```python
#!/usr/bin/env python3
import json
import sys

payload = json.loads(sys.stdin.read() or "{}")
print(json.dumps(payload))
```

## Example 2: trim student text before grading

Mount: before_grade_submission

```python
#!/usr/bin/env python3
import json
import sys

payload = json.loads(sys.stdin.read() or "{}")
text = str(payload.get("student_text", ""))
payload["student_text"] = text[:20000]
print(json.dumps(payload))
```

## Example 3: annotate grading JSON after success

Mount: after_grade_submission

```python
#!/usr/bin/env python3
import json
import sys

payload = json.loads(sys.stdin.read() or "{}")
if payload.get("error") is None:
    result_json = payload.get("result_json", "")
    try:
        result = json.loads(result_json)
        result["_hook_tag"] = "postprocessed"
        payload["result_json"] = json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        pass
print(json.dumps(payload))
```

## Example 4: no-op on stdout

If a hook prints nothing and exits 0, runtime keeps original payload unchanged.

```python
#!/usr/bin/env python3
import sys

_ = sys.stdin.read()
sys.exit(0)
```

## Failure behavior

- if hook exits non-zero, the stage fails immediately.
- if stdout is invalid JSON, the stage fails.
- if stdout is valid JSON but not an object, the stage fails.

## Authoring checklist

1. Always read stdin safely.
2. Always output a JSON object when producing output.
3. Keep hooks fast and deterministic.
4. Avoid network calls unless necessary.
5. Make mount-specific assumptions explicit in code comments.
