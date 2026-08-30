from __future__ import annotations

import json
import tomllib
from pathlib import Path

from src.aliases import load_alias_file
from src.canvas_fetch import fetch_assignment, remember_course_fetch
from src.processing import preprocess_assignment


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


def test_attachments_layout_and_cache(tmp_path: Path) -> None:
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

    fetch_assignment(canvas, 1, 2, out)

    # Single attachment -> flat; two attachments -> <uid>/ folder.
    assert (out / "100.ipynb").exists()
    assert (out / "200_LATE_0.ipynb").exists()
    assert (out / "300" / "300.ipynb").exists()
    assert (out / "300" / "300_1.docx").exists()
    assert not (out / "300.ipynb").exists()
    assert (out / ".fetch-cache.json").exists()
    aliases = _student_aliases(out)
    assert aliases == {
        "100": "Alpha, A",
        "200": "Beta, B",
        "300": "Gamma, G",
        "400": "Empty, E",
    }
    assert not (out.parent / "roster.csv").exists()

    # Second run: unchanged attachments are not re-downloaded (cache keys are
    # the plain filenames, so the folder layout does not matter).
    StubAtt.downloads = 0
    fetch_assignment(canvas, 1, 2, out)
    assert StubAtt.downloads == 0

    # Changed timestamp triggers re-download.
    subs[2].attachments[0].updated_at = "2026-02-02T00:00:00Z"
    fetch_assignment(canvas, 1, 2, out)
    assert StubAtt.downloads == 1


def test_body_flat_and_cached(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    subs = [
        StubSub(100, name="Alpha, A", sortable_name="Alpha, A", body="my answer"),
        StubSub(
            200, name="Beta, B", sortable_name="Beta, B", late=True, body="late answer"
        ),
        StubSub(300, name="Empty, E", sortable_name="Empty, E", body=""),
    ]
    canvas = StubCanvas(StubAssignment(subs))

    fetch_assignment(canvas, 1, 2, out)

    assert (out / "100.html").read_text() == "my answer"
    assert (out / "200_LATE_0.html").read_text() == "late answer"
    assert not (out / "300.html").exists()
    assert (out / ".fetch-cache.json").exists()
    assert set(_student_aliases(out)) == {"100", "200", "300"}

    # Body files are cached by (filename, updated_at) too: an unchanged item
    # is not rewritten (tampered content survives the second run).
    (out / "100.html").write_text("tampered", encoding="utf-8")
    fetch_assignment(canvas, 1, 2, out)
    assert (out / "100.html").read_text() == "tampered"


def test_body_plus_attachment_goes_to_folder(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            body="my answer",
            attachments=[StubAtt("solution.ipynb")],
        ),
    ]
    rows = fetch_assignment(StubCanvas(StubAssignment(subs)), 1, 2, out)

    assert (out / "100" / "100.html").read_text() == "my answer"
    assert (out / "100" / "100_0.ipynb").exists()
    assert rows[0]["file"] == "100.html"  # first file of the student


def test_html_attachment_and_body_no_name_collision(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            body="my answer",
            attachments=[StubAtt("page.html")],
        ),
    ]
    fetch_assignment(StubCanvas(StubAssignment(subs)), 1, 2, out)

    assert (out / "100" / "100.html").read_text() == "my answer"
    # Attachment at i=0 with a body present is forced to _0 (no name clash).
    assert (out / "100" / "100_0.html").exists()
    assert not (out / "100.html").exists()


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


