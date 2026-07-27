"""Textual wizard for editing ``bundle.yml``.

The wizard is a *view*. Every decision it collects is written back to the manifest and
generation goes through the same :mod:`app.pipeline` the CLI uses, so what is previewed
here is exactly what CI produces. No planning logic lives in this module.
"""

from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection

from app import pipeline
from core.manifest import Manifest, ServiceEntry
from core.model import MountMode
from ui.i18n import t


class WizardApp(App[None]):
    """Single-window, tabbed editor over the manifest."""

    CSS = """
    Screen { layout: vertical; }
    #status { dock: bottom; height: 1; padding: 0 1; background: $panel; }
    .hint { color: $text-muted; padding: 0 1; }
    SelectionList { height: 1fr; }
    DataTable { height: 1fr; }
    RadioSet { height: auto; margin: 0 1 1 1; }
    """

    BINDINGS = [
        Binding("s", "save", "save"),
        Binding("g", "generate", "generate"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, manifest: Manifest, *, pull: bool = False) -> None:
        super().__init__()
        self.manifest = manifest
        self.pull = pull
        self.context = pipeline.load(manifest, pull=pull)
        pipeline.ensure_entries(self.context)
        self._by_slug = {spec.slug: spec for spec in self.context.discovery.services}

    # -- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent():
            with TabPane(t("wizard.services"), id="tab-services"):
                yield Static(t("wizard.mounts_hint"), classes="hint")
                yield SelectionList(*self._service_options(), id="services")
            with TabPane(t("wizard.mounts"), id="tab-mounts"):
                yield Static(t("wizard.mounts_hint"), classes="hint")
                yield SelectionList(*self._mount_options(), id="mounts")
            with TabPane(t("wizard.ports"), id="tab-ports"):
                yield Static(t("wizard.ports_hint"), classes="hint")
                yield DataTable(id="ports")
            with TabPane(t("wizard.replicas"), id="tab-replicas"):
                yield Static(t("wizard.replicas_hint"), classes="hint")
                yield VerticalScroll(*self._replica_inputs(), id="replicas")
            with TabPane(t("wizard.conflicts"), id="tab-conflicts"):
                yield VerticalScroll(*self._conflict_widgets(), id="conflicts")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_ports()
        self._status(t("wizard.help"))

    # -- option builders -------------------------------------------------

    def _service_options(self) -> list[Selection]:
        options: list[Selection] = []
        for spec in self.context.discovery.services:
            entry = self.manifest.services.get(spec.slug)
            recipe, is_fallback = self.context.registry.resolve(spec)
            if recipe.bakeable:
                placement = t("wizard.bake")
            else:
                why = recipe.reason.strip().splitlines()[0]
                placement = f"{t('wizard.unbakeable')}: {why}"
            suffix = " *" if is_fallback else ""
            label = f"{spec.slug:34} {recipe.name}{suffix:2}  {placement}"
            options.append(Selection(label, spec.slug, entry is not None and entry.enabled))
        return options

    def _mount_options(self) -> list[Selection]:
        from plan import mounts as mounts_mod

        options: list[Selection] = []
        for spec in self.context.selected:
            recipe, _ = self.context.registry.resolve(spec)
            mounts_mod.classify(spec, recipe)
            entry = self.manifest.services.get(spec.slug)
            for mount in spec.mounts:
                if not mount.bakeable:
                    continue
                override = (entry.mounts.get(mount.target) if entry else None) or mount.mode.value
                label = (
                    f"{spec.slug:28} {mount.source:22} -> "
                    f"{mount.target:32} {mount.kind.value}"
                )
                baked = override == MountMode.COPY.value
                options.append(Selection(label, f"{spec.slug}|{mount.target}", baked))
        return options or [Selection("(no bind mounts)", "none", False)]

    def _replica_inputs(self) -> list:
        widgets: list = []
        for spec in self.context.selected:
            entry = self.manifest.services.get(spec.slug)
            widgets.append(Label(spec.slug))
            widgets.append(
                Input(
                    value=str(entry.replicas if entry else 1),
                    id=f"rep-{spec.slug}",
                    type="integer",
                )
            )
        return widgets or [Label("(nothing selected)")]

    def _conflict_widgets(self) -> list:
        conflicts = pipeline.conflicts(self.context)
        if not conflicts:
            return [Static(t("wizard.no_conflicts"), classes="hint")]

        widgets: list = [Static(t("wizard.conflict_prompt"), classes="hint")]
        for conflict in conflicts:
            widgets.append(Label(conflict.summary()))
            buttons = [RadioButton(t("wizard.resolve_prefix"), value=True, name="prefix")]
            buttons += [
                RadioButton(t("wizard.resolve_keep", slug=slug), name=f"keep:{slug}")
                for slug in sorted(conflict.values)
            ]
            widgets.append(RadioSet(*buttons, id=f"cf-{conflict.key}"))
        return widgets

    # -- state -----------------------------------------------------------

    def _refresh_ports(self) -> None:
        table = self.query_one("#ports", DataTable)
        table.clear(columns=True)
        table.add_columns("service", "original", "assigned", "published")

        from plan import ports as ports_mod

        pairs = []
        for spec in self.context.selected:
            recipe, _ = self.context.registry.resolve(spec)
            if recipe.bakeable:
                pairs.append((spec, recipe))

        allocation = ports_mod.allocate(
            pairs,
            pinned={s: e.ports for s, e in self.manifest.services.items() if e.ports},
            port_range=self.manifest.port_range,
        )
        for slug, entries in allocation.ports.items():
            for port in entries:
                assigned = str(port.container)
                if port.remapped:
                    assigned = f"[yellow]{assigned}[/yellow]"
                table.add_row(slug, str(port.original), assigned, port.published or "-")

        for error in allocation.errors:
            self._status(f"[red]{error}[/red]")

    def _collect(self) -> None:
        """Write every widget's state back into the manifest."""
        selected = set(self.query_one("#services", SelectionList).selected)
        for slug in self._by_slug:
            entry = self.manifest.services.get(slug)
            if entry is None:
                spec = self._by_slug[slug]
                entry = ServiceEntry(slug=slug, package=spec.package, service=spec.name)
                self.manifest.services[slug] = entry
            entry.enabled = slug in selected

        baked = set(self.query_one("#mounts", SelectionList).selected)
        for slug, entry in self.manifest.services.items():
            spec = self._by_slug.get(slug)
            if spec is None:
                continue
            for mount in spec.mounts:
                if not mount.bakeable:
                    continue
                key = f"{slug}|{mount.target}"
                entry.mounts[mount.target] = (
                    MountMode.COPY.value if key in baked else MountMode.VOLUME.value
                )

        for slug, entry in self.manifest.services.items():
            try:
                widget = self.query_one(f"#rep-{slug}", Input)
            except Exception:
                continue
            try:
                entry.replicas = max(1, int(widget.value or "1"))
            except ValueError:
                entry.replicas = 1

        for conflict in pipeline.conflicts(self.context):
            try:
                radio = self.query_one(f"#cf-{conflict.key}", RadioSet)
            except Exception:
                continue
            pressed = radio.pressed_button
            if pressed is not None and pressed.name:
                self.manifest.env_conflicts[conflict.key] = pressed.name

        # Re-resolve the selection so ports and conflicts reflect the new choices.
        self.context.selected = [
            spec
            for spec in self.context.discovery.services
            if self.manifest.services.get(spec.slug, ServiceEntry(slug=spec.slug)).enabled
        ]

    def _status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    # -- actions ----------------------------------------------------------

    @on(SelectionList.SelectedChanged, "#services")
    def _services_changed(self) -> None:
        self._collect()
        self._refresh_ports()

    def action_save(self) -> None:
        self._collect()
        path = self.manifest.save()
        self._status(t("wizard.saved", path=path))

    def action_generate(self) -> None:
        self._collect()
        self.manifest.save()
        try:
            written = pipeline.generate(
                self.context, variant=self.manifest.variants[0] if self.manifest.variants else "cpu"
            )
        except pipeline.PlanError as exc:
            self._status(f"[red]{exc.problems[0]}[/red]")
            return
        self._status(t("wizard.generated", path=written.directory))


def run(manifest: Manifest, *, pull: bool = False) -> None:
    WizardApp(manifest, pull=pull).run()


__all__ = ["WizardApp", "run"]
