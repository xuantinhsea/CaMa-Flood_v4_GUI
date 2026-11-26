"""Phase 0 test suite: the core SDK, headless.

Runs against the real CaMa-Flood package rather than fixtures wherever possible -- the whole
point of the SDK is that it reads *actual* model files correctly, and synthetic fixtures would
not have caught the params.txt dual format or the diminfo edge-order difference.

Tests that need the package data skip cleanly when it is absent, so the suite still runs in CI.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest

from camafloodcast.core import Namelist, RiverMap, read_diminfo, read_params
from camafloodcast.core.analysis import Gauge, compare, snap_gauge
from camafloodcast.core.domain import analyse_domain, find_outlet_near, validate_domain
from camafloodcast.core.errors import FileFormatError, Severity
from camafloodcast.core.maps import NEXTXY_MOUTH, Grid, read_bifprm
from camafloodcast.core.namelist import format_value, new_namelist, parse_value
from camafloodcast.core.output import OutputFile, OutputSet
from camafloodcast.core.project import CaseStatus, Project, RunRecord
from camafloodcast.core.runner import (
    AdaptiveStep,
    ExitKind,
    LogReader,
    WaterBalance,
    classify_exit,
    parse_log_line,
)
from camafloodcast.core.schema import required_capabilities, validate

# The model package this SDK drives.
PKG = Path(__file__).resolve().parents[2]
MAP_GLB = PKG / "map" / "glb_15min"
MAP_PHL = PKG / "map" / "phl_15min"
MAP_MEK = PKG / "map" / "mekong_15min"

needs_glb = pytest.mark.skipif(not (MAP_GLB / "params.txt").exists(),
                               reason="global map not present")
needs_phl = pytest.mark.skipif(not (MAP_PHL / "params.txt").exists(),
                               reason="Philippines regional map not built")
needs_mek = pytest.mark.skipif(not (MAP_MEK / "params.txt").exists(),
                               reason="Mekong regional map not built")


# =========================================================================== namelist


class TestNamelistValues:
    @pytest.mark.parametrize("text,expect", [
        (".TRUE.", True), (".FALSE.", False), (".true.", True), (".T.", True), (".f.", False),
        ("3600", 3600), ("-9999", -9999),
        ("0.03D0", 0.03), ("1.D-5", 1e-5), ("1.E20", 1e20), ("0.7", 0.7),
        ('"a/path.bin"', "a/path.bin"), ("'quoted'", "quoted"),
    ])
    def test_parse(self, text, expect):
        assert parse_value(text) == expect

    def test_int_stays_int(self):
        """DT = 3600 must round-trip as 3600, not 3600.0."""
        assert isinstance(parse_value("3600"), int)

    @pytest.mark.parametrize("value,expect", [
        (True, ".TRUE."), (False, ".FALSE."), (3600, "3600"), ("p.bin", '"p.bin"'),
    ])
    def test_format(self, value, expect):
        assert format_value(value) == expect

    def test_comment_not_split_inside_quotes(self):
        nml = Namelist.parse('&G\nP = "dir/with!bang"   ! real comment\n/\n')
        assert nml.get("G", "P") == "dir/with!bang"


class TestNamelistRoundTrip:
    SRC = (
        "&NRUNVER\n"
        "LADPSTP  = .TRUE.                  ! adaptive step\n"
        "LPTHOUT  = .FALSE.\n"
        "/\n"
        "&NDIMTIME\n"
        "DT       = 3600\n"
        "IFRQ_INP = 24\n"
        "/\n"
    )

    def test_unchanged_is_byte_identical(self):
        nml = Namelist.parse(self.SRC)
        assert nml.render() == self.SRC

    def test_edit_touches_only_that_line(self):
        nml = Namelist.parse(self.SRC)
        nml.set("NDIMTIME", "DT", 1800)
        out = nml.render().splitlines()
        src = self.SRC.splitlines()
        differing = [i for i in range(len(src)) if src[i] != out[i]]
        assert differing == [5]
        assert out[5] == "DT       = 1800"      # alignment preserved

    def test_comment_preserved_on_edit(self):
        nml = Namelist.parse(self.SRC)
        nml.set("NRUNVER", "LADPSTP", False)
        assert "! adaptive step" in nml.render()

    def test_insert_goes_into_the_right_group(self):
        nml = Namelist.parse(self.SRC)
        nml.set("NDIMTIME", "CDIMINFO", "d.txt")
        re_read = Namelist.parse(nml.render())
        assert re_read.get("NDIMTIME", "CDIMINFO") == "d.txt"
        assert "CDIMINFO" not in re_read.group_items("NRUNVER")

    def test_unterminated_group_raises(self):
        from camafloodcast.core.errors import NamelistError
        with pytest.raises(NamelistError):
            Namelist.parse("&NRUNVER\nDT = 1\n")

    def test_set_on_a_group_absent_from_the_file_appends_it(self):
        """CaMa-Flood reads NBOUND/NLEVEE/NDAMOUT only when their switch is on
        (cmf_drv_control_mod.F90), so a real namelist for a run with the feature off
        legitimately has no such group at all. Turning it on and setting its first field must
        not crash just because the group was never in the file."""
        nml = Namelist.parse(self.SRC)
        assert "NLEVEE" not in nml.groups
        nml.set("NLEVEE", "CLEVHGT", "levhgt.bin")
        assert "NLEVEE" in nml.groups
        assert nml.get("NLEVEE", "CLEVHGT") == "levhgt.bin"

        re_read = Namelist.parse(nml.render())
        assert re_read.get("NLEVEE", "CLEVHGT") == "levhgt.bin"
        # The original groups are untouched -- this is an append, not a rewrite.
        assert re_read.get("NRUNVER", "LADPSTP") is True

    def test_set_on_an_existing_empty_group_does_not_duplicate_it(self):
        """A namelist that already carries an empty '&NLEVEE / ' placeholder (common in
        hand-written files, for a feature currently off) must be recognised as already present,
        not treated as absent and appended a second time."""
        src = self.SRC + "&NLEVEE\n/\n"
        nml = Namelist.parse(src)
        assert "NLEVEE" in nml.groups
        nml.set("NLEVEE", "CLEVHGT", "levhgt.bin")
        assert nml.render().count("&NLEVEE") == 1

    def test_diff(self):
        a = Namelist.parse(self.SRC)
        b = Namelist.parse(self.SRC)
        b.set("NDIMTIME", "DT", 900)
        assert ("NDIMTIME/DT", 3600, 900) in a.diff(b)

    def test_save_makes_a_backup(self, tmp_path):
        p = tmp_path / "input_cmf.nam"
        p.write_text(self.SRC, encoding="utf-8")
        nml = Namelist.load(p)
        nml.set("NDIMTIME", "DT", 60)
        nml.save()
        assert list(tmp_path.glob("*.bak")), "a backup must be written before overwriting"


# =========================================================================== schema


class TestValidation:
    def _base(self) -> Namelist:
        return Namelist.parse(
            "&NRUNVER\nLADPSTP = .TRUE.\nLKINE = .FALSE.\nLPTHOUT = .FALSE.\n/\n"
            "&NDIMTIME\nDT = 3600\nIFRQ_INP = 24\n/\n"
            "&NSIMTIME\nSYEAR = 2000\nSMON = 1\nSDAY = 1\nSHOUR = 0\n"
            "EYEAR = 2001\nEMON = 1\nEDAY = 1\nEHOUR = 0\n/\n"
            "&NFORCE\nLINTERP = .FALSE.\n/\n"
        )

    def test_clean_config_passes(self):
        assert validate(self._base(), check_files=False).ok

    def test_dt_must_be_multiple_of_60(self):
        nml = self._base()
        nml.set("NDIMTIME", "DT", 3500)
        d = validate(nml, check_files=False)
        assert not d.ok
        assert any(x.code == "CFG_DT_MULT" for x in d)

    def test_dtin_must_be_multiple_of_dt(self):
        nml = self._base()
        nml.set("NDIMTIME", "DT", 7200)
        nml.set("NDIMTIME", "IFRQ_INP", 1)      # 3600 s, not a multiple of 7200
        assert any(x.code == "CFG_DTIN_MULT" for x in validate(nml, check_files=False))

    def test_kine_plus_pthout_is_fatal(self):
        nml = self._base()
        nml.set("NRUNVER", "LKINE", True)
        nml.set("NRUNVER", "LPTHOUT", True)
        d = validate(nml, check_files=False)
        assert not d.ok
        assert any(x.code == "CFG_KINE_PTH" for x in d)

    def test_period_must_advance(self):
        nml = self._base()
        nml.set("NSIMTIME", "EYEAR", 1999)
        assert any(x.code == "CFG_PERIOD" for x in validate(nml, check_files=False))

    def test_unknown_key_is_info_not_error(self):
        nml = self._base()
        nml.set("NRUNVER", "LFUTURE_OPTION", True)
        d = validate(nml, check_files=False)
        assert d.ok
        assert any(x.code == "NML_UNKNOWN_KEY" for x in d)

    def test_capability_detection(self):
        nml = self._base()
        assert required_capabilities(nml) == set()
        nml.set("NFORCE", "LINPCDF", True)
        assert "netcdf" in required_capabilities(nml)

    def test_missing_file_detected(self, tmp_path):
        nml = Namelist.parse('&NMAP\nCNEXTXY = "nope.bin"\n/\n')
        d = validate(nml, base_dir=tmp_path)
        assert any(x.code == "NML_MISSING_FILE" for x in d)

    def test_sealev_without_frequency_is_rejected(self):
        """IFRQ_SL defaults to 9999, the model's own sentinel for 'not used' -- verified against
        cmf_ctrl_boundary_mod.F90. Leaving it there while LSEALEV is on would build and run,
        producing no sea-level boundary effect at all, silently."""
        nml = self._base()
        nml.set("NRUNVER", "LSEALEV", True)
        d = validate(nml, check_files=False)
        assert not d.ok
        assert any(x.code == "CFG_SEALEV_FREQ" for x in d)

    def test_sealev_plain_binary_needs_a_directory(self):
        nml = self._base()
        nml.set("NRUNVER", "LSEALEV", True)
        nml.set("NBOUND", "IFRQ_SL", 60)
        nml.set("NBOUND", "CSEALEVDIR", "")
        d = validate(nml, check_files=False)
        assert any(x.code == "CFG_SEALEV_FILE" for x in d)

    def test_sealev_netcdf_needs_capability_and_a_file(self):
        nml = self._base()
        nml.set("NRUNVER", "LSEALEV", True)
        nml.set("NBOUND", "IFRQ_SL", 60)
        nml.set("NBOUND", "LSEALEVCDF", True)
        nml.set("NBOUND", "CSEALEVCDF", "")
        d = validate(nml, check_files=False)
        assert any(x.code == "CFG_SEALEV_FILE" for x in d)
        assert "netcdf" in required_capabilities(nml)

    def test_sealev_fully_configured_passes(self):
        nml = self._base()
        nml.set("NRUNVER", "LSEALEV", True)
        nml.set("NBOUND", "IFRQ_SL", 60)
        nml.set("NBOUND", "CSEALEVDIR", "./sealev/")
        assert validate(nml, check_files=False).ok

    def test_nbound_is_in_all_groups(self):
        from camafloodcast.core import schema

        assert "NBOUND" in schema.GROUPS_BY_NAME
        assert schema.field_for("NBOUND", "IFRQ_SL") is not None

    def test_damout_without_a_file_is_rejected(self):
        nml = self._base()
        nml.set("NRUNVER", "LDAMOUT", True)
        d = validate(nml, check_files=False)
        assert not d.ok
        assert any(x.code == "CFG_DAM_FILE" for x in d)

    def test_damout_with_a_file_passes(self):
        nml = self._base()
        nml.set("NRUNVER", "LDAMOUT", True)
        nml.set("NDAMOUT", "CDAMFILE", "./dam_params.csv")
        assert validate(nml, check_files=False).ok

    def test_ndamout_is_in_all_groups(self):
        from camafloodcast.core import schema

        assert "NDAMOUT" in schema.GROUPS_BY_NAME
        assert schema.field_for("NDAMOUT", "CDAMFILE") is not None


# =========================================================================== maps


class TestGrid:
    def test_geometry(self):
        g = Grid(44, 68, 10, 0.25, 116.0, 127.0, 4.0, 21.0)
        assert g.shape == (68, 44)
        assert g.lons()[0] == pytest.approx(116.125)
        assert g.lats()[0] == pytest.approx(20.875)          # north to south
        assert g.cell_of(121.625, 18.375) == (22, 10)
        assert g.cell_of(0.0, 0.0) is None

    def test_offset_in_parent(self):
        glb = Grid(1440, 720, 10, 0.25, -180.0, 180.0, -90.0, 90.0)
        phl = Grid(44, 68, 10, 0.25, 116.0, 127.0, 4.0, 21.0)
        assert phl.offset_in(glb) == (1184, 276)


@needs_glb
class TestRealMaps:
    def test_params_global_flavour(self):
        """Global maps use '1440   !! comment'."""
        g = read_params(MAP_GLB / "params.txt")
        assert (g.nx, g.ny, g.nlfp) == (1440, 720, 10)
        assert g.north == 90.0 and g.south == -90.0

    @needs_phl
    def test_params_setmap_flavour(self):
        """set_map writes '44    grid number' with no '!!' -- must parse identically."""
        g = read_params(MAP_PHL / "params.txt")
        assert (g.nx, g.ny) == (44, 68)
        assert (g.west, g.east, g.south, g.north) == (116.0, 127.0, 4.0, 21.0)

    @needs_mek
    def test_diminfo_edge_order_differs_from_params(self):
        """params.txt is west/east/SOUTH/NORTH; diminfo is west/east/NORTH/SOUTH."""
        d = read_diminfo(MAP_MEK / "diminfo_test-1deg.txt")
        assert d.grid.north == 34.0
        assert d.grid.south == 8.0
        assert (d.nxin, d.nyin) == (360, 180)
        assert not d.forcing_matches_routing

    @needs_mek
    def test_diminfo_detects_matching_forcing_grid(self):
        p = MAP_MEK / "diminfo_15min.txt"
        if not p.exists():
            pytest.skip("0.25 deg forcing not prepared")
        assert read_diminfo(p).forcing_matches_routing

    @needs_phl
    def test_river_map_counts(self):
        m = RiverMap(MAP_PHL)
        assert m.n_river_cells == 536
        assert m.n_outlets == 276

    @needs_phl
    def test_layers_are_memmapped(self):
        m = RiverMap(MAP_PHL)
        assert isinstance(m.layer("uparea"), np.memmap)

    @needs_phl
    def test_wrong_grid_is_refused_not_misread(self, tmp_path):
        """Risk R3: a RECL/size mismatch must fail loudly, never read garbage."""
        from camafloodcast.core.maps import read_layer
        wrong = Grid(100, 100, 10, 0.25, 0, 25, 0, 25)
        with pytest.raises(FileFormatError, match="size"):
            read_layer(MAP_PHL / "uparea.bin", wrong, "uparea")

    @needs_mek
    def test_bifprm(self):
        b = read_bifprm(MAP_MEK / "bifprm.txt")
        assert b.npath == 268 and b.nlev == 5
        assert b.width.shape == (268, 5)


# =========================================================================== domain


@needs_mek
class TestDomainClosure:
    def test_mekong_basin_is_closed(self):
        """The reason this module exists: uparea alone cannot show truncation."""
        report = analyse_domain(RiverMap(MAP_MEK))
        mekong = find_outlet_near(report, 106.38, 9.62, radius_deg=0.5)
        assert mekong is not None
        assert mekong.uparea_km2 == pytest.approx(776098, rel=0.01)
        assert mekong.closed
        assert mekong.deficit < 0.01

    def test_neighbouring_basins_are_truncated_as_expected(self):
        report = analyse_domain(RiverMap(MAP_MEK))
        assert report.truncated(top=10), "a rectangular cut must truncate something"

    def test_target_truncation_is_an_error_others_are_not(self):
        report = analyse_domain(RiverMap(MAP_MEK))
        truncated = report.truncated(top=10)[0]
        d = validate_domain(report, target_basin=truncated)
        assert not d.ok
        assert any(x.code == "DOM_TARGET_TRUNCATED" for x in d)

        mekong = find_outlet_near(report, 106.38, 9.62, radius_deg=0.5)
        d2 = validate_domain(report, target_basin=mekong)
        assert d2.ok, "truncating a neighbouring basin must not block the run"


@needs_phl
class TestPhilippinesIsSelfContained:
    def test_every_basin_closed(self):
        """Why an archipelago is the right national domain."""
        report = analyse_domain(RiverMap(MAP_PHL))
        for o in report.largest[:8]:
            assert o.closed, f"{o.lon},{o.lat} unexpectedly truncated ({o.deficit:.1%})"

    def test_cagayan_outlet_resolved(self):
        report = analyse_domain(RiverMap(MAP_PHL))
        top = report.largest[0]
        assert top.uparea_km2 == pytest.approx(28039, rel=0.01)
        assert top.lon == pytest.approx(121.625, abs=0.01)


# =========================================================================== log parsing


class TestLogParser:
    WB = ("2000/01/11_00:00    11 flx:      981.545     981.445     981.785   0.196E-11"
          "         3.580       3.681 stg:      981.785     981.785   0.208E-11"
          "     747.180     234.606     733.208")

    def test_water_balance(self):
        wb = parse_log_line(self.WB)
        assert isinstance(wb, WaterBalance)
        assert wb.when == dt.datetime(2000, 1, 11, 0, 0)
        assert wb.substep == 11
        assert wb.sto_pre == pytest.approx(981.545)
        assert wb.riv_storage == pytest.approx(747.180)
        assert wb.fld_storage == pytest.approx(234.606)
        assert wb.relative_flux_error < 1e-12
        assert wb.healthy

    def test_unhealthy_balance_detected(self):
        bad = self.WB.replace("0.196E-11", "0.196E+02")
        wb = parse_log_line(bad)
        assert not wb.healthy

    def test_adaptive_step(self):
        a = parse_log_line("    ADPSTP: NT=  11   3600.00    334.82    327.27")
        assert isinstance(a, AdaptiveStep)
        assert a.substeps == 11
        assert a.actual_dt == pytest.approx(327.27)

    def test_nseqmax_and_hypsometry(self):
        assert parse_log_line(" CALC_REGION: NSEQMAX=      252383") == {"nseqmax": 252383}
        assert parse_log_line(
            " TOPO_INIT: floodplain hypsometry check: flat layers =  27034  of  2523830"
        ) == {"flat_layers": 27034}

    def test_error_line(self):
        out = parse_log_line("Fortran runtime error: Cannot open file 'input_cmf.nam'")
        assert "error" in out

    def test_noise_is_ignored(self):
        assert parse_log_line("CMF::DRV_INIT: end") is None

    def test_incremental_reading(self, tmp_path):
        log = tmp_path / "log_CaMa.txt"
        log.write_text(" CALC_REGION: NSEQMAX=      536\n", encoding="utf-8")
        r = LogReader(log)
        assert len(r.poll()) == 1
        assert r.progress.nseqmax == 536
        assert r.poll() == [], "a second poll with no new bytes must return nothing"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(self.WB + "\n")
        assert len(r.poll()) == 1
        assert len(r.progress.balances) == 1


class TestExitCodes:
    @pytest.mark.parametrize("code,kind", [
        (0, ExitKind.SUCCESS), (2, ExitKind.RUNTIME_ERROR),
        (9, ExitKind.MODEL_STOP), (None, ExitKind.KILLED), (137, ExitKind.UNKNOWN),
    ])
    def test_classification(self, code, kind):
        assert classify_exit(code) is kind


# =========================================================================== output


@needs_phl
class TestOutputReaders:
    def _run_dir(self) -> Path:
        d = PKG / "etc" / "v430fix_tests" / "regional" / "out_phl_1deg"
        if not d.exists() or not list(d.glob("*.bin")):
            pytest.skip("no completed run to read")
        return d

    def test_discovery_and_units(self):
        grid = read_params(MAP_PHL / "params.txt")
        s = OutputSet(self._run_dir(), grid, tag="phl")
        assert "outflw" in s
        assert s["outflw"].unit == "m3/s"

    def test_windowed_read(self):
        grid = read_params(MAP_PHL / "params.txt")
        f = OutputSet(self._run_dir(), grid, tag="phl")["outflw"]
        assert f.record(0, window=(10, 10, 4, 6)).shape == (6, 4)

    def test_series_is_strided_not_full_load(self):
        grid = read_params(MAP_PHL / "params.txt")
        f = OutputSet(self._run_dir(), grid, tag="phl")["outflw"]
        ts = f.series_at(22, 10)
        assert ts.shape == (f.nrec,)

    def test_statistics_exclude_the_undefined_value(self):
        """Without masking, every variable reports a maximum of RMIS = 1e20."""
        grid = read_params(MAP_PHL / "params.txt")
        m = RiverMap(MAP_PHL)
        f = OutputSet(self._run_dir(), grid, tag="phl")["rivdph"]
        _, lo, hi = f.check_finite(mask=m.land_mask)
        assert hi < 1e6, "RMIS leaked into the statistics"

    def test_size_mismatch_refused(self, tmp_path):
        p = tmp_path / "outflwx.bin"
        p.write_bytes(b"\x00" * 100)         # not a whole number of records
        with pytest.raises(FileFormatError):
            OutputFile(p, Grid(44, 68, 10, 0.25, 116, 127, 4, 21))


class TestOutputVariables:
    """CaMa-Flood can write more variables than this GUI recognised at one point -- an unlisted
    variable's output file is silently invisible everywhere (Map & Results, Analysis,
    Downscaling), not an error. Confirmed against src/cmf_ctrl_output_mod.F90's CVLNAME/CVUNITS
    table, which is the model's own authoritative list."""

    @pytest.mark.parametrize("name,unit", [
        ("levdph", "m"), ("levsto", "m3"), ("gdwsto", "m3"), ("outins", "m3/s"),
    ])
    def test_levee_and_groundwater_variables_are_recognised(self, name, unit):
        from camafloodcast.core.output import VARIABLES

        assert name in VARIABLES
        assert VARIABLES[name][0] == unit

    def test_a_previously_unrecognised_variable_is_discovered(self, tmp_path):
        grid = Grid(4, 4, 1, 0.25, 0.0, 1.0, 0.0, 1.0)
        n = 3
        (tmp_path / "levdphtag.bin").write_bytes(
            np.zeros((n, grid.ny, grid.nx), dtype="<f4").tobytes())
        s = OutputSet(tmp_path, grid, tag="tag")
        assert "levdph" in s.variables
        assert s["levdph"].unit == "m"

    def test_levdph_and_levsto_carry_a_levee_specific_caveat(self):
        from camafloodcast.core.output import CAVEATS

        assert "LLEVEE" in CAVEATS["levdph"]
        assert "LLEVEE" in CAVEATS["levsto"]

    def test_display_name_is_a_readable_label_not_the_short_code(self):
        from camafloodcast.core.output import display_name

        assert display_name("flddph") == "Floodplain water depth"
        assert display_name("outflw") == "Total outflow (channel + floodplain)"

    def test_display_name_falls_back_to_the_short_code_for_an_unknown_variable(self):
        from camafloodcast.core.output import display_name

        assert display_name("nonsense") == "nonsense"


