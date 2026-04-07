"""Check localization scores against PP 1875 minimum requirements."""
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

_scores_cache: dict | None = None


def _load_scores() -> dict:
    global _scores_cache
    if _scores_cache is None:
        data_path = Path(__file__).parent.parent / "data" / "pp1875_scores.json"
        if data_path.exists():
            with open(data_path, encoding="utf-8") as f:
                _scores_cache = json.load(f)
        else:
            _scores_cache = {}
    return _scores_cache


@dataclass
class LocalizationResult:
    status: str  # ok | insufficient | okpd_not_found | score_missing
    actual_score: Optional[float] = None
    required_score: Optional[float] = None
    okpd2_code: Optional[str] = None


def check_localization(okpd2_code: Optional[str], actual_score: Optional[float]) -> LocalizationResult:
    """
    Check if localization score meets PP 1875 minimum requirements for given OKPD2 code.
    """
    if not okpd2_code:
        return LocalizationResult(status="okpd_not_found", okpd2_code=okpd2_code)

    scores = _load_scores()
    code = okpd2_code.strip()

    # Try exact match, then progressively shorter codes (hierarchical)
    required = None
    for length in (len(code), 9, 7, 5, 2):
        key = code[:length]
        if key in scores:
            required = scores[key]
            break

    if required is None:
        return LocalizationResult(status="okpd_not_found", okpd2_code=code)

    if actual_score is None:
        return LocalizationResult(
            status="score_missing",
            actual_score=None,
            required_score=required,
            okpd2_code=code,
        )

    if actual_score >= required:
        return LocalizationResult(
            status="ok",
            actual_score=actual_score,
            required_score=required,
            okpd2_code=code,
        )
    else:
        return LocalizationResult(
            status="insufficient",
            actual_score=actual_score,
            required_score=required,
            okpd2_code=code,
        )
