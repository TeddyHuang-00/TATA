"""Subprocess transports for providers that authenticate through a vendor CLI.

Some people have a ChatGPT or Claude subscription but no API key, and buying
per-token API credit just to grade a lab is a hard sell. Both vendors ship a
signed-in command line client (``codex`` and ``claude``), and both expose a
headless mode meant for exactly this: another program hands them a prompt and
reads the answer back. The CLI owns the OAuth session — it stores the tokens,
refreshes them, and re-prompts for login when they expire. TATA never sees a
credential, which is the whole point of routing through it rather than
re-implementing either vendor's sign-in flow against their private endpoints.

The cost is that neither CLI speaks OpenAI chat-completions, so this module
rebuilds the slice of that interface the pipeline actually calls:

    client.chat.completions.create(model=…, messages=[…], response_model=…)

:class:`CliClient` duck-types it, which keeps ``grading``, ``rubric_gen`` and
the TUI probe unchanged — they cannot tell an instructor client from this one.
Structured output, which instructor would normally get from a tool call, is
recovered by appending the response model's JSON schema to the prompt and
validating what comes back (:func:`_coerce_response`), retrying on a parse
failure with the error fed back to the model.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

#: Seconds to wait on one CLI invocation. Grading prompts carry a whole
#: submission plus a reference answer, and a subscription CLI may queue behind
#: rate limits, so this is deliberately generous.
DEFAULT_TIMEOUT = 900

#: Parse failures to absorb before giving up on one completion. The retry
#: re-sends the prompt with the validation error appended.
DEFAULT_MAX_RETRIES = 2


class Transport(StrEnum):
    """How a provider reaches its model."""

    #: Direct HTTP to an OpenAI-compatible endpoint, authenticated by API key.
    OPENAI = "openai"
    #: Shell out to Claude Code (``claude``), signed in to a Claude account.
    CLAUDE_CLI = "claude_cli"
    #: Shell out to Codex (``codex``), signed in to a ChatGPT account.
    CODEX_CLI = "codex_cli"

    @property
    def is_cli(self) -> bool:
        return self is not Transport.OPENAI


#: Default executable name per transport, overridable via ``cli_path``.
_BINARY: dict[Transport, str] = {
    Transport.CLAUDE_CLI: "claude",
    Transport.CODEX_CLI: "codex",
}

#: Shown when the binary is missing — the fix differs per vendor.
_INSTALL_HINT: dict[Transport, str] = {
    Transport.CLAUDE_CLI: (
        "Install Claude Code (https://claude.com/claude-code), then run "
        "'claude' once and sign in with /login."
    ),
    Transport.CODEX_CLI: (
        "Install Codex (npm i -g @openai/codex), then run 'codex login' and "
        "choose 'Sign in with ChatGPT'."
    ),
}

#: Substrings that mark a CLI failure as "you are not signed in" rather than a
#: transient error, so the message can point at the fix instead of the stderr.
_AUTH_MARKERS = (
    "not logged in",
    "not authenticated",
    "please log in",
    "please run /login",
    "run 'codex login'",
    "invalid api key",
    "authentication_error",
    "oauth token",
    "unauthorized",
)


class CliTransportError(RuntimeError):
    """A CLI-backed completion could not be produced."""


# ---------- prompt assembly ----------


def _decode_data_url(url: str) -> bytes | None:
    """Bytes behind a ``data:…;base64,…`` URL, or None if it is not one."""
    match = re.match(r"^data:[^;,]*;base64,(.*)$", url, re.DOTALL)
    if match is None:
        return None
    try:
        return base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError):
        return None


def _flatten_content(content: str | Sequence[Any], image_sink: list[bytes]) -> str:
    """Collapse one message's content to text, diverting inline images.

    The pipeline sends screenshots as base64 ``image_url`` blocks. Neither CLI
    accepts those inline, but both read images from disk, so the bytes go to
    ``image_sink`` for the caller to spill into temp files and the text keeps a
    placeholder so the model knows an attachment belongs there.
    """
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "image_url":
            url = str(block.get("image_url", {}).get("url", ""))
            raw = _decode_data_url(url)
            if raw is None:
                # A plain http(s) image URL: neither CLI fetches one for us,
                # so pass the address through and let the model ignore it.
                parts.append(f"[image: {url}]")
            else:
                image_sink.append(raw)
                parts.append(f"[image attachment {len(image_sink)}]")
    return "\n\n".join(part for part in parts if part)


def _split_messages(
    messages: Sequence[dict], image_sink: list[bytes]
) -> tuple[str, str]:
    """Split chat messages into (system prompt, user-visible transcript).

    System turns go to the CLI's own system-prompt flag. The rest is
    linearized; role labels are added only for a genuine multi-turn exchange,
    since labelling a single user message just adds noise.
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        text = _flatten_content(message.get("content", ""), image_sink)
        if role == "system":
            system_parts.append(text)
        elif text:
            turns.append((role, text))

    if len(turns) == 1 and turns[0][0] == "user":
        body = turns[0][1]
    else:
        body = "\n\n".join(f"{role.capitalize()}:\n{text}" for role, text in turns)
    return "\n\n".join(system_parts), body