class TestColorPresets:
    """The discrete named-preset colour path in services/render.py, alongside the pre-existing
    continuous-colormap path (which these tests must not disturb -- see the last two cases)."""

    def test_band_colorize_matches_band_edges_exactly(self):
        from camafloodcast.services.render import ColorSpec, colorize, hex_to_rgb

        spec = ColorSpec(colors=("#000000", "#ffffff"), bounds=(0.0, 10.0))
        rgba = colorize(np.array([0.0, 10.0]), spec)
        assert tuple(rgba[0, :3]) == (0, 0, 0)
        assert tuple(rgba[1, :3]) == (255, 255, 255)
        assert rgba[0, 3] > 0 and rgba[1, 3] > 0

    def test_band_colorize_interpolates_at_the_midpoint(self):
        from camafloodcast.services.render import ColorSpec, colorize

        spec = ColorSpec(colors=("#000000", "#ffffff"), bounds=(0.0, 10.0))
        rgba = colorize(np.array([5.0]), spec)
        # halfway between black and white
        assert tuple(int(c) for c in rgba[0, :3]) == (128, 128, 128) or \
            tuple(int(c) for c in rgba[0, :3]) == (127, 127, 127)

    def test_below_first_bound_is_transparent_unless_clamp_low(self):
        from camafloodcast.services.render import ColorSpec, colorize

        spec = ColorSpec(colors=("#ff0000", "#0000ff"), bounds=(0.0, 10.0))
        rgba = colorize(np.array([-1.0]), spec)
        assert rgba[0, 3] == 0

        clamped = ColorSpec(colors=("#ff0000", "#0000ff"), bounds=(0.0, 10.0), clamp_low=True)
        rgba2 = colorize(np.array([-1.0]), clamped)
        assert tuple(int(c) for c in rgba2[0, :3]) == (255, 0, 0)
        assert rgba2[0, 3] > 0

    def test_transparent_below_zeroes_alpha_in_discrete_mode_too(self):
        from camafloodcast.services.render import ColorSpec, colorize

        spec = ColorSpec(colors=("#ffffff", "#0000ff"), bounds=(0.0, 5.0), transparent_below=0.5)
        rgba = colorize(np.array([0.1, 1.0]), spec)
        assert rgba[0, 3] == 0
        assert rgba[1, 3] > 0

    def test_from_preset_builds_a_discrete_spec(self):
        from camafloodcast.services.render import COLOR_PRESETS, ColorSpec

        spec = ColorSpec.from_preset("Floodplain depth (blue)")
        preset = COLOR_PRESETS["Floodplain depth (blue)"]
        assert spec.colors == preset.colors
        assert spec.bounds == preset.bounds
        assert spec.vmin == preset.bounds[0]
        assert spec.vmax == preset.bounds[-1]
        assert spec.transparent_below == preset.transparent_below

    def test_legend_stops_returns_the_presets_own_band_edges(self):
        from camafloodcast.services.render import ColorSpec, legend_stops

        spec = ColorSpec.from_preset("Difference (diverging)")
        stops = legend_stops(spec)
        preset_bounds = (-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0)
        assert [v for v, _ in stops] == list(preset_bounds)

    def test_set_color_preset_carries_over_transparent_below_when_not_given(self):
        from camafloodcast.services.render import COLOR_PRESETS, set_color_preset

        set_color_preset("Floodplain depth (blue)",
                         ["#111111", "#222222"], [0.0, 9.0])
        preset = COLOR_PRESETS["Floodplain depth (blue)"]
        assert preset.transparent_below == 1e-3   # unchanged from the built-in default
        # restore so other tests (and this test on a re-run) see the original preset
        set_color_preset("Floodplain depth (blue)",
                         ["#FFFFFF", "#BDE5F8", "#7EC8E3", "#4BA3C3", "#2E86AB", "#1B4F72", "#0B2545"],
                         [0.0, 0.1, 0.3, 0.5, 1.0, 2.0, 5.0], transparent_below=1e-3)

    def test_save_and_load_presets_round_trip(self, tmp_path):
        from camafloodcast.services.render import (
            COLOR_PRESETS, load_presets, save_presets, set_color_preset,
        )

        set_color_preset("Test preset", ["#000000", "#ffffff"], [0.0, 1.0])
        path = tmp_path / "presets.json"
        save_presets(path)
        del COLOR_PRESETS["Test preset"]

        n = load_presets(path)
        assert n >= 1
        assert COLOR_PRESETS["Test preset"].colors == ("#000000", "#ffffff")
        assert COLOR_PRESETS["Test preset"].bounds == (0.0, 1.0)

    def test_load_presets_missing_file_is_not_an_error(self, tmp_path):
        from camafloodcast.services.render import load_presets

        assert load_presets(tmp_path / "does_not_exist.json") == 0

    def test_continuous_colormap_path_is_unchanged_when_no_preset_is_set(self):
        """Existing callers (ColorSpec.for_variable, no colors/bounds) must render exactly as
        before -- this is the regression guard for extending colorize() with the band path."""
        from camafloodcast.services.render import ColorSpec, colorize

        spec = ColorSpec.for_variable("flddph", vmin=0.0, vmax=5.0)
        assert spec.colors is None and spec.bounds is None
        rgba = colorize(np.array([0.0, 2.5, 5.0]), spec)
        assert rgba.shape == (3, 4)

    def test_for_variable_never_sets_a_preset_field(self):
        """ColorSpec.for_variable is the pre-existing continuous-cmap constructor; it must not
        start emitting discrete-mode fields as a side effect of adding from_preset()."""
        from camafloodcast.services.render import ColorSpec

        spec = ColorSpec.for_variable("outflw", vmin=0.0, vmax=1000.0)
        assert spec.colors is None
        assert spec.bounds is None
        assert spec.preset is None


