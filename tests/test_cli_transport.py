"""Tests for the CLI-backed provider transports (``src.shared.cli_transport``).

The CLI itself is never invoked: ``_run`` is patched, so these pin the parts
TATA owns — prompt assembly, JSON recovery from a chatty reply, retry on a
parse failure, and the error messages a TA actually sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from src.shared import cli_transport as ct
from src.shared.cli_transport import (
    CliClient,
    CliTransportError,
    Transport,
    _claude_text,
    _codex_text,
    _coerce_response,
    _split_messages,
)


class Grade(BaseModel):
    score: int
    comment: str


def _client(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[str],
    max_retries: int = ct.DEFAULT_MAX_RETRIES,
) -> CliClient:
    """A CliClient whose CLI returns ``replies`` in order."""
    monkeypatch.setattr(ct, "_resolve_binary", lambda *_a, **_k: "/fake/claude")
    sent: list[str] = []

    def fake_run(
        _transport: Transport,
        _binary: str,
        _argv: list[str],
        prompt: str,
        _timeout: int,
    ) -> str:
        sent.append(prompt)
        return json.dumps({"is_error": False, "result": replies[len(sent) - 1]})

    monkeypatch.setattr(ct, "_run", fake_run)
    client = CliClient(Transport.CLAUDE_CLI, max_retries=max_retries)
    client.sent = sent  # type: ignore[attr-defined]
    return client


class TestMessageFlattening:
    def test_system_is_split_from_body(self) -> None:
        system, body = _split_messages(
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
            ],
            [],
        )
        assert system == "be terse"
        assert body == "hello"  # lone user turn carries no role label

    def test_multi_turn_gets_role_labels(self) -> None:
        _system, body = _split_messages(
            [
                {"role": "user", "content": "reference"},
                {"role": "user", "content": "submission"},
            ],
            [],
        )
        assert body == "User:\nreference\n\nUser:\nsubmission"

    def test_inline_image_is_diverted_to_sink(self) -> None:
        sink: list[bytes] = []
        _system, body = _split_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "grade this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aGk="},
                        },
                    ],
                }
            ],
            sink,
        )
        assert sink == [b"hi"]
        assert "grade this" in body
        assert "[image attachment 1]" in body


class TestResponseParsing:
    def test_bare_json(self) -> None:
        got = _coerce_response('{"score": 7, "comment": "ok"}', Grade)
        assert got.score == 7

    def test_fenced_json(self) -> None:
        got = _coerce_response('```json\n{"score": 3, "comment": "x"}\n```', Grade)
        assert got.score == 3

    def test_json_surrounded_by_prose(self) -> None:
        got = _coerce_response(
            'Sure!\n{"score": 9, "comment": "great"}\nHope that helps.', Grade
        )
        assert got.score == 9

    def test_braces_inside_strings_do_not_truncate(self) -> None:
        got = _coerce_response('{"score": 1, "comment": "uses {} literal"}', Grade)
        assert got.comment == "uses {} literal"

    def test_unparseable_raises(self) -> None:
        with pytest.raises(CliTransportError, match="could not parse"):
            _coerce_response("I'd rather not.", Grade)


class TestVendorOutputExtraction:
    def test_claude_json_result(self) -> None:
        assert _claude_text(json.dumps({"is_error": False, "result": "hi"})) == "hi"

    def test_claude_error_raises(self) -> None:
        with pytest.raises(CliTransportError, match="claude reported an error"):
            _claude_text(json.dumps({"is_error": True, "result": "boom"}))

    def test_claude_plain_text_passthrough(self) -> None:
        assert _claude_text("not json") == "not json"

    @pytest.mark.parametrize(
        "line",
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}',
            '{"msg":{"type":"agent_message","message":"hi"}}',
            '{"type":"agent_message","message":"hi"}',
        ],
    )
    def test_codex_event_shapes(self, line: str) -> None:
        assert _codex_text(f'{{"type":"thread.started"}}\n{line}\n') == "hi"

    def test_codex_keeps_last_message(self) -> None:
        stream = (
            '{"type":"agent_message","message":"first"}\n'
            '{"type":"agent_message","message":"second"}\n'
        )
        assert _codex_text(stream) == "second"


class TestCreate:
    def test_structured_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(monkeypatch, ['{"score": 5, "comment": "fine"}'])
        got = client.chat.completions.create(
            model="sonnet",
            response_model=Grade,
            messages=[{"role": "user", "content": "grade"}],
        )
        assert isinstance(got, Grade)
        assert got.score == 5

    def test_schema_is_appended_to_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(monkeypatch, ['{"score": 5, "comment": "fine"}'])
        client.chat.completions.create(
            model="sonnet",
            response_model=Grade,
            messages=[{"role": "user", "content": "grade"}],
        )
        assert "JSON Schema" in client.sent[0]  # type: ignore[attr-defined]

    def test_no_response_model_returns_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(monkeypatch, ["pong"])
        assert (
            client.chat.completions.create(
                model="sonnet", messages=[{"role": "user", "content": "ping"}]
            )
            == "pong"
        )

    def test_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(monkeypatch, ["nope", '{"score": 2, "comment": "ok"}'])
        got = client.chat.completions.create(
            model="sonnet",
            response_model=Grade,
            messages=[{"role": "user", "content": "grade"}],
        )
        assert got.score == 2
        sent = client.sent  # type: ignore[attr-defined]
        assert len(sent) == 2
        assert "could not be parsed" in sent[1]

    def test_gives_up_after_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(monkeypatch, ["no"] * 5, max_retries=1)
        with pytest.raises(CliTransportError, match="could not parse"):
            client.chat.completions.create(
                model="sonnet",
                response_model=Grade,
                messages=[{"role": "user", "content": "grade"}],
            )
        assert len(client.sent) == 2  # type: ignore[attr-defined]

    def test_sampling_kwargs_are_ignored_not_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The TUI probe passes max_tokens; grading may pass temperature."""
        client = _client(monkeypatch, ["pong"])
        assert (
            client.chat.completions.create(
                model="sonnet",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0.0,
            )
            == "pong"
        )


