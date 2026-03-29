#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, TypedDict, override
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, model_validator

try:
    from src.cli_options import CliOptions, parse_cli_args
except ModuleNotFoundError:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from src.cli_options import CliOptions, parse_cli_args

MAX_PORT = 65535
HTML_PAGE_FILE = Path(__file__).with_name("score_review_web.html")


class CriterionRow(TypedDict):
    criterion: str
    rating: str
    comment: str


class StudentPayload(TypedDict):
    student: str
    file_name: str
    criteria: list[CriterionRow]
    json: dict[str, object]


class ScoreReviewWebCliOptions(CliOptions):
    score_dir: Path = Field(
        validation_alias=AliasChoices("score-dir", "dir", "d"),
        description="Directory containing per-student JSON scoring/grading outputs.",
    )
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("host", "bind"),
        description="Host interface to bind the local server.",
    )
    port: int = Field(
        default=8765,
        validation_alias=AliasChoices("port", "p"),
        description="Port for the local web server.",
    )

    @model_validator(mode="after")
    def _validate_options(self) -> ScoreReviewWebCliOptions:
        score_dir = self.score_dir.resolve()
        if not score_dir.exists() or not score_dir.is_dir():
            msg = f"--score-dir not found or not a directory: {score_dir}"
            raise ValueError(msg)
        self.score_dir = score_dir

        if not 1 <= self.port <= MAX_PORT:
            msg = f"--port must be within 1..{MAX_PORT}"
            raise ValueError(msg)

        return self


def _extract_criterion_feedback(
    payload: object, prefix: str = ""
) -> list[CriterionRow]:
    rows: list[CriterionRow] = []

    if isinstance(payload, dict):
        feedback = payload.get("feedback")
        rating = payload.get("rating")
        if isinstance(feedback, str) and feedback.strip():
            rows.append({
                "criterion": prefix or "criterion",
                "rating": str(rating) if rating is not None else "",
                "comment": feedback.strip(),
            })

        for key, value in payload.items():
            if key == "feedback":
                continue
            key_name = str(key)
            next_prefix = f"{prefix}.{key_name}" if prefix else key_name
            rows.extend(_extract_criterion_feedback(value, next_prefix))

    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            next_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            rows.extend(_extract_criterion_feedback(value, next_prefix))

    return rows


def _load_students(score_dir: Path) -> list[StudentPayload]:
    files = sorted(score_dir.glob("*.json"), key=lambda file: file.name.lower())
    students: list[StudentPayload] = []

    for file in files:
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
        except Exception:
            continue

        students.append({
            "student": file.stem,
            "file_name": file.name,
            "criteria": _extract_criterion_feedback(payload),
            "json": payload,
        })

    return students


def _collect_rating_types(students: list[StudentPayload]) -> list[str]:
    ratings: set[str] = set()
    has_empty = False

    for student in students:
        for criterion in student["criteria"]:
            rating = criterion["rating"].strip()
            if rating:
                ratings.add(rating)
            else:
                has_empty = True

    all_ratings = sorted(ratings, key=lambda value: value.lower())
    if has_empty:
        all_ratings.append("(empty)")
    return all_ratings


def _load_html_page() -> str:
    return HTML_PAGE_FILE.read_text(encoding="utf-8")


class _Handler(BaseHTTPRequestHandler):
    html_page: ClassVar[str] = ""
    rating_types: ClassVar[list[str]] = []
    students: ClassVar[list[StudentPayload]] = []

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = self.html_page.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/students":
            body = json.dumps(self.students, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/ratings":
            body = json.dumps(self.rating_types, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    @override
    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    args = parse_cli_args(ScoreReviewWebCliOptions)
    students = _load_students(args.score_dir)
    rating_types = _collect_rating_types(students)
    html_page = _load_html_page()

    _Handler.html_page = html_page
    _Handler.rating_types = rating_types
    _Handler.students = students
    server = ThreadingHTTPServer((args.host, args.port), _Handler)

    print(f"Loaded {len(students)} student JSON files from: {args.score_dir}")
    print(f"Discovered {len(rating_types)} rating types")
    print(f"Open in browser: http://{args.host}:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Server stopped.")


if __name__ == "__main__":
    main()