# =========================================================================== analysis


@needs_mek
class TestGaugeSnapping:
    @pytest.mark.parametrize("name,lon,lat,area", [
        ("Kratie", 106.02, 12.48, 646000),
        ("Chiang Saen", 100.08, 20.27, 189000),
        ("Pakse", 105.80, 15.12, 545000),
    ])
    def test_snaps_by_drainage_area(self, name, lon, lat, area):
        g = snap_gauge(Gauge(name, lon, lat, area_km2=area), RiverMap(MAP_MEK))
        assert g.snapped
        assert g.area_error < 0.05, f"{name}: {g.match_quality}"


class TestMetrics:
    def test_perfect_match(self):
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        m = compare(x, x)
        assert m.nse == pytest.approx(1.0)
        assert m.pbias == pytest.approx(0.0)

    def test_bias_detected(self):
        obs = np.array([1.0, 2.0, 3.0, 4.0])
        m = compare(obs * 1.5, obs)
        assert m.pbias == pytest.approx(50.0)

    def test_nan_pairs_dropped(self):
        m = compare(np.array([1.0, np.nan, 3.0]), np.array([1.0, 2.0, 3.0]))
        assert m.n == 2


class TestLeveeStats:
    def test_never_overtopped(self):
        from camafloodcast.core.analysis import levee_protection_stats

        s = levee_protection_stats(np.zeros(10))
        assert s.fraction_overtopped == 0.0
        assert s.first_overtopped_record is None
        assert "never overtopped" in s.summary()

    def test_partial_overtopping(self):
        from camafloodcast.core.analysis import levee_protection_stats

        # dry, dry, overtopped at 0.5m, overtopped at 1.5m, back to dry
        series = np.array([0.0, 0.0, 0.5, 1.5, 0.0])
        s = levee_protection_stats(series)
        assert s.n == 5
        assert s.fraction_overtopped == pytest.approx(2 / 5)
        assert s.first_overtopped_record == 2
        assert s.max_depth_m == pytest.approx(1.5)
        assert s.mean_depth_when_wet_m == pytest.approx(1.0)
        assert "overtopped" in s.summary() and "40.0%" in s.summary()

    def test_threshold_filters_floating_point_noise(self):
        from camafloodcast.core.analysis import levee_protection_stats

        s = levee_protection_stats(np.array([1e-9, -1e-9, 0.0]), threshold_m=0.01)
        assert s.fraction_overtopped == 0.0

    def test_empty_series(self):
        from camafloodcast.core.analysis import levee_protection_stats

        s = levee_protection_stats(np.array([]))
        assert s.n == 0
        assert s.summary() == "no records"


class TestFieldDiff:
    def test_diff_is_a_minus_b(self, tmp_path):
        from camafloodcast.core.analysis import diff_field

        grid = Grid(nx=2, ny=1, nlfp=10, gsize=1.0, west=0, east=2, south=0, north=1)
        # Named outflwA/outflwB so split_filename() reads the variable as "outflw" for both,
        # matching what diff_field's cross-check expects of two real case outputs.
        pa = tmp_path / "outflwA.bin"
        pb = tmp_path / "outflwB.bin"
        np.array([[1.0, 2.0]], dtype="<f4").tofile(pa)
        np.array([[1.0, 5.0]], dtype="<f4").tofile(pb)
        fa = OutputFile(pa, grid)
        fb = OutputFile(pb, grid)

        d = diff_field(fa, fb, 0)
        assert np.allclose(d.diff, [[0.0, -3.0]])
        assert d.max_abs_diff == pytest.approx(3.0)
        assert d.mean_abs_diff == pytest.approx(1.5)

    def test_rejects_mismatched_grids(self, tmp_path):
        from camafloodcast.core.analysis import diff_field

        g1 = Grid(nx=2, ny=1, nlfp=10, gsize=1.0, west=0, east=2, south=0, north=1)
        g2 = Grid(nx=3, ny=1, nlfp=10, gsize=1.0, west=0, east=3, south=0, north=1)
        p1, p2 = tmp_path / "a.bin", tmp_path / "b.bin"
        np.zeros((1, 2), dtype="<f4").tofile(p1)
        np.zeros((1, 3), dtype="<f4").tofile(p2)
        with pytest.raises(ValueError, match="routing grid"):
            diff_field(OutputFile(p1, g1), OutputFile(p2, g2), 0)


@needs_mek
class TestFieldDiffEndToEnd:
    """A real scenario pair: baseline (LPTHOUT=True) vs no_bifurcation (LPTHOUT=False), built
    by examples/make_mekong_scenario.py -- everything else about the two cases is identical."""

    PROJECT = PKG / "CaMa_Floodcast" / "projects" / "mekong"

    @pytest.mark.skipif(not (PROJECT / "cases" / "no_bifurcation" / "run.json").exists(),
                        reason="no_bifurcation scenario not built")
    def test_bifurcation_measurably_changes_the_flow_field(self):
        from camafloodcast.core.analysis import diff_field

        grid = read_params(MAP_MEK / "params.txt")
        base = OutputFile(self.PROJECT / "cases" / "baseline" / "outflwmekong.bin", grid)
        scen = OutputFile(self.PROJECT / "cases" / "no_bifurcation" / "outflwno_bifurcation.bin",
                          grid)
        land = RiverMap(MAP_MEK).land_mask

        d = diff_field(base, scen, 297, mask=land)      # the October peak used elsewhere
        # Turning bifurcation off is documented as "essential for deltas and anabranching
        # rivers" (03-gui-design.md) -- it must move discharge somewhere by a real amount,
        # not just numerical noise.
        assert d.max_abs_diff > 100.0
        assert d.mean_abs_diff > 0.0

    @pytest.mark.skipif(not (PROJECT / "cases" / "no_bifurcation" / "run.json").exists(),
                        reason="no_bifurcation scenario not built")
    def test_far_upstream_gauges_are_unaffected(self):
        """Kratie and Pakse sit well upstream of the delta bifurcations, so turning them off
        must leave those two gauges' hydrographs identical -- the point being that a scenario
        diff is a spatial question, not just a single-gauge one."""
        from camafloodcast.core.analysis import extract_hydrograph

        grid = read_params(MAP_MEK / "params.txt")
        rmap = RiverMap(MAP_MEK)
        base = OutputFile(self.PROJECT / "cases" / "baseline" / "outflwmekong.bin", grid)
        scen = OutputFile(self.PROJECT / "cases" / "no_bifurcation" / "outflwno_bifurcation.bin",
                          grid)

        g = snap_gauge(Gauge("Kratie", 106.02, 12.48, 646000), rmap)
        hb = extract_hydrograph(base, g)
        hs = extract_hydrograph(scen, g)
        assert np.allclose(hb.values, hs.values)


class TestTraceDownstream:
    """nextxy values are 1-indexed (the Fortran convention), not 0-indexed like every numpy
    array elsewhere in this codebase -- verified empirically against the real Mekong map before
    writing trace_downstream at all. These synthetic maps exist to test the edge cases that
    would be awkward to provoke on the real network (an out-of-bounds pointer, a loop)."""

    def _write_map(self, tmp_path, nextxy_x, nextxy_y, nxtdst, elevtn, *, gsize=0.25):
        nextxy_x = np.asarray(nextxy_x)
        ny, nx = nextxy_x.shape
        d = tmp_path / "synthmap"
        d.mkdir()
        (d / "params.txt").write_text(
            f"{nx}\n{ny}\n10\n{gsize}\n0.0\n{nx * gsize}\n0.0\n{ny * gsize}\n")
        np.stack([nextxy_x, np.asarray(nextxy_y)]).astype("<i4").tofile(d / "nextxy.bin")
        np.asarray(nxtdst, dtype="<f4").tofile(d / "nxtdst.bin")
        np.asarray(elevtn, dtype="<f4").tofile(d / "elevtn.bin")
        return RiverMap(d)

    def test_follows_the_1_indexed_convention_to_the_mouth(self, tmp_path):
        from camafloodcast.core.analysis import trace_downstream

        # A 3-cell mainstem: (0,0) -> (1,0) -> (2,0) -> mouth. Stored 1-indexed: downstream of
        # 0-indexed (0,0) is 0-indexed (1,0), i.e. Fortran (2,1).
        nextxy_x = np.array([[2, 3, NEXTXY_MOUTH]])
        nextxy_y = np.array([[1, 1, NEXTXY_MOUTH]])
        rmap = self._write_map(tmp_path, nextxy_x, nextxy_y,
                               nxtdst=[[1000.0, 2000.0, 0.0]], elevtn=[[10.0, 5.0, 0.0]])

        path = trace_downstream(rmap, 0, 0)
        assert path == [(0, 0), (1, 0), (2, 0)]

    def test_out_of_bounds_target_is_a_clear_error(self, tmp_path):
        from camafloodcast.core.analysis import trace_downstream

        # Points at Fortran (5,1) -> 0-indexed (4,0), outside a 1x1 grid.
        rmap = self._write_map(tmp_path, [[5]], [[1]], nxtdst=[[1000.0]], elevtn=[[10.0]])
        with pytest.raises(ValueError, match="outside the grid"):
            trace_downstream(rmap, 0, 0)

    def test_a_loop_is_a_clear_error_not_an_infinite_loop(self, tmp_path):
        from camafloodcast.core.analysis import trace_downstream

        # (0,0) -> (1,0) -> (0,0) -> ... : each points at the other, 1-indexed.
        nextxy_x = np.array([[2, 1]])
        nextxy_y = np.array([[1, 1]])
        rmap = self._write_map(tmp_path, nextxy_x, nextxy_y,
                               nxtdst=[[1000.0, 1000.0]], elevtn=[[10.0, 10.0]])
        with pytest.raises(ValueError, match="loops"):
            trace_downstream(rmap, 0, 0)


@needs_mek
class TestLongitudinalProfileEndToEnd:
    """A real profile from Kratie to the delta outlet, on the real Mekong project."""

    PROJECT = PKG / "CaMa_Floodcast" / "projects" / "mekong"

    @pytest.mark.skipif(not (PROJECT / "cases" / "baseline" / "run.json").exists(),
                        reason="Mekong baseline case not built")
    def test_profile_descends_from_kratie_to_the_delta(self):
        from camafloodcast.core.analysis import longitudinal_profile, trace_downstream

        rmap = RiverMap(MAP_MEK)
        grid = read_params(MAP_MEK / "params.txt")
        g = snap_gauge(Gauge("Kratie", 106.02, 12.48, 646000), rmap)
        assert g.snapped

        path = trace_downstream(rmap, g.ix, g.iy)
        assert path[0] == (g.ix, g.iy)
        assert rmap.outlet_mask[path[-1][1], path[-1][0]], "path must end at a mouth or sink"

        up = np.asarray(rmap.layer("uparea"), dtype=np.float64)
        along = [up[iy, ix] for ix, iy in path]
        # Upstream area is non-decreasing along the flow direction by construction of the
        # network; a strict decrease anywhere would mean the trace went the wrong way.
        assert all(b >= a - 1.0 for a, b in zip(along, along[1:])), \
            "uparea must not decrease downstream"

        sfcelv = OutputFile(self.PROJECT / "cases" / "baseline" / "sfcelvmekong.bin", grid)
        profile = longitudinal_profile(rmap, path, sfcelv, 297)

        assert len(profile.distance_km) == len(path)
        assert profile.distance_km[-1] > profile.distance_km[0] == 0.0
        # Kratie sits well inland; the delta outlet is at sea level. A river runs downhill.
        assert profile.elevtn_m[0] > profile.elevtn_m[-1]
        assert profile.sfcelv_m[0] > profile.sfcelv_m[-1]
        # Real river distance from Kratie to the South China Sea is a few hundred km.
        assert 100.0 < profile.distance_km[-1] < 2000.0


# =========================================================================== project


