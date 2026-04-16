"""Orchestrates the full check pipeline for a supplier file."""
import json
import os
import asyncio
import time
import logging
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session
from sqlmodel import Session as SMSession
from ..models import RegimeCheck, RegimeCheckItem
from .file_parser import parse_supplier_file
from ..database import engine
from .gisp_checker import check_gisp_characteristics
from .localization_checker import check_localization, should_check_rep_level
from .report_generator import generate_report

logger = logging.getLogger(__name__)

GISP_SCRAPER_URL = os.getenv("GISP_SCRAPER_URL", "http://gisp-scraper:8000").rstrip("/")

# Max concurrent item checks (don't hammer APIs too hard)
MAX_CONCURRENT = 5


async def _check_registry_via_scraper(
    registry_number: str, client: httpx.AsyncClient,
) -> dict:
    """Check registry number via gisp-scraper /pp719/ endpoint.

    Returns dict with keys: status, is_actual, cert_end_date, url,
    okpd2_from_registry, localization_score.
    """
    if not registry_number or not registry_number.strip():
        return {"status": "not_found", "is_actual": None, "cert_end_date": None,
                "url": None, "okpd2_from_registry": None, "localization_score": None}

    import re
    clean = re.sub(r"[^\d]", "", registry_number)
    if not clean:
        return {"status": "not_found", "is_actual": None, "cert_end_date": None,
                "url": None, "okpd2_from_registry": None, "localization_score": None}

    url = f"{GISP_SCRAPER_URL}/pp719/{clean}"
    try:
        resp = await client.get(url, timeout=30.0)
    except httpx.RequestError as exc:
        logger.warning(f"[registry] scraper unavailable for {clean}: {exc}")
        return {"status": "registry_error", "is_actual": None, "cert_end_date": None,
                "url": None, "okpd2_from_registry": None, "localization_score": None}

    if resp.status_code != 200:
        return {"status": "not_found", "is_actual": None, "cert_end_date": None,
                "url": None, "okpd2_from_registry": None, "localization_score": None}

    data = resp.json()
    scraper_status = data.get("status", "not_found")

    if scraper_status == "not_found":
        return {"status": "not_found", "is_actual": None, "cert_end_date": None,
                "url": None, "okpd2_from_registry": None, "localization_score": None}

    record = data.get("record") or {}
    is_actual = scraper_status == "found_actual"
    cert_end = record.get("res_valid_till") or record.get("res_end_date")
    okpd2 = record.get("product_okpd2_code")
    score = None
    try:
        score = float(record.get("res_score") or 0)
    except (ValueError, TypeError):
        pass
    product_id = record.get("id") or record.get("product_id")
    reg_url = f"https://gisp.gov.ru/pp719v2/pub/prod/{product_id}/" if product_id else None

    return {
        "status": "ok" if is_actual else "not_actual",
        "is_actual": is_actual,
        "cert_end_date": cert_end,
        "url": reg_url,
        "okpd2_from_registry": okpd2,
        "localization_score": score,
    }


