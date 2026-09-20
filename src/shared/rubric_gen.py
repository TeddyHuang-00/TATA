from __future__ import annotations

from pathlib import Path

import tomlkit
from pydantic import BaseModel, Field

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
- "desc": a precise, self-contained evaluation instruction that describes the
  quality levels (what a correct, a partially correct, and an incorrect
  answer look like) without tying them to specific points or deductions. It
  must be specific enough that a grader can locate the relevant part of a
  student answer and apply it. Restating the assignment's own question is fine:
  a level may require exactly what the assignment asks for, and nothing more.
  Describe each level in terms of the student's intent and
  result: the highest level must be reachable by any reasonable attempt that
  meets the requirement, even if it differs from the reference in approach,
  structure, or naming.
  The correct level must be a single condition, never a list of independent
  requirements joined by "and": an answer that meets the assignment must
  never be rated partial just because one extra conjunct is missing. Never
  require anything the assignment did not ask for: no justification,
  explanation, comparison, elimination, trace, or shown work, and no specific
  path, count, or value when the question only asks which one or whether. If a
  fully correct answer to the assignment's question could fail the correct
  wording, that wording is wrong.
  The partial level must name the concrete error or the missing answer in the
  assignment's own terms: never the absence of something the correct level
  lists, never a shortfall in explanation, documentation, formatting, or
  completeness, and never "addresses only part of the question" unless the
  assignment asks for such parts. Keep each desc to at most four sentences.
  If the assignment does not fix a value the criterion depends on -- a start or
  goal node, a dataset, a column, a scenario, an example, or a threshold --
  never assume one: a desc that quietly picks one (e.g. "the route from A to G"
  when the assignment names no endpoints) adds a requirement the assignment never made.
  Grade the student's own choice instead: correct when the answer is right for
  the value the student states or clearly implies, and partial or incorrect
  only when that choice is not permitted, when the answer contradicts the
  student's own stated choice, or when the answer is wrong for it.
  Worked example: for "What is the total cost of the cheapest route from A to
  G?", a student answers "7". Correct wording: "States the total cost is 7."
  Wrong wording: "Accumulates the per-edge costs, compares the alternative
  routes, and traces the frontier"; it demands work the question never asked
  for and would rate a correct answer partial. If the assignment had not named
  those endpoints, a criterion that still named them would invent a start and
  goal the assignment left open; correct wording grades the route the student
  names instead.
- "rating": "ternary" (correct, partial, incorrect). Always "ternary".
- "grading": "standard". Always "standard"; never "custom", "strict", or
  "round up", and never generate "custom_scale".
- "pts": a positive number of points awarded for this criterion.

Rules:
- Cover every major requirement explicitly stated in the assignment description
  with at least one criterion. Do not add criteria or finer sub-rules the
  assignment does not explicitly require, and do not nitpick details it does
  not mention.
- Read every requirement the way a student would: take the most natural and
  lenient reading of what the assignment asks, and give credit for any
  reasonable way of satisfying it. When a requirement can be read strictly or
  generously, the criterion must be written so that the generous reading still
  earns the highest level.
- When the assignment does not specify how something is done (tool, format,
  order, naming, or exact count), do not require a specific choice: accept any
  reasonable alternative.
- Do not invent specific quantitative thresholds, counts, or structural
  requirements that the assignment does not state. If the assignment describes
  a requirement qualitatively (e.g., "multiple test cases", "organized and
  readable"), keep it qualitative; assessing such a requirement must not add
  numbers or conditions the assignment never specifies. Treat vague qualifiers
  generously: "multiple" means two or more, "several" means more than one, and
  any answer that demonstrates the intended behavior satisfies the requirement.
- If the assignment description includes a rubric table (criterion names with
  point values), use those exact names and point values for the corresponding
  criteria; do not rename or reinterpret them. Only follow the table when the
  assignment actually provides one.
- Prefer fewer, broader criteria (typically 3-10) over many overlapping ones.
- All "pts" must be positive; all "name"/"desc" must be non-empty.
- pts across all criteria should sum to the assignment total (100 unless the assignment states otherwise).
- Respond only with the RubricDefinition object.
"""

#: System prompt for the parameter audit that runs before generation: name every
#: value the assignment's questions depend on that the text does not state. A
#: weak model answers this narrow factual question correctly even when it will
#: not obey the same rule while composing a rubric (the 4.08 start/goal node).
RUBRIC_GEN_PARAMETER_PROMPT = """
You are a meticulous reader of university assignment descriptions. The
questions in an assignment depend on parameters such as a start or goal node, a
dataset, a column, a scenario, an example, or a threshold. List every such
parameter that the assignment text does NOT state. A parameter counts as stated
only when you can quote the text that fixes it (including any graph, figure, or
table in the assignment); if no quote fixes its value, it is not stated.
Output one short line per unstated parameter: the parameter name, then either
the quote that mentions it without fixing it or "NOT STATED" when the text
never mentions it. List only parameters the assignment's questions depend on;
an empty list is correct when the assignment fixes every value they need.
"""


class UnstatedParameters(BaseModel):
    """Parameters the assignment's questions depend on but never fix."""

    unstated_parameters: list[str] = Field(
        default_factory=list,
        description=(
            "One short line per unstated parameter, e.g. 'Start node: NOT "
            "STATED'. Empty when the assignment fixes every value its "
            "questions need."
        ),
    )


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

    # Ask the narrow factual question first, then state its answer as fact in
    # the generation call: a weak model answers this correctly even when it
    # ignores the same rule while composing a rubric.
    audit = client.chat.completions.create(
        model=model_name,
        response_model=UnstatedParameters,
        messages=[
            {"role": "system", "content": RUBRIC_GEN_PARAMETER_PROMPT},
            {"role": "user", "content": f"Assignment Description:\n{assignment_text}"},
        ],
    )

    user_message = f"Assignment Description:\n{assignment_text}"
    if audit.unstated_parameters:
        findings = "\n".join(f"- {item}" for item in audit.unstated_parameters)
        user_message += (
            "\n\nThe assignment does NOT state the following, so students choose"
            " them themselves (grade each student's own choice, and do not assume"
            f" one):\n{findings}"
        )

    rubric = client.chat.completions.create(
        model=model_name,
        response_model=RubricDefinition,
        messages=[
            {"role": "system", "content": RUBRIC_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )

    # pydantic (via instructor) validated the structure; check the content too.
    _validate_rubric_content(rubric)
    _write_rubric(out_path, rubric)
    return rubric
