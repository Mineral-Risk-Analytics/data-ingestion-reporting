"""Batch-apply 2026-06-11 residual confidence recalibration.

Edits the confidence value (4th tuple element) for specific (hs_code, material)
pairs in _MAPPINGS, plus deletes / reassigns two material-misassignment rows.

Approach: parse seed_hs_mappings.py line-by-line, find tuples matching
(hs_code, material), and rewrite the line in place.  Works because every
multi-line tuple in the seed file has the pattern:

    ("{hs}", "{material}",
     "{description}",
     {confidence}, "{stage}", {digit_count}, "{scope}"),

We match the first line on (hs, material) and edit the 4th line (where the
confidence value lives).
"""

import re
from pathlib import Path

SEED_PATH = Path(
    "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine/"
    "app/services/ingestion/seed_hs_mappings.py"
)

# ─── Type B: multi-material codes → 1/N defaults with overrides ──────────
# Format: (hs_code, material, new_conf)
#
# 283329 ("other sulphates") gets a market-share-weighted split rather than
# flat 0.25/0.25/0.25/0.25 because Iron sulphate (LFP cathode demand) is
# the dominant chemistry globally:
#   Iron Ore (LFP)  0.40  (LFP cathode + water treatment + fertilizer)
#   Cobalt          0.25  (NMC precursor)
#   Manganese       0.20  (NMC + LMO precursor)
#   Zinc            0.10  (industrial — not battery)
# Other codes use the 1/N default — partner can refine specific ones later.

TYPE_B_CALIBRATIONS = [
    # 2617 (Antimony + REE ores n.e.c. — 2 materials)
    ("2617", "Antimony",                   0.50),
    ("2617", "Rare Earth Elements",        0.50),
    # 283329 ("other sulphates" — Fe/Co/Mn/Zn, market-share-weighted)
    ("283329", "Iron Ore",     0.40),
    ("283329", "Cobalt",                   0.25),
    ("283329", "Manganese",                0.20),
    ("283329", "Zinc",                     0.10),
    # 283699 ("other carbonates" — Co + Zr, 2 materials)
    ("283699", "Cobalt",                   0.50),
    ("283699", "Zirconium",                0.50),
    # 253090 ("other mineral substances nes" — Fluorspar + Lithium, 2 mats)
    ("253090", "Fluorspar",                0.50),
    ("253090", "Lithium",                  0.50),
    # 2811 (4-digit "other inorganic acids/oxygen compounds" — Fluorspar + Se)
    ("2811", "Fluorspar",                  0.50),
    ("2811", "Selenium",                   0.50),
    # 282739 ("other chlorides" — Ge + Li, 2 materials)
    ("282739", "Germanium",                0.50),
    ("282739", "Lithium",                  0.50),
    # 811299 ("other rare-metal articles" — Ge + Nb + V, 3 materials)
    ("811299", "Germanium",                0.33),
    ("811299", "Niobium",                  0.33),
    ("811299", "Vanadium",                 0.33),
    # 2530 (4-digit "other mineral substances" — Lithium only flagged, but
    # treated as 2-material shared bucket per audit; default 0.5)
    ("2530", "Lithium",                    0.50),
    # 2825 (4-digit "other inorganic bases" — Lithium + ? — 2 mats)
    ("2825", "Lithium",                    0.50),
    # 282590 ("other metal oxides nes" — Nb + Ta + W, 3 materials)
    ("282590", "Niobium",                  0.33),
    ("282590", "Tantalum",                 0.33),
    ("282590", "Tungsten",                 0.33),
    # 720299 ("other ferro-alloys" — REE + Zr, 2 materials)
    ("720299", "Rare Earth Elements",      0.50),
    ("720299", "Zirconium",                0.50),
    # 2804 (4-digit "Hydrogen/rare gases/other nonmetals" — Se + Si + Te, 3)
    ("2804", "Selenium",                   0.33),
    ("2804", "Silicon (Anode Grade)",      0.33),
    ("2804", "Tellurium",                  0.33),
    # 262099 ("other metal slag/ash residues" — Ti + V, 2 materials)
    ("262099", "Titanium",                 0.50),
    ("262099", "Vanadium",                 0.50),
]

# ─── Type C: 9 flat 0.3 single-material residuals (chemical "other" basket)
TYPE_C_CALIBRATIONS = [
    ("291529", "Cobalt",                   0.30),
    ("285390", "Gallium",                  0.30),
    ("284290", "Iron Ore",     0.30),
    ("261790", "Rare Earth Elements",      0.30),
    ("850519", "Rare Earth Elements",      0.30),
    ("284190", "Rhenium",                  0.30),
    ("281129", "Selenium",                 0.30),
    ("282690", "Tantalum",                 0.30),
    ("284990", "Tungsten",                 0.50),   # WC likely majority of "other carbides"
]

# Single-line tuple format: ("hs", "material", "desc", conf, "stage", dig, "scope"),
SINGLE_LINE_TUPLE = re.compile(
    r'^(?P<indent>\s*)\(\s*"(?P<hs>\d+)"\s*,\s*"(?P<mat>[^"]+)"\s*,\s*'
    r'"(?P<desc>[^"]+)"\s*,\s*(?P<conf>[\d.]+)\s*,\s*"(?P<stage>[^"]+)"\s*,\s*'
    r'(?P<digit>\d+)\s*,\s*"(?P<scope>[^"]+)"\s*\),\s*$'
)
# Two-line tuple format: opener line followed by "desc", conf, "stage", dig, "scope"),
TWO_LINE_OPENER = re.compile(
    r'^(?P<indent>\s*)\(\s*"(?P<hs>\d+)"\s*,\s*"(?P<mat>[^"]+)"\s*,\s*$'
)
TWO_LINE_DESC_CONF = re.compile(
    r'^(?P<indent>\s+)"(?P<desc>[^"]+)"\s*,\s*(?P<conf>[\d.]+)\s*,\s*"(?P<stage>[^"]+)"\s*,\s*'
    r'(?P<digit>\d+)\s*,\s*"(?P<scope>[^"]+)"\s*\),\s*$'
)

