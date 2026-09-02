from __future__ import annotations

from pathlib import Path

import pytest
from instructor import Mode
from pydantic import ValidationError
from src.shared.provider import ProviderInfo, get_providers


class TestProviderInfoTemperature:
    """Tests for the temperature field on ProviderInfo."""

    def test_temperature_defaults_to_none(self) -> None:
        info = ProviderInfo(
            base_url="http://localhost",
            api_key="sk-test",
            model="gpt-4",
            mode=Mode.TOOLS,
        )
        assert info.temperature is None

    def test_temperature_accepts_zero(self) -> None:
        info = ProviderInfo(
            base_url="http://localhost",
            api_key="sk-test",
            model="gpt-4",
            mode=Mode.TOOLS,
            temperature=0.0,
        )
        assert info.temperature == pytest.approx(0.0)

    def test_temperature_accepts_max(self) -> None:
        info = ProviderInfo(
            base_url="http://localhost",
            api_key="sk-test",
            model="gpt-4",
            mode=Mode.TOOLS,
            temperature=2.0,
        )
        assert info.temperature == pytest.approx(2.0)

    def test_temperature_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            ProviderInfo(
                base_url="http://localhost",
                api_key="sk-test",
                model="gpt-4",
                mode=Mode.TOOLS,
                temperature=-0.1,
            )

    def test_temperature_rejects_above_max(self) -> None:
        with pytest.raises(ValidationError):
            ProviderInfo(
                base_url="http://localhost",
                api_key="sk-test",
                model="gpt-4",
                mode=Mode.TOOLS,
                temperature=2.1,
            )


def _write_provider(base: Path, name: str, text: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}.toml").write_text(text, encoding="utf-8")


class TestGetProviders:
    """Folder loading: one flat provider file per name (data/providers/)."""

    def test_loads_names_from_stems(self, tmp_path: Path) -> None:
        _write_provider(
            tmp_path,
            "ollama",
            'base_url = "http://localhost:11434/v1"\n'
            'api_key = "ollama"\n'
            'model = "qwen3.8:latest"\n'
            'mode = "markdown_json_mode"\n',
        )
        _write_provider(
            tmp_path,
            "deepseek",
            'base_url = "https://api.deepseek.com"\n'
            'api_key = "${DEEPSEEK_API_KEY}"\n'
            'model = "deepseek-chat"\n'
            'mode = "tool_call"\n'
            "temperature = 0.3\n",
        )
        providers = get_providers(tmp_path)
        assert sorted(providers.providers) == ["deepseek", "ollama"]
        assert providers.providers["ollama"].base_url == "http://localhost:11434/v1"
        assert providers.providers["deepseek"].temperature == pytest.approx(0.3)

    def test_missing_dir_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="no provider files"):
            get_providers(tmp_path / "nope")

    def test_bad_file_raises_value_error(self, tmp_path: Path) -> None:
        _write_provider(tmp_path, "broken", "not toml\n")
        with pytest.raises(ValueError, match="invalid provider file"):
            get_providers(tmp_path)

    def test_env_placeholder_resolved_through_getitem(self, tmp_path: Path) -> None:
        _write_provider(
            tmp_path,
            "deepseek",
            'base_url = "https://api.deepseek.com"\n'
            'api_key = "${TEST_PROVIDER_KEY}"\n'
            'model = "deepseek-chat"\n'
            'mode = "tool_call"\n',
        )
        providers = get_providers(tmp_path)
        resolved = providers["deepseek"]
        assert resolved.api_key == ""  # unset env -> empty, then resolved
        assert resolved.model == "deepseek-chat"