def _schema_instruction(response_model: type[BaseModel]) -> str:
    """The contract that replaces instructor's tool-call structured output."""
    schema = json.dumps(response_model.model_json_schema(), indent=2)
    return (
        "Respond with a single JSON object that validates against this JSON "
        "Schema. Output the JSON object and nothing else: no prose before or "
        "after it, no code fence, no explanation.\n\n"
        f"JSON Schema:\n{schema}"
    )


# ---------- response parsing ----------


def _candidate_json(text: str) -> Iterator[str]:
    """Yield plausible JSON objects in ``text``, most likely first.

    Models wrap JSON in fences or bracket it with a sentence even when told not
    to, so bare ``json.loads`` on the whole reply is not enough.
    """
    stripped = text.strip()
    yield stripped

    for match in re.finditer(
        r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE
    ):
        yield match.group(1).strip()

    # Outermost brace pair, scanned with a depth counter so nested objects and
    # braces inside strings do not truncate the span.
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(stripped):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                yield stripped[start : index + 1]
                start = -1


def _coerce_response(text: str, response_model: type[BaseModel]) -> BaseModel:
    """Validate the first candidate JSON span that satisfies the model."""
    errors: list[str] = []
    for candidate in _candidate_json(text):
        if not candidate:
            continue
        try:
            return response_model.model_validate_json(candidate)
        except ValidationError as exc:
            errors.append(str(exc))
        except ValueError:
            continue  # not JSON at all; try the next span
    detail = errors[0] if errors else "no JSON object found in the reply"
    msg = f"could not parse a {response_model.__name__} from the CLI reply: {detail}"
    raise CliTransportError(msg)