class TestStrictSchema:
    """Codex uses native structured output; strict mode has extra rules."""

    def test_objects_get_additional_properties_false(self) -> None:
        schema = ct._strict_schema(Grade)
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["comment", "score"]

    def test_nested_defs_are_rewritten_too(self) -> None:
        class Inner(BaseModel):
            a: str

        class Outer(BaseModel):
            items: list[Inner]

        schema = ct._strict_schema(Outer)
        assert schema["$defs"]["Inner"]["additionalProperties"] is False
        assert schema["$defs"]["Inner"]["required"] == ["a"]

    def test_optional_fields_are_forced_required(self) -> None:
        class Opt(BaseModel):
            a: str
            b: str | None = None

        assert ct._strict_schema(Opt)["required"] == ["a", "b"]


class TestCodexNativeSchema:
    def test_schema_and_last_message_flags_are_passed(self) -> None:
        argv = ct._argv(
            Transport.CODEX_CLI,
            "codex",
            "gpt-5",
            "",
            [],
            schema_path=Path("/tmp/s.json"),
            last_message_path=Path("/tmp/last.txt"),
        )
        assert "--output-schema" in argv
        assert "--output-last-message" in argv
        assert "--sandbox" in argv
        assert "read-only" in argv

    def test_claude_argv_has_no_schema_flags(self) -> None:
        argv = ct._argv(Transport.CLAUDE_CLI, "claude", "sonnet", "sys", [])
        assert "--output-schema" not in argv
        assert "--append-system-prompt" in argv
        assert "--restricted" in argv

    def test_last_message_file_wins_over_events(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The written file is authoritative; events are only a fallback."""
        monkeypatch.setattr(ct, "_resolve_binary", lambda *_a, **_k: "/fake/codex")

        def fake_run(_t: Transport, _b: str, argv: list[str], _p: str, _to: int) -> str:
            out = Path(argv[argv.index("--output-last-message") + 1])
            out.write_text('{"score": 4, "comment": "from file"}', encoding="utf-8")
            return '{"type":"item.completed","item":{"type":"agent_message","text":"ignored"}}'

        monkeypatch.setattr(ct, "_run", fake_run)
        got = CliClient(Transport.CODEX_CLI).chat.completions.create(
            model="gpt-5",
            response_model=Grade,
            messages=[{"role": "user", "content": "grade"}],
        )
        assert got.comment == "from file"

    def test_rejected_schema_falls_back_to_prompting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict mode refusing the schema must not fail the submission."""
        monkeypatch.setattr(ct, "_resolve_binary", lambda *_a, **_k: "/fake/codex")
        seen: list[bool] = []

        def fake_run(
            _t: Transport, _b: str, argv: list[str], prompt: str, _to: int
        ) -> str:
            used_native = "--output-schema" in argv
            seen.append(used_native)
            if used_native:
                msg = "codex exited 1: invalid_json_schema"
                raise CliTransportError(msg)
            assert "JSON Schema" in prompt  # degraded to the prompted path
            out = Path(argv[argv.index("--output-last-message") + 1])
            out.write_text('{"score": 6, "comment": "fallback"}', encoding="utf-8")
            return ""

        monkeypatch.setattr(ct, "_run", fake_run)
        got = CliClient(Transport.CODEX_CLI).chat.completions.create(
            model="gpt-5",
            response_model=Grade,
            messages=[{"role": "user", "content": "grade"}],
        )
        assert got.comment == "fallback"
        assert seen == [True, False]

    def test_other_errors_still_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ct, "_resolve_binary", lambda *_a, **_k: "/fake/codex")

        def fake_run(*_a: object) -> str:
            msg = "codex is installed but not signed in."
            raise CliTransportError(msg)

        monkeypatch.setattr(ct, "_run", fake_run)
        with pytest.raises(CliTransportError, match="not signed in"):
            CliClient(Transport.CODEX_CLI).chat.completions.create(
                model="gpt-5",
                response_model=Grade,
                messages=[{"role": "user", "content": "x"}],
            )

    def test_turn_failed_event_surfaces_the_reason(self) -> None:
        stream = (
            '{"type":"thread.started"}\n'
            '{"type":"error","message":"rate limit exceeded"}\n'
        )
        with pytest.raises(CliTransportError, match="rate limit exceeded"):
            _codex_text(stream)


class TestFailureMessages:
    def test_missing_binary_names_the_install_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ct.shutil, "which", lambda _c: None)
        client = CliClient(Transport.CODEX_CLI)
        with pytest.raises(CliTransportError, match="codex login"):
            client.chat.completions.create(
                model="gpt-5", messages=[{"role": "user", "content": "x"}]
            )

    def test_auth_failure_names_the_login_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ct, "_resolve_binary", lambda *_a, **_k: "/fake/claude")

        class Done:
            returncode = 1
            stdout = ""
            stderr = "Error: not logged in"

        monkeypatch.setattr(ct.subprocess, "run", lambda *_a, **_k: Done())
        client = CliClient(Transport.CLAUDE_CLI)
        with pytest.raises(CliTransportError, match="not signed in"):
            client.chat.completions.create(
                model="sonnet", messages=[{"role": "user", "content": "x"}]
            )

    def test_openai_transport_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a CLI transport"):
            CliClient(Transport.OPENAI)