class TestProject:
    def test_create_and_open(self, tmp_path):
        p = Project.create(tmp_path / "study", name="Test Study")
        assert (tmp_path / "study" / "project.json").exists()
        assert Project.open(tmp_path / "study").config.name == "Test Study"

    def test_time_axis_reads_start_date_and_output_interval(self, tmp_path):
        p = Project.create(tmp_path / "s")
        nml = new_namelist(["NSIMTIME", "NOUTPUT"])
        nml.update("NSIMTIME", dict(SYEAR=2000, SMON=7, SDAY=1, SHOUR=0,
                                    EYEAR=2001, EMON=1, EDAY=1, EHOUR=0))
        nml.set("NOUTPUT", "IFRQ_OUT", 24)
        c = p.add_case("baseline", namelist=nml)

        time = c.time_axis(count=10)
        assert time.count == 10
        assert time.date_of(0) == dt.datetime(2000, 7, 1)
        assert time.date_of(1) == dt.datetime(2000, 7, 2)

    def test_time_axis_falls_back_when_dates_are_absent(self, tmp_path):
        p = Project.create(tmp_path / "s")
        c = p.add_case("baseline", namelist=new_namelist(["NSIMTIME"]))
        time = c.time_axis(count=5)
        assert time.date_of(0) == dt.datetime(2000, 1, 1)

    def test_case_lifecycle(self, tmp_path):
        p = Project.create(tmp_path / "s")
        nml = new_namelist(["NDIMTIME"])
        nml.set("NDIMTIME", "DT", 3600)
        c = p.add_case("baseline", namelist=nml)
        assert c.exists()
        assert c.status() is CaseStatus.NOT_RUN
        assert [x.name for x in p.cases()] == ["baseline"]

    def test_duplicate_case_refused(self, tmp_path):
        p = Project.create(tmp_path / "s")
        nml = new_namelist(["NDIMTIME"])
        nml.set("NDIMTIME", "DT", 3600)
        p.add_case("a", namelist=nml)
        with pytest.raises(FileExistsError):
            p.add_case("a", namelist=nml)

    def test_stale_detection(self, tmp_path):
        """The headline provenance feature: results that no longer match their config."""
        p = Project.create(tmp_path / "s")
        nml = new_namelist(["NDIMTIME"])
        nml.set("NDIMTIME", "DT", 3600)
        c = p.add_case("baseline", namelist=nml)

        c.save_record(RunRecord(status=CaseStatus.COMPLETED, input_hash=c.input_hash()))
        assert c.status() is CaseStatus.COMPLETED

        edited = c.namelist()
        edited.set("NDIMTIME", "DT", 1800)
        edited.save(backup=False)
        assert c.status() is CaseStatus.STALE

    def test_comment_change_does_not_make_results_stale(self, tmp_path):
        """Hashing by value, not bytes: reformatting must not invalidate a run."""
        p = Project.create(tmp_path / "s")
        nml = new_namelist(["NDIMTIME"])
        nml.set("NDIMTIME", "DT", 3600)
        c = p.add_case("baseline", namelist=nml)
        c.save_record(RunRecord(status=CaseStatus.COMPLETED, input_hash=c.input_hash()))

        text = c.namelist_path.read_text(encoding="utf-8")
        c.namelist_path.write_text("! a note to myself\n" + text, encoding="utf-8")
        assert c.status() is CaseStatus.COMPLETED


class TestBatch:
    """Parameter sweeps: N cases differing in exactly one namelist field."""

    def _base(self, tmp_path) -> tuple[Project, Case]:
        p = Project.create(tmp_path / "s")
        nml = new_namelist(["NRUNVER", "NPARAM", "NOUTPUT"])
        nml.set("NPARAM", "PMANFLD", 0.10)
        nml.set("NOUTPUT", "COUTTAG", "base")
        c = p.add_case("baseline", namelist=nml)
        return p, c

    def test_slugify_is_filesystem_and_tag_safe(self):
        from camafloodcast.core.batch import slugify

        assert slugify(0.15) == "0p15"
        assert slugify(-5) == "m5"
        assert slugify("a b") == "a_b"

    def test_spec_rejects_an_unknown_field(self):
        from camafloodcast.core.batch import SweepSpec

        with pytest.raises(ValueError, match="schema"):
            SweepSpec("NPARAM", "NOT_A_REAL_FIELD", (1, 2))

    def test_spec_rejects_no_values(self):
        from camafloodcast.core.batch import SweepSpec

        with pytest.raises(ValueError, match="at least one value"):
            SweepSpec("NPARAM", "PMANFLD", ())

    def test_parse_sweep_values_by_field_kind(self):
        from camafloodcast.core import schema
        from camafloodcast.core.batch import parse_sweep_values

        assert parse_sweep_values(schema.field_for("NPARAM", "PMANFLD"), "0.05, 0.10, 0.20") \
            == [0.05, 0.10, 0.20]
        assert parse_sweep_values(schema.field_for("NDIMTIME", "IFRQ_INP"), "1,6,24") == [1, 6, 24]
        assert parse_sweep_values(schema.field_for("NRUNVER", "LPTHOUT"), "true, false, .T.") \
            == [True, False, True]

    def test_parse_sweep_values_rejects_empty_input(self):
        from camafloodcast.core import schema
        from camafloodcast.core.batch import parse_sweep_values

        with pytest.raises(ValueError, match="no values"):
            parse_sweep_values(schema.field_for("NPARAM", "PMANFLD"), "  , ,")

    def test_build_sweep_creates_one_case_per_value_varying_only_the_swept_field(self, tmp_path):
        from camafloodcast.core.batch import SweepSpec, build_sweep

        proj, base = self._base(tmp_path)
        spec = SweepSpec("NPARAM", "PMANFLD", (0.05, 0.10, 0.20))
        cases = build_sweep(proj, base, spec)

        # str(0.10) is "0.1" in Python (no trailing zero) -- slugify follows that rather than
        # imposing its own zero-padding, so this is the real, correct name, not a rounded one.
        assert [c.name for c in cases] == ["pmanfld_0p05", "pmanfld_0p1", "pmanfld_0p2"]
        for c, value in zip(cases, spec.values):
            nml = c.namelist()
            assert nml.get("NPARAM", "PMANFLD") == pytest.approx(value)
            # Every other field is untouched, including the ones not explicitly set by _base.
            assert nml.get("NOUTPUT", "COUTTAG") == c.name
        # COUTTAGs are unique so output files from different sweep cases can never collide,
        # even though every case also lives in its own directory.
        assert len({c.namelist().get("NOUTPUT", "COUTTAG") for c in cases}) == 3

    def test_build_sweep_refuses_to_silently_overwrite(self, tmp_path):
        from camafloodcast.core.batch import SweepSpec, build_sweep

        proj, base = self._base(tmp_path)
        spec = SweepSpec("NPARAM", "PMANFLD", (0.05,))
        build_sweep(proj, base, spec)
        with pytest.raises(FileExistsError):
            build_sweep(proj, base, spec)
        # But will replace it when explicitly told to.
        build_sweep(proj, base, spec, overwrite=True)

    def test_build_sweep_needs_a_real_base_case(self, tmp_path):
        from camafloodcast.core.batch import SweepSpec, build_sweep

        proj, _ = self._base(tmp_path)
        ghost = proj.case("does_not_exist")
        with pytest.raises(FileNotFoundError):
            build_sweep(proj, ghost, SweepSpec("NPARAM", "PMANFLD", (0.05,)))


class TestDams:
    """core.dams: the CDAMFILE reservoir-parameter format, verified against
    src/cmf_ctrl_damout_mod.F90."""

    def _record(self, **overrides):
        from camafloodcast.core.dams import DamRecord

        kw = dict(dam_id=1, name="Test Dam", lat=27.0, lon=99.0, upreal_km2=80000.0,
                 ix=23, iy=24, fldvol_mcm=70.0, convol_mcm=120.0, totvol_mcm=190.0,
                 qn=850.0, qf=2400.0)
        kw.update(overrides)
        return DamRecord(**kw)

    def test_round_trip_preserves_1_indexed_conversion(self, tmp_path):
        from camafloodcast.core.dams import read_dam_file, write_dam_file

        rec = self._record(ix=5, iy=10)
        path = tmp_path / "dam_params.csv"
        write_dam_file([rec], path)

        # The file itself must store 1-indexed coordinates (Fortran convention) -- verified
        # directly, not just round-tripped, so a bug that shifted both write and read the same
        # way wouldn't hide behind a passing round trip.
        line = path.read_text(encoding="utf-8").splitlines()[2]
        fields = [f.strip() for f in line.split(",")]
        assert fields[5] == "6" and fields[6] == "11"

        back = read_dam_file(path)
        assert len(back) == 1
        assert (back[0].ix, back[0].iy) == (5, 10)
        assert back[0].name == rec.name
        assert back[0].totvol_mcm == pytest.approx(rec.totvol_mcm)

    def test_write_refuses_no_records(self, tmp_path):
        from camafloodcast.core.dams import write_dam_file

        with pytest.raises(ValueError, match="no dam records"):
            write_dam_file([], tmp_path / "d.csv")

    def test_read_rejects_a_row_count_mismatch(self, tmp_path):
        from camafloodcast.core.dams import write_dam_file, read_dam_file

        path = tmp_path / "dam_params.csv"
        write_dam_file([self._record(), self._record(dam_id=2)], path)
        # Corrupt the declared count so it no longer matches the data rows.
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = "5"
        path.write_text("\n".join(lines), encoding="utf-8")
        with pytest.raises(ValueError, match="only"):
            read_dam_file(path)

    @needs_mek
    def test_write_validates_allocation_against_a_real_map(self, tmp_path):
        from camafloodcast.core.dams import write_dam_file

        rmap = RiverMap(MAP_MEK)
        grid = rmap.grid

        with pytest.raises(ValueError, match="outside"):
            write_dam_file([self._record(ix=grid.nx + 50, iy=0)], tmp_path / "d.csv",
                           river_map=rmap)

        # A real, verified non-land cell in this map (land_mask is "part of the routing
        # network", not "not ocean" -- most of a regional cut is neither).
        non_land = tuple(zip(*np.where(~rmap.land_mask)))[0]
        with pytest.raises(ValueError, match="not a river cell"):
            write_dam_file([self._record(ix=int(non_land[1]), iy=int(non_land[0]))],
                           tmp_path / "d.csv", river_map=rmap)

    @needs_mek
    def test_the_five_real_lancang_dams_snap_well(self):
        """The real GRanD records for Wunonglong/Lidi/Huangdeng/Dahuaqiao/Miaowei
        (map/data/GRanD_alloc+Mekong.csv), snapped by drainage area the same way a gauge is."""
        rmap = RiverMap(MAP_MEK)
        dams = [
            ("Wunonglong", 27.939, 98.924, 79787.0),
            ("Lidi", 27.848, 99.031, 80088.867),
            ("Huangdeng", 26.559, 99.116, 85886.0),
            ("Dahuaqiao", 26.341, 99.143, 86353.102),
            ("Miaowei", 25.854, 99.161, 87596.398),
        ]
        for name, lat, lon, area in dams:
            g = snap_gauge(Gauge(name, lon, lat, area_km2=area), rmap, radius_cells=3)
            assert g.snapped, name
            assert g.area_error < 0.01, f"{name}: {g.match_quality}"

    def test_inflow_stats_from_a_synthetic_series(self, tmp_path):
        from camafloodcast.core.dams import inflow_stats

        grid = Grid(nx=2, ny=1, nlfp=10, gsize=1.0, west=0, east=2, south=0, north=1)
        # outflwT.bin so split_filename reads the variable as "outflw".
        path = tmp_path / "outflwT.bin"
        series = np.array([100.0, 200.0, 300.0, 900.0], dtype="<f4")   # one value per day
        np.tile(series[:, None], (1, 2)).astype("<f4").tofile(path)
        f = OutputFile(path, grid)

        qn, qf = inflow_stats(f, 0, 0, flood_percentile=75.0)
        assert qn == pytest.approx(series.mean())
        assert qf == pytest.approx(np.percentile(series, 75.0))
        assert qf > qn


@needs_mek
class TestDamsEndToEnd:
    """A real reservoir scenario: 5 real Lancang dams (Wunonglong, Lidi, Huangdeng, Dahuaqiao,
    Miaowei), built by examples/make_mekong_dams.py, run for real with LDAMOUT=True."""

    PROJECT = PKG / "CaMa_Floodcast" / "projects" / "mekong"

    @pytest.mark.skipif(not (PROJECT / "cases" / "with_dams" / "run.json").exists(),
                        reason="with_dams scenario not built (examples/make_mekong_dams.py)")
    def test_reservoirs_measurably_change_the_flow_field(self):
        from camafloodcast.core.analysis import diff_field

        grid = read_params(MAP_MEK / "params.txt")
        base = OutputFile(self.PROJECT / "cases" / "baseline" / "rivdphmekong.bin", grid)
        dams = OutputFile(self.PROJECT / "cases" / "with_dams" / "rivdphwith_dams.bin", grid)
        land = RiverMap(MAP_MEK).land_mask

        d = diff_field(base, dams, 265, mask=land)     # the day of the largest reservoir's peak
        assert d.max_abs_diff > 10.0, "a reservoir pooling water must show up as a real depth change"

    @pytest.mark.skipif(not (PROJECT / "cases" / "with_dams" / "run.json").exists(),
                        reason="with_dams scenario not built (examples/make_mekong_dams.py)")
    def test_the_largest_dams_reservoir_pool_is_the_deepest_cell_in_its_own_domain(self):
        """Huangdeng (992.6 Mm3, the largest of the five) should show the single deepest
        pooled channel cell anywhere in the with_dams run -- a real reservoir raising the
        local water level far above a natural channel, not a numerical artefact, since it
        lands exactly on the dam's own allocated cell."""
        grid = read_params(MAP_MEK / "params.txt")
        rivdph = OutputFile(self.PROJECT / "cases" / "with_dams" / "rivdphwith_dams.bin", grid)
        land = RiverMap(MAP_MEK).land_mask

        best = (-1.0, -1, -1)
        for r in range(rivdph.nrec):
            arr = np.where(land, np.asarray(rivdph.record(r)), -1.0)
            iy, ix = np.unravel_index(np.argmax(arr), arr.shape)
            v = float(arr[iy, ix])
            if v > best[0]:
                best = (v, ix, iy)

        _, ix, iy = best
        assert (ix, iy) == (24, 29), "expected the deepest cell to be Huangdeng's own allocation"