def _claude_text(stdout: str) -> str:
    """Pull the assistant text out of ``claude -p --output-format json``."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout  # --output-format text, or a non-JSON diagnostic
    if not isinstance(payload, dict):
        return stdout
    if payload.get("is_error"):
        msg = f"claude reported an error: {payload.get('result', stdout)}"
        raise CliTransportError(msg)
    result = payload.get("result")
    return str(result) if result is not None else stdout


def _codex_text(stdout: str) -> str:
    """Pull the final assistant message out of ``codex exec --json`` JSONL.

    The event schema has moved across Codex releases, so rather than pin one
    shape this keeps the last agent message under any of the known layouts and
    falls back to the raw stream if none matched.
    """
    latest: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        # Newer: {"type": "item.completed", "item": {"type": "agent_message", …}}
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            latest = str(item.get("text") or item.get("message") or latest or "")
            continue
        # Older: {"msg": {"type": "agent_message", "message": …}}
        inner = event.get("msg")
        if isinstance(inner, dict) and inner.get("type") == "agent_message":
            latest = str(inner.get("message") or inner.get("text") or latest or "")
            continue
        # Flat: {"type": "agent_message", "message": …}
        if event.get("type") == "agent_message":
            latest = str(event.get("message") or event.get("text") or latest or "")
    return latest if latest is not None else stdout


# ---------- invocation ----------


def _resolve_binary(transport: Transport, cli_path: str | None) -> str:
    candidate = cli_path or _BINARY[transport]
    found = shutil.which(candidate)
    if found is None and Path(candidate).is_file():
        found = candidate
    if found is None:
        msg = (
            f"'{candidate}' is not on PATH, so the {transport.value} provider "
            f"cannot run. {_INSTALL_HINT[transport]}"
        )
        raise CliTransportError(msg)
    return found


def _argv(
    transport: Transport,
    binary: str,
    model: str,
    system_prompt: str,
    image_paths: list[Path],
) -> list[str]:
    """Build the headless invocation; the prompt itself arrives on stdin.

    Submissions run to tens of kilobytes, which is close enough to ARG_MAX to
    be worth avoiding, and stdin sidesteps shell quoting entirely.
    """
    if transport is Transport.CLAUDE_CLI:
        argv = [binary, "--print", "--output-format", "json"]
        if model:
            argv += ["--model", model]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        # Grading is a pure text transform: no tools, no MCP servers, no
        # project CLAUDE.md bleeding into the rubric.
        argv += ["--restricted", "--strict-mcp-config", "--max-turns", "1"]
        for path in image_paths:
            argv += ["--add-dir", str(path.parent)]
        return argv

    argv = [binary, "exec", "--json", "--skip-git-repo-check"]
    if model:
        argv += ["--model", model]
    # Read-only sandbox: the model has no reason to touch the filesystem.
    argv += ["--sandbox", "read-only"]
    for path in image_paths:
        argv += ["--image", str(path)]
    return argv


def _run(
    transport: Transport,
    binary: str,
    argv: list[str],
    prompt: str,
    timeout: int,
) -> str:
    try:
        completed = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        msg = f"could not start {binary}: {exc}"
        raise CliTransportError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"{binary} did not finish within {timeout}s"
        raise CliTransportError(msg) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        haystack = detail.lower()
        if any(marker in haystack for marker in _AUTH_MARKERS):
            msg = (
                f"{binary} is installed but not signed in. "
                f"{_INSTALL_HINT[transport]}\n{detail}"
            )
        else:
            msg = f"{binary} exited {completed.returncode}: {detail}"
        raise CliTransportError(msg)

    return completed.stdout


# ---------- client ----------


class _Completions:
    """The ``chat.completions`` surface the pipeline calls."""

    def __init__(
        self,
        transport: Transport,
        cli_path: str | None,
        timeout: int,
        max_retries: int,
    ) -> None:
        self._transport = transport
        self._cli_path = cli_path
        self._timeout = timeout
        self._max_retries = max_retries

    def create(
        self,
        *,
        messages: Sequence[dict],
        model: str = "",
        response_model: type[BaseModel] | None = None,
        **_ignored: Any,  # ruff: ignore[any-type]
    ) -> BaseModel | str:
        """Run one completion.

        Returns a ``response_model`` instance when one is given (matching
        instructor), otherwise the raw reply text. ``temperature`` and
        ``max_tokens`` are accepted and ignored: neither CLI exposes sampling
        controls, so they are silently dropped rather than faked.
        """
        binary = _resolve_binary(self._transport, self._cli_path)
        images: list[bytes] = []
        system_prompt, body = _split_messages(messages, images)

        if response_model is not None:
            body = f"{body}\n\n{_schema_instruction(response_model)}"

        with tempfile.TemporaryDirectory(prefix="tata-cli-") as tmpdir:
            image_paths = _spill_images(images, Path(tmpdir))
            if image_paths and self._transport is Transport.CLAUDE_CLI:
                listing = "\n".join(
                    f"- attachment {index}: {path}"
                    for index, path in enumerate(image_paths, start=1)
                )
                body = f"{body}\n\nRead these image files:\n{listing}"
            argv = _argv(self._transport, binary, model, system_prompt, image_paths)
            return self._complete(argv, binary, body, response_model)

    def _complete(
        self,
        argv: list[str],
        binary: str,
        body: str,
        response_model: type[BaseModel] | None,
    ) -> BaseModel | str:
        """Invoke the CLI, retrying a parse failure with the error attached."""
        prompt = body
        last: CliTransportError | None = None
        for _attempt in range(self._max_retries + 1):
            stdout = _run(self._transport, binary, argv, prompt, self._timeout)
            text = (
                _claude_text(stdout)
                if self._transport is Transport.CLAUDE_CLI
                else _codex_text(stdout)
            )
            if response_model is None:
                return text
            try:
                return _coerce_response(text, response_model)
            except CliTransportError as exc:
                last = exc
                prompt = (
                    f"{body}\n\nYour previous reply could not be parsed:\n{exc}\n"
                    "Reply again with only the JSON object."
                )
        if last is None:  # unreachable: the loop runs at least once
            msg = "no completion attempt was made"
            raise CliTransportError(msg)
        raise last


def _spill_images(images: list[bytes], tmpdir: Path) -> list[Path]:
    """Write inline image bytes to files the CLI can open."""
    paths: list[Path] = []
    for index, raw in enumerate(images, start=1):
        path = tmpdir / f"attachment-{index}.png"
        path.write_bytes(raw)
        paths.append(path)
    return paths


@dataclass(frozen=True)
class _Chat:
    """Namespace matching ``client.chat.completions`` on the real client."""

    completions: _Completions


class CliClient:
    """Instructor-shaped client backed by a signed-in vendor CLI."""

    def __init__(
        self,
        transport: Transport,
        cli_path: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        if not transport.is_cli:
            msg = f"{transport.value} is not a CLI transport"
            raise ValueError(msg)
        self.transport = transport
        self.chat = _Chat(_Completions(transport, cli_path, timeout, max_retries))


def build_cli_client(
    transport: Transport,
    cli_path: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> CliClient:
    """Construction site for CLI-backed clients (mirrors ``build_provider_client``)."""
    return CliClient(transport, cli_path=cli_path, timeout=timeout)


def cli_login_hint(transport: Transport) -> str:
    """One-line setup instruction for a CLI transport."""
    return _INSTALL_HINT.get(transport, "")
