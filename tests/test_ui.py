"""Phase 1 smoke tests: the GUI builds, loads real data, and its views populate.

Runs offscreen (``QT_QPA_PLATFORM=offscreen``) so it works in CI and over SSH. These are
deliberately shallow -- they check wiring, not pixels. The behaviour that matters is already
covered headless in ``test_core.py``; what can break here is construction, signal connections
and the adapter boundary.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt, QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

# QtWebEngine (ui.map.leaflet) must either be imported, or this attribute set, before any
# QApplication exists -- see the matching comment in ui/app.py. Set here, before the qapp
# fixture (or anything else in the session) can construct one, so the real Leaflet renderer is
# what these tests exercise rather than a silent fallback to the native one.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

from camafloodcast.core.namelist import Namelist  # noqa: E402
from camafloodcast.core.project import Project  # noqa: E402

PKG = Path(__file__).resolve().parents[2]
MAP_PHL = PKG / "map" / "phl_15min"
MAP_GLB = PKG / "map" / "glb_15min"
MAP_MEK = PKG / "map" / "mekong_15min"
RUNOFF_1DEG = PKG / "inp" / "test_1deg" / "runoff"
REAL_NAM = Path(__file__).parent / "data" / "real_input_cmf.nam"
DOWNSCALE_EXE = PKG / "CaMa_Floodcast" / "bin" / "win64" / (
    "downscale_flddph_trib.exe" if sys.platform == "win32" else "downscale_flddph_trib")
MEKONG_PROJECT = PKG / "CaMa_Floodcast" / "projects" / "mekong"

needs_phl = pytest.mark.skipif(not (MAP_PHL / "params.txt").exists(),
                               reason="Philippines regional map not built")
needs_glb = pytest.mark.skipif(not (MAP_GLB / "params.txt").exists(),
                               reason="global map not built")
needs_runoff = pytest.mark.skipif(not RUNOFF_1DEG.exists(),
                                  reason="1-degree test runoff not available")
needs_downscale = pytest.mark.skipif(
    not (DOWNSCALE_EXE.exists() and (MAP_MEK / "1min" / "location.txt").exists()
        and (MEKONG_PROJECT / "project.json").exists()),
    reason="downscale tool, 1min sub-grid data, or the Mekong test project are not present")
needs_scenario = pytest.mark.skipif(
    not (MEKONG_PROJECT / "cases" / "no_bifurcation" / "run.json").exists(),
    reason="no_bifurcation scenario case not built (examples/make_mekong_scenario.py)")
needs_dams = pytest.mark.skipif(
    not (MEKONG_PROJECT / "cases" / "with_dams" / "run.json").exists(),
    reason="with_dams scenario case not built (examples/make_mekong_dams.py)")
needs_levee = pytest.mark.skipif(
    not (MEKONG_PROJECT / "cases" / "with_levee" / "run.json").exists(),
    reason="with_levee scenario case not built (examples/make_mekong_levee.py)")


def wait_for(signal, timeout_ms: int = 20_000) -> bool:
    """Spin the event loop until a signal fires (or the timeout elapses).

    The domain/forcing views do their work on a ``QThread``; this is how a test observes
    the result without a real display or a sleep-and-poll loop.
    """
    loop = QEventLoop()
    fired = {"ok": False}

    def done(*_a):
        fired["ok"] = True
        loop.quit()

    signal.connect(done)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return fired["ok"]


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture
def window(qapp):
    from camafloodcast.ui.main_window import MainWindow

    w = MainWindow()
    # Offscreen has nothing to dismiss a modal dialog, so a confirmation prompt would block
    # the event loop forever. Suppress them for the duration of the test.
    w.interactive = False
    yield w
    w.close()


class TestShell:
    def test_window_builds(self, window):
        assert window.windowTitle().startswith("CaMa_Floodcast")
        assert window.tabs.count() == 9

    def test_tabs_present(self, window):
        names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        assert names == ["Project", "Domain", "Forcing", "Model Setup", "Physics",
                         "Run Manager", "Map && Results", "Analysis", "Downscaling"]

    def test_run_disabled_without_a_case(self, window):
        assert not window.btn_run.isEnabled()

    def test_workflow_strip_starts_empty(self, window):
        assert len(window.workflow.labels) == 5
        assert all("○" in l.text() for l in window.workflow.labels)


class TestSetupView:
    def test_form_covers_the_whole_schema(self, window):
        from camafloodcast.core import schema

        expected = sum(len(g.fields) for g in schema.ALL_GROUPS)
        assert len(window.setup._editors) == expected

    def test_advanced_fields_hidden_by_default(self, window):
        assert not window.setup.chk_advanced.isChecked()

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_loads_a_real_namelist(self, window, tmp_path):
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=Namelist.load(REAL_NAM))
        window.set_project(proj)
        window.select_case(case)

        assert window.setup.namelist is not None
        assert window.setup._editors[("NDIMTIME", "DT")].value() == 3600
        assert window.setup._editors[("NRUNVER", "LADPSTP")].value() is True
        assert not window.setup.dirty, "loading a case must not mark it dirty"

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_editing_marks_dirty_and_does_not_rewrite_the_file(self, window, tmp_path):
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=Namelist.load(REAL_NAM))
        window.set_project(proj)
        window.select_case(case)

        before = case.namelist_path.read_text(encoding="utf-8")
        window.setup._editors[("NDIMTIME", "DT")].set_value(1800)
        window.setup._field_changed("NDIMTIME", "DT")

        assert window.setup.dirty
        assert case.namelist_path.read_text(encoding="utf-8") == before, \
            "editing must not touch the file until Save"

        window.setup.save()
        assert not window.setup.dirty
        assert Namelist.load(case.namelist_path).get("NDIMTIME", "DT") == 1800
        assert list(case.dir.glob("*.bak")), "a backup must be written on save"

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_validation_reaches_the_pane(self, window, tmp_path):
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=Namelist.load(REAL_NAM))
        window.set_project(proj)
        window.select_case(case)

        window.setup._editors[("NDIMTIME", "DT")].set_value(3500)
        window.setup._field_changed("NDIMTIME", "DT")
        d = window.setup.validate()

        assert not d.ok
        assert window.validation.tree.topLevelItemCount() > 0
        assert "block" in window.validation.summary.text().lower()


class TestProjectView:
    def test_case_table_lists_cases(self, window, tmp_path):
        from camafloodcast.core.namelist import new_namelist

        proj = Project.create(tmp_path / "p")
        nml = new_namelist(["NDIMTIME"])
        nml.set("NDIMTIME", "DT", 3600)
        proj.add_case("baseline", namelist=nml)
        proj.add_case("scenario", namelist=nml)

        window.set_project(proj)
        assert window.project_view.table.rowCount() == 2

    def test_stale_case_shown_as_stale(self, window, tmp_path):
        from camafloodcast.core.namelist import new_namelist
        from camafloodcast.core.project import CaseStatus, RunRecord

        proj = Project.create(tmp_path / "p")
        nml = new_namelist(["NDIMTIME"])
        nml.set("NDIMTIME", "DT", 3600)
        case = proj.add_case("baseline", namelist=nml)
        case.save_record(RunRecord(status=CaseStatus.COMPLETED, input_hash=case.input_hash()))

        edited = case.namelist()
        edited.set("NDIMTIME", "DT", 900)
        edited.save(backup=False)

        window.set_project(proj)
        cell = window.project_view.table.item(0, 1)
        assert cell.text() == "stale"

    def test_sweep_button_disabled_until_a_case_is_selected(self, window, tmp_path):
        from camafloodcast.core.namelist import new_namelist

        proj = Project.create(tmp_path / "p")
        nml = new_namelist(["NPARAM"])
        nml.set("NPARAM", "PMANFLD", 0.30)
        proj.add_case("baseline", namelist=nml)

        window.set_project(proj)
        assert not window.project_view.btn_sweep.isEnabled()
        window.project_view.table.selectRow(0)
        assert window.project_view.btn_sweep.isEnabled()

    def test_new_sweep_builds_one_case_per_value(self, window, tmp_path):
        from camafloodcast.core.namelist import new_namelist
        from camafloodcast.ui.modules.project_view import NewSweepDialog

        proj = Project.create(tmp_path / "p")
        nml = new_namelist(["NPARAM", "NOUTPUT"])
        nml.set("NPARAM", "PMANFLD", 0.30)
        base = proj.add_case("baseline", namelist=nml)

        dlg = NewSweepDialog(base)
        dlg.cmb_group.setCurrentText("NPARAM")
        dlg.cmb_field.setCurrentText("PMANFLD")
        assert "Floodplain Manning" in dlg.lbl_doc.text()
        dlg.txt_values.setText("0.03, 0.05")
        dlg._accept()

        names = {c.name for c in dlg.new_cases}
        assert names == {"pmanfld_0p03", "pmanfld_0p05"}
        for c in dlg.new_cases:
            assert c.namelist().get("NOUTPUT", "COUTTAG") == c.name
        assert Namelist.load(proj.case("pmanfld_0p03").namelist_path).get(
            "NPARAM", "PMANFLD") == 0.03


class TestColorScaleEditor:
    def test_opens_on_the_requested_preset(self, qapp):
        from camafloodcast.ui.widgets.color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor()
        dlg.set_current("Difference (diverging)")
        assert dlg.preset_combo.currentText() == "Difference (diverging)"
        result = dlg.get_result()
        assert result["colors"][0] == "#053061"
        assert result["bounds"] == [-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0]

    def test_editing_a_bound_updates_the_result(self, qapp):
        from camafloodcast.ui.widgets.color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor()
        dlg.set_current("Floodplain depth (blue)")
        dlg._spins[0].setValue(0.5)
        assert dlg.get_result()["bounds"][0] == 0.5

    def test_editing_a_colour_updates_the_result(self, qapp):
        from camafloodcast.ui.widgets.color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor()
        dlg.set_current("Floodplain depth (blue)")
        dlg._on_color_changed(0, "#123456")
        assert dlg.get_result()["colors"][0] == "#123456"

    def test_add_and_remove_band(self, qapp):
        from camafloodcast.ui.widgets.color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor()
        dlg.set_current("Floodplain depth (blue)")
        n = len(dlg.get_result()["colors"])
        dlg._add_band()
        assert len(dlg.get_result()["colors"]) == n + 1
        dlg._remove_band()
        assert len(dlg.get_result()["colors"]) == n

    def test_cannot_remove_below_two_bands(self, qapp):
        from camafloodcast.ui.widgets.color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor()
        dlg._set_bands(["#000000", "#ffffff"], [0.0, 1.0])
        dlg._remove_band()
        assert len(dlg.get_result()["colors"]) == 2

    def test_get_spec_builds_a_discrete_colorspec(self, qapp):
        from camafloodcast.ui.widgets.color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor()
        dlg.set_current("Floodplain depth (blue)")
        spec = dlg.get_spec()
        assert spec.colors is not None and spec.bounds is not None
        assert spec.transparent_below == 1e-3
        assert spec.preset == "Floodplain depth (blue)"

    def test_save_preset_writes_to_the_project_cache_dir(self, qapp, tmp_path):
        from camafloodcast.ui.widgets.color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor(project_dir=tmp_path)
        dlg.interactive = False   # the success/error boxes would otherwise block offscreen
        dlg.set_current("Floodplain depth (blue)")
        dlg._save_preset()
        assert (tmp_path / "cache" / "color_presets.json").exists()


class TestLayerPanel:
    """The per-row LayerPanel (replacing the old single-shared-style-panel design) -- each
    row's controls must affect only its own layer."""

    def _layer(self, name="flddph", **kw):
        from camafloodcast.core.maps import Grid
        from camafloodcast.services.render import ColorSpec
        from camafloodcast.ui.map.renderer import MapLayer

        grid = Grid(nx=2, ny=2, nlfp=1, gsize=1.0, west=0, east=2, south=0, north=2)
        spec = kw.pop("spec", None) or ColorSpec.for_variable("flddph", 0.0, 5.0)
        return MapLayer(name=name, grid=grid, provider=lambda r: np.zeros((2, 2)),
                        spec=spec, **kw)

    def test_one_row_per_layer(self, qapp):
        from camafloodcast.ui.widgets.controls import LayerPanel

        panel = LayerPanel()
        panel.set_layers([self._layer("a"), self._layer("b")])
        assert len(panel._rows) == 2

    def test_visibility_toggle_only_affects_its_own_layer(self, qapp):
        from camafloodcast.ui.widgets.controls import LayerPanel

        panel = LayerPanel()
        a, b = self._layer("a"), self._layer("b")
        panel.set_layers([a, b])
        panel._rows["a"].chk_visible.setChecked(False)
        assert a.visible is False
        assert b.visible is True

    def test_opacity_slider_only_affects_its_own_layer(self, qapp):
        from camafloodcast.ui.widgets.controls import LayerPanel

        panel = LayerPanel()
        a, b = self._layer("a"), self._layer("b")
        panel.set_layers([a, b])
        panel._rows["a"].opacity.setValue(40)
        assert a.spec.opacity == pytest.approx(0.4)
        assert b.spec.opacity != pytest.approx(0.4)

    def test_choosing_a_colormap_clears_any_discrete_preset(self, qapp):
        from camafloodcast.services.render import ColorSpec
        from camafloodcast.ui.widgets.controls import LayerPanel

        panel = LayerPanel()
        layer = self._layer(spec=ColorSpec.from_preset("Floodplain depth (blue)"))
        panel.set_layers([layer])
        panel._rows[layer.name].style_combo.setCurrentText("viridis")
        assert layer.spec.cmap == "viridis"
        assert layer.spec.colors is None
        assert layer.spec.preset is None

    def test_choosing_a_preset_switches_to_discrete_mode(self, qapp):
        from camafloodcast.ui.widgets.controls import LayerPanel

        panel = LayerPanel()
        layer = self._layer()
        panel.set_layers([layer])
        panel._rows[layer.name].style_combo.setCurrentText("Difference (diverging)")
        assert layer.spec.preset == "Difference (diverging)"
        assert layer.spec.colors is not None

    def test_move_reorders_layers(self, qapp):
        from camafloodcast.ui.widgets.controls import LayerPanel

        panel = LayerPanel()
        a, b = self._layer("a"), self._layer("b")
        panel.set_layers([a, b])
        panel._on_move("b", -1)
        assert [l.name for l in panel.layers] == ["b", "a"]

    def test_remove_drops_the_layer_and_its_row(self, qapp):
        from camafloodcast.ui.widgets.controls import LayerPanel

        panel = LayerPanel()
        a, b = self._layer("a"), self._layer("b")
        panel.set_layers([a, b])
        panel._on_remove("a")
        assert [l.name for l in panel.layers] == ["b"]
        assert "a" not in panel._rows

    def test_edit_button_applies_the_edited_spec(self, qapp, monkeypatch):
        from camafloodcast.ui.widgets import color_scale_editor
        from camafloodcast.ui.widgets.controls import LayerPanel

        monkeypatch.setattr(
            color_scale_editor.ColorScaleEditor, "exec",
            lambda self: color_scale_editor.QDialog.DialogCode.Accepted)

        panel = LayerPanel()
        layer = self._layer()
        panel.set_layers([layer])
        panel._on_edit(layer.name)
        assert layer.spec.colors is not None
        assert layer.spec.preset is not None


