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

`data/providers/deepseek.toml`:

```toml
base_url = "https://api.deepseek.com"
api_key = "${DEEPSEEK_API_KEY}"
model = "deepseek-flash"
mode = "tool_call"
```

`data/providers/ollama.toml` (recommended first provider — no API key; run
`ollama serve` locally and `ollama pull qwen3.8:latest` before grading):

```toml
base_url = "http://localhost:11434/v1"
api_key = "ollama"
model = "qwen3.8:latest"
mode = "tool_call"
```

`qwen3.8:latest` is the Qwen3.8-27B model (27.3B parameters); `qwen3.8`
supports vision and tools, so it can be used for `visual_evaluation` mode.

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

- Assignment config uses `provider = "deepseek"`
- Then `data/providers/deepseek.toml` must exist.

## Common mistakes

- Missing required field (`base_url/api_key/model/mode`).
- Invalid `mode` value.
- Typo between assignment provider key and provider file stem.
- Placeholder env var not exported in runtime environment.

## Subscription-backed providers (no API key)

A provider normally calls an OpenAI-compatible endpoint with an API key.
The `transport` field changes that: instead of making the HTTP call
itself, TATA hands the prompt to a vendor command line tool that you have
already signed in to, and that tool supplies the credentials.

This exists for a common case — you pay for ChatGPT or Claude already,
and buying separate per-token API credit just to grade a lab is hard to
justify.

| `transport` | CLI | Sign in with |
| --- | --- | --- |
| `openai` (default) | — | `api_key` |
| `claude_cli` | `claude` | run `claude`, then `/login` |
| `codex_cli` | `codex` | `codex login`, "Sign in with ChatGPT" |

A CLI provider file omits `base_url` and `api_key` entirely:

```toml
transport = 'claude_cli'
model = 'sonnet'
mode = 'json_schema_mode'
```

Optional keys:

- `cli_path` — path to the executable, when it is not on `PATH`.
- `timeout` — seconds to wait on one invocation (default 900).

A blank `model` is allowed and means "use whatever model the CLI is
already configured for" — Codex reads `~/.codex/config.toml`, Claude Code
uses its own default. That is the safer default for a shared config,
since it cannot name a model your plan does not have.

### Finding the Codex binary on macOS

Codex ships inside the ChatGPT desktop app and is **not** on `PATH` when
installed that way. If `codex` is not found, point `cli_path` at it:

```toml
cli_path = '/Applications/ChatGPT.app/Contents/Resources/codex'
```

### Why delegate instead of implementing the login

Neither vendor offers an OAuth registration that a third-party tool can
sign up for. TATA could embed the official clients' credentials and drive
their private endpoints, but that impersonates those clients: it breaks
whenever they rotate, and it puts your account at risk of suspension.
Delegating to the installed CLI is the supported path, and it means TATA
never stores or even sees a token — the CLI owns the session, refreshes
it, and prompts you to sign in again when it lapses.

### Trade-offs

- **Slower.** Each submission spawns a process, so expect noticeably
  longer runs than a direct API call on a large class.
- **No `temperature`.** Neither CLI exposes sampling controls, so the
  field is ignored rather than faked. Grading benefits from
  `temperature = 0.0`; if run-to-run consistency matters to you, prefer a
  keyed provider.
- **Structured output differs by transport.** `codex_cli` passes the
  response schema to `codex exec --output-schema`, so the API enforces it
  the same way a keyed provider would. `claude_cli` has no equivalent
  flag, so the JSON Schema is appended to the prompt and the reply is
  validated, retrying twice with the error fed back — reliable with
  strong models, less so with small ones. If strict mode rejects a
  generated schema, the codex path degrades to prompting rather than
  failing the submission.
- **Subject to your subscription's rate limits**, which are tuned for
  interactive use, not batch grading.

### Troubleshooting

Use **Test connection** in the TUI (Library -> Providers), which runs a
real one-token completion and doubles as a login check.

- `'claude' is not on PATH` — the tool is not installed, or is installed
  somewhere unusual; set `cli_path`.
- `... is installed but not signed in` — run the sign-in command above.
- `could not parse a <Model> from the CLI reply` — the model did not
  return valid JSON after retries; try a stronger `model`.
- `codex reported an error: ...` — the turn failed upstream (rate limit,
  unavailable model). The message is passed through verbatim; a model
  name your plan lacks is the usual cause, so try a blank `model`.
