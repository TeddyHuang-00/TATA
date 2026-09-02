# Provider Config Format

This guide explains how to structure provider files under `data/providers/`
without schema validation.

## Where these files sit in the workflow

During `grade` stage, `grading.provider` in assignment config selects one
provider file from `data/providers/`. That file determines:

- API endpoint (`base_url`)
- Authentication (`api_key`)
- Model selection (`model`)
- Instructor parsing mode (`mode`)

If provider config is wrong, grading fails before or during API calls.

## Top-level structure

One provider per TOML file named `<provider_name>.toml` in `data/providers/`.
The filename stem is the provider name; keys are flat top-level fields:

```toml
base_url = "..."
api_key = "..."
model = "..."
mode = "..."
```

## Example

`data/providers/deepseek_chat_tool.toml`:

```toml
base_url = "https://api.deepseek.com"
api_key = "${DEEPSEEK_API_KEY}"
model = "deepseek-chat"
mode = "tool_call"
```

`data/providers/ollama.toml`:

```toml
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
- `temperature` (optional float, `0.0`-`2.0`): sampling temperature. Omitted or `None` for provider default.

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

`data/<course>/<assignment>/config.toml` -> `[grading].provider` must exactly match a provider file stem in `data/providers/`.

Example:

- Assignment config uses `provider = "deepseek_chat_tool"`
- Then `data/providers/deepseek_chat_tool.toml` must exist.

## Common mistakes

- Missing required field (`base_url/api_key/model/mode`).
- Invalid `mode` value.
- Typo between assignment provider key and provider file stem.
- Placeholder env var not exported in runtime environment.