class TestNativeMapViewFrameCache:
    """The bounded render cache added to avoid recolouring+re-encoding on every scrub tick."""

    def _layer(self, name="v", **kw):
        from camafloodcast.core.maps import Grid
        from camafloodcast.services.render import ColorSpec
        from camafloodcast.ui.map.renderer import MapLayer

        grid = Grid(nx=2, ny=2, nlfp=1, gsize=1.0, west=0, east=2, south=0, north=2)
        spec = kw.pop("spec", None) or ColorSpec(vmin=0.0, vmax=1.0)
        provider = kw.pop("provider", lambda r: np.full((2, 2), 0.5))
        return MapLayer(name=name, grid=grid, provider=provider, spec=spec, **kw)

    def test_rendering_the_same_record_twice_hits_the_cache(self, qapp, monkeypatch):
        from camafloodcast.ui.map import native

        calls = {"n": 0}
        real_colorize = native.colorize

        def counting_colorize(*a, **kw):
            calls["n"] += 1
            return real_colorize(*a, **kw)

        monkeypatch.setattr(native, "colorize", counting_colorize)

        view = native.NativeMapView()
        view.set_layers([self._layer()])
        assert calls["n"] == 1
        view.refresh()   # same layer, same record, same style -- must reuse the cached pixmap
        assert calls["n"] == 1
        assert view.cache_stats["frames"] == 1

    def test_changing_record_adds_a_new_cache_entry(self, qapp):
        from camafloodcast.ui.map.native import NativeMapView

        view = NativeMapView()
        view.set_layers([self._layer(provider=lambda r: np.full((2, 2), float(r)))])
        view.set_record(1)
        assert view.cache_stats["frames"] == 2

    def test_set_layers_clears_stale_entries_from_the_previous_layer_set(self, qapp):
        from camafloodcast.ui.map.native import NativeMapView

        view = NativeMapView()
        view.set_layers([self._layer("a")])
        assert view.cache_stats["frames"] == 1
        view.set_layers([self._layer("b")])
        assert view.cache_stats["frames"] == 1

    def test_mutating_the_spec_in_place_renders_fresh_not_stale(self, qapp):
        from camafloodcast.ui.map.native import NativeMapView

        layer = self._layer()
        view = NativeMapView()
        view.set_layers([layer])
        assert view.cache_stats["frames"] == 1
        layer.spec.opacity = 0.9   # no set_layers() call -- refresh() alone must not go stale
        view.refresh()
        assert view.cache_stats["frames"] == 2

    def test_cache_evicts_the_oldest_entry_past_its_byte_budget(self, qapp):
        from camafloodcast.ui.map.native import _FrameCache
        from camafloodcast.ui.map.native import array_to_pixmap

        pm = array_to_pixmap(np.zeros((4, 4, 4), dtype=np.uint8))
        cache = _FrameCache(limit_bytes=_FrameCache._size_of(pm))   # room for exactly one frame
        cache.put("a", pm)
        cache.put("b", pm)
        assert cache.get("a") is None
        assert cache.get("b") is not None

    def test_make_map_view_returns_native_when_leaflet_is_reported_unavailable(
            self, qapp, monkeypatch):
        import camafloodcast.ui.map.leaflet as leaflet_mod
        from camafloodcast.ui.map import make_map_view
        from camafloodcast.ui.map.native import NativeMapView

        monkeypatch.setattr(leaflet_mod, "HAS_LEAFLET_MAP", False)
        assert isinstance(make_map_view(), NativeMapView)

    def test_make_map_view_returns_leaflet_when_available(self, qapp):
        from camafloodcast.ui.map import make_map_view
        from camafloodcast.ui.map.leaflet import HAS_LEAFLET_MAP

        if not HAS_LEAFLET_MAP:
            pytest.skip("PyQt6-WebEngine not available in this environment")
        from camafloodcast.ui.map.leaflet import LeafletMapView

        assert isinstance(make_map_view(), LeafletMapView)

    def test_layer_at_index_zero_paints_on_top(self, qapp):
        """Regression: index 0 (the LayerPanel's "top of the stack" -- add_layer() inserts
        there, and the Up button moves toward it) must have the highest zValue, not the
        lowest. A newly-added variable landing underneath the base layer's own ~90%-opaque
        fill is exactly the failure this guards against."""
        from camafloodcast.ui.map.native import NativeMapView

        view = NativeMapView()
        view.set_layers([self._layer("top"), self._layer("bottom")])
        assert view._items["top"].zValue() > view._items["bottom"].zValue()


