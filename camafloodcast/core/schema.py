"""Typed schema for the CaMa-Flood namelist, plus cross-field validation.

Field names, defaults and validation rules are taken from the v4.30 source, principally
``src/cmf_ctrl_nmlist_mod.F90`` (groups NRUNVER / NDIMTIME / NPARAM), ``cmf_ctrl_maps_mod.F90``
(NMAP), ``cmf_ctrl_forcing_mod.F90`` (NFORCE), ``cmf_ctrl_output_mod.F90`` (NOUTPUT),
``cmf_ctrl_restart_mod.F90`` (NRESTART), ``cmf_ctrl_time_mod.F90`` (NSIMTIME) and
``cmf_ctrl_levee_mod.F90`` (NLEVEE).

Design decision: **unknown keys are a warning, not an error.** This schema records what we
know; a user's namelist may legitimately carry keys from a newer model version or a local
patch, and refusing to load it would be worse than useless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .errors import DiagnosticList, Severity
from .namelist import Namelist

Kind = Literal["logical", "int", "real", "path", "string"]


@dataclass(frozen=True)
class Field:
    """One namelist key: its type, default, meaning and constraints."""

    name: str
    kind: Kind
    default: Any
    doc: str
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] | None = None
    #: path fields: must the file exist? ("NONE" is always accepted as "not used")
    must_exist: bool = False
    #: guidance shown in the GUI beyond ``doc`` -- consequences, recommended values
    guidance: str = ""
    advanced: bool = False


@dataclass(frozen=True)
class Group:
    name: str
    doc: str
    fields: tuple[Field, ...]

    def by_name(self) -> dict[str, Field]:
        return {f.name.upper(): f for f in self.fields}


def _L(name, default, doc, **kw) -> Field:
    return Field(name, "logical", default, doc, **kw)


def _I(name, default, doc, **kw) -> Field:
    return Field(name, "int", default, doc, **kw)


def _R(name, default, doc, **kw) -> Field:
    return Field(name, "real", default, doc, **kw)


def _P(name, default, doc, **kw) -> Field:
    return Field(name, "path", default, doc, **kw)


def _S(name, default, doc, **kw) -> Field:
    return Field(name, "string", default, doc, **kw)


# --------------------------------------------------------------------------- NRUNVER

NRUNVER = Group(
    "NRUNVER",
    "Physics and run-mode switches.",
    (
        _L("LADPSTP", True, "Adaptive (CFL-limited) time step",
           guidance="Strongly recommended with the local-inertial scheme. The model subdivides "
                    "DT into NT substeps; NT is reported in the log as 'ADPSTP: NT='."),
        _L("LFPLAIN", True, "Represent floodplain storage via sub-grid hypsometry"),
        _L("LKINE", False, "Use kinematic wave instead of local inertial",
           guidance="Incompatible with LPTHOUT. Adaptive stepping is not meaningful here."),
        _L("LFLDOUT", True, "Floodplain (high-water channel) flow"),
        _L("LPTHOUT", False, "Bifurcation channel flow",
           guidance="Essential for deltas and anabranching rivers. Requires CPTHOUT (bifprm.txt)."),
        _L("LDAMOUT", False, "Reservoir / dam operation"),
        _L("LLEVEE", False, "Levee scheme", guidance="Requires CLEVHGT and CLEVFRC maps."),
        _L("LROSPLIT", False, "Forcing supplies surface and sub-surface runoff separately"),
        _L("LGDWDLY", False, "Groundwater delay reservoir", guidance="Requires LROSPLIT."),
        _L("LWEVAP", False, "Extract open-water evaporation from floodplain storage"),
        _L("LWEVAPFIX", False, "Close the water balance when evaporation is limited", advanced=True),
        _L("LWEXTRACTRIV", False, "Also extract evaporation from the river channel", advanced=True),
        _L("LSLPMIX", False, "Mix kinematic and local-inertial by slope (netCDF maps only)", advanced=True),
        _L("LSLOPEMOUTH", False, "Prescribe bed slope at river mouths (netCDF maps only)", advanced=True),
        _L("LMEANSL", False, "Apply a mean sea level field at river mouths"),
        _L("LSEALEV", False, "Apply a time-varying sea level boundary"),
        _L("LRESTART", False, "Start from a restart file"),
        _L("LSTOONLY", False, "Restart from storage only (for data assimilation)", advanced=True),
        _L("LOUTPUT", True, "Write output files"),
        _L("LOUTINI", False, "Write the initial state (netCDF only)", advanced=True),
        _L("LOUTINS", False, "Diagnose instantaneous (unrouted) discharge", advanced=True),
        _L("LGRIDMAP", True, "Standard lat/lon gridded map"),
        _L("LLEAPYR", True, "Include 29 February"),
        _L("LMAPEND", False, "Byte-swap map files (big-endian input)",
           guidance="Set if map binaries were written on a big-endian machine. A wrong setting "
                    "usually shows up as absurd elevations rather than an error."),
        _L("LBITSAFE", False, "Legacy; superseded by the NoAtom_CMF build flag", advanced=True),
        _L("LSTG_ES", False, "Vector-processor flood-stage kernel",
           guidance="For NEC-class vector machines. Slower on scalar CPUs."),
        _L("LSPAMAT", True, "Quasi-sparse-matrix inflow gather (avoids OpenMP atomics)",
           guidance="Default. Also makes runs bit-reproducible across thread counts."),
        _L("LSEDOUT", False, "Sediment transport (tentative scheme)", advanced=True),
        _L("LTRACE", False, "Tracer transport (tentative scheme)", advanced=True),
        _L("LWBLOG", True, "Write the per-substep water balance line to the log",
           guidance="Keep on while developing -- it drives the live water-balance monitor. Turn "
                    "OFF for production: it is a 15-field formatted write on EVERY substep."),
    ),
)

# --------------------------------------------------------------------------- NDIMTIME

NDIMTIME = Group(
    "NDIMTIME",
    "Grid dimensions and time stepping.",
    (
        _P("CDIMINFO", "NONE", "Dimension information file (diminfo_*.txt)", must_exist=True),
        _R("DT", 86400.0, "Base time step", unit="s", minimum=60,
           guidance="Must be >= 60 and a multiple of 60: the model tracks time in minutes. "
                    "With LADPSTP this is the OUTER step; the model subdivides it as needed."),
        _I("IFRQ_INP", 24, "Forcing update interval", unit="hour", minimum=1, maximum=24),
    ),
)

# --------------------------------------------------------------------------- NPARAM

NPARAM = Group(
    "NPARAM",
    "Physical and numerical parameters.",
    (
        _R("PMANRIV", 0.03, "Channel Manning coefficient (fallback if CRIVMAN absent)",
           minimum=0.005, maximum=0.2),
        _R("PMANFLD", 0.10, "Floodplain Manning coefficient (scalar)",
           minimum=0.01, maximum=0.5,
           guidance="A single value worldwide. Prefer CFLDMAN (a 2-D map) where land cover "
                    "varies: measured 9% RMS sensitivity in discharge."),
        _R("PGRV", 9.8, "Gravitational acceleration", unit="m/s2", advanced=True),
        _R("PDSTMTH", 10000.0, "Assumed downstream distance at river mouths", unit="m"),
        _R("PCADP", 0.7, "CFL safety coefficient for the adaptive step",
           minimum=0.1, maximum=1.0,
           guidance="dt <= PCADP * dx / sqrt(g*h). Lower is more stable and slower. The CFL "
                    "here uses wave celerity only, not |u| + sqrt(gh)."),
        _R("PMINSLP", 1.0e-5, "Minimum bed slope (kinematic wave only)", advanced=True),
        _R("PSTOLIM", 0.05, "Storage-change limiter: max fraction of cell storage per substep",
           minimum=0.0, maximum=1.0,
           guidance="0.05 reproduces v4.30. <=0 disables. It damps backwater filling of dry "
                    "cells and delta spreading through bifurcation channels -- worth a "
                    "sensitivity test before tuning anything else around it."),
        _I("IMIS", -9999, "Integer missing value", advanced=True),
        _R("RMIS", 1.0e20, "Real missing value", advanced=True),
        _R("DMIS", 1.0e20, "Double missing value", advanced=True),
        _S("CSUFBIN", ".bin", "Suffix for 2-D binary output", advanced=True),
        _S("CSUFVEC", ".vec", "Suffix for vector output", advanced=True),
        _S("CSUFPTH", ".pth", "Suffix for bifurcation output", advanced=True),
        _S("CSUFCDF", ".nc", "Suffix for netCDF output", advanced=True),
    ),
)

# --------------------------------------------------------------------------- NSIMTIME

NSIMTIME = Group(
    "NSIMTIME",
    "Simulation period.",
    (
        _I("SYEAR", 2000, "Start year"),
        _I("SMON", 1, "Start month", minimum=1, maximum=12),
        _I("SDAY", 1, "Start day", minimum=1, maximum=31),
        _I("SHOUR", 0, "Start hour", minimum=0, maximum=23),
        _I("EYEAR", 2001, "End year"),
        _I("EMON", 1, "End month", minimum=1, maximum=12),
        _I("EDAY", 1, "End day", minimum=1, maximum=31),
        _I("EHOUR", 0, "End hour", minimum=0, maximum=23),
    ),
)

# --------------------------------------------------------------------------- NMAP

NMAP = Group(
    "NMAP",
    "River network map and channel parameters.",
    (
        _L("LMAPCDF", False, "Read maps from netCDF instead of plain binary"),
        _P("CNEXTXY", "./nextxy.bin", "Downstream cell index (2 records, int32)", must_exist=True),
        _P("CGRAREA", "./ctmare.bin", "Unit-catchment area [m2]", must_exist=True),
        _P("CELEVTN", "./elevtn.bin", "Channel bank-top elevation [m]", must_exist=True),
        _P("CNXTDST", "./nxtdst.bin", "Distance to the downstream cell [m]", must_exist=True),
        _P("CRIVLEN", "./rivlen.bin", "Channel length [m]", must_exist=True),
        _P("CFLDHGT", "./fldhgt.bin", "Floodplain hypsometry, NLFP records [m]", must_exist=True),
        _P("CRIVWTH", "./rivwth.bin", "Channel width [m]", must_exist=True),
        _P("CRIVHGT", "./rivhgt.bin", "Channel bankfull depth [m]", must_exist=True),
        _P("CRIVMAN", "./rivman.bin", "Channel Manning map", must_exist=True),
        _P("CFLDMAN", "NONE", "Floodplain Manning MAP",
           guidance='"NONE" uses the scalar PMANFLD and is bit-identical to v4.30. Supply a 2-D '
                    "map to vary floodplain roughness by land cover."),
        _P("CPTHOUT", "./bifprm.txt", "Bifurcation channel table (bifprm.txt)"),
        _P("CGDWDLY", "NONE", "Groundwater delay parameter map", advanced=True),
        _P("CMEANSL", "NONE", "Mean sea level map"),
        _P("CMPIREG", "NONE", "MPI region map", advanced=True),
        _P("CRIVCLINC", "NONE", "netCDF climate/topography map", advanced=True),
        _P("CRIVPARNC", "NONE", "netCDF channel parameter map", advanced=True),
        _P("CMEANSLNC", "NONE", "netCDF mean sea level", advanced=True),
        _P("CMPIREGNC", "NONE", "netCDF MPI region map", advanced=True),
    ),
)

# --------------------------------------------------------------------------- NRESTART

NRESTART = Group(
    "NRESTART",
    "Restart input and output.",
    (
        _P("CRESTSTO", "", "Restart file to start from (empty = cold start)"),
        _P("CRESTDIR", "./", "Directory for restart output"),
        _S("CVNREST", "restart", "Restart file prefix"),
        _L("LRESTCDF", False, "netCDF restart"),
        _I("IFRQ_RST", 0, "Restart write frequency", unit="hour",
           guidance="0 = only at the end of the run; 1-24 = hourly; 30 = monthly."),
    ),
)

# --------------------------------------------------------------------------- NFORCE

NFORCE = Group(
    "NFORCE",
    "Runoff forcing. CaMa-Flood consumes RUNOFF, not rainfall.",
    (
        _L("LINPCDF", False, "Forcing is netCDF", guidance="Requires a netCDF-enabled binary."),
        _L("LINTERP", True, "Interpolate forcing with the input matrix (mass conservative)",
           guidance="TRUE: use CINPMAT, correct for any forcing grid. FALSE: nearest point, "
                    "exact only when the forcing grid equals the routing grid."),
        _L("LINPEND", False, "Byte-swap forcing (big-endian input)"),
        _L("LINPDAY", False, "Daily forcing files", advanced=True),
        _L("LITRPCDF", False, "netCDF input matrix", advanced=True),
        _P("CINPMAT", "NONE", "Input matrix file (inpmat_*.bin)"),
        _R("DROFUNIT", 86400000.0, "Runoff unit conversion divisor",
           guidance="Divides runoff to reach m/s before multiplying by cell area. "
                    "mm/day -> 86400000; mm/s -> 1000; m/s -> 1."),
        _P("CROFDIR", "./", "Runoff input directory"),
        _S("CROFPRE", "Roff____", "Runoff filename prefix"),
        _S("CROFSUF", ".one", "Runoff filename suffix"),
        _S("CROFCDF", "NONE", "netCDF runoff file", advanced=True),
        _S("CVNROF", "runoff", "netCDF runoff variable name", advanced=True),
        _S("CVNSUB", "NONE", "netCDF sub-surface runoff variable name", advanced=True),
    ),
)

# --------------------------------------------------------------------------- NOUTPUT

NOUTPUT = Group(
    "NOUTPUT",
    "Output selection and format.",
    (
        _P("COUTDIR", "./", "Output directory"),
        _S("CVARSOUT", "outflw,storge", "Comma-separated output variables",
           guidance="Common: outflw, storge, rivdph, flddph, fldare, fldfrc, sfcelv, rivvel, "
                    "maxdph, maxflw. Each costs one file of NX*NY*4 bytes per record."),
        _S("COUTTAG", "", "Output filename tag (appended to the variable name)"),
        _L("LOUTVEC", False, "Write 1-D vector output instead of 2-D maps",
           guidance="About 6x smaller on a global domain, because only river cells are stored. "
                    "Strongly recommended at global scale."),
        _L("LOUTCDF", False, "netCDF output", guidance="Requires a netCDF-enabled binary."),
        _I("NDLEVEL", 0, "netCDF deflation level", minimum=0, maximum=9, advanced=True),
        _I("IFRQ_OUT", 24, "Output write interval", unit="hour", minimum=1, maximum=24),
        _L("LOUTTXT", False, "Write gauge time series as text", advanced=True),
        _P("CGAUTXT", "NONE", "Gauge list for text output", advanced=True),
    ),
)

# --------------------------------------------------------------------------- NLEVEE

NLEVEE = Group(
    "NLEVEE",
    "Levee scheme maps (only read when LLEVEE is true).",
    (
        _P("CLEVHGT", "NONE", "Levee crown height above the channel bank top [m]",
           guidance="The model silently raises this to the levee's own base height (derived "
                    "from CLEVFRC below) if you set it any lower -- a crown height that looks "
                    "reasonable on its own can end up doing nothing if CLEVFRC places the base "
                    "above it. Check the two together, not CLEVHGT alone."),
        _P("CLEVFRC", "NONE", "Unprotected fraction: relative channel-to-levee distance [0-1]",
           guidance="Not simply 'how far the levee sits from the river': the model maps this "
                    "fraction onto the floodplain hypsometry's own layer heights to get the "
                    "levee's base height, so the same CLEVFRC means a very different real base "
                    "height on a steep-sided cell versus a flat one. A small value (comfortably "
                    "under 1/NLFP, so the base falls inside the first floodplain layer) keeps "
                    "the base a small, predictable fraction of that layer's own height -- a "
                    "value like 0.5 can silently put the base far higher than an intended "
                    "CLEVHGT, neutralising the levee (see CLEVHGT above)."),
    ),
)

# --------------------------------------------------------------------------- NBOUND

NBOUND = Group(
    "NBOUND",
    "Sea level boundary (only read when LSEALEV is true). Verified against "
    "src/cmf_ctrl_boundary_mod.F90.",
    (
        _L("LSEALEVCDF", False, "Sea level boundary is netCDF instead of plain binary"),
        _P("CSEALEVDIR", "./sealev/", "Sea level boundary directory (plain binary)"),
        _S("CSEALEVPRE", "sealev", "Sea level boundary filename prefix", advanced=True),
        _S("CSEALEVSUF", ".bin", "Sea level boundary filename suffix", advanced=True),
        _P("CSEALEVCDF", "./sealev/", "Sea level netCDF file"),
        _S("CVNSEALEV", "variable", "Sea level netCDF variable name", advanced=True),
        _I("SYEARSL", 0, "Start year of the netCDF sea level record", advanced=True),
        _I("SMONSL", 0, "Start month of the netCDF sea level record", minimum=0, maximum=12,
           advanced=True),
        _I("SDAYSL", 0, "Start day of the netCDF sea level record", minimum=0, maximum=31,
           advanced=True),
        _I("SHOURSL", 0, "Start hour of the netCDF sea level record", minimum=0, maximum=23,
           advanced=True),
        _P("CSLMAP", "./sealev/", "Station-to-grid conversion table (netCDF only)",
           advanced=True),
        _I("IFRQ_SL", 9999, "Sea level update interval", unit="min",
           guidance="9999 (the default) means the dynamic sea level boundary is not used, "
                    "even if LSEALEV is true."),
    ),
)

# --------------------------------------------------------------------------- NDAMOUT

NDAMOUT = Group(
    "NDAMOUT",
    "Reservoir operation (only read when LDAMOUT is true). Verified against "
    "src/cmf_ctrl_damout_mod.F90.",
    (
        _P("CDAMFILE", "./dam_params.csv", "Dam parameter file",
           guidance="Free-format CSV: a count, a header line, then one row per dam -- DamID, "
                    "DamName, DamLat, DamLon, upreal, DamIX, DamIY, FldVol_mcm, ConVol_mcm, "
                    "TotVol_mcm, Qn, Qf. DamIX/DamIY are 1-indexed and a dam landing outside "
                    "the grid or off the river network is skipped silently, not rejected -- "
                    "see core.dams."),
        _L("LDAMTXT", True, "Write per-dam inflow/outflow to a text file"),
        _L("LDAMH22", False, "Use the Hanazaki (2022) operation scheme instead of "
           "Yamazaki & Funato's"),
        _L("LDAMYBY", False, "Year-by-year dam activation (each dam's CSV row needs a DamYear)"),
        _L("LiVnorm", False, "Initialise reservoir storage at normal volume instead of empty"),
    ),
)


ALL_GROUPS: tuple[Group, ...] = (
    NRUNVER, NDIMTIME, NPARAM, NSIMTIME, NMAP, NRESTART, NFORCE, NOUTPUT, NLEVEE, NBOUND,
    NDAMOUT,
)
GROUPS_BY_NAME: dict[str, Group] = {g.name: g for g in ALL_GROUPS}


def field_for(group: str, key: str) -> Field | None:
    g = GROUPS_BY_NAME.get(group.upper())
    return None if g is None else g.by_name().get(key.upper())


def defaults(group: str) -> dict[str, Any]:
    g = GROUPS_BY_NAME.get(group.upper())
    return {} if g is None else {f.name: f.default for f in g.fields}


# --------------------------------------------------------------------------- validation


_KIND_TYPES: dict[Kind, tuple[type, ...]] = {
    "logical": (bool,),
    "int": (int,),
    "real": (int, float),
    "path": (str,),
    "string": (str,),
}


def validate(nml: Namelist, *, base_dir: Path | None = None,
             check_files: bool = True) -> DiagnosticList:
    """Validate a namelist: types, ranges, referenced files, and cross-field rules.

    ``base_dir`` resolves relative paths (normally the case directory, since CaMa-Flood
    interprets every path relative to its working directory).
    """
    diags = DiagnosticList()
    base = Path(base_dir) if base_dir else Path.cwd()

    # ---- per-field checks -------------------------------------------------
    for entry in nml:
        f = field_for(entry.group, entry.key)
        if f is None:
            diags.info("NML_UNKNOWN_KEY",
                       f"{entry.group}/{entry.key} is not in the known schema; passed through",
                       where=entry.name, line=entry.line_no + 1)
            continue

        v = entry.value
        if isinstance(v, list):
            continue  # arrays are not schema-checked

        if not isinstance(v, _KIND_TYPES[f.kind]) or (f.kind != "logical" and isinstance(v, bool)):
            diags.error("NML_TYPE",
                        f"{entry.name} should be {f.kind}, got {type(v).__name__} ({v!r})",
                        where=entry.name, line=entry.line_no + 1)
            continue

        if f.kind in ("int", "real"):
            if f.minimum is not None and v < f.minimum:
                diags.error("NML_RANGE",
                            f"{entry.name} = {v} is below the minimum {f.minimum}",
                            where=entry.name, line=entry.line_no + 1, hint=f.guidance or None)
            if f.maximum is not None and v > f.maximum:
                diags.error("NML_RANGE",
                            f"{entry.name} = {v} is above the maximum {f.maximum}",
                            where=entry.name, line=entry.line_no + 1, hint=f.guidance or None)

        if f.kind == "path" and check_files and f.must_exist:
            if isinstance(v, str) and v.strip() and v.strip().upper() != "NONE":
                p = Path(v)
                if not p.is_absolute():
                    p = base / v
                if not p.exists():
                    diags.error("NML_MISSING_FILE",
                                f"{entry.name} points at a file that does not exist: {v}",
                                where=entry.name, line=entry.line_no + 1,
                                hint="Paths are resolved relative to the case working directory.")

    # ---- cross-field rules ------------------------------------------------
    g = lambda grp, key, dflt=None: nml.get(grp, key, dflt)

    # Time stepping. Verified in CMF_CONFIG_CHECK: both of these are STOP 9.
    dt = g("NDIMTIME", "DT", 86400)
    if isinstance(dt, (int, float)):
        if dt < 60:
            diags.error("CFG_DT_MIN", f"DT = {dt} s is below the minimum of 60 s",
                        where="NDIMTIME/DT",
                        hint="CaMa-Flood tracks time in whole minutes. The model will STOP 9.")
        elif int(dt) % 60 != 0:
            diags.error("CFG_DT_MULT", f"DT = {dt} s is not a multiple of 60",
                        where="NDIMTIME/DT",
                        hint="CaMa-Flood tracks time in whole minutes. The model will STOP 9.")

        ifrq = g("NDIMTIME", "IFRQ_INP", 24)
        if isinstance(ifrq, int) and dt >= 60:
            dtin = ifrq * 3600
            if dtin % int(dt) != 0:
                diags.error("CFG_DTIN_MULT",
                            f"forcing interval DTIN = {dtin} s is not a multiple of DT = {int(dt)} s",
                            where="NDIMTIME/IFRQ_INP",
                            hint="The model will STOP 9. Choose DT so it divides IFRQ_INP*3600.")

    # Physics combinations.
    if g("NRUNVER", "LKINE", False) and g("NRUNVER", "LPTHOUT", False):
        diags.error("CFG_KINE_PTH",
                    "LKINE and LPTHOUT cannot both be true",
                    where="NRUNVER/LPTHOUT",
                    hint="Bifurcation flow is only implemented for the local-inertial scheme. "
                         "The model will STOP 9.")

    if g("NRUNVER", "LKINE", False) and g("NRUNVER", "LADPSTP", False):
        diags.warning("CFG_KINE_ADP",
                      "LADPSTP with LKINE: the adaptive step is tuned for the local-inertial scheme",
                      where="NRUNVER/LADPSTP",
                      hint="Set a fixed DT appropriate to the kinematic wave instead.")

    if not g("NRUNVER", "LFPLAIN", True) and not g("NRUNVER", "LKINE", False):
        diags.warning("CFG_NOFLD",
                      "LFPLAIN is false without LKINE; no-floodplain runs are normally kinematic",
                      where="NRUNVER/LFPLAIN")

    if g("NRUNVER", "LGDWDLY", False) and not g("NRUNVER", "LROSPLIT", False):
        diags.warning("CFG_GDW_ROS",
                      "LGDWDLY has no effect without LROSPLIT",
                      where="NRUNVER/LGDWDLY",
                      hint="The groundwater reservoir is fed by sub-surface runoff, which "
                           "requires split runoff forcing.")

    for dep in ("LWEVAPFIX", "LWEXTRACTRIV"):
        if g("NRUNVER", dep, False) and not g("NRUNVER", "LWEVAP", False):
            diags.warning("CFG_WEVAP_DEP", f"{dep} has no effect without LWEVAP",
                          where=f"NRUNVER/{dep}")

    # Feature switches that need their data.
    if g("NRUNVER", "LPTHOUT", False):
        pth = g("NMAP", "CPTHOUT", "NONE")
        if not pth or str(pth).upper() == "NONE":
            diags.error("CFG_PTH_FILE", "LPTHOUT is on but CPTHOUT is not set",
                        where="NMAP/CPTHOUT",
                        hint="Bifurcation needs bifprm.txt, produced by set_bifparam.")
        elif check_files:
            p = Path(pth)
            p = p if p.is_absolute() else base / pth
            if not p.exists():
                diags.error("CFG_PTH_FILE", f"LPTHOUT is on but {pth} does not exist",
                            where="NMAP/CPTHOUT")

    if g("NRUNVER", "LLEVEE", False):
        for key in ("CLEVHGT", "CLEVFRC"):
            v = g("NLEVEE", key, "NONE")
            if not v or str(v).upper() == "NONE":
                diags.error("CFG_LEVEE_FILE", f"LLEVEE is on but {key} is not set",
                            where=f"NLEVEE/{key}")

    if g("NRUNVER", "LSEALEV", False):
        if g("NBOUND", "IFRQ_SL", 9999) == 9999:
            diags.error("CFG_SEALEV_FREQ",
                        "LSEALEV is on but IFRQ_SL is still 9999 (the sentinel for 'not used')",
                        where="NBOUND/IFRQ_SL",
                        hint="Set the sea level update interval in minutes.")
        if g("NBOUND", "LSEALEVCDF", False):
            cdf = g("NBOUND", "CSEALEVCDF", "")
            if not cdf or not str(cdf).strip():
                diags.error("CFG_SEALEV_FILE", "LSEALEV/LSEALEVCDF is on but CSEALEVCDF is empty",
                            where="NBOUND/CSEALEVCDF")
        else:
            d = g("NBOUND", "CSEALEVDIR", "")
            if not d or not str(d).strip():
                diags.error("CFG_SEALEV_FILE", "LSEALEV is on but CSEALEVDIR is empty",
                            where="NBOUND/CSEALEVDIR")

    if g("NRUNVER", "LDAMOUT", False):
        dam = g("NDAMOUT", "CDAMFILE", "")
        if not dam or not str(dam).strip():
            diags.error("CFG_DAM_FILE", "LDAMOUT is on but CDAMFILE is empty",
                        where="NDAMOUT/CDAMFILE")
        elif check_files:
            p = Path(dam)
            p = p if p.is_absolute() else base / dam
            if not p.exists():
                diags.error("CFG_DAM_FILE", f"LDAMOUT is on but {dam} does not exist",
                            where="NDAMOUT/CDAMFILE",
                            hint="A dam allocated outside the grid or off the river network is "
                                 "skipped silently by the model, not rejected -- see "
                                 "core.dams.write_dam_file, which validates this before writing.")

    # Forcing.
    if g("NFORCE", "LINTERP", True):
        mat = g("NFORCE", "CINPMAT", "NONE")
        if not mat or str(mat).upper() == "NONE":
            diags.error("CFG_INPMAT",
                        "LINTERP is true but CINPMAT is not set",
                        where="NFORCE/CINPMAT",
                        hint="Mass-conservative interpolation needs the input matrix. If the "
                             "forcing grid equals the routing grid, set LINTERP=.FALSE. instead.")
    else:
        diags.info("CFG_NOINTERP",
                   "LINTERP is false: forcing is taken by nearest point",
                   where="NFORCE/LINTERP",
                   hint="Exact only if the forcing grid matches the routing grid cell for cell.")

    if g("NRUNVER", "LRESTART", False):
        rst = g("NRESTART", "CRESTSTO", "")
        if not str(rst).strip():
            diags.error("CFG_RESTART_FILE", "LRESTART is on but CRESTSTO is empty",
                        where="NRESTART/CRESTSTO")

    # Period sanity.
    try:
        import datetime as _dt
        s = _dt.date(int(g("NSIMTIME", "SYEAR", 2000)), int(g("NSIMTIME", "SMON", 1)),
                     int(g("NSIMTIME", "SDAY", 1)))
        e = _dt.date(int(g("NSIMTIME", "EYEAR", 2000)), int(g("NSIMTIME", "EMON", 1)),
                     int(g("NSIMTIME", "EDAY", 1)))
        if e <= s:
            diags.error("CFG_PERIOD", f"end date {e} is not after start date {s}",
                        where="NSIMTIME/EYEAR")
        elif (e - s).days > 366 * 50:
            diags.warning("CFG_PERIOD_LONG",
                          f"simulation spans {(e - s).days} days; check the period is intended",
                          where="NSIMTIME/EYEAR")
    except (ValueError, TypeError) as exc:
        diags.error("CFG_PERIOD_BAD", f"simulation period is not a valid date: {exc}",
                    where="NSIMTIME/SYEAR")

    # Output.
    if g("NRUNVER", "LOUTPUT", True):
        v = str(g("NOUTPUT", "CVARSOUT", "") or "")
        if not v.strip():
            diags.warning("CFG_NOVARS", "LOUTPUT is on but CVARSOUT is empty",
                          where="NOUTPUT/CVARSOUT")

    return diags


def required_capabilities(nml: Namelist) -> set[str]:
    """Build-flag capabilities this configuration needs from the model binary.

    Cross-checked against the binary registry at pre-flight (Gate 3), so a user with a
    non-netCDF build is told before the run rather than after it fails.
    """
    caps: set[str] = set()
    if (nml.get("NOUTPUT", "LOUTCDF", False) or nml.get("NFORCE", "LINPCDF", False)
            or nml.get("NMAP", "LMAPCDF", False) or nml.get("NRESTART", "LRESTCDF", False)
            or (nml.get("NRUNVER", "LSEALEV", False) and nml.get("NBOUND", "LSEALEVCDF", False))):
        caps.add("netcdf")
    return caps