# =========================================================================== adapter


class TestAdapter:
    def test_implements_the_interface(self):
        from camafloodcast.adapters.base import ModelAdapter
        from camafloodcast.adapters.camaflood import CamaFloodAdapter
        a = CamaFloodAdapter()
        assert isinstance(a, ModelAdapter)
        assert a.log_filename() == "log_CaMa.txt"

    def test_mvp_needs_exactly_one_binary(self):
        """The toolchain is tiered: the MVP needs only MAIN_cmf, like RRI."""
        from camafloodcast.adapters.camaflood import CamaFloodAdapter
        mvp = [b for b in CamaFloodAdapter().required_binaries()
               if b.required_from_phase == "mvp"]
        assert len(mvp) == 1
        assert mvp[0].filename == "MAIN_cmf"

    def test_domain_phase_binaries(self):
        from camafloodcast.adapters.camaflood import CamaFloodAdapter
        names = {b.filename for b in CamaFloodAdapter().required_binaries()
                 if b.required_from_phase == "domain"}
        assert {"cut_domain", "set_map", "generate_inpmat", "set_bifparam"} <= names

    def test_log_line_becomes_a_progress_event(self):
        from camafloodcast.adapters.camaflood import CamaFloodAdapter
        ev = CamaFloodAdapter().parse_log_line(TestLogParser.WB)
        assert ev is not None and ev.kind == "balance"
        assert ev.payload["healthy"] is True


# =========================================================================== forcing (Phase 2)

RUNOFF_1DEG = PKG / "inp" / "test_1deg" / "runoff"
needs_runoff = pytest.mark.skipif(not RUNOFF_1DEG.exists(),
                                  reason="1-degree test runoff not available")


@needs_runoff
class TestForcing:
    """Gate 2: units, coverage, and orientation -- the three ways forcing fails silently."""

    def test_scan_finds_the_real_files(self):
        from camafloodcast.core.forcing import ForcingSpec, scan_directory

        spec = ForcingSpec(directory=RUNOFF_1DEG)
        report = scan_directory(spec)
        assert report.n_files == 731
        assert report.first_day == dt.date(2000, 1, 1)
        assert report.last_day == dt.date(2001, 12, 31)
        assert not report.missing
        assert report.inferred_unit == "mm/day"

    def test_audit_passes_a_covered_period_in_the_right_unit(self):
        from camafloodcast.core.forcing import ForcingSpec, audit

        spec = ForcingSpec(directory=RUNOFF_1DEG)
        d = audit(spec, dt.date(2000, 1, 1), dt.date(2000, 1, 6))
        assert d.ok
        assert any(c.code == "FRC_UNITS_OK" for c in d)

    def test_audit_catches_a_gap(self):
        """CaMa-Flood does not stop for a missing day -- it produces a plausible but wrong
        recession. This is the check that stands in for it not stopping."""
        from camafloodcast.core.forcing import ForcingSpec, audit

        spec = ForcingSpec(directory=RUNOFF_1DEG)
        d = audit(spec, dt.date(1999, 12, 25), dt.date(2000, 1, 3))
        assert not d.ok
        assert any(c.code == "FRC_COVERAGE" for c in d)

    def test_audit_catches_wrong_units(self):
        """The same files read as mm/s -- 1000x too small a divisor -- must not pass silently."""
        from camafloodcast.core.forcing import ForcingSpec, audit

        spec = ForcingSpec(directory=RUNOFF_1DEG, unit="mm/s")
        d = audit(spec, dt.date(2000, 1, 1), dt.date(2000, 1, 6))
        assert not d.ok
        assert any(c.code == "FRC_UNITS" for c in d)

    def test_read_day_matches_the_declared_grid(self):
        from camafloodcast.core.forcing import ForcingSpec, read_day

        spec = ForcingSpec(directory=RUNOFF_1DEG)
        arr = read_day(spec, dt.date(2000, 1, 1))
        assert arr.shape == (180, 360)
        assert np.isfinite(arr).all()

    def test_read_day_rejects_a_size_mismatch(self):
        from camafloodcast.core.forcing import ForcingSpec, read_day

        spec = ForcingSpec(directory=RUNOFF_1DEG, nx=100, ny=100)   # wrong grid for these files
        with pytest.raises(FileFormatError):
            read_day(spec, dt.date(2000, 1, 1))

    def test_infer_unit_recognises_a_typical_daily_value(self):
        from camafloodcast.core.forcing import infer_unit

        assert infer_unit(2.0) == "mm/day"
        assert infer_unit(-1.0) is None

    def test_interpolation_advice_exact_match_needs_no_matrix(self):
        from camafloodcast.core.forcing import ForcingSpec, interpolation_advice
        from camafloodcast.core.maps import Grid

        grid = Grid(nx=360, ny=180, nlfp=10, gsize=1.0,
                   west=-180, east=180, south=-90, north=90)
        spec = ForcingSpec(directory=RUNOFF_1DEG, nx=360, ny=180)
        d = interpolation_advice(spec, grid)
        assert any(c.code == "FRC_DIRECT_OK" for c in d)

    def test_interpolation_advice_compares_cell_size_not_column_count(self):
        """Regression: a global forcing grid and a small regional routing window at the SAME
        cell size must be reported as the same resolution, not compared by raw column count
        (360 vs 20), which was the bug in the earlier wording."""
        from camafloodcast.core.forcing import ForcingSpec, interpolation_advice
        from camafloodcast.core.maps import Grid

        grid = Grid(nx=20, ny=15, nlfp=10, gsize=1.0, west=100, east=120, south=5, north=20)
        spec = ForcingSpec(directory=RUNOFF_1DEG, nx=360, ny=180)      # global, 1 deg
        d = interpolation_advice(spec, grid)
        msg = next(c.message for c in d if c.code == "FRC_NEEDS_INPMAT")
        assert "same resolution" in msg