def test_remember_course_fetch_appends_entry_and_dedupes(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[grading]\nrubric = "x.toml"\n')
    remember_course_fetch(cfg, course_id=1, assignment_id=2)
    text = cfg.read_text()
    assert "[fetch]" in text
    assert "course_id = 1" in text
    assert "mode" not in text  # never writes [fetch].mode
    assert "assignment_id" not in text
    assert tomllib.loads(text)["fetch"]["assignments"] == [{"id": 2}]

    # course_id replaced; the same assignment id is NOT appended twice and
    # an existing entry stays untouched.
    remember_course_fetch(cfg, course_id=3, assignment_id=2)
    text = cfg.read_text()
    data = tomllib.loads(text)
    assert data["fetch"]["course_id"] == 3
    assert [e["id"] for e in data["fetch"]["assignments"]] == [2]
    assert "[grading]" in text  # other sections untouched

    # A new id appends.
    remember_course_fetch(cfg, assignment_id=4)
    data = tomllib.loads(cfg.read_text())
    assert [e["id"] for e in data["fetch"]["assignments"]] == [2, 4]


def test_remember_course_fetch_dedupes_legacy_assignment_id(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[fetch]\ncourse_id = 9\n\n[[fetch.assignments]]\nassignment_id = 5\n",
        encoding="utf-8",
    )
    remember_course_fetch(cfg, course_id=1, assignment_id=5)
    data = tomllib.loads(cfg.read_text())
    # Legacy key counts as the same id: no duplicate entry appended.
    assert len(data["fetch"]["assignments"]) == 1
    assert data["fetch"]["assignments"][0]["assignment_id"] == 5
    assert data["fetch"]["course_id"] == 1


def test_remember_course_fetch_preserves_existing_mode_key(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[fetch]\ncourse_id = 9\nmode = "text"\n\n[[fetch.assignments]]\nid = 1\n',
        encoding="utf-8",
    )
    remember_course_fetch(cfg, course_id=1, assignment_id=2)
    data = tomllib.loads(cfg.read_text())
    assert data["fetch"]["mode"] == "text"  # pre-existing key untouched
    assert [e["id"] for e in data["fetch"]["assignments"]] == [1, 2]


def test_remember_course_fetch_creates_missing_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    remember_course_fetch(cfg, course_id=42)
    text = cfg.read_text()
    assert "[fetch]" in text
    assert "course_id = 42" in text
    assert "assignments" not in text  # no entry given -> no list created


def test_fetch_folderize_transition_preserves_cache(tmp_path: Path) -> None:
    """Regression: when a file moves into a per-student folder, the obsolete
    flat copy is deleted but the plain-name cache key (re-stamped by the
    folder copy) must survive — the next fetch must download nothing and
    preprocess still sees the real submitted stamp."""
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            attachments=[StubAtt("a.docx")],
        ),
    ]
    canvas = StubCanvas(StubAssignment(subs))
    # Run 1: single file -> flat.
    StubAtt.downloads = 0
    fetch_assignment(canvas, 1, 2, out)
    assert StubAtt.downloads == 1
    assert (out / "100.docx").exists()

    # Run 2: same student now submits two files -> folderized; the stale
    # flat copy goes away, the cache keys stay.
    subs[0].attachments = [StubAtt("a.docx"), StubAtt("b.docx")]
    fetch_assignment(canvas, 1, 2, out)
    assert not (out / "100.docx").exists()
    assert (out / "100" / "100.docx").exists()
    assert (out / "100" / "100_1.docx").exists()

    # Run 3: unchanged -> cache hits, nothing re-downloaded; cache still
    # holds the folder file names.
    StubAtt.downloads = 0
    fetch_assignment(canvas, 1, 2, out)
    assert StubAtt.downloads == 0
    cache = json.loads((out / ".fetch-cache.json").read_text())
    assert "100.docx" in cache
    assert "100_1.docx" in cache


def test_fetch_prunes_folder_on_shrink_to_flat(tmp_path: Path) -> None:
    """Regression (reverse transition): a student whose file set shrinks to
    exactly one file is fetched flat — the stale per-student folder tree
    (and its cache keys for names not produced this run) must be removed,
    or the R4 stale-flat guard would skip the new flat file and preprocess
    would keep using the old folder contents."""
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            attachments=[StubAtt("a.html"), StubAtt("b.html")],
        ),
    ]
    canvas = StubCanvas(StubAssignment(subs))
    fetch_assignment(canvas, 1, 2, out)
    assert (out / "100" / "100.html").exists()
    assert (out / "100" / "100_1.html").exists()

    subs[0].attachments = [StubAtt("a.html")]
    fetch_assignment(canvas, 1, 2, out)

    assert (out / "100.html").exists()
    assert not (out / "100").exists()
    cache = json.loads((out / ".fetch-cache.json").read_text())
    assert cache.get("100.html")  # produced flat -> key kept
    assert "100_1.html" not in cache  # folder-only name -> key dropped

    # Preprocess then produces <uid>.md from the flat file.
    (tmp_path / "config.toml").write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\nprovider = "deepseek"\n',
        encoding="utf-8",
    )
    result = preprocess_assignment(tmp_path / "config.toml")
    md = tmp_path / "processed" / "100.md"
    assert md.exists()
    assert result is not None
    assert result["success"] == 1
    assert "content" in md.read_text(encoding="utf-8")