from camafloodcast.ui.map.leaflet import HAS_LEAFLET_MAP  # noqa: E402

needs_leaflet = pytest.mark.skipif(
    not HAS_LEAFLET_MAP, reason="PyQt6-WebEngine not available in this environment")


@needs_leaflet
class TestLeafletMapView:
    """LeafletMapView: the same MapLayer/ColorSpec-driven contract as NativeMapView (see
    TestNativeMapViewFrameCache), with a real basemap. These check the Python-side wiring --
    generated JS strings, the pending-call queue, probe()'s pure-Python math -- not that
    Chromium actually painted a tile, which the screenshot-based verification covers."""

    def _layer(self, name="v", **kw):
        from camafloodcast.core.maps import Grid
        from camafloodcast.services.render import ColorSpec
        from camafloodcast.ui.map.renderer import MapLayer

        grid = Grid(nx=2, ny=2, nlfp=1, gsize=1.0,
                   west=0.0, east=2.0, south=0.0, north=2.0)
        spec = kw.pop("spec", None) or ColorSpec(vmin=0.0, vmax=1.0)
        provider = kw.pop("provider", lambda r: np.full((2, 2), 0.5))
        return MapLayer(name=name, grid=grid, provider=provider, spec=spec, **kw)

    def test_construction_does_not_raise(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        assert view.layers == []
        assert view.record == 0

    def test_pending_js_queue_flushes_after_page_loads(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        view._page_ready = False
        view.run_js("theTestCall();")
        assert view._pending_js == ["theTestCall();"]

        # Replace the page accessor itself (not just a live QWebEnginePage's method) so this
        # checks the queue/flush mechanism in isolation from the real Chromium-backed page --
        # observed flaky under pytest's default output capturing otherwise, for reasons that
        # trace to that capturing rather than to this queue logic.
        executed = []

        class _FakePage:
            def runJavaScript(self, code):
                executed.append(code)

        view.web_view.page = lambda: _FakePage()
        view._on_page_loaded(True)
        assert executed == ["theTestCall();"]
        assert view._pending_js == []

    def test_failed_load_drops_pending_calls_instead_of_hanging_onto_them(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        view._page_ready = False
        view.run_js("neverRuns();")
        view._on_page_loaded(False)
        assert view._pending_js == []
        assert view._page_ready is False

    def test_set_layers_populates_the_frame_cache(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        view.set_layers([self._layer()])
        assert view.cache_stats["frames"] == 1

    def test_probe_matches_the_value_at_the_clicked_cell(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        layer = self._layer(provider=lambda r: np.array([[1.0, 2.0], [3.0, 4.0]]))
        view.set_layers([layer])
        # grid: west=0,east=2,south=0,north=2,gsize=1 -> cell (0,0) is the NW quadrant
        rows = view.probe(0.5, 1.5)
        assert rows == [("v", "1.00")]

    def test_layer_zero_gets_a_higher_z_index_than_layer_one(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        calls = []
        view = LeafletMapView()
        view.run_js = calls.append
        view.set_layers([self._layer("top"), self._layer("bottom")])

        def z_index_for(name):
            call = next(c for c in calls if f"setLayerImage('{name}'" in c)
            return int(call.split(",")[-2])

        assert z_index_for("top") > z_index_for("bottom")

    def test_zoom_to_calls_fit_bounds_with_the_given_extent(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        calls = []
        view = LeafletMapView()
        view.run_js = calls.append
        view.zoom_to(west=100.0, south=10.0, east=101.0, north=11.0)
        assert calls == ["fitBounds(10.0,100.0,11.0,101.0);"]

    def test_zoom_to_layers_uses_the_top_layers_grid_bounds(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        calls = []
        view = LeafletMapView()
        view.run_js = calls.append
        view.set_layers([self._layer()])
        calls.clear()
        view.zoom_to_layers()
        assert calls == ["fitBounds(0.0,0.0,2.0,2.0);"]

    def test_set_markers_and_domain_rect_do_not_raise(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        view.set_markers([(105.0, 12.0, "#ff0000", "Gauge")])
        view.set_domain_rect((100.0, 10.0, 101.0, 11.0))
        view.set_domain_rect(None)

    def test_cursor_moved_reorders_lat_lon_to_lon_lat(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        received = []
        view.cursorMoved.connect(lambda lon, lat: received.append((lon, lat)))
        view._on_cursor_moved(12.0, 105.0)   # bridge delivers (lat, lon)
        assert received == [(105.0, 12.0)]

    def test_cell_clicked_reports_the_correct_grid_cell(self, qapp):
        from camafloodcast.ui.map.leaflet import LeafletMapView

        view = LeafletMapView()
        view.set_layers([self._layer()])
        received = []
        view.cellClicked.connect(lambda lon, lat, ix, iy: received.append((lon, lat, ix, iy)))
        view._on_cell_clicked(1.5, 0.5)   # bridge delivers (lat, lon)
        assert received == [(0.5, 1.5, 0, 0)]


@needs_phl
class TestResultsView:
    def _prepared(self, window, tmp_path):
        assert REAL_NAM.exists()
        proj = Project.create(tmp_path / "p", map_dir=str(MAP_PHL))
        case = proj.add_case("c", namelist=Namelist.load(REAL_NAM))
        window.set_project(proj)
        window.select_case(case)
        return proj, case

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_handles_a_case_with_no_results(self, window, tmp_path):
        self._prepared(window, tmp_path)
        assert "no results" in window.results.resolution.text().lower()
        assert not window.results.btn_plot.isEnabled()

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_resolution_badge_states_the_grid(self, window, tmp_path):
        """The viewer must never let a 0.25 deg field pass as an inundation map."""
        proj, case = self._prepared(window, tmp_path)
        run = PKG / "etc" / "v430fix_tests" / "regional" / "out_phl_1deg"
        if not run.exists() or not list(run.glob("*.bin")):
            pytest.skip("no completed run to display")
        for f in run.glob("*.bin"):
            (case.dir / f.name).write_bytes(f.read_bytes())

        window.results.load(case, MAP_PHL)
        text = window.results.resolution.text()
        assert "0.25" in text and "not downscaled" in text
        assert window.results.var_list.count() > 0


needs_mek_run = pytest.mark.skipif(
    not (MEKONG_PROJECT / "cases" / "baseline" / "run.json").exists(),
    reason="Mekong baseline case not built (examples/make_mekong_project.py)")


@needs_mek_run
class TestResultsViewVariableNames:
    def test_variable_picker_and_layer_show_the_explicit_name_not_the_short_code(self, window):
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)
        window.select_case(proj.case("baseline"))

        i = window.results.var_list.findData("outflw")
        assert i >= 0
        # displayed text is the explicit name; the short code is only kept as the item's data
        assert window.results.var_list.itemText(i) != "outflw"
        assert "outflow" in window.results.var_list.itemText(i).lower()
        assert window.results.var_list.itemData(i) == "outflw"

        window.results.var_list.setCurrentIndex(i)
        window.results._add_layer()
        names = [layer.name for layer in window.results.layers.layers]
        assert not any(n.startswith("outflw ") for n in names)
        assert any("outflow" in n.lower() for n in names)


@needs_levee
class TestResultsViewLevee:
    """core.output's fix for the previously-invisible ``levdph`` variable (see
    core.analysis.levee_protection_stats), proven against a real levee-enabled run."""

    def test_levdph_is_discovered_and_plots_protected_side_overtopping_stats(self, window):
        from camafloodcast.core.analysis import Gauge
        from camafloodcast.core.maps import read_layer

        proj = Project.open(MEKONG_PROJECT)
        case = proj.case("with_levee")
        window.set_project(proj)
        window.select_case(case)

        assert "levdph" in window.results.outputs.variables

        grid = window.results.river_map.grid
        hgt_path = Path(str(case.namelist().get("NLEVEE", "CLEVHGT")))
        hgt = read_layer(hgt_path, grid, layer="levhgt")
        iy, ix = np.unravel_index(np.argmax(hgt), hgt.shape)

        window.results.var_list.setCurrentIndex(window.results.var_list.findData("levdph"))
        window.results.gauges = [Gauge("leveed cell", 0.0, 0.0, ix=int(ix), iy=int(iy))]
        window.results._plot()

        assert "overtopped" in window.results.notes.text()


class TestAdapterBoundary:
    def test_window_uses_the_adapter_not_camaflood_directly(self, window):
        from camafloodcast.adapters.base import ModelAdapter

        assert isinstance(window.adapter, ModelAdapter)

    def test_adapter_validate_is_what_gates_a_run(self, window, tmp_path):
        """Gate 3 must go through the adapter, so a second model can reuse the shell."""
        from camafloodcast.core.namelist import new_namelist

        proj = Project.create(tmp_path / "p")
        nml = new_namelist(["NDIMTIME", "NRUNVER"])
        nml.set("NDIMTIME", "DT", 3500)           # not a multiple of 60
        case = proj.add_case("bad", namelist=nml)
        d = window.adapter.validate(case.namelist(), base_dir=case.dir)
        assert not d.ok


class TestDomainView:
    """Phase 2: the domain builder tab, and Gate 1 (closure)."""

    def test_tab_present_before_any_project(self, window):
        assert window.tabs.tabText(1) == "Domain"
        assert window.domain_view.maps_root is None
        assert window.domain_view.tools is None

    @needs_glb
    def test_active_map_offered_as_a_cut_source_without_copying_it(self, window, tmp_path):
        """The project's active map (here 8+ GB, real, on disk elsewhere) must be usable as a
        cut source in place -- copying it into the project would be absurd."""
        proj = Project.create(tmp_path / "p", map_dir=str(MAP_GLB))
        window.set_project(proj)

        assert window.domain_view.maps_root == proj.root / "domain"
        assert window.domain_view.cmb_source.count() >= 1
        assert window.domain_view.source is not None
        assert window.domain_view.source.dir == MAP_GLB

    @needs_glb
    def test_bundled_tools_are_found_automatically(self, window, tmp_path):
        """bin/win64 ships beside the application; the domain builder must find it without the
        user pointing at it, the same way the model binary is auto-guessed."""
        proj = Project.create(tmp_path / "p", map_dir=str(MAP_GLB))
        window.set_project(proj)

        assert window.domain_view.tools is not None
        assert not window.domain_view.tools.missing(), \
            f"missing tools: {window.domain_view.tools.missing()}"
        assert window.domain_view.btn_build.isEnabled()

    @needs_phl
    def test_show_domain_renders_gate1_on_a_real_regional_map(self, window, tmp_path):
        """Exercises the closure gate end-to-end against an already-cut real map, without
        running the (slow) cut chain itself -- that path is covered in test_core.py."""
        proj = Project.create(tmp_path / "p", map_dir=str(MAP_PHL))
        window.set_project(proj)

        received = []
        window.domain_view.validationChanged.connect(received.append)
        window.domain_view.show_domain(MAP_PHL)

        assert window.domain_view.report is not None
        assert window.domain_view.tbl.rowCount() > 0
        assert window.domain_view.lbl_gate.text().startswith(("PASS", "BLOCKED"))
        assert received, "Gate 1 must reach the shared validation dock"


class TestForcingView:
    """Phase 2: the runoff forcing tab, and Gate 2 (coverage)."""

    def test_tab_present(self, window):
        assert window.tabs.tabText(2) == "Forcing"
        assert not window.forcing_view.btn_apply.isEnabled()

    @needs_runoff
    def test_scan_passes_gate2_on_matching_real_data(self, window):
        """The real 1-degree test forcing, with the defaults the tab starts with (360x180,
        Roff____*.one, mm/day), is exactly what the integration test runs the model against --
        Gate 2 must pass it."""
        fv = window.forcing_view
        fv.txt_dir.setText(str(RUNOFF_1DEG))
        fv.set_period(dt.date(2000, 1, 1), dt.date(2000, 1, 5))

        received = []
        fv.validationChanged.connect(received.append)
        fv._scan_now()
        assert wait_for(fv.validationChanged), "Gate 2 never reported back"

        assert received
        diags = received[-1]
        assert diags.ok, f"Gate 2 should pass on real, matching test data: " \
                         f"{[str(d) for d in diags]}"
        assert fv.lbl_gate.text().startswith("PASS")
        assert fv.report is not None and fv.report.n_files > 0

    @needs_runoff
    def test_scan_blocks_on_a_period_the_data_does_not_cover(self, window):
        """CaMa-Flood does not stop for a missing forcing day -- it produces a plausible but
        wrong recession. Gate 2 exists to catch that before the run, not after."""
        fv = window.forcing_view
        fv.txt_dir.setText(str(RUNOFF_1DEG))
        fv.set_period(dt.date(1999, 1, 1), dt.date(1999, 1, 5))   # entirely before the data

        received = []
        fv.validationChanged.connect(received.append)
        fv._scan_now()
        assert wait_for(fv.validationChanged)

        assert received and not received[-1].ok
        assert any(d.code == "FRC_COVERAGE" for d in received[-1])
        assert fv.lbl_gate.text().startswith("BLOCKED")

    @needs_runoff
    def test_load_case_prefills_from_the_namelist(self, window, tmp_path):
        from camafloodcast.core.namelist import new_namelist

        nml = new_namelist(["NFORCE", "NSIMTIME"])
        nml.update("NFORCE", {"CROFDIR": str(RUNOFF_1DEG) + "/", "CROFPRE": "Roff____",
                              "CROFSUF": ".one", "DROFUNIT": 86_400_000.0})
        nml.update("NSIMTIME", {"SYEAR": 2000, "SMON": 1, "SDAY": 1,
                                "EYEAR": 2000, "EMON": 1, "EDAY": 6})

        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=nml)
        window.set_project(proj)
        window.select_case(case)

        fv = window.forcing_view
        assert Path(fv.txt_dir.text()) == RUNOFF_1DEG
        assert fv.cmb_unit.currentText() == "mm/day"
        assert fv.dt_start.date().toPyDate() == dt.date(2000, 1, 1)
        assert fv.dt_end.date().toPyDate() == dt.date(2000, 1, 6)
        assert fv.btn_apply.isEnabled()

    def test_download_disabled_without_credentials(self, window, tmp_path, monkeypatch):
        from pathlib import Path as _Path

        monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))
        fv = window.forcing_view
        fv.cmb_download_source.setCurrentIndex(0)          # ERA5-Land
        fv._update_download_creds()
        assert not fv.btn_download.isEnabled()
        assert ".cdsapirc" in fv.lbl_download_creds.text()

        fv.cmb_download_source.setCurrentIndex(1)          # GLDAS
        fv._update_download_creds()
        assert not fv.btn_download.isEnabled()
        assert ".netrc" in fv.lbl_download_creds.text()

    @needs_phl
    def test_download_enabled_with_credentials_and_a_grid(self, window, tmp_path, monkeypatch):
        from pathlib import Path as _Path

        (tmp_path / ".cdsapirc").write_text("url: https://example\nkey: 1\n")
        monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))

        proj = Project.create(tmp_path / "p", map_dir=str(MAP_PHL))
        window.set_project(proj)

        fv = window.forcing_view
        fv.cmb_download_source.setCurrentIndex(0)          # ERA5-Land
        fv._update_download_creds()
        assert fv.grid is not None
        assert fv.btn_download.isEnabled()

    @needs_phl
    def test_download_and_convert_flows_into_the_existing_import_handler(
            self, window, tmp_path, monkeypatch):
        """No real network call: era5_land_download/era5_land_daily_totals are replaced with
        fakes (the second one writes a real, tiny, known-value daily netCDF), so the real
        netcdf_to_binary conversion downstream is genuinely exercised end to end -- proving the
        GUI wiring reuses the exact same success/failure handlers a manual netCDF import gets."""
        from pathlib import Path as _Path

        import camafloodcast.core.forcing_download as fd_module

        (tmp_path / ".cdsapirc").write_text("url: https://example\nkey: 1\n")
        monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))

        proj = Project.create(tmp_path / "p", map_dir=str(MAP_PHL))
        window.set_project(proj)
        fv = window.forcing_view
        fv.cmb_download_source.setCurrentIndex(0)
        fv.set_period(dt.date(2000, 1, 1), dt.date(2000, 1, 2))
        fv.open_download_dialog()          # syncs dt_dl_start/end from the tab's own period

        def fake_download(request, out_dir, *, progress=None):
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            return out / "raw.nc"

        def fake_daily_totals(raw_path, out_path):
            grid = fv.grid
            nc = pytest.importorskip("netCDF4")
            lat = np.linspace(grid.north - grid.gsize / 2, grid.south + grid.gsize / 2, grid.ny)
            lon = np.linspace(grid.west + grid.gsize / 2, grid.east - grid.gsize / 2, grid.nx)
            data = np.full((2, grid.ny, grid.nx), 5.0, dtype="f4")   # 5 mm/day everywhere
            fd_module._write_daily_netcdf(
                Path(out_path), data, lat, lon, [dt.date(2000, 1, 1), dt.date(2000, 1, 2)])
            return 2

        monkeypatch.setattr(fd_module, "era5_land_download", fake_download)
        monkeypatch.setattr(fd_module, "era5_land_daily_totals", fake_daily_totals)

        outdir = tmp_path / "forcing_out"
        fv.txt_dl_outdir.setText(str(outdir))
        assert fv.chk_dl_convert.isChecked()          # default: convert after download

        statuses = []
        fv.statusMessage.connect(statuses.append)
        fv._download_forcing()
        assert fv._download is not None
        assert wait_for(fv._download.done, timeout_ms=30_000), "download did not complete in time"

        assert Path(fv.txt_dir.text()) == outdir
        assert fv.cmb_unit.currentText() == "mm/day"
        assert any("download step 3/3" in s for s in statuses)

    def test_download_dialog_domain_fields_auto_fill_from_the_project(self, window, tmp_path):
        """The West/East/South/North/Resolution fields should reflect the real project grid
        without the user having to click anything, and 'Fit to domain' resets them on demand."""
        proj = Project.create(tmp_path / "p", map_dir=str(MAP_PHL))
        window.set_project(proj)
        fv = window.forcing_view

        assert fv.spn_dl["west"].value() == pytest.approx(fv.grid.west)
        assert fv.spn_dl["east"].value() == pytest.approx(fv.grid.east)
        assert fv.spn_dl["south"].value() == pytest.approx(fv.grid.south)
        assert fv.spn_dl["north"].value() == pytest.approx(fv.grid.north)
        assert fv.spn_dl_gsize.value() == pytest.approx(fv.grid.gsize)

        fv.spn_dl["west"].setValue(0.0)          # simulate a manual edit away from the grid
        fv._fit_download_domain()
        assert fv.spn_dl["west"].value() == pytest.approx(fv.grid.west)

    def test_gee_project_row_visibility_follows_the_source_combo(self, window):
        # The window/dialog are never shown in this offscreen fixture, so isVisible() would be
        # False regardless of this widget's own state -- isHidden() checks the widget itself,
        # same reasoning as the downscale period chart's own isHidden()-not-isVisible() check.
        fv = window.forcing_view
        fv.cmb_download_source.setCurrentIndex(0)          # ERA5-Land (CDS)
        assert fv.w_gee_project.isHidden()
        fv.cmb_download_source.setCurrentIndex(2)          # ERA5-Land (Earth Engine)
        assert not fv.w_gee_project.isHidden()
        fv.cmb_download_source.setCurrentIndex(3)          # GLDAS (Earth Engine)
        assert not fv.w_gee_project.isHidden()
        fv.cmb_download_source.setCurrentIndex(1)          # GLDAS (GES DISC)
        assert fv.w_gee_project.isHidden()

    def test_save_gee_project_persists_and_updates_credentials_label(
            self, window, tmp_path, monkeypatch):
        from pathlib import Path as _Path

        monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".config" / "earthengine").mkdir(parents=True)
        (tmp_path / ".config" / "earthengine" / "credentials").write_text("{}")

        fv = window.forcing_view
        fv.cmb_download_source.setCurrentIndex(2)          # ERA5-Land (Earth Engine)
        assert "no project id" in fv.lbl_download_creds.text().lower()

        fv.txt_gee_project.setText("my-test-project")
        fv._save_gee_project()

        from camafloodcast.core.forcing_download import get_gee_project

        assert get_gee_project(home=tmp_path) == "my-test-project"
        assert fv.lbl_download_creds.text() == "Credentials found."

    @needs_phl
    def test_convert_checkbox_unchecked_skips_conversion(self, window, tmp_path, monkeypatch):
        """With the checkbox off, the daily netCDF itself is the result -- the tab's own
        Directory field must stay untouched, and the status/dialog should say a file was saved
        instead of claiming a binary forcing directory is ready."""
        from pathlib import Path as _Path

        import camafloodcast.core.forcing_download as fd_module

        (tmp_path / ".cdsapirc").write_text("url: https://example\nkey: 1\n")
        monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))

        proj = Project.create(tmp_path / "p", map_dir=str(MAP_PHL))
        window.set_project(proj)
        fv = window.forcing_view
        fv.cmb_download_source.setCurrentIndex(0)
        fv.chk_dl_convert.setChecked(False)

        saved_path = tmp_path / "raw_daily.nc"

        def fake_download(request, out_dir, *, progress=None):
            return Path(out_dir) / "raw.nc"

        def fake_daily_totals(raw_path, out_path):
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"not a real netcdf, never opened by this test")
            saved_path.write_bytes(b"marker")
            return 1

        monkeypatch.setattr(fd_module, "era5_land_download", fake_download)
        monkeypatch.setattr(fd_module, "era5_land_daily_totals", fake_daily_totals)

        outdir = tmp_path / "forcing_out"
        fv.txt_dl_outdir.setText(str(outdir))
        original_dir_text = fv.txt_dir.text()

        results = []
        fv._download = None
        fv.statusMessage.connect(lambda s: results.append(s))
        fv._download_forcing()
        assert fv._download is not None
        assert wait_for(fv._download.done, timeout_ms=30_000)

        assert fv.txt_dir.text() == original_dir_text      # untouched -- no conversion ran
        assert any("saved daily-total netcdf" in s.lower() for s in results)

    def test_download_forcing_menu_action_opens_the_dialog(self, window):
        """The Forcing menu's Download forcing... action (moved out of the tab's own panel so
        it's reachable without navigating there first) opens the same dialog the tab's download
        button lives in."""
        fv = window.forcing_view
        assert not fv._download_dialog.isVisible()

        window.act_download_forcing.trigger()

        assert fv._download_dialog.isVisible()
        assert fv.cmb_download_source.parent() is fv._download_dialog
        assert fv.btn_download.parent() is fv._download_dialog


