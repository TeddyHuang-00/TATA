from __future__ import annotations

from pathlib import Path

from src.aliases import load_alias_file
from src.canvas_fetch import fetch_assignment, remember_fetch


class StubAtt:
    downloads = 0

    def __init__(self, filename: str, updated_at: str = "2026-01-01T00:00:00Z") -> None:
        self.filename = filename
        self.updated_at = updated_at

    def download(self, dest: Path) -> None:
        StubAtt.downloads += 1
        dest.write_bytes(b"content")


class StubSub:
    def __init__(
        self,
        user_id: int,
        name: str = "Doe, Jane",
        sortable_name: str = "Doe, Jane",
        late: bool = False,
        attachments: list[StubAtt] | None = None,
        body: str = "",
    ) -> None:
        self.user_id = user_id
        self.user = {"name": name, "sortable_name": sortable_name}
        self.late = late
        self.attachments = attachments or []
        self.body = body


class StubAssignment:
    def __init__(self, subs: list[StubSub]) -> None:
        self._subs = subs

    def get_submissions(self, include: list[str]) -> list[StubSub]:
        return self._subs


class StubCourse:
    def __init__(self, assignment: StubAssignment) -> None:
        self._assignment = assignment

    def get_assignment(self, assignment_id: int) -> StubAssignment:
        return self._assignment


class StubCanvas:
    def __init__(self, assignment: StubAssignment) -> None:
        self._assignment = assignment

    def get_course(self, course_id: int) -> StubCourse:
        return StubCourse(self._assignment)


def _student_aliases(out: Path) -> dict[str, str]:
    return load_alias_file(out.parent / "alias.toml").get("student", {})


def test_attach_mode_naming_and_cache(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            attachments=[StubAtt("nb.ipynb")],
        ),
        StubSub(
            200,
            name="Beta, B",
            sortable_name="Beta, B",
            late=True,
            attachments=[StubAtt("nb.ipynb")],
        ),
        StubSub(
            300,
            name="Gamma, G",
            sortable_name="Gamma, G",
            attachments=[
                StubAtt("one.ipynb"),
                StubAtt("two.docx", "2026-01-02T00:00:00Z"),
            ],
        ),
        StubSub(400, name="Empty, E", sortable_name="Empty, E"),
    ]
    canvas = StubCanvas(StubAssignment(subs))

    fetch_assignment(canvas, 1, 2, out, mode="auto")  # auto -> attach

    assert (out / "100.ipynb").exists()
    assert (out / "200_LATE_0.ipynb").exists()
    assert (out / "300.ipynb").exists()
    assert (out / "300_1.docx").exists()
    assert (out / ".fetch-cache.json").exists()
    aliases = _student_aliases(out)
    assert aliases == {
        "100": "Alpha, A",
        "200": "Beta, B",
        "300": "Gamma, G",
        "400": "Empty, E",
    }
    assert not (out.parent / "roster.csv").exists()

    # Second run: unchanged attachments are not re-downloaded.
    StubAtt.downloads = 0
    fetch_assignment(canvas, 1, 2, out, mode="attach")
    assert StubAtt.downloads == 0

    # Changed timestamp triggers re-download.
    subs[2].attachments[0].updated_at = "2026-02-02T00:00:00Z"
    fetch_assignment(canvas, 1, 2, out, mode="attach")
    assert StubAtt.downloads == 1


def test_text_mode_and_auto_detection(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    subs = [
        StubSub(100, name="Alpha, A", sortable_name="Alpha, A", body="my answer"),
        StubSub(
            200, name="Beta, B", sortable_name="Beta, B", late=True, body="late answer"
        ),
        StubSub(300, name="Empty, E", sortable_name="Empty, E", body=""),
    ]
    canvas = StubCanvas(StubAssignment(subs))

    fetch_assignment(canvas, 1, 2, out, mode="auto")  # auto -> text (no attachments)

    assert (out / "100.txt").read_text() == "my answer"
    assert (out / "200_LATE_0.txt").read_text() == "late answer"
    assert not (out / "300.txt").exists()
    assert set(_student_aliases(out)) == {"100", "200", "300"}
    assert not (out / ".fetch-cache.json").exists()


def test_rows_sorted_by_sortable_name(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    subs = [
        StubSub(300, name="Zed, Z", sortable_name="Zed, Z"),
        StubSub(100, name="Alpha, A", sortable_name="Alpha, A"),
    ]
    rows = fetch_assignment(StubCanvas(StubAssignment(subs)), 1, 2, out)
    assert [r["user_id"] for r in rows] == [100, 300]


def test_alias_name_fallbacks(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    subs = [
        StubSub(100, name="Name, F", sortable_name=""),
        StubSub(200, name="?", sortable_name="?"),
    ]
    fetch_assignment(StubCanvas(StubAssignment(subs)), 1, 2, out)
    aliases = _student_aliases(out)
    # empty sortable -> user_name; missing user name -> user_id
    assert aliases["100"] == "Name, F"
    assert aliases["200"] == "200"


def test_upsert_preserves_manual_overrides_and_tables(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    alias_path = out.parent / "alias.toml"
    alias_path.write_text(
        '# manual edits\n[course]\n"1" = "Course One"\n'
        '[student]\n"100" = "Manual, Override"\n'
        'unknown_key = "kept"\n',
        encoding="utf-8",
    )
    subs = [
        StubSub(100, name="Alpha, A", sortable_name="Alpha, A"),
        StubSub(200, name="Beta, B", sortable_name="Beta, B"),
    ]
    fetch_assignment(StubCanvas(StubAssignment(subs)), 1, 2, out)
    aliases = _student_aliases(out)
    assert aliases["100"] == "Manual, Override"  # manual wins
    assert aliases["200"] == "Beta, B"  # new key filled
    text = alias_path.read_text()
    assert '[course]\n"1" = "Course One"' in text
    assert "unknown_key = " in text
    assert "Manual, Override" in text


def test_remember_fetch_append_and_replace(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[grading]\nrubric = "x.toml"\n')
    remember_fetch(cfg, course_id=1, assignment_id=2)
    text = cfg.read_text()
    assert "[fetch]" in text
    assert "course_id = 1" in text
    # Defaults are omitted: mode=auto, out_dir=raw.
    assert "mode =" not in text
    assert "out_dir" not in text

    remember_fetch(cfg, course_id=3, assignment_id=4, out_dir="data", mode="text")
    text = cfg.read_text()
    assert 'mode = "text"' in text
    assert 'out_dir = "data"' in text
    assert text.count("[fetch]") == 1
    assert "[grading]" in text  # other sections untouched


def test_remember_fetch_creates_missing_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    remember_fetch(cfg, assignment_id=42)
    text = cfg.read_text()
    assert "[fetch]" in text
    assert "assignment_id = 42" in text
    assert "course_id" not in text
