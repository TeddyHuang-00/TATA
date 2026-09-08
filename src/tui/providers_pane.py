"""Providers pane (:class:`ProvidersPane`) for the Library tab: provider
registry editor over ``data/providers/<name>.toml``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, override

import tomlkit
from instructor import Mode
from pydantic import ValidationError
from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Button, Input, Label, Select, Static

from src import REPO_ROOT
from src.shared.provider import (
    ProviderInfo,
    build_provider_client,
    resolve_env_placeholders,
)
from src.tui.css_loader import load_css
from src.tui.pane_shared import new_value
from src.tui.rubrics_pane import FileNameModal
from src.tui.workspace import ConfirmationModal

if TYPE_CHECKING:
    from src.tui.app import AppState


_MODE_VALUES = tuple(mode.value for mode in Mode)


def _provider_reference_configs(data_dir: Path, name: str) -> list[Path]:
    """Parse-based scan: config.toml files whose ``[grading].provider``
    references ``name``. Same path patterns as :func:`grading_reference_configs`;
    unreadable/unparseable files are skipped."""
    hits: list[Path] = []
    for pattern in ("*/config.toml", "*/*/config.toml"):
        for path in data_dir.glob(pattern):
            try:
                doc = tomlkit.parse(path.read_text(encoding="utf-8"))
            except (OSError, tomlkit.exceptions.ParseError):
                continue
            grading = doc.get("grading")
            if isinstance(grading, MutableMapping) and grading.get("provider") == name:
                hits.append(path)
    return hits


def _ping_provider(base_url: str, api_key: str, model: str, mode: str) -> None:
    """Real connectivity probe: one tiny chat completion via the shared
    instructor-wrapped client (same construction as grading). Runs on a
    worker thread; raises on any failure."""
    client = build_provider_client(base_url, api_key, Mode(mode))
    client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=1,
    )


class ProvidersPane(Vertical):
    """Provider registry editor: Select + form + Save/Delete/Test connection.

    One provider per file in ``data/providers/<name>.toml`` with flat
    top-level keys (base_url, api_key, model, mode, temperature), edited
    with tomlkit (comment-preserving). ``providers_dir`` is injectable for
    isolated tests; the default is the repo's ``data/providers``.
    """

    DEFAULT_CSS = load_css("library.tcss")

    def __init__(self, state: AppState, providers_dir: Path | None = None) -> None:
        super().__init__()
        self.state = state
        self._providers_dir = providers_dir or (REPO_ROOT / "data" / "providers")
        #: Name of the provider loaded into the form (None = new provider).
        self._current: str | None = None

    def _provider_file(self, name: str) -> Path:
        return self._providers_dir / f"{name}.toml"

    def _doc(self, name: str) -> tomlkit.TOMLDocument:
        """Parse one provider file; an empty document when missing/unreadable."""
        try:
            return tomlkit.parse(self._provider_file(name).read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError):
            return tomlkit.document()

    def _names(self) -> list[str]:
        return sorted(p.stem for p in self._providers_dir.glob("*.toml"))

    def _options(self) -> list[tuple[str, str]]:
        return [(name, name) for name in self._names()] + [("New provider…", new_value)]

    # ---------- composition ----------

    @override
    def compose(self) -> ComposeResult:
        with ScrollableContainer():
            yield Static("[b]Providers[/b]", id="pv-title")
            with Horizontal(id="pv-file-row"):
                yield Select(self._options(), id="pv-name", allow_blank=False)
                yield Input("", placeholder="new provider name", id="pv-new-name")
            yield Static("", id="pv-status")
            with Vertical(id="pv-form"):
                with Vertical(classes="rb-field"):
                    yield Label("base_url")
                    yield Input(
                        id="pv-base-url", placeholder="https://api.example.com/v1"
                    )
                with Vertical(classes="rb-field"):
                    yield Label("api_key")
                    yield Input(
                        id="pv-api-key", placeholder="literal value or ${ENV_VAR}"
                    )
                with Vertical(classes="rb-field"):
                    yield Label("model")
                    yield Input(id="pv-model", placeholder="model id")
                with Vertical(classes="rb-field"):
                    yield Label("mode")
                    yield Select(
                        [(value, value) for value in _MODE_VALUES],
                        id="pv-mode",
                        allow_blank=False,
                    )
                with Vertical(classes="rb-field"):
                    yield Label("temperature (optional, 0.0-2.0)")
                    yield Input(
                        id="pv-temperature", placeholder="blank = provider default"
                    )
            with Horizontal(id="pv-actions"):
                yield Button("Save", id="pv-save", variant="primary")
                yield Button("Rename", id="pv-rename", disabled=True)
                yield Button("Delete", id="pv-delete", disabled=True)
                yield Button("Test connection", id="pv-test")

    @override
    def on_mount(self) -> None:
        self._on_name_change(str(self.query_one("#pv-name", Select).value))

    # ---------- selection ----------

    def reload_files(self) -> None:
        """Reload the provider list (called when the Library tab activates)."""
        self.query_one("#pv-name", Select).set_options(self._options())

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "pv-name":
            self._on_name_change(str(event.value))

    def _on_name_change(self, value: str) -> None:
        is_new = value == new_value
        self.query_one("#pv-new-name", Input).display = "block" if is_new else "none"
        self._current = None if is_new else value
        if is_new:
            self._clear_form()
            return
        self._load_provider(value)

    def _load_provider(self, name: str) -> None:
        doc = self._doc(name)
        if not doc:
            self._set_status(f"[red]Could not load provider {escape(name)}[/red]")
            self._clear_form()
            return
        mode = str(doc.get("mode", _MODE_VALUES[0]))
        if mode not in _MODE_VALUES:
            mode = _MODE_VALUES[0]
        temperature = doc.get("temperature")
        self.query_one("#pv-base-url", Input).value = str(doc.get("base_url", ""))
        self.query_one("#pv-api-key", Input).value = str(doc.get("api_key", ""))
        self.query_one("#pv-model", Input).value = str(doc.get("model", ""))
        self.query_one("#pv-mode", Select).value = mode
        self.query_one("#pv-temperature", Input).value = (
            "" if temperature is None else str(temperature)
        )
        self._set_status("")
        self._sync_buttons()

    def _clear_form(self) -> None:
        self.query_one("#pv-base-url", Input).value = ""
        self.query_one("#pv-api-key", Input).value = ""
        self.query_one("#pv-model", Input).value = ""
        self.query_one("#pv-mode", Select).value = _MODE_VALUES[0]
        self.query_one("#pv-temperature", Input).value = ""
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        disabled = self._current is None
        self.query_one("#pv-rename", Button).disabled = disabled
        self.query_one("#pv-delete", Button).disabled = disabled

    def _set_status(self, text: str) -> None:
        self.query_one("#pv-status", Static).update(text)

    # ---------- form ----------

    def _form_values(self) -> dict | None:
        base_url = self.query_one("#pv-base-url", Input).value.strip()
        api_key = self.query_one("#pv-api-key", Input).value.strip()
        model = self.query_one("#pv-model", Input).value.strip()
        mode = str(self.query_one("#pv-mode", Select).value)
        temperature = self.query_one("#pv-temperature", Input).value.strip()
        if not base_url:
            self._set_status("[red]base_url cannot be empty[/red]")
            return None
        if not api_key:
            self._set_status("[red]api_key cannot be empty[/red]")
            return None
        if not model:
            self._set_status("[red]model cannot be empty[/red]")
            return None
        values: dict = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "mode": mode,
        }
        if temperature:
            try:
                values["temperature"] = float(temperature)
            except ValueError:
                self._set_status(
                    f"[red]temperature must be a number: {escape(temperature)}[/red]"
                )
                return None
        return values

    def _target_name(self) -> str | None:
        if self._current is not None:
            return self._current
        raw = self.query_one("#pv-new-name", Input).value.strip()
        if not raw:
            self._set_status("[red]Enter a name for the new provider[/red]")
            return None
        if raw == new_value or Path(raw).name != raw:
            self._set_status(f"[red]Invalid provider name: {escape(raw)}[/red]")
            return None
        if raw in self._names():
            self._set_status(
                f"[red]A provider named {escape(raw)} already exists[/red]"
            )
            return None
        return raw

    def _show_validation_errors(self, exc: ValidationError) -> None:
        message = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', []))}: {error.get('msg')}"
            for error in exc.errors()
        )
        self._set_status(f"[red]{escape(message)}[/red]")
        self.app.notify(message, severity="error")

    def _write(self, name: str, doc: tomlkit.TOMLDocument) -> bool:
        out = tomlkit.dumps(doc)
        if not out.endswith("\n"):
            out += "\n"
        try:
            path = self._provider_file(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(out, encoding="utf-8")
        except OSError as exc:
            self._set_status(f"[red]Write failed: {exc}[/red]")
            return False
        return True

    # ---------- save / delete ----------

    def action_save(self) -> None:
        name = self._target_name()
        if name is None:
            return
        values = self._form_values()
        if values is None:
            return
        try:
            ProviderInfo.model_validate(values)
        except ValidationError as exc:
            self._show_validation_errors(exc)
            return
        doc = self._doc(name)
        for key, value in values.items():
            doc[key] = value
        if "temperature" not in values:
            with suppress(KeyError):
                del doc["temperature"]
        if not self._write(name, doc):
            return
        self._current = name
        self.query_one("#pv-name", Select).set_options(self._options())
        self.query_one("#pv-name", Select).value = name
        self.query_one("#pv-new-name", Input).display = "none"
        self._sync_buttons()
        self._set_status(f"[green]Saved provider: {escape(name)}[/green]")
        self.app.notify(f"Saved provider: {name}", severity="success")

    def action_delete(self) -> None:
        name = self._current
        if name is None:
            self._set_status("[warning]Select an existing provider to delete[/warning]")
            return
        refs = _provider_reference_configs(self.state.assignments_dir, name)
        message = f"Delete provider {name}? This cannot be undone."
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference {name}"
                " and will be broken. Consider updating them first."
            )
        self.app.push_screen(
            ConfirmationModal("Delete provider", message, [("Delete", "delete")]),
            lambda choice: self._finish_delete(choice, name),
        )

    def _finish_delete(self, choice: str | None, name: str) -> None:
        if choice is None:
            return
        path = self._provider_file(name)
        if not path.exists():
            self._set_status(f"[red]Not found: {escape(name)}[/red]")
            return
        try:
            path.unlink()
        except OSError as exc:
            self._set_status(f"[red]Remove failed: {exc}[/red]")
            return
        self._current = None
        self.query_one("#pv-name", Select).set_options(self._options())
        self._select_first()
        self._set_status(f"[green]Deleted provider: {escape(name)}[/green]")
        self.app.notify(f"Deleted provider: {name}", severity="warning")

    def action_rename(self) -> None:
        if self._current is None:
            self._set_status("[warning]Select an existing provider to rename[/warning]")
            return
        self.app.push_screen(
            FileNameModal("Rename provider", self._current), self._handle_rename
        )

    def _handle_rename(self, result: str | None) -> None:
        if result is None:
            return
        current = self._current
        if current is None:
            return
        raw = result.strip()
        if not raw:
            self._set_status("[red]Enter a provider name[/red]")
            return
        if raw == new_value or Path(raw).name != raw:
            self._set_status(f"[red]Invalid provider name: {escape(raw)}[/red]")
            return
        if raw == current:
            self._set_status("[red]New name is the same as the current name[/red]")
            return
        if raw in self._names():
            self._set_status(
                f"[red]A provider named {escape(raw)} already exists[/red]"
            )
            return
        refs = _provider_reference_configs(self.state.assignments_dir, current)
        message = f"Rename {current} to {raw}?"
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference {current}"
                " and will be broken. Consider updating them first."
            )
        self.app.push_screen(
            ConfirmationModal("Rename provider", message, [("Rename", "rename")]),
            lambda choice: self._finish_rename(choice, current, raw, len(refs)),
        )

    def _finish_rename(
        self, choice: str | None, old: str, new: str, n_refs: int
    ) -> None:
        if choice is None:
            return
        old_path = self._provider_file(old)
        if not old_path.exists():
            self._set_status(f"[red]Not found: {escape(old)}[/red]")
            return
        try:
            old_path.rename(self._provider_file(new))
        except OSError as exc:
            self._set_status(f"[red]Rename failed: {exc}[/red]")
            return
        self._current = new
        self.query_one("#pv-name", Select).set_options(self._options())
        self.query_one("#pv-name", Select).value = new
        self._sync_buttons()
        if n_refs:
            self.app.notify(
                f"Renamed provider: {new} · update {n_refs} config reference(s)",
                severity="warning",
            )
        else:
            self.app.notify(f"Renamed provider: {new}", severity="success")
        self._set_status(
            f"[green]Renamed provider: {escape(old)} to {escape(new)}[/green]"
        )

    def _select_first(self) -> None:
        first = next(
            (value for _, value in self._options() if value != new_value), new_value
        )
        self.query_one("#pv-name", Select).value = first

    # ---------- test connection ----------

    def action_test_connection(self) -> None:
        values = self._form_values()
        if values is None:
            return
        base_url = values["base_url"]
        api_key = resolve_env_placeholders(values["api_key"])
        model = values["model"]
        self._set_status("[dim]Testing connection…[/dim]")

        def probe() -> None:
            try:
                _ping_provider(base_url, api_key, model, values["mode"])
            except Exception as exc:
                ok, message = (
                    False,
                    f"Test connection failed: {type(exc).__name__}: {exc}",
                )
            else:
                ok, message = True, f"Test connection OK: {model}"
            with suppress(RuntimeError):  # app closed mid-probe
                self.app.call_from_thread(self._test_done, ok, message)

        self.run_worker(probe, thread=True, group="library-test")

    def _test_done(self, ok: bool, message: str) -> None:
        color = "green" if ok else "red"
        self._set_status(f"[{color}]{escape(message)}[/{color}]")
        self.app.notify(message, severity="success" if ok else "error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "pv-save":
            self.action_save()
        elif button_id == "pv-rename":
            self.action_rename()
        elif button_id == "pv-delete":
            self.action_delete()
        elif button_id == "pv-test":
            self.action_test_connection()
