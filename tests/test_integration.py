"""Phase 1 integration: run a real simulation through the GUI's own execution path.

The smoke tests check that widgets build. This checks the part that actually matters and is
hardest to get right: ``QProcess`` lifecycle, the log poller, water-balance parsing arriving as
signals, ``run.json`` being written, and the results view loading what the run produced.

Skipped unless the model binary and a regional map are present.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from camafloodcast.core.namelist import new_namelist  # noqa: E402
from camafloodcast.core.project import CaseStatus, Project  # noqa: E402

PKG = Path(__file__).resolve().parents[2]
MAP_PHL = PKG / "map" / "phl_15min"
EXE = PKG / "src" / ("MAIN_cmf.exe" if sys.platform == "win32" else "MAIN_cmf")
RUNOFF = PKG / "inp" / "test_1deg" / "runoff"

pytestmark = pytest.mark.skipif(
    not (EXE.exists() and (MAP_PHL / "params.txt").exists() and RUNOFF.exists()),
    reason="model binary, regional map or forcing not available",
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv[:1])


def build_namelist(map_dir: Path, days: int = 2, tag: str = "it"):
    end = dt.date(2000, 1, 1) + dt.timedelta(days=days)
    nml = new_namelist(["NRUNVER", "NDIMTIME", "NPARAM", "NSIMTIME",
                        "NMAP", "NRESTART", "NFORCE", "NOUTPUT"])
    nml.update("NRUNVER", dict(LADPSTP=True, LFLDOUT=True, LPTHOUT=True,
                               LDAMOUT=False, LRESTART=False, LWBLOG=True))
    nml.update("NDIMTIME", {"CDIMINFO": str(map_dir / "diminfo_test-1deg.txt"),
                            "DT": 3600, "IFRQ_INP": 24})
    nml.update("NPARAM", dict(PMANRIV=0.03, PMANFLD=0.10, PDSTMTH=10000.0, PCADP=0.7))
    nml.update("NSIMTIME", dict(SYEAR=2000, SMON=1, SDAY=1, SHOUR=0,
                                EYEAR=end.year, EMON=end.month, EDAY=end.day, EHOUR=0))
    nml.update("NMAP", {k: str(map_dir / v) for k, v in {
        "CNEXTXY": "nextxy.bin", "CGRAREA": "ctmare.bin", "CELEVTN": "elevtn.bin",
        "CNXTDST": "nxtdst.bin", "CRIVLEN": "rivlen.bin", "CFLDHGT": "fldhgt.bin",
        "CRIVWTH": "rivwth_gwdlr.bin", "CRIVHGT": "rivhgt.bin", "CRIVMAN": "rivman.bin",
        "CPTHOUT": "bifprm.txt"}.items()} | {"LMAPCDF": False})
    nml.update("NRESTART", {"CRESTSTO": "", "CRESTDIR": "./", "CVNREST": "restart",
                            "LRESTCDF": False, "IFRQ_RST": 0})
    nml.update("NFORCE", {"LINPCDF": False, "LINTERP": True,
                          "CINPMAT": str(map_dir / "inpmat_test-1deg.bin"),
                          "DROFUNIT": 86400000, "CROFDIR": str(RUNOFF) + "/",
                          "CROFPRE": "Roff____", "CROFSUF": ".one"})
    nml.update("NOUTPUT", {"COUTDIR": "./", "COUTTAG": tag, "LOUTVEC": False,
                           "LOUTCDF": False, "NDLEVEL": 0, "IFRQ_OUT": 24,
                           "CVARSOUT": "outflw,rivdph,storge"})
    return nml


def wait_for(signal, timeout_ms: int = 120_000) -> bool:
    """Spin the event loop until a signal fires. Returns False on timeout."""
    loop = QEventLoop()
    fired = {"ok": False}

    def done(*_a):
        fired["ok"] = True
        loop.quit()

    signal.connect(done)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return fired["ok"]


class TestRunThroughTheGui:
    def test_full_cycle(self, qapp, tmp_path):
        from camafloodcast.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        try:
            proj = Project.create(tmp_path / "study", name="integration",
                                  map_dir=str(MAP_PHL), model_binary=str(EXE))
            case = proj.add_case("baseline", namelist=build_namelist(MAP_PHL))
            win.set_project(proj)
            win.select_case(case)

            # --- Gate 3 must pass before anything runs
            diags = win.adapter.validate(case.namelist(), base_dir=case.dir)
            assert diags.ok, f"pre-flight failed: {[str(d) for d in diags]}"
            assert case.status() is CaseStatus.NOT_RUN

            # --- run it through the queue, exactly as the Run button does
            captured: list[str] = []
            win.run_view.logLines.connect(captured.extend)

            win.run_case(case)
            assert win.queue is not None and win.queue.busy, "the job should be running"
            assert wait_for(win.queue.queueDrained), "the run did not finish in time"

            # --- process outcome
            rec = case.record()
            assert rec.returncode == 0, f"model exited {rec.returncode}"
            assert case.status() is CaseStatus.COMPLETED
            assert rec.input_hash, "run.json must record the input hash for stale detection"
            assert rec.elapsed_s and rec.elapsed_s > 0

            # --- the log poller actually parsed the run
            assert captured, "no log lines reached the GUI"
            p = win.queue.current.progress if win.queue.current else None
            prog = p or win.run_view  # queue is drained; use what the view captured
            assert rec.max_substeps and rec.max_substeps >= 1
            assert rec.worst_balance_error is not None
            assert rec.worst_balance_error < 1e-9, "water balance did not close"

            # --- outputs exist and the results view can load them
            assert case.output_files(), "no output files produced"
            win.results.load(case, MAP_PHL)
            assert win.results.var_list.count() >= 1
            assert "0.25" in win.results.resolution.text()

            # --- a layer renders without touching a widget from a worker
            win.results._add_layer()
            assert len(win.results.layers.layers) >= 2      # base + variable

            # --- stale detection closes the loop
            edited = case.namelist()
            edited.set("NPARAM", "PMANFLD", 0.2)
            edited.save(backup=False)
            assert case.status() is CaseStatus.STALE
        finally:
            win.close()

    def test_bad_config_is_blocked_before_launching(self, qapp, tmp_path):
        """Gate 3 must stop a run that CaMa-Flood would reject with STOP 9."""
        from camafloodcast.ui.main_window import MainWindow

        win = MainWindow()
        win.interactive = False
        try:
            proj = Project.create(tmp_path / "s", map_dir=str(MAP_PHL),
                                  model_binary=str(EXE))
            nml = build_namelist(MAP_PHL)
            nml.set("NDIMTIME", "DT", 3500)          # not a multiple of 60
            case = proj.add_case("bad", namelist=nml)
            win.set_project(proj)

            diags = win.adapter.validate(case.namelist(), base_dir=case.dir)
            assert not diags.ok
            assert any(d.code == "CFG_DT_MULT" for d in diags)
        finally:
            win.close()
