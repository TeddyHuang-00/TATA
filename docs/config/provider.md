# Provider Config Format

This guide explains how to structure `config/provider.toml` without schema validation.

## Where this file sits in the workflow

During `grade` stage, `grading.provider` in assignment config selects one provider entry from this file. That entry determines:

- API endpoint (`base_url`)
- Authentication (`api_key`)
- Model selection (`model`)
- Instructor parsing mode (`mode`)

If provider config is wrong, grading fails before or during API calls.

## Top-level structure

Each provider is a TOML table under `providers`:

```toml
[providers.<provider_name>]
base_url = "..."
api_key = "..."
model = "..."
mode = "..."
```

## Example

```toml
[providers.deepseek_chat_tool]
base_url = "https://api.deepseek.com"
api_key = "${DEEPSEEK_API_KEY}"
model = "deepseek-chat"
mode = "tool_call"

[providers.ollama]
base_url = "http://localhost:11434/v1"
api_key = "ollama"
model = "qwen3.5:35b-a3b"
mode = "markdown_json_mode"
```

## Field behavior

- `base_url` (string): request target used by OpenAI-compatible client.
- `api_key` (string): auth token; supports `${ENV_VAR}` placeholder substitution at runtime.
- `model` (string): model name passed to provider API.
- `mode` (string): instructor response parsing mode.

## Allowed mode values

Use values currently supported by runtime/instructor mode enum:

- `markdown_json_mode`
- `tool_call`
- `tools_strict`

Using an unsupported mode fails validation at provider load.

## Environment variable placeholders

`api_key` can reference environment variables:

- Config: `api_key = "${DEEPSEEK_API_KEY}"`
- Runtime: placeholder replaced with env value.

If env var is missing, placeholder resolves to empty string, which usually causes auth failure.

## Cross-file consistency rule

`assignments/<name>/config.toml` -> `[grading].provider` must exactly match a key under `[providers.<name>]` here.

Example:

- Assignment config uses `provider = "deepseek_chat_tool"`
- Then this file must contain `[providers.deepseek_chat_tool]`

## Common mistakes

- Missing required field (`base_url/api_key/model/mode`).
- Invalid `mode` value.
- Typo between assignment provider key and provider table name.
- Placeholder env var not exported in runtime environment.