class TestForcingDownload:
    """core.forcing_download: collapsing sub-daily ERA5-Land/GLDAS runoff into a daily total --
    the part core.forcing.netcdf_to_binary does not do itself (it assumes each time index is
    already one calendar day). The network calls themselves are mocked (no live CDS/Earthdata
    account exists in this environment); the aggregation math is exercised against real,
    synthetic-but-known-value netCDF files written locally."""

    def _write_nc(self, path, *, lat, lon, time_vals, time_units, varname, data, units):
        import netCDF4 as nc

        ds = nc.Dataset(path, "w", format="NETCDF4")
        try:
            ds.createDimension("time", len(time_vals))
            ds.createDimension("lat", len(lat))
            ds.createDimension("lon", len(lon))
            v_lat = ds.createVariable("lat", "f8", ("lat",))
            v_lat[:] = lat
            v_lon = ds.createVariable("lon", "f8", ("lon",))
            v_lon[:] = lon
            v_time = ds.createVariable("time", "f8", ("time",))
            v_time.units = time_units
            v_time.calendar = "standard"
            v_time[:] = time_vals
            v = ds.createVariable(varname, "f4", ("time", "lat", "lon"))
            v.units = units
            v[:] = data.astype("f4")
        finally:
            ds.close()

    def _write_gldas_step(self, path, *, lat, lon, hour, qs, qsb):
        import netCDF4 as nc

        ds = nc.Dataset(path, "w", format="NETCDF4")
        try:
            ds.createDimension("time", 1)
            ds.createDimension("lat", len(lat))
            ds.createDimension("lon", len(lon))
            v_lat = ds.createVariable("lat", "f8", ("lat",))
            v_lat[:] = lat
            v_lon = ds.createVariable("lon", "f8", ("lon",))
            v_lon[:] = lon
            v_time = ds.createVariable("time", "f8", ("time",))
            v_time.units = "hours since 2000-01-01 00:00:00"
            v_time.calendar = "standard"
            v_time[:] = [float(hour)]
            v_qs = ds.createVariable("Qs_tavg", "f4", ("time", "lat", "lon"))
            v_qs.units = "kg/m2/s"
            v_qs[:] = np.full((1, len(lat), len(lon)), qs, dtype="f4")
            v_qsb = ds.createVariable("Qsb_tavg", "f4", ("time", "lat", "lon"))
            v_qsb.units = "kg/m2/s"
            v_qsb[:] = np.full((1, len(lat), len(lon)), qsb, dtype="f4")
        finally:
            ds.close()

    def test_era5_land_daily_totals_sums_across_the_day_boundary(self, tmp_path):
        pytest.importorskip("netCDF4")
        from camafloodcast.core.forcing_download import era5_land_daily_totals

        lat = np.array([10.0, 9.75])
        lon = np.array([100.0, 100.25])
        cell_factor = np.array([[1.0, 2.0], [0.5, 1.5]])

        hours = np.arange(48)
        # Day 1: accumulates 0.002 m/hr, resets, day 2 accumulates 0.001 m/hr -- deliberately
        # different so a boundary leak (day 2 inheriting day 1's total) is obvious.
        day1 = (hours[:24] + 1) * 0.002
        day2 = (hours[24:] - 24 + 1) * 0.001
        accum = np.concatenate([day1, day2])
        data = accum[:, None, None] * cell_factor[None, :, :]

        raw = tmp_path / "era5land_hourly.nc"
        self._write_nc(raw, lat=lat, lon=lon, time_vals=hours.astype("f8"),
                       time_units="hours since 2000-01-01 00:00:00",
                       varname="ro", data=data, units="m")

        out = tmp_path / "era5land_daily.nc"
        n = era5_land_daily_totals(raw, out)
        assert n == 2

        import netCDF4 as nc

        ds = nc.Dataset(out)
        try:
            assert ds.variables["runoff"].units == "mm/day"
            runoff = np.asarray(ds.variables["runoff"][:])
        finally:
            ds.close()
        assert runoff.shape == (2, 2, 2)
        assert np.allclose(runoff[0], 0.048 * 1000 * cell_factor)   # day 1's own peak, in mm
        assert np.allclose(runoff[1], 0.024 * 1000 * cell_factor)   # day 2, not day 1's total

    def test_era5_land_daily_totals_handles_the_modern_cds_variable_names(self, tmp_path):
        """Found against the real CDS API (2026-09-22): its current infrastructure names the
        coordinates valid_time/latitude/longitude (plus extra number/expver variables), not the
        time/lat/lon this module's own output (and older-style downloads) use. Reproduces that
        exact real response shape rather than the idealised lat/lon/time one the other test uses."""
        pytest.importorskip("netCDF4")
        import netCDF4 as nc

        from camafloodcast.core.forcing_download import era5_land_daily_totals

        raw = tmp_path / "era5land_real_shape.nc"
        ds = nc.Dataset(raw, "w", format="NETCDF4")
        try:
            ds.createDimension("valid_time", 24)
            ds.createDimension("latitude", 2)
            ds.createDimension("longitude", 2)
            ds.createDimension("number", 1)
            v_lat = ds.createVariable("latitude", "f8", ("latitude",))
            v_lat[:] = [10.0, 9.75]
            v_lon = ds.createVariable("longitude", "f8", ("longitude",))
            v_lon[:] = [100.0, 100.25]
            v_time = ds.createVariable("valid_time", "f8", ("valid_time",))
            v_time.units = "hours since 2000-01-01 00:00:00"
            v_time.calendar = "standard"
            v_time[:] = np.arange(24, dtype="f8")
            v_number = ds.createVariable("number", "i4", ("number",))
            v_number[:] = [0]
            v_ro = ds.createVariable("ro", "f4", ("valid_time", "latitude", "longitude"))
            accum = ((np.arange(24) + 1) * 0.002)[:, None, None]
            v_ro[:] = np.broadcast_to(accum, (24, 2, 2)).astype("f4")
        finally:
            ds.close()

        out = tmp_path / "era5land_real_shape_daily.nc"
        n = era5_land_daily_totals(raw, out)
        assert n == 1

        ds = nc.Dataset(out)
        try:
            runoff = np.asarray(ds.variables["runoff"][:])
        finally:
            ds.close()
        assert runoff.shape == (1, 2, 2)
        assert np.allclose(runoff, 0.048 * 1000)   # 24th hour's accumulation, in mm

    def test_era5_land_download_unwraps_a_zip_response(self, tmp_path, monkeypatch):
        """Found against the real CDS API (2026-09-22): 'format': 'netcdf' can still come back
        as a zip archive containing one .nc file, undocumented anywhere obvious. Reproduced here
        with a real zip wrapping a real (tiny) netCDF file, matching exactly what the live
        service returned."""
        pytest.importorskip("netCDF4")
        import sys as _sys
        import types
        import zipfile

        from camafloodcast.core.forcing_download import DownloadRequest, era5_land_download

        inner = tmp_path / "data_0.nc"
        self._write_nc(inner, lat=np.array([10.0]), lon=np.array([100.0]),
                       time_vals=np.array([0.0]), time_units="hours since 2000-01-01 00:00:00",
                       varname="ro", data=np.array([[[0.001]]]), units="m")

        class FakeClient:
            def retrieve(self, dataset, params, target):
                with zipfile.ZipFile(target, "w") as zf:
                    zf.write(inner, arcname="data_0.nc")

        fake_module = types.SimpleNamespace(Client=lambda: FakeClient())
        monkeypatch.setitem(_sys.modules, "cdsapi", fake_module)

        request = DownloadRequest(source="era5-land", west=100.0, south=8.0, east=105.0,
                                  north=12.0, gsize=0.25,
                                  start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 1))
        out = era5_land_download(request, tmp_path / "out")

        assert not zipfile.is_zipfile(out)
        import netCDF4 as nc

        ds = nc.Dataset(out)
        try:
            assert "ro" in ds.variables
        finally:
            ds.close()

    def test_gldas_daily_totals_sums_qs_and_qsb_over_eight_steps(self, tmp_path):
        pytest.importorskip("netCDF4")
        from camafloodcast.core.forcing_download import DownloadRequest, gldas_daily_totals

        lat = np.array([9.0, 9.25, 9.5, 9.75, 10.0])
        lon = np.array([99.5, 99.75, 100.0, 100.25, 100.5])
        qs_rate, qsb_rate = 1e-5, 2e-6                              # kg/m2/s

        raw_paths = []
        for h in range(0, 24, 3):
            p = tmp_path / f"gldas_{h:02d}.nc4"
            self._write_gldas_step(p, lat=lat, lon=lon, hour=h, qs=qs_rate, qsb=qsb_rate)
            raw_paths.append(p)

        request = DownloadRequest(source="gldas", west=99.9, south=9.1, east=100.4, north=9.9,
                                  gsize=0.25, start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 1))
        out = tmp_path / "gldas_daily.nc"
        n = gldas_daily_totals(raw_paths, out, request)
        assert n == 1

        import netCDF4 as nc

        ds = nc.Dataset(out)
        try:
            assert ds.variables["runoff"].units == "mm/day"
            runoff = np.asarray(ds.variables["runoff"][:])
            out_lat = np.asarray(ds.variables["lat"][:])
            out_lon = np.asarray(ds.variables["lon"][:])
        finally:
            ds.close()
        # Cropped to the request bounds: lat 9.25/9.5/9.75, lon 100.0/100.25.
        assert list(out_lat) == [9.25, 9.5, 9.75]
        assert list(out_lon) == [100.0, 100.25]
        expected_mm = (qs_rate + qsb_rate) * 3 * 3600 * 8            # 8 three-hourly steps
        assert runoff.shape == (1, 3, 2)
        assert np.allclose(runoff, expected_mm, rtol=1e-6)

    def test_credentials_status_reports_missing_files(self, tmp_path, monkeypatch):
        from camafloodcast.core.forcing_download import credentials_status

        monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
        assert credentials_status(home=tmp_path) == {
            "era5-land": False, "gldas": False,
            "gee": False, "era5-land-gee": False, "gldas-gee": False}

    def test_credentials_status_reports_present_files(self, tmp_path, monkeypatch):
        from camafloodcast.core.forcing_download import (
            EARTHDATA_HOST,
            credentials_status,
            save_gee_project,
        )

        monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
        (tmp_path / ".cdsapirc").write_text(
            "url: https://cds.climate.copernicus.eu/api\nkey: 12345\n")
        netrc_name = "_netrc" if sys.platform == "win32" else ".netrc"
        (tmp_path / netrc_name).write_text(
            f"machine {EARTHDATA_HOST}\nlogin someone\npassword secret\n")
        ee_dir = tmp_path / ".config" / "earthengine"
        ee_dir.mkdir(parents=True)
        (ee_dir / "credentials").write_text("{}")
        save_gee_project("my-project", home=tmp_path)

        assert credentials_status(home=tmp_path) == {
            "era5-land": True, "gldas": True,
            "gee": True, "era5-land-gee": True, "gldas-gee": True}

    def test_missing_cdsapi_raises_a_clear_error(self, tmp_path, monkeypatch):
        import builtins

        from camafloodcast.core.errors import FileFormatError
        from camafloodcast.core.forcing_download import DownloadRequest, era5_land_download

        real_import = builtins.__import__

        def blocked_import(name, *a, **kw):
            if name == "cdsapi":
                raise ImportError("blocked for this test")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        request = DownloadRequest(source="era5-land", west=100.0, south=8.0, east=105.0,
                                  north=12.0, gsize=0.25,
                                  start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 1))
        with pytest.raises(FileFormatError, match="download"):
            era5_land_download(request, tmp_path)

    def test_missing_earthaccess_raises_a_clear_error(self, tmp_path, monkeypatch):
        import builtins

        from camafloodcast.core.errors import FileFormatError
        from camafloodcast.core.forcing_download import DownloadRequest, gldas_download

        real_import = builtins.__import__

        def blocked_import(name, *a, **kw):
            if name == "earthaccess":
                raise ImportError("blocked for this test")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        request = DownloadRequest(source="gldas", west=100.0, south=8.0, east=105.0, north=12.0,
                                  gsize=0.25, start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 1))
        with pytest.raises(FileFormatError, match="download"):
            gldas_download(request, tmp_path)

    def test_era5_land_download_builds_the_right_cds_request(self, tmp_path, monkeypatch):
        import sys as _sys
        import types

        from camafloodcast.core.forcing_download import DownloadRequest, era5_land_download

        captured = {}

        class FakeClient:
            def retrieve(self, dataset, params, target):
                captured["dataset"] = dataset
                captured["params"] = params
                Path(target).write_bytes(b"")

        fake_module = types.SimpleNamespace(Client=lambda: FakeClient())
        monkeypatch.setitem(_sys.modules, "cdsapi", fake_module)

        request = DownloadRequest(source="era5-land", west=93.0, south=8.0, east=110.0,
                                  north=34.0, gsize=0.25,
                                  start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 3))
        out = era5_land_download(request, tmp_path)

        assert captured["dataset"] == "reanalysis-era5-land"
        p = captured["params"]
        assert p["variable"] == "runoff"
        assert p["area"] == [34.0, 93.0, 8.0, 110.0]
        assert p["grid"] == "0.25/0.25"
        assert sorted(p["day"]) == ["01", "02", "03"]
        assert p["year"] == ["2000"] and p["month"] == ["01"]
        assert len(p["time"]) == 24
        assert out == tmp_path / "era5_land_hourly_runoff.nc"

    def test_gldas_download_searches_with_the_right_parameters(self, tmp_path, monkeypatch):
        import sys as _sys
        import types

        from camafloodcast.core.forcing_download import DownloadRequest, gldas_download

        captured = {}

        def fake_login(**kw):
            captured["login_kw"] = kw

        def fake_search_data(**kw):
            captured["search_kw"] = kw
            return ["granule1", "granule2"]

        def fake_download(results, out_dir):
            return [str(Path(out_dir) / "g1.nc4"), str(Path(out_dir) / "g2.nc4")]

        fake_module = types.SimpleNamespace(login=fake_login, search_data=fake_search_data,
                                            download=fake_download)
        monkeypatch.setitem(_sys.modules, "earthaccess", fake_module)

        request = DownloadRequest(source="gldas", west=93.0, south=8.0, east=110.0, north=34.0,
                                  gsize=0.25, start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 3))
        paths = gldas_download(request, tmp_path)

        assert captured["login_kw"] == {"strategy": "netrc"}
        assert captured["search_kw"]["short_name"] == "GLDAS_NOAH025_3H"
        assert captured["search_kw"]["bounding_box"] == (93.0, 8.0, 110.0, 34.0)
        assert captured["search_kw"]["temporal"] == ("2000-01-01", "2000-01-03")
        assert paths == [tmp_path / "g1.nc4", tmp_path / "g2.nc4"]

    # ----------------------------------------------------------- Google Earth Engine

    def test_gee_project_round_trips_through_the_settings_file(self, tmp_path, monkeypatch):
        from camafloodcast.core.forcing_download import (
            gee_project_path,
            get_gee_project,
            save_gee_project,
        )

        monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
        assert get_gee_project(home=tmp_path) is None

        save_gee_project("my-project", home=tmp_path)
        assert gee_project_path(home=tmp_path).is_file()
        assert get_gee_project(home=tmp_path) == "my-project"

    def test_gee_project_env_var_takes_priority_over_the_file(self, tmp_path, monkeypatch):
        from camafloodcast.core.forcing_download import get_gee_project, save_gee_project

        save_gee_project("file-project", home=tmp_path)
        monkeypatch.setenv("EARTHENGINE_PROJECT", "env-project")
        assert get_gee_project(home=tmp_path) == "env-project"

    def test_credentials_status_covers_the_gee_sources(self, tmp_path, monkeypatch):
        from camafloodcast.core.forcing_download import credentials_status, save_gee_project

        monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
        status = credentials_status(home=tmp_path)
        assert status["gee"] is False
        assert status["era5-land-gee"] is False
        assert status["gldas-gee"] is False

        ee_dir = tmp_path / ".config" / "earthengine"
        ee_dir.mkdir(parents=True)
        (ee_dir / "credentials").write_text("{}")
        assert credentials_status(home=tmp_path)["gee"] is False   # signed in, but no project

        save_gee_project("my-project", home=tmp_path)
        status = credentials_status(home=tmp_path)
        assert status["gee"] is True
        assert status["era5-land-gee"] is True
        assert status["gldas-gee"] is True

    def test_missing_earthengine_api_raises_a_clear_error(self, tmp_path, monkeypatch):
        import builtins

        from camafloodcast.core.errors import FileFormatError
        from camafloodcast.core.forcing_download import DownloadRequest, era5_land_gee_download

        monkeypatch.setenv("EARTHENGINE_PROJECT", "irrelevant-here")
        real_import = builtins.__import__

        def blocked_import(name, *a, **kw):
            if name == "rasterio":
                raise ImportError("blocked for this test")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        request = DownloadRequest(source="era5-land-gee", west=100.0, south=8.0, east=100.5,
                                  north=8.5, gsize=0.25,
                                  start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 1))
        with pytest.raises(FileFormatError, match="geo"):
            era5_land_gee_download(request, tmp_path)

    def test_gee_download_without_a_project_raises_a_clear_error(self, tmp_path, monkeypatch):
        from camafloodcast.core.errors import FileFormatError
        from camafloodcast.core.forcing_download import DownloadRequest, era5_land_gee_download

        pytest.importorskip("rasterio")
        monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
        monkeypatch.setattr("camafloodcast.core.forcing_download.Path.home", lambda: tmp_path)
        request = DownloadRequest(source="era5-land-gee", west=100.0, south=8.0, east=100.5,
                                  north=8.5, gsize=0.25,
                                  start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 1))
        with pytest.raises(FileFormatError, match="project"):
            era5_land_gee_download(request, tmp_path)

    def _fake_ee_module(self, captured, image_for_day):
        import types

        class FakeCollection:
            def __init__(self, collection_id):
                captured["collection_id"] = collection_id
                self._day = None

            def filterDate(self, start_iso, end_iso):
                captured.setdefault("filterDate_calls", []).append((start_iso, end_iso))
                self._day = dt.date.fromisoformat(start_iso)
                return self

            def select(self, bands):
                captured["bands"] = bands
                return self

            def sum(self):
                return image_for_day(self._day)

        return types.SimpleNamespace(
            Initialize=lambda project=None: captured.__setitem__("init_project", project),
            ImageCollection=FakeCollection,
        )

    def _write_tif(self, path, data, *, west, north, gsize):
        import rasterio
        from rasterio.transform import from_origin

        transform = from_origin(west, north, gsize, gsize)
        with rasterio.open(
            path, "w", driver="GTiff", height=data.shape[1], width=data.shape[2],
            count=data.shape[0], dtype="float64", crs="EPSG:4326", transform=transform,
        ) as dst:
            for b in range(data.shape[0]):
                dst.write(data[b], b + 1)

    def test_era5_land_gee_download_samples_onto_the_exact_grid_and_converts_units(
            self, tmp_path, monkeypatch):
        """crsTransform/dimensions must be built from the request's own bounds+gsize (exact-grid
        sampling, no separate alignment check needed downstream), and runoff_hourly (metres) ->
        mm/day needs the same x1000 conversion the raw-CDS path does."""
        import urllib.request

        pytest.importorskip("rasterio")
        from camafloodcast.core.forcing_download import DownloadRequest, era5_land_gee_download

        monkeypatch.setenv("EARTHENGINE_PROJECT", "test-project")

        request = DownloadRequest(source="era5-land-gee", west=100.0, south=8.0, east=100.5,
                                  north=8.5, gsize=0.25,
                                  start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 2))

        captured: dict = {}
        tif_paths = {}
        for day in request.dates():
            p = tmp_path / f"{day.isoformat()}.tif"
            value = 0.001 * (day.day)              # metres; day 1 -> 0.001 m, day 2 -> 0.002 m
            self._write_tif(p, np.full((1, 2, 2), value), west=100.0, north=8.5, gsize=0.25)
            tif_paths[day] = p

        class FakeImage:
            def __init__(self, day):
                self.day = day

            def getDownloadURL(self, params):
                captured["getDownloadURL_params"] = params
                return f"fake://{self.day.isoformat()}"

        fake_ee = self._fake_ee_module(captured, FakeImage)
        monkeypatch.setitem(__import__("sys").modules, "ee", fake_ee)

        class FakeResponse:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(url):
            day = dt.date.fromisoformat(url.split("//")[1])
            return FakeResponse(tif_paths[day].read_bytes())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        out = era5_land_gee_download(request, tmp_path)

        assert captured["init_project"] == "test-project"
        assert captured["collection_id"] == "ECMWF/ERA5_LAND/HOURLY"
        assert captured["bands"] == ["runoff_hourly"]
        params = captured["getDownloadURL_params"]
        assert params["dimensions"] == "2x2"
        assert params["crsTransform"] == [0.25, 0, 100.0, 0, -0.25, 8.5]
        assert params["crs"] == "EPSG:4326"

        import netCDF4 as nc

        ds = nc.Dataset(out)
        try:
            runoff = np.asarray(ds.variables["runoff"][:])
            assert ds.variables["runoff"].units == "mm/day"
        finally:
            ds.close()
        assert runoff.shape == (2, 2, 2)
        assert np.allclose(runoff[0], 1.0)     # 0.001 m -> 1 mm
        assert np.allclose(runoff[1], 2.0)     # 0.002 m -> 2 mm

    def test_gldas_gee_download_sums_qs_and_qsb_bands(self, tmp_path, monkeypatch):
        """Qs_acc/Qsb_acc are already accumulated depths in mm -- summing the two bands is the
        whole conversion, unlike the raw GES DISC path's rate-to-depth multiply."""
        import urllib.request

        pytest.importorskip("rasterio")
        from camafloodcast.core.forcing_download import DownloadRequest, gldas_gee_download

        monkeypatch.setenv("EARTHENGINE_PROJECT", "test-project")

        request = DownloadRequest(source="gldas-gee", west=100.0, south=8.0, east=100.5,
                                  north=8.5, gsize=0.25,
                                  start=dt.date(2000, 1, 1), end=dt.date(2000, 1, 1))

        tif_path = tmp_path / "day.tif"
        self._write_tif(tif_path, np.stack([np.full((2, 2), 3.0), np.full((2, 2), 4.0)]),
                        west=100.0, north=8.5, gsize=0.25)

        captured: dict = {}

        class FakeImage:
            def __init__(self, day):
                self.day = day

            def getDownloadURL(self, params):
                captured["getDownloadURL_params"] = params
                return "fake://gldas"

        fake_ee = self._fake_ee_module(captured, FakeImage)
        monkeypatch.setitem(__import__("sys").modules, "ee", fake_ee)

        class FakeResponse:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda url: FakeResponse(tif_path.read_bytes()))

        out = gldas_gee_download(request, tmp_path)

        assert captured["collection_id"] == "NASA/GLDAS/V021/NOAH/G025/T3H"
        assert captured["bands"] == ["Qs_acc", "Qsb_acc"]

        import netCDF4 as nc

        ds = nc.Dataset(out)
        try:
            runoff = np.asarray(ds.variables["runoff"][:])
        finally:
            ds.close()
        assert np.allclose(runoff, 7.0)        # 3 mm + 4 mm


