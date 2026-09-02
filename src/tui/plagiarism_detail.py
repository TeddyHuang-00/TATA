"""Full-screen plagiarism detail views (S4 feedback 3).

Pushed by ``PlagiarismScreen`` on DataTable row selection: aggregate pair
detail, assignment stats (similarity histogram), student summary (Tree with
per-assignment similarities), and single-pair detail (z-score, highlighted
histogram, embedded compare).  Every screen wears a ``Plagiarism / view /
item`` breadcrumb and pops with [esc] (``push_screen`` pattern from
:class:`src.tui.score_review.ScoreReviewScreen`).  All UI copy is English.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Static, Tree

from src.shared.aliases import assignment_display_name, course_student_display_name
from src.tui.plagiarism import _overlap_display, compare_content, pair_side_name
from src.tui.scan import AssignmentInfo, _pair_pct
from src.tui.score_review import base_uid

if TYPE_CHECKING:
    from src.tui.app import AppState
    from src.tui.scan import CourseInfo

PAGE_ROWS = 20
MIN_SIMS_FOR_Z = 2
_UID_PAREN_RE = re.compile(r"\(([^()]+)\)$")


# ---------- pure helpers ----------


def parse_uid(label: str) -> str:
    """Aggregate student label -> uid: 'Mia(415019)' -> '415019', else label."""
    match = _UID_PAREN_RE.search(str(label))
    return match.group(1) if match else str(label)


def pair_uids(pair: dict) -> frozenset[str]:
    """Base uids of a pair's two sides (test/reference stems, suffix-stripped)."""
    return frozenset({
        base_uid(Path(str(pair.get("test_file") or "")).stem),
        base_uid(Path(str(pair.get("reference_file") or "")).stem),
    })


def find_pair_for_uids(pairs: list[dict], uids: frozenset[str]) -> dict | None:
    """First pair whose sides are exactly ``uids`` (order-insensitive)."""
    return next((pair for pair in pairs if pair_uids(pair) == uids), None)


