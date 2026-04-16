"""Check a product's compliance with the Russian national regime (ПП 1875 / 719 / 878).

This module consumes the merged справочник produced by
``scripts/build_pp_requirements.py`` (``app/data/pp_requirements.json``) and
returns a structured verdict.

Status values (str, stored in DB's RegimeCheckItem.localization_status):

    ok                     — товар соответствует применимым требованиям
    insufficient           — балл локализации ниже установленного порога ПП 719
    score_missing          — для ОКПД2 есть порог, но фактический балл неизвестен
    out_of_scope           — ОКПД2 не попадает под нацрежим (закупать свободно)
    okpd_not_found         — ОКПД2 не указан во входных данных вовсе
    advisory_min_share     — код под мин. долей (ПП 1875 прил. 3) — информативно

The checker does NOT verify «has the product been registered» — that's the
registry check; the localization layer only judges баллы/уровень.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REQUIREMENTS_CACHE: Optional[dict] = None


def _load_requirements() -> dict:
    global _REQUIREMENTS_CACHE
    if _REQUIREMENTS_CACHE is None:
        path = Path(__file__).parent.parent / "data" / "pp_requirements.json"
        if not path.exists():
            logger.warning("pp_requirements.json missing at %s — checker will treat every code as out_of_scope", path)
            _REQUIREMENTS_CACHE = {"okpd2": {}}
        else:
            with open(path, encoding="utf-8") as f:
                _REQUIREMENTS_CACHE = json.load(f)
    return _REQUIREMENTS_CACHE


def reload_requirements() -> None:
    """Force-reload the справочник from disk (tests / hot-reload)."""
    global _REQUIREMENTS_CACHE
    _REQUIREMENTS_CACHE = None


def should_check_rep_level(okpd2_code: Optional[str]) -> bool:
    """True if the dictionary marks this ОКПД2 as needing a РЭП level lookup.

    Used by the orchestrator (check_runner) to decide whether to spend an
    extra HTTP call on gisp-scraper /rep/. Walks parents like the main
    checker does, since rep_level lives at the same coverage level as pp1875.
    """
    if not okpd2_code:
        return False
    requirements = _load_requirements().get("okpd2", {})
    code = okpd2_code.strip()
    entry = requirements.get(code)
    if entry is None:
        parts = code.split(".")
        while len(parts) > 1:
            parts.pop()
            cand = ".".join(parts)
            if cand in requirements:
                entry = requirements[cand]
                break
    if entry is None:
        return False
    rep = entry.get("rep_level")
    return bool(rep and rep.get("applicable"))


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass
class LocalizationResult:
    """Outcome of a localization/ нацрежим check on a single item.

    The first three fields are the compact form stored in the DB.
    ``details`` holds the full per-check breakdown — checker→UI can use it to
    render cascade info («ПП 1875 приложение 1 — запрет, ПП 719 — порог 90,
    актуально 75 → insufficient»).
    """

    status: str
    actual_score: Optional[float] = None
    required_score: Optional[float] = None
    okpd2_code: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REGIME_LABELS = {
    "ban": "Запрет иностранных (ПП 1875, прил. 1)",
    "restriction": "Ограничение допуска («третий лишний», ПП 1875, прил. 2)",
    "minimum_share": "Минимальная доля российских товаров (ПП 1875, прил. 3)",
}


def _effective_threshold(pp719: dict, as_of: date) -> tuple[Optional[int], Optional[str]]:
    """Return (min_score, effective_from_iso) of the threshold applicable on
    ``as_of``.  Walks the schedule: current ≤ upcoming points whose date ≤ as_of.
    """
    cur = pp719.get("current_threshold")
    eff_from = pp719.get("effective_from")
    if cur is None:
        return None, None

    best_score, best_date = cur, eff_from
    for point in pp719.get("upcoming", []):
        try:
            pt_date = date.fromisoformat(point["from"])
        except (KeyError, ValueError):
            continue
        if pt_date <= as_of:
            # upcoming point has already kicked in — use it as current
            if pt_date >= date.fromisoformat(best_date or "1900-01-01"):
                best_score = point["min_score"]
                best_date = point["from"]
    return best_score, best_date


def _upcoming_warning(pp719: dict, as_of: date) -> Optional[dict]:
    """Earliest future threshold that would tighten the current requirement."""
    cur_score, _ = _effective_threshold(pp719, as_of)
    nearest: Optional[dict] = None
    for point in pp719.get("upcoming", []):
        try:
            pt_date = date.fromisoformat(point["from"])
        except (KeyError, ValueError):
            continue
        if pt_date <= as_of:
            continue
        if cur_score is not None and point["min_score"] <= cur_score:
            continue
        if nearest is None or pt_date < date.fromisoformat(nearest["from"]):
            nearest = point
    return nearest


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def check_localization(
    okpd2_code: Optional[str],
    actual_score: Optional[float],
    *,
    as_of: Optional[date] = None,
    rep_level_observed: Optional[str] = None,
) -> LocalizationResult:
    """Decide compliance of a single product.

    Parameters
    ----------
    okpd2_code
        ОКПД2 of the supplier's item (or a code resolved from the registry).
    actual_score
        Совокупный балл локализации as shown in РРПП. None if unknown.
    as_of
        Date of the procurement (defaults to today). Affects which point of
        the threshold schedule is picked.
    rep_level_observed
        «Уровень N» as fetched from the РЭП registry for THIS specific
        product. If supplied AND the dictionary marks the ОКПД2 as
        rep_level.applicable, we attach this fact to ``details.rep_level``
        for UI display. Pass/fail by level is procurement-specific and is
        NOT decided here — checker stays neutral on it.
    """
    if as_of is None:
        as_of = date.today()
    if not okpd2_code:
        return LocalizationResult(status="okpd_not_found")

    code = okpd2_code.strip()
    requirements = _load_requirements().get("okpd2", {})
    entry = requirements.get(code)

    # If the exact code is missing but a parent lives in the dictionary, fall
    # back to the parent — matches how the dictionary itself propagates
    # pp1875 / pp719 via `inherited_from`.
    used_code = code
    if entry is None:
        parts = code.split(".")
        while len(parts) > 1:
            parts.pop()
            cand = ".".join(parts)
            if cand in requirements:
                entry = requirements[cand]
                used_code = cand
                break

    if entry is None:
        return LocalizationResult(
            status="out_of_scope",
            okpd2_code=code,
            details={"reason": "ОКПД2 не найден в справочнике нацрежима"},
        )

    pp1875 = entry.get("pp1875")
    pp719 = entry.get("pp719")
    rep_level = entry.get("rep_level")

    details: dict[str, Any] = {
        "okpd2_matched": used_code,
        "name": entry.get("name"),
        "pp1875": None,
        "pp719": None,
        "rep_level": None,
        "upcoming_warning": None,
    }

    # --- No nacrezhim applicability at all ------------------------------------
    if pp1875 is None:
        return LocalizationResult(
            status="out_of_scope",
            okpd2_code=code,
            details={**details, "reason": "ОКПД2 вне действия ПП 1875"},
        )

    details["pp1875"] = {
        "appendix": pp1875["appendix"],
        "regime": pp1875["regime"],
        "regime_label": _REGIME_LABELS.get(pp1875["regime"], pp1875["regime"]),
        "position": pp1875.get("position"),
        "inherited_from": pp1875.get("inherited_from"),
        "min_share_percent": pp1875.get("min_share_percent"),
        "all_regimes": pp1875.get("all_regimes"),
    }

    if rep_level and rep_level.get("applicable"):
        details["rep_level"] = {
            "applicable": True,
            "observed_levels": rep_level.get("observed_levels", {}),
            "this_product_level": rep_level_observed,
        }
    elif rep_level_observed:
        # Even if the dictionary doesn't flag this ОКПД2 as electronics-with-
        # levels, surface the observed level when callers bothered to fetch it.
        details["rep_level"] = {
            "applicable": False,
            "this_product_level": rep_level_observed,
        }

    # --- Threshold evaluation --------------------------------------------------
    if pp719:
        threshold, eff_from = _effective_threshold(pp719, as_of)
        upcoming = _upcoming_warning(pp719, as_of)
        details["pp719"] = {
            "section": pp719.get("section"),
            "threshold": threshold,
            "effective_from": eff_from,
            "inherited_from": pp719.get("inherited_from"),
        }
        if upcoming:
            details["upcoming_warning"] = {
                "from": upcoming["from"],
                "min_score": upcoming["min_score"],
            }

        if threshold is None:
            # Entry says «есть требование», но schedule пуст (редкий случай,
            # например всё ужесточение ещё в будущем). Treat as no-threshold —
            # registration в реестре достаточно.
            return LocalizationResult(
                status="ok",
                okpd2_code=code,
                actual_score=actual_score,
                details={**details, "reason": "Порог ещё не вступил в силу"},
            )

        if actual_score is None:
            return LocalizationResult(
                status="score_missing",
                okpd2_code=code,
                actual_score=None,
                required_score=threshold,
                details={**details, "reason": "Не удалось получить фактический балл из реестра"},
            )

        if actual_score >= threshold:
            return LocalizationResult(
                status="ok",
                okpd2_code=code,
                actual_score=actual_score,
                required_score=threshold,
                details=details,
            )

        return LocalizationResult(
            status="insufficient",
            okpd2_code=code,
            actual_score=actual_score,
            required_score=threshold,
            details=details,
        )

    # --- No balltable threshold (чек-листное требование) ----------------------
    # ПП 719 проверяется Минпромторгом в момент включения в РРПП; раз товар
    # в реестре — чек-лист пройден. Специальная семантика по режимам:

    if pp1875["regime"] == "minimum_share":
        # Не влияет на допуск конкретного поставщика, лишь на статистику закупок
        return LocalizationResult(
            status="advisory_min_share",
            okpd2_code=code,
            actual_score=actual_score,
            details={
                **details,
                "reason": (
                    f"Мин. доля российских товаров {pp1875.get('min_share_percent')}% "
                    "(ПП 1875, приложение 3) — требование относится к заказчику, не к поставщику"
                ),
            },
        )

    # ban / restriction без балльного порога — «прошёл регистрацию = прошёл»
    return LocalizationResult(
        status="ok",
        okpd2_code=code,
        actual_score=actual_score,
        details={
            **details,
            "reason": "Требования к баллам не установлены; соответствие подтверждается записью в реестре",
        },
    )