# =========================================================================== domain builder (Phase 2)


class TestDomainBuilderRequest:
    """Pure logic: no files, no subprocess."""

    def test_rejects_zero_or_negative_extent(self):
        from camafloodcast.core.domain_builder import DomainRequest
        from camafloodcast.core.maps import Grid

        grid = Grid(nx=1440, ny=720, nlfp=10, gsize=0.25, west=-180, east=180, south=-90, north=90)
        bad = DomainRequest(name="x", source_map=Path("nowhere"),
                            west=110, east=90, south=8, north=20)   # east < west
        d = bad.validate(grid)
        assert not d.ok
        assert any(c.code == "DOM_BOUNDS" for c in d)

    def test_rejects_a_domain_outside_the_source(self):
        from camafloodcast.core.domain_builder import DomainRequest
        from camafloodcast.core.maps import Grid

        grid = Grid(nx=40, ny=40, nlfp=10, gsize=0.25, west=90, east=100, south=0, north=10)
        req = DomainRequest(name="x", source_map=Path("nowhere"),
                            west=95, east=200, south=0, north=10)
        d = req.validate(grid)
        assert not d.ok
        assert any(c.code == "DOM_OUTSIDE" for c in d)

    def test_snapped_removes_a_half_cell_offset(self):
        from camafloodcast.core.domain_builder import DomainRequest

        req = DomainRequest(name="x", source_map=Path("nowhere"),
                            west=99.9, east=110.1, south=7.9, north=20.05)
        snapped = req.snapped(0.25)
        for v in (snapped.west, snapped.east, snapped.south, snapped.north):
            assert abs(round(v / 0.25) * 0.25 - v) < 1e-9


class TestFloodplainRoughness:
    def test_lookup_covers_mapped_and_default_classes(self):
        from camafloodcast.core.domain_builder import DEFAULT_ROUGHNESS, build_floodplain_roughness
        from camafloodcast.core.maps import Grid

        grid = Grid(nx=3, ny=1, nlfp=10, gsize=1.0, west=0, east=3, south=0, north=1)
        classes = np.array([[1, 2, 99]])                # 99 is unmapped -> default
        out = build_floodplain_roughness(classes, {1: 0.03, 2: 0.15}, grid, default=0.10)
        assert out[0, 0] == pytest.approx(0.03)
        assert out[0, 1] == pytest.approx(0.15)
        assert out[0, 2] == pytest.approx(0.10)
        assert len(DEFAULT_ROUGHNESS) == 12

    def test_shape_mismatch_is_rejected(self):
        from camafloodcast.core.domain_builder import build_floodplain_roughness
        from camafloodcast.core.maps import Grid

        grid = Grid(nx=3, ny=1, nlfp=10, gsize=1.0, west=0, east=3, south=0, north=1)
        with pytest.raises(ValueError):
            build_floodplain_roughness(np.zeros((2, 2)), {}, grid)

    def test_igbp_lookup_covers_every_class_code_with_a_known_category(self):
        from camafloodcast.core.domain_builder import (
            DEFAULT_ROUGHNESS, IGBP_CLASSES, default_igbp_lookup,
        )

        lookup = default_igbp_lookup()
        assert set(lookup) == set(IGBP_CLASSES)
        for code, (_, category) in IGBP_CLASSES.items():
            assert category in DEFAULT_ROUGHNESS, f"class {code}: unknown category {category!r}"
            assert lookup[code] == pytest.approx(DEFAULT_ROUGHNESS[category])

    def test_igbp_lookup_override_replaces_only_its_own_category(self):
        from camafloodcast.core.domain_builder import IGBP_CLASSES, default_igbp_lookup

        base = default_igbp_lookup()
        overridden = default_igbp_lookup(overrides={"urban": 0.09})
        urban_codes = [c for c, (_, cat) in IGBP_CLASSES.items() if cat == "urban"]
        assert urban_codes, "expected at least one urban IGBP class"
        for code in urban_codes:
            assert overridden[code] == pytest.approx(0.09)
        for code in set(IGBP_CLASSES) - set(urban_codes):
            assert overridden[code] == pytest.approx(base[code])

    def test_igbp_lookup_feeds_build_floodplain_roughness_directly(self):
        """The whole point: an IGBP-coded raster needs no reclassification step."""
        from camafloodcast.core.domain_builder import build_floodplain_roughness, default_igbp_lookup
        from camafloodcast.core.maps import Grid

        grid = Grid(nx=2, ny=1, nlfp=10, gsize=1.0, west=0, east=2, south=0, north=1)
        classes = np.array([[17, 13]])                  # water, urban
        out = build_floodplain_roughness(classes, default_igbp_lookup(), grid)
        assert out[0, 0] == pytest.approx(0.030)         # water
        assert out[0, 1] == pytest.approx(0.080)         # urban


@needs_glb
class TestDomainBuilderEndToEnd:
    """Cuts a real, small domain out of the real global map with the real Fortran tools.

    The source map here is deliberately NOT a sibling of ``maps_root`` -- it lives under the
    package's own ``map/`` tree while the cut is written into a pytest ``tmp_path``. That is
    exactly the layout the old hardcoded ``'../../' + name`` in ``prepare()`` got wrong: it
    assumed the source always sat directly inside ``maps_root``. This is the regression test
    for that fix.
    """

    TOOLS_DIR = PKG / "CaMa_Floodcast" / "bin" / "win64"

    @pytest.mark.skipif(not TOOLS_DIR.exists(), reason="domain-cutting tools not built")
    def test_cuts_a_small_domain_from_a_non_sibling_source(self, tmp_path):
        from camafloodcast.core.domain import analyse_domain
        from camafloodcast.core.domain_builder import DomainBuilder, DomainRequest, ToolSet
        from camafloodcast.services import binaries

        tools = ToolSet(directory=self.TOOLS_DIR)
        assert not tools.missing(), f"missing tools: {tools.missing()}"
        tools.runtime_path = binaries.resolve_runtime_path(tools.path("cut_domain"))

        maps_root = tmp_path / "domain"
        maps_root.mkdir()
        # A tiny box in the Mekong delta -- small enough that combine_hires stays fast.
        req = DomainRequest(name="cut_test", source_map=MAP_GLB,
                            west=104.0, east=107.0, south=9.0, north=12.0)

        builder = DomainBuilder(tools, maps_root)
        steps: list[str] = []
        result = builder.build(req, progress=lambda n, i, t: steps.append(n))

        assert result.ok, result.steps
        assert (result.map_dir / "params.txt").exists()
        assert (result.map_dir / "nextxy.bin").exists()
        assert "done" in steps

        report = analyse_domain(RiverMap(result.map_dir))
        assert report.largest, "the cut domain should contain at least one river outlet"

    @pytest.mark.skipif(not TOOLS_DIR.exists(), reason="domain-cutting tools not built")
    def test_a_vietnam_shaped_cut_truncates_the_mekong_and_the_red_river(self, tmp_path):
        """The canonical bad-cut example from 05-workflows.md: 'a Vietnam-shaped box severs
        both the Mekong and the Red River from their headwaters.' Cuts a real box covering
        mainland Vietnam only -- deliberately excluding the upper Mekong (China/Laos/Thailand/
        Cambodia) and the upper Red River (Yunnan) -- and checks that Gate 1 both quantifies
        the truncation and blocks the Mekong as a target basin.

        This is intentionally NOT persisted under map/ as a fixture: cutting it fresh from
        glb_15min here keeps it reproducible and avoids maintaining a second large binary
        regional map purely to demonstrate a failure mode."""
        from camafloodcast.core.domain import find_outlet_near, validate_domain
        from camafloodcast.core.domain_builder import DomainBuilder, DomainRequest, ToolSet
        from camafloodcast.services import binaries

        tools = ToolSet(directory=self.TOOLS_DIR)
        tools.runtime_path = binaries.resolve_runtime_path(tools.path("cut_domain"))

        maps_root = tmp_path / "domain"
        maps_root.mkdir()
        req = DomainRequest(name="vnm_test", source_map=MAP_GLB,
                            west=102.0, east=109.5, south=8.0, north=23.5)

        builder = DomainBuilder(tools, maps_root)
        result = builder.build(req)
        assert result.ok, result.steps

        report = analyse_domain(RiverMap(result.map_dir))
        mekong = find_outlet_near(report, 106.4, 9.6, radius_deg=1.0)
        red_river = find_outlet_near(report, 106.6, 20.6, radius_deg=1.5)

        assert mekong is not None and red_river is not None
        assert not mekong.closed, "the Mekong must be truncated by a Vietnam-only box"
        assert not red_river.closed, "the Red River must be truncated by a Vietnam-only box"
        assert mekong.deficit > 0.25
        assert red_river.deficit > 0.25

        diags = validate_domain(report, target_basin=mekong)
        assert not diags.ok
        assert any(d.code == "DOM_TARGET_TRUNCATED" for d in diags)


# =========================================================================== gis / CRS (Phase 4)


class TestGeoTiffImport:
    """core.gis: importing a real-world GeoTIFF (any CRS) onto CaMa-Flood's own WGS84 grid.

    The source raster here is synthetic, but the CRS detection and the rasterio/pyproj warp
    itself are real -- the same category of verification as this session's other "illustrative
    input, real physics/real library" tests (e.g. the levee scenario's synthetic crown height
    driving the real Fortran levee scheme).
    """

    def _write_utm_geotiff(self, path, west, south, east, north, data):
        import rasterio
        from rasterio.transform import from_bounds

        transform = from_bounds(west, south, east, north, data.shape[1], data.shape[0])
        with rasterio.open(
            path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
            count=1, dtype="float64", crs="EPSG:32648", transform=transform, nodata=-9999.0,
        ) as dst:
            dst.write(data, 1)

    def _utm_bounds_for(self, west, south, east, north):
        from pyproj import Transformer

        to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32648", always_xy=True)
        utm_w, utm_s = to_utm.transform(west, south)
        utm_e, utm_n = to_utm.transform(east, north)
        return utm_w, utm_s, utm_e, utm_n

    def test_detect_crs_reads_the_real_epsg_code(self, tmp_path):
        pytest.importorskip("rasterio")
        from camafloodcast.core.gis import detect_crs

        w, s, e, n = 105.0, 12.0, 105.1, 12.1
        utm_bounds = self._utm_bounds_for(w, s, e, n)
        path = tmp_path / "landclass_utm.tif"
        self._write_utm_geotiff(path, *utm_bounds, np.array([[1.0, 2.0]]))

        assert detect_crs(path) == 32648

    def test_reproject_to_grid_lands_values_on_the_correct_side(self, tmp_path):
        pytest.importorskip("rasterio")
        pytest.importorskip("pyproj")
        from camafloodcast.core.gis import import_geotiff_to_grid
        from camafloodcast.core.maps import Grid

        # A small real WGS84 patch near the Mekong delta (105E is UTM zone 48N's own central
        # meridian), and the same raster written out in that projected CRS: west half = 1.0,
        # east half = 2.0.
        w, s, e, n = 105.0, 12.0, 105.1, 12.1
        utm_bounds = self._utm_bounds_for(w, s, e, n)
        path = tmp_path / "landclass_utm.tif"
        self._write_utm_geotiff(path, *utm_bounds, np.array([[1.0, 2.0]]))

        grid = Grid(nx=10, ny=10, nlfp=1, gsize=0.01, west=w, east=e, south=s, north=n)
        out, epsg = import_geotiff_to_grid(path, grid, method="nearest")

        assert epsg == 32648
        assert out.shape == (10, 10)
        assert np.allclose(out[:, :4], 1.0)
        assert np.allclose(out[:, 6:], 2.0)

    def test_import_assumes_wgs84_when_the_file_carries_no_crs(self, tmp_path):
        pytest.importorskip("rasterio")
        import rasterio
        from rasterio.transform import from_bounds

        from camafloodcast.core.gis import import_geotiff_to_grid
        from camafloodcast.core.maps import Grid

        w, s, e, n = 105.0, 12.0, 105.1, 12.1
        path = tmp_path / "no_crs.tif"
        transform = from_bounds(w, s, e, n, 2, 1)
        with rasterio.open(
            path, "w", driver="GTiff", height=1, width=2, count=1,
            dtype="float64", transform=transform, nodata=-9999.0,
        ) as dst:
            dst.write(np.array([[3.0, 3.0]]), 1)

        grid = Grid(nx=4, ny=2, nlfp=1, gsize=0.025, west=w, east=e, south=s, north=n)
        out, epsg = import_geotiff_to_grid(path, grid)
        assert epsg == 4326
        assert np.allclose(out, 3.0)

    def test_missing_rasterio_raises_a_clear_error(self, tmp_path, monkeypatch):
        import builtins

        from camafloodcast.core.errors import FileFormatError
        from camafloodcast.core.gis import detect_crs

        real_import = builtins.__import__

        def blocked_import(name, *a, **kw):
            if name == "rasterio":
                raise ImportError("blocked for this test")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        with pytest.raises(FileFormatError, match="geo"):
            detect_crs(tmp_path / "whatever.tif")