class TestDomainViewRoughness:
    """Phase 2 tail: land cover -> CFLDMAN."""

    def test_disabled_before_a_domain_is_open(self, window):
        dv = window.domain_view
        assert not dv.txt_landclass.isEnabled()
        assert not dv.btn_lc_write.isEnabled()
        assert not dv.btn_lc_apply.isEnabled()

    @needs_phl
    def test_write_and_apply_cfldman(self, window, tmp_path):
        """End to end: a synthetic all-urban land-cover raster produces a uniform 0.08 Manning
        map, gets written as fldman.bin, and 'Apply to case' points CFLDMAN at it."""
        import shutil

        from camafloodcast.core.namelist import new_namelist

        # Work on a private copy -- writing fldman.bin must never touch the real, shared
        # package map directory that other tests also read.
        local_map = tmp_path / "phl_15min"
        shutil.copytree(MAP_PHL, local_map)

        proj = Project.create(tmp_path / "p", map_dir=str(local_map))
        window.set_project(proj)
        dv = window.domain_view
        dv.show_domain(local_map)
        assert dv.active_map is not None
        assert dv.btn_lc_write.isEnabled()

        grid = dv.active_map.grid
        classes = np.full((grid.ny, grid.nx), 13, dtype="<i4")   # 13 = IGBP urban
        lc_path = tmp_path / "landclass.bin"
        classes.tofile(lc_path)
        dv.txt_landclass.setText(str(lc_path))

        dv._preview_roughness()
        assert dv._fldman is not None
        assert np.allclose(dv._fldman[dv.active_map.land_mask], 0.080)

        dv._write_roughness()
        out = local_map / "fldman.bin"
        assert out.exists()
        written = np.fromfile(out, dtype="<f4").reshape(grid.ny, grid.nx)
        assert np.allclose(written[dv.active_map.land_mask], 0.080)

        nml = new_namelist(["NMAP"])
        nml.set("NMAP", "CNEXTXY", "./nextxy.bin")   # a real namelist always has NMAP populated
        case = proj.add_case("c", namelist=nml)
        window.select_case(case)
        assert dv.case is case
        assert dv.btn_lc_apply.isEnabled()

        received = []
        dv.caseUpdated.connect(received.append)
        dv._apply_roughness_to_case()
        assert received == [case]
        assert Namelist.load(case.namelist_path).get("NMAP", "CFLDMAN") == str(out)

    @needs_phl
    def test_write_and_apply_cfldman_from_a_geotiff(self, window, tmp_path):
        """core.gis wired into the roughness pipeline: a real-world land-cover GeoTIFF in a
        projected CRS is reprojected onto the domain automatically -- see TestGeoTiffImport in
        test_core.py for the precise reprojection-math verification this builds on."""
        import shutil

        pytest.importorskip("rasterio")
        pytest.importorskip("pyproj")
        import rasterio
        from pyproj import Transformer
        from rasterio.transform import from_bounds

        local_map = tmp_path / "phl_15min"
        shutil.copytree(MAP_PHL, local_map)

        proj = Project.create(tmp_path / "p", map_dir=str(local_map))
        window.set_project(proj)
        dv = window.domain_view
        dv.show_domain(local_map)
        grid = dv.active_map.grid

        # UTM 51N roughly covers this domain; a bounding box sampled around the whole
        # rectangle (not just two corners), padded generously, keeps the reprojected coverage
        # safely larger than the domain despite meridian convergence over an 11x17 deg extent.
        # A single UTM zone would never really be used this way -- the point is to prove the
        # .tif branch and the reprojection wiring, which the core-level test already verified
        # precisely at a realistic (small) extent.
        to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
        xs, ys = [], []
        for lon in np.linspace(grid.west, grid.east, 6):
            for lat in np.linspace(grid.south, grid.north, 6):
                x, y = to_utm.transform(lon, lat)
                xs.append(x)
                ys.append(y)
        pad_x = (max(xs) - min(xs)) * 0.1
        pad_y = (max(ys) - min(ys)) * 0.1
        utm_w, utm_e = min(xs) - pad_x, max(xs) + pad_x
        utm_s, utm_n = min(ys) - pad_y, max(ys) + pad_y

        classes = np.full((grid.ny, grid.nx), 13, dtype="float64")   # 13 = IGBP urban
        tif_path = tmp_path / "landclass_utm.tif"
        transform = from_bounds(utm_w, utm_s, utm_e, utm_n, grid.nx, grid.ny)
        with rasterio.open(
            tif_path, "w", driver="GTiff", height=grid.ny, width=grid.nx,
            count=1, dtype="float64", crs="EPSG:32651", transform=transform, nodata=-9999.0,
        ) as dst:
            dst.write(classes, 1)

        dv.txt_landclass.setText(str(tif_path))
        dv._preview_roughness()
        assert dv._fldman is not None
        assert "EPSG:32651" in dv._reprojection_note
        assert "EPSG:32651" in dv.lbl_lc_summary.text()

        # A central sub-region, away from any edge-of-coverage effect, should read 0.080
        # exactly (the built-in default for IGBP urban).
        cy0, cy1 = grid.ny // 4, 3 * grid.ny // 4
        cx0, cx1 = grid.nx // 4, 3 * grid.nx // 4
        centre_mask = dv.active_map.land_mask[cy0:cy1, cx0:cx1]
        centre_vals = dv._fldman[cy0:cy1, cx0:cx1]
        assert np.allclose(centre_vals[centre_mask], 0.080)

    @needs_phl
    def test_tab_dialogs_do_not_block_when_the_window_is_non_interactive(self, window, tmp_path):
        """Regression: DomainView/ForcingView used to call QMessageBox directly, with no way
        to suppress it. Applying CFLDMAN to a case whose namelist file has gone missing (so
        ``case.namelist()`` raises) used to reach an unconditional ``QMessageBox.critical`` and
        hang forever in an offscreen host -- exactly the failure mode MainWindow's own
        ``interactive`` switch exists to prevent for itself. It must now cascade to both tabs.

        (Namelist.set on a group the file doesn't have used to be the trigger here, before it
        was changed to append the group instead of raising -- see test_core.py's
        test_set_on_a_group_absent_from_the_file_appends_it.)"""
        import shutil

        from camafloodcast.core.maps import RiverMap
        from camafloodcast.core.namelist import new_namelist

        assert window.interactive is False        # the fixture already set this
        assert window.domain_view.interactive is False
        assert window.forcing_view.interactive is False

        local_map = tmp_path / "phl_15min"
        shutil.copytree(MAP_PHL, local_map)

        dv = window.domain_view
        dv.active_map = RiverMap(local_map)
        # A real fldman.bin, so _apply_roughness_to_case gets past the "write it first" check
        # and reaches the namelist write that actually fails.
        (local_map / "fldman.bin").write_bytes(
            np.zeros(dv.active_map.grid.shape, dtype="<f4").tobytes())

        proj = Project.create(tmp_path / "p", map_dir=str(local_map))
        dv.case = proj.add_case("c", namelist=new_namelist(["NMAP"]))
        dv.case.namelist_path.unlink()          # case.namelist() must now raise

        statuses = []
        dv.statusMessage.connect(statuses.append)
        dv._apply_roughness_to_case()           # must return promptly, not open a modal
        assert any("Apply to case" in s for s in statuses)