# ─── Type C wrong-material rows: delete entirely
ROWS_TO_DELETE = [
    ("283526", "Sodium"),  # UN text = "Phosphates; of calcium n.e.c."  Calcium
                            # phosphates aren't battery-relevant.  Delete.
]

# ─── Type C wrong-material rows: reassign to correct material
ROWS_TO_REASSIGN = [
    # 284169 ("manganates / permanganates" residual): currently tagged Sodium,
    # but the chemistry is about manganese salts.  Lithium manganate (LMO
    # cathode chemistry) reports here, so the material is Manganese.
    ("284169", "Sodium", "Manganese", 0.30,
     "Salts of oxometallic acids; manganates/permanganates — includes Li manganate (LMO cathode chemistry)"),
]


def apply_edits():
    text = SEED_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # Build target dict: (hs, material) → new_conf for Type B + Type C
    conf_targets: dict[tuple[str, str], float] = {}
    for hs, mat, conf in TYPE_B_CALIBRATIONS + TYPE_C_CALIBRATIONS:
        conf_targets[(hs, mat)] = conf

    # Build delete set + reassign dict
    delete_set: set[tuple[str, str]] = set(ROWS_TO_DELETE)
    reassign_dict: dict[tuple[str, str], tuple[str, float, str]] = {
        (hs, mat): (new_mat, new_conf, new_desc)
        for hs, mat, new_mat, new_conf, new_desc in ROWS_TO_REASSIGN
    }

    # Tuple-start regex: ("{hs}", "{material}",
    tuple_start = re.compile(
        r'^(\s*)\(\s*"(?P<hs>[\d]+)"\s*,\s*"(?P<mat>[^"]+)"\s*,\s*$'
    )

    # Confidence regex on the 4th line of a tuple:
    #     {conf}, "{stage}", {digit}, "{scope}"),
    conf_line = re.compile(
        r'^(?P<indent>\s+)(?P<conf>[\d.]+)\s*,\s*"(?P<stage>[^"]+)"\s*,\s*'
        r'(?P<digit>\d+)\s*,\s*"(?P<scope>[^"]+)"\s*\),\s*$'
    )

    out: list[str] = []
    i = 0
    changes = {"conf_updated": 0, "deleted": 0, "reassigned": 0}
    while i < len(lines):
        line = lines[i]
        m = tuple_start.match(line)
        if not m:
            out.append(line)
            i += 1
            continue

        hs = m.group("hs")
        mat = m.group("mat")
        key = (hs, mat)

        # Look ahead — find the confidence line (typically i+2 or i+3).
        # Tuples span up to ~4 lines.
        end_idx = None
        for j in range(i + 1, min(i + 6, len(lines))):
            if conf_line.match(lines[j]):
                end_idx = j
                break

        if end_idx is None:
            out.append(line)
            i += 1
            continue

        if key in delete_set:
            # Skip all lines of this tuple (i through end_idx inclusive)
            changes["deleted"] += 1
            i = end_idx + 1
            continue

        if key in reassign_dict:
            new_mat, new_conf, new_desc = reassign_dict[key]
            cm = conf_line.match(lines[end_idx])
            indent = cm.group("indent")
            stage = cm.group("stage")
            digit = cm.group("digit")
            scope = cm.group("scope")
            # Rewrite first line with new material, replace mid lines with
            # new single-line description, rewrite conf line.
            out.append(f'    ("{hs}", "{new_mat}",\n')
            out.append(f'     "{new_desc}",\n')
            out.append(f'{indent}{new_conf}, "{stage}", {digit}, "{scope}"),\n')
            changes["reassigned"] += 1
            i = end_idx + 1
            continue

        if key in conf_targets:
            new_conf = conf_targets[key]
            # Pass through lines unchanged up to conf line
            for k in range(i, end_idx):
                out.append(lines[k])
            # Rewrite conf line with new value
            cm = conf_line.match(lines[end_idx])
            indent = cm.group("indent")
            stage = cm.group("stage")
            digit = cm.group("digit")
            scope = cm.group("scope")
            out.append(f'{indent}{new_conf}, "{stage}", {digit}, "{scope}"),\n')
            changes["conf_updated"] += 1
            i = end_idx + 1
            continue

        out.append(line)
        i += 1

    SEED_PATH.write_text("".join(out), encoding="utf-8")
    return changes


if __name__ == "__main__":
    changes = apply_edits()
    print(f"Confidence updates: {changes['conf_updated']}")
    print(f"Rows deleted     : {changes['deleted']}")
    print(f"Rows reassigned  : {changes['reassigned']}")
    total_expected = (
        len(TYPE_B_CALIBRATIONS) + len(TYPE_C_CALIBRATIONS)
    )
    print(f"Expected updates : {total_expected}")
    print(f"Expected deletes : {len(ROWS_TO_DELETE)}")
    print(f"Expected reassign: {len(ROWS_TO_REASSIGN)}")
