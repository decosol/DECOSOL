import sys
import os
import re
import json
import csv
import datetime
import base64
import importlib.util
import io
import tkinter as tk
from tkinter import ttk, messagebox, filedialog


# -----------------------------------------------------------------------------
# COMPAT: builder embedded .set / .in (fallback se il MAIN non espone helper)
# -----------------------------------------------------------------------------
def _compat_build_set_text_from_args(args) -> str:
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

def _patch_in_text_last_stop(in_text: str, last_stop_m: float) -> str:
    """Patch the generated .in text to support last stop at 4 or 5 m
    without changing the engine MAIN.

    Strategy:
      - keep deep stop step-size unchanged (typically 3 m)
      - enforce step change at 6 m: step = 6 - last_stop (1 m for 5 m, 2 m for 4 m)
      - enforce step change at last_stop: step = last_stop (go straight to surface)
      - remove any ascent-parameter changes shallower than last_stop (e.g., 3 m)
    """
    try:
        ls = float(last_stop_m)
    except Exception:
        return in_text
    if ls not in (4.0, 5.0):
        return in_text

    lines = in_text.splitlines(True)  # keep line endings
    # Find decompress block marker (line starting with '99')
    idx99 = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("99"):
            idx99 = i
            break
    if idx99 is None or idx99 + 2 >= len(lines):
        return in_text

    # The next line should contain number of ascent parameter changes
    nline_idx = idx99 + 1
    try:
        n_changes = int(lines[nline_idx].split("!")[0].strip().split()[0])
    except Exception:
        return in_text

    first_change_idx = idx99 + 2
    change_lines = lines[first_change_idx:first_change_idx + n_changes]
    rest_lines = lines[first_change_idx + n_changes:]

    entries = []
    for ln in change_lines:
        # expected: depth,mix,asc_rate,step !comment
        main_part, *comment_part = ln.split("!")
        comment = "!" + comment_part[0] if comment_part else ""
        parts = [p.strip() for p in main_part.strip().split(",") if p.strip()]
        if len(parts) < 4:
            continue
        try:
            d = float(parts[0])
            mix = int(float(parts[1]))
            rate = float(parts[2])
            step = float(parts[3])
        except Exception:
            continue
        entries.append([d, mix, rate, step, comment])

    if not entries:
        return in_text

    # Helper: choose mix/rate from closest deeper entry (depth >= target, minimal depth)
    def template_for(target_depth: float):
        candidates = [e for e in entries if e[0] >= target_depth - 1e-6]
        if candidates:
            e = min(candidates, key=lambda x: x[0])
            return int(e[1]), float(e[2])
        # fallback to deepest
        e = max(entries, key=lambda x: x[0])
        return int(e[1]), float(e[2])

    # Remove entries shallower than last stop (e.g., 3 m)
    entries = [e for e in entries if e[0] >= ls - 1e-6]

    # Upsert entry at 6 m
    mix6, rate6 = template_for(6.0)
    step6 = max(0.5, 6.0 - ls)
    # remove existing 6
    entries = [e for e in entries if abs(e[0] - 6.0) > 1e-6]
    entries.append([6.0, mix6, rate6, step6, " !Step_Size change (GUI last stop)\n"])

    # Upsert entry at last stop depth
    mixls, ratels = template_for(ls)
    entries = [e for e in entries if abs(e[0] - ls) > 1e-6]
    entries.append([ls, mixls, ratels, ls, " !Last stop (GUI)\n"])

    # Sort descending by depth
    entries.sort(key=lambda x: x[0], reverse=True)

    # Rebuild change lines
    new_change_lines = []
    for d, mix, rate, step, comment in entries:
        # keep 6 decimals like other builders
        s = f"{d:.6f},{mix:d},{rate:.6f},{step:.6f}"
        if comment:
            # comment already includes leading '!' and often newline
            s += comment
        if not s.endswith("\n"):
            s += "\n"
        new_change_lines.append(s)

    # Update n_changes line (preserve original comment if any)
    n_comment = ""
    if "!" in lines[nline_idx]:
        n_comment = "!" + lines[nline_idx].split("!", 1)[1]
        if not n_comment.endswith("\n"):
            n_comment += "\n"
    lines[nline_idx] = f"{len(new_change_lines)} {n_comment}" if n_comment else f"{len(new_change_lines)}\n"

    # Reassemble
    lines = lines[:first_change_idx] + new_change_lines + rest_lines
    return "".join(lines)



def _compat_build_in_text_from_args(args) -> str:
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
    # Supporto ultima sosta 4/5 m senza toccare il MAIN:
    # manteniamo base_step (tipicamente 3 m) per tutte le soste profonde,
    # poi forziamo due cambi step in risalita: a 6 m (delta) e a last_stop (verso superficie).
    if last_stop_m in (4.0, 5.0):
        candidate_depths.add(6.0)
        candidate_depths.add(float(last_stop_m))
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

        # Ultima sosta "non multipla" (4/5 m) mantenendo lo step profondo a 3 m:
        # - a 6 m: step = (6 - last_stop) -> 1 m per 5 m, 2 m per 4 m
        # - a last_stop: step = last_stop -> si va direttamente a 0 m
        if last_stop_m in (4.0, 5.0):
            if abs(depth_change - 6.0) < 1e-6:
                step_here = max(0.5, 6.0 - float(last_stop_m))
            elif abs(depth_change - float(last_stop_m)) < 1e-6:
                step_here = float(last_stop_m)
        else:
            # Caso standard: se l'ultima sosta è 6 m (step base=3), imponiamo step=6 a 6 m
            if last_stop_m > base_step + 1e-9 and abs(depth_change - last_stop_m) < 1e-6:
                step_here = last_stop_m
        lines.append(
            f"{depth_change:.6f},{mix_idx:d},{asc_rate_fortran:.6f},{step_here:.6f} !Starting depth, gasmix, rate, step size\n"
        )
    lines.append("0 ! Repetitive code 0 = last dive/end of file\n")
    return "".join(lines)


import tkinter.font as tkfont



# -----------------------------------------------------------------------------
# HELP (embedded from Help.docx) — rich text runs (bold/italic/underline)
# No runtime dependencies.
# Each item: {'k': 'h'|'p'|'blank', 'r': [[text, bold, italic, underline], ...]}
# -----------------------------------------------------------------------------
HELP_DOC_EMBEDDED = [{'k': 'h', 'r': [['Input immersione', True, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Tempo di fondo [min]', True, False, False]]}, {'k': 'p', 'r': [['non comprende il tempo di discesa e le eventuali soste in discesa, è il tempo netto al fondo, non il runtime alla fine del tempo di fondo.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Solubility N2/He [num]', True, False, False]]}, {'k': 'p', 'r': [['è il rapporto tra la solubilità dell’azoto e quella dell’elio, che viene impiegato per la correzione della helium penalty, la ben nota penalizzazione decompressiva in capo all’elio. In DECOSOL v.1.0beta Solubility N2/He viene impiegata simmetricamente nei due algoritmi implementati: Valori consigliati per le simulazioni sono attorno a 1.75 considerando che tale è il rapporto tra la solubilità del dell’azoto e quella dell’elio nel sangue. Per valori di Solubility N2/He =1.00 VPMB e ZH-L16 sono identici a come sono stati costruiti, quindi è nullo l’effetto delle diverse solubilità. L’utente che non voglia applicare la correzione della helium penalty in base al rapporto di solubilità N2/He lascerà quindi Solubility N2/He [num] = 1.00.', False, False, False]]}, {'k': 'p', 'r': [['Il principio della correzione degli algoritmi VPMB e ZH-L16 è che i tessuti sopportano un surplus di volume di gas in soluzione in stato di sovrasaturazione indipendente dal gas inerte che li occupa per cui nel caso dell’uso di elio in sostituzione dell’azoto abbiamo:', False, False, False]]}, {'k': 'p', 'r': [['Surplus_volume = gradiente_accettabile_N2 * solubilità_N2 = gradiente_accettabile_He * solubilità_He;', False, False, False]]}, {'k': 'p', 'r': [['dove: solubilità_N2 / solubilità_He = Solubility N2 / He;', False, False, False]]}, {'k': 'p', 'r': [['quindi: gradiente_accettabile_He = gradiente_accettabile_N2*Solubility N2/He.', False, False, False]]}, {'k': 'p', 'r': [['Questa è la relazione base per la correzione degli algoritmi: se ad esempio sapessimo che l’azoto è il doppio solubile dell’elio possiamo permettere all’elio un gradiente pressorio doppio rispetto a quello dell’azoto. In tal caso avremmo infatti la stessa quantità di elio disciolto e di azoto disciolto, ovvero la stessa quantità di gas inerte in stato di sovrasaturazione che costituisce il fattore di rischio per la crescita delle bolle e il verificarsi di malattia da decompressione.', False, False, False]]}, {'k': 'h', 'r': [['Solubility N2/He in ZH-L16', True, False, False]]}, {'k': 'p', 'r': [['Solubility N2/He in ZH-L16 modifica il gradiente di sovrapressione permesso all’elio con un calcolo identico a quello impiegato per i GF ma nel senso dell’aumento di tale gradiente.', False, False, False]]}, {'k': 'p', 'r': [['Tale parametro è applicato solo all’elio con la stessa matematica dei GF e l’effetto della solubility si cumula a quello dei GF, quindi se in ZH-L16 si applica un GF high = 0.85 e una Solubility N2/He = 2.05 otterrò in superficie un aumento del gradiente per l’elio pari a 0.85x2.05=1.74. Se in ZH-L16 si impostano valori di Solubility N2/He > 1.00 è essenziale impostare significativi valori di GF low dell’ordine dello 0.10-0.30 in modo da ottenere sufficiente gradualità delle fasi inziali della risalita contrastando così la nota tendenza di tale algoritmo ad accelerare le prime fasi della decompressione.', False, False, False]]}, {'k': 'h', 'r': [['Solubility N2/He in VPMB', True, False, False]]}, {'k': 'p', 'r': [['Solubility N2/He in VPMB aumenta il gradiente iniziale dell’elio (initial_allowable_gradient_He che a cascata dà vita al new_allowable_gradient_He) proporzionalmente al valore selezionato (da 1.00 a 3.00). In VPMB non essendoci i GF il fattore Solubility N2/He viene applicato tal quale con la sola necessità di porre Raggio critico He [µm] = 0.55 quando si setta Solubility N2/He > 1.10. Il modello originale di VPMB prevede un raggio critico per l’azoto = 0.55 e uno per l’elio = 0.45, mentre è fisicamente evidente che azoto ed elio non possono che essere indistinguibilmente mescolati nelle microbolle. La microbolla è un mix caotico di tutti i gas presenti (N2, He, O2, CO2 e vapore acqueo) e le molecole si mescolano casualmente senza alcuna possibile separazione tra bolle di elio e bolle di azoto: la natura non etichetta le bolle come "bolla di azoto" o "bolla di elio". La distinzione tra i raggi di N2 ed He da parte dei modellisti di VPM è presumibilmente dovuta alla necessità di trattare “più favorevolmente” l’elio rispetto all’azoto attribuendogli un raggio critico di 0.45 µm rispetto a quello dell’azoto di 0.55 µm. Se quindi introduciamo la variabile Solubility N2/He dobbiamo mettere entrambi i raggi allo stesso valore. Lo stesso valore potrebbe anche essere 0.45 o 0.50 per entrambi, ma prudenzialmente optiamo per 0.55 per entrambi. In tal modo il modello VPMB viene ad avere un unico raggio critico per N2 ed He, il che è biologicamente più consono, ed un parametro fisico – Solubility N2/He – per regolare il diverso comportamento dei gas nei tessuti.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Algoritmo di calcolo', True, False, False]]}, {'k': 'p', 'r': [['DECOSOL v.1.0 beta supporta il calcolo decompressivo basato su VPMB e ZH-L16 B/C.', False, False, False]]}, {'k': 'p', 'r': [['Quando VPMB è selezionato permette l’input dei raggi critici di azoto ed elio e permette l’accesso al menù Parametri VPM che riporta i parametri nativi con i valori di default suggeriti dagli autori.', False, False, False]]}, {'k': 'p', 'r': [['Quando ZH-L16 è selezionato permette l’input dei GF e l’accesso al menù Parametri ZH-L16 B/C in cui è possibile: selezionare il set dei parametri a e b per azoto ed elio per le versioni B (meno conservativo) e C (più conservativo) di ZH-L16. La versione selezionata è riportata come suffisso nel nome del pulsante per ricordare all’utente con quale set si sta lavorando. Quando si selezionano i set B o C vengono mostrati i valori dei parametri che risultano modificabili e ripristinabili da utente. Questa è un’area altamente specialistica per la quale è richiesta preparazione estremamente dettagliata. Infine è possibile selezionare i punti di ancoraggio per GF low e GF hi. Anche questa è una sezione sperimentale specialistica.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Modalità di respirazione', True, False, False]]}, {'k': 'p', 'r': [['DECOSOL v.1.0 beta supporta il calcolo decompressivo sia per circuito aperto che per circuito chiuso + piano bailout da selezionare in alternativa.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'p', 'r': [['Selezionando OC si apre il relativo pannello per la selezione del Gas di fondo e gas deco in numero di 4 gas decompressivi. Per ciascun gas oltre alle frazioni dei gas è inputabile la MOD in base alla quale sono calcolate le pressioni parziali di O2 N2 He. I gas impiegati nei calcoli decompressivi sono esclusivamente quelli in stato ON (checkbox prima colonna tabella gas) per i quali viene anche calcolata la differenza di ppN2 alla quota di cambio gas come indicatore del rischio di controdiffusione isobarica. Le ultime cinque colonne della tabella sono dedicate al bilancio dei consumi dei gas per permettere una adeguata simulazione di programmazione inserendo consumi superficiali in l/min (VRM), volumi delle bombole in litri e pressioni iniziali delle bombole in bar. A fine calcolo il programma restituisce i litri consumati per ciascun gas impiegato e il bilancio delle pressioni (in rosso se negativo).', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'p', 'r': [['Selezionando CC si apre il relativo pannello per la selezione dei setpoint, del diluente e dei gas di bailout.', False, False, False]]}, {'k': 'p', 'r': [['Setpoint: DECOSOL v.1.0 beta supporta calcoli in CC sia con SP singolo che con SP fino a cinque bande. DECOSOL v.1.0 beta riporta automaticamente i valori di SP impostati a quelli fisicamente ottenibili in base alla combinazione di ossigeno, diluente selezionato e pressione ambiente.', False, False, False]]}, {'k': 'p', 'r': [['Nel box Diluente oltre alle frazioni dei gas è previsto l’input della MOD al fine di calcolare a scopo informativo le pressioni parziali del diluente e la sua densità alla quota massima di impiego per diluente puro.', False, False, False]]}, {'k': 'p', 'r': [['Nel box Bailout è possibile impostare cinque gas di BO con identici criteri, parametri e rappresentazioni del gas di OC (vedi sopra). Il calcolo del piano BO è attivato dal relativo checkbox ed impiega solamente i gas selezionati col presupposto che il BO inizia potenzialmente sempre dalla fine del tempo di fondo (mai prima) ma ciò si verifica effettivamente solo se c’è un gas con MOD maggiore o uguale alla profondità di fondo. Diversamente il BO inizia alla MOD del primo gas disponibile. Il piano di BO dura fino alla superficie e non sono previsti ritorni in CC.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Parametri deco', True, False, False]]}, {'k': 'p', 'r': [['Nel menù Parametri deco è selezionabile la quota dell’ultima sosta decompressiva. Inoltre sono impostabili le velocità di discesa differenziate fino a quattro bande con l’opzione di assegnare a ciascuna banda una sosta in discesa come potrebbe essere un bubble check e/o una navigazione a quota costante. Analogamente la velocità di risalita è differenziabile in un sistema fino a cinque bande impostate dall’utente con le relative velocità. Infine è selezionabile il tempo minimo delle soste deco che funge anche come criterio di arrotondamento del runtime a fine sosta.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Deco volontaria (ON/OFF)', True, False, False]]}, {'k': 'p', 'r': [['Nel menu sotto il pulsante Deco volontaria è possibile settare soste decompressive volontarie da 66 m a 6 m che rimangono stabili in memoria. Nel menù le soste sono anche sommate in 4 fasce corrispondenti alle fasce di utilizzo dei gas decompressivi più comuni e infine sommate in toto ai fini del controllo del data entry. I tempi di sosta volontaria stabiliti non contengono i tempi di risalita che sono calcolati separatamente in base alle velocità di risalita del menù Parametri deco. Affinché le soste volontarie rientrino nel calcolo decompressivo deve essere selezionato il checkbox “Abilita” a fianco del pulsante. In tal caso la scritta del pulsante diventa Deco volontaria (ON) in rosso per ricordare all’utente che le soste decompressive volontarie sono attive all’interno del calcolo decompressivo. Diversamente se il checkbox “Abilita” a fianco del pulsante non è selezionato la scritta del pulsante rimane Deco volontaria (OFF) in nero e le soste decompressive volontarie non sono attive all’interno del calcolo decompressivo. Con Deco volontaria (ON) le soste selezionate nel menù con profondità inferiore alla profondità di fondo vengono incluse nel calcolo decompressivo e vengono effettuate prima della sosta decompressiva calcolata dal programma per quella quota. Nel profilo dettagliato le soste di Deco volontaria sono nominate STOPV con sfondo verde. A valle dello STOPV DECOSOL v.1.0 beta calcola l’eventuale residuo di debito decompressivo e in caso lo espone come sosta deco nominata STOP ed il relativo tempo di sosta, altrimenti se il debito decompressivo è soddisfatto da STOPV DECOSOL v.1.0 beta espone una sosta deco (STOP) a tempo zero con la nota “deco stop satisfied by STOPV”. La funzione deco volontaria oltre a prestarsi per rappresentare possibili eventi non rappresentabili in un profilo calcolato automaticamente rappresenta una opportunità per chi usa sistemi decompressivi come deco mnemonica o ratio deco di paragonarne l’efficienza decompressiva rispetto agli algoritmi implementati sia riguardo alla quantità di tempo deco totale sia riguardo alla distribuzione delle soste. Se associata all’uso del parametro Solubility N2/He la Deco volontaria diventa un potente strumento di paragone.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Risultati', True, False, False]]}, {'k': 'p', 'r': [['Dopo aver attivato "Calcola deco" sul fondo della pagina principale appaiono i risultati:', False, False, False]]}, {'k': 'p', 'r': [['Runtime totale [min]:', True, False, False], [' tempo totale dell’immersione', False, False, False]]}, {'k': 'p', 'r': [['Runtime deco [min]:', True, False, False], [' tempo dalla fine del tempo di fondo alla fine dell’immersione', False, False, False]]}, {'k': 'p', 'r': [['Inizio zona deco [m]:', True, False, False], [' quota alla quale il primo tessuto entra in stato di sovrasaturazione. È la quota sotto la quale non è possibile che si verifichi decompressione', False, False, False]]}, {'k': 'p', 'r': [['Profondità media finale [m]:', True, False, False], [' profondità media ponderata sul profilo', False, False, False]]}, {'k': 'p', 'r': [['Gas density fondo [g/l]:', True, False, False], [' massima densità del gas respirato', False, False, False]]}, {'k': 'p', 'r': [['CNS% finale:', True, False, False], [' indice della tossicità neurologica-acuta dell’ossigeno (sindrome Paul Bert)', False, False, False]]}, {'k': 'p', 'r': [['OTU finale:', True, False, False], [' indice della tossicità polmonare-cronica dell’ossigeno (sindrome di Loarrain Smith)', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Profilo dettagliato', True, False, False]]}, {'k': 'p', 'r': [['Dopo il calcolo deco si genera un Profilo dettagliato nel relativo tab che contiene le seguenti colonne:', False, False, False]]}, {'k': 'p', 'r': [['n:', True, False, False], [' numero progressivo del segmento', False, False, False]]}, {'k': 'p', 'r': [['tipo:', True, False, False], [' tipo del segmento (DESC=discesa; CONST=profondità costante in discesa e fondo; ASC=tratto in risalita; STOPV=deco volontaria/sosta in risalita; STOP=sosta deco calcolata da DECOSOL v.1.0 beta)', False, False, False]]}, {'k': 'p', 'r': [['seg_time:', True, False, False], [' durata in min del segmento', False, False, False]]}, {'k': 'p', 'r': [['run_time:', True, False, False], [' durata in min di tutto il profilo fino a quel punto', False, False, False]]}, {'k': 'p', 'r': [['from:', True, False, False], [' profondità iniziale del segmento se il segmento è DESC o ASC', False, False, False]]}, {'k': 'p', 'r': [['to:', True, False, False], [' profondità finale del segmento se il segmento è DESC o ASC', False, False, False]]}, {'k': 'p', 'r': [['depth:', True, False, False], [' profondità costante del segmento se il segmento è CONST, STOPV o STOP', False, False, False]]}, {'k': 'p', 'r': [['note:', True, False, False], [' note esplicative a commento del segmento, es: rate che esprime la velocità di ascesa e risalita', False, False, False]]}, {'k': 'p', 'r': [['resp_algo:', True, False, False], [' modalità di respirazione selezionata (OC; CC; BO) e algoritmo di calcolo selezionato (VPM; ZHL16)', False, False, False]]}, {'k': 'p', 'r': [['gas:', True, False, False], [' gas respirato nel segmento: in CC DIL_xx/yy/xx e/o BO_n_xx/yy/zz; in OC BOTT_xx/yy/zz e/o DEC_n_xx/yy/zz', False, False, False]]}, {'k': 'p', 'r': [['ppO2:', True, False, False], [' ppO2 media del segmento (non a fine segmento)', False, False, False]]}, {'k': 'p', 'r': [['Depth_avg:', True, False, False], [' profondità media da inizio immersione alla fine del segmento', False, False, False]]}, {'k': 'p', 'r': [['Gas_dens_gL:', True, False, False], [' densità del gas respirato nel segmento', False, False, False]]}, {'k': 'p', 'r': [['CNS_%:', True, False, False], [' CNS% cumulato da inizio immersione alla fine del segmento', False, False, False]]}, {'k': 'p', 'r': [['OTU:', True, False, False], [' OTU cumulate da inizio immersione alla fine del segmento', False, False, False]]}, {'k': 'p', 'r': [['EAD_m:', True, False, False], [' profondità equivalente in aria media del segmento', False, False, False], [', indice di narcosi', False, False, False]]}, {'k': 'p', 'r': [['GF_actual:', True, False, False], [' GF calcolato per quel segmento (solo per ZH-L16)', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Esporta profilo (CSV)', True, False, False]]}, {'k': 'p', 'r': [['Questo pulsante salva un CSV del profilo dettagliato prodotto dopo il calcolo deco che rappresenta il profilo eseguito identico a quello contenuto nel tab Profilo dettagliato', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'h', 'r': [['Esporta grafico (PDF)', True, False, False]]}, {'k': 'p', 'r': [['Questo pulsante salva un PDF del grafico prodotto dopo il calcolo deco identico a quello contenuto nel tab Grafico che rappresenta il profilo eseguito, la profondità media progressiva, l’algoritmo di calcolo e i gas respirati con la distinzione tra OC e CC.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'p', 'r': [['DECOSOL v.1.0 beta calcola decompressioni di profili eseguiti in acqua di mare a livello del mare e non ripetitivi.', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'p', 'r': [['Ringraziamenti: \tRoberto Antolini, Alessio Pollice, Giovanni Marola, Corrado Bonuccelli,', False, False, False]]}, {'k': 'p', 'r': [['Ross Hemingway, Massimo Barnini, Eugenio Aliani, Serena Gorini', False, False, False]]}, {'k': 'blank', 'r': []}, {'k': 'p', 'r': [['DECOSOL v.1.0 beta © 2026 Luca Brambilla.', False, False, False]]}, {'k': 'p', 'r': [['Released under the MIT License. See LICENSE.txt for details.', False, False, False]]}]






def _render_rich_text_into_widget(widget: tk.Text, content) -> None:
    """Render embedded rich text (from Help.docx) into a Tk Text widget.

    content: list of dicts with keys:
      - k: 'h' (heading) | 'p' (paragraph) | 'blank'
      - r: list of runs [text, bold, italic, underline]
    """
    base_font = ("Segoe UI", 10)
    head_font = ("Segoe UI", 12, "bold")
    bold_font = ("Segoe UI", 10, "bold")
    italic_font = ("Segoe UI", 10, "italic")
    bold_italic_font = ("Segoe UI", 10, "bold italic")
    underline_font = ("Segoe UI", 10, "underline")
    bold_underline_font = ("Segoe UI", 10, "bold underline")
    italic_underline_font = ("Segoe UI", 10, "italic underline")
    bold_italic_underline_font = ("Segoe UI", 10, "bold italic underline")

    widget.configure(font=base_font)

    # Tags
    widget.tag_configure("H", font=head_font, spacing1=6, spacing3=6)
    widget.tag_configure("P", font=base_font, spacing1=2, spacing3=4)

    widget.tag_configure("B", font=bold_font)
    widget.tag_configure("I", font=italic_font)
    widget.tag_configure("BI", font=bold_italic_font)

    widget.tag_configure("U", font=underline_font)
    widget.tag_configure("BU", font=bold_underline_font)
    widget.tag_configure("IU", font=italic_underline_font)
    widget.tag_configure("BIU", font=bold_italic_underline_font)

    def _pick_tag(b: bool, i: bool, u: bool) -> str | None:
        if b and i and u:
            return "BIU"
        if b and i:
            return "BI"
        if b and u:
            return "BU"
        if i and u:
            return "IU"
        if b:
            return "B"
        if i:
            return "I"
        if u:
            return "U"
        return None

    widget.delete("1.0", "end")

    for para in content or []:
        kind = (para or {}).get("k", "p")
        runs = (para or {}).get("r", []) or []

        if kind == "blank":
            widget.insert("end", "\n")
            continue

        base_tag = "H" if kind == "h" else "P"

        # Insert paragraph runs
        if not runs:
            widget.insert("end", "\n", (base_tag,))
            continue

        for t, b, i, u in runs:
            if not t:
                continue
            style_tag = _pick_tag(bool(b), bool(i), bool(u))
            if style_tag:
                widget.insert("end", t, (base_tag, style_tag))
            else:
                widget.insert("end", t, (base_tag,))

        widget.insert("end", "\n")

# --- Plot support (profilo) ---
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from contextlib import redirect_stdout
from types import SimpleNamespace

# Percorso assoluto allo script VPMB (stesso folder di questo GUI)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Motore decompressivo (MAIN) — path robusto (evita nomi hard-coded fragili)
DEFAULT_MAIN = "MAIN_DECOSOL_1_0.py"
ENGINE_MODULE_NAME = "MAIN_DECOSOL_1_0"
VPMB_SCRIPT = str(os.environ.get("VPM_ENGINE_PATH", "") or "").strip()
if not VPMB_SCRIPT:
    VPMB_SCRIPT = os.path.join(SCRIPT_DIR, DEFAULT_MAIN)
if not os.path.isfile(VPMB_SCRIPT):
    try:
        import glob
        _cands = glob.glob(os.path.join(SCRIPT_DIR, "vpmdeco_MAIN_VPM_finale_ZHL16_SINGLEPASS_v16*.py"))
        if _cands:
            _cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            VPMB_SCRIPT = _cands[0]
    except Exception:
        pass

# STOPV: quote fisse per soste volontarie in risalita
STOPV_DEPTHS_M = [66, 63, 60, 57, 54, 51, 48, 45, 42, 39, 36, 33, 30, 27, 24, 21, 18, 15, 12, 9, 6]

# -----------------------------------------------------------------------------
# Persistenza: DEFAULT + LAST INPUTS in %APPDATA%\DECOSOL (Windows)
# - default_inputs.json: baseline di fabbrica (copiato da DEFAULT_INPUTS_EMBEDDED)
# - last_inputs.json: ultimo stato utente (stabile fra le sessioni)
# NOTE: mai scrivere accanto all'exe (permessi + AV). Sempre in APPDATA.
# -----------------------------------------------------------------------------
APP_NAME = "DECOSOL"
APPDATA_ROOT = os.environ.get("APPDATA") or SCRIPT_DIR
APPDATA_DIR = os.path.join(APPDATA_ROOT, APP_NAME)
try:
    os.makedirs(APPDATA_DIR, exist_ok=True)
except Exception:
    # fallback estremo: directory dello script
    APPDATA_DIR = SCRIPT_DIR

DEFAULT_INPUTS_FILE = os.path.join(APPDATA_DIR, "default_inputs.json")
LAST_INPUTS_FILE = os.path.join(APPDATA_DIR, "last_inputs.json")

# Default embedded (derivato dal file last_inputs.json fornito come baseline)
DEFAULT_INPUTS_EMBEDDED_JSON = r'''{
  "mode": "CC",
  "depth": "100",
  "bottom": "20",
  "rapsol": "1.00",
  "crit_rad_n2": "0.55",
  "crit_rad_he": "0.45",
  "deco_model": "VPM",
  "gf_low": "20",
  "gf_high": "85",
  "zhl_gf_ramp_anchor": "SODZ",
  "zhl_gf_ramp_hi_anchor": "LASTSTOP",
  "zhl16_variant": "C",
  "zhl16_coeffs_C": [
    [
      1,
      1.1696,
      0.5578,
      1.6189,
      0.477
    ],
    [
      2,
      1.0,
      0.6514,
      1.383,
      0.5747
    ],
    [
      3,
      0.8618,
      0.7222,
      1.1919,
      0.6527
    ],
    [
      4,
      0.7562,
      0.7825,
      1.0458,
      0.7223
    ],
    [
      5,
      0.62,
      0.8126,
      0.922,
      0.7582
    ],
    [
      6,
      0.5043,
      0.8434,
      0.8205,
      0.7957
    ],
    [
      7,
      0.441,
      0.8693,
      0.7305,
      0.8279
    ],
    [
      8,
      0.4,
      0.891,
      0.6502,
      0.8553
    ],
    [
      9,
      0.375,
      0.9092,
      0.595,
      0.8757
    ],
    [
      10,
      0.35,
      0.9222,
      0.5545,
      0.8903
    ],
    [
      11,
      0.3295,
      0.9319,
      0.5333,
      0.8997
    ],
    [
      12,
      0.3065,
      0.9403,
      0.5189,
      0.9073
    ],
    [
      13,
      0.2835,
      0.9477,
      0.5181,
      0.9122
    ],
    [
      14,
      0.261,
      0.9544,
      0.5176,
      0.9171
    ],
    [
      15,
      0.248,
      0.9602,
      0.5172,
      0.9217
    ],
    [
      16,
      0.2327,
      0.9653,
      0.5119,
      0.9267
    ]
  ],
  "zhl16_coeffs_B": [
    [
      1,
      1.1696,
      0.5578,
      1.6189,
      0.477
    ],
    [
      2,
      1.0,
      0.6514,
      1.383,
      0.5747
    ],
    [
      3,
      0.8618,
      0.7222,
      1.1919,
      0.6527
    ],
    [
      4,
      0.7562,
      0.7825,
      1.0458,
      0.7223
    ],
    [
      5,
      0.6667,
      0.8126,
      0.922,
      0.7582
    ],
    [
      6,
      0.56,
      0.8434,
      0.8205,
      0.7957
    ],
    [
      7,
      0.4947,
      0.8693,
      0.7305,
      0.8279
    ],
    [
      8,
      0.45,
      0.891,
      0.6502,
      0.8553
    ],
    [
      9,
      0.4187,
      0.9092,
      0.595,
      0.8757
    ],
    [
      10,
      0.3798,
      0.9222,
      0.5545,
      0.8903
    ],
    [
      11,
      0.3497,
      0.9319,
      0.5333,
      0.8997
    ],
    [
      12,
      0.3223,
      0.9403,
      0.5189,
      0.9073
    ],
    [
      13,
      0.285,
      0.9477,
      0.5181,
      0.9122
    ],
    [
      14,
      0.2737,
      0.9544,
      0.5176,
      0.9171
    ],
    [
      15,
      0.2523,
      0.9602,
      0.5172,
      0.9217
    ],
    [
      16,
      0.2327,
      0.9653,
      0.5119,
      0.9267
    ]
  ],
  "zhl16_coeffs": [
    [
      1,
      1.1696,
      0.5578,
      1.6189,
      0.477
    ],
    [
      2,
      1.0,
      0.6514,
      1.383,
      0.5747
    ],
    [
      3,
      0.8618,
      0.7222,
      1.1919,
      0.6527
    ],
    [
      4,
      0.7562,
      0.7825,
      1.0458,
      0.7223
    ],
    [
      5,
      0.62,
      0.8126,
      0.922,
      0.7582
    ],
    [
      6,
      0.5043,
      0.8434,
      0.8205,
      0.7957
    ],
    [
      7,
      0.441,
      0.8693,
      0.7305,
      0.8279
    ],
    [
      8,
      0.4,
      0.891,
      0.6502,
      0.8553
    ],
    [
      9,
      0.375,
      0.9092,
      0.595,
      0.8757
    ],
    [
      10,
      0.35,
      0.9222,
      0.5545,
      0.8903
    ],
    [
      11,
      0.3295,
      0.9319,
      0.5333,
      0.8997
    ],
    [
      12,
      0.3065,
      0.9403,
      0.5189,
      0.9073
    ],
    [
      13,
      0.2835,
      0.9477,
      0.5181,
      0.9122
    ],
    [
      14,
      0.261,
      0.9544,
      0.5176,
      0.9171
    ],
    [
      15,
      0.248,
      0.9602,
      0.5172,
      0.9217
    ],
    [
      16,
      0.2327,
      0.9653,
      0.5119,
      0.9267
    ]
  ],
  "use_bailout_ascent": false,
  "deco_last_stop_m": 6.0,
  "deco_desc_profile": "Standard (20 m/min)",
  "deco_asc_profile": "Standard (9 m/min)",
  "descent_stops": [
    {
      "depth": 6.0,
      "time": 2.0
    },
    {
      "depth": 20.0,
      "time": 2.0
    },
    {
      "depth": 60.0,
      "time": 2.0
    }
  ],
  "descent_speed_bands": [
    {
      "from_m": 0.0,
      "to_m": 6.0,
      "speed": 6.0,
      "stop_min": 2.0
    },
    {
      "from_m": 6.0,
      "to_m": 20.0,
      "speed": 10.0,
      "stop_min": 2.0
    },
    {
      "from_m": 20.0,
      "to_m": 60.0,
      "speed": 15.0,
      "stop_min": 2.0
    },
    {
      "from_m": 60.0,
      "to_m": 150.0,
      "speed": 20.0,
      "stop_min": 0.0
    }
  ],
  "ascent_speed_bands": [
    {
      "from_m": 150.0,
      "to_m": 90.0,
      "speed": 10.0
    },
    {
      "from_m": 90.0,
      "to_m": 65.0,
      "speed": 7.0
    },
    {
      "from_m": 65.0,
      "to_m": 36.0,
      "speed": 5.0
    },
    {
      "from_m": 36.0,
      "to_m": 6.0,
      "speed": 3.0
    },
    {
      "from_m": 6.0,
      "to_m": 0.0,
      "speed": 1.0
    }
  ],
  "gases": [
    {
      "name": "gas fondo",
      "enabled": true,
      "fo2": "0.10",
      "fhe": "0.80",
      "mod": "120",
      "vrm": "15",
      "tank": "40.0"
    },
    {
      "name": "gas deco1",
      "enabled": true,
      "fo2": "0.18",
      "fhe": "0.62",
      "mod": "65",
      "vrm": "15",
      "tank": "11.0"
    },
    {
      "name": "gas deco2",
      "enabled": true,
      "fo2": "0.35",
      "fhe": "0.30",
      "mod": "36",
      "vrm": "15",
      "tank": "11.0"
    },
    {
      "name": "gas deco3",
      "enabled": true,
      "fo2": "0.50",
      "fhe": "0.10",
      "mod": "21",
      "vrm": "15",
      "tank": "11.0"
    },
    {
      "name": "gas deco4",
      "enabled": true,
      "fo2": "1.00",
      "fhe": "0.00",
      "mod": "6",
      "vrm": "15",
      "tank": "5.6"
    }
  ],
  "cc": {
    "sp_single": "1.1",
    "msp_enabled": true,
    "sp_descent": "0.90",
    "sp_bottom": "1.1",
    "sp_deco1": "1.2",
    "sp_deco2": "1.3",
    "sp_deco3": "1.4",
    "deco1_a": "36",
    "deco2_a": "21",
    "dil_fo2": "0.10",
    "dil_fhe": "0.70",
    "dil_mod": "120"
  },
  "bailout": [
    {
      "name": "bailout gas 1",
      "enabled": true,
      "fo2": "0.10",
      "fhe": "0.70",
      "mod": "120",
      "vrm": "15",
      "tank": "40.0"
    },
    {
      "name": "bailout gas 2",
      "enabled": true,
      "fo2": "0.18",
      "fhe": "0.62",
      "mod": "65",
      "vrm": "15",
      "tank": "11.0"
    },
    {
      "name": "bailout gas 3",
      "enabled": true,
      "fo2": "0.35",
      "fhe": "0.30",
      "mod": "36",
      "vrm": "15",
      "tank": "11.0"
    },
    {
      "name": "bailout gas 4",
      "enabled": true,
      "fo2": "0.50",
      "fhe": "0.10",
      "mod": "21",
      "vrm": "15",
      "tank": "11.0"
    },
    {
      "name": "bailout gas 5",
      "enabled": true,
      "fo2": "1.00",
      "fhe": "0.00",
      "mod": "6",
      "vrm": "15",
      "tank": "5.6"
    }
  ],
  "adv": {
    "adv_Minimum_Deco_Stop_Time": 1.0,
    "adv_Critical_Volume_Algorithm": "ON",
    "adv_Crit_Volume_Parameter_Lambda": 6500.0,
    "adv_Gradient_Onset_of_Imperm_Atm": 8.2,
    "adv_Surface_Tension_Gamma": 0.0179,
    "adv_Skin_Compression_GammaC": 0.257,
    "adv_Regeneration_Time_Constant": 20160.0,
    "adv_Pressure_Other_Gases_mmHg": 102.0
  },
  "stopv_minutes_by_depth": {
    "66": 0.0,
    "63": 0.0,
    "60": 1.0,
    "57": 1.0,
    "54": 1.0,
    "51": 1.0,
    "48": 1.0,
    "45": 1.0,
    "42": 1.0,
    "39": 1.0,
    "36": 3.0,
    "33": 3.0,
    "30": 3.0,
    "27": 3.0,
    "24": 3.0,
    "21": 6.0,
    "18": 6.0,
    "15": 6.0,
    "12": 6.0,
    "9": 6.0,
    "6": 20.0
  },
  "stopv_enabled": false,
  "zhl16": {}
}'''

def _ensure_inputs_files_exist() -> None:
    """Assicura la presenza dei file in APPDATA.

    - Se manca default_inputs.json: lo crea dall'embedded.
    - Se manca last_inputs.json: lo inizializza copiando il default.
    """
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
    except Exception:
        pass

    try:
        if not os.path.exists(DEFAULT_INPUTS_FILE):
            with open(DEFAULT_INPUTS_FILE, "w", encoding="utf-8") as f:
                f.write(DEFAULT_INPUTS_EMBEDDED_JSON)
    except Exception:
        pass

    try:
        if not os.path.exists(LAST_INPUTS_FILE):
            # inizializza last = default
            try:
                data = json.loads(DEFAULT_INPUTS_EMBEDDED_JSON)
            except Exception:
                data = {}
            with open(LAST_INPUTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_last_inputs():
    """Carica gli ultimi input salvati (APPDATA)."""
    _ensure_inputs_files_exist()
    if not os.path.exists(LAST_INPUTS_FILE):
        return None
    try:
        with open(LAST_INPUTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_last_inputs(data: dict) -> None:
    """Salva gli input correnti (APPDATA). Gli errori non devono bloccare la GUI."""
    _ensure_inputs_files_exist()
    try:
        with open(LAST_INPUTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def parse_float(value: str, field_name: str) -> float:
    """
    Converte una stringa in float accettando anche la virgola come separatore.
    Se fallisce, solleva ValueError con messaggio esplicito.
    """
    s = (value or "").strip().replace(",", ".")
    if not s:
        raise ValueError(f"Il campo '{field_name}' è vuoto.")
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"Il campo '{field_name}' deve essere un numero (es. 80 o 80.0).")






# -----------------------------
# Metriche operative (post-processing GUI, OC)
# Nessun impatto sul motore VPM.
# -----------------------------
import math

MW_O2 = 31.998
MW_N2 = 28.0134
MW_He = 4.0026

# Volume molare (L/mol). Se necessario per match con planner diversi, si può passare a ~24.055 (20°C).
MOLAR_VOL_L = 22.414

PH2O_ATM = 0.0627  # 47 mmHg (37°C)

# NOAA single exposure limits (min) vs ppO2 (atm) — interpolazione lineare
NOAA_LIMITS_MIN = [
    (0.50, 720),
    (0.60, 720),
    (0.70, 570),
    (0.80, 450),
    (0.90, 360),
    (1.00, 300),
    (1.10, 240),
    (1.20, 210),
    (1.30, 180),
    (1.40, 150),
    (1.50, 120),
    (1.60, 45),
]

def p_abs_atm(depth_m: float) -> float:
    return depth_m / 10.0 + 1.0

def mix_density_surface_gL(fo2: float, fhe: float) -> float:
    fn2 = max(0.0, 1.0 - fo2 - fhe)
    mw = fo2 * MW_O2 + fhe * MW_He + fn2 * MW_N2
    return mw / MOLAR_VOL_L  # g/L @ 1 atm

def gas_density_gL(depth_m: float, fo2: float, fhe: float) -> float:
    return p_abs_atm(depth_m) * mix_density_surface_gL(fo2, fhe)

def icd_atm(depth_m: float, fo2: float, fhe: float) -> float:
    fn2 = max(0.0, 1.0 - fo2 - fhe)
    return (p_abs_atm(depth_m) - PH2O_ATM) * (fn2 + fhe)

def ead_m(depth_m: float, fo2: float, fhe: float):
    fn2 = max(0.0, 1.0 - fo2 - fhe)
    pamb = p_abs_atm(depth_m)  # ata
    # EAD = pamb * FN2/0.79 * 10 - 10  (metri)
    return pamb * (fn2 / 0.79) * 10.0 - 10.0

def noaa_limit_minutes(ppO2: float):
    if ppO2 < 0.5:
        return None
    if ppO2 >= 1.6:
        return 45.0
    pts = NOAA_LIMITS_MIN
    for i in range(len(pts) - 1):
        p1, t1 = pts[i]
        p2, t2 = pts[i + 1]
        if ppO2 >= p1 and ppO2 <= p2:
            if abs(p2 - p1) < 1e-12:
                return float(t1)
            w = (ppO2 - p1) / (p2 - p1)
            return float(t1 + w * (t2 - t1))
    return None

def cns_rate_percent_per_min(ppO2: float) -> float:
    """CNS% per minute based on the user's piecewise Excel model.
    ppO2 is in atm (≈bar). Returns CNS percent per minute.
    """
    p = float(ppO2)

    if p < 0.51:
        return 0.1

    # Linear formula segments (as provided)
    if p < 0.61:
        return 1.0 / (p * 100.0 * -533.07 + 54000.0) * 3000.0
    if p < 0.71:
        return 1.0 / (p * 100.0 * -444.22 + 48600.0) * 3000.0
    if p < 0.81:
        return 1.0 / (p * 100.0 * -355.38 + 42300.0) * 3000.0
    if p < 0.91:
        return 1.0 / (p * 100.0 * -266.53 + 35100.0) * 3000.0
    if p < 1.01:
        return 1.0 / (p * 100.0 * -177.69 + 27000.0) * 3000.0
    if p < 1.11:
        return 1.0 / (p * 100.0 * -177.69 + 27000.0) * 3000.0
    if p < 1.21:
        return 1.0 / (p * 100.0 * -88.84 + 17100.0) * 3000.0
    if p < 1.31:
        return 1.0 / (p * 100.0 * -88.84 + 17100.0) * 3000.0
    if p < 1.41:
        return 1.0 / (p * 100.0 * -88.84 + 17100.0) * 3000.0
    if p < 1.51:
        return 1.0 / (p * 100.0 * -88.84 + 17100.0) * 3000.0
    if p < 1.61:
        return 1.0 / (p * 100.0 * -222.11 + 37350.0) * 3000.0

    # Discrete steps
    if p < 1.66:
        return 2.25
    if p < 1.71:
        return 3.06
    if p < 1.76:
        return 4.08
    if p < 1.81:
        return 5.4
    if p < 1.86:
        return 7.11
    if p < 1.91:
        return 9.3
    if p < 1.96:
        return 12.03
    if p < 2.01:
        return 15.51
    if p < 2.06:
        return 22.8
    if p < 2.11:
        return 33.0
    if p < 2.16:
        return 45.0
    if p < 2.21:
        return 62.7
    if p < 2.26:
        return 87.0
    if p < 2.31:
        return 117.0
    return 144.6

def cns_increment_percent(seg_min: float, ppO2: float) -> float:
    # CNS progressive increment for the segment
    return cns_rate_percent_per_min(ppO2) * float(seg_min)

def otu_increment(seg_min: float, ppO2: float) -> float:
    if ppO2 <= 0.5:
        return 0.0
    return seg_min * ((ppO2 - 0.5) / 0.5) ** (5.0 / 6.0)

def _effective_descent_segments(depth_bottom: float, descent_bands: list[dict]) -> list[dict]:
    """Return a list of descent segments (DESC) + optional DSTOP segments derived from bands.

    IMPORTANT: does NOT mutate descent_bands (they are global presets, not tied to current dive).
    It only truncates the *effective* profile to the current bottom depth.
    """
    segments: list[dict] = []
    if depth_bottom <= 0.0:
        return segments

    # Ensure we have a shallow->deep ordered list
    bands = []
    for b in (descent_bands or []):
        try:
            f = float(b.get("from_m", 0.0))
            t = float(b.get("to_m", 0.0))
            sp = float(b.get("speed", 0.0))
            sm = float(b.get("stop_min", 0.0))
        except Exception:
            continue
        bands.append({"from_m": f, "to_m": t, "speed": sp, "stop_min": sm})
    bands.sort(key=lambda x: x["from_m"])

    cur = 0.0
    rt = 0.0

    for b in bands:
        if cur >= depth_bottom - 1e-9:
            break

        # segment end is the lesser between band to_m and bottom depth
        seg_end = min(depth_bottom, max(cur, b["to_m"]))
        if seg_end <= cur + 1e-9:
            continue

        speed = b["speed"] if b["speed"] > 0 else 20.0
        seg_time = (seg_end - cur) / speed
        rt += seg_time
        segments.append({
            "Tipo": "DESC",
            "Seg_time_min": seg_time,
            "Run_time_min": rt,
            "Depth_from_m": cur,
            "Depth_to_m": seg_end,
            "Depth_m": None,
        })
        cur = seg_end

        # optional stop at end of band (only if not yet at bottom depth)
        stop_min = b["stop_min"]
        if stop_min and stop_min > 0.0 and cur < depth_bottom - 1e-9:
            rt += stop_min
            segments.append({
                "Tipo": "DSTOP",
                "Seg_time_min": stop_min,
                "Run_time_min": rt,
                "Depth_from_m": None,
                "Depth_to_m": None,
                "Depth_m": cur,
            })

    # If bands do not reach bottom, finish with a last descent at the last known speed (or 20)
    if cur < depth_bottom - 1e-9:
        last_speed = 20.0
        if bands:
            try:
                last_speed = float(bands[-1].get("speed", 20.0)) or 20.0
            except Exception:
                last_speed = 20.0
        seg_time = (depth_bottom - cur) / last_speed
        rt += seg_time
        segments.append({
            "Tipo": "DESC",
            "Seg_time_min": seg_time,
            "Run_time_min": rt,
            "Depth_from_m": cur,
            "Depth_to_m": depth_bottom,
            "Depth_m": None,
        })

    return segments


def _ascent_time_piecewise(from_depth: float, to_depth: float, ascent_bands: list[dict]) -> float:
    """Compute ascent travel time from from_depth down to to_depth using piecewise band speeds.
    Bands are expected deep->shallow with fields from_m (deep), to_m (shallow), speed.
    """
    if from_depth <= to_depth + 1e-9:
        return 0.0
    d = float(from_depth)
    target = float(to_depth)
    time = 0.0

    # Normalize bands
    bands = []
    for b in (ascent_bands or []):
        try:
            f = float(b.get("from_m", 0.0))
            t = float(b.get("to_m", 0.0))
            sp = float(b.get("speed", 0.0))
        except Exception:
            continue
        if sp <= 0.0:
            continue
        # ensure f >= t
        if f < t:
            f, t = t, f
        bands.append({"from_m": f, "to_m": t, "speed": sp})
    # sort deep->shallow
    bands.sort(key=lambda x: -x["from_m"])

    def pick_band(cur_depth: float):
        for b in bands:
            if cur_depth <= b["from_m"] + 1e-9 and cur_depth >= b["to_m"] - 1e-9:
                return b
        return None

    while d > target + 1e-9:
        b = pick_band(d)
        if b is None:
            # fallback: 9 m/min
            next_depth = target
            time += (d - next_depth) / 9.0
            d = next_depth
            continue

        next_depth = max(target, b["to_m"])
        if next_depth >= d - 1e-9:
            # avoid infinite loop
            next_depth = target
        time += (d - next_depth) / b["speed"]
        d = next_depth

    return time


def build_profile_rows_from_result(depth_bottom: float,
                                  bottom_time_min: float,
                                  descent_bands: list[dict],
                                  ascent_bands: list[dict],
                                  stops: list,
                                  runtime_total: float | None = None,
                                  engine_profile: list[dict] | None = None,
                                  ) -> list[dict]:
    """Build the detailed profile rows for the GUI table/export.

    STRADA A (vincolante): la tabella *Profilo dettagliato* deve essere costruita
    esclusivamente dal profilo passivo del MAIN (result['profile']).

    - Nessun fallback/ricostruzione GUI.
    - Se engine_profile è assente o vuoto -> errore (fail-fast).

    Le colonne 1–7 (segmento/tipo/durate/runtime/profondità) derivano dal MAIN.
    Le colonne Note e Mode sono importate dal MAIN (note, mode_row).

    Parametri come descent_bands/ascent_bands/stops sono mantenuti per compatibilità
    firma ma non vengono usati in STRADA A.
    """

    if not engine_profile:
        raise RuntimeError(
            "ENGINE_PROFILE_MISSING: MAIN did not provide passive profile rows (result['profile'])."
        )

    rows: list[dict] = []
    seg_n = 1
    seen_ascent = False

    for i, seg in enumerate(engine_profile):
        kind = str(seg.get('kind', '')).upper()

        # FIX (layout-only): elimina righe spurie 'ASC' che duplicano la discesa.
        # Alcune pipeline possono produrre una riga ASC con from->to crescente (quindi in realtà DESC)
        # e, talvolta, la riga DESC corretta subito dopo con stessi campi e runtime identico.
        # In tal caso, scartiamo l'ASC spurio; altrimenti lo riclassifichiamo come DESC.
        try:
            _from = seg.get('from_m', None)
            _to = seg.get('to_m', None)
            if kind == 'ASC' and _from is not None and _to is not None:
                f = float(_from)
                t = float(_to)
                if t > f + 1e-9:  # profondità aumenta -> discesa
                    # se il prossimo segmento è la copia DESC, elimina questo
                    if i + 1 < len(engine_profile):
                        nxt = engine_profile[i + 1] or {}
                        k2 = str(nxt.get('kind', '')).upper()
                        try:
                            f2 = float(nxt.get('from_m'))
                            t2 = float(nxt.get('to_m'))
                        except Exception:
                            f2 = t2 = None
                        try:
                            rt1 = float(seg.get('runtime_end', 0.0))
                            rt2 = float(nxt.get('runtime_end', 0.0))
                        except Exception:
                            rt1 = rt2 = 0.0
                        try:
                            st1 = float(seg.get('step_min', 0.0))
                            st2 = float(nxt.get('step_min', 0.0))
                        except Exception:
                            st1 = st2 = 0.0
                        if k2 == 'DESC' and f2 is not None and t2 is not None and abs(f2 - f) < 1e-9 and abs(t2 - t) < 1e-9 and abs(rt2 - rt1) < 1e-9 and abs(st2 - st1) < 1e-9:
                            continue  # scarta riga ASC spurio
                    kind = 'DESC'
        except Exception:
            pass
        phase = 'ASCENT_DECO' if seen_ascent else 'DESCENT'
        step_min = seg.get('step_min', None)
        rt_end = seg.get('runtime_end', None)
        from_m = seg.get('from_m', None)
        to_m = seg.get('to_m', None)
        depth_m = seg.get('depth_m', None)
        note = seg.get('note', '')
        mode_row = seg.get('mode_row', '')
        mix = seg.get('mix', None)
        gf_actual = seg.get('GF_actual', None)

        rows.append({
            'Segmento': seg_n,
            'Tipo': kind,
            'Seg_time_min': None if step_min is None else float(step_min),
            'Run_time_min': None if rt_end is None else float(rt_end),
            'Depth_from_m': None if from_m is None else float(from_m),
            'Depth_to_m': None if to_m is None else float(to_m),
            'Depth_m': None if depth_m is None else float(depth_m),
            'Phase': phase,
            'Note': '' if note is None else str(note),
            'Mode': '' if mode_row is None else str(mode_row).upper(),
            # Campi eventualmente utili in Fase 2 (mapping gas)
            'Mix': None if mix is None else int(mix),
            # GF effettivo calcolato dal MAIN (in percentuale) se disponibile
            'GF_actual': None if gf_actual is None else float(gf_actual),
        })
        if kind == 'ASC':
            seen_ascent = True
        seg_n += 1

    return rows
def run_vpmb_subprocess(depth_m: float,
                        bottom_time_min: float,
                        desc_rate: float,
                        asc_rate: float,
                        fo2: float,
                        fhe: float,
                        rapsol: float,
                        crit_rad_n2: float,
                        crit_rad_he: float,
                        adv=None,
                        last_stop_m: float = 3.0,
                        gas_table=None) -> str:
    """
    Esegue il motore VPMB/ZHL-16C *in process* usando il porting Python
    e restituisce l'output testuale del PROFILO DETTAGLIATO.
    Integra inoltre i profili di discesa/risalita (A1/A2/A3) passandoli
    come JSON al main (_build_in_text_from_args).
    """
    # Carica il modulo del motore a partire dal path VPMB_SCRIPT
    # Carica il modulo del motore a partire dal path VPMB_SCRIPT
    # (workaround robusto: su alcuni ambienti Windows spec_from_file_location può restituire loader=None).
    try:
        import importlib

        # In build compilata (Nuitka/standalone) il motore deve essere incluso come modulo,
        # quindi NON va caricato da file .py su disco.
        _compiled = getattr(sys, "frozen", False) or ("__compiled__" in globals())
        if _compiled:
            module = importlib.import_module(ENGINE_MODULE_NAME)
        else:
            import importlib.machinery
            loader = importlib.machinery.SourceFileLoader("vpmdeco_engine", VPMB_SCRIPT)
            spec = importlib.util.spec_from_loader(loader.name, loader)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Impossibile caricare il motore VPMB da {VPMB_SCRIPT}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except Exception as _e:
        raise RuntimeError(f"Impossibile caricare il motore VPMB: {_e}")

    try:
        enabled = False
        if adv is not None and hasattr(adv, "var_stopv_enabled"):
            try:
                enabled = bool(adv.var_stopv_enabled.get())
            except Exception:
                enabled = bool(getattr(adv, "var_stopv_enabled"))

        if enabled and adv is not None and hasattr(adv, "stopv_minutes_by_depth") and isinstance(getattr(adv, "stopv_minutes_by_depth"), dict):
            module.STOPV_MINUTES_BY_DEPTH = {int(k): float(v) for k, v in dict(getattr(adv, "stopv_minutes_by_depth")).items()}
        else:
            # Forza a zero: i valori eventualmente presenti restano memorizzati in GUI ma non vengono applicati.
            module.STOPV_MINUTES_BY_DEPTH = {int(d): 0.0 for d in STOPV_DEPTHS_M}
    except Exception:
        pass
    # Debug Projected Ascent (CSV) removed in production
    debug_flag = 0

    # Profili bande discesa/risalita (A2/A3)
    descent_bands = getattr(adv, "descent_speed_bands", [])
    ascent_bands = getattr(adv, "ascent_speed_bands", [])
    # Nota: il motore (0023) risolve la velocità di risalita cercando la prima banda che "contiene" la quota.
    # Per evitare ambiguità sulle quote di confine (es. 6 m è sia "to" della banda sopra che "from" della banda sotto),
    # passiamo le bande in ordine dal più superficiale al più profondo, così il confine eredita la banda "sotto" (più lenta).
    ascent_bands_for_engine = list(ascent_bands)[::-1] if ascent_bands else []

    # Soste di discesa derivate dalle bande:
    # ogni banda con stop_min > 0 genera uno stop a profondità = to_m per tempo = stop_min.
    descent_stops = []
    for b in descent_bands:
        try:
            to_m = float(b.get("to_m", 0.0))
            stop_min = float(b.get("stop_min", 0.0))
        except Exception:
            continue
        if to_m > 0.0 and stop_min > 0.0:
            descent_stops.append({"depth": to_m, "time": stop_min})

    # (pulizia: rimuovi soste nulle / non valide)
    descent_stops = [
        s for s in descent_stops
        if s.get("depth", 0.0) > 0.0 and s.get("time", 0.0) > 0.0
    ]

    args = SimpleNamespace(
        depth_m=float(depth_m),
        bottom_time_min=float(bottom_time_min),
        desc_rate=float(desc_rate),
        asc_rate=float(asc_rate),
        FO2=float(fo2),
        FHe=float(fhe),
        rapsol=float(rapsol),
        crit_rad_n2=float(crit_rad_n2),
        crit_rad_he=float(crit_rad_he),
        # Parametri VPM avanzati collegati al main
        Minimum_Deco_Stop_Time=getattr(adv, "adv_Minimum_Deco_Stop_Time", 1.0),
        Critical_Volume_Algorithm=getattr(adv, "adv_Critical_Volume_Algorithm", "ON"),
        Crit_Volume_Parameter_Lambda=getattr(adv, "adv_Crit_Volume_Parameter_Lambda", 6500.0),
        Gradient_Onset_of_Imperm_Atm=getattr(adv, "adv_Gradient_Onset_of_Imperm_Atm", 8.2),
        Surface_Tension_Gamma=getattr(adv, "adv_Surface_Tension_Gamma", 0.0179),
        Skin_Compression_GammaC=getattr(adv, "adv_Skin_Compression_GammaC", 0.257),
        Regeneration_Time_Constant=getattr(adv, "adv_Regeneration_Time_Constant", 20160.0),
        Pressure_Other_Gases_mmHg=getattr(adv, "adv_Pressure_Other_Gases_mmHg", 102.0),
        step_size=3.0,
        last_stop_m=float(last_stop_m),
        gases_json=json.dumps(gas_table) if gas_table else None,
        # Nuovi JSON per profili A1/A2/A3
        descent_stops_json=json.dumps(descent_stops),
        descent_bands_json=json.dumps(descent_bands),
        ascent_bands_json=json.dumps(ascent_bands_for_engine),
        # debug_projected_ascent removed in production
)

    # Imposta i testi embedded come farebbe il main del motore
    module.VPMDECO_SET_TEXT = module._build_set_text_from_args(args)
    _in_text = module._build_in_text_from_args(args)
    _in_text = _patch_in_text_last_stop(_in_text, float(getattr(args, "last_stop_m", 3.0)))
    module.VPMDECO_IN_TEXT = _in_text
    # module.DEBUG_PROJECTED_ASCENT removed in production
    buf = io.StringIO()
    try:
        # Redirige stdout temporaneamente per catturare la stampa del profilo
        old_stdout = sys.stdout
        sys.stdout = buf
        # Abilita il "passive schedule log" del motore (profilo segmentato coerente con il calcolo)
        _prev_vpm_sched = os.environ.get("VPM_FORTRAN_SCHEDULE")


        # -----------------------------
        # Modalità CCR: la GUI governa il toggle OC/CC e il setpoint "bottom" verso il main tramite env var.
        # Nessun nuovo interruttore nel main: qui è solo wiring GUI→MAIN.
        _prev_ccr_mode = os.environ.get("VPM_CCR_MODE")
        _prev_ccr_sp = os.environ.get("VPM_CCR_SP_ATM")
        _prev_bailout = os.environ.get("VPM_BAILOUT_ONESHOT")
        try:
            is_cc = (adv is not None and hasattr(adv, "mode_var") and adv.mode_var.get() == "CC")
            if is_cc:
                # CC: abilita CCR e imposta un setpoint MONO-SP (per ora) governato dalla GUI.
                os.environ["VPM_CCR_MODE"] = "1"


                # Multi-SP a bande (stabile): se abilitato in GUI, passiamo una struttura esplicita al MAIN
                # evitando side-effects globali (env var) e lasciando invariata la logica mono-SP se disabilitato.
                try:
                    if hasattr(module, "CCR_SETTINGS"):
                        module.CCR_SETTINGS = None
                except Exception:
                    pass

                _use_msp = False
                try:
                    _use_msp = bool(getattr(adv, "cc_msp_enabled").get()) if hasattr(adv, "cc_msp_enabled") else False
                except Exception:
                    _use_msp = False

                if _use_msp and hasattr(adv, "get_cc_setpoint_segments"):
                    _segs, _errs = adv.get_cc_setpoint_segments()
                    if _errs:
                        raise ValueError("CCR multi-setpoint: " + "; ".join([str(x) for x in _errs]))
                    try:
                        module.CCR_SETTINGS = {
                            "enabled": True,
                            "segments": _segs,
                            "units": "msw",
                        }
                    except Exception:
                        pass

                # Regola mono-SP: usa SP singolo (fonte unica). Per retro-compatibilità, se assente usa la vecchia logica (bottom→descent→default).
                sp_txt = ""
                if hasattr(adv, "cc_sp_single"):
                    sp_txt = str(adv.cc_sp_single.get()).strip()

                # Backward compatibility (vecchie GUI senza SP_single)
                if not sp_txt and hasattr(adv, "cc_sp_bottom"):
                    sp_txt = str(adv.cc_sp_bottom.get()).strip()
                if not sp_txt and hasattr(adv, "cc_sp_descent"):
                    sp_txt = str(adv.cc_sp_descent.get()).strip()

                sp_txt = sp_txt.replace(",", ".").strip()

                if sp_txt:
                    os.environ["VPM_CCR_SP_ATM"] = sp_txt
                else:
                    os.environ["VPM_CCR_SP_ATM"] = "0.9"
            else:
                # OC: disabilita CCR e rimuove il setpoint (il main userà logica OC pura)
                os.environ["VPM_CCR_MODE"] = "0"
                os.environ.pop("VPM_CCR_SP_ATM", None)
                # Evita che eventuali settaggi CCR strutturati restino appesi tra calcoli
                try:
                    if hasattr(module, "CCR_SETTINGS"):
                        module.CCR_SETTINGS = None
                except Exception:
                    pass


            # Bailout one-shot: attivo solo se siamo in CC e la checkbox BO è attiva
            try:
                if is_cc and hasattr(adv, "chk_use_bailout") and adv.chk_use_bailout is not None and bool(adv.chk_use_bailout.get()):
                    os.environ["VPM_BAILOUT_ONESHOT"] = "1"
                else:
                    os.environ["VPM_BAILOUT_ONESHOT"] = "0"
            except Exception:
                os.environ["VPM_BAILOUT_ONESHOT"] = "0"
        except Exception:
            # se qualcosa va storto, non blocchiamo il run; il main andrà coi default
            pass
# Allinea anche le variabili globali del modulo (CCR_MODE / CCR_SP_ATM),
        # perché nel main vengono inizializzate a import-time. Qui forziamo lo stato coerente con la GUI.
        try:
            module.CCR_MODE = (os.environ.get("VPM_CCR_MODE", "1") in ("1", "true", "TRUE", "yes", "YES"))
            if os.environ.get("VPM_CCR_SP_ATM") is not None:
                _sp_atm = float(str(os.environ.get("VPM_CCR_SP_ATM")).replace(",", "."))
                module.CCR_SP_ATM = _sp_atm
                # Il main calcola anche CCR_SP_MSW a import-time: riallinealo se presente.
                if hasattr(module, "CCR_SP_MSW"):
                    module.CCR_SP_MSW = _sp_atm * 10.0
        except Exception:
            pass

        # Bailout one-shot: abilita BO (CCR -> OC) nel main solo se richiesto dalla GUI
        os.environ["VPM_FORTRAN_SCHEDULE"] = "1"
        # Esegue il motore (funzione main-like) e cattura result numerico
        result = module.VPMDECO_ORG()
        # Salva anche il passive profile "finale" del motore (senza alcun post-processing GUI)
        try:
            prof_raw = (result.get("profile") or [])
            # NOTE: per contratto, il report deve usare il profilo "passivo" completo del MAIN
            # (una riga = un segmento MAIN), che contiene anche note/mode_row.
            # Le funzioni _select_final_profile* producono un sottoinsieme (tipicamente STOP-only)
            # e perderebbero le info di reporting.
            adv.last_engine_profile = prof_raw
        except Exception:
            adv.last_engine_profile = None
        # Stampa il profilo dettagliato sul buffer
        module._print_profile_dettagliato(args, result)
    except Exception as e:
        raise RuntimeError(f"Errore VPMB (esecuzione interna): {e}")
    finally:
        # ripristina env var del motore
        try:
            if _prev_vpm_sched is None:
                os.environ.pop("VPM_FORTRAN_SCHEDULE", None)
            else:
                os.environ["VPM_FORTRAN_SCHEDULE"] = _prev_vpm_sched
        except Exception:
            pass
        # Ripristina env var CCR (se presenti)
        try:
            if _prev_ccr_mode is None:
                os.environ.pop("VPM_CCR_MODE", None)
            else:
                os.environ["VPM_CCR_MODE"] = _prev_ccr_mode
            if _prev_ccr_sp is None:
                os.environ.pop("VPM_CCR_SP_ATM", None)
            else:
                os.environ["VPM_CCR_SP_ATM"] = _prev_ccr_sp
            if _prev_bailout is None:
                os.environ.pop("VPM_BAILOUT_ONESHOT", None)
            else:
                os.environ["VPM_BAILOUT_ONESHOT"] = _prev_bailout
        except Exception:
            pass
        sys.stdout = old_stdout

    output_text = buf.getvalue()

    # Salva l'ultimo risultato numerico sull'istanza della GUI
    try:
        adv.last_vpmb_result = result
    except Exception:
        pass
    # Debug Projected Ascent (CSV) export removed in production

    return output_text

def parse_detailed_profile(output_text):
    """
    Estrae la tabella 'PROFILO DETTAGLIATO' dall'output di vpmb_zhl_main.py
    (ora include anche DESC e BOTT) e la ritorna come lista di dizionari.
    """
    lines = output_text.splitlines()
    in_section = False
    rows = []

    asc_pattern = re.compile(
        r"^\s*(\d+)\s+ASC\s+seg_time=\s*([0-9.]+)\s+run_time=\s*([0-9.]+)\s+from=\s*([0-9.]+)m\s+to=\s*([0-9.]+)m"
    )
    stop_pattern = re.compile(
        r"^\s*(\d+)\s+STOP\s+seg_time=\s*([0-9.]+)\s+run_time=\s*([0-9.]+)\s+depth=\s*([0-9.]+)m"
    )
    desc_pattern = re.compile(
        r"^\s*(\d+)\s+DESC\s+seg_time=\s*([0-9.]+)\s+run_time=\s*([0-9.]+)\s+from=\s*([0-9.]+)m\s+to=\s*([0-9.]+)m"
    )
    bott_pattern = re.compile(
        r"^\s*(\d+)\s+BOTT\s+seg_time=\s*([0-9.]+)\s+run_time=\s*([0-9.]+)\s+depth=\s*([0-9.]+)m"
    )

    for line in lines:
        if "=== PROFILO DETTAGLIATO" in line:
            in_section = True
            continue
        if not in_section:
            continue
        if not line.strip():
            continue

        m = desc_pattern.match(line)
        if m:
            seg, seg_time, run_time, d_from, d_to = m.groups()
            rows.append({
                "Segmento": int(seg),
                "Tipo": "DESC",
                "Seg_time_min": float(seg_time),
                "Run_time_min": float(run_time),
                "Depth_from_m": float(d_from),
                "Depth_to_m": float(d_to),
                "Depth_m": None,
            })
            continue

        m = bott_pattern.match(line)
        if m:
            seg, seg_time, run_time, depth = m.groups()
            rows.append({
                "Segmento": int(seg),
                "Tipo": "BOTT",
                "Seg_time_min": float(seg_time),
                "Run_time_min": float(run_time),
                "Depth_from_m": None,
                "Depth_to_m": None,
                "Depth_m": float(depth),
            })
            continue

        m = asc_pattern.match(line)
        if m:
            seg, seg_time, run_time, d_from, d_to = m.groups()
            rows.append({
                "Segmento": int(seg),
                "Tipo": "ASC",
                "Seg_time_min": float(seg_time),
                "Run_time_min": float(run_time),
                "Depth_from_m": float(d_from),
                "Depth_to_m": float(d_to),
                "Depth_m": None,
            })
            continue

        m = stop_pattern.match(line)
        if m:
            seg, seg_time, run_time, depth = m.groups()
            rows.append({
                "Segmento": int(seg),
                "Tipo": "STOP",
                "Seg_time_min": float(seg_time),
                "Run_time_min": float(run_time),
                "Depth_from_m": None,
                "Depth_to_m": None,
                "Depth_m": float(depth),
            })
            continue

    return rows


def parse_tissue_profile(output: str):
    """
    Parsea la sezione:

      === PROFILO TESSUTI VPM-B (per confronto Py/Fortran) ===
      step;quota_m;tempo_step_min;runtime_min;Pamb_bar;ppO2_bar;...

    Restituisce una lista di dizionari, uno per ciascun segmento di deco
    (ASC/STOP) del profilo VPM-B.
    """
    lines = output.splitlines()
    start_idx = None

    # Cerca la riga di intestazione della sezione tessuti
    for i, line in enumerate(lines):
        if "=== PROFILO TESSUTI VPM-B" in line:
            start_idx = i
            break

    if start_idx is None:
        return []

    # Cerca la riga di header (quella che comincia con "step;")
    header_idx = None
    for j in range(start_idx + 1, len(lines)):
        line = lines[j].strip()
        if not line:
            continue
        if line.startswith("step;"):
            header_idx = j
            break

    if header_idx is None:
        return []

    header = lines[header_idx].strip().split(";")
    rows = []

    for k in range(header_idx + 1, len(lines)):
        line = lines[k].strip()
        if not line:
            break
        if line.startswith("==="):
            break

        parts = line.split(";")
        if len(parts) != len(header):
            break

        row = {}
        for name, value in zip(header, parts):
            value = value.strip()
            if name == "step":
                try:
                    row[name] = int(value)
                except ValueError:
                    row[name] = None
            else:
                # I valori stampati dal main sono con il punto come separatore
                try:
                    row[name] = float(value.replace(",", "."))
                except ValueError:
                    row[name] = None

        rows.append(row)

    return rows


class VPMBApp(tk.Tk):
    def _validate_non_negative_number(self, P):
        """Entry key-validation: allow empty while typing; allow numeric >= 0 (dot or comma)."""
        if P is None:
            return True
        s = str(P).strip()
        if s == "":
            return True
        if s in (".", ","):
            return True
        s_norm = s.replace(",", ".")
        if "-" in s_norm:
            return False
        if not re.fullmatch(r"(\d+(\.\d*)?|\.\d+)", s_norm):
            return False
        try:
            return float(s_norm) >= 0.0
        except Exception:
            return False

    def __init__(self):
        super().__init__()

        # App icon (Windows): replace default Tk feather/leaf icon with DECOSOL DS
        try:
            _ico = _ensure_ds_ico_file()
            if _ico:
                self.iconbitmap(_ico)
        except Exception:
            pass
        self.title("DECOSOL programma sperimentale di calcolo decompressivo in versione provvisoria v.1.0 beta — Build 2026.02")
        self.geometry("900x600")

        # --- ttk styles (error highlighting) ---
        try:
            self._style = ttk.Style(self)
            # Make an "Error" entry style (works on most themes; fallback harmless)
            self._style.configure("Error.TEntry", foreground="red")
            # STOPV button styles (OFF uses default TButton)
            self._style.configure("StopvOn.TButton", foreground="#cc0000")
            self._style.configure("StopvOff.TButton")

            self._style.map("Error.TEntry",
                            foreground=[("!disabled", "red")])
            # Calculated fields styles (grey background; red for negative)
            try:
                self._style.configure("Calc.TEntry", foreground="black", fieldbackground="#e6e6e6")
                self._style.configure("CalcNeg.TEntry", foreground="red", fieldbackground="#e6e6e6")
            except Exception:
                pass

        except Exception:
            self._style = None

        # Inizializza subito i parametri avanzati ai valori di default
        # così esistono anche se non hai mai aperto la finestra "Parametri VPM avanzati"
        self.init_advanced_defaults()

        # -----------------------------
        # ZH-L16 coefficients (default) - GUI only
        # - support both ZH-L16C and ZH-L16B (a/b for N2 and He)
        # - GUI holds 2 editable sets; selection is persisted and passed to the MAIN (when used)
        # -----------------------------
        self.zhl16_variant = "C"  # 'C' (default) or 'B'
        self.zhl16_coeff_defaults_C =         [
            (1, 1.1696, 0.5578, 1.6189, 0.4770),
            (2, 1.0000, 0.6514, 1.3830, 0.5747),
            (3, 0.8618, 0.7222, 1.1919, 0.6527),
            (4, 0.7562, 0.7825, 1.0458, 0.7223),
            (5, 0.6200, 0.8126, 0.9220, 0.7582),
            (6, 0.5043, 0.8434, 0.8205, 0.7957),
            (7, 0.4410, 0.8693, 0.7305, 0.8279),
            (8, 0.4000, 0.8910, 0.6502, 0.8553),
            (9, 0.3750, 0.9092, 0.5950, 0.8757),
            (10, 0.3500, 0.9222, 0.5545, 0.8903),
            (11, 0.3295, 0.9319, 0.5333, 0.8997),
            (12, 0.3065, 0.9403, 0.5189, 0.9073),
            (13, 0.2835, 0.9477, 0.5181, 0.9122),
            (14, 0.2610, 0.9544, 0.5176, 0.9171),
            (15, 0.2480, 0.9602, 0.5172, 0.9217),
            (16, 0.2327, 0.9653, 0.5119, 0.9267),
        ]
        self.zhl16_coeff_defaults_B =         [
            (1, 1.1696, 0.5578, 1.6189, 0.4770),
            (2, 1.0000, 0.6514, 1.3830, 0.5747),
            (3, 0.8618, 0.7222, 1.1919, 0.6527),
            (4, 0.7562, 0.7825, 1.0458, 0.7223),
            (5, 0.6667, 0.8126, 0.9220, 0.7582),
            (6, 0.5600, 0.8434, 0.8205, 0.7957),
            (7, 0.4947, 0.8693, 0.7305, 0.8279),
            (8, 0.4500, 0.8910, 0.6502, 0.8553),
            (9, 0.4187, 0.9092, 0.5950, 0.8757),
            (10, 0.3798, 0.9222, 0.5545, 0.8903),
            (11, 0.3497, 0.9319, 0.5333, 0.8997),
            (12, 0.3223, 0.9403, 0.5189, 0.9073),
            (13, 0.2850, 0.9477, 0.5181, 0.9122),
            (14, 0.2737, 0.9544, 0.5176, 0.9171),
            (15, 0.2523, 0.9602, 0.5172, 0.9217),
            (16, 0.2327, 0.9653, 0.5119, 0.9267),
        ]

        # Current editable copies (one per set)
        self.zhl16_coeffs_C = [tuple(row) for row in self.zhl16_coeff_defaults_C]
        self.zhl16_coeffs_B = [tuple(row) for row in self.zhl16_coeff_defaults_B]

        # Backward-compat alias: points to the currently selected set
        self.zhl16_coeff_defaults = self.zhl16_coeff_defaults_C
        self.zhl16_coeffs = self.zhl16_coeffs_C
        self._zhl16_coeff_vars = None


        # Inizializza subito anche i parametri deco (ultima sosta, set velocità, ecc.)
        self.init_deco_defaults()

        # Inizializza contenitori per ultimo risultato
        self.last_vpmb_result = None

        # Colonne tabella profilo (base vs estese con metriche operative)
        self.cols_base = ("n", "tipo", "seg_time", "run_time", "from", "to", "depth", "note", "mode", "gas", "ppO2")
        self.cols_ext  = self.cols_base + ("Depth_avg", "Gas_dens_gL", "CNS_%", "OTU", "EAD_m", "GF_actual")

        # Crea tutti i widget della GUI
        self.create_widgets()

        # Fine-tuning posizione warning solubility (px). Modifica questo valore per spostare a dx/sx.
        self.SOL_HINT_X_OFFSET = -9

        
        # Attach CCR logical validation traces (GUI-only)
        self._attach_ccr_logic_traces()
# Prova a ricaricare gli ultimi input salvati
        try:
            self.load_inputs_from_file()
        except Exception:
            pass

        # Autosave (persistenza): modello deco + GF + coeff ZH-L16
        self._autosave_after_id = None
        self._attach_persistence_traces()

        # Salva anche alla chiusura finestra
        try:
            self.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception:
            pass

    def _update_solubility_warning(self, *_args):
        """Reactive visibility for the solubility/rcritHe hint.

        Shows the warning ONLY if:
          - solubility N2/He > 1.1
          - and Raggio critico He (µm) < 0.55
        Otherwise hides the label.
        """
        # VPM-only: this hint refers to Rcrit He (VPM) and must be hidden in ZH-L16 mode
        try:
            if hasattr(self, 'deco_model_var') and str(self.deco_model_var.get()).strip().upper() != 'VPM':
                try:
                    self.label_solubility_hint.configure(text='')
                    self.label_solubility_hint.place_forget()
                except Exception:
                    pass
                return
        except Exception:
            pass

        # 1) solubility
        try:
            s = ""
            try:
                s = str(self.var_solubility.get())
            except Exception:
                s = str(self.entry_rapsol.get())
            s = (s or "").strip().replace(",", ".")
            sol = float(s) if s else None
        except Exception:
            sol = None

        # 2) raggio critico He (µm)
        try:
            r = (self.entry_crit_rad_he.get() or "").strip().replace(",", ".")
            rcrit_he = float(r) if r else None
        except Exception:
            rcrit_he = None

        try:
            if (sol is not None) and (sol > 1.1) and (rcrit_he is not None) and (rcrit_he < 0.55):
                self.label_solubility_hint.configure(
                    text="con Solubility N2/He > 1.1 settare Raggio critico He (μm) = 0.55",
                    foreground="#cc0000"
                )
                self._position_solubility_hint()
            else:
                self.label_solubility_hint.place_forget()
        except Exception:
            pass



    def _after_reposition_solubility_hint(self):
        """Debounce leggero: riposiziona il warning solubility dopo i resize/layout."""
        try:
            self.after_idle(self._position_solubility_hint)
        except Exception:
            try:
                self._position_solubility_hint()
            except Exception:
                pass

    def _position_solubility_hint(self):
        """Posiziona (via place) il warning solubility vicino alla entry (effetto visivo)."""
        try:
            # se vuoto/non visibile, non posizionare
            t = ''
            try:
                t = str(self.label_solubility_hint.cget('text'))
            except Exception:
                t = ''
            if not t:
                try:
                    self.label_solubility_hint.place_forget()
                except Exception:
                    pass
                return

            parent = None
            try:
                parent = self.entry_rapsol.master
            except Exception:
                parent = None

            if parent is None:
                return

            parent.update_idletasks()

            ex = self.entry_rapsol.winfo_x()
            ey = self.entry_rapsol.winfo_y()
            ew = self.entry_rapsol.winfo_width()
            eh = self.entry_rapsol.winfo_height()

            # X: allineamento sotto l'etichetta 'solubility N2/He:' (colonna 4), con offset fine-tuning
            try:
                lx = self.lbl_solubility.winfo_x()
            except Exception:
                lx = ex
            x_offset = getattr(self, 'SOL_HINT_X_OFFSET', 0)  # px (+ destra, - sinistra)
            x = lx + int(x_offset)
            y = ey + max(0, eh // 2)

            self.label_solubility_hint.place(in_=parent, x=x, y=y, anchor='w')
        except Exception:
            pass


    def pick_gas_for_depth(self, depth_m, gas_table=None):
        """
        Seleziona il gas attivo a una certa profondità usando la stessa
        logica del main/Fortran:
          - gas abilitato (enabled == True)
          - MOD >= profondità
          - tra quelli utilizzabili, MOD più bassa.
        Se non trova nulla, ritorna il gas di fondo (prima riga).
        """
        if gas_table is None:
            gas_table = getattr(self, "last_gas_table", None)
        if not gas_table:
            return None

        usable = []
        for g in gas_table:
            try:
                enabled = bool(g.get("enabled", True))
                mod = float(g.get("MOD", 0.0))
            except Exception:
                continue
            if enabled and depth_m <= mod + 1e-6:
                usable.append(g)

        if not usable:
            # fallback: gas di fondo
            return gas_table[0]

        # gas con MOD più bassa tra quelli utilizzabili
        usable.sort(key=lambda x: float(x.get("MOD", 0.0)))
        return usable[0]

    def format_gas_label(self, gas_dict):
        """
        Restituisce una stringa tipo '18/45' a partire da FO2/FHe.
        """
        if not gas_dict:
            return ""
        try:
            fo2 = float(gas_dict.get("FO2", 0.0))
            fhe = float(gas_dict.get("FHe", 0.0))
        except Exception:
            return ""
        return f"{int(round(fo2 * 100)):d}/{int(round(fhe * 100)):d}"


    def create_widgets(self):
        root_frame = ttk.Frame(self, padding=10)
        root_frame.pack(fill=tk.BOTH, expand=True)
        # Initial refresh for inline warnings
        try:
            self._validate_gf_fields()
        except Exception:
            pass
        try:
            self._update_solubility_warning()
        except Exception:
            pass



        # ---------------------
        # Tabs
        # ---------------------
        self.notebook = ttk.Notebook(root_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        tab_main = ttk.Frame(self.notebook)
        tab_profile = ttk.Frame(self.notebook)
        tab_plot = ttk.Frame(self.notebook)

        tab_help = ttk.Frame(self.notebook)

        self.notebook.add(tab_main, text="Setup & Risultati")
        self.notebook.add(tab_profile, text="Profilo dettagliato")
        self.notebook.add(tab_plot, text="Grafico")
        self.notebook.add(tab_help, text="Help")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Per compatibilità col layout esistente, usiamo main_frame come parent dei widget principali
        # ---------------------
        # Scrollable area (Tab: Setup & Risultati)
        # ---------------------
        tab_main_canvas = tk.Canvas(tab_main, highlightthickness=0)
        tab_main_vscroll = ttk.Scrollbar(tab_main, orient="vertical", command=tab_main_canvas.yview)
        tab_main_canvas.configure(yscrollcommand=tab_main_vscroll.set)

        tab_main_vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        tab_main_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tab_main_inner = ttk.Frame(tab_main_canvas)
        tab_main_window = tab_main_canvas.create_window((0, 0), window=tab_main_inner, anchor="nw")

        def _tab_main_on_frame_configure(event):
            tab_main_canvas.configure(scrollregion=tab_main_canvas.bbox("all"))

        def _tab_main_on_canvas_configure(event):
            # Keep inner frame width aligned to canvas width
            tab_main_canvas.itemconfigure(tab_main_window, width=event.width)

        tab_main_inner.bind("<Configure>", _tab_main_on_frame_configure)
        tab_main_canvas.bind("<Configure>", _tab_main_on_canvas_configure)

        def _tab_main_on_mousewheel(event):
            # Windows/macOS: event.delta (multiples of 120 on Windows)
            if getattr(event, "delta", 0):
                tab_main_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            else:
                # Linux (X11) may send Button-4/5
                if getattr(event, "num", None) == 4:
                    tab_main_canvas.yview_scroll(-3, "units")
                elif getattr(event, "num", None) == 5:
                    tab_main_canvas.yview_scroll(3, "units")

        tab_main_canvas.bind("<Enter>", lambda e: tab_main_canvas.focus_set())
        tab_main_canvas.bind("<MouseWheel>", _tab_main_on_mousewheel)
        tab_main_canvas.bind("<Button-4>", _tab_main_on_mousewheel)
        tab_main_canvas.bind("<Button-5>", _tab_main_on_mousewheel)

        # use tab_main_inner as parent of widgets principali
        main_frame = tab_main_inner

        # ---------------------
        # Frame Input
        # ---------------------
                # ---------------------
        # Frame Input (REWRITE deterministico: solo grid, warning inline)
        # ---------------------
        input_frame = ttk.LabelFrame(main_frame, text="Input immersione", padding=6)
        # General error/info line for blocking validation (used by on_calculate)
        self.label_error = ttk.Label(input_frame, text="", foreground="red")
        input_frame.pack(side=tk.TOP, fill=tk.X)

        # Colonne: 0..7 (7 elastica)
        try:
            input_frame.grid_columnconfigure(7, weight=1)
        except Exception:
            pass

        # ---- Row 0: Depth / Time / Solubility + warning solubility inline (a dx del campo) ----
        ttk.Label(input_frame, text="Profondità fondo [m]:").grid(row=0, column=0, sticky=tk.W, padx=(0,2), pady=0)
        self.entry_depth = ttk.Entry(input_frame, width=4)
        self.entry_depth.grid(row=0, column=1, sticky=tk.W, padx=(0,10), pady=10)
        self.entry_depth.configure(validate='key', validatecommand=(self.register(self._validate_non_negative_number), '%P'))
        self.entry_depth.delete(0, tk.END)
        self.entry_depth.insert(0, "80")

        ttk.Label(input_frame, text="Tempo di fondo [min]:").grid(row=0, column=2, sticky=tk.W, padx=(8,2), pady=10)
        self.entry_bottom = ttk.Entry(input_frame, width=4)
        self.entry_bottom.grid(row=0, column=3, sticky=tk.W, padx=(0,10), pady=10)
        self.entry_bottom.configure(validate='key', validatecommand=(self.register(self._validate_non_negative_number), '%P'))
        self.entry_bottom.delete(0, tk.END)
        self.entry_bottom.insert(0, "25")

        self.lbl_solubility = ttk.Label(input_frame, text="Solubility N2/He [num]:")
        self.lbl_solubility.grid(row=0, column=4, sticky=tk.W, padx=(8,2), pady=10)
        self.entry_rapsol = ttk.Entry(input_frame, width=4)
        self.entry_rapsol.grid(row=0, column=5, sticky=tk.W, padx=(0,6), pady=10)
        try:
            self.entry_rapsol.delete(0, tk.END)
            self.entry_rapsol.insert(0, "1.00")
        except Exception:
            pass

        # Warning solubility (VPM-only): stesso ROW, a destra del campo solubility
        self.label_solubility_hint = ttk.Label(input_frame, text="", foreground="#cc0000")
        # Warning solubility (VPM-only): posizionato via .place() per effetto visivo vicino al campo (no padx negativi)
        self.label_solubility_hint.place_forget()
        try:
            # riposiziona il warning quando il frame cambia dimensione
            input_frame.bind('<Configure>', lambda e: self._after_reposition_solubility_hint())
        except Exception:
            pass
        ttk.Label(input_frame, text="").grid(row=0, column=7, sticky=tk.W)

        # ---- Row 1: Modalità respirazione (grid, no pack) ----
        if not hasattr(self, "mode_var"):
            self.mode_var = tk.StringVar(value="OC")
        ttk.Label(input_frame, text="Modalità respirazione:").grid(row=1, column=0, sticky=tk.W, padx=(0,6), pady=0)
        ttk.Radiobutton(input_frame, text="Open Circuit (OC)", variable=self.mode_var, value="OC",
                        command=self._on_mode_change).grid(row=1, column=1, sticky=tk.W, pady=4)
        ttk.Radiobutton(input_frame, text="Closed Circuit (CC)", variable=self.mode_var, value="CC",
                        command=self._on_mode_change).grid(row=1, column=2, sticky=tk.W, pady=4)

        # ---- Row 2: Algoritmo VPMB + parametri inline ----
        if not hasattr(self, "deco_model_var"):
            self.deco_model_var = tk.StringVar(value="VPM")
        ttk.Label(input_frame, text="Algoritmo calcolo:").grid(row=2, column=0, sticky=tk.W, padx=(0,6), pady=(6,0))

        ttk.Radiobutton(input_frame, text="VPMB", variable=self.deco_model_var, value="VPM",
                        command=self._on_deco_model_change).grid(row=2, column=1, sticky=tk.W, pady=(6,2))

        ttk.Label(input_frame, text="Raggio critico N2 [µm]:").grid(row=2, column=2, sticky=tk.W, padx=(8,2), pady=(6,2))
        self.entry_crit_rad_n2 = ttk.Entry(input_frame, width=4)
        self.entry_crit_rad_n2.grid(row=2, column=3, sticky=tk.W, pady=(6,2))
        self.entry_crit_rad_n2.delete(0, tk.END)
        self.entry_crit_rad_n2.insert(0, "1.0")

        ttk.Label(input_frame, text=" Raggio critico He [µm]:").grid(row=2, column=4, sticky=tk.W, padx=(8,2), pady=(6,2))
        self.entry_crit_rad_he = ttk.Entry(input_frame, width=4)
        self.entry_crit_rad_he.grid(row=2, column=5, sticky=tk.W, pady=(6,2))
        self.entry_crit_rad_he.delete(0, tk.END)
        self.entry_crit_rad_he.insert(0, "0.55")

        self.btn_param_vpm = ttk.Button(input_frame, text="Parametri VPM", command=self.open_advanced_params_window)
        self.btn_param_vpm.grid(row=2, column=6, sticky=tk.W, padx=(0,0), pady=(6,2))

        # ---- Row 3: Algoritmo ZH-L16 + GF + warning GF inline (dopo bottone) ----
        if not hasattr(self, "var_gf_low"):
            self.var_gf_low = tk.StringVar(value="30")
        if not hasattr(self, "var_gf_high"):
            self.var_gf_high = tk.StringVar(value="85")
        # Anchor for GF ramp (ZH-L16): where GF low is applied
        # "1STOP" = at first deco stop; "SODZ" = at start-of-deco-zone depth (SoDZ); "BOTTOM" = at bottom depth
        if not hasattr(self, "var_zhl_gf_ramp_anchor"):
            self.var_zhl_gf_ramp_anchor = tk.StringVar(value="SODZ")
        try:
            self.var_zhl_gf_ramp_anchor.trace_add("write", lambda *_: self._request_autosave())
        except Exception:
            pass



        # Anchor for GF high point (ZH-L16): where GF high is applied
        # "SURFACE" = at surface (0 m); "LASTSTOP" = at last stop depth (e.g., 3 m or 6 m)
        if not hasattr(self, "var_zhl_gf_ramp_hi_anchor"):
            self.var_zhl_gf_ramp_hi_anchor = tk.StringVar(value="LASTSTOP")
        try:
            self.var_zhl_gf_ramp_hi_anchor.trace_add("write", lambda *_: self._request_autosave())
        except Exception:
            pass

        ttk.Radiobutton(input_frame, text="ZH-L16", variable=self.deco_model_var, value="ZHL16",
                        command=self._on_deco_model_change).grid(row=3, column=1, sticky=tk.W, pady=(0,6))

        ttk.Label(input_frame, text="Gradient Factor low %:").grid(row=3, column=2, sticky=tk.W, padx=(8,2), pady=(0,6))
        self.entry_gf_low = ttk.Entry(input_frame, textvariable=self.var_gf_low, width=4)
        self.entry_gf_low.grid(row=3, column=3, sticky=tk.W, pady=(0,6))

        ttk.Label(input_frame, text="Gradient Factor high %:").grid(row=3, column=4, sticky=tk.W, padx=(8,2), pady=(0,6))
        self.entry_gf_high = ttk.Entry(input_frame, textvariable=self.var_gf_high, width=4)
        self.entry_gf_high.grid(row=3, column=5, sticky=tk.W, pady=(0,6))

        self.btn_param_zhl16 = ttk.Button(input_frame, text="Parametri ZH-L16", command=self.open_zhl16_params_window)
        self.btn_param_zhl16.grid(row=3, column=6, sticky=tk.W, padx=(0,0), pady=(0,6))

        # Sync button label with ZH-L16 variant
        try:
            variant = getattr(self, "zhl16_variant", "C")
            self.btn_param_zhl16.config(text=f"Parametri ZH-L16{variant}")
        except Exception:
            pass


        self.lbl_gf_warning = ttk.Label(input_frame, text="", foreground="red")
        self.lbl_gf_warning.grid(row=4, column=3, columnspan=5, sticky=tk.W, padx=(0,0), pady=(0,6))

        try:
            self.var_gf_low.trace_add("write", lambda *_: self._validate_gf_fields())
            self.var_gf_high.trace_add("write", lambda *_: self._validate_gf_fields())
        except Exception:
            pass

        # Solubility warning reactive (Rcrit He)
        # Also react to solubility value changes (entry + StringVar)
        try:
            self.entry_rapsol.bind("<KeyRelease>", self._update_solubility_warning)
            self.entry_rapsol.bind("<FocusOut>",  self._update_solubility_warning)
        except Exception:
            pass
        try:
            if hasattr(self, "var_solubility") and self.var_solubility is not None:
                try:
                    self.var_solubility.trace_add("write", lambda *_: self._update_solubility_warning())
                except Exception:
                    self.var_solubility.trace("w", lambda *_: self._update_solubility_warning())
        except Exception:
            pass
        self.entry_crit_rad_he.bind("<KeyRelease>", self._update_solubility_warning)
        self.entry_crit_rad_he.bind("<FocusOut>",  self._update_solubility_warning)

        self._on_deco_model_change()
        self._validate_gf_fields()
        self._update_solubility_warning()
        # ---- Row 4: Riga vuota (riservata a warning) ----
        self.lbl_row4_warning = ttk.Label(input_frame, text="", foreground="red")
        self.lbl_row4_warning.grid(row=4, column=7, sticky=tk.W, pady=(0,0))
        try:
            input_frame.grid_rowconfigure(4, minsize=8)
        except Exception:
            pass



        # ---- Row 5: Bottoni ----
        btn_deco = ttk.Button(input_frame, text="Parametri deco", command=self.open_deco_params_window)
        btn_deco.grid(row=5, column=0, sticky=tk.W, pady=(8,0))

        self.btn_voluntary_deco = ttk.Button(input_frame, text="Deco volontaria (OFF)", command=self.open_voluntary_deco_window)
        self.btn_voluntary_deco.grid(row=5, column=1, sticky=tk.W, padx=(6,0), pady=(8,0))

        if not hasattr(self, "var_stopv_enabled"):
            self.var_stopv_enabled = tk.BooleanVar(value=False)

        def _update_stopv_indicator(*_):
            try:
                enabled = bool(self.var_stopv_enabled.get())
            except Exception:
                enabled = False
            try:
                if enabled:
                    self.btn_voluntary_deco.configure(text="Deco volontaria   (ON)")
                    try:
                        self.btn_voluntary_deco.configure(style="StopvOn.TButton")
                    except Exception:
                        pass
                else:
                    self.btn_voluntary_deco.configure(text="Deco volontaria  (OFF)")
                    try:
                        self.btn_voluntary_deco.configure(style="TButton")
                    except Exception:
                        pass
            except Exception:
                pass

        # keep reference for JSON-load re-sync
        self._update_stopv_indicator = _update_stopv_indicator
        try:
            self.var_stopv_enabled.trace_add('write', _update_stopv_indicator)
        except Exception:
            pass

        chk_stopv = ttk.Checkbutton(
            input_frame,
            text="Abilita",
            variable=self.var_stopv_enabled,
            command=_update_stopv_indicator
        )
        chk_stopv.grid(row=5, column=2, sticky=tk.W, padx=(8,0), pady=(8,0))
        _update_stopv_indicator()

        # "Calcola deco" button: custom Label to keep a real green fill on both Windows and macOS (Aqua ignores Button bg).
        self.btn_calcola = tk.Label(
            input_frame,
            text="Calcola deco",
            bg="#A9D08E",
            fg="black",
            padx=10,
            pady=3,
            bd=1,
            relief=tk.RAISED,
            cursor="hand2",
        )
        def _calcola_enter(_e=None):
            self.btn_calcola.config(bg="#8CCB6A")
        def _calcola_leave(_e=None):
            self.btn_calcola.config(bg="#A9D08E", relief=tk.RAISED)
        def _calcola_press(_e=None):
            self.btn_calcola.config(relief=tk.SUNKEN)
        def _calcola_release(_e=None):
            self.btn_calcola.config(relief=tk.RAISED)
            self.on_calculate()
        self.btn_calcola.bind("<Enter>", _calcola_enter)
        self.btn_calcola.bind("<Leave>", _calcola_leave)
        self.btn_calcola.bind("<ButtonPress-1>", _calcola_press)
        self.btn_calcola.bind("<ButtonRelease-1>", _calcola_release)
        self.btn_calcola.grid(row=5, column=3, sticky=tk.W, padx=(0,0), pady=(8,0))
        ttk.Button(input_frame, text="Esporta Profilo (CSV)", command=self.on_export_csv).grid(row=5, column=4, sticky=tk.W, padx=(12,0), pady=(8,0))
        ttk.Button(input_frame, text="Esporta Grafico (PDF)", command=self.on_export_plot_pdf).grid(row=5, column=5, sticky=tk.W, padx=(0,0), pady=(8,0))
        self.label_error.grid(row=6, column=0, columnspan=8, sticky=tk.W, pady=(6,0))
        # (qui sotto continui con il frame Risultati, Profilo dettagliato, ecc.)

        # ---------------------
        # Frame Gas (fondo + 4 deco)
        # ---------------------
        self.gas_frame = ttk.LabelFrame(main_frame, text="Gas fondo e gas deco", padding=10)
        self.gas_frame.pack(side=tk.TOP, fill=tk.X, pady=10)

        self.create_gas_table(self.gas_frame)
        # ---------------------
        # Frame CCR (Closed Circuit) - Setpoint & Diluente
        # (visibile solo in modalità CC)
        # ---------------------
        self.ccr_frame = ttk.LabelFrame(main_frame, text="CCR (Closed Circuit) - Setpoint & diluente", padding=10)
        self.ccr_frame.pack(side=tk.TOP, fill=tk.X, pady=10)
        self._create_ccr_panel(self.ccr_frame)

        # ---------------------
        # Frame Bailout (5 gas) - (visibile solo in modalità CC)
        # ---------------------
        self.bailout_frame = ttk.LabelFrame(main_frame, text="Bailout (5 gas)", padding=6)
        self.bailout_frame.pack(side=tk.TOP, fill=tk.X, pady=00)
        self._create_bailout_panel(self.bailout_frame)

        # Applica visibilità iniziale (default OC)
        self._apply_mode_visibility()

        # ---------------------
        # Frame Output sintesi
        # ---------------------
        summary_frame = ttk.LabelFrame(main_frame, text="Risultati", padding=10)
        summary_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=10)

        ttk.Label(summary_frame, text="Runtime totale [min]:").grid(row=0, column=0, sticky=tk.W)
        self.label_runtime = ttk.Label(summary_frame, text="--")
        self.label_runtime.grid(row=0, column=1, sticky=tk.W, padx=5)

        ttk.Label(summary_frame, text="Runtime deco [min]:").grid(row=0, column=2, sticky=tk.W)
        self.label_deco = ttk.Label(summary_frame, text="--")
        self.label_deco.grid(row=0, column=3, sticky=tk.W, padx=5)

        ttk.Label(summary_frame, text="Inizio zona deco [m]:").grid(row=0, column=4, sticky=tk.W)
        self.label_decozone = ttk.Label(summary_frame, text="--")
        self.label_decozone.grid(row=0, column=5, sticky=tk.W, padx=5)
        ttk.Label(summary_frame, text="Profondità media finale [m]:").grid(row=0, column=6, sticky=tk.W)
        self.label_final_depthavg = ttk.Label(summary_frame, text="--")
        self.label_final_depthavg.grid(row=0, column=7, sticky=tk.W, padx=5)

        # Seconda riga risultati (derivata dal profilo dettagliato)
        ttk.Label(summary_frame, text="Gas density fondo [g/l]:").grid(row=1, column=0, sticky=tk.W)
        self.label_max_gasdens = ttk.Label(summary_frame, text="--")
        self.label_max_gasdens.grid(row=1, column=1, sticky=tk.W, padx=5)

        ttk.Label(summary_frame, text="CNS% finale:").grid(row=1, column=2, sticky=tk.W)
        self.label_final_cns = ttk.Label(summary_frame, text="--")
        self.label_final_cns.grid(row=1, column=3, sticky=tk.W, padx=5)

        ttk.Label(summary_frame, text="OTU finale:").grid(row=1, column=4, sticky=tk.W)
        self.label_final_otu = ttk.Label(summary_frame, text="--")
        self.label_final_otu.grid(row=1, column=5, sticky=tk.W, padx=5)


        
        # ---------------------
        # Toggle metriche operative (post-processing, nessun impatto sul motore)

# ---------------------
        # Frame tabella profilo
        # ---------------------
        table_frame = ttk.LabelFrame(tab_profile, text="Profilo dettagliato", padding=10)
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)


        # ---------------------
        # Tab Grafico immersione
        # ---------------------
        plot_frame = ttk.Frame(tab_plot, padding=10)
        plot_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(7.5, 4.5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Tempo (min)")
        self.ax.set_ylabel("Profondità (m)")
        self.ax.grid(True)
        # Titolo: algoritmo selezionato (VPM / ZH-L16)
        # (titolo algoritmo spostato nella legenda; niente titolo in alto)
        # Griglie e tick: X major=30 (con label), X minor=5 (senza label)
        #               Y major=5 (con label),  Y minor=1 (senza label)
        from matplotlib.ticker import MultipleLocator
        self.ax.xaxis.set_major_locator(MultipleLocator(30))
        self.ax.xaxis.set_minor_locator(MultipleLocator(5))
        self.ax.yaxis.set_major_locator(MultipleLocator(5))
        self.ax.yaxis.set_minor_locator(MultipleLocator(1))
        self.ax.grid(which='minor', linestyle=':', linewidth=0.6, alpha=0.6)

        # Riduci font tick ~20% (major + minor)
        try:
            self.ax.tick_params(axis='both', which='major', labelsize=8)
            self.ax.tick_params(axis='both', which='minor', labelsize=8)
        except Exception:
            pass

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        # ---------------------
        # Tab Help
        # ---------------------
        help_frame = ttk.Frame(tab_help, padding=12)
        help_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Scrollable, rich text (embedded; no external dependencies)
        help_scroll = ttk.Scrollbar(help_frame, orient="vertical")
        help_widget = tk.Text(help_frame, wrap="word", borderwidth=0, yscrollcommand=help_scroll.set)
        help_scroll.config(command=help_widget.yview)

        help_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        help_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        _render_rich_text_into_widget(help_widget, HELP_DOC_EMBEDDED)
        help_widget.configure(state="disabled")


        # n = numero segmento
        # tipo = DESC / BOTT / ASC / STOP
        # seg_time, run_time, from/to/depth come prima
        # gas = es. "18/45" per FO2=0.18, FHe=0.45
        # ppO2_bar = ppO2 del segmento (dai dati tessuti)
        # costruzione tabella profilo (base; estesa via toggle)
        self.table_frame = table_frame
        self._build_profile_tree(self.cols_ext)

        # Auto-riempimento colonne: reagisce ai resize del contenitore (anche a mezzo schermo verticale)
        self._profile_autofit_after_id = None
        try:
            self.table_frame.bind("<Configure>", lambda e: self._schedule_profile_autofit())
        except Exception:
            pass

        # Per salvare l'ultimo profilo in memoria, utile per export
        self.last_profile_rows = []
        self.last_tissue_rows = []

        # All'avvio: assicurare che ΔppN2 sia valorizzato (formule invariate)
        try:
            self._refresh_delta_ppn2_ui()
        except Exception:
            pass

    
    def _build_profile_tree(self, cols):
        # Ricrea la Treeview del profilo con le colonne richieste (base o estese)
        # (metodo leggero, chiamato solo su toggle o dopo un calcolo)
        if hasattr(self, "tree") and self.tree is not None:
            try:
                self.tree.destroy()
            except Exception:
                pass
        if hasattr(self, "tree_vsb") and self.tree_vsb is not None:
            try:
                self.tree_vsb.destroy()
            except Exception:
                pass

        if hasattr(self, "tree_hsb") and self.tree_hsb is not None:
            try:
                self.tree_hsb.destroy()
            except Exception:
                pass

        self.tree = ttk.Treeview(self.table_frame, columns=cols, show="headings", height=15)

        # larghezze ragionevoli
        width_map = {
            "n": 50, "tipo": 60, "seg_time": 80, "run_time": 90,
            "from": 70, "to": 70, "depth": 70, "note": 140, "mode": 60, "gas": 80, "ppO2": 80,
            "Depth_avg": 90, "Gas_dens_gL": 95, "CNS_%": 70, "OTU": 70, "ΔppN2_iso": 80, "EAD_m": 70, "GF_actual": 70,
        }
        self._profile_width_map = width_map

        for c in cols:
            head = ("resp_algo" if c == "mode" else c)
            self.tree.heading(c, text=head)
            self.tree.column(c, width=width_map.get(c, 80), anchor=tk.CENTER, stretch=False)

        # Layout robusto: Treeview + scrollbar vertical/orizzontale con grid
        # (evita che la larghezza delle colonne venga "stirata" dal pack e garantisce la h-scrollbar)
        try:
            self.table_frame.grid_rowconfigure(0, weight=1)
            self.table_frame.grid_columnconfigure(0, weight=1)
        except Exception:
            pass

        self.tree.grid(row=0, column=0, sticky="nsew")

        # Evidenziazione righe STOP / STOPV nel profilo dettagliato (solo resa grafica)
        try:
            self.tree.tag_configure('STOP', background='#D9ECFF', foreground='black')
            self.tree.tag_configure('STOPV', background='#DFF5E1', foreground='black')
        except Exception:
            pass

        self.tree_vsb = ttk.Scrollbar(self.table_frame, orient="vertical", command=self.tree.yview)
        self.tree_vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=self.tree_vsb.set)

        # Scrollbar orizzontale (necessaria quando lo spazio è troppo stretto per mostrare tutte le colonne)
        self.tree_hsb = ttk.Scrollbar(self.table_frame, orient="horizontal", command=self.tree.xview)
        self.tree_hsb.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.tree.configure(xscrollcommand=self.tree_hsb.set)

        # Auto-riempimento colonne (anche in finestra stretta/mezza schermata)
        self._schedule_profile_autofit()

    def _schedule_profile_autofit(self):
        """Debounce: pianifica l'auto-riempimento colonne del profilo dettagliato.

        Nota: l'auto-fit può generare eventi <Configure> (perché cambia le larghezze colonne).
        Per evitare loop/queue flood, usiamo:
          - debounce con after()
          - guard 'in progress'
        """
        try:
            # Se un autofit è già in corso, non pianificare ulteriormente
            if getattr(self, "_profile_autofit_in_progress", False):
                return

            if getattr(self, "_profile_autofit_after_id", None):
                try:
                    self.after_cancel(self._profile_autofit_after_id)
                except Exception:
                    pass

            # Usa after_idle prima, poi un piccolo delay: più stabile su Windows/Tk
            def _kick():
                try:
                    self._profile_autofit_after_id = self.after(80, self._autofit_profile_columns)
                except Exception:
                    pass

            try:
                self.after_idle(_kick)
            except Exception:
                self._profile_autofit_after_id = self.after(80, self._autofit_profile_columns)
        except Exception:
            pass

    def _autofit_profile_columns(self):
        """Adatta le larghezze colonne della Treeview al contenitore corrente (anche in finestra stretta).

        Anti-loop:
          - set flag in_progress
          - applica le larghezze solo se cambiano in modo significativo
        """
        if getattr(self, "_profile_autofit_in_progress", False):
            return

        self._profile_autofit_in_progress = True
        try:
            if not hasattr(self, "tree") or self.tree is None:
                return

            cols = list(self.tree["columns"])
            if not cols:
                return

            # Misura contenitore reale
            try:
                container_w = int(self.table_frame.winfo_width())
            except Exception:
                container_w = int(self.tree.winfo_width() or 0)

            if container_w <= 1:
                # ancora non mappato; riprova più tardi
                try:
                    self._profile_autofit_after_id = self.after(120, self._autofit_profile_columns)
                except Exception:
                    pass
                return

            # sottrai scrollbar verticale (se presente)
            sb_w = 0
            try:
                if hasattr(self, "tree_vsb") and self.tree_vsb is not None:
                    sb_w = int(self.tree_vsb.winfo_width() or 0)
            except Exception:
                sb_w = 0

            avail = max(50, container_w - max(0, sb_w) - 6)

            width_map = getattr(self, "_profile_width_map", {})
            desired = [int(width_map.get(c, 80)) for c in cols]
            total = sum(desired) if desired else 0
            if total <= 0:
                return


            # Colonne compatte (FINALIZZAZIONE GUI):
            # - larghezza minima = larghezza dell'header (testo colonna) + padding
            # - nessuna distribuzione dello spazio extra a finestra larga
            # - se le colonne eccedono lo spazio, entra in gioco la scrollbar orizzontale
            try:
                heading_font = tkfont.nametofont("TkHeadingFont")
            except Exception:
                try:
                    heading_font = tkfont.nametofont("TkDefaultFont")
                except Exception:
                    heading_font = None

            def _header_px(colname: str) -> int:
                pad = 18
                try:
                    if heading_font is not None:
                        return int(heading_font.measure(colname)) + pad
                except Exception:
                    pass
                return int(len(colname) * 7) + pad  # fallback

            # max(header; values): usa anche la larghezza massima dei valori presenti (fino a un limite righe per performance)
            try:
                cell_font = tkfont.nametofont("TkDefaultFont")
            except Exception:
                cell_font = heading_font

            def _cell_px(val) -> int:
                pad = 18
                s = "" if val is None else str(val)
                try:
                    if cell_font is not None:
                        return int(cell_font.measure(s)) + pad
                except Exception:
                    pass
                return int(len(s) * 7) + pad  # fallback

            # base = header
            new_w = [max(30, _header_px(c)) for c in cols]

            # estende in base ai valori già presenti nella tree
            try:
                max_rows = 250
                iids = list(self.tree.get_children(''))
                for ridx, iid in enumerate(iids):
                    if ridx >= max_rows:
                        break
                    vals = self.tree.item(iid, "values")
                    for j, c in enumerate(cols):
                        try:
                            v = vals[j] if j < len(vals) else ""
                        except Exception:
                            v = ""
                        px = _cell_px(v)
                        if px > new_w[j]:
                            new_w[j] = px
            except Exception:
                pass

            # Applica solo se cambia davvero (evita configure-loop)
            changed = False
            try:
                cur_w = [int(self.tree.column(c, "width")) for c in cols]
            except Exception:
                cur_w = None

            if cur_w is None:
                changed = True
            else:
                for cw, nw in zip(cur_w, new_w):
                    if abs(int(cw) - int(nw)) >= 2:
                        changed = True
                        break

            if not changed:
                return

            for c, w in zip(cols, new_w):
                try:
                    self.tree.column(c, width=int(w), stretch=False)
                except Exception:
                    pass
        finally:
            self._profile_autofit_in_progress = False


    def refresh_profile_table(self):


        _n_disp = 0  # display row counter
        # Rende coerente la tabella con il toggle; non ricalcola nulla (usa self.last_profile_rows)
        cols = self.cols_ext
        if not hasattr(self, "table_frame"):
            return
        self._build_profile_tree(cols)

        # Se non abbiamo dati, stop
        if not getattr(self, "last_profile_rows", None):
            return

        gas_table = getattr(self, "last_gas_table", None)

        for row in self.last_profile_rows:
            _n_disp += 1

            vals = list(self._format_profile_row_for_tree(row, gas_table, show_metrics=True))

            if vals:

                vals[0] = str(_n_disp)
            tipo_u = str(row.get('Tipo', '')).upper()
            if tipo_u == 'STOP':
                self.tree.insert('', tk.END, values=vals, tags=('STOP',))
            elif tipo_u == 'STOPV':
                self.tree.insert('', tk.END, values=vals, tags=('STOPV',))
            else:
                self.tree.insert('', tk.END, values=vals)

    def _format_profile_row_for_tree(self, row, gas_table, show_metrics: bool):
        # Replica la logica già usata nel riempimento tabella, con aggiunta metriche opzionali
        tipo = row.get("Tipo", "")

        seg = row.get("Segmento", "")
        seg_time = row.get("Seg_time_min", "")
        run_time = row.get("Run_time_min", "")

        d_from = row.get("Depth_from_m", "")
        d_to = row.get("Depth_to_m", "")
        depth = row.get("Depth_m", "")

        # Righe tessuti: in tabella mostriamo ppO2 già calcolata dal motore se presente,
        # altrimenti calcolo fisico da profondità media.
        ppO2 = row.get("ppO2_bar", row.get("ppO2", None))
        if ppO2 is None:
            try:
                # profondità media DEL SEGMENTO (non cumulativa) per ppO2 step
                tipo_u = str(tipo).upper()
                if tipo_u in ("ASC", "DESC"):
                    z1 = float(d_from) if d_from not in ("", None) else 0.0
                    z2 = float(d_to) if d_to not in ("", None) else 0.0
                    zmean_seg = 0.5 * (z1 + z2)
                else:
                    zmean_seg = float(depth) if depth not in ("", None) else 0.0

                # gas per ppO2 fisico (stessa logica multigas della tabella)
                if tipo_u in ("DESC","DSTOP","BOTT"):
                    g = gas_table[0] if gas_table else None
                else:
                    if tipo_u in ("ASC", "DESC"):
                        z_for_gas = float(d_from) if d_from not in ("", None) else float(d_to or 0.0)
                    else:
                        z_for_gas = float(depth) if depth not in ("", None) else 0.0
                    g = self.pick_gas_for_depth(z_for_gas, gas_table) if gas_table else None

                fo2 = float(g.get("FO2", 0.0)) if g else 0.0
                ppO2 = fo2 * (zmean_seg / 10.0 + 1.0)
            except Exception:
                ppO2 = None


        ppO2_str = "" if ppO2 in ("", None) else f"{float(ppO2):.2f}"

        # GAS (col 10) — Fase 2: mode-driven + regole discesa OC + selezione per MOD su quota FROM
        mode_row = str(row.get("Mode", "")).upper()
        phase_row = str(row.get("Phase", "DESCENT")).upper()

        def _pf(val, default=0.0):
            try:
                if val is None:
                    return float(default)
                if hasattr(val, "get"):
                    val = val.get()
                s = str(val).strip().replace(",", ".")
                if s == "":
                    return float(default)
                return float(s)
            except Exception:
                return float(default)

        def _is_on(v, default=True):
            try:
                if v is None:
                    return bool(default)
                if hasattr(v, "get"):
                    v = v.get()
                if isinstance(v, bool):
                    return v
                if isinstance(v, (int, float)):
                    return bool(v)
                s = str(v).strip().lower()
                if s in ("1", "true", "t", "yes", "y", "on", "enabled", "enable"):
                    return True
                if s in ("0", "false", "f", "no", "n", "off", "disabled", "disable"):
                    return False
                # fallback: python truthiness on non-empty strings is misleading; assume default
                return bool(default)
            except Exception:
                return bool(default)

        def _fmt_triplet(prefix: str, fo2: float, fhe: float) -> str:
            try:
                fn2 = max(0.0, 1.0 - float(fo2) - float(fhe))
            except Exception:
                fn2 = 0.0
            return f"{prefix}_{int(round(float(fo2)*100)):02d}/{int(round(float(fhe)*100)):02d}/{int(round(fn2*100)):02d}"

        def _pick_best(depth_switch_m: float, table: list[dict] | None):
            best_i = None
            best_g = None
            best_mod = None
            for i, g in enumerate(table or []):
                try:
                    enabled = _is_on(g.get("enabled", g.get("Enabled", True)), True)
                    if not enabled:
                        continue
                    mod = float(g.get("MOD", 0.0))
                    # usable if depth <= MOD
                    if float(depth_switch_m) <= mod + 1e-6:
                        if best_mod is None or mod < best_mod:
                            best_mod = mod
                            best_i = i
                            best_g = g
                except Exception:
                    continue
            return best_i, best_g

        gas_label = ""
        gas_dict = None

        try:
            # depth_switch: ASC usa FROM (switch dal segmento successivo); STOP usa depth
            if tipo == "ASC":
                depth_switch = float(d_from) if d_from not in ("", None) else float(d_to or 0.0)
            else:
                depth_switch = float(depth) if depth not in ("", None) else 0.0

            if mode_row == "CC":
                # CC: gas sempre DILUENTE
                fo2 = _pf(getattr(self, "cc_dil_fo2", tk.StringVar(value="0.0")), 0.0)
                fhe = _pf(getattr(self, "cc_dil_fhe", tk.StringVar(value="0.0")), 0.0)
                gas_label = _fmt_triplet("DIL", fo2, fhe)

            elif mode_row == "OC":
                if phase_row == "DESCENT":
                    g0 = gas_table[0] if gas_table else None
                    gas_dict = g0
                    fo2 = float(g0.get("FO2", 0.0)) if g0 else 0.0
                    fhe = float(g0.get("FHe", 0.0)) if g0 else 0.0
                    gas_label = _fmt_triplet("BOTT", fo2, fhe)
                else:
                    gi, gsel = _pick_best(depth_switch, gas_table)
                    if gsel is None:
                        gi, gsel = (0, gas_table[0]) if gas_table else (None, None)
                    gas_dict = gsel
                    fo2 = float(gsel.get("FO2", 0.0)) if gsel else 0.0
                    fhe = float(gsel.get("FHe", 0.0)) if gsel else 0.0
                    prefix = "BOTT" if (gi in (0, None)) else f"DEC{gi}"
                    gas_label = _fmt_triplet(prefix, fo2, fhe)

            elif mode_row == "BO":
                # BO: usa SEMPRE il Mix calcolato dal MAIN (row['Mix']) per stampare il gas.
                # Questo evita gas vuoto e consente diagnosi: se Mode=BO ma Mix==1, allora e' un bug del MAIN.
                bo_table_all = []
                for rr in getattr(self, "bailout_rows", []) or []:
                    try:
                        enabled_var = rr.get("enabled_var") or rr.get("var_enabled")
                        enabled = bool(enabled_var.get()) if enabled_var is not None else False
                        fo2 = _pf(rr.get("fo2").get() if rr.get("fo2") else "0.0", 0.0)
                        fhe = _pf(rr.get("fhe").get() if rr.get("fhe") else "0.0", 0.0)
                        mod = _pf(rr.get("mod").get() if rr.get("mod") else "0.0", 0.0)
                        bo_table_all.append({"FO2": fo2, "FHe": fhe, "MOD": mod, "enabled": enabled})
                    except Exception:
                        bo_table_all.append({"FO2": 0.0, "FHe": 0.0, "MOD": 0.0, "enabled": False})

                mix_idx = None
                try:
                    mix_val = row.get("Mix", None)
                    mix_idx = int(mix_val) if mix_val is not None else None
                except Exception:
                    mix_idx = None

                if mix_idx is not None and mix_idx >= 2:
                    # Mix 2 -> BO_1, Mix 3 -> BO_2, ...
                    bi = mix_idx - 2
                    if 0 <= bi < len(bo_table_all):
                        gsel = bo_table_all[bi]
                        gas_dict = gsel
                        fo2 = float(gsel.get("FO2", 0.0))
                        fhe = float(gsel.get("FHe", 0.0))
                        prefix = f"BO_{bi + 1}"
                        gas_label = _fmt_triplet(prefix, fo2, fhe)
                    else:
                        gas_label = f"BO_{bi + 1}"
                elif mix_idx == 1:
                    # Mode=BO ma Mix=1 => il MAIN sta usando il DILUENTE: mostralo (diagnostico).
                    fo2 = _pf(getattr(self, "cc_dil_fo2", tk.StringVar(value="0.0")), 0.0)
                    fhe = _pf(getattr(self, "cc_dil_fhe", tk.StringVar(value="0.0")), 0.0)
                    gas_label = _fmt_triplet("DIL", fo2, fhe)
                else:
                    # Fallback: se manca Mix, mantieni la vecchia logica (best usable) ma NON lasciare vuoto
                    gi, gsel = _pick_best(depth_switch, bo_table_all)
                    gas_dict = gsel
                    if gsel is not None and gi is not None:
                        fo2 = float(gsel.get("FO2", 0.0))
                        fhe = float(gsel.get("FHe", 0.0))
                        prefix = f"BO_{gi + 1}"
                        gas_label = _fmt_triplet(prefix, fo2, fhe)
                    else:
                        gas_label = "BO"

            else:
                gas_label = ""

        except Exception:
            gas_label = ""
        vals = [
            seg,
            tipo,
            "" if seg_time in ("", None) else f"{float(seg_time):.1f}",
            "" if run_time in ("", None) else f"{float(run_time):.1f}",
            "" if d_from in ("", None) else f"{float(d_from):.1f}",
            "" if d_to in ("", None) else f"{float(d_to):.1f}",
            "" if depth in ("", None) else f"{float(depth):.1f}",
            ("" if row.get("Note", "") is None else str(row.get("Note", ""))),
            (
                "" if row.get("Mode", "") is None else self._mode_with_algo(str(row.get("Mode", "")))
            ),
            gas_label,
            ppO2_str,
        ]

        if show_metrics:
            def fnum(x, fmt):
                try:
                    if x is None or x == "":
                        return ""
                    return format(float(x), fmt)
                except Exception:
                    return ""

            vals += [
                fnum(row.get("Depth_avg", ""), ".1f"),
                fnum(row.get("Gas_dens_gL", ""), ".2f"),
                fnum(row.get("CNS_%", ""), ".0f"),
                fnum(row.get("OTU", ""), ".0f"),
                ("" if row.get("EAD_m", None) is None else fnum(row.get("EAD_m"), ".1f")),
                fnum(row.get("GF_actual", ""), ".1f"),
            ]


        return tuple(vals)

    def _mode_with_algo(self, mode_text: str) -> str:
        """
        Estende la colonna 'Mode' (testo informativo) includendo anche l'algoritmo deco selezionato:
        OC_VPM / CC_VPM / BO_VPM / OC_ZHL16 / CC_ZHL16 / BO_ZHL16, ecc.
        """
        try:
            m = (mode_text or "").strip()
            if not m:
                return ""
            # già marcato?
            up = m.upper()
            if up.endswith("_VPM") or up.endswith("_ZHL16") or up.endswith("_ZHL-16"):
                return m
            algo = "VPM"
            try:
                if getattr(self, "_last_calc_algo", None):
                    algo = str(self._last_calc_algo).strip().upper()
                elif hasattr(self, "deco_model_var"):
                    algo = str(self.deco_model_var.get() or "VPM").strip().upper()
            except Exception:
                algo = "VPM"
            if algo not in ("VPM", "ZHL16"):
                algo = "VPM"
            return f"{m}_{algo}"
        except Exception:
            return mode_text


        # Riallinea stati UI (enable/disable) e warning GF
        try:
            if hasattr(self, "_on_deco_model_change"):
                self._on_deco_model_change()
        except Exception:
            pass
        try:
            if hasattr(self, "_validate_gf_fields"):
                self._validate_gf_fields()
        except Exception:
            pass

    # ==========================================================

    # CC UI (solo input) - STEP 3A
    # ==========================================================
    def _on_mode_change(self):
        """Callback radiobutton OC/CC: cambia solo la visibilità dei pannelli.

        Nota: NON cambia la logica di calcolo ΔppN2; garantisce solo l'aggiornamento in UI.
        """
        self._apply_mode_visibility()
        self._refresh_delta_ppn2_ui()


    def _on_deco_model_change(self):
        """Abilita/disabilita i campi in funzione dell'algoritmo selezionato.
        Vincolo: nessuna modifica alla logica VPM; ZH-L16 per ora è solo UI/parametri.
        """
        try:
            is_vpm = (self.deco_model_var.get() == "VPM")
        except Exception:
            is_vpm = True

        # Campi VPM (raggio critico)
        try:
            self.entry_crit_rad_n2.configure(state=("normal" if is_vpm else "disabled"))
        except Exception:
            pass
        try:
            self.entry_crit_rad_he.configure(state=("normal" if is_vpm else "disabled"))
        except Exception:
            pass

        # Campi ZH-L16 (GF)
        try:
            self.entry_gf_low.configure(state=("disabled" if is_vpm else "normal"))
        except Exception:
            pass
        try:
            self.entry_gf_high.configure(state=("disabled" if is_vpm else "normal"))
        except Exception:
            pass

        # Pulsante Parametri VPM
        try:
            self.btn_param_vpm.configure(state=("normal" if is_vpm else "disabled"))
        except Exception:
            pass

        
        # Pulsante Parametri ZH-L16
        try:
            self.btn_param_zhl16.configure(state=("disabled" if is_vpm else "normal"))
        except Exception:
            pass

        # Esporta in env var (MAIN attuale le ignorerà: zero rischio regressioni)
        try:
            os.environ["VPM_DECO_MODEL"] = ("VPM" if is_vpm else "ZHL16")
            if not is_vpm:
                os.environ["VPM_GF_LOW"] = str(self.var_gf_low.get()).strip()
                os.environ["VPM_GF_HIGH"] = str(self.var_gf_high.get()).strip()
                os.environ["VPM_ZHL_GF_RAMP_ANCHOR"] = str(getattr(self, "var_zhl_gf_ramp_anchor", None).get() if hasattr(self, "var_zhl_gf_ramp_anchor") else "SODZ").strip()
                os.environ["VPM_ZHL_GF_RAMP_HI_ANCHOR"] = str(getattr(self, "var_zhl_gf_ramp_hi_anchor", None).get() if hasattr(self, "var_zhl_gf_ramp_hi_anchor") else "SURFACE").strip()
                os.environ["VPM_LAST_STOP_M"] = str(getattr(self, 'deco_last_stop_m', 3.0))
            # ZH-L16 variant + coeff tables (B/C) passed to MAIN via env (JSON).
            try:
                if not is_vpm:
                    os.environ["VPM_ZHL16_VARIANT"] = str(getattr(self, 'zhl16_variant', 'C') or 'C')
                    _vv = str(getattr(self, 'zhl16_variant', 'C') or 'C').strip().upper()
                    _rows = getattr(self, 'zhl16_coeffs_C', None) if _vv != 'B' else getattr(self, 'zhl16_coeffs_B', None)
                    if isinstance(_rows, list) and len(_rows) == 16:
                        os.environ["VPM_ZHL16_COEFFS_JSON"] = json.dumps(_rows)
            except Exception:
                pass
        except Exception:
            pass
        # Refresh solubility hint visibility after switching algorithm
        try:
            self._update_solubility_warning()
        except Exception:
            pass


    def _validate_gf_fields(self):
        """Validazione GF (solo warning rosso): 
        a) GF low: intero 5..110
        b) GF high: intero 50..110
        c) GF high >= GF low
        """
        try:
            is_vpm = (self.deco_model_var.get() == "VPM")
        except Exception:
            is_vpm = True

        # Se siamo in VPM, i campi GF sono inattivi: nessun warning
        if is_vpm:
            try:
                if hasattr(self, "lbl_gf_warning"):
                    self.lbl_gf_warning.config(text="")
            except Exception:
                pass
            return

        low_s = str(self.var_gf_low.get()).strip() if hasattr(self, "var_gf_low") else ""
        high_s = str(self.var_gf_high.get()).strip() if hasattr(self, "var_gf_high") else ""

        errors = []

        def _parse_int(label, s):
            if not re.fullmatch(r"\d+", s or ""):
                errors.append(f"{label} deve essere un intero (decimali 0)")
                return None
            try:
                return int(s)
            except Exception:
                errors.append(f"{label} non valido")
                return None

        low = _parse_int("GF low", low_s)
        high = _parse_int("GF high", high_s)

        if low is not None:
            if low < 5 or low > 110:
                errors.append("GF low fuori range (5..110)")
        if high is not None:
            if high < 50 or high > 110:
                errors.append("GF high fuori range (50..110)")
        if low is not None and high is not None:
            if high < low:
                errors.append("GF high deve essere ≥ GF low")

        msg = " | ".join(errors)
        try:
            if hasattr(self, "lbl_gf_warning"):
                self.lbl_gf_warning.config(text=msg)
        except Exception:
            pass

    def _apply_mode_visibility(self):
        """Mostra solo i pannelli coerenti con la modalità selezionata."""
        mode = self.mode_var.get() if hasattr(self, "mode_var") else "OC"
        if mode == "OC":
            # OC: mostra gas_frame OC, nascondi CC
            if hasattr(self, "gas_frame"):
                self.gas_frame.pack(side=tk.TOP, fill=tk.X, pady=10)
            if hasattr(self, "ccr_frame"):
                self.ccr_frame.pack_forget()
            if hasattr(self, "bailout_frame"):
                self.bailout_frame.pack_forget()
        else:
            # CC: nascondi OC, mostra CC
            if hasattr(self, "gas_frame"):
                self.gas_frame.pack_forget()
            if hasattr(self, "ccr_frame"):
                self.ccr_frame.pack(side=tk.TOP, fill=tk.X, pady=10)
            if hasattr(self, "bailout_frame"):
                self.bailout_frame.pack(side=tk.TOP, fill=tk.X, pady=10)

        # aggiorna visibilità checkbox "Calcolo della risalita in Bailout"
        self._update_bailout_calc_checkbox_visibility()



    def _update_bailout_calc_checkbox_visibility(self):
        """Aggiorna visibilità checkbox 'Calcolo risalita in Bailout'.

        Nella baseline attuale la checkbox è già gestita tramite pack/pack_forget dei frame.
        Questa funzione è volutamente un NO-OP per evitare errori in chiamate legacy.
        """
        return


    def _refresh_delta_ppn2_ui(self):
        """Aggiorna *solo* la visualizzazione/trigger del ΔppN2 (senza modificare formule).

        - In OC: ricalcola ΔppN2 nella tabella gas (update_gas_calculated_fields).
        - In CC: ricalcola ΔppN2 nella tabella Bailout (refresh_bailout_table esposto).

        Questo risolve i casi in cui il calcolo era corretto ma non veniva invocato
        (apertura GUI, toggle, calcola deco, bailout ascent).
        """
        mode = self.mode_var.get() if hasattr(self, "mode_var") else "OC"
        try:
            if mode == "OC":
                self.update_gas_calculated_fields()
            else:
                if hasattr(self, "_refresh_bailout_table") and callable(getattr(self, "_refresh_bailout_table")):
                    self._refresh_bailout_table()
        except Exception:
            # Non bloccare la GUI per un errore di refresh UI
            pass

    def _on_bailout_ascent_toggle(self):
        """Handler checkbox 'Calcolo della risalita in Bailout'.

        Non cambia formule ΔppN2: forza solo l'aggiornamento immediato.
        """
        self._refresh_delta_ppn2_ui()

    def _create_ccr_panel(self, parent):
        """Crea pannello CCR: setpoint multipli e diluente (FN2 calcolato)."""

        # Campo setpoint singolo (MONO-SP): fonte unica del setpoint "di base" in CCR.
        # Nota: quando Multi-SP a bande è DISATTIVO, lo SP "Fondo" nella tabella è un mirror di questo valore
        # ed è sola lettura (coerenza mono-SP).
        self.cc_sp_single = tk.StringVar(value="0.90")

        def _sync_cc_sp_bottom_mirror(*_):
            """Sync campo 'SP Fondo' (banda fondo) con logica FAIL-SAFE:

            - SP Fondo è SEMPRE = SP singolo (mirror), indipendentemente da MSP ON/OFF.
            - Il campo NON è mai inputabile (sola lettura), per evitare incoerenze.
            """
            try:
                self.cc_sp_bottom.set(self.cc_sp_single.get())
            except Exception:
                pass
            try:
                self.entry_cc_sp_bottom.configure(state="readonly")
            except Exception:
                pass

        # Esponi la funzione di sync come metodo (utile anche dopo load_inputs)
        self._cc_sync_sp_bottom_mirror = _sync_cc_sp_bottom_mirror

        def _on_cc_msp_toggle():
            """Toggle MSP: sync UI + re-validate to clear/show warnings immediately."""
            try:
                _sync_cc_sp_bottom_mirror()
            except Exception:
                pass
            try:
                self._validate_ccr_sp_logic()
            except Exception:
                pass

        self._on_cc_msp_toggle = _on_cc_msp_toggle

        # SP singolo (sempre disponibile)
        sp_single_frame = ttk.Frame(parent)
        sp_single_frame.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(0, 6))
        ttk.Label(sp_single_frame, text="SP singolo [atm]:").pack(side=tk.LEFT)
        self.entry_cc_sp_single = ttk.Entry(sp_single_frame, width=8, textvariable=self.cc_sp_single)
        self.entry_cc_sp_single.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            sp_single_frame,
            text=""
        ).pack(side=tk.LEFT, padx=(10, 0))

        # Toggle multi-setpoint a bande (stabile: default OFF, mantiene mono-SP baseline)
        self.cc_msp_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            parent,
            text="Abilita CCR multi-setpoint a bande",
            variable=self.cc_msp_enabled,
            command=self._on_cc_msp_toggle
        ).pack(side=tk.TOP, anchor=tk.W, padx=4, pady=(0, 6))

        # Quando cambia MSP (anche via load/persistenza), riesegui validazione per aggiornare/azzerare warning
        try:
            self.cc_msp_enabled.trace_add("write", lambda *_: self._validate_ccr_sp_logic())
        except Exception:
            try:
                self.cc_msp_enabled.trace("w", lambda *_: self._validate_ccr_sp_logic())
            except Exception:
                pass

        # Aggiorna mirror fondo quando cambia SP singolo
        try:
            self.cc_sp_single.trace_add("write", _sync_cc_sp_bottom_mirror)
        except Exception:
            pass

        sp_frame = ttk.LabelFrame(parent, text="Setpoint per fase", padding=8)
        sp_frame.pack(side=tk.TOP, fill=tk.X)

        # Inner table frame: keep columns compact on the left (do not expand with the container)
        sp_table = ttk.Frame(sp_frame)
        sp_table.pack(side=tk.TOP, anchor=tk.W)

        headers = ["Fase", "SP [atm]", "da [m]", "a [m]", ""]
        for c, h in enumerate(headers):
            ttk.Label(sp_table, text=h).grid(row=0, column=c, sticky=tk.W, padx=(4 if c==0 else 1))

        self.cc_sp_descent = tk.StringVar(value="0.70")
        self.cc_sp_bottom  = tk.StringVar(value="")
        self.cc_sp_deco1   = tk.StringVar(value="")
        self.cc_sp_deco2   = tk.StringVar(value="")
        self.cc_sp_deco3   = tk.StringVar(value="")

        # Soglie "A quota" per le fasi deco (input)
        # Deco 1: da Bottom a Deco1_a
        # Deco 2: da Deco1_a a Deco2_a
        # Deco 3: da Deco2_a a 0
        self.cc_deco1_a = tk.StringVar(value="36")
        self.cc_deco2_a = tk.StringVar(value="21")

        def ro_label(r, c, val):
            lbl = ttk.Label(sp_table, text=val)
            lbl.grid(row=r, column=c, sticky=tk.W, padx=(4 if c==0 else 1), pady=1)
            return lbl

        # Discesa
        ro_label(1, 0, "Discesa")
        self.entry_cc_sp_descent = ttk.Entry(sp_table, width=8, textvariable=self.cc_sp_descent)
        self.entry_cc_sp_descent.grid(row=1, column=1, sticky=tk.W, padx=1, pady=1)
        ro_label(1, 2, "0")
        ro_label(1, 3, "Fondo")

        # Fondo
        ro_label(2, 0, "Fondo")
        self.entry_cc_sp_bottom = ttk.Entry(sp_table, width=8, textvariable=self.cc_sp_bottom)
        self.entry_cc_sp_bottom.grid(row=2, column=1, sticky=tk.W, padx=1, pady=1)
        ttk.Label(sp_table, text="SP fondo derivato automaticamente da SP singolo").grid(row=2, column=4, sticky=tk.W, padx=(10,0), pady=1)
        # Applica subito lo stato read-only/normal in base a Multi-SP e sincronizza SP Fondo
        _sync_cc_sp_bottom_mirror()

        ro_label(2, 2, "Fondo")
        ro_label(2, 3, "Fondo")

        # Deco1
        ro_label(3, 0, "Deco 1")
        self.entry_cc_sp_deco1 = ttk.Entry(sp_table, width=8, textvariable=self.cc_sp_deco1)
        self.entry_cc_sp_deco1.grid(row=3, column=1, sticky=tk.W, padx=1, pady=1)
        ro_label(3, 2, "Fondo")
        self.entry_cc_deco1_a = ttk.Entry(sp_table, width=8, textvariable=self.cc_deco1_a)
        self.entry_cc_deco1_a.grid(row=3, column=3, sticky=tk.W, padx=4, pady=1)

        # Deco2
        ro_label(4, 0, "Deco 2")
        self.entry_cc_sp_deco2 = ttk.Entry(sp_table, width=8, textvariable=self.cc_sp_deco2)
        self.entry_cc_sp_deco2.grid(row=4, column=1, sticky=tk.W, padx=1, pady=1)
        self.lbl_cc_from_deco2 = ro_label(4, 2, self.cc_deco1_a.get())
        self.entry_cc_deco2_a = ttk.Entry(sp_table, width=8, textvariable=self.cc_deco2_a)
        self.entry_cc_deco2_a.grid(row=4, column=3, sticky=tk.W, padx=4, pady=1)

        # Deco3
        ro_label(5, 0, "Deco 3")
        self.entry_cc_sp_deco3 = ttk.Entry(sp_table, width=8, textvariable=self.cc_sp_deco3)
        self.entry_cc_sp_deco3.grid(row=5, column=1, sticky=tk.W, padx=1, pady=1)
        self.lbl_cc_from_deco3 = ro_label(5, 2, self.cc_deco2_a.get())
        ro_label(5, 3, "0")

        # Messaggi logici CCR (SP vs diluente / quote) - fuori dalla griglia della tabella (non deve allargare le colonne)
        sp_msgs = ttk.Frame(sp_frame)
        sp_msgs.pack(side=tk.TOP, anchor=tk.W, fill=tk.X, padx=4, pady=(6, 0))

        self.lbl_cc_sp_error = ttk.Label(sp_msgs, text="", foreground="red")
        self.lbl_cc_sp_error.pack(side=tk.TOP, anchor=tk.W)

        self.lbl_cc_sp_info = ttk.Label(sp_msgs, text="", foreground="black", wraplength=900, justify="left")
        # (compact UI) info label not packed to avoid extra vertical space


        def sync_cc_sp_thresholds(*_):
            # aggiorna i "Da quota" read-only derivati
            try:
                self.lbl_cc_from_deco2.config(text=self.cc_deco1_a.get())
                self.lbl_cc_from_deco3.config(text=self.cc_deco2_a.get())
            except Exception:
                pass

        self.cc_deco1_a.trace_add("write", sync_cc_sp_thresholds)
        self.cc_deco2_a.trace_add("write", sync_cc_sp_thresholds)

# Diluente
        dil_frame = ttk.LabelFrame(parent, text="Diluente", padding=8)
        dil_frame.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))

        # Colonne come tabella Bailout: MOD + ppO2/ppHe/ppN2 (ΔppN2/VRM/Tank NON previste per diluente)
        ttk.Label(dil_frame, text="FO2").grid(row=0, column=0, sticky=tk.W, padx=4)
        ttk.Label(dil_frame, text="FHe").grid(row=0, column=1, sticky=tk.W, padx=1)
        ttk.Label(dil_frame, text="FN2").grid(row=0, column=2, sticky=tk.W, padx=4)
        ttk.Label(dil_frame, text="MOD").grid(row=0, column=3, sticky=tk.W, padx=4)
        ttk.Label(dil_frame, text="ppO2").grid(row=0, column=4, sticky=tk.W, padx=4)
        ttk.Label(dil_frame, text="ppHe").grid(row=0, column=5, sticky=tk.W, padx=4)
        ttk.Label(dil_frame, text="ppN2").grid(row=0, column=6, sticky=tk.W, padx=4)
        ttk.Label(dil_frame, text="dens g/L").grid(row=0, column=7, sticky=tk.W, padx=4)

        self.cc_dil_fo2 = tk.StringVar(value="0.10")
        self.cc_dil_fhe = tk.StringVar(value="0.80")
        self.cc_dil_fn2 = tk.StringVar(value="0.10")

        # MOD di riferimento per pp (input)
        self.cc_dil_mod = tk.StringVar(value="30")

        # pp (read-only)
        self.cc_dil_ppO2 = tk.StringVar(value="--")
        self.cc_dil_ppHe = tk.StringVar(value="--")
        self.cc_dil_ppN2 = tk.StringVar(value="--")

        # densità gas (g/L) @ MOD
        self.cc_dil_dens = tk.StringVar(value="--")

        # FO2 / FHe input
        self.entry_cc_dil_fo2 = ttk.Entry(dil_frame, width=8, textvariable=self.cc_dil_fo2)
        self.entry_cc_dil_fo2.grid(row=1, column=0, sticky=tk.W, padx=4, pady=1)

        self.entry_cc_dil_fhe = ttk.Entry(dil_frame, width=8, textvariable=self.cc_dil_fhe)
        self.entry_cc_dil_fhe.grid(row=1, column=1, sticky=tk.W, padx=1, pady=1)

        # FN2 calculated (read-only)
        self.entry_cc_dil_fn2 = ttk.Entry(dil_frame, width=10, textvariable=self.cc_dil_fn2, state="readonly")
        self.entry_cc_dil_fn2.grid(row=1, column=2, sticky=tk.W, padx=4, pady=1)

        # MOD input
        self.entry_cc_dil_mod = ttk.Entry(dil_frame, width=8, textvariable=self.cc_dil_mod)
        self.entry_cc_dil_mod.grid(row=1, column=3, sticky=tk.W, padx=4, pady=1)

        # pp entries (read-only)
        self.entry_cc_dil_ppO2 = ttk.Entry(dil_frame, width=10, textvariable=self.cc_dil_ppO2, state="readonly")
        self.entry_cc_dil_ppO2.grid(row=1, column=4, sticky=tk.W, padx=4, pady=1)

        self.entry_cc_dil_ppHe = ttk.Entry(dil_frame, width=10, textvariable=self.cc_dil_ppHe, state="readonly")
        self.entry_cc_dil_ppHe.grid(row=1, column=5, sticky=tk.W, padx=4, pady=1)

        self.entry_cc_dil_ppN2 = ttk.Entry(dil_frame, width=10, textvariable=self.cc_dil_ppN2, state="readonly")
        self.entry_cc_dil_ppN2.grid(row=1, column=6, sticky=tk.W, padx=4, pady=1)


        self.entry_cc_dil_dens = ttk.Entry(dil_frame, width=10, textvariable=self.cc_dil_dens, state="readonly")
        self.entry_cc_dil_dens.grid(row=1, column=7, sticky=tk.W, padx=4, pady=1)

        self.lbl_cc_dil_error = ttk.Label(dil_frame, text="", foreground="red")
        self.lbl_cc_dil_error.grid(row=2, column=0, columnspan=8, sticky=tk.W, padx=4, pady=(2, 0))

        def _parse_float(s: str, default: float = 0.0):
            try:
                return float((s or "").replace(",", "."))
            except Exception:
                return default

        def recalc_fn2_and_pp(*_):
            """FN2 = 1 - FO2 - FHe. ppX = Pamb(MOD) * FX con Pamb = MOD/10 + 1."""
            fo2 = _parse_float(self.cc_dil_fo2.get(), 0.0)
            fhe = _parse_float(self.cc_dil_fhe.get(), 0.0)
            mod_m = _parse_float(self.cc_dil_mod.get(), 0.0)

            fn2 = 1.0 - fo2 - fhe
            is_err = (fn2 < 0.0)

            if is_err:
                # Do not "accept" negative FN2: show 0.00 but flag the error clearly
                fn2 = 0.0
                self.cc_dil_fn2.set("0.00")
                try:
                    self.entry_cc_dil_fn2.configure(style="Error.TEntry")
                except Exception:
                    pass
                try:
                    self.lbl_cc_dil_error.config(text="Errore: FO2 + FHe > 1.00 (FN2 negativo)")
                except Exception:
                    pass
            else:
                self.cc_dil_fn2.set(f"{fn2:.2f}")
                try:
                    self.entry_cc_dil_fn2.configure(style="TEntry")
                except Exception:
                    pass
                try:
                    self.lbl_cc_dil_error.config(text="")
                except Exception:
                    pass

            # pp calcolate a Pamb(MOD) (solo GUI)
            pamb = (mod_m / 10.0) + 1.0
            ppO2 = pamb * fo2
            ppHe = pamb * fhe
            ppN2 = pamb * fn2

            self.cc_dil_ppO2.set(f"{ppO2:.2f}")
            self.cc_dil_ppHe.set(f"{ppHe:.2f}")
            self.cc_dil_ppN2.set(f"{ppN2:.2f}")

            dens = gas_density_gL(mod_m, fo2, fhe)
            self.cc_dil_dens.set(f"{dens:.2f}")

        self.cc_dil_fo2.trace_add("write", recalc_fn2_and_pp)
        self.cc_dil_fhe.trace_add("write", recalc_fn2_and_pp)
        self.cc_dil_mod.trace_add("write", recalc_fn2_and_pp)

        recalc_fn2_and_pp()

    def _create_bailout_panel(self, parent):
        """Crea pannello Bailout: tabella IDENTICA a quella OC (solo input GUI) + checkbox uso bailout."""
        # Usiamo un sub-frame dedicato per la tabella (grid dentro), così evitiamo mix pack/grid nello stesso container
        tbl = ttk.Frame(parent)
        tbl.pack(side=tk.TOP, fill=tk.X)

        headers = ["", "ON", "FO2", "FHe", "FN2", "MOD", "ppO2", "ppHe", "ppN2", "ΔppN2", "VRM", "tank", "bar INI", "consumo", "bar END"]
        for col, h in enumerate(headers):
            ttk.Label(tbl, text=h).grid(row=0, column=col, padx=3, pady=1)

        # Defaults: replico i 5 gas OC (come base), ma per Bailout sono tutti OFF di default
        gas_specs = [
            ("bailout gas 1", 0.10, 0.70, 120, 15, 40.0),
            ("bailout gas 2", 0.18, 0.62, 65,  15, 11.0),
            ("bailout gas 3", 0.35, 0.30, 36,  15, 11.0),
            ("bailout gas 4", 0.50, 0.10, 21,  15, 11.0),
            ("bailout gas 5", 1.00, 0.00, 6,   15, 5.6),
        ]

        self.bailout_rows = []

        for r, (name, fo2, fhe, mod, vrm, tank) in enumerate(gas_specs, start=1):
            row = {}
            row["name"] = name

            ttk.Label(tbl, text=name).grid(row=r, column=0, padx=3, pady=1, sticky=tk.W)

            # ON/OFF: per bailout tutti selezionabili (default OFF)
            enabled_var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(tbl, variable=enabled_var)
            cb.grid(row=r, column=1, padx=3, pady=1)
            row["var_enabled"] = enabled_var
            row["enabled_var"] = enabled_var
            row["cb"] = cb

            # FO2
            e_fo2 = ttk.Entry(tbl, width=6)
            e_fo2.grid(row=r, column=2, padx=3, pady=1)
            e_fo2.insert(0, f"{fo2:.2f}")
            row["fo2"] = e_fo2

            # FHe
            e_fhe = ttk.Entry(tbl, width=6)
            e_fhe.grid(row=r, column=3, padx=3, pady=1)
            e_fhe.insert(0, f"{fhe:.2f}")
            row["fhe"] = e_fhe

            # FN2 (readonly)
            e_fn2 = ttk.Entry(tbl, width=6, state="readonly")
            e_fn2.grid(row=r, column=4, padx=3, pady=1)
            row["fn2"] = e_fn2

            # MOD
            e_mod = ttk.Entry(tbl, width=6)
            e_mod.grid(row=r, column=5, padx=3, pady=1)
            e_mod.insert(0, f"{mod:d}")
            row["mod"] = e_mod

            # ppO2 (readonly)
            e_ppO2 = ttk.Entry(tbl, width=6, state="readonly")
            e_ppO2.grid(row=r, column=6, padx=3, pady=1)
            row["ppO2_bar"] = e_ppO2

            # ppHe (readonly)
            e_ppHe = ttk.Entry(tbl, width=6, state="readonly")
            e_ppHe.grid(row=r, column=7, padx=3, pady=1)
            row["ppHe"] = e_ppHe

            # ppN2 (readonly)
            e_ppN2 = ttk.Entry(tbl, width=6, state="readonly")
            e_ppN2.grid(row=r, column=8, padx=3, pady=1)
            row["ppN2"] = e_ppN2

            # ΔppN2 (readonly)
            e_dppN2 = ttk.Entry(tbl, width=6, state="readonly")
            e_dppN2.grid(row=r, column=9, padx=3, pady=1)
            row["dppN2"] = e_dppN2

            # VRM
            e_vrm = ttk.Entry(tbl, width=6)
            e_vrm.grid(row=r, column=10, padx=3, pady=1)
            e_vrm.insert(0, f"{float(vrm):.0f}")
            row["vrm"] = e_vrm

            # tank
            e_tank = ttk.Entry(tbl, width=6)
            e_tank.grid(row=r, column=11, padx=3, pady=1)
            e_tank.insert(0, f"{float(tank):.1f}")
            row["tank"] = e_tank


            # bar INI (input) - default 200
            e_bar_ini = ttk.Entry(tbl, width=6)
            e_bar_ini.grid(row=r, column=12, padx=3, pady=1)
            e_bar_ini.insert(0, "200")
            row["bar_ini"] = e_bar_ini

            # consumo [l] (calcolato, readonly)
            e_cons = ttk.Entry(tbl, width=7, state="readonly", style="Calc.TEntry")
            e_cons.grid(row=r, column=13, padx=3, pady=1)
            row["cons_l"] = e_cons

            # bar END (calcolato, readonly; rosso se negativo)
            e_bar_end = ttk.Entry(tbl, width=7, state="readonly", style="Calc.TEntry")
            e_bar_end.grid(row=r, column=14, padx=3, pady=1)
            row["bar_end"] = e_bar_end

            # Update consumption/bar_end when relevant inputs change
            for key in ("vrm", "tank", "bar_ini"):
                try:
                    row[key].bind("<FocusOut>", lambda event, self=self: self._update_gas_pressure_consumption())
                    row[key].bind("<Return>", lambda event, self=self: self._update_gas_pressure_consumption())
                except Exception:
                    pass
            self.bailout_rows.append(row)


        # GUI-only: replica calcoli automatici della tabella OC (FN2, pp, delta) in modo identico
        def _parse_float(entry_widget):
            try:
                return float(entry_widget.get().strip().replace(",", "."))
            except Exception:
                return None

        def _set_readonly(entry_widget, value_str: str):
            try:
                entry_widget.configure(state="normal")
                entry_widget.delete(0, tk.END)
                entry_widget.insert(0, value_str)
            finally:
                entry_widget.configure(state="readonly")

        def refresh_bailout_table(*_):
            """Aggiorna FN2, ppO2/ppHe/ppN2 e ΔppN2 della tabella Bailout.

            Regole ΔppN2 (Pamb semplice = depth/10+1):
            - Per ogni gas bailout ON: ΔppN2 è calcolato alla sua MOD come:
                ΔppN2 = Pamb(MOD) * (FN2_gas - FN2_gas_precedente_attivo)
              dove il precedente è il primo bailout ON precedente (scorrendo all'indietro).
            - Per il *primo* gas bailout ON: ΔppN2 è calcolato a EOBT (profondità fondo) come:
                ΔppN2 = Pamb(bottom) * (FN2_gas - FN2_CC@EOBT)
              con:
                FN2_CC@EOBT = ((Pamb(bottom) - SP_fondo)/Pamb(bottom)) * (FN2_dil/(FN2_dil+FHe_dil))
            """
            # --- calcolo FN2 equivalente del CC al fondo (da soli input GUI) ---
            def _safe_float(val):
                try:
                    return float(str(val).strip().replace(",", "."))
                except Exception:
                    return None

            bottom_depth = _safe_float(getattr(self, "entry_depth", None).get() if getattr(self, "entry_depth", None) else None)
            pamb_bottom = (bottom_depth / 10.0 + 1.0) if bottom_depth is not None else None

            sp_bottom = _safe_float(self.cc_sp_bottom.get() if hasattr(self, "cc_sp_bottom") else None)

            # Diluent fractions
            dil_fn2 = _safe_float(self.cc_dil_fn2.get() if hasattr(self, "cc_dil_fn2") else None)
            dil_fhe = _safe_float(self.cc_dil_fhe.get() if hasattr(self, "cc_dil_fhe") else None)

            fn2_cc_eobt = None
            if pamb_bottom is not None and sp_bottom is not None and dil_fn2 is not None and dil_fhe is not None:
                denom = (dil_fn2 + dil_fhe)
                if denom > 0 and pamb_bottom > 0:
                    r_n2 = dil_fn2 / denom
                    fn2_cc_eobt = ((pamb_bottom - sp_bottom) / pamb_bottom) * r_n2

            prev_fn2_active = None


            error_any = False
            for rr in self.bailout_rows:
                fo2_v = _parse_float(rr["fo2"])
                fhe_v = _parse_float(rr["fhe"])
                mod_v = _parse_float(rr["mod"])

                if fo2_v is None or fhe_v is None:
                    # se mancano i dati, lascia vuoti i campi calcolati
                    _set_readonly(rr["fn2"], "")
                    _set_readonly(rr["ppO2_bar"], "")
                    _set_readonly(rr["ppHe"], "")
                    _set_readonly(rr["ppN2"], "")
                    _set_readonly(rr["dppN2"], "")
                    continue

                fn2_raw = 1.0 - fo2_v - fhe_v
                if fn2_raw < 0:
                    # Non accettare FN2 negativa: forza 0.00 e segnala errore (come per il Diluente)
                    fn2_v = 0.0
                    error_any = True
                    try:
                        rr["fn2"].configure(style="Error.TEntry")
                    except Exception:
                        pass
                else:
                    fn2_v = fn2_raw
                    try:
                        rr["fn2"].configure(style="TEntry")
                    except Exception:
                        pass

                _set_readonly(rr["fn2"], f"{fn2_v:.2f}")

                # pp calcolate a Pamb(MOD) (solo GUI, coerente con OC)
                if mod_v is None:
                    mod_v = 0.0
                pamb_mod = (mod_v / 10.0) + 1.0
                ppO2 = pamb_mod * fo2_v
                ppHe = pamb_mod * fhe_v
                ppN2 = pamb_mod * fn2_v

                _set_readonly(rr["ppO2_bar"], f"{ppO2:.2f}")
                _set_readonly(rr["ppHe"], f"{ppHe:.2f}")
                _set_readonly(rr["ppN2"], f"{ppN2:.2f}")

                enabled = bool(rr.get("enabled_var").get()) if rr.get("enabled_var") is not None else False

                if not enabled:
                    # gas non portato: ΔppN2 vuoto e non aggiorna la catena
                    _set_readonly(rr["dppN2"], "")
                    continue

                # Gas ON: calcola ΔppN2 secondo le regole
                if prev_fn2_active is None:
                    # primo bailout attivo: riferimento = CC@EOBT a Pamb(bottom)
                    if pamb_bottom is None or fn2_cc_eobt is None:
                        _set_readonly(rr["dppN2"], "")
                    else:
                        dpp = pamb_bottom * (fn2_v - fn2_cc_eobt)
                        _set_readonly(rr["dppN2"], f"{dpp:.2f}")
                else:
                    dpp = pamb_mod * (fn2_v - prev_fn2_active)
                    _set_readonly(rr["dppN2"], f"{dpp:.2f}")

                prev_fn2_active = fn2_v


            # Messaggio errore FN2 (almeno una riga con FO2+FHe>1)
            try:
                if error_any:
                    # UI note: eventuale label errore può essere stata rimossa per compattare il layout.
                    # Manteniamo qui un hook sicuro: se esiste una label, la aggiorniamo.
                    if hasattr(self, "lbl_gas_error") and self.lbl_gas_error is not None:
                        try:
                            self.lbl_gas_error.configure(text="Errore: FO2+FHe > 1.00 in almeno una riga gas.")
                        except Exception:
                            pass
                else:
                    if hasattr(self, "lbl_gas_error") and self.lbl_gas_error is not None:
                        try:
                            self.lbl_gas_error.configure(text="")
                        except Exception:
                            pass
            except Exception:
                pass


            # --- controllo logico: Bailout gas 1 MOD >= profondità di fondo ---
            # MOD è un dato di input (responsabilità del diver). Se MOD_BO1 < depth_bottom:
            # - warning (testo)
            # - blocca SOLO il calcolo della risalita in Bailout (forza checkbox OFF e disabilita),
            #   senza impattare la risalita in CC.
            try:
                depth_bottom = _safe_float(self.entry_depth.get())
            except Exception:
                depth_bottom = None
            try:
                mod_bo1 = _safe_float(self.bailout_rows[0]["mod"].get()) if self.bailout_rows else None
            except Exception:
                mod_bo1 = None

            eps_m = 0.1  # tolleranza metrica anti-rounding
            warn_mod = False
            if (depth_bottom is not None) and (mod_bo1 is not None):
                if mod_bo1 < (depth_bottom - eps_m):
                    warn_mod = True

            # Evidenzia MOD BO1 in rosso se inferiore alla profondità di fondo
            try:
                if self.bailout_rows and isinstance(self.bailout_rows, list) and len(self.bailout_rows) >= 1:
                    e_mod_bo1 = self.bailout_rows[0].get("mod")
                    if e_mod_bo1 is not None:
                        e_mod_bo1.configure(style=("Error.TEntry" if warn_mod else "TEntry"))
            except Exception:
                pass

            try:
                if hasattr(self, "lbl_bailout_mod_warning") and self.lbl_bailout_mod_warning is not None:
                    if warn_mod:
                        self.lbl_bailout_mod_warning.config(
                            text="Il bailout gas 1 ha MOD inferiore alla profondità di fondo"
                        )
                    else:
                        self.lbl_bailout_mod_warning.config(text="")
            except Exception:
                pass

            # Applica blocco bail-out ascent solo se i widget esistono già
            try:
                if hasattr(self, "chk_use_bailout_widget") and hasattr(self, "chk_use_bailout"):
                    if warn_mod:
                        try:
                            self.chk_use_bailout.set(False)
                        except Exception:
                            pass
                        self.chk_use_bailout_widget.state(["disabled"])
                    else:
                        self.chk_use_bailout_widget.state(["!disabled"])
            except Exception:
                pass

            # mostra/nasconde la checkbox "Calcolo della risalita in Bailout"
            try:
                self._update_bailout_calc_checkbox_visibility()
            except Exception:
                pass

        # hook su cambi FO2/FHe/MOD/ON: ricalcolo immediato (solo GUI)
        for rr in self.bailout_rows:
            for k in ("fo2", "fhe", "mod"):
                rr[k].bind("<FocusOut>", refresh_bailout_table)
                rr[k].bind("<KeyRelease>", refresh_bailout_table)
            # anche toggle ON aggiorna ΔppN2
            try:
                rr["cb"].configure(command=refresh_bailout_table)
            except Exception:
                pass


        # anche profondità di fondo impatta ΔppN2 e il warning MOD del primo bailout
        try:
            self.entry_depth.bind("<FocusOut>", refresh_bailout_table)
            self.entry_depth.bind("<KeyRelease>", refresh_bailout_table)
        except Exception:
            pass
        # Espone il refresh ΔppN2 Bailout a livello di istanza (per richiamo da altri handler)
        self._refresh_bailout_table = refresh_bailout_table
        refresh_bailout_table()

        # Checkbox permanente (solo input): sempre visibile nel box Bailout
        self.chk_use_bailout = tk.BooleanVar(value=False)
        self.chk_use_bailout_widget = ttk.Checkbutton(
            tbl,
            text="Calcolo della risalita in Bailout",
            variable=self.chk_use_bailout,
            command=self._on_bailout_ascent_toggle
        )
        self.chk_use_bailout_widget.grid(row=6, column=0, columnspan=12, sticky=tk.W, padx=3, pady=(2, 0))
# applica subito eventuale blocco MOD_BO1<profondità fondo
        try:
            refresh_bailout_table()
        except Exception:
            pass

    def create_gas_table(self, parent):
        """
        Tabella dei gas:
        - riga 1: gas fondo (sempre ON, senza pulsante)
        - righe 2-5: gas deco1..4 con pulsante ON/OFF
        Colonne: [nome, ON, FO2, FHe, FN2, MOD, ppO2, VRM, tank]
        FN2 e ppO2 sono calcolate automaticamente.
        """
        headers = ["", "ON", "FO2", "FHe", "FN2", "MOD", "ppO2", "ppHe", "ppN2", "ΔppN2", "VRM", "tank", "bar INI", "consumo", "bar END"]
        for col, h in enumerate(headers):
            ttk.Label(parent, text=h).grid(row=0, column=col, padx=3, pady=1)

        # nome, FO2, FHe, MOD, VRM, tank  (FN2 e ppO2 li calcoliamo noi)
        gas_specs = [
            ("gas fondo", 0.10, 0.70, 120, 15, 40.0),
            ("gas deco1", 0.18, 0.62, 65,  15, 11.0),
            ("gas deco2", 0.35, 0.30, 36,  15, 11.0),
            ("gas deco3", 0.50, 0.10, 21,  15, 11.0),
            ("gas deco4", 1.00, 0.00, 6,   15, 5.6),
        ]

        self.gas_rows = []

        for r, (name, fo2, fhe, mod, vrm, tank) in enumerate(gas_specs, start=1):
            row = {}
            row["name"] = name  # <--- nome logico del gas

            # Etichetta riga (gas fondo, gas deco1, ...)
            ttk.Label(parent, text=name).grid(row=r, column=0, padx=3, pady=1, sticky=tk.W)

            # Pulsante ON/OFF: disabilitato per gas fondo (sempre ON)
            enabled_var = tk.BooleanVar(value=True)
            if name == "gas fondo":
                cb = ttk.Checkbutton(parent, variable=enabled_var, state="disabled")
            else:
                cb = ttk.Checkbutton(parent, variable=enabled_var, command=self.update_gas_calculated_fields)
            cb.grid(row=r, column=1, padx=3, pady=1)
            row["var_enabled"] = enabled_var

            # FO2
            e_fo2 = ttk.Entry(parent, width=6)
            e_fo2.grid(row=r, column=2, padx=3, pady=1)
            e_fo2.insert(0, f"{fo2:.2f}")
            row["fo2"] = e_fo2

            # FHe
            e_fhe = ttk.Entry(parent, width=6)
            e_fhe.grid(row=r, column=3, padx=3, pady=1)
            e_fhe.insert(0, f"{fhe:.2f}")
            row["fhe"] = e_fhe

            # FN2 (calcolata, sola lettura)
            e_fn2 = ttk.Entry(parent, width=6, state="readonly")
            e_fn2.grid(row=r, column=4, padx=3, pady=1)
            row["fn2"] = e_fn2

            # MOD (m)
            e_mod = ttk.Entry(parent, width=6)
            e_mod.grid(row=r, column=5, padx=3, pady=1)
            e_mod.insert(0, f"{mod:.0f}")
            row["mod"] = e_mod

            # ppO2 (calcolata, sola lettura)
            e_ppo2 = ttk.Entry(parent, width=6, state="readonly")
            e_ppo2.grid(row=r, column=6, padx=3, pady=1)
            row["ppo2"] = e_ppo2

            # ppHe (calcolata, sola lettura)
            e_pphe = ttk.Entry(parent, width=6, state="readonly")
            e_pphe.grid(row=r, column=7, padx=3, pady=1)
            row["pphe"] = e_pphe

            # ppN2 (calcolata, sola lettura)
            # ppN2 (calcolata, sola lettura)
            e_ppn2 = ttk.Entry(parent, width=6, state="readonly")
            e_ppn2.grid(row=r, column=8, padx=3, pady=1)
            row["ppn2"] = e_ppn2

            # ΔppN2 (calcolata, sola lettura)
            e_dppn2 = ttk.Entry(parent, width=6, state="readonly")
            e_dppn2.grid(row=r, column=9, padx=3, pady=1)
            row["dppn2"] = e_dppn2

            # VRM
            e_vrm = ttk.Entry(parent, width=6)
            e_vrm.grid(row=r, column=10, padx=3, pady=1)
            e_vrm.insert(0, f"{vrm:.0f}")
            row["vrm"] = e_vrm

            # tank (litri)
            e_tank = ttk.Entry(parent, width=6)
            e_tank.grid(row=r, column=11, padx=3, pady=1)
            e_tank.insert(0, f"{tank:.1f}")
            row["tank"] = e_tank


            # bar INI (input) - default 200
            e_bar_ini = ttk.Entry(parent, width=6)
            e_bar_ini.grid(row=r, column=12, padx=3, pady=1)
            e_bar_ini.insert(0, "200")
            row["bar_ini"] = e_bar_ini

            # consumo [l] (calcolato, readonly)
            e_cons = ttk.Entry(parent, width=7, state="readonly", style="Calc.TEntry")
            e_cons.grid(row=r, column=13, padx=3, pady=1)
            row["cons_l"] = e_cons

            # bar END (calcolato, readonly; rosso se negativo)
            e_bar_end = ttk.Entry(parent, width=7, state="readonly", style="Calc.TEntry")
            e_bar_end.grid(row=r, column=14, padx=3, pady=1)
            row["bar_end"] = e_bar_end

            # Update consumption/bar_end when relevant inputs change
            for key in ("vrm", "tank", "bar_ini"):
                try:
                    row[key].bind("<FocusOut>", lambda event, self=self: self._update_gas_pressure_consumption())
                    row[key].bind("<Return>", lambda event, self=self: self._update_gas_pressure_consumption())
                except Exception:
                    pass
            # Aggiornamento immediato FN2/pp quando si digita + fallback su focus out
            for key in ("fo2", "fhe", "mod"):
                row[key].bind("<KeyRelease>", lambda event, self=self: self.update_gas_calculated_fields())
                row[key].bind("<FocusOut>", lambda event, self=self: self.update_gas_calculated_fields())

            self.gas_rows.append(row)

        self.lbl_gas_error = ttk.Label(parent, text="", foreground="red")
        self.lbl_gas_error.grid(row=6, column=0, columnspan=12, sticky=tk.W, padx=3, pady=(2, 0))

        # Primo calcolo iniziale con i default
        self.update_gas_calculated_fields()
    def _update_gas_pressure_consumption(self):
        """
        Calcola e aggiorna (solo GUI) le colonne:
        - consumo [l] = VRM * Σ(seg_time * P_amb_media_segmento) per ciascun gas OC/BO effettivamente respirato
          dove P_amb_media_segmento = (depth_mean/10 + 1) ATA e depth_mean:
            - se (from==0 e to==0) usa depth
            - altrimenti usa (from+to)/2
        - bar END = (barINI*tank - consumo)/tank
        Vincoli:
        - si calcola SOLO per segmenti OC e BO (nessun consumo in CC)
        - i gas sono attribuiti usando la stessa logica della colonna GAS (col 10) del profilo dettagliato
        - valori a 0 decimali; bar END in rosso se negativo
        """
        profile = getattr(self, "last_profile_rows", None) or []

        def _pf(v, default=0.0):
            try:
                if v is None:
                    return default
                if isinstance(v, (int, float)):
                    return float(v)
                s = str(v).strip().replace(",", ".")
                if s == "":
                    return default
                return float(s)
            except Exception:
                return default

        # Accumulo surface-minutes per chiave logica tabella (OC/BO)
        sm_by_key = {}

        for rr in profile:
            try:
                seg_time = _pf(rr.get("Seg_time_min", rr.get("seg_time", 0.0)), 0.0)
                if seg_time <= 0:
                    continue

                d_from = rr.get("Depth_from_m", rr.get("from", 0.0))
                d_to   = rr.get("Depth_to_m", rr.get("to", 0.0))
                d_fix  = rr.get("Depth_m", rr.get("depth", 0.0))

                d_from_f = _pf(d_from, 0.0)
                d_to_f   = _pf(d_to, 0.0)
                d_fix_f  = _pf(d_fix, 0.0)

                # depth mean: quota costante (from=to=0) => usa depth
                if abs(d_from_f) < 1e-9 and abs(d_to_f) < 1e-9 and d_fix_f > 0:
                    depth_mean = d_fix_f
                else:
                    depth_mean = 0.5 * (d_from_f + d_to_f)

                ata = depth_mean / 10.0 + 1.0
                if ata <= 0:
                    continue

                # Attribuzione gas tramite formatter colonna 10 (GAS)
                # show_metrics=False per performance; prendiamo il gas_label.
                # Attribuzione gas tramite formatter colonna 10 (GAS)
                # IMPORTANT: _format_profile_row_for_tree vuole una gas_table "engine-like" (lista di dict FO2/FHe/MOD/enable),
                # non la struttura dei widget. In OC usiamo self.last_gas_table (già costruita da Calcola deco).
                # In BO (CC+BO) costruiamo al volo una tabella equivalente dai widget bailout_rows.
                try:
                    mode_here = str(rr.get("Mode", rr.get("mode", ""))).strip().upper()

                    oc_table = getattr(self, "last_gas_table", None)
                    if not oc_table:
                        oc_table = []
                        for roww in getattr(self, "gas_rows", []) or []:
                            def _to_float_entry(w):
                                try:
                                    s = (w.get() or "").strip().replace(",", ".")
                                    return float(s) if s else 0.0
                                except Exception:
                                    return 0.0
                            def _is_on_var(v):
                                try:
                                    return True if v is None else bool(v.get())
                                except Exception:
                                    return True
                            oc_table.append({
                                "name": roww.get("name", ""),
                                "FO2": _to_float_entry(roww.get("fo2")),
                                "FHe": _to_float_entry(roww.get("fhe")),
                                "MOD": _to_float_entry(roww.get("mod")),
                                "enabled": _is_on_var(roww.get("var_enabled")),
                            })

                    bo_table = []
                    for roww in getattr(self, "bailout_rows", []) or []:
                        def _to_float_entry(w):
                            try:
                                s = (w.get() or "").strip().replace(",", ".")
                                return float(s) if s else 0.0
                            except Exception:
                                return 0.0
                        def _is_on_var(v):
                            try:
                                return True if v is None else bool(v.get())
                            except Exception:
                                return True
                        bo_table.append({
                            "name": roww.get("name", ""),
                            "FO2": _to_float_entry(roww.get("fo2")),
                            "FHe": _to_float_entry(roww.get("fhe")),
                            "MOD": _to_float_entry(roww.get("mod")),
                            "enabled": _is_on_var(roww.get("var_enabled")),
                        })

                    gas_table_for_fmt = bo_table if mode_here == "BO" else oc_table
                    cols = self._format_profile_row_for_tree(rr, gas_table_for_fmt, show_metrics=False)
                    gas_label = str(cols[9] if len(cols) > 9 else "").strip().lstrip("'")
                except Exception:
                    gas_label = ""
                if not gas_label:
                    continue

                # Normalizza eventuale apostrofo Excel
                gas_label = gas_label.lstrip("'")

                # Mappa prefisso -> chiave tabella (OC/BO)
                key = None
                if gas_label.startswith("BO_"):
                    # BO_1_... -> bailout gas 1
                    m = re.match(r"BO_(\d+)_", gas_label)
                    if m:
                        key = f"bailout gas {int(m.group(1))}"
                elif gas_label.startswith("BOTT_"):
                    key = "gas fondo"
                elif gas_label.startswith("DEC"):
                    m = re.match(r"DEC(\d+)_", gas_label)
                    if m:
                        key = f"gas deco{int(m.group(1))}"

                if key is None:
                    # CC (DIL_) o altro: non conteggiare consumo
                    continue

                sm_by_key[key] = sm_by_key.get(key, 0.0) + seg_time * ata

            except Exception:
                continue

        def _set_ro(entry, val_str, style_name="Calc.TEntry"):
            if entry is None:
                return
            try:
                entry.configure(state="normal")
            except Exception:
                pass
            try:
                entry.delete(0, tk.END)
                entry.insert(0, val_str)
            except Exception:
                pass
            try:
                entry.configure(style=style_name)
            except Exception:
                pass
            try:
                entry.configure(state="readonly")
            except Exception:
                pass

        def _is_enabled(row):
            # OC: var_enabled; BO: var_enabled (checkbox)
            try:
                v = row.get("var_enabled", None)
                return True if v is None else bool(v.get())
            except Exception:
                return True

        # Aggiorna tabella OC (self.gas_rows)
        for row in getattr(self, "gas_rows", []) or []:
            name = row.get("name")
            if not name:
                continue
            enabled = _is_enabled(row) or (name == "gas fondo")
            sm = sm_by_key.get(name, 0.0) if enabled else 0.0

            vrm = _pf(row.get("vrm").get() if row.get("vrm") else 0.0, 0.0)
            tank = _pf(row.get("tank").get() if row.get("tank") else 0.0, 0.0)
            bar_ini = _pf(row.get("bar_ini").get() if row.get("bar_ini") else 0.0, 0.0)

            cons_l = max(0.0, vrm * sm) if enabled else 0.0
            cons_str = f"{cons_l:.0f}" if enabled else "0"

            bar_end = bar_ini
            if tank > 0:
                bar_end = (bar_ini * tank - cons_l) / tank
            bar_end_str = f"{bar_end:.0f}" if enabled else f"{bar_ini:.0f}"

            style = "Calc.TEntry"
            if enabled and bar_end < 0:
                style = "CalcNeg.TEntry"

            _set_ro(row.get("cons_l"), cons_str, "Calc.TEntry")
            _set_ro(row.get("bar_end"), bar_end_str, style)

        # Aggiorna tabella Bailout (self.bailout_rows)
        for row in getattr(self, "bailout_rows", []) or []:
            name = row.get("name")
            if not name:
                continue
            enabled = _is_enabled(row)
            sm = sm_by_key.get(name, 0.0) if enabled else 0.0

            vrm = _pf(row.get("vrm").get() if row.get("vrm") else 0.0, 0.0)
            tank = _pf(row.get("tank").get() if row.get("tank") else 0.0, 0.0)
            bar_ini = _pf(row.get("bar_ini").get() if row.get("bar_ini") else 0.0, 0.0)

            cons_l = max(0.0, vrm * sm) if enabled else 0.0
            cons_str = f"{cons_l:.0f}" if enabled else "0"

            bar_end = bar_ini
            if tank > 0:
                bar_end = (bar_ini * tank - cons_l) / tank
            bar_end_str = f"{bar_end:.0f}" if enabled else f"{bar_ini:.0f}"

            style = "Calc.TEntry"
            if enabled and bar_end < 0:
                style = "CalcNeg.TEntry"

            _set_ro(row.get("cons_l"), cons_str, "Calc.TEntry")
            _set_ro(row.get("bar_end"), bar_end_str, style)


    def update_gas_calculated_fields(self):
        """
        Aggiorna FN2, ppO2, ppHe, ppN2 e ΔppN2 per tutte le righe della tabella gas.
        ppO2/ppHe/ppN2 sono calcolate alla MOD con:
            ppX = (MOD/10 + 1) * FX
        ΔppN2 è calcolata solo per i gas deco ON come differenza tra:
            ppN2_nuovo(MOD_deco) - ppN2_vecchio(MOD_deco)
        dove ppN2_vecchio è la ppN2 del gas che si abbandona alla stessa MOD.
        """
        prev_fn2_val = None  # frazione N2 del gas precedente "attivo" (ON)

        error_any = False  # almeno una riga con FO2+FHe>1.00

        for row in getattr(self, "gas_rows", []):
            def to_float(entry):
                txt = entry.get().strip().replace(",", ".")
                if not txt:
                    return None
                try:
                    return float(txt)
                except ValueError:
                    return None

            fo2 = to_float(row["fo2"])
            fhe = to_float(row["fhe"])
            mod = to_float(row["mod"])
            # FN2 = 1 - FO2 - FHe (con controllo logico: FN2 non può essere negativa)
            if fo2 is not None and fhe is not None:
                fn2_raw = 1.0 - fo2 - fhe
                if fn2_raw < 0:
                    # Non accettare FN2 negativa: forza 0.00 e segnala errore
                    fn2_val = 0.0
                    fn2_str = f"{fn2_val:.2f}"
                    error_any = True
                    try:
                        row["fn2"].configure(style="Error.TEntry")
                    except Exception:
                        pass
                else:
                    fn2_val = fn2_raw
                    fn2_str = f"{fn2_val:.2f}"
                    try:
                        row["fn2"].configure(style="TEntry")
                    except Exception:
                        pass
            else:
                fn2_val = None
                fn2_str = ""
                try:
                    row["fn2"].configure(style="TEntry")
                except Exception:
                    pass

            row["fn2"].config(state="normal")
            row["fn2"].delete(0, tk.END)
            row["fn2"].insert(0, fn2_str)
            row["fn2"].config(state="readonly")

            # ppO2 in bar
            if fo2 is not None and mod is not None:
                ppo2 = (mod / 10.0 + 1.0) * fo2
                ppo2_str = f"{ppo2:.2f}"
            else:
                ppo2 = None
                ppo2_str = ""

            row["ppo2"].config(state="normal")
            row["ppo2"].delete(0, tk.END)
            row["ppo2"].insert(0, ppo2_str)
            row["ppo2"].config(state="readonly")

            # ppHe in bar
            if fhe is not None and mod is not None:
                pphe = (mod / 10.0 + 1.0) * fhe
                pphe_str = f"{pphe:.2f}"
            else:
                pphe = None
                pphe_str = ""

            row["pphe"].config(state="normal")
            row["pphe"].delete(0, tk.END)
            row["pphe"].insert(0, pphe_str)
            row["pphe"].config(state="readonly")

            # ppN2 in bar
            if fn2_val is not None and mod is not None:
                ppn2 = (mod / 10.0 + 1.0) * fn2_val
                ppn2_str = f"{ppn2:.2f}"
            else:
                ppn2 = None
                ppn2_str = ""

            row["ppn2"].config(state="normal")
            row["ppn2"].delete(0, tk.END)
            row["ppn2"].insert(0, ppn2_str)
            row["ppn2"].config(state="readonly")

            # ΔppN2: reset campo a vuoto di default se esiste
            if "dppn2" in row:
                row["dppn2"].config(state="normal")
                row["dppn2"].delete(0, tk.END)
                row["dppn2"].insert(0, "")
                row["dppn2"].config(state="readonly")

            enabled = bool(row["var_enabled"].get())
            name = row.get("name", "")

            if enabled and fn2_val is not None and mod is not None:
                if prev_fn2_val is None:
                    # primo gas abilitato (tipicamente gas fondo)
                    prev_fn2_val = fn2_val
                else:
                    # per i gas deco ON (non gas fondo) calcoliamo ΔppN2
                    if name != "gas fondo" and "dppn2" in row:
                        pamb = (mod / 10.0) + 1.0  # bar assoluta alla MOD
                        ppn2_old = pamb * prev_fn2_val
                        ppn2_new = pamb * fn2_val
                        delta = ppn2_new - ppn2_old

                        row["dppn2"].config(state="normal")
                        row["dppn2"].delete(0, tk.END)
                        row["dppn2"].insert(0, f"{delta:.2f}")
                        row["dppn2"].config(state="readonly")

                    # aggiorna sempre il gas precedente attivo
                    prev_fn2_val = fn2_val
            else:
                # gas non abilitato: non entra nella catena di respiri, non aggiorna prev_fn2_val
                pass


        # Messaggio errore FN2 (almeno una riga con FO2+FHe>1)
        try:
            if getattr(self, "lbl_gas_error", None) is not None:
                if error_any:
                    self.lbl_gas_error.config(text="Errore: FO2 + FHe > 1.00 (FN2 negativo)")
                else:
                    self.lbl_gas_error.config(text="")
        except Exception:
            pass
        # Aggiorna consumo e bar END (solo GUI)
        try:
            self._update_gas_pressure_consumption()
        except Exception:
            pass

    def on_calculate(self):
        self.label_error.config(text="")
        self.label_runtime.config(text="--")
        self.label_deco.config(text="--")
        if hasattr(self, "label_decozone"):
            self.label_decozone.config(text="--")
        if hasattr(self, "label_final_depthavg"):
            self.label_final_depthavg.config(text="--")
        if hasattr(self, "label_max_gasdens"):
            self.label_max_gasdens.config(text="--")
        if hasattr(self, "label_final_cns"):
            self.label_final_cns.config(text="--")
        if hasattr(self, "label_final_otu"):
            self.label_final_otu.config(text="--")
        self.tree.delete(*self.tree.get_children())
        self.last_profile_rows = []
        # Aggiorna campi FN2 e ppO2 della tabella gas
        self.update_gas_calculated_fields()

        # -----------------------------
        # 1) Lettura e validazione input
        _requested_algo = (self.deco_model_var.get() or "VPM").strip().upper()
        # -----------------------------
        try:
            depth = parse_float(self.entry_depth.get(), "Profondità fondo")
            bottom = parse_float(self.entry_bottom.get(), "Tempo fondo")
            # Modalità respirazione (OC/CC)
            mode = self.mode_var.get() if hasattr(self, "mode_var") else "OC"

            def gas_float(entry, label):
                return parse_float(entry.get(), label)

            if mode == "CC":
                # In CC, il "gas" di riferimento per il motore è il DILUENTE.
                if not (hasattr(self, "entry_cc_dil_fo2") and hasattr(self, "entry_cc_dil_fhe")):
                    raise ValueError("Pannello CCR non disponibile: impossibile leggere il diluente.")
                fo2 = gas_float(self.entry_cc_dil_fo2, "FO2 diluente")
                fhe = gas_float(self.entry_cc_dil_fhe, "FHe diluente")

                if fo2 < 0.0 or fo2 > 1.0:
                    raise ValueError("FO2 diluente deve essere tra 0.0 e 1.0.")
                if fhe < 0.0 or fhe > 1.0:
                    raise ValueError("FHe diluente deve essere tra 0.0 e 1.0.")
                if fo2 + fhe > 1.0 + 1e-6:
                    raise ValueError("FO2 + FHe diluente non può superare 1.0.")

                # Setpoint bottom è richiesto per definire il calcolo CC nel main
                if not (hasattr(self, "cc_sp_bottom") and str(self.cc_sp_bottom.get()).strip()):
                    raise ValueError("In modalità CC è necessario impostare almeno il setpoint di fondo (SP bottom).")
            else:
                # OC: gas di fondo preso dalla prima riga della tabella gas
                fondo = self.gas_rows[0]
                fo2 = gas_float(fondo["fo2"], "FO2 gas fondo")
                fhe = gas_float(fondo["fhe"], "FHe gas fondo")

                if fo2 < 0.0 or fo2 > 1.0:
                    raise ValueError("FO2 gas fondo deve essere tra 0.0 e 1.0.")
                if fhe < 0.0 or fhe > 1.0:
                    raise ValueError("FHe gas fondo deve essere tra 0.0 e 1.0.")
                if fo2 + fhe > 1.0 + 1e-6:
                    raise ValueError("FO2 + FHe gas fondo non può superare 1.0.")

            rapsol = parse_float(self.entry_rapsol.get(), "rapsol")
            # Normalizza solubility N2/He a 3 decimali (UI + calcoli)
            rapsol = round(float(rapsol), 3)
            crit_rad_n2 = parse_float(self.entry_crit_rad_n2.get(), "Raggio critico N2 (µm)")
            crit_rad_he = parse_float(self.entry_crit_rad_he.get(), "Raggio critico He (µm)")

            if fo2 < 0 or fo2 > 1 or fhe < 0 or fhe > 1:
                raise ValueError("FO2 e FHe devono essere fra 0 e 1.")
            if fo2 + fhe > 1.0 + 1e-6:
                raise ValueError("FO2 + FHe non può essere > 1. (FN2 = 1 - FO2 - FHe).")
            if rapsol < 1.0 or rapsol > 3.0:
                raise ValueError("Solubility N2/He deve essere compresa tra 1.00 e 3.00")
            if not (0.20 <= crit_rad_n2 <= 1.35):
                raise ValueError("Raggio critico N2 deve essere tra 0.20 e 1.35 µm.")
            if not (0.20 <= crit_rad_he <= 1.35):
                raise ValueError("Raggio critico He deve essere tra 0.20 e 1.35 µm.")

        except ValueError as e:
            self.label_error.config(text=str(e))
            return

        # Velocità nominali di discesa/risalita derivate dai profili A2/A3
        # (usate solo come valori di comodo per il main; il profilo reale segue le bande JSON)
        try:
            descent_bands = getattr(self, "descent_speed_bands", [])
        except Exception:
            descent_bands = []
        try:
            ascent_bands = getattr(self, "ascent_speed_bands", [])
        except Exception:
            ascent_bands = []

        if descent_bands:
            desc_rate = float(descent_bands[-1].get("speed", 20.0))
        else:
            desc_rate = 20.0

        if ascent_bands:
            asc_rate = float(ascent_bands[0].get("speed", 10.0))
        else:
            asc_rate = 10.0

# -----------------------------
        # 2) Costruzione tabella gas per il main
        # -----------------------------
        def _is_on(v):
            try:
                if isinstance(v, bool):
                    return v
                if v is None:
                    return False
                if isinstance(v, (int, float)):
                    return bool(int(v))
                s = str(v).strip().lower()
                return s in ("1","true","on","yes","y","t")
            except Exception:
                return False

        gas_table = []
        for row in self.gas_rows:
            def to_float_entry(entry):
                txt = entry.get().strip().replace(",", ".")
                if not txt:
                    return 0.0
                try:
                    return float(txt)
                except ValueError:
                    return 0.0

            gas_table.append({
                "name": row["name"],
                "FO2": to_float_entry(row["fo2"]),
                "FHe": to_float_entry(row["fhe"]),
                "MOD": to_float_entry(row["mod"]),
                "enabled": bool(row.get("var_enabled").get()) if row.get("var_enabled") is not None else True,
            })

        
        # Salva gas_table calcolata per uso GUI (profili / metriche / export)
        self.last_gas_table = gas_table

        # In modalità CC, per il motore il "mix 1" è sempre il DILUENTE.
        # Se l'utente attiva "Calcolo della risalita in Bailout", aggiungiamo anche i gas di Bailout
        # come mix 2..N (con ON/OFF per consentire fallback automatico sul precedente gas attivo).
        # Questo wiring NON altera la fisica OC né la parte CC normale: serve solo a fornire al main
        # la lista gas completa quando si richiede BO.
        if (hasattr(self, "mode_var") and self.mode_var.get() == "CC"):
            # mix 1 = diluente
            try:
                dil_mod = float(str(self.cc_dil_mod.get()).strip().replace(",", ".")) if hasattr(self, "cc_dil_mod") else float(depth)
            except Exception:
                dil_mod = float(depth)

            gas_table_cc = [{
                "name": "Diluente",
                "FO2": float(fo2),
                "FHe": float(fhe),
                "MOD": float(dil_mod),
                "enabled": True,
            }]

            use_bailout_ascent = False
            try:
                if hasattr(self, "chk_use_bailout") and self.chk_use_bailout is not None:
                    use_bailout_ascent = bool(self.chk_use_bailout.get())
            except Exception:
                use_bailout_ascent = False

            if use_bailout_ascent and hasattr(self, "bailout_rows") and self.bailout_rows:
                def _to_float_entry_widget(w):
                    try:
                        s = (w.get() or "").strip().replace(",", ".")
                        return float(s) if s else 0.0
                    except Exception:
                        return 0.0

                for rr in self.bailout_rows:
                    gas_table_cc.append({
                        "name": rr.get("name", "bailout"),
                        "FO2": _to_float_entry_widget(rr.get("fo2")),
                        "FHe": _to_float_entry_widget(rr.get("fhe")),
                        "MOD": _to_float_entry_widget(rr.get("mod")),
                        "enabled": _is_on(rr.get("enabled_var").get()) if rr.get("enabled_var") is not None else False,
                    })

            gas_table = gas_table_cc
            self.last_gas_table = gas_table

        # Salva ultimi input su file
        try:
            save_last_inputs(self.collect_inputs_for_save())
        except Exception:
            pass

        # -----------------------------
        # 3) Lancio motore VPM-B

        # -----------------------------
        _requested_algo = 'VPM'
        try:
            if hasattr(self, 'deco_model_var'):
                _requested_algo = str(self.deco_model_var.get() or 'VPM').strip().upper()
        except Exception:
            _requested_algo = 'VPM'

        # -----------------------------
        try:
            # --- Refresh DECO model + GF env vars on every calculation (ZH-L16) ---
            # This prevents stale GF values after a first run.
            import os
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            _model = (self.deco_model_var.get() or 'VPM').strip().upper()
            if _model in ('ZHL16','ZH-L16','ZHL-16'):
                os.environ['VPM_DECO_MODEL'] = 'ZHL16'
                os.environ['VPM_GF_LOW'] = str(self.var_gf_low.get()).strip()
                os.environ['VPM_GF_HIGH'] = str(self.var_gf_high.get()).strip()
                os.environ['VPM_ZHL_GF_RAMP_ANCHOR'] = str(getattr(self, 'var_zhl_gf_ramp_anchor', None).get() if hasattr(self, 'var_zhl_gf_ramp_anchor') else 'SODZ').strip()
                os.environ['VPM_ZHL_GF_RAMP_HI_ANCHOR'] = str(getattr(self, 'var_zhl_gf_ramp_hi_anchor', None).get() if hasattr(self, 'var_zhl_gf_ramp_hi_anchor') else 'SURFACE').strip()
                os.environ['VPM_LAST_STOP_M'] = str(getattr(self, 'deco_last_stop_m', 3.0))
                # ZH-L16 variant + coeff tables (B/C) passed to MAIN via env (JSON).
                try:
                    os.environ['VPM_ZHL16_VARIANT'] = str(getattr(self, 'zhl16_variant', 'C') or 'C')
                    _vv = str(getattr(self, 'zhl16_variant', 'C') or 'C').strip().upper()
                    _rows = getattr(self, 'zhl16_coeffs_C', None) if _vv != 'B' else getattr(self, 'zhl16_coeffs_B', None)
                    if isinstance(_rows, list) and len(_rows) == 16:
                        os.environ['VPM_ZHL16_COEFFS_JSON'] = json.dumps(_rows)
                except Exception:
                    pass
            else:
                os.environ['VPM_DECO_MODEL'] = 'VPM'
                os.environ.pop('VPM_GF_LOW', None)
                os.environ.pop('VPM_GF_HIGH', None)
                os.environ.pop('VPM_ZHL_GF_RAMP_ANCHOR', None)
                os.environ.pop('VPM_ZHL_GF_RAMP_HI_ANCHOR', None)
                os.environ.pop('VPM_ZHL16_VARIANT', None)
                os.environ.pop('VPM_ZHL16_COEFFS_JSON', None)

            output = run_vpmb_subprocess(
                depth_m=depth,
                bottom_time_min=bottom,
                desc_rate=desc_rate,
                asc_rate=asc_rate,
                fo2=fo2,
                fhe=fhe,
                rapsol=rapsol,
                crit_rad_n2=crit_rad_n2,
                crit_rad_he=crit_rad_he,
                adv=self,           # parametri avanzati
                last_stop_m=float(getattr(self, 'deco_last_stop_m', 3.0)),
                gas_table=gas_table # 🔥 nuova tabella gas verso il main
            )
            # Freeze algorithm info for reporting/graph (do not follow later UI changes)
            try:
                self._last_calc_algo = _requested_algo
            except Exception:
                pass
        except RuntimeError as e:
            # Caso borderline: il main può emettere il warning tecnico Fortran:
            # "ERROR! OFF-GASSING GRADIENT IS TOO SMALL TO DECOMPRESS AT THE X STOP"
            # Qui NON alteriamo automaticamente i parametri: informiamo l'utente e offriamo un retry manuale
            # con rapsol + 0.01 (esperienza empirica: risolve alcuni casi limite BO-after-CC).
            msg = str(e)
            if "OFF-GASSING GRADIENT IS TOO SMALL TO DECOMPRESS" in msg:
                # prova a estrarre la quota dal messaggio (se presente)
                stop_m = None
                try:
                    m = re.search(r"OFF-GASSING GRADIENT IS TOO SMALL TO DECOMPRESS AT THE\s+([0-9.]+)", msg)
                    if m:
                        stop_m = float(m.group(1))
                except Exception:
                    stop_m = None

                rapsol_retry = round(float(rapsol) + 0.01, 3)
                hint = "\n\nSuggerimento operativo (casi borderline): riprovare incrementando solubility N2/He di +0.01."
                hint += f"\nsolubility N2/He attuale: {float(rapsol):.3f}  ->  Retry: {rapsol_retry:.3f}"
                if stop_m is not None:
                    hint = f"\n\nStop segnalato dal motore: {stop_m:.1f} m." + hint

                do_retry = messagebox.askretrycancel(

                    "Warning VPMB (gradiente off-gassing troppo piccolo)",
                    msg + hint + "\n\nVuoi riprovare ora con Solubility N2/He +0.01?"
                )
                if do_retry:
                    try:
                        output = run_vpmb_subprocess(
                            depth_m=depth,
                            bottom_time_min=bottom,
                            desc_rate=desc_rate,
                            asc_rate=asc_rate,
                            fo2=fo2,
                            fhe=fhe,
                            rapsol=rapsol_retry,
                            crit_rad_n2=crit_rad_n2,
                            crit_rad_he=crit_rad_he,
                            adv=self,
                            last_stop_m=float(getattr(self, 'deco_last_stop_m', 3.0)),
                            gas_table=gas_table
                        )
                        # Nota: NON cambiamo il campo solubility N2/He in GUI; segnaliamo solo che il calcolo è stato eseguito in retry.
                        try:
                            self._last_retry_rapsol_effective = float(rapsol_retry)
                        except Exception:
                            pass
                        try:
                            messagebox.showinfo(
                                "Retry eseguito",
                                f"Calcolo completato rieseguendo con Solubility N2/He={rapsol_retry:.3f} (solo per questo run)."
                            )
                        except Exception:
                            pass
                    except Exception as e2:
                        messagebox.showerror("Errore VPMB", str(e2))
                        return
                else:
                    return
            else:
                messagebox.showerror("Errore VPMB", msg)
                return

        # -----------------------------
        # 3) Parsing output
        # -----------------------------
        # Profilo dettagliato (GUI): include discesa a bande + soste (DSTOP) + ascesa/stop coerenti con i dati del motore
        try:
            stops = None
            if hasattr(self, "last_vpmb_result") and isinstance(self.last_vpmb_result, dict):
                stops = self.last_vpmb_result.get("stops")
        except Exception:
            stops = None

        try:
            profile_rows = build_profile_rows_from_result(
                depth_bottom=depth,
                bottom_time_min=bottom,
                descent_bands=getattr(self, "descent_speed_bands", []),
                ascent_bands=getattr(self, "ascent_speed_bands", []),
                stops=stops,
                runtime_total=(float(self.last_vpmb_result.get("runtime_total", 0.0)) if (hasattr(self, "last_vpmb_result") and isinstance(self.last_vpmb_result, dict)) else None),
                engine_profile=(getattr(self, "last_engine_profile", None) if hasattr(self, "last_engine_profile") else None),
            )
        except Exception as e:
            # FAIL-FAST: no fallback schedule reconstruction allowed.
            try:
                import tkinter.messagebox as _mb
                prof_len = None
                try:
                    prof_len = len(getattr(self, "last_engine_profile", None) or [])
                except Exception:
                    prof_len = None
                _mb.showerror(
                    "Errore profilo MAIN",
                    f"Impossibile costruire il report: engine_profile mancante o non valido.\n\nDettaglio: {e}\n\nlen(engine_profile)={prof_len}"
                )
            except Exception:
                pass
            return

        # Allinea il runtime finale al valore del motore SOLO sull'ultimo segmento (tipicamente ASC finale),
        # senza alterare gli STOP (che devono restare identici al motore).
        try:
            rt_engine = None
            if hasattr(self, "last_vpmb_result") and isinstance(self.last_vpmb_result, dict):
                rt_engine_val = float(self.last_vpmb_result.get("runtime_total", 0.0))
                if rt_engine_val > 0.0:
                    rt_engine = rt_engine_val
            if profile_rows and rt_engine is not None:
                rt_gui = float(profile_rows[-1].get("Run_time_min", 0.0))
                if abs(rt_gui - rt_engine) > 1e-6:
                    # Solo se l'ultimo segmento è ASC verso superficie possiamo correggere la durata
                    last = profile_rows[-1]
                    if last.get("Tipo") == "ASC" and float(last.get("Depth_to_m") or 0.0) == 0.0 and len(profile_rows) >= 2:
                        prev_rt = float(profile_rows[-2].get("Run_time_min", 0.0))
                        seg_new = rt_engine - prev_rt
                        if seg_new >= 0.0:
                            last["Seg_time_min"] = seg_new
                            last["Run_time_min"] = rt_engine
        except Exception:
            pass



        # -----------------------------
        # 3a) Post-processing report: merge segmenti consecutivi ASC/DESC con stessa velocita' (rate)
        # Regola: unire SOLO se consecutivi e senza eventi intermedi (STOP/CONST/STOPV/DSTOP ecc.).
        # - Si uniscono solo righe adiacenti entrambe di tipo ASC oppure entrambe di tipo DESC
        # - Stesso rate numerico (estratto da note "rate=...")
        # - Contiguita' di profondita': Depth_to(prev) == Depth_from(curr) (tolleranza minima)
        # - Stesso mode e stesso gas
        # NOTA: non si toccano mai i segmenti STOP/CONST ecc.; non si attraversano.
        def _parse_rate_note(note_val):
            try:
                s = str(note_val or '')
                k = s.find('rate=')
                if k < 0:
                    return None
                t = s[k+5:]
                # termina su spazio o ';' se presente
                for sep in [';', ' ', ',']:
                    j = t.find(sep)
                    if j > 0:
                        t = t[:j]
                        break
                return float(t)
            except Exception:
                return None

        def _merge_consecutive_same_rate(rows):
            if not rows:
                return rows
            out = []
            tol = 1e-9
            for r in rows:
                try:
                    tipo = str(r.get('Tipo','')).upper()
                except Exception:
                    tipo = ''
                if tipo not in ('ASC','DESC'):
                    out.append(r)
                    continue
                if not out:
                    out.append(r)
                    continue
                prev = out[-1]
                try:
                    prev_tipo = str(prev.get('Tipo','')).upper()
                except Exception:
                    prev_tipo = ''
                if prev_tipo != tipo:
                    out.append(r)
                    continue

                # vincoli per merge
                try:
                    prev_rate = _parse_rate_note(prev.get('Note'))
                    cur_rate = _parse_rate_note(r.get('Note'))
                except Exception:
                    prev_rate = None
                    cur_rate = None
                if prev_rate is None or cur_rate is None or abs(prev_rate - cur_rate) > 1e-12:
                    out.append(r)
                    continue

                if str(prev.get('Mode','')) != str(r.get('Mode','')):
                    out.append(r)
                    continue
                if str(prev.get('Gas','')) != str(r.get('Gas','')):
                    out.append(r)
                    continue

                # contiguita' profondita'
                try:
                    prev_to = float(prev.get('Depth_to_m'))
                    cur_from = float(r.get('Depth_from_m'))
                except Exception:
                    out.append(r)
                    continue
                if abs(prev_to - cur_from) > tol:
                    out.append(r)
                    continue

                # MERGE: somma tempi, estendi a fine corrente, aggiorna runtime finale.
                try:
                    prev_seg = float(prev.get('Seg_time_min', 0.0))
                    cur_seg = float(r.get('Seg_time_min', 0.0))
                except Exception:
                    prev_seg = 0.0
                    cur_seg = 0.0
                prev['Seg_time_min'] = prev_seg + cur_seg

                # aggiorna profondita' finale e runtime
                prev['Depth_to_m'] = r.get('Depth_to_m')
                prev['Run_time_min'] = r.get('Run_time_min')

                # Ricalcola depth_avg per report (semplice media dei capi)
                try:
                    prev_from = float(prev.get('Depth_from_m'))
                    prev_to2 = float(prev.get('Depth_to_m'))
                    prev['Depth_avg_m'] = (prev_from + prev_to2) / 2.0
                except Exception:
                    pass

                # Campi "di stato": se differiscono, svuotali per evitare informazioni errate
                for fld in ('ppO2','Gas_dens_gL','EAD_m'):
                    try:
                        a = prev.get(fld)
                        b = r.get(fld)
                        if a is None:
                            a = ''
                        if b is None:
                            b = ''
                        if str(a) != str(b):
                            prev[fld] = ''
                    except Exception:
                        pass

                # Campi cumulativi (a fine segmento): mantieni l'ultimo
                for fld in ('CNS_%','OTU'):
                    try:
                        prev[fld] = r.get(fld)
                    except Exception:
                        pass

                # Note, mode, gas, tipo restano quelli del prev (identici per definizione)

            # rinumerazione n
            for i, rr in enumerate(out, start=1):
                rr['n'] = i
            return out

        try:
            profile_rows = _merge_consecutive_same_rate(profile_rows)
        except Exception:
            pass

        # Profilo tessuti VPM-B (BOTT + ASC/STOP deco)
        tissue_rows = parse_tissue_profile(output)

        # Runtime totale e runtime deco
        if profile_rows:
            # Runtime: preferisci il valore numerico del motore (robusto anche con Minimum_Deco_Stop_Time < 1)
            total_runtime = None
            try:
                if hasattr(self, "last_vpmb_result") and isinstance(self.last_vpmb_result, dict):
                    total_runtime = float(self.last_vpmb_result.get("runtime_total"))
            except Exception:
                total_runtime = None
            if total_runtime is None:
                total_runtime = profile_rows[-1]["Run_time_min"]

            # Il tempo di inizio deco è il runtime a fine BOTTOM (start of ascent).
            # STRADA A: nel profilo passivo del MAIN il fondo è tipicamente 'CONST' (non 'BOTT').
            # Quindi: deco_start_rt = runtime del segmento immediatamente precedente al primo 'ASC'.
            deco_start_rt = None
            try:
                first_asc_idx = None
                for j, r in enumerate(profile_rows):
                    if str(r.get("Tipo", "")).upper() == "ASC":
                        first_asc_idx = j
                        break
                if first_asc_idx is not None and first_asc_idx > 0:
                    deco_start_rt = profile_rows[first_asc_idx - 1].get("Run_time_min")
                else:
                    # fallback legacy: se esiste un BOTT esplicito (profili non-STRADA A)
                    for r in profile_rows:
                        if str(r.get("Tipo", "")).upper() == "BOTT":
                            deco_start_rt = r.get("Run_time_min")
                            break
            except Exception:
                deco_start_rt = None

            if deco_start_rt is not None:
                total_deco = total_runtime - deco_start_rt
                self.label_deco.config(text=f"{total_deco:.1f}")
                self.label_runtime.config(text=f"{total_runtime:.1f}")
            else:
                self.label_deco.config(text="(non trovato)")
                self.label_runtime.config(text=f"{total_runtime:.1f}")

            # Profondità di inizio zona deco dal risultato numerico del motore
            depth_deco = None
            try:
                if hasattr(self, "last_vpmb_result") and isinstance(self.last_vpmb_result, dict):
                    depth_deco = self.last_vpmb_result.get("depth_start_of_deco_zone")
            except Exception:
                depth_deco = None

            if depth_deco is not None:
                try:
                    self.label_decozone.config(text=f"{float(depth_deco):.1f}")
                except Exception:
                    self.label_decozone.config(text="(n/d)")
            else:
                self.label_decozone.config(text="(non trovato)")
        else:
            self.label_deco.config(text="(non trovato)")
            self.label_runtime.config(text="(non trovato)")
            self.label_decozone.config(text="(non trovato)")

        
        # -----------------------------
        # 3b) Metriche operative (post-processing GUI)
        # -----------------------------
        def _depth_mean_for_row(r):
            """Depth mean for a profile row.

            Supports both key styles:
            - Main-style: Tipo / Depth_from_m / Depth_to_m / Depth_m
            - GUI/CSV-style: tipo / from / to / depth
            """
            tipo_local = str(r.get("Tipo", r.get("tipo", ""))).strip().upper()

            def _f(key1, key2, default=0.0):
                try:
                    v = r.get(key1, None)
                    if v is None and key2 is not None:
                        v = r.get(key2, None)
                    if v is None or v == "":
                        return float(default)
                    return float(v)
                except Exception:
                    return float(default)

            if tipo_local in ("ASC", "DESC"):
                z1 = _f("Depth_from_m", "from", 0.0)
                z2 = _f("Depth_to_m", "to", 0.0)
                return 0.5 * (z1 + z2)
            else:
                return _f("Depth_m", "depth", 0.0)


        # -----------------------------
        # FASE 3 — ppO₂ (col 11) in CC: SP_target con clampatura doppia unica
        # ppO2_CC = min(max(SP_target, Pamb_media_segmento * FO2_dil), Pamb_media_segmento)
        # - Se Multi-SP OFF: SP_target = SP singolo
        # - Se Multi-SP ON:
        #   - fino a inizio fondo (incl. soste in discesa): SP discesa
        #   - fondo: SP fondo
        #   - risalita/deco: pickup tra Deco1/Deco2/Deco3 (in base alla quota media del segmento)
        # -----------------------------
        def _safe_float(_x, _default=0.0):
            """Robust float conversion for GUI model values (StringVar/Entry strings).

            - Reads from .get() if present (StringVar-like)
            - Trims whitespace
            - Does NOT change decimal separator (internal GUI should already use '.')
            """
            try:
                if hasattr(_x, "get"):
                    _x = _x.get()
                s = str(_x).strip()
                if s == "":
                    return float(_default)
                return float(s)
            except Exception:
                return float(_default)

        # inferenza deterministica del tratto di BOTTOM (non esiste un "BOTT" esplicito in tabella)
        # - BOTTOM = blocco contiguo più "lungo" (in minuti) vicino alla profondità massima dell’immersione
        # - INIZIO RISALITA = primo segmento ASC dopo il bottom (da qui inizia il pickup Deco1/2/3)
        def _infer_bottom_and_ascent_start(_rows):
            # BOTTOM non è marcato: va inferito come il/i segmento/i CONST (prof costante) alla massima profondità.
            # Inizio risalita: primo segmento ASC dopo il bottom (da qui parte il pickup Deco1/2/3).
            if not _rows:
                return (0, 0, 0)

            # candidati bottom: Tipo=CONST e profondità costante (Depth_from≈Depth_to), profondità = Depth_m
            _cand = []
            for _i, _r in enumerate(_rows):
                _tip = str(_r.get("Tipo", _r.get("tipo",""))).strip().upper()
                if _tip != "CONST":
                    continue
                try:
                    _z_from = float(_r.get("Depth_from_m") or _r.get("from") or 0.0)
                    _z_to = float(_r.get("Depth_to_m") or _r.get("to") or 0.0)
                except Exception:
                    _z_from = 0.0
                    _z_to = 0.0
                # CONST in tabella normalmente ha from==to; in ogni caso usiamo Depth_m come quota del segmento
                if abs(_z_from - _z_to) > 1e-6 and (_z_from != 0.0 or _z_to != 0.0):
                    # se qualcuno mette CONST ma con from/to diversi, lo ignoriamo per evitare falsi bottom
                    continue
                try:
                    _z = float(_r.get("Depth_m") or _r.get("depth") or 0.0)
                except Exception:
                    _z = 0.0
                _cand.append((_i, _z))

            # se non troviamo candidati, fallback: usa il punto di massima profondità (meno robusto, ma evita crash)
            if not _cand:
                _zmeans = [_depth_mean_for_row(_r) for _r in _rows]
                _dmax = max(_zmeans) if _zmeans else 0.0
                _bottom_start = int(_zmeans.index(_dmax)) if _zmeans else 0
                _bottom_end = _bottom_start
            else:
                _bottom_depth = max(_z for _, _z in _cand)

                # tolleranza metrica per match della profondità del bottom (per rumore/arrotondamenti)
                _tol = max(0.3, 0.01 * float(_bottom_depth or 0.0))

                # BOTTOM = segmento CONST (prof costante) alla profondità massima.
                # Se ci sono più CONST alla stessa profondità (entro tolleranza), scegliamo quello
                # con durata (Seg_time_min) maggiore; a parità, quello più "tardo" (indice maggiore).
                _cand_max = []
                for _i, _z in _cand:
                    if _z >= (_bottom_depth - _tol):
                        try:
                            _tmin = float(_rows[_i].get("Seg_time_min") or 0.0)
                        except Exception:
                            _tmin = 0.0
                        _cand_max.append((_i, _tmin))

                if _cand_max:
                    _bottom_start = max(_cand_max, key=lambda x: (x[1], x[0]))[0]
                else:
                    # estrema difesa: prendi l'indice del candidato più profondo
                    _bottom_start = max(_cand, key=lambda x: x[1])[0]

# bottom_end = estendi solo su CONST contigui allo stesso livello (entro tolleranza)
                _bottom_end = _bottom_start
                for _j in range(_bottom_start + 1, len(_rows)):
                    if str(_rows[_j].get("Tipo", "")).upper() != "CONST":
                        break
                    try:
                        _zj = float(_rows[_j].get("Depth_m") or 0.0)
                    except Exception:
                        _zj = 0.0
                    if _zj < (_bottom_depth - _tol):
                        break
                    _bottom_end = _j

            # inizio risalita: primo ASC dopo bottom_end
            _ascent_start = _bottom_end + 1
            for _j in range(_bottom_end + 1, len(_rows)):
                if str(_rows[_j].get("Tipo", _rows[_j].get("tipo",""))).strip().upper() == "ASC":
                    _ascent_start = _j
                    break
            if _ascent_start >= len(_rows):
                _ascent_start = len(_rows)

            return (_bottom_start, _bottom_end, _ascent_start)

        _idx_bottom_start, _idx_bottom_end, _idx_ascent_start = _infer_bottom_and_ascent_start(profile_rows)

        def _cc_sp_target_for_row(_i, _r):
            # Multi-SP ON/OFF
            try:
                _use_msp = bool(self.cc_msp_enabled.get()) if hasattr(self, "cc_msp_enabled") else False
            except Exception:
                _use_msp = False

            if not _use_msp:
                # mono-SP: SP singolo
                try:
                    return _safe_float(self.cc_sp_single.get())
                except Exception:
                    return 0.0

            # multi-SP: setpoint per fase
            try:
                # Se lo SP di discesa non è valorizzato, fallback deterministico a SP singolo
                # Fallback robusto: se SP singolo è vuoto (input legacy) usa SP fondo; altrimenti default 0.9
                _base_sp = _safe_float(self.cc_sp_single.get(), 0.0)
                if _base_sp <= 0.0:
                    _base_sp = _safe_float(self.cc_sp_bottom.get(), 0.9)
                _sp_des = _safe_float(self.cc_sp_descent.get(), _base_sp)
                if _sp_des <= 0.0:
                    _sp_des = _base_sp
            except Exception:
                _sp_des = _safe_float(self.cc_sp_single.get(), 0.0)

            try:
                # Regola baseline: sul fondo si usa SEMPRE lo SP singolo (anche se Multi-SP è attivo).
                _sp_bot = _safe_float(self.cc_sp_single.get())
            except Exception:
                _sp_bot = 0.0

            # se qualche SP deco non è valorizzato, fallback deterministico a quello precedente
            try:
                _sp_d1 = _safe_float(self.cc_sp_deco1.get(), _sp_bot)
            except Exception:
                _sp_d1 = _sp_bot
            try:
                _sp_d2 = _safe_float(self.cc_sp_deco2.get(), _sp_d1)
            except Exception:
                _sp_d2 = _sp_d1
            try:
                _sp_d3 = _safe_float(self.cc_sp_deco3.get(), _sp_d2)
            except Exception:
                _sp_d3 = _sp_d2

            try:
                _a1 = _safe_float(self.cc_deco1_a.get(), 0.0)
            except Exception:
                _a1 = 0.0
            try:
                _a2 = _safe_float(self.cc_deco2_a.get(), 0.0)
            except Exception:
                _a2 = 0.0

            # 2a) Discesa: dall'inizio immersione a inizio fondo (incluse eventuali soste in discesa)
            if _i < _idx_bottom_start:
                return _sp_des

            # 2b) Fondo: dal bottom_start fino al primo ASC dopo il bottom (inizio risalita)
            if _i < _idx_ascent_start:
                return _sp_bot

            # 2c) Risalita / deco (da primo ASC dopo bottom): pickup tra deco1/2/3
            _zmean = _depth_mean_for_row(_r)
            if _zmean > _a1:
                return _sp_d1
            elif _zmean > _a2:
                return _sp_d2
            else:
                return _sp_d3

        def _cc_ppO2_for_row(_i, _r):
            _pamb = p_abs_atm(_depth_mean_for_row(_r))
            try:
                _fo2_dil = _safe_float(self.cc_dil_fo2.get(), 0.0) if hasattr(self, "cc_dil_fo2") else 0.0
            except Exception:
                _fo2_dil = 0.0
            _sp_target = _cc_sp_target_for_row(_i, _r)
            return min(max(_sp_target, _pamb * _fo2_dil), _pamb)


        cns_cum = 0.0
        otu_cum = 0.0

        # Pre-lettura frazioni del DILUENTE CCR (usate solo se Mode == 'CC')
        _cc_dil_fo2 = None
        _cc_dil_fhe = None
        _cc_dil_fn2 = None
        try:
            if hasattr(self, "entry_cc_dil_fo2") and hasattr(self, "entry_cc_dil_fhe"):
                _cc_dil_fo2 = float(str(self.entry_cc_dil_fo2.get()).strip().replace(",", "."))
                _cc_dil_fhe = float(str(self.entry_cc_dil_fhe.get()).strip().replace(",", "."))
                _cc_dil_fn2 = max(0.0, 1.0 - float(_cc_dil_fo2) - float(_cc_dil_fhe))
        except Exception:
            _cc_dil_fo2 = None
            _cc_dil_fhe = None
            _cc_dil_fn2 = None

        t_cum = 0.0
        zt_cum = 0.0
        prev_fn2_eff = None

        for _i, r in enumerate(profile_rows):
            seg_min = float(r.get("Seg_time_min") or 0.0)
            zmean = _depth_mean_for_row(r)
            pamb = p_abs_atm(zmean)  # Pamb_media del segmento (ata ≈ bar)

            # Gas attivo (coerente con colonna GAS) + ricostruzione deterministica ppN2/ppHe
            t = str(r.get("Tipo", "")).upper()
            mode_r = str(r.get("Mode", "")).upper()

            ppO2 = None
            ppN2 = None
            ppHe = None
            fn2_eff = None

            if mode_r == "CC":
                # ppO2 in CC: setpoint target (già corretto altrove)
                try:
                    ppO2 = float(_cc_ppO2_for_row(_i, r))
                except Exception:
                    ppO2 = None

                # Ricostruzione loop: FO2_loop = ppO2/Pamb; inerti ripartiti secondo inerti del DIL
                try:
                    if ppO2 is None or pamb <= 0.0:
                        ppN2 = 0.0
                        ppHe = 0.0
                        fn2_eff = 0.0
                    else:
                        fo2_loop = ppO2 / pamb
                        # clamp fisico
                        if fo2_loop < 0.0:
                            fo2_loop = 0.0
                        if fo2_loop > 1.0:
                            fo2_loop = 1.0

                        finert = 1.0 - fo2_loop
                        if finert < 0.0:
                            finert = 0.0

                        dil_fn2 = _cc_dil_fn2 if _cc_dil_fn2 is not None else 0.0
                        dil_fhe = _cc_dil_fhe if _cc_dil_fhe is not None else 0.0
                        denom = dil_fn2 + dil_fhe

                        if denom > 0.0 and finert > 0.0:
                            fn2_loop = finert * (dil_fn2 / denom)
                            fhe_loop = finert * (dil_fhe / denom)
                        else:
                            fn2_loop = 0.0
                            fhe_loop = 0.0

                        ppN2 = fn2_loop * pamb
                        ppHe = fhe_loop * pamb
                        fn2_eff = fn2_loop
                except Exception:
                    ppN2 = 0.0
                    ppHe = 0.0
                    fn2_eff = 0.0
            else:
                # OC / BO: usa il gas effettivamente respirato (colonna GAS già calcolata e mostrata)
                # (fix: evita di ricalcolare via depth picker nei CONST di discesa; si appoggia alla colonna GAS)
                gas_label = ""
                try:
                    # Usa la stessa logica della tabella (colonna GAS) per determinare il gas effettivo del segmento
                    cols_tmp = self._format_profile_row_for_tree(r, gas_table, show_metrics=False)
                    gas_label = str(cols_tmp[9] if len(cols_tmp) > 9 else "").strip().lstrip("'")
                except Exception:
                    gas_label = ""

                g = None
                try:
                    if gas_table and gas_label:
                        if gas_label.startswith("BOTT_"):
                            g = gas_table[0]
                        elif gas_label.startswith("DEC"):
                            # DEC1_, DEC2_, ... -> indice 1..4 nella gas_table (0=BOTT)
                            m_dec = re.match(r"DEC(\d+)_", gas_label)
                            if m_dec:
                                di = int(m_dec.group(1))
                                if 0 <= di < len(gas_table):
                                    g = gas_table[di]
                        elif gas_label.startswith("BO_"):
                            # BO_1_, ... -> indice 0.. nella tabella bailout (se disponibile)
                            m_bo = re.match(r"BO_(\d+)_", gas_label)
                            if m_bo:
                                bi = int(m_bo.group(1)) - 1
                                bo_table = []
                                try:
                                    if hasattr(self, "bailout_rows"):
                                        for rr in self.bailout_rows:
                                            try:
                                                bo_table.append({
                                                    "FO2": float(str(rr.get("fo2").get()).strip().replace(",", ".")),
                                                    "FHe": float(str(rr.get("fhe").get()).strip().replace(",", ".")),
                                                    "MOD": float(str(rr.get("mod").get()).strip().replace(",", ".")),
                                                    "enabled": bool(rr.get("enabled_var").get()) if rr.get("enabled_var") is not None else False,
                                                })
                                            except Exception:
                                                continue
                                except Exception:
                                    bo_table = []
                                if bo_table and 0 <= bi < len(bo_table):
                                    g = bo_table[bi]
                    # fallback: comportamento precedente (depth picker)
                    if g is None:
                        if t == "DESC":
                            g = gas_table[0] if gas_table else None
                        else:
                            if t in ("ASC", "DESC"):
                                z_for_gas = float(r.get("Depth_from_m") or r.get("Depth_to_m") or 0.0)
                            else:
                                z_for_gas = float(r.get("Depth_m") or 0.0)
                            g = self.pick_gas_for_depth(z_for_gas, gas_table) if gas_table else None
                except Exception:
                    g = gas_table[0] if gas_table else None

                fo2_m = float(g.get("FO2", 0.0)) if g else 0.0
                fhe_m = float(g.get("FHe", 0.0)) if g else 0.0
                fn2_m = max(0.0, 1.0 - fo2_m - fhe_m)

                ppO2 = fo2_m * pamb
                ppN2 = fn2_m * pamb
                ppHe = fhe_m * pamb
                fn2_eff = fn2_m

            # Gas Density: sempre da ppO2 + ppN2 + ppHe (gas ideali, T costante già usata in GUI)
            try:
                _ppO2 = float(ppO2) if ppO2 is not None else 0.0
                _ppN2 = float(ppN2) if ppN2 is not None else 0.0
                _ppHe = float(ppHe) if ppHe is not None else 0.0
                dens = (_ppO2 * MW_O2 + _ppN2 * MW_N2 + _ppHe * MW_He) / MOLAR_VOL_L  # g/L
            except Exception:
                dens = 0.0

            # ΔppN2_iso: usa sempre la frazione N2 effettiva del segmento (loop in CC)
            try:
                if prev_fn2_eff is None or fn2_eff is None:
                    icd = 0.0
                else:
                    icd = (p_abs_atm(zmean) - 0.0493) * (float(fn2_eff) - float(prev_fn2_eff))
            except Exception:
                icd = 0.0
            prev_fn2_eff = fn2_eff

            # EAD: esclusivamente da ppN2 (coerente con GUI)
            try:
                _ppN2 = float(ppN2) if ppN2 is not None else 0.0
                ead = (_ppN2 / 0.79 - 1.0) * 10.0
            except Exception:
                ead = 0.0

            # CNS/OTU cumulativi (ppO2 già corretto in CC; in OC/BO = FO2*Pamb_media)
            try:
                _ppO2 = float(ppO2) if ppO2 is not None else 0.0
            except Exception:
                _ppO2 = 0.0
            cns_cum += cns_increment_percent(seg_min, _ppO2)
            otu_cum += otu_increment(seg_min, _ppO2)

            t_cum += seg_min
            zt_cum += (zmean * seg_min)
            depth_cum = (zt_cum / t_cum) if (t_cum > 1e-12) else zmean

            r["Depth_avg"] = depth_cum
            r["Gas_dens_gL"] = dens
            r["CNS_%"] = cns_cum
            r["OTU"] = otu_cum
            r["ΔppN2_iso"] = icd
            r["EAD_m"] = ead
        # Profondità media finale (ultimo segmento del profilo dettagliato, colonna Depth_avg)
        try:
            if hasattr(self, "label_final_depthavg"):
                if profile_rows and profile_rows[-1].get("Depth_avg", None) is not None:
                    self.label_final_depthavg.config(text=f"{float(profile_rows[-1].get('Depth_avg')):.1f}")
                else:
                    self.label_final_depthavg.config(text="(n/d)")
        except Exception:
            try:
                if hasattr(self, "label_final_depthavg"):
                    self.label_final_depthavg.config(text="(n/d)")
            except Exception:
                pass


        # Seconda riga risultati: max Gas Density e CNS/OTU finali dal profilo dettagliato
        try:
            if profile_rows:
                # Max Gas density (fondo)
                try:
                    _gd_vals = []
                    for _r in profile_rows:
                        _v = _r.get("Gas_dens_gL", None)
                        if _v is None or _v == "":
                            continue
                        _gd_vals.append(float(_v))
                    _gd_max = max(_gd_vals) if _gd_vals else 0.0
                except Exception:
                    _gd_max = 0.0

                if hasattr(self, "label_max_gasdens"):
                    self.label_max_gasdens.config(text=f"{_gd_max:.2f}")

                # CNS/OTU finali (ultima riga)
                try:
                    _cns_fin = float(profile_rows[-1].get("CNS_%", 0.0))
                except Exception:
                    _cns_fin = 0.0
                try:
                    _otu_fin = float(profile_rows[-1].get("OTU", 0.0))
                except Exception:
                    _otu_fin = 0.0

                if hasattr(self, "label_final_cns"):
                    self.label_final_cns.config(text=f"{_cns_fin:.0f}")
                if hasattr(self, "label_final_otu"):
                    self.label_final_otu.config(text=f"{_otu_fin:.0f}")
            else:
                if hasattr(self, "label_max_gasdens"):
                    self.label_max_gasdens.config(text="(n/d)")
                if hasattr(self, "label_final_cns"):
                    self.label_final_cns.config(text="(n/d)")
                if hasattr(self, "label_final_otu"):
                    self.label_final_otu.config(text="(n/d)")
        except Exception:
            pass


# -----------------------------
        # 4) Riempi tabella GUI
        # -----------------------------
        # Etichetta gas (es. 18/45)
        # Il gas verrà calcolato per ogni riga in base alla profondità (multigas)

        tissue_rows = tissue_rows or []
        deco_idx = 0  # indice per le righe deco nei tessuti

        for _i, row in enumerate(profile_rows):
            tipo = row["Tipo"]

            # Aggancio eventuale riga tessuti (stessa logica dell'export CSV)
            if tipo == "BOTT":
                # primo elemento dei tessuti = fondo
                if len(tissue_rows) > 0:
                    trow = tissue_rows[0]
                else:
                    trow = None
            elif tipo in ("ASC", "STOP"):
                # righe successive di deco usano tissue_rows[1..]
                deco_idx += 1
                if deco_idx < len(tissue_rows):
                    trow = tissue_rows[deco_idx]
                else:
                    trow = None
            else:  # DESC
                trow = None

            # Profondità da mostrare
            if tipo in ("ASC", "DESC"):
                d_from = row["Depth_from_m"]
                d_to = row["Depth_to_m"]
                depth = ""
            else:  # STOP o BOTT
                d_from = ""
                d_to = ""
                depth = row["Depth_m"]

            # ppO2 (col 11): reportistica deterministica, NON dipende dal MAIN.
            # CC -> SP_target clampato (già definito in _cc_ppO2_for_row)
            # OC/BO -> FO2(gas effettivo del segmento) * Pamb_media_segmento
            mode_row = str(row.get("Mode", "")).upper()

            # profondità media del SEGMENTO per Pamb_media
            try:
                if tipo in ("ASC", "DESC"):
                    _z1 = float(row.get("Depth_from_m") or 0.0)
                    _z2 = float(row.get("Depth_to_m") or 0.0)
                    _zmean_seg = 0.5 * (_z1 + _z2)
                else:
                    _zmean_seg = float(row.get("Depth_m") or 0.0)
            except Exception:
                _zmean_seg = 0.0

            try:
                _pamb_seg = p_abs_atm(_zmean_seg)
            except Exception:
                _pamb_seg = (_zmean_seg / 10.0 + 1.0)

            ppO2_val = None

            if mode_row == "CC":
                try:
                    ppO2_val = float(_cc_ppO2_for_row(_i, row))
                except Exception:
                    ppO2_val = None
            else:
                # OC / BO: gas effettivamente respirato nel segmento
                try:
                    # fino a inizio fondo (incluse eventuali soste in discesa): sempre gas di fondo
                    if _i < _idx_bottom_start:
                        _g = gas_table[0] if gas_table else None
                    else:
                        if tipo in ("DESC","DSTOP","BOTT"):
                            _g = gas_table[0] if gas_table else None
                        else:
                            # ASC usa quota FROM; STOP/CONST usa depth del segmento
                            if tipo in ("ASC", "DESC"):
                                _z_for_gas = float(row.get("Depth_from_m") or row.get("Depth_to_m") or 0.0)
                            else:
                                _z_for_gas = float(row.get("Depth_m") or 0.0)
                            _g = self.pick_gas_for_depth(_z_for_gas, gas_table) if gas_table else None

                    _fo2 = float(_g.get("FO2", 0.0)) if _g else 0.0
                    ppO2_val = _fo2 * _pamb_seg
                except Exception:
                    ppO2_val = None

            # Salva sia ppO2_bar (compatibilità) sia ppO2 (tabella/grafico/csv)
            row["ppO2_bar"] = ppO2_val
            row["ppO2"] = ppO2_val


            # Seleziona il gas attivo per questa riga (stessa logica del main)
            # Per ASC/DESC usiamo la quota di partenza, per STOP/BOTT la quota dello stop.
            if tipo in ("ASC", "DESC"):
                depth_for_gas = d_from if d_from not in ("", None) else d_to
            else:
                depth_for_gas = depth

            try:
                depth_for_gas_val = float(depth_for_gas) if depth_for_gas not in ("", None) else 0.0
            except (TypeError, ValueError):
                depth_for_gas_val = 0.0

            # Determinazione gas per segmento
            if tipo in ("DESC","DSTOP","BOTT"):
                # gas di fondo
                gas_dict = gas_table[0]
            elif tipo == "ASC":
                gas_dict = self.pick_gas_for_depth(depth_for_gas_val, gas_table)
            else:  # STOP o BOTT
                gas_dict = self.pick_gas_for_depth(depth_for_gas_val, gas_table)

            # Etichetta protetta per Excel (es. '10/70)
            gas_label = f"'{self.format_gas_label(gas_dict)}"

        # Salvo ultimo profilo/tessuti per eventuale export
        self.last_profile_rows = profile_rows
        self.last_tissue_rows = tissue_rows

        # Aggiorna consumo e bar END (solo GUI) dopo aver costruito il profilo
        try:
            self._update_gas_pressure_consumption()
        except Exception:
            pass



        # Aggiorna tabella (base/estesa) in base al toggle
        self.refresh_profile_table()
        self.update_plot()

        # Assicura aggiornamento ΔppN2 (formule invariate): copre CC-BO su Calcola deco
        self._refresh_delta_ppn2_ui()


    
    def load_inputs_from_file(self):
        """Ricarica gli ultimi input salvati (se presenti) e li applica ai widget."""
        data = load_last_inputs()
        if not isinstance(data, dict):
            return

        # Modalità OC/CC
        try:
            if "mode" in data and hasattr(self, "mode_var"):
                self.mode_var.set(str(data.get("mode") or "OC"))
                # refresh UI if handler exists
                if hasattr(self, "_on_mode_change"):
                    try:
                        self._on_mode_change()
                    except Exception:
                        pass
        except Exception:
            pass

        # Input immersione base
        for key, entry_attr in [
            ("depth", "entry_depth"),
            ("bottom", "entry_bottom"),
            ("rapsol", "entry_rapsol"),
            ("crit_rad_n2", "entry_crit_rad_n2"),
            ("crit_rad_he", "entry_crit_rad_he"),
        ]:
            try:
                if key in data and hasattr(self, entry_attr):
                    e = getattr(self, entry_attr)
                    e.delete(0, tk.END)
                    v = data.get(key, "")
                    # Solubility N2/He: display with 2 decimals (presentation only)
                    if key == "rapsol":
                        try:
                            v = f"{float(v):.2f}"
                        except Exception:
                            v = str(v)
                    e.insert(0, str(v))
            except Exception:
                pass

        # Opzioni
        try:
            if "use_bailout_ascent" in data and hasattr(self, "chk_use_bailout"):
                self.chk_use_bailout.set(bool(data.get("use_bailout_ascent")))
        except Exception:
            pass
        # debug_projected_ascent removed in production

        # Parametri deco condivisi
        try:
            if "deco_last_stop_m" in data:
                self.deco_last_stop_m = float(data.get("deco_last_stop_m"))
        except Exception:
            pass
        self.deco_desc_speed_profile = str(data.get("deco_desc_profile", getattr(self, "deco_desc_speed_profile", "Standard (20 m/min)")))
        self.deco_asc_speed_profile = str(data.get("deco_asc_profile", getattr(self, "deco_asc_speed_profile", "Standard (9 m/min)")))

        # Bande velocità / soste (se presenti)
        try:
            if isinstance(data.get("descent_speed_bands"), list):
                self.descent_speed_bands = [dict(b) for b in data.get("descent_speed_bands")]
            if isinstance(data.get("ascent_speed_bands"), list):
                self.ascent_speed_bands = [dict(b) for b in data.get("ascent_speed_bands")]
        except Exception:
            pass

        # Soste volontarie in risalita (STOPV)
        try:
            sv = data.get("stopv_minutes_by_depth", {})
            if isinstance(sv, dict):
                # normalizza chiavi a int e valori a float >=0
                new_sv = {}
                for k, v in sv.items():
                    try:
                        dk = int(float(k))
                    except Exception:
                        continue
                    try:
                        dv = float(str(v).replace(",", "."))
                    except Exception:
                        dv = 0.0
                    if dv < 0.0:
                        dv = 0.0
                    new_sv[dk] = float(dv)

                # applica solo alle quote note (se non inizializzate, crea default)
                if not hasattr(self, "stopv_depths_m"):
                    self.stopv_depths_m = list(STOPV_DEPTHS_M)
                self.stopv_minutes_by_depth = {int(d): float(new_sv.get(int(d), 0.0)) for d in self.stopv_depths_m}

                # stato checkbox STOPV
                if hasattr(self, 'var_stopv_enabled') and 'stopv_enabled' in data:
                    try:
                        self.var_stopv_enabled.set(bool(data.get('stopv_enabled')))
                    except Exception:
                        pass
                    # ensure label/button reflects persisted checkbox
                    try:
                        if hasattr(self, '_update_stopv_indicator'):
                            self._update_stopv_indicator()
                    except Exception:
                        pass
        except Exception:
            pass

        # Gas OC
        try:
            gases = data.get("gases", [])
            if isinstance(gases, list):
                for row, saved in zip(getattr(self, "gas_rows", []), gases):
                    try:
                        row["var_enabled"].set(bool(saved.get("enabled", False)))
                        for fld in ["fo2", "fhe", "mod", "vrm", "tank"]:
                            if fld in saved and row.get(fld):
                                row[fld].delete(0, tk.END)
                                row[fld].insert(0, str(saved.get(fld, "")))
                    except Exception:
                        continue
        except Exception:
            pass

        # CC inputs
        try:
            cc = data.get("cc", {})
            if isinstance(cc, dict):
                mapping = {
                    "sp_single": "cc_sp_single",
                    "sp_descent": "cc_sp_descent",
                    "sp_bottom": "cc_sp_bottom",
                    "sp_deco1": "cc_sp_deco1",
                    "sp_deco2": "cc_sp_deco2",
                    "sp_deco3": "cc_sp_deco3",
                    "deco1_a": "cc_deco1_a",
                    "deco2_a": "cc_deco2_a",
                    "dil_fo2": "cc_dil_fo2",
                    "dil_fhe": "cc_dil_fhe",
                    "dil_mod": "cc_dil_mod",
                }
                for k, varname in mapping.items():
                    if k in cc and hasattr(self, varname):
                        getattr(self, varname).set(str(cc.get(k, "")))

                # Multi-SP enabled (checkbox)
                try:
                    if "msp_enabled" in cc and hasattr(self, "cc_msp_enabled"):
                        self.cc_msp_enabled.set(bool(cc.get("msp_enabled")))
                except Exception:
                    pass

                # Dopo load: riallinea SP Fondo (mirror) e stato read-only
                try:
                    if hasattr(self, "_cc_sync_sp_bottom_mirror"):
                        self._cc_sync_sp_bottom_mirror()
                except Exception:
                    pass
        except Exception:
            pass

        # Bailout gases
        try:
            bo = data.get("bailout", [])
            if isinstance(bo, list):
                for row, saved in zip(getattr(self, "bailout_rows", []), bo):
                    try:
                        if row.get("var_enabled"):
                            row["var_enabled"].set(bool(saved.get("enabled", False)))
                        for fld in ["fo2", "fhe", "mod", "vrm", "tank"]:
                            if fld in saved and row.get(fld):
                                row[fld].delete(0, tk.END)
                                row[fld].insert(0, str(saved.get(fld, "")))
                    except Exception:
                        continue
        except Exception:
            pass

        # Advanced params adv_*
        try:
            adv = data.get("adv", {})
            if isinstance(adv, dict):
                for k, v in adv.items():
                    if k.startswith("adv_"):
                        setattr(self, k, v)
        except Exception:
            pass

        # ZH-L16 UI (model + GF + coeff table)
        try:
            if "deco_model" in data and hasattr(self, "deco_model_var"):
                self.deco_model_var.set(str(data.get("deco_model") or "VPM"))
        except Exception:
            pass
        try:
            if "gf_low" in data:
                val = str(data.get("gf_low") or "")
                if hasattr(self, "var_gf_low") and self.var_gf_low is not None:
                    self.var_gf_low.set(val)
                elif hasattr(self, "entry_gf_low"):
                    self.entry_gf_low.delete(0, tk.END)
                    self.entry_gf_low.insert(0, val)
            if "gf_high" in data:
                val = str(data.get("gf_high") or "")
                if hasattr(self, "var_gf_high") and self.var_gf_high is not None:
                    self.var_gf_high.set(val)
                elif hasattr(self, "entry_gf_high"):
                    self.entry_gf_high.delete(0, tk.END)
                    self.entry_gf_high.insert(0, val)
        except Exception:
            pass
        try:
            # Variant (persisted): 'C' (default) or 'B'
            # GF ramp anchor mode (persisted): "1STOP", "SODZ" or "BOTTOM"
            v = str(data.get("zhl_gf_ramp_anchor", "SODZ") or "SODZ").strip().upper()
            if v not in ("1STOP", "SODZ", "BOTTOM"):
                v = "SODZ"
            if hasattr(self, "var_zhl_gf_ramp_anchor") and self.var_zhl_gf_ramp_anchor is not None:
                self.var_zhl_gf_ramp_anchor.set(v)
            v = str(data.get("zhl_gf_ramp_hi_anchor", "SURFACE")).strip() or "SURFACE"
            if hasattr(self, "var_zhl_gf_ramp_hi_anchor") and self.var_zhl_gf_ramp_hi_anchor is not None:
                self.var_zhl_gf_ramp_hi_anchor.set(v)
            v = str(data.get("zhl16_variant", getattr(self, "zhl16_variant", "C")) or "C").strip().upper()
            if v in ("B", "C"):
                self.zhl16_variant = v
        except Exception:
            pass

        def _clean_zhl_rows(rows):
            cleaned = []
            for r in (rows or []):
                if not isinstance(r, (list, tuple)) or len(r) < 5:
                    continue
                try:
                    cleaned.append((int(r[0]),
                                    float(str(r[1]).replace(",", ".")),
                                    float(str(r[2]).replace(",", ".")),
                                    float(str(r[3]).replace(",", ".")),
                                    float(str(r[4]).replace(",", "."))))
                except Exception:
                    continue
            return cleaned

        try:
            # Defaults alias helpers (short lines to avoid corruption)
            _def_C = getattr(self, "zhl16_coeff_defaults_C", getattr(self, "zhl16_coeff_defaults", []))
            _def_B = getattr(self, "zhl16_coeff_defaults_B", _def_C)

            # New format: store both sets
            if "zhl16_coeffs_C" in data and isinstance(data.get("zhl16_coeffs_C"), list):
                cleaned = _clean_zhl_rows(data.get("zhl16_coeffs_C"))
                if cleaned:
                    self.zhl16_coeffs_C = cleaned

            if "zhl16_coeffs_B" in data and isinstance(data.get("zhl16_coeffs_B"), list):
                cleaned = _clean_zhl_rows(data.get("zhl16_coeffs_B"))
                if cleaned:
                    self.zhl16_coeffs_B = cleaned

            # Legacy format: single list -> assign to the persisted variant (B/C)
            # (older versions stored only "zhl16_coeffs" as the active set)
            if ("zhl16_coeffs_C" not in data) and ("zhl16_coeffs_B" not in data) and ("zhl16_coeffs" in data) and isinstance(data.get("zhl16_coeffs"), list):
                cleaned = _clean_zhl_rows(data.get("zhl16_coeffs"))
                if cleaned:
                    _vv = str(getattr(self, "zhl16_variant", "C") or "C").strip().upper()
                    if _vv == "B":
                        self.zhl16_coeffs_B = cleaned
                    else:
                        self.zhl16_coeffs_C = cleaned


            # Keep backward-compat alias aligned to current variant
            if str(getattr(self, "zhl16_variant", "C") or "C").strip().upper() == "B":
                self.zhl16_coeff_defaults = _def_B
                self.zhl16_coeffs = getattr(self, "zhl16_coeffs_B", getattr(self, "zhl16_coeffs", []))
            else:
                self.zhl16_coeff_defaults = _def_C
                self.zhl16_coeffs = getattr(self, "zhl16_coeffs_C", getattr(self, "zhl16_coeffs", []))
        except Exception:
            pass

        # ZH-L16 placeholder
        try:
            z = data.get("zhl16", {})
            if isinstance(z, dict):
                self.zhl16_params = z
        except Exception:
            pass

        # refresh calculated columns if methods exist
        for fn in ["_recalc_gases_table", "_recalc_bailout_table", "_recalc_cc_diluent_table", "_validate_ccr_sp_logic"]:
            if hasattr(self, fn):
                try:
                    getattr(self, fn)()
                except Exception:
                    pass
        # aggiorna stati UI (abilitazioni / warning)
        try:
            self._on_deco_model_change()
        except Exception:
            pass
        try:
            self._validate_gf_fields()
        except Exception:
            pass

    # ==========================================================
    # Persistenza (JSON) — salvataggio/ricarica automatici
    # ==========================================================
    def _attach_persistence_traces(self):
        """Aggancia i trace per salvare su JSON senza dipendere dal tasto 'Calcola deco'."""
        # Deco model (VPM/ZHL16)
        try:
            if hasattr(self, "deco_model_var") and self.deco_model_var is not None:
                try:
                    self.deco_model_var.trace_add("write", lambda *args: self._request_autosave())
                except Exception:
                    self.deco_model_var.trace("w", lambda *args: self._request_autosave())
        except Exception:
            pass

        # Modalità respirazione (OC/CC): persistenza immediata
        try:
            if hasattr(self, "mode_var") and self.mode_var is not None:
                try:
                    self.mode_var.trace_add("write", lambda *args: self._request_autosave())
                except Exception:
                    self.mode_var.trace("w", lambda *args: self._request_autosave())
        except Exception:
            pass

        
        # GF variables (trace su StringVar: salva anche senza eventi tastiera)
        for vname in ["var_gf_low", "var_gf_high"]:
            v = getattr(self, vname, None)
            if v is None:
                continue
            try:
                v.trace_add("write", lambda *args: self._request_autosave())
            except Exception:
                try:
                    v.trace("w", lambda *args: self._request_autosave())
                except Exception:
                    pass
# GF entries (KeyRelease + FocusOut)
        for wname in ["entry_gf_low", "entry_gf_high"]:
            w = getattr(self, wname, None)
            if w is None:
                continue
            try:
                w.bind("<KeyRelease>", lambda e: self._request_autosave())
                w.bind("<FocusOut>", lambda e: self._request_autosave())
            except Exception:
                pass

    def _request_autosave(self):
        """Debounce: evita scritture ripetute su disco mentre si digita."""
        try:
            if getattr(self, "_autosave_after_id", None) is not None:
                try:
                    self.after_cancel(self._autosave_after_id)
                except Exception:
                    pass
            self._autosave_after_id = self.after(300, self._do_autosave)
        except Exception:
            # fallback immediato
            try:
                self._do_autosave()
            except Exception:
                pass

    def _do_autosave(self):
        try:
            self._autosave_after_id = None
        except Exception:
            pass
        try:
            save_last_inputs(self.collect_inputs_for_save())
        except Exception:
            pass

    def _on_close(self):
        """Salva gli input e chiude l'app."""
        try:
            save_last_inputs(self.collect_inputs_for_save())
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


    def collect_inputs_for_save(self) -> dict:
        """Raccoglie tutti gli input correnti in un dizionario serializzabile."""
        data = {
            # Modalità
            "mode": self.mode_var.get() if hasattr(self, "mode_var") else "OC",

            # Input immersione base
            "depth": self.entry_depth.get(),
            "bottom": self.entry_bottom.get(),
            "rapsol": self.entry_rapsol.get(),
            "crit_rad_n2": self.entry_crit_rad_n2.get(),
            "crit_rad_he": self.entry_crit_rad_he.get(),

            # ZH-L16 UI
            "deco_model": self.deco_model_var.get() if hasattr(self, "deco_model_var") else "VPM",
            "gf_low": (self.var_gf_low.get() if hasattr(self, "var_gf_low") else ""),
            "gf_high": (self.var_gf_high.get() if hasattr(self, "var_gf_high") else ""),
            "zhl_gf_ramp_anchor": (self.var_zhl_gf_ramp_anchor.get() if hasattr(self, "var_zhl_gf_ramp_anchor") else "SODZ"),
"zhl_gf_ramp_hi_anchor": (self.var_zhl_gf_ramp_hi_anchor.get() if hasattr(self, "var_zhl_gf_ramp_hi_anchor") else "SURFACE"),
            "zhl16_variant": getattr(self, "zhl16_variant", "C"),
            "zhl16_coeffs_C": getattr(self, "zhl16_coeffs_C", None),
            "zhl16_coeffs_B": getattr(self, "zhl16_coeffs_B", None),
            "zhl16_coeffs": getattr(self, "zhl16_coeffs", None),

            # Debug / opzioni
            "use_bailout_ascent": bool(self.chk_use_bailout.get()) if hasattr(self, "chk_use_bailout") else False,
            # debug_projected_ascent removed in production

            # Parametri deco condivisi (tab semplificato)
            "deco_last_stop_m": getattr(self, "deco_last_stop_m", 3.0),
            "deco_desc_profile": getattr(self, "deco_desc_speed_profile", "Standard (20 m/min)"),
            "deco_asc_profile": getattr(self, "deco_asc_speed_profile", "Standard (9 m/min)"),

            # Profili dettagliati A1/A2/A3
            "descent_stops": [
                {"depth": float(b.get("to_m", 0.0)), "time": float(b.get("stop_min", 0.0))}
                for b in getattr(self, "descent_speed_bands", [])
                if float(b.get("to_m", 0.0)) > 0.0 and float(b.get("stop_min", 0.0)) > 0.0
            ],
            "descent_speed_bands": [dict(b) for b in getattr(self, "descent_speed_bands", [])],
            "ascent_speed_bands": [dict(b) for b in getattr(self, "ascent_speed_bands", [])],

            # OC gases
            "gases": [],

            # CC (CCR) setpoint / diluente
            "cc": {
                "sp_single": self.cc_sp_single.get() if hasattr(self, "cc_sp_single") else "",
                "msp_enabled": bool(self.cc_msp_enabled.get()) if hasattr(self, "cc_msp_enabled") else False,
                "sp_descent": self.cc_sp_descent.get() if hasattr(self, "cc_sp_descent") else "",
                "sp_bottom": self.cc_sp_bottom.get() if hasattr(self, "cc_sp_bottom") else "",
                "sp_deco1": self.cc_sp_deco1.get() if hasattr(self, "cc_sp_deco1") else "",
                "sp_deco2": self.cc_sp_deco2.get() if hasattr(self, "cc_sp_deco2") else "",
                "sp_deco3": self.cc_sp_deco3.get() if hasattr(self, "cc_sp_deco3") else "",
                "deco1_a": self.cc_deco1_a.get() if hasattr(self, "cc_deco1_a") else "",
                "deco2_a": self.cc_deco2_a.get() if hasattr(self, "cc_deco2_a") else "",
                "dil_fo2": self.cc_dil_fo2.get() if hasattr(self, "cc_dil_fo2") else "",
                "dil_fhe": self.cc_dil_fhe.get() if hasattr(self, "cc_dil_fhe") else "",
                "dil_mod": self.cc_dil_mod.get() if hasattr(self, "cc_dil_mod") else "",
            },

            # Bailout gases (5)
            "bailout": [],
        }

        # OC gas table
        for row in getattr(self, "gas_rows", []):
            try:
                data["gases"].append(
                    {
                        "name": row.get("name"),
                        "enabled": bool(row.get("var_enabled").get()) if row.get("var_enabled") is not None else True,
                        "fo2": row["fo2"].get(),
                        "fhe": row["fhe"].get(),
                        "mod": row["mod"].get(),
                        "vrm": row["vrm"].get(),
                        "tank": row["tank"].get(),
                    }
                )
            except Exception:
                continue

        # Bailout table
        for row in getattr(self, "bailout_rows", []):
            try:
                data["bailout"].append(
                    {
                        "name": row.get("name"),
                        "enabled": bool(row.get("var_enabled").get()) if row.get("var_enabled") else False,
                        "fo2": row["fo2"].get() if row.get("fo2") else "",
                        "fhe": row["fhe"].get() if row.get("fhe") else "",
                        "mod": row["mod"].get() if row.get("mod") else "",
                        "vrm": row["vrm"].get() if row.get("vrm") else "",
                        "tank": row["tank"].get() if row.get("tank") else "",
                    }
                )
            except Exception:
                continue

        # Parametri VPM avanzati / deco (tutti gli adv_* presenti)
        adv = {}
        for k, v in self.__dict__.items():
            if k.startswith("adv_"):
                # solo tipi serializzabili
                if isinstance(v, (int, float, str, bool)) or v is None:
                    adv[k] = v
        data["adv"] = adv        # Soste volontarie in risalita (STOPV)
        try:
            data["stopv_minutes_by_depth"] = dict(getattr(self, "stopv_minutes_by_depth", {}) or {})
        except Exception:
            data["stopv_minutes_by_depth"] = {}

        # Abilitazione STOPV (checkbox)
        try:
            data["stopv_enabled"] = bool(self.var_stopv_enabled.get()) if hasattr(self, "var_stopv_enabled") else False
        except Exception:
            data["stopv_enabled"] = False



        # Placeholder ZH-L16 (pronto per futuro)
        data["zhl16"] = getattr(self, "zhl16_params", {}) if isinstance(getattr(self, "zhl16_params", {}), dict) else {}

        return data
    def init_advanced_defaults(self):
        """
        Inizializza i parametri avanzati VPM ai valori di default.
        """
        self.adv_Minimum_Deco_Stop_Time = 1.0
        self.adv_Critical_Volume_Algorithm = "ON"
        self.adv_Crit_Volume_Parameter_Lambda = 6500.0
        self.adv_Gradient_Onset_of_Imperm_Atm = 8.2
        self.adv_Surface_Tension_Gamma = 0.0179
        self.adv_Skin_Compression_GammaC = 0.257
        self.adv_Regeneration_Time_Constant = 20160.0
        self.adv_Pressure_Other_Gases_mmHg = 102.0

    
    def init_deco_defaults(self):
        """Inizializza i parametri deco condivisi.

        - Ultima sosta (3 / 6 m)
        - Preset semplici per velocità globali discesa/risalita
        - Profili dettagliati: 2 soste in discesa, 4 velocità di discesa, 5 di risalita.
        """
        # Ultima sosta deco (m): 3 o 6. Default 3.
        self.deco_last_stop_m = 3.0

        # Profili di velocità globali (etichette -> valori numerici in m/min)
        # Usati dal tab "Semplificato" per aggiornare le entry di discesa/risalita.
        self.deco_desc_speed_sets = {
            "Conservativa (15 m/min)": 15.0,
            "Standard (20 m/min)": 20.0,
            "Veloce (25 m/min)": 25.0,
        }
        self.deco_asc_speed_sets = {
            "Conservativa (6 m/min)": 6.0,
            "Standard (9 m/min)": 9.0,
            "Moderata (10 m/min)": 10.0,
            "Aggressiva (12 m/min)": 12.0,
            "Molto aggressiva (15 m/min)": 15.0,
        }

        # Selezioni correnti per i preset semplici (salvate anche su disco)
        self.deco_desc_speed_profile = "Standard (20 m/min)"
        self.deco_asc_speed_profile = "Standard (9 m/min)"

        # --- Profili dettagliati tipo A1/A2/A3 ---
        # 2 soste di discesa (se tempo o profondità sono vuoti / 0 -> sosta ignorata)
        self.descent_stop1_depth_m = 6.0
        self.descent_stop1_time_min = 3.0
        self.descent_stop2_depth_m = 15.0
        self.descent_stop2_time_min = 20.0

        # 4 velocità di discesa (A2)
        # Ogni voce: from_m (incluso), to_m (incluso), speed_m_per_min
        self.descent_speed_bands = [
            {"from_m": 0.0, "to_m": 6.0, "speed": 5.0, "stop_min": 0.0},
            {"from_m": 6.0, "to_m": 30.0, "speed": 10.0, "stop_min": 0.0},
            {"from_m": 30.0, "to_m": 60.0, "speed": 15.0, "stop_min": 0.0},
            {"from_m": 60.0, "to_m": 150.0, "speed": 20.0, "stop_min": 0.0},
        ]
# 5 velocità di risalita (A3)
        self.ascent_speed_bands = [
            {"from_m": 150.0, "to_m": 90.0, "speed": 10.0},
            {"from_m": 90.0, "to_m": 65.0, "speed": 7.0},
            {"from_m": 65.0, "to_m": 36.0, "speed": 5.0},
            {"from_m": 36.0, "to_m": 6.0, "speed": 3.0},
            {"from_m": 6.0, "to_m": 0.0, "speed": 1.0},
        ]

    


        # --- Soste volontarie in risalita (STOPV) ---
        # Quote fisse (m): 66..6 (step 3). Durate in minuti (default 0).
        self.stopv_depths_m = list(STOPV_DEPTHS_M)
        self.stopv_minutes_by_depth = {d: 0.0 for d in self.stopv_depths_m}
    def open_deco_params_window(self):
        """Apre la finestra con i parametri deco.

        In questa release si modifica SOLO la parte discesa:
        - Profilo di discesa a bande (multivelocità) con sosta finale opzionale per banda.
        - Le soste di discesa sono derivate dalle bande (stop_min > 0) e passate al main come segmenti constant depth.
        La risalita (A3) resta invariata (si modificano solo le velocità).
        """
        win = tk.Toplevel(self)
        win.title("Parametri deco")
        win.transient(self)
        win.grab_set()

        frame = ttk.Frame(win, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")

        row = 0

        # --- Ultima sosta deco (3m / 4m / 5m / 6m) ---
        ttk.Label(frame, text="Ultima sosta deco (m):").grid(row=row, column=0, sticky=tk.W, pady=1)
        last_stop_var = tk.DoubleVar(value=float(getattr(self, "deco_last_stop_m", 3.0)))
        ttk.Radiobutton(frame, text="3 m", variable=last_stop_var, value=3.0).grid(row=row, column=1, padx=5, pady=1, sticky=tk.W)
        ttk.Radiobutton(frame, text="4 m", variable=last_stop_var, value=4.0).grid(row=row, column=2, padx=5, pady=1, sticky=tk.W)
        ttk.Radiobutton(frame, text="5 m", variable=last_stop_var, value=5.0).grid(row=row, column=3, padx=5, pady=1, sticky=tk.W)
        ttk.Radiobutton(frame, text="6 m", variable=last_stop_var, value=6.0).grid(row=row, column=4, padx=5, pady=1, sticky=tk.W)

        row += 2

        # --- Profilo di discesa (bande + sosta finale opzionale) ---
        ttk.Label(frame, text="Profilo di discesa (bande + sosta finale opzionale)", font=("TkDefaultFont", 10, "bold")).grid(
            row=row, column=0, columnspan=6, sticky=tk.W, pady=(4, 2)
        )
        row += 1

        ttk.Label(frame, text="Banda").grid(row=row, column=0, sticky=tk.W)
        ttk.Label(frame, text="da [m]").grid(row=row, column=1, sticky=tk.W)
        ttk.Label(frame, text="a [m]").grid(row=row, column=2, sticky=tk.W)
        ttk.Label(frame, text="Velocità [m/min]").grid(row=row, column=3, sticky=tk.W)
        ttk.Label(frame, text="Sosta finale [min]").grid(row=row, column=4, sticky=tk.W)
        row += 1

        # Variabili per bande discesa
        self._var_desc_to = []
        self._var_desc_speed = []
        self._var_desc_stop = []
        self._lbl_desc_from = []

        # Calcola i "from" contigui a partire dalle bande attuali (se incoerenti, ricalcoliamo comunque in on_ok)
        prev_to = 0.0
        n_desc_bands = len(getattr(self, "descent_speed_bands", []))
        for idx_band, band in enumerate(getattr(self, "descent_speed_bands", []), start=1):
            try:
                to_m = float(band.get("to_m", prev_to))
            except Exception:
                to_m = prev_to

            from_m = prev_to
            prev_to = to_m

            ttk.Label(frame, text=f"{idx_band}").grid(row=row, column=0, sticky=tk.W)

            lbl_from = ttk.Label(frame, text=f"{from_m:.0f}")
            lbl_from.grid(row=row, column=1, sticky=tk.W, padx=3)
            self._lbl_desc_from.append(lbl_from)

            var_to = tk.StringVar(value=f"{to_m:.0f}")
            e_to = ttk.Entry(frame, textvariable=var_to, width=6)
            e_to.grid(row=row, column=2, padx=3, pady=1, sticky=tk.W)

            var_speed = tk.StringVar(value=f"{float(band.get('speed', 20.0)):.1f}")
            e_speed = ttk.Entry(frame, textvariable=var_speed, width=6)
            e_speed.grid(row=row, column=3, padx=3, pady=1, sticky=tk.W)

            # Sosta finale: per l'ultima banda (piu profonda) non e significativa -> non mostrare campo
            if idx_band == n_desc_bands:
                var_stop = tk.StringVar(value='0')
                ttk.Label(frame, text='').grid(row=row, column=4, padx=3, pady=1, sticky=tk.W)
            else:
                var_stop = tk.StringVar(value=f"{float(band.get('stop_min', 0.0)):.0f}")
                e_stop = ttk.Entry(frame, textvariable=var_stop, width=6)
                e_stop.grid(row=row, column=4, padx=3, pady=1, sticky=tk.W)

            self._var_desc_to.append(var_to)
            self._var_desc_speed.append(var_speed)
            self._var_desc_stop.append(var_stop)

            row += 1

        row += 1

        # --- Profilo di risalita (A3) - Velocità di risalita ---
        ttk.Label(frame, text="Velocità di risalta", font=("TkDefaultFont", 10, "bold")).grid(
            row=row, column=0, columnspan=6, sticky=tk.W, pady=(6, 2)
        )
        row += 1

        ttk.Label(frame, text="Banda").grid(row=row, column=0, sticky=tk.W)
        ttk.Label(frame, text="da [m]").grid(row=row, column=1, sticky=tk.W)
        ttk.Label(frame, text="a [m]").grid(row=row, column=2, sticky=tk.W)
        ttk.Label(frame, text="Velocità [m/min]").grid(row=row, column=3, sticky=tk.W)
        row += 1

        def _parse_float_or_none(s):
            s = (s or "").strip().replace(",", ".")
            if not s:
                return None
            try:
                return float(s)
            except Exception:
                return None

        # Refresh immediato delle etichette "da [m]" per le bande di DISCESA:
        # quando l'utente modifica "a [m]", aggiorniamo subito la colonna "da [m]"
        # per mantenere la contiguità visiva (salvataggio definitivo avviene comunque su OK).
        def _refresh_descent_from_labels():
            prev_to = 0.0
            for i in range(len(getattr(self, "_var_desc_to", []))):
                # aggiorna "da" (read-only)
                try:
                    self._lbl_desc_from[i].configure(text=f"{prev_to:.0f}")
                except Exception:
                    pass

                # calcola nuovo prev_to dal valore "a [m]" corrente
                v_to = _parse_float_or_none(self._var_desc_to[i].get())
                if v_to is None:
                    v_to = prev_to
                if v_to < prev_to:
                    v_to = prev_to
                prev_to = v_to

        # refresh iniziale + hook su ogni modifica
        _refresh_descent_from_labels()
        for v in getattr(self, "_var_desc_to", []):
            try:
                v.trace_add("write", lambda *args: _refresh_descent_from_labels())
            except Exception:
                pass

        # Regole (come da specifica):
        # - Banda 1: da[m] = a[m] banda 4 della DISCESA (read-only)
        # - Bande 1..4: a[m] editabile
        # - Bande 2..5: da[m] = a[m] della banda precedente (read-only)
        # - Banda 5: a[m] = 0 fisso
        self._var_asc_to = []
        self._var_asc_speeds = []
        self._lbl_asc_from = []

        # Base per "da" banda 1 = a[m] banda 4 discesa (se non disponibile, fallback a from_m salvato)
        try:
            descent_band4_to = float(getattr(self, "descent_speed_bands", [])[3].get("to_m", 0.0))
        except Exception:
            descent_band4_to = float(getattr(self, "ascent_speed_bands", [{"from_m": 150.0}])[0].get("from_m", 150.0))

        asc_bands = list(getattr(self, "ascent_speed_bands", []))
        # Garantiamo sempre 5 righe
        while len(asc_bands) < 5:
            asc_bands.append({"from_m": 0.0, "to_m": 0.0, "speed": 10.0})

        def _refresh_ascent_from_labels():
            """Aggiorna i campi read-only "da" delle bande A3 in modo contiguo."""
            nonlocal descent_band4_to

            # Tenta di leggere in tempo reale il valore di a[m] banda 4 discesa dall'entry (se presente)
            if hasattr(self, "_var_desc_to") and len(getattr(self, "_var_desc_to", [])) >= 4:
                v = _parse_float_or_none(self._var_desc_to[3].get())
                if v is not None:
                    descent_band4_to = float(v)

            prev_from = float(descent_band4_to)
            for i in range(5):
                # set label "from"
                if i < len(self._lbl_asc_from):
                    self._lbl_asc_from[i].config(text=f"{prev_from:.0f}")

                # next "from" = this band's "to" (except band 5, which has fixed to=0)
                if i < 4 and i < len(self._var_asc_to):
                    v_to = _parse_float_or_none(self._var_asc_to[i].get())
                    if v_to is None:
                        # keep previous until user types
                        v_to = float(asc_bands[i].get("to_m", 0.0))
                    prev_from = float(v_to)
                else:
                    prev_from = 0.0

        for idx_band in range(1, 6):
            band = asc_bands[idx_band - 1]
            ttk.Label(frame, text=f"{idx_band}").grid(row=row, column=0, sticky=tk.W)

            lbl_from = ttk.Label(frame, text="")
            lbl_from.grid(row=row, column=1, sticky=tk.W, padx=3)
            self._lbl_asc_from.append(lbl_from)

            if idx_band <= 4:
                var_to = tk.StringVar(value=f"{float(band.get('to_m', 0.0)):.0f}")
                ttk.Entry(frame, textvariable=var_to, width=6).grid(row=row, column=2, padx=3, pady=1, sticky=tk.W)
                self._var_asc_to.append(var_to)
            else:
                ttk.Label(frame, text="0").grid(row=row, column=2, sticky=tk.W, padx=3)

            var_speed = tk.StringVar(value=f"{float(band.get('speed', 10.0)):.1f}")
            ttk.Entry(frame, textvariable=var_speed, width=6).grid(row=row, column=3, padx=3, pady=1, sticky=tk.W)
            self._var_asc_speeds.append(var_speed)

            row += 1

        # Refresh immediato + auto-refresh su modifiche
        _refresh_ascent_from_labels()
        for v in self._var_asc_to:
            v.trace_add("write", lambda *args: _refresh_ascent_from_labels())
        if hasattr(self, "_var_desc_to") and len(getattr(self, "_var_desc_to", [])) >= 4:
            self._var_desc_to[3].trace_add("write", lambda *args: _refresh_ascent_from_labels())

        # --- Pulsanti ---

        # --- Parametro deco (non VPM): Minimum_Deco_Stop_Time
        if not hasattr(self, "adv_Minimum_Deco_Stop_Time"):
            self.adv_Minimum_Deco_Stop_Time = 1.0

        ttk.Label(frame, text="Minimum_Deco_Stop_Time (min):").grid(row=row, column=0, sticky=tk.W, pady=1)
        min_deco_stop_var = tk.StringVar(value=str(self.adv_Minimum_Deco_Stop_Time))
        entry_min_deco_stop = ttk.Entry(frame, width=12, textvariable=min_deco_stop_var)
        entry_min_deco_stop.grid(row=row, column=1, sticky=tk.W, pady=1)
        row += 1

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=6, pady=(10, 4), sticky="ew")
        btn_frame.grid_columnconfigure(2, weight=1)

        def on_ok():
            # Salva ultima sosta
            try:
                v = float(last_stop_var.get())
                # Validazione locale GUI: consentiti solo 3/4/5/6 m
                if v in (3.0, 4.0, 5.0, 6.0):
                    self.deco_last_stop_m = v
                else:
                    self.deco_last_stop_m = 3.0
            except Exception:
                self.deco_last_stop_m = 3.0

            # Minimum_Deco_Stop_Time (min) - parametro deco, non VPM
            try:
                self.adv_Minimum_Deco_Stop_Time = float((min_deco_stop_var.get() or "").strip().replace(",", "."))
            except Exception:
                self.adv_Minimum_Deco_Stop_Time = 1.0

            # Profondità di fondo attuale (serve per clamp dei "to")
            # NOTA: la griglia delle bande di discesa è un criterio trasversale e NON viene clamped sulla profondità

            # dell\'immersione corrente. La profondità di fondo viene usata solo al momento del calcolo per troncare i segmenti effettivi.

            bottom_depth = None  # mantenuto per retrocompatibilità del codice che segue (non usato per clamp)

            # Aggiorna bande discesa (from contigui; to clamped; speed clamped; stop_min int 0..120)
            new_bands = []
            prev_to_local = 0.0

            n_bands = len(getattr(self, "descent_speed_bands", []))
            for i in range(n_bands):
                # to_m
                to_val = _parse_float_or_none(self._var_desc_to[i].get())
                if to_val is None:
                    # se vuoto o non valido, manteniamo almeno il contiguo
                    to_val = prev_to_local

                # clamp: mai < precedente
                if to_val < prev_to_local:
                    to_val = prev_to_local

                # clamp su profondità di fondo: DISABILITATO (persistenza bande indipendente dall'immersione corrente)

                # forzatura ultima banda su profondità di fondo: DISABILITATA

                # speed
                sp_val = _parse_float_or_none(self._var_desc_speed[i].get())
                if sp_val is None:
                    sp_val = float(getattr(self.descent_speed_bands[i], "get", lambda k, d=None: d)("speed", 20.0))
                sp_val = max(0.1, min(99.0, sp_val))
                sp_val = round(sp_val, 1)

                # stop_min
                st_val = _parse_float_or_none(self._var_desc_stop[i].get())
                if st_val is None:
                    st_val = 0.0
                st_val = max(0.0, min(120.0, st_val))
                st_val = float(int(round(st_val)))

                new_bands.append({
                    "from_m": float(prev_to_local),
                    "to_m": float(to_val),
                    "speed": float(sp_val),
                    "stop_min": float(st_val),
                })
                prev_to_local = float(to_val)

            # Ricalcolo dei label "da [m]" in finestra (coerenza visiva)
            for i, b in enumerate(new_bands):
                try:
                    self._lbl_desc_from[i].configure(text=f"{b['from_m']:.0f}")
                    self._var_desc_to[i].set(f"{b['to_m']:.0f}")
                except Exception:
                    pass

            self.descent_speed_bands = new_bands

            # Aggiorna profilo di risalita (A3) secondo specifica:
            # - Banda 1: from = a[m] banda 4 della DISCESA
            # - Bande 1..4: to editabile
            # - Bande 2..5: from contiguo al to precedente
            # - Banda 5: to = 0 fisso
            try:
                asc_from_1 = float(self.descent_speed_bands[3].get("to_m", 0.0))
            except Exception:
                try:
                    asc_from_1 = float(getattr(self, "ascent_speed_bands", [])[0].get("from_m", 150.0))
                except Exception:
                    asc_from_1 = 150.0

            # Normalizza lista esistente a 5 elementi
            old_asc = list(getattr(self, "ascent_speed_bands", []))
            while len(old_asc) < 5:
                old_asc.append({"from_m": 0.0, "to_m": 0.0, "speed": 10.0})

            new_asc = []
            prev_from = float(asc_from_1)
            # Bande 1..4
            for i in range(4):
                # to
                v_to = None
                if hasattr(self, "_var_asc_to") and len(self._var_asc_to) > i:
                    v_to = _parse_float_or_none(self._var_asc_to[i].get())
                if v_to is None:
                    try:
                        v_to = float(old_asc[i].get("to_m", 0.0))
                    except Exception:
                        v_to = 0.0

                # clamp: 0..prev_from
                v_to = max(0.0, min(float(prev_from), float(v_to)))

                # speed
                v_sp = None
                if hasattr(self, "_var_asc_speeds") and len(self._var_asc_speeds) > i:
                    v_sp = _parse_float_or_none(self._var_asc_speeds[i].get())
                if v_sp is None:
                    try:
                        v_sp = float(old_asc[i].get("speed", 10.0))
                    except Exception:
                        v_sp = 10.0
                v_sp = max(0.1, min(99.0, float(v_sp)))
                v_sp = round(v_sp, 1)

                new_asc.append({
                    "from_m": float(prev_from),
                    "to_m": float(v_to),
                    "speed": float(v_sp),
                })
                prev_from = float(v_to)

            # Banda 5
            v_sp5 = None
            if hasattr(self, "_var_asc_speeds") and len(self._var_asc_speeds) >= 5:
                v_sp5 = _parse_float_or_none(self._var_asc_speeds[4].get())
            if v_sp5 is None:
                try:
                    v_sp5 = float(old_asc[4].get("speed", 10.0))
                except Exception:
                    v_sp5 = 10.0
            v_sp5 = max(1.0, min(99.0, float(v_sp5)))
            v_sp5 = round(v_sp5, 1)
            new_asc.append({
                "from_m": float(prev_from),
                "to_m": 0.0,
                "speed": float(v_sp5),
            })

            self.ascent_speed_bands = new_asc

            win.destroy()

        def on_cancel():
            win.destroy()
        # (PRODUCTION) Debug Projected Ascent checkbox removed
        ttk.Button(btn_frame, text="Annulla", command=on_cancel).grid(row=0, column=3, padx=5)
        ttk.Button(btn_frame, text="OK", command=on_ok).grid(row=0, column=4, padx=5)

    
    def open_voluntary_deco_window(self):
        """Apre la finestra 'Deco volontaria' per impostare le soste volontarie in risalita (STOPV)."""
        # Assicura default
        if not hasattr(self, "stopv_depths_m") or not hasattr(self, "stopv_minutes_by_depth"):
            self.stopv_depths_m = list(STOPV_DEPTHS_M)
            self.stopv_minutes_by_depth = {d: 0.0 for d in self.stopv_depths_m}

        win = tk.Toplevel(self)
        win.title("Deco volontaria")
        win.transient(self)
        win.grab_set()

        # Palette colori (come schema proposto)
        COL_NEUTRAL = "#EDEDED"   # grigio chiaro
        COL_L1 = "#E2EFDA"        # verde chiarissimo
        COL_L2 = "#C6E0B4"        # verde chiaro
        COL_L3 = "#A9D08E"        # verde medio
        COL_L4 = "#00B050"        # verde intenso

        # mapping quota -> bg
        def bg_for_depth(d: int) -> str:
            if d >= 39:
                return COL_NEUTRAL
            if d in (36, 33, 30, 27, 24):
                return COL_L1
            if d in (21, 18, 15, 12, 9):
                return COL_L3
            if d == 6:
                return COL_L4
            return COL_NEUTRAL

        frame = tk.Frame(win, bg=COL_NEUTRAL, padx=8, pady=8)
        frame.grid(row=0, column=0, sticky="nsew")
        win.grid_rowconfigure(0, weight=1)
        win.grid_columnconfigure(0, weight=1)

        # Header
        hdr_font = ("TkDefaultFont", 10, "bold")
        tk.Label(frame, text="Soste volontarie in risalita", bg=COL_NEUTRAL, font=hdr_font).grid(row=0, column=0, sticky="w", padx=2, pady=(0, 6))
        tk.Label(frame, text="a m", bg=COL_NEUTRAL, font=hdr_font, width=6).grid(row=0, column=1, sticky="w", padx=2, pady=(0, 6))
        tk.Label(frame, text="min", bg=COL_NEUTRAL, font=hdr_font, width=8).grid(row=0, column=2, sticky="w", padx=2, pady=(0, 6))

        # Vars per minuti
        self._stopv_vars = []
        self._stopv_depths_order = list(self.stopv_depths_m)

        for i, d in enumerate(self._stopv_depths_order, start=1):
            bg = bg_for_depth(int(d))
            # labels/entries: usiamo tk per poter impostare bg
            tk.Label(frame, text="sosta a m", bg=bg, width=20, anchor="w").grid(row=i, column=0, sticky="w", padx=1, pady=1)
            tk.Label(frame, text=str(int(d)), bg=bg, width=6, anchor="center").grid(row=i, column=1, sticky="w", padx=1, pady=1)

            v = tk.StringVar()
            try:
                v.set(str(int(self.stopv_minutes_by_depth.get(int(d), 0.0))) if float(self.stopv_minutes_by_depth.get(int(d), 0.0)) == int(float(self.stopv_minutes_by_depth.get(int(d), 0.0))) else str(self.stopv_minutes_by_depth.get(int(d), 0.0)))
            except Exception:
                v.set("0")
            e = tk.Entry(frame, textvariable=v, width=8, justify="right", bg=bg, relief="solid", bd=1)
            e.grid(row=i, column=2, sticky="w", padx=1, pady=1)
            self._stopv_vars.append(v)

        # --- Totali per fascia + totale generale (presentazionale) ---
        # Nota: aggiornamento in tempo reale su ogni variazione dei minuti.
        def _safe_float_minutes(s: str) -> float:
            try:
                st = (s or "").strip()
                if not st:
                    return 0.0
                st = st.replace(",", ".")
                v = float(st)
                if v < 0:
                    v = 0.0
                return v
            except Exception:
                return 0.0

        def _fmt_minutes(v: float) -> str:
            try:
                if abs(v - round(v)) < 1e-9:
                    return str(int(round(v)))
                # massimo 3 decimali, senza zeri finali
                s = f"{v:.3f}".rstrip("0").rstrip(".")
                return s if s else "0"
            except Exception:
                return "0"

        depth_to_var = {int(d): var for d, var in zip(self._stopv_depths_order, self._stopv_vars)}

        var_fascia4 = tk.StringVar(value="0")
        var_fascia3 = tk.StringVar(value="0")
        var_fascia2 = tk.StringVar(value="0")
        var_fascia1 = tk.StringVar(value="0")
        var_totale  = tk.StringVar(value="0")

        def _recalc_stopv_totals(*_args):
            # Fasce: 4=66..39, 3=36..24, 2=21..9, 1=6
            f4_depths = (66, 63, 60, 57, 54, 51, 48, 45, 42, 39)
            f3_depths = (36, 33, 30, 27, 24)
            f2_depths = (21, 18, 15, 12, 9)
            f1_depths = (6,)

            f4 = sum(_safe_float_minutes(depth_to_var.get(d).get() if depth_to_var.get(d) else "") for d in f4_depths)
            f3 = sum(_safe_float_minutes(depth_to_var.get(d).get() if depth_to_var.get(d) else "") for d in f3_depths)
            f2 = sum(_safe_float_minutes(depth_to_var.get(d).get() if depth_to_var.get(d) else "") for d in f2_depths)
            f1 = sum(_safe_float_minutes(depth_to_var.get(d).get() if depth_to_var.get(d) else "") for d in f1_depths)
            tot = f4 + f3 + f2 + f1

            var_fascia4.set(_fmt_minutes(f4))
            var_fascia3.set(_fmt_minutes(f3))
            var_fascia2.set(_fmt_minutes(f2))
            var_fascia1.set(_fmt_minutes(f1))
            var_totale.set(_fmt_minutes(tot))

        # Aggancio eventi (Tk >= 8.5): trace_add; fallback a trace
        for _v in self._stopv_vars:
            try:
                _v.trace_add("write", _recalc_stopv_totals)
            except Exception:
                try:
                    _v.trace("w", _recalc_stopv_totals)
                except Exception:
                    pass

        # prima valorizzazione
        _recalc_stopv_totals()

        # righe di totale (stesso layout tabellare)
        base_row = len(self._stopv_depths_order) + 2

        blank_bg = frame.cget('bg')

        def _make_total_row(label_text, bg_label, var_total, pady_val):
            """Crea una riga totale con label (col0), col1 vuota, e cella min (col2) in stile Entry."""
            nonlocal base_row
            tk.Label(frame, text=label_text, bg=bg_label, width=20, anchor="w").grid(row=base_row, column=0, sticky="w", padx=1, pady=pady_val)
            tk.Label(frame, text="", bg=blank_bg, width=6, anchor="center").grid(row=base_row, column=1, sticky="w", padx=1, pady=pady_val)
            e = tk.Entry(frame, textvariable=var_total, width=8, justify="right", bg=bg_label, relief="solid", bd=1,
                         state="readonly", readonlybackground=bg_label)
            e.grid(row=base_row, column=2, sticky="w", padx=1, pady=pady_val)
            base_row += 1

        # fascia 4 (66..39) - neutro
        _make_total_row("fascia 4 da 66 a 39 m", COL_NEUTRAL, var_fascia4, (10, 1))

        # fascia 3 (36..24) - verdino (COL_L1)
        _make_total_row("fascia 3 da 36 a 24 m", COL_L1, var_fascia3, 1)

        # fascia 2 (21..9) - verdino (COL_L3)
        _make_total_row("fascia 2 da 21 a 9 m", COL_L3, var_fascia2, 1)

        # fascia 1 (6) - verde (COL_L4)
        _make_total_row("fascia 1 a 6 m", COL_L4, var_fascia1, 1)

        # totale generale
        tk.Label(frame, text="totale", bg=COL_NEUTRAL, width=20, anchor="w").grid(row=base_row, column=0, sticky="w", padx=1, pady=(10, 1))
        tk.Label(frame, text="", bg=blank_bg, width=6, anchor="center").grid(row=base_row, column=1, sticky="w", padx=1, pady=(10, 1))
        e_var_totale = tk.Entry(frame, textvariable=var_totale, width=8, justify="right", bg=COL_NEUTRAL, relief="solid", bd=1,
                         state="readonly", readonlybackground=COL_NEUTRAL)
        e_var_totale.grid(row=base_row, column=2, sticky="w", padx=1, pady=(10, 1))
        base_row += 1

# Avviso informativo
        tk.Label(
            frame,
            text="Le soste inserite non contengono i tempi di risalita che sono calcolati separatamente in base alle velocità di risalita del menù Parametri deco",
            bg=COL_NEUTRAL,
            fg="black",
            justify="left",
            wraplength=450
        ).grid(row=base_row, column=0, columnspan=3, sticky="w", padx=1, pady=(8, 1))

        # Pulsanti
        btn_frame = tk.Frame(frame, bg=COL_NEUTRAL)
        btn_frame.grid(row=base_row + 1, column=0, columnspan=3, sticky="e", pady=(8, 0))

        def _parse_min(txt: str) -> float:
            s = (txt or "").strip().replace(",", ".")
            if s == "":
                return 0.0
            return float(s)

        def on_ok():
            try:
                new_map = {}
                for d, var in zip(self._stopv_depths_order, self._stopv_vars):
                    dv = _parse_min(var.get())
                    if dv < 0:
                        raise ValueError(f"Minuti non validi a {int(d)} m: deve essere ≥ 0")
                    # preferibilmente interi: se è quasi intero, arrotonda a int
                    if abs(dv - round(dv)) < 1e-9:
                        dv = float(int(round(dv)))
                    new_map[int(d)] = float(dv)
                self.stopv_minutes_by_depth = new_map
            except Exception as e:
                messagebox.showerror("Errore", str(e), parent=win)
                return
            win.destroy()

        def on_cancel():
            win.destroy()

        ttk.Button(btn_frame, text="Annulla", command=on_cancel).grid(row=0, column=0, padx=5)
        ttk.Button(btn_frame, text="OK", command=on_ok).grid(row=0, column=1, padx=5)

    def open_zh_l16_params_window(self):
        """Compatibilità: il vecchio pulsante placeholder ora apre 'Deco volontaria'."""
        return self.open_voluntary_deco_window()

    def open_advanced_params_window(self):
        """Apre una finestra con i parametri avanzati VPM."""
        # Se non abbiamo ancora i default, inizializziamoli
        if not hasattr(self, "adv_Critical_Volume_Algorithm"):
            self.init_advanced_defaults()

        win = tk.Toplevel(self)
        win.title("Parametri VPM avanzati")

        frame = ttk.Frame(win, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")

        # Helper per creare una riga label+entry
        def add_row(r, label, initial_value, attr_name):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky=tk.W, pady=1)
            e = ttk.Entry(frame, width=12)
            e.grid(row=r, column=1, padx=5, pady=1, sticky=tk.W)
            e.insert(0, str(initial_value))
            setattr(self, f"_adv_entry_{attr_name}", e)

        row = 0
        # Critical_Volume_Algorithm: ON / OFF
        ttk.Label(frame, text="Critical_Volume_Algorithm:").grid(row=row, column=0, sticky=tk.W, pady=1)
        self._adv_combo_Critical_Volume_Algorithm = ttk.Combobox(
            frame, values=["ON", "OFF"], state="readonly", width=9
        )
        self._adv_combo_Critical_Volume_Algorithm.grid(row=row, column=1, padx=5, pady=1, sticky=tk.W)
        self._adv_combo_Critical_Volume_Algorithm.set(self.adv_Critical_Volume_Algorithm)
        row += 1

        add_row(row, "Crit_Volume_Parameter_Lambda (fsw-min):", self.adv_Crit_Volume_Parameter_Lambda, "Crit_Volume_Parameter_Lambda"); row += 1
        add_row(row, "Gradient_Onset_of_Imperm_Atm (atm):", self.adv_Gradient_Onset_of_Imperm_Atm, "Gradient_Onset_of_Imperm_Atm"); row += 1
        add_row(row, "Surface_Tension_Gamma (N/m):", self.adv_Surface_Tension_Gamma, "Surface_Tension_Gamma"); row += 1
        add_row(row, "Skin_Compression_GammaC (N/m):", self.adv_Skin_Compression_GammaC, "Skin_Compression_GammaC"); row += 1
        add_row(row, "Regeneration_Time_Constant (min):", self.adv_Regeneration_Time_Constant, "Regeneration_Time_Constant"); row += 1
        add_row(row, "Pressure_Other_Gases_mmHg:", self.adv_Pressure_Other_Gases_mmHg, "Pressure_Other_Gases_mmHg"); row += 1

        def get_float_entry(attr_name, label):
            e = getattr(self, f"_adv_entry_{attr_name}")
            txt = e.get().strip().replace(",", ".")
            if not txt:
                raise ValueError(f"{label}: valore mancante")
            return float(txt)

        def restore_defaults():
            # Ripristina i default VPM senza toccare Minimum_Deco_Stop_Time (min)
            # (Parametro spostato nel menu 'Parametri deco' e deve restare persistente)
            _keep_min_deco = getattr(self, 'adv_Minimum_Deco_Stop_Time', None)
            self.init_advanced_defaults()
            if _keep_min_deco is not None:
                self.adv_Minimum_Deco_Stop_Time = _keep_min_deco
            win.destroy()
            self.open_advanced_params_window()

        def on_ok():
            try:
                self.adv_Critical_Volume_Algorithm = self._adv_combo_Critical_Volume_Algorithm.get()
                if self.adv_Critical_Volume_Algorithm not in ("ON", "OFF"):
                    raise ValueError("Critical_Volume_Algorithm deve essere ON o OFF.")

                self.adv_Crit_Volume_Parameter_Lambda = get_float_entry("Crit_Volume_Parameter_Lambda", "Crit_Volume_Parameter_Lambda")
                if not (6500.0 <= self.adv_Crit_Volume_Parameter_Lambda <= 8300.0):
                    raise ValueError("Crit_Volume_Parameter_Lambda deve essere tra 6500 e 8300.")

                self.adv_Gradient_Onset_of_Imperm_Atm = get_float_entry("Gradient_Onset_of_Imperm_Atm", "Gradient_Onset_of_Imperm_Atm")
                if not (5.0 <= self.adv_Gradient_Onset_of_Imperm_Atm <= 10.0):
                    raise ValueError("Gradient_Onset_of_Imperm_Atm deve essere tra 5.0 e 10.0 atm.")

                self.adv_Surface_Tension_Gamma = get_float_entry("Surface_Tension_Gamma", "Surface_Tension_Gamma")
                if not (0.010 <= self.adv_Surface_Tension_Gamma <= 0.030):
                    raise ValueError("Surface_Tension_Gamma deve essere tra 0.010 e 0.030 N/m.")

                self.adv_Skin_Compression_GammaC = get_float_entry("Skin_Compression_GammaC", "Skin_Compression_GammaC")
                if not (0.10 <= self.adv_Skin_Compression_GammaC <= 0.50):
                    raise ValueError("Skin_Compression_GammaC deve essere tra 0.10 e 0.50 N/m.")

                self.adv_Regeneration_Time_Constant = get_float_entry("Regeneration_Time_Constant", "Regeneration_Time_Constant")
                if not (10080.0 <= self.adv_Regeneration_Time_Constant <= 51840.0):
                    raise ValueError("Regeneration_Time_Constant deve essere tra 10080 e 51840 min.")

                self.adv_Pressure_Other_Gases_mmHg = get_float_entry("Pressure_Other_Gases_mmHg", "Pressure_Other_Gases_mmHg")
                if not (0.0 <= self.adv_Pressure_Other_Gases_mmHg <= 200.0):
                    raise ValueError("Pressure_Other_Gases_mmHg deve essere tra 0 e 200 mmHg.")

            except ValueError as e:
                messagebox.showerror("Errore parametri avanzati", str(e), parent=win)
                return

            win.destroy()

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=(8, 4), sticky="e")
        ttk.Button(btn_frame, text="Ripristina default", command=restore_defaults).grid(row=0, column=0, padx=5)
        ttk.Button(btn_frame, text="OK", command=on_ok).grid(row=0, column=1, padx=5)


    def _build_effective_descent_rows(self, bottom_depth_m: float):
        """Costruisce righe sintetiche di discesa (DESC + DSTOP) coerenti con:
        - bande configurate (self.descent_speed_bands)
        - soste finali per banda (stop_min)
        - profondità di fondo dell'immersione corrente (bottom_depth_m)

        NOTA: Le bande persistono indipendentemente dalla profondità corrente.
        Qui vengono solo TRONCATE per ottenere i segmenti effettivi da 0 a bottom_depth_m.
        """
        # Copia bande (non modificare self.descent_speed_bands)
        bands = []
        for b in getattr(self, "descent_speed_bands", []) or []:
            try:
                bands.append({
                    "from_m": float(b.get("from_m", 0.0)),
                    "to_m": float(b.get("to_m", 0.0)),
                    "speed": float(b.get("speed", 20.0)),
                    "stop_min": float(b.get("stop_min", 0.0)),
                })
            except Exception:
                continue

        # Speed lookup come nel main
        def _desc_speed_for_depth(depth_m: float) -> float:
            base = 20.0
            try:
                base = float((self.entry_desc_rate.get() or "").strip().replace(",", "."))
            except Exception:
                base = base
            for band in bands:
                try:
                    lo = min(float(band.get("from_m", 0.0)), float(band.get("to_m", 0.0)))
                    hi = max(float(band.get("from_m", 0.0)), float(band.get("to_m", 0.0)))
                    sp = float(band.get("speed", base))
                except Exception:
                    continue
                if lo - 1e-9 <= depth_m <= hi + 1e-9:
                    return max(0.1, sp)
            return max(0.1, base)

        # Soste effettive: profondità=to_m, tempo=stop_min, solo se <= fondo
        stops = []
        for band in bands:
            try:
                d = float(band.get("to_m", 0.0))
                t = float(band.get("stop_min", 0.0))
            except Exception:
                continue
            if d > 0.0 and t > 0.0 and d <= bottom_depth_m + 1e-9:
                stops.append((d, t))
        # unique by depth (somma tempi se ripetute)
        stop_map = {}
        for d, t in stops:
            dkey = round(float(d), 6)
            stop_map[dkey] = stop_map.get(dkey, 0.0) + float(t)

        waypoints = [0.0] + sorted([d for d in stop_map.keys() if d > 0.0 and d <= bottom_depth_m + 1e-9])
        if round(float(bottom_depth_m), 6) not in waypoints:
            waypoints.append(round(float(bottom_depth_m), 6))
        waypoints = sorted(set([round(float(w), 6) for w in waypoints]))

        rows = []
        rt = 0.0
        seg = 1

        for i in range(len(waypoints) - 1):
            start_d = float(waypoints[i])
            end_d = float(waypoints[i + 1])
            if end_d <= start_d + 1e-12:
                continue
            mid = 0.5 * (start_d + end_d)
            v = _desc_speed_for_depth(mid)
            dt = (end_d - start_d) / v if v > 0.0 else 0.0
            rt += dt
            rows.append({
                "Segmento": seg,
                "Tipo": "DESC",
                "Seg_time_min": float(dt),
                "Run_time_min": float(rt),
                "Depth_from_m": float(start_d),
                "Depth_to_m": float(end_d),
                "Depth_m": None,
            })
            seg += 1

            # sosta alla fine della tratta (se prevista)
            t_stop = stop_map.get(round(float(end_d), 6), 0.0)
            if t_stop and t_stop > 0.0:
                rt += float(t_stop)
                rows.append({
                    "Segmento": seg,
                    "Tipo": "DSTOP",
                    "Seg_time_min": float(t_stop),
                    "Run_time_min": float(rt),
                    "Depth_from_m": None,
                    "Depth_to_m": None,
                    "Depth_m": float(end_d),
                })
                seg += 1

        return rows, float(rt)

    def _inject_descent_rows_into_profile(self, parsed_rows, bottom_depth_m: float, bottom_time_min: float):
        """Corregge/integra il profilo stampato dal motore inserendo:
        - discesa a segmenti (bande) + soste di discesa (DSTOP)
        - correzione dei run_time successivi (il motore, per compatibilità, stampa una sola discesa)

        Restituisce una nuova lista di righe con Segmento ricalcolato.
        """
        if not parsed_rows:
            return parsed_rows

        descent_rows, descent_rt = self._build_effective_descent_rows(float(bottom_depth_m))

        idx_bott = None
        printed_bott_rt_end = None
        for i, r in enumerate(parsed_rows):
            if r.get("Tipo") == "BOTT":
                try:
                    d = float(r.get("Depth_m", 0.0))
                except Exception:
                    d = None
                if d is not None and abs(d - float(bottom_depth_m)) < 1e-6:
                    idx_bott = i
                    try:
                        printed_bott_rt_end = float(r.get("Run_time_min", 0.0))
                    except Exception:
                        printed_bott_rt_end = 0.0
                    break

        if idx_bott is None:
            return parsed_rows

        corrected_bott_rt_end = float(descent_rt) + float(bottom_time_min)
        delta = corrected_bott_rt_end - float(printed_bott_rt_end or 0.0)

        new_rows = []
        new_rows.extend(descent_rows)

        for r in parsed_rows[idx_bott:]:
            rr = dict(r)
            try:
                rr["Run_time_min"] = float(rr.get("Run_time_min", 0.0)) + delta
            except Exception:
                pass
            if rr.get("Tipo") == "BOTT":
                try:
                    d = float(rr.get("Depth_m", 0.0))
                except Exception:
                    d = None
                if d is not None and abs(d - float(bottom_depth_m)) < 1e-6:
                    rr["Seg_time_min"] = float(bottom_time_min)
                    rr["Run_time_min"] = float(corrected_bott_rt_end)
            new_rows.append(rr)

        for i, r in enumerate(new_rows, start=1):
            r["Segmento"] = i
        return new_rows
    def on_export_csv(self):
        """Esporta un CSV in formato 'report' (come prototipo mu1.csv): sezioni + tabella profilo (identica al video),
        con separatore ';' per la tabella e decimali con virgola nella tabella.
        """
        if not getattr(self, "last_profile_rows", None):
            messagebox.showinfo(
                "Export CSV",
                "Nessun profilo da esportare. Calcola prima la deco."
            )
            return

        fpath = filedialog.asksaveasfilename(
            title="Salva profilo come CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Tutti i file", "*.*")],
        )
        if not fpath:
            return

        # Helper: converte numeri tipo "12.3" -> "12,3" SOLO per la tabella profilo
        num_re = re.compile(r"^-?\d+(?:\.\d+)?$")

        def _to_decimal_comma(v):
            if v is None:
                return ""
            s = str(v)
            if num_re.match(s):
                return s.replace(".", ",")
            return s

        def _pad_semis(n=14):
            return ";" * n

        def _w(line: str, fh):
            fh.write(line + "\n")

        try:
            # Timestamp con timezone locale
            try:
                ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            except Exception:
                ts = ""

            # Inputs (chiave->valore) dai widget (già pronti per save)
            try:
                inputs = self.collect_inputs_for_save()
            except Exception:
                inputs = {}

            # Gas table (ultima calcolata) per elenco gas
            gas_table = getattr(self, "last_gas_table", None)
            if not gas_table:
                try:
                    gas_table = self.build_gas_table_for_engine()
                except Exception:
                    gas_table = None

            # Risultati dal riquadro GUI
            runtime_tot = getattr(self, "label_runtime", None).cget("text") if getattr(self, "label_runtime", None) else ""
            runtime_deco = getattr(self, "label_deco", None).cget("text") if getattr(self, "label_deco", None) else ""
            start_deco = getattr(self, "label_decozone", None).cget("text") if getattr(self, "label_decozone", None) else ""

            # CNS/OTU finali (dalla last_profile_rows: ultimo valore cumulativo)
            cns_final = ""
            otu_final = ""
            if self.last_profile_rows:
                last = self.last_profile_rows[-1]
                if "CNS_%" in last:
                    cns_final = last.get("CNS_%")
                if "OTU" in last:
                    otu_final = last.get("OTU")

            # Scrittura file
            with open(fpath, "w", encoding="utf-8", newline="") as f:
                # HEADER
                _w(f"Report,VPM-B OC Metrics Export{_pad_semis()}", f)
                _w(f"Generated_at,{ts}{_pad_semis()}", f)
                _w(_pad_semis(), f)

                # INPUTS (ordine prototipo)
                _w(f"Inputs{_pad_semis()}", f)
                # mapping chiavi prototipo
                # nel dict inputs possono esserci stringhe: usiamo come sono
                for k in ("depth", "bottom", "rapsol", "crit_rad_n2", "crit_rad_he"):
                    if k in inputs:
                        _w(f"{k},{inputs.get(k)}{_pad_semis()}", f)
                # eventuali altri input (se presenti) ma non in prototipo -> dopo
                extra_keys = [k for k in inputs.keys() if k not in ("depth", "bottom", "rapsol", "crit_rad_n2", "crit_rad_he")]
                for k in extra_keys:
                    _w(f"{k},{inputs.get(k)}{_pad_semis()}", f)

                _w(_pad_semis(), f)

                # GASES USED
                _w(f"Gases_Used{_pad_semis()}", f)
                _w(f"Type,Mix#,Name,FO2,FHe,FN2,MOD_m,Enabled{_pad_semis()}", f)

                if gas_table:
                    # assume gas_table[0] = fondo, altri = deco
                    for i, g in enumerate(gas_table, start=1):
                        name = g.get("Name", f"mix{i}")
                        fo2 = float(g.get("FO2", 0.0))
                        fhe = float(g.get("FHe", 0.0))
                        fn2 = max(0.0, 1.0 - fo2 - fhe)
                        mod = g.get("MOD_m", g.get("MOD", ""))
                        enabled = g.get("Enabled", 1)
                        gtype = "Bottom" if i == 1 else "Deco"
                        _w(f"{gtype},{i},{name},{fo2:.4f},{fhe:.4f},{fn2:.4f},{mod},{enabled}{_pad_semis()}", f)

                _w(_pad_semis(), f)

                # RESULTS
                _w(f"Results{_pad_semis()}", f)
                _w(f"Runtime_totale_min,{runtime_tot}{_pad_semis()}", f)
                _w(f"Runtime_deco_min,{runtime_deco}{_pad_semis()}", f)
                _w(f"Inizio_zona_deco_m,{start_deco}{_pad_semis()}", f)
                if cns_final != "":
                    try:
                        _w(f"CNS_final_pct,{float(cns_final):.1f}{_pad_semis()}", f)
                    except Exception:
                        _w(f"CNS_final_pct,{cns_final}{_pad_semis()}", f)
                if otu_final != "":
                    try:
                        _w(f"OTU_final,{float(otu_final):.1f}{_pad_semis()}", f)
                    except Exception:
                        _w(f"OTU_final,{otu_final}{_pad_semis()}", f)

                _w(_pad_semis(), f)

                # TABLE (identica al video, ma con ';' e decimali con virgola)
                # ---- DEBUG (FASE 3) ---------------------------------------------------------
                # Queste 4 colonne NON alterano alcuna logica: servono solo per capire
                # bottom_idx / ascent_idx / phase / SP_target effettivamente scelti.
                def _get_row_field(_r, *keys, default=None):
                    for _k in keys:
                        if _k in _r and _r[_k] is not None:
                            return _r[_k]
                    return default

                def _row_tipo(_r):
                    return str(_get_row_field(_r, "Tipo", "tipo", default="")).upper()

                def _row_mode(_r):
                    return str(_get_row_field(_r, "Mode", "mode", default="")).upper()

                def _row_depth_mean(_r):
                    _tip = _row_tipo(_r)
                    try:
                        _z_from = float(_get_row_field(_r, "Depth_from_m", "from", default=0.0) or 0.0)
                        _z_to   = float(_get_row_field(_r, "Depth_to_m", "to", default=0.0) or 0.0)
                        _z_m    = float(_get_row_field(_r, "Depth_m", "depth", default=0.0) or 0.0)
                    except Exception:
                        _z_from, _z_to, _z_m = 0.0, 0.0, 0.0
                    if _tip in ("DESC", "ASC"):
                        return 0.5 * (_z_from + _z_to)
                    return _z_m

                def _infer_bottom_and_ascent_start_debug(_rows):
                    if not _rows:
                        return (0, 0)

                    max_depth = None
                    cand = []
                    for i, r in enumerate(_rows):
                        if _row_tipo(r) != "CONST":
                            continue
                        try:
                            z = float(_get_row_field(r, "Depth_m", "depth", default=0.0) or 0.0)
                        except Exception:
                            z = 0.0
                        if max_depth is None or z > max_depth:
                            max_depth = z
                        cand.append((i, z, r))
                    if not cand or max_depth is None:
                        return (0, 0)

                    tol = max(0.01, 0.001 * max_depth)

                    best_i = None
                    best_t = -1.0
                    for i, z, r in cand:
                        if z < max_depth - tol:
                            continue
                        try:
                            t = float(_get_row_field(r, "Seg_time_min", "seg_time", default=0.0) or 0.0)
                        except Exception:
                            t = 0.0
                        if (t > best_t) or (t == best_t and (best_i is None or i > best_i)):
                            best_t = t
                            best_i = i
                    if best_i is None:
                        best_i = cand[-1][0]

                    ascent_i = 0
                    for j in range(best_i + 1, len(_rows)):
                        if _row_tipo(_rows[j]) == "ASC":
                            ascent_i = j
                            break
                    return (best_i, ascent_i)

                _dbg_bottom_idx, _dbg_ascent_idx = _infer_bottom_and_ascent_start_debug(self.last_profile_rows)

                try:
                    _dbg_msp = bool(self.cc_msp_enabled.get()) if hasattr(self, "cc_msp_enabled") else False
                except Exception:
                    _dbg_msp = False
                try:
                    _dbg_sp_single = _safe_float(self.cc_sp_single.get()) if hasattr(self, "cc_sp_single") else 0.0
                except Exception:
                    _dbg_sp_single = 0.0
                try:
                    _dbg_sp_des = _safe_float(self.cc_sp_descent.get()) if hasattr(self, "cc_sp_descent") else _dbg_sp_single
                except Exception:
                    _dbg_sp_des = _dbg_sp_single
                try:
                    _dbg_sp_d1 = _safe_float(self.cc_sp_deco1.get(), _dbg_sp_single)
                except Exception:
                    _dbg_sp_d1 = _dbg_sp_single
                try:
                    _dbg_sp_d2 = _safe_float(self.cc_sp_deco2.get(), _dbg_sp_d1)
                except Exception:
                    _dbg_sp_d2 = _dbg_sp_d1
                try:
                    _dbg_sp_d3 = _safe_float(self.cc_sp_deco3.get(), _dbg_sp_d2)
                except Exception:
                    _dbg_sp_d3 = _dbg_sp_d2
                try:
                    _dbg_a1 = _safe_float(self.cc_deco1_a.get(), 0.0)
                except Exception:
                    _dbg_a1 = 0.0
                try:
                    _dbg_a2 = _safe_float(self.cc_deco2_a.get(), 0.0)
                except Exception:
                    _dbg_a2 = 0.0
                # -------------------------------------------------------------------------------
                header = [
                    "n","tipo","seg_time","run_time","from","to","depth",
                    "note","mode","gas","ppO2",
                    "Depth_avg","Gas_dens_gL","CNS_%","OTU","EAD_m","GF_actual"
                ]
                _w(";".join(header), f)

                for i, row in enumerate(self.last_profile_rows, start=1):
                    vals = list(self._format_profile_row_for_tree(row, gas_table, show_metrics=True))
                    if vals:
                        vals[0] = str(i)

                    # La _format_profile_row_for_tree in questa GUI produce le colonne "base+metriche" (senza debug).
                    # La _format_profile_row_for_tree produce le colonne base+metriche fino a GF_actual.
                    # Allineiamo alla lunghezza fino a GF_actual (incluso), poi appendiamo LEAD+Pt.
                    base_len = (header.index("GF_actual") + 1) if ("GF_actual" in header) else len(vals)
                    # normalizza lunghezza alle sole colonne base/metriche
                    if len(vals) > base_len:
                        vals = vals[:base_len]
                    elif len(vals) < base_len:
                        vals.extend([""] * (base_len - len(vals)))

                    vals = [_to_decimal_comma(v) for v in vals]
                    _w(";".join(vals), f)

            messagebox.showinfo("Export CSV", f"CSV salvato:\n{fpath}")

        except Exception as e:
            messagebox.showerror("Export CSV", f"Errore durante l'export CSV:\n{e}")


    def on_export_plot_pdf(self):
            """Esporta il grafico del tab "Grafico" in PDF, chiedendo all'utente dove salvarlo (come per il CSV)."""
            if not getattr(self, "last_profile_rows", None):
                try:
                    messagebox.showinfo("Export PDF", "Nessun profilo da esportare. Calcola prima la deco.")
                except Exception:
                    pass
                return

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            default_name = f"grafico_immersione_{ts}.pdf"

            # Directory iniziale: Desktop se esiste, altrimenti home, altrimenti cwd
            try:
                home_dir = os.path.expanduser("~")
                desktop_dir = os.path.join(home_dir, "Desktop")
                initial_dir = desktop_dir if os.path.isdir(desktop_dir) else home_dir
            except Exception:
                initial_dir = os.getcwd()

            fpath = filedialog.asksaveasfilename(
                title="Salva grafico come PDF",
                initialdir=initial_dir,
                initialfile=default_name,
                defaultextension=".pdf",
                filetypes=[("PDF", "*.pdf"), ("Tutti i file", "*.*")],
            )
            if not fpath:
                return

            try:
                try:
                    if hasattr(self, "canvas") and self.canvas:
                        self.canvas.draw_idle()
                except Exception:
                    pass

                if not hasattr(self, "fig") or self.fig is None:
                    raise RuntimeError("Figura matplotlib non disponibile.")

                self.fig.savefig(fpath, format="pdf", bbox_inches="tight")
                try:
                    messagebox.showinfo("Export PDF", f"Grafico esportato in:\n{fpath}")
                except Exception:
                    pass
            except Exception as e:
                try:
                    messagebox.showerror("Export PDF", f"Errore export grafico PDF:\n{e}")
                except Exception:
                    pass
    def _on_tab_changed(self, event=None):
        """Aggiorna il grafico quando si seleziona la TAB Grafico."""
        try:
            tab_text = self.notebook.tab(self.notebook.select(), "text")
        except Exception:
            return
        if tab_text == "Grafico":
            self.update_plot()



    def update_plot(self):
        """Aggiorna il grafico immersione (profilo eseguito + Depth_avg cumulata)."""
        if not hasattr(self, "ax") or not hasattr(self, "canvas"):
            return

        prof = getattr(self, "last_profile_rows", None) or []
        gas_table = getattr(self, "last_gas_table", None)

        self.ax.clear()
        self.ax.set_xlabel("Tempo (min)")
        self.ax.set_ylabel("Profondità (m)")
        self.ax.grid(True)
        # Griglie e tick: X major=60 (con label), X minor=5 (senza label)
        #               Y major=10 (con label), Y minor=1 (senza label)
        from matplotlib.ticker import MultipleLocator
        self.ax.xaxis.set_major_locator(MultipleLocator(30))
        self.ax.xaxis.set_minor_locator(MultipleLocator(5))
        self.ax.yaxis.set_major_locator(MultipleLocator(5))
        self.ax.yaxis.set_minor_locator(MultipleLocator(1))
        # Riduci dimensione font tick (~-20%)
        try:
            self.ax.tick_params(axis='both', which='major', labelsize=8)
            self.ax.tick_params(axis='both', which='minor', labelsize=8)
        except Exception:
            pass
        self.ax.grid(which='minor', linestyle=':', linewidth=0.6, alpha=0.6)

        # Titolo: algoritmo selezionato (sorgente GUI)
        # (titolo algoritmo spostato nella legenda; niente titolo in alto)

        if not prof:
            self.canvas.draw()
            return

        # Helper: gas label for a row (reuse the exact same mapping used by the detailed profile table)
        # We deliberately read the already-defined "gas" column (col 10 in the table output),
        # so the plot uses the same labels with prefix (BOTT_/DECx_/DIL_/BO_x) and the same
        # OC vs CC vs BO rules.
        def _gas_for_row(row):
            try:
                vals = self._format_profile_row_for_tree(row, gas_table, show_metrics=False)
                # cols: n,tipo,seg_time,run_time,from,to,depth,note,mode,gas,ppO2
                lbl = vals[9] if (vals and len(vals) > 9) else ""
            except Exception:
                lbl = ""
            return (None, lbl)


        # Build segments and plot.
        # - Color: determined only by gas label (with prefix), stable across segments.
        # - Style: CC segments are rendered as a "doppio binario" (double thin line),
        #          OC/BO segments are rendered as a normal single line.
        # - Legend: each gas appears only once (even if it spans multiple segments).
        current_gas = None
        current_mode = None
        seg_t = []
        seg_z = []

        color_by_gas = {}
        seen_in_legend = set()
        seen_order = []  # preserve first-appearance order for legend
        gas_has_cc = {}   # gas_label -> True if any segment is CC
        _color_idx = 0

        def _color_for_gas(glabel):
            """Return a deterministic color for a given gas label.

            OC/BO colors are fixed by role/prefix (independent from mix), per UI spec:
              - BOTT_ / BO_1_  -> red
              - DEC1_ / BO_2_  -> purple
              - DEC2_ / BO_3_  -> orange
              - DEC3_ / BO_4_  -> blue
              - DEC4_ / BO_5_  -> green

            CC rendering is handled separately (fixed black/white strokes).
            Never returns None (Matplotlib would fallback to default blue in legend).
            """
            nonlocal _color_idx
            glabel = (glabel or "").strip()

            # Fixed mapping by prefix for OC/BO and BO gases
            fixed = None
            up = glabel.upper()
            if up.startswith("BOTT_") or up.startswith("BO_1_"):
                fixed = "red"
            elif up.startswith("DEC1_") or up.startswith("BO_2_"):
                fixed = "purple"
            elif up.startswith("DEC2_") or up.startswith("BO_3_"):
                fixed = "orange"
            elif up.startswith("DEC3_") or up.startswith("BO_4_"):
                fixed = "blue"
            elif up.startswith("DEC4_") or up.startswith("BO_5_"):
                fixed = "green"

            if fixed is not None:
                color_by_gas[glabel] = fixed
                return fixed

            # Fallback: stable cycler-based color for any other label
            if glabel not in color_by_gas:
                try:
                    c = next(self.ax._get_lines.prop_cycler)["color"]
                except Exception:
                    c = f"C{_color_idx % 10}"
                color_by_gas[glabel] = c
                _color_idx += 1
            return color_by_gas[glabel]

        def _mode_for_row(row):
            try:
                vals = self._format_profile_row_for_tree(row, gas_table, show_metrics=False)
                # cols: n,tipo,seg_time,run_time,from,to,depth,note,mode,gas,ppO2
                return vals[8] if (vals and len(vals) > 8) else ""
            except Exception:
                return ""

        def _plot_segment(tlist, zlist, gas_label, mode_label):
            if not tlist or len(tlist) < 2:
                return
            gas_label = gas_label or ""
            color = _color_for_gas(gas_label)
            want_label = gas_label and (gas_label not in seen_in_legend)
            if want_label:
                seen_in_legend.add(gas_label)
                seen_order.append(gas_label)
            # track whether this gas ever appears in CC
            if gas_label:
                if str(mode_label).upper().startswith("CC"):
                    gas_has_cc[gas_label] = True
                else:
                    gas_has_cc.setdefault(gas_label, False)
            label = None  # custom legend will be built after plotting

            base_lw = 1.8  # final stroke width (CC and OC/BO); previously CC outer was 3.6 -> now ~1/2
            if str(mode_label).upper().startswith("CC"):
                # CC fixed style: thin double line (black outer, yellow inner), independent of gas color
                self.ax.plot(tlist, zlist, color="black", linewidth=base_lw,
                             solid_capstyle='round', label=None, zorder=3)
                self.ax.plot(tlist, zlist, color="yellow", linewidth=max(0.6, base_lw * 0.55),
                             solid_capstyle='round', label=label, zorder=4)
            else:
                self.ax.plot(tlist, zlist, color=color, linewidth=base_lw,
                             solid_capstyle='round', label=label)

        for row in prof:
            seg_min = float(row.get("Seg_time_min") or 0.0)
            t1 = float(row.get("Run_time_min") or 0.0)
            t0 = t1 - seg_min

            tipo = str(row.get("Tipo","")).upper()
            if tipo in ("ASC", "DESC"):
                z0 = float(row.get("Depth_from_m") or 0.0)
                z1 = float(row.get("Depth_to_m") or 0.0)
            else:
                z0 = float(row.get("Depth_m") or 0.0)
                z1 = z0

            _g, glabel = _gas_for_row(row)
            mlabel = _mode_for_row(row)

            if (glabel != current_gas) or (mlabel != current_mode):
                # flush previous polyline
                _plot_segment(seg_t, seg_z, current_gas, current_mode)
                seg_t = [t0, t1]
                seg_z = [z0, z1]
                current_gas = glabel
                current_mode = mlabel
            else:
                # extend
                seg_t += [t0, t1]
                seg_z += [z0, z1]

        _plot_segment(seg_t, seg_z, current_gas, current_mode)


        # Depth_avg cumulativa (a fine step)
        t_avg = []
        z_avg = []
        for row in prof:
            t = float(row.get("Run_time_min") or 0.0)
            z = row.get("Depth_avg", None)
            if z is None:
                continue
            try:
                z = float(z)
            except Exception:
                continue
            t_avg.append(t)
            z_avg.append(z)

        if t_avg:
            self.ax.plot(t_avg, z_avg, linestyle="--", label="depth avg")

        # Depth positive down: invert axis
        try:
            self.ax.invert_yaxis()
        except Exception:
            pass

        # Legend (depth avg + gas labels)
        try:
            # Custom legend:
            # - one entry per GAS (with prefix), in first-appearance order
            # - GAS that appears in CC is shown with the same "double line" (doppio binario) style
            from matplotlib.lines import Line2D
            from matplotlib.legend_handler import HandlerBase

            class _HandlerCCDoubleBW(HandlerBase):
                def __init__(self, lw_under, lw_over):
                    super().__init__()
                    self.lw_under = lw_under
                    self.lw_over = lw_over

                def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
                    # Draw two coincident horizontal lines inside the legend handle box
                    y = ydescent + 0.5 * height
                    x0 = xdescent
                    x1 = xdescent + width
                    cap = orig_handle.get_solid_capstyle()
                    # Fixed CC style: black outer stroke + white inner stroke
                    l1 = Line2D([x0, x1], [y, y], color="black", linewidth=self.lw_under,
                                solid_capstyle=cap, transform=trans)
                    l2 = Line2D([x0, x1], [y, y], color="yellow", linewidth=self.lw_over,
                                solid_capstyle=cap, transform=trans)
                    return [l1, l2]

            handles = []
            labels = []
            handler_map = {}

            base_lw = 1.8  # aligned with _plot_segment

            for glabel in seen_order:
                if not glabel:
                    continue
                c = _color_for_gas(glabel)
                # CC segments are rendered with fixed black/white style (independent of gas color)
                if gas_has_cc.get(glabel, False):
                    c = "black"
                h = Line2D([], [], color=c, linewidth=base_lw, solid_capstyle='round')
                handles.append(h)
                labels.append(glabel)
                if gas_has_cc.get(glabel, False):
                    handler_map[h] = _HandlerCCDoubleBW(base_lw, max(0.6, base_lw * 0.55))


            # Prima riga legenda: algoritmo (VPMB / ZH-L16)
            try:
                algo = "VPM"
                if getattr(self, "_last_calc_algo", None):
                    algo = str(self._last_calc_algo).strip().upper()
                else:
                    algo = str(self.deco_model_var.get() or "VPM").strip().upper()
                algo_label = ("ZH-L16" if algo == "ZHL16" else "VPMB")
                handles.insert(0, Line2D([], [], linestyle="None", marker="", color="none"))
                labels.insert(0, algo_label)
            except Exception:
                pass

            # Add depth avg (if present) as last entry
            if t_avg:
                handles.append(Line2D([], [], color="black", linestyle="--", linewidth=1.2))
                labels.append("depth avg")

            if handles:
                self.ax.legend(handles=handles, labels=labels, loc="best",
                               handler_map=handler_map)
        except Exception:
            # fallback: do not crash plotting
            try:
                self.ax.legend(loc="best")
            except Exception:
                pass

        self.canvas.draw()



    # --- CCR logical checks: Setpoint vs diluent FO2 and deco depths order (GUI-only) ---
    def _safe_float(self, s):
        try:
            if s is None:
                return None
            t = str(s).strip().replace(",", ".")
            if t == "":
                return None
            return float(t)
        except Exception:
            return None

    def _safe_float_entry(self, entry):
        try:
            return self._safe_float(entry.get())
        except Exception:
            return None

    def _set_entry_error(self, entry_widget, is_error: bool):
        if not entry_widget:
            return
        try:
            entry_widget.configure(style=("Error.TEntry" if is_error else "TEntry"))
        except Exception:
            try:
                entry_widget.configure(style=("Error.TEntry" if is_error else ""))
            except Exception:
                pass


    def get_cc_setpoint_segments(self):
        """Build CCR setpoint segments (GUI→MAIN interface, deterministic).

        Philosophy (V-Planner like):
        - User setpoints are *desired* setpoints.
        - The effective inspired ppO2 will be handled by the main using the physical clamp:
              SP_eff(d) = min( max(SP_req, Pamb(d)*FO2_dil), Pamb(d)*1.0 )
          (PVAPOR belongs to the main only.)
        - GUI does NOT invalidate setpoints due to 'incompatibility' with the diluent.
        - Minimal GUI constraints:
            * SP singolo è REQUIRED (otherwise CC calculation cannot be defined).
            * Depth thresholds must be numeric and ordered: Bottom >= Deco1_a >= Deco2_a >= 0.

        Returns:
            segments: list[dict] with keys:
                - name: str  (Descent, Bottom, Deco1, Deco2, Deco3)
                - depth_from_m: float
                - depth_to_m: float
                - setpoint_req_ata: float
                - bottom_time_min: float|None (only Bottom)
            errors: list[str] blocking issues.
        """
        segments = []
        errors = []

        # ---- parse core values
        try:
            depth_bottom = self._safe_float(getattr(self, "entry_depth", None).get())
        except Exception:
            depth_bottom = None

        try:
            bottom_time = self._safe_float(getattr(self, "entry_bottom_time", None).get())
        except Exception:
            bottom_time = None

        deco1_a = self._safe_float(getattr(self, "cc_deco1_a", None).get() if getattr(self, "cc_deco1_a", None) else None)
        deco2_a = self._safe_float(getattr(self, "cc_deco2_a", None).get() if getattr(self, "cc_deco2_a", None) else None)

        # setpoints (requested)
        sp_descent = self._safe_float(getattr(self, "cc_sp_descent", None).get() if getattr(self, "cc_sp_descent", None) else None)
        # SP singolo: fonte unica (anche quando Multi-SP è attivo). SP Fondo tabella è solo mirror UI.
        sp_single = self._safe_float(getattr(self, "cc_sp_single", None).get() if getattr(self, "cc_sp_single", None) else None)
        sp_bottom  = sp_single
        sp_deco1   = self._safe_float(getattr(self, "cc_sp_deco1", None).get() if getattr(self, "cc_sp_deco1", None) else None)
        sp_deco2   = self._safe_float(getattr(self, "cc_sp_deco2", None).get() if getattr(self, "cc_sp_deco2", None) else None)
        sp_deco3   = self._safe_float(getattr(self, "cc_sp_deco3", None).get() if getattr(self, "cc_sp_deco3", None) else None)

        # defaults and required fields
        if sp_descent is None:
            sp_descent = 0.70

        if depth_bottom is None:
            errors.append("Profondità di fondo mancante/non valida.")
            return segments, errors

        if sp_single is None:
            errors.append("Impostare SP singolo [atm].")
            return segments, errors

        # fill-forward for deco SPs
        if sp_deco1 is None:
            sp_deco1 = sp_bottom
        if sp_deco2 is None:
            sp_deco2 = sp_deco1
        if sp_deco3 is None:
            sp_deco3 = sp_deco2

        # validate thresholds (needed to define segments)
        msp_enabled = False
        try:
            msp_enabled = bool(getattr(self, 'cc_msp_enabled', None).get())
        except Exception:
            msp_enabled = False
        if msp_enabled and (deco1_a is None or deco2_a is None):
            errors.append("Quote soglia deco CCR mancanti/non valide (Deco1_a e/o Deco2_a).")
            return segments, errors

        if msp_enabled and not (depth_bottom >= deco1_a >= deco2_a >= 0.0):
            errors.append("Quote soglia deco CCR non coerenti: Bottom ≥ Deco1_a ≥ Deco2_a ≥ 0.")
            return segments, errors

        # ---- build deterministic segments (requested setpoints)
        segments.append({
            "name": "Descent",
            "depth_from_m": 0.0,
            "depth_to_m": float(depth_bottom),
            "setpoint_req_ata": float(sp_descent),
            "bottom_time_min": None,
        })
        segments.append({
            "name": "Bottom",
            "depth_from_m": float(depth_bottom),
            "depth_to_m": float(depth_bottom),
            "setpoint_req_ata": float(sp_bottom),
            "bottom_time_min": None if bottom_time is None else float(bottom_time),
        })
        segments.append({
            "name": "Deco1",
            "depth_from_m": float(depth_bottom),
            "depth_to_m": float(deco1_a),
            "setpoint_req_ata": float(sp_deco1),
            "bottom_time_min": None,
        })
        segments.append({
            "name": "Deco2",
            "depth_from_m": float(deco1_a),
            "depth_to_m": float(deco2_a),
            "setpoint_req_ata": float(sp_deco2),
            "bottom_time_min": None,
        })
        segments.append({
            "name": "Deco3",
            "depth_from_m": float(deco2_a),
            "depth_to_m": 0.0,
            "setpoint_req_ata": float(sp_deco3),
            "bottom_time_min": None,
        })

        return segments, errors

    def _validate_ccr_sp_logic(self):
        """CCR GUI logic (V-Planner-like): no 'incompatibility' errors for SP vs diluent.

        - SP bottom is required (otherwise CC plan is undefined).
        - Deco threshold depths must be numeric and ordered (Bottom ≥ Deco1_a ≥ Deco2_a ≥ 0).
        - Effective ppO2 clamping is handled by the main using FO2_dil and depth step-by-step.
        """
        # Generic information (always)
        try:
            self.lbl_cc_sp_info.config(text='Avviso: i SP selezionati sono applicati finché è possibile sulla base delle combinazioni tra profondità e frazione di ossigeno; successivamente vengono sostituiti dalle ppO2 fisicamente possibili.')
        except Exception:
            pass

        # Reset messages
        try:
            self.lbl_cc_sp_error.config(text="")
        except Exception:
            pass

        # Reset entry colors
        for w in [getattr(self, "entry_cc_sp_descent", None),
                  getattr(self, "entry_cc_sp_single", None),
                  getattr(self, "entry_cc_sp_bottom", None),
                  getattr(self, "entry_cc_sp_deco1", None),
                  getattr(self, "entry_cc_sp_deco2", None),
                  getattr(self, "entry_cc_sp_deco3", None),
                  getattr(self, "entry_cc_deco1_a", None),
                  getattr(self, "entry_cc_deco2_a", None)]:
            if w is not None:
                try:
                    self._set_entry_error(w, False)
                except Exception:
                    pass

        # Parse required SP singolo (fonte unica)
        sp_single = self._safe_float(getattr(self, "cc_sp_single", None).get() if getattr(self, "cc_sp_single", None) else None)
        if sp_single is None:
            try:
                self._set_entry_error(self.entry_cc_sp_single, True)
                self.lbl_cc_sp_error.config(text="Impostare SP singolo [atm].")
            except Exception:
                pass
            return
        # Validate threshold depths ordering (structural, not physiological)
        depth_bottom = self._safe_float(getattr(self, "entry_depth", None).get() if getattr(self, "entry_depth", None) else None)
        deco1_a = self._safe_float(getattr(self, "cc_deco1_a", None).get() if getattr(self, "cc_deco1_a", None) else None)
        deco2_a = self._safe_float(getattr(self, "cc_deco2_a", None).get() if getattr(self, "cc_deco2_a", None) else None)

        # If depths are missing, keep it silent here (other GUI checks already handle profile inputs)
        if depth_bottom is None or deco1_a is None or deco2_a is None:
            return

        msp_enabled = False
        try:
            msp_enabled = bool(getattr(self, 'cc_msp_enabled', None).get())
        except Exception:
            msp_enabled = False
        if msp_enabled and not (depth_bottom >= deco1_a >= deco2_a >= 0.0):
            try:
                self._set_entry_error(self.entry_cc_deco1_a, True)
                self._set_entry_error(self.entry_cc_deco2_a, True)
                try:
                    msp_enabled = bool(getattr(self, 'cc_msp_enabled', None).get())
                except Exception:
                    msp_enabled = False
                if msp_enabled:
                    self.lbl_cc_sp_error.config(text="Quote soglia deco CCR non coerenti: Bottom ≥ Deco1_a ≥ Deco2_a ≥ 0.")

            except Exception:
                pass
            return
    def _attach_ccr_logic_traces(self):
        """Aggancia i trace/callback per rieseguire le validazioni CCR al variare degli input."""
        # StringVar traces
        for var_name in ["cc_sp_single", "cc_sp_descent", "cc_sp_bottom", "cc_sp_deco1", "cc_sp_deco2", "cc_sp_deco3",
                         "cc_deco1_a", "cc_deco2_a", "cc_dil_fo2"]:
            v = getattr(self, var_name, None)
            if v is None:
                continue
            try:
                v.trace_add("write", lambda *args: self._validate_ccr_sp_logic())
            except Exception:
                try:
                    v.trace("w", lambda *args: self._validate_ccr_sp_logic())
                except Exception:
                    pass

        # Entry depth key release
        try:
            self.entry_depth.bind("<KeyRelease>", lambda e: self._validate_ccr_sp_logic())
        except Exception:
            pass

        # Run once
        try:
            self.after(50, self._validate_ccr_sp_logic)
        except Exception:
            self._validate_ccr_sp_logic()

    def open_zhl16_params_window(self):
        """Finestra Parametri ZH-L16: coefficienti a/b per 16 compartimenti (N2 + He).
        Supporta due set: ZH-L16C e ZH-L16B (selezione tramite dropdown).
        Nota: la GUI mantiene entrambi i set; il pulsante "Ripristina default" resetta entrambi.
        """
        try:
            import tkinter.messagebox as messagebox
        except Exception:
            messagebox = None

        # --- working copies (Cancel discards, OK commits) ---
        coeff_tmp = {
            "C": [tuple(r) for r in getattr(self, "zhl16_coeffs_C", getattr(self, "zhl16_coeffs", []))],
            "B": [tuple(r) for r in getattr(self, "zhl16_coeffs_B", getattr(self, "zhl16_coeffs", []))],
        }
        defaults = {
            "C": [tuple(r) for r in getattr(self, "zhl16_coeff_defaults_C", getattr(self, "zhl16_coeff_defaults", []))],
            "B": [tuple(r) for r in getattr(self, "zhl16_coeff_defaults_B", getattr(self, "zhl16_coeff_defaults", []))],
        }

        # Se per qualche ragione il set B non è presente, inizializza a copia del C (fail-safe, ma non dovrebbe accadere)
        if not coeff_tmp["B"]:
            coeff_tmp["B"] = [tuple(r) for r in coeff_tmp["C"]]
        if not defaults["B"]:
            defaults["B"] = [tuple(r) for r in defaults["C"]]

        current_variant = str(getattr(self, "zhl16_variant", "C") or "C").strip().upper()
        if current_variant not in ("B", "C"):
            current_variant = "C"

        win = tk.Toplevel(self)
        win.title(f"Parametri ZH-L16 (ZH-L16{current_variant})")
        try:
            self.zhl16_variant = current_variant
            if hasattr(self, "btn_param_zhl16"):
                self.btn_param_zhl16.config(text=f"Parametri ZH-L16{current_variant}")
        except Exception:
            pass
        win.transient(self)
        try:
            win.grab_set()
        except Exception:
            pass

        container = ttk.Frame(win, padding=10)
        container.pack(fill="both", expand=True)

        # Header row: label + dropdown
        hdr_row = ttk.Frame(container)
        hdr_row.pack(fill="x", pady=(0, 8))

        hdr = ttk.Label(
            hdr_row,
            text="Coefficienti ZH-L16 (default modificabili)",
            font=("TkDefaultFont", 10, "bold"),
        )
        hdr.pack(side="left")

        var_sel = tk.StringVar(value=f"ZH-L16{current_variant}")
        cmb = ttk.Combobox(
            hdr_row,
            textvariable=var_sel,
            state="readonly",
            width=9,
            values=("ZH-L16C", "ZH-L16B"),
        )
        cmb.pack(side="left", padx=(10, 0))

        table = ttk.Frame(container)
        table.pack(fill="both", expand=True)

        headers = ["cmpt", "a for N2", "b for N2", "a for He", "b for He"]
        for c, h in enumerate(headers):
            ttk.Label(table, text=h).grid(row=0, column=c, padx=6, pady=(0, 6), sticky="w")

        # Vars (persistenti finché la finestra resta aperta)
        self._zhl16_coeff_vars = []

        # Build rows once (16 compartments)
        seed = coeff_tmp.get(current_variant) or []
        for r, row in enumerate(seed, start=1):
            cmpt, a_n2, b_n2, a_he, b_he = row

            ttk.Label(table, text=str(cmpt)).grid(row=r, column=0, padx=6, pady=2, sticky="w")

            v_a_n2 = tk.StringVar(value=f"{float(a_n2):.4f}")
            v_b_n2 = tk.StringVar(value=f"{float(b_n2):.4f}")
            v_a_he = tk.StringVar(value=f"{float(a_he):.4f}")
            v_b_he = tk.StringVar(value=f"{float(b_he):.4f}")

            e1 = ttk.Entry(table, width=10, textvariable=v_a_n2)
            e2 = ttk.Entry(table, width=10, textvariable=v_b_n2)
            e3 = ttk.Entry(table, width=10, textvariable=v_a_he)
            e4 = ttk.Entry(table, width=10, textvariable=v_b_he)

            e1.grid(row=r, column=1, padx=6, pady=2, sticky="w")
            e2.grid(row=r, column=2, padx=6, pady=2, sticky="w")
            e3.grid(row=r, column=3, padx=6, pady=2, sticky="w")
            e4.grid(row=r, column=4, padx=6, pady=2, sticky="w")

            self._zhl16_coeff_vars.append((cmpt, v_a_n2, v_b_n2, v_a_he, v_b_he))

        def _parse_vars_to_rows():
            rows = []
            for cmpt, v_a_n2, v_b_n2, v_a_he, v_b_he in self._zhl16_coeff_vars:
                a_n2 = float(v_a_n2.get().replace(",", "."))
                b_n2 = float(v_b_n2.get().replace(",", "."))
                a_he = float(v_a_he.get().replace(",", "."))
                b_he = float(v_b_he.get().replace(",", "."))
                rows.append((int(cmpt), a_n2, b_n2, a_he, b_he))
            return rows

        def _load_rows_to_vars(rows):
            try:
                for i, row in enumerate(rows):
                    cmpt, a_n2, b_n2, a_he, b_he = row
                    _cmpt, v_a_n2, v_b_n2, v_a_he, v_b_he = self._zhl16_coeff_vars[i]
                    v_a_n2.set(f"{float(a_n2):.4f}")
                    v_b_n2.set(f"{float(b_n2):.4f}")
                    v_a_he.set(f"{float(a_he):.4f}")
                    v_b_he.set(f"{float(b_he):.4f}")
            except Exception:
                pass

        def _commit_current_to_tmp():
            nonlocal current_variant
            try:
                coeff_tmp[current_variant] = _parse_vars_to_rows()
                return True
            except Exception as e:
                if messagebox:
                    messagebox.showerror("Parametri ZH-L16", f"Valore non valido nella tabella:\n{e}")
                return False

        def _switch_variant(_evt=None):
            nonlocal current_variant
            sel = str(var_sel.get() or "").strip().upper()
            new_variant = "C" if sel.endswith("C") else "B"
            if new_variant == current_variant:
                return
            if not _commit_current_to_tmp():
                # revert combobox
                var_sel.set(f"ZH-L16{current_variant}")
                return
            current_variant = new_variant
            win.title(f"Parametri ZH-L16 (ZH-L16{current_variant})")
            try:
                self.zhl16_variant = current_variant
                if hasattr(self, "btn_param_zhl16"):
                    self.btn_param_zhl16.config(text=f"Parametri ZH-L16{current_variant}")
            except Exception:
                pass
            _load_rows_to_vars(coeff_tmp[current_variant])

        cmb.bind("<<ComboboxSelected>>", _switch_variant)


        # ---- GF ramp anchor (where GF low is applied) ----
        try:
            if not hasattr(self, "var_zhl_gf_ramp_anchor") or self.var_zhl_gf_ramp_anchor is None:
                self.var_zhl_gf_ramp_anchor = tk.StringVar(value="SODZ")
        except Exception:
            pass
        anchor_box = ttk.LabelFrame(container, text="Ancoraggio rampa GF (GF low)")
        anchor_box.pack(fill="x", pady=(8, 0))
        ttk.Radiobutton(anchor_box, text="GF low @ 1° stop", variable=self.var_zhl_gf_ramp_anchor, value="1STOP").pack(side="left")
        ttk.Radiobutton(anchor_box, text="GF low @ SoDZ", variable=self.var_zhl_gf_ramp_anchor, value="SODZ").pack(side="left", padx=(12, 0))
        ttk.Radiobutton(anchor_box, text="GF low @ Bottom", variable=self.var_zhl_gf_ramp_anchor, value="BOTTOM").pack(side="left", padx=(12, 0))


        hi_anchor_box = ttk.LabelFrame(container, text="Ancoraggio rampa GF (GF high)")
        hi_anchor_box.pack(fill="x", pady=(8, 0))
        ttk.Radiobutton(hi_anchor_box, text="GF high @ Surface (0 m)", variable=self.var_zhl_gf_ramp_hi_anchor, value="SURFACE").pack(side="left", padx=(12, 0))
        ttk.Radiobutton(hi_anchor_box, text="GF high @ Last stop", variable=self.var_zhl_gf_ramp_hi_anchor, value="LASTSTOP").pack(side="left", padx=(12, 0))


        btns = ttk.Frame(container)
        btns.pack(fill="x", pady=(10, 0))

        def _restore_defaults():
            nonlocal current_variant
            coeff_tmp["C"] = [tuple(r) for r in defaults["C"]]
            coeff_tmp["B"] = [tuple(r) for r in defaults["B"]]
            _load_rows_to_vars(coeff_tmp[current_variant])

        def _ok():
            nonlocal current_variant
            if not _commit_current_to_tmp():
                return

            # Commit to self (both sets)
            try:
                self.zhl16_coeffs_C = [tuple(r) for r in coeff_tmp["C"]]
                self.zhl16_coeffs_B = [tuple(r) for r in coeff_tmp["B"]]
                self.zhl16_variant = current_variant

                # Update backward-compat alias
                if current_variant == "B":
                    self.zhl16_coeff_defaults = getattr(self, "zhl16_coeff_defaults_B", getattr(self, "zhl16_coeff_defaults_C", self.zhl16_coeff_defaults))
                    self.zhl16_coeffs = self.zhl16_coeffs_B
                else:
                    self.zhl16_coeff_defaults = getattr(self, "zhl16_coeff_defaults_C", self.zhl16_coeff_defaults)
                    self.zhl16_coeffs = self.zhl16_coeffs_C
            except Exception:
                pass

            try:
                self._request_autosave()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        def _cancel():
            try:
                win.destroy()
            except Exception:
                pass

        ttk.Button(btns, text="Ripristina default", command=_restore_defaults).pack(side="left")
        ttk.Button(btns, text="Annulla", command=_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="OK", command=_ok).pack(side="right")

# -----------------------------------------------------------------------------
# DISCLAIMER / CONSENSO (sempre richiesto) — schermata di ingresso
# -----------------------------------------------------------------------------
DS_ICO_B64 = """AAABAAUAgIAAAAEAIAAoCAEAVgAAAEBAAAABACAAKEIAAH4IAQAwMAAAAQAgAKglAACmSgEAICAA
AAEAIACoEAAATnABABAQAAABACAAaAQAAPaAAQAoAAAAgAAAAAABAAABACAAAAAAAAAAAQAjLgAA
Iy4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB3OgABdzoALHc6AGR3OgCSdzoAt3c6
ANJ3OgDkdzoA7Xc6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA
73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDv
dzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93
OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6
AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA
73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDv
dzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93OgDvdzoA73c6AO93
OgDvdzoA73c6AOUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAHc6AAN3OgBHdzoAnXc6AOh3OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA9AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAHc6AA93OgB3dzoA4Xc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAHc6AAd3OgB2dzoA7nc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6APQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHc6AAB3
OgBAdzoA2nc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA9AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB3OgAFdzoAi3c6
AP13OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdzoAFHc6AMF3OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6APQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHc6ACB3OgDadzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA9AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB3OgAgdzoA4Xc6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdzoAFHc6ANp3OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
APQAAAAAAAAAAAAAAAAAAAAAAAAAAHc6AAV3OgDBdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
9AAAAAAAAAAAAAAAAAAAAAB3OgAAdzoAi3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0
AAAAAAAAAAAAAAAAAAAAAHc6AEF3OgD9dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APQA
AAAAAAAAAAAAAAB3OgAIdzoA2nc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9AAA
AAAAAAAAAAAAAHc6AHd3OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0AAAA
AAAAAAB3OgAPdzoA7nc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APQAAAAA
AAAAAHc6AHh3OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9AAAAAB3
OgADdzoA4nc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0AAAAAHc6
AEh3OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APQAAAAAdzoA
n3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AAJ3OgDq
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0dzoALnc6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/eTwA/4JGAP+MUAD/lVoA/55iAP+lagD/q3AA/7F2AP+1egD/uX4A
/7uAAP+9ggD/voMA/76DAP+8gQD/un8A/7Z7AP+ydwD/rXEA/6ZqAP+fYgD/llkA/4tPAP+AQwD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgBmdzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP94OwD/g0YB
/5JXAv+hZgP/rnQD/7qBA//EiwP/xYwB/8WMAP/FiwD/xYsA/8SKAP/EigD/xIkA/8OJAP/DiQD/
w4gA/8KIAP/ChwD/wocA/8GHAP/BhgD/wYYA/8CFAP/AhQD/wIQA/8CEAP+/gwD/v4MA/76DAP+5
fgD/rG8A/5tfAP+KTQD/eTwA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AJR3OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/ej0B/4xQA/+fZQX/sngH/8KKB//Hjwf/
x44G/8eOBf/HjgX/xo0E/8aNA//FjAH/xYwB/8WLAP/FiwD/xIoA/8SKAP/EiQD/w4kA/8OJAP/D
iAD/wogA/8KHAP/ChwD/wYcA/8GGAP/BhgD/wIUA/8CFAP/AhAD/wIQA/7+DAP+/gwD/voMA/76C
AP++ggD/voEA/72BAP+6fQD/pmoA/49SAP96PQD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0dzoAuHc6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP+FSQL/nWMG/7R7Cf/Gjgv/yZEL/8iQCv/IkAn/yJAI/8ePB//H
jgb/x44F/8eOBf/GjQT/xo0D/8WMAf/FjAH/xYwA/8WLAP/EigD/xIoA/8SJAP/DiQD/w4kA/8OI
AP/CiAD/wogA/8KHAP/BhwD/wYYA/8GGAP/AhQD/wIUA/8CEAP/AhAD/wIQA/7+DAP++gwD/voIA
/76CAP++gQD/vYEA/72AAP+8gAD/vIAA/7h7AP+eYQD/gEMA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDUdzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/4lNA/+kagj/v4cL/8qTDP/Kkwz/yZIM/8mRC//JkQv/yJAK/8iQCf/IkAj/x48H/8eO
Bv/Hjgb/x44F/8aNBP/GjQP/xYwC/8WMAf/FjAD/xYsA/8WLAP/EigD/xIkA/8OJAP/DiQD/w4gA
/8KIAP/CiAD/wocA/8KHAP/BhgD/wYYA/8CFAP/AhQD/wIQA/8CEAP/AhAD/v4MA/76DAP++ggD/
voIA/76BAP+9gQD/vYAA/7yAAP+8gAD/vH8A/7t/AP+6fQD/oWQA/39CAP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AOZ3OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/iU4E/6Zs
CP/Big3/y5UO/8uUDv/LlA3/ypMM/8qTDP/Jkgz/yZIM/8mRC//IkAr/yJAK/8iQCP/Hjwf/x44G
/8eOBv/HjgX/xo0E/8aNA//FjAL/xYwB/8WMAP/FiwD/xYsA/8SKAP/EigD/w4kA/8OJAP/DiAD/
wogA/8KIAP/ChwD/wocA/8GGAP/BhgD/wIUA/8CFAP/AhAD/wIQA/8CEAP+/gwD/voMA/76DAP++
ggD/voEA/72BAP+9gAD/vIAA/7yAAP+8fwD/u38A/7t/AP+7fgD/t3sA/5ZaAP94OwD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP+JTQP/pmwJ/8OLDf/Nlg//zZYO
/8yWDv/LlQ7/y5QO/8uUDf/Kkw3/ypMM/8mSDP/Jkgz/yZEL/8mRC//IkAr/yJAI/8ePB//Hjgb/
x44G/8eOBf/GjQT/xo0D/8WMAv/FjAH/xYwA/8WLAP/FiwD/xIoA/8SKAP/DiAD/wYcA/76DAP+7
gAD/uX4A/7l+AP+5fgD/uX4A/7uAAP++gwD/v4QA/8CEAP/AhAD/wIQA/7+DAP++gwD/voMA/76C
AP++ggD/vYEA/72AAP+8gAD/vIAA/7x/AP+7fwD/u38A/7t+AP+6fgD/un0A/6tuAP9/QgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzsA/4lOBP+mbQn/w4wO/86YD//Olw//zpcP/82WD//Nlg7/
zJYO/8uVDv/LlA7/y5QN/8qTDf/Kkwz/yZIM/8mSDP/JkQv/yZEL/8iQCv/IkAj/x48H/8eOBv/H
jgb/x44F/8aNBP++hQP/sngC/6dtAf+dYgD/lFkA/4xQAP+FSQD/f0IA/3o9AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP96PQD/f0IA/4ZJAP+NUQD/lloA/6BkAP+rbwD/t3sA
/76CAP+9gQD/vYEA/72AAP+8gAD/vH8A/7t/AP+7fwD/u34A/7p+AP+6fQD/un0A/7R3AP+GSQD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP94OwD/jFEF/6lwCv/Fjg//z5oQ/8+ZEP/PmRD/zpgP/86XD//Olw//zZYP/82WDv/M
lg7/y5UO/8uUDv/LlA3/ypMN/8qTDP/Jkgz/yZIM/8mRC//JkQv/yJAK/8WNCP+2fQb/pmwE/5dc
A/+JTQH/fD8A/3c6AP93OgD/dzoA/3k9Av+ESgz/jlgW/5djH/+ebSb/pHQs/6h6MP+rfjP/rYA0
/62ANP+sfjP/qXsx/6V2LP+fbif/mGUg/5BaGP+GTg7/fEAE/3c6AP93OgD/dzoA/3c6AP93OgD/
f0IA/41RAP+eYQD/sHMA/7yAAP+8fwD/u38A/7t/AP+7fgD/un4A/7p9AP+6fQD/uXwA/7Z5AP+J
TAD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3xAAf+U
Wgb/r3cM/8mUEP/QmxH/0JsR/9CaEf/PmhD/z5kQ/8+ZEP/OmBD/zpgP/86XD//Nlg//zZYO/8yW
Dv/LlQ7/y5QO/8uUDf/Lkw3/ypMM/8mSDP/Diwv/r3YI/5thBf+ITQL/eTwA/3c6AP93OgD/dzoA
/3c6AP93OgD/kVwa/8WfTP/WtVz/4cRn/+PGaf/jxmn/48Zp/+PGaf/jxmn/48Zp/+PGaf/jxmn/
48Zp/+PGaf/jxmn/48Zo/+LGZ//ixWb/4sNk/+LCY//hwGH/2bZZ/8ukTP+7jz3/qnkt/5diHP+D
SQr/dzoA/3c6AP93OgD/fkEA/5JVAP+pbAD/un4A/7t+AP+6fgD/un0A/7p9AP+5fAD/uXwA/7Z4
AP+FSAD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/45VE/+NUxL/eDsB
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP94PAD/ik8F/6JqCv+7hQ//z5sT/9Gd
Ev/RnBP/0ZwR/9CbEf/QmxH/0JoR/8+aEP/PmRD/z5kQ/86YEP/OmA//zpcP/86XD//Nlg7/zJYO
/8uVDv/LlA7/yZIN/7V8Cv+eZAb/h0wD/3g7AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/i1QU/8ypU//jxmn/48Zp/+PGaf/jxmn/48Zp/+PGaf/jxmn/48Zp/+PGaf/j
xmn/48Zp/+PGaf/jxmj/4sZn/+LFZv/iw2T/4sJj/+HBYv/hwWH/4b9g/+C+Xv/gvV3/4L1b/9+7
Wv/UrVD/vY87/6NvJP+HTQ3/dzoA/3c6AP9+QQD/l1oA/7J1AP+6fQD/un0A/7l8AP+5fAD/uXsA
/7J0AP99QAD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/6BpIf/RoUb/
vYo1/6JrIP+JTw3/eDsB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/fD8C/45UB/+jagv/uIIQ/82ZFP/TnxX/0p4U/9KeE//RnRP/0Z0T
/9GcEv/RnBH/0JsR/9CbEf/QmhH/z5oQ/8+ZEP/PmRD/zpgQ/86YD//Olw//zpcP/82WD//DjA3/
qnEJ/5FWBP97PwH/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/eT0C/4lREv+OWBb/lF8c/5tpI/+kdCz/roE1/7mPQP/FoEz/07Ja/+DD
Zv/jxmn/48Zp/+PGaP/ixmf/4sVm/+LDZP/iwmP/4cFi/+HBYf/hv2D/4L5e/+C9Xf/gvVz/37ta
/9+6Wf/fuVj/3rlW/963VP/PpUb/r34s/4xTEP93OgD/eDsA/41RAP+tcAD/uXwA/7l8AP+5ewD/
uXsA/6VoAP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/4BFB/+5
hjP/37FO/+CxTf/aqkf/xZI2/7B7J/+dZRr/jFIO/31BBP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP95PQH/hUoG
/5NaCv+jaw//tH0T/8WRGP/Tnxr/1KEZ/9SgF//ToBb/06AV/9OfFf/TnhT/0p4U/9GdE//RnRP/
0ZwS/9GcEv/QmxH/0JsR/9CaEf/PmhD/z5kQ/8+ZEP/OmBD/zZcP/7yEDP+iaAj/iEwD/3c7AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/eT0C
/4dPEP+ZZiH/rYA0/8GbSP/Xt1z/4sNk/+HCY//hwWL/4cFh/+G/YP/gvl7/4L1d/+C9XP/fu1r/
37pZ/9+5WP/euVb/3rdU/962Uv/dtlD/3rVO/8ufP/+ibSD/fEAE/3c6AP+NUAD/sHMA/7l7AP+5
ewD/uHsA/41QAP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/dzoA/3c6
AP+NUxH/xZM5/+CxS//gsUj/37BH/9+vRf/fr0T/3q5B/9WlPP/JljP/voor/7WAJf+tdx//p28b
/6JqGP+eZhX/nGQU/5xkFP+dZRP/oGgU/6RsFf+pchf/sHoa/7iDHP/CjR//zJkh/9WjJP/WpCP/
1qMh/9ajH//Wox7/1aId/9WhGv/UoRn/1KAX/9OgF//ToBX/058V/9OeFP/SnhT/0p4T/9GdE//R
nBL/0ZwS/9CbEf/QmxH/0JoR/8+aEP/LlRD/tX0M/5pfB/+ARAL/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP+FTA3/n20l/7qPPv/XtFj/4b9g/+C+Xv/gvV3/4L1c/9+7W//f
uln/37lY/9+5V//et1T/3rdS/922UP/etU7/3bRM/9yzSv/VqkP/qXUi/3xABP95PAD/l1oA/7d5
AP+4ewD/r3EA/3g7AP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/klkU/8aTN//fsEf/369G/9+vRP/er0L/3a5B/92tP//drT7/3a08/9ysOv/cqzn/
26s3/9urNv/bqjT/26ky/9qpMf/aqC//2act/9mnLP/Ypyv/2KYp/9imKP/YpSb/16Ul/9akI//W
oyH/1qMf/9ajHv/Voh3/1aEa/9ShGf/UoRj/06AX/9OgFf/TnxX/054U/9KeFP/SnhP/0Z0T/9Gd
Ev/RnBL/0JsR/8aQD/+scwr/kVcF/3s+Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3g7Af+NVhT/r4Ay/9KsUf/gvVz/37tb/9+6
Wf/fuVj/37lX/963VP/et1L/3bZQ/961Tv/dtEz/3LNK/9yySP/csUb/0qU9/5pjF/93OgD/gkUA
/6tuAP+4egD/jlEA/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/41TD/+6hS3/26tC/96vQv/erkH/3a0//92tPv/drT3/3Kw6/9yrOf/b
qzj/26s2/9uqNP/bqTL/2qkx/9qoL//Zpy3/2acs/9inK//Ypin/2KYo/9ilJv/XpSX/16Qj/9aj
If/Wox//1qMe/9WiHf/VoRv/1KEZ/9ShGP/ToBf/06AV/9OfFf/TnhT/0p4U/9KeE//OmhP/uYMO
/6BnCf+HSwP/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/eDsB/5FaFv+6jTr/3LdX
/9+6WP/fuVf/3rhU/963Uv/dtlD/3rVO/920TP/cs0r/3LJI/9yxRv/bsET/269B/7mHKv98QAT/
eTwA/5xfAP+pawD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3o9Av+ESQr/fUEE/3g7Af9/QwX/omsc/8WTMv/crD//3a0+/92tPf/crDr/3Ks5/9ur
OP/bqzb/26o0/9upMv/aqTH/2qkw/9moLf/Zpyz/2Kcr/9imKf/Ypij/2KUm/9elJf/XpCP/1qQi
/9ajH//Wox//1aId/9WhG//UoRn/1KEY/9OgF//ToBX/0Z0V/7+IEP+nbwv/kFYG/3s+Af93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/bzQc/2YtPv9m
LT7/Zi0+/2YtPv9mLT7/Zi0+/2YtPv9mLT7/Zi0+/2YtPv9mLT7/Zi0+/2YtPv9mLT7/Zi0+/2Yt
Pv9mLT7/Zi0+/2YtPv9mLT7/Zi0+/2YtPv9mLT7/Zi0+/2YtPv9mLT7/Zi0+/2YtPv9mLT7/Zi0+
/2YtPv9mLT7/aTA2/24zI/91OQv/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP+CRwn/
r38w/9qzU//euFT/3rdS/922UP/etU7/3bRM/9yzSv/cskj/3LFG/9uwRP/br0H/264//8iZMv+A
RQb/dzoA/5BTAP9+QQD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93OgD/dzoA/6yG
Y/+zkXH/s5Fx/7aVc//KrYL/3MKP/+DHkv/av4z/1bmJ/9vAjP/r0ZT/7NGT/+zRkf/r0JD/69CQ
/+vQj//rz47/68+N/+rPjP/qz4v/6s6K/+rOif/pzon/6c6I/+nNh//pzYb/6c2F/+jMhP/ozIT/
6MyC/+jMgv/oy4H/6MuA/+fLf//myX3/1LJs/7OJTv+NWSX/eDwD/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP9iKk//OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zkK4f9AEMf/TRqa/1wmY/9uNCH/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/gUYI/7aHM//dtlL/3bZR/961T//dtEz/3bNK/9yySP/csUb/27BE/9uvQf/brj//2q49/8mY
Mf98QAP/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/3c6AP93OgD/3c3A
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
///////////////////////////////////////////////38/H/2ce5/7OQcv+HUR//dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/20zJ/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4v9FFLb/Xide/3Q4C/93OgD/dzoA/3c6
AP93OgD/dzoA/5BYFP/PpUb/3rVP/920TP/ds0r/3LJI/9yxRv/bsET/3LBB/9uuP//arj3/2q07
/7aCJf93OgD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/dzoA/3c6AP/FqpP/
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////////////////////////////////////////////////79/f/aybv/nnJL
/3g7Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/djkF/zoL3P84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/PQ3S/1oka/91OQn/dzoA
/3c6AP93OgD/dzoA/31BBf++jzf/3bRM/92zSv/cskj/3LFG/9uwRP/csEH/264//9quPf/arTv/
2qw5/4tSDP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93OgD/dzoA/62HZv//
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
4dTJ/5ZnPf93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/RBO5/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/0AQx/9lLUT/
dzoA/3c6AP93OgD/dzoA/3g7Af+0hC7/3bNL/9yySP/csUb/27BE/9ywQv/brj//2q49/9qtO//a
rDr/sn4j/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/3c6AP93OgD/lWU5////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
/////v7+/8ivmv9+RA7/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP9PHJH/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4v9R
Hor/dTgJ/3c6AP93OgD/dzoA/3g7Af+6ijL/3LJI/9yxRv/bsET/3LBC/9uuP//arj3/2q07/9qs
Ov/KmjD/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/dzoA/3c6AP9+Qw7//v7+
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
/////////////+vh2v+PXS//dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/1okaf84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/9FFLb/cTYY/3c6AP93OgD/dzoA/3s/A//NoD3/3LFG/9uwRf/csEL/264//9quPf/arTv/2qw6
/9OkNf93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP/t5N7/
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////j18/+fdE7/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/Zi1B/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/9BEMX/cDUb/3c6AP93OgD/dzoA/5NbFP/csUb/27BF/9ywQv/brj//2q49/9qtO//arDr/
z58z/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/3c6AP93OgD/dzoA/9XBsf//
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////////37+/+ogF3/dzoA/3c6AP93OgD/dzoA/3c6AP9xNhn/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/9CEsD/czcP/3c6AP93OgD/dzoA/8eZOP/bsEX/3LBC/9uvQP/arj3/2607/9qsOv+9
iyn/dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/vZ+E////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////////////38+/+jelX/dzoA/3c6AP93OgD/dzoA/3c6Af89DtL/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/9LGZ//dzoB/3c6AP93OgD/pG8f/9uwRf/csEL/269A/9quPv/brTz/2qw6/59p
GP93OgD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP+kfFf/////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////////////////r49/+UZTn/dzoA/3c6AP93OgD/dzoA/0gWrP84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/9fKFj/dzoA/3c6AP+MUw//27BF/9ywQv/br0D/264+/9utPP/TpDb/fEAD
/3c6AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/3c6AP93OgD/dzoA/4xZKv//////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////////////////////Do4/+BSRT/dzoA/3c6AP93OgD/Ux+E/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/z4Oz/90OAv/dzoA/4BEBv/bsEX/3LBC/9uvQP/brj7/2608/6JsGv93OgD/
dzoA/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/eT0E//r49///
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////9C7qf93OgD/dzoA/3c6AP9eJ1v/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/1slZv93OgD/fkIF/9uwRf/csEL/269A/9uuPv/Eky//eDwB/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/5NjP////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////51xSf93OgD/dzoA/20zJf9PG5D/TxuQ/08b
kP9PG5D/TxuQ/08bkP9PG5D/TxuQ/08bkP9PG5D/TxuQ/08bkP9PG5D/TxuQ/08bkP9PG5D/TxuQ
/08bkP9PG5D/TxuQ/08bkP9PG5D/TxuQ/08bkP9PG5D/TxuQ/08bkP9PG5D/TxuQ/08bkP9PG5D/
ThuW/0kXp/8+Ds//OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/QhHB/3c6Af+GSwr/3LFF/9ywQ//br0D/0aM4/4JHB/93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD0dzoA73c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP/MtaL/////
/////////////////////////////////////////////////////////////////8qynP+2lnf/
tpZ3/7aWd/+2lnf/tpZ3/7aWd/+2lnf/tpZ3/7aWd/+2lnf/tpZ3/7aWd/+2lnf/tpZ3/7aWd/+2
lnf/tpZ3/7aWd/+3lnj/vJ2D/9C6qP/07uv/////////////////////////////////////////
////////////////////////////////////////5tvS/3g8Av93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6Av9oLzr/RRS0/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/bDIq/5dgF//csUX/3LBD/9WoPf+LUQ3/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6APR3OgDvdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/7STdf//////
////////////////////////////////////////////////////////////////rolp/3g7Af93
OgH/dzoB/3c6Af93OgH/dzoB/3c6Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3o/B/+vi2v/9/Ty////////////////////////////////
////////////////////////////////////////////oXdR/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP91OQb/SBas/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/9bJWX/sX8p/9yxRf/WqT//jVQP/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA9Hc6AO93OgD/dzoA/3c6AP93OgH/dzoB/3g7Af94OwL/nXFJ////////
///////////////////////////////////////////////////////////////HrZb/eT0D/3k8
A/95PAP/eTwD/3k8A/95PAL/eTwC/3k8Av94OwL/eDsC/3g7Av94OwH/dzoB/3c6Af93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP+OWy3/8+3p////////////////////////////
///////////////////////////////////////////by77/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP9jK0n/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/1Eelf/SpkD/0qU//4pRDf93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD0dzoB73g7Av94OwL/eDwC/3k8Av95PAP/eTwD/3k9A/+HUB7/////////
/////////////////////////////////////////////////////////////9/Qw/97PgX/ez4F
/3s+Bf97PgX/ej4F/3o+BP96PgT/ej0E/3o9BP96PQT/eT0D/3k9A/95PAP/eTwC/3k8Av94OwL/
eDsC/3c6Af93OgH/dzoA/3c6AP93OgD/dzoA/3c6AP+YakH//v39////////////////////////
//////////////////////////////////////////79/f+HUR//dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/280H/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/VCfH/8iaOf+CSAj/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6APR5PAPveT0D/3o9BP96PQT/ej4E/3o+Bf97PgX/ez8G/3s/Bv/07+z/////
////////////////////////////////////////////////////////////9vHu/3xACP98QAf/
fEAH/3xAB/98QAf/fEAH/3s/B/97Pwb/ez8G/3s/Bv97Pwb/ez4F/3s+Bf96PgT/ej0E/3o9BP95
PQP/eTwD/3k8Av94PAL/eDsC/3g7Af93OgH/dzoA/3c6AP/Puqf/////////////////////////
/////////////////////////////////////////////7GOb/93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoC/zwM2P84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/88Dd//eT0I/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA9Hs+Be97PgX/ez8G/3s/Bv97Pwf/fEAH/3xAB/98QAj/fEAI/97Owf//////
////////////////////////////////////////////////////////////////i1Yl/31BCf99
QQn/fUEJ/31BCf99QQn/fEEI/3xACP98QAj/fEAI/3xACP98QAf/fEAH/3s/B/97Pwb/ez8G/3s/
Bv97PgX/ej4E/3o9BP96PQP/eTwD/3k8Av94OwL/eDsC/5doPv//////////////////////////
////////////////////////////////////////////1sO0/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/RhWx/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/9sMin/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD0fEAH73xAB/98QAj/fEAI/3xBCP99QQn/fUEJ/31CCf9+Qgr/x62X////////
//////////////////////////////////////////////////////////////+jeFL/f0ML/39D
C/9/Qwv/fkIL/35CC/9+Qgv/fkIK/35CCv9+Qgr/fUIJ/31BCf99QQn/fEEI/3xACP98QAj/fEAI
/3xAB/97Pwf/ez8G/3s/Bv97PgX/ej4E/3o9BP95PQP/ej4G//Xx7v//////////////////////
///////////////////////////////////////////07+v/eDsC/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP9MGZ//OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/2EqUf93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6APR9QQnvfUEJ/35CCv9+Qgr/fkIL/35CC/9/Qwv/f0MM/39DDP+xjW3/////////
/////////////////////////////////////////////////////////////7qafv+ARA3/gEQN
/4BEDf+ARA3/f0QN/39EDf9/RAz/f0MM/39DDP9/Qwz/f0ML/39DC/9+Qgv/fkIK/35CCv99QQn/
fUEJ/3xBCP98QAj/fEAI/3xAB/97Pwf/ez8G/3s+Bf96PgX/3My/////////////////////////
//////////////////////////////////////////////+GUB7/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/bjQh/zwM1/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/ViF4/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA9H5CC+9/Qwv/f0MM/39DDP9/RA3/f0QN/4BEDf+ARQ3/gEUO/5ttQ///////////
////////////////////////////////////////////////////////////0byp/4FGEP+BRhD/
gUYP/4FGD/+ARg//gEUO/4BFDv+ARQ7/gEUO/4BFDf+ARA3/gEQN/39EDf9/RAz/f0MM/39DC/9/
Qwv/fkIL/35CCv99Qgn/fUEJ/3xACP98QAj/fEAH/3xAB//Fq5T/////////////////////////
/////////////////////////////////////////////55yS/93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/djkE/1wmZf88DNf/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/9PG5T/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD0f0QN74BEDf+ARQ3/gEUO/4BFDv+ARg//gUYP/4FGEP+BRhD/iE8c//79/f//////
///////////////////////////////////////////////////////////o3dT/gkgR/4JIEf+C
RxH/gkcR/4JHEf+CRxH/gkcR/4JHEP+BRhD/gUYQ/4FGEP+BRg//gEUO/4BFDv+ARQ7/gEQN/39E
Df9/RA3/f0MM/39DDP9+Qgv/fkIK/35CCv99QQn/fEEI/6+Kaf//////////////////////////
////////////////////////////////////////////tpV4/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/2kw
M/9EE7n/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/0sZoP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6APSARg/vgUYP/4FGEP+CRxD/gkcR/4JHEf+CRxH/gkgR/4NIEf+DSBL/7ubf////////
//////////////////////////////////////////////////////////v6+f+GThf/hEoS/4RK
Ev+ESRL/g0kS/4NJEv+DSRL/g0gR/4NIEf+CSBH/gkgR/4JHEf+CRxH/gkcQ/4FGEP+BRhD/gEYP
/4BFDv+ARQ7/gEQN/39EDf9/RAz/f0MM/39DC/9+Qgr/mWo/////////////////////////////
///////////////////////////////////////////OuKX/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3M3EP9SHon/OQrh
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/ThuW/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA9IJHEe+CRxH/gkgR/4NIEf+DSRL/g0kS/4RKEv+EShL/hEoS/4RKEv/ZxrX/////////
/////////////////////////////////////////////////////////////5pqO/+GTBL/hUsS
/4VLEv+FSxL/hUsS/4VLEv+EShL/hEoS/4RKEv+EShL/g0kS/4NJEv+DSBL/g0gR/4JIEf+CRxH/
gkcR/4FGEP+BRhD/gEYP/4BFDv+ARQ3/gEQN/39EDP+FTBj//f38////////////////////////
/////////////////////////////////////////+bb0v93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6Af9gKVX/Pg7Q/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/9WIXf/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD0g0kS74RJEv+EShL/hEoS/4VLEv+FSxL/hUsS/4ZMEv+GTBL/hkwS/8SnjP//////////
////////////////////////////////////////////////////////////sYtl/4dOEv+HThL/
h04S/4dNEv+HTRL/h00S/4ZMEv+GTBL/hkwS/4ZMEv+FSxL/hUsS/4RKEv+EShL/hEoS/4NJEv+D
SRL/g0gR/4JIEf+CRxH/gkcQ/4FGEP+ARg//gEUO/4BFDf/t5N7/////////////////////////
////////////////////////////////////////+/n4/3s/Cf93OgH/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP9tMyb/SBar/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/2MsSf93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6APSFSxLvhUsS/4ZMEv+GTBL/h00S/4dNEv+HThL/h04S/4hOEv+ITxL/sIli////////////
///////////////////////////////////////////////////////////Hq4//iVAS/4lQEv+J
UBL/iU8S/4hPEv+ITxL/iE8S/4hOEv+HThL/h04S/4dNEv+HTRL/hkwS/4ZMEv+FSxL/hUsS/4VL
Ev+EShL/hEkS/4NJEv+DSBH/gkgR/4JHEf+CRxD/gUYQ/9fEtP//////////////////////////
////////////////////////////////////////////kF0v/3k8Av94OwH/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP91OAr/ViF4/zoK3v84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/85Ct7/czcQ/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA9IdNEu+HTRL/h04S/4hOEv+ITxL/iU8S/4lQEv+JUBL/iVAS/4lQEv+cbDj/////////////
/////////////////////////////////////////////////////////9zLuf+LUhL/i1IS/4pR
Ev+KURL/ilES/4pREv+JUBL/iVAS/4lQEv+JUBL/iU8S/4hPEv+ITxL/h04S/4dOEv+HTRL/hkwS
/4ZMEv+FSxL/hUsS/4RKEv+EShL/g0kS/4NIEf+CSBH/waSK////////////////////////////
//////////////////////////////////////////+ogV3/ej0E/3k8A/94OwL/dzoB/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/ZS1E/0EQxv84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/00amP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD0iE8S74lPEv+JUBL/iVAS/4pREv+KURL/ilES/4tSEv+LUhL/i1IT/41VF//7+ff/////////
////////////////////////////////////////////////////////8enj/4xUE/+MVBP/jFMT
/4xTE/+MUxP/jFMT/4xTE/+LUhL/i1IS/4tSEv+KURL/ilES/4lQEv+JUBL/iVAS/4hPEv+ITxL/
h04S/4dNEv+HTRL/hkwS/4VLEv+FSxL/hEoS/4RJEv+shGD/////////////////////////////
/////////////////////////////////////////8Ckiv97Pwb/ej4E/3k9A/95PAL/eDsC/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/cDUb/00am/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/88
DdX/bzQe/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
APSKURLvilES/4tSEv+LUhL/jFMT/4xTE/+MUxP/jFQT/41UE/+NVBP/jVQT/+nd0v//////////
///////////////////////////////////////////////////////+/v7/lV8j/45VFP+OVRT/
jlUU/45VFP+NVRT/jVQT/41UE/+MVBP/jFQT/4xTE/+MUxP/i1IT/4tSEv+KURL/ilES/4lQEv+J
UBL/iU8S/4hPEv+IThL/h00S/4dNEv+GTBL/hUsS/5dmN///////////////////////////////
////////////////////////////////////////2Ma2/3xAB/97Pwb/ez4F/3o9BP95PAP/eDsC
/3c6Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
djkE/1slZ/87DNj/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OwzY/2gv
N/93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
9IxTE++MUxP/jFQT/41UE/+NVBP/jlUU/45VFP+OVRT/jlYU/45WFP+PVhX/1cCp////////////
//////////////////////////////////////////////////////////+pfUz/j1cV/49XFf+P
VxX/j1YV/49WFf+PVhX/jlYU/45VFP+OVRT/jlUU/41UE/+NVBP/jFQT/4xTE/+MUxP/i1IT/4tS
Ev+KURL/iVAS/4lQEv+JTxL/iE8S/4dOEv+HTRL/iE8W//r49v//////////////////////////
///////////////////////////////////////v6OL/fUEJ/3xACP98QAf/ez8G/3o+Bf95PQP/
eTwC/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/2kwNf9E
E7r/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/z4Oz/9qMTD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0
jVQT745VFP+OVRT/jlYU/49WFf+PVhX/j1cV/49XFv+QVxb/kFcW/5BYFv/Co4D/////////////
/////////////////////////////////////////////////////////76cdv+RWBf/kVgX/5FY
F/+RWBf/kFgX/5BYFv+QVxb/kFcW/49XFf+PVhX/j1YV/45WFP+OVRT/jlUU/41UE/+NVBP/jFQT
/4xTE/+LUhP/i1IS/4pREv+JUBL/iVAS/4hPEv+IThL/59vQ////////////////////////////
//////////////////////////////////////79/f+GThv/fUIJ/3xACP98QAf/ez8G/3s+Bf96
PQT/eTwD/3g7Av93OgH/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3I3Ev9RHYv/OQrh/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9IF6r/cDUc/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSP
VhXvj1YV/49XFf+QVxb/kFcW/5BYF/+RWBf/kVkX/5FZF/+SWRj/klkY/7CHWP//////////////
////////////////////////////////////////////////////////0rqf/5NaF/+TWhf/kloX
/5JaF/+SWhj/klkY/5FZGP+RWRf/kVgX/5FYF/+QWBb/kFcW/49XFf+PVhX/j1YV/45WFP+OVRT/
jVUU/41UE/+MVBP/jFMT/4tSE/+KURL/ilES/4lQEv/Svab/////////////////////////////
/////////////////////////////////////////51uRf9/Qwv/fkIK/31BCf98QAj/fEAH/3s/
Bv96PgT/eT0D/3k8Av93OgH/dzoA/3c6AP93OgD/dzoA/3c6Af9gKVf/PQ7R/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9BEMX/YipO/3c6Af93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9JBX
Fu+QWBf/kVgX/5FZGP+SWRj/kloY/5JaF/+TWhf/k1sW/5NbFv+TWxb/nmst////////////////
///////////////////////////////////////////////////////l2Mj/lFwV/5RcFf+UXBX/
k1wV/5NbFv+TWxb/k1sW/5NaF/+SWhf/kloX/5JZGP+RWRj/kVgX/5FYF/+QWBb/kFcW/49XFf+P
VhX/jlYU/45VFP+NVRT/jVQT/4xTE/+MUxP/i1IS/76eff//////////////////////////////
////////////////////////////////////////tJBx/39EDf9/Qwz/fkIK/31CCf98QAj/fEAH
/3s/Bv96PgX/ej0D/3k8Av94OwL/dzoA/3c6AP9tMij/SBat/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/87DNn/WiRq/3U5B/93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0klkY
75JaGP+SWhf/k1oX/5NbFv+TWxb/lFwV/5RcFf+UXRT/lV0U/5VdE/+WXhP/9vLt////////////
//////////////////////////////////////////////////////j08P+WXxP/ll4S/5ZeEv+V
XhP/lV0T/5VdFP+VXRT/lFwV/5RcFf+TWxb/k1sW/5NbFv+TWhf/kloX/5JZGP+RWRj/kVgX/5BY
Fv+QVxb/j1cV/49WFf+OVRT/jlUU/41UE/+MVBP/q4FU////////////////////////////////
///////////////////////////////////////Lsp3/gEUO/4BEDf9/Qwz/fkIL/35CCv99QQj/
fEAI/3s/B/97PgX/ej0E/3k8A/91OQz/ViF6/zoK3v84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/TBqc/3A1HP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSTWxbv
k1sW/5RcFf+UXBX/lV0U/5VdE/+WXhL/ll4S/5ZfEf+XXxH/l18R/5dgEP/k1sP/////////////
/////////////////////////////////////////////////////////6NyLP+YYBD/l2AQ/5dg
EP+XXxD/l18R/5ZfEf+WXhL/ll4S/5VeE/+VXRT/lFwU/5RcFf+TWxb/k1sW/5NaF/+SWhf/klkY
/5FZF/+RWBf/kFcW/49XFf+PVhX/jlYU/45VFP+YZCv/////////////////////////////////
/////////////////////////////////////+HTx/+BRhD/gEUP/4BFDf9/RAz/f0ML/35CCv99
QQn/fEAI/3s/B/97PgX/Zi9I/0AQyP84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/QRDF/2UtQ/93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9JVdFO+V
XRP/ll4S/5ZeEv+XXxH/l18R/5dgEP+YYBD/mGEP/5hhD/+YYQ//mWIO/9O7mP//////////////
////////////////////////////////////////////////////////t5BV/5liDv+ZYg7/mWIO
/5liD/+YYQ//mGEP/5hgEP+XYBD/l2AQ/5dfEf+WXxL/ll4S/5VdE/+VXRT/lFwV/5NcFf+TWxb/
k1oX/5JaF/+SWRj/kVgX/5BYF/+QVxb/j1YV/49WFf/18Ov/////////////////////////////
////////////////////////////////////9/Lw/4NJE/+CRxH/gUYP/4BFDv9/RA3/f0MM/35C
Cv99QQn/ej8Q/1IfkP84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
Ogvd/1cidf91OQn/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0ll8R75df
Ef+XYBD/mGAQ/5hhD/+YYQ//mWIO/5liDv+aYw3/mmMN/5pjDP+aZAv/wqBs////////////////
///////////////////////////////////////////////////////KrH7/m2QL/5tkC/+aZAv/
mmMM/5pjDP+aYw3/mWIN/5liDv+ZYg//mGEP/5hhEP+XYBD/l18R/5ZfEf+WXhL/lV0T/5VdFP+U
XBX/k1sW/5NbFv+SWhf/klkY/5FZF/+QWBf/kFcW/+LTw///////////////////////////////
////////////////////////////////////////kl4u/4JIEf+CRxH/gUYQ/4BFDv+ARA3/f0MM
/3o/Gv9IF7T/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/0oXpv9u
MyP/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSYYQ/vmGEP
/5liDv+ZYg7/mmMN/5pjDP+aZAv/m2QL/5tlCv+cZQr/nGUK/5xmCf+yhj//////////////////
/////////////////////////////////////////////////////9zIqP+dZgn/nWYJ/5xmCf+c
ZQn/nGUK/5tlCv+bZAv/m2QL/5pjDP+aYw3/mWIN/5liDv+YYQ//mGEP/5dgEP+XXxH/ll8R/5Ze
Ev+VXRP/lFwU/5NcFf+TWxb/kloX/5JZGP+RWRf/z7aa////////////////////////////////
//////////////////////////////////////+of1j/hEoS/4NIEv+CRxH/gUYQ/4BFDv99Qxb/
SBe1/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/z8Py/9iKk7/dzoB/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9JliDe+aYwz/
mmMM/5tkC/+bZQr/nGUK/5xmCf+dZgn/nWYI/51nCP+dZwf/nmgH/6JvFP/+/v3/////////////
////////////////////////////////////////////////////7uTU/55oB/+eaAf/nmgH/55n
B/+dZwj/nWcI/51mCf+dZgn/nGUJ/5xlCv+bZAv/mmQL/5pjDP+ZYg3/mWIO/5hhD/+YYQ//l2AQ
/5dfEf+WXhL/lV0T/5RdFP+UXBX/k1sW/5NaF/+8mXL/////////////////////////////////
/////////////////////////////////////7+ggv+FSxL/hEoS/4NJEv+CRxH/gUYQ/1Mhlf84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zkK3/9UIH//dDgN/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0m2QL75xlCv+c
ZQn/nWYJ/51nCP+dZwj/nmgH/55oB/+eaAf/n2kG/59pBv+faQb/oGoG//Lq3v//////////////
///////////////////////////////////////////////////8+/n/om0N/6BqBv+faQb/n2kG
/59pBv+faQf/nmgH/55oB/+dZwf/nWcI/51mCf+cZgn/nGUK/5tkCv+aZAv/mmMM/5liDf+ZYg7/
mGEP/5hgEP+XXxH/ll8S/5VeE/+VXRT/lFwV/6p9SP//////////////////////////////////
////////////////////////////////////1MCs/4ZNEv+FSxL/hEoS/4NJEv9tN0//OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9HFa//bDIr/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSdZgnvnWcI/55n
B/+eaAf/nmgH/59pBv+faQb/oGoG/6BqBv+hawb/oWsG/6JsBf+ibAX/4tCy////////////////
//////////////////////////////////////////////////////+yhTL/omwF/6JsBf+hawb/
oWsG/6BqBv+gagb/n2kG/59pBv+faQf/nmgH/55oB/+dZwj/nWYJ/5xmCf+cZQr/m2QL/5pjDP+a
Yw3/mWIO/5hhD/+YYBD/l18R/5ZeEv+VXRP/mWQg//79/P//////////////////////////////
///////////////////////////////////q39b/h04S/4dNEv+FSxL/hEoT/0YWvv84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/89DtH/XyhY/3c6Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9J5oB++faQf/n2kG
/6BqBv+gagb/oWsG/6FrBf+ibAX/omwF/6JtBf+jbQX/o20G/6NuBv/St4b/////////////////
/////////////////////////////////////////////////////8ShX/+jbgb/o20G/6NtBf+i
bQX/omwF/6JsBf+ibAX/oWsG/6BqBv+gagb/n2kG/59pBv+eaAf/nmcH/51nCP+dZgn/nGUK/5tk
Cv+aYwv/mmMN/5liDv+YYQ//mGAQ/5dfEf+WXhL/8Oje////////////////////////////////
//////////////////////////////////z6+f+MVBn/iE4S/4dNEv9yPEj/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/85CuH/UR2K/3I3Ev93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0oGoG76BqBv+hawb/
omwF/6JsBf+ibQX/o20F/6NuBv+kbgb/pG8H/6RvB/+lcAj/pXAI/8OfXP//////////////////
////////////////////////////////////////////////////1buM/6VwCP+lcAj/pW8H/6Rv
B/+kbgb/o24G/6NtBv+ibQX/omwF/6JsBf+hawb/oWsG/6BqBv+faQb/nmgH/55oB/+dZwj/nWYJ
/5xlCf+bZQr/mmQL/5pjDf+ZYg7/mGEP/5dgEP/ezLT/////////////////////////////////
/////////////////////////////////////55vPf+JUBL/iE4S/1ooi/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/RBO4/2kwM/93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSibAXvomwF/6JtBf+j
bgb/pG4G/6RvB/+lbwf/pXAI/6ZxCP+mcQj/p3EJ/6dyCf+ncgr/tYgy////////////////////
///////////////////////////////////////////////////l1bj/p3IK/6dyCf+ncgn/pnEJ
/6ZxCP+lcAj/pW8H/6RvB/+kbgb/o24G/6JtBf+ibAX/omwF/6FrBv+gagb/n2kG/59pB/+eaAf/
nWcI/51mCf+cZQn/m2QK/5pjDP+ZYg3/mWIO/8ywif//////////////////////////////////
////////////////////////////////////tI9n/4pREv+JUBL/SRm5/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/PAzW
/1wmY/92OQT/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9KNtBe+kbgb/pG8H/6Vw
CP+mcAj/pnEI/6dyCf+ncgn/qHMK/6hzCv+odAv/qXQL/6l0DP+rdhD//Pr3////////////////
//////////////////////////////////////////////////Xu4/+pdAz/qXQM/6h0C/+ocwv/
qHMK/6dyCv+ncgn/pnEJ/6ZwCP+lcAj/pG8H/6RuBv+jbgb/om0F/6JsBf+hawX/oGoG/6BqBv+f
aQb/nmgH/51nCP+dZgn/nGUK/5tkC/+aYwz/u5Ve////////////////////////////////////
///////////////////////////////////JrpH/i1IS/4pREv8/ENT/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAni/08clv9xNhj/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0pW8H76VwCP+mcQj/p3IJ
/6dyCv+ocwr/qHQL/6l0DP+pdAz/qnUN/6p1Dv+rdg7/q3YO/6t3D//u5NH/////////////////
/////////////////////////////////////////////////v7+/7B/H/+rdg7/qnYO/6p1Df+q
dQz/qXQM/6l0C/+ocwv/p3MK/6dyCf+mcQn/pnAI/6VwB/+kbwf/o24G/6JtBf+ibAX/oWsF/6Bq
Bv+faQb/n2kH/55oB/+dZwj/nWYJ/5tlCv+rfTX/////////////////////////////////////
/////////////////////////////////9zLuf+MUxP/i1IS/zsM2/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/0MSwf9pMUD/eT0D/3g7Av93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSncQnvp3IK/6hzCv+odAv/
qXQM/6p1Df+qdQ7/q3YO/6t3D/+sdw//rHgQ/614EP+teBH/rXkR/+DMp///////////////////
////////////////////////////////////////////////////wJhK/614EP+seBD/rHcP/6t3
D/+rdg7/qnYO/6p1Df+pdAz/qXQL/6hzCv+ncgr/p3IJ/6ZxCP+lcAj/pG8H/6NuBv+ibQX/omwF
/6FrBf+gagb/n2kG/55oB/+eZwf/nWYJ/6RyH///////////////////////////////////////
////////////////////////////////7OLY/41VFP+MUxP/PQ/Y/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9NG6D/ej8O/3s/Bv96PQT/eTwD/3g7
Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9KhzC++pdAz/qnUM/6p1Dv+r
dg7/q3cP/6x3EP+teBD/rXkR/655Ev+uehL/r3oT/697E/+vexT/07Z/////////////////////
///////////////////////////////////////////////////QsXX/r3oT/656Ev+ueRL/rXkR
/614Ef+seBD/rHcP/6t2Dv+qdg7/qnUN/6l0DP+ocwv/qHMK/6dyCf+mcQj/pXAI/6RvB/+jbgb/
om0F/6JsBf+hawb/oGoG/59pBv+eaAf/qXko////////////////////////////////////////
///////////////////////////////28e7/jlYU/41UE/9FFsP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/283OP98QAj/fEAH/3s+Bf96PQT/eTwC
/3c6Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0qnUN76p2Dv+rdw//rHcQ/614
EP+teRH/rnoS/696E/+vexT/r3sU/7B8Ff+wfBX/sHwW/7F9Fv/GoFf/////////////////////
/////////////////////////////////////////////////97JoP+wfBX/sHwV/697FP+vexT/
r3oT/656Ev+ueRL/rXgR/6x4EP+rdw//q3YO/6p1Df+pdAz/qHQL/6hzCv+ncgn/pnEI/6VwCP+k
bwb/o20G/6JsBf+hawX/oGoG/59pBv++mlr/////////////////////////////////////////
//////////////////////////////v49v+QVxb/jlUU/1Qjov84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/cTk0/31BCf98QAj/ez8H/3o+Bf95PQP/
eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSsdw/vrHgQ/615Ef+ueRL/r3oT
/697FP+wfBT/sHwV/7B8Fv+xfRb/sX0X/7J+GP+yfhj/s38Y/7qLMP//////////////////////
////////////////////////////////////////////////7eDK/7J+GP+yfhf/sX0X/7F9Fv+w
fBX/sHwU/697FP+vehP/rnoS/615Ef+teBD/rHcP/6t2Dv+qdQ3/qXQM/6h0C/+ncwr/p3IJ/6Zw
CP+kbwf/pG4G/6JtBf+ibAX/omwI/+ncxv//////////////////////////////////////////
////////////////////////////+vf0/5BYF/+PVhX/ZjR1/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9dKHT/fkIK/31BCP98QAf/ez8G/3o9BP95
PAP/eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9K15Ee+uehL/r3sT/697FP+wfBX/
sX0W/7F9F/+yfhj/sn4Y/7N/Gf+zfxn/tIAZ/7SAGv+0gRr/tYEb//n07f//////////////////
///////////////////////////////////////////////69/H/tIAb/7SAGf+zfxn/s38Y/7J+
GP+xfRf/sX0W/7B8Ff+wfBT/r3sT/696E/+ueRL/rXgQ/6x3D/+rdg7/qnUN/6l0DP+ocwv/p3IJ
/6ZxCf+lcAj/pG8H/6VxDf/Zw5z/////////////////////////////////////////////////
///////////////////////////z7Ob/kVgX/5BXFv9+ST//OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/z0N1v90Oyz/fkIK/3xACP98QAf/ez4F/3o9
A/95PAL/dzoB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0r3sT77B8FP+wfBb/sX0W/7J+F/+y
fhj/s38Z/7SAGf+0gBr/tYEa/7WBGv+1ghv/toIb/7aDG/+2gxv/7N/F////////////////////
///////////////////////////////////////////////////RsHD/zKhh/8uoYf/Lp2D/y6dg
/8unYP/Kpl//yaVf/8mlXv/JpV7/yKRd/8ikXP/Ho1z/xqJb/8ahWv/FoVn/xKBY/8SfV//Dn1b/
w59Y/8akY//Vu4z/8+3h////////////////////////////////////////////////////////
/////////////////////////+bYyv+SWhj/kVgX/49XF/9EFcj/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/z0O0/9cKHX/aDFL/2gxSP9oMEf/Zy9G
/2YuRf9mLUT/ZS1D/2UtQ/9lLUP/ZS1D/2UtQ/9lLUP/ZS1D/2UtQ/9lLUP/ZS1D/2UtQ/9lLUP/
ZS1D/2UtQ/9lLUP/ZS1D/2UtQ/9lLUP/ZS1D/2UtQ/9lLUP/ZS1D/2UtQ/9lLUP/ZS1D/2UtQ/9y
NhT/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APSwfBbvsX0X/7J+GP+zfxj/s38Z/7SA
Gv+1gRr/tYIa/7WCG/+2gxv/t4Mb/7eEG/+3hBv/uIUb/7iFG//gyp3/////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////0rqe/5NbF/+SWRj/kVgX/2Yzev84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/14n
Xv93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9LJ+GO+zfxj/tIAZ/7SAGv+1gRr/tYIb
/7aDG/+3gxv/t4Qb/7iFG/+5hhv/uYYb/7qGG/+6hxv/uocb/9W2df//////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////////+4kmL/k1wV/5NaF/+SWRj/ilMo/z0O2f84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/Uh6G
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0tIAZ77SAGv+1gRr/toIb/7aDG/+3hBv/
uIUb/7mGG/+6hhv/uocb/7qHG/+7iBv/u4gb/7yJG/+8iRv/y6JN////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////+vf0/5tmHv+VXRT/k1sW/5JaF/+RWRj/ZDJ+/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9HFq7/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APS1gRrvtYIb/7aDG/+3hBv/uIUb/7mGG/+6
hxv/uocb/7uIG/+8iRv/vIkb/72KG/+9ihv/vosc/76LHP/BkSf//v79////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
///////////////UvZ3/l18R/5ZeE/+UXBX/k1sW/5JaGP+OVh//RRbG/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zwN1f93
OgH/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9LaDG++3hBv/uIUb/7mGG/+6hxv/u4gb/7yJ
G/+8iRv/vYob/72KG/++ixz/v4wc/7+NHP/AjRz/wI0c/8COHP/27+D/////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////+/n3/6V0LP+YYBD/ll8R/5VdE/+UXBX/k1oX/5JZGP9+SUP/Ogze/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/3A1
G/93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0uIUb77mGG/+6hxv/u4gb/7yJG/+8iRv/vYob
/76LHP+/jBz/v40c/8CNHP/Ajhz/wY4d/8GPHf/Cjx3/wpAd/+zcuf//////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
///////LroL/mmMN/5hhD/+XYBD/ll4S/5VdFP+TWxb/kloX/5FZGP9vO2b/OAni/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/ZS1D
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APS6hhvvuocb/7uIG/+8iRv/vYob/76LHP+/jBz/
wI0c/8COHP/Bjx3/wY8d/8KQHf/CkB7/w5Ee/8ORH//Ekh//4smS////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
5tjD/55oDv+aZAv/mWIO/5hhD/+XXxH/lV4T/5RcFf+TWxb/klkY/5FYF/9oNXX/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9aJGv/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA9LuIG++8iRv/vYob/76LHP+/jBz/wI0c/8COHP/B
jx3/wpAd/8KQHv/DkR7/xJIf/8SSIP/FkyD/xZMg/8WTIP/Yt2v/////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
//////////////////////////////////////////////////////////////////////Dn2f+l
cxz/nWYJ/5tlCv+aYwz/mWIO/5hgEP+WXxH/lV0U/5NcFf+TWhf/kVkY/5BXFv9rOG3/OQrg/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/08bk/93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD0vIkb772KG/++jBz/wI0c/8COHP/Bjx3/wpAd/8KQ
Hv/DkR//xJIg/8WTIP/FkyD/xpQg/8aUIP/HlSD/x5Uf/9CmQ///////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
///////////////////////////////////////////////////////////////u5NL/qXkg/55o
B/+dZwj/nGUJ/5tkC/+ZYg3/mGEP/5dfEP+WXhL/lFwV/5NbFv+SWhj/kVgX/49XFf92Qk//PxDT
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/RBO7/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APS+ixzvv4wc/8CNHP/Bjhz/wpAd/8KQHv/DkR//xJIg
/8WTIP/GlCD/xpQg/8eVIP/Hlh//yJYe/8iXHv/Jlx3/ypkh//379///////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////3sqn/6dzEv+hawb/n2kG
/55oB/+dZgj/nGUK/5pjDP+ZYg7/mGAQ/5ZfEf+VXRT/k1wV/5NaF/+RWRj/kFcW/49WFf+FTir/
UyKj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/86C97/djkG
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA8b+NHO/Ajhz/wY8d/8KQHf/DkR7/xJIf/8WTIP/GlCD/
x5Ug/8eVH//Ilh//yZce/8mYHf/KmRz/y5kb/8uaG//Lmhr/9evT////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////7+XU/7+ZUf+kbwf/o20F/6JsBf+gagb/
n2kH/51nCP+dZgn/m2QL/5pjDf+YYQ//l18Q/5ZeEv+UXBX/k1sW/5JZGP+RWBf/j1cV/45VFP+N
VBP/dUBN/0oat/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9sMin/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgDowI4c78GPHf/CkB7/w5Ef/8WTIP/FkyD/xpQg/8eVH//I
lh7/yZce/8qYHf/LmRv/y5oa/8ybGf/Mmxn/zZsY/82cGP/s2qn/////////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////
///////////////////////////59e//4c6r/8GaUP+odAz/p3EJ/6VwCP+kbgb/omwF/6FrBv+f
aQb/nmgH/51mCP+cZQr/mmMM/5liDv+YYBD/ll8R/5VdFP+TWxb/kloX/5FYF/+QVxb/j1YV/45V
FP+MUxP/i1IT/3dCQv9YJpD/QBHQ/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/2EqUf93
OgD/dzoA/3c6AP93OgD/dzoA/3c6ANbCjx3vwpAe/8SSH//FkyD/xpQg/8eVIP/Hlh//yZce/8qY
Hf/LmRv/y5oa/8ybGf/Nmxj/zZwX/86dF//OnRb/z54W/926Wv/s2aH/7Nmh/+zZof/s2aH/7Nmh
/+zZof/s2KH/7Nii/+zYov/r2KL/69ei/+vXov/r16P/6tej/+rWpP/q1qT/6dWl/+nVpf/o1Kb/
6NSm/+jUpv/n06X/59Ok/+bSpP/m0aT/5dGk/+XQpP/k0KT/5M+k/+PPpP/izqT/4s2j/+HNo//h
zKP/3sec/9e8iP/Mqmn/vZI+/656FP+sdw//qnUN/6l0C/+ncgr/pnEI/6RvB/+jbQX/omwF/6Bq
Bv+faQf/nWcI/5xmCf+bZAv/mWIN/5hhD/+XXxH/lV4T/5RcFf+TWhf/klkY/5BYF/+PVhX/jlUU
/4xUE/+LUhP/ilES/4lPEv+HTRP/ekM1/2k0Xf9eKnv/ViOO/1MhmP9SIJj/UiCY/1Efl/9RH5f/
UB6W/1Aelv9QHZX/TxyV/08clP9PHJT/TxyU/08clP9PHJT/TxyU/08clP9PHJT/TxyU/08clP9P
HJT/TxyU/08clP9PHJT/TxyU/08clP9PHJT/TxyU/08clP9PHJT/TxyU/08clP9PHJT/YyxK/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoAu8KQHu/Ekh//xZMg/8aUIP/HlSD/yJYe/8mYHf/KmRz/y5oa
/8ybGf/NnBj/zpwX/86dFv/PnhX/0J8V/9CgFf/RoBX/0aAV/9KhFf/SoRX/0qEV/9KhFf/SoRX/
0aEV/9GgFf/RoBX/0J8V/8+eFf/Pnhb/zp0X/82cGP/Mmxn/zJoa/8uZG//JmB3/yJYe/8eVH//G
lCD/xZMg/8SSH//DkR7/wpAd/8GOHP/AjRz/vosc/72KG/+8iRv/uocb/7mGG/+3hBv/toIb/7WB
Gv+zfxn/sn4Y/7B8Fv+vexT/rnoS/614EP+rdg7/qnUM/6hzC/+ncgn/pXAI/6NuBv+ibAX/oWsG
/59pBv+eaAf/nWYJ/5tlCv+aYwz/mWIO/5dgEP+WXhL/lV0U/5NbFv+SWhj/kVgX/49XFf+OVhT/
jVQT/4xTE/+KURL/iVAS/4hOEv+GTBL/hUsS/4NJEv+CSBH/gUYQ/4BFDv9/RAz/fkIL/31BCf98
QAj/ez8G/3o9BP95PAL/dzoB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgCXxJIf78WTIP/GlCD/x5Ug/8iWHv/KmB3/y5kb/8yaGf/Nmxj/
zZwX/86dFv/PnhX/0J8V/9GgFf/SoRX/0qEU/9OiFP/TohT/06MU/9SjFP/UoxT/1KMU/9OjFP/T
ohT/06IU/9OiFP/SoRX/0aAV/9GgFf/QnxX/z54W/86dF//Nmxj/zJsZ/8uaG//KmB3/yJce/8eW
H//GlCD/xZMg/8SSH//DkR7/wpAd/8COHP+/jRz/vosc/7yJG/+7iBv/uocb/7iFG/+3gxv/tYIb
/7SAGv+zfxn/sX0X/7B8Ff+vexP/rXkR/6x3D/+qdQ3/qXQL/6dyCv+mcAj/pG8H/6JtBf+ibAX/
oGoG/55oB/+dZwj/nGUK/5pkC/+ZYg7/mGEQ/5dfEf+VXRP/k1wV/5JaF/+RWRj/kFcW/49WFf+O
VRT/jFMT/4tSEv+JUBL/iE8S/4dNEv+FSxL/hEoS/4JIEf+CRxD/gEUO/39EDf9/Qwv/fUIJ/3xA
CP97Pwb/ej4E/3k8A/94OwH/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AGnFkyDvxpQg/8eVIP/Ilh7/ypgd/8uaG//Mmxn/zZwY/86dF//P
nhb/0J8V/9GgFf/SoRX/06IU/9OjFP/UoxT/1KQU/9WlFP/VpRT/1aUU/9alFP/VpRT/1aUU/9Wl
FP/VpBT/1KQU/9SjFP/TohT/0qEV/9KhFf/QoBX/z54V/86dFv/NnBj/zJsZ/8uaGv/KmRz/yZce
/8eWH//GlCD/xZMg/8SSH//CkB7/wY8d/8COHP+/jBz/vYob/7yJG/+6hxv/uYYb/7eEG/+2gxv/
tYEa/7SAGf+yfhj/sHwW/697FP+uehL/rHgQ/6t2Dv+pdAz/qHMK/6dxCf+lbwf/o24G/6JsBf+h
awb/n2kG/55nB/+dZgn/m2QL/5pjDf+YYQ//l18R/5VeE/+UXBX/k1oX/5JZGP+QWBf/j1YV/45V
FP+MVBP/i1IT/4pREv+ITxL/h00S/4ZMEv+EShL/g0gS/4JHEf+ARg//gEQN/39DDP9+Qgr/fEAI
/3s/B/97PgX/eT0D/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoAMcaUIO/HlSD/yJYe/8qYHf/Lmhv/zJsZ/82cGP/OnRb/0J8V/9Gg
Ff/SoRX/06IU/9SjFP/UpBT/1aUU/9amFP/WphT/16cU/9enFP/XpxT/16cU/9enFP/XpxT/16cU
/9emFP/WphT/1aUU/9SkFP/UoxT/06IU/9KhFf/RoBX/0J8V/8+eFv/NnBf/zJsZ/8uaGv/KmRz/
yJce/8eVH//GlCD/xZMg/8ORH//CkB3/wY8c/8CNHP++ixz/vYob/7uIG/+6hxv/uIUb/7eDG/+1
ghv/tIAa/7N/GP+xfRf/sHwV/696E/+teRH/q3cP/6p1Df+odAv/p3IJ/6VwCP+kbgb/om0F/6Fr
Bv+faQb/nmgH/51mCf+bZQr/mmMM/5liDv+XYBD/ll4S/5RdFP+TWxb/kloY/5FYF/+PVxX/jlUU
/41UE/+MUxP/ilES/4lQEv+HThL/hkwS/4RKEv+DSRL/gkcR/4FGD/+ARQ3/f0MM/35CCv98QQj/
fEAH/3s+Bf95PQP/eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AOx3OgADx5Ug78iWH//JmB3/y5kb/8ybGf/NnBj/zp0W/9CfFf/RoBX/0qEV
/9OiFP/UpBT/1aUU/9amFP/XpxT/2KcU/9ioFP/YqBX/2akV/9mpFf/ZqRX/2akV/9mpFf/YqRX/
2KgU/9inFP/XpxT/1qYU/9WlFP/UpBT/06MU/9OiFP/RoRX/0J8V/8+eFv/NnBf/zJsZ/8uaG//K
mB3/yJYe/8eVIP/FlCD/xZMg/8ORHv/CkB3/wI4c/7+NHP+9ihv/vIkb/7qHG/+5hhv/t4Qb/7aD
G/+1gRr/s38Z/7J+F/+wfBb/r3sU/655Ev+sdxD/qnYO/6l0DP+ncgr/pnEI/6RvB/+jbQX/omwF
/6BqBv+eaAf/nWcI/5xlCv+aYwv/mWIO/5hgEP+WXxH/lV0U/5NbFv+SWhf/kVgX/5BXFv+OVhT/
jVQT/4xTE/+KURL/iVAS/4hOEv+GTBL/hUsS/4NJEv+CRxH/gUYQ/4BFDv9/RAz/fkIL/31BCf98
QAf/ez8G/3o9BP95PAL/dzoB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoAowAAAADHlh/vyZce/8qZHP/Mmhr/zZsY/86dFv/QnxX/0aAV/9KhFf/ToxT/
1KQU/9alFP/XpxT/2KcU/9ioFP/ZqRX/2qoW/9qrF//bqxf/26sX/9usGP/bqxf/26sX/9qrF//a
qhb/2akV/9ipFf/YqBT/16cU/9amFP/VpBT/1KMU/9OiFP/RoRX/0J8V/86dFv/NnBj/zJsZ/8uZ
G//JmB3/x5Yf/8aVIP/FkyD/xJIf/8KQHf/Bjx3/wI0c/76LHP+9ihv/u4gb/7qHG/+4hRv/toMb
/7WCG/+0gBn/s38Y/7F9Fv+vexT/rnoS/614EP+rdg7/qXQM/6hzCv+ncQn/pW8H/6NuBv+ibAX/
oGoG/59pBv+dZwj/nGYJ/5tkC/+ZYg7/mGEP/5dfEf+VXRP/lFwV/5NaF/+RWRj/kFcW/49WFf+O
VRT/jFMT/4tSEv+JUBL/iE8S/4dNEv+FSxL/hEoS/4JIEf+BRhD/gEUO/39EDf9+Qgv/fUEJ/3xA
CP97Pwb/ej0E/3k8Av93OgH/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgBMAAAAAMiWHu/KmB3/y5oa/8ybGf/OnRf/z54V/9GgFf/SoRX/06IU/9SkFP/W
phT/16cU/9ioFP/ZqRX/2qoW/9urF//brBj/3K0Z/92uGf/drhn/3a4a/92uGf/drhn/3K0Z/9ys
GP/bqxf/2qoW/9mpFf/YqBT/16cU/9amFP/VpRT/1KMU/9OiFf/RoBX/0J8V/86dFv/Nmxj/zJoa
/8qZHP/Ilx7/x5Uf/8aUIP/FkyD/w5Ee/8KQHf/Ajhz/v4wc/72KG/+8iRv/uocb/7mGG/+3hBv/
tYIb/7SAGv+zfxn/sX0X/7B8Ff+vehP/rXkR/6t3D/+qdQ3/qHML/6dyCf+lcAj/pG4G/6JsBf+h
awb/n2kG/55oB/+dZgn/m2QL/5pjDf+YYQ//l18R/5VeE/+UXBX/k1oX/5FZGP+QWBb/j1YV/45V
FP+MVBP/i1IS/4lQEv+ITxL/h00S/4VLEv+EShL/g0gR/4JHEP+ARQ7/f0QN/39DC/99Qgn/fEAI
/3s/Bv96PgT/eTwD/3c6Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA5Xc6AAQAAAAAyZce78uZHP/Mmxn/zZwY/8+eFv/QnxX/0qEV/9OiFP/UpBT/1qUU/9en
FP/YqBT/2akV/9qrF//brBj/3K0Z/92uGv/erxv/368b/9+wHP/fsBz/37Ac/9+vHP/erxv/3a4a
/92uGf/crBj/2qsX/9mqFf/YqBX/16cU/9amFP/VpBT/06MU/9KhFf/RoBX/z54W/82cF//Mmxn/
y5ob/8mYHf/Hlh//xpUg/8WTIP/Ekh//wpAd/8GOHP/AjRz/vosc/7yJG/+7iBv/uoYb/7eEG/+2
gxv/tYEa/7N/Gf+yfhf/sHwW/697E/+ueRL/rHcP/6p1Df+pdAv/p3IJ/6ZwCP+kbgb/om0F/6Fr
Bf+faQb/nmgH/51mCf+bZQr/mmMM/5liD/+XYBD/ll4S/5RcFf+TWxb/klkY/5BYF/+PVhX/jlUU
/4xUE/+LUhP/ilES/4hPEv+HTRL/hkwS/4RKEv+DSBH/gkcR/4BFD/+ARA3/f0ML/31CCv98QAj/
ez8G/3o+Bf95PAP/eDsB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgB9AAAAAAAAAADKmB3vy5oa/8ybGf/OnRf/0J8V/9GgFf/TohT/1KMU/9WlFP/XpxT/2KgU
/9mpFf/aqxf/3KwY/92uGv/erxv/37Ac/9+xHv/gsR//4LIf/+GyIP/hsh//4LEf/+CxHv/fsB3/
3q8b/92uGv/crRj/26sX/9mqFf/YqBT/16cU/9alFP/UpBT/06IU/9KhFf/QnxX/zp0W/82bGP/L
mhr/ypkc/8iWHv/HlSD/xZMg/8SSH//CkB7/wY8d/8CNHP++jBz/vYob/7uIG/+6hxv/uIUb/7aD
G/+1gRr/tIAZ/7J+GP+wfBb/r3sU/655Ev+sdxD/qnYO/6l0DP+ncgr/pnEI/6RvB/+ibQX/oWsF
/6BqBv+eaAf/nWYI/5xlCv+aYwz/mWIO/5dgEP+WXhL/lV0U/5NbFv+SWRj/kVgX/49XFf+OVRT/
jVQT/4xTE/+KURL/iU8S/4dOEv+GTBL/hEoS/4NIEv+CRxH/gEYP/4BEDf9/Qwz/fkIK/3xACP97
Pwf/ej4F/3k9A/94OwL/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
8Xc6ABIAAAAAAAAAAMqZHO/Mmhr/zZwY/86dFv/QnxX/0qEV/9OiFP/UpBT/1qYU/9inFP/ZqRX/
2qsW/9usGP/drhr/3q8b/9+wHf/gsR//4bIg/+KzIf/itCL/47Qi/+O0Iv/itCL/4bMg/+CyH//f
sR7/368b/92uGv/crRj/2qsX/9mpFf/YqBT/16YU/9WlFP/UoxT/0qEV/9GgFf/Pnhb/zZwY/8yb
Gf/LmRv/yZce/8eVH//GlCD/xZMg/8ORHv/CkB3/wI4c/7+MHP+9ihv/vIkb/7qHG/+4hRv/t4Mb
/7WCG/+0gBn/s38Y/7F9Fv+vexT/rnoS/614EP+rdg7/qXQM/6hzCv+mcQj/pG8H/6NtBf+ibAX/
oGoG/55oB/+dZwj/nGUK/5pjDP+ZYg7/mGAQ/5ZfEv+VXRT/k1sW/5JaGP+RWBf/j1cV/45VFP+N
VBP/jFMT/4pREv+JUBL/h04S/4ZMEv+EShL/g0kS/4JHEf+BRg//gEQN/39DDP9+Qgr/fEAI/3s/
B/97PgX/eT0D/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgB8
AAAAAAAAAAAAAAAAy5kb78ybGf/NnBf/z54W/9GgFf/SoRX/1KMU/9WlFP/XpxT/2KgU/9mqFf/b
qxj/3K0Z/96vG//fsB3/4LIf/+KzIf/jtCL/5LUj/+S2JP/ltiT/5LYk/+S1JP/jtSP/4rMh/+Gy
H//fsB7/3q8b/92uGf/brBj/2qoW/9ioFf/XpxT/1qUU/9SkFP/TohT/0aAV/8+eFf/OnRf/zJsZ
/8uaG//JmB3/x5Yf/8aUIP/FkyD/w5Ef/8KQHf/Ajhz/v40c/72KG/+8iRv/uocb/7mGG/+3gxv/
tYIb/7SAGv+zfxj/sX0X/7B8FP+vehP/rXgQ/6t2Dv+pdAz/qHMK/6ZxCf+kbwf/o24G/6JsBf+g
agb/n2kH/51nCP+cZQr/mmQL/5liDv+YYBD/ll8R/5VdFP+TWxb/kloX/5FYF/+QVxX/jlYU/41U
E/+MUxP/ilES/4lQEv+HThL/hkwS/4RKEv+DSRL/gkcR/4FGD/+ARQ3/f0MM/35CCv98QAj/fEAH
/3s+Bf95PQP/eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA3nc6AAkA
AAAAAAAAAAAAAADLmhvvzJsZ/86dF//PnhX/0aAV/9OiFP/UpBT/1qUU/9enFP/YqRX/2qoW/9ys
GP/drhr/368c/+CxH//hsyD/47Ui/+S2JP/ltyb/5rgn/+a4KP/muCj/5bcm/+S2JP/jtSP/4rMh
/+CxH//fsBz/3a4a/9ytGP/aqxf/2akV/9inFP/WphT/1KQU/9OiFP/SoRX/0J8V/86dF//Nmxj/
y5oa/8qYHf/Ilh//x5Ug/8WTIP/Ekh//wpAd/8GOHP+/jRz/vYsb/7yJG/+6hxv/uYYb/7eEG/+1
ghv/tIAa/7N/GP+xfRf/sHwV/696E/+teBH/q3YP/6p1DP+ocwv/p3EJ/6VvB/+jbgb/omwF/6Bq
Bv+faQf/nWcI/5xlCf+aZAv/mWIO/5hhEP+WXxH/lV0U/5NbFv+SWhf/kVgX/5BXFv+OVhT/jVQT
/4xTE/+KURL/iVAS/4dOEv+GTBL/hUsS/4NJEv+CRxH/gUYP/4BFDf9/Qwz/fkIK/3xBCP98QAf/
ez4F/3k9A/94OwL/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP53OgBGAAAAAAAA
AAAAAAAAAAAAAMuaGu/Mmxn/zp0X/9CfFf/RoRX/06IU/9SkFP/WphT/2KcU/9mpFf/aqxf/3K0Y
/96uGv/fsB3/4LIf/+K0If/ktSP/5bcl/+a4KP/ouir/6Lsr/+i6Kv/nuSj/5bcm/+S2JP/itCL/
4bIg/9+wHf/erxv/3K0Z/9urF//ZqRX/2KcU/9amFP/VpBT/06IU/9KhFf/QnxX/zp0W/82bGP/L
mhr/ypgd/8iWHv/HlSD/xZMg/8SSH//CkB3/wY4d/8CNHP++ixz/vIkb/7qHG/+5hhv/t4Qb/7aC
G/+0gBr/s38Z/7F9F/+wfBX/r3oT/614Ef+rdw//qnUN/6hzC/+ncQn/pXAI/6NuBv+ibAX/oGoG
/59pB/+dZwj/nGUJ/5tkC/+ZYg7/mGEQ/5dfEf+VXRT/k1sW/5JaF/+RWBf/kFcW/45WFP+NVBP/
jFMT/4pREv+JUBL/h04S/4ZMEv+FSxL/g0kS/4JHEf+BRhD/gEUN/39DDP9+Qgr/fEEI/3xAB/97
PgX/eT0D/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoAknc6AAAAAAAAAAAA
AAAAAAAAAAAAy5oa782bGP/OnRf/0J8V/9KhFf/TohT/1KQU/9amFP/YpxT/2akV/9qrF//crRn/
3q8b/9+wHf/hsh//4rQi/+S1JP/ltyb/57kp/+i7K//qvCz/6bss/+e5Kf/luCf/5LYk/+O0Iv/h
siD/37Ee/96vG//drhn/26sX/9mqFf/YqBT/16YU/9WkFP/TohT/0qEV/9CfFf/OnRb/zZsY/8ua
Gv/KmRz/yJYe/8eVIP/FkyD/xJIf/8KQHf/Bjh3/wI0c/76LHP+8iRv/u4gb/7mGG/+3hBv/toIb
/7SAGv+zfxn/sX0X/7B8Ff+vehP/rXgR/6t3D/+qdQ3/qHML/6dxCf+lcAj/o24G/6JsBf+gagb/
n2kH/51nCP+cZQn/m2QL/5liDv+YYQ//l18R/5VdFP+TWxb/kloX/5FYF/+QVxb/jlYU/41UE/+M
UxP/ilES/4lQEv+HThL/hkwS/4VLEv+DSRL/gkcR/4FGEP+ARQ3/f0MM/35CCv98QQj/fEAH/3s+
Bf95PQP/eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AMZ3OgAHAAAAAAAAAAAAAAAA
AAAAAAAAAADLmhrvzJsZ/86dF//QnxX/0aEV/9OiFP/UpBT/1qYU/9inFP/ZqRX/2qsX/9ytGP/e
rhr/37Ad/+CyH//itCH/5LUj/+W3Jv/muCj/6Loq/+i7K//ouir/57kp/+W3Jv/ktiT/4rQi/+Gy
IP/fsB3/3q8b/9ytGf/bqxf/2akV/9inFP/WphT/1aQU/9OiFP/SoRX/0J8V/86dFv/Nmxj/y5oa
/8qYHf/Ilh7/x5Ug/8WTIP/Ekh//wpAd/8GOHf/AjRz/vosc/7yJG/+6hxv/uYYb/7eEG/+2ghv/
tIAa/7N/Gf+xfRf/sHwV/696E/+teBH/q3cP/6p1Df+ocwv/p3EJ/6VwCP+jbgb/omwF/6BqBv+f
aQf/nWcI/5xlCf+bZAv/mWIO/5hhEP+XXxH/lV0U/5NbFv+SWhf/kVgX/5BXFv+OVhT/jVQT/4xT
E/+KURL/iVAS/4dOEv+GTBL/hUsS/4NJEv+CRxH/gUYQ/4BFDf9/Qwz/fkIK/3xBCP98QAf/ez4F
/3k9A/94OwL/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgDedzoAGAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAMuaG+/Mmxn/zp0X/8+eFf/RoBX/06IU/9SkFP/WpRT/16cU/9ipFf/aqhb/3KwY/92u
Gv/frxz/4LEf/+KzIP/jtSP/5LYk/+W3Jv/muCj/5rgo/+a4KP/ltyf/5bYk/+O1I//isyH/4LIf
/9+wHP/erhr/3K0Y/9qrF//ZqRX/2KcU/9amFP/UpBT/06IU/9KhFf/QnxX/zp0X/82bGP/Lmhr/
ypgd/8iWH//HlSD/xZMg/8SSH//CkB3/wY4c/7+NHP+9ixz/vIkb/7qHG/+5hhv/t4Qb/7WCG/+0
gBr/s38Y/7F9F/+wfBX/r3oT/614Ef+rdg//qnUM/6hzC/+ncQn/pW8H/6NuBv+ibAX/oGoG/59p
B/+dZwj/nGUJ/5pkC/+ZYg7/mGEQ/5ZfEf+VXRT/k1sW/5JaF/+RWBf/kFcW/45WFP+NVBP/jFMT
/4pREv+JUBL/h04S/4ZMEv+FSxL/g0kS/4JHEf+BRg//gEUN/39DDP9+Qgr/fEEI/3xAB/97PgX/
eT0D/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA5Hc6ACQAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAy5kb78ybGf/NnBf/z54W/9GgFf/SoRX/1KMU/9WlFP/XpxT/2KgU/9mqFf/bqxj/3a0Z
/96vG//fsB3/4LIf/+KzIf/jtSL/5LUk/+W2JP/ltiX/5bYk/+S2JP/jtSP/4rQh/+GyH//fsR7/
3q8b/92uGv/brBj/2qoW/9ioFf/XpxT/1qUU/9SkFP/TohT/0aAV/9CfFf/OnRf/zJsZ/8uaG//J
mB3/x5Yf/8aUIP/FkyD/w5Ef/8KQHf/Ajhz/v40c/72KG/+8iRv/uocb/7mGG/+3gxv/tYIb/7SA
Gv+zfxj/sX0X/7B8FP+vehP/rXgQ/6t2Dv+pdAz/qHMK/6ZxCf+kbwf/o24G/6JsBf+gagb/n2kH
/51nCP+cZQn/mmQL/5liDv+YYBD/ll8R/5VdFP+TWxb/kloX/5FYF/+QVxX/jlYU/41UE/+MUxP/
ilES/4lQEv+HThL/hkwS/4RKEv+DSRL/gkcR/4FGD/+ARQ3/f0MM/35CCv98QAj/fEAH/3s+Bf95
PQP/eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AN53OgAkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAADKmRzvzJoa/82cGP/OnRb/0J8V/9KhFf/TohT/1aQU/9amFP/YpxT/2akV/9qrFv/crBj/
3a4a/96vG//fsB3/4LIf/+GzIP/itCH/47Qi/+O0Iv/jtCL/4rQi/+KzIP/gsh//37Ee/9+vHP/d
rhr/3K0Y/9qrF//ZqRX/2KgU/9emFP/VpRT/1KMU/9KhFf/RoBX/z54W/82cF//Mmxn/y5kb/8mX
Hv/HlR//xpQg/8WTIP/DkR7/wpAd/8COHP+/jBz/vYob/7yJG/+6hxv/uIUb/7eDG/+1ghv/tIAZ
/7N/GP+xfRb/r3sU/656Ev+teBD/q3YO/6l0DP+ocwr/pnEI/6RvB/+jbQX/omwF/6BqBv+eaAf/
nWcI/5xlCv+aYwz/mWIO/5hgEP+WXxL/lV0U/5NbFv+SWhj/kVgX/49XFf+OVRT/jVQT/4xTE/+K
URL/iVAS/4dOEv+GTBL/hEoS/4NJEv+CRxH/gUYP/4BEDf9/Qwz/fkIK/3xACP98QAf/ez4F/3k9
A/94OwL/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgDHdzoAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAMqYHe/Lmhr/zJsY/86dF//QnxX/0aAV/9OiFP/UoxT/1aUU/9enFP/YqBT/2akV/9qrF//c
rBj/3a4a/96vG//fsBz/37Ee/+CxH//hsh//4bIg/+GyH//gsh//4LEe/9+wHf/erxv/3a4a/9yt
Gf/bqxf/2aoV/9ioFP/XpxT/1qUU/9SkFP/TohT/0qEV/9CfFf/OnRb/zZsY/8yaGv/KmRz/yJce
/8eVIP/FkyD/xJIg/8KQHv/Bjx3/wI0c/76MHP+9ihv/u4gb/7qHG/+4hRv/toMb/7WBGv+0gBn/
sn4Y/7F9Fv+vexT/rnkS/6x3EP+qdg7/qXQM/6dyCv+mcQj/pG8H/6JtBf+hawX/oGoG/55oB/+d
Zgj/nGUK/5pjDP+ZYg7/l2AQ/5ZeEv+VXRT/k1sW/5JaGP+RWBf/j1cV/45VFP+NVBP/jFMT/4pR
Ev+JTxL/h04S/4ZMEv+EShL/g0gS/4JHEf+ARg//gEQN/39DDP9+Qgr/fEAI/3s/B/96PgX/eT0D
/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD+dzoAknc6AAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAyZcd78uZG//Mmxn/zZwY/8+eFv/QnxX/0qEV/9OiFP/UpBT/1qUU/9enFP/YqBT/2akV/9qr
F//brBj/3K0Z/92uGv/erxv/368c/9+wHP/fsB3/37Ad/9+vHP/erxv/3q4a/92uGf/crBj/2qsX
/9mqFv/YqBX/16cU/9amFP/VpBT/06MU/9KhFf/RoBX/z54W/82cF//Mmxn/y5ob/8mYHf/Ilh//
xpUg/8WTIP/Ekh//wpAd/8GOHf/AjRz/vosc/7yJG/+7iBv/uoYb/7eEG/+2gxv/tYEa/7N/Gf+y
fhf/sHwW/697E/+ueRL/rHcP/6p1Df+pdAv/p3IJ/6ZwCP+kbgb/om0F/6FrBf+faQb/nmgH/51m
Cf+bZQr/mmMM/5liD/+XYBD/ll4S/5RcFf+TWxb/klkY/5BYF/+PVhX/jlUU/4xUE/+LUhP/ilES
/4hPEv+HTRL/hkwS/4RKEv+DSBH/gkcR/4BFD/+ARA3/f0ML/31CCv98QAj/ez8H/3o+Bf95PAP/
eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA33c6AEZ3OgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AADIlh7vypgd/8uaGv/Mmxj/zp0X/8+eFf/RoBX/0qEV/9OjFP/VpBT/1qYU/9enFP/YqBT/2akV
/9qqFv/bqxf/3KwY/9ytGf/drhn/3a4a/92uGv/drhr/3a4Z/9ytGf/crBj/26sY/9qrFv/ZqRX/
2KgU/9enFP/WphT/1aUU/9SjFP/TohT/0aAV/9CfFf/OnRb/zZsY/8yaGv/KmRz/yJce/8eVH//G
lCD/xZMg/8ORHv/CkB3/wI4c/7+MHP+9ihv/vIkb/7qHG/+5hhv/t4Qb/7WCG/+0gBr/s38Z/7F9
F/+wfBX/r3oT/615Ef+rdw//qnUN/6h0C/+ncgn/pXAI/6RuBv+ibAX/oWsG/59pBv+eaAf/nWYJ
/5tkC/+aYw3/mGEP/5dfEP+VXhP/lFwV/5NaF/+SWRj/kFgW/49WFf+OVRT/jFQT/4tSEv+JUBL/
iE8S/4dNEv+FSxL/hEoS/4NIEf+CRxD/gEUO/39EDf9/Qwv/fUIJ/3xACP97Pwb/ej4E/3k8A/93
OgH/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA8Xc6
AH13OgAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AMeWH+/Jlx7/ypkc/8ybGf/NnBj/zp0W/9CfFf/RoBX/0qEV/9OjFP/UpBT/1qUU/9enFP/YpxT/
2KgV/9mpFf/aqhb/2qsX/9urF//brBj/26wY/9usGP/bqxf/2qsX/9qqFv/ZqhX/2KkV/9ioFP/X
pxT/1qYU/9WkFP/UoxT/06IU/9KhFf/QnxX/z54W/82cGP/Mmxn/y5kb/8mYHf/Ilh//x5Ug/8WT
IP/Ekh//wpAd/8GPHf/AjRz/vowc/72KG/+7iBv/uocb/7iFG/+2gxv/tYIb/7SAGf+zfxj/sX0W
/697FP+uehL/rXgQ/6t2Dv+qdQz/qHMK/6dxCf+lbwf/o24G/6JsBf+gagb/n2kG/51nB/+dZgn/
m2QL/5liDf+YYQ//l18R/5VdE/+UXBX/k1oX/5FZGP+QVxb/j1YV/45VFP+MUxP/i1IS/4lQEv+I
TxL/h00S/4VLEv+EShL/gkgR/4FGEP+ARQ7/f0QN/35CC/99QQn/fEAI/3s/Bv96PQT/eTwC/3c6
Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA5nc6AH53OgASAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
x5Ug78iWH//JmB3/y5kb/8ybGf/NnBj/zp0W/9CfFf/RoBX/0qEV/9OiFP/UpBT/1aUU/9amFP/X
pxT/2KcU/9ioFP/YqRX/2akV/9mpFf/ZqRX/2akV/9mpFf/ZqRX/2KgU/9ioFP/XpxT/1qYU/9Wl
FP/UpBT/1KMU/9OiFP/SoRX/0J8V/8+eFv/NnBf/zJsZ/8uaGv/KmB3/yJYe/8eVIP/FlCD/xZMg
/8ORHv/CkB3/wI4c/7+NHP+9ixv/vIkb/7uIG/+5hhv/t4Qb/7aDG/+1gRr/s38Z/7J+GP+wfBb/
r3sU/655Ev+sdxD/qnYO/6l0DP+ncgr/pnEI/6RvB/+jbQX/omwF/6BqBv+eaAf/nWcI/5xlCv+a
Ywv/mWIO/5hgEP+WXxH/lV0U/5NbFv+SWhf/kVgX/5BXFv+OVhT/jVQT/4xTE/+KURL/iVAS/4hO
Ev+HTRL/hUsS/4NJEv+CSBH/gUYQ/4BFDv9/RAz/fkIL/31BCf98QAf/ez8G/3o9BP95PAL/dzoB
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgDtdzoApHc6AE13OgAFAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADG
lCDlx5Ug9MiWHvTKmB30y5ob9MybGfTNnBj0zp0W9NCfFfTRoBX00qEV9NOiFPTUoxT01KQU9NWl
FPTWphT016YU9NenFPTXpxT02KcU9NinFPTYpxT016cU9NenFPTXpxT01qYU9NWlFPTVpBT01KMU
9NOiFPTSoRX00aAV9NCfFfTPnhb0zZwX9MybGfTLmhr0ypkc9MmXHvTHlR/0xpQg9MWTIPTDkR/0
wpAd9MGPHfTAjRz0vowc9L2KG/S7iBv0uocb9LiFG/S3gxv0tYIb9LSAGvSzfxn0sX0X9LB8FfSv
ehP0rXkR9Kt3D/SqdQ30qHQL9KdyCfSlcAj0pG4G9KJtBfShawX0n2kG9J5oB/SdZgj0nGUK9Jpj
DPSZYg70l2AQ9JZeEvSVXRT0k1sW9JJaGPSRWBf0j1cV9I5VFPSNVBP0jFMT9IpREvSJUBL0h04S
9IZMEvSEShL0g0kS9IJHEfSBRg/0gEUN9H9DDPR+Qgr0fEEI9HxAB/R7PgX0ej0D9Hg7AvR3OgD0
dzoA83c6AOp3OgDYdzoAvXc6AJh3OgBqdzoAM3c6AAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP//
8AAAAAAAAAAAAAAAAAD//4AAAAAAAAAAAAAAAAAA//4AAAAAAAAAAAAAAAAAAP/4AAAAAAAAAAAA
AAAAAAD/4AAAAAAAAAAAAAAAAAAA/8AAAAAAAAAAAAAAAAAAAP+AAAAAAAAAAAAAAAAAAAD/AAAA
AAAAAAAAAAAAAAAA/gAAAAAAAAAAAAAAAAAAAPwAAAAAAAAAAAAAAAAAAAD4AAAAAAAAAAAAAAAA
AAAA8AAAAAAAAAAAAAAAAAAAAPAAAAAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAA4AAAAAAA
AAAAAAAAAAAAAMAAAAAAAAAAAAAAAAAAAADAAAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAA
AIAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAA
AAAAAAAAAQAAAAAAAAAAAAAAAAAAAAMAAAAAAAAAAAAAAAAAAAADAAAAAAAAAAAAAAAAAAAABwAA
AAAAAAAAAAAAAAAAAAcAAAAAAAAAAAAAAAAAAAAPAAAAAAAAAAAAAAAAAAAADwAAAAAAAAAAAAAA
AAAAAB8AAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAAAAAAAAAAAfwAAAAAAAAAAAAAAAAAAAP8AAAAA
AAAAAAAAAAAAAAH/AAAAAAAAAAAAAAAAAAAD/wAAAAAAAAAAAAAAAAAAB/8AAAAAAAAAAAAAAAAA
AB//AAAAAAAAAAAAAAAAAAB//wAAAAAAAAAAAAAAAAAB//8AAAAAAAAAAAAAAAAAD///KAAAAEAA
AACAAAAAAQAgAAAAAAAAQAAAIy4AACMuAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAdzoAAXc6ADl3OgCFdzoAvXc6AOJ3OgD0dzoA93c6APd3OgD3dzoA93c6APd3OgD3
dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3
OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6
APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA93c6APd3OgD3dzoA
93c6APIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB3OgACdzoAXXc6ANZ3OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD6AAAAAAAAAAAAAAAAAAAAAAAAAAB3OgAk
dzoAxnc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA+gAAAAAAAAAAAAAAAAAAAAB3OgBDdzoA8Hc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APoAAAAAAAAAAAAAAAB3OgBDdzoA93c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD6AAAAAAAAAAB3OgAkdzoA8Hc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA+gAAAAB3OgACdzoAxnc6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
APoAAAAAdzoAXXc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD6dzoAAXc6ANZ3OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
+nc6ADp3OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APp3OgCGdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP96PQD/hEgA/4xQAP+SVgD/l1sA/5leAP+aXgD/mV0A/5ZZAP+QVAD/iUwA
/35BAP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD6
dzoAvnc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/31AAf+QVAP/oWYE/7B2BP+9hAT/xYwC/8WMAP/FiwD/xIoA
/8OJAP/DiAD/wocA/8GGAP/AhQD/wIUA/8CEAP++gwD/uHwA/6hrAP+UVwD/fkEA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA+nc6AON3OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP+HSwP/oWcG/7mBCv/IkAv/yJAK
/8ePB//Hjgb/xo0E/8aNAv/FjAD/xYsA/8SKAP/DiQD/w4gA/8KHAP/BhgD/wIUA/8CFAP/AhAD/
v4MA/76CAP+9gQD/vYAA/7t+AP+laAD/g0cA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APp3
OgD1dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/h0sD
/6RqCP/AiAz/y5QO/8uTDf/Jkgz/yZEL/8iQCv/Hjwf/x44G/8eOBf/GjQL/xYwA/8WLAP/EigD/
wogA/7+FAP+9gwD/vYIA/7+EAP/AhQD/wIQA/7+DAP++ggD/vYEA/72AAP+8fwD/u38A/7p9AP+d
YAD/eTwA/3c6AP93OgD/dzoA/3c6AP93OgD6dzoA93c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/4lNBP+lbAn/wosO/86YD//Olw//zZYO/8uVDv/Lkw3/yZIM/8mRC//IkAr/
w4oH/7N5Bf+kagP/mFwB/41SAf+NUwn/jlcR/49ZF/+SXBr/klwa/49ZF/+JUhL/hEoK/4FFAf+J
TAD/lFgA/6JlAP+ydQD/vH8A/7t/AP+7fgD/un0A/6ptAP97PgD/dzoA/3c6AP93OgD/dzoA+nc6
APeCRwn/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3xAAf+TWQb/rHQL/8eREP/QmxH/z5oR/8+ZEP/OmBD/
zpcP/82WDv/LlQ7/xY0M/651CP+YXgX/hEgC/3g7AP93OgD/dzoA/5ZiHv/aumD/48Zp/+PGaf/j
xmn/48Zp/+PGaf/jxmn/4sVn/+LDZP/fvl//0qxS/8CVQP+rey3/k10Y/4RIA/+WWQD/sHMA/7p9
AP+5fAD/qmwA/3k8AP93OgD/dzoA/3c6APp3OgD3gUYI/7J9Lf+7hzL/o2wf/49VEP9+QgX/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/ez8C/4lOBv+aYAv/rHUP/8CLEv/SnhX/
0p4U/9GdE//RnBL/0JsR/9CaEP/PmRD/zpgP/76GDf+kagj/ik8D/3g7AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/fEEF/4RLDf+LVBT/lWEd/6JxKv+xhTj/w5xK/9e3XP/iw2T/4cFh/+C/
X//gvVz/37ta/9+5V//as1D/vY82/5dgGP+MTwH/rG8A/7l7AP+YWwD/dzoA/3c6AP93OgD6dzoA
93c6AP93OgD/kFYT/8aTOP/fsEb/3q9D/9amPP/LmTP/w48r/76KJv+8hiP/vIgi/8CLIf/GkiL/
z5wj/9akJP/WoyD/1aId/9WhGf/ToBf/058V/9KeFP/RnRP/0ZwS/86YEf+3gAz/nGIH/4JGAv93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/hEsM/6BuJv+/lUH/3blZ/9+7Wv/fuVf/3rdT/921T//ds0v/zaA9
/5hhFv+SVAD/s3UA/31AAP93OgD/dzoA+nc6APd3OgD/dzoA/3c6AP97PwP/j1UQ/7eCKf/Xpjz/
3a09/9yrOf/bqzf/26kz/9qpMP/Zpy3/2Kcq/9ilJ//XpST/1qMg/9WiHv/VoRr/1KAX/9KfFf/D
jRH/q3ML/5JXBv97PgH/dzoA/3c6AP93OgD/dzoA/3U5B/9uMx//bjMf/24zH/9uMx//bjMf/24z
H/9uMx//bjMf/24zH/9uMx//bjMf/24zH/9uMx//bjMf/24zH/9uMx//cTYW/3Y6A/93OgD/dzoA
/35CBv+kcSf/0qlN/963U//dtU//3bNL/9yxR//bsEL/toQo/4NGAf+LTgD/dzoA/3c6APp3OgD3
dzoA/3c6AP/PuaX/2sm4/+nbxP/u4cf/697F//Xoyf/16Mj/9ejH//Xnxv/158X/9OfE//TmxP/0
5sP/9ObC//Tlwf/z5cD/8+W//+7euv/QuJz/sIxr/4pVJP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP9vNB3/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/86C9z/RhSx/1kjb/9wNRr/dzoA/3k9Av+mdCb/2rFN/92zS//cskf/
27BD/9uuPv+1giX/dzoA/3c6AP93OgD6dzoA93c6AP93OgD/3My+////////////////////////
////////////////////////////////////////////////////////////////////////////
//////7/3c7B/5psQv93OgD/dzoA/3c6AP93OgD/dzoB/zwM1/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OQrf/1IdiP9yNxH/dzoA/4tRD//Tp0T/3LJH/9uwQ//brj7/2qw6/4tRDP93OgD/dzoA+nc6APd3
OgD/dzoA/8Spkf//////////////////////////////////////////////////////////////
////////////////////////////////////////////////////////zLWg/31DDP93OgD/dzoA
/3c6AP9GFbD/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/Pg7N/2gvNv93OgD/iVAN/9itRf/b
sEP/264+/9qsO/+jbBn/dzoA/3c6APp3OgD3dzoA/3c6AP+shmT/////////////////////////
////////////////////////////////////////////////////////////////////////////
///////////////////////l2c//g0wX/3c6AP93OgD/Uh2I/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/86C9v/Zy46/3c6AP+reCX/3LBD/9uuPv/arDv/n2gX/3c6AP93OgD6dzoA93c6
AP93OgD/lGQ3////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////+bb0v9+RQ7/
dzoA/10mYP84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/z0N0v9xNhb/h00L/9yw
Q//brj//2as6/4JHB/93OgD/dzoA+nc6APd3OgD/dzoA/31CDP/+/f3/////////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////0Luo/3c6AP9oLzj/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/UR2J/3s/A//csET/264//656If93OgD/dzoA/3c6APp3OgD3dzoA
/3c6AP93OgD/7OPc/////////////////////////////////+DRxP/byrv/28q7/9vKu//byrv/
28q7/9vKu//byrv/28q7/9vLu//i1cr//Pv6//////////////////////////////////////+d
cEf/dTgJ/2MqSP9jKkj/YypI/2MqSP9jKkj/YypI/2MqSP9jKkj/YypI/2MqSP9jKkj/YypI/2Mq
SP9jKkj/YypI/2MqSf9dJl7/RxWt/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zoL2/+ARhP/3LBE
/8OTMf96PQL/dzoA/3c6AP93OgD6dzoA93c6AP93OgD/dzoB/9TAr///////////////////////
//////////+Za0H/eDsC/3g7Av94OwL/eDsB/3g7Af93OgH/dzoA/3c6AP93OgD/dzoA/4ZPHf/e
z8L/////////////////////////////////39DD/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6Av9HFa//OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/jFpZ/8OUNP99QQT/dzoA/3c6AP93OgD/dzoA+ng8Avd5PAP/
ej0E/3o9BP+9n4T/////////////////////////////////s5Bw/3s/Bv97Pwb/ez8G/3s+Bf96
PgX/ej0E/3k9A/95PAP/eDsC/3g7Af93OgH/f0YQ//Pt6f//////////////////////////////
/v+JVST/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/ViF3/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/3RDev96PQL/
dzoA/3c6AP93OgD/dzoA/3c6APp7Pwb3fEAH/3xACP99QQj/qH9a////////////////////////
/////////8uznf9+Qgr/fkIK/31CCv99QQn/fUEJ/3xACP98QAf/ez8H/3s/Bv96PgX/ej0E/3k8
A//Bpoz/////////////////////////////////ropo/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/2AoVP84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/9PG5D/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD6fkIK935DC/9/
Qwz/f0QM/5NgMv/////////////////////////////////i1cn/gEUO/4BFDv+ARQ7/gEQN/39E
Df9/Qwz/f0ML/35CC/99Qgr/fUEJ/3xACP97Pwf/pn1Y////////////////////////////////
/8mwmv93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3A1Gv9HFa3/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/RRS0/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA+oBFDveBRg//gUYQ/4JHEP+ESRT/+/j3////////////////////
////////+PXz/4RKE/+DSBH/g0gR/4JIEf+CRxH/gUcQ/4FGD/+ARQ//gEUO/39EDf9/Qwz/fkIK
/5FeL//////////////////////////////////h08f/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/djkE/1skZ/87DNj/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/0IRv/93OgD/dzoA/3c6AP93OgD/dzoA/3c6APqDSBH3g0kS/4RK
Ev+FSxL/hUsS/+fb0P////////////////////////////////+WYzH/hkwS/4ZMEv+GTBL/hUsS
/4RKEv+EShL/g0kS/4NIEf+CRxH/gUYP/4BFDv+BRhD/+vj2////////////////////////////
+PTy/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/aS80/0MSu/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9KGKL/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD6hkwS94dNEv+IThL/iE8S/4lPEv/Svab/////////////////////
////////////roZb/4pREv+JUBL/iVAS/4hPEv+ITxL/h04S/4dNEv+GTBL/hUsS/4RKEv+DSBH/
gkcR/+XZz/////////////////////////////////+LViX/eDsC/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP9yNxH/UR2L/zgJ4v84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/XCZi/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA+olQEveKURL/i1IS
/4tTE/+MUxP/wKB9/////////////////////////////////8Snhv+NVBP/jVQT/4xUE/+MUxP/
i1IT/4tSEv+KURL/iVAS/4hPEv+HTRL/hkwS/4VLEv/QuqX/////////////////////////////
////pHpT/3o+Bf95PAP/dzoB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6Af9fKFf/PQ3R/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/RhSy/3U5B/93OgD/dzoA
/3c6AP93OgD/dzoA/3c6APqNVBP3jVUU/45VFP+PVhX/j1cV/66EVf//////////////////////
///////////ZxrD/kFgW/5BXFv+PVxX/j1YV/45WFP+OVRT/jVQT/4xTE/+LUhP/ilES/4lQEv+I
TxL/vJx8/////////////////////////////////7ydgf98QAj/ez8G/3o9BP94OwL/dzoA/3c6
AP93OgD/dzoA/2wyJ/9HFa3/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/TBme/3Q4DP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD6j1cW95BYFv+RWRf/
klkX/5JaF/+daiz/////////////////////////////////7eTZ/5NbFv+TWxb/kloX/5JaF/+R
WRf/kVgX/5BXFv+PVxX/jlYU/41VFP+MUxP/i1IS/6l/Uv//////////////////////////////
///Uv63/f0ML/31BCf98QAf/ej4F/3k8Av93OgD/dDgK/1Ugev85Ct7/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9BEML/ZCtH/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA+pJaF/eTWxb/lFwV/5VdFP+WXhP/ll8S//bx6///////////////////
//////////38+/+aZBj/ll8R/5ZeEv+VXhP/lV0U/5RcFf+TWxb/kloX/5FZF/+QWBb/j1YV/45V
FP+XYyn/////////////////////////////////6uHY/4BFDv9/RAz/fkIK/3xACP97PgX/ZS1I
/0APyP84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zoL2/9YInD/
dTkH/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APqWXhL3l18R/5hgEP+Y
YQ//mWIO/5liDf/l1sH/////////////////////////////////rYE7/5pjDf+ZYg3/mWIO/5hh
D/+XYBD/ll8S/5VdE/+UXBX/k1sW/5JZF/+RWBf/j1cW//Xw6///////////////////////////
//38+/+GTRn/gUYP/39EDf99Qg7/Ux+O/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/0oYov9vNB//dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD6mWIO95pjDf+bZAv/nGUK/5xmCf+dZgj/1LyU////////////////////
/////////////8GfY/+dZwj/nWYJ/5xlCf+bZAr/mmQM/5liDf+YYQ//l2AQ/5ZfEv+VXRT/k1sW
/5JaF//i08P/////////////////////////////////nG0//4NIEf+BRhD/VCGR/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/Pw/J/2MrSv93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA+pxlCfedZwj/nmgH/59p
B/+gagb/oGoG/8WkZ//////////////////////////////////Uu47/oWsG/6BqBv+faQb/nmgH
/55nCP+dZgn/nGUK/5pkDP+ZYg7/mGEP/5ZfEf+VXRT/0LeZ////////////////////////////
/////7OOaf+FSxL/bzhM/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/85
Ct//VSB7/3Q4C/93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6APqfaQb3oGoG/6FsBv+ibQX/o20G/6RuBv+3jTz/////////////////////
////////////5ta6/6RvB/+jbgb/o20G/6JsBf+hawb/oGoG/59pB/+eZwj/nGYJ/5tkC/+ZYg3/
mGEP/7+cbf/////////////////////////////////Jr5P/iE4S/08dpv84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9IFaz/bDIn/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD6om0F96RuBv+lcAf/pnEI
/6dyCf+ocwr/rHkW//7+/f////////////////////////////bw5v+ocwv/p3IK/6dxCf+mcAj/
pG8H/6NuBv+ibAX/oWsG/59pBv+eZwf/nGYJ/5tkC/+ug0D/////////////////////////////
////38+9/4pREv8+D9X/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/z4Oz/9gKVT/dzoB/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA+qZxCPencgr/qXQL/6p1Df+rdg7/q3cP/6x3EP/z7N7/////////////////
////////////////soEi/6t2D/+qdQ3/qXQM/6hzC/+ncgn/pXAI/6RuBv+ibQX/oWsG/59pBv+d
Zwj/om4a//////////////////////////////////Lr5P+MUxP/Ogve/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/1Edlf92OhP/eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APqpdQz3q3YO/6x3EP+teRH/
rnoT/697FP+wfBX/5tW1/////////////////////////////////8OcT/+vexP/rnoS/614Ef+s
dw//qnUN/6l0DP+ncgr/pnAI/6RuBv+ibAX/oGoG/6l5JP//////////////////////////////
///8+vj/jlYV/0ITy/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/92PB//ez8H/3o9A/93OgH/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD6rXgR9656E/+wexT/sX0W/7J+F/+zfxj/s38Z/9rAjv//////////////////
///////////////TtXv/sn4Y/7F9F/+wfBX/r3sU/655Ev+seBD/qnYO/6l0C/+ncgn/pXAH/6Nu
B//Zw5r/////////////////////////////////+/j2/5BXFv9VJJ//OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/Yy1g/31BCP97Pwb/eTwC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA+rB8FfexfRf/s38Z/7SAGv+1
gRr/toIb/7eDG//PrGb/////////////////////////////////8+vb/+XTsP/l07D/5dOv/+TS
r//k0q7/49Gt/+PQrf/i0Kz/4c+r/+LQrv/y6dv/////////////////////////////////////
/+3k2v+SWRf/cj5c/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zkK3/9NG6H/UB2V/08clP9PG5P/
ThuT/04bk/9OG5P/ThuT/04bk/9OG5P/ThuT/04bk/9OG5P/ThuT/04bk/9OG5P/WyVm/3c6AP93
OgD/dzoA/3c6APqzfxn3tYEa/7aCG/+3hBv/uYUb/7qHG/+7iBv/xpo+////////////////////
////////////////////////////////////////////////////////////////////////////
///////////////////////////////////////Tu53/k1sW/5BYHP9EFMf/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/0IRvv93OgD/dzoA/3c6AP93OgD6toIb97iFG/+6hhv/u4gb/7yJ
G/+9ixz/vowc/8COH//9+/f/////////////////////////////////////////////////////
///////////////////////////////////////////////////////////////////////+/v3/
qnw6/5VeE/+TWxb/eUNQ/zkK4v84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/85CuD/dTkH/3c6
AP93OgD/dzoA+rmGG/e7iBv/vYob/76MHP/AjRz/wY8c/8KQHf/DkR7/8+nS////////////////
////////////////////////////////////////////////////////////////////////////
////////////////////////////////07uU/5liDf+XYBD/lF0U/5JaF/9oNHX/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/2sxLP93OgD/dzoA/3c6APq8iRv3vosc/8COHP/Bjx3/w5Ee
/8SSH//FkyD/xpQg/+rXq///////////////////////////////////////////////////////
////////////////////////////////////////////////////////////////4dGz/6BqDf+b
ZAv/mWEP/5ZeEv+TWxb/kVgX/2o3bf86C9//OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9gKVT/dzoA
/3c6AP93OgD6v4wc98GPHf/DkR7/xZMg/8aUIP/Hlh//yZce/8qYHP/ixoH/////////////////
////////////////////////////////////////////////////////////////////////////
///////////////7+fT/0LR//6NuCf+faQb/nWYJ/5pjDP+XYBD/lV0U/5JaF/+QVxb/fUc9/0wb
s/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/VSB8/3c6AP93OgD/dzoA9sGPHffEkh//xpQg/8eWH//JmB3/
y5ob/8ybGf/NnBj/2bNM//bs0P/27ND/9uzQ//Xs0P/169D/9evR//Xr0f/06tL/9OrS//Tq0v/z
6dL/8+nS//Lo0v/y6NH/8efR//Hm0f/w5tH/7eDJ/+LOqf/NrW//r34d/6ZxCf+jbgb/oWsG/55o
B/+bZAr/mWEP/5ZeEv+TWxb/kVgX/45WFP+MUxP/eEI+/14qf/9OHKj/Rha7/0UUvv9FFL3/RBO9
/0QTvP9DErz/QxK8/0MSvP9DErz/QxK8/0MSvP9DErz/QxK8/0MSvP9DErz/QxK8/1Mfhf93OgD/
dzoA/3c6AOTEkh/3xpQg/8iWHv/LmRv/zJsZ/86dF//QnxX/0aAV/9KhFf/TohT/06IU/9KiFP/S
oRX/0aAV/8+eFv/NnBf/zJoa/8qYHf/Hlh//xZMg/8ORHv/Bjhz/vosc/7uIG/+4hRv/toIa/7N/
Gf+wfBX/rnkS/6t2Dv+ocwr/pW8H/6JsBf+faQf/nGYJ/5pjDf+XXxH/lFwV/5JZF/+PVhX/jVQT
/4pREv+HThL/hEoS/4JHEf+ARA3/fkIK/3w/B/95PQP/dzoB/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgC/xpQg98iXHv/Lmhv/zZwY/8+eFv/S
oRX/06IU/9WkFP/WpRT/1qYU/9amFP/WphT/1aUU/9SkFP/TohT/0aAV/86dFv/Mmxn/ypgc/8eV
H//FkyD/wpAe/8CNHP+9ihv/uocb/7eEG/+0gRr/sn4X/697E/+sdxD/qXQM/6ZxCP+jbQb/oGoG
/51nCP+aZAz/mGEQ/5VdE/+SWhf/kFcW/41VFP+LUhL/iE4S/4VLEv+CSBH/gEUO/35DC/98QAj/
ej0E/3g7Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoAiMiWH/fLmRv/zZwY/9CfFf/SoRX/1KQU/9amFP/YqBT/2akV/9qqFv/aqhb/2qoW/9mp
Ff/XpxT/1qUU/9SjFP/RoBX/z54W/8ybGf/JmB3/x5Ug/8SSH//Bjx3/v4wc/7yJG/+5hRv/toIa
/7N/Gf+wfBX/rXkR/6p1Df+ncgn/pG4G/6FrBv+eaAf/m2UK/5lhD/+WXhL/k1sW/5BYF/+OVRT/
i1IT/4hPEv+GTBL/g0gR/4FGD/9/Qwz/fEEI/3o+Bf94OwL/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6ADzJmB33zJsZ/8+eFv/SoRX/1KQU/9en
FP/ZqRX/26wY/92tGf/erxr/3q8b/92uGv/crRn/2qsX/9ioFf/WphT/1KMU/9GgFf/OnRf/y5oa
/8iWHv/FlCD/w5Ae/8CNHP+9ihv/uocb/7aDG/+0gBn/sX0W/656Ev+rdg7/qHMK/6RvB/+ibAX/
n2kH/5xlCv+ZYg7/ll8R/5NbFv+RWBf/jlYU/4xTE/+JUBL/hkwS/4NJEv+BRhD/f0MM/31BCf97
PgX/eDsC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
ANh3OgABy5kb982cF//RoBX/1KMU/9amFP/ZqRX/3KwY/96vG//gsR7/4bMg/+KzIf/hsiD/37Ad
/92uGv/bqxf/2KgU/9WlFP/TohT/z54W/8ybGf/JmB3/xpQg/8SSH//Bjhz/vosc/7uIG/+3hBv/
tIEa/7F9F/+vehP/q3cP/6hzC/+lcAj/omwF/59pBv+cZgn/mWIN/5dfEf+UXBX/kVkX/49WFf+M
UxP/iVAS/4dNEv+ESRL/gUYQ/39EDP99QQn/ez8G/3k8Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgBgAAAAAMyaGvfOnRb/0qEV/9WkFP/YqBT/2qsX
/96uGv/gsR//47Qi/+W3Jf/ltyb/5LYk/+KzIf/fsB3/3K0Z/9mqFv/XphT/1KMU/9GgFf/NnBj/
ypkc/8eVH//Ekh//wY8d/76MHP+7iBv/uIUb/7WBGv+yfhj/r3sU/6x3EP+pdAv/pnAI/6JtBf+f
aQb/nWYJ/5pjDf+XYBD/lFwV/5JZF/+PVhX/jFQT/4lQEv+HTRL/hEoS/4FHEP9/RA3/fUEJ/3s/
Bv95PAP/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgDIdzoA
AgAAAADMmhn3z54W/9KhFP/VpRT/2KgU/9usGP/erxz/4bMg/+S2Jf/nuSn/6bsr/+a4KP/jtSP/
4LEf/92uGv/aqhb/16cU/9SjFP/RoBX/zpwX/8uZG//Hlh//xJIg/8GPHf+/jBz/u4gb/7iFG/+1
gRr/sn4Y/697FP+sdxD/qXQM/6ZxCP+jbQX/oGoG/51mCP+aYwz/l2AQ/5RcFf+SWRf/j1YV/41U
E/+KUBL/h00S/4RKEv+BRxD/f0QN/31BCf97Pwb/eTwD/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgDxdzoAJgAAAAAAAAAAzJoa98+eFv/SoRX/1aUU/9ioFP/brBf/
3q8b/+GyIP/ktiT/5rgo/+e5Kf/ltyb/47Qi/+CxHv/drhr/2qoW/9enFP/UoxT/0aAV/82cF//L
mRv/x5Uf/8SSIP/Bjx3/vowc/7uIG/+4hRv/tYEa/7J+GP+vexT/rHcQ/6l0DP+mcAj/om0F/59p
Bv+dZgj/mmMM/5dgEP+UXBX/klkX/49WFf+MVBP/iVAS/4dNEv+EShL/gUcQ/39EDf99QQn/ez8G
/3k8A/93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD4dzoARgAAAAAAAAAA
AAAAAMuaGvfOnRf/0aAV/9SkFP/XpxT/2qoW/92uGf/fsB3/4rMh/+O1I//ktSP/47Qi/+GyH//e
rxz/3KwY/9mpFf/WphT/06IU/9CfFf/NnBj/ypgc/8eVIP/Ekh//wY8d/76LHP+7iBv/uIQb/7WB
Gv+yfhf/r3sT/6x3D/+pdAv/pXAI/6JtBf+faQb/nWYJ/5pjDf+XYBH/lFwV/5FZF/+PVhX/jFQT
/4lQEv+HTRL/hEoS/4FGEP9/RA3/fUEJ/3s/Bv95PAL/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgDxdzoARgAAAAAAAAAAAAAAAAAAAADKmRz3zZwY/9CfFf/TohT/1qUU/9ioFP/a
qxf/3a4Z/96vHP/gsR7/4LEe/9+wHf/erxv/3K0Y/9mqFv/XpxT/1KQU/9KhFf/Pnhb/zJsZ/8mX
Hv/GlCD/w5Ee/8COHP+9ihv/uocb/7eEG/+0gBr/sX0X/656E/+rdg//qHML/6VwB/+ibAX/n2kH
/5xmCf+ZYg3/l18R/5RcFf+RWRf/j1YV/4xTE/+JUBL/hk0S/4NJEv+BRhD/f0QM/31BCf97Pwb/
eDwC/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgDIdzoAJgAAAAAAAAAAAAAAAAAAAAAA
AAAAyZce98uaGv/OnRf/0aAV/9OjFP/WphT/2KgU/9mqFv/brBf/3K0Y/9ytGf/crBj/2qsX/9mp
Ff/XpxT/1aUU/9OiFP/QnxX/zZwY/8qZHP/Hlh//xZMg/8KQHf+/jRz/vIkb/7mGG/+2gxv/s38Z
/7B8Fv+ueRL/qnYO/6dyCv+kbwf/oWsG/55oB/+cZQr/mWIO/5ZeEv+TWxb/kVgX/45VFP+LUxP/
iVAS/4ZMEv+DSRL/gUYP/39DDP99QQj/ez4F/3g7Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6ANh3
OgBgdzoAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMeVIPLKmB36zJsZ+s6dFvrRoBX606IU+tWl
FPrXphT62KcU+tioFfrYqBX62KgU+tenFPrWphT61KQU+tKiFPrQnxX6zZwX+suaGvrIlx76xpQg
+sORH/rBjhz6vosc+ruIG/q4hRv6tYEa+rJ+GPqwexT6rXgQ+ql1DPqmcQn6o24G+qFrBvqeZwf6
m2QL+phhD/qVXhP6k1oX+pBYFvqOVRT6i1IS+ohPEvqFSxL6g0gR+oBFD/p/Qwv6fEAI+no+Bfp4
OwH6dzoA93c6AOV3OgDAdzoAiHc6ADx3OgABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAD/AAAAAAAAAPwAAAAAAAAA+AAAAAAAAADwAAAAAAAAAOAAAAAAAAAAwAAAAAAAAACAAAAAAAAA
AIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAEAAAAAAAAAAQAAAAAAAAADAAAAAAAAAAcAAAAAAAAADwAAAAAAAAAfAAAAAAAAAD8AAAAAAAAA
/ygAAAAwAAAAYAAAAAEAIAAAAAAAACQAACMuAAAjLgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAHc6AAF3OgA3dzoAiHc6AMZ3OgDsdzoA+Hc6APl3OgD5dzoA+Xc6APl3OgD5dzoA+Xc6APl3
OgD5dzoA+Xc6APl3OgD5dzoA+Xc6APl3OgD5dzoA+Xc6APl3OgD5dzoA+Xc6APl3OgD5dzoA+Xc6
APl3OgD5dzoA+Xc6APl3OgD5dzoA+Xc6APl3OgD5dzoA+Xc6APl3OgD5dzoA+Xc6APl3OgD5dzoA
+Xc6APUAAAAAAAAAAAAAAAB3OgAAdzoAKnc6AK93OgD3dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APsAAAAAAAAAAAAAAAB3OgBQdzoA6Xc6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APsAAAAAdzoAAHc6
AFB3OgD7dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6APsAAAAAdzoAKnc6AOl3OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APt3OgABdzoAsHc6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APt3OgA4
dzoA93c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6APt3OgCJdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3g7AP97PgD/fUAA/39CAP+AQwD/gEMA/39CAP99QAD/ej0A/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APt3OgDHdzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/eDsA/4dLAv+ZXgP/p2wD/7J4Av+5fwD/vYMA/8CFAP/B
hgD/wIYA/76DAP+6fgD/s3cA/6puAP+ZXAD/hEgA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
APt3OgDtdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/f0MB/5RaBf+vdgj/xIwL/8iQCv/Hjwf/
x44F/8aNA//FjAD/xIoA/8OJAP/CiAD/wocA/8GGAP/AhAD/v4MA/76CAP+9gQD/vIAA/65yAP+M
UAD/eDsA/3c6AP93OgD/dzoA/3c6APt3OgD5dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP94OwD/gEMC/5VbBv+zegv/xo8N
/8uUDv/Kkw3/yZIM/8iQCv/FjQf/wIcF/7h/A/+tcwH/qG4D/6RqBv+hZwb/n2UG/59kBf+gZQL/
pWkA/69zAP+3ewD/u38A/7t/AP+5fQD/oWQA/3s+AP93OgD/dzoA/3c6APt5PQL5ez8E/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/g0cD/5ti
CP+2fw3/yZMQ/8+ZEP/OmA//zZYP/8uVDv/Diwz/rHMI/5hdBP+HTAL/fUEA/4lREf/EnUv/z6xV
/9OyWv/VtFz/1bRb/9GvV//LpU7/wJdD/7CCM/+gbB//lFoK/5xfAP+0eAD/un0A/6VoAP94OwD/
dzoA/3c6APt3OgD5kVgU/7N/Lf+veij/nGQZ/49VD/+HTAn/gkcG/4BFBf+BRgX/hEkG/4pPCP+U
Wgv/omoO/7R9Ef/IlBT/0p4U/9GdE//RnBL/0JsR/8yWEP+7gwz/omkI/4lNA/94OwD/dzoA/3c6
AP93OgD/dzoA/3c6AP98QAX/g0oM/4xVFf+ZZiH/qXsx/7yUQ//QrFP/371e/+C9Xf/fuln/3rdV
/8qeQf+lcCD/ml8G/7FzAP+VWAD/dzoA/3c6APt3OgD5dzoA/3w/A/+gaBz/yZc3/9mpPv/Ypzr/
1aM0/9OhMP/ToCz/06Ep/9WjJv/WpCP/1qMf/9WhGv/ToBf/0Z0U/8mUEv+1fQz/m2EH/4RIAv95
PAD/dzoA/3U5B/91OAj/dTgI/3U4CP91OAj/dTgI/3U4CP91OAj/dTgI/3U4CP91OAj/dTgI/3U5
Bv97QAX/iVEQ/6l4LP/Ppkv/3bdV/922UP/cs0r/z6E8/6BoFv+bXQD/ez4A/3c6APt3OgD5dzoA
/6iBXf/Fqo3/0rmW/+LLoP/v2ab/79ik/+7Yov/u16D/7dee/+3WnP/s1Zr/7NWY/+zUlv/kyo3/
wZ1o/5BdJ/93OgD/dzoA/3c6AP93OgD/dTkG/00ZmP9DErr/QxK6/0MSuv9DErr/QxK6/0MSuv9D
Err/QxK6/0MSuv9DErr/QxK6/0QTtv9LGKD/WSNt/240IP98QAT/n2sh/9SqSf/ds0r/3LFF/9ms
Pv+cZRX/eDsA/3c6APt3OgD5dzoA/7+jiP//////////////////////////////////////////
///////////////////////////////////////dzb//mWtB/3c7Af93OgD/dzoA/0wZnP84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zkK4f9R
HIv/cDUY/4JHCP/KnT3/3LFF/9uvP//OnjP/fUEE/3c6APt3OgD5dzoA/6iAXP//////////////
////////////////////////////////////////////////////////////////////////+ff1
/7ucgP95PQT/dzoA/1chdf84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/QhHA/2wxKf+JTw3/1alB/9uvP//XqTn/gkYG/3c6APt3
OgD5dzoA/5BeL///////////////////////////////////////////////////////////////
//////////////////////////////////+9n4T/eDwD/2IqTv84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/0EQwv90OAz/
uoku/9uvP//JmTH/ez8D/3c6APt3OgD5dzoA/3o/B//8+vn/////////////////////////////
///////////////////////////////////////////////////////////////+/fz/pn1Y/24z
I/8+Dc7/Pg3O/z4Nzv8+Dc7/Pg3O/z4Nzv8+Dc7/Pg3O/z4Nzv8+Dc7/Pg3O/z4Nz/86C9v/OAnj
/zgJ4/84CeP/OAnj/zgJ4/9XIXX/rXkl/9quP/+XXxP/dzoA/3c6APt3OgD5dzoA/3c6AP/o3dT/
/////////////////////8aslP+gdk3/oHVN/6B1Tf+gdU3/oHVN/6B1TP+gdk7/s5Bx/+zk3P//
////////////////////6eDX/35EEP9yNhL/cjYS/3I2Ev9yNhL/cjYS/3I2Ev9yNhL/cjYS/3I2
Ev9yNhL/cjYS/3I2Ev9vNB7/UByO/zgJ4/84CeP/OAnj/zgJ4/9DErv/t4Y3/6l1If94OwD/dzoA
/3c6APt4OwL5eTwD/3k9A//RvKn//////////////////////8Knjf97PgX/ej4F/3o+BP96PQT/
eTwD/3g8Av94OwH/dzoB/4pVI//49fL//////////////////v39/6F3T/93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/bDIn/zgJ4v84CeP/OAnj/zgJ
4/88Dtv/kFsv/3g7Af93OgD/dzoA/3c6APt8QAf5fEAI/31BCf+7m37/////////////////////
/9vLvP9+Qwv/fkIK/35CCv99QQn/fUEJ/3xACP97Pwb/ej4F/3o9BP/NtqH/////////////////
/////8atlf93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/cDUa/zsM1/84CeP/OAnj/zgJ4/84CeP/Zy45/3c6AP93OgD/dzoA/3c6APt/RAz5gEQN/4BF
Dv+nfVj////+//////////////////Ls5/+CRxD/gUcQ/4FGD/+BRQ//gEUO/39EDf9/Qwv/fkIK
/31BCf+0knL//////////////////////9/RxP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6Af9qMDH/SBWs/zgJ4/84CeP/OAnj/zgJ4/84CeP/YChU/3c6AP93
OgD/dzoA/3c6APuCSBH5g0kR/4RKEv+ZaTn/+vf1///////////////////+/v+PWST/hUwS/4VL
Ev+EShL/hEkS/4NIEf+CRxD/gUYP/4BFDv+hdUz//v79//////////////////by7/94OwH/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/czcN/1Uge/86C9r/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/ZCtF/3c6AP93OgD/dzoA/3c6APuHTRL5iE4S/4lPEv+PWB3/8+3m////////
//////////////+ofU//ilES/4pREv+JUBL/iE8S/4dOEv+GTBL/hUsS/4RJEv+TYDH/+PTx////
//////////////////+KVCP/eDsB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3Y6A/9hKU//QA/I
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/8+Ds7/cTYV/3c6AP93OgD/dzoA/3c6APuLUhP5
jFMT/41UE/+OVRT/5djJ//////////////////////+/nnn/jlYV/45VFP+NVRT/jVQT/4xTE/+K
URL/iVAS/4hOEv+JURj/8Ojh//////////////////////+jeFH/ez4F/3k8A/93OgH/dzoA/3c6
AP93OgH/bTIl/0wZnP85CuD/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zwM1P9lLEL/dzoA
/3c6AP93OgD/dzoA/3c6APuPVhX5kFgW/5FYF/+SWRf/07yh///////////////////////UvaP/
k1oW/5JaF/+RWRf/kVgW/5BXFv+OVhX/jVUU/4xTE/+KURL/38++//////////////////////+7
m37/fUIK/3w/B/96PQT/eDsB/3Y5Bv9ZI2//PA3T/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ
4/84CeL/TBmd/2wyKP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APuTWxb5lFwU/5VeE/+WXxL/wqFz
///////////////////////n2sj/mWIU/5dfEf+WXhL/lV0U/5RcFf+TWhb/kVkX/5BXFv+OVhT/
y7GU///////////////////////Tv6z/gEUO/35DC/98QAj/aDBB/0MSvf84CeP/OAnj/zgJ4/84
CeP/OAnj/zgJ4/84CeP/OAnj/0IRwP9lLEL/dzoB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
APuXYBD5mWIO/5pjDf+bZAv/s4lF///////////////////////07uT/o3Ab/5tkC/+aYwz/mWIN
/5hhD/+XXxH/lV0T/5RbFf+SWRf/uZZt///////////////////////m29D/hk0Y/4FGD/9iLGL/
Owza/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/88DNX/VyF1/3M3EP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6APucZQr5nWcI/55oB/+faQf/qnoh//v59f//////////////
///+/fz/roAr/59pBv+faAf/nmcI/5xmCf+bZAv/mWIN/5hgEP+WXhP/qXxD////////////////
///////18Oz/kFsm/3g/M/87C9z/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAni/0kWp/9sMij/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APugagb5oWwG/6NtBv+k
bgf/qXYS//Ho2P//////////////////////wZ1W/6RvB/+jbgb/omwG/6FrBv+faQf/nWcI/5xl
Cv+aYw3/oW4l//fz7v////////////////////7/n3A+/1snif84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/9BEMT/YSpP/3c6Av93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6APukbwf5pnEJ/6hzCv+pdAz/qnUO/+bXuf//////////////////////07iE/6l1DP+o
cwv/p3IJ/6VwCP+jbgb/omwG/6BpBv+dZwj/nmkT/+zi0v//////////////////////tI9n/08e
qf84CeP/OAnj/zgJ4/84CeP/Owvb/1Ugg/9yNxP/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APupdAz5q3YO/6x4EP+ueRL/r3sT/9rCkv//////
////////////////5NKw/656E/+teRH/rHcP/6p1Df+ocwr/pnEI/6NuBv+hawb/oWwN/+vgzf//
////////////////////w6WC/1Qiof84CeP/OAnj/zgJ4/84CeP/Uh+R/3s/B/95PAP/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APuueRL5sHsU
/7F9Fv+zfxj/tIAZ/8+ta///////////////////////8+vc/7mJK/+4iCn/toYn/7WEJf+zgiL/
sYAg/659Hf+tfSD/x6Zp//38+v//////////////////////w6SB/2g0dP84CeP/OAnj/zgJ4/84
CeP/RBO+/3M5Kf92Oxb/czgS/3I3Ef9yNxH/cjcR/3I3Ef9yNxH/cjcR/3I3Ef9yNxH/cjcR/3Q4
DP93OgD/dzoA/3c6APuyfhf5tIAZ/7WCGv+3hBv/uIUb/8acRP///////////////////////v79
//n07P/49Ov/+PTr//j06//49Ov/+PPq//jz6v/49Oz//v38////////////////////////////
s4ta/4dQMP87DN3/OAnj/zgJ4/84CeP/OAnj/zwM1v8+DtD/Pg7P/z4Nz/8+Dc//Pg3P/z4Nz/8+
Dc//Pg3P/z4Nz/8+Dc//Pg3P/0gWqv93OgD/dzoA/3c6APu2ghr5uIUb/7qHG/+8iRv/vYob/8CP
Iv/+/fr/////////////////////////////////////////////////////////////////////
///////////////////////y6uD/nWki/5NbFv9hLob/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zoL3P92OQT/dzoA/3c6APu6
hxv5vYob/7+MHP/Bjh3/wpAd/8ORHv/17Nj/////////////////////////////////////////
//////////////////////////////////////////////79+/+8l1r/mWEO/5VdE/+PVx7/VSSf
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/
OAnj/zgJ4/9tMib/dzoA/3c6APu+ixz5wY4c/8ORHv/Fkx//xpUf/8iWH//s27D/////////////
////////////////////////////////////////////////////////////////////+PTt/8en
bP+eaAj/m2QM/5dgEP+TWxX/jFQg/2Atg/88DNr/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/9iKk7/dzoA/3c6APrBjx35xJIf/8eVH//Jlx3/
y5ob/8ybGf/ixXj/+PHc//jx3P/48Nz/+PDc//fw3P/38N3/9+/d//bv3f/27t3/9e7d//Xt3f/0
7N3/8+ra/+vdxP/Vu4f/tIcv/6NuBv+gagb/nGYJ/5liDv+VXRP/klkX/49WFf98RTn/XiqB/0oZ
sf9CEsb/QhHH/0ERxv9BEMb/QRDF/0EQxf9BEMX/QRDF/0EQxf9BEMX/QRDF/0EQxf9cJWT/dzoA
/3c6AO7FkyD5yJYf/8uZG//NnBj/0J8W/9KhFf/TohT/06MU/9OiFP/SoRX/0aAV/86dF//Mmxn/
yZcd/8aUIP/DkR7/wI0c/7yJG/+4hRv/tYEa/7F9Fv+teRH/qXQM/6VwCP+hbAb/nmgH/5pjDP+X
XxH/k1sW/5BXFv+MUxP/iU8S/4VLEv+CRxD/f0MM/3xACP95PAP/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AMjHlR/5y5kb/86dF//RoBX/1KMU/9amFP/YpxT/2KgV
/9ioFf/XpxT/1aUU/9OiFP/Pnhb/zJsZ/8mXHf/FkyD/wpAd/76MHP+6hxv/toMb/7N/GP+vexP/
q3YO/6dyCf+jbQb/n2kH/5tlCv+YYBD/lFwV/5FYF/+NVBT/iVAS/4ZMEv+CSBH/f0QN/31BCf96
PQT/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AIrKmB35zZwY/9Gg
Ff/VpBT/2KcU/9qqFv/crRn/3a4a/92uGv/brBj/2akV/9amFP/TohT/z54W/8uaGv/Hlh//xJIf
/8COHP+8iRv/uIUb/7SAGv+wfBX/rHgQ/6hzC/+kbgf/oGoG/5xmCf+YYQ//lV0U/5FZF/+OVRT/
ilES/4dNEv+DSBH/gEUO/31BCf96PgX/dzoB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA+Hc6ADnLmhr5z54W/9OjFP/XpxT/26sX/96vG//hsh//4rQi/+KzIf/fsB3/3K0Z/9mp
Ff/VpRT/0aAV/82cGP/Jlx3/xZMg/8GPHf+9ihz/uYYb/7WBGv+xfRb/rXkR/6l0DP+lbwf/oWsG
/51nCP+ZYg7/lV4T/5JZF/+OVhT/i1IS/4dNEv+DSRH/gEUO/31CCv97PgX/eDsB/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoAsXc6AALMmxn50J8V/9SkFP/YqRX/3K0Z/+CyH//k
tiT/57kp/+a4J//itCH/3q8c/9qrF//WphT/0qIU/86dF//KmBz/xpQg/8KQHf++ixz/uocb/7WC
Gv+xfRf/rnkS/6l1DP+lcAf/oWsG/51nCP+ZYg3/ll4S/5JaF/+OVhX/i1IS/4dOEv+DSRL/gEUO
/35CCv97PgX/eDsB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgDqdzoALAAAAADMmxn5
0J8V/9WkFP/ZqRX/3a0Z/+GyH//ltiX/57oq/+a4KP/itCL/368c/9qrF//WphT/0qIU/86dF//K
mBz/xpQg/8KQHf++ixz/uocb/7WCGv+yfhf/rnkS/6l1DP+lcAj/oWsG/51nCP+ZYg3/ll4S/5Ja
F/+PVhX/i1IS/4dOEv+DSRL/gEUO/35CCv97PgX/eDsB/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6APt3OgBSdzoAAAAAAADMmhr5z54W/9OjFP/XpxT/26sX/96vHP/hsyD/47Qi/+K0Iv/gsR7/
3a0Z/9mpFf/VpRT/0aEV/82cGP/JmB3/xZMg/8GPHf+9ixz/uYYb/7WBGv+xfRf/rXkR/6l0DP+l
bwf/oWsG/51nCP+ZYg7/lV4T/5JZF/+OVhT/i1IS/4dOEv+DSRH/gEUO/31CCv97PgX/eDsB/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA6nc6AFMAAAAAAAAAAAAAAADKmBz5zpwX/9GhFf/VpRT/2KgV
/9urF//drhr/3q8b/96uG//crRj/2aoW/9emFP/TohT/z54W/8yaGv/Ilh//xJIf/8COHP+8iRv/
uIUb/7SBGv+wfBX/rHgQ/6hzC/+kbgf/oGoG/5xmCf+ZYQ7/lV0U/5FZF/+OVRT/ilES/4dNEv+D
SBH/gEUO/31BCf96PgX/dzoB/3c6AP93OgD/dzoA/3c6APh3OgCydzoALHc6AAAAAAAAAAAAAAAA
AADIlh/1y5oa+86dFvvSoRX71aQU+9enFPvYqBX72akV+9mpFfvYqBT71qUU+9OjFPvQnxX7zZwY
+8mYHfvGlCD7wpAe+7+MHPu7iBv7t4Mb+7N/GfuvexT7q3cP+6dyCvujbQb7n2kG+5xlCvuYYRD7
lFwV+5FYF/uNVRT7ilES+4ZMEvuCSBH7gEQN+31BCft6PQT7dzoB+nc6AO53OgDIdzoAi3c6ADl3
OgACAAAAAAAAAAAAAAAAAAAAAAAAAAD4AAAAAAAAAOAAAAAAAAAA4AAAAAAAAACAAAAAAAAAAIAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAABAAAAAAAAAAcAAAAAAAAABwAA
AAAAAAAfAAAoAAAAIAAAAEAAAAABACAAAAAAAAAQAAAjLgAAIy4AAAAAAAAAAAAAAAAAAAAAAAAA
AAAAdzoAGHc6AIR3OgDQdzoA9Xc6APt3OgD7dzoA+3c6APt3OgD7dzoA+3c6APt3OgD7dzoA+3c6
APt3OgD7dzoA+3c6APt3OgD7dzoA+3c6APt3OgD7dzoA+3c6APt3OgD7dzoA+3c6APt3OgD7dzoA
+3c6APgAAAAAAAAAAHc6AFZ3OgDxdzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/AAAAAB3OgBWdzoA/Xc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD8dzoAGHc6APF3OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6APx3OgCEdzoA/3c6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/
dzoA/Hc6ANB3OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/f0IB/5BUAv+dYgH/pmsA/6xxAP+ucwD/rHEA/6ZqAP+bXwD/i04A/3k8
AP93OgD/dzoA/3c6AP93OgD8dzoA9Xc6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA
/3c6AP93OgD/dzoA/3c6AP+GSgP/omgH/7uDCv/IkQv/x48H/8aNA//FiwD/w4kA/8CGAP+/hAD/
wIQA/76DAP+9gQD/tnkA/5RXAP94OwD/dzoA/3c6APx6PQL7dzoA/3c6AP93OgD/dzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/eDsA/4tQBP+mbQn/wYoO/86XD//MlQ7/wooL/6tyB/+ZXgP/klkJ/7aJ
Nf+5jz//upFB/7eOPv+xhDP/rHkl/6VtEf+kaAH/uHsA/6JlAP93OgD/dzoA/Ho9AvudZR3/toEr
/6p0If+fZxf/mmES/5tiEf+haRH/rHUT/7yGFP/OmhX/0p4T/9CbEf+8hQ3/oWcI/4dLA/93OgD/
dzoA/3c6AP93OgD/fEAE/4RKDP+QWxj/onIp/7qPPv/XslX/37pZ/9WrSv+zgSj/pGgG/5BTAP93
OgD8dzoA+41aKf+zjmT/2rp8/+nKgv/oyX7/58d6/+bGdv/lxXL/5MNu/9m1Yf+qeCv/f0IC/3c6
AP93OgD/YypK/1Megf9THoH/Ux6B/1Megf9THoH/Ux6B/1Megf9UH3//WyVk/281JP+aZB3/z6RF
/9yzSf/SpDv/j1QK/3c6APx3OgD7o3pU////////////////////////////////////////////
///////////dzsD/jlsr/3c6AP9cJWL/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84
CeP/OAni/08bkv94PRH/xJY3/9uvQf+5hif/dzoA/Hc6APuLVyf/////////////////////////
///////////////////////////////////49vP/mGk+/2cuOv84CeP/OAnj/zgJ4/84CeP/OAnj
/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/0YUs/+HTRL/269B/7WBJf93OgD8dzoA+3g8A//6+Pb/
///////////39PD/7eXd/+3l3f/t5d3/7eXd/+/n4f/+/v7////////////z7un/fEQi/04alf9O
GpX/ThqV/04alf9OGpX/ThqV/04alf9NGpb/RRO0/zgJ4/84CeP/OAnj/2IrXv/VqD7/hksJ/3c6
APx4OwH7eTwC/+TXzP///////////9O+rP96PQT/eT0D/3k8A/94OwL/dzoB/5doPP/8+/n/////
//////+3l3n/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/QxK7/zgJ4/84CeP/
XCym/4xTDv93OgD/dzoA/H1BCft+Qgr/zrej////////////6+HZ/39DDP9/Qwv/fkIK/31BCf98
QAf/ez4F/9nIuP///////////93OwP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3U5
B/9GFLL/OAnj/zgJ4/9BEMP/dzoA/3c6AP93OgD8gkcQ+4NIEf+7mnv////////////9/fz/iVEa
/4RKEv+DSRH/gkcQ/4FGD/+ARA3/w6eN////////////9vHu/3c6Af93OgD/dzoA/3c6AP93OgD/
dzoA/3c6AP9mLT3/QRDB/zgJ4/84CeP/OAnj/z8Pyv93OgD/dzoA/3c6APyITxL7iVAS/6qAUv//
//////////////+idEH/i1IT/4pREv+JTxL/h00S/4VLEv+viWb/////////////////iFIg/3g7
Af93OgD/dzoA/3c6AP9xNRb/ThqU/zgJ4/84CeP/OAnj/zgJ4/84CeP/VB9//3c6AP93OgD/dzoA
/I5WFfuQVxb/m2gr/////////////////7qXbf+RWRb/kFgW/49WFf+NVBT/i1IT/55vPP//////
//////////+jeFD/ez8G/3g8Av92OgP/XSZg/zwM1f84CeP/OAnj/zgJ4/84CeP/Ogvb/1gicv92
OQP/dzoA/3c6AP93OgD8lV0U+5ZfEv+XYBD/9vHq////////////0biT/5hhD/+XXxH/lV0T/5Nb
Ff+RWRb/kVoa//37+v///////////7ucf/9/RA3/cjgq/0UUtf84CeP/OAnj/zgJ4/84CeP/OAnj
/0sYoP9vNB7/dzoA/3c6AP93OgD/dzoA/3c6APybZAv7nWYJ/55oB//m2L7////////////l1rz/
n2gH/51nCP+cZQr/mWIN/5dgEf+UXBT/7OLW////////////076q/35FIP8/D87/OAnj/zgJ4/84
CeP/OAnj/0APyP9jK0f/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/KFsBvukbgf/pXAI/9jB
k/////////////bx6P+mcQj/pG8H/6JsBv+gagb/nWcI/5pjDP/bx6v////////////p39T/aDNo
/zgJ4/84CeP/OAnj/zkK3v9WIHn/dDgK/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD8
qHML+6t2Dv+teBH/za1u/////////////////7SDJf+sdw//qXQM/6ZxCf+jbQb/oGoG/9K5j///
//////////v59/9mMnT/OAnj/zgJ4/84CeP/bjUz/3g7Af93OgD/dzoA/3c6AP93OgD/dzoA/3c6
AP93OgD/dzoA/3c6APyvexT7sn4X/7SAGf/FnEr/////////////////2L2I/8uoY//JpmH/x6Re
/8WhW//Hpmb/9fDm////////////+vfz/3pFSv84CeP/OAnj/zgJ4/9aJXr/ZS1M/2MqSv9jKkn/
YypJ/2MqSf9jKkn/YypJ/2YtPv93OgD/dzoA/LWCGvu4hRv/u4gb/8CPJf/+/v3/////////////
///////////////////////////////////////////////ezbX/k1sX/0sbt/84CeP/OAnj/zgJ
4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OwvZ/3c6Av93OgD8vIkb+7+NHP/CkB7/xJIf
//fv3///////////////////////////////////////////////////////+PPs/6p7Lv+WXxH/
iFAu/0UVxP84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/OAnj/zgJ4/84CeP/bjMg/3c6APzB
jx37xZMf/8iXHv/Lmhv/7Nmn//r16P/69ej/+vXo//r16P/59On/+fTo//jz6P/48+j/8+vc/97J
oP+vgCb/nmgH/5liDf+UXBX/i1Mg/2Iuev9HFrv/Pw/Q/z4O0P8+Ds//Pg7P/z4Oz/8+Ds//Pg7P
/z4Oz/9lLUD/dzoA9saUH/vLmRv/z54W/9KhFf/UpBT/1KQU/9OiFP/Qnxb/zJsZ/8eWH//DkR7/
vosc/7iFG/+yfhj/rXgR/6dyCf+hawb/m2UK/5ZeEv+RWBb/jFMT/4ZMEv+BRg//fUEJ/3k8Av93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgDRypgc+8+eFv/VpBT/2akV/9usGP/crBj/2qoW/9am
FP/RoBX/zJoa/8aUH//Bjh3/u4gb/7WBGv+vexT/qXQM/6NtBv+dZwj/l2AQ/5JaFv+NVBP/h04S
/4JHEP9+Qgr/eT0D/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AIXNmxn706IU/9ioFf/erxv/
4rQi/+O1I//fsB3/2qoW/9WkFP/OnRf/yJce/8KQHv+8ihv/toMa/7B8Ff+qdQ3/pG4H/55oCP+Y
YQ//k1oW/45VFP+ITxL/g0gR/35CC/96PQT/dzoA/3c6AP93OgD/dzoA/3c6AP93OgDxdzoAGM2c
GPvUoxT/2qoW/+CxHv/ltyb/57kp/+KzIf/brBj/1qUU/8+eFv/Jlx3/w5Ee/72KG/+3gxv/sX0W
/6p2Dv+kbwf/nmgH/5lhDv+TWxb/jlUU/4hPEv+DSBH/fkML/3o+BP93OgD/dzoA/3c6AP93OgD/
dzoA/Xc6AFcAAAAAzJsZ+9KhFf/YqBX/3a4a/+GyH//hsyD/3q8c/9mpFv/UoxT/zp0X/8iWHv/C
kB7/vIkb/7aCGv+wfBX/qnUN/6RuBv+eZwj/mGEP/5NaFv+NVRT/iE4S/4JIEf9+Qgv/ej0E/3c6
AP93OgD/dzoA/3c6APF3OgBXAAAAAAAAAADJmB34zp0X/NOjFPzXpxX82qoW/NqqFvzYqBX81aQU
/NCfFvzLmRv8xZMf/MCOHPy6hxv8tIAZ/K96E/yodAv8om0G/J1mCfyXYBD8klkW/I1UE/yHTRL8
gkcQ/H5CCvx5PQP8dzoA9nc6ANJ3OgCFdzoAGQAAAAAAAAAAAAAAAOAAAADAAAAAgAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAMAAAAHKAAA
ABAAAAAgAAAAAQAgAAAAAAAABAAAIy4AACMuAAAAAAAAAAAAAAAAAAB3OgBXdzoA1Hc6APt3OgD9
dzoA/Xc6AP13OgD9dzoA/Xc6AP13OgD9dzoA/Xc6AP13OgD9dzoA/Xc6APx3OgBYdzoA/3c6AP93
OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP93OgD+dzoA1Xc6
AP93OgD/dzoA/3c6AP93OgD/dzoA/3c6AP95PAD/h0sB/5BUAP+SVgD/jE8A/3w/AP93OgD/dzoA
/ng7Afx3OgD/dzoA/3c6AP93OgD/fEAB/5VbBv+xeAr/vIQK/651Bf++ix3/vIog/7eBFv+vdAX/
mVwA/3c6AP6HThL9u49L/8KXSv/ClkT/zKFC/8mZLf+hZwj/gEYV/2UsQf9lLEH/aTJF/3ZCUP+X
Z0f/x5xB/8GQLP+DRwL+h1If/f//////////////////////////2ce3/3Q+Nv84CeP/OAnj/zgJ
4/84CeP/OAnj/1Edjv/AkDP/l18T/ng7Av338/D/8uzm/7ORcP+zkHD/v6KH//7+/v/JsqH/YipL
/2IqS/9iKkv/YipL/04alf84CeP/iFVU/3s+Av6ARQ394tTH//r39f+DSBH/gEUN/35CCv/n29H/
9O/r/3c6AP93OgD/dzoA/3M3D/9NGpf/OAnj/1wlY/93OgD+jFMU/dG5n///////nm42/4xTFP+J
UBP/076o//////+HUR7/dzoB/2AoU/8+Dc//OAnj/0AQxf9uMyH/dzoA/plhDv3EpHD//////7uW
Wf+ZYg7/lV0S/8SlgP//////o3lV/0sZpP84CeP/Ogvc/1chc/91OAf/dzoA/3c6AP6mcQr9vpZG
///////UuYX/p3IK/6JsCP+6k1P//////6yPqv84CeP/RhS2/24zIf93OgD/dzoA/3c6AP93OgD+
tIAY/b2NKf////7/9e7h/+TTsP/i0a7/7+XS//////+5mYL/PQ3Y/0AQyf9OGpf/TRqW/00alv9P
G5H/dzoA/sCOHf3GlR3/9+/b//368//8+vT//Pn0//r38f/hz6z/nmkV/3tFSv9GFr//OwzZ/zsL
2f87C9n/OwvZ/3A1GPzLmRv91KMV/9ioFv/VpBX/zJsa/8KPHf+2gxr/q3YO/59pCP+UXBT/iVAS
/39EDf94OwH/dzoA/3c6AP93OgDV0J8W/dysGf/ktiX/3q4b/9KhFf/GlB7/uoYb/615Ef+hawf/
ll4S/4tSE/+ARQ7/eDwC/3c6AP93OgD/dzoAWM2cGPzYqBb+3a4b/tmpF/7Pnhf+xJIe/riFG/6s
eBD+oGoH/pVdE/6KURP+gEUN/ng7Avx3OgDVdzoAWAAAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAA"""

def _ensure_ds_ico_file() -> str:
    """
    Ensures DS.ico exists as a temporary file and returns its path.
    This avoids external file dependencies (onefile packaging friendly).
    """
    try:
        import tempfile
        p = os.path.join(tempfile.gettempdir(), "DECOSOL_DS.ico")
        if not os.path.isfile(p):
            with open(p, "wb") as f:
                f.write(base64.b64decode(DS_ICO_B64))
        return p if os.path.isfile(p) else ""
    except Exception:
        return ""

LOGO_PNG_B64 = """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"""

DISCLAIMER_TEXT = """DECOSOL programma sperimentale di calcolo decompressivo in versione provvisoria v. 1.0 beta.

DECOSOL v. 1.0 beta è basato sul codice VPMB di Erik C. Baker di cui rappresenta una traduzione oltre a una implementazione di ZH-L1B/C di A. Buhlmann realizzate da Luca Brambilla con il supporto di ChatGPT 5.2.

DECOSOL v. 1.0 beta è destinato a subacquei tecnici formati ed esperti di decompressione. Scopo di DECOSOL v. 1.0 beta è quello di permettere approfondimenti sul piano teorico delle dinamiche decompressive in immersione subacquea.

DECOSOL v. 1.0 beta non attiva controlli sui livelli delle pressioni parziali dei gas respirati e non ne limita quindi l’impiego anche in condizioni potenzialmente pericolose o addirittura letali come quelle legate a eccessi o carenze di ppO2, basandosi sia sul presupposto della adeguata preparazione sulla decompressione da parte dell’utente che sullo scopo meramente teorico e di studio di DECOSOL v. 1.0 beta.

DECOSOL v. 1.0 beta non sostituisce addestramento, procedure operative, tabelle validate o strumenti certificati.
L’uso è a esclusiva responsabilità dell’utente. Verifica sempre risultati e parametri che potrebbero contenere errori.
"""


class DisclaimerWindow(tk.Tk):
    """Schermata iniziale con richiesta di consenso (sempre)."""

    def __init__(self):
        super().__init__()

        # Set application icon (embedded DS.ico)
        try:
            _ico = _ensure_ds_ico_file()
            if _ico:
                self.iconbitmap(_ico)
        except Exception:
            pass

        self.title("DECOSOL — Avvertenza")

        # Dimensioni finestra: abbastanza grande da mostrare testo + bottoni senza tagli
        sw = int(self.winfo_screenwidth())
        sh = int(self.winfo_screenheight())
        win_w = min(980, max(820, int(sw * 0.70)))
        win_h = min(820, max(680, int(sh * 0.80)))
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(760, 640)

        # Layout
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(
            outer,
            text="DECOSOL — Programma sperimentale di calcolo decompressivo (v1.0beta)",
            font=("TkDefaultFont", 11, "bold"),
        )
        title.pack(anchor="w", pady=(0, 1))
        # Logo DECOSOL (embedded PNG, resized for compact layout)
        try:
            # Prefer high-quality resize via Pillow (PIL). Fallback to Tk PhotoImage subsample.
            import base64, io
            try:
                from PIL import Image, ImageTk  # type: ignore
                logo_bytes = base64.b64decode(LOGO_PNG_B64)
                img = Image.open(io.BytesIO(logo_bytes))
                w, h = img.size
                target_w = 760  # px, fit within consent window
                scale = min(1.0, float(target_w) / float(max(1, w)))
                # slight shrink to keep margins
                scale = min(scale, 0.90)
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
                self._logo_img = ImageTk.PhotoImage(img)
            except Exception:
                # Fallback: no Pillow available
                self._logo_img = tk.PhotoImage(data=LOGO_PNG_B64)
                try:
                    # Subsample only supports integers; 2≈50% (still better than full size)
                    self._logo_img = self._logo_img.subsample(2, 2)
                except Exception:
                    pass

            ttk.Label(outer, image=self._logo_img).pack(anchor="center", pady=(0, 1))
        except Exception:
            self._logo_img = None

        text_frame = ttk.Frame(outer)
        text_frame.pack(fill="both", expand=True, pady=(0, 1))

        txt = tk.Text(text_frame, wrap="word", height=18)
        vscroll = ttk.Scrollbar(text_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vscroll.set)

        txt.insert("1.0", DISCLAIMER_TEXT)
        txt.config(state="disabled")

        txt.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        self.accept_var = tk.BooleanVar(value=False)

        chk = ttk.Checkbutton(
            outer,
            text="Ho letto e accetto i termini d’uso e la limitazione di responsabilità.",
            variable=self.accept_var,
            command=self._on_toggle,
        )
        chk.pack(anchor="w", pady=(2, 2))

        btns = ttk.Frame(outer)
        btns.pack(anchor="e", pady=(2, 0))

        self.btn_continue = ttk.Button(btns, text="Continua", command=self._continue, state="disabled")
        self.btn_continue.pack(side="right", padx=(8, 0))

        ttk.Button(btns, text="Esci", command=self.destroy).pack(side="right")

        # UX: Enter attiva "Continua" solo se abilitato
        self.bind("<Return>", lambda _e: self._continue() if self.accept_var.get() else None)

    def _on_toggle(self):
        self.btn_continue.config(state=("normal" if self.accept_var.get() else "disabled"))

    def _continue(self):
        if not self.accept_var.get():
            return
        self.destroy()
        app = VPMBApp()
        app.mainloop()

if __name__ == "__main__":
    DisclaimerWindow().mainloop()