class TestPhysicsView:
    """Phase 2 tail: the Physics Options tab.

    It deliberately does not hold its own ``Namelist`` -- it edits Model Setup's, through
    ``get_value``/``set_value``, so there is exactly one in-memory copy and one save/dirty
    cycle. Most of these tests exist to prove that sharing actually holds in both directions.
    """

    def test_tab_present(self, window):
        assert window.tabs.tabText(4) == "Physics"

    def test_disabled_before_a_case_is_loaded(self, window):
        pv = window.physics_view
        assert not pv.body.isEnabled()
        # The window is never shown in this offscreen fixture, so isVisible() is always False
        # regardless of the widget's own state -- isHidden() checks the widget itself.
        assert not pv.lbl_nocase.isHidden()

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_editing_here_writes_into_setups_namelist_not_a_copy(self, window, tmp_path):
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=Namelist.load(REAL_NAM))
        window.set_project(proj)
        window.select_case(case)

        pv = window.physics_view
        assert pv.body.isEnabled()

        ed = pv._bound[("NRUNVER", "LWBLOG")]
        new_value = not ed.value()
        ed.set_value(new_value)
        pv._on_edit("NRUNVER", "LWBLOG", ed)

        assert window.setup.namelist.get("NRUNVER", "LWBLOG") == new_value
        assert window.setup.dirty
        # Must not spam a backup per toggle -- only an explicit Save writes the file.
        assert not list(case.dir.glob("*.bak"))
        # And Model Setup's own generic editor shows the same value: one namelist, not two.
        assert window.setup._editors[("NRUNVER", "LWBLOG")].value() == new_value

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_editing_in_model_setup_is_reflected_here(self, window, tmp_path):
        """The reverse direction: editing the generic Model Setup tab directly must not leave
        Physics Options showing a stale value."""
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=Namelist.load(REAL_NAM))
        window.set_project(proj)
        window.select_case(case)

        ed = window.setup._editors[("NRUNVER", "LFLDOUT")]
        new_value = not ed.value()
        ed.set_value(new_value)
        window.setup._field_changed("NRUNVER", "LFLDOUT")

        assert window.physics_view._bound[("NRUNVER", "LFLDOUT")].value() == new_value

    @pytest.mark.skipif(not REAL_NAM.exists(), reason="no captured namelist")
    def test_dependent_fields_grey_out_with_their_switch(self, window, tmp_path):
        proj = Project.create(tmp_path / "p")
        nml = Namelist.load(REAL_NAM)
        nml.set("NRUNVER", "LPTHOUT", False)
        nml.set("NRUNVER", "LLEVEE", False)
        case = proj.add_case("c", namelist=nml)
        window.set_project(proj)
        window.select_case(case)

        pv = window.physics_view
        assert not pv._bound[("NMAP", "CPTHOUT")].isEnabled()
        assert not pv._bound[("NLEVEE", "CLEVHGT")].isEnabled()

        pth = pv._bound[("NRUNVER", "LPTHOUT")]
        pth.set_value(True)
        pv._on_edit("NRUNVER", "LPTHOUT", pth)
        assert pv._bound[("NMAP", "CPTHOUT")].isEnabled()

    @needs_phl
    def test_bifurcation_stats_from_a_real_bifprm(self, window, tmp_path):
        from camafloodcast.core.namelist import new_namelist

        nml = new_namelist(["NRUNVER", "NMAP"])
        nml.set("NRUNVER", "LPTHOUT", True)
        nml.set("NMAP", "CPTHOUT", str(MAP_PHL / "bifprm.txt"))
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=nml)
        window.set_project(proj)
        window.select_case(case)

        pv = window.physics_view
        pv._refresh_bifurcation_stats()
        assert "11" in pv.lbl_bif_stats.text()
        assert len(pv.bif_map._markers) == 11

    @needs_phl
    def test_levee_stats_from_synthetic_maps_on_the_real_grid(self, window, tmp_path):
        from camafloodcast.core.maps import read_params
        from camafloodcast.core.namelist import new_namelist

        grid = read_params(MAP_PHL / "params.txt")
        hgt_path, frc_path = tmp_path / "levhgt.bin", tmp_path / "levfrc.bin"
        np.full((grid.ny, grid.nx), 2.5, dtype="<f4").tofile(hgt_path)
        np.full((grid.ny, grid.nx), 0.3, dtype="<f4").tofile(frc_path)

        nml = new_namelist(["NRUNVER", "NMAP", "NLEVEE"])
        nml.set("NRUNVER", "LLEVEE", True)
        nml.set("NMAP", "CNEXTXY", str(MAP_PHL / "nextxy.bin"))
        nml.set("NLEVEE", "CLEVHGT", str(hgt_path))
        nml.set("NLEVEE", "CLEVFRC", str(frc_path))
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=nml)
        window.set_project(proj)
        window.select_case(case)

        pv = window.physics_view
        pv._refresh_levee_stats()
        text = pv.lbl_lev_stats.text()
        assert "2.50" in text
        assert "0.30" in text

    def test_dam_table_disabled_and_empty_when_ldamout_is_off(self, window):
        pv = window.physics_view
        # No case loaded at all -- the table must not show stale data from a previous test's
        # widget state (module-level widgets would carry over if this weren't reset per case).
        assert pv.tbl_dams.rowCount() == 0
        assert not pv._bound[("NDAMOUT", "CDAMFILE")].isEnabled()

    def test_dam_table_reads_a_synthetic_cdamfile(self, window, tmp_path):
        from camafloodcast.core.dams import DamRecord, write_dam_file
        from camafloodcast.core.namelist import new_namelist

        dam_path = tmp_path / "dam_params.csv"
        write_dam_file([
            DamRecord(dam_id=1, name="Synthetic Dam", lat=27.0, lon=99.0, upreal_km2=80000.0,
                     ix=1, iy=1, fldvol_mcm=70.0, convol_mcm=120.0, totvol_mcm=190.0,
                     qn=400.0, qf=850.0),
        ], dam_path)

        nml = new_namelist(["NRUNVER", "NDAMOUT"])
        nml.set("NRUNVER", "LDAMOUT", True)
        nml.set("NDAMOUT", "CDAMFILE", str(dam_path))
        proj = Project.create(tmp_path / "p")
        case = proj.add_case("c", namelist=nml)
        window.set_project(proj)
        window.select_case(case)

        pv = window.physics_view
        assert pv._bound[("NDAMOUT", "CDAMFILE")].isEnabled()
        assert pv.tbl_dams.rowCount() == 1
        assert pv.tbl_dams.item(0, 0).text() == "Synthetic Dam"
        assert "190.0" in pv.tbl_dams.item(0, 3).text()
        assert "1 dam" in pv.lbl_dam_stats.text()

    @needs_dams
    def test_dam_table_reads_the_real_lancang_dam_file(self, window):
        """The 5 real Lancang dams (Wunonglong, Lidi, Huangdeng, Dahuaqiao, Miaowei), built by
        examples/make_mekong_dams.py, allocated by real drainage-area snapping and given
        Qn/Qf measured from the real baseline run."""
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)
        window.select_case(proj.case("with_dams"))

        pv = window.physics_view
        assert pv.tbl_dams.rowCount() == 5
        names = {pv.tbl_dams.item(r, 0).text() for r in range(5)}
        assert names == {"Wunonglong", "Lidi", "Huangdeng", "Dahuaqiao", "Miaowei"}
        assert "5 dam" in pv.lbl_dam_stats.text()

    def test_open_roughness_builder_switches_tab(self, window):
        window.physics_view.openRoughnessBuilder.emit()
        assert window.tabs.currentWidget() is window.domain_view
        assert window.domain_view.right.currentIndex() == 1