class TestGeoTiffExport:
    """core.gis.write_geotiff: the counterpart to read_geotiff/reproject_to_grid -- saving
    something already on CaMa-Flood's own WGS84 grid (routing-grid or downscaled) as a real
    GeoTIFF a GIS tool can open."""

    def test_round_trips_values_bounds_and_nodata(self, tmp_path):
        pytest.importorskip("rasterio")
        import rasterio

        from camafloodcast.core.gis import write_geotiff

        data = np.array([[1.0, 2.0, -9999.0], [3.0, 4.0, 5.0]])
        bounds = (104.0, 8.0, 104.3, 8.2)     # west, south, east, north
        path = tmp_path / "downscaled.tif"
        write_geotiff(path, data, bounds, nodata=-9999.0)

        with rasterio.open(path) as src:
            assert src.crs.to_epsg() == 4326
            assert src.nodata == -9999.0
            b = src.bounds
            assert (b.left, b.bottom, b.right, b.top) == pytest.approx(bounds)
            out = src.read(1)
        assert np.allclose(out, data)

    def test_missing_rasterio_raises_a_clear_error(self, tmp_path, monkeypatch):
        import builtins

        from camafloodcast.core.errors import FileFormatError
        from camafloodcast.core.gis import write_geotiff

        real_import = builtins.__import__

        def blocked_import(name, *a, **kw):
            if name == "rasterio":
                raise ImportError("blocked for this test")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        with pytest.raises(FileFormatError, match="geo"):
            write_geotiff(tmp_path / "out.tif", np.zeros((2, 2)), (0.0, 0.0, 1.0, 1.0))


# =========================================================================== downscale (Phase 3)


DOWNSCALE_EXE = PKG / "CaMa_Floodcast" / "bin" / "win64" / (
    "downscale_flddph_trib.exe" if sys.platform == "win32" else "downscale_flddph_trib")
MEKONG_FLDDPH = PKG / "CaMa_Floodcast" / "projects" / "mekong" / "cases" / "baseline" / "flddphmekong.bin"

needs_downscale = pytest.mark.skipif(
    not (DOWNSCALE_EXE.exists() and (MAP_MEK / "1min" / "location.txt").exists()
        and MEKONG_FLDDPH.exists()),
    reason="downscale tool, 1min sub-grid data, or the Mekong test project are not present")


class TestDownscaleWindow:
    """Pure logic: no files, no subprocess."""

    def test_shape_matches_the_fortran_tools_own_rounding(self):
        from camafloodcast.core.downscale import DownscaleWindow

        # The documented Mekong delta example: 4 x 3.5 deg at 1 arc-min -> 240 x 210.
        w = DownscaleWindow(west=104, east=108, south=8.5, north=12, resolution="1min")
        assert w.shape == (210, 240)

    def test_rejects_zero_or_negative_extent(self):
        from camafloodcast.core.downscale import DownscaleWindow

        with pytest.raises(ValueError):
            DownscaleWindow(west=108, east=104, south=8.5, north=12)

    def test_rejects_an_unknown_resolution(self):
        from camafloodcast.core.downscale import DownscaleWindow

        with pytest.raises(ValueError):
            DownscaleWindow(west=104, east=108, south=8.5, north=12, resolution="7min")


class TestRecordForDate:
    def test_matches_the_tools_1_indexed_record_convention(self):
        from camafloodcast.core.downscale import record_for_date
        from camafloodcast.core.output import TimeAxis

        time = TimeAxis(start=dt.datetime(2000, 1, 1), step_hours=24, count=365)
        assert record_for_date(time, dt.date(2000, 1, 1)) == 1
        assert record_for_date(time, dt.datetime(2000, 1, 1)) == 1
        # Round-trip through the SDK's own 0-indexed date_of(), rather than a hand-counted
        # calendar date: the 1-indexed record for whatever date sits at 0-indexed record 297
        # must come back as 298 -- the value the historical Mekong October-peak reference uses.
        day298 = time.date_of(297)
        assert record_for_date(time, day298) == 298


class TestInundationStats:
    def test_nodata_is_excluded_from_both_valid_and_wet(self):
        from camafloodcast.core.downscale import DownscaleWindow, NODATA, inundation_stats

        w = DownscaleWindow(west=0, east=1, south=0, north=1, resolution="1min")
        arr = np.full(w.shape, NODATA, dtype="<f4")
        arr[10:20, 10:20] = 0.0     # mapped, dry
        arr[10:15, 10:15] = 2.0     # mapped, wet
        stats = inundation_stats(arr, w)
        assert stats.valid_pixels == 100
        assert stats.inundated_pixels == 25
        assert stats.mean_depth_m == pytest.approx(2.0)

    def test_no_inundation_is_not_an_error(self):
        from camafloodcast.core.downscale import DownscaleWindow, inundation_stats

        w = DownscaleWindow(west=0, east=1, south=0, north=1, resolution="1min")
        stats = inundation_stats(np.zeros(w.shape, dtype="<f4"), w)
        assert stats.inundated_pixels == 0
        assert "no inundation" in stats.summary()

    def test_volume_is_area_times_depth_for_a_uniform_field(self):
        """Depth [m] * area [km2->m2], summed over wet pixels, in millions of m3 -- with a
        spatially uniform depth this collapses to area_km2 * depth_m exactly, which is a
        cleaner check than hand-deriving the spherical pixel area."""
        from camafloodcast.core.downscale import DownscaleWindow, inundation_stats

        w = DownscaleWindow(west=0, east=1, south=0, north=1, resolution="1min")
        arr = np.zeros(w.shape, dtype="<f4")
        arr[10:20, 10:20] = 2.0
        stats = inundation_stats(arr, w)
        assert stats.inundated_volume_mcm == pytest.approx(stats.inundated_area_km2 * 2.0, rel=1e-6)


@needs_downscale
class TestDownscaleEndToEnd:
    """Runs the real Fortran tool against the real Mekong test project's real output.

    Record 298 (day 298 of 2000, late October) is the same day documented in
    etc/v430fix_tests/regional/README.md's worked example (~41,000 km2 at the Mekong flood
    peak). The exact figures differ from that reference because it used 0.25-deg E2O forcing
    while examples/make_mekong_project.py uses 1-deg test forcing -- both are real, physically
    reasonable runs; this test checks order of magnitude and internal consistency, not a
    byte-identical match to a run made with different forcing.
    """

    def setup_method(self):
        from camafloodcast.core.downscale import DownscaleTool, DownscaleWindow
        from camafloodcast.services import binaries

        self.window = DownscaleWindow(west=104, east=108, south=8.5, north=12, resolution="1min")
        # Resolved once here rather than per test: an exe with no runtime_path fails with
        # STATUS_ENTRYPOINT_NOT_FOUND before it ever reaches its own argument handling (risk
        # R4), which would masquerade as whatever DownscaleError a test happened to expect.
        self.tool = DownscaleTool(exe=DOWNSCALE_EXE,
                                  runtime_path=binaries.resolve_runtime_path(DOWNSCALE_EXE))

    def test_downscales_the_october_peak(self, tmp_path):
        from camafloodcast.core.downscale import inundation_stats

        out = tmp_path / "flood_rec298.bin"
        arr = self.tool.run(MAP_MEK, MEKONG_FLDDPH, 298, self.window, out, scratch=tmp_path / "work")

        assert arr.shape == self.window.shape
        assert out.exists()

        stats = inundation_stats(arr, self.window)
        # Same order of magnitude as the documented reference (~41,000 km2); a different
        # forcing product should not move it by an order of magnitude.
        assert 10_000 <= stats.inundated_area_km2 <= 100_000
        assert 0.1 <= stats.mean_depth_m <= 20.0

    def test_is_deterministic(self, tmp_path):
        a = self.tool.run(MAP_MEK, MEKONG_FLDDPH, 298, self.window, tmp_path / "a.bin",
                          scratch=tmp_path / "work_a")
        b = self.tool.run(MAP_MEK, MEKONG_FLDDPH, 298, self.window, tmp_path / "b.bin",
                          scratch=tmp_path / "work_b")
        assert np.array_equal(a, b)

    def test_a_dry_season_day_floods_far_less_than_the_october_peak(self, tmp_path):
        """Sanity check on the whole pipeline, not just the tool: day 15 (mid-January, deep
        dry season per the project's own reference table) must show much less inundation than
        the October peak -- if it didn't, the record-selection argument would be suspect."""
        from camafloodcast.core.downscale import inundation_stats

        dry = self.tool.run(MAP_MEK, MEKONG_FLDDPH, 15, self.window, tmp_path / "dry.bin",
                            scratch=tmp_path / "work_dry")
        peak = self.tool.run(MAP_MEK, MEKONG_FLDDPH, 298, self.window, tmp_path / "peak.bin",
                             scratch=tmp_path / "work_peak")

        dry_stats = inundation_stats(dry, self.window)
        peak_stats = inundation_stats(peak, self.window)
        assert dry_stats.inundated_area_km2 < peak_stats.inundated_area_km2

    def test_missing_subgrid_resolution_is_a_clear_error(self, tmp_path):
        from camafloodcast.core.downscale import DownscaleError, DownscaleWindow

        w = DownscaleWindow(west=104, east=108, south=8.5, north=12, resolution="15sec")
        with pytest.raises(DownscaleError, match="15sec"):
            self.tool.run(MAP_MEK, MEKONG_FLDDPH, 298, w, tmp_path / "out.bin",
                          scratch=tmp_path / "work")

    def test_record_beyond_the_file_is_a_clear_error(self, tmp_path):
        from camafloodcast.core.downscale import DownscaleError

        with pytest.raises(DownscaleError):
            self.tool.run(MAP_MEK, MEKONG_FLDDPH, 999_999, self.window, tmp_path / "out.bin",
                          scratch=tmp_path / "work")


@needs_downscale
class TestDownscalePeriod:
    """Batch downscaling: a per-day time series plus a maximum-depth composite, against the
    same real Mekong project as TestDownscaleEndToEnd."""

    def setup_method(self):
        from camafloodcast.core.downscale import DownscaleTool, DownscaleWindow
        from camafloodcast.core.output import TimeAxis
        from camafloodcast.services import binaries

        self.window = DownscaleWindow(west=104, east=108, south=8.5, north=12, resolution="1min")
        self.tool = DownscaleTool(exe=DOWNSCALE_EXE,
                                  runtime_path=binaries.resolve_runtime_path(DOWNSCALE_EXE))
        # Matches examples/make_mekong_project.py exactly: SYEAR=2000, IFRQ_OUT=24, 365 days.
        self.time = TimeAxis(start=dt.datetime(2000, 1, 1), step_hours=24, count=365)

    def test_batch_around_the_october_peak(self):
        from camafloodcast.core.downscale import downscale_period, mask_nodata

        calls: list[tuple[int, int, int]] = []
        result = downscale_period(self.tool, MAP_MEK, MEKONG_FLDDPH, self.time, self.window,
                                  295, 299, progress=lambda rec, i, n: calls.append((rec, i, n)))

        assert result.records == [296, 297, 298, 299, 300]
        assert len(result.dates) == 5
        assert result.dates == [self.time.date_of(r) for r in range(295, 300)]
        assert calls == [(296, 1, 5), (297, 2, 5), (298, 3, 5), (299, 4, 5), (300, 5, 5)]

        assert result.area_km2.shape == (5,)
        assert (result.area_km2 > 0).all()
        assert (result.volume_mcm > 0).all()
        assert result.max_depth_field.shape == self.window.shape

        # The composite is the pixelwise max across days -- its own overall maximum must
        # therefore equal the largest single-day maximum, by commutativity of max().
        valid = ~mask_nodata(result.max_depth_field)
        assert result.max_depth_field[valid].max() == pytest.approx(result.max_depth_m.max())
        assert 0 <= result.peak_index < 5

    def test_rejects_a_record_range_outside_the_file(self):
        from camafloodcast.core.downscale import downscale_period

        with pytest.raises(ValueError):
            downscale_period(self.tool, MAP_MEK, MEKONG_FLDDPH, self.time, self.window,
                             360, 999, progress=None)
