from __future__ import annotations

import json
from pathlib import Path

from src.shared.assignment_config import write_assignment_schema
from src.shared.provider import ProviderList
from src.shared.rubric import RubricDefinition


def generate_all_schemas(project_root: Path) -> list[Path]:
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    assignment_schema = write_assignment_schema(config_dir / "assignment.schema.json")

    provider_schema = config_dir / "provider.schema.json"
    provider_schema.write_text(
        json.dumps(ProviderList.model_json_schema(), indent=4),
        encoding="utf-8",
    )

    rubric_schema = config_dir / "rubric.schema.json"
    rubric_schema.write_text(
        json.dumps(RubricDefinition.model_json_schema(), indent=4),
        encoding="utf-8",
    )

    return [assignment_schema, provider_schema, rubric_schema]