class TestHydrographChart:
    """A second y-axis (series2/unit2) for two quantities with different units and magnitudes
    -- added for the Downscaling tab's period chart (area vs. volume), but generic to the
    widget."""

    def test_series2_plots_on_a_second_axis_and_merges_the_legend(self, qapp):
        from camafloodcast.ui.widgets.charts import HydrographChart

        chart = HydrographChart()
        chart.plot([("inundated area", np.array([1.0, 2.0, 3.0]))], unit="km2",
                  series2=[("inundated volume", np.array([10.0, 20.0, 30.0]))], unit2="Mm3")

        assert len(chart.canvas.figure.axes) == 2
        assert chart._ax2 is not None
        legend_labels = [t.get_text() for t in chart.ax.get_legend().get_texts()]
        assert legend_labels == ["inundated area", "inundated volume"]

        # A second plot() call must not stack a third axis onto the figure.
        chart.plot([("inundated area", np.array([1.0, 2.0]))], unit="km2",
                  series2=[("inundated volume", np.array([10.0, 20.0]))], unit2="Mm3")
        assert len(chart.canvas.figure.axes) == 2

    def test_export_csv_includes_both_axes(self, qapp, tmp_path):
        from camafloodcast.ui.widgets.charts import HydrographChart

        chart = HydrographChart()
        chart.plot([("inundated area", np.array([1.0, 2.0]))], unit="km2",
                  series2=[("inundated volume", np.array([10.0, 20.0]))], unit2="Mm3")
        n = chart.export_csv(tmp_path / "out.csv")
        assert n == 2
        text = (tmp_path / "out.csv").read_text()
        assert "inundated area" in text and "inundated volume" in text
        assert "10" in text and "20" in text

    def test_plot_without_series2_has_no_second_axis(self, qapp):
        from camafloodcast.ui.widgets.charts import HydrographChart

        chart = HydrographChart()
        chart.plot([("x", np.array([1.0, 2.0]))], unit="m")
        assert len(chart.canvas.figure.axes) == 1
        assert chart._ax2 is None