async def _fetch_rep_level_via_scraper(
    registry_number: str, client: httpx.AsyncClient,
) -> Optional[str]:
    """Fetch the РЭП level («Уровень N») for a registry number.

    Returns None if the number isn't found in РЭП, the scraper is unreachable,
    or the response has no level field. We never raise — REP enrichment is
    best-effort, never blocks the main check.
    """
    if not registry_number or not registry_number.strip():
        return None
    import re
    clean = re.sub(r"[^\d]", "", registry_number)
    if not clean:
        return None
    try:
        resp = await client.get(f"{GISP_SCRAPER_URL}/rep/{clean}", timeout=30.0)
    except httpx.RequestError as exc:
        logger.warning(f"[rep_level] scraper unavailable for {clean}: {exc}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if data.get("status") != "found_actual":
        return None
    record = data.get("active_record") or {}
    level = record.get("product_level")
    return str(level).strip() if level else None


# In-memory progress store: check_id -> {total, processed, status, message, timings, stages}
_progress: dict[int, dict] = {}


def _get_session() -> SMSession:
    """Create a new session from the engine."""
    return SMSession(engine)


def _make_stage(name: str, status: str = "pending", detail: str = "") -> dict:
    """Create a stage entry. status: pending | in_progress | done | skipped."""
    return {"name": name, "status": status, "detail": detail}


def _update_stage(check_id: int, index: int, status: str, detail: str = "") -> None:
    """Update a specific stage in the progress store."""
    p = _progress.get(check_id)
    if p and "stages" in p and index < len(p["stages"]):
        p["stages"][index]["status"] = status
        if detail:
            p["stages"][index]["detail"] = detail


# Stage indices (constants for readability)
STAGE_PARSE = 0
STAGE_REGISTRY = 1
STAGE_LOCALIZATION = 2
STAGE_GISP = 3
STAGE_REPORT = 4


def get_progress(check_id: int) -> dict:
    return _progress.get(check_id, {
        "total": 0, "processed": 0, "status": "pending", "message": "",
        "stages": [],
    })


async def run_check(check_id: int, db: Session) -> None:
    """Path 1: load items from the supplier file attached to the RegimeCheck.

    This is the original M4 entry point. It uses the file_parser → unified
    LLM KP extractor and then hands the items to ``_process_items_into_check``.
    """
    check = db.query(RegimeCheck).filter(RegimeCheck.id == check_id).first()
    if not check:
        return

    timings: dict[str, float] = {}
    pipeline_start = time.monotonic()

    _progress[check_id] = {
        "total": 0, "processed": 0, "status": "processing",
        "message": "Парсинг файла...",
        "stages": [
            _make_stage("Сбор позиций из КП", "in_progress"),
            _make_stage("Проверка реестра ПП №719"),
            _make_stage("Проверка баллов локализации"),
            _make_stage("Сравнение характеристик (ГИСП)"),
            _make_stage("Формирование отчёта PDF"),
        ],
    }

    try:
        t0 = time.monotonic()
        items = await parse_supplier_file(check.file_path)
        timings["1_parse_file"] = round(time.monotonic() - t0, 1)
        logger.info(
            f"[check={check_id}] Step 1 parse_file: {timings['1_parse_file']}s, "
            f"found {len(items) if items else 0} items"
        )
        if not items:
            _set_error(check, db, "Не удалось извлечь товары из файла")
            return
        _update_stage(check_id, STAGE_PARSE, "done", f"{len(items)} позиций")
        await _process_items_into_check(
            check_id, check, items, db, timings, pipeline_start
        )
    except Exception as exc:
        _record_pipeline_failure(check_id, check, db, pipeline_start, exc)
        raise


async def run_check_from_items(
    check_id: int,
    items: list[dict],
    db: Session,
) -> None:
    """Path 2: items already gathered by the caller (e.g. from BidLot rows).

    Same per-item pipeline as ``run_check``, but skips the file_parser step.
    Used by ``POST /regime/purchases/{id}/check/from-bid/{bid_id}``.
    """
    check = db.query(RegimeCheck).filter(RegimeCheck.id == check_id).first()
    if not check:
        return

    timings: dict[str, float] = {"1_parse_file": 0.0}
    pipeline_start = time.monotonic()

    _progress[check_id] = {
        "total": len(items),
        "processed": 0,
        "status": "processing",
        "message": "Проверка товаров...",
        "stages": [
            _make_stage("Сбор позиций из КП", "done", f"{len(items)} позиций"),
            _make_stage("Проверка реестра ПП №719", "in_progress"),
            _make_stage("Проверка баллов локализации"),
            _make_stage("Сравнение характеристик (ГИСП)"),
            _make_stage("Формирование отчёта PDF"),
        ],
    }

    try:
        if not items:
            _set_error(check, db, "Список товаров пуст — нечего проверять")
            return
        await _process_items_into_check(
            check_id, check, items, db, timings, pipeline_start
        )
    except Exception as exc:
        _record_pipeline_failure(check_id, check, db, pipeline_start, exc)
        raise


async def _process_items_into_check(
    check_id: int,
    check: RegimeCheck,
    items: list[dict],
    db: Session,
    timings: dict[str, float],
    pipeline_start: float,
) -> None:
    """Shared per-item pipeline (Step 2 + Step 3) used by both run_check entrypoints."""
    check.status = "processing"
    db.commit()

    p = _progress[check_id]
    p["total"] = len(items)
    p["processed"] = 0
    p["message"] = "Проверка реестра..."
    _update_stage(check_id, STAGE_REGISTRY, "in_progress", f"0 из {len(items)}")

    ok = warning = error = not_found = 0
    processed_count = 0
    registry_done = 0
    loc_done = 0
    gisp_done = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    lock = asyncio.Lock()
    n = len(items)

    async def check_single_item(pos: int, raw_item: dict, http_client: httpx.AsyncClient):
        nonlocal processed_count, registry_done, loc_done, gisp_done
        async with semaphore:
            item_start = time.monotonic()

            check_item = RegimeCheckItem(
                check_id=check_id,
                source_bid_id=raw_item.get("_bid_id"),
                source_supplier=raw_item.get("_supplier_name"),
                product_name=raw_item.get("name"),
                registry_number=raw_item.get("registry_number"),
                okpd2_code=raw_item.get("okpd2_code"),
                supplier_characteristics=json.dumps(raw_item.get("characteristics", []), ensure_ascii=False),
            )

            registry_number = raw_item.get("registry_number") or ""
            supplier_chars = raw_item.get("characteristics", [])

            # 2a. Registry check (via gisp-scraper)
            t0 = time.monotonic()
            reg_result = await _check_registry_via_scraper(registry_number, http_client)
            t_registry = round(time.monotonic() - t0, 1)

            check_item.registry_status = reg_result["status"]
            check_item.registry_actual = reg_result["is_actual"]
            check_item.registry_cert_end_date = reg_result["cert_end_date"]
            check_item.registry_raw_url = reg_result["url"]

            async with lock:
                registry_done += 1
                _update_stage(check_id, STAGE_REGISTRY, "in_progress", f"{registry_done} из {n}")
                if registry_done == n:
                    _update_stage(check_id, STAGE_REGISTRY, "done", f"{n} из {n}")
                _update_stage(check_id, STAGE_LOCALIZATION, "in_progress", f"{loc_done} из {n}")

            okpd2_code = raw_item.get("okpd2_code") or reg_result["okpd2_from_registry"]

            # 2b. Localization check
            if reg_result["status"] in ("ok", "not_actual"):
                # REP level lookup — only when the dictionary says this OKPD2
                # actually carries levels. Saves an HTTP call for the 98% of
                # codes that don't.
                rep_level: Optional[str] = None
                if should_check_rep_level(okpd2_code):
                    rep_level = await _fetch_rep_level_via_scraper(
                        registry_number, http_client
                    )
                loc_result = check_localization(
                    okpd2_code,
                    reg_result["localization_score"],
                    rep_level_observed=rep_level,
                )
                check_item.localization_status = loc_result.status
                check_item.localization_actual_score = loc_result.actual_score
                check_item.localization_required_score = loc_result.required_score
                check_item.localization_details = (
                    json.dumps(loc_result.details, ensure_ascii=False, default=str)
                    if loc_result.details else None
                )
            else:
                check_item.localization_status = "skipped"

            async with lock:
                loc_done += 1
                _update_stage(check_id, STAGE_LOCALIZATION, "in_progress", f"{loc_done} из {n}")
                if loc_done == n:
                    _update_stage(check_id, STAGE_LOCALIZATION, "done", f"{n} из {n}")
                _update_stage(check_id, STAGE_GISP, "in_progress", f"{gisp_done} из {n}")

            # 2c. GISP check
            t_gisp = 0.0
            if reg_result["status"] == "ok" and supplier_chars:
                t0 = time.monotonic()
                gisp_result = await check_gisp_characteristics(
                    registry_number=registry_number,
                    product_name=raw_item.get("name", ""),
                    supplier_characteristics=supplier_chars,
                    client=http_client,
                )
                t_gisp = round(time.monotonic() - t0, 1)
                check_item.gisp_status = gisp_result.status
                check_item.gisp_characteristics = json.dumps(gisp_result.gisp_characteristics, ensure_ascii=False)
                check_item.gisp_comparison = json.dumps(gisp_result.comparison, ensure_ascii=False)
                check_item.gisp_url = gisp_result.gisp_url
            else:
                check_item.gisp_status = "skipped"

            check_item.overall_status = _compute_overall(check_item)

            item_total = round(time.monotonic() - item_start, 1)
            logger.info(
                f"[check={check_id}] Item {pos}/{n}: "
                f"total={item_total}s (registry={t_registry}s, gisp={t_gisp}s)"
            )

            # Update progress (thread-safe)
            async with lock:
                processed_count += 1
                gisp_done += 1
                _progress[check_id]["processed"] = processed_count
                _progress[check_id]["message"] = f"Проверено {processed_count} из {n}"
                _update_stage(check_id, STAGE_GISP, "in_progress", f"{gisp_done} из {n}")
                if gisp_done == n:
                    _update_stage(check_id, STAGE_GISP, "done", f"{n} из {n}")

            return pos, check_item

    # Run all items in parallel with shared HTTP client (proxied for GISP)
    proxy = os.getenv("GISP_PROXY_URL") or None
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, proxy=proxy) as http_client:
        tasks = [
            check_single_item(pos, raw_item, http_client)
            for pos, raw_item in enumerate(items, start=1)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Save results to DB in order
    for result in sorted(results, key=lambda r: r[0] if isinstance(r, tuple) else 0):
        if isinstance(result, Exception):
            logger.error(f"[check={check_id}] Item failed: {result}")
            continue
        pos, check_item = result

        if check_item.overall_status == "ok":
            ok += 1
        elif check_item.overall_status == "warning":
            warning += 1
        elif check_item.overall_status == "error":
            error += 1
        else:
            not_found += 1

        db.add(check_item)

    db.commit()

    # Step 3: Generate PDF report
    _update_stage(check_id, STAGE_REPORT, "in_progress")
    _progress[check_id]["message"] = "Формирование отчёта..."
    t0 = time.monotonic()
    reports_dir = os.getenv("REPORTS_DIR", "./reports")
    Path(reports_dir).mkdir(parents=True, exist_ok=True)
    report_filename = f"report_{check_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    report_path = str(Path(reports_dir) / report_filename)

    db.refresh(check)
    db_items = db.query(RegimeCheckItem).filter(RegimeCheckItem.check_id == check_id).all()
    generate_report(check, db_items, report_path)
    timings["3_pdf"] = round(time.monotonic() - t0, 1)

    total_time = round(time.monotonic() - pipeline_start, 1)
    timings["total"] = total_time
    timings["avg_per_item"] = round(total_time / len(items), 1) if items else 0

    logger.info(
        f"[check={check_id}] DONE in {total_time}s "
        f"(parse={timings.get('1_parse_file', '?')}s, "
        f"avg_item={timings['avg_per_item']}s, "
        f"pdf={timings['3_pdf']}s, "
        f"concurrency={MAX_CONCURRENT})"
    )

    check.ok_count = ok
    check.warning_count = warning
    check.error_count = error
    check.not_found_count = not_found
    check.status = "done"
    db.commit()

    _update_stage(check_id, STAGE_REPORT, "done")
    # Preserve stages in final progress
    stages = _progress.get(check_id, {}).get("stages", [])
    _progress[check_id] = {
        "total": len(items),
        "processed": len(items),
        "status": "done",
        "message": f"Готово за {total_time}с (≈{timings['avg_per_item']}с/товар)",
        "timings": timings,
        "stages": stages,
    }


def _record_pipeline_failure(
    check_id: int,
    check: RegimeCheck,
    db: Session,
    pipeline_start: float,
    exc: Exception,
) -> None:
    total_time = round(time.monotonic() - pipeline_start, 1)
    logger.error(f"[check={check_id}] FAILED after {total_time}s: {exc}")
    _set_error(check, db, str(exc))
    prev = _progress.get(check_id, {})
    _progress[check_id] = {
        "total": prev.get("total", 0),
        "processed": prev.get("processed", 0),
        "status": "error",
        "message": f"Ошибка: {exc}",
        "stages": prev.get("stages", []),
    }


def _compute_overall(item: RegimeCheckItem) -> str:
    if item.registry_status == "registry_error":
        return "warning"
    if item.registry_status == "not_found":
        return "not_found"
    if item.registry_status == "not_actual":
        return "warning"
    if item.gisp_status == "mismatch" or item.localization_status == "insufficient":
        return "error"
    # okpd_not_found = supplier didn't provide ОКПД2 at all — real gap
    # score_missing = threshold exists but registry didn't return a score
    if item.gisp_status in ("warning", "gisp_unavailable") or item.localization_status in ("score_missing", "okpd_not_found"):
        return "warning"
    # out_of_scope / advisory_min_share / ok → товар проходит
    return "ok"


def _set_error(check: RegimeCheck, db: Session, message: str) -> None:
    check.status = "error"
    db.commit()