def test_fetch_drops_cache_for_deleted_flat_names(tmp_path: Path) -> None:
    """A flat name no longer produced anywhere this run is truly deleted:
    its file and cache entry are removed, and only the current output's
    entry survives."""
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            attachments=[StubAtt("a.docx")],
        ),
    ]
    canvas = StubCanvas(StubAssignment(subs))
    fetch_assignment(canvas, 1, 2, out)
    assert (out / "100.docx").exists()

    # The student now submits a pdf instead: the docx is gone everywhere.
    subs[0].attachments = [StubAtt("a.pdf")]
    fetch_assignment(canvas, 1, 2, out)
    assert (out / "100.pdf").exists()
    assert not (out / "100.docx").exists()
    cache = json.loads((out / ".fetch-cache.json").read_text())
    assert "100.pdf" in cache
    assert "100.docx" not in cache


def test_fetch_prunes_stale_folder_members_on_rename(tmp_path: Path) -> None:
    """Folder -> folder rename: a stale in-folder member (name no longer
    produced this run) is unlinked and its cache key dropped, while the
    folder and its current members survive."""
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            body="first",
            attachments=[StubAtt("a.docx", "2026-01-01T00:00:00Z")],
        ),
    ]
    canvas = StubCanvas(StubAssignment(subs))
    fetch_assignment(canvas, 1, 2, out)
    assert (out / "100" / "100.html").exists()
    assert (out / "100" / "100_0.docx").exists()

    # Same student: the docx attachment is replaced by an ipynb (name
    # becomes 100_0.ipynb); the stale 100_0.docx must be pruned.
    subs[0].attachments = [StubAtt("b.ipynb", "2026-02-02T00:00:00Z")]
    fetch_assignment(canvas, 1, 2, out)

    assert (out / "100").is_dir()
    assert (out / "100" / "100.html").exists()
    assert (out / "100" / "100_0.ipynb").exists()
    assert not (out / "100" / "100_0.docx").exists()
    cache = json.loads((out / ".fetch-cache.json").read_text())
    assert "100.html" in cache  # produced name -> key kept
    assert "100_0.ipynb" in cache
    assert "100_0.docx" not in cache


def test_fetch_removes_folder_for_unsubmitted_student(tmp_path: Path) -> None:
    """2 -> 0 unsubmit: a student who submitted files in an earlier run and
    submits nothing now leaves the whole folder removed and its cache keys
    (for names not produced this run) dropped."""
    out = tmp_path / "raw"
    subs = [
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            body="first",
            attachments=[StubAtt("a.docx", "2026-01-01T00:00:00Z")],
        ),
    ]
    canvas = StubCanvas(StubAssignment(subs))
    fetch_assignment(canvas, 1, 2, out)
    assert (out / "100" / "100.html").exists()
    assert (out / "100" / "100_0.docx").exists()

    # Empty submission (body empty, no attachments): stale folder goes away.
    subs[0].body = ""
    subs[0].attachments = []
    fetch_assignment(canvas, 1, 2, out)

    assert not (out / "100").exists()
    cache = json.loads((out / ".fetch-cache.json").read_text())
    assert "100.html" not in cache
    assert "100_0.docx" not in cache


def test_fetch_same_uid_flat_and_folder_keeps_folder(tmp_path: Path) -> None:
    """A uid appearing in BOTH layouts this run (duplicate submission
    entries): the folder is canonical — the produced dir is never rmtree'd
    (no same-run delete) and the flat copies of that uid are dropped."""
    out = tmp_path / "raw"
    subs = [
        StubSub(100, name="Alpha, A", sortable_name="Alpha, A", body="flat answer"),
        StubSub(
            100,
            name="Alpha, A",
            sortable_name="Alpha, A",
            body="folder answer",
            attachments=[StubAtt("a.docx", "2026-01-01T00:00:00Z")],
        ),
    ]
    canvas = StubCanvas(StubAssignment(subs))
    fetch_assignment(canvas, 1, 2, out)

    assert (out / "100").is_dir()  # folder written this run survives
    assert (out / "100" / "100.html").exists()
    assert (out / "100" / "100_0.docx").exists()
    assert not (out / "100.html").exists()  # flat copy of the same uid removed
    cache = json.loads((out / ".fetch-cache.json").read_text())
    assert "100.html" in cache  # the folder copy carries the name
    assert "100_0.docx" in cache