class TestDownscaleView:
    """Phase 3: 1-arcmin inundation from the routing-grid stage the model already computed."""

    def test_tab_present(self, window):
        assert window.tabs.tabText(8) == "Downscaling"

    def test_disabled_before_a_project(self, window):
        dv = window.downscale_view
        assert not dv.btn_run.isEnabled()
        assert "No active map" in dv.lbl_missing.text()

    @needs_downscale
    def test_loading_the_real_mekong_case_configures_the_time_bar(self, window):
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)
        window.select_case(proj.case("baseline"))

        dv = window.downscale_view
        assert dv.outputs is not None
        assert "flddph" in dv.outputs.variables
        assert dv.time.count == dv.outputs["flddph"].nrec
        assert dv.lbl_missing.text() == ""
        assert dv.btn_run.isEnabled()

    @needs_downscale
    def test_runs_the_real_tool_and_populates_the_map(self, window):
        """End to end through the GUI's own code path: the real Fortran tool, on the real
        Mekong project, redistributing the real October-peak flood stage (record 298, the
        historical worked example in etc/v430fix_tests/regional/README.md)."""
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)
        window.select_case(proj.case("baseline"))

        dv = window.downscale_view
        dv.spn["west"].setValue(104.0)
        dv.spn["east"].setValue(108.0)
        dv.spn["south"].setValue(8.5)
        dv.spn["north"].setValue(12.0)
        dv.time.set_record(297)     # 0-indexed; the tool's 1-indexed record 298

        statuses = []
        dv.statusMessage.connect(statuses.append)
        dv._run()
        assert dv._thread is not None
        assert wait_for(dv._thread.done, timeout_ms=120_000), "downscale did not complete in time"

        assert len(dv.map.layers) == 1
        assert dv.map.layers[0].variable == "flddph"
        assert (dv.map.layers[0].grid.nx, dv.map.layers[0].grid.ny) == (240, 210)
        assert "km2" in dv.lbl_stats.text()
        assert any("downscale complete" in s for s in statuses)
        assert not dv.progress.isVisible()
        assert dv.btn_run.isEnabled()

        assert dv._last_array is not None
        assert dv._last_array.shape == (210, 240)
        assert dv._last_bounds == (104.0, 8.5, 108.0, 12.0)
        assert dv.btn_export_tif.isEnabled()
        assert dv.btn_export_png.isEnabled()
        assert not dv.btn_export_csv.isEnabled()      # no time series for a single day

    @needs_downscale
    def test_period_button_disabled_when_only_one_day_is_available(self, window):
        """A case with only one output record has nothing to batch over. Rather than
        fabricating a truncated flddph file, this drives the same enabling condition
        load_case() would reach for such a case: spn_period capped at 1."""
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)
        window.select_case(proj.case("baseline"))

        dv = window.downscale_view
        assert dv.btn_period.isEnabled()      # 365 days available -> enabled

        dv.spn_period.setRange(1, 1)
        dv._check_available()
        assert not dv.btn_period.isEnabled()

    @needs_downscale
    def test_runs_a_real_period_batch_and_populates_chart_and_composite(self, window, tmp_path):
        """End to end: a short batch around the October peak, through the GUI's own thread."""
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)
        window.select_case(proj.case("baseline"))

        dv = window.downscale_view
        dv.spn["west"].setValue(104.0)
        dv.spn["east"].setValue(108.0)
        dv.spn["south"].setValue(8.5)
        dv.spn["north"].setValue(12.0)
        dv.time.set_record(295)
        dv.spn_period.setValue(5)

        statuses = []
        dv.statusMessage.connect(statuses.append)
        dv._run_period()
        assert dv._pthread is not None
        assert wait_for(dv._pthread.done, timeout_ms=180_000), \
            "period downscale did not complete in time"

        assert len(dv.map.layers) == 1
        assert dv.map.layers[0].name.startswith("max flddph, 5d")
        assert (dv.map.layers[0].grid.nx, dv.map.layers[0].grid.ny) == (240, 210)
        # The window is never shown in this offscreen fixture, so isVisible() is always False
        # regardless of the widget's own state -- isHidden() checks the widget itself.
        assert not dv.chart.isHidden()
        assert len(dv.chart._series) == 1
        assert len(dv.chart._series[0][1]) == 5
        assert dv.chart._series[0][0] == "inundated area"
        assert len(dv.chart._series2) == 1
        assert dv.chart._series2[0][0] == "inundated volume"
        assert len(dv.chart._series2[0][1]) == 5
        assert "peak day" in dv.lbl_stats.text()
        assert any("period downscale complete" in s for s in statuses)
        assert not dv.progress.isVisible()
        assert dv.btn_period.isEnabled()

        assert dv._last_array is not None
        assert dv._last_bounds == (104.0, 8.5, 108.0, 12.0)
        assert dv.btn_export_tif.isEnabled()
        assert dv.btn_export_png.isEnabled()
        assert dv.btn_export_csv.isEnabled()

        n = dv.chart.export_csv(tmp_path / "period.csv")
        assert n == 5
        header = (tmp_path / "period.csv").read_text().splitlines()[0]
        assert "inundated area" in header and "inundated volume" in header

    @needs_downscale
    def test_fit_to_domain_reads_the_real_project_bounds(self, window):
        """The default window (104/108/8.5/12) is a Mekong-demo-specific hardcoded default --
        this button should replace it with the actual project's own domain, whatever project is
        open (here: the real Mekong regional cut, 93..110E, 8..34N)."""
        from camafloodcast.core.maps import RiverMap

        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)
        window.select_case(proj.case("baseline"))

        dv = window.downscale_view
        assert dv.btn_fit.isEnabled()
        dv._fit_to_domain()

        grid = RiverMap(dv.map_dir).grid
        assert dv.spn["west"].value() == pytest.approx(grid.west)
        assert dv.spn["east"].value() == pytest.approx(grid.east)
        assert dv.spn["south"].value() == pytest.approx(grid.south)
        assert dv.spn["north"].value() == pytest.approx(grid.north)


