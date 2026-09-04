from __future__ import annotations

from pathlib import Path

import tomlkit

from .assignment_config import load_assignment_file
from .grading import build_client
from .provider import get_providers
from .rubric import RubricDefinition

#: System prompt for rubric generation: role, output schema, and content rules.
RUBRIC_GEN_SYSTEM_PROMPT = """
You are a teaching assistant for a university course. Given an assignment
description, design a grading rubric that an automated grader can apply to
every student submission.

Output a RubricDefinition: an array "criterion". Each criterion is an object:
- "name": short, distinctive criterion name.
- "desc": a precise, self-contained evaluation instruction. It must say exactly
  what earns or loses points, and be specific enough that a grader can locate
  the relevant part of a student answer and apply it. It must not just restate
  the assignment requirement.
- "rating": one of "binary", "ternary", "likert" (the correctness scale).
- "grading": one of "standard", "strict", "round up", "custom". When "custom",
  also provide "custom_scale": one value per rating of the chosen scale,
  ordered from lowest to highest.
- "pts": a positive number of points awarded for this criterion.

Rules:
- Cover every major requirement of the assignment with at least one criterion.
- Prefer fewer, broader criteria (typically 3-10) over many overlapping ones.
- All "pts" must be positive; all "name"/"desc" must be non-empty.
- pts across all criteria should sum to the assignment total (100 unless the assignment states otherwise).
- Respond only with the RubricDefinition object.
"""


def _validate_rubric_content(rubric: RubricDefinition) -> None:
    """Content check complementing pydantic's structural validation."""
    if not rubric.criterion:
        msg = "Generated rubric has no criteria; at least one is required."
        raise ValueError(msg)

    for criterion in rubric.criterion:
        if not criterion.name.strip():
            msg = "Generated rubric contains a criterion with an empty name."
            raise ValueError(msg)
        if not criterion.desc.strip():
            msg = f"Generated rubric criterion '{criterion.name}' has an empty desc."
            raise ValueError(msg)
        if criterion.pts <= 0:
            msg = f"Generated rubric criterion '{criterion.name}' has non-positive pts: {criterion.pts}."
            raise ValueError(msg)


def _write_rubric(out_path: Path, rubric: RubricDefinition) -> None:
    """Write the rubric as ``[[criterion]]`` TOML (same format as the TUI)."""
    doc = tomlkit.document()
    rows = tomlkit.aot()
    for criterion in rubric.criterion:
        rows.append(
            tomlkit.item({
                key: value
                for key, value in criterion.model_dump().items()
                if value is not None
            })
        )
    doc["criterion"] = rows
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def generate_rubric(assignment_config_path: Path, out_path: Path) -> RubricDefinition:
    """Generate a rubric from the assignment description using [grading].provider.

    ``out_path`` receives the rubric TOML. The provider (and its temperature)
    is the grader's — the same provider configured in ``[grading]``.
    """
    if out_path.exists():
        msg = (
            f"Output already exists: {out_path}. "
            "Choose a different -o path or delete the existing file first."
        )
        raise ValueError(msg)

    cfg = load_assignment_file(assignment_config_path)
    provider_name = str(cfg.grading.provider)

    assignment_dir = assignment_config_path.resolve().parent
    assignment_file = assignment_dir / "assignment.md"
    if not assignment_file.exists():
        msg = (
            f"Assignment description not found: {assignment_file}\n"
            "Run fetch first (it saves the assignment description as assignment.md)."
        )
        raise ValueError(msg)
    assignment_text = assignment_file.read_text(encoding="utf-8")

    providers = get_providers()
    if provider_name not in providers.providers:
        msg = (
            f"Provider '{provider_name}' not found in the provider list. "
            f"Available providers: {sorted(providers.providers)}"
        )
        raise ValueError(msg)

    client, model_name = build_client(provider_name)
    rubric = client.chat.completions.create(
        model=model_name,
        response_model=RubricDefinition,
        messages=[
            {"role": "system", "content": RUBRIC_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": f"Assignment Description:\n{assignment_text}"},
        ],
    )

    # pydantic (via instructor) validated the structure; check the content too.
    _validate_rubric_content(rubric)
    _write_rubric(out_path, rubric)
    return rubric