def render_histogram(
    values: list[float], highlight: float | None = None, width: int = 48
) -> list[str]:
    """ASCII 10%-bin histogram (0..100); the bin holding ``highlight`` is
    marked with a diamond and a ``← A-B`` suffix (yellow markup)."""
    bins = [0.0] * 10
    for value in values:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            pct = 0.0
        bins[min(9, max(0, int(pct // 10)))] += 1
    peak = max(bins) if bins else 0.0
    lines: list[str] = []
    for index, count in enumerate(bins):
        bar = "█" * (round(count / peak * width) if peak else 0)
        label = f"{index * 10}-{(index + 1) * 10}%".ljust(6)
        line = f"{label} {bar} {int(count)}"
        if (
            highlight is not None
            and min(9, max(0, int(float(highlight) // 10))) == index
        ):
            line = f"[yellow]◆ {line}  ← A-B[/yellow]"
        lines.append(line)
    return lines


@dataclass
class PlagiarismDocs:
    """Snapshot of course-scoped plagiarism data handed to detail screens.

    Built once from the plagiarism workspace's already-loaded lists; detail
    screens never re-read JSON (the pushing screen owns freshness).
    """

    state: AppState
    course: CourseInfo
    pairs_by_assignment: dict[str, list[dict]]
    aggregate_rows: list[dict]
    threshold_pct: float

    @property
    def assignments_dir(self) -> Path:
        return self.state.assignments_dir

    @property
    def course_dir_name(self) -> str:
        return self.course.dir_name

    def assignment_pairs(self, info: AssignmentInfo) -> list[dict]:
        return self.pairs_by_assignment.get(info.dir_name, [])

    def assignment_name(self, info: AssignmentInfo) -> str:
        return assignment_display_name(
            self.assignments_dir,
            self.course_dir_name,
            info.dir_name,
            info.assignment_id,
        )


def shared_pairs(
    docs: PlagiarismDocs, uid_a: str, uid_b: str
) -> list[tuple[AssignmentInfo, dict]]:
    """(assignment, pair) rows where both students appear, in assignment order."""
    uids = frozenset({uid_a, uid_b})
    found: list[tuple[AssignmentInfo, dict]] = []
    for info in docs.state.assignments:
        pair = find_pair_for_uids(docs.assignment_pairs(info), uids)
        if pair is not None:
            found.append((info, pair))
    return found


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------- shared screen shell ----------


class _DetailScreen(Screen):
    """Escapable full-screen detail: breadcrumb line + escape-pop shell."""

    BINDINGS: ClassVar = [Binding("escape", "close", "Back")]

    def __init__(self, docs: PlagiarismDocs, breadcrumb: str) -> None:
        super().__init__()
        self.docs = docs
        self._breadcrumb = breadcrumb

    def compose(self) -> ComposeResult:
        yield Static(
            f"Plagiarism / {self._breadcrumb}  [dim](esc: back)[/dim]",
            id="detail-breadcrumb",
            markup=True,
        )

    def action_close(self) -> None:
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()


# ---------- aggregate pair detail ----------


class AggregatePairDetailScreen(_DetailScreen):
    """One aggregate pair: banner (z/p/raw sim) + shared-assignment rows."""

    def __init__(
        self,
        docs: PlagiarismDocs,
        uid_a: str,
        uid_b: str,
        aggregate_row: dict | None,
    ) -> None:
        self.uid_a = uid_a
        self.uid_b = uid_b
        self.aggregate_row = aggregate_row
        a_label = str((aggregate_row or {}).get("student_a") or uid_a)
        b_label = str((aggregate_row or {}).get("student_b") or uid_b)
        name_a = course_student_display_name(
            docs.assignments_dir, docs.course_dir_name, a_label
        )
        name_b = course_student_display_name(
            docs.assignments_dir, docs.course_dir_name, b_label
        )
        self._names = (name_a, name_b)
        self._shared_rows = shared_pairs(docs, uid_a, uid_b)
        stats = ""
        if aggregate_row is not None:
            z = float(aggregate_row.get("z_score") or 0.0)
            p = float(aggregate_row.get("one_sided_p_value") or 1.0)
            sim = float(aggregate_row.get("raw_similarity_pct") or 0.0)
            stats = f"    aggregate z {z:.2f} · p {p:.3g} · raw sim {sim:.1f}%"
        else:
            stats = "    from student summary"
        self._banner = f"[b]{name_a} ↔ {name_b}[/b]{stats}"
        super().__init__(docs, f"Aggregate / [b]{name_a} ↔ {name_b}[/b]")

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(self._banner, id="detail-banner", markup=True)
        yield Static("Shared assignments", id="detail-section", markup=True)
        yield DataTable(id="detail-shared-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="detail-empty", markup=True)

    def on_mount(self) -> None:
        table = self.query_one("#detail-shared-table", DataTable)
        empty = self.query_one("#detail-empty", Static)
        if not self._shared_rows:
            self._show_empty(
                table, empty, "No shared assignments between these students."
            )
            return
        table.add_columns("Assignment", "sim %", "overlap")
        for index, (info, pair) in enumerate(self._shared_rows):
            table.add_row(
                self.docs.assignment_name(info),
                f"{_pair_pct(pair):.1f}",
                _overlap_display(pair),
                key=str(index),
            )
        table.styles.height = "1fr"
        empty.display = False
        table.focus()

    @staticmethod
    def _show_empty(table: DataTable, empty: Static, message: str) -> None:
        empty.update(message)
        empty.styles.height = "1fr"
        empty.styles.content_align = ("center", "middle")
        empty.styles.opacity = 0.6
        empty.display = True
        table.display = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        index = event.cursor_row
        if not (0 <= index < len(self._shared_rows)):
            return
        info, pair = self._shared_rows[index]
        self.app.push_screen(AssignmentPairDetailScreen(self.docs, info, pair))


# ---------- assignment stats detail ----------


class AssignmentDetailScreen(_DetailScreen):
    """One assignment: banner stats + similarity histogram + ranked pairs."""

    def __init__(self, docs: PlagiarismDocs, assignment: AssignmentInfo) -> None:
        self.assignment = assignment
        pairs = docs.assignment_pairs(assignment)
        self._rows = sorted(
            pairs,
            key=lambda p: (
                _pair_pct(p) < docs.threshold_pct,
                -_pair_pct(p),
                str(p.get("test_file") or ""),
            ),
        )[:PAGE_ROWS]
        sims = [_pair_pct(p) for p in pairs]
        flagged = sum(1 for sim in sims if sim >= docs.threshold_pct)
        max_sim = max(sims, default=0.0)
        self._histogram = f"Similarity distribution (n={len(sims)})\n" + "\n".join(
            render_histogram(sims)
        )
        self._banner = (
            f"[b]{docs.assignment_name(assignment)}[/b]"
            f"    pairs {len(sims)} · flagged {flagged} · max sim {max_sim:.1f}%"
            f"  [dim](display threshold {docs.threshold_pct:.0f}%)[/dim]"
        )
        super().__init__(
            docs, f"Assignments / [b]{docs.assignment_name(assignment)}[/b]"
        )

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(self._banner, id="detail-banner", markup=True)
        yield Static(self._histogram, id="detail-histogram", markup=True)
        yield DataTable(id="detail-pairs-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="detail-empty", markup=True)

    def on_mount(self) -> None:
        table = self.query_one("#detail-pairs-table", DataTable)
        empty = self.query_one("#detail-empty", Static)
        if not self._rows:
            AggregatePairDetailScreen._show_empty(
                table, empty, "No pairs for this assignment."
            )
            return
        table.add_columns("Student A", "Student B", "sim %", "overlap", "Flag")
        for index, pair in enumerate(self._rows):
            sim = _pair_pct(pair)
            table.add_row(
                pair_side_name(
                    self.docs.assignments_dir,
                    self.docs.course_dir_name,
                    self.assignment,
                    str(pair.get("test_file")),
                ),
                pair_side_name(
                    self.docs.assignments_dir,
                    self.docs.course_dir_name,
                    self.assignment,
                    str(pair.get("reference_file")),
                ),
                f"{sim:.1f}",
                _overlap_display(pair),
                "FLAG" if sim >= self.docs.threshold_pct else "-",
                key=str(index),
            )
        table.styles.height = "1fr"
        empty.display = False
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        index = event.cursor_row
        if not (0 <= index < len(self._rows)):
            return
        self.app.push_screen(
            AssignmentPairDetailScreen(self.docs, self.assignment, self._rows[index])
        )


# ---------- student summary detail ----------


class StudentDetailScreen(_DetailScreen):
    """One student: aggregated-position banner + Tree of correlated peers."""

    def __init__(self, docs: PlagiarismDocs, uid: str) -> None:
        self.uid = uid
        name = course_student_display_name(
            docs.assignments_dir, docs.course_dir_name, uid
        )
        self._student_name = name
        self._peers: list[tuple[str, dict | None, float, float, list]] = []
        # (other uid, aggregate row or None, rank z, mean sim, [(assignment, pair)])
        self._fallback_note = ""
        if docs.aggregate_rows:
            for row in docs.aggregate_rows:
                a_uid = parse_uid(str(row.get("student_a") or ""))
                b_uid = parse_uid(str(row.get("student_b") or ""))
                if uid not in {a_uid, b_uid}:
                    continue
                other = b_uid if uid == a_uid else a_uid
                shared = shared_pairs(docs, uid, other)
                if not shared:
                    continue
                mean_sim = _mean([_pair_pct(p) for _info, p in shared])
                z = float(row.get("z_score") or 0.0)
                self._peers.append((other, row, z, mean_sim, shared))
            # rank by aggregate score (z desc), tie by mean sim desc
            self._peers.sort(key=lambda item: (-item[2], -item[3]))
        else:
            self._fallback_note = (
                "[dim]aggregate report not run — per-assignment similarities only[/dim]"
            )
            per_peer: dict[str, list[float]] = {}
            for info in docs.state.assignments:
                for pair in docs.assignment_pairs(info):
                    uids = pair_uids(pair)
                    if uid not in uids:
                        continue
                    other = next((x for x in uids if x != uid), None)
                    if other is None:
                        continue
                    per_peer.setdefault(other, []).append(_pair_pct(pair))
            for other, sims in per_peer.items():
                shared = shared_pairs(docs, uid, other)
                self._peers.append((other, None, 0.0, _mean(sims), shared))
            self._peers.sort(key=lambda item: -item[3])
        self._banner = f"[b]{name}[/b]"
        super().__init__(docs, f"Students / [b]{name}[/b]")

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(self._banner, id="detail-banner", markup=True)
        yield Static(
            "Most correlated students (desc; expand a peer for per-assignment similarities)",
            id="detail-section",
            markup=True,
        )
        yield Tree(self._student_name, id="detail-tree")

    def on_mount(self) -> None:
        tree = self.query_one("#detail-tree", Tree)
        note = self._fallback_note or (
            "[dim]no correlated students found in the aggregate report[/dim]"
        )
        if note:
            self.query_one("#detail-banner", Static).update(
                self._banner + "    " + note
            )
        if not self._peers:
            tree.root.add("[dim]No correlated students found.[/dim]", data=("none",))
            tree.root.expand()
            tree.focus()
            return
        for other, row, _rank, _mean_sim, shared in self._peers:
            other_name = course_student_display_name(
                self.docs.assignments_dir, self.docs.course_dir_name, other
            )
            if row is not None:
                z = float(row.get("z_score") or 0.0)
                mean_sim = _mean([_pair_pct(p) for _info, p in shared])
                label = f"{other_name} (z {z:.2f} / mean sim {mean_sim:.1f}%)"
            else:
                mean_sim = _mean([_pair_pct(p) for _info, p in shared])
                label = f"{other_name} (mean sim {mean_sim:.1f}%)"
            peer = tree.root.add(label, data=("peer", other, row))
            for info, pair in shared:
                peer.add(
                    f"{self.docs.assignment_name(info)} · sim {_pair_pct(pair):.1f}%",
                    data=("assign", info, pair),
                )
        tree.root.expand()
        tree.focus()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if data is None:
            return
        if data[0] == "peer":
            self.app.push_screen(
                AggregatePairDetailScreen(self.docs, self.uid, data[1], data[2])
            )
        elif data[0] == "assign":
            self.app.push_screen(
                AssignmentPairDetailScreen(self.docs, data[1], data[2])
            )


# ---------- single pair detail ----------


class AssignmentPairDetailScreen(_DetailScreen):
    """One pair of one assignment: z-score, highlighted histogram, compare."""

    def __init__(
        self, docs: PlagiarismDocs, assignment: AssignmentInfo, pair: dict
    ) -> None:
        self.assignment = assignment
        self.pair = pair
        sim = _pair_pct(pair)
        assignment_pairs = docs.assignment_pairs(assignment)
        sims = [_pair_pct(p) for p in assignment_pairs]
        # ponytail: raw-sim z over this assignment's pairs (the aggregate z
        # from aggregate.json stays the cross-assignment authority)
        z_text = "z —"
        if len(sims) >= MIN_SIMS_FOR_Z:
            mean_s = _mean(sims)
            std = (sum((s - mean_s) ** 2 for s in sims) / len(sims)) ** 0.5
            if std > 0:
                z_text = f"z {((sim - mean_s) / std):.2f}"
        flag_text = (
            f"[red]FLAG[/red] — at/above display threshold {docs.threshold_pct:.0f}%"
            if sim >= docs.threshold_pct
            else f"below display threshold {docs.threshold_pct:.0f}%"
        )
        name_a = pair_side_name(
            docs.assignments_dir,
            docs.course_dir_name,
            assignment,
            str(pair.get("test_file")),
        )
        name_b = pair_side_name(
            docs.assignments_dir,
            docs.course_dir_name,
            assignment,
            str(pair.get("reference_file")),
        )
        self._histogram = (
            f"Similarity distribution (n={len(sims)}, ← pair marked)\n"
            + "\n".join(render_histogram(sims, highlight=sim))
        )
        self._banner = (
            f"[b]{docs.assignment_name(assignment)}[/b] · "
            f"[b]{name_a} ↔ {name_b}[/b]"
            f"    max sim {sim:.1f}% · overlap {_overlap_display(pair)} · {z_text}"
            f"    {flag_text}"
        )
        super().__init__(
            docs,
            f"Pairs / [b]{docs.assignment_name(assignment)}[/b] · "
            f"[b]{name_a} ↔ {name_b}[/b]",
        )

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(self._banner, id="detail-banner", markup=True)
        yield Static(self._histogram, id="detail-histogram", markup=True)
        with Horizontal(id="detail-cmp"):
            with VerticalScroll():
                yield Static("", id="detail-cmp-left", markup=True)
            with VerticalScroll():
                yield Static("", id="detail-cmp-right", markup=True)

    def on_mount(self) -> None:
        left, right = compare_content(self.assignment.config_path.parent, self.pair)
        self.query_one("#detail-cmp-left", Static).update(left)
        self.query_one("#detail-cmp-right", Static).update(right)
        cmp_pane = self.query_one("#detail-cmp", Horizontal)
        cmp_pane.styles.height = "1fr"
        for scroll in self.query("#detail-cmp VerticalScroll"):
            scroll.styles.width = "1fr"