class TestAnalysisView:
    """Phase 3 tail: scenario comparison -- A minus B difference maps and paired hydrographs."""

    def test_tab_present(self, window):
        assert window.tabs.tabText(7) == "Analysis"

    def test_disabled_before_a_project(self, window):
        av = window.analysis_view
        assert not av.btn_map.isEnabled()
        assert not av.btn_hydro.isEnabled()
        assert "No project open" in av.lbl_missing.text()

    @needs_downscale
    def test_loading_the_mekong_project_lists_cases_and_gauges(self, window):
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)

        av = window.analysis_view
        assert "baseline" in [av.cmb_a.itemText(i) for i in range(av.cmb_a.count())]
        assert av.cmb_gauge.count() == 3        # Kratie, Pakse, Chiang Saen
        assert av.river_map is not None

    @needs_scenario
    def test_shows_a_real_difference_map(self, window):
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)

        av = window.analysis_view
        av.cmb_a.setCurrentText("baseline")
        av.cmb_b.setCurrentText("no_bifurcation")
        assert av.btn_map.isEnabled()
        av.cmb_var.setCurrentText("outflw")
        av.time.set_record(297)          # the October peak used elsewhere

        statuses = []
        av.statusMessage.connect(statuses.append)
        av._show_diff_map()

        assert len(av.map.layers) == 1
        assert av.map.layers[0].variable == "outflw"
        assert "max" in av.lbl_stats.text()
        # LPTHOUT off must move discharge somewhere by a real amount (03-gui-design.md: bifurcation
        # is "essential for deltas and anabranching rivers"), not just numerical noise -- and the
        # colour range must not collapse to zero even though most cells show no change at all.
        assert av.map.layers[0].spec.vmax > 1.0
        assert any("difference map" in s for s in statuses)

    @needs_scenario
    def test_shows_a_real_hydrograph_comparison(self, window):
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)

        av = window.analysis_view
        av.cmb_a.setCurrentText("baseline")
        av.cmb_b.setCurrentText("no_bifurcation")
        av.cmb_var.setCurrentText("outflw")
        av.cmb_gauge.setCurrentText("Mekong @ Kratie")
        assert av.btn_hydro.isEnabled()

        av._show_hydrograph()

        assert not av.chart.isHidden()
        assert len(av.chart._series) == 2
        assert [name for name, _ in av.chart._series] == ["baseline", "no_bifurcation"]
        # Kratie sits well upstream of the delta bifurcations, so the two series must be
        # identical there -- the point of a scenario diff is that it is a spatial question.
        assert np.allclose(av.chart._series[0][1], av.chart._series[1][1])
        assert "vs A" in av.lbl_stats.text()

    @needs_downscale
    def test_profile_works_with_only_one_case_loaded(self, window):
        """The profile is a single-case feature (it traces one case's own output along the
        river network), so it must not require a second case the way the comparison buttons
        do -- baseline alone is enough here since no_bifurcation may not be built."""
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)

        av = window.analysis_view
        av.cmb_a.setCurrentText("baseline")
        av.cmb_b.setCurrentText("baseline")     # same case in both -> B intentionally unset
        assert av.outs_b is None
        assert av.outs_a is not None
        var_items = [av.cmb_var.itemText(i) for i in range(av.cmb_var.count())]
        assert "sfcelv" in var_items
        assert av.cmb_gauge.count() == 3
        assert av.btn_profile.isEnabled()
        assert not av.btn_map.isEnabled()        # still needs a real, distinct B

    @needs_downscale
    def test_shows_a_real_profile_from_kratie_to_the_outlet(self, window):
        proj = Project.open(MEKONG_PROJECT)
        window.set_project(proj)

        av = window.analysis_view
        av.cmb_a.setCurrentText("baseline")
        av.cmb_gauge.setCurrentText("Mekong @ Kratie")
        av.time.set_record(297)
        assert av.btn_profile.isEnabled()

        statuses = []
        av.statusMessage.connect(statuses.append)
        av._show_profile()

        assert not av.chart.isHidden()
        assert [name for name, _ in av.chart._series] == ["water surface", "channel bed"]
        assert len(av.chart._series[0][1]) > 1
        assert av.chart._x is not None and len(av.chart._x) == len(av.chart._series[0][1])
        # A river runs downhill from an inland gauge to the sea.
        assert av.chart._series[1][1][0] > av.chart._series[1][1][-1]
        assert "outlet" in av.lbl_stats.text()
        assert any("profile" in s for s in statuses)
