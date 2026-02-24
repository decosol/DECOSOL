# -*- coding: utf-8 -*-
"""
MEMO/CONTRACT
PORTING 1:1 VPMDECO (FORTRAN → PYTHON)

- Single-file deliverable.
- No simplifications / no numeric forcing / no OOP.
- COMMON blocks will be mapped to Python globals (same implicit scope).
- Fortran I/O (.out/.0out) is disabled (no-op).
- Validation targets: total runtime, first stop, stop list.

This file is the BASELINE "v0": it embeds the complete original Fortran source
verbatim (FORTRAN_SOURCE) to prevent missing-code regressions. Translation is
performed by progressively replacing the corresponding Python stubs/functions
with 1:1 logic.

Generated on: 2025-12-16
"""

from __future__ import annotations
import math
import os
import json


# ============================================================
# PRODUCTION FREEZE HEADER
# ============================================================
ENGINE_STATUS = "PRODUCTION FREEZE"
ENGINE_VERSION = "1.0"
ALGORITHM = "VPM-B + ZH-L16"
DO_NOT_MODIFY_CORE = True

ENGINE_FROZEN_DATE = "2026-02"
ENGINE_VALIDATED = True

# Runtime configuration may be injected by the GUI via environment variables.
# Avoid setting these externally when running the engine, to ensure deterministic behavior.

DEBUG_ENABLED = False  # hard kill-switch for any passive debug I/O
# ============================================================
# FORTRAN REAL semantics (single precision) + rounding helpers
# ============================================================
USE_FORTRAN_REAL32 = True  # emulate Fortran REAL (single precision) where possible
DEBUG_PROJECTED_ASCENT = False  # debug flag for PROJECTED_ASCENT

# ============================================================
# CCR (Closed Circuit Rebreather) extension — MINIMAL "A" HARNESS
# - Default OFF: OC results must remain identical to baseline.
# - When CCR_MODE=True, gas mix fractions are treated as DILUENT fractions.
# - Only Inspired_N2 / Inspired_He calculations are replaced inside:
#     GAS_LOADINGS_ASCENT_DESCENT, GAS_LOADINGS_CONSTANT_DEPTH
# ============================================================
CCR_MODE = False


# Bailout mode handling:
# - BO_MODE: bailout calculation requested (calculate ascent in bailout).
# - BO_EFFECTIVE: becomes True only once we actually switch to a bailout mix (Mix_Number != 1).
#   Until then, we must keep CCR physics (setpoint) even if bailout is requested, so that
#   Mix 1 (diluent) is NEVER treated as an open-circuit bailout gas.
BO_MODE = False
BO_EFFECTIVE = False

# BO_REQUESTED: True when GUI requests 'calculate ascent in bailout' (CCR -> OC) by providing bailout gases (mixes 2..N).
BO_REQUESTED = False

# Bailout gas gating (CCR->OC): computed in main() from gases_json (mix 1 = diluent, mixes 2..N = bailout table).
# Stored as quantised MOD (multiplo inferiore di 3 m) for metric gating, and enabled flags.
BO_MIX_MODQ = None          # list[float] indexed by mix_number (1..N); entry 0 unused
BO_MIX_ENABLED = None       # list[bool]  indexed by mix_number (1..N); entry 0 unused

def _bo_mod_quantize_m(mod_m: float) -> float:
    """Quantise MOD to the lower multiple of 3 m (metric rule)."""
    try:
        return math.floor(float(mod_m) / 3.0) * 3.0
    except Exception:
        return 0.0

def _bo_pick_first_valid_in_order(depth_m: float) -> int:
    """Pick the *best* enabled bailout mix usable at depth (MOD-fit).

    Eligible mixes are 2..N only (mix 1 is diluent and must never be used as bailout gas).
    A bailout mix is usable if MODq >= depth.

    Selection rule (best valid among ON gases):
      - consider only enabled mixes (checkbox ON)
      - among usable mixes, pick the one with the smallest (MODq - depth) i.e. MOD closest above depth
      - return 1 if none is usable at this depth (fallback: remain in CCR/diluent)

    Note: Function name kept for backward compatibility with older call-sites.
    """
    global BO_MIX_MODQ, BO_MIX_ENABLED
    try:
        if BO_MIX_MODQ is None or BO_MIX_ENABLED is None:
            return 1
        best_mix = 1
        best_delta = None
        n = min(len(BO_MIX_MODQ), len(BO_MIX_ENABLED))
        for mixn in range(2, n):
            if not BO_MIX_ENABLED[mixn]:
                continue
            modq = float(BO_MIX_MODQ[mixn])
            if modq >= float(depth_m):
                delta = modq - float(depth_m)
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_mix = mixn
        return int(best_mix)
    except Exception:
        return 1


def _bo_pick_mix_for_depth(depth_m: float) -> int:
    """Compatibility helper: pick BO mix for a given depth using best-fit MOD rule.
    Returns 1 when no enabled BO gas is usable (fallback CCR/diluent).
    """
    return _bo_pick_first_valid_in_order(depth_m)



def _bo_pick_next_in_sequence(current_mix: int, depth_m: float) -> int:
    """Policy (3): keep current BO gas until the *next* enabled gas in list order becomes usable.

    - If current_mix is not a bailout mix (<=1), this behaves like _bo_pick_first_valid_in_order.
    - Otherwise, look only at enabled mixes with index > current_mix (list order),
      and pick the first one that is usable (MODq >= depth). If none, keep current_mix.
    """
    global BO_MIX_MODQ, BO_MIX_ENABLED
    try:
        if BO_MIX_MODQ is None or BO_MIX_ENABLED is None:
            return current_mix
        start = max(2, int(current_mix) + 1)
        for mixn in range(start, min(len(BO_MIX_MODQ), len(BO_MIX_ENABLED))):
            if not BO_MIX_ENABLED[mixn]:
                continue
            if BO_MIX_MODQ[mixn] >= depth_m:
                return mixn
    except Exception:
        pass
    return current_mix


def _bo_apply_dynamic(current_mix: int, depth_m: float) -> int:
    """Apply bailout gas selection at the current depth (best-fit MOD among enabled BO gases).

    This is used by the planner at discrete decision points (e.g., CCR→BO engage and deco stops).

    Rule:
      - candidates: only bailout mixes with checkbox ON (2..N)
      - usable: MODq >= depth
      - choose: smallest (MODq - depth)  => MOD closest above depth
      - if none usable: fallback to CCR/diluent (mix 1) and mark BO as not effective
    """
    global BO_MODE, BO_EFFECTIVE

    if not BO_MODE:
        return current_mix

    cand = _bo_pick_first_valid_in_order(depth_m)  # now best-fit among enabled
    if cand == 1:
        BO_EFFECTIVE = False
        return 1

    # A real BO gas is available at this depth
    return int(cand)



def _CCR_ACTIVE() -> bool:
    """Return True while CCR physiology applies.
    In CCR+BO one-shot, CCR remains active until a real bailout mix (mix!=1) is engaged.
    This prevents the impossible state: BO effective while still breathing mix 1 (diluent).
    """
    try:
        m = int(Mix_Number)
    except Exception:
        m = 1
    return CCR_MODE and (not (BO_MODE and BO_EFFECTIVE and m != 1))


def _update_bo_effective(mix_number: int) -> None:
    global BO_EFFECTIVE
    if BO_MODE and (not BO_EFFECTIVE) and mix_number != 1:
        BO_EFFECTIVE = True

# Minimal harness: single setpoint for the whole profile (atm), converted to msw.
CCR_SP_ATM = 1.3
CCR_SP_MSW = CCR_SP_ATM * 10.0
# --------------------------------------------------------------------
# CCR MULTI-SETPOINT SUPPORT (GUI -> MAIN)
# --------------------------------------------------------------------
# The GUI may inject a structured object into this module before calling main routines:
#     CCR_SETTINGS = {"enabled": True, "segments": [...], "units": "msw"}
# Segments are built deterministically by the GUI and contain:
#     {"name": "Descent|Bottom|Deco1|Deco2|Deco3", "depth_from_m": float, "depth_to_m": float,
#      "setpoint_req_ata": float, "bottom_time_min": float|None}
# If CCR_SETTINGS is missing/disabled/invalid, the code falls back to mono-SP (CCR_SP_MSW).
CCR_SETTINGS = None  # type: ignore

def _CCR_GET_SP_MSW(depth_msw: float, rate_msw_per_min: float, is_constant_depth: bool) -> float:
    # --- MSP band selection helper (stable, avoids boundary double-counting) ---
    def _msp_depth_in_band(depth_m: float, a_m: float, b_m: float) -> bool:
        """True if depth_m is inside the band using a half-open rule (skip zero-width bands)."""
        hi = a_m if a_m >= b_m else b_m
        lo = b_m if a_m >= b_m else a_m
        # zero-width band -> ignore (must not affect results)
        if abs(hi - lo) < 1e-12:
            return False
        # shallowest band (to surface) includes depth==0
        if lo <= 1e-12:
            return (depth_m <= hi + 1e-12) and (depth_m >= 0.0)
        # half-open on shallow boundary: depth==lo goes to next shallower band
        return (depth_m <= hi + 1e-12) and (depth_m > lo + 1e-12)
    # --- end helper ---
    """Return requested CCR setpoint (msw) for the current step.
    Priority:
      1) CCR_SETTINGS (structured segments) if enabled.
      2) Mono setpoint (CCR_SP_MSW) otherwise.
    Notes:
      - This function only selects the *requested* setpoint. Physical clamping is still done
        exclusively by _CCR_CLAMP_PPO2_AND_INERTS (no new formulas here).
      - For non-constant segments, 'Descent' is preferred when rate>0.
      - For constant depth at bottom, 'Bottom' is preferred when depth matches exactly.
      - Otherwise selection is by depth band with preference: Deco1 -> Deco2 -> Deco3 -> Bottom.
    """
    try:
        d = float(depth_msw)
    except Exception:
        return float(CCR_SP_MSW)
    try:
        r = float(rate_msw_per_min)
    except Exception:
        r = 0.0

    s = globals().get("CCR_SETTINGS", None)
    if isinstance(s, dict) and bool(s.get("enabled", False)):
        segs = s.get("segments", None)
        if isinstance(segs, list) and segs:
            # Prefer Descent segment only during active descent (not during constant depth)
            if (not is_constant_depth) and (r > 0.0):
                for seg in segs:
                    name = str(seg.get("name", "")).strip().lower()
                    if name == "descent":
                        sp = seg.get("setpoint_req_ata", None)
                        if sp is not None:
                            try:
                                return float(sp) * 10.0
                            except Exception:
                                break

            # Collect depth-matching segments (excluding Descent)
            candidates = []
            for seg in segs:
                name = str(seg.get("name", "")).strip().lower()
                if name == "descent":
                    continue
                df = seg.get("depth_from_m", None)
                dt = seg.get("depth_to_m", None)
                if df is None or dt is None:
                    continue
                try:
                    dfv = float(df); dtv = float(dt)
                except Exception:
                    continue
                lo = dfv if dfv < dtv else dtv
                hi = dtv if dfv < dtv else dfv
                # inclusive band match
                if (d >= (lo - 1e-9)) and (d <= (hi + 1e-9)):
                    candidates.append(seg)

            # Constant depth at exact bottom: prefer Bottom segment if present
            if is_constant_depth and candidates:
                for seg in candidates:
                    if str(seg.get("name", "")).strip().lower() == "bottom":
                        try:
                            dfv = float(seg.get("depth_from_m", d))
                        except Exception:
                            dfv = d
                        if abs(dfv - d) <= 1e-6:
                            sp = seg.get("setpoint_req_ata", None)
                            if sp is not None:
                                try:
                                    return float(sp) * 10.0
                                except Exception:
                                    break

            # Otherwise: prefer deco bands, then Bottom
            pref = ("deco1", "deco2", "deco3", "bottom")
            for p in pref:
                for seg in candidates:
                    if str(seg.get("name", "")).strip().lower() == p:
                        sp = seg.get("setpoint_req_ata", None)
                        if sp is not None:
                            try:
                                return float(sp) * 10.0
                            except Exception:
                                break

            # If nothing matched, fall back to mono SP
    return float(CCR_SP_MSW)

def _CCR_CLAMP_PPO2_AND_INERTS(P_alv: float, FO2_dil: float, FHe_dil: float, FN2_dil: float, SP_msw: float):
    """Return (ppHe, ppN2, mode) in msw, using PVAPOR through P_alv = Pamb - PVAPOR.
    mode in {'SP','FO2MIN','PAMBMAX'} identifies which clamp branch is active at this P_alv.
    """
    # Physically meaningful bounds
    if P_alv <= 0.0:
        return 0.0, 0.0, "PAMBMAX"

    # Clamp ppO2 (msw)
    ppO2_min = P_alv * FO2_dil
    ppO2 = SP_msw
    if ppO2 < ppO2_min:
        ppO2 = ppO2_min
        mode = "FO2MIN"
    else:
        mode = "SP"
    if ppO2 > P_alv:
        ppO2 = P_alv
        mode = "PAMBMAX"

    pp_inert = P_alv - ppO2
    denom = FHe_dil + FN2_dil
    if denom <= 0.0 or pp_inert <= 0.0:
        return 0.0, 0.0, mode

    ppHe = pp_inert * (FHe_dil / denom)
    ppN2 = pp_inert * (FN2_dil / denom)
    return ppHe, ppN2, mode

def _CCR_RATES(Rate: float, mode: str, FHe_dil: float, FN2_dil: float, FO2_dil: float):
    """Given ambient pressure rate in msw/min and active clamp mode at start, return (He_Rate, N2_Rate) in msw/min.
    Assumes the segment stays within the same clamp regime (GUI segmentation responsibility).
    """
    denom = FHe_dil + FN2_dil
    if denom <= 0.0:
        return 0.0, 0.0

    if mode == "SP":
        # pp_inert = P_alv - SP  -> slope = Rate
        slope_inert = Rate
        return slope_inert * (FHe_dil / denom), slope_inert * (FN2_dil / denom)
    elif mode == "FO2MIN":
        # ppO2 = P_alv*FO2 -> pp_inert = P_alv*(1-FO2)=P_alv*denom -> ppHe=P_alv*FHe, ppN2=P_alv*FN2
        return Rate * FHe_dil, Rate * FN2_dil
    else:
        # PAMBMAX: ppO2=P_alv -> pp_inert=0
        return 0.0, 0.0

import csv

try:
    import numpy as _np  # local name to avoid collisions
except Exception:  # pragma: no cover
    _np = None

def REAL(x: float) -> float:
    """Cast to Fortran REAL precision (float32) if enabled."""
    if USE_FORTRAN_REAL32 and _np is not None:
        return float(_np.float32(x))
    return float(x)


def _CLAMP_NONNEG(x: float, eps: float = 1e-18) -> float:
    """Clamp tiny negative values to zero.

    When emulating Fortran REAL (float32), roundoff can make expressions that
    are mathematically non-negative become a very small negative number
    (e.g. -2e-21). Fortran would typically still proceed; in Python, sqrt() would
    raise. This helper preserves the intended algebra without "fixing" values.
    """
    xf = float(x)
    if xf < 0.0 and xf > -eps:
        return 0.0
    return xf

import re
import math
import os
import datetime
from typing import Any, Dict, List, Tuple, Optional


# ============================================================
# PASSIVE DEBUG LOG (v10 baseline) — profile_debug_v10.csv
# DO NOT AFFECT PHYSICS/SCHEDULE/BO/CCR LOGIC.
# Records the *effective* inspired partial pressures used for tissue loading,
# at the exact point they are computed (CCR clamp vs OC/BO fractions).
# ============================================================

DEBUG_PROFILE_ROWS: List[Dict[str, Any]] = []
DEBUG_CTX_SEG_TYPE: str = ""  # optional context set by caller ("DESC"/"ASC"/"STOP")
DEBUG_CTX_DEPTH_FROM: float = float("nan")
DEBUG_CTX_DEPTH_TO: float = float("nan")

def _debug_profile_append(seg_type: str,
                          depth_from: float,
                          depth_to: float,
                          runtime_end: float,
                          mix_number: int,
                          ppO2_inspired: float,
                          ppN2_inspired: float,
                          ppHe_inspired: float,
                          ccr_active: bool,
                          bo_effective: bool) -> None:
    if not DEBUG_ENABLED:
        return

    """Append a single debug row. Must never raise."""
    try:
        mode = "CCR" if ccr_active else "OC_BO"
        # 'mix_label' is informational only; MUST NOT be used to infer model branch.
        mix_label = f"mix{int(mix_number)}"
        DEBUG_PROFILE_ROWS.append({
            "seg_type": seg_type,
            "depth_from": float(depth_from),
            "depth_to": float(depth_to),
            "runtime_end": float(runtime_end),
            "ccr_active": bool(ccr_active),
            "bo_effective": bool(bo_effective),
            "ppO2_inspired": float(ppO2_inspired),
            "ppN2_inspired": float(ppN2_inspired),
            "ppHe_inspired": float(ppHe_inspired),
            "mode": mode,
            "mix_label": mix_label,
        })
    except Exception:
        # Passive logging must never break the engine.
        pass

def _debug_profile_write_csv() -> None:
    if not DEBUG_ENABLED:
        return

    """Write profile_debug_v10.csv next to this script. Always overwrites. Must never raise."""
    try:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile_debug_v10.csv")
        fieldnames = [
            "seg_type",
            "depth_from",
            "depth_to",
            "runtime_end",
            "ccr_active",
            "bo_effective",
            "ppO2_inspired",
            "ppN2_inspired",
            "ppHe_inspired",
            "mode",
            "mix_label",
        ]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in DEBUG_PROFILE_ROWS:
                w.writerow({k: row.get(k, "") for k in fieldnames})
    except Exception:
        pass


# ============================================================
# EMBEDDED INPUT FILES (replacing .set and .in file reads)
# ============================================================
VPMDECO_SET_TEXT = """\
Units=msw
Altitude_Dive_Algorithm=OFF
Minimum_Deco_Stop_Time=1.0
Critical_Radius_N2_Microns=0.55
Critical_Radius_He_Microns=0.45
Critical_Volume_Algorithm='ON'
Crit_Volume_Parameter_Lambda=7500.0
Gradient_Onset_of_Imperm_Atm=8.2
Surface_Tension_Gamma=0.0179
Skin_Compression_GammaC=0.257
rapsol1=1.00
rapsol2=1.00
Regeneration_Time_Constant=20160.0
Pressure_Other_Gases_mmHg=102.0
"""

VPMDECO_IN_TEXT = """\
Trimix DIVE TO 80 MSW_60min_no deco gas !prova CC
1 !Number of gas mixes
.10,.80,.10 !Fraction O2, Fraction He, Fraction N2
1 !Profile code 1 = descent
0,80,20,1 !Starting depth, ending depth, rate, gasmix
2 !Profile code 2 = constant depth
80,64,1 !Depth, run time at end of segment, gasmix
99 !Profile code 99 = decompress
1 !Number of ascent parameter changes
80,1,-10,3 !Starting depth, gasmix, rate, step size
0 ! Repetitive code 0 = last dive/end of file"""
# ============================================================
# FORTRAN I/O DISABLED (no-op)
# ============================================================


def _select_profile_final_run(profile_rows, stops_list, target_rt):
    """Report-only: select the run inside profile_rows that matches stops_list best and ends at target_rt.

    Deterministic scoring:
      1) max number of matching STOP triples (depth, stop_min, runtime_end) vs engine stops_list
      2) closeness of last runtime_end to target_rt
      3) longer run length
    Also trims the chosen run to runtime_end <= target_rt (+small tol).
    """

    try:
        if not profile_rows:
            return []
        # Split into runs by runtime reset
        runs=[]
        cur=[]
        prev=None
        for row in profile_rows:
            rt=row.get("runtime_end")
            if cur and rt is not None and prev is not None and rt < prev - 1e-6:
                runs.append(cur); cur=[]
            cur.append(row)
            if rt is not None:
                prev=rt
        if cur:
            runs.append(cur)

        eng=[]
        for (d, st, rt) in (stops_list or []):
            eng.append((float(d), float(st), float(rt)))
        # tolerances
        tol_depth=1e-6
        tol_time=0.51
        tol_rt=0.51

        def score_run(run):
            # derive stops from run
            derived=[]
            prev_rt=None
            for r in run:
                rt=r.get("runtime_end")
                if r.get("seg_type") != "STOP":
                    prev_rt = rt if rt is not None else prev_rt
                    continue
                if rt is None or prev_rt is None:
                    prev_rt = rt if rt is not None else prev_rt
                    continue
                depth = r.get("depth_to")
                if depth is None:
                    prev_rt = rt
                    continue
                stop_min=float(rt)-float(prev_rt)
                if stop_min <= 0:
                    prev_rt = rt
                    continue
                derived.append((float(depth), float(stop_min), float(rt)))
                prev_rt = rt

            # match sequentially
            matches=0
            j=0
            for d, st, rt in derived:
                while j < len(eng):
                    de, ste, rte = eng[j]
                    if abs(d-de)<=tol_depth and abs(rt-rte)<=tol_rt and abs(st-ste)<=tol_time:
                        matches += 1
                        j += 1
                        break
                    j += 1

            # end proximity
            last_rt=None
            for rr in reversed(run):
                v=rr.get("runtime_end")
                if v is not None:
                    last_rt=float(v); break
            end_diff=abs((last_rt if last_rt is not None else 1e9) - float(target_rt))
            return (matches, -end_diff, len(run))

        best=max(runs, key=score_run)
        # trim to target_rt (keep <= target_rt+tol)
        tr=float(target_rt)
        trimmed=[r for r in best if (r.get("runtime_end") is None) or (float(r["runtime_end"]) <= tr + 1e-6)]
        return trimmed
    except Exception:
        return []
def _fortran_write_noop(*args: Any, **kwargs: Any) -> None:
    return

# ============================================================
# PARSERS (Fortran-style, list-directed-ish)
# ============================================================
def parse_set_text(set_text: str) -> Dict[str, str]:
    """
    Parse the embedded .set content (key=value per line).
    Keeps raw values (strings) to preserve original semantics.
    """
    cfg: Dict[str, str] = {}
    for raw in set_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("!", "#")):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg

def _strip_fortran_comment(s: str) -> str:
    # In these inputs, '!' is used for inline comments.
    return s.split("!", 1)[0].strip()

def parse_in_text(in_text: str) -> Dict[str, Any]:
    """
    Parse the embedded .in schedule in the same spirit as the Fortran READ(*,*)
    usage: numeric tokens are separated by commas and/or whitespace.
    Returns a dict with description, mixes, profile segments, and code99 changes.
    """
    lines = [ln.rstrip("\n") for ln in in_text.splitlines() if ln.strip() != ""]
    idx = 0

    def next_line() -> str:
        nonlocal idx
        if idx >= len(lines):
            raise ValueError("Unexpected EOF while parsing VPMDECO_IN_TEXT")
        ln = lines[idx]
        idx += 1
        return ln

    def parse_nums(line: str) -> List[float]:
        core = _strip_fortran_comment(line)
        if not core:
            return []
        # Split by commas and/or whitespace, keep Fortran-like .20 format
        toks = [t for t in re.split(r"[\s,]+", core) if t]
        out: List[float] = []
        for t in toks:
            # Fortran may use D exponent; accept it
            tt = t.replace("D", "E").replace("d", "e")
            out.append(float(tt))
        return out

    description = next_line()

    # number of gas mixes
    n_mixes = int(parse_nums(next_line())[0])
    mixes: List[Tuple[float, float, float]] = []
    for _ in range(n_mixes):
        fo2, fhe, fn2 = parse_nums(next_line())
        mixes.append((fo2, fhe, fn2))

    segments: List[Dict[str, Any]] = []
    ascent_changes: List[Dict[str, Any]] = []

    while True:
        code_line = next_line()
        code = int(parse_nums(code_line)[0])
        if code == 1:
            # descent/ascent segment
            start, end, rate, mix = parse_nums(next_line())
            segments.append({"code": 1, "start": start, "end": end, "rate": rate, "mix": int(mix)})
        elif code == 2:
            depth, runtime_end, mix = parse_nums(next_line())
            segments.append({"code": 2, "depth": depth, "runtime_end": runtime_end, "mix": int(mix)})
        elif code == 99:
            n_changes = int(parse_nums(next_line())[0])
            for _ in range(n_changes):
                start_depth, mix, rate, step = parse_nums(next_line())
                ascent_changes.append({"start_depth": start_depth, "mix": int(mix), "rate": rate, "step": step})
            rep_code = int(parse_nums(next_line())[0])
            # (profile_debug_v10.csv disabled for production)
            return {
                "description": description,
                "n_mixes": n_mixes,
                "mixes": mixes,
                "segments": segments,
                "ascent_changes": ascent_changes,
                "repetitive_code": rep_code,
            }
        else:
            raise ValueError(f"Unsupported profile code {code} while parsing VPMDECO_IN_TEXT")

# ============================================================
# COMMON blocks → Python globals (1:1 names)
# ============================================================
# NOTE: We start by expanding the COMMON blocks needed by the main program
# (VPMDECO_org). This will grow as we port additional subroutines.

# COMMON /cambiogas/
Number_of_Changes: int = 0
Mix_Change = [0]*10
Fraction_Oxygen = [0.0]*10
Depth_Change = [0.0]*10

# Ascent parameter changes (locals in main, but kept global for 1:1 access)
Rate_Change = [0.0]*10
Step_Size_Change = [0.0]*10

# ============================================================
# STOPV (Voluntary ascent stops, pre-VPM) — defaults (all zero)
# GUI should overwrite STOPV_MINUTES_BY_DEPTH before calling VPMDECO_ORG.
# Depths are fixed in meters.
# ============================================================
STOPV_DEPTHS_M = [66, 63, 60, 57, 54, 51, 48, 45, 42, 39, 36, 33, 30, 27, 24, 21, 18, 15, 12, 9, 6]
STOPV_MINUTES_BY_DEPTH = {d: 0.0 for d in STOPV_DEPTHS_M}


# COMMON /Block_8/
Water_Vapor_Pressure: float = 0.0

# ============================================================
# PVAPOR conventions (keep VPM and ZH-L16 aligned to their references)
# - VPM (VPMDECO/V-Planner style): 0.493 msw (1.607 fsw)
# - ZH-L16 (Buhlmann / MultiDeco / Subsurface): 47 mmHg @ 37C -> 0.627 msw (2.041 fsw)
# ============================================================
PVAPOR_VPM_MSW = 0.493 + 0.134 * 0
PVAPOR_VPM_FSW = 1.607
PVAPOR_ZHL_MSW = 0.627 - 0.134 * 0

# PVAPOR lock (prevents silent overwrites by legacy initializers)
PVAPOR_LOCKED = False
PVAPOR_LOCK_SOURCE = ""  # "vpm" or "zhl16"
PVAPOR_ZHL_FSW = 2.041


# ============================================================
# DETERMINISTIC RUN RESET (MAIN-only fix)
# ------------------------------------------------------------
# Purpose:
#   Ensure that repeated calls to VPMDECO_ORG() within the *same*
#   Python process (GUI session, Nuitka onefile/standalone, etc.)
#   produce identical results for identical inputs, without needing
#   to reload the module.
#
# Rationale:
#   A few "mode/lock" globals (PVAPOR lock, bailout flags, cached
#   ZH-L16 coefficient tables, debug buffers) intentionally persist
#   at module scope. That is fine across *processes*, but it must not
#   leak across *runs* inside one process.
# ============================================================
def ENGINE_RESET_FOR_NEW_RUN() -> None:
    """Reset only the run-scoped globals that can leak across runs.

    IMPORTANT:
      - This must NOT change any physics when running a single plan.
      - It must also NOT wipe GUI-provided configuration that is intended
        to persist (e.g., STOPV_MINUTES_BY_DEPTH, CCR_SETTINGS, BO gas tables).
    """
    global PVAPOR_LOCKED, PVAPOR_LOCK_SOURCE
    global BO_MODE, BO_EFFECTIVE, BO_REQUESTED
    global DEBUG_PROFILE_ROWS
    global _ZHL16_ACTIVE_VARIANT, _ZHL16_ACTIVE_A_N2_BAR, _ZHL16_ACTIVE_B_N2, _ZHL16_ACTIVE_A_HE_BAR, _ZHL16_ACTIVE_B_HE

    # PVAPOR selection is algorithm-dependent and must be re-applied per run
    PVAPOR_LOCKED = False
    PVAPOR_LOCK_SOURCE = ""

    # Bailout state must never carry to the next run
    BO_MODE = False
    BO_EFFECTIVE = False
    BO_REQUESTED = False

    # Passive debug buffer must not accumulate across runs
    try:
        DEBUG_PROFILE_ROWS.clear()
    except Exception:
        DEBUG_PROFILE_ROWS = []

    # ZH-L16 coeff cache: the GUI provides the active table per run via env JSON.
    _ZHL16_ACTIVE_VARIANT = ""
    _ZHL16_ACTIVE_A_N2_BAR = None
    _ZHL16_ACTIVE_B_N2 = None
    _ZHL16_ACTIVE_A_HE_BAR = None
    _ZHL16_ACTIVE_B_HE = None


# COMMON /Block_19/
Surface_Tension_Gamma: float = 0.0
Skin_Compression_GammaC: float = 0.0
rapsol1: float = 0.0
rapsol2: float = 0.0

# COMMON /Block_20/
Crit_Volume_Parameter_Lambda: float = 0.0

# COMMON /Block_21/
Minimum_Deco_Stop_Time: float = 0.0

# COMMON /Block_22/
Regeneration_Time_Constant: float = 0.0

# COMMON /Block_17/
Constant_Pressure_Other_Gases: float = 0.0

# COMMON /Block_14/
Gradient_Onset_of_Imperm_Atm: float = 0.0

# COMMON /Block_2/
Run_Time: float = 0.0
Segment_Number: int = 0
Segment_Time: float = 0.0

# COMMON /Block_4/
Ending_Ambient_Pressure: float = 0.0

# COMMON /Block_9/
Mix_Number: int = 0

# COMMON /Block_18/
Barometric_Pressure: float = 0.0

# Additional arrays referenced in main (declared here for 1:1 continuity)
Helium_Half_Time = [0.0]*16
Nitrogen_Half_Time = [0.0]*16
He_Pressure_Start_of_Ascent = [0.0]*16
N2_Pressure_Start_of_Ascent = [0.0]*16
He_Pressure_Start_of_Deco_Zone = [0.0]*16
N2_Pressure_Start_of_Deco_Zone = [0.0]*16
Phase_Volume_Time = [0.0]*16
Last_Phase_Volume_Time = [0.0]*16

# COMMON /Block_1A/ and /Block_1B/
Helium_Time_Constant = [0.0]*16
Nitrogen_Time_Constant = [0.0]*16

# COMMON /Block_3/
Helium_Pressure = [0.0]*16
Nitrogen_Pressure = [0.0]*16

# COMMON /Block_5/
Fraction_Helium = [0.0]*10
Fraction_Nitrogen = [0.0]*10

# COMMON /Block_23/
Initial_Helium_Pressure = [0.0]*16
Initial_Nitrogen_Pressure = [0.0]*16


# Additional COMMON-style arrays used throughout VPMDECO (initialized here)
Surface_Phase_Volume_Time = [0.0]*16
Amb_Pressure_Onset_of_Imperm = [0.0]*16
Gas_Tension_Onset_of_Imperm = [0.0]*16
Initial_Critical_Radius_N2 = [0.0]*16
Initial_Critical_Radius_He = [0.0]*16
Adjusted_Critical_Radius_N2 = [0.0]*16
Adjusted_Critical_Radius_He = [0.0]*16

# Additional COMMON-style arrays (declared explicitly to avoid missing globals)
Regenerated_Radius_N2 = [0.0]*16
Regenerated_Radius_He = [0.0]*16
Adjusted_Crushing_Pressure_N2 = [0.0]*16
Adjusted_Crushing_Pressure_He = [0.0]*16
Allowable_Gradient_He = [0.0]*16
Allowable_Gradient_N2 = [0.0]*16
Initial_Allowable_Gradient_He = [0.0]*16
Initial_Allowable_Gradient_N2 = [0.0]*16
Deco_Gradient_He = [0.0]*16
Deco_Gradient_N2 = [0.0]*16
def write_debug_press_tess_fine_t_fondo_csv(filename: str, deco_phase_volume_time: float) -> None:
    """Write CSV debug table for 'AT START OF ASCENT (END OF BOTTOM TIME)' for 16 tissues.

    Pure output helper: it does not change any decompression state.
    """
    header1 = ["PY", "AT START OF ASCENT (END OF BOTTOM TIME)"]
    header2 = [
        "i",
        "Nitrogen_Pressure(I)",
        "Helium_Pressure(I)",
        "Max_Crushing_Pressure_N2",
        "Max_Crushing_Pressure_He",
        "Regenerated_Radius_N2",
        "Regenerated_Radius_He",
        "Adj_Crush_Press_N2",
        "Adj_Crush_Press_He",
        "Initial_Allowable_Gradient_N2(I)",
        "Initial_Allowable_Gradient_He(I)",
        "New_Allowable_Gradient_N2(I)",
        "New_Allowable_Gradient_He(I)",
        "Deco_Phase_Volume_Time",
        "Surface_Phase_Volume_Time(I)",
        "Phase_Volume_Time(I)",
    ]

    with open(filename, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header1)
        w.writerow(header2)
        for i in range(16):
            w.writerow([
                i + 1,
                f"{N2_Pressure_Start_of_Ascent[i]:.4f}",      # press tess fine tempo fondo N2
                f"{He_Pressure_Start_of_Ascent[i]:.4f}",      # press tess fine tempo fondo He
                f"{Max_Crushing_Pressure_N2[i]:.4f}",
                f"{Max_Crushing_Pressure_He[i]:.4f}",
                f"{Regenerated_Radius_N2[i]:.9e}",            # 9 decimali per confronto con Fortran
                f"{Regenerated_Radius_He[i]:.9e}",
                f"{Adjusted_Crushing_Pressure_N2[i]:.4f}",
                f"{Adjusted_Crushing_Pressure_He[i]:.4f}",
                f"{Initial_Allowable_Gradient_N2[i]:.4f}",
                f"{Initial_Allowable_Gradient_He[i]:.4f}",
                f"{Allowable_Gradient_N2[i]:.4f}",            # New_Allowable_Gradient_N2
                f"{Allowable_Gradient_He[i]:.4f}",            # New_Allowable_Gradient_He
                f"{deco_phase_volume_time:.4f}",
                f"{Surface_Phase_Volume_Time[i]:.4f}",
                f"{Phase_Volume_Time[i]:.4f}",
            ])

# Unit-related variables (Fortran globals)
Units_Word1 = ""
Units_Word2 = ""
Units_Factor = 0.0
Water_Vapor_Pressure = 0.0
Constant_Pressure_Other_Gases = 0.0
# These are updated by other routines; declared now so VPMDECO_org can reset them
Max_Crushing_Pressure_He = [0.0]*16
Max_Crushing_Pressure_N2 = [0.0]*16
Max_Actual_Gradient = [0.0]*16

# ============================================================
# 1:1 ROUTINE STUBS (same names as Fortran)
# ============================================================
def VPMDECO_org() -> None:
    """Alias for Fortran routine name casing."""
    return VPMDECO_ORG()


def VPMDECO_ORG() -> dict:
    """FORTRAN SUBROUTINE VPMDECO_org (practical 1:1 main flow for single dive).

    Returns a dict with keys:
      - runtime_total
      - first_stop_depth
      - stops: list of (depth, stop_time, run_time_at_end_stop)
      - depth_start_of_deco_zone
    """
    # --- Reset run-scoped state to guarantee deterministic repeated runs ---
    ENGINE_RESET_FOR_NEW_RUN()



    # ============================================================
    # BAILOUT (CCR -> OC) — one-shot harness (DEFAULT OFF)
    # - MUST NOT affect OC or CC normal regressions when OFF.
    # - When ON, CCR_MODE is used up to end-of-bottom (start of ascent),
    #   then the remainder of ascent/deco is computed as OC (dryPamb * Fx)
    #   using bailout gas table (typically mixes 2..N; mix 1 is diluent).
    # ============================================================
    
    # Determine if bailout ascent was requested by the GUI:
    # The GUI controls this via env var VPM_BAILOUT_ONESHOT (set to '1' only when the checkbox
    # "calcolo della risalita in Bailout" is enabled in CC).
    # NOTE: BO gas table (mix 2..N) is always passed, but MUST NOT trigger BO unless this flag is ON.
    global CCR_MODE, BO_REQUESTED
    BAILOUT_ONESHOT = (os.environ.get("VPM_BAILOUT_ONESHOT", "0") in ("1", "true", "TRUE", "yes", "YES"))
    BO_REQUESTED = bool(BAILOUT_ONESHOT)
    bailout_active = bool(CCR_MODE and BAILOUT_ONESHOT)
    global Number_of_Changes, Mix_Change, Fraction_Oxygen, Depth_Change, Rate_Change, Step_Size_Change
    global Water_Vapor_Pressure, Surface_Tension_Gamma, Skin_Compression_GammaC, rapsol1, rapsol2

    # --- PASSIVE SCHEDULE LOG (report-only; no impact on decompression math) ---
    # CONTRACT (GUI reporting): the MAIN must ALWAYS provide a per-segment profile
    # to the caller (GUI). Therefore the passive schedule log is ALWAYS enabled.
    # This has zero impact on decompression math because it is pure append-only logging.
    log_enabled = True
    profile_log = []  # list of dicts, appended only when log_enabled

    def _log_seg(kind, from_m=None, to_m=None, depth_m=None, mix=None,
                 step_min=None, runtime_end=None, note="", gf_actual=None):
        if not log_enabled:
            return

        # Determine mode per-row for reporting (BO > CC > OC).
        # This is report-only and must not affect decompression math.
        try:
            _ccr = bool(_CCR_ACTIVE())
        except Exception:
            _ccr = False
        try:
            _bo = bool(BO_EFFECTIVE) and (mix is not None) and (int(mix) != 1)
        except Exception:
            _bo = False
        if _bo:
            _mode_row = "BO"
        elif _ccr:
            _mode_row = "CC"
        else:
            _mode_row = "OC"

        # --- LEAD + tissue tensions snapshot (report-only) ---
        lead = None
        try:
            _model = (os.environ.get("VPM_DECO_MODEL", "VPM") or "VPM").strip().upper()
        except Exception:
            _model = "VPM"
        try:
            # snapshot tissues (msw absolute)
            _n2 = [float(x) for x in Nitrogen_Pressure]
            _he = [float(x) for x in Helium_Pressure]
        except Exception:
            _n2 = [None] * 16
            _he = [None] * 16

        try:
            if "ZHL" in _model:
                # gf_actual may be passed as 0-1 or 0-100; normalize.
                if gf_actual is None:
                    _gf_use = 1.0
                else:
                    _g = float(gf_actual)
                    _gf_use = (_g / 100.0) if _g > 1.5 else _g
                _ceil, lead = _zhl16_ceiling_msw_and_pilot(gf=_gf_use, rapsol=float(rapsol1), baro_msw=float(Barometric_Pressure))
            else:
                _ceil, lead, _dbg = _calc_deco_ceiling_and_pilot()
        except Exception:
            lead = None



        profile_log.append({
            "kind": str(kind),
            "from_m": None if from_m is None else float(from_m),
            "to_m": None if to_m is None else float(to_m),
            "depth_m": None if depth_m is None else float(depth_m),
            "mix": None if mix is None else int(mix),
            "step_min": None if step_min is None else float(step_min),
            "runtime_end": None if runtime_end is None else float(runtime_end),
            "note": str(note) if note is not None else "",
            "GF_actual": (None if gf_actual is None else float(gf_actual)),
            "LEAD": (None if lead is None else int(lead)),
            "PtN2_01": (None if _n2[0] is None else float(_n2[0])),
            "PtN2_02": (None if _n2[1] is None else float(_n2[1])),
            "PtN2_03": (None if _n2[2] is None else float(_n2[2])),
            "PtN2_04": (None if _n2[3] is None else float(_n2[3])),
            "PtN2_05": (None if _n2[4] is None else float(_n2[4])),
            "PtN2_06": (None if _n2[5] is None else float(_n2[5])),
            "PtN2_07": (None if _n2[6] is None else float(_n2[6])),
            "PtN2_08": (None if _n2[7] is None else float(_n2[7])),
            "PtN2_09": (None if _n2[8] is None else float(_n2[8])),
            "PtN2_10": (None if _n2[9] is None else float(_n2[9])),
            "PtN2_11": (None if _n2[10] is None else float(_n2[10])),
            "PtN2_12": (None if _n2[11] is None else float(_n2[11])),
            "PtN2_13": (None if _n2[12] is None else float(_n2[12])),
            "PtN2_14": (None if _n2[13] is None else float(_n2[13])),
            "PtN2_15": (None if _n2[14] is None else float(_n2[14])),
            "PtN2_16": (None if _n2[15] is None else float(_n2[15])),
            "PtHe_01": (None if _he[0] is None else float(_he[0])),
            "PtHe_02": (None if _he[1] is None else float(_he[1])),
            "PtHe_03": (None if _he[2] is None else float(_he[2])),
            "PtHe_04": (None if _he[3] is None else float(_he[3])),
            "PtHe_05": (None if _he[4] is None else float(_he[4])),
            "PtHe_06": (None if _he[5] is None else float(_he[5])),
            "PtHe_07": (None if _he[6] is None else float(_he[6])),
            "PtHe_08": (None if _he[7] is None else float(_he[7])),
            "PtHe_09": (None if _he[8] is None else float(_he[8])),
            "PtHe_10": (None if _he[9] is None else float(_he[9])),
            "PtHe_11": (None if _he[10] is None else float(_he[10])),
            "PtHe_12": (None if _he[11] is None else float(_he[11])),
            "PtHe_13": (None if _he[12] is None else float(_he[12])),
            "PtHe_14": (None if _he[13] is None else float(_he[13])),
            "PtHe_15": (None if _he[14] is None else float(_he[14])),
            "PtHe_16": (None if _he[15] is None else float(_he[15])),
            "mode_row": _mode_row,
        })
    
    # ------------------------------------------------------------
    # STOPV helpers (voluntary ascent stops, pre-VPM)
    # - STOPV are executed at exact tabulated depths (x.000 m).
    # - They must not alter VPM decision logic; they only add constant-depth time
    #   using the standard tissue update routines.
    # - They apply also in NO-DECO case (diver-chosen profile).
    # ------------------------------------------------------------
    def _stopv_minutes_for_depth(depth_m: float) -> float:
        """Return configured STOPV minutes for an exact stop depth, else 0."""
        try:
            d_int = int(round(float(depth_m)))
        except Exception:
            return 0.0
        if abs(float(depth_m) - d_int) > 0.25:
            return 0.0
        return float(STOPV_MINUTES_BY_DEPTH.get(d_int, 0.0) or 0.0)

    def _apply_stopv_at_depth(depth_m: float, mix_num: int) -> float:
        """Apply STOPV at this depth (if configured >0), updating tissues and logging.

        Returns the minutes actually applied (0.0 if none).
        """
        mins = _stopv_minutes_for_depth(depth_m)
        if mins <= 0.0:
            return 0.0
        _rt0 = Run_Time
        GAS_LOADINGS_CONSTANT_DEPTH(float(depth_m), float(Run_Time) + float(mins), int(mix_num))
        _rt1 = Run_Time
        _log_seg("STOPV", depth_m=float(depth_m), mix=int(mix_num),
                 step_min=(_rt1 - _rt0), runtime_end=_rt1, note="voluntary stop")
        return float(mins)

    def _ascent_with_stopv(depth_from: float, depth_to: float, rate_m_per_min: float,
                          mix_num: int, include_to_depth: bool = False) -> None:
        """Ascend from depth_from to depth_to inserting STOPV at fixed depths crossed.

        Notes:
        - This is a purely kinematic/tissue integration helper.
        - It does NOT change VPM ceiling / decision logic.
        - STOPV at the final target depth can be optionally included (include_to_depth=True),
          useful for the initial ascent to start-of-deco-zone.
        """
        cur = float(depth_from)
        target = float(depth_to)

        applied_total = 0.0

        # If not ascending, keep original behaviour.
        if target >= cur:
            _rt0 = Run_Time
            GAS_LOADINGS_ASCENT_DESCENT(cur, target, float(rate_m_per_min))
            _rt1 = Run_Time
            _log_seg("ASC", from_m=cur, to_m=target, mix=int(mix_num),
                     step_min=(_rt1 - _rt0), runtime_end=_rt1, note=f"rate={rate_m_per_min}")
            return 0.0
        # Determine STOPV depths crossed in this ascent (deep -> shallow).
        depths = []
        for d in STOPV_DEPTHS_M:
            if cur > float(d) > target:
                depths.append(float(d))
        if include_to_depth:
            # Include STOPV exactly at target depth if it is a tabulated STOPV depth.
            for d in STOPV_DEPTHS_M:
                if abs(float(d) - target) < 0.25:
                    depths.append(float(d))
                    break
        depths = sorted(set(depths), reverse=True)

        for q in depths:
            if q < target - 1e-9:
                continue
            if cur > q + 1e-9:
                _rt0 = Run_Time
                GAS_LOADINGS_ASCENT_DESCENT(cur, q, float(rate_m_per_min))
                _rt1 = Run_Time
                _log_seg("ASC", from_m=cur, to_m=q, mix=int(mix_num),
                         step_min=(_rt1 - _rt0), runtime_end=_rt1, note=f"rate={rate_m_per_min}")
                cur = q
            # STOPV at q
            applied_total += _apply_stopv_at_depth(q, mix_num)

        # Finish ascent to target (if not already there)
        if abs(cur - target) > 1e-9:
            _rt0 = Run_Time
            GAS_LOADINGS_ASCENT_DESCENT(cur, target, float(rate_m_per_min))
            _rt1 = Run_Time
            _log_seg("ASC", from_m=cur, to_m=target, mix=int(mix_num),
                     step_min=(_rt1 - _rt0), runtime_end=_rt1, note=f"rate={rate_m_per_min}")

        return float(applied_total)

    def _select_final_profile(prof, target_rt):
        """Pick the final, monotone segment list.
        The engine may execute multiple internal passes; we split on runtime decreases
        and keep the pass whose end runtime is closest to target_rt."""
        if not prof:
            return []
        runs = []
        cur = [prof[0]]
        prev_rt = prof[0].get("runtime_end")
        for row in prof[1:]:
            rt = row.get("runtime_end")
            # treat None as break
            if rt is None or prev_rt is None or rt < prev_rt - 1e-6:
                runs.append(cur)
                cur = [row]
            else:
                cur.append(row)
            prev_rt = rt
        runs.append(cur)
        # choose run closest to target_rt (prefer exact match; fallback to longest)
        best = None
        best_key = None
        for r in runs:
            end_rt = r[-1].get("runtime_end")
            if end_rt is None:
                continue
            key = (abs(end_rt - target_rt), -len(r))
            if best is None or key < best_key:
                best = r
                best_key = key
        return best if best is not None else runs[-1]
    global Crit_Volume_Parameter_Lambda, Minimum_Deco_Stop_Time, Regeneration_Time_Constant
    global Constant_Pressure_Other_Gases, Gradient_Onset_of_Imperm_Atm
    global Run_Time, Segment_Number, Segment_Time, Mix_Number, Barometric_Pressure
    global Helium_Half_Time, Nitrogen_Half_Time
    global He_Pressure_Start_of_Ascent, N2_Pressure_Start_of_Ascent
    global He_Pressure_Start_of_Deco_Zone, N2_Pressure_Start_of_Deco_Zone
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Max_Crushing_Pressure_He, Max_Crushing_Pressure_N2, Max_Actual_Gradient, Surface_Phase_Volume_Time
    global Amb_Pressure_Onset_of_Imperm, Gas_Tension_Onset_of_Imperm
    global Initial_Critical_Radius_N2, Initial_Critical_Radius_He, Adjusted_Critical_Radius_N2, Adjusted_Critical_Radius_He
    global Units_Word1, Units_Word2, Units_Factor
    global Phase_Volume_Time, Last_Phase_Volume_Time
    global Helium_Pressure, Nitrogen_Pressure
    global Fraction_Helium, Fraction_Nitrogen, Fraction_Oxygen, Fraction_Oxygen
    global Allowable_Gradient_He, Allowable_Gradient_N2
    global DEBUG_PROFILE_ROWS, DEBUG_CTX_SEG_TYPE, DEBUG_CTX_DEPTH_FROM, DEBUG_CTX_DEPTH_TO

    # --- PASSIVE DEBUG: reset per run ---
    try:
        DEBUG_PROFILE_ROWS.clear()
    except Exception:
        pass

    # ----------------------------
    # READ .set
    # ----------------------------
    set_kv = parse_set_text(VPMDECO_SET_TEXT)
    Units = set_kv.get("Units", "").strip().upper()
    Altitude_Dive_Algorithm = set_kv.get("Altitude_Dive_Algorithm", "").strip().upper()
    Critical_Volume_Algorithm = set_kv.get("Critical_Volume_Algorithm", "").strip().upper().strip("'").strip('"')

    Minimum_Deco_Stop_Time = float(set_kv.get("Minimum_Deco_Stop_Time", "0.0"))
    Critical_Radius_N2_Microns = float(set_kv.get("Critical_Radius_N2_Microns", "0.0"))
    Critical_Radius_He_Microns = float(set_kv.get("Critical_Radius_He_Microns", "0.0"))
    Crit_Volume_Parameter_Lambda = float(set_kv.get("Crit_Volume_Parameter_Lambda", "0.0"))
    Gradient_Onset_of_Imperm_Atm = float(set_kv.get("Gradient_Onset_of_Imperm_Atm", "0.0"))
    Surface_Tension_Gamma = float(set_kv.get("Surface_Tension_Gamma", "0.0"))
    Skin_Compression_GammaC = float(set_kv.get("Skin_Compression_GammaC", "0.0"))
    rapsol1 = float(set_kv.get("rapsol1", "0.0"))
    rapsol2 = float(set_kv.get("rapsol2", "0.0"))
    Regeneration_Time_Constant = float(set_kv.get("Regeneration_Time_Constant", "0.0"))
    Pressure_Other_Gases_mmHg = float(set_kv.get("Pressure_Other_Gases_mmHg", "0.0"))

    if Units not in ("FSW", "MSW"):
        raise ValueError("ERROR! UNITS MUST BE FSW OR MSW")

    Units_Equal_Fsw = (Units == "FSW")
    Units_Equal_Msw = (Units == "MSW")

    if Units_Equal_Fsw:
        Units_Word1 = "fswg"
        Units_Word2 = "fsw/min"
        Units_Factor = 33.0
    else:
        Units_Word1 = "mswg"
        Units_Word2 = "msw/min"
        Units_Factor = 10.1325

# ----------------------------------------------------------------------
# Legacy Fortran port (VPMDECO):
# In the original Baker code, Water_Vapor_Pressure was set here based
# solely on Units (0.493 msw ≈ 1.607 fsw), representing the same
# physical constant expressed in different units.
#
# In DECOSOL this assignment is intentionally disabled because
# Water_Vapor_Pressure is now selected explicitly per model
# (VPM / ZHL) before tissue initialization.
#
# Keeping this block commented preserves traceability to the
# original Fortran implementation while avoiding double initialization.
# ----------------------------------------------------------------------
# if Units_Equal_Fsw:
#     Water_Vapor_Pressure = PVAPOR_VPM_FSW
# else:
#     Water_Vapor_Pressure = PVAPOR_VPM_MSW

    # CV / altitude flags
    Critical_Volume_Algorithm_Off = (Critical_Volume_Algorithm in ("OFF",))
    if Critical_Volume_Algorithm not in ("ON","OFF"):
        raise ValueError("ERROR! CRITICAL VOLUME ALGORITHM MUST BE ON OR OFF")

    Altitude_Dive_Algorithm_Off = (Altitude_Dive_Algorithm in ("OFF",))
    if Altitude_Dive_Algorithm not in ("ON","OFF"):
        raise ValueError("ERROR! ALTITUDE DIVE ALGORITHM MUST BE ON OR OFF")

    # ----------------------------
    # Half-times (Fortran DATA)
    # ----------------------------
    Nitrogen_Half_Time[:] = [5.0, 8.0, 12.5, 18.5, 27.0, 38.3, 54.3, 77.0,
                             109.0, 146.0, 187.0, 239.0, 305.0, 390.0, 498.0, 635.0]
    Helium_Half_Time[:] = [1.88, 3.02, 4.72, 6.99, 10.21, 14.48, 20.53, 29.11,
                           41.20, 55.19, 70.69, 90.34, 115.29, 147.42, 188.24, 240.03]

    # ----------------------------
    # Init constants/vars
    # ----------------------------
    Constant_Pressure_Other_Gases = (Pressure_Other_Gases_mmHg/760.0) * Units_Factor
    Run_Time = 0.0
    Segment_Number = 0

    for i in range(16):
        Helium_Time_Constant[i] = math.log(2.0)/Helium_Half_Time[i]
        Nitrogen_Time_Constant[i] = math.log(2.0)/Nitrogen_Half_Time[i]
        Max_Crushing_Pressure_He[i] = 0.0
        Max_Crushing_Pressure_N2[i] = 0.0
        Max_Actual_Gradient[i] = 0.0
        Surface_Phase_Volume_Time[i] = 0.0
        Amb_Pressure_Onset_of_Imperm[i] = 0.0
        Gas_Tension_Onset_of_Imperm[i] = 0.0
        Initial_Critical_Radius_N2[i] = Critical_Radius_N2_Microns * 1.0e-6
        Initial_Critical_Radius_He[i] = Critical_Radius_He_Microns * 1.0e-6
        Adjusted_Critical_Radius_N2[i] = Initial_Critical_Radius_N2[i]
        Adjusted_Critical_Radius_He[i] = Initial_Critical_Radius_He[i]

    # ----------------------------
    # Sea-level init of barometric pressure and initial tissue pressures
    # ----------------------------
    if Altitude_Dive_Algorithm_Off:
        CALC_BAROMETRIC_PRESSURE(0.0)
        # --- PVAPOR selection (locked) BEFORE tissue initialization ---
        global PVAPOR_LOCKED, PVAPOR_LOCK_SOURCE
        _deco_model = (os.environ.get('VPM_DECO_MODEL', 'VPM') or 'VPM').strip().upper()
        if not PVAPOR_LOCKED:
            if _deco_model in ('ZHL16','ZH-L16','ZHL16C','ZHL16-C','ZHL16B','ZHL16-B','ZH-L16B','ZH-L16-B'):
                if not PVAPOR_LOCKED: Water_Vapor_Pressure = PVAPOR_ZHL_FSW if Units_Equal_Fsw else PVAPOR_ZHL_MSW
                PVAPOR_LOCK_SOURCE = 'zhl16'
            else:
                if not PVAPOR_LOCKED: Water_Vapor_Pressure = PVAPOR_VPM_FSW if Units_Equal_Fsw else PVAPOR_VPM_MSW
                PVAPOR_LOCK_SOURCE = 'vpm'
            PVAPOR_LOCKED = True
        # --- end PVAPOR selection ---
        for i in range(16):
            Helium_Pressure[i] = REAL(0.0)
            Nitrogen_Pressure[i] = REAL((Barometric_Pressure - Water_Vapor_Pressure) * 0.79)
    else:
        raise NotImplementedError("Altitude algorithm not yet ported 1:1")

    # ----------------------------
    # READ .in
    # ----------------------------
    in_data = parse_in_text(VPMDECO_IN_TEXT)
    Number_of_Mixes = int(in_data["n_mixes"])
    mixes = in_data["mixes"]
    segments = in_data["segments"]
    ascent_changes = in_data["ascent_changes"]
    Repetitive_Dive_Flag = int(in_data["repetitive_code"])
    Dive_Bottom_Depth_M = 0.0  # max depth encountered in profile (msw or fsw units)

    # Load gas mixes (Fortran arrays are 1..10; here we keep 0..9)
    for j in range(Number_of_Mixes):
        fo2, fhe, fn2 = mixes[j]
        if abs((fo2+fhe+fn2) - 1.0) > 1.0e-6:
            raise ValueError("ERROR IN INPUT FILE (GASMIX DATA)")
        Fraction_Oxygen[j] = fo2
        Fraction_Helium[j] = fhe
        Fraction_Nitrogen[j] = fn2

    # ----------------------------
    # PROFILE LOOP
    # ----------------------------
    for seg in segments:
        code = int(seg["code"])
        if code == 1:
            Starting_Depth = float(seg["start"])
            Ending_Depth = float(seg["end"])
            Dive_Bottom_Depth_M = max(Dive_Bottom_Depth_M, Starting_Depth, Ending_Depth)
            Rate = float(seg["rate"])
            Mix_Number = int(seg["mix"])
            if _CCR_ACTIVE():
                # In CCR, standalone engine enforces single diluent (mix 1). GUI already segments/forces this.
                Mix_Number = 1
            _rt0 = Run_Time
            _ascent_with_stopv(Starting_Depth, Ending_Depth, Rate, Mix_Number, include_to_depth=False)
            _rt1 = Run_Time
            _kind = "DESC" if Ending_Depth > Starting_Depth else "ASC"
            _log_seg(_kind, from_m=Starting_Depth, to_m=Ending_Depth, mix=Mix_Number,
                     step_min=(_rt1 - _rt0), runtime_end=_rt1, note=f"rate={Rate}")
            if Ending_Depth > Starting_Depth:
                CALC_CRUSHING_PRESSURE(Starting_Depth, Ending_Depth, Rate)
        elif code == 2:
            Depth = float(seg["depth"])
            Dive_Bottom_Depth_M = max(Dive_Bottom_Depth_M, Depth)
            Run_Time_End_of_Segment = float(seg["runtime_end"])
            Mix_Number = int(seg["mix"])
            if _CCR_ACTIVE():
                # In CCR, standalone engine enforces single diluent (mix 1). GUI already segments/forces this.
                Mix_Number = 1
            _rt0 = Run_Time
            DEBUG_CTX_SEG_TYPE = "STOP"
            DEBUG_CTX_DEPTH_FROM = Depth
            DEBUG_CTX_DEPTH_TO = Depth
            GAS_LOADINGS_CONSTANT_DEPTH(Depth, Run_Time_End_of_Segment, Mix_Number)
            _rt1 = Run_Time
            _log_seg("CONST", depth_m=Depth, mix=Mix_Number, step_min=(_rt1 - _rt0),
                     runtime_end=_rt1, note="constant depth")
        elif code == 99:
            break
        else:
            raise ValueError("ERROR IN INPUT FILE (PROFILE CODE)")

    # ----------------------------
    # BEGIN ASCENT/DECO PROCESS
    # ----------------------------
    NUCLEAR_REGENERATION(Run_Time)
    CALC_INITIAL_ALLOWABLE_GRADIENT()

    for i in range(16):
        He_Pressure_Start_of_Ascent[i] = Helium_Pressure[i]
        N2_Pressure_Start_of_Ascent[i] = Nitrogen_Pressure[i]
    Run_Time_Start_of_Ascent = Run_Time
    Segment_Number_Start_of_Ascent = Segment_Number

    # ascent parameter changes
    Number_of_Changes = len(ascent_changes)
    for j in range(Number_of_Changes):
        Depth_Change[j] = float(ascent_changes[j]["start_depth"])
        Mix_Change[j] = int(ascent_changes[j]["mix"])
        Rate_Change[j] = float(ascent_changes[j]["rate"])
        Step_Size_Change[j] = float(ascent_changes[j]["step"])

    # Preserve input ascent gas schedule (used for bailout gas switching).
    # In CCR normal mode, the engine enforces single diluent (mix 1) for the whole dive;
    # GUI already supplies segmented CCR profile accordingly. For standalone CCR runs, we
    # must override any mix indices coming from the embedded .in.
    Mix_Change_input = Mix_Change[:Number_of_Changes]

    # Bailout requested in CCR: build an OC gas-switch schedule (mixes 2..N only),
    # gated by MOD (quantised down to 3 m). Until a bailout gas becomes usable, keep mix 1
    # so the engine stays in CCR. First time Mix_Number != 1, BO becomes effective and CCR is disabled.
    if bailout_active:
        for j in range(Number_of_Changes):
            try:
                Mix_Change_input[j] = _bo_pick_mix_for_depth(float(Depth_Change[j]))
            except Exception:
                Mix_Change_input[j] = 1
    if _CCR_ACTIVE():
        for j in range(Number_of_Changes):
            Mix_Change[j] = 1

    # If bailout is active, ascent/deco becomes OC from *start of ascent* onward:
    # - Inspired inerts follow OC (dryPamb * Fx)
    # - Gas switching follows the input schedule, typically using mixes 2..N (mix 1 is diluent).
    if bailout_active:
        # restore input schedule (no-op if already intact)
        for j in range(Number_of_Changes):
            Mix_Change[j] = Mix_Change_input[j]
        # Bailout requested: keep CCR physics until the first real bailout mix is engaged (Mix_Number != 1).
        global BO_MODE, BO_EFFECTIVE
        BO_MODE = True
        BO_EFFECTIVE = False
    Starting_Depth = Depth_Change[0]
    Mix_Number = Mix_Change[0]
    Rate = Rate_Change[0]
    Step_Size = Step_Size_Change[0]


    # [BO policy3] If bailout is requested and a BO gas is already valid at the start depth,
    # engage BO *immediately* for the purposes of SoDZ search (CALC_START_OF_DECO_ZONE) and the
    # initial ascent-to-SoDZ gas loadings. This matches V-Planner-like behaviour where BO begins
    # at end-of-bottom when a suitable BO gas exists at that depth.
    if bailout_active and BO_MODE and (not BO_EFFECTIVE):
        try:
            _mix0 = _bo_pick_first_valid_in_order(float(Starting_Depth))
        except Exception:
            _mix0 = 1
        if _mix0 != 1:
            Mix_Number = _mix0
            BO_EFFECTIVE = True
            # keep the first change entry consistent with the engaged mix (does not affect rates/steps)
            try:
                Mix_Change[0] = _mix0
            except Exception:
                pass

    # start of deco zone + deepest possible stop
    # (debug_input_calc_start_decozone.csv disabled for production)

    Depth_Start_of_Deco_Zone = CALC_START_OF_DECO_ZONE(Starting_Depth, Rate)

    if Units_Equal_Fsw:
        if Step_Size < 10.0:
            Rounding_Operation1 = (Depth_Start_of_Deco_Zone/Step_Size) - 0.5
            Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1) * Step_Size
        else:
            Rounding_Operation1 = (Depth_Start_of_Deco_Zone/10.0) - 0.5
            Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1) * 10.0
    else:
        if Step_Size < 3.0:
            Rounding_Operation1 = (Depth_Start_of_Deco_Zone/Step_Size) - 0.5
            Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1) * Step_Size
        else:
            Rounding_Operation1 = (Depth_Start_of_Deco_Zone/3.0) - 0.5
            Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1) * 3.0

    # temporarily ascend to start of deco zone (with optional STOPV)
    _ascent_with_stopv(Starting_Depth, Depth_Start_of_Deco_Zone, Rate, Mix_Number, include_to_depth=True)
    Run_Time_Start_of_Deco_Zone = Run_Time

    Deco_Phase_Volume_Time = 0.0
    Last_Run_Time = 0.0
    Schedule_Converged = False

    for i in range(16):
        Last_Phase_Volume_Time[i] = 0.0
        He_Pressure_Start_of_Deco_Zone[i] = Helium_Pressure[i]
        N2_Pressure_Start_of_Deco_Zone[i] = Nitrogen_Pressure[i]
        Max_Actual_Gradient[i] = 0.0


    # ============================================================
    # ZH-L16 (single-pass) switch (no GUI changes; MAIN-only)
    # ============================================================
    _deco_model = (os.environ.get("VPM_DECO_MODEL", "VPM") or "VPM").strip().upper()
    # Select PVAPOR convention by algorithm (prevents cross-contamination between VPM and ZH-L16).
    # NOTE: coefficients a/b come from GUI for ZH-L16; PVAPOR is a model constant, kept internal to MAIN.
    if _deco_model in ("ZHL16", "ZH-L16", "ZHL16C", "ZHL16-C", "ZHL16B", "ZHL16-B"):
        if not PVAPOR_LOCKED: Water_Vapor_Pressure = PVAPOR_ZHL_FSW if Units_Equal_Fsw else PVAPOR_ZHL_MSW
    else:
        if not PVAPOR_LOCKED: Water_Vapor_Pressure = PVAPOR_VPM_FSW if Units_Equal_Fsw else PVAPOR_VPM_MSW

    if _deco_model in ("ZHL16", "ZH-L16", "ZHL16C", "ZHL16-C", "ZHL16B", "ZHL16-B", "ZH-L16B", "ZH-L16-B"):
        # Parse GF from env (GUI uses %), accept both 0.30 or 30
        def _parse_gf(_s: str, _default: float) -> float:

            """Parse GF from env/GUI. Accepts fraction (0.30) or percent (30)."""
            try:
                v = float(str(_s).strip())
                if v > 1.0 and v <= 110.0:
                    v = v / 100.0
            except Exception:
                v = float(_default)
            if v < 0.01:
                v = 0.01
            if v > 1.1:
                v = 1.1
            return v

        _gf_low = _parse_gf(os.environ.get("VPM_GF_LOW", "0.30"), 0.30)
        _gf_high = _parse_gf(os.environ.get("VPM_GF_HIGH", "0.85"), 0.85)

        # Run ZH-L16 single-pass decompression from the start-of-deco-zone state
        
        # Select active ZH-L16 coefficients (from GUI)
        _zhl_var = os.environ.get("VPM_ZHL16_VARIANT", "") or _deco_model
        _zhl_json = os.environ.get("VPM_ZHL16_COEFFS_JSON", "")
        _zhl16_set_active_coeffs(_zhl_var, _zhl_json)
        zhl = ZHL16_SINGLEPASS_DECO(
            depth_start_deco_zone=float(Depth_Start_of_Deco_Zone),
            depth_bottom=float(Dive_Bottom_Depth_M),
            rate=float(Rate),
            step_size=float(Step_Size),
            mix_start=int(Mix_Number),
            number_of_changes=int(Number_of_Changes),
            depth_change=Depth_Change,
            mix_change=Mix_Change,
            rate_change=Rate_Change,
            step_size_change=Step_Size_Change,
            gf_low=_gf_low,
            gf_high=_gf_high,
            last_stop_m=float(os.environ.get('VPM_LAST_STOP_M', '3.0')),
            rapsol=float(rapsol1),
            log_seg=_log_seg,
            ascent_with_stopv=_ascent_with_stopv,
        )

        # (profile_debug_v10.csv disabled for production)
        return {
            "runtime_total": Run_Time,
            "first_stop_depth": float(zhl.get("first_stop_depth") or 0.0),
            "stops": list(zhl.get("stops") or []),
            "depth_start_of_deco_zone": float(Depth_Start_of_Deco_Zone),
            "profile": profile_log,
            "profile_final": _select_profile_final_run(DEBUG_PROFILE_ROWS, list(zhl.get("stops") or []), Run_Time),
        }
    first_stop_depth_out = None
    final_stops = []

    # ----------------------------
    # CRITICAL VOLUME LOOP
    # ----------------------------
    while True:
        Ascent_Ceiling_Depth = CALC_ASCENT_CEILING()

        if Ascent_Ceiling_Depth <= 0.0:
            Deco_Stop_Depth = 0.0
        else:
            Rounding_Operation2 = (Ascent_Ceiling_Depth/Step_Size) + 0.5
            Deco_Stop_Depth = ANINT(Rounding_Operation2) * Step_Size

        if Deco_Stop_Depth > Depth_Start_of_Deco_Zone:
            raise RuntimeError("ERROR! STEP SIZE IS TOO LARGE TO DECOMPRESS")

        Deco_Stop_Depth = PROJECTED_ASCENT(Depth_Start_of_Deco_Zone, Rate, Deco_Stop_Depth, Step_Size)

        if Deco_Stop_Depth > Depth_Start_of_Deco_Zone:
            raise RuntimeError("ERROR! STEP SIZE IS TOO LARGE TO DECOMPRESS")

        # Special case: no deco
        if Deco_Stop_Depth == 0.0:
            for i in range(16):
                Helium_Pressure[i] = He_Pressure_Start_of_Ascent[i]
                Nitrogen_Pressure[i] = N2_Pressure_Start_of_Ascent[i]
            Run_Time = Run_Time_Start_of_Ascent
            Segment_Number = Segment_Number_Start_of_Ascent
            # --- Passive profile: keep pre-ascent segments, then log FINAL no-deco ascent only (report-only)
            if log_enabled:
                try:
                    _rt_keep = float(Run_Time_Start_of_Ascent)
                except Exception:
                    _rt_keep = 0.0
                # keep only rows up to start-of-ascent (drops trial CV passes)
                _kept = []
                for _r in list(profile_log):
                    try:
                        _rt = float(_r.get('runtime_end', 0.0))
                    except Exception:
                        _rt = 0.0
                    if _rt <= _rt_keep + 1e-6:
                        _kept.append(_r)
                profile_log[:] = _kept
            Starting_Depth = Depth_Change[0]
            Ending_Depth = 0.0
            _ascent_with_stopv(Starting_Depth, Ending_Depth, Rate, Mix_Number, include_to_depth=True)
            # (profile_debug_v10.csv disabled for production)
            return {
                "runtime_total": Run_Time,
                "first_stop_depth": 0.0,
                "stops": [],
                "depth_start_of_deco_zone": Depth_Start_of_Deco_Zone,
                "profile": profile_log,
                "profile_final": _select_profile_final_run(DEBUG_PROFILE_ROWS, final_stops, Run_Time),
            }

        Starting_Depth = Depth_Start_of_Deco_Zone
        First_Stop_Depth = Deco_Stop_Depth
        first_stop_depth_out = First_Stop_Depth

        # --- trial deco stop loop block (no output) ---
        while True:
            _ascent_with_stopv(Starting_Depth, Deco_Stop_Depth, Rate, Mix_Number, include_to_depth=False)
            if Deco_Stop_Depth <= 0.0:
                break

            if Number_of_Changes > 1:
                for j in range(1, Number_of_Changes):
                    if Depth_Change[j] >= Deco_Stop_Depth:
                        Mix_Number = Mix_Change[j]
                        _update_bo_effective(Mix_Number)
                        Rate = Rate_Change[j]
                        Step_Size = Step_Size_Change[j]

            
            # [BO] Apply dynamic bailout gas switching at each stop depth also in TRIAL schedule
            Mix_Number = _bo_apply_dynamic(Mix_Number, Deco_Stop_Depth)
            _update_bo_effective(Mix_Number)

            BOYLES_LAW_COMPENSATION(First_Stop_Depth, Deco_Stop_Depth, Step_Size)
            _stopv_applied_min = _apply_stopv_at_depth(Deco_Stop_Depth, Mix_Number)
            # STOPV extension (non-Fortran): if STOPV already satisfies this deco step, skip VPM stop
            _next_stop = Deco_Stop_Depth - Step_Size
            if _next_stop < 0.0:
                _next_stop = 0.0
            _ceiling = CALC_DECO_CEILING()
            if (_stopv_applied_min > 0.0) and (_ceiling <= _next_stop):
                _rt0 = Run_Time
                _rt1 = Run_Time
                _log_seg("STOP", depth_m=Deco_Stop_Depth, mix=Mix_Number, step_min=0.0,
                         runtime_end=_rt1, note="deco stop satisfied by STOPV")
            else:
                _rt0 = Run_Time
                DECOMPRESSION_STOP(Deco_Stop_Depth, Step_Size)
                _rt1 = Run_Time
                if (_rt1 - _rt0) > 0.0:
                    _log_seg("STOP", depth_m=Deco_Stop_Depth, mix=Mix_Number, step_min=(_rt1 - _rt0),
                             runtime_end=_rt1, note="deco stop")

            Starting_Depth = Deco_Stop_Depth
            Deco_Stop_Depth = Deco_Stop_Depth - Step_Size
            Last_Run_Time = Run_Time

        Deco_Phase_Volume_Time = Run_Time - Run_Time_Start_of_Deco_Zone
        CALC_SURFACE_PHASE_VOLUME_TIME()

        Schedule_Converged = False
        for i in range(16):
            Phase_Volume_Time[i] = Deco_Phase_Volume_Time + Surface_Phase_Volume_Time[i]
            Critical_Volume_Comparison = abs(Phase_Volume_Time[i] - Last_Phase_Volume_Time[i])
            if Critical_Volume_Comparison <= 1.0:
                Schedule_Converged = True
        # (debug_press_tess_fine_t_fondo.csv disabled for production)

        if Schedule_Converged or Critical_Volume_Algorithm_Off:
            # reset to start of ascent and compute final schedule with outputs
            for i in range(16):
                Helium_Pressure[i] = He_Pressure_Start_of_Ascent[i]
                Nitrogen_Pressure[i] = N2_Pressure_Start_of_Ascent[i]
            Run_Time = Run_Time_Start_of_Ascent
            Segment_Number = Segment_Number_Start_of_Ascent
            # --- Passive profile: keep pre-ascent segments, then log FINAL schedule only (report-only)
            if log_enabled:
                try:
                    _rt_keep = float(Run_Time_Start_of_Ascent)
                except Exception:
                    _rt_keep = 0.0
                # keep only rows up to start-of-ascent (drops trial CV passes)
                _kept = []
                for _r in list(profile_log):
                    try:
                        _rt = float(_r.get('runtime_end', 0.0))
                    except Exception:
                        _rt = 0.0
                    if _rt <= _rt_keep + 1e-6:
                        _kept.append(_r)
                profile_log[:] = _kept

            Starting_Depth = Depth_Change[0]
            Mix_Number = Mix_Change[0]
            _update_bo_effective(Mix_Number)
            Rate = Rate_Change[0]
            Step_Size = Step_Size_Change[0]
            Deco_Stop_Depth = First_Stop_Depth
            Last_Run_Time = 0.0

            final_stops = []
            while True:
                _ascent_with_stopv(Starting_Depth, Deco_Stop_Depth, Rate, Mix_Number, include_to_depth=False)

                CALC_MAX_ACTUAL_GRADIENT(Deco_Stop_Depth)

                if Deco_Stop_Depth <= 0.0:
                    break

                if Number_of_Changes > 1:
                    for j in range(1, Number_of_Changes):
                        if Depth_Change[j] >= Deco_Stop_Depth:
                            # Ascent/deco parameter changes (rate/step) apply regardless of bailout.
                            # Gas selection in bailout is handled only at deco stops via _bo_apply_dynamic().
                            if not BO_MODE:
                                Mix_Number = Mix_Change[j]
                                _update_bo_effective(Mix_Number)
                            Rate = Rate_Change[j]
                            Step_Size = Step_Size_Change[j]

                Mix_Number = _bo_apply_dynamic(Mix_Number, Deco_Stop_Depth)
                _update_bo_effective(Mix_Number)

                BOYLES_LAW_COMPENSATION(First_Stop_Depth, Deco_Stop_Depth, Step_Size)
                _stopv_applied_min = _apply_stopv_at_depth(Deco_Stop_Depth, Mix_Number)
                # STOPV extension (non-Fortran): if STOPV already satisfies this deco step, skip VPM stop
                _next_stop = Deco_Stop_Depth - Step_Size
                if _next_stop < 0.0:
                    _next_stop = 0.0
                _ceiling = CALC_DECO_CEILING()
                if (_stopv_applied_min > 0.0) and (_ceiling <= _next_stop):
                    _rt0 = Run_Time
                    _rt1 = Run_Time
                                        # GF actual used by ZH-L16 at this stop depth (percent)
                    try:
                        _gf_actual_pct = 100.0 * float(_zhl16_gf_at_depth(float(Deco_Stop_Depth), float(anchor_gf_low_m), float(anchor_gf_high_m), float(gf_low), float(gf_high), float(baro_msw)))
                    except Exception:
                        _gf_actual_pct = None
                    _log_seg('STOP', depth_m=Deco_Stop_Depth, mix=Mix_Number,
                             step_min=0.0, runtime_end=_rt1, note='deco stop satisfied by STOPV', gf_actual=_gf_actual_pct)
                    Stop_Time = 0.0
                else:
                    _rt0 = Run_Time
                    DECOMPRESSION_STOP(Deco_Stop_Depth, Step_Size)
                    _rt1 = Run_Time
                    if (_rt1 - _rt0) > 0.0:
                                                # GF actual used by ZH-L16 at this stop depth (percent)
                        try:
                            _gf_actual_pct = 100.0 * float(_zhl16_gf_at_depth(float(Deco_Stop_Depth), float(anchor_gf_low_m), float(anchor_gf_high_m), float(gf_low), float(gf_high), float(baro_msw)))
                        except Exception:
                            _gf_actual_pct = None
                        _log_seg('STOP', depth_m=Deco_Stop_Depth, mix=Mix_Number,
                                 step_min=(_rt1 - _rt0), runtime_end=_rt1, note='deco stop', gf_actual=_gf_actual_pct)

                    if Last_Run_Time == 0.0:
                        Stop_Time = ANINT((Segment_Time/Minimum_Deco_Stop_Time) + 0.5) * Minimum_Deco_Stop_Time
                    else:
                        Stop_Time = Run_Time - Last_Run_Time

                final_stops.append((Deco_Stop_Depth, Stop_Time, Run_Time))

                Starting_Depth = Deco_Stop_Depth
                Deco_Stop_Depth = Deco_Stop_Depth - Step_Size
                Last_Run_Time = Run_Time

            # --- PASSIVE DEBUG: write CSV at end of run (always overwrite) ---
            # (profile_debug_v10.csv disabled for production)

            return {
                "runtime_total": Run_Time,
                "first_stop_depth": float(first_stop_depth_out),
                "stops": final_stops,
                "depth_start_of_deco_zone": float(Depth_Start_of_Deco_Zone),
                "profile": profile_log,
                "profile_final": _select_profile_final_run(DEBUG_PROFILE_ROWS, final_stops, Run_Time),
            }

        # Not converged and CV ON => relax gradients and iterate
        CRITICAL_VOLUME(Deco_Phase_Volume_Time)
        Deco_Phase_Volume_Time = 0.0
        Run_Time = Run_Time_Start_of_Deco_Zone
        Starting_Depth = Depth_Start_of_Deco_Zone
        Mix_Number = Mix_Change[0]
        Rate = Rate_Change[0]
        Step_Size = Step_Size_Change[0]
        for i in range(16):
            Last_Phase_Volume_Time[i] = Phase_Volume_Time[i]
            Helium_Pressure[i] = He_Pressure_Start_of_Deco_Zone[i]
            Nitrogen_Pressure[i] = N2_Pressure_Start_of_Deco_Zone[i]


def _select_final_profile_by_stops(profile_rows, stops_list, target_rt):
    """Select the *final* segment log among multiple internal passes.

    We prefer the pass whose STOP rows best match the engine stop list (depth, stop_time, run_time_end),
    then break ties by closeness of the last runtime_end to target_rt, and then by length.
    This is report-only and must not affect the engine state.
    """
    if not profile_rows:
        return []
    # Split into candidate runs whenever runtime_end decreases (engine internal replays)
    runs = []
    cur = []
    prev_rt = None
    for row in profile_rows:
        rt = row.get("runtime_end")
        if cur and rt is not None and prev_rt is not None and rt < prev_rt - 1e-6:
            runs.append(cur)
            cur = []
        cur.append(row)
        prev_rt = rt
    if cur:
        runs.append(cur)

    # Normalize engine stop list for matching
    eng = []
    for d, st, rt in (stops_list or []):
        eng.append((float(d), float(st), float(rt)))
    def score_run(r):
        # Extract STOP rows that correspond to deco stops
        # We match by depth and runtime_end primarily; stop_time from step_min.
        stop_rows = []
        for row in r:
            if (row.get("kind") == "STOP") and ("deco stop" in (row.get("note") or "")):
                dm = row.get("depth_m")
                step = row.get("step_min")
                rt = row.get("runtime_end")
                if dm is None or step is None or rt is None:
                    continue
                stop_rows.append((float(dm), float(step), float(rt)))
        # Compare sequentially to engine list; allow small rounding diffs
        tol_depth = 1e-3
        tol_time = 0.51  # minutes, because some prints are rounded to 0.1 but we keep internal 0.001
        matches = 0
        mismatches = 0
        n = min(len(stop_rows), len(eng))
        for i in range(n):
            d1, st1, rt1 = stop_rows[i]
            d2, st2, rt2 = eng[i]
            if abs(d1 - d2) <= tol_depth and abs(rt1 - rt2) <= tol_time and abs(st1 - st2) <= tol_time:
                matches += 1
            else:
                mismatches += 1
        # Penalize missing/extra stops
        mismatches += abs(len(stop_rows) - len(eng))
        end_rt = r[-1].get("runtime_end")
        end_rt = float(end_rt) if end_rt is not None else 0.0
        # Higher matches, lower mismatches, closer end_rt
        return (matches, -mismatches, -abs(end_rt - float(target_rt)), len(r))

    best = None
    best_score = None
    for r in runs:
        sc = score_run(r)
        if best is None or sc > best_score:
            best = r
            best_score = sc
    return best if best is not None else runs[-1]

def SCHREINER_EQUATION(Initial_Inspired_Gas_Pressure: float,
                       Rate_Change_Insp_Gas_Pressure: float,
                       Interval_Time: float,
                       Gas_Time_Constant: float,
                       Initial_Gas_Pressure: float) -> float:
    """FORTRAN FUNCTION SCHREINER_EQUATION (1:1).

    Applied during linear ascents/descents at constant rate.
    For ascents, a negative number for rate must be used (as in Fortran).
    """
    return (
        Initial_Inspired_Gas_Pressure
        + Rate_Change_Insp_Gas_Pressure * (Interval_Time - 1.0 / Gas_Time_Constant)
        - (Initial_Inspired_Gas_Pressure - Initial_Gas_Pressure - Rate_Change_Insp_Gas_Pressure / Gas_Time_Constant)
        * math.exp(-Gas_Time_Constant * Interval_Time)
    )


def HALDANE_EQUATION(Initial_Gas_Pressure: float,
                     Inspired_Gas_Pressure: float,
                     Gas_Time_Constant: float,
                     Interval_Time: float) -> float:
    """FORTRAN FUNCTION HALDANE_EQUATION (1:1).

    Applied during constant depth intervals.
    """
    return Initial_Gas_Pressure + (Inspired_Gas_Pressure - Initial_Gas_Pressure) * (1.0 - math.exp(-Gas_Time_Constant * Interval_Time))


def GAS_LOADINGS_ASCENT_DESCENT(Starting_Depth: float, Ending_Depth: float, Rate: float) -> None:
    """FORTRAN SUBROUTINE GAS_LOADINGS_ASCENT_DESCENT (1:1).

    Applies SCHREINER_EQUATION to update Helium_Pressure and Nitrogen_Pressure
    for a linear ascent/descent segment at constant Rate.
    """
    global Run_Time, Segment_Number, Segment_Time
    global Ending_Ambient_Pressure, Mix_Number, Barometric_Pressure
    global Water_Vapor_Pressure
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Helium_Pressure, Nitrogen_Pressure
    global Fraction_Helium, Fraction_Nitrogen
    global Initial_Helium_Pressure, Initial_Nitrogen_Pressure

    # CALCULATIONS (Fortran order preserved)
    Segment_Time = (Ending_Depth - Starting_Depth) / Rate
    Last_Run_Time = Run_Time
    Run_Time = Last_Run_Time + Segment_Time
    Last_Segment_Number = Segment_Number
    Segment_Number = Last_Segment_Number + 1

    Ending_Ambient_Pressure = Ending_Depth + Barometric_Pressure
    Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure

    # Mix_Number is 1-based in Fortran
    mh = Fraction_Helium[Mix_Number - 1]
    mn = Fraction_Nitrogen[Mix_Number - 1]

    if _CCR_ACTIVE():
        # CCR: mix fractions are DILUENT fractions; clamp includes PVAPOR via (Pamb - Water_Vapor_Pressure)
        P_alv_start = (Starting_Ambient_Pressure - Water_Vapor_Pressure)
        FO2_dil = Fraction_Oxygen[Mix_Number - 1]
        FHe_dil = mh
        FN2_dil = mn
        sp_seg_msw = _CCR_GET_SP_MSW(Starting_Depth, Rate, is_constant_depth=False)
        ppHe_start, ppN2_start, clamp_mode = _CCR_CLAMP_PPO2_AND_INERTS(P_alv_start, FO2_dil, FHe_dil, FN2_dil, sp_seg_msw)
        Initial_Inspired_He_Pressure = ppHe_start
        Initial_Inspired_N2_Pressure = ppN2_start
        Helium_Rate, Nitrogen_Rate = _CCR_RATES(Rate, clamp_mode, FHe_dil, FN2_dil, FO2_dil)

        # CCR clamp: if a linear segment crosses the PAMBMAX boundary (P_alv == SP),
        # split the Schreiner update so pp_inert never becomes negative (and can turn on after the boundary).
        ccr_split_pambmax = False
        ccr_t1 = 0.0
        ccr_t2 = 0.0
        # stage2 parameters (defaults are PAMBMAX: pp_inert = 0)
        ccr2_insp_he = 0.0
        ccr2_insp_n2 = 0.0
        ccr2_rate_he = 0.0
        ccr2_rate_n2 = 0.0
        if Rate != 0.0:
            P_alv_end = (Ending_Ambient_Pressure - Water_Vapor_Pressure)
            if (P_alv_start - sp_seg_msw) * (P_alv_end - sp_seg_msw) < 0.0:
                Depth_PAMBMAX = (sp_seg_msw + Water_Vapor_Pressure) - Barometric_Pressure
                ccr_t1 = (Depth_PAMBMAX - Starting_Depth) / Rate
                if ccr_t1 < 0.0:
                    ccr_t1 = 0.0
                if ccr_t1 > Segment_Time:
                    ccr_t1 = Segment_Time
                ccr_t2 = Segment_Time - ccr_t1
                # Determine direction: if we start in PAMBMAX and go deeper, stage2 becomes SP (inerts turn on).
                if clamp_mode == 'PAMBMAX' and (P_alv_end > sp_seg_msw) and ccr_t2 > 0.0:
                    denom = FHe_dil + FN2_dil
                    if denom > 0.0:
                        # At the boundary, pp_inert = 0, then increases with slope = Rate
                        ccr2_insp_he = 0.0
                        ccr2_insp_n2 = 0.0
                        ccr2_rate_he = Rate * (FHe_dil / denom)
                        ccr2_rate_n2 = Rate * (FN2_dil / denom)
                # else: SP -> PAMBMAX (or any other crossing): stage2 stays zero-inert
                ccr_split_pambmax = (ccr_t2 > 0.0 and ccr_t1 >= 0.0)
    else:
        Initial_Inspired_He_Pressure = (Starting_Ambient_Pressure - Water_Vapor_Pressure) * mh
        Initial_Inspired_N2_Pressure = (Starting_Ambient_Pressure - Water_Vapor_Pressure) * mn

        Helium_Rate = Rate * mh
        Nitrogen_Rate = Rate * mn

    # --- PASSIVE DEBUG (effective inspired pp at segment start) ---
    try:
        _seg_type = DEBUG_CTX_SEG_TYPE if DEBUG_CTX_SEG_TYPE in ("DESC", "ASC", "STOP") else ("DESC" if Ending_Depth > Starting_Depth else "ASC")
        P_alv_start_dbg = (Starting_Ambient_Pressure - Water_Vapor_Pressure)
        ppHe_dbg = float(Initial_Inspired_He_Pressure)
        ppN2_dbg = float(Initial_Inspired_N2_Pressure)
        ppO2_dbg = float(max(0.0, P_alv_start_dbg - ppHe_dbg - ppN2_dbg))
        _debug_profile_append(_seg_type, Starting_Depth, Ending_Depth, Run_Time, Mix_Number,
                             ppO2_dbg, ppN2_dbg, ppHe_dbg,
                             bool(_CCR_ACTIVE()), bool(BO_EFFECTIVE))
    except Exception:
        pass

    for i in range(16):
        Initial_Helium_Pressure[i] = Helium_Pressure[i]
        Initial_Nitrogen_Pressure[i] = Nitrogen_Pressure[i]

        if _CCR_ACTIVE() and ccr_split_pambmax:
            # Stage 1: CCR mode as computed at segment start, up to the boundary
            he_mid = SCHREINER_EQUATION(
                Initial_Inspired_He_Pressure,
                Helium_Rate,
                ccr_t1,
                Helium_Time_Constant[i],
                Initial_Helium_Pressure[i],
            )
            n2_mid = SCHREINER_EQUATION(
                Initial_Inspired_N2_Pressure,
                Nitrogen_Rate,
                ccr_t1,
                Nitrogen_Time_Constant[i],
                Initial_Nitrogen_Pressure[i],
            )
            # Stage 2: boundary-crossing continuation (PAMBMAX by default; SP if inerts turn on after boundary)
            Helium_Pressure[i] = SCHREINER_EQUATION(
                ccr2_insp_he,
                ccr2_rate_he,
                ccr_t2,
                Helium_Time_Constant[i],
                he_mid,
            )
            Nitrogen_Pressure[i] = SCHREINER_EQUATION(
                ccr2_insp_n2,
                ccr2_rate_n2,
                ccr_t2,
                Nitrogen_Time_Constant[i],
                n2_mid,
            )
        else:
            Helium_Pressure[i] = SCHREINER_EQUATION(
                Initial_Inspired_He_Pressure,
                Helium_Rate,
                Segment_Time,
                Helium_Time_Constant[i],
                Initial_Helium_Pressure[i],
            )

            Nitrogen_Pressure[i] = SCHREINER_EQUATION(
                Initial_Inspired_N2_Pressure,
                Nitrogen_Rate,
                Segment_Time,
                Nitrogen_Time_Constant[i],
                Initial_Nitrogen_Pressure[i],
            )
    return



def CALC_CRUSHING_PRESSURE(Starting_Depth: float, Ending_Depth: float, Rate: float) -> None:
    """FORTRAN SUBROUTINE CALC_CRUSHING_PRESSURE (1:1)."""
    global Gradient_Onset_of_Imperm_Atm
    global Constant_Pressure_Other_Gases
    global Surface_Tension_Gamma, Skin_Compression_GammaC, rapsol1, rapsol2
    global Units_Factor, Barometric_Pressure
    global Helium_Pressure, Nitrogen_Pressure
    global Adjusted_Critical_Radius_He, Adjusted_Critical_Radius_N2
    global Max_Crushing_Pressure_He, Max_Crushing_Pressure_N2
    global Amb_Pressure_Onset_of_Imperm, Gas_Tension_Onset_of_Imperm
    global Initial_Helium_Pressure, Initial_Nitrogen_Pressure

    Gradient_Onset_of_Imperm = Gradient_Onset_of_Imperm_Atm * Units_Factor
    Gradient_Onset_of_Imperm_Pa = Gradient_Onset_of_Imperm_Atm * 101325.0

    Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure
    Ending_Ambient_Pressure = Ending_Depth + Barometric_Pressure

    for I in range(1, 17):
        Starting_Gas_Tension = (
            Initial_Helium_Pressure[I - 1]
            + Initial_Nitrogen_Pressure[I - 1]
            + Constant_Pressure_Other_Gases
        )
        Starting_Gradient = Starting_Ambient_Pressure - Starting_Gas_Tension

        Ending_Gas_Tension = (
            Helium_Pressure[I - 1]
            + Nitrogen_Pressure[I - 1]
            + Constant_Pressure_Other_Gases
        )
        Ending_Gradient = Ending_Ambient_Pressure - Ending_Gas_Tension

        Radius_Onset_of_Imperm_He = 1.0 / (
            Gradient_Onset_of_Imperm_Pa / (2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma))
            + 1.0 / Adjusted_Critical_Radius_He[I - 1]
        )

        Radius_Onset_of_Imperm_N2 = 1.0 / (
            Gradient_Onset_of_Imperm_Pa / (2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma))
            + 1.0 / Adjusted_Critical_Radius_N2[I - 1]
        )

        if Ending_Gradient <= Gradient_Onset_of_Imperm:
            Crushing_Pressure_He = Ending_Ambient_Pressure - Ending_Gas_Tension
            Crushing_Pressure_N2 = Ending_Ambient_Pressure - Ending_Gas_Tension

        if Ending_Gradient > Gradient_Onset_of_Imperm:
            if Starting_Gradient == Gradient_Onset_of_Imperm:
                Amb_Pressure_Onset_of_Imperm[I - 1] = Starting_Ambient_Pressure
                Gas_Tension_Onset_of_Imperm[I - 1] = Starting_Gas_Tension

            if Starting_Gradient < Gradient_Onset_of_Imperm:
                ONSET_OF_IMPERMEABILITY(Starting_Ambient_Pressure, Ending_Ambient_Pressure, Rate, I)

            Ending_Ambient_Pressure_Pa = (Ending_Ambient_Pressure / Units_Factor) * 101325.0
            Amb_Press_Onset_of_Imperm_Pa = (Amb_Pressure_Onset_of_Imperm[I - 1] / Units_Factor) * 101325.0
            Gas_Tension_Onset_of_Imperm_Pa = (Gas_Tension_Onset_of_Imperm[I - 1] / Units_Factor) * 101325.0

            B_He = 2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma)
            A_He = (
                Ending_Ambient_Pressure_Pa
                - Amb_Press_Onset_of_Imperm_Pa
                + Gas_Tension_Onset_of_Imperm_Pa
                + (2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma)) / Radius_Onset_of_Imperm_He
            )
            C_He = Gas_Tension_Onset_of_Imperm_Pa * Radius_Onset_of_Imperm_He ** 3

            D = (
                B_He ** 3
                + 27.0 / 2.0 * A_He ** 2 * C_He
                + 3.0 / 2.0 * math.sqrt(3.0) * A_He * math.sqrt(4.0 * B_He ** 3 * C_He + 27.0 * A_He ** 2 * C_He ** 2)
            ) ** (1.0 / 3.0)
            Ending_Radius_He = 1.0 / 3.0 * (B_He / A_He + B_He ** 2 / (A_He * D) + D / A_He)

            Crushing_Pressure_Pascals_He = (
                Gradient_Onset_of_Imperm_Pa
                + Ending_Ambient_Pressure_Pa
                - Amb_Press_Onset_of_Imperm_Pa
                + Gas_Tension_Onset_of_Imperm_Pa * (1.0 - Radius_Onset_of_Imperm_He ** 3 / Ending_Radius_He ** 3)
            )

            Crushing_Pressure_He = (Crushing_Pressure_Pascals_He / 101325.0) * Units_Factor

            B_N2 = 2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma)
            A_N2 = (
                Ending_Ambient_Pressure_Pa
                - Amb_Press_Onset_of_Imperm_Pa
                + Gas_Tension_Onset_of_Imperm_Pa
                + (2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma)) / Radius_Onset_of_Imperm_N2
            )
            C_N2 = Gas_Tension_Onset_of_Imperm_Pa * Radius_Onset_of_Imperm_N2 ** 3

            D = (
                B_N2 ** 3
                + 27.0 / 2.0 * A_N2 ** 2 * C_N2
                + 3.0 / 2.0 * math.sqrt(3.0) * A_N2 * math.sqrt(4.0 * B_N2 ** 3 * C_N2 + 27.0 * A_N2 ** 2 * C_N2 ** 2)
            ) ** (1.0 / 3.0)
            Ending_Radius_N2 = 1.0 / 3.0 * (B_N2 / A_N2 + B_N2 ** 2 / (A_N2 * D) + D / A_N2)

            Crushing_Pressure_Pascals_N2 = (
                Gradient_Onset_of_Imperm_Pa
                + Ending_Ambient_Pressure_Pa
                - Amb_Press_Onset_of_Imperm_Pa
                + Gas_Tension_Onset_of_Imperm_Pa * (1.0 - Radius_Onset_of_Imperm_N2 ** 3 / Ending_Radius_N2 ** 3)
            )

            Crushing_Pressure_N2 = (Crushing_Pressure_Pascals_N2 / 101325.0) * Units_Factor

        Max_Crushing_Pressure_He[I - 1] = max(Max_Crushing_Pressure_He[I - 1], Crushing_Pressure_He)
        Max_Crushing_Pressure_N2[I - 1] = max(Max_Crushing_Pressure_N2[I - 1], Crushing_Pressure_N2)

    return



def ONSET_OF_IMPERMEABILITY(Starting_Ambient_Pressure: float, Ending_Ambient_Pressure: float, Rate: float, I: int) -> None:
    """FORTRAN SUBROUTINE ONSET_OF_IMPERMEABILITY (1:1).

    NOTE (bugfix, no physics change intended):
    In CCR mode, inspired inert pressures and their rates are NOT simply (Pamb-Pvapor)*Fraction.
    They depend on PPO2 clamp logic (SP / PAMBMAX) and may require a split if a linear segment
    crosses the PAMBMAX boundary (P_alv == SP). If we ignore that, the bisection bracketing
    can fail spuriously ("root not within brackets") even when the caller's gradients indicate
    a crossing.
    This routine therefore mirrors the same CCR clamp + optional split logic used by
    GAS_LOADINGS_ASCENT_DESCENT for the specific segment.
    """
    global Water_Vapor_Pressure
    global Gradient_Onset_of_Imperm_Atm
    global Constant_Pressure_Other_Gases
    global Mix_Number
    global Units_Factor
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Fraction_Helium, Fraction_Nitrogen, Fraction_Oxygen
    global Amb_Pressure_Onset_of_Imperm, Gas_Tension_Onset_of_Imperm
    global Initial_Helium_Pressure, Initial_Nitrogen_Pressure
    global Barometric_Pressure

    Gradient_Onset_of_Imperm = Gradient_Onset_of_Imperm_Atm * Units_Factor

    # Root search bounds (time along this linear segment)
    Low_Bound = 0.0
    High_Bound = (Ending_Ambient_Pressure - Starting_Ambient_Pressure) / Rate

    # Build inspired inert pressure model for this segment (OC or CCR).
    # We compute: (inspHe0, inspN20, rateHe, rateN2) for stage 1, and optionally stage 2 if split.
    # Stage 1 always starts at t=0 with Initial_*_Pressure[I-1].
    # If split, stage 1 runs for t1, then stage 2 runs for remaining time.
    ccr_split = False
    t1 = 0.0  # duration of stage 1
    # stage1 parameters
    inspHe1 = 0.0
    inspN21 = 0.0
    rateHe1 = 0.0
    rateN21 = 0.0
    # stage2 parameters (only used if ccr_split)
    inspHe2 = 0.0
    inspN22 = 0.0
    rateHe2 = 0.0
    rateN22 = 0.0
    ccr_mode_start = None

    if _CCR_ACTIVE():
        # In CCR, Mix_Number is the diluent mix (engine already enforces Mix_Number=1 upstream).
        FO2_dil = Fraction_Oxygen[Mix_Number - 1]
        FHe_dil = Fraction_Helium[Mix_Number - 1]
        FN2_dil = Fraction_Nitrogen[Mix_Number - 1]

        P_alv_start = (Starting_Ambient_Pressure - Water_Vapor_Pressure)
        sp_seg_msw = _CCR_GET_SP_MSW(Starting_Ambient_Pressure - Barometric_Pressure, Rate, is_constant_depth=False)
        ppHe_start, ppN2_start, clamp_mode = _CCR_CLAMP_PPO2_AND_INERTS(P_alv_start, FO2_dil, FHe_dil, FN2_dil, sp_seg_msw)
        inspHe1 = ppHe_start
        inspN21 = ppN2_start
        ccr_mode_start = clamp_mode
        rateHe1, rateN21 = _CCR_RATES(Rate, clamp_mode, FHe_dil, FN2_dil, FO2_dil)

        # Optional split if the linear segment crosses the PAMBMAX boundary (P_alv == SP).
        # This mirrors GAS_LOADINGS_ASCENT_DESCENT logic (keeps inert pp physically valid).
        if Rate != 0.0:
            P_alv_end = (Ending_Ambient_Pressure - Water_Vapor_Pressure)
            if (P_alv_start - sp_seg_msw) * (P_alv_end - sp_seg_msw) < 0.0:
                Depth_PAMBMAX = (sp_seg_msw + Water_Vapor_Pressure) - Barometric_Pressure
                Starting_Depth = Starting_Ambient_Pressure - Barometric_Pressure
                Segment_Time = High_Bound
                t1 = (Depth_PAMBMAX - Starting_Depth) / Rate
                if t1 < 0.0:
                    t1 = 0.0
                if t1 > Segment_Time:
                    t1 = Segment_Time
                # Determine stage2 parameters.
                # If we start in PAMBMAX (inerts zero) and go deeper -> stage2 becomes SP (inerts turn on).
                # Else (SP -> PAMBMAX or other), stage2 is PAMBMAX (inerts zero).
                if clamp_mode == "PAMBMAX" and (P_alv_end > sp_seg_msw) and (Segment_Time - t1) > 0.0:
                    denom = (FHe_dil + FN2_dil)
                    if denom > 0.0:
                        inspHe2 = 0.0
                        inspN22 = 0.0
                        rateHe2 = Rate * (FHe_dil / denom)
                        rateN22 = Rate * (FN2_dil / denom)
                    else:
                        inspHe2 = 0.0
                        inspN22 = 0.0
                        rateHe2 = 0.0
                        rateN22 = 0.0
                else:
                    # stage2 inert remains zero
                    inspHe2 = 0.0
                    inspN22 = 0.0
                    rateHe2 = 0.0
                    rateN22 = 0.0
                ccr_split = (Segment_Time - t1) > 0.0
    else:
        # OC: standard inspired inert model
        mh = Fraction_Helium[Mix_Number - 1]
        mn = Fraction_Nitrogen[Mix_Number - 1]
        inspHe1 = (Starting_Ambient_Pressure - Water_Vapor_Pressure) * mh
        inspN21 = (Starting_Ambient_Pressure - Water_Vapor_Pressure) * mn
        rateHe1 = Rate * mh
        rateN21 = Rate * mn

    def _tissue_pressures_at_time(t: float) -> tuple[float, float]:
        """Return (He, N2) tissue pressures at time t along the segment, using the segment's inspired model."""
        if not ccr_split or t <= t1 + 1.0e-12:
            he = SCHREINER_EQUATION(inspHe1, rateHe1, t, Helium_Time_Constant[I - 1], Initial_Helium_Pressure[I - 1])
            n2 = SCHREINER_EQUATION(inspN21, rateN21, t, Nitrogen_Time_Constant[I - 1], Initial_Nitrogen_Pressure[I - 1])
            return he, n2

        # Two-stage integration: evolve to t1 with stage1, then from t1 to t with stage2.
        he_t1 = SCHREINER_EQUATION(inspHe1, rateHe1, t1, Helium_Time_Constant[I - 1], Initial_Helium_Pressure[I - 1])
        n2_t1 = SCHREINER_EQUATION(inspN21, rateN21, t1, Nitrogen_Time_Constant[I - 1], Initial_Nitrogen_Pressure[I - 1])
        dt = t - t1
        he = SCHREINER_EQUATION(inspHe2, rateHe2, dt, Helium_Time_Constant[I - 1], he_t1)
        n2 = SCHREINER_EQUATION(inspN22, rateN22, dt, Nitrogen_Time_Constant[I - 1], n2_t1)
        return he, n2

    # Function at low bound (t=0)
    Starting_Gas_Tension = Initial_Helium_Pressure[I - 1] + Initial_Nitrogen_Pressure[I - 1] + Constant_Pressure_Other_Gases
    Function_at_Low_Bound = Starting_Ambient_Pressure - Starting_Gas_Tension - Gradient_Onset_of_Imperm

    # Function at high bound (t=Segment_Time)
    he_high, n2_high = _tissue_pressures_at_time(High_Bound)
    Ending_Gas_Tension = he_high + n2_high + Constant_Pressure_Other_Gases
    Function_at_High_Bound = Ending_Ambient_Pressure - Ending_Gas_Tension - Gradient_Onset_of_Imperm

    if (Function_at_High_Bound * Function_at_Low_Bound) >= 0.0:
        # Rich diagnostic (no control-flow change besides the message)
        msg = (
            "ERROR! ROOT IS NOT WITHIN BRACKETS (ONSET_OF_IMPERMEABILITY)\n"
            f"I={I} Rate={Rate} Mix_Number={Mix_Number} CCR_ACTIVE={_CCR_ACTIVE()} ccr_split={ccr_split} ccr_mode_start={ccr_mode_start}\n"
            f"StartAmb={Starting_Ambient_Pressure} EndAmb={Ending_Ambient_Pressure} HighBound={High_Bound} t1={t1}\n"
            f"f(low)={Function_at_Low_Bound} f(high)={Function_at_High_Bound} GasT(low)={Starting_Gas_Tension} GasT(high)={Ending_Gas_Tension}"
        )
        raise RuntimeError(msg)

    if Function_at_Low_Bound < 0.0:
        Time = Low_Bound
        Differential_Change = High_Bound - Low_Bound
    else:
        Time = High_Bound
        Differential_Change = Low_Bound - High_Bound

    Mid_Range_Ambient_Pressure = Starting_Ambient_Pressure
    Gas_Tension_at_Mid_Range = Starting_Gas_Tension

    for J in range(1, 201):
        Last_Diff_Change = Differential_Change
        Differential_Change = Last_Diff_Change * 0.5
        Mid_Range_Time = Time + Differential_Change

        Mid_Range_Ambient_Pressure = Starting_Ambient_Pressure + Rate * Mid_Range_Time

        he_mid, n2_mid = _tissue_pressures_at_time(Mid_Range_Time)
        Gas_Tension_at_Mid_Range = he_mid + n2_mid + Constant_Pressure_Other_Gases

        Function_at_Mid_Range = Mid_Range_Ambient_Pressure - Gas_Tension_at_Mid_Range - Gradient_Onset_of_Imperm

        if Function_at_Mid_Range <= 0.0:
            Time = Mid_Range_Time

        if (abs(Differential_Change) < 1.0e-3) or (Function_at_Mid_Range == 0.0):
            break
    else:
        raise RuntimeError("ERROR! ROOT SEARCH EXCEEDED MAXIMUM ITERATIONS (ONSET_OF_IMPERMEABILITY)")

    Amb_Pressure_Onset_of_Imperm[I - 1] = Mid_Range_Ambient_Pressure
    Gas_Tension_Onset_of_Imperm[I - 1] = Gas_Tension_at_Mid_Range
    return



def RADIUS_ROOT_FINDER(A: float, B: float, C: float, Low_Bound: float, High_Bound: float) -> float:
    """FORTRAN SUBROUTINE RADIUS_ROOT_FINDER (1:1). Returns Ending_Radius."""
    Function_at_Low_Bound = Low_Bound * (Low_Bound * (A * Low_Bound - B)) - C
    Function_at_High_Bound = High_Bound * (High_Bound * (A * High_Bound - B)) - C

    if (Function_at_Low_Bound > 0.0) and (Function_at_High_Bound > 0.0):
        raise RuntimeError("ERROR! ROOT IS NOT WITHIN BRACKETS (RADIUS_ROOT_FINDER)")
    if (Function_at_Low_Bound < 0.0) and (Function_at_High_Bound < 0.0):
        raise RuntimeError("ERROR! ROOT IS NOT WITHIN BRACKETS (RADIUS_ROOT_FINDER)")

    if Function_at_Low_Bound == 0.0:
        return REAL(REAL(Low_Bound))
    elif Function_at_High_Bound == 0.0:
        return High_Bound
    elif Function_at_Low_Bound < 0.0:
        Radius_at_Low_Bound = Low_Bound
        Radius_at_High_Bound = High_Bound
    else:
        Radius_at_High_Bound = Low_Bound
        Radius_at_Low_Bound = High_Bound

    Ending_Radius = 0.5 * (Low_Bound + High_Bound)
    Last_Diff_Change = abs(High_Bound - Low_Bound)
    Differential_Change = Last_Diff_Change

    Function = Ending_Radius * (Ending_Radius * (A * Ending_Radius - B)) - C
    Derivative_of_Function = Ending_Radius * (Ending_Radius * 3.0 * A - 2.0 * B)

    for _I in range(1, 101):
        if (
            (((Ending_Radius - Radius_at_High_Bound) * Derivative_of_Function - Function) *
             ((Ending_Radius - Radius_at_Low_Bound) * Derivative_of_Function - Function) >= 0.0)
            or (abs(2.0 * Function) > abs(Last_Diff_Change * Derivative_of_Function))
        ):
            Last_Diff_Change = Differential_Change
            Differential_Change = 0.5 * (Radius_at_High_Bound - Radius_at_Low_Bound)
            Ending_Radius = Radius_at_Low_Bound + Differential_Change
            if Radius_at_Low_Bound == Ending_Radius:
                return Ending_Radius
        else:
            Last_Diff_Change = Differential_Change
            Differential_Change = Function / Derivative_of_Function
            Last_Ending_Radius = Ending_Radius
            Ending_Radius = Ending_Radius - Differential_Change
            if Last_Ending_Radius == Ending_Radius:
                return Ending_Radius

        if abs(Differential_Change) < 1.0e-12:
            return Ending_Radius

        Function = Ending_Radius * (Ending_Radius * (A * Ending_Radius - B)) - C
        Derivative_of_Function = Ending_Radius * (Ending_Radius * 3.0 * A - 2.0 * B)

        if Function < 0.0:
            Radius_at_Low_Bound = Ending_Radius
        else:
            Radius_at_High_Bound = Ending_Radius

    raise RuntimeError("ERROR! ROOT SEARCH EXCEEDED MAXIMUM ITERATIONS (RADIUS_ROOT_FINDER)")


def GAS_LOADINGS_CONSTANT_DEPTH(Depth: float, Run_Time_End_of_Segment: float, Mix_Number_In: int) -> None:
    """FORTRAN SUBROUTINE GAS_LOADINGS_CONSTANT_DEPTH (1:1).

    Mix_Number = Mix_Number_In
    Applies HALDANE_EQUATION to update Helium_Pressure and Nitrogen_Pressure
    for a constant depth segment.
    """
    global Run_Time, Segment_Number, Segment_Time
    global Ending_Ambient_Pressure, Mix_Number, Barometric_Pressure
    global Water_Vapor_Pressure
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Helium_Pressure, Nitrogen_Pressure
    global Fraction_Helium, Fraction_Nitrogen

    Segment_Time = Run_Time_End_of_Segment - Run_Time
    Last_Run_Time = Run_Time_End_of_Segment
    Run_Time = Last_Run_Time
    Last_Segment_Number = Segment_Number
    Segment_Number = Last_Segment_Number + 1

    Ambient_Pressure = Depth + Barometric_Pressure

    mh = Fraction_Helium[Mix_Number - 1]
    mn = Fraction_Nitrogen[Mix_Number - 1]

    if _CCR_ACTIVE():
        # CCR: mix fractions are DILUENT fractions; clamp includes PVAPOR via (Pamb - Water_Vapor_Pressure)
        P_alv = (Ambient_Pressure - Water_Vapor_Pressure)
        FO2_dil = Fraction_Oxygen[Mix_Number - 1]
        FHe_dil = mh
        FN2_dil = mn
        sp_cd_msw = _CCR_GET_SP_MSW(Depth, 0.0, is_constant_depth=True)
        ppHe, ppN2, _mode = _CCR_CLAMP_PPO2_AND_INERTS(P_alv, FO2_dil, FHe_dil, FN2_dil, sp_cd_msw)
        Inspired_Helium_Pressure = REAL(ppHe)
        Inspired_Nitrogen_Pressure = REAL(ppN2)
    else:
        Inspired_Helium_Pressure = REAL((Ambient_Pressure - Water_Vapor_Pressure) * mh)
        Inspired_Nitrogen_Pressure = REAL((Ambient_Pressure - Water_Vapor_Pressure) * mn)

    Ending_Ambient_Pressure = Ambient_Pressure

    # --- PASSIVE DEBUG (effective inspired pp at constant depth) ---
    try:
        _seg_type = DEBUG_CTX_SEG_TYPE if DEBUG_CTX_SEG_TYPE in ("DESC", "ASC", "STOP") else "STOP"
        P_alv_dbg = (Ambient_Pressure - Water_Vapor_Pressure)
        ppHe_dbg = float(Inspired_Helium_Pressure)
        ppN2_dbg = float(Inspired_Nitrogen_Pressure)
        ppO2_dbg = float(max(0.0, P_alv_dbg - ppHe_dbg - ppN2_dbg))
        _debug_profile_append(_seg_type, Depth, Depth, Run_Time, Mix_Number,
                             ppO2_dbg, ppN2_dbg, ppHe_dbg,
                             bool(_CCR_ACTIVE()), bool(BO_EFFECTIVE))
    except Exception:
        pass

    for i in range(16):
        Initial_He = Helium_Pressure[i]
        Initial_N2 = Nitrogen_Pressure[i]

        Helium_Pressure[i] = HALDANE_EQUATION(
            Initial_He,
            Inspired_Helium_Pressure,
            Helium_Time_Constant[i],
            Segment_Time,
        )

        Nitrogen_Pressure[i] = HALDANE_EQUATION(
            Initial_N2,
            Inspired_Nitrogen_Pressure,
            Nitrogen_Time_Constant[i],
            Segment_Time,
        )
    return


def NUCLEAR_REGENERATION(Dive_Time):
    """FORTRAN SUBROUTINE NUCLEAR_REGENERATION (Dive_Time) - 1:1 port.
    Fortran I/O (write to unit 8) is disabled/no-op by contract.
    """
    global Surface_Tension_Gamma, Skin_Compression_GammaC, rapsol1, rapsol2
    global Regeneration_Time_Constant
    global Units_Factor
    global Adjusted_Critical_Radius_He, Adjusted_Critical_Radius_N2
    global Max_Crushing_Pressure_He, Max_Crushing_Pressure_N2
    global Regenerated_Radius_He, Regenerated_Radius_N2
    global Adjusted_Crushing_Pressure_He, Adjusted_Crushing_Pressure_N2

    # DO I = 1,16
    for I in range(1, 17):
        i = I - 1

        Crushing_Pressure_Pascals_He = (Max_Crushing_Pressure_He[i] / Units_Factor) * 101325.0
        Crushing_Pressure_Pascals_N2 = (Max_Crushing_Pressure_N2[i] / Units_Factor) * 101325.0

        Ending_Radius_He = 1.0 / (
            Crushing_Pressure_Pascals_He / (2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma))
            + 1.0 / Adjusted_Critical_Radius_He[i]
        )

        Ending_Radius_N2 = 1.0 / (
            Crushing_Pressure_Pascals_N2 / (2.0 * (Skin_Compression_GammaC - Surface_Tension_Gamma))
            + 1.0 / Adjusted_Critical_Radius_N2[i]
        )

        Regenerated_Radius_He[i] = Adjusted_Critical_Radius_He[i] + (
            (Ending_Radius_He - Adjusted_Critical_Radius_He[i]) * math.exp(-Dive_Time / Regeneration_Time_Constant)
        )

        Regenerated_Radius_N2[i] = Adjusted_Critical_Radius_N2[i] + (
            (Ending_Radius_N2 - Adjusted_Critical_Radius_N2[i]) * math.exp(-Dive_Time / Regeneration_Time_Constant)
        )

        Crush_Pressure_Adjust_Ratio_He = (
            Ending_Radius_He * (Adjusted_Critical_Radius_He[i] - Regenerated_Radius_He[i])
        ) / (
            Regenerated_Radius_He[i] * (Adjusted_Critical_Radius_He[i] - Ending_Radius_He)
        )

        Crush_Pressure_Adjust_Ratio_N2 = (
            Ending_Radius_N2 * (Adjusted_Critical_Radius_N2[i] - Regenerated_Radius_N2[i])
        ) / (
            Regenerated_Radius_N2[i] * (Adjusted_Critical_Radius_N2[i] - Ending_Radius_N2)
        )

        Adj_Crush_Pressure_He_Pascals = Crushing_Pressure_Pascals_He * Crush_Pressure_Adjust_Ratio_He
        Adj_Crush_Pressure_N2_Pascals = Crushing_Pressure_Pascals_N2 * Crush_Pressure_Adjust_Ratio_N2

        Adjusted_Crushing_Pressure_He[i] = (Adj_Crush_Pressure_He_Pascals / 101325.0) * Units_Factor
        Adjusted_Crushing_Pressure_N2[i] = (Adj_Crush_Pressure_N2_Pascals / 101325.0) * Units_Factor

def CALC_INITIAL_ALLOWABLE_GRADIENT() -> None:
    """FORTRAN SUBROUTINE CALC_INITIAL_ALLOWABLE_GRADIENT - 1:1 port.

    Computes initial allowable gradients (He/N2) from regenerated radii, in Pa then units.
    Also copies them into Allowable_Gradient_* arrays.
    """
    global Surface_Tension_Gamma, Skin_Compression_GammaC, rapsol1, rapsol2
    global Units_Factor
    global Regenerated_Radius_He, Regenerated_Radius_N2
    global Allowable_Gradient_He, Allowable_Gradient_N2
    global Initial_Allowable_Gradient_He, Initial_Allowable_Gradient_N2

    for I in range(1, 17):
        i = I - 1

        Initial_Allowable_Grad_N2_Pa = (
            (2.0 * Surface_Tension_Gamma * (Skin_Compression_GammaC - Surface_Tension_Gamma))
            / (Regenerated_Radius_N2[i] * Skin_Compression_GammaC)
        )

        Initial_Allowable_Grad_He_Pa = (
            (2.0 * Surface_Tension_Gamma * (Skin_Compression_GammaC - Surface_Tension_Gamma))
            / (Regenerated_Radius_He[i] * Skin_Compression_GammaC)
        ) * rapsol1

        Initial_Allowable_Gradient_N2[i] = (Initial_Allowable_Grad_N2_Pa / 101325.0) * Units_Factor
        Initial_Allowable_Gradient_He[i] = (Initial_Allowable_Grad_He_Pa / 101325.0) * Units_Factor

        Allowable_Gradient_He[i] = Initial_Allowable_Gradient_He[i]
        Allowable_Gradient_N2[i] = Initial_Allowable_Gradient_N2[i]

    return


def CALC_ASCENT_CEILING() -> float:
    """FORTRAN SUBROUTINE CALC_ASCENT_CEILING - 1:1 port.

    Returns the maximum compartment ascent ceiling depth (pressure units), i.e., deepest ceiling.
    """
    global Constant_Pressure_Other_Gases
    global Barometric_Pressure
    global Helium_Pressure, Nitrogen_Pressure
    global Allowable_Gradient_He, Allowable_Gradient_N2

    Compartment_Ascent_Ceiling = [0.0] * 16

    for I in range(1, 17):
        i = I - 1
        Gas_Loading = Helium_Pressure[i] + Nitrogen_Pressure[i]

        if Gas_Loading > 0.0:
            Weighted_Allowable_Gradient = (
                Allowable_Gradient_He[i] * Helium_Pressure[i]
                + Allowable_Gradient_N2[i] * Nitrogen_Pressure[i]
            ) / (Helium_Pressure[i] + Nitrogen_Pressure[i])

            Tolerated_Ambient_Pressure = (Gas_Loading + Constant_Pressure_Other_Gases) - Weighted_Allowable_Gradient
        else:
            Weighted_Allowable_Gradient = min(Allowable_Gradient_He[i], Allowable_Gradient_N2[i])
            Tolerated_Ambient_Pressure = Constant_Pressure_Other_Gases - Weighted_Allowable_Gradient

        if Tolerated_Ambient_Pressure < 0.0:
            Tolerated_Ambient_Pressure = 0.0

        Compartment_Ascent_Ceiling[i] = Tolerated_Ambient_Pressure - Barometric_Pressure

    Ascent_Ceiling_Depth = Compartment_Ascent_Ceiling[0]
    for I in range(2, 17):
        i = I - 1
        Ascent_Ceiling_Depth = max(Ascent_Ceiling_Depth, Compartment_Ascent_Ceiling[i])

    return Ascent_Ceiling_Depth


def CALC_MAX_ACTUAL_GRADIENT(Deco_Stop_Depth: float) -> None:
    """FORTRAN SUBROUTINE CALC_MAX_ACTUAL_GRADIENT (1:1)."""
    global Constant_Pressure_Other_Gases
    global Barometric_Pressure
    global Helium_Pressure, Nitrogen_Pressure
    global Max_Actual_Gradient

    for I in range(16):
        Compartment_Gradient = (Helium_Pressure[I] + Nitrogen_Pressure[I] + Constant_Pressure_Other_Gases) - (Deco_Stop_Depth + Barometric_Pressure)
        if Compartment_Gradient <= 0.0:
            Compartment_Gradient = 0.0
        if Max_Actual_Gradient[I] <= Compartment_Gradient:
            Max_Actual_Gradient[I] = Compartment_Gradient
    return


def CALC_SURFACE_PHASE_VOLUME_TIME():
    """FORTRAN SUBROUTINE CALC_SURFACE_PHASE_VOLUME_TIME - 1:1 port."""
    global Water_Vapor_Pressure
    global Barometric_Pressure
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Helium_Pressure, Nitrogen_Pressure
    global Surface_Phase_Volume_Time

    Surface_Inspired_N2_Pressure = (Barometric_Pressure - Water_Vapor_Pressure) * 0.79

    for I in range(1, 17):
        i = I - 1

        if Nitrogen_Pressure[i] > Surface_Inspired_N2_Pressure:
            Surface_Phase_Volume_Time[i] = (
                Helium_Pressure[i] / Helium_Time_Constant[i]
                + (Nitrogen_Pressure[i] - Surface_Inspired_N2_Pressure) / Nitrogen_Time_Constant[i]
            ) / (Helium_Pressure[i] + Nitrogen_Pressure[i] - Surface_Inspired_N2_Pressure)

        elif (Nitrogen_Pressure[i] <= Surface_Inspired_N2_Pressure) and (
            (Helium_Pressure[i] + Nitrogen_Pressure[i]) >= Surface_Inspired_N2_Pressure
        ):
            Decay_Time_to_Zero_Gradient = (
                1.0 / (Nitrogen_Time_Constant[i] - Helium_Time_Constant[i])
                * math.log((Surface_Inspired_N2_Pressure - Nitrogen_Pressure[i]) / Helium_Pressure[i])
            )

            Integral_Gradient_x_Time = (
                Helium_Pressure[i]
                / Helium_Time_Constant[i]
                * (1.0 - math.exp(-Helium_Time_Constant[i] * Decay_Time_to_Zero_Gradient))
                + (Nitrogen_Pressure[i] - Surface_Inspired_N2_Pressure)
                / Nitrogen_Time_Constant[i]
                * (1.0 - math.exp(-Nitrogen_Time_Constant[i] * Decay_Time_to_Zero_Gradient))
            )

            Surface_Phase_Volume_Time[i] = Integral_Gradient_x_Time / (
                Helium_Pressure[i] + Nitrogen_Pressure[i] - Surface_Inspired_N2_Pressure
            )

        else:
            Surface_Phase_Volume_Time[i] = 0.0

def CRITICAL_VOLUME(Deco_Phase_Volume_Time: float) -> None:
    """FORTRAN SUBROUTINE CRITICAL_VOLUME (1:1 port).

    Applies the VPM Critical Volume Algorithm to compute relaxed allowable gradients.
    Fortran I/O to unit 8 is disabled/no-op by contract.
    """
    global Surface_Tension_Gamma, Skin_Compression_GammaC, rapsol1, rapsol2
    global Crit_Volume_Parameter_Lambda
    global Units_Factor
    global Adjusted_Critical_Radius_He, Adjusted_Critical_Radius_N2
    global Surface_Phase_Volume_Time
    global Adjusted_Crushing_Pressure_He, Adjusted_Crushing_Pressure_N2
    global Allowable_Gradient_He, Allowable_Gradient_N2
    global Initial_Allowable_Gradient_He, Initial_Allowable_Gradient_N2

    Phase_Volume_Time_local = [0.0]*16

    Lambda_Pascals_Parameter = (Crit_Volume_Parameter_Lambda/33.0) * 101325.0

    for I in range(1, 17):
        i = I - 1
        Phase_Volume_Time_local[i] = Deco_Phase_Volume_Time + Surface_Phase_Volume_Time[i]

    for I in range(1, 17):
        i = I - 1
        Adj_Crush_Pressure_He_Pascals = (Adjusted_Crushing_Pressure_He[i]/Units_Factor) * 101325.0
        Initial_Allowable_Grad_He_Pa = (Initial_Allowable_Gradient_He[i]/Units_Factor) * 101325.0

        B = Initial_Allowable_Grad_He_Pa + (Lambda_Pascals_Parameter*Surface_Tension_Gamma)/(
            Skin_Compression_GammaC*Phase_Volume_Time_local[i]
        )
        C = (Surface_Tension_Gamma*(Surface_Tension_Gamma*(Lambda_Pascals_Parameter*Adj_Crush_Pressure_He_Pascals)))/(
            Skin_Compression_GammaC*(Skin_Compression_GammaC*Phase_Volume_Time_local[i])
        )

        New_Allowable_Grad_He_Pascals = (B + math.sqrt(B**2 - 4.0*C))/2.0
        Allowable_Gradient_He[i] = (New_Allowable_Grad_He_Pascals/101325.0) * Units_Factor

    for I in range(1, 17):
        i = I - 1
        Adj_Crush_Pressure_N2_Pascals = (Adjusted_Crushing_Pressure_N2[i]/Units_Factor) * 101325.0
        Initial_Allowable_Grad_N2_Pa = (Initial_Allowable_Gradient_N2[i]/Units_Factor) * 101325.0

        B = Initial_Allowable_Grad_N2_Pa + (Lambda_Pascals_Parameter*Surface_Tension_Gamma)/(
            Skin_Compression_GammaC*Phase_Volume_Time_local[i]
        )
        C = (Surface_Tension_Gamma*(Surface_Tension_Gamma*(Lambda_Pascals_Parameter*Adj_Crush_Pressure_N2_Pascals)))/(
            Skin_Compression_GammaC*(Skin_Compression_GammaC*Phase_Volume_Time_local[i])
        )

        New_Allowable_Grad_N2_Pascals = (B + math.sqrt(B**2 - 4.0*C))/2.0
        Allowable_Gradient_N2[i] = (New_Allowable_Grad_N2_Pascals/101325.0) * Units_Factor

    return


def CALC_START_OF_DECO_ZONE(Starting_Depth: float, Rate: float) -> float:
    """FORTRAN SUBROUTINE CALC_START_OF_DECO_ZONE (1:1).

    Returns Depth_Start_of_Deco_Zone.
    """
    global Water_Vapor_Pressure, Constant_Pressure_Other_Gases
    global Mix_Number, Barometric_Pressure
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Helium_Pressure, Nitrogen_Pressure
    global Fraction_Helium, Fraction_Nitrogen

    # Starting ambient pressure at beginning of ascent
    Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure

    # Inspired gas pressures at starting ambient pressure
    if _CCR_ACTIVE():
        # CCR (Waite-style for start-of-deco-zone ascent search):
        # Use clamp regime at start; in SP regime, treat inert inspired pressures as a fixed fraction
        # of alveolar pressure, with fraction computed from Pamb(start).
        P_alv_start = (Starting_Ambient_Pressure - Water_Vapor_Pressure)
        P_ref_start = Starting_Ambient_Pressure
        FO2_dil = Fraction_Oxygen[Mix_Number - 1]
        FHe_dil = Fraction_Helium[Mix_Number - 1]
        FN2_dil = Fraction_Nitrogen[Mix_Number - 1]
        sp_csdz_msw = _CCR_GET_SP_MSW(Starting_Depth, Rate, is_constant_depth=False)

        ppHe_start, ppN2_start, clamp_mode = _CCR_CLAMP_PPO2_AND_INERTS(P_alv_start, FO2_dil, FHe_dil, FN2_dil, sp_csdz_msw)

        if clamp_mode == "SP":
            denom = FHe_dil + FN2_dil
            if denom <= 0.0 or P_ref_start <= 0.0:
                Initial_Inspired_He_Pressure = 0.0
                Initial_Inspired_N2_Pressure = 0.0
                Helium_Rate = 0.0
                Nitrogen_Rate = 0.0
            else:
                F_inert_eff = 1.0 - (sp_csdz_msw / P_ref_start)
                if F_inert_eff < 0.0:
                    F_inert_eff = 0.0
                Initial_Inspired_He_Pressure = P_alv_start * F_inert_eff * (FHe_dil / denom)
                Initial_Inspired_N2_Pressure = P_alv_start * F_inert_eff * (FN2_dil / denom)
                Helium_Rate = Rate * F_inert_eff * (FHe_dil / denom)
                Nitrogen_Rate = Rate * F_inert_eff * (FN2_dil / denom)
        else:
            Initial_Inspired_He_Pressure = ppHe_start
            Initial_Inspired_N2_Pressure = ppN2_start
            Helium_Rate, Nitrogen_Rate = _CCR_RATES(Rate, clamp_mode, FHe_dil, FN2_dil, FO2_dil)
    else:
        Initial_Inspired_He_Pressure = (
            Starting_Ambient_Pressure - Water_Vapor_Pressure
        ) * Fraction_Helium[Mix_Number - 1]
        Initial_Inspired_N2_Pressure = (
            Starting_Ambient_Pressure - Water_Vapor_Pressure
        ) * Fraction_Nitrogen[Mix_Number - 1]

        # Rates of change of inspired gas pressures (Rate < 0 in ascent)
        Helium_Rate = Rate * Fraction_Helium[Mix_Number - 1]
        Nitrogen_Rate = Rate * Fraction_Nitrogen[Mix_Number - 1]

    # Fortran bounds: 0 and -Starting_Ambient_Pressure / Rate
    Low_Bound = 0.0
    High_Bound = -1.0 * (Starting_Ambient_Pressure / Rate)
    # CCR clamp for SoDZ search: if the ascent crosses PAMBMAX (P_alv == SP),
    # evaluate Schreiner in two stages to avoid negative inert pressures at shallow ambient pressure.
    soz_split_pambmax = False
    soz_tsplit = 0.0
    if _CCR_ACTIVE() and Rate != 0.0 and clamp_mode == 'SP':
        P_alv_start = (Starting_Ambient_Pressure - Water_Vapor_Pressure)
        P_alv_high = ((Starting_Ambient_Pressure + Rate * High_Bound) - Water_Vapor_Pressure)
        if (P_alv_start - sp_csdz_msw) * (P_alv_high - sp_csdz_msw) < 0.0:
            Depth_PAMBMAX = (sp_csdz_msw + Water_Vapor_Pressure) - Barometric_Pressure
            soz_tsplit = (Depth_PAMBMAX - Starting_Depth) / Rate
            if soz_tsplit < 0.0:
                soz_tsplit = 0.0
            if soz_tsplit > High_Bound:
                soz_tsplit = High_Bound
            soz_split_pambmax = ((High_Bound - soz_tsplit) > 0.0)

    Depth_Start_of_Deco_Zone = 0.0

    # Loop over the 16 tissue compartments
    for I in range(1, 17):
        # Local starting gas tensions for compartment I
        Initial_Helium_Pressure = Helium_Pressure[I - 1]
        Initial_Nitrogen_Pressure = Nitrogen_Pressure[I - 1]

        # Function value at Low_Bound (time = 0)
        Function_at_Low_Bound = (
            Initial_Helium_Pressure
            + Initial_Nitrogen_Pressure
            + Constant_Pressure_Other_Gases
            - Starting_Ambient_Pressure
        )

        # Gas tensions at High_Bound using Schreiner
        if _CCR_ACTIVE() and soz_split_pambmax and High_Bound > soz_tsplit:
            he_mid = SCHREINER_EQUATION(
                Initial_Inspired_He_Pressure,
                Helium_Rate,
                soz_tsplit,
                Helium_Time_Constant[I - 1],
                Initial_Helium_Pressure,
            )
            n2_mid = SCHREINER_EQUATION(
                Initial_Inspired_N2_Pressure,
                Nitrogen_Rate,
                soz_tsplit,
                Nitrogen_Time_Constant[I - 1],
                Initial_Nitrogen_Pressure,
            )
            High_Bound_Helium_Pressure = SCHREINER_EQUATION(
                0.0,
                0.0,
                (High_Bound - soz_tsplit),
                Helium_Time_Constant[I - 1],
                he_mid,
            )
            High_Bound_Nitrogen_Pressure = SCHREINER_EQUATION(
                0.0,
                0.0,
                (High_Bound - soz_tsplit),
                Nitrogen_Time_Constant[I - 1],
                n2_mid,
            )
        else:
            High_Bound_Helium_Pressure = SCHREINER_EQUATION(
                Initial_Inspired_He_Pressure,
                Helium_Rate,
                High_Bound,
                Helium_Time_Constant[I - 1],
                Initial_Helium_Pressure,
            )
            High_Bound_Nitrogen_Pressure = SCHREINER_EQUATION(
                Initial_Inspired_N2_Pressure,
                Nitrogen_Rate,
                High_Bound,
                Nitrogen_Time_Constant[I - 1],
                Initial_Nitrogen_Pressure,
            )

        Function_at_High_Bound = (
            High_Bound_Helium_Pressure
            + High_Bound_Nitrogen_Pressure
            + Constant_Pressure_Other_Gases
            - (Starting_Ambient_Pressure + Rate * High_Bound)
        )

        # Root must be bracketed between Low_Bound and High_Bound (Fortran semantics)
        if Function_at_High_Bound * Function_at_Low_Bound >= 0.0:
            raise RuntimeError(
                "ERROR! ROOT IS NOT WITHIN BRACKETS (CALC_START_OF_DECO_ZONE)"
            )

        # Initial guess and step size exactly as in Fortran
        if Function_at_Low_Bound < 0.0:
            Time_to_Start_of_Deco_Zone = Low_Bound
            Differential_Change = High_Bound - Low_Bound
        else:
            Time_to_Start_of_Deco_Zone = High_Bound
            Differential_Change = Low_Bound - High_Bound

        # Iterative search (1..300), duplicating Fortran's control flow
        for J in range(1, 301):
            Last_Diff_Change = Differential_Change
            Differential_Change = Last_Diff_Change * 0.5

            Mid_Range_Time = Time_to_Start_of_Deco_Zone + Differential_Change

            if _CCR_ACTIVE() and soz_split_pambmax and Mid_Range_Time > soz_tsplit:
                he_mid = SCHREINER_EQUATION(
                    Initial_Inspired_He_Pressure,
                    Helium_Rate,
                    soz_tsplit,
                    Helium_Time_Constant[I - 1],
                    Initial_Helium_Pressure,
                )
                n2_mid = SCHREINER_EQUATION(
                    Initial_Inspired_N2_Pressure,
                    Nitrogen_Rate,
                    soz_tsplit,
                    Nitrogen_Time_Constant[I - 1],
                    Initial_Nitrogen_Pressure,
                )
                Mid_Range_Helium_Pressure = SCHREINER_EQUATION(
                    0.0,
                    0.0,
                    (Mid_Range_Time - soz_tsplit),
                    Helium_Time_Constant[I - 1],
                    he_mid,
                )
                Mid_Range_Nitrogen_Pressure = SCHREINER_EQUATION(
                    0.0,
                    0.0,
                    (Mid_Range_Time - soz_tsplit),
                    Nitrogen_Time_Constant[I - 1],
                    n2_mid,
                )
            else:
                Mid_Range_Helium_Pressure = SCHREINER_EQUATION(
                    Initial_Inspired_He_Pressure,
                    Helium_Rate,
                    Mid_Range_Time,
                    Helium_Time_Constant[I - 1],
                    Initial_Helium_Pressure,
                )
                Mid_Range_Nitrogen_Pressure = SCHREINER_EQUATION(
                    Initial_Inspired_N2_Pressure,
                    Nitrogen_Rate,
                    Mid_Range_Time,
                    Nitrogen_Time_Constant[I - 1],
                    Initial_Nitrogen_Pressure,
                )

            Function_at_Mid_Range = (
                Mid_Range_Helium_Pressure
                + Mid_Range_Nitrogen_Pressure
                + Constant_Pressure_Other_Gases
                - (Starting_Ambient_Pressure + Rate * Mid_Range_Time)
            )

            # Come in Fortran: aggiorna solo quando f(mid) <= 0
            if Function_at_Mid_Range <= 0.0:
                Time_to_Start_of_Deco_Zone = Mid_Range_Time

            # Convergenza: passo piccolo o zero esatto
            if (abs(Differential_Change) < 1.0e-3) or (
                Function_at_Mid_Range == 0.0
            ):
                break
        else:
            # Equivalente al "fall-through" oltre 300 iterazioni
            raise RuntimeError(
                "ERROR! ROOT SEARCH EXCEEDED MAXIMUM ITERATIONS (CALC_START_OF_DECO_ZONE)"
            )

        Cpt_Depth_Start_of_Deco_Zone = (
            Starting_Ambient_Pressure + Rate * Time_to_Start_of_Deco_Zone
        ) - Barometric_Pressure

        if Cpt_Depth_Start_of_Deco_Zone > Depth_Start_of_Deco_Zone:
            Depth_Start_of_Deco_Zone = Cpt_Depth_Start_of_Deco_Zone

    return Depth_Start_of_Deco_Zone

def write_debug_input_calc_start_decozone_csv(filename: str, starting_depth: float, rate: float) -> None:
    """Debug helper: dump inputs to CALC_START_OF_DECO_ZONE for 16 tissues.

    Pure output: does not alter any decompression state.
    """
    try:
        mix_idx = Mix_Number - 1
        header0 = [
            "Starting_Depth", starting_depth,
            "Rate", rate,
            "Mix_Number", Mix_Number,
            "Barometric_Pressure", Barometric_Pressure,
            "Water_Vapor_Pressure", Water_Vapor_Pressure,
            "Constant_Pressure_Other_Gases", Constant_Pressure_Other_Gases,
            "Fraction_Helium", Fraction_Helium[mix_idx],
            "Fraction_Nitrogen", Fraction_Nitrogen[mix_idx],
        ]
    except Exception:
        # If globals are not yet set, do nothing
        return

    header1 = [
        "i",
        "Helium_Pressure(I)",
        "Nitrogen_Pressure(I)",
        "Helium_Time_Constant(I)",
        "Nitrogen_Time_Constant(I)",
    ]

    try:
        with open(filename, "w", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["PY", "INPUT CALC_START_OF_DECO_ZONE"])
            w.writerow(header0)
            w.writerow(header1)
            for i in range(16):
                w.writerow([
                    i + 1,
                    f"{Helium_Pressure[i]:.6f}",
                    f"{Nitrogen_Pressure[i]:.6f}",
                    f"{Helium_Time_Constant[i]:.6f}",
                    f"{Nitrogen_Time_Constant[i]:.6f}",
                ])
    except Exception:
        # Debug must never interfere with the algorithm
        pass


def PROJECTED_ASCENT(Starting_Depth: float, Rate: float, Deco_Stop_Depth: float, Step_Size: float) -> float:
    """FORTRAN SUBROUTINE PROJECTED_ASCENT (1:1).

    Returns (possibly adjusted) Deco_Stop_Depth.
    """
    global Water_Vapor_Pressure, Constant_Pressure_Other_Gases
    global Mix_Number, Barometric_Pressure
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Helium_Pressure, Nitrogen_Pressure
    global Fraction_Helium, Fraction_Nitrogen
    global Allowable_Gradient_He, Allowable_Gradient_N2

    New_Ambient_Pressure = Deco_Stop_Depth + Barometric_Pressure
    Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure

    Initial_Inspired_He_Pressure = (Starting_Ambient_Pressure - Water_Vapor_Pressure) * Fraction_Helium[Mix_Number-1]
    Initial_Inspired_N2_Pressure = (Starting_Ambient_Pressure - Water_Vapor_Pressure) * Fraction_Nitrogen[Mix_Number-1]

    Helium_Rate = Rate * Fraction_Helium[Mix_Number-1]
    Nitrogen_Rate = Rate * Fraction_Nitrogen[Mix_Number-1]

    Initial_Helium_Pressure = Helium_Pressure[:]
    Initial_Nitrogen_Pressure = Nitrogen_Pressure[:]

    Temp_Gas_Loading = [0.0]*16
    Allowable_Gas_Loading = [0.0]*16

    while True:   # label 665 loop
        Ending_Ambient_Pressure = New_Ambient_Pressure
        Segment_Time = (Ending_Ambient_Pressure - Starting_Ambient_Pressure) / Rate

        for I in range(16):  # 670
            Temp_Helium_Pressure = SCHREINER_EQUATION(
                Initial_Inspired_He_Pressure, Helium_Rate, Segment_Time,
                Helium_Time_Constant[I], Initial_Helium_Pressure[I]
            )
            Temp_Nitrogen_Pressure = SCHREINER_EQUATION(
                Initial_Inspired_N2_Pressure, Nitrogen_Rate, Segment_Time,
                Nitrogen_Time_Constant[I], Initial_Nitrogen_Pressure[I]
            )
            Temp_Gas_Loading[I] = Temp_Helium_Pressure + Temp_Nitrogen_Pressure

            if Temp_Gas_Loading[I] > 0.0:
                Weighted_Allowable_Gradient = (
                    (Allowable_Gradient_He[I]*Temp_Helium_Pressure + Allowable_Gradient_N2[I]*Temp_Nitrogen_Pressure) /
                    Temp_Gas_Loading[I]
                )
            else:
                Weighted_Allowable_Gradient = min(Allowable_Gradient_He[I], Allowable_Gradient_N2[I])

            Allowable_Gas_Loading[I] = Ending_Ambient_Pressure + Weighted_Allowable_Gradient - Constant_Pressure_Other_Gases

            if DEBUG_PROJECTED_ASCENT:
                print(
                    f"DEBUG_PROJECTED_ASCENT_FULL depth={Deco_Stop_Depth:.1f} i={I+1} "
                    f"TempHe={Temp_Helium_Pressure:.9f} TempN2={Temp_Nitrogen_Pressure:.9f} "
                    f"Temp={Temp_Gas_Loading[I]:.9f} Allow={Allowable_Gas_Loading[I]:.9f} "
                    f"Pamb_end={Ending_Ambient_Pressure:.9f}"
                )


        if DEBUG_PROJECTED_ASCENT and (int(round(Deco_Stop_Depth)) in (18, 21)):
            # Report the tightest compartment at this candidate stop depth
            max_margin = -1.0e30
            max_i = 0
            for _i in range(16):
                _margin = Temp_Gas_Loading[_i] - Allowable_Gas_Loading[_i]
                if _margin > max_margin:
                    max_margin = _margin
                    max_i = _i
            print(
                f"DEBUG_PROJECTED_ASCENT candidate_depth={Deco_Stop_Depth:.1f} "
                f"max_diff={max_margin:.9e} i={max_i+1} "
                f"Temp={Temp_Gas_Loading[max_i]:.9f} Allow={Allowable_Gas_Loading[max_i]:.9f}"
            )

        violated = False
        for I in range(16):  # 671
            if Temp_Gas_Loading[I] > Allowable_Gas_Loading[I]:
                New_Ambient_Pressure = Ending_Ambient_Pressure + Step_Size
                Deco_Stop_Depth = Deco_Stop_Depth + Step_Size
                violated = True
                if DEBUG_PROJECTED_ASCENT and (int(round(Deco_Stop_Depth)) in (18, 21)):
                    print(f"DEBUG_PROJECTED_ASCENT violate_depth={Deco_Stop_Depth:.1f} i={I+1} Temp={Temp_Gas_Loading[I]:.9f} Allow={Allowable_Gas_Loading[I]:.9f} diff={(Temp_Gas_Loading[I]-Allowable_Gas_Loading[I]):.9e}")
                break
        if not violated:
            break

    return Deco_Stop_Depth


def BOYLES_LAW_COMPENSATION(First_Stop_Depth: float, Deco_Stop_Depth: float, Step_Size: float) -> None:
    """FORTRAN SUBROUTINE BOYLES_LAW_COMPENSATION (1:1)."""
    global Surface_Tension_Gamma, Skin_Compression_GammaC, rapsol1, rapsol2
    global Barometric_Pressure, Units_Factor
    global Allowable_Gradient_He, Allowable_Gradient_N2
    global Deco_Gradient_He, Deco_Gradient_N2

    Next_Stop = Deco_Stop_Depth - Step_Size

    Ambient_Pressure_First_Stop = First_Stop_Depth + Barometric_Pressure
    Ambient_Pressure_Next_Stop = Next_Stop + Barometric_Pressure

    Amb_Press_First_Stop_Pascals = (Ambient_Pressure_First_Stop/Units_Factor) * 101325.0
    Amb_Press_Next_Stop_Pascals = (Ambient_Pressure_Next_Stop/Units_Factor) * 101325.0

    # N2
    for I in range(16):
        Allow_Grad_First_Stop_N2_Pa = (Allowable_Gradient_N2[I]/Units_Factor) * 101325.0
        Radius_First_Stop_N2 = (2.0 * Surface_Tension_Gamma) / Allow_Grad_First_Stop_N2_Pa

        A = Amb_Press_Next_Stop_Pascals
        B = -2.0 * Surface_Tension_Gamma
        C = (Amb_Press_First_Stop_Pascals + (2.0*Surface_Tension_Gamma)/Radius_First_Stop_N2) * Radius_First_Stop_N2 * (Radius_First_Stop_N2*(Radius_First_Stop_N2))

        D = (B**3 + 27.0/2.0*A**2*C + 3.0/2.0*math.sqrt(3.0)*A*math.sqrt(_CLAMP_NONNEG(4.0*B**3*C + 27.0*A**2*C**2)))**(1.0/3.0)
        Ending_Radius = (1.0/3.0) * (B/A + B**2/(A*D) + D/A)

        Deco_Gradient_Pascals = (2.0 * Surface_Tension_Gamma) / Ending_Radius
        Deco_Gradient_N2[I] = (Deco_Gradient_Pascals/101325.0) * Units_Factor

    # He
    for I in range(16):
        Allow_Grad_First_Stop_He_Pa = (Allowable_Gradient_He[I]/Units_Factor) * 101325.0
        Radius_First_Stop_He = (2.0 * Surface_Tension_Gamma) / Allow_Grad_First_Stop_He_Pa

        A = Amb_Press_Next_Stop_Pascals
        B = -2.0 * Surface_Tension_Gamma
        C = (Amb_Press_First_Stop_Pascals + (2.0*Surface_Tension_Gamma)/Radius_First_Stop_He) * Radius_First_Stop_He * (Radius_First_Stop_He*(Radius_First_Stop_He))

        D = (B**3 + 27.0/2.0*A**2*C + 3.0/2.0*math.sqrt(3.0)*A*math.sqrt(_CLAMP_NONNEG(4.0*B**3*C + 27.0*A**2*C**2)))**(1.0/3.0)
        Ending_Radius = (1.0/3.0) * (B/A + B**2/(A*D) + D/A)

        Deco_Gradient_Pascals = (2.0 * Surface_Tension_Gamma) / Ending_Radius
        Deco_Gradient_He[I] = (Deco_Gradient_Pascals/101325.0) * Units_Factor

    return


def _FORTRAN_ANINT(x: float) -> float:
    """Fortran ANINT approximation."""
    if x >= 0.0:
        return float(int(x + 0.5))
    else:
        return -float(int((-x) + 0.5))


def ANINT(x: float) -> float:
    """Fortran ANINT wrapper (kept for 1:1 call sites)."""
    return _FORTRAN_ANINT(x)


def DECOMPRESSION_STOP(Deco_Stop_Depth: float, Step_Size: float) -> None:
    """FORTRAN SUBROUTINE DECOMPRESSION_STOP - 1:1 port.

    Updates Run_Time/Segment_Time and tissue pressures for the stop so that the next stop is allowed.
    """
    global Water_Vapor_Pressure, Constant_Pressure_Other_Gases, Minimum_Deco_Stop_Time
    global Run_Time, Segment_Number, Segment_Time
    global Ending_Ambient_Pressure
    global Mix_Number, Barometric_Pressure
    global Helium_Time_Constant, Nitrogen_Time_Constant
    global Helium_Pressure, Nitrogen_Pressure
    global Fraction_Helium, Fraction_Nitrogen
    global Deco_Gradient_He, Deco_Gradient_N2

    # OS_Command = 'CLS' -> no-op

    Last_Run_Time = Run_Time
    Round_Up_Operation = _FORTRAN_ANINT((Last_Run_Time / Minimum_Deco_Stop_Time) + 0.5) * Minimum_Deco_Stop_Time
    Segment_Time = Round_Up_Operation - Run_Time
    Run_Time = Round_Up_Operation

    Temp_Segment_Time = Segment_Time
    Last_Segment_Number = Segment_Number
    Segment_Number = Last_Segment_Number + 1

    Ambient_Pressure = Deco_Stop_Depth + Barometric_Pressure
    Ending_Ambient_Pressure = Ambient_Pressure
    Next_Stop = Deco_Stop_Depth - Step_Size

    mh = Fraction_Helium[Mix_Number-1]
    mn = Fraction_Nitrogen[Mix_Number-1]

    if _CCR_ACTIVE():
        # CCR: mix fractions are DILUENT fractions; clamp includes PVAPOR via (Pamb - Water_Vapor_Pressure)
        P_alv = (Ambient_Pressure - Water_Vapor_Pressure)
        FO2_dil = Fraction_Oxygen[Mix_Number - 1]
        FHe_dil = mh
        FN2_dil = mn
        sp_stop_msw = _CCR_GET_SP_MSW(Deco_Stop_Depth, 0.0, is_constant_depth=True)
        ppHe, ppN2, _mode = _CCR_CLAMP_PPO2_AND_INERTS(P_alv, FO2_dil, FHe_dil, FN2_dil, sp_stop_msw)
        Inspired_Helium_Pressure = REAL(ppHe)
        Inspired_Nitrogen_Pressure = REAL(ppN2)
    else:
        Inspired_Helium_Pressure = (Ambient_Pressure - Water_Vapor_Pressure) * mh
        Inspired_Nitrogen_Pressure = (Ambient_Pressure - Water_Vapor_Pressure) * mn# lock-up prevention check
    for I in range(1, 17):
        i = I-1
        if (Inspired_Helium_Pressure + Inspired_Nitrogen_Pressure) > 0.0:
            Weighted_Allowable_Gradient = (
                Deco_Gradient_He[i] * Inspired_Helium_Pressure
                + Deco_Gradient_N2[i] * Inspired_Nitrogen_Pressure
            ) / (Inspired_Helium_Pressure + Inspired_Nitrogen_Pressure)

            if (Inspired_Helium_Pressure + Inspired_Nitrogen_Pressure + Constant_Pressure_Other_Gases - Weighted_Allowable_Gradient - 0.1) > (Next_Stop + Barometric_Pressure):
                raise RuntimeError(f"ERROR! OFF-GASSING GRADIENT IS TOO SMALL TO DECOMPRESS AT THE {Deco_Stop_Depth:6.1f} STOP")

    # Label 700: increase time in Minimum_Deco_Stop_Time increments until ceiling clears Next_Stop
    _guard_iter = 0
    _guard_max_iter = int(os.environ.get('VPM_FREEZE_GUARD_MAXITER', '20000'))

    while True:
        Initial_Helium_Pressure = [0.0]*16
        Initial_Nitrogen_Pressure = [0.0]*16

        for I in range(1, 17):
            i = I-1
            Initial_Helium_Pressure[i] = Helium_Pressure[i]
            Initial_Nitrogen_Pressure[i] = Nitrogen_Pressure[i]

            Helium_Pressure[i] = HALDANE_EQUATION(
                Initial_Helium_Pressure[i],
                Inspired_Helium_Pressure,
                Helium_Time_Constant[i],
                Segment_Time
            )
            Nitrogen_Pressure[i] = HALDANE_EQUATION(
                Initial_Nitrogen_Pressure[i],
                Inspired_Nitrogen_Pressure,
                Nitrogen_Time_Constant[i],
                Segment_Time
            )

        Deco_Ceiling_Depth = CALC_DECO_CEILING()
        # Freeze-guard: if this stop loop does not converge, abort with diagnostics.
        _guard_iter += 1
        if _guard_iter >= _guard_max_iter:
            dc, pilot, dbg = _calc_deco_ceiling_and_pilot()
            raise RuntimeError(
                "ERROR! OFF-GASSING GRADIENT IS TOO SMALL TO DECOMPRESS AT THE. "
                "stop={:.3f}m next={:.3f}m mix={} iter={} run_time={:.3f} seg_time={:.3f} "
                "ceiling={:.6f}m pilot={} pilot_loading(He,N2)=({:.6f},{:.6f}) "
                "pilot_grad(He,N2)=({:.6f},{:.6f}) pilot_wag={:.6f} tol_amb={:.6f} baro={:.6f} "
                "insp_inert(He,N2)=({:.6f},{:.6f})"
                .format(
                    Deco_Stop_Depth, Next_Stop, Mix_Number, _guard_iter, Run_Time, Segment_Time,
                    dc, pilot, dbg['he'], dbg['n2'], dbg['ghe'], dbg['gn2'], dbg['wag'],
                    dbg['tol_amb'], Barometric_Pressure, Inspired_Helium_Pressure, Inspired_Nitrogen_Pressure
                )
            )

        if Deco_Ceiling_Depth > Next_Stop:
            Segment_Time = Minimum_Deco_Stop_Time
            Time_Counter = Temp_Segment_Time
            Temp_Segment_Time = Time_Counter + Minimum_Deco_Stop_Time
            Last_Run_Time = Run_Time
            Run_Time = Last_Run_Time + Minimum_Deco_Stop_Time
            # GOTO 700
            continue

        Segment_Time = Temp_Segment_Time
        return


def CALC_DECO_CEILING() -> float:
    """FORTRAN SUBROUTINE CALC_DECO_CEILING - 1:1 port.

    Returns the maximum compartment deco ceiling depth computed from Deco_Gradient arrays.
    """
    global Constant_Pressure_Other_Gases
    global Barometric_Pressure
    global Helium_Pressure, Nitrogen_Pressure
    global Deco_Gradient_He, Deco_Gradient_N2

    Compartment_Deco_Ceiling = [0.0] * 16

    for I in range(1, 17):
        i = I - 1
        Gas_Loading = Helium_Pressure[i] + Nitrogen_Pressure[i]

        if Gas_Loading > 0.0:
            Weighted_Allowable_Gradient = (
                Deco_Gradient_He[i] * Helium_Pressure[i]
                + Deco_Gradient_N2[i] * Nitrogen_Pressure[i]
            ) / (Helium_Pressure[i] + Nitrogen_Pressure[i])

            Tolerated_Ambient_Pressure = (Gas_Loading + Constant_Pressure_Other_Gases) - Weighted_Allowable_Gradient
        else:
            Weighted_Allowable_Gradient = min(Deco_Gradient_He[i], Deco_Gradient_N2[i])
            Tolerated_Ambient_Pressure = Constant_Pressure_Other_Gases - Weighted_Allowable_Gradient

        if Tolerated_Ambient_Pressure < 0.0:
            Tolerated_Ambient_Pressure = 0.0

        Compartment_Deco_Ceiling[i] = Tolerated_Ambient_Pressure - Barometric_Pressure

    Deco_Ceiling_Depth = Compartment_Deco_Ceiling[0]
    ipilot = 1  # preserved for parity; not used elsewhere
    for I in range(2, 17):
        i = I - 1
        if Deco_Ceiling_Depth <= Compartment_Deco_Ceiling[i]:
            ipilot = I
            Deco_Ceiling_Depth = Compartment_Deco_Ceiling[i]

    return Deco_Ceiling_Depth

def _calc_deco_ceiling_and_pilot():
    """Return (deco_ceiling_depth, pilot_compartment(1..16), dbg_dict).

    This mirrors CALC_DECO_CEILING(), but also returns the compartment index that
    sets the ceiling, plus key quantities for diagnostics.
    """
    global Constant_Pressure_Other_Gases
    global Barometric_Pressure
    global Helium_Pressure, Nitrogen_Pressure
    global Deco_Gradient_He, Deco_Gradient_N2

    comp = [0.0] * 16
    dbg_per = [None] * 16

    for I in range(1, 17):
        i = I - 1
        he = Helium_Pressure[i]
        n2 = Nitrogen_Pressure[i]
        gas_loading = he + n2
        if gas_loading > 0.0:
            wag = (Deco_Gradient_He[i] * he + Deco_Gradient_N2[i] * n2) / gas_loading
            tol_amb = (gas_loading + Constant_Pressure_Other_Gases) - wag
        else:
            wag = min(Deco_Gradient_He[i], Deco_Gradient_N2[i])
            tol_amb = Constant_Pressure_Other_Gases - wag

        if tol_amb < 0.0:
            tol_amb = 0.0

        comp[i] = tol_amb - Barometric_Pressure
        dbg_per[i] = {
            'he': float(he),
            'n2': float(n2),
            'ghe': float(Deco_Gradient_He[i]),
            'gn2': float(Deco_Gradient_N2[i]),
            'wag': float(wag),
            'tol_amb': float(tol_amb),
        }

    ceiling = comp[0]
    pilot = 1
    for I in range(2, 17):
        i = I - 1
        if ceiling <= comp[i]:
            ceiling = comp[i]
            pilot = I

    return float(ceiling), int(pilot), dbg_per[pilot - 1]


def GAS_LOADINGS_SURFACE_INTERVAL(*args, **kwargs):
    """FORTRAN SUBROUTINE GAS_LOADINGS_SURFACE_INTERVAL - TODO: 1:1 port."""
    raise NotImplementedError('Not yet ported')


def VPM_REPETITIVE_ALGORITHM(*args, **kwargs):
    """FORTRAN SUBROUTINE VPM_REPETITIVE_ALGORITHM - TODO: 1:1 port."""
    raise NotImplementedError('Not yet ported')


def CALC_BAROMETRIC_PRESSURE(Altitude_of_Dive: float) -> None:
    """FORTRAN SUBROUTINE CALC_BAROMETRIC_PRESSURE (simplified for Altitude=0).

    VPMDECO expects Barometric_Pressure in diving pressure units (fsw/msw).
    For Altitude_of_Dive == 0, barometric pressure is 1 atm = Units_Factor.
    """
    global Barometric_Pressure, Units_Factor
    # For the current porting scope we implement the sea-level case exactly.
    if abs(Altitude_of_Dive) < 1.0e-9:
        Barometric_Pressure = 10.0
    else:
        # A full altitude model will be ported 1:1 when the altitude routines are in-scope.
        # For now, approximate by scaling sea-level pressure; this branch is not used when Altitude_Dive_Algorithm=OFF.
        Barometric_Pressure = Units_Factor * math.exp(-Altitude_of_Dive/7000.0)
    return


def VPM_ALTITUDE_DIVE_ALGORITHM(*args, **kwargs):
    """FORTRAN SUBROUTINE VPM_ALTITUDE_DIVE_ALGORITHM - TODO: 1:1 port."""
    raise NotImplementedError('Not yet ported')


def CLOCK() -> Tuple[int, int, int, int, int, str]:
    """FORTRAN SUBROUTINE CLOCK (minimal).

    Returns: Month, Day, Year, Clock_Hour, Minute, M (am/pm marker)
    This is used only for banner output in the original program.
    """
    now = datetime.datetime.now()
    hour = now.hour
    m = 'a'
    if hour >= 12:
        m = 'p'
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return now.month, now.day, now.year, hour12, now.minute, m


# ============================================================
# ENTRY POINT
# ============================================================
def _build_set_text_from_args(args) -> str:
    # Keep the same keys the port expects; values overwrite defaults.
    fn2 = 1.0 - args.FO2 - args.FHe
    return f'''\
Units=msw
Altitude_Dive_Algorithm=OFF
Minimum_Deco_Stop_Time={args.Minimum_Deco_Stop_Time}
Critical_Radius_N2_Microns={args.crit_rad_n2}
Critical_Radius_He_Microns={args.crit_rad_he}
Critical_Volume_Algorithm='{args.Critical_Volume_Algorithm}'
Crit_Volume_Parameter_Lambda={args.Crit_Volume_Parameter_Lambda}
Gradient_Onset_of_Imperm_Atm={args.Gradient_Onset_of_Imperm_Atm}
Surface_Tension_Gamma={args.Surface_Tension_Gamma}
Skin_Compression_GammaC={args.Skin_Compression_GammaC}
rapsol1={args.rapsol}
rapsol2={args.rapsol}
Regeneration_Time_Constant={args.Regeneration_Time_Constant}
Pressure_Other_Gases_mmHg={args.Pressure_Other_Gases_mmHg}
'''
def _build_in_text_from_args(args) -> str:
    """Build a Fortran-style .in schedule from CLI args.

    Supports:
      - single-mix (default): uses --FO2/--FHe for entire dive
      - multigas: if --gases_json is provided, it must be a JSON array of dicts
        with keys at least: FO2, FHe, MOD, enabled (optional).
        The first row is treated as bottom gas (mix 1). Gas switches during ascent
        are encoded as ascent-parameter changes (code 99 block), like in Baker's input.
      - advanced profile shaping from GUI:
        * optional JSON for descent bands (A2)
        * optional JSON for ascent bands (A3)
        * optional JSON for descent stops (A1)
        These do **not** change the physics of VPM-B, only the way the
        schedule (segments + ascent parameter changes) is constructed.
    """

    import json

    def _coerce_bool(v, default=True) -> bool:
        if v is None:
            return default
        try:
            if isinstance(v, bool):
                return v
        except Exception:
            return default
        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
        return default

    
    # ----------------------------
    # Parse gases table (from GUI/CLI)
    # ----------------------------
    gases = None
    if getattr(args, "gases_json", None):
        try:
            gases = json.loads(args.gases_json)
        except Exception as e:
            raise SystemExit(f"gases_json non valido: {e}")
    if not isinstance(gases, list) or not gases:
        # fallback: single mix from FO2/FHe with MOD=bottom depth
        gases = [{
            "name": "mix1",
            "FO2": float(getattr(args, "FO2", 0.21) or 0.21),
            "FHe": float(getattr(args, "FHe", 0.0) or 0.0),
            "MOD": float(getattr(args, "depth_m", 0.0) or 0.0),
            "enabled": True,
        }]
    def _pick_gas_for_depth(depth_m: float, gases: list[dict]) -> dict:
        """Among enabled gases with MOD >= depth, pick the one with the lowest MOD."""
        usable = []
        for g in gases:
            try:
                if not _coerce_bool(g.get("enabled", True), True):
                    continue
                mod = float(g.get("MOD", 0.0))
            except Exception:
                continue
            if depth_m <= mod + 1e-9:
                usable.append((mod, g))
        if not usable:
            return gases[0]
        usable.sort(key=lambda t: t[0])
        return usable[0][1]
    # ----------------------------
    # Parse optional JSON from GUI (A1/A2/A3)
    # ----------------------------
    descent_bands = None
    ascent_bands = None
    descent_stops = None

    if getattr(args, "descent_bands_json", None):
        try:
            descent_bands = json.loads(args.descent_bands_json)
        except Exception as e:
            raise SystemExit(f"descent_bands_json non valido: {e}")

    if getattr(args, "ascent_bands_json", None):
        try:
            ascent_bands = json.loads(args.ascent_bands_json)
        except Exception as e:
            raise SystemExit(f"ascent_bands_json non valido: {e}")

    if getattr(args, "descent_stops_json", None):
        try:
            descent_stops = json.loads(args.descent_stops_json)
        except Exception as e:
            raise SystemExit(f"descent_stops_json non valido: {e}")

    # Normalise shapes
    if not isinstance(descent_bands, list) or not descent_bands:
        descent_bands = [{
            "from_m": 0.0,
            "to_m": float(getattr(args, "depth_m", 0.0) or 0.0),
            "speed": float(getattr(args, "desc_rate", 20.0) or 20.0),
        }]
    if not isinstance(ascent_bands, list) or not ascent_bands:
        ascent_bands = None
    if not isinstance(descent_stops, list):
        descent_stops = []

    
    # ------------------------------------------------------------
    # Bailout gating arrays (used by VPMDECO_ORG when CCR + BO is ON)
    # mix numbering is 1..N as passed to the engine (.in schedule):
    #   - in CC mode from GUI: mix 1 = diluent, mix 2..N = bailout gases
    #   - in OC multigas: mix 1..N = OC gases (bailout not used)
    # ------------------------------------------------------------
    global BO_MIX_MODQ, BO_MIX_ENABLED
    try:
        if isinstance(gases, list) and gases:
            n = len(gases)
            BO_MIX_MODQ = [0.0] * (n + 1)     # index 0 unused
            BO_MIX_ENABLED = [False] * (n + 1)
            for i, g in enumerate(gases, start=1):
                try:
                    mod = float(g.get("MOD", 0.0))
                except Exception:
                    mod = 0.0
                try:
                    en = _coerce_bool(g.get("enabled", True), True)
                except Exception:
                    en = True
                BO_MIX_MODQ[i] = _bo_mod_quantize_m(mod)
                BO_MIX_ENABLED[i] = bool(en)
        else:
            BO_MIX_MODQ = None
            BO_MIX_ENABLED = None
    except Exception:
        BO_MIX_MODQ = None
        BO_MIX_ENABLED = None

    depth_bottom = float(args.depth_m)
    bottom_time = float(args.bottom_time_min)

    def _desc_speed_for_depth(depth_m: float) -> float:
        """Return descent speed (m/min) to use around this depth."""
        base = float(args.desc_rate)
        for band in descent_bands or []:
            try:
                from_m = float(band.get("from_m", 0.0))
                to_m = float(band.get("to_m", 0.0))
                speed = float(band.get("speed", base))
            except Exception:
                continue
            lo = min(from_m, to_m)
            hi = max(from_m, to_m)
            if lo - 1e-9 <= depth_m <= hi + 1e-9:
                return max(0.1, speed)
        return max(0.1, base)

    def _asc_speed_for_depth(depth_m: float) -> float:
        """Return ascent speed (m/min, positive) to use around this depth."""
        base = float(args.asc_rate)
        if not ascent_bands:
            return max(0.1, base)
        for band in ascent_bands:
            try:
                from_m = float(band.get("from_m", 0.0))
                to_m = float(band.get("to_m", 0.0))
                speed = float(band.get("speed", base))
            except Exception:
                continue
            lo = min(from_m, to_m)
            hi = max(from_m, to_m)
            if lo - 1e-9 <= depth_m <= hi + 1e-9:
                return max(0.1, speed)
        return max(0.1, base)

    # ----------------------------
    # Determine mixes (unchanged logic, with optional gases_json)
    # ----------------------------
    gases = None
    if args.gases_json:
        try:
            gases = json.loads(args.gases_json)
            if not isinstance(gases, list) or not gases:
                raise ValueError("gases_json must be a non-empty list")
        except Exception as e:
            raise SystemExit(f"gases_json non valido: {e}")
    if gases is None:
        gases = [{
            "FO2": args.FO2,
            "FHe": args.FHe,
            "MOD": max(6.0, float(args.depth_m)),
            "enabled": True,
        }]

    mixes = []
    for g in gases:
        try:
            fo2 = float(g.get("FO2", 0.0))
            fhe = float(g.get("FHe", 0.0))
        except Exception:
            raise SystemExit("gases_json: FO2/FHe devono essere numerici")
        if fo2 < 0.0 or fhe < 0.0 or (fo2 + fhe) > 1.0 + 1e-12:
            raise SystemExit("gases_json: FO2/FHe non validi (FO2+FHe<=1)")
        fn2 = 1.0 - fo2 - fhe
        mixes.append((fo2, fhe, fn2))

    # ----------------------------
    # Build PROFILE segments (1 = discesa, 2 = fondo/soste)
    # ----------------------------
    valid_stops: list[tuple[float, float]] = []
    for s in descent_stops:
        try:
            d = float(s.get("depth", 0.0))
            t = float(s.get("time", 0.0))
        except Exception:
            continue
        if d > 0.0 and t > 0.0 and d <= depth_bottom + 1e-9:
            valid_stops.append((d, t))
    valid_stops.sort(key=lambda x: x[0])

    # Build waypoints for descent legs.
    # IMPORTANT: split descent also at band boundaries (even if no descent stops are defined).
    # This prevents the whole descent being collapsed into a single leg using only the deepest band speed.
    waypoints = [0.0]
    def _wp_add(val: float):
        """Add waypoint if not already present within tolerance."""
        for w in waypoints:
            if abs(w - val) < 1e-6:
                return
        waypoints.append(val)

    # 1) explicit descent stops (from GUI)
    for d, _t in valid_stops:
        _wp_add(float(d))

    # 2) descent band boundaries (from GUI)
    for band in descent_bands or []:
        try:
            bm_from = float(band.get("from_m", 0.0))
            bm_to = float(band.get("to_m", 0.0))
        except Exception:
            continue
        for bd in (bm_from, bm_to):
            if bd > 0.0 and bd < depth_bottom - 1e-9:
                _wp_add(bd)

    # 3) bottom depth
    _wp_add(depth_bottom)
    waypoints.sort()

    seg_lines: list[str] = []
    runtime = 0.0
    current_mix_idx = 1  # bottom gas for discesa

    # Legs di discesa a tratti
    for i in range(len(waypoints) - 1):
        start_d = waypoints[i]
        end_d = waypoints[i + 1]
        if end_d <= start_d:
            continue
        mid_depth = 0.5 * (start_d + end_d)
        v_desc = _desc_speed_for_depth(mid_depth)
        rate_fortran = abs(v_desc)
        dt = (end_d - start_d) / rate_fortran if rate_fortran > 0.0 else 0.0
        runtime += dt
        seg_lines.append("1 !Profile code 1 = descent\n")
        seg_lines.append(
            f"{start_d:.6f},{end_d:.6f},{rate_fortran:.6f},{current_mix_idx:d} !Starting depth, ending depth, rate, gasmix\n"
        )
        for d_stop, t_stop in valid_stops:
            if abs(d_stop - end_d) < 1e-6 and t_stop > 0.0:
                runtime += t_stop
                seg_lines.append("2 !Profile code 2 = constant depth\n")
                seg_lines.append(
                    f"{end_d:.6f},{runtime:.6f},{current_mix_idx:d} !Depth, run time at end of segment, gasmix\n"
                )

    if bottom_time > 0.0:
        runtime += bottom_time
        seg_lines.append("2 !Profile code 2 = constant depth\n")
        seg_lines.append(
            f"{depth_bottom:.6f},{runtime:.6f},{current_mix_idx:d} !Depth, run time at end of segment, gasmix\n"
        )

    # ----------------------------
    # Ascent parameter changes (code 99)
    # ----------------------------
    depth_bottom_f = float(depth_bottom)
    candidate_depths = {depth_bottom_f}
    enabled_mods = []

    for i, g in enumerate(gases, start=1):
        if i == 1:
            continue
        if not _coerce_bool(g.get("enabled", True), True):
            continue
        try:
            mod = float(g.get("MOD", 0.0))
        except Exception:
            continue
        if mod <= 0.0:
            continue
        if mod <= depth_bottom_f + 1e-9:
            enabled_mods.append(mod)
            candidate_depths.add(mod)

    if ascent_bands:
        for band in ascent_bands:
            try:
                from_m = float(band.get("from_m", 0.0))
                to_m = float(band.get("to_m", 0.0))
            except Exception:
                continue
            for d in (from_m, to_m):
                d_f = float(d)
                if d_f < 0.0:
                    continue
                if d_f > depth_bottom_f + 1e-9:
                    continue
                candidate_depths.add(d_f)

    sorted_depths = sorted(candidate_depths, reverse=True)

    changes: list[tuple[float, int, float]] = []
    for d in sorted_depths:
        gas_pick = _pick_gas_for_depth(float(d), gases)
        try:
            mix_idx = gases.index(gas_pick) + 1
        except ValueError:
            mix_idx = 1
        v_asc = _asc_speed_for_depth(float(d))
        changes.append((float(d), mix_idx, v_asc))

    n_changes = len(changes)

    # ----------------------------
    # Compose .in text
    # ----------------------------
    lines: list[str] = []
    lines.append("CLI dive (GUI)\n")
    lines.append(f"{len(mixes)} !Number of gas mixes\n")
    for fo2, fhe, fn2 in mixes:
        lines.append(f"{fo2:.6f},{fhe:.6f},{fn2:.6f} !Fraction O2, Fraction He, Fraction N2\n")

    lines.extend(seg_lines)

    lines.append("99 !Profile code 99 = decompress\n")
    lines.append(f"{n_changes} !Number of ascent parameter changes\n")
    last_stop_m = float(getattr(args, "last_stop_m", 3.0))
    base_step = float(args.step_size)

    for depth_change, mix_idx, v_asc in changes:
        asc_rate_fortran = -abs(v_asc)
        step_here = base_step
        # If user selected last stop deeper than 3 m (e.g., 6 m),
        # emit a step size change at that depth so the next stop is surface (0 m).
        if last_stop_m > base_step + 1e-9 and abs(depth_change - last_stop_m) < 1e-6:
            step_here = last_stop_m
        lines.append(
            f"{depth_change:.6f},{mix_idx:d},{asc_rate_fortran:.6f},{step_here:.6f} !Starting depth, gasmix, rate, step size\n"
        )
    lines.append("0 ! Repetitive code 0 = last dive/end of file\n")
    return "".join(lines)
def _print_profile_dettagliato(args, result: dict) -> None:
    # Match vpmb_GUI.py regex patterns exactly.
    n = 1
    # DESC
    desc_time = args.depth_m / args.desc_rate
    print("=== PROFILO DETTAGLIATO")
    print(f"{n:3d} DESC seg_time= {desc_time:.1f} run_time= {desc_time:.1f} from= {0.0:.1f}m to= {args.depth_m:.1f}m")
    n += 1
    # BOTT
    rt_bott_end = desc_time + args.bottom_time_min
    print(f"{n:3d} BOTT seg_time= {args.bottom_time_min:.1f} run_time= {rt_bott_end:.1f} depth= {args.depth_m:.1f}m")
    n += 1

    stops = result.get("stops") or []
    if not stops:
        # ASC straight to surface
        asc_time = args.depth_m / args.asc_rate
        rt_end = rt_bott_end + asc_time
        print(f"{n:3d} ASC  seg_time= {asc_time:.1f} run_time= {rt_end:.1f} from= {args.depth_m:.1f}m to= {0.0:.1f}m")
        return

    # First ascent to first stop
    first_stop_depth = stops[0][0]
    asc_time = (args.depth_m - first_stop_depth) / args.asc_rate
    rt = rt_bott_end + asc_time
    print(f"{n:3d} ASC  seg_time= {asc_time:.1f} run_time= {rt:.1f} from= {args.depth_m:.1f}m to= {first_stop_depth:.1f}m")
    n += 1

    last_depth = first_stop_depth
    last_rt_end = rt
    # STOPs + intermediate ASC
    for i, (depth, stop_time, rt_end_stop) in enumerate(stops):
        # STOP at depth
        print(f"{n:3d} STOP seg_time= {stop_time:.1f} run_time= {rt_end_stop:.1f} depth= {depth:.1f}m")
        n += 1
        last_depth = depth
        last_rt_end = rt_end_stop
        # next ascent
        next_depth = stops[i+1][0] if i+1 < len(stops) else 0.0
        if next_depth < 0.0:
            next_depth = 0.0
        if last_depth > next_depth:
            asc_time = (last_depth - next_depth) / args.asc_rate
            rt_end = last_rt_end + asc_time
            print(f"{n:3d} ASC  seg_time= {asc_time:.1f} run_time= {rt_end:.1f} from= {last_depth:.1f}m to= {next_depth:.1f}m")
            n += 1
            last_depth = next_depth
            last_rt_end = rt_end


# ---- Passive profile post-processing helpers (report-only) ----
def _split_profile_runs(profile_rows):
    """Split a raw profile log into runs when runtime decreases.
    Some internal engine passes may re-run schedule generation; those runs restart runtime.
    """
    runs = []
    cur = []
    last_rt = None
    for r in profile_rows or []:
        try:
            rt = float(r.get("runtime_end", 0.0))
        except Exception:
            rt = 0.0
        if last_rt is not None and rt + 1e-6 < last_rt:
            if cur:
                runs.append(cur)
            cur = []
        cur.append(r)
        last_rt = rt
    if cur:
        runs.append(cur)
    return runs

def _select_final_profile(profile_rows, target_runtime_total):
    """Pick the final monotone run and return it.
    Strategy: split on runtime decreases and choose the run whose final runtime_end
    is closest to target_runtime_total (ties -> longer run).
    """
    runs = _split_profile_runs(profile_rows)
    if not runs:
        return []
    best = None
    best_key = None
    for run in runs:
        # final runtime_end
        try:
            end_rt = float(run[-1].get("runtime_end", 0.0))
        except Exception:
            end_rt = 0.0
        key = (abs(end_rt - float(target_runtime_total or 0.0)), -len(run))
        if best_key is None or key < best_key:
            best_key = key
            best = run
    # enforce monotone runtime (just in case)
    out = []
    last = None
    for r in best or []:
        try:
            rt = float(r.get("runtime_end", 0.0))
        except Exception:
            rt = 0.0
        if last is None or rt + 1e-6 >= last:
            out.append(r)
            last = rt
    return out
# ---- end helpers ----

def main() -> None:
    import argparse, json
    global VPMDECO_SET_TEXT, VPMDECO_IN_TEXT
    global DEBUG_PROJECTED_ASCENT

    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--depth_m", type=float, required=False, default=None)
    p.add_argument("--bottom_time_min", type=float, required=False, default=None)
    p.add_argument("--desc_rate", type=float, required=False, default=None)
    p.add_argument("--asc_rate", type=float, required=False, default=None)
    p.add_argument("--FO2", type=float, required=False, default=None)
    p.add_argument("--FHe", type=float, required=False, default=None)
    p.add_argument("--rapsol", type=float, required=False, default=1.0)
    p.add_argument("--crit_rad_n2", type=float, required=False, default=0.55)
    p.add_argument("--crit_rad_he", type=float, required=False, default=0.45)
    p.add_argument("--step_size", type=float, required=False, default=3.0)
    p.add_argument("--last_stop_m", type=float, required=False, default=3.0)
    p.add_argument("--gases_json", type=str, required=False, default=None)
    p.add_argument("--debug_projected_ascent", type=int, required=False, default=0)

    # advanced params (GUI)
    p.add_argument("--Surface_Tension_Gamma", type=float, required=False, default=0.0179)
    p.add_argument("--Skin_Compression_GammaC", type=float, required=False, default=0.257)
    p.add_argument("--Regeneration_Time_Constant", type=float, required=False, default=20160.0)
    p.add_argument("--Pressure_Other_Gases_mmHg", type=float, required=False, default=102.0)
    p.add_argument("--Minimum_Deco_Stop_Time", type=float, required=False, default=1.0)
    p.add_argument("--Critical_Volume_Algorithm", type=str, required=False, default="ON")
    p.add_argument("--Crit_Volume_Parameter_Lambda", type=float, required=False, default=7500.0)
    p.add_argument("--Gradient_Onset_of_Imperm_Atm", type=float, required=False, default=8.2)

    args, _unknown = p.parse_known_args()
    DEBUG_PROJECTED_ASCENT = (int(args.debug_projected_ascent) != 0)

    # CCR minimal harness toggle (default OFF). When enabled, only inspired inert pressures change.
    global CCR_MODE, CCR_SP_ATM, CCR_SP_MSW
    if os.environ.get("VPM_CCR_MODE", "1") in ("1", "true", "TRUE", "yes", "YES"):
        CCR_MODE = True
        # optional: allow overriding setpoint (atm) from env
        try:
            CCR_SP_ATM = float(os.environ.get("VPM_CCR_SP_ATM", str(CCR_SP_ATM)))
        except Exception:
            pass
        CCR_SP_MSW = CCR_SP_ATM * 10.0

    # If called without CLI params, keep the embedded defaults and just run.
    if args.depth_m is None:
        result = VPMDECO_ORG()
        print("=== VPMDECO_ORG (Python port) ===")
        print(f"Depth_Start_of_Deco_Zone: {result['depth_start_of_deco_zone']:.1f}")
        print(f"First stop: {result['first_stop_depth']:.1f}")
        print(f"Runtime total: {result['runtime_total']:.1f}")
        if result['stops']:
            print("-- Stops (depth, stop_time, run_time_end) --")
            for d, st, rt in result['stops']:
                print(f"{d:6.1f}  {st:6.1f}  {rt:7.1f}")

        if os.environ.get("VPM_FORTRAN_SCHEDULE", "0") == "1":
            prof = _select_final_profile_by_stops((result.get("profile") or []), result.get('stops') or [], float(result.get("runtime_total", 0.0)))
            if prof:
                print("-- Full profile (passive log; ASC/STOP/CONST/DESC) --")
                print("step  kind   step_min  runtime_end   from_m   to_m   depth_m  mix  note")
                stepn = 1
                for row in prof:
                    kind = row.get("kind","")
                    step_min = row.get("step_min")
                    rt_end = row.get("runtime_end")
                    fm = row.get("from_m")
                    tm = row.get("to_m")
                    dm = row.get("depth_m")
                    mix = row.get("mix")
                    note = row.get("note","")
                    print(f"{stepn:4d}  {kind:5s}  {step_min if step_min is not None else 0:8.3f}  "
                          f"{rt_end if rt_end is not None else 0:10.3f}  "
                          f"{'' if fm is None else f'{fm:6.1f}':>6}  "
                          f"{'' if tm is None else f'{tm:6.1f}':>6}  "
                          f"{'' if dm is None else f'{dm:6.1f}':>6}  "
                          f"{'' if mix is None else str(mix):>3}  {note}")
                    stepn += 1
        return

    # Basic validation
    if args.asc_rate <= 0 or args.desc_rate <= 0:
        raise SystemExit("desc_rate e asc_rate devono essere > 0 (m/min)")
    if args.FO2 < 0 or args.FHe < 0 or (args.FO2 + args.FHe) > 1.0:
        raise SystemExit("FO2/FHe non validi (FO2+FHe<=1)")

    # overwrite embedded inputs
    VPMDECO_SET_TEXT = _build_set_text_from_args(args)
    VPMDECO_IN_TEXT = _build_in_text_from_args(args)

    result = VPMDECO_ORG()

    # Print the section the GUI expects to parse
    _print_profile_dettagliato(args, result)

if __name__ == "__main__":
    main()

# ============================================================
# VERBATIM ORIGINAL FORTRAN SOURCE (DO NOT EDIT)
# ============================================================
FORTRAN_SOURCE = r"""\
      Subroutine VPMDECO_org
      !USE DFLIB
      USE QWPAINT
C===============================================================================
C     Varying Permeability Model (VPM) Decompression Program in FORTRAN
C     with Boyle's Law compensation algorithm (VPM-B)
C
C     Author:  Erik C. Baker
C
C     "DISTRIBUTE FREELY - CREDIT THE AUTHORS"
C
C     This program extends the 1986 VPM algorithm (Yount & Hoffman) to include
C     mixed gas, repetitive, and altitude diving.  Developments to the algorithm
C     were made by David E. Yount, Eric B. Maiken, and Erik C. Baker over a
C     period from 1999 to 2001.  This work is dedicated in remembrance of
C     Professor David E. Yount who passed away on April 27, 2000.
C 
C     Notes:
C     1.  This program uses the sixteen (16) half-time compartments of the
C         Buhlmann ZH-L16 model.  The optional Compartment 1b is used here with
C         half-times of 1.88 minutes for helium and 5.0 minutes for nitrogen.
C
C     2.  This program uses various DEC, IBM, and Microsoft extensions which
C         may not be supported by all FORTRAN compilers.  Comments are made with
C         a capital "C" in the first column or an exclamation point "!" placed
C         in a line after code.  An asterisk "*" in column 6 is a continuation
C         of the previous line.  All code, except for line numbers, starts in
C         column 7.
C
C     3.  Comments and suggestions for improvements are welcome.  Please
C         respond by e-mail to:  EBaker@se.aeieng.com
C
C     Acknowledgment:  Thanks to Kurt Spaugh for recommendations on how to clean
C     up the code.
C===============================================================================
      IMPLICIT NONE
C===============================================================================
C     LOCAL VARIABLES - MAIN PROGRAM
C===============================================================================
      CHARACTER M*1, OS_Command*3, Word*7, Units*3
      CHARACTER Line1*70, Critical_Volume_Algorithm*3
      CHARACTER Units_Word1*4, Units_Word2*7, Altitude_Dive_Algorithm*3

      INTEGER I, J                                                !loop counters
      INTEGER*2 Month, Day, Year, Clock_Hour, Minute
      INTEGER Number_of_Mixes, Number_of_Changes, Profile_Code  
      INTEGER Segment_Number_Start_of_Ascent, Repetitive_Dive_Flag

      LOGICAL Schedule_Converged, Critical_Volume_Algorithm_Off
      LOGICAL Altitude_Dive_Algorithm_Off

      REAL Ascent_Ceiling_Depth, Deco_Stop_Depth, Step_Size
      REAL Sum_of_Fractions, Sum_Check
      REAL Depth, Ending_Depth, Starting_Depth 
      REAL Rate, Rounding_Operation1, Run_Time_End_of_Segment
      REAL Last_Run_Time, Stop_Time, Depth_Start_of_Deco_Zone
      REAL Rounding_Operation2, Deepest_Possible_Stop_Depth
      REAL First_Stop_Depth, Critical_Volume_Comparison
      REAL Next_Stop, Run_Time_Start_of_Deco_Zone
      REAL Critical_Radius_N2_Microns, Critical_Radius_He_Microns
      REAL Run_Time_Start_of_Ascent, Altitude_of_Dive
      REAL Deco_Phase_Volume_Time, Surface_Interval_Time
      REAL Pressure_Other_Gases_mmHg
C===============================================================================
C     LOCAL ARRAYS - MAIN PROGRAM
C===============================================================================
      INTEGER Mix_Change(10)

      REAL Fraction_Oxygen(10)
      REAL Depth_Change (10)
      Common/cambiogas/
     #       Number_of_Changes,Mix_Change,Fraction_Oxygen,Depth_Change
      REAL Rate_Change(10), Step_Size_Change(10)
      REAL Helium_Half_Time(16), Nitrogen_Half_Time(16)
      REAL He_Pressure_Start_of_Ascent(16)
      REAL N2_Pressure_Start_of_Ascent(16)
      REAL He_Pressure_Start_of_Deco_Zone(16)
      REAL N2_Pressure_Start_of_Deco_Zone(16) 
      REAL Phase_Volume_Time (16)
      REAL Last_Phase_Volume_Time(16)
 
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure
      
      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2
      
      REAL Crit_Volume_Parameter_Lambda
      COMMON /Block_20/ Crit_Volume_Parameter_Lambda
      
      REAL Minimum_Deco_Stop_Time
      COMMON /Block_21/ Minimum_Deco_Stop_Time
      
      REAL Regeneration_Time_Constant
      COMMON /Block_22/ Regeneration_Time_Constant
      
      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases
      
      REAL Gradient_Onset_of_Imperm_Atm
      COMMON /Block_14/ Gradient_Onset_of_Imperm_Atm
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      INTEGER Segment_Number
      REAL Run_Time, Segment_Time
      COMMON /Block_2/ Run_Time, Segment_Number, Segment_Time

      REAL Ending_Ambient_Pressure 
      COMMON /Block_4/ Ending_Ambient_Pressure
      
      INTEGER Mix_Number
      COMMON /Block_9/ Mix_Number

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure

      LOGICAL Units_Equal_Fsw, Units_Equal_Msw
      COMMON /Block_15/ Units_Equal_Fsw, Units_Equal_Msw

      REAL Units_Factor
      COMMON /Block_16/ Units_Factor
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      real x(200),y(200),zn2(200,16),zHe(200,16)
      integer kk
      common /grafici/x,y,zN2,zHe,kk
      Real DCD(10,16)
      integer ipilot,jmax(16),jj
      common /pilota/ipilot,jmax,DCD
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure

      REAL Fraction_Helium(10), Fraction_Nitrogen(10)  
      COMMON /Block_5/ Fraction_Helium, Fraction_Nitrogen

      REAL Initial_Critical_Radius_He(16)
      REAL Initial_Critical_Radius_N2(16)      
      COMMON /Block_6/ Initial_Critical_Radius_He,
     *           Initial_Critical_Radius_N2     

      REAL Adjusted_Critical_Radius_He(16)
      REAL Adjusted_Critical_Radius_N2(16)
      COMMON /Block_7/ Adjusted_Critical_Radius_He,
     *           Adjusted_Critical_Radius_N2 

      REAL Max_Crushing_Pressure_He(16), Max_Crushing_Pressure_N2(16)
      COMMON /Block_10/ Max_Crushing_Pressure_He,
     *                  Max_Crushing_Pressure_N2

      REAL Surface_Phase_Volume_Time(16)
      COMMON /Block_11/ Surface_Phase_Volume_Time

      REAL Max_Actual_Gradient(16)
      COMMON /Block_12/ Max_Actual_Gradient

      REAL Amb_Pressure_Onset_of_Imperm(16)
      REAL Gas_Tension_Onset_of_Imperm(16)
      COMMON /Block_13/ Amb_Pressure_Onset_of_Imperm,
     *            Gas_Tension_Onset_of_Imperm
C===============================================================================
C     NAMELIST FOR PROGRAM SETTINGS (READ IN FROM ASCII TEXT FILE)
C===============================================================================
      NAMELIST /Program_Settings/ Units, Altitude_Dive_Algorithm,
     *         Minimum_Deco_Stop_Time, Critical_Radius_N2_Microns, 
     *         Critical_Radius_He_Microns, Critical_Volume_Algorithm, 
     *         Crit_Volume_Parameter_Lambda,
     *         Gradient_Onset_of_Imperm_Atm,
     *         Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,
     *         rapsol2,Regeneration_Time_Constant, 
     *         Helium_Half_Time,
     #         Pressure_Other_Gases_mmHg
C===============================================================================
C     ASSIGN HALF-TIME VALUES TO BUHLMANN COMPARTMENT ARRAYS
C===============================================================================
!      DATA Helium_Half_Time(1)/1.88/,Helium_Half_Time(2)/3.02/,
!     *     Helium_Half_Time(3)/4.72/,Helium_Half_Time(4)/6.99/,
!     *     Helium_Half_Time(5)/10.21/,Helium_Half_Time(6)/14.48/,
!     *     Helium_Half_Time(7)/20.53/,Helium_Half_Time(8)/29.11/,
!     *     Helium_Half_Time(9)/41.20/,Helium_Half_Time(10)/55.19/,
!     *     Helium_Half_Time(11)/70.69/,Helium_Half_Time(12)/90.34/,
!     *     Helium_Half_Time(13)/115.29/,Helium_Half_Time(14)/147.42/,
!     *     Helium_Half_Time(15)/188.24/,Helium_Half_Time(16)/240.03/
      DATA Nitrogen_Half_Time(1)/5.0/,Nitrogen_Half_Time(2)/8.0/,
     *     Nitrogen_Half_Time(3)/12.5/,Nitrogen_Half_Time(4)/18.5/,
     *     Nitrogen_Half_Time(5)/27.0/,Nitrogen_Half_Time(6)/38.3/,
     *     Nitrogen_Half_Time(7)/54.3/,Nitrogen_Half_Time(8)/77.0/,
     *     Nitrogen_Half_Time(9)/109.0/,Nitrogen_Half_Time(10)/146.0/,
     *     Nitrogen_Half_Time(11)/187.0/,Nitrogen_Half_Time(12)/239.0/,
     *     Nitrogen_Half_Time(13)/305.0/,Nitrogen_Half_Time(14)/390.0/,
     *     Nitrogen_Half_Time(15)/498.0/,Nitrogen_Half_Time(16)/635.0/
C===============================================================================
C     OPEN FILES FOR PROGRAM INPUT/OUTPUT
C===============================================================================

          
     
      OPEN (UNIT = 7, FILE = 'VPMDECO.IN', STATUS = 'UNKNOWN',
     *         ACCESS = 'SEQUENTIAL', FORM = 'FORMATTED')
      OPEN (UNIT = 8, FILE = 'VPMDECO.OUT', STATUS = 'UNKNOWN',
     *         ACCESS = 'SEQUENTIAL', FORM = 'FORMATTED')        
      OPEN (UNIT = 9, FILE = 'VPMDECO_0.OUT', STATUS = 'UNKNOWN',
     *         ACCESS = 'SEQUENTIAL', FORM = 'FORMATTED')     
      OPEN (UNIT = 10, FILE = 'VPMDECO.SET', STATUS = 'UNKNOWN',
     *         ACCESS = 'SEQUENTIAL', FORM = 'FORMATTED')  

 
C===============================================================================
C     BEGIN PROGRAM EXECUTION WITH OUTPUT MESSAGE TO SCREEN
C===============================================================================
      OS_Command = 'CLS'
      CALL SYSTEMQQ (OS_Command)                    !Pass "clear screen" command 
      PRINT *,' '                                   !to MS operating system
      PRINT *,'PROGRAM VPMDECO'
      PRINT *,' '                            !asterisk indicates print to screen
C===============================================================================
C     READ IN PROGRAM SETTINGS AND CHECK FOR ERRORS
C     IF THERE ARE ERRORS, WRITE AN ERROR MESSAGE AND TERMINATE PROGRAM
C===============================================================================
     
      READ (10,Program_Settings)
!      write (6,Program_Settings)
!      write(6,*) 'rapsol1',rapsol1,'rapsol2',rapsol2
!      pause
      
      write(8,*) 'Nitrogen_Half_Time','     Helium_Half_Time'

      do i=1,16
          write(8,*) Nitrogen_Half_Time(i),Helium_Half_Time(i),i
      end do


            
      IF ((Units .EQ. 'fsw').OR.(Units .EQ. 'FSW')) THEN
          Units_Equal_Fsw = (.TRUE.)
          Units_Equal_Msw = (.FALSE.)
      ELSE IF ((Units .EQ. 'msw').OR.(Units .EQ. 'MSW')) THEN
          Units_Equal_Fsw = (.FALSE.)
          Units_Equal_Msw = (.TRUE.)
      ELSE
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,901)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'              
      END IF
!      pause
      
      IF ((Altitude_Dive_Algorithm .EQ. 'ON') .OR.
     *                        (Altitude_Dive_Algorithm .EQ. 'on')) THEN
          Altitude_Dive_Algorithm_Off = (.FALSE.)
      ELSE IF ((Altitude_Dive_Algorithm .EQ. 'OFF') .OR.
     *                       (Altitude_Dive_Algorithm .EQ. 'off')) THEN
          Altitude_Dive_Algorithm_Off = (.TRUE.)
      ELSE
          WRITE (*,902)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'          
      END IF      

      IF ((Critical_Radius_N2_Microns .LT. 0.2) .OR.
     *    (Critical_Radius_N2_Microns .GT. 1.35)) THEN      
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,903)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'          
      END IF
!      pause
      IF ((Critical_Radius_He_Microns .LT. 0.2) .OR.
     *    (Critical_Radius_He_Microns .GT. 1.35)) THEN      
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,903)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'      
      END IF
!      pause
      IF ((Critical_Volume_Algorithm .EQ. 'ON').OR.
     *                      (Critical_Volume_Algorithm .EQ. 'on')) THEN
          Critical_Volume_Algorithm_Off = (.FALSE.)
      ELSE IF ((Critical_Volume_Algorithm .EQ. 'OFF').OR.
     *                      (Critical_Volume_Algorithm .EQ. 'off')) THEN
          Critical_Volume_Algorithm_Off = (.TRUE.)
      ELSE
          WRITE (*,904)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'          
      END IF  
!      pause
C===============================================================================
C     INITIALIZE CONSTANTS/VARIABLES BASED ON SELECTION OF UNITS - FSW OR MSW
C     fsw = feet of seawater, a unit of pressure
C     msw = meters of seawater, a unit of pressure
C===============================================================================
      IF (Units_Equal_Fsw) THEN
          WRITE (*,800)
          Units_Word1 = 'fswg'
          Units_Word2 = 'fsw/min'
          Units_Factor = 33.0
          Water_Vapor_Pressure = 1.607     !based on respiratory quotient of 0.8   
                                           !(Schreiner value)
      END IF
      IF (Units_Equal_Msw) THEN
          WRITE (*,801)
          Units_Word1 = 'mswg'
          Units_Word2 = 'msw/min'
          Units_Factor = 10.1325
          Water_Vapor_Pressure = 0.493     !based on respiratory quotient of 0.8
      END IF                               !(Schreiner value)
C===============================================================================
C     INITIALIZE CONSTANTS/VARIABLES
C===============================================================================
      Constant_Pressure_Other_Gases = (Pressure_Other_Gases_mmHg/760.0)
     *                                 * Units_Factor
!      write(6,*) 'Cons_Pres_Other_Gases',
!     *    1.0000*Constant_Pressure_Other_Gases,Units_Factor
!      pause
      Run_Time = 0.0
      Segment_Number = 0


      DO I = 1,16
           Helium_Time_Constant(I) = ALOG(2.0)/Helium_Half_Time(I)
           Nitrogen_Time_Constant(I) = ALOG(2.0)/Nitrogen_Half_Time(I)
           Max_Crushing_Pressure_He(I) = 0.0
           Max_Crushing_Pressure_N2(I) = 0.0
           Max_Actual_Gradient(I) = 0.0
           Surface_Phase_Volume_Time(I) = 0.0
           Amb_Pressure_Onset_of_Imperm(I) = 0.0
           Gas_Tension_Onset_of_Imperm(I) = 0.0
           Initial_Critical_Radius_N2(I) = Critical_Radius_N2_Microns
     *        * 1.0E-6
           Initial_Critical_Radius_He(I) = Critical_Radius_He_Microns
     *        * 1.0E-6
      END DO
     
C===============================================================================
C     INITIALIZE VARIABLES FOR SEA LEVEL OR ALTITUDE DIVE
C     See subroutines for explanation of altitude calculations.  Purposes are
C     1) to determine barometric pressure and 2) set or adjust the VPM critical
C     radius variables and gas loadings, as applicable, based on altitude,
C     ascent to altitude before the dive, and time at altitude before the dive 
C===============================================================================
      IF (Altitude_Dive_Algorithm_Off) THEN
          Altitude_of_Dive = 0.0
          CALL CALC_BAROMETRIC_PRESSURE (Altitude_of_Dive)           !subroutine
          WRITE (9,802) Altitude_of_Dive, Barometric_Pressure    
          DO I = 1,16
          Adjusted_Critical_Radius_N2(I) = Initial_Critical_Radius_N2(I)
          Adjusted_Critical_Radius_He(I) = Initial_Critical_Radius_He(I)
          Helium_Pressure(I) = 0.0
          Nitrogen_Pressure(I) = (Barometric_Pressure -
     *        Water_Vapor_Pressure)*0.79
          END DO
      ELSE
          CALL VPM_ALTITUDE_DIVE_ALGORITHM                           !subroutine
      END IF
C===============================================================================
C     START OF REPETITIVE DIVE LOOP
C     This is the largest loop in the main program and operates between Lines
C     30 and 330.  If there is one or more repetitive dives, the program will
C     return to this point to process each repetitive dive. 
C===============================================================================
      
30    DO 330, WHILE (.TRUE.)                   !loop will run continuously until
                                               !there is an exit statement
C===============================================================================
C     INPUT DIVE DESCRIPTION AND GAS MIX DATA FROM ASCII TEXT INPUT FILE
C     BEGIN WRITING HEADINGS/OUTPUT TO ASCII TEXT OUTPUT FILE 
C     See separate explanation of format for input file.  
C===============================================================================
      
      READ (7,805) Line1                                          
!      CALL CLOCK (Year, Month, Day, Clock_Hour, Minute, M)           !subroutine
      WRITE (9,811)
      WRITE (9,812)
      WRITE (9,813)
      WRITE (9,813)
      WRITE (9,814) Month, Day, Year, Clock_Hour, Minute, M   
      WRITE (9,813)
      WRITE (9,815) Line1
      WRITE (9,813)

      READ (7,*) Number_of_Mixes                   !check for errors in gasmixes
      
      DO I = 1, Number_of_Mixes                    
          READ (7,*) Fraction_Oxygen(I), Fraction_Helium(I),
     *               Fraction_Nitrogen(I)
          Sum_of_Fractions = Fraction_Oxygen(I) + Fraction_Helium(I) +
     *                       Fraction_Nitrogen(I)
          Sum_Check = Sum_of_Fractions
          IF (Sum_Check .NE. 1.0) THEN
              CALL SYSTEMQQ (OS_Command)
              WRITE (*,906)
              WRITE (*,900)    
              STOP 'PROGRAM TERMINATED'          
          END IF
      END DO
      
!      pause
      WRITE (9,820)
      
      DO J = 1, Number_of_Mixes
          WRITE (9,821) J, Fraction_Oxygen(J), Fraction_Helium(J),
     *                     Fraction_Nitrogen(J)
      END DO
      WRITE (9,813)
      WRITE (9,813)
      WRITE (9,830)
      WRITE (9,813)
      WRITE (9,831)
      WRITE (9,832)
      WRITE (9,833) Units_Word1, Units_Word1, Units_Word2, Units_Word1
      WRITE (9,834)
!      pause
C===============================================================================
C     DIVE PROFILE LOOP - INPUT DIVE PROFILE DATA FROM ASCII TEXT INPUT FILE
C     AND PROCESS DIVE AS A SERIES OF ASCENT/DESCENT AND CONSTANT DEPTH
C     SEGMENTS.  THIS ALLOWS FOR MULTI-LEVEL DIVES AND UNUSUAL PROFILES.  UPDATE
C     GAS LOADINGS FOR EACH SEGMENT.  IF IT IS A DESCENT SEGMENT, CALC CRUSHING
C     PRESSURE ON CRITICAL RADII IN EACH COMPARTMENT.
C     "Instantaneous" descents are not used in the VPM.  All ascent/descent
C     segments must have a realistic rate of ascent/descent.  Unlike Haldanian
C     models, the VPM is actually more conservative when the descent rate is
C     slower becuase the effective crushing pressure is reduced.  Also, a
C     realistic actual supersaturation gradient must be calculated during
C     ascents as this affects critical radii adjustments for repetitive dives.
C     Profile codes: 1 = Ascent/Descent, 2 = Constant Depth, 99 = Decompress
C===============================================================================
       
      DO WHILE (.TRUE.)                        !loop will run continuously until
                                               !there is an exit statement
      READ (7,*) Profile_Code
      IF (Profile_Code .EQ. 1) THEN
          READ (7,*) Starting_Depth, Ending_Depth, 
     *               Rate, Mix_Number    
          Starting_Depth=Starting_Depth/1.0000            
          Ending_Depth=Ending_Depth/1.0000                
          Rate=Rate/1.0000                                          
          
!  raccolta valori iniziali per la grafica
          kk=1
          x(kk)=Run_Time
          y(kk)=1.0000*Starting_Depth

          do I=1,16
              zN2(kk,I)=1.0000*Nitrogen_Pressure(I)
              zHe(kk,I)=1.0000*Helium_Pressure(I)
          end do
! fine raccolta valori iniziali  
          
          CALL GAS_LOADINGS_ASCENT_DESCENT (Starting_Depth,          !subroutine
     *                                      Ending_Depth, Rate)

!  raccolta valori per la grafica
          kk=kk+1
          x(kk)=Run_Time
          y(kk)=1.0000*Ending_Depth

                  
          do I=1,16
              zN2(kk,I)=1.0000*Nitrogen_Pressure(I)
              zHe(kk,I)=1.0000*Helium_Pressure(I)
          end do
! fine raccolta valori          
          
          IF (Ending_Depth .GT. Starting_Depth) THEN
              CALL CALC_CRUSHING_PRESSURE (Starting_Depth,           !subroutine
     *                                     Ending_Depth, Rate)
          END IF
          IF (Ending_Depth .GT. Starting_Depth) THEN
              Word = 'Descent'
          ELSE IF (Starting_Depth .GT. Ending_Depth) THEN
              Word = 'Ascent '
          ELSE
              Word = 'ERROR'          
          END IF
          WRITE (9,840) Segment_Number, Segment_Time, Run_Time,
     *                  Mix_Number, Word, 1.0000*Starting_Depth, 
     *    1.0000*Ending_Depth,              1.0000*Rate
      ELSE IF (Profile_Code .EQ. 2) THEN
          READ (7,*) Depth, Run_Time_End_of_Segment, Mix_Number 
           Depth=Depth/1.0000
          CALL GAS_LOADINGS_CONSTANT_DEPTH (Depth,                   !subroutine
     *                                      Run_Time_End_of_Segment)
          WRITE (9,845) Segment_Number, Segment_Time, Run_Time,
     *                  Mix_Number, 1.0000*Depth

!  raccolta valori per la grafica
          kk=kk+1
          x(kk)=Run_Time_End_of_Segment
          y(kk)=1.0000*Depth

          do I=1,16
              zN2(kk,I)=1.0000*Nitrogen_Pressure(I)
              zHe(kk,I)=1.0000*Helium_Pressure(I)
          end do
! fine raccolta valori
                                               
          ELSE IF (Profile_Code .EQ. 99) THEN
          EXIT
      ELSE
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,907)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'          
      END IF 
      END DO
!      pause
C===============================================================================
C     BEGIN PROCESS OF ASCENT AND DECOMPRESSION
C     First, calculate the regeneration of critical radii that takes place over
C     the dive time.  The regeneration time constant has a time scale of weeks
C     so this will have very little impact on dives of normal length, but will
C     have major impact for saturation dives.       
C===============================================================================
      CALL NUCLEAR_REGENERATION (Run_Time)                           !subroutine

C===============================================================================
C     CALCULATE INITIAL ALLOWABLE GRADIENTS FOR ASCENT
C     This is based on the maximum effective crushing pressure on critical radii
C     in each compartment achieved during the dive profile.   
C===============================================================================
      CALL CALC_INITIAL_ALLOWABLE_GRADIENT                           !subroutine

C===============================================================================
C     SAVE VARIABLES AT START OF ASCENT (END OF BOTTOM TIME) SINCE THESE WILL
C     BE USED LATER TO COMPUTE THE FINAL ASCENT PROFILE THAT IS WRITTEN TO THE
C     OUTPUT FILE.
C     The VPM uses an iterative process to compute decompression schedules so
C     there will be more than one pass through the decompression loop.
C===============================================================================

      write(8,*) 'AT START OF ASCENT (END OF BOTTOM TIME)'
      
          write(8,*) 'Nitrogen_Pressure(I)           Helium_Pressure(I)'
          
          do i=1,16
      write(8,31) 1.0000*Nitrogen_Pressure(I),1.0000*Helium_Pressure(I) 
              end do
31            format(f10.3,20x,f10.3,25x,I3) 
          
      DO I = 1,16
          He_Pressure_Start_of_Ascent(I) = Helium_Pressure(I)
          N2_Pressure_Start_of_Ascent(I) = Nitrogen_Pressure(I)

      END DO
     
          
      Run_Time_Start_of_Ascent = Run_Time
      Segment_Number_Start_of_Ascent = Segment_Number
C===============================================================================
C     INPUT PARAMETERS TO BE USED FOR STAGED DECOMPRESSION AND SAVE IN ARRAYS.
C     ASSIGN INITAL PARAMETERS TO BE USED AT START OF ASCENT
C     The user has the ability to change mix, ascent rate, and step size in any
C     combination at any depth during the ascent.
C===============================================================================
      READ (7,*) Number_of_Changes
      DO I = 1, Number_of_Changes    
          READ (7,*) Depth_Change(I), Mix_Change(I), Rate_Change(I),
     *               Step_Size_Change(I)
           Depth_Change(I)=Depth_Change(I)           
           Rate_Change(I)=Rate_Change(I)/1.0000                
           Step_Size_Change(I)=Step_Size_Change(I)/1.0000     
      END DO
      Starting_Depth = Depth_Change(1)
      Mix_Number = Mix_Change(1)
      _update_bo_effective(Mix_Number)
      Rate = Rate_Change(1)
      Step_Size = Step_Size_Change(1)

C===============================================================================
C     CALCULATE THE DEPTH WHERE THE DECOMPRESSION ZONE BEGINS FOR THIS PROFILE
C     BASED ON THE INITIAL ASCENT PARAMETERS AND WRITE THE DEEPEST POSSIBLE
C     DECOMPRESSION STOP DEPTH TO THE OUTPUT FILE
C     Knowing where the decompression zone starts is very important.  Below
C     that depth there is no possibility for bubble formation because there
C     will be no supersaturation gradients.  Deco stops should never start
C     below the deco zone.  The deepest possible stop deco stop depth is
C     defined as the next "standard" stop depth above the point where the
C     leading compartment enters the deco zone.  Thus, the program will not
C     base this calculation on step sizes larger than 10 fsw or 3 msw.  The
C     deepest possible stop depth is not used in the program, per se, rather
C     it is information to tell the diver where to start putting on the brakes
C     during ascent.  This should be prominently displayed by any deco program.
C===============================================================================
      CALL CALC_START_OF_DECO_ZONE (Starting_Depth, Rate,            !subroutine
     *                              Depth_Start_of_Deco_Zone)
     
      
      
      IF (Units_Equal_Fsw) THEN
          IF (Step_Size .LT. 10.0) THEN
              Rounding_Operation1 =
     *        (Depth_Start_of_Deco_Zone/Step_Size) - 0.5   
              Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1)
     *        * Step_Size
          ELSE
              Rounding_Operation1 = (Depth_Start_of_Deco_Zone/10.0)
     *         - 0.5
              Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1)
     *        * 10.0
          END IF
      END IF
      IF (Units_Equal_Msw) THEN
          IF (Step_Size .LT. 3.0) THEN
              Rounding_Operation1 =
     *        (Depth_Start_of_Deco_Zone/Step_Size) - 0.5   
              Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1)
     *        * Step_Size
          ELSE
              Rounding_Operation1 = (Depth_Start_of_Deco_Zone/3.0)
     *         - 0.5
              Deepest_Possible_Stop_Depth = ANINT(Rounding_Operation1)
     *        * 3.0
          END IF
          
      END IF
      

      WRITE (9,813)
      WRITE (9,813)
      WRITE (9,850)
      WRITE (9,813)
      WRITE (9,857) 1.0000*Depth_Start_of_Deco_Zone, Units_Word1
      WRITE (9,858) 1.0000*Deepest_Possible_Stop_Depth, Units_Word1
      WRITE (9,813)
      WRITE (9,851)
      WRITE (9,852)
      WRITE (9,853) Units_Word1, Units_Word2, Units_Word1
      WRITE (9,854)
C===============================================================================
C     TEMPORARILY ASCEND PROFILE TO THE START OF THE DECOMPRESSION ZONE, SAVE
C     VARIABLES AT THIS POINT, AND INITIALIZE VARIABLES FOR CRITICAL VOLUME LOOP
C     The iterative process of the VPM Critical Volume Algorithm will operate
C     only in the decompression zone since it deals with excess gas volume
C     released as a result of supersaturation gradients (not possible below the
C     decompression zone).
C===============================================================================
       
      CALL GAS_LOADINGS_ASCENT_DESCENT (Starting_Depth,              !subroutine
     *                                  Depth_Start_of_Deco_Zone, Rate)
      
!  raccolta valori per la grafica
          kk=kk+1
          x(kk)=Run_Time
          y(kk)=1.0000*Depth_Start_of_Deco_Zone

          do I=1,16
              zN2(kk,I)=1.0000*Nitrogen_Pressure(I)
              zHe(kk,I)=1.0000*Helium_Pressure(I)
          end do
! fine raccolta valori 
          
      write(8,*) 'AT Start_of_Deco_Zone'
      write(8,*) 'Run_Time',Run_Time
      
      write(8,*) 'Depth_Start_of_Deco_Zone',
     *        1.0000*Depth_Start_of_Deco_Zone
      
      do I=1,16
          write(8,*) 'Nitrogen_Press(I):',
     *     1.0000*Nitrogen_Pressure(I) ,     
     *  'Helium_Press(I):',
     *     1.0000*Helium_Pressure(I),I 
          end do
      write(8,*) ' '
          
      Run_Time_Start_of_Deco_Zone = Run_Time
      Deco_Phase_Volume_Time = 0.0
      Last_Run_Time = 0.0
      Schedule_Converged = (.FALSE.)
      DO I = 1,16
          Last_Phase_Volume_Time(I) = 0.0
          He_Pressure_Start_of_Deco_Zone(I) = Helium_Pressure(I)
          N2_Pressure_Start_of_Deco_Zone(I) = Nitrogen_Pressure(I)
          Max_Actual_Gradient(I) = 0.0
      END DO
C===============================================================================
C     START OF CRITICAL VOLUME LOOP
C     This loop operates between Lines 50 and 100.  If the Critical Volume
C     Algorithm is toggled "off" in the program settings, there will only be
C     one pass through this loop.  Otherwise, there will be two or more passes
C     through this loop until the deco schedule is "converged" - that is when a
C     comparison between the phase volume time of the present iteration and the
C     last iteration is less than or equal to one minute.  This implies that
C     the volume of released gas in the most recent iteration differs from the
C     "critical" volume limit by an acceptably small amount.  The critical
C     volume limit is set by the Critical Volume Parameter Lambda in the program
C     settings (default setting is 7500 fsw-min with adjustability range from
C     from 6500 to 8300 fsw-min according to Bruce Wienke).
C===============================================================================
50    DO 100, WHILE (.TRUE.)                   !loop will run continuously until
                                               !there is an exit statement
C===============================================================================
C     CALCULATE INITIAL ASCENT CEILING BASED ON ALLOWABLE SUPERSATURATION
C     GRADIENTS AND SET FIRST DECO STOP.  CHECK TO MAKE SURE THAT SELECTED STEP
C     SIZE WILL NOT ROUND UP FIRST STOP TO A DEPTH THAT IS BELOW THE DECO ZONE.
C===============================================================================
      CALL CALC_ASCENT_CEILING (Ascent_Ceiling_Depth)                !subroutine

      
      IF (Ascent_Ceiling_Depth .LE. 0.0) THEN
           Deco_Stop_Depth = 0.0
      ELSE
           Rounding_Operation2 = (Ascent_Ceiling_Depth/Step_Size) + 0.5
           Deco_Stop_Depth = ANINT(Rounding_Operation2) * Step_Size
      END IF
      
!      write(6,*) 'Deco_Stop_Depth,ipilot, xx',
!     #       Deco_Stop_Depth,ipilot
!      pause
      
      IF (Deco_Stop_Depth .GT. Depth_Start_of_Deco_Zone) THEN
          WRITE (*,905)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'
      END IF
!      pause
C===============================================================================
C     PERFORM A SEPARATE "PROJECTED ASCENT" OUTSIDE OF THE MAIN PROGRAM TO MAKE
C     SURE THAT AN INCREASE IN GAS LOADINGS DURING ASCENT TO THE FIRST STOP WILL
C     NOT CAUSE A VIOLATION OF THE DECO CEILING.  IF SO, ADJUST THE FIRST STOP
C     DEEPER BASED ON STEP SIZE UNTIL A SAFE ASCENT CAN BE MADE.
C     Note: this situation is a possibility when ascending from extremely deep
C     dives or due to an unusual gas mix selection.
C     CHECK AGAIN TO MAKE SURE THAT ADJUSTED FIRST STOP WILL NOT BE BELOW THE
C     DECO ZONE. 
C===============================================================================   
  
      CALL PROJECTED_ASCENT (Depth_Start_of_Deco_Zone, Rate,         !subroutine
     *                       Deco_Stop_Depth, Step_Size)

      IF (Deco_Stop_Depth .GT. Depth_Start_of_Deco_Zone) THEN
          WRITE (*,905)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'
      END IF
C===============================================================================
C     HANDLE THE SPECIAL CASE WHEN NO DECO STOPS ARE REQUIRED - ASCENT CAN BE
C     MADE DIRECTLY TO THE SURFACE
C     Write ascent data to output file and exit the Critical Volume Loop.
C===============================================================================
      IF (Deco_Stop_Depth .EQ. 0.0) THEN
           DO I = 1,16
              Helium_Pressure(I) = He_Pressure_Start_of_Ascent(I)
              Nitrogen_Pressure(I) = N2_Pressure_Start_of_Ascent(I)
           END DO
           Run_Time = Run_Time_Start_of_Ascent
           Segment_Number = Segment_Number_Start_of_Ascent
           Starting_Depth = Depth_Change(1)
           Ending_Depth = 0.0
           CALL GAS_LOADINGS_ASCENT_DESCENT (Starting_Depth,         !subroutine
     *                                       Ending_Depth, Rate)
           WRITE (9,860) Segment_Number, Segment_Time, Run_Time,
     *                   Mix_Number, 1.0000*Deco_Stop_Depth,1.0000*Rate

           EXIT                       !exit the critical volume loop at Line 100
      END IF      
C===============================================================================
C     ASSIGN VARIABLES FOR ASCENT FROM START OF DECO ZONE TO FIRST STOP.  SAVE
C     FIRST STOP DEPTH FOR LATER USE WHEN COMPUTING THE FINAL ASCENT PROFILE
C===============================================================================
      Starting_Depth = Depth_Start_of_Deco_Zone
      First_Stop_Depth = Deco_Stop_Depth
      
            write(8,*) 'First_Stop_Depth',
     *     1.0000*First_Stop_Depth  
 
C===============================================================================
C     DECO STOP LOOP BLOCK WITHIN CRITICAL VOLUME LOOP
C     This loop computes a decompression schedule to the surface during each
C     iteration of the critical volume loop.  No output is written from this
C     loop, rather it computes a schedule from which the in-water portion of the
C     total phase volume time (Deco_Phase_Volume_Time) can be extracted.  Also,
C     the gas loadings computed at the end of this loop are used the subroutine
C     which computes the out-of-water portion of the total phase volume time
C     (Surface_Phase_Volume_Time) for that schedule.
C
C     Note that exit is made from the loop after last ascent is made to a deco
C     stop depth that is less than or equal to zero.  A final deco stop less
C     than zero can happen when the user makes an odd step size change during
C     ascent - such as specifying a 5 msw step size change at the 3 msw stop! 
C===============================================================================
      write(8,*) ' '
      DO WHILE (.TRUE.)                        !loop will run continuously until
                                               !there is an exit statement
C            write(8,*) 
C     *     1.0000*Starting_Depth,1.0000*Nitrogen_Pressure(1),Run_Time 
                  
                  
          CALL GAS_LOADINGS_ASCENT_DESCENT (Starting_Depth,          !subroutine
     *                                      Deco_Stop_Depth, Rate)

          write(8,*) 'Depth inizio stop',
     *      1.0000*Deco_Stop_Depth,'Time',Run_Time
          write(8,*) ' Nitrogen_Pressure(I)'
          write(8,*) (1.0000*Nitrogen_Pressure(I),I=1,16)
          write(8,*) ' '
          write(8,*) ' Helium_Pressure(I):'
          write(8,*) (1.0000*Helium_Pressure(I),I=1,16)
          write(8,*) '____________________________________________ '            
            
          IF (Deco_Stop_Depth .LE. 0.0) EXIT                    !exit at Line 60
          IF (Number_of_Changes .GT. 1) THEN
              DO I = 2, Number_of_Changes
                  IF (Depth_Change(I) .GE. Deco_Stop_Depth) THEN
                      Mix_Number = Mix_Change(I)
                      _update_bo_effective(Mix_Number)
                      Rate = Rate_Change(I)
                      Step_Size = Step_Size_Change(I)
                  END IF
              END DO
          END IF

          CALL BOYLES_LAW_COMPENSATION (First_Stop_Depth,
     *                             Deco_Stop_Depth, Step_Size)       !subroutine 

!            write(8,*) 
!     *     1.0000*Deco_Stop_Depth,1.0000*Nitrogen_Pressure(1),Run_Time
            
            CALL DECOMPRESSION_STOP (Deco_Stop_Depth, Step_Size)       !subroutine
            
          write(8,*) 'Depth fine stop',
     *      1.0000*Deco_Stop_Depth,'Time',Run_Time
          write(8,*) ' Nitrogen_Pressure(I):'
          write(8,*) (1.0000*Nitrogen_Pressure(I),I=1,16)
          write(8,*) ' '
          write(8,*) ' Helium_Pressure(I):'
          write(8,*) (1.0000*Helium_Pressure(I),I=1,16)
          write(8,*) '____________________________________________ '
                        
          Starting_Depth = Deco_Stop_Depth
          Next_Stop = Deco_Stop_Depth - Step_Size
          Deco_Stop_Depth = Next_Stop
          Last_Run_Time = Run_Time
60        END DO                                        !end of deco stop loop block
      
          
!                        write(8,*) 
!     *     'Helium_Pressure(I),     Nitrogen_Pressure(I)'
                                                
!          do j=1,16
!           write(8,12) 
!     *     1.0000*Helium_Pressure(j),1.0000*Nitrogen_Pressure(j),j 
!          end do
12      format(f10.3,20x,f10.3,i10)
C===============================================================================
C     COMPUTE TOTAL PHASE VOLUME TIME AND MAKE CRITICAL VOLUME COMPARISON
C     The deco phase volume time is computed from the run time.  The surface
C     phase volume time is computed in a subroutine based on the surfacing gas
C     loadings from previous deco loop block.  Next the total phase volume time
C     (in-water + surface) for each compartment is compared against the previous
C     total phase volume time.  The schedule is converged when the difference is
C     less than or equal to 1 minute in any one of the 16 compartments.
C
C     Note:  the "phase volume time" is somewhat of a mathematical concept.
C     It is the time divided out of a total integration of supersaturation
C     gradient x time (in-water and surface).  This integration is multiplied
C     by the excess bubble number to represent the amount of free-gas released
C     as a result of allowing a certain number of excess bubbles to form. 
C===============================================================================
      Deco_Phase_Volume_Time = Run_Time - Run_Time_Start_of_Deco_Zone
      CALL CALC_SURFACE_PHASE_VOLUME_TIME                            !subroutine
      write(8,55) 
55    format('Deco_Phase_Volume_Time',1x,'Surface_Phase_Volume_Time(I)'
     *      ,1x,'Phase_Volume_Time(I)')
      CALL CALC_SURFACE_PHASE_VOLUME_TIME                            !subroutine

      DO I = 1,16
          Phase_Volume_Time(I) = Deco_Phase_Volume_Time +
     *                           Surface_Phase_Volume_Time(I)
          Critical_Volume_Comparison = ABS(Phase_Volume_Time(I) -
     *                                 Last_Phase_Volume_Time(I))
          
      write(8,33)  Deco_Phase_Volume_Time,Surface_Phase_Volume_Time(I),
     *  Phase_Volume_Time(I),I
 33   format(3(f10.3,15x),i5)
     
      IF (Critical_Volume_Comparison .LE. 1.0) THEN
              Schedule_Converged = (.TRUE.)
          END IF
      END DO
C===============================================================================
C     CRITICAL VOLUME DECISION TREE BETWEEN LINES 70 AND 99
C     There are two options here.  If the Critical Volume Agorithm setting is
C     "on" and the schedule is converged, or the Critical Volume Algorithm
C     setting was "off" in the first place, the program will re-assign variables
C     to their values at the start of ascent (end of bottom time) and process
C     a complete decompression schedule once again using all the same ascent
C     parameters and first stop depth.  This decompression schedule will match
C     the last iteration of the Critical Volume Loop and the program will write
C     the final deco schedule to the output file.
C
C     Note: if the Critical Volume Agorithm setting was "off", the final deco
C     schedule will be based on "Initial Allowable Supersaturation Gradients."
C     If it was "on", the final schedule will be based on "Adjusted Allowable
C     Supersaturation Gradients" (gradients that are "relaxed" as a result of
C     the Critical Volume Algorithm).
C
C     If the Critical Volume Agorithm setting is "on" and the schedule is not
C     converged, the program will re-assign variables to their values at the
C     start of the deco zone and process another trial decompression schedule.  
C===============================================================================
70    IF ((Schedule_Converged) .OR. 
     *                    (Critical_Volume_Algorithm_Off)) THEN
          DO I = 1,16
              Helium_Pressure(I) = He_Pressure_Start_of_Ascent(I)
              Nitrogen_Pressure(I) = N2_Pressure_Start_of_Ascent(I)
          END DO
          Run_Time = Run_Time_Start_of_Ascent
          Segment_Number = Segment_Number_Start_of_Ascent
          Starting_Depth = Depth_Change(1)
          Mix_Number = Mix_Change(1)
          _update_bo_effective(Mix_Number)
          Rate = Rate_Change(1)
          Step_Size = Step_Size_Change(1)
          Deco_Stop_Depth = First_Stop_Depth
          Last_Run_Time = 0.0
C===============================================================================
C     DECO STOP LOOP BLOCK FOR FINAL DECOMPRESSION SCHEDULE
C===============================================================================
      write(8,*) '*************************************************'
      write(8,*) 'FINAL DECOMPRESSION SCHEDULE'
      write(8,*) '*************************************************' 
      jmax=0          
      DO WHILE (.TRUE.)                    !loop will run continuously until
                                               !there is an exit statement

              CALL GAS_LOADINGS_ASCENT_DESCENT (Starting_Depth,      !subroutine
     *                                          Deco_Stop_Depth, Rate)
          write(8,*) 'Depth inizio stop',
     *      1.0000*Deco_Stop_Depth,'Time',Run_Time
          write(8,*) ' Nitrogen_Pressure(I)'
          write(8,*) (1.0000*Nitrogen_Pressure(I),I=1,16)
          write(8,*) ' '
          write(8,*) ' Helium_Pressure(I):'
          write(8,*) (1.0000*Helium_Pressure(I),I=1,16)
          write(8,*) '____________________________________________ '            
          
!  raccolta valori inizio stop per la grafica
          kk=kk+1
          x(kk)=Run_Time
          y(kk)=1.0000*Deco_Stop_Depth

          do I=1,16
              zN2(kk,I)=1.0000*Nitrogen_Pressure(I)
              zHe(kk,I)=1.0000*Helium_Pressure(I)
          end do
! fine raccolta valori inizio stop
          
C===============================================================================
C     DURING FINAL DECOMPRESSION SCHEDULE PROCESS, COMPUTE MAXIMUM ACTUAL
C     SUPERSATURATION GRADIENT RESULTING IN EACH COMPARTMENT
C     If there is a repetitive dive, this will be used later in the VPM
C     Repetitive Algorithm to adjust the values for critical radii.
C===============================================================================
              CALL CALC_MAX_ACTUAL_GRADIENT (Deco_Stop_Depth)        !subroutine

              WRITE (9,860) Segment_Number, Segment_Time, Run_Time,
     *                Mix_Number, 1.0000*Deco_Stop_Depth, 1.0000*Rate
              IF (Deco_Stop_Depth .LE. 0.0) EXIT                !exit at Line 80
              IF (Number_of_Changes .GT. 1) THEN
                  DO I = 2, Number_of_Changes
                      IF (Depth_Change(I) .GE. Deco_Stop_Depth) THEN
                          Mix_Number = Mix_Change(I)
                          _update_bo_effective(Mix_Number)
                          Rate = Rate_Change(I)
                          Step_Size = Step_Size_Change(I)
                      END IF
                  END DO
              END IF

          CALL BOYLES_LAW_COMPENSATION (First_Stop_Depth,
     *                             Deco_Stop_Depth, Step_Size)       !subroutine 

              CALL DECOMPRESSION_STOP (Deco_Stop_Depth, Step_Size)   !subroutine
              
!          write(9,*) 'Deco_Stop_Depth,ipilot,yy',
!     #       Deco_Stop_Depth,ipilot
!      pause
          write(8,*) 'Depth fine stop',
     *      1.0000*Deco_Stop_Depth,'Time',Run_Time
          write(8,*) ' Nitrogen_Pressure(I):'
          write(8,*) (1.0000*Nitrogen_Pressure(I),I=1,16)
          write(8,*) ' '
          write(8,*) ' Helium_Pressure(I):'
          write(8,*) (1.0000*Helium_Pressure(I),I=1,16)
          write(8,*) '____________________________________________ '

!  raccolta valori fine stop per la grafica
          kk=kk+1
          x(kk)=Run_Time
          y(kk)=1.0000*Deco_Stop_Depth

          do I=1,16
              zN2(kk,I)=1.0000*Nitrogen_Pressure(I)
              zHe(kk,I)=1.0000*Helium_Pressure(I)
          end do
! fine raccolta valori fine stop
          
C===============================================================================
C     This next bit justs rounds up the stop time at the first stop to be in
C     whole increments of the minimum stop time (to make for a nice deco table).
C===============================================================================
              IF (Last_Run_Time .EQ. 0.0) THEN
                   Stop_Time =
     *             ANINT((Segment_Time/Minimum_Deco_Stop_Time) + 0.5) *
     *                    Minimum_Deco_Stop_Time
              ELSE
                   Stop_Time = Run_Time - Last_Run_Time
              END IF
C===============================================================================
C     DURING FINAL DECOMPRESSION SCHEDULE, IF MINIMUM STOP TIME PARAMETER IS A
C     WHOLE NUMBER (i.e. 1 minute) THEN WRITE DECO SCHEDULE USING INTEGER
C     NUMBERS (looks nicer).  OTHERWISE, USE DECIMAL NUMBERS.
C     Note: per the request of a noted exploration diver(!), program now allows
C     a minimum stop time of less than one minute so that total ascent time can
C     be minimized on very long dives.  In fact, with step size set at 1 fsw or
C     0.2 msw and minimum stop time set at 0.1 minute (6 seconds), a near
C     continuous decompression schedule can be computed.  
C===============================================================================

              
              IF (AINT(Minimum_Deco_Stop_Time) .EQ.
     *                                     Minimum_Deco_Stop_Time) THEN
      
      jmax(ipilot)=jmax(ipilot)+1
      jj=jmax(ipilot)
      DCD(jj,ipilot)=Deco_Stop_Depth
      
!      write(6,*) 'DCD(jj,ipilot),jj,ipilo',DCD(jj,ipilot),jj,ipilot                  
!      pause            
                  
                  
                  WRITE (9,862) Segment_Number, Segment_Time, Run_Time,
     *                Mix_Number,ipilot, INT(1.0000*Deco_Stop_Depth),
     *                      INT(Stop_Time), INT(Run_Time)
              ELSE
                  WRITE (9,863) Segment_Number, Segment_Time, Run_Time,
     *                Mix_Number, 1.0000*Deco_Stop_Depth, Stop_Time,
     *                      Run_Time
              END IF
              Starting_Depth = Deco_Stop_Depth
              Next_Stop = Deco_Stop_Depth - Step_Size
              Deco_Stop_Depth = Next_Stop
              Last_Run_Time = Run_Time
80        END DO                                    !end of deco stop loop block
                                                    !for final deco schedule

          EXIT                            !exit critical volume loop at Line 100  
                                                    !final deco schedule written
      ELSE            
C===============================================================================
C     IF SCHEDULE NOT CONVERGED, COMPUTE RELAXED ALLOWABLE SUPERSATURATION
C     GRADIENTS WITH VPM CRITICAL VOLUME ALGORITHM AND PROCESS ANOTHER
C     ITERATION OF THE CRITICAL VOLUME LOOP
C===============================================================================
          CALL CRITICAL_VOLUME (Deco_Phase_Volume_Time)              !subroutine
          Deco_Phase_Volume_Time = 0.0
          Run_Time = Run_Time_Start_of_Deco_Zone
          Starting_Depth = Depth_Start_of_Deco_Zone
          Mix_Number = Mix_Change(1)
          _update_bo_effective(Mix_Number)
          Rate = Rate_Change(1)
          Step_Size = Step_Size_Change(1)
          DO I = 1,16
              Last_Phase_Volume_Time(I) = Phase_Volume_Time(I)
              Helium_Pressure(I) = He_Pressure_Start_of_Deco_Zone(I)
              Nitrogen_Pressure(I) = N2_Pressure_Start_of_Deco_Zone(I)
          END DO

          CYCLE                         !Return to start of critical volume loop
                                        !(Line 50) to process another iteration

99    END IF                               !end of critical volume decision tree

100   CONTINUE                                      !end of critical volume loop
C===============================================================================
C     PROCESSING OF DIVE COMPLETE.  READ INPUT FILE TO DETERMINE IF THERE IS A
C     REPETITIVE DIVE.  IF NONE, THEN EXIT REPETITIVE LOOP.
C===============================================================================
      READ (7,*) Repetitive_Dive_Flag
      IF (Repetitive_Dive_Flag .EQ. 0) THEN
          
          EXIT                                        !exit repetitive dive loop 
                                                      !at Line 330
C===============================================================================
C     IF THERE IS A REPETITIVE DIVE, COMPUTE GAS LOADINGS (OFF-GASSING) DURING
C     SURFACE INTERVAL TIME.  ADJUST CRITICAL RADII USING VPM REPETITIVE
C     ALGORITHM.  RE-INITIALIZE SELECTED VARIABLES AND RETURN TO START OF
C     REPETITIVE LOOP AT LINE 30.
C===============================================================================
      ELSE IF (Repetitive_Dive_Flag .EQ. 1) THEN
          READ (7,*) Surface_Interval_Time
!      pause
          CALL GAS_LOADINGS_SURFACE_INTERVAL (Surface_Interval_Time) !subroutine

          CALL VPM_REPETITIVE_ALGORITHM (Surface_Interval_Time)      !subroutine

          DO I = 1,16
              Max_Crushing_Pressure_He(I) = 0.0
              Max_Crushing_Pressure_N2(I) = 0.0
              Max_Actual_Gradient(I) = 0.0
          END DO
          Run_Time = 0.0
          Segment_Number = 0
          WRITE (9,880)
          WRITE (9,890)
          WRITE (9,813)

          CYCLE      !Return to start of repetitive loop to process another dive
C===============================================================================
C     WRITE ERROR MESSAGE AND TERMINATE PROGRAM IF THERE IS AN ERROR IN THE
C     INPUT FILE FOR THE REPETITIVE DIVE FLAG
C===============================================================================
      ELSE
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,908)
          WRITE (*,900)    
          STOP 'PROGRAM TERMINATED'   
      END IF       
330   CONTINUE                                           !End of repetitive loop
C===============================================================================
C     FINAL WRITES TO OUTPUT AND CLOSE PROGRAM FILES
C===============================================================================
      WRITE (*,813)
      WRITE (*,871)
      WRITE (*,872)
      WRITE (*,813)
      WRITE (9,880)
      CLOSE (UNIT = 7, STATUS = 'KEEP')
      CLOSE (UNIT = 8, STATUS = 'KEEP')
      CLOSE (UNIT = 9, STATUS = 'KEEP')
      CLOSE (UNIT = 10, STATUS = 'KEEP')
      
C===============================================================================
C     FORMAT STATEMENTS - PROGRAM INPUT/OUTPUT
C===============================================================================
800   FORMAT ('0UNITS = FEET OF SEAWATER (FSW)')
801   FORMAT ('0UNITS = METERS OF SEAWATER (MSW)')
802   FORMAT ('0ALTITUDE = ',1X,F7.1,4X,'BAROMETRIC PRESSURE = ',
     *F6.3)
805   FORMAT (A70)
811   FORMAT (26X,'DECOMPRESSION CALCULATION PROGRAM')
812   FORMAT (24X,'Developed in FORTRAN by Erik C. Baker')
814   FORMAT ('Program Run:',4X,I2.2,'-',I2.2,'-',I4,1X,'at',1X,I2.2,
     *        ':',I2.2,1X,A1,'m',23X,'Model: VPM-B')
815   FORMAT ('Description:',4X,A70)
813   FORMAT (' ')
820   FORMAT ('Gasmix Summary:',24X,'FO2',4X,'FHe',4X,'FN2')
821   FORMAT (26X,'Gasmix #',I2,2X,F5.2,2X,F5.2,2X,F5.2)
830   FORMAT (36X,'DIVE PROFILE')
831   FORMAT ('Seg-',2X,'Segm.',2X,'Run',3X,'|',1X,'Gasmix',1X,'|',1X,
     *       'Ascent',4X,'From',5X,'To',6X,'Rate',4X,'|',1X,'Constant')
832   FORMAT ('ment',2X,'Time',3X,'Time',2X,'|',2X,'Used',2X,'|',3X,
     *        'or',5X,'Depth',3X,'Depth',4X,'+Dn/-Up',2X,'|',2X,'Depth')
833   FORMAT (2X,'#',3X,'(min)',2X,'(min)',1X,'|',4X,'#',3X,'|',1X,
     *        'Descent',2X,'(',A4,')',2X,'(',A4,')',2X,'(',A7,')',1X,
     *        '|',2X,'(',A4,')')
834   FORMAT ('-----',1X,'-----',2X,'-----',1X,'|',1X,'------',1X,'|',
     *        1X,'-------',2X,'------',2X,'------',2X,'---------',1X,
     *        '|',1X,'--------')
840   FORMAT (I3,3X,F5.1,1X,F6.1,1X,'|',3X,I2,3X,'|',1X,A7,F7.0,
     *            1X,F7.0,3X,F7.1,3X,'|')   
845   FORMAT (I3,3X,F5.1,1X,F6.1,1X,'|',3X,I2,3X,'|',36X,'|',F7.0)
850   FORMAT (31X,'DECOMPRESSION PROFILE')
851   FORMAT ('Seg-',2X,'Segm.',2X,'Run',3X,'|',1X,'Gasmix',1X,'|',1X,
     *        'Ascent',3X,'Ascent',3X,'Pilot',1X,'|',2X,'DECO',3X,'STOP',
     *        3X,'RUN')
852   FORMAT ('ment',2X,'Time',3X,'Time',2X,'|',2X,'Used',2X,'|',3X,
     *        'To',6X,'Rate',4X,'Tess.',1X,'|',2X,'STOP',3X,'TIME',3X,
     *         'TIME')
853   FORMAT (2X,'#',3X,'(min)',2X,'(min)',1X,'|',4X,'#',3X,'|',1X,
     *        '(',A4,')',1X,'(',A7,')',2X,'  # ',2X,'|',1X,'(',A4,')',
     *        2X,'(min)',2X,'(min)')
854   FORMAT ('-----',1X,'-----',2X,'-----',1X,'|',1X,'------',1X,'|',
     *        1X,'------',1X,'---------',1X,'------',1X,'|',1X,
     *        '------',2X,'-----',2X,'-----')
857   FORMAT (10X,'Leading compartment enters the decompression zone',
     *        1X,'at',F7.1,1X,A4)
858   FORMAT (17X,'Deepest possible decompression stop is',F7.1,1X,A4)  
860   FORMAT (I3,3X,F5.1,1X,F6.1,1X,'|',3X,I2,3X,'|',2X,F4.0,3X,F6.1,
     *        10X,'|')
862   FORMAT (I3,3X,F5.1,1X,F6.1,1X,'|',3X,I2,3X,'|',20X,i2,3x,'|',
     *        2X,I4,3X,I4,2X,I5)
863   FORMAT (I3,3X,F5.1,1X,F6.1,1X,'|',3X,I2,3X,'|',25X,'|',2X,F5.0,1X,
     *        F6.1,1X,F7.1)
871   FORMAT (' PROGRAM CALCULATIONS COMPLETE')
872   FORMAT ('0Output data is located in the file VPMDECO.OUT')
880   FORMAT (' ')
890   FORMAT ('REPETITIVE DIVE:')
C===============================================================================
C     FORMAT STATEMENTS - ERROR MESSAGES
C===============================================================================
900   FORMAT (' ')
901   FORMAT ('0ERROR! UNITS MUST BE FSW OR MSW')
902   FORMAT ('0ERROR! ALTITUDE DIVE ALGORITHM MUST BE ON OR OFF')
903   FORMAT ('0ERROR! RADIUS MUST BE BETWEEN 0.2 AND 1.35 MICRONS')
904   FORMAT ('0ERROR! CRITICAL VOLUME ALGORITHM MUST BE ON OR OFF')
905   FORMAT ('0ERROR! STEP SIZE IS TOO LARGE TO DECOMPRESS')
906   FORMAT ('0ERROR IN INPUT FILE (GASMIX DATA)')
907   FORMAT ('0ERROR IN INPUT FILE (PROFILE CODE)')
908   FORMAT ('0ERROR IN INPUT FILE (REPETITIVE DIVE CODE)')
C===============================================================================
C     END OF MAIN PROGRAM
C===============================================================================
      END


C===============================================================================
C     NOTE ABOUT PRESSURE UNITS USED IN CALCULATIONS:
C     It is the convention in decompression calculations to compute all gas
C     loadings, absolute pressures, partial pressures, etc., in the units of
C     depth pressure that you are diving - either feet of seawater (fsw) or
C     meters of seawater (msw).  This program follows that convention with the
C     the exception that all VPM calculations are performed in SI units (by
C     necessity).  Accordingly, there are several conversions back and forth
C     between the diving pressure units and the SI units.   
C===============================================================================


C===============================================================================
C     FUNCTION SUBPROGRAM FOR GAS LOADING CALCULATIONS - ASCENT AND DESCENT
C===============================================================================
      FUNCTION SCHREINER_EQUATION (Initial_Inspired_Gas_Pressure,
     *Rate_Change_Insp_Gas_Pressure, Interval_Time, Gas_Time_Constant,  
     *Initial_Gas_Pressure)
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Initial_Inspired_Gas_Pressure                                  !input
      REAL Rate_Change_Insp_Gas_Pressure                                  !input
      REAL Interval_Time, Gas_Time_Constant                               !input
      REAL Initial_Gas_Pressure                                           !input
      REAL SCHREINER_EQUATION                                            !output
C===============================================================================
C     Note: The Schreiner equation is applied when calculating the uptake or
C     elimination of compartment gases during linear ascents or descents at a
C     constant rate.  For ascents, a negative number for rate must be used.
C===============================================================================
      SCHREINER_EQUATION =
     *Initial_Inspired_Gas_Pressure + Rate_Change_Insp_Gas_Pressure*
     *(Interval_Time - 1.0/Gas_Time_Constant) -
     *(Initial_Inspired_Gas_Pressure - Initial_Gas_Pressure -
     *Rate_Change_Insp_Gas_Pressure/Gas_Time_Constant)*
     *EXP (-Gas_Time_Constant*Interval_Time)
      RETURN
      END


C===============================================================================
C     FUNCTION SUBPROGRAM FOR GAS LOADING CALCULATIONS - CONSTANT DEPTH
C===============================================================================
      FUNCTION HALDANE_EQUATION (Initial_Gas_Pressure,
     *Inspired_Gas_Pressure, Gas_Time_Constant, Interval_Time)
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Initial_Gas_Pressure, Inspired_Gas_Pressure                    !input
      REAL Gas_Time_Constant, Interval_Time                               !input
      REAL HALDANE_EQUATION                                              !output
C===============================================================================
C     Note: The Haldane equation is applied when calculating the uptake or
C     elimination of compartment gases during intervals at constant depth (the
C     outside ambient pressure does not change).
C===============================================================================
      HALDANE_EQUATION = Initial_Gas_Pressure + 
     *(Inspired_Gas_Pressure - Initial_Gas_Pressure)*
     *(1.0 - EXP(-Gas_Time_Constant * Interval_Time))
      RETURN
      END


C===============================================================================
C     SUBROUTINE GAS_LOADINGS_ASCENT_DESCENT
C     Purpose: This subprogram applies the Schreiner equation to update the
C     gas loadings (partial pressures of helium and nitrogen) in the half-time
C     compartments due to a linear ascent or descent segment at a constant rate.
C===============================================================================
      SUBROUTINE GAS_LOADINGS_ASCENT_DESCENT (Starting_Depth,
     *                                        Ending_Depth, Rate)
      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Starting_Depth, Ending_Depth, Rate                             !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter
      INTEGER Last_Segment_Number

      REAL Initial_Inspired_He_Pressure
      REAL Initial_Inspired_N2_Pressure
      REAL Last_Run_Time
      REAL Helium_Rate, Nitrogen_Rate, Starting_Ambient_Pressure

      REAL SCHREINER_EQUATION                               !function subprogram
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      INTEGER Segment_Number                                         !both input
      REAL Run_Time, Segment_Time                                    !and output
      COMMON /Block_2/ Run_Time, Segment_Number, Segment_Time

      REAL Ending_Ambient_Pressure                                       !output
      COMMON /Block_4/ Ending_Ambient_Pressure

      INTEGER Mix_Number
      COMMON /Block_9/ Mix_Number

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                !both input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure            !and output

      REAL Fraction_Helium(10), Fraction_Nitrogen(10)  
      COMMON /Block_5/ Fraction_Helium, Fraction_Nitrogen

      REAL Initial_Helium_Pressure(16), Initial_Nitrogen_Pressure(16)    !output
      COMMON /Block_23/ Initial_Helium_Pressure,
     *                  Initial_Nitrogen_Pressure
C===============================================================================
C     CALCULATIONS
C===============================================================================
      Segment_Time = (Ending_Depth - Starting_Depth)/Rate
      Last_Run_Time = Run_Time
      Run_Time = Last_Run_Time + Segment_Time
      Last_Segment_Number = Segment_Number
      Segment_Number = Last_Segment_Number + 1
      Ending_Ambient_Pressure = Ending_Depth + Barometric_Pressure
      Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure
      Initial_Inspired_He_Pressure = (Starting_Ambient_Pressure -
     *               Water_Vapor_Pressure)*Fraction_Helium(Mix_Number)
      Initial_Inspired_N2_Pressure = (Starting_Ambient_Pressure -
     *               Water_Vapor_Pressure)*Fraction_Nitrogen(Mix_Number)
      Helium_Rate = Rate*Fraction_Helium(Mix_Number)
      Nitrogen_Rate = Rate*Fraction_Nitrogen(Mix_Number)
      DO I = 1,16
          Initial_Helium_Pressure(I) = Helium_Pressure(I)
          Initial_Nitrogen_Pressure(I) = Nitrogen_Pressure(I)

          Helium_Pressure(I) = SCHREINER_EQUATION
     *        (Initial_Inspired_He_Pressure, Helium_Rate,
     *        Segment_Time, Helium_Time_Constant(I),
     *        Initial_Helium_Pressure(I))     

          Nitrogen_Pressure(I) = SCHREINER_EQUATION
     *        (Initial_Inspired_N2_Pressure, Nitrogen_Rate,
     *        Segment_Time, Nitrogen_Time_Constant(I),
     *        Initial_Nitrogen_Pressure(I))     
      END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CALC_CRUSHING_PRESSURE
C     Purpose: Compute the effective "crushing pressure" in each compartment as
C     a result of descent segment(s).  The crushing pressure is the gradient
C     (difference in pressure) between the outside ambient pressure and the
C     gas tension inside a VPM nucleus (bubble seed).  This gradient acts to
C     reduce (shrink) the radius smaller than its initial value at the surface.
C     This phenomenon has important ramifications because the smaller the radius
C     of a VPM nucleus, the greater the allowable supersaturation gradient upon
C     ascent.  Gas loading (uptake) during descent, especially in the fast
C     compartments, will reduce the magnitude of the crushing pressure.  The
C     crushing pressure is not cumulative over a multi-level descent.  It will
C     be the maximum value obtained in any one discrete segment of the overall
C     descent.  Thus, the program must compute and store the maximum crushing
C     pressure for each compartment that was obtained across all segments of
C     the descent profile.
C
C     The calculation of crushing pressure will be different depending on
C     whether or not the gradient is in the VPM permeable range (gas can diffuse
C     across skin of VPM nucleus) or the VPM impermeable range (molecules in
C     skin of nucleus are squeezed together so tight that gas can no longer
C     diffuse in or out of nucleus; the gas becomes trapped and further resists
C     the crushing pressure).  The solution for crushing pressure in the VPM
C     permeable range is a simple linear equation.  In the VPM impermeable
C     range, a cubic equation must be solved using a numerical method.
C
C     Separate crushing pressures are tracked for helium and nitrogen because
C     they can have different critical radii.  The crushing pressures will be
C     the same for helium and nitrogen in the permeable range of the model, but
C     they will start to diverge in the impermeable range.  This is due to
C     the differences between starting radius, radius at the onset of
C     impermeability, and radial compression in the impermeable range.      
C===============================================================================
      SUBROUTINE CALC_CRUSHING_PRESSURE (Starting_Depth, Ending_Depth,
     *                                   Rate)
      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Starting_Depth, Ending_Depth, Rate                             !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Starting_Ambient_Pressure, Ending_Ambient_Pressure,D
      REAL Starting_Gas_Tension, Ending_Gas_Tension
      REAL Crushing_Pressure_He, Crushing_Pressure_N2 
      REAL Gradient_Onset_of_Imperm, Gradient_Onset_of_Imperm_Pa
      REAL Ending_Ambient_Pressure_Pa, Amb_Press_Onset_of_Imperm_Pa
      REAL Gas_Tension_Onset_of_Imperm_Pa
      REAL Crushing_Pressure_Pascals_He, Crushing_Pressure_Pascals_N2
      REAL Starting_Gradient, Ending_Gradient
      REAL A_He, B_He, C_He, Ending_Radius_He, High_Bound_He
      REAL Low_Bound_He
      REAL A_N2, B_N2, C_N2, Ending_Radius_N2, High_Bound_N2
      REAL Low_Bound_N2       
      REAL Radius_Onset_of_Imperm_He, Radius_Onset_of_Imperm_N2
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Gradient_Onset_of_Imperm_Atm
      COMMON /Block_14/ Gradient_Onset_of_Imperm_Atm

      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases

      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Units_Factor
      COMMON /Block_16/ Units_Factor

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                     !input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure                  

      REAL Adjusted_Critical_Radius_He(16)                                !input
      REAL Adjusted_Critical_Radius_N2(16)
      COMMON /Block_7/ Adjusted_Critical_Radius_He,
     *           Adjusted_Critical_Radius_N2 

      REAL Max_Crushing_Pressure_He(16), Max_Crushing_Pressure_N2(16)    !output
      COMMON /Block_10/ Max_Crushing_Pressure_He,
     *                  Max_Crushing_Pressure_N2

      REAL Amb_Pressure_Onset_of_Imperm(16)                               !input
      REAL Gas_Tension_Onset_of_Imperm(16)
      COMMON /Block_13/ Amb_Pressure_Onset_of_Imperm,
     *            Gas_Tension_Onset_of_Imperm

      REAL Initial_Helium_Pressure(16), Initial_Nitrogen_Pressure(16)     !input
      COMMON /Block_23/ Initial_Helium_Pressure,
     *                  Initial_Nitrogen_Pressure
C===============================================================================
C     CALCULATIONS
C     First, convert the Gradient for Onset of Impermeability from units of
C     atmospheres to diving pressure units (either fsw or msw) and to Pascals
C     (SI units).  The reason that the Gradient for Onset of Impermeability is
C     given in the program settings in units of atmospheres is because that is
C     how it was reported in the original research papers by Yount and
C     colleauges.
C===============================================================================
      Gradient_Onset_of_Imperm = Gradient_Onset_of_Imperm_Atm        !convert to
     *                           * Units_Factor                    !diving units

      Gradient_Onset_of_Imperm_Pa = Gradient_Onset_of_Imperm_Atm     !convert to
     *                              * 101325.0                          !Pascals
C===============================================================================
C     Assign values of starting and ending ambient pressures for descent segment
C===============================================================================
      Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure
      Ending_Ambient_Pressure = Ending_Depth + Barometric_Pressure
C===============================================================================
C     MAIN LOOP WITH NESTED DECISION TREE
C     For each compartment, the program computes the starting and ending
C     gas tensions and gradients.  The VPM is different than some dissolved gas
C     algorithms, Buhlmann for example, in that it considers the pressure due to
C     oxygen, carbon dioxide, and water vapor in each compartment in addition to
C     the inert gases helium and nitrogen.  These "other gases" are included in
C     the calculation of gas tensions and gradients.
C===============================================================================
      write(8,87)
 87   format( ' Nitrogen_Pressure(I)',3x,  'Helium_Pressure(I)',3x, 
     *   'Crushing_Pressure_N2',3x,      'Crushing_Pressure_He')
      
      DO I = 1,16
          Starting_Gas_Tension = Initial_Helium_Pressure(I) +
     *      Initial_Nitrogen_Pressure(I) + Constant_Pressure_Other_Gases

          Starting_Gradient = Starting_Ambient_Pressure -
     *                        Starting_Gas_Tension

          Ending_Gas_Tension = Helium_Pressure(I) + Nitrogen_Pressure(I)
     *                         + Constant_Pressure_Other_Gases

          Ending_Gradient = Ending_Ambient_Pressure - Ending_Gas_Tension
C===============================================================================
C     Compute radius at onset of impermeability for helium and nitrogen
C     critical radii
C===============================================================================
          Radius_Onset_of_Imperm_He = 1.0/(Gradient_Onset_of_Imperm_Pa/
     *        (2.0*(Skin_Compression_GammaC-Surface_Tension_Gamma)) +
     *        1.0/Adjusted_Critical_Radius_He(I))

          Radius_Onset_of_Imperm_N2 = 1.0/(Gradient_Onset_of_Imperm_Pa/
     *        (2.0*(Skin_Compression_GammaC-Surface_Tension_Gamma)) +
     *        1.0/Adjusted_Critical_Radius_N2(I))
C===============================================================================
C     FIRST BRANCH OF DECISION TREE - PERMEABLE RANGE
C     Crushing pressures will be the same for helium and nitrogen
C===============================================================================
          IF (Ending_Gradient .LE. Gradient_Onset_of_Imperm) THEN

              Crushing_Pressure_He = Ending_Ambient_Pressure -
     *                               Ending_Gas_Tension

              Crushing_Pressure_N2 = Ending_Ambient_Pressure -
     *                               Ending_Gas_Tension
      write(8,77) 1.0000*Nitrogen_Pressure(I), 1.0000*Helium_Pressure(I)
     *    ,1.0000*Crushing_Pressure_N2,1.0000*Crushing_Pressure_He,I

      END IF
 77   format(f10.3,15X,f10.3,15X,f10.3,15X,f10.3,15X,i5)
      
C===============================================================================
C     SECOND BRANCH OF DECISION TREE - IMPERMEABLE RANGE
C     Both the ambient pressure and the gas tension at the onset of
C     impermeability must be computed in order to properly solve for the ending
C     radius and resultant crushing pressure.  The first decision block
C     addresses the special case when the starting gradient just happens to be
C     equal to the gradient for onset of impermeability (not very likely!).
C===============================================================================
          IF (Ending_Gradient .GT. Gradient_Onset_of_Imperm) THEN

              IF(Starting_Gradient .EQ. Gradient_Onset_of_Imperm) THEN
                  Amb_Pressure_Onset_of_Imperm(I) =
     *                                  Starting_Ambient_Pressure
                  Gas_Tension_Onset_of_Imperm(I) = Starting_Gas_Tension
              END IF
C===============================================================================
C     In most cases, a subroutine will be called to find these values using a
C     numerical method.
C===============================================================================
              IF(Starting_Gradient .LT. Gradient_Onset_of_Imperm) THEN

!      write(8,*) 'Amb_Pressure_Onset_of_Imperm(I)  
!     *            Gas_Tension_Onset_of_Imperm(I)'
                  
                  CALL ONSET_OF_IMPERMEABILITY                       !subroutine
     *                          (Starting_Ambient_Pressure,
     *                           Ending_Ambient_Pressure, Rate, I)
              END IF
C===============================================================================
C     Next, using the values for ambient pressure and gas tension at the onset
C     of impermeability, the equations are set up to process the calculations
C     through the radius root finder subroutine.  This subprogram will find the
C     root (solution) to the cubic equation using a numerical method.  In order
C     to do this efficiently, the equations are placed in the form
C     Ar^3 - Br^2 - C = 0, where r is the ending radius after impermeable
C     compression.  The coefficients A, B, and C for helium and nitrogen are
C     computed and passed to the subroutine as arguments.  The high and low
C     bounds to be used by the numerical method of the subroutine are also
C     computed (see separate page posted on Deco List ftp site entitled
C     "VPM: Solving for radius in the impermeable regime").  The subprogram
C     will return the value of the ending radius and then the crushing
C     pressures for helium and nitrogen can be calculated.   
C===============================================================================
              Ending_Ambient_Pressure_Pa =
     *            (Ending_Ambient_Pressure/Units_Factor) * 101325.0

              Amb_Press_Onset_of_Imperm_Pa =
     *            (Amb_Pressure_Onset_of_Imperm(I)/Units_Factor)
     *            * 101325.0

              Gas_Tension_Onset_of_Imperm_Pa =
     *            (Gas_Tension_Onset_of_Imperm(I)/Units_Factor)
     *            * 101325.0

              B_He = 2.0*(Skin_Compression_GammaC-Surface_Tension_Gamma)

              A_He = Ending_Ambient_Pressure_Pa -
     *            Amb_Press_Onset_of_Imperm_Pa +
     *            Gas_Tension_Onset_of_Imperm_Pa +
     *            (2.0*(Skin_Compression_GammaC-Surface_Tension_Gamma))
     *            /Radius_Onset_of_Imperm_He

              C_He = Gas_Tension_Onset_of_Imperm_Pa *
     *            Radius_Onset_of_Imperm_He**3

              High_Bound_He = Radius_Onset_of_Imperm_He
              Low_Bound_He = B_He/A_He
              
      D = ( B_He**3 + 27./2.* A_He**2*C_He + 3./2.*Sqrt(3.)*A_He* 
     #    Sqrt(4.* B_He**3 *C_He + 27. *A_He**2* C_He**2))**(1./3.) 
      Ending_Radius_He = 1./3.*(B_He/A_He + B_He**2/(A_He*D) + D/A_He)
!      write(6,*) 'Ending_Radius_N2',Ending_Radius_He      
!      pause 'ending radius 1'       

!              CALL RADIUS_ROOT_FINDER (A_He,B_He,C_He,               !subroutine
!     *               Low_Bound_He, High_Bound_He, Ending_Radius_He)
!             write(6,*) 'Ending_Radius_He',Ending_Radius_He
!      pause 'ending radius 11'
              Crushing_Pressure_Pascals_He =
     *            Gradient_Onset_of_Imperm_Pa +
     *            Ending_Ambient_Pressure_Pa -
     *            Amb_Press_Onset_of_Imperm_Pa + 
     *            Gas_Tension_Onset_of_Imperm_Pa *
     *            (1.0-Radius_Onset_of_Imperm_He**3/Ending_Radius_He**3)

              Crushing_Pressure_He =
     *            (Crushing_Pressure_Pascals_He/101325.0) * Units_Factor

              B_N2 = 2.0*(Skin_Compression_GammaC-Surface_Tension_Gamma)

              A_N2 = Ending_Ambient_Pressure_Pa -
     *            Amb_Press_Onset_of_Imperm_Pa +
     *            Gas_Tension_Onset_of_Imperm_Pa +
     *            (2.0*(Skin_Compression_GammaC-Surface_Tension_Gamma))
     *            /Radius_Onset_of_Imperm_N2

              C_N2 = Gas_Tension_Onset_of_Imperm_Pa *
     *            Radius_Onset_of_Imperm_N2**3

              High_Bound_N2 = Radius_Onset_of_Imperm_N2
              Low_Bound_N2 = B_N2/A_N2

      D = ( B_N2**3 + 27./2.* A_N2**2*C_N2 + 3./2.*Sqrt(3.)*A_N2* 
     #    Sqrt(4.* B_N2**3 *C_N2 + 27. *A_N2**2* C_N2**2))**(1./3.) 
      Ending_Radius_N2 = 1./3.*(B_N2/A_N2 + B_N2**2/(A_N2*D) + D/A_N2)
       
!      write(6,*) 'Ending_Radius_N2',Ending_Radius_N2
!      pause 'ending radius 2'
      
!      CALL RADIUS_ROOT_FINDER (A_N2,B_N2,C_N2,               !subroutine
!     *               Low_Bound_N2,High_Bound_N2, Ending_Radius_N2)
      
!      write(6,*) 'Ending_Radius_N2',Ending_Radius_N2
!      pause 'ending radius 22'      
      
                    Crushing_Pressure_Pascals_N2 =
     *            Gradient_Onset_of_Imperm_Pa +
     *            Ending_Ambient_Pressure_Pa -
     *            Amb_Press_Onset_of_Imperm_Pa + 
     *            Gas_Tension_Onset_of_Imperm_Pa *
     *            (1.0-Radius_Onset_of_Imperm_N2**3/Ending_Radius_N2**3)

              Crushing_Pressure_N2 =
     *            (Crushing_Pressure_Pascals_N2/101325.0) * Units_Factor
          END IF
C===============================================================================
C     UPDATE VALUES OF MAX CRUSHING PRESSURE IN GLOBAL ARRAYS
C===============================================================================
          Max_Crushing_Pressure_He(I) = MAX(Max_Crushing_Pressure_He(I),
     *                                            Crushing_Pressure_He)

          Max_Crushing_Pressure_N2(I) = MAX(Max_Crushing_Pressure_N2(I),
     *                                            Crushing_Pressure_N2)
      END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE ONSET_OF_IMPERMEABILITY
C     Purpose:  This subroutine uses the Bisection Method to find the ambient 
C     pressure and gas tension at the onset of impermeability for a given
C     compartment.  Source:  "Numerical Recipes in Fortran 77",
C     Cambridge University Press, 1992.
C===============================================================================
      SUBROUTINE ONSET_OF_IMPERMEABILITY (Starting_Ambient_Pressure,
     *                              Ending_Ambient_Pressure, Rate, I)
      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      INTEGER I                         !input - array subscript for compartment

      REAL Starting_Ambient_Pressure, Ending_Ambient_Pressure, Rate       !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER J                                                    !loop counter

      REAL Initial_Inspired_He_Pressure
      REAL Initial_Inspired_N2_Pressure, Time
      REAL Helium_Rate, Nitrogen_Rate
      REAL Low_Bound, High_Bound, High_Bound_Helium_Pressure
      REAL High_Bound_Nitrogen_Pressure, Mid_Range_Helium_Pressure
      REAL Mid_Range_Nitrogen_Pressure, Last_Diff_Change
      REAL Function_at_High_Bound, Function_at_Low_Bound
      REAL Mid_Range_Time, Function_at_Mid_Range, Differential_Change
      REAL Mid_Range_Ambient_Pressure, Gas_Tension_at_Mid_Range
      REAL Gradient_Onset_of_Imperm
      REAL Starting_Gas_Tension, Ending_Gas_Tension

      REAL SCHREINER_EQUATION                               !function subprogram
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure

      REAL Gradient_Onset_of_Imperm_Atm
      COMMON /Block_14/ Gradient_Onset_of_Imperm_Atm

      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      INTEGER Mix_Number
      COMMON /Block_9/ Mix_Number

      REAL Units_Factor
      COMMON /Block_16/ Units_Factor
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Fraction_Helium(10), Fraction_Nitrogen(10)  
      COMMON /Block_5/ Fraction_Helium, Fraction_Nitrogen

      REAL Amb_Pressure_Onset_of_Imperm(16)                              !output
      REAL Gas_Tension_Onset_of_Imperm(16)
      COMMON /Block_13/ Amb_Pressure_Onset_of_Imperm,
     *            Gas_Tension_Onset_of_Imperm

      REAL Initial_Helium_Pressure(16), Initial_Nitrogen_Pressure(16)     !input
      COMMON /Block_23/ Initial_Helium_Pressure,
     *                  Initial_Nitrogen_Pressure
C===============================================================================
C     CALCULATIONS
C     First convert the Gradient for Onset of Impermeability to the diving
C     pressure units that are being used
C===============================================================================
      Gradient_Onset_of_Imperm = Gradient_Onset_of_Imperm_Atm
     *                           * Units_Factor
C===============================================================================
C     ESTABLISH THE BOUNDS FOR THE ROOT SEARCH USING THE BISECTION METHOD
C     In this case, we are solving for time - the time when the ambient pressure
C     minus the gas tension will be equal to the Gradient for Onset of
C     Impermeabliity.  The low bound for time is set at zero and the high
C     bound is set at the elapsed time (segment time) it took to go from the
C     starting ambient pressure to the ending ambient pressure.  The desired
C     ambient pressure and gas tension at the onset of impermeability will
C     be found somewhere between these endpoints.  The algorithm checks to
C     make sure that the solution lies in between these bounds by first
C     computing the low bound and high bound function values.
C===============================================================================
      Initial_Inspired_He_Pressure = (Starting_Ambient_Pressure -
     *               Water_Vapor_Pressure)*Fraction_Helium(Mix_Number)

      Initial_Inspired_N2_Pressure = (Starting_Ambient_Pressure -
     *             Water_Vapor_Pressure)*Fraction_Nitrogen(Mix_Number)

      Helium_Rate = Rate*Fraction_Helium(Mix_Number)
      Nitrogen_Rate = Rate*Fraction_Nitrogen(Mix_Number)
      Low_Bound = 0.0

      High_Bound = (Ending_Ambient_Pressure - Starting_Ambient_Pressure)
     *             /Rate

      Starting_Gas_Tension = Initial_Helium_Pressure(I) +
     *      Initial_Nitrogen_Pressure(I) + Constant_Pressure_Other_Gases

      Function_at_Low_Bound = Starting_Ambient_Pressure -
     *                  Starting_Gas_Tension - Gradient_Onset_of_Imperm

      High_Bound_Helium_Pressure = SCHREINER_EQUATION
     *    (Initial_Inspired_He_Pressure, Helium_Rate,
     *    High_Bound, Helium_Time_Constant(I),
     *    Initial_Helium_Pressure(I))     

      High_Bound_Nitrogen_Pressure = SCHREINER_EQUATION
     *    (Initial_Inspired_N2_Pressure, Nitrogen_Rate,
     *    High_Bound, Nitrogen_Time_Constant(I),
     *    Initial_Nitrogen_Pressure(I))     

      Ending_Gas_Tension = High_Bound_Helium_Pressure +
     *      High_Bound_Nitrogen_Pressure + Constant_Pressure_Other_Gases

      Function_at_High_Bound = Ending_Ambient_Pressure -
     *                    Ending_Gas_Tension - Gradient_Onset_of_Imperm      

      IF ((Function_at_High_Bound*Function_at_Low_Bound) .GE. 0.0) THEN
          PRINT *,'ERROR! ROOT IS NOT WITHIN BRACKETS'
          PAUSE
      END IF
C===============================================================================
C     APPLY THE BISECTION METHOD IN SEVERAL ITERATIONS UNTIL A SOLUTION WITH
C     THE DESIRED ACCURACY IS FOUND
C     Note: the program allows for up to 100 iterations.  Normally an exit will
C     be made from the loop well before that number.  If, for some reason, the
C     program exceeds 100 iterations, there will be a pause to alert the user.
C===============================================================================
      IF (Function_at_Low_Bound .LT. 0.0) THEN
          Time = Low_Bound
          Differential_Change = High_Bound - Low_Bound
      ELSE
          Time = High_Bound
          Differential_Change = Low_Bound - High_Bound
      END IF
      DO J = 1, 200
          Last_Diff_Change = Differential_Change
          Differential_Change = Last_Diff_Change*0.5
          Mid_Range_Time = Time + Differential_Change

          Mid_Range_Ambient_Pressure = (Starting_Ambient_Pressure +
     *                                  Rate*Mid_Range_Time)

          Mid_Range_Helium_Pressure = SCHREINER_EQUATION
     *        (Initial_Inspired_He_Pressure, Helium_Rate,
     *        Mid_Range_Time, Helium_Time_Constant(I),
     *        Initial_Helium_Pressure(I))     

          Mid_Range_Nitrogen_Pressure = SCHREINER_EQUATION
     *        (Initial_Inspired_N2_Pressure, Nitrogen_Rate,
     *        Mid_Range_Time, Nitrogen_Time_Constant(I),
     *        Initial_Nitrogen_Pressure(I))     

          Gas_Tension_at_Mid_Range = Mid_Range_Helium_Pressure +
     *       Mid_Range_Nitrogen_Pressure + Constant_Pressure_Other_Gases

          Function_at_Mid_Range = Mid_Range_Ambient_Pressure -
     *        Gas_Tension_at_Mid_Range - Gradient_Onset_of_Imperm       

          IF (Function_at_Mid_Range .LE. 0.0) Time = Mid_Range_Time

          IF ((ABS(Differential_Change) .LT. 1.0E-3) .OR.
     *        (Function_at_Mid_Range .EQ. 0.0)) GOTO 100

      END DO
      PRINT *,'ERROR! ROOT SEARCH EXCEEDED MAXIMUM ITERATIONS'
      PAUSE '3'
C===============================================================================
C     When a solution with the desired accuracy is found, the program jumps out
C     of the loop to Line 100 and assigns the solution values for ambient
C     pressure and gas tension at the onset of impermeability.
C===============================================================================
100   Amb_Pressure_Onset_of_Imperm(I) = Mid_Range_Ambient_Pressure
      Gas_Tension_Onset_of_Imperm(I) = Gas_Tension_at_Mid_Range
            write(8,*) 1.0000*Amb_Pressure_Onset_of_Imperm(I),  
     *            1.0000*Gas_Tension_Onset_of_Imperm(I),I
            
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE RADIUS_ROOT_FINDER
C     Purpose: This subroutine is a "fail-safe" routine that combines the
C     Bisection Method and the Newton-Raphson Method to find the desired root.
C     This hybrid algorithm takes a bisection step whenever Newton-Raphson would
C     take the solution out of bounds, or whenever Newton-Raphson is not
C     converging fast enough.  Source:  "Numerical Recipes in Fortran 77",
C     Cambridge University Press, 1992.  
C===============================================================================
      SUBROUTINE RADIUS_ROOT_FINDER (A,B,C, Low_Bound, High_Bound,
     *                               Ending_Radius)
      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL A, B, C, Low_Bound, High_Bound                                 !input
      REAL Ending_Radius                                                 !output
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Function, Derivative_of_Function, Differential_Change
      REAL Last_Diff_Change, Last_Ending_Radius
      REAL Radius_at_Low_Bound, Radius_at_High_Bound
      REAL Function_at_Low_Bound, Function_at_High_Bound
      Real x,D
C===============================================================================
C     BEGIN CALCULATIONS BY MAKING SURE THAT THE ROOT LIES WITHIN BOUNDS
C     In this case we are solving for radius in a cubic equation of the form,
C     Ar^3 - Br^2 - C = 0.  The coefficients A, B, and C were passed to this
C     subroutine as arguments.
C===============================================================================
      
      Function_at_Low_Bound =
     *    Low_Bound*(Low_Bound*(A*Low_Bound - B)) - C

      Function_at_High_Bound =
     *    High_Bound*(High_Bound*(A*High_Bound - B)) - C

      IF ((Function_at_Low_Bound .GT. 0.0).AND.
     *    (Function_at_High_Bound .GT. 0.0)) THEN
          PRINT *,'ERROR! ROOT IS NOT WITHIN BRACKETS'
          PAUSE
      END IF      
C===============================================================================
C     Next the algorithm checks for special conditions and then prepares for
C     the first bisection.
C===============================================================================
      IF ((Function_at_Low_Bound .LT. 0.0).AND.
     *    (Function_at_High_Bound .LT. 0.0)) THEN
          PRINT *,'ERROR! ROOT IS NOT WITHIN BRACKETS'
          PAUSE
      END IF       
      IF (Function_at_Low_Bound .EQ. 0.0) THEN
          Ending_Radius = Low_Bound
          RETURN
      ELSE IF (Function_at_High_Bound .EQ. 0.0) THEN
          Ending_Radius = High_Bound
          RETURN
      ELSE IF (Function_at_Low_Bound .LT. 0.0) THEN
          Radius_at_Low_Bound = Low_Bound
          Radius_at_High_Bound = High_Bound
      ELSE
          Radius_at_High_Bound = Low_Bound
          Radius_at_Low_Bound = High_Bound
      END IF
      Ending_Radius = 0.5*(Low_Bound + High_Bound)
      Last_Diff_Change = ABS(High_Bound-Low_Bound)
      Differential_Change = Last_Diff_Change
C===============================================================================
C     At this point, the Newton-Raphson Method is applied which uses a function
C     and its first derivative to rapidly converge upon a solution.
C     Note: the program allows for up to 100 iterations.  Normally an exit will
C     be made from the loop well before that number.  If, for some reason, the
C     program exceeds 100 iterations, there will be a pause to alert the user.
C     When a solution with the desired accuracy is found, exit is made from the
C     loop by returning to the calling program.  The last value of ending
C     radius has been assigned as the solution.
C===============================================================================
      Function = Ending_Radius*(Ending_Radius*(A*Ending_Radius - B)) - C

      Derivative_of_Function =
     *    Ending_Radius*(Ending_Radius*3.0*A - 2.0*B)

      DO I = 1,100
          IF((((Ending_Radius-Radius_at_High_Bound)*
     *        Derivative_of_Function-Function)*
     *        ((Ending_Radius-Radius_at_Low_Bound)*
     *        Derivative_of_Function-Function).GE.0.0) .OR.
     *        (ABS(2.0*Function).GT.
     *        (ABS(Last_Diff_Change*Derivative_of_Function)))) THEN

              Last_Diff_Change = Differential_Change

              Differential_Change = 0.5*(Radius_at_High_Bound -
     *            Radius_at_Low_Bound)

              Ending_Radius = Radius_at_Low_Bound + Differential_Change
              IF (Radius_at_Low_Bound .EQ. Ending_Radius) RETURN
          ELSE
              Last_Diff_Change = Differential_Change
              Differential_Change = Function/Derivative_of_Function
              Last_Ending_Radius = Ending_Radius
              Ending_Radius = Ending_Radius - Differential_Change
              IF (Last_Ending_Radius .EQ. Ending_Radius) RETURN
          END IF
          IF (ABS(Differential_Change) .LT. 1.0E-12) RETURN      
          Function =
     *        Ending_Radius*(Ending_Radius*(A*Ending_Radius - B)) - C

          Derivative_of_Function =
     *        Ending_Radius*(Ending_Radius*3.0*A - 2.0*B)

          IF (Function .LT. 0.0) THEN
              Radius_at_Low_Bound = Ending_Radius
          ELSE
              Radius_at_High_Bound = Ending_Radius
          END IF
      END DO
      PRINT *,'ERROR! ROOT SEARCH EXCEEDED MAXIMUM ITERATIONS'
!      PAUSE '2'
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      END


C===============================================================================
C     SUBROUTINE GAS_LOADINGS_CONSTANT_DEPTH
C     Purpose: This subprogram applies the Haldane equation to update the
C     gas loadings (partial pressures of helium and nitrogen) in the half-time
C     compartments for a segment at constant depth.
C===============================================================================
      SUBROUTINE GAS_LOADINGS_CONSTANT_DEPTH (Depth,
     *                                        Run_Time_End_of_Segment)
      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Depth, Run_Time_End_of_Segment                                 !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter
      INTEGER Last_Segment_Number

      REAL Initial_Helium_Pressure, Initial_Nitrogen_Pressure
      REAL Inspired_Helium_Pressure, Inspired_Nitrogen_Pressure
      REAL Ambient_Pressure, Last_Run_Time

      REAL HALDANE_EQUATION                                 !function subprogram
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      INTEGER Segment_Number                                         !both input
      REAL Run_Time, Segment_Time                                    !and output
      COMMON /Block_2/ Run_Time, Segment_Number, Segment_Time

      REAL Ending_Ambient_Pressure                                       !output
      COMMON /Block_4/ Ending_Ambient_Pressure

      INTEGER Mix_Number
      COMMON /Block_9/ Mix_Number

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                !both input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure            !and output

      REAL Fraction_Helium(10), Fraction_Nitrogen(10)  
      COMMON /Block_5/ Fraction_Helium, Fraction_Nitrogen
C===============================================================================
C     CALCULATIONS
C===============================================================================
      Segment_Time = Run_Time_End_of_Segment - Run_Time
      Last_Run_Time = Run_Time_End_of_Segment
      Run_Time = Last_Run_Time
      Last_Segment_Number = Segment_Number
      Segment_Number = Last_Segment_Number + 1
      Ambient_Pressure = Depth + Barometric_Pressure

      Inspired_Helium_Pressure = (Ambient_Pressure -
     *        Water_Vapor_Pressure)*Fraction_Helium(Mix_Number)

      Inspired_Nitrogen_Pressure = (Ambient_Pressure -
     *        Water_Vapor_Pressure)*Fraction_Nitrogen(Mix_Number)

      Ending_Ambient_Pressure = Ambient_Pressure
      DO I = 1,16
          Initial_Helium_Pressure = Helium_Pressure(I)
          Initial_Nitrogen_Pressure = Nitrogen_Pressure(I)

          Helium_Pressure(I) = HALDANE_EQUATION
     *        (Initial_Helium_Pressure, Inspired_Helium_Pressure,
     *        Helium_Time_Constant(I), Segment_Time)

          Nitrogen_Pressure(I) = HALDANE_EQUATION
     *        (Initial_Nitrogen_Pressure, Inspired_Nitrogen_Pressure,
     *        Nitrogen_Time_Constant(I), Segment_Time)
      END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE NUCLEAR_REGENERATION
C     Purpose: This subprogram calculates the regeneration of VPM critical
C     radii that takes place over the dive time.  The regeneration time constant
C     has a time scale of weeks so this will have very little impact on dives of
C     normal length, but will have a major impact for saturation dives.
C===============================================================================
      SUBROUTINE NUCLEAR_REGENERATION (Dive_Time)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Dive_Time                                                      !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Crushing_Pressure_Pascals_He, Crushing_Pressure_Pascals_N2
      REAL Ending_Radius_He, Ending_Radius_N2
      REAL Crush_Pressure_Adjust_Ratio_He 
      REAL Crush_Pressure_Adjust_Ratio_N2
      REAL Adj_Crush_Pressure_He_Pascals, Adj_Crush_Pressure_N2_Pascals 
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2
      REAL Regeneration_Time_Constant
      COMMON /Block_22/ Regeneration_Time_Constant
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Units_Factor
      COMMON /Block_16/ Units_Factor
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Adjusted_Critical_Radius_He(16)                                !input
      REAL Adjusted_Critical_Radius_N2(16)
      COMMON /Block_7/ Adjusted_Critical_Radius_He,
     *                 Adjusted_Critical_Radius_N2 

      REAL Max_Crushing_Pressure_He(16), Max_Crushing_Pressure_N2(16)     !input
      COMMON /Block_10/ Max_Crushing_Pressure_He,
     *                  Max_Crushing_Pressure_N2

      REAL Regenerated_Radius_He(16), Regenerated_Radius_N2(16)          !output
      COMMON /Block_24/ Regenerated_Radius_He, Regenerated_Radius_N2

      REAL Adjusted_Crushing_Pressure_He(16)                             !output
      REAL Adjusted_Crushing_Pressure_N2(16)       
      COMMON /Block_25/ Adjusted_Crushing_Pressure_He,
     *                  Adjusted_Crushing_Pressure_N2
C===============================================================================
C     CALCULATIONS
C     First convert the maximum crushing pressure obtained for each compartment
C     to Pascals.  Next, compute the ending radius for helium and nitrogen
C     critical nuclei in each compartment. 
C===============================================================================
      write(8,*) ' '
      write(8,87)
87    format(  'Regenerated_Radius_N2',3x, 'Regenerated_Radius_He',3x,
     *  'Adj_Crush_Press_N2',3x,       'Adj_Crush_Press_He')
      
      DO I = 1,16
          Crushing_Pressure_Pascals_He =
     *        (Max_Crushing_Pressure_He(I)/Units_Factor) * 101325.0

          Crushing_Pressure_Pascals_N2 =
     *        (Max_Crushing_Pressure_N2(I)/Units_Factor) * 101325.0

          Ending_Radius_He = 1.0/(Crushing_Pressure_Pascals_He/
     *        (2.0*(Skin_Compression_GammaC - Surface_Tension_Gamma)) + 
     *        1.0/Adjusted_Critical_Radius_He(I))

          Ending_Radius_N2 = 1.0/(Crushing_Pressure_Pascals_N2/
     *        (2.0*(Skin_Compression_GammaC - Surface_Tension_Gamma)) + 
     *        1.0/Adjusted_Critical_Radius_N2(I))
C===============================================================================
C     A "regenerated" radius for each nucleus is now calculated based on the
C     regeneration time constant.  This means that after application of
C     crushing pressure and reduction in radius, a nucleus will slowly grow
C     back to its original initial radius over a period of time.  This
C     phenomenon is probabilistic in nature and depends on absolute temperature.
C     It is independent of crushing pressure.
C===============================================================================
          Regenerated_Radius_He(I) = Adjusted_Critical_Radius_He(I) +
     *        (Ending_Radius_He - Adjusted_Critical_Radius_He(I)) *
     *        EXP(-Dive_Time/Regeneration_Time_Constant)
      
          Regenerated_Radius_N2(I) = Adjusted_Critical_Radius_N2(I) +
     *        (Ending_Radius_N2 - Adjusted_Critical_Radius_N2(I)) *
     *        EXP(-Dive_Time/Regeneration_Time_Constant)
C===============================================================================
C     In order to preserve reference back to the initial critical radii after
C     regeneration, an "adjusted crushing pressure" for the nuclei in each
C     compartment must be computed.  In other words, this is the value of
C     crushing pressure that would have reduced the original nucleus to the
C     to the present radius had regeneration not taken place.  The ratio
C     for adjusting crushing pressure is obtained from algebraic manipulation
C     of the standard VPM equations.  The adjusted crushing pressure, in lieu
C     of the original crushing pressure, is then applied in the VPM Critical
C     Volume Algorithm and the VPM Repetitive Algorithm. 
C===============================================================================
          Crush_Pressure_Adjust_Ratio_He =
     *        (Ending_Radius_He*(Adjusted_Critical_Radius_He(I) -
     *        Regenerated_Radius_He(I))) / (Regenerated_Radius_He(I) *
     *        (Adjusted_Critical_Radius_He(I) - Ending_Radius_He))
      
          Crush_Pressure_Adjust_Ratio_N2 =
     *        (Ending_Radius_N2*(Adjusted_Critical_Radius_N2(I) -
     *        Regenerated_Radius_N2(I))) / (Regenerated_Radius_N2(I) *
     *        (Adjusted_Critical_Radius_N2(I) - Ending_Radius_N2))

          Adj_Crush_Pressure_He_Pascals = Crushing_Pressure_Pascals_He *
     *        Crush_Pressure_Adjust_Ratio_He

          Adj_Crush_Pressure_N2_Pascals = Crushing_Pressure_Pascals_N2 *
     *        Crush_Pressure_Adjust_Ratio_N2

          Adjusted_Crushing_Pressure_He(I) =
     *        (Adj_Crush_Pressure_He_Pascals / 101325.0) * Units_Factor  

          Adjusted_Crushing_Pressure_N2(I) =
     *        (Adj_Crush_Pressure_N2_Pascals / 101325.0) * Units_Factor  

          write(8,77) Regenerated_Radius_N2(I),Regenerated_Radius_He(I),
     *         1.0000*Adjusted_Crushing_Pressure_N2(I), 
     *                   1.0000*Adjusted_Crushing_Pressure_He(I) ,I

 77   format(e10.3,15X,e10.3,15x,2(f10.3,15X),i5)      
    
      END DO
      write(8,*) ' ' 
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CALC_INITIAL_ALLOWABLE_GRADIENT
C     Purpose: This subprogram calculates the initial allowable gradients for
C     helium and nitrogren in each compartment.  These are the gradients that
C     will be used to set the deco ceiling on the first pass through the deco
C     loop.  If the Critical Volume Algorithm is set to "off", then these
C     gradients will determine the final deco schedule.  Otherwise, if the
C     Critical Volume Algorithm is set to "on", these gradients will be further
C     "relaxed" by the Critical Volume Algorithm subroutine.  The initial
C     allowable gradients are referred to as "PssMin" in the papers by Yount
C     and colleauges, i.e., the minimum supersaturation pressure gradients
C     that will probe bubble formation in the VPM nuclei that started with the
C     designated minimum initial radius (critical radius).
C
C     The initial allowable gradients are computed directly from the
C     "regenerated" radii after the Nuclear Regeneration subroutine.  These
C     gradients are tracked separately for helium and nitrogen.  
C===============================================================================
      SUBROUTINE CALC_INITIAL_ALLOWABLE_GRADIENT

      IMPLICIT NONE
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Initial_Allowable_Grad_He_Pa, Initial_Allowable_Grad_N2_Pa
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Units_Factor
      COMMON /Block_16/ Units_Factor
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Regenerated_Radius_He(16), Regenerated_Radius_N2(16)           !input
      COMMON /Block_24/ Regenerated_Radius_He, Regenerated_Radius_N2

      REAL Allowable_Gradient_He(16), Allowable_Gradient_N2 (16)         !output
      COMMON /Block_26/ Allowable_Gradient_He, Allowable_Gradient_N2

      REAL Initial_Allowable_Gradient_He(16)                             !output
      REAL Initial_Allowable_Gradient_N2(16)
      COMMON /Block_27/
     *    Initial_Allowable_Gradient_He, Initial_Allowable_Gradient_N2
C===============================================================================
C     CALCULATIONS
C     The initial allowable gradients are computed in Pascals and then converted
C     to the diving pressure units.  Two different sets of arrays are used to
C     save the calculations - Initial Allowable Gradients and Allowable
C     Gradients.  The Allowable Gradients are assigned the values from Initial
C     Allowable Gradients however the Allowable Gradients can be changed later
C     by the Critical Volume subroutine.  The values for the Initial Allowable
C     Gradients are saved in a global array for later use by both the Critical
C     Volume subroutine and the VPM Repetitive Algorithm subroutine.
C===============================================================================
      Write(8,87) 
      
87    format('Initial_Allowable_Gradient_N2(I)',3x,  
     *  'Initial_Allowable_Gradient_He(I)' )
      
      DO I = 1,16

          Initial_Allowable_Grad_N2_Pa = ((2.0*Surface_Tension_Gamma*
     *        (Skin_Compression_GammaC - Surface_Tension_Gamma)) /
     *        (Regenerated_Radius_N2(I)*Skin_Compression_GammaC))

          Initial_Allowable_Grad_He_Pa = ((2.0*Surface_Tension_Gamma*
     *        (Skin_Compression_GammaC - Surface_Tension_Gamma)) /
     *    (Regenerated_Radius_He(I)*Skin_Compression_GammaC))*rapsol1

          Initial_Allowable_Gradient_N2(I) =
     *        (Initial_Allowable_Grad_N2_Pa / 101325.0) * Units_Factor

          Initial_Allowable_Gradient_He(I) = 
     *        (Initial_Allowable_Grad_He_Pa / 101325.0) * Units_Factor

          Allowable_Gradient_He(I) = Initial_Allowable_Gradient_He(I)
          Allowable_Gradient_N2(I) = Initial_Allowable_Gradient_N2(I) 
          
          write(8,31) 1.0000*Allowable_Gradient_N2(I),
     *    1.0000*Allowable_Gradient_He(I),I
 31   format(f10.3,30x,f10.3,25x,I3) 
      
          END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CALC_ASCENT_CEILING
C     Purpose: This subprogram calculates the ascent ceiling (the safe ascent
C     depth) in each compartment, based on the allowable gradients, and then
C     finds the deepest ascent ceiling across all compartments.
C===============================================================================
      SUBROUTINE CALC_ASCENT_CEILING (Ascent_Ceiling_Depth)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Ascent_Ceiling_Depth                                          !output
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Gas_Loading, Weighted_Allowable_Gradient
            
!      common /gradienti/Weighted_Allowable_Gradient
      REAL Tolerated_Ambient_Pressure
C===============================================================================
C     LOCAL ARRAYS
C===============================================================================
      REAL Compartment_Ascent_Ceiling(16)
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                     !input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure

      REAL Allowable_Gradient_He(16), Allowable_Gradient_N2 (16)          !input
      COMMON /Block_26/ Allowable_Gradient_He, Allowable_Gradient_N2
      Real DCD(10,16)
      integer ipilot,jmax(16)
      common /pilota/ipilot,jmax,DCD
C===============================================================================
C     CALCULATIONS
C     Since there are two sets of allowable gradients being tracked, one for
C     helium and one for nitrogen, a "weighted allowable gradient" must be
C     computed each time based on the proportions of helium and nitrogen in
C     each compartment.  This proportioning follows the methodology of
C     Buhlmann/Keller.  If there is no helium and nitrogen in the compartment,
C     such as after extended periods of oxygen breathing, then the minimum value
C     across both gases will be used.  It is important to note that if a
C     compartment is empty of helium and nitrogen, then the weighted allowable
C     gradient formula cannot be used since it will result in division by zero.
C===============================================================================
      DO I = 1,16
          Gas_Loading = Helium_Pressure(I) + Nitrogen_Pressure(I)
      
      IF (Gas_Loading .GT. 0.0) THEN
          Weighted_Allowable_Gradient =
     *    (Allowable_Gradient_He(I)* Helium_Pressure(I) +
     *    Allowable_Gradient_N2(I)* Nitrogen_Pressure(I)) /
     *    (Helium_Pressure(I) + Nitrogen_Pressure(I))

          Tolerated_Ambient_Pressure = (Gas_Loading +
     *    Constant_Pressure_Other_Gases) - Weighted_Allowable_Gradient

      ELSE
          Weighted_Allowable_Gradient =
     *    MIN(Allowable_Gradient_He(I), Allowable_Gradient_N2(I)) 

          Tolerated_Ambient_Pressure =
     *    Constant_Pressure_Other_Gases - Weighted_Allowable_Gradient
      END IF
C===============================================================================
C     The tolerated ambient pressure cannot be less than zero absolute, i.e.,
C     the vacuum of outer space!
C===============================================================================
      IF (Tolerated_Ambient_Pressure .LT. 0.0) THEN
          Tolerated_Ambient_Pressure = 0.0
      END IF
C===============================================================================
C     The Ascent Ceiling Depth is computed in a loop after all of the individual
C     compartment ascent ceilings have been calculated.  It is important that
C     the Ascent Ceiling Depth (max ascent ceiling across all compartments) only
C     be extracted from the compartment values and not be compared against some
C     initialization value.  For example, if MAX(Ascent_Ceiling_Depth . .) was
C     compared against zero, this could cause a program lockup because sometimes
C     the Ascent Ceiling Depth needs to be negative (but not less than zero
C     absolute ambient pressure) in order to decompress to the last stop at zero
C     depth.
C===============================================================================
          Compartment_Ascent_Ceiling(I) =
     *        Tolerated_Ambient_Pressure - Barometric_Pressure
          
!      write(6,*) ' Weighted_Allowable_Gradient,Barometric_Pressure',
!     #             Weighted_Allowable_Gradient,Barometric_Pressure 
!      write(6,*) ' Compartment_Ascent_Ceiling(I),i',
!     #             Compartment_Ascent_Ceiling(I),i
!      write(6,*) 'Tolerated_Ambient_Pressure,i',
!     #            Tolerated_Ambient_Pressure,i
      
      END DO
      
      Ascent_Ceiling_Depth = Compartment_Ascent_Ceiling(1)
!      ipilot=1
!      write(6,*) 'Ascent_Ceiling_Depth prima', Ascent_Ceiling_Depth
      DO I = 2,16 

      if(Ascent_Ceiling_Depth.lt.Compartment_Ascent_Ceiling(I)) then
!      Ascent_Ceiling_Depth=Compartment_Ascent_Ceiling(I)
!      ipilot=I

!      pause
      end if 

!      write(6,*) Compartment_Ascent_Ceiling(I),i
      
          Ascent_Ceiling_Depth =
     *        MAX(Ascent_Ceiling_Depth, Compartment_Ascent_Ceiling(I))

      END DO  
!      write(6,*) 'Ascent_Ceiling_Depth dopo', Ascent_Ceiling_Depth
!      pause
      
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CALC_MAX_ACTUAL_GRADIENT
C     Purpose: This subprogram calculates the actual supersaturation gradient
C     obtained in each compartment as a result of the ascent profile during
C     decompression.  Similar to the concept with crushing pressure, the
C     supersaturation gradients are not cumulative over a multi-level, staged
C     ascent.  Rather, it will be the maximum value obtained in any one discrete
C     step of the overall ascent.  Thus, the program must compute and store the
C     maximum actual gradient for each compartment that was obtained across all
C     steps of the ascent profile.  This subroutine is invoked on the last pass
C     through the deco stop loop block when the final deco schedule is being
C     generated.
C
C     The max actual gradients are later used by the VPM Repetitive Algorithm to
C     determine if adjustments to the critical radii are required.  If the max
C     actual gradient did not exceed the initial alllowable gradient, then no
C     adjustment will be made.  However, if the max actual gradient did exceed
C     the intitial allowable gradient, such as permitted by the Critical Volume
C     Algorithm, then the critical radius will be adjusted (made larger) on the
C     repetitive dive to compensate for the bubbling that was allowed on the
C     previous dive.  The use of the max actual gradients is intended to prevent
C     the repetitive algorithm from being overly conservative.  
C===============================================================================
      SUBROUTINE CALC_MAX_ACTUAL_GRADIENT (Deco_Stop_Depth)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Deco_Stop_Depth                                                !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Compartment_Gradient
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                     !input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure

      REAL Max_Actual_Gradient(16)
      COMMON /Block_12/ Max_Actual_Gradient                              !output
C===============================================================================
C     CALCULATIONS
C     Note: negative supersaturation gradients are meaningless for this
C     application, so the values must be equal to or greater than zero.
C===============================================================================
!            Write(8,*) 'Max_Actual_Gradient'

      DO I = 1,16
          Compartment_Gradient = (Helium_Pressure(I) +
     *        Nitrogen_Pressure(I) + Constant_Pressure_Other_Gases)
     *        - (Deco_Stop_Depth + Barometric_Pressure)
          IF (Compartment_Gradient .LE. 0.0) THEN
              Compartment_Gradient = 0.0
          END IF
          
      if(Max_Actual_Gradient(I).le.Compartment_Gradient) then
      Max_Actual_Gradient(I) = Compartment_Gradient

!      write(9,*) 'Max_Actual_Gradient(I),Compartment_Gradient,i',
!     #            Max_Actual_Gradient(I),Compartment_Gradient,i      

     
!     pause

      end if 
      
!      Max_Actual_Gradient(I) =
!     *         MAX(Max_Actual_Gradient(I), Compartment_Gradient)

!      Write(9,31) Max_Actual_Gradient(I),I
!      write(9,*)  
!          pause
      END DO
            
 31       format(62x,f10.6,30x,I3) 
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CALC_SURFACE_PHASE_VOLUME_TIME
C     Purpose: This subprogram computes the surface portion of the total phase
C     volume time.  This is the time factored out of the integration of
C     supersaturation gradient x time over the surface interval.  The VPM
C     considers the gradients that allow bubbles to form or to drive bubble
C     growth both in the water and on the surface after the dive.
C
C     This subroutine is a new development to the VPM algorithm in that it
C     computes the time course of supersaturation gradients on the surface
C     when both helium and nitrogen are present.  Refer to separate write-up
C     for a more detailed explanation of this algorithm.
C===============================================================================
      SUBROUTINE CALC_SURFACE_PHASE_VOLUME_TIME

      IMPLICIT NONE
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Integral_Gradient_x_Time, Decay_Time_to_Zero_Gradient
      REAL Surface_Inspired_N2_Pressure
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                     !input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure        

      REAL Surface_Phase_Volume_Time(16)                                 !output
      COMMON /Block_11/ Surface_Phase_Volume_Time
C===============================================================================
C     CALCULATIONS
C===============================================================================
      Surface_Inspired_N2_Pressure = (Barometric_Pressure -
     *        Water_Vapor_Pressure)*0.79
      DO I = 1,16      
          IF (Nitrogen_Pressure(I) .GT. Surface_Inspired_N2_Pressure)
     *                                                           THEN
              Surface_Phase_Volume_Time(I)=
     *            (Helium_Pressure(I)/Helium_Time_Constant(I)+
     *            (Nitrogen_Pressure(I)-Surface_Inspired_N2_Pressure)/
     *            Nitrogen_Time_Constant(I))
     *            /(Helium_Pressure(I) + Nitrogen_Pressure(I) -
     *            Surface_Inspired_N2_Pressure)

          ELSE IF ((Nitrogen_Pressure(I) .LE.
     *            Surface_Inspired_N2_Pressure).AND.
     *            (Helium_Pressure(I)+Nitrogen_Pressure(I).GE.
     *            Surface_Inspired_N2_Pressure)) THEN

              Decay_Time_to_Zero_Gradient =
     *           1.0/(Nitrogen_Time_Constant(I)-Helium_Time_Constant(I))
     *           *ALOG((Surface_Inspired_N2_Pressure -
     *           Nitrogen_Pressure(I))/Helium_Pressure(I))

              Integral_Gradient_x_Time =
     *            Helium_Pressure(I)/Helium_Time_Constant(I)*
     *            (1.0-EXP(-Helium_Time_Constant(I)*
     *            Decay_Time_to_Zero_Gradient))+
     *            (Nitrogen_Pressure(I)-Surface_Inspired_N2_Pressure)/
     *            Nitrogen_Time_Constant(I)*
     *            (1.0-EXP(-Nitrogen_Time_Constant(I)*
     *            Decay_Time_to_Zero_Gradient))

              Surface_Phase_Volume_Time(I) =
     *            Integral_Gradient_x_Time/(Helium_Pressure(I) +
     *            Nitrogen_Pressure(I) - Surface_Inspired_N2_Pressure)

          ELSE
              Surface_Phase_Volume_Time(I) = 0.0
          END IF
      END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CRITICAL_VOLUME
C     Purpose: This subprogram applies the VPM Critical Volume Algorithm.  This
C     algorithm will compute "relaxed" gradients for helium and nitrogen based
C     on the setting of the Critical Volume Parameter Lambda.
C===============================================================================
      SUBROUTINE CRITICAL_VOLUME (Deco_Phase_Volume_Time)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Deco_Phase_Volume_Time                                         !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Lambda_Pascals_Parameter
      REAL Adj_Crush_Pressure_He_Pascals, Adj_Crush_Pressure_N2_Pascals 
      REAL Initial_Allowable_Grad_He_Pa, Initial_Allowable_Grad_N2_Pa
      REAL New_Allowable_Grad_He_Pascals, New_Allowable_Grad_N2_Pascals
      REAL B, C
C===============================================================================
C     LOCAL ARRAYS
C===============================================================================
      REAL Phase_Volume_Time(16)
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2
      
      REAL Crit_Volume_Parameter_Lambda
      COMMON /Block_20/ Crit_Volume_Parameter_Lambda
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Units_Factor
      COMMON /Block_16/ Units_Factor
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Adjusted_Critical_Radius_He(16)                                !input
      REAL Adjusted_Critical_Radius_N2(16)
      COMMON /Block_7/ Adjusted_Critical_Radius_He,
     *           Adjusted_Critical_Radius_N2 

      REAL Surface_Phase_Volume_Time(16)                                  !input
      COMMON /Block_11/ Surface_Phase_Volume_Time

      REAL Adjusted_Crushing_Pressure_He(16)                              !input
      REAL Adjusted_Crushing_Pressure_N2(16)       
      COMMON /Block_25/ Adjusted_Crushing_Pressure_He,
     *                  Adjusted_Crushing_Pressure_N2

      REAL Allowable_Gradient_He(16), Allowable_Gradient_N2 (16)         !output
      COMMON /Block_26/ Allowable_Gradient_He, Allowable_Gradient_N2

      REAL Initial_Allowable_Gradient_He(16)                              !input
      REAL Initial_Allowable_Gradient_N2(16)
      COMMON /Block_27/
     *    Initial_Allowable_Gradient_He, Initial_Allowable_Gradient_N2
C===============================================================================
C     CALCULATIONS
C     Note:  Since the Critical Volume Parameter Lambda was defined in units of
C     fsw-min in the original papers by Yount and colleauges, the same
C     convention is retained here.  Although Lambda is adjustable only in units
C     of fsw-min in the program settings (range from 6500 to 8300 with default
C     7500), it will convert to the proper value in Pascals-min in this
C     subroutine regardless of which diving pressure units are being used in
C     the main program - feet of seawater (fsw) or meters of seawater (msw).
C     The allowable gradient is computed using the quadratic formula (refer to
C     separate write-up posted on the Deco List web site).
C===============================================================================
      Lambda_Pascals_Parameter = (Crit_Volume_Parameter_Lambda/33.0)
     *                            * 101325.0
      DO I = 1,16
          Phase_Volume_Time(I) = Deco_Phase_Volume_Time +
     *        Surface_Phase_Volume_Time(I)
      END DO

      DO I = 1,16
          Adj_Crush_Pressure_He_Pascals =
     *        (Adjusted_Crushing_Pressure_He(I)/Units_Factor) * 101325.0

          Initial_Allowable_Grad_He_Pa =
     *        (Initial_Allowable_Gradient_He(I)/Units_Factor) * 101325.0 

          B = Initial_Allowable_Grad_He_Pa +
     *        (Lambda_Pascals_Parameter*Surface_Tension_Gamma)/
     *        (Skin_Compression_GammaC*Phase_Volume_Time(I))

          C = (Surface_Tension_Gamma*(Surface_Tension_Gamma*
     *        (Lambda_Pascals_Parameter*
     *        Adj_Crush_Pressure_He_Pascals)))
     *        /(Skin_Compression_GammaC*(Skin_Compression_GammaC*
     *        Phase_Volume_Time(I)))

          New_Allowable_Grad_He_Pascals = (B + SQRT(B**2
     *        - 4.0*C))/2.0*rapsol2

          Allowable_Gradient_He(I) =
     *        (New_Allowable_Grad_He_Pascals/101325.0)*Units_Factor
      END DO
      
            Write(8,*) 'New_Allowable_Gradient_N2(I)
     *     New_Allowable_Gradient_He(I)'
      
          DO I = 1,16
          Adj_Crush_Pressure_N2_Pascals =
     *        (Adjusted_Crushing_Pressure_N2(I)/Units_Factor) * 101325.0

          Initial_Allowable_Grad_N2_Pa =
     *        (Initial_Allowable_Gradient_N2(I)/Units_Factor) * 101325.0  

          B = Initial_Allowable_Grad_N2_Pa +
     *        (Lambda_Pascals_Parameter*Surface_Tension_Gamma)/
     *        (Skin_Compression_GammaC*Phase_Volume_Time(I))

          C = (Surface_Tension_Gamma*(Surface_Tension_Gamma*
     *        (Lambda_Pascals_Parameter*
     *        Adj_Crush_Pressure_N2_Pascals)))
     *        /(Skin_Compression_GammaC*(Skin_Compression_GammaC*
     *        Phase_Volume_Time(I)))

          New_Allowable_Grad_N2_Pascals = (B + SQRT(B**2
     *        - 4.0*C))/2.0

          Allowable_Gradient_N2(I) =
     *        (New_Allowable_Grad_N2_Pascals/101325.0)*Units_Factor
      
          Write(8,31)   1.0000*Allowable_Gradient_N2(I),
     *    1.0000*Allowable_Gradient_He(I),I
            END DO
            
 31       format(f10.3,30x,f10.3,25x,I3) 
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CALC_START_OF_DECO_ZONE
C     Purpose: This subroutine uses the Bisection Method to find the depth at
C     which the leading compartment just enters the decompression zone.
C     Source:  "Numerical Recipes in Fortran 77", Cambridge University Press,
C     1992.
C===============================================================================
      SUBROUTINE CALC_START_OF_DECO_ZONE (Starting_Depth, Rate,
     *                                    Depth_Start_of_Deco_Zone)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Starting_Depth, Rate, Depth_Start_of_Deco_Zone                 !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I, J                                                !loop counters

      REAL Initial_Helium_Pressure, Initial_Nitrogen_Pressure
      REAL Initial_Inspired_He_Pressure
      REAL Initial_Inspired_N2_Pressure
      REAL Time_to_Start_of_Deco_Zone, Helium_Rate, Nitrogen_Rate
      REAL Starting_Ambient_Pressure
      REAL Cpt_Depth_Start_of_Deco_Zone, Low_Bound, High_Bound
      REAL High_Bound_Helium_Pressure, High_Bound_Nitrogen_Pressure
      REAL Mid_Range_Helium_Pressure, Mid_Range_Nitrogen_Pressure
      REAL Function_at_High_Bound, Function_at_Low_Bound, Mid_Range_Time
      REAL Function_at_Mid_Range, Differential_Change, Last_Diff_Change

      REAL SCHREINER_EQUATION                               !function subprogram
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure
      
      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      INTEGER Mix_Number
      COMMON /Block_9/ Mix_Number

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure

      REAL Fraction_Helium(10), Fraction_Nitrogen(10)  
      COMMON /Block_5/ Fraction_Helium, Fraction_Nitrogen
C===============================================================================
C     CALCULATIONS
C     First initialize some variables
C===============================================================================
      Depth_Start_of_Deco_Zone = 0.0
      Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure

      Initial_Inspired_He_Pressure = (Starting_Ambient_Pressure -
     *        Water_Vapor_Pressure)*Fraction_Helium(Mix_Number)

      Initial_Inspired_N2_Pressure = (Starting_Ambient_Pressure -
     *        Water_Vapor_Pressure)*Fraction_Nitrogen(Mix_Number)

      Helium_Rate = Rate * Fraction_Helium(Mix_Number)
      Nitrogen_Rate = Rate * Fraction_Nitrogen(Mix_Number)
C===============================================================================
C     ESTABLISH THE BOUNDS FOR THE ROOT SEARCH USING THE BISECTION METHOD
C     AND CHECK TO MAKE SURE THAT THE ROOT WILL BE WITHIN BOUNDS.  PROCESS
C     EACH COMPARTMENT INDIVIDUALLY AND FIND THE MAXIMUM DEPTH ACROSS ALL
C     COMPARTMENTS (LEADING COMPARTMENT)
C     In this case, we are solving for time - the time when the gas tension in
C     the compartment will be equal to ambient pressure.  The low bound for time
C     is set at zero and the high bound is set at the time it would take to
C     ascend to zero ambient pressure (absolute).  Since the ascent rate is
C     negative, a multiplier of -1.0 is used to make the time positive.  The
C     desired point when gas tension equals ambient pressure is found at a time
C     somewhere between these endpoints.  The algorithm checks to make sure that
C     the solution lies in between these bounds by first computing the low bound
C     and high bound function values.
C===============================================================================
      Low_Bound = 0.0
      High_Bound = -1.0*(Starting_Ambient_Pressure/Rate)
!      Depth_Start_of_Deco_Zone=0.
      DO 200 I = 1,16
          Initial_Helium_Pressure = Helium_Pressure(I)
          Initial_Nitrogen_Pressure = Nitrogen_Pressure(I)

          Function_at_Low_Bound = Initial_Helium_Pressure +
     *        Initial_Nitrogen_Pressure + Constant_Pressure_Other_Gases
     *        - Starting_Ambient_Pressure

          High_Bound_Helium_Pressure = SCHREINER_EQUATION
     *        (Initial_Inspired_He_Pressure, Helium_Rate,
     *        High_Bound, Helium_Time_Constant(I),
     *        Initial_Helium_Pressure)     

          High_Bound_Nitrogen_Pressure = SCHREINER_EQUATION
     *        (Initial_Inspired_N2_Pressure, Nitrogen_Rate,
     *        High_Bound, Nitrogen_Time_Constant(I),
     *        Initial_Nitrogen_Pressure)    

          Function_at_High_Bound = High_Bound_Helium_Pressure +
     *        High_Bound_Nitrogen_Pressure+Constant_Pressure_Other_Gases      

          IF ((Function_at_High_Bound * Function_at_Low_Bound) .GE. 0.0)
     *                                                             THEN
              PRINT *,'ERROR! ROOT IS NOT WITHIN BRACKETS'
              PAUSE
          END IF
C===============================================================================
C     APPLY THE BISECTION METHOD IN SEVERAL ITERATIONS UNTIL A SOLUTION WITH
C     THE DESIRED ACCURACY IS FOUND
C     Note: the program allows for up to 100 iterations.  Normally an exit will
C     be made from the loop well before that number.  If, for some reason, the
C     program exceeds 100 iterations, there will be a pause to alert the user.
C===============================================================================
          IF (Function_at_Low_Bound .LT. 0.0) THEN
              Time_to_Start_of_Deco_Zone = Low_Bound
              Differential_Change = High_Bound - Low_Bound
          ELSE
              Time_to_Start_of_Deco_Zone = High_Bound
              Differential_Change = Low_Bound - High_Bound
          END IF
          DO 150 J = 1, 300
              Last_Diff_Change = Differential_Change
              Differential_Change = Last_Diff_Change*0.5

              Mid_Range_Time = Time_to_Start_of_Deco_Zone +
     *                         Differential_Change

              Mid_Range_Helium_Pressure = SCHREINER_EQUATION
     *            (Initial_Inspired_He_Pressure, Helium_Rate,
     *            Mid_Range_Time, Helium_Time_Constant(I),
     *            Initial_Helium_Pressure)     

              Mid_Range_Nitrogen_Pressure = SCHREINER_EQUATION
     *            (Initial_Inspired_N2_Pressure, Nitrogen_Rate,
     *            Mid_Range_Time, Nitrogen_Time_Constant(I),
     *            Initial_Nitrogen_Pressure)     

              Function_at_Mid_Range =
     *            Mid_Range_Helium_Pressure +
     *            Mid_Range_Nitrogen_Pressure +
     *            Constant_Pressure_Other_Gases -
     *            (Starting_Ambient_Pressure + Rate*Mid_Range_Time)      

              IF (Function_at_Mid_Range .LE. 0.0)
     *            Time_to_Start_of_Deco_Zone = Mid_Range_Time

              IF ((ABS(Differential_Change) .LT. 1.0E-3) .OR.
     *            (Function_at_Mid_Range .EQ. 0.0)) GOTO 170
150       CONTINUE
          PRINT *,'ERROR! ROOT SEARCH EXCEEDED MAXIMUM ITERATIONS'
          PAUSE '1'
C===============================================================================
C     When a solution with the desired accuracy is found, the program jumps out
C     of the loop to Line 170 and assigns the solution value for the individual
C     compartment.
C===============================================================================
170   Cpt_Depth_Start_of_Deco_Zone = (Starting_Ambient_Pressure +
     *        Rate*Time_to_Start_of_Deco_Zone) - Barometric_Pressure
C===============================================================================
C     The overall solution will be the compartment with the maximum depth where
C     gas tension equals ambient pressure (leading compartment).
C===============================================================================
!      if(Depth_Start_of_Deco_Zone.lt.Cpt_Depth_Start_of_Deco_Zone)
!     #   Depth_Start_of_Deco_Zone = Cpt_Depth_Start_of_Deco_Zone
     
      Depth_Start_of_Deco_Zone = MAX(Depth_Start_of_Deco_Zone,
     *        Cpt_Depth_Start_of_Deco_Zone)
      
200   CONTINUE
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE PROJECTED_ASCENT
C     Purpose: This subprogram performs a simulated ascent outside of the main
C     program to ensure that a deco ceiling will not be violated due to unusual
C     gas loading during ascent (on-gassing).  If the deco ceiling is violated,
C     the stop depth will be adjusted deeper by the step size until a safe
C     ascent can be made.
C===============================================================================
      SUBROUTINE PROJECTED_ASCENT (Starting_Depth, Rate,
     *                             Deco_Stop_Depth, Step_Size)
      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Starting_Depth, Rate, Step_Size                                !input
      REAL Deco_Stop_Depth                                     !input and output
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Initial_Inspired_He_Pressure, Initial_Inspired_N2_Pressure
      REAL Helium_Rate, Nitrogen_Rate 
      REAL Starting_Ambient_Pressure, Ending_Ambient_Pressure
      REAL New_Ambient_Pressure, Segment_Time
      REAL Temp_Helium_Pressure, Temp_Nitrogen_Pressure
      REAL Weighted_Allowable_Gradient
            
!      common /gradienti/Weighted_Allowable_Gradient

      REAL SCHREINER_EQUATION                               !function subprogram
C===============================================================================
C     LOCAL ARRAYS
C===============================================================================
      REAL Initial_Helium_Pressure(16), Initial_Nitrogen_Pressure(16) 
      REAL Temp_Gas_Loading(16), Allowable_Gas_Loading (16)
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure
      
      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      INTEGER Mix_Number
      COMMON /Block_9/ Mix_Number

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                     !input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure

      REAL Fraction_Helium(10), Fraction_Nitrogen(10)  
      COMMON /Block_5/ Fraction_Helium, Fraction_Nitrogen

      REAL Allowable_Gradient_He(16), Allowable_Gradient_N2 (16)          !input
      COMMON /Block_26/ Allowable_Gradient_He, Allowable_Gradient_N2
C===============================================================================
C     CALCULATIONS
C===============================================================================
      New_Ambient_Pressure = Deco_Stop_Depth + Barometric_Pressure
      Starting_Ambient_Pressure = Starting_Depth + Barometric_Pressure

      Initial_Inspired_He_Pressure = (Starting_Ambient_Pressure -
     *        Water_Vapor_Pressure)*Fraction_Helium(Mix_Number)

      Initial_Inspired_N2_Pressure = (Starting_Ambient_Pressure -
     *        Water_Vapor_Pressure)*Fraction_Nitrogen(Mix_Number)

      Helium_Rate = Rate * Fraction_Helium(Mix_Number)
      Nitrogen_Rate = Rate * Fraction_Nitrogen(Mix_Number)
      DO I = 1,16
          Initial_Helium_Pressure(I) = Helium_Pressure(I)
          Initial_Nitrogen_Pressure(I) = Nitrogen_Pressure(I)
      END DO
665   Ending_Ambient_Pressure = New_Ambient_Pressure

      Segment_Time = (Ending_Ambient_Pressure -
     *    Starting_Ambient_Pressure)/Rate

      DO 670 I = 1,16
          Temp_Helium_Pressure = SCHREINER_EQUATION
     *        (Initial_Inspired_He_Pressure, Helium_Rate,
     *        Segment_Time, Helium_Time_Constant(I),
     *        Initial_Helium_Pressure(I))     

          Temp_Nitrogen_Pressure = SCHREINER_EQUATION
     *        (Initial_Inspired_N2_Pressure, Nitrogen_Rate,
     *        Segment_Time, Nitrogen_Time_Constant(I),
     *        Initial_Nitrogen_Pressure(I))     

          Temp_Gas_Loading(I) = Temp_Helium_Pressure +
     *        Temp_Nitrogen_Pressure

      IF (Temp_Gas_Loading(I) .GT. 0.0) THEN
          Weighted_Allowable_Gradient =
     *    (Allowable_Gradient_He(I)* Temp_Helium_Pressure +
     *    Allowable_Gradient_N2(I)* Temp_Nitrogen_Pressure) /
     *    Temp_Gas_Loading(I)       
      ELSE
          Weighted_Allowable_Gradient =
     *    MIN(Allowable_Gradient_He(I),Allowable_Gradient_N2(I)) 
      END IF
      
          Allowable_Gas_Loading(I) = Ending_Ambient_Pressure +
     *       Weighted_Allowable_Gradient - Constant_Pressure_Other_Gases
      
670   CONTINUE
      DO 671 I = 1,16
          IF (Temp_Gas_Loading(I) .GT. Allowable_Gas_Loading(I)) THEN
              New_Ambient_Pressure = Ending_Ambient_Pressure + Step_Size
              Deco_Stop_Depth = Deco_Stop_Depth + Step_Size
              GOTO 665
          END IF    
671   CONTINUE
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE BOYLES_LAW_COMPENSATION
C     Purpose: This subprogram calculates the reduction in allowable gradients
C     with decreasing ambient pressure during the decompression profile based
C     on Boyle's Law considerations.
C===============================================================================
      SUBROUTINE BOYLES_LAW_COMPENSATION (First_Stop_Depth,
     *                                    Deco_Stop_Depth, Step_Size)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL First_Stop_Depth, Deco_Stop_Depth, Step_Size                   !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Next_Stop,D
      REAL Ambient_Pressure_First_Stop, Ambient_Pressure_Next_Stop
      REAL Amb_Press_First_Stop_Pascals, Amb_Press_Next_Stop_Pascals
      REAL A, B, C, Low_Bound, High_Bound, Ending_Radius
      REAL Deco_Gradient_Pascals
      REAL Allow_Grad_First_Stop_He_Pa, Radius_First_Stop_He
      REAL Allow_Grad_First_Stop_N2_Pa, Radius_First_Stop_N2

C===============================================================================
C     LOCAL ARRAYS
C===============================================================================
      REAL Radius1_He(16), Radius2_He(16) 
      REAL Radius1_N2(16), Radius2_N2(16) 

C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2

C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure

      REAL Units_Factor
      COMMON /Block_16/ Units_Factor
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Allowable_Gradient_He(16), Allowable_Gradient_N2(16)
      COMMON /Block_26/ Allowable_Gradient_He, Allowable_Gradient_N2

      REAL Deco_Gradient_He(16), Deco_Gradient_N2(16)                                
      COMMON /Block_34/ Deco_Gradient_He, Deco_Gradient_N2
C===============================================================================
C     CALCULATIONS
C===============================================================================
      Next_Stop = Deco_Stop_Depth - Step_Size
      
      Ambient_Pressure_First_Stop = First_Stop_Depth +
     *                              Barometric_Pressure

      Ambient_Pressure_Next_Stop = Next_Stop + Barometric_Pressure

      Amb_Press_First_Stop_Pascals =
     *        (Ambient_Pressure_First_Stop/Units_Factor) * 101325.0

      Amb_Press_Next_Stop_Pascals =
     *        (Ambient_Pressure_Next_Stop/Units_Factor) * 101325.0


      
      write(8,*) 'Radius2_N2(I) Deco_Gradient_N2(I)   (Boyle)'
      DO I = 1,16
      Allow_Grad_First_Stop_N2_Pa =
     *          (Allowable_Gradient_N2(I)/Units_Factor) * 101325.0

      Radius_First_Stop_N2 = (2.0 * Surface_Tension_Gamma) /
     *                        Allow_Grad_First_Stop_N2_Pa

      Radius1_N2(I) = Radius_First_Stop_N2
      A = Amb_Press_Next_Stop_Pascals
      B = -2.0 * Surface_Tension_Gamma
      C = (Amb_Press_First_Stop_Pascals + (2.0*Surface_Tension_Gamma)/
     *       Radius_First_Stop_N2)* Radius_First_Stop_N2*
     *       (Radius_First_Stop_N2*(Radius_First_Stop_N2))
      Low_Bound = Radius_First_Stop_N2
      High_Bound = Radius_First_Stop_N2*(Amb_Press_First_Stop_Pascals/
     *   Amb_Press_Next_Stop_Pascals)**(1.0/3.0)
      
      D = ( B**3 + 27./2.* A**2*C + 3./2.*Sqrt(3.)*A* 
     #    Sqrt(4.* B**3 *C + 27. *A**2* C**2))**(1./3.)
      
      Ending_Radius = 1./3.*(B/A + B**2/(A*D) + D/A)
      
!      Write(6,*) 'A,B,C,Ending_Radius',A,B,C,Ending_Radius
!      pause
             
!      write(6,*) 'Ending_Radius',Ending_Radius
!      pause 'ending radius 3'
!      CALL RADIUS_ROOT_FINDER (A,B,C, Low_Bound, High_Bound,
!     *                               Ending_Radius)
!              write(6,*) 'Ending_Radius_N2',Ending_Radius
!      pause 'ending radius 33'     
      Radius2_N2(I) = Ending_Radius
      Deco_Gradient_Pascals = (2.0 * Surface_Tension_Gamma) /
     *                        Ending_Radius
     
      Deco_Gradient_N2(I) = (Deco_Gradient_Pascals / 101325.0)*
     *                       Units_Factor
      write(8,*) Radius2_N2(I),1.0000*Deco_Gradient_N2(I)
      END DO
      
            write(8,*) 'Radius2_He(I) Deco_Gradient_He(I)   (Boyle)'
      DO I = 1,16
      Allow_Grad_First_Stop_He_Pa =
     *          (Allowable_Gradient_He(I)/Units_Factor) * 101325.0

      Radius_First_Stop_He = (2.0 * Surface_Tension_Gamma) /
     *                        Allow_Grad_First_Stop_He_Pa

      Radius1_He(I) = Radius_First_Stop_He
      A = Amb_Press_Next_Stop_Pascals
      B = -2.0 * Surface_Tension_Gamma
      C = (Amb_Press_First_Stop_Pascals + (2.0*Surface_Tension_Gamma)/
     *       Radius_First_Stop_He)* Radius_First_Stop_He*
     *       (Radius_First_Stop_He*(Radius_First_Stop_He))
      Low_Bound = Radius_First_Stop_He
      High_Bound = Radius_First_Stop_He*(Amb_Press_First_Stop_Pascals/
     *   Amb_Press_Next_Stop_Pascals)**(1.0/3.0)
      
      D = ( B**3 + 27./2.* A**2*C + 3./2.*Sqrt(3.)*A* 
     #    Sqrt(4.* B**3 *C + 27. *A**2* C**2))**(1./3.) 
      
      Ending_Radius = 1./3.*(B/A + B**2/(A*D) + D/A)
      
!      write(6,*) 'Ending_Radius',Ending_Radius
!      pause 'ending radius 4' 
      
!      CALL RADIUS_ROOT_FINDER (A,B,C, Low_Bound, High_Bound,
!     *                               Ending_Radius)
      
!             write(6,*) 'Ending_Radius',Ending_Radius
!      pause 'ending radius 44' 
      
      Radius2_He(I) = Ending_Radius
      Deco_Gradient_Pascals = (2.0 * Surface_Tension_Gamma) /
     *                        Ending_Radius
     
      Deco_Gradient_He(I) = (Deco_Gradient_Pascals / 101325.0)*
     *                       Units_Factor
      
      write(8,*) Radius2_He(I),1.0000*Deco_Gradient_He(I)
      END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END

C===============================================================================
C     SUBROUTINE DECOMPRESSION_STOP
C     Purpose: This subprogram calculates the required time at each
C     decompression stop.
C===============================================================================
      SUBROUTINE DECOMPRESSION_STOP (Deco_Stop_Depth, Step_Size)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Deco_Stop_Depth, Step_Size                                     !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      CHARACTER OS_Command*3

      INTEGER I                                                    !loop counter
      INTEGER Last_Segment_Number

      REAL Ambient_Pressure
      REAL Inspired_Helium_Pressure, Inspired_Nitrogen_Pressure
      REAL Last_Run_Time
      REAL Deco_Ceiling_Depth, Next_Stop
      REAL Round_Up_Operation, Temp_Segment_Time, Time_Counter
      REAL Weighted_Allowable_Gradient
!      common /gradienti/Weighted_Allowable_Gradient
      REAL HALDANE_EQUATION                                 !function subprogram
C===============================================================================
C     LOCAL ARRAYS
C===============================================================================
      REAL Initial_Helium_Pressure(16)
      REAL Initial_Nitrogen_Pressure(16)
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure

      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases

      REAL Minimum_Deco_Stop_Time
      COMMON /Block_21/ Minimum_Deco_Stop_Time
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      INTEGER Segment_Number
      REAL Run_Time, Segment_Time
      COMMON /Block_2/ Run_Time, Segment_Number, Segment_Time

      REAL Ending_Ambient_Pressure 
      COMMON /Block_4/ Ending_Ambient_Pressure
      
      INTEGER Mix_Number
      COMMON /Block_9/ Mix_Number

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                !both input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure            !and output

      REAL Fraction_Helium(10), Fraction_Nitrogen(10)  
      COMMON /Block_5/ Fraction_Helium, Fraction_Nitrogen

      REAL Deco_Gradient_He(16), Deco_Gradient_N2(16)                                
      COMMON /Block_34/ Deco_Gradient_He, Deco_Gradient_N2
      Real DCD(10,16)
      integer ipilot,jmax(16)
      common /pilota/ipilot,jmax,DCD
C===============================================================================
C     CALCULATIONS
C===============================================================================
      OS_Command = 'CLS'
      Last_Run_Time = Run_Time
      Round_Up_Operation = ANINT((Last_Run_Time/Minimum_Deco_Stop_Time)
     *                            + 0.5) * Minimum_Deco_Stop_Time
      Segment_Time = Round_Up_Operation - Run_Time
      Run_Time = Round_Up_Operation
      Temp_Segment_Time = Segment_Time
      Last_Segment_Number = Segment_Number
      Segment_Number = Last_Segment_Number + 1
      Ambient_Pressure = Deco_Stop_Depth + Barometric_Pressure
      Ending_Ambient_Pressure = Ambient_Pressure
      Next_Stop = Deco_Stop_Depth - Step_Size

      Inspired_Helium_Pressure = (Ambient_Pressure -
     *    Water_Vapor_Pressure)*Fraction_Helium(Mix_Number)

      Inspired_Nitrogen_Pressure = (Ambient_Pressure -
     *    Water_Vapor_Pressure)*Fraction_Nitrogen(Mix_Number)
C===============================================================================
C     Check to make sure that program won't lock up if unable to decompress
C     to the next stop.  If so, write error message and terminate program.
C===============================================================================
      DO I = 1,16
      IF ((Inspired_Helium_Pressure + Inspired_Nitrogen_Pressure)
     *                                                  .GT. 0.0) THEN
          Weighted_Allowable_Gradient =
     *    (Deco_Gradient_He(I)* Inspired_Helium_Pressure +
     *     Deco_Gradient_N2(I)* Inspired_Nitrogen_Pressure) /
     *    (Inspired_Helium_Pressure + Inspired_Nitrogen_Pressure) 
          
!          write(9,*) ' Weighted_Allowable_Gradient,Run_time,I',
!     #                 Weighted_Allowable_Gradient,Run_time,I
!          pause
          
          
          IF ((Inspired_Helium_Pressure + Inspired_Nitrogen_Pressure +
     *      Constant_Pressure_Other_Gases - Weighted_Allowable_Gradient)
     *      .GT. (Next_Stop + Barometric_Pressure)) THEN
              CALL SYSTEMQQ (OS_Command)
              WRITE (*,905) Deco_Stop_Depth           
              WRITE (*,906)    
              WRITE (*,907)    
              STOP 'PROGRAM TERMINATED'
          END IF
      END IF
      END DO
      
! il loop che segue serve a identificare il "Minimum_Deco_Stop_Time"
! cio quello che garantisce che il Deco_Ceiling_Depth di tutti i compartimenti risulti inferiore al Next_Stop
      
700   DO 720 I = 1,16
          Initial_Helium_Pressure(I) = Helium_Pressure(I)
          Initial_Nitrogen_Pressure(I) = Nitrogen_Pressure(I)

          Helium_Pressure(I) = HALDANE_EQUATION
     *    (Initial_Helium_Pressure(I), Inspired_Helium_Pressure,
     *    Helium_Time_Constant(I), Segment_Time)

          Nitrogen_Pressure(I) = HALDANE_EQUATION
     *    (Initial_Nitrogen_Pressure(I), Inspired_Nitrogen_Pressure,
     *    Nitrogen_Time_Constant(I), Segment_Time)

720   CONTINUE
      CALL CALC_DECO_CEILING (Deco_Ceiling_Depth)
      
!      write(9,*) 'Deco_Stop_Depth,run_time,ipilot,zz',
!     #            Deco_Stop_Depth,run_time,ipilot

!      pause
      
      IF (Deco_Ceiling_Depth .GT. Next_Stop) THEN
           Segment_Time = Minimum_Deco_Stop_Time
           Time_Counter = Temp_Segment_Time
           Temp_Segment_Time =  Time_Counter + Minimum_Deco_Stop_Time
           Last_Run_Time = Run_Time
           Run_Time = Last_Run_Time + Minimum_Deco_Stop_Time
           GOTO 700
      END IF
      Segment_Time = Temp_Segment_Time
!      write(6,*) 'segment_Time ,run_time,ipilot',
!     #            segment_Time,run_time ,ipilot
!      pause
      RETURN
C===============================================================================
C     FORMAT STATEMENTS - ERROR MESSAGES
C===============================================================================
905   FORMAT ('0ERROR! OFF-GASSING GRADIENT IS TOO SMALL TO DECOMPRESS'
     *1X,'AT THE',F6.1,1X,'STOP')
906   FORMAT ('0REDUCE STEP SIZE OR INCREASE OXYGEN FRACTION')
907   FORMAT (' ')
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      END


C===============================================================================
C     SUBROUTINE CALC_DECO_CEILING
C     Purpose: This subprogram calculates the deco ceiling (the safe ascent
C     depth) in each compartment, based on the allowable "deco gradients"
C     computed in the Boyle's Law Compensation subroutine, and then finds the
C     deepest deco ceiling across all compartments.  This deepest value
C     (Deco Ceiling Depth) is then used by the Decompression Stop subroutine
C     to determine the actual deco schedule.
C===============================================================================
      SUBROUTINE CALC_DECO_CEILING (Deco_Ceiling_Depth)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Deco_Ceiling_Depth                                            !output
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Gas_Loading, Weighted_Allowable_Gradient
          
!      common /gradienti/Weighted_Allowable_Gradient
      REAL Tolerated_Ambient_Pressure
C===============================================================================
C     LOCAL ARRAYS
C===============================================================================
      REAL Compartment_Deco_Ceiling(16)
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                     !input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure

      REAL Deco_Gradient_He(16), Deco_Gradient_N2(16)                     !input                                
      COMMON /Block_34/ Deco_Gradient_He, Deco_Gradient_N2
      
      integer ipilot,jmax(16),jj
      Real DCD(10,16)
      common /pilota/ipilot,jmax,DCD
C===============================================================================
C     CALCULATIONS
C     Since there are two sets of deco gradients being tracked, one for
C     helium and one for nitrogen, a "weighted allowable gradient" must be
C     computed each time based on the proportions of helium and nitrogen in
C     each compartment.  This proportioning follows the methodology of
C     Buhlmann/Keller.  If there is no helium and nitrogen in the compartment,
C     such as after extended periods of oxygen breathing, then the minimum value
C     across both gases will be used.  It is important to note that if a
C     compartment is empty of helium and nitrogen, then the weighted allowable
C     gradient formula cannot be used since it will result in division by zero.
C===============================================================================
      DO I = 1,16
          Gas_Loading = Helium_Pressure(I) + Nitrogen_Pressure(I)
      
      IF (Gas_Loading .GT. 0.0) THEN
          Weighted_Allowable_Gradient =
     *    (Deco_Gradient_He(I)* Helium_Pressure(I) +
     *     Deco_Gradient_N2(I)* Nitrogen_Pressure(I)) /
     *    (Helium_Pressure(I) + Nitrogen_Pressure(I))

          Tolerated_Ambient_Pressure = (Gas_Loading +
     *    Constant_Pressure_Other_Gases) - Weighted_Allowable_Gradient

      ELSE
          Weighted_Allowable_Gradient =
     *    MIN(Deco_Gradient_He(I), Deco_Gradient_N2(I)) 

          Tolerated_Ambient_Pressure =
     *    Constant_Pressure_Other_Gases - Weighted_Allowable_Gradient
      END IF
C===============================================================================
C     The tolerated ambient pressure cannot be less than zero absolute, i.e.,
C     the vacuum of outer space!
C===============================================================================
      IF (Tolerated_Ambient_Pressure .LT. 0.0) THEN
          Tolerated_Ambient_Pressure = 0.0
      END IF
C===============================================================================
C     The Deco Ceiling Depth is computed in a loop after all of the individual
C     compartment deco ceilings have been calculated.  It is important that the
C     Deco Ceiling Depth (max deco ceiling across all compartments) only be
C     extracted from the compartment values and not be compared against some
C     initialization value.  For example, if MAX(Deco_Ceiling_Depth . .) was
C     compared against zero, this could cause a program lockup because sometimes
C     the Deco Ceiling Depth needs to be negative (but not less than absolute
C     zero) in order to decompress to the last stop at zero depth.
C===============================================================================
          Compartment_Deco_Ceiling(I) =
     *        Tolerated_Ambient_Pressure - Barometric_Pressure

      END DO
     
      Deco_Ceiling_Depth = Compartment_Deco_Ceiling(1)
      ipilot=1

      DO I = 2,16
          
!         write(9,*) 'Deco_Ceiling_Depth:',Deco_Ceiling_Depth,' ',
        
!     #   'Compartment_Deco_Ceiling(I):', Compartment_Deco_Ceiling(I)

      
      if(Deco_Ceiling_Depth.le.Compartment_Deco_Ceiling(I)) then
 
        ipilot=I        
 
      Deco_Ceiling_Depth=Compartment_Deco_Ceiling(I)    

      end if   
 
!          Deco_Ceiling_Depth =
!     *        MAX(Deco_Ceiling_Depth, Compartment_Deco_Ceiling(I))
!          write(9,31) Deco_Ceiling_Depth,I

      END DO 
!            write(9,*) 
      
31    format(62x,f10.6,i3)
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE GAS_LOADINGS_SURFACE_INTERVAL
C     Purpose: This subprogram calculates the gas loading (off-gassing) during
C     a surface interval.
C===============================================================================
      SUBROUTINE GAS_LOADINGS_SURFACE_INTERVAL (Surface_Interval_Time)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Surface_Interval_Time                                          !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter

      REAL Inspired_Helium_Pressure, Inspired_Nitrogen_Pressure
      REAL Initial_Helium_Pressure, Initial_Nitrogen_Pressure

      REAL HALDANE_EQUATION                                 !function subprogram
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Helium_Time_Constant(16)
      COMMON /Block_1A/ Helium_Time_Constant

      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                !both input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure            !and output
C===============================================================================
C     CALCULATIONS
C===============================================================================
      Inspired_Helium_Pressure = 0.0
      Inspired_Nitrogen_Pressure = (Barometric_Pressure -
     *        Water_Vapor_Pressure)*0.79
      DO I = 1,16
          Initial_Helium_Pressure = Helium_Pressure(I)
          Initial_Nitrogen_Pressure = Nitrogen_Pressure(I)

          Helium_Pressure(I) = HALDANE_EQUATION
     *    (Initial_Helium_Pressure, Inspired_Helium_Pressure,
     *    Helium_Time_Constant(I), Surface_Interval_Time)

          Nitrogen_Pressure(I) = HALDANE_EQUATION
     *    (Initial_Nitrogen_Pressure, Inspired_Nitrogen_Pressure,
     *    Nitrogen_Time_Constant(I), Surface_Interval_Time)
      END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE VPM_REPETITIVE_ALGORITHM
C     Purpose: This subprogram implements the VPM Repetitive Algorithm that was
C     envisioned by Professor David E. Yount only months before his passing.
C===============================================================================
      SUBROUTINE VPM_REPETITIVE_ALGORITHM (Surface_Interval_Time)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Surface_Interval_Time                                          !input
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER I                                                    !loop counter   
  
      REAL Max_Actual_Gradient_Pascals
      REAL Adj_Crush_Pressure_He_Pascals, Adj_Crush_Pressure_N2_Pascals 
      REAL Initial_Allowable_Grad_He_Pa, Initial_Allowable_Grad_N2_Pa
      REAL New_Critical_Radius_He, New_Critical_Radius_N2
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2

      REAL Regeneration_Time_Constant
      COMMON /Block_22/ Regeneration_Time_Constant
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Units_Factor
      COMMON /Block_16/ Units_Factor
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Initial_Critical_Radius_He(16)                                 !input
      REAL Initial_Critical_Radius_N2(16)      
      COMMON /Block_6/ Initial_Critical_Radius_He,
     *           Initial_Critical_Radius_N2     

      REAL Adjusted_Critical_Radius_He(16)                               !output
      REAL Adjusted_Critical_Radius_N2(16)
      COMMON /Block_7/ Adjusted_Critical_Radius_He,
     *                 Adjusted_Critical_Radius_N2 

      REAL Max_Actual_Gradient(16)                                        !input
      COMMON /Block_12/ Max_Actual_Gradient

      REAL Adjusted_Crushing_Pressure_He(16)                              !input
      REAL Adjusted_Crushing_Pressure_N2(16)       
      COMMON /Block_25/ Adjusted_Crushing_Pressure_He,
     *                  Adjusted_Crushing_Pressure_N2

      REAL Initial_Allowable_Gradient_He(16)                              !input
      REAL Initial_Allowable_Gradient_N2(16)
      COMMON /Block_27/
     *    Initial_Allowable_Gradient_He, Initial_Allowable_Gradient_N2
C===============================================================================
C     CALCULATIONS
C===============================================================================
      DO I = 1,16
          Max_Actual_Gradient_Pascals =
     *        (Max_Actual_Gradient(I)/Units_Factor) * 101325.0

          Adj_Crush_Pressure_He_Pascals =
     *        (Adjusted_Crushing_Pressure_He(I)/Units_Factor) * 101325.0

          Adj_Crush_Pressure_N2_Pascals =
     *        (Adjusted_Crushing_Pressure_N2(I)/Units_Factor) * 101325.0

          Initial_Allowable_Grad_He_Pa =
     *        (Initial_Allowable_Gradient_He(I)/Units_Factor) * 101325.0 

          Initial_Allowable_Grad_N2_Pa =
     *        (Initial_Allowable_Gradient_N2(I)/Units_Factor) * 101325.0  

          IF (Max_Actual_Gradient(I)
     *                       .GT. Initial_Allowable_Gradient_N2(I)) THEN

              New_Critical_Radius_N2 = ((2.0*Surface_Tension_Gamma*
     *        (Skin_Compression_GammaC - Surface_Tension_Gamma))) /
     *        (Max_Actual_Gradient_Pascals*Skin_Compression_GammaC -
     *        Surface_Tension_Gamma*Adj_Crush_Pressure_N2_Pascals)

              Adjusted_Critical_Radius_N2(I) =
     *        Initial_Critical_Radius_N2(I) +
     *        (Initial_Critical_Radius_N2(I)-New_Critical_Radius_N2)*
     *        EXP(-Surface_Interval_Time/Regeneration_Time_Constant)
          ELSE
              Adjusted_Critical_Radius_N2(I) =
     *        Initial_Critical_Radius_N2(I)
          END IF

          IF (Max_Actual_Gradient(I)
     *                       .GT. Initial_Allowable_Gradient_He(I)) THEN

              New_Critical_Radius_He = ((2.0*Surface_Tension_Gamma*
     *        (Skin_Compression_GammaC - Surface_Tension_Gamma))) /
     *        (Max_Actual_Gradient_Pascals*Skin_Compression_GammaC -
     *        Surface_Tension_Gamma*Adj_Crush_Pressure_He_Pascals)

              Adjusted_Critical_Radius_He(I) =
     *        Initial_Critical_Radius_He(I) +
     *        (Initial_Critical_Radius_He(I)-New_Critical_Radius_He)*
     *        EXP(-Surface_Interval_Time/Regeneration_Time_Constant)

          ELSE
              Adjusted_Critical_Radius_He(I) =
     *        Initial_Critical_Radius_He(I)
          END IF
      END DO
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END


C===============================================================================
C     SUBROUTINE CALC_BAROMETRIC_PRESSURE
C     Purpose: This sub calculates barometric pressure at altitude based on the
C     publication "U.S. Standard Atmosphere, 1976", U.S. Government Printing
C     Office, Washington, D.C. The source for this code is a Fortran 90 program
C     written by Ralph L. Carmichael (retired NASA researcher) and endorsed by
C     the National Geophysical Data Center of the National Oceanic and 
C     Atmospheric Administration.  It is available for download free from 
C     Public Domain Aeronautical Software at:  http://www.pdas.com/atmos.htm 
C===============================================================================
      SUBROUTINE CALC_BAROMETRIC_PRESSURE (Altitude)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      REAL Altitude                                                       !input
C===============================================================================
C     LOCAL CONSTANTS
C===============================================================================
      REAL Radius_of_Earth, Acceleration_of_Gravity
      REAL Molecular_weight_of_Air, Gas_Constant_R
      REAL Temp_at_Sea_Level, Temp_Gradient
      REAL Pressure_at_Sea_Level_Fsw, Pressure_at_Sea_Level_Msw
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      REAL Pressure_at_Sea_Level, GMR_Factor
      REAL Altitude_Feet, Altitude_Meters
      REAL Altitude_Kilometers, Geopotential_Altitude
      REAL Temp_at_Geopotential_Altitude
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      LOGICAL Units_Equal_Fsw, Units_Equal_Msw
      COMMON /Block_15/ Units_Equal_Fsw, Units_Equal_Msw

      REAL Barometric_Pressure                                           !output
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     CALCULATIONS
C===============================================================================
      Radius_of_Earth = 6369.0                                       !kilometers
      Acceleration_of_Gravity = 9.80665                         !meters/second^2
      Molecular_weight_of_Air = 28.9644                                    !mols
      Gas_Constant_R = 8.31432                            !Joules/mol*deg Kelvin
      Temp_at_Sea_Level = 288.15                                 !degrees Kelvin

      Pressure_at_Sea_Level_Fsw = 33.0      !feet of seawater based on 101325 Pa
                                             !at sea level (Standard Atmosphere) 

      Pressure_at_Sea_Level_Msw = 10.0    !meters of seawater based on 100000 Pa
                                                 !at sea level (European System) 
      
      Temp_Gradient = -6.5                       !Change in Temp deg Kelvin with
                                               !change in geopotential altitude, 
                                            !valid for first layer of atmosphere
                                             !up to 11 kilometers or 36,000 feet

      GMR_Factor = Acceleration_of_Gravity *
     *             Molecular_weight_of_Air / Gas_Constant_R

      IF (Units_Equal_Fsw) THEN
          Altitude_Feet = Altitude
          Altitude_Kilometers = Altitude_Feet / 3280.839895
          Pressure_at_Sea_Level = Pressure_at_Sea_Level_Fsw
      END IF
      IF (Units_Equal_Msw) THEN      
          Altitude_Meters = Altitude
          Altitude_Kilometers = Altitude_Meters / 1000.0
          Pressure_at_Sea_Level = Pressure_at_Sea_Level_Msw
      END IF
          
      Geopotential_Altitude =  (Altitude_Kilometers * Radius_of_Earth) /
     *                         (Altitude_Kilometers + Radius_of_Earth)

      Temp_at_Geopotential_Altitude = Temp_at_Sea_Level
     *              + Temp_Gradient * Geopotential_Altitude

      Barometric_Pressure = Pressure_at_Sea_Level *
     *    EXP(ALOG(Temp_at_Sea_Level / Temp_at_Geopotential_Altitude) *
     *    GMR_Factor / Temp_Gradient)
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END      


C===============================================================================
C     SUBROUTINE VPM_ALTITUDE_DIVE_ALGORITHM
C     Purpose:  This subprogram updates gas loadings and adjusts critical radii
C     (as required) based on whether or not diver is acclimatized at altitude or
C     makes an ascent to altitude before the dive.
C===============================================================================
      SUBROUTINE VPM_ALTITUDE_DIVE_ALGORITHM

      IMPLICIT NONE
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      CHARACTER Diver_Acclimatized_at_Altitude*3, OS_Command*3

      INTEGER I                                                    !loop counter  
      LOGICAL Diver_Acclimatized

      REAL Altitude_of_Dive, Starting_Acclimatized_Altitude
      REAL Ascent_to_Altitude_Hours, Hours_at_Altitude_Before_Dive
      REAL Ascent_to_Altitude_Time, Time_at_Altitude_Before_Dive 
      REAL Starting_Ambient_Pressure, Ending_Ambient_Pressure
      REAL Initial_Inspired_N2_Pressure, Rate, Nitrogen_Rate
      REAL Inspired_Nitrogen_Pressure, Initial_Nitrogen_Pressure
      REAL Compartment_Gradient, Compartment_Gradient_Pascals
      REAL Gradient_He_Bubble_Formation, Gradient_N2_Bubble_Formation
      REAL New_Critical_Radius_He, New_Critical_Radius_N2
      REAL Ending_Radius_He, Ending_Radius_N2
      REAL Regenerated_Radius_He, Regenerated_Radius_N2

      REAL HALDANE_EQUATION                                 !function subprogram

      REAL SCHREINER_EQUATION                               !function subprogram
C===============================================================================
C     GLOBAL CONSTANTS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Water_Vapor_Pressure
      COMMON /Block_8/ Water_Vapor_Pressure

      REAL Constant_Pressure_Other_Gases
      COMMON /Block_17/ Constant_Pressure_Other_Gases

      REAL Surface_Tension_Gamma,Skin_Compression_GammaC,rapsol1,rapsol2
      COMMON /Block_19/ Surface_Tension_Gamma,Skin_Compression_GammaC
     #,rapsol1,rapsol2
      REAL Regeneration_Time_Constant
      COMMON /Block_22/ Regeneration_Time_Constant
C===============================================================================
C     GLOBAL VARIABLES IN NAMED COMMON BLOCKS
C===============================================================================
      LOGICAL Units_Equal_Fsw, Units_Equal_Msw
      COMMON /Block_15/ Units_Equal_Fsw, Units_Equal_Msw

      REAL Units_Factor
      COMMON /Block_16/ Units_Factor

      REAL Barometric_Pressure
      COMMON /Block_18/ Barometric_Pressure
C===============================================================================
C     GLOBAL ARRAYS IN NAMED COMMON BLOCKS
C===============================================================================
      REAL Nitrogen_Time_Constant(16)
      COMMON /Block_1B/ Nitrogen_Time_Constant

      REAL Helium_Pressure(16), Nitrogen_Pressure(16)                !both input
      COMMON /Block_3/ Helium_Pressure, Nitrogen_Pressure            !and output

      REAL Initial_Critical_Radius_He(16)                            !both input   
 
      REAL Initial_Critical_Radius_N2(16)                            !and output
      COMMON /Block_6/ Initial_Critical_Radius_He,
     *           Initial_Critical_Radius_N2     

      REAL Adjusted_Critical_Radius_He(16)                               !output
      REAL Adjusted_Critical_Radius_N2(16)
      COMMON /Block_7/ Adjusted_Critical_Radius_He,
     *                 Adjusted_Critical_Radius_N2 
C===============================================================================
C     NAMELIST FOR PROGRAM SETTINGS (READ IN FROM ASCII TEXT FILE)
C===============================================================================
      NAMELIST /Altitude_Dive_Settings/ Altitude_of_Dive,
     *         Diver_Acclimatized_at_Altitude,
     *         Starting_Acclimatized_Altitude, Ascent_to_Altitude_Hours,
     *         Hours_at_Altitude_Before_Dive 
C===============================================================================
C     CALCULATIONS
C===============================================================================   
  
      OS_Command = 'CLS'
      OPEN (UNIT = 12, FILE = 'ALTITUDE.SET', STATUS = 'UNKNOWN',
     *         ACCESS = 'SEQUENTIAL', FORM = 'FORMATTED')   

      READ (12,Altitude_Dive_Settings)

      IF ((Units_Equal_Fsw) .AND. (Altitude_of_Dive .GT. 30000.0)) THEN
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,900) 
          WRITE (*,901)    
          STOP 'PROGRAM TERMINATED'          
      END IF
      IF ((Units_Equal_Msw) .AND. (Altitude_of_Dive .GT. 9144.0)) THEN
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,900) 
          WRITE (*,901)    
          STOP 'PROGRAM TERMINATED'          
      END IF

      IF ((Diver_Acclimatized_at_Altitude .EQ. 'YES') .OR.
     *                 (Diver_Acclimatized_at_Altitude .EQ. 'yes')) THEN
          Diver_Acclimatized = (.TRUE.)
      ELSE IF ((Diver_Acclimatized_at_Altitude .EQ. 'NO') .OR.
     *                  (Diver_Acclimatized_at_Altitude .EQ. 'no')) THEN
          Diver_Acclimatized = (.FALSE.)
      ELSE
          CALL SYSTEMQQ (OS_Command)
          WRITE (*,902)
          WRITE (*,901)    
          STOP 'PROGRAM TERMINATED'          
      END IF    

      Ascent_to_Altitude_Time = Ascent_to_Altitude_Hours * 60.0
      Time_at_Altitude_Before_Dive = Hours_at_Altitude_Before_Dive*60.0

      IF (Diver_Acclimatized) THEN
          CALL CALC_BAROMETRIC_PRESSURE (Altitude_of_Dive)           !subroutine
          WRITE (*,802) Altitude_of_Dive, Barometric_Pressure    
          DO I = 1,16
          Adjusted_Critical_Radius_N2(I) = Initial_Critical_Radius_N2(I)
          Adjusted_Critical_Radius_He(I) = Initial_Critical_Radius_He(I)
          Helium_Pressure(I) = 0.0
          Nitrogen_Pressure(I) = (Barometric_Pressure -
     *        Water_Vapor_Pressure)*0.79
          END DO
      ELSE      
          IF ((Starting_Acclimatized_Altitude .GE. Altitude_of_Dive)
     *              .OR. (Starting_Acclimatized_Altitude .LT. 0.0)) THEN 
              CALL SYSTEMQQ (OS_Command)
              WRITE (*,903)
              WRITE (*,904)
              WRITE (*,901)    
              STOP 'PROGRAM TERMINATED'          
          END IF    
          CALL CALC_BAROMETRIC_PRESSURE                              !subroutine
     *                       (Starting_Acclimatized_Altitude)
          Starting_Ambient_Pressure = Barometric_Pressure
          DO I = 1,16
          Helium_Pressure(I) = 0.0
          Nitrogen_Pressure(I) = (Barometric_Pressure -
     *        Water_Vapor_Pressure)*0.79
          END DO
          CALL CALC_BAROMETRIC_PRESSURE (Altitude_of_Dive)           !subroutine
          WRITE (*,802) Altitude_of_Dive, Barometric_Pressure
          Ending_Ambient_Pressure = Barometric_Pressure
          Initial_Inspired_N2_Pressure = (Starting_Ambient_Pressure
     *               - Water_Vapor_Pressure)*0.79
          Rate = (Ending_Ambient_Pressure - Starting_Ambient_Pressure)
     *            / Ascent_to_Altitude_Time 
          Nitrogen_Rate = Rate*0.79

          DO I = 1,16
              Initial_Nitrogen_Pressure = Nitrogen_Pressure(I)

              Nitrogen_Pressure(I) = SCHREINER_EQUATION
     *            (Initial_Inspired_N2_Pressure, Nitrogen_Rate,
     *            Ascent_to_Altitude_Time, Nitrogen_Time_Constant(I),
     *            Initial_Nitrogen_Pressure)     

              Compartment_Gradient = (Nitrogen_Pressure(I)
     *            + Constant_Pressure_Other_Gases)
     *            - Ending_Ambient_Pressure

              Compartment_Gradient_Pascals =
     *            (Compartment_Gradient / Units_Factor) * 101325.0

              Gradient_He_Bubble_Formation =
     *        ((2.0*Surface_Tension_Gamma*
     *        (Skin_Compression_GammaC - Surface_Tension_Gamma)) /
     *        (Initial_Critical_Radius_He(I)*Skin_Compression_GammaC))

              IF (Compartment_Gradient_Pascals .GT.
     *                                Gradient_He_Bubble_Formation) THEN

                  New_Critical_Radius_He = ((2.0*Surface_Tension_Gamma*
     *            (Skin_Compression_GammaC - Surface_Tension_Gamma))) /
     *            (Compartment_Gradient_Pascals*Skin_Compression_GammaC)

                  Adjusted_Critical_Radius_He(I) =
     *            Initial_Critical_Radius_He(I) +
     *            (Initial_Critical_Radius_He(I)-
     *            New_Critical_Radius_He)*
     *            EXP(-Time_at_Altitude_Before_Dive/
     *            Regeneration_Time_Constant)

                  Initial_Critical_Radius_He(I) =
     *            Adjusted_Critical_Radius_He(I)
              ELSE
                  Ending_Radius_He = 1.0/(Compartment_Gradient_Pascals/
     *            (2.0*(Surface_Tension_Gamma-Skin_Compression_GammaC)) 
     *            + 1.0/Initial_Critical_Radius_He(I))

                  Regenerated_Radius_He =
     *            Initial_Critical_Radius_He(I) +
     *            (Ending_Radius_He - Initial_Critical_Radius_He(I)) *
     *            EXP(-Time_at_Altitude_Before_Dive/
     *            Regeneration_Time_Constant)

                  Initial_Critical_Radius_He(I) =
     *            Regenerated_Radius_He

                  Adjusted_Critical_Radius_He(I) =
     *            Initial_Critical_Radius_He(I)
              END IF

              Gradient_N2_Bubble_Formation =
     *        ((2.0*Surface_Tension_Gamma*
     *        (Skin_Compression_GammaC - Surface_Tension_Gamma)) /
     *        (Initial_Critical_Radius_N2(I)*Skin_Compression_GammaC))

              IF (Compartment_Gradient_Pascals .GT.
     *                                Gradient_N2_Bubble_Formation) THEN

                  New_Critical_Radius_N2 = ((2.0*Surface_Tension_Gamma*
     *            (Skin_Compression_GammaC - Surface_Tension_Gamma))) /
     *            (Compartment_Gradient_Pascals*Skin_Compression_GammaC)

                  Adjusted_Critical_Radius_N2(I) =
     *            Initial_Critical_Radius_N2(I) +
     *            (Initial_Critical_Radius_N2(I)-
     *            New_Critical_Radius_N2)*
     *            EXP(-Time_at_Altitude_Before_Dive/
     *            Regeneration_Time_Constant)

                  Initial_Critical_Radius_N2(I) =
     *            Adjusted_Critical_Radius_N2(I)
              ELSE
                  Ending_Radius_N2 = 1.0/(Compartment_Gradient_Pascals/
     *            (2.0*(Surface_Tension_Gamma-Skin_Compression_GammaC)) 
     *            + 1.0/Initial_Critical_Radius_N2(I))

                  Regenerated_Radius_N2 =
     *            Initial_Critical_Radius_N2(I) +
     *            (Ending_Radius_N2 - Initial_Critical_Radius_N2(I)) *
     *            EXP(-Time_at_Altitude_Before_Dive/
     *            Regeneration_Time_Constant)

                  Initial_Critical_Radius_N2(I) =
     *            Regenerated_Radius_N2

                  Adjusted_Critical_Radius_N2(I) =
     *            Initial_Critical_Radius_N2(I)
              END IF
          END DO
          Inspired_Nitrogen_Pressure = (Barometric_Pressure -
     *    Water_Vapor_Pressure)*0.79
          DO I = 1,16
              Initial_Nitrogen_Pressure = Nitrogen_Pressure(I)

              Nitrogen_Pressure(I) = HALDANE_EQUATION
     *        (Initial_Nitrogen_Pressure, Inspired_Nitrogen_Pressure,
     *        Nitrogen_Time_Constant(I), Time_at_Altitude_Before_Dive)
          END DO
      END IF
      CLOSE (UNIT = 12, STATUS = 'KEEP')
      RETURN
C===============================================================================
C     FORMAT STATEMENTS - PROGRAM OUTPUT
C===============================================================================
802   FORMAT ('0ALTITUDE = ',1X,F7.1,4X,'BAROMETRIC PRESSURE = ',
     *F6.3)
C===============================================================================
C     FORMAT STATEMENTS - ERROR MESSAGES
C===============================================================================
900   FORMAT ('0ERROR! ALTITUDE OF DIVE HIGHER THAN MOUNT EVEREST')
901   FORMAT (' ')
902   FORMAT ('0ERROR! DIVER ACCLIMATIZED AT ALTITUDE',
     *1X,'MUST BE YES OR NO')
903   FORMAT ('0ERROR! STARTING ACCLIMATIZED ALTITUDE MUST BE LESS',
     *1X,'THAN ALTITUDE OF DIVE')
904   FORMAT (' AND GREATER THAN OR EQUAL TO ZERO')
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      END


C===============================================================================
C     SUBROUTINE CLOCK
C     Purpose:  This subprogram retrieves clock information from the Microsoft
C     operating system so that date and time stamp can be included on program
C     output. 
C===============================================================================
      SUBROUTINE CLOCK (Year, Month, Day, Clock_Hour, Minute, M)

      IMPLICIT NONE
C===============================================================================
C     ARGUMENTS
C===============================================================================
      CHARACTER M*1                                                      !output
      INTEGER*2 Month, Day, Year                                         !output
      INTEGER*2 Minute, Clock_Hour                                       !output
C===============================================================================
C     LOCAL VARIABLES
C===============================================================================
      INTEGER*2 Hour, Second, Hundredth
C===============================================================================
C     CALCULATIONS
C===============================================================================
      CALL GETDAT (Year, Month, Day)                         !Microsoft run-time 
      CALL GETTIM (Hour, Minute, Second, Hundredth)                 !subroutines  
      IF (Hour .GT. 12) THEN
         Clock_Hour = Hour - 12
         M = 'p'
      ELSE
         Clock_Hour = Hour
         M = 'a'
      ENDIF
C===============================================================================
C     END OF SUBROUTINE
C===============================================================================
      RETURN
      END

"""


# =====================================================================
# ZH-L16 SINGLE-PASS SECTION (from line 7959)
# Purpose: alternative decompression engine using existing tissue kinetics
#          (Schreiner/Haldane from VPM part). Only ceiling + stop logic differs.
# Notes:
# - Uses fixed ZH-L16C a/b tables (in bar), converted to msw using MSW_PER_BAR.
# - Applies rapsol (rapsol1 from GUI) to He coefficients only (a and b transform).
# - Works in msw pressure units consistent with the VPM tissue arrays.
# =====================================================================

_MSW_PER_BAR = 9.86923  # 1 bar = 9.86923 msw when 10 msw = 1.01325 bar (standard convention)

# ZH-L16 coefficient tables
# ------------------------------------------------------------
# POLICY (project contract):
#   - The MAIN is passive regarding ZH-L16 coefficient sets.
#   - The GUI is the single source of truth for (B/C) selection and
#     any user edits to a/b coefficients for N2 and He.
#   - Therefore, the engine must receive the active coefficient table
#     from the GUI via env var VPM_ZHL16_COEFFS_JSON.
#
# Expected JSON format (length 16):
#   - list of dicts with keys: aN2, bN2, aHe, bHe (optional cmpt index)
#   - OR list of lists/tuples: [cmpt, aN2, bN2, aHe, bHe] or [aN2,bN2,aHe,bHe]
#
# Coefficients are in BAR (standard convention). Internally the engine
# will convert to msw/atm where appropriate using Units_Factor.
# ------------------------------------------------------------

_ZHL16_ACTIVE_VARIANT = ""
_ZHL16_ACTIVE_A_N2_BAR = None  # type: ignore
_ZHL16_ACTIVE_B_N2 = None      # type: ignore
_ZHL16_ACTIVE_A_HE_BAR = None  # type: ignore
_ZHL16_ACTIVE_B_HE = None      # type: ignore

def _zhl16_set_active_coeffs(variant: str | None, coeffs_json: str | None) -> None:
    """Set active ZH-L16 coefficients from GUI-provided JSON.

    This function intentionally does NOT provide internal defaults.
    """
    global _ZHL16_ACTIVE_VARIANT
    global _ZHL16_ACTIVE_A_N2_BAR, _ZHL16_ACTIVE_B_N2, _ZHL16_ACTIVE_A_HE_BAR, _ZHL16_ACTIVE_B_HE

    if coeffs_json is None or str(coeffs_json).strip() == "":
        raise RuntimeError("ZH-L16 coefficients missing: VPM_ZHL16_COEFFS_JSON is empty")

    try:
        data = json.loads(coeffs_json)
    except Exception as e:
        raise RuntimeError(f"ZH-L16 coefficients JSON parse error: {e}")

    if not isinstance(data, list) or len(data) != 16:
        raise RuntimeError("ZH-L16 coefficients JSON must be a list of 16 rows")

    aN2 = []
    bN2 = []
    aHe = []
    bHe = []

    for i, row in enumerate(data):
        if isinstance(row, dict):
            try:
                aN2.append(float(row.get("aN2")))
                bN2.append(float(row.get("bN2")))
                aHe.append(float(row.get("aHe")))
                bHe.append(float(row.get("bHe")))
            except Exception:
                raise RuntimeError("ZH-L16 coefficients JSON dict rows require aN2,bN2,aHe,bHe")
        elif isinstance(row, (list, tuple)):
            # accepted shapes:
            #   [aN2,bN2,aHe,bHe]
            #   [cmpt,aN2,bN2,aHe,bHe]
            try:
                if len(row) == 4:
                    aN2.append(float(row[0])); bN2.append(float(row[1])); aHe.append(float(row[2])); bHe.append(float(row[3]))
                elif len(row) == 5:
                    aN2.append(float(row[1])); bN2.append(float(row[2])); aHe.append(float(row[3])); bHe.append(float(row[4]))
                else:
                    raise RuntimeError("ZH-L16 coefficients JSON list rows must have length 4 or 5")
            except Exception as e:
                raise RuntimeError(f"ZH-L16 coefficients JSON list row error at {i+1}: {e}")
        else:
            raise RuntimeError("ZH-L16 coefficients JSON rows must be dict or list")

    _ZHL16_ACTIVE_VARIANT = (str(variant).strip().upper() if variant is not None else "")
    _ZHL16_ACTIVE_A_N2_BAR = aN2
    _ZHL16_ACTIVE_B_N2 = bN2
    _ZHL16_ACTIVE_A_HE_BAR = aHe
    _ZHL16_ACTIVE_B_HE = bHe
def _zhl16_apply_rapsol_to_he(a_he_bar, b_he, rapsol):
    """Apply rapsol correction to He coefficients in bar-domain.
    a' = a * rapsol
    b' = 1 / ((1/b - 1)*rapsol + 1)
    """
    try:
        r = float(rapsol)
    except Exception:
        r = 1.0
    if r <= 0.0:
        r = 1.0
    a_eff = a_he_bar * r
    # guard b in (0,1)
    b = float(b_he)
    if b <= 0.0:
        b = 1e-6
    if b >= 1.0:
        b = 0.999999
    b_eff = 1.0 / (((1.0/b) - 1.0)*r + 1.0)
    return float(a_eff), float(b_eff)

def _zhl16_gf_at_depth(depth_m, gf_low_anchor_m, gf_high_anchor_m, gf_low, gf_high, baro_msw):
    """Linear GF ramp on absolute ambient pressure between two anchors.

    - gf_low is applied at gf_low_anchor_m (typically 1st stop, SoDZ, or bottom).
    - gf_high is applied at gf_high_anchor_m (surface=0 m, or last stop depth).
    """
    gfL = float(gf_low)
    gfH = float(gf_high)

    if gf_low_anchor_m is None:
        gf_low_anchor_m = 0.0
    if gf_high_anchor_m is None:
        gf_high_anchor_m = 0.0

    p_low = float(gf_low_anchor_m) + float(baro_msw)
    p_hi = float(gf_high_anchor_m) + float(baro_msw)
    p_cur = float(depth_m) + float(baro_msw)

    denom = (p_low - p_hi)
    if abs(denom) < 1e-12:
        return gfH

    # t=0 at low-anchor, t=1 at high-anchor
    t = (p_low - p_cur) / denom

    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0

    gf = gfL + t * (gfH - gfL)
    return gf

def _zhl16_ceiling_msw(gf, rapsol, baro_msw):
    """Compute ZH-L16 ceiling depth (msw gauge) from current tissue pressures.
    Uses global Helium_Pressure/Nitrogen_Pressure (msw absolute).
    Returns the most restrictive ceiling (>=0).
    """
    global Helium_Pressure, Nitrogen_Pressure
    max_ceiling = 0.0
    gf = float(gf)
    baro = float(baro_msw)

    # Precompute msw 'a' coefficients (convert bar->msw)
    aN2_msw = [a * _MSW_PER_BAR for a in _ZHL16_ACTIVE_A_N2_BAR]
    # He: apply rapsol in bar then convert to msw
    aHe_msw = []
    bHe_eff = []
    for a_bar, b in zip(_ZHL16_ACTIVE_A_HE_BAR, _ZHL16_ACTIVE_B_HE):
        a_eff_bar, b_eff = _zhl16_apply_rapsol_to_he(a_bar, b, rapsol)
        aHe_msw.append(a_eff_bar * _MSW_PER_BAR)
        bHe_eff.append(b_eff)

    for i in range(16):
        pn2 = float(Nitrogen_Pressure[i])
        phe = float(Helium_Pressure[i])
        ptot = pn2 + phe
        if ptot <= 1e-12:
            continue

        wN2 = pn2 / ptot
        wHe = phe / ptot

        a = wN2 * aN2_msw[i] + wHe * aHe_msw[i]
        b = wN2 * float(_ZHL16_ACTIVE_B_N2[i]) + wHe * float(bHe_eff[i])
        if b <= 1e-9:
            b = 1e-9

        denom = (1.0 - gf) + (gf / b)
        if denom <= 1e-12:
            continue
        pamb_allowed = (ptot - gf * a) / denom  # msw absolute
        ceiling = pamb_allowed - baro  # gauge depth
        if ceiling > max_ceiling:
            max_ceiling = ceiling

    if max_ceiling < 0.0:
        max_ceiling = 0.0
    return float(max_ceiling)

def _zhl16_ceiling_msw_and_pilot(gf, rapsol, baro_msw):
    """Like _zhl16_ceiling_msw(), but also returns the pilot compartment (1..16)
    that sets the ceiling, using the same ptot-weighted a/b and current gf.
    """
    global Helium_Pressure, Nitrogen_Pressure
    max_ceiling = 0.0
    pilot = 1
    gf = float(gf)
    baro = float(baro_msw)

    aN2_msw = [a * _MSW_PER_BAR for a in _ZHL16_ACTIVE_A_N2_BAR]
    aHe_msw = []
    bHe_eff = []
    for a_bar, b in zip(_ZHL16_ACTIVE_A_HE_BAR, _ZHL16_ACTIVE_B_HE):
        a_eff_bar, b_eff = _zhl16_apply_rapsol_to_he(a_bar, b, rapsol)
        aHe_msw.append(a_eff_bar * _MSW_PER_BAR)
        bHe_eff.append(b_eff)

    for i in range(16):
        pn2 = float(Nitrogen_Pressure[i])
        phe = float(Helium_Pressure[i])
        ptot = pn2 + phe
        if ptot <= 1e-12:
            continue

        wN2 = pn2 / ptot
        wHe = phe / ptot

        a = wN2 * aN2_msw[i] + wHe * aHe_msw[i]
        b = wN2 * float(_ZHL16_ACTIVE_B_N2[i]) + wHe * float(bHe_eff[i])
        if b <= 1e-9:
            b = 1e-9

        denom = (1.0 - gf) + (gf / b)
        if denom <= 1e-12:
            continue

        pamb_allowed = (ptot - gf * a) / denom  # msw absolute
        ceiling = pamb_allowed - baro  # gauge depth

        if ceiling > max_ceiling:
            max_ceiling = ceiling
            pilot = i + 1

    if max_ceiling < 0.0:
        max_ceiling = 0.0
    return float(max_ceiling), int(pilot)


def ZHL16_SINGLEPASS_DECO(
    depth_start_deco_zone: float,
    depth_bottom: float,
    rate: float,
    step_size: float,
    mix_start: int,
    number_of_changes: int,
    depth_change,
    mix_change,
    rate_change,
    step_size_change,
    gf_low: float,
    gf_high: float,
    last_stop_m: float,
    rapsol: float,
    log_seg,
    ascent_with_stopv,
):
    """Compute decompression schedule using ZH-L16 (single pass), operating in msw units.
    This function updates global tissues and Run_Time by calling existing GAS_LOADINGS_* routines.
    It appends segments via provided log_seg and ascent_with_stopv callables (from VPMDECO_ORG).
    Returns dict with 'stops' and 'first_stop_depth'.
    """
    global Run_Time, Barometric_Pressure, Mix_Number

    baro = float(Barometric_Pressure)  # surface absolute pressure in msw (typically 10)
    cur_depth = float(depth_start_deco_zone)
    Mix_Number = int(mix_start)


    # ------------------------------------------------------------
    # STOPV application at *current deco depth* (ZHL branch)
    # Replicates VPM semantics: STOPV is always applied at the end of each ASC leg,
    # then ZH-L16 decides whether additional deco at this depth is needed.
    # ------------------------------------------------------------
    def _stopv_minutes_for_depth_zhl(depth_m: float) -> float:
        try:
            d_int = int(round(float(depth_m)))
        except Exception:
            return 0.0
        if abs(float(depth_m) - d_int) > 0.25:
            return 0.0
        try:
            return float(STOPV_MINUTES_BY_DEPTH.get(d_int, 0.0) or 0.0)
        except Exception:
            return 0.0

    def _apply_stopv_at_depth_zhl(depth_m: float, mix_num: int) -> float:
        mins = _stopv_minutes_for_depth_zhl(depth_m)
        if mins <= 0.0:
            return 0.0
        rt0 = float(Run_Time)
        GAS_LOADINGS_CONSTANT_DEPTH(float(depth_m), float(Run_Time) + float(mins), int(mix_num))
        rt1 = float(Run_Time)
        # Log STOPV segment (same label as VPM)
        try:
            log_seg('STOPV', depth_m=float(depth_m), mix=int(mix_num),
                    step_min=float(rt1 - rt0), runtime_end=float(rt1), note='voluntary stop')
        except Exception:
            pass
        return float(mins)

    # Determine first stop from ceiling at start-of-deco-zone using GF at that depth (initially gf_low)
    # Compute a first ceiling with gf_low and round to step_size
    ceiling0 = _zhl16_ceiling_msw(gf=float(gf_low), rapsol=rapsol, baro_msw=baro)
    if ceiling0 <= 0.0:
        # No deco: direct ascent to surface (includes STOPV if enabled)
        ascent_with_stopv(cur_depth, 0.0, float(rate), int(Mix_Number), include_to_depth=True)
        return {"first_stop_depth": 0.0, "stops": []}

    # Round first stop
    # Round first stop (MUST round UP to never start shallower than ceiling)
    first_stop = math.ceil(float(ceiling0) / float(step_size)) * float(step_size)
    if first_stop < float(step_size):
        first_stop = float(step_size)

    # Safety: if (due to numeric/coeff corner cases) the ceiling with gf_low is still deeper,
    # push first_stop deeper by step_size until it is safe.
    try:
        _ceil_chk = _zhl16_ceiling_msw(gf=float(gf_low), rapsol=float(rapsol), baro_msw=float(baro))
        _guard = 0
        while float(_ceil_chk) > float(first_stop) + 1e-6 and _guard < 200:
            first_stop = float(first_stop) + float(step_size)
            _ceil_chk = _zhl16_ceiling_msw(gf=float(gf_low), rapsol=float(rapsol), baro_msw=float(baro))
            _guard += 1
    except Exception:
        pass
# Align to 1 decimal like rest of engine
    first_stop = float(first_stop)

    # --- GF ramp anchor (freeze once) ---
    # By default, GF low is applied at the first discrete deco stop ("1STOP").
    # Optionally, anchor GF low at start-of-deco-zone depth (SoDZ) or bottom depth via env var.
    _anchor_mode = str(os.environ.get("VPM_ZHL_GF_RAMP_ANCHOR", "1STOP") or "1STOP").strip().upper()
    if _anchor_mode not in ("1STOP", "SODZ", "BOTTOM"):
        _anchor_mode = "1STOP"
    if _anchor_mode == "SODZ":
        anchor_gf_low_m = float(depth_start_deco_zone)
    elif _anchor_mode == "BOTTOM":
        anchor_gf_low_m = float(depth_bottom)
    else:
        anchor_gf_low_m = float(first_stop)

    # GF high ramp anchor (always evaluated, independent from low-anchor mode)
    _hi_anchor_mode = os.environ.get("VPM_ZHL_GF_RAMP_HI_ANCHOR", "SURFACE").strip().upper()
    if _hi_anchor_mode == "LASTSTOP":
        anchor_gf_high_m = float(last_stop_m)
    else:
        anchor_gf_high_m = 0.0

    stops = []
    deco_depth = first_stop
    last_rt = float(Run_Time)

    # Main stop ladder: from first_stop down to 0
    while deco_depth > 0.0 + 1e-9:
        # Ascent from current depth to deco_depth (do not include_to_depth; we want stop segment separate)
        ascent_with_stopv(cur_depth, deco_depth, float(rate), int(Mix_Number), include_to_depth=False)

        # Pick mix at this depth based on change tables (same logic as VPM loop)
                # Apply ascent/deco parameter changes (mix/rate/step) based on change tables
        # EXACTLY aligned with VPM loop semantics: changes take effect at the CURRENT stop depth.
        if int(number_of_changes) > 1:
            for j in range(1, int(number_of_changes)):
                try:
                    if deco_depth <= float(depth_change[j]) + 1e-9:
                        if not BO_MODE:
                            Mix_Number = int(mix_change[j])
                        rate = float(rate_change[j])
                        step_size = float(step_size_change[j])
                except Exception:
                    pass


        # [BO] Apply dynamic bailout gas switching at each stop depth (VPM semantics)
        if BO_MODE:
            try:
                Mix_Number = _bo_apply_dynamic(int(Mix_Number), float(deco_depth))
                _update_bo_effective(int(Mix_Number))
            except Exception:
                pass

        # Apply STOPV at this deco depth *after* mix/rate/step changes (VPM semantics)
        _stopv_applied_min = _apply_stopv_at_depth_zhl(float(deco_depth), int(Mix_Number))

        # Now hold at deco_depth until ceiling clears next stop
        next_stop = deco_depth - float(step_size)
        if next_stop < 0.0:
            next_stop = 0.0


        # If STOPV already cleared this step, mimic VPM behaviour:
        # log an explicit STOP with time=0 and note 'deco stop satisfied by STOPV'
        if (_stopv_applied_min > 0.0):
            gf_chk = _zhl16_gf_at_depth(float(deco_depth), float(anchor_gf_low_m), float(anchor_gf_high_m), float(gf_low), float(gf_high), float(baro))
            ceil_chk = _zhl16_ceiling_msw(gf=float(gf_chk), rapsol=float(rapsol), baro_msw=float(baro))
            if float(ceil_chk) <= float(next_stop) + 1e-6:
                stop_end_rt = float(Run_Time)
                _gf_actual_pct = None
                try:
                    _gf_actual_pct = float(gf_chk) * 100.0
                except Exception:
                    _gf_actual_pct = None
                try:
                    log_seg('STOP', depth_m=float(deco_depth), mix=int(Mix_Number),
                            step_min=0.0, runtime_end=float(stop_end_rt),
                            note='deco stop satisfied by STOPV', gf_actual=_gf_actual_pct)
                except Exception:
                    pass
                stops.append((float(deco_depth), 0.0, float(stop_end_rt)))
                cur_depth = float(deco_depth)
                deco_depth = float(next_stop)
                last_rt = float(stop_end_rt)
                continue

        # Stop ticks until cleared (internal tick fixed to 0.1 min; output rounding uses Minimum_Deco_Stop_Time)
        stop_start_rt = float(Run_Time)

        # Internal integration tick (minutes) — fixed (do not bind to MDST)
        tick_min = 0.1

        # Output stop quantization (minutes) — uses MDST, clamped >=0.1
        try:
            mdst = float(globals().get('Minimum_Deco_Stop_Time', 1.0) or 1.0)
        except Exception:
            mdst = 1.0
        if mdst < 0.1:
            mdst = 0.1

        # safety guard: allow long schedules; override via env if desired
        try:
            max_ticks = int(os.environ.get('ZHL_FREEZE_GUARD_MAXTICKS', '20000'))
        except Exception:
            max_ticks = 20000
        if max_ticks < 1000:
            max_ticks = 1000

        ticks = 0
        while True:
            # GF ramp at current stop depth
            gf = _zhl16_gf_at_depth(deco_depth, anchor_gf_low_m, anchor_gf_high_m, gf_low, gf_high, baro)
            ceiling = _zhl16_ceiling_msw(gf=gf, rapsol=rapsol, baro_msw=baro)
            if ceiling <= next_stop + 1e-6:
                break
            # add one minute at constant depth
            GAS_LOADINGS_CONSTANT_DEPTH(float(deco_depth), float(Run_Time) + float(tick_min), int(Mix_Number))
            ticks += 1
            if ticks >= max_ticks:
                raise RuntimeError("ZHL16_SINGLEPASS: stop loop did not converge (freeze guard)")

        # --- Quantize END-OF-STOP RUNTIME to MDST (Minimum_Deco_Stop_Time) ---
        # IMPORTANT:
        #   - internal integration tick stays fixed at 0.1 min (tick_min)
        #   - rounding/quantization is applied at the *runtime* level at the end of each stop
        #     (not on the stop duration alone), to keep the table readable and consistent.
        #   - any rounded-up surplus time is *physically simulated* at constant depth.
        try:
            q = float(mdst) if float(mdst) > 0.0 else 0.1
            if q < 0.1:
                q = 0.1
            # Round up the absolute runtime at the end of this stop
            # (ceil with tiny epsilon to avoid bumping exact multiples).
            target_rt = math.ceil((float(Run_Time) - 1e-12) / q) * q
            add_min = float(target_rt) - float(Run_Time)
            if add_min > 1e-9:
                GAS_LOADINGS_CONSTANT_DEPTH(float(deco_depth), float(Run_Time) + float(add_min), int(Mix_Number))
        except Exception:
            pass

        stop_end_rt = float(Run_Time)
        stop_time = stop_end_rt - stop_start_rt
        if stop_time > 0.0:
            _gf_actual_pct = None
            try:
                _gf_actual_pct = float(_zhl16_gf_at_depth(float(deco_depth), float(anchor_gf_low_m), float(anchor_gf_high_m), float(gf_low), float(gf_high), float(baro))) * 100.0
            except Exception:
                _gf_actual_pct = None
            log_seg('STOP', depth_m=float(deco_depth), mix=int(Mix_Number),
                    step_min=float(stop_time), runtime_end=float(stop_end_rt), note='ZH-L16 stop', gf_actual=_gf_actual_pct)
        stops.append((float(deco_depth), float(stop_time), float(stop_end_rt)))

        cur_depth = float(deco_depth)
        deco_depth = float(next_stop)
        last_rt = float(stop_end_rt)

    # Final ascent to surface (includes STOPV if enabled)
    ascent_with_stopv(cur_depth, 0.0, float(rate), int(Mix_Number), include_to_depth=True)
    return {"first_stop_depth": float(first_stop), "stops": stops}