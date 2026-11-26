"""The application shell.

Follows RRI_Floodcast -- menu, toolbar with a prominent green Run, module tabs, three-pane
body, bottom dock, status bar -- with one addition: a **workflow strip** showing where the
user is in the chain and what is blocking them. CaMa-Flood's setup has many steps that fail
quietly, and greying out a tab tells the user something is unavailable but never why.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QWidget,
)

from ..adapters.camaflood import CamaFloodAdapter
from ..core.domain_builder import ToolSet
from ..core.errors import DiagnosticList
from ..core.maps import Grid, RiverMap
from ..core.namelist import Namelist
from ..core.project import Case, CaseStatus, Project
from ..services import binaries
from ..services.execution import JobQueue
from .modules.analysis_view import AnalysisView
from .modules.domain_view import DomainView
from .modules.downscale_view import DownscaleView
from .modules.forcing_view import ForcingView
from .modules.physics_view import PhysicsView
from .modules.project_view import ProjectView
from .modules.results_view import ResultsView
from .modules.run_view import RunView
from .modules.setup_view import SetupView
from . import theme
from .theme import STYLESHEET
from .widgets.docks import LogDock, ValidationPane

STEP_OK = theme.GOOD
STEP_WARN = theme.WARN
STEP_TODO = theme.DISABLED


class WorkflowStrip(QWidget):
    """The persistent progress indicator across the top."""

    STEPS = ("1 Domain", "2 Forcing", "3 Config", "4 Run", "5 Results")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 3, 10, 3)
        lay.setSpacing(16)
        self.labels: list[QLabel] = []
        for name in self.STEPS:
            lbl = QLabel(f"○ {name}")
            lbl.setStyleSheet(f"color:{STEP_TODO};")
            self.labels.append(lbl)
            lay.addWidget(lbl)
        lay.addStretch(1)
        self.note = QLabel("")
        self.note.setStyleSheet(f"color:{STEP_WARN};")
        lay.addWidget(self.note)

    def set_state(self, index: int, state: str, tip: str = "") -> None:
        mark, colour = {"ok": ("✓", STEP_OK),
                        "warn": ("!", STEP_WARN),
                        "todo": ("○", STEP_TODO)}[state]
        lbl = self.labels[index]
        lbl.setText(f"{mark} {self.STEPS[index]}")
        lbl.setStyleSheet(f"color:{colour};")
        lbl.setToolTip(tip)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CaMa_Floodcast")
        self.resize(1500, 950)
        self.setStyleSheet(STYLESHEET)

        self.adapter = CamaFloodAdapter()
        self.project: Project | None = None
        self.case: Case | None = None
        self.queue: JobQueue | None = None
        #: Set False to suppress every modal dialog, here and on the Domain/Forcing tabs (see
        #: the ``interactive`` property below). Used by the offscreen tests: a modal box there
        #: has nothing to dismiss it and blocks the event loop forever. Messages are routed to
        #: the status bar and the log instead, so nothing is silently lost.
        self._interactive = True

        self._build_actions()
        self._build_toolbar()
        self._build_body()
        self._build_docks()
        self.statusBar().showMessage("Ready")
        self._update_workflow()

    # ---------------------------------------------------------------- chrome

    def _build_actions(self) -> None:
        self.act_new = QAction("&New project...", self)
        self.act_new.setShortcut(QKeySequence.StandardKey.New)
        self.act_new.triggered.connect(self.new_project)

        self.act_open = QAction("&Open project...", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_project)

        self.act_import = QAction("&Import CaMa-Flood namelist...", self)
        self.act_import.triggered.connect(self.import_namelist)

        self.act_save = QAction("&Save configuration", self)
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.triggered.connect(lambda: self.setup.save())

        self.act_run = QAction("&Run case", self)
        self.act_run.setShortcut("F5")
        self.act_run.triggered.connect(self.run_current)

        self.act_stop = QAction("Sto&p", self)
        self.act_stop.triggered.connect(lambda: self.queue and self.queue.cancel_all())

        self.act_binary = QAction("Model &binary...", self)
        self.act_binary.triggered.connect(self.choose_binary)

        self.act_download_forcing = QAction("&Download forcing...", self)
        self.act_download_forcing.triggered.connect(
            lambda: self.forcing_view.open_download_dialog())

        self.act_quit = QAction("E&xit", self)
        self.act_quit.triggered.connect(self.close)

        self.act_about = QAction("&About", self)
        self.act_about.triggered.connect(self.about)

        m = self.menuBar()
        f = m.addMenu("&File")
        f.addAction(self.act_new)
        f.addAction(self.act_open)
        f.addAction(self.act_import)
        f.addSeparator()
        f.addAction(self.act_save)
        f.addSeparator()
        f.addAction(self.act_quit)

        s = m.addMenu("&Simulation")
        s.addAction(self.act_run)
        s.addAction(self.act_stop)
        s.addSeparator()
        s.addAction(self.act_binary)

        fc = m.addMenu("F&orcing")
        fc.addAction(self.act_download_forcing)

        h = m.addMenu("&Help")
        h.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction(self.act_new)
        tb.addAction(self.act_open)
        tb.addAction(self.act_import)
        tb.addSeparator()

        self.btn_run = QPushButton("▶  Run Simulation")
        self.btn_run.setObjectName("runButton")
        self.btn_run.clicked.connect(self.run_current)
        self.btn_run.setEnabled(False)
        tb.addWidget(self.btn_run)

        tb.addSeparator()
        self.lbl_case = QLabel("  no case  ")
        self.lbl_case.setStyleSheet(f"color:{theme.MUTED};")
        tb.addWidget(self.lbl_case)

    def _build_body(self) -> None:
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)

        container = QWidget()
        from PyQt6.QtWidgets import QVBoxLayout

        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        self.workflow = WorkflowStrip()
        vl.addWidget(self.workflow)

        self.tabs = QTabWidget()
        self.project_view = ProjectView()
        self.domain_view = DomainView()
        self.forcing_view = ForcingView()
        self.setup = SetupView()
        self.physics_view = PhysicsView()
        self.run_view = RunView()
        self.results = ResultsView()
        self.analysis_view = AnalysisView()
        self.downscale_view = DownscaleView()

        # PhysicsView edits the same Namelist Model Setup does -- see setup_view.namelistChanged
        # -- rather than loading an independent copy, so the two tabs cannot silently diverge.
        self.physics_view.bind(self.setup)

        # DomainView/ForcingView/PhysicsView pop their own dialogs (build/scan/apply failures
        # the shell never sees) -- sync them to this window's interactive setting at
        # construction; the property setter below keeps them in sync with any later change.
        self.domain_view.interactive = self._interactive
        self.forcing_view.interactive = self._interactive
        self.physics_view.interactive = self._interactive
        self.analysis_view.interactive = self._interactive
        self.downscale_view.interactive = self._interactive

        self.tabs.addTab(self.project_view, "Project")
        self.tabs.addTab(self.domain_view, "Domain")
        self.tabs.addTab(self.forcing_view, "Forcing")
        self.tabs.addTab(self.setup, "Model Setup")
        self.tabs.addTab(self.physics_view, "Physics")
        self.tabs.addTab(self.run_view, "Run Manager")
        self.tabs.addTab(self.results, "Map && Results")
        self.tabs.addTab(self.analysis_view, "Analysis")
        self.tabs.addTab(self.downscale_view, "Downscaling")
        vl.addWidget(self.tabs, 1)

        lay.addWidget(container)
        self.setCentralWidget(central)

        self.project_view.caseSelected.connect(self.select_case)
        self.project_view.runRequested.connect(self.run_case)
        self.setup.validationChanged.connect(self._on_validation)
        self.setup.dirtyChanged.connect(lambda d: self._update_workflow())
        self.run_view.runFinished.connect(self._on_run_finished)
        self.results.statusMessage.connect(self.statusBar().showMessage)

        self.domain_view.statusMessage.connect(self.statusBar().showMessage)
        self.domain_view.validationChanged.connect(self._on_domain_validation)
        self.domain_view.domainBuilt.connect(self._on_domain_built)
        self.domain_view.caseUpdated.connect(self._on_case_namelist_written)
        self.forcing_view.statusMessage.connect(self.statusBar().showMessage)
        self.forcing_view.validationChanged.connect(self._on_forcing_validation)
        self.forcing_view.caseUpdated.connect(self._on_case_namelist_written)

        self.physics_view.statusMessage.connect(self.statusBar().showMessage)
        self.physics_view.openRoughnessBuilder.connect(self._goto_roughness_builder)

        self.analysis_view.statusMessage.connect(self.statusBar().showMessage)

        self.downscale_view.statusMessage.connect(self.statusBar().showMessage)

    def _build_docks(self) -> None:
        self.dock = QDockWidget("Output", self)
        self.dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.dock_tabs = QTabWidget()
        self.log = LogDock()
        self.validation = ValidationPane()
        self.dock_tabs.addTab(self.log, "Log")
        self.dock_tabs.addTab(self.validation, "Validation")
        self.dock.setWidget(self.dock_tabs)
        self.dock.setMinimumHeight(190)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock)

        self.validation.locationActivated.connect(self._goto_location)
        self.run_view.logLines.connect(self.log.append)

    # ---------------------------------------------------------------- project

    def new_project(self) -> None:
        root = QFileDialog.getExistingDirectory(self, "Choose a folder for the new project")
        if not root:
            return
        name, ok = QInputDialog.getText(self, "New project", "Project name:",
                                        text=Path(root).name)
        if not ok or not name.strip():
            return
        try:
            proj = Project.create(Path(root) / name.strip(), name=name.strip())
        except Exception as exc:
            QMessageBox.critical(self, "New project", str(exc))
            return
        self.set_project(proj)
        QMessageBox.information(
            self, "New project",
            "Project created.\n\nUse File → Import CaMa-Flood namelist to create "
            "first case from an existing setup.")

    def open_project(self) -> None:
        root = QFileDialog.getExistingDirectory(self, "Open project folder")
        if not root:
            return
        try:
            self.set_project(Project.open(Path(root)))
        except Exception as exc:
            QMessageBox.warning(self, "Open project", str(exc))

    def import_namelist(self) -> None:
        """Create a project and first case from an existing CaMa-Flood setup.

        This is the MVP's primary entry point: users who already run CaMa-Flood by hand should
        be able to point at their namelist and see results, without building anything first.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Select input_cmf.nam", "", "Namelist (*.nam);;All files (*)")
        if not path:
            return
        nam = Path(path)

        try:
            nml = Namelist.load(nam)
        except Exception as exc:
            QMessageBox.critical(self, "Import", f"Could not parse the namelist:\n\n{exc}")
            return

        # The map directory is wherever CNEXTXY points.
        map_dir = None
        nextxy = nml.get("NMAP", "CNEXTXY", "")
        if nextxy:
            p = Path(str(nextxy))
            p = p if p.is_absolute() else (nam.parent / str(nextxy))
            if p.parent.exists():
                map_dir = p.parent

        if self.project is None:
            root = QFileDialog.getExistingDirectory(self, "Where should the project live?")
            if not root:
                return
            proj = Project.create(Path(root) / f"{nam.parent.name}_project",
                                  name=nam.parent.name,
                                  map_dir=str(map_dir) if map_dir else "domain")
            self.set_project(proj)

        assert self.project is not None
        name, ok = QInputDialog.getText(self, "Import", "Name for this case:",
                                        text=nam.parent.name or "imported")
        if not ok or not name.strip():
            return
        try:
            case = self.project.add_case(name.strip(), namelist=nml)
        except FileExistsError as exc:
            QMessageBox.warning(self, "Import", str(exc))
            return

        # Bring any existing results along, so an already-run setup is immediately viewable.
        copied = 0
        for f in nam.parent.glob("*.bin"):
            if f.name.startswith("restart"):
                continue
            try:
                (case.dir / f.name).write_bytes(f.read_bytes())
                copied += 1
            except OSError:
                pass

        if map_dir:
            self.project.config.map_dir = str(map_dir)
            self.project.save()

        self.project_view.refresh()
        self.analysis_view.set_project(self.project)
        self._configure_domain_tools()
        self.statusBar().showMessage(
            f"imported {nam.name}" + (f" with {copied} result file(s)" if copied else ""))
        self.select_case(case)

    def set_project(self, project: Project | None) -> None:
        self.project = project
        self.project_view.set_project(project)
        self.case = None
        self.setup.load_case(None)
        self.forcing_view.load_case(None)
        self.domain_view.load_case(None)
        self.physics_view.load_case(None)
        self.downscale_view.load_case(None)
        self.results.load(None, None)
        self.analysis_view.set_project(project)
        self.setWindowTitle(
            f"CaMa_Floodcast  --  {project.config.name}" if project else "CaMa_Floodcast")
        self._rebuild_queue()
        self._configure_domain_tools()
        self._update_workflow()

    def _active_grid(self) -> Grid | None:
        """The routing grid of the project's currently wired map, if it opens cleanly."""
        if self.project is None:
            return None
        try:
            return RiverMap(self.project.map_dir).grid
        except Exception:                                           # noqa: BLE001
            return None

    @staticmethod
    def _search_anchor() -> Path:
        """Where the upward search for ``bin/win64`` and ``src/`` starts. A frozen
        (PyInstaller) build keeps this module inside its ``_internal/`` bundle, one level
        deeper than a source checkout, so the search starts from the .exe there instead."""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve()
        return Path(__file__).resolve()

    def _guess_tools_dir(self) -> Path | None:
        here = self._search_anchor()
        for base in list(here.parents)[:6]:
            cand = base / "bin" / "win64"
            if cand.exists():
                return cand
        return None

    def _tools(self) -> ToolSet:
        """The domain-cutting Fortran tools, with their runtime library directory resolved
        the same way as the model binary (risk R4 applies here too)."""
        ts = ToolSet(directory=self._guess_tools_dir() or Path("bin/win64"))
        exe = ts.path("cut_domain")
        ts.runtime_path = binaries.resolve_runtime_path(exe) if exe.exists() else None
        return ts

    def _downscale_tool(self):
        from ..core.downscale import DownscaleTool

        name = "downscale_flddph_trib.exe" if sys.platform == "win32" else "downscale_flddph_trib"
        exe = (self._guess_tools_dir() or Path("bin/win64")) / name
        return DownscaleTool(exe=exe,
                             runtime_path=binaries.resolve_runtime_path(exe) if exe.exists() else None)

    def _configure_domain_tools(self) -> None:
        if self.project is None:
            return
        active = self.project.map_dir if (self.project.map_dir / "params.txt").exists() else None
        self.domain_view.configure(self.project.root / "domain", self._tools(),
                                   active_map=active)
        self.forcing_view.configure(self._active_grid())
        self.downscale_view.configure(active, self._downscale_tool())

    def _rebuild_queue(self) -> None:
        if self.project is None:
            self.queue = None
            self.run_view.set_queue(None)
            return
        exe = Path(self.project.config.model_binary or "")
        if not exe.is_file():
            guess = self._guess_binary()
            if guess:
                exe = guess
                self.project.config.model_binary = str(exe)
                self.project.save()

        # Resolve the runtime library directory and PREPEND it. Without this a MinGW-built
        # MAIN_cmf.exe dies with STATUS_ENTRYPOINT_NOT_FOUND and produces no output at all
        # (risk R4) -- which is exactly how the integration test first failed.
        self.binary = binaries.inspect(exe, run_self_test=False) if exe.is_file() else None
        runtime = self.binary.runtime_path if self.binary else None

        self.queue = JobQueue(exe, threads=self.project.config.default_threads,
                              extra_path=runtime, parent=self)
        self.run_view.set_queue(self.queue)

    def _guess_binary(self) -> Path | None:
        name = "MAIN_cmf.exe" if sys.platform == "win32" else "MAIN_cmf"
        here = self._search_anchor()
        for base in list(here.parents)[:6]:
            cand = base / "src" / name
            if cand.exists():
                return cand
        # An installed copy ships the model beside the other Fortran tools in bin/win64,
        # together with the MinGW runtime DLLs it needs.
        tools = self._guess_tools_dir()
        if tools and (tools / name).exists():
            return tools / name
        return None

    def choose_binary(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Model binary", "Open or create a project first.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select MAIN_cmf", "",
                                              "Executable (*.exe);;All files (*)")
        if not path:
            return
        self.project.config.model_binary = path
        self.project.save()
        self._rebuild_queue()
        self.project_view.set_project(self.project)
        self.run_self_test()

    def run_self_test(self) -> None:
        """Can the chosen binary actually start, and what was it compiled with?

        Worth doing explicitly: on Windows a binary that cannot load its runtime libraries
        fails with no message whatsoever, and the exit code is the only evidence.
        """
        if self.project is None:
            return
        exe = Path(self.project.config.model_binary or "")
        info = binaries.inspect(exe, run_self_test=True)
        self.binary = info
        sig, test = info.signature, info.tested

        lines = [f"Binary : {exe}",
                 f"Build  : {sig.describe()}"]
        if sig.source != "manifest":
            lines.append("         (no manifest.json beside the binary -- capabilities unknown, "
                         "so a netCDF configuration cannot be checked in advance)")
        if info.runtime_path:
            lines.append(f"Runtime: {info.runtime_path}")
        lines.append("")
        lines.append(test.message if test else "not tested")
        if test and test.hint:
            lines.append("")
            lines.append(test.hint)

        text = "\n".join(lines)
        if test and test.ok:
            self._inform("Model binary", text)
            self.statusBar().showMessage(f"model binary OK: {exe.name}")
        else:
            self._warn("Model binary", text)

    # ---------------------------------------------------------------- notifications

    @property
    def interactive(self) -> bool:
        return self._interactive

    @interactive.setter
    def interactive(self, value: bool) -> None:
        self._interactive = value
        self.domain_view.interactive = value
        self.forcing_view.interactive = value
        self.physics_view.interactive = value
        self.analysis_view.interactive = value
        self.downscale_view.interactive = value

    def _warn(self, title: str, text: str) -> None:
        """Modal when interactive; status bar plus log otherwise, so nothing is lost."""
        if self.interactive:
            QMessageBox.warning(self, title, text)
        else:
            self.statusBar().showMessage(f"{title}: {text.splitlines()[0]}")
            self.log.append([f"[{title}] {line}" for line in text.splitlines()])

    def _inform(self, title: str, text: str) -> None:
        if self.interactive:
            QMessageBox.information(self, title, text)
        else:
            self.statusBar().showMessage(f"{title}: {text.splitlines()[0]}")

    # ---------------------------------------------------------------- case

    def select_case(self, case: Case | None) -> None:
        if self.setup.dirty and self.case is not None and self.interactive:
            if QMessageBox.question(
                    self, "Unsaved changes",
                    f"Save changes to '{self.case.name}' first?") == QMessageBox.StandardButton.Yes:
                self.setup.save()

        self.case = case
        self.setup.load_case(case)
        self.forcing_view.load_case(case)
        self.domain_view.load_case(case)
        self.physics_view.load_case(case)
        self.downscale_view.load_case(case)
        self.lbl_case.setText(f"  {case.name}  " if case else "  no case  ")
        map_dir = self.project.map_dir if self.project else None
        self.results.load(case, map_dir)
        self.btn_run.setEnabled(case is not None and self.queue is not None)
        self._update_workflow()

    def run_current(self) -> None:
        if self.case is not None:
            self.run_case(self.case)

    def run_case(self, case: Case) -> None:
        if self.queue is None or not Path(self.queue.executable).exists():
            self._warn("Run", "No model binary is configured.\n\n"
                              "Simulation → Model binary...")
            return

        if self.setup.dirty and self.case is case:
            self.setup.save()

        diags = self.adapter.validate(case.namelist(), base_dir=case.dir)
        self.validation.show_diagnostics(diags, context="pre-flight")
        if not diags.ok:
            self.dock_tabs.setCurrentWidget(self.validation)
            self._warn("Pre-flight validation",
                       f"This configuration would fail: {diags.summary()}.\n\n"
                       "See the Validation tab for the details.")
            return

        caps = self.adapter.required_capabilities(case.namelist())
        if "netcdf" in caps:
            self._inform("Model capability",
                         "This configuration needs a netCDF-enabled build. If the binary was "
                         "compiled without -DUseCDF_CMF the run will fail at runtime, not at "
                         "startup.")

        self.tabs.setCurrentWidget(self.run_view)
        self.run_view.enqueue(case)
        self.statusBar().showMessage(f"queued {case.name}")
        self._update_workflow()

    def _on_run_finished(self, case: Case, code: int) -> None:
        self.project_view.refresh()
        if code == 0:
            self.statusBar().showMessage(f"{case.name} completed")
            if self.case is case:
                self.results.load(case, self.project.map_dir if self.project else None)
                self.downscale_view.load_case(case)
                self.tabs.setCurrentWidget(self.results)
        else:
            self.statusBar().showMessage(f"{case.name} failed (exit {code})")
            self._warn("Run failed", self.adapter.explain_exit(code))
        self._update_workflow()

    # ---------------------------------------------------------------- domain / forcing

    def _on_domain_built(self, map_dir: Path) -> None:
        """A regional cut just finished: wire it in as the project's active map.

        There is one active map per project today, so building a domain and not using it
        would be unusual -- this mirrors ``choose_binary`` immediately committing its choice
        rather than asking first.
        """
        if self.project is None:
            return
        map_dir = Path(map_dir)
        try:
            self.project.config.map_dir = str(map_dir.relative_to(self.project.root))
        except ValueError:
            self.project.config.map_dir = str(map_dir)
        self.project.save()
        self.project_view.set_project(self.project)
        self.analysis_view.set_project(self.project)
        self._configure_domain_tools()
        if self.case is not None:
            self.results.load(self.case, self.project.map_dir)
        self.statusBar().showMessage(f"domain '{map_dir.name}' built and set as the active map")
        self._update_workflow()

    def _on_domain_validation(self, diags: DiagnosticList) -> None:
        """Gate 1 (domain closure), routed to the shared dock -- but not to the workflow strip's
        'Config' step, which is driven by the namelist only."""
        self.validation.show_diagnostics(diags, context="domain closure")

    def _on_forcing_validation(self, diags: DiagnosticList) -> None:
        """Gate 2 (forcing coverage), routed to the shared dock; same reasoning as above."""
        self.validation.show_diagnostics(diags, context="forcing coverage")

    def _on_case_namelist_written(self, case: Case) -> None:
        """A tab other than Model Setup wrote directly into a case's namelist (forcing's
        CROFDIR/CROFPRE/CROFSUF/DROFUNIT, or the domain builder's CFLDMAN) -- reload the
        editor if it is showing that case, so it doesn't silently go stale."""
        if self.case is case:
            self.setup.load_case(case)
        self.project_view.refresh()
        self._update_workflow()

    # ---------------------------------------------------------------- state

    def _on_validation(self, diags: DiagnosticList) -> None:
        self.validation.show_diagnostics(diags, context="configuration")
        self._update_workflow(diags)

    def _goto_location(self, where: str) -> None:
        self.tabs.setCurrentWidget(self.setup)
        self.setup.focus_location(where)

    def _goto_roughness_builder(self) -> None:
        self.tabs.setCurrentWidget(self.domain_view)
        self.domain_view.show_roughness_tab()

    def _update_workflow(self, diags: DiagnosticList | None = None) -> None:
        w = self.workflow

        have_map = bool(self.project and (self.project.map_dir / "params.txt").exists())
        w.set_state(0, "ok" if have_map else "todo",
                    "Domain map found." if have_map
                    else "No river map. Import a namelist that points at one, or build a "
                         "regional domain (Phase 2).")

        have_forcing = False
        if self.case and self.case.exists():
            try:
                d = self.case.namelist().get("NFORCE", "CROFDIR", "")
                # Relative paths are relative to the case directory, the model's own cwd.
                have_forcing = bool(d) and (self.case.dir / str(d)).exists()
            except Exception:
                have_forcing = False
        w.set_state(1, "ok" if have_forcing else "todo",
                    "Runoff forcing directory exists." if have_forcing
                    else "Forcing directory not found. CaMa-Flood needs RUNOFF, not rainfall.")

        if self.case is None:
            w.set_state(2, "todo", "No case selected.")
        elif diags is not None and not diags.ok:
            w.set_state(2, "warn", diags.summary())
        elif self.setup.dirty:
            w.set_state(2, "warn", "Unsaved configuration changes.")
        else:
            w.set_state(2, "ok", "Configuration validates.")

        status = self.case.status() if self.case else None
        if status is CaseStatus.COMPLETED:
            w.set_state(3, "ok", "Run completed.")
        elif status is CaseStatus.STALE:
            w.set_state(3, "warn", "Results are stale: the configuration changed after the run.")
        elif status is CaseStatus.FAILED:
            w.set_state(3, "warn", "The last run failed.")
        else:
            w.set_state(3, "todo", "Not run yet.")

        has_results = bool(self.case and self.case.output_files())
        w.set_state(4, "ok" if has_results else "todo",
                    "Results available." if has_results else "No results yet.")

        if status is CaseStatus.STALE:
            w.note.setText("Results are stale — re-run before using them")
        elif self.setup.dirty:
            w.note.setText("Unsaved changes")
        else:
            w.note.setText("")

    def about(self) -> None:
        from .. import __version__

        QMessageBox.about(
            self, "About CaMa_Floodcast",
            f"<h3>CaMa_Floodcast {__version__}</h3>"
            "A desktop application for the CaMa-Flood global river hydrodynamic model."
            "<p><b>Features</b></p>"
            "<ul>"
            "<li><b>Project</b> &ndash; projects and cases, stale-result detection, "
            "parameter sweeps</li>"
            "<li><b>Domain Builder</b> &ndash; regional domain cutting from the global map, "
            "basin-closure check, land cover &rarr; floodplain roughness (GeoTIFF in any "
            "CRS)</li>"
            "<li><b>Forcing</b> &ndash; runoff import, unit and coverage audit, direct "
            "ERA5-Land / GLDAS download (CDS, NASA GES DISC or Google Earth Engine)</li>"
            "<li><b>Model Setup &amp; Physics</b> &ndash; validated namelist editor; "
            "bifurcation, levees, dams/reservoirs, sea level, adaptive time step, floodplain "
            "roughness</li>"
            "<li><b>Run Manager</b> &ndash; job queue, live log and water-balance "
            "monitor</li>"
            "<li><b>Map &amp; Results</b> &ndash; Leaflet basemaps, layer stack, colour-scale "
            "presets, click-to-probe, gauge hydrographs, levee overtopping statistics</li>"
            "<li><b>Analysis</b> &ndash; scenario A&minus;B difference maps, paired "
            "hydrographs (NSE/KGE/PBIAS), longitudinal river profiles</li>"
            "<li><b>Downscaling</b> &ndash; 1-arcmin inundation depth, single day or period "
            "batch, GeoTIFF/PNG/CSV export</li>"
            "</ul>"
            "<p><b>Developed by</b>: Nguyen Xuan TINH (Ph.D.)</p>"
            "<p><b>License and Copyright</b> &copy; 2026 Nippon Koei Co.,Ltd.<br>"
            "Water Resources &amp; Energy Dept.<br>"
            "〒102-8539: 5-4 Kojimachi, Chiyoda-ku, Tokyo, JAPAN<br>"
            "Tel: +81-80-4689-7461<br>"
            "E-Mail: <a href='mailto:tinh-ng@n-koei.jp'>tinh-ng@n-koei.jp</a><br>"
            "URL: <a href='https://www.n-koei.co.jp/consulting/english/'>"
            "https://www.n-koei.co.jp/consulting/english/</a></p>")

    def closeEvent(self, event) -> None:
        if not self.interactive:
            if self.queue and self.queue.busy:
                self.queue.cancel_all()
            event.accept()
            return

        if self.queue and self.queue.busy:
            if QMessageBox.question(
                    self, "Quit",
                    "A simulation is running. Stop it and quit?") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.queue.cancel_all()
        if self.setup.dirty:
            if QMessageBox.question(
                    self, "Quit",
                    "Save configuration changes first?") == QMessageBox.StandardButton.Yes:
                self.setup.save()
        event.accept()
