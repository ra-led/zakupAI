"""
Merge pp1875.json + pp719_thresholds.json + pp_registries_coverage.json
into a single flat ОКПД2 → requirements справочник used by the checker.

Usage:
    python scripts/build_pp_requirements.py \
        --pp1875 app/data/pp1875.json \
        --pp719  app/data/pp719_thresholds.json \
        --reg    app/data/pp_registries_coverage.json \
        --out    app/data/pp_requirements.json

For every ОКПД2 (union of all sources) we produce a self-contained entry:

{
  "name": "...",
  "pp1875": {                         # if this code (or a parent) is in ПП 1875
    "appendix": 1|2|3,
    "regime": "ban|restriction|minimum_share",
    "position": 70,
    "min_share_percent": 90,          # only for minimum_share
    "inherited_from": "28.92.22"      # if match is via prefix-lookup, not direct
  },
  "pp719": {                          # if this code has a score threshold
    "section": "III. Продукция ...",
    "current_threshold": 90,
    "effective_from": "2026-01-01",
    "upcoming": [{"from": "2028-01-01", "min_score": 110}],
    "inherited_from": "28.92.22"      # if inherited via prefix
  },
  "rep_level": {                      # if the код shows up in РЭП (ПП 878 режим)
    "applicable": true,
    "observed_levels": {"Уровень 1": 8, "Уровень 2": 83}
  },
  "registry": {                       # live stats from РРПП/РЭП registers
    "pp719_records": 761,
    "pp878_records": 0,
    "pp719_score_median": 188,
    "departments": [...]
  }
}

Codes with *none* of these populated mean: not regulated by any of the three
acts and no registry footprint. They're likely OKPD-only artefacts of the
union — we keep them anyway so checker lookups never miss.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ОКПД2 класс-уровни (26.xx = computers, 27.11.xx/27.12.xx = electrical,
# 30.30.xx = aviation electronics etc). We don't use this as source-of-truth —
# rep_level.applicable is derived empirically from registry level histogram.
# Kept here only for documentation.
_ELECTRONICS_HINT_PREFIXES = ("26.1", "26.2", "26.3", "26.4", "26.5", "27.11", "27.12")


def _prefix_lookup(code: str, source: dict) -> Optional[str]:
    """Find the longest parent of `code` that exists as a key in `source`.

    >>> _prefix_lookup("28.92.22.110", {"28.92.22": ..., "28.92": ...})
    "28.92.22"

    Returns None if no direct or parent match exists.
    """
    if code in source:
        return code
    parts = code.split(".")
    while len(parts) > 1:
        parts.pop()
        cand = ".".join(parts)
        if cand in source:
            return cand
    return None


def _resolve_pp1875(code: str, pp1875_codes: dict) -> Optional[dict]:
    """Return the most restrictive ПП 1875 entry that applies to `code`.

    Priority between appendices (strictest → softest):
        1 (ban) > 2 (restriction) > 3 (minimum_share)

    We check direct match first, then parents.
    """
    match_code = _prefix_lookup(code, pp1875_codes)
    if match_code is None:
        return None
    entries = pp1875_codes[match_code]["entries"]
    if not entries:
        return None
    # Sort by appendix ascending — 1 wins, 3 loses
    entries_sorted = sorted(entries, key=lambda e: e["appendix"])
    top = entries_sorted[0]
    result = {
        "appendix": top["appendix"],
        "regime": top["regime"],
        "position": top["position"],
        "source_name": top.get("name") or pp1875_codes[match_code].get("okpd2", ""),
    }
    if "min_share_percent" in top:
        result["min_share_percent"] = top["min_share_percent"]
    if match_code != code:
        result["inherited_from"] = match_code
    # If the same code lands in several appendices (very common — ban + min_share),
    # expose the full list so UI can explain all applicable regimes.
    if len(entries_sorted) > 1:
        result["all_regimes"] = [
            {
                "appendix": e["appendix"],
                "regime": e["regime"],
                "position": e["position"],
                **({"min_share_percent": e["min_share_percent"]} if "min_share_percent" in e else {}),
            }
            for e in entries_sorted
        ]
    return result


def _resolve_pp719(code: str, pp719_codes: dict) -> Optional[dict]:
    """Return the ПП 719 threshold entry applicable to `code`.

    We only propagate thresholds that are actually set (current is not None).
    A code whose parent has "no threshold" entry contributes nothing.
    """
    # Direct match first
    direct = pp719_codes.get(code)
    if direct and direct.get("current") is not None:
        out = {
            "section": direct.get("section", ""),
            "current_threshold": direct["current"]["min_score"],
            "effective_from": direct["current"]["effective_from"],
            "upcoming": list(direct.get("upcoming", [])),
        }
        return out

    # Parent lookup — but only for codes where the parent has a threshold
    parts = code.split(".")
    while len(parts) > 1:
        parts.pop()
        cand = ".".join(parts)
        parent = pp719_codes.get(cand)
        if parent and parent.get("current") is not None:
            return {
                "section": parent.get("section", ""),
                "current_threshold": parent["current"]["min_score"],
                "effective_from": parent["current"]["effective_from"],
                "upcoming": list(parent.get("upcoming", [])),
                "inherited_from": cand,
            }
    return None


def _derive_rep_level_block(registry_entry: dict) -> Optional[dict]:
    """Return rep_level info iff the код has REAL (non-empty) level observations."""
    dist = registry_entry.get("pp878_level_distribution", {}) if registry_entry else {}
    real_levels = {lvl: n for lvl, n in dist.items() if lvl.strip() != "Нет уровня"}
    if not real_levels:
        return None
    return {
        "applicable": True,
        "observed_levels": real_levels,
    }


def _condense_registry(entry: dict) -> dict:
    """Keep only the fields that are useful at checker time."""
    out = {
        "pp719_records": entry.get("pp719_records", 0),
        "pp878_records": entry.get("pp878_records", 0),
    }
    stats = entry.get("pp719_score_stats")
    if stats and stats.get("count", 0) > 0:
        out["pp719_score_median"] = stats["median"]
        out["pp719_score_min"] = stats["min"]
        out["pp719_score_max"] = stats["max"]
    depts = entry.get("departments") or []
    if depts:
        out["departments"] = depts
    return out


def _pick_name(code: str, pp1875_codes: dict, pp719_codes: dict) -> str:
    """Prefer the most specific name: direct ПП 1875 → direct ПП 719 →
    parent names from either source."""
    # direct 1875
    if code in pp1875_codes:
        entries = pp1875_codes[code]["entries"]
        if entries:
            name = entries[0].get("name", "").strip()
            if name:
                return name
    # direct 719
    if code in pp719_codes:
        n = pp719_codes[code].get("name", "").strip()
        if n:
            return n
    # parent 1875
    parts = code.split(".")
    while len(parts) > 1:
        parts.pop()
        cand = ".".join(parts)
        if cand in pp1875_codes:
            entries = pp1875_codes[cand]["entries"]
            if entries and entries[0].get("name"):
                return entries[0]["name"].strip()
        if cand in pp719_codes and pp719_codes[cand].get("name"):
            return pp719_codes[cand]["name"].strip()
    return ""


def build(pp1875_data: dict, pp719_data: dict, registry_data: dict) -> dict:
    pp1875_codes = pp1875_data["okpd2"]
    pp719_codes = pp719_data["okpd2"]
    registry_codes = registry_data["okpd2"]

    all_codes = set(pp1875_codes) | set(pp719_codes) | set(registry_codes)

    merged: dict[str, dict] = {}
    for code in sorted(all_codes):
        entry: dict = {"name": _pick_name(code, pp1875_codes, pp719_codes)}

        pp1875 = _resolve_pp1875(code, pp1875_codes)
        if pp1875:
            entry["pp1875"] = pp1875

        pp719 = _resolve_pp719(code, pp719_codes)
        if pp719:
            entry["pp719"] = pp719

        reg = registry_codes.get(code)
        if reg:
            entry["registry"] = _condense_registry(reg)
            rep_block = _derive_rep_level_block(reg)
            if rep_block:
                entry["rep_level"] = rep_block

        merged[code] = entry
    return merged


def summarize(merged: dict) -> dict:
    n_total = len(merged)
    n_1875 = sum(1 for e in merged.values() if "pp1875" in e)
    n_1875_direct = sum(1 for e in merged.values() if "pp1875" in e and "inherited_from" not in e["pp1875"])
    n_719 = sum(1 for e in merged.values() if "pp719" in e)
    n_719_direct = sum(1 for e in merged.values() if "pp719" in e and "inherited_from" not in e["pp719"])
    n_rep = sum(1 for e in merged.values() if "rep_level" in e)
    n_reg = sum(1 for e in merged.values() if "registry" in e)
    n_out_of_scope = sum(1 for e in merged.values() if "pp1875" not in e)

    regime_counter: dict[str, int] = {}
    for e in merged.values():
        regime = e.get("pp1875", {}).get("regime")
        if regime:
            regime_counter[regime] = regime_counter.get(regime, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_okpd2": n_total,
        "covered_by_pp1875": n_1875,
        "  direct_pp1875_match": n_1875_direct,
        "  inherited_pp1875_match": n_1875 - n_1875_direct,
        "out_of_scope_pp1875": n_out_of_scope,
        "with_pp719_threshold": n_719,
        "  direct_pp719_match": n_719_direct,
        "  inherited_pp719_match": n_719 - n_719_direct,
        "with_rep_level": n_rep,
        "with_registry_footprint": n_reg,
        "pp1875_regime_distribution": regime_counter,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pp1875", type=Path, required=True)
    p.add_argument("--pp719", type=Path, required=True)
    p.add_argument("--reg", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    with open(args.pp1875, encoding="utf-8") as f:
        pp1875_data = json.load(f)
    with open(args.pp719, encoding="utf-8") as f:
        pp719_data = json.load(f)
    with open(args.reg, encoding="utf-8") as f:
        registry_data = json.load(f)

    merged = build(pp1875_data, pp719_data, registry_data)
    summary = summarize(merged)

    out = {
        "_summary": summary,
        "_sources": {
            "pp1875": pp1875_data.get("_summary", {}).get("source", ""),
            "pp719": pp719_data.get("_summary", {}).get("source", ""),
            "registry": {
                "pp719": registry_data.get("_summary", {}).get("pp719", {}).get("file", ""),
                "pp878": registry_data.get("_summary", {}).get("pp878", {}).get("file", ""),
            },
        },
        "_appendix_4_medical_devices": pp1875_data.get("appendix_4_medical_devices", []),
        "okpd2": merged,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("=== merge summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"  written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
