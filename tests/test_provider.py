from __future__ import annotations

import pytest
from instructor import Mode
from pydantic import ValidationError
from src.shared.provider import ProviderInfo


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
