"""Orchestrates the full check pipeline for a supplier file."""
import json
import os
import asyncio
import time
import logging
import httpx
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.orm import Session
from sqlmodel import Session as SMSession
from ..models import RegimeCheck, RegimeCheckItem
from .file_parser import parse_supplier_file
from .registry_checker import check_registry_number
from ..database import engine
from .gisp_checker import check_gisp_characteristics
from .localization_checker import check_localization
from .report_generator import generate_report

logger = logging.getLogger(__name__)

# Max concurrent item checks (don't hammer APIs too hard)
MAX_CONCURRENT = 5

# In-memory progress store: check_id -> {total, processed, status, message, timings}
_progress: dict[int, dict] = {}


def _get_session() -> SMSession:
    """Create a new session from the engine."""
    return SMSession(engine)


def get_progress(check_id: int) -> dict:
    return _progress.get(check_id, {"total": 0, "processed": 0, "status": "pending", "message": ""})


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

    _progress[check_id] = {"total": 0, "processed": 0, "status": "processing", "message": "Парсинг файла..."}

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

    _progress[check_id] = {
        "total": len(items),
        "processed": 0,
        "status": "processing",
        "message": "Проверка товаров...",
    }

    ok = warning = error = not_found = 0
    processed_count = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    lock = asyncio.Lock()

    async def check_single_item(pos: int, raw_item: dict, http_client: httpx.AsyncClient):
        nonlocal processed_count
        async with semaphore:
            item_start = time.monotonic()

            check_item = RegimeCheckItem(
                check_id=check_id,
                product_name=raw_item.get("name"),
                registry_number=raw_item.get("registry_number"),
                okpd2_code=raw_item.get("okpd2_code"),
                supplier_characteristics=json.dumps(raw_item.get("characteristics", []), ensure_ascii=False),
            )

            registry_number = raw_item.get("registry_number") or ""
            supplier_chars = raw_item.get("characteristics", [])

            # 2a. Registry check (local DB, synchronous)
            t0 = time.monotonic()
            registry_db = _get_session()
            try:
                reg_result = check_registry_number(registry_number, db=registry_db)
            finally:
                registry_db.close()
            t_registry = round(time.monotonic() - t0, 1)

            check_item.registry_status = reg_result.status
            check_item.registry_actual = reg_result.is_actual
            check_item.registry_cert_end_date = reg_result.cert_end_date
            check_item.registry_raw_url = reg_result.url

            okpd2_code = raw_item.get("okpd2_code") or reg_result.okpd2_from_registry

            # 2b. Localization check
            if reg_result.status in ("ok", "not_actual"):
                loc_result = check_localization(okpd2_code, reg_result.localization_score)
                check_item.localization_status = loc_result.status
                check_item.localization_actual_score = loc_result.actual_score
                check_item.localization_required_score = loc_result.required_score
            else:
                check_item.localization_status = "skipped"

            # 2c. GISP check
            t_gisp = 0.0
            if reg_result.status == "ok" and supplier_chars:
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
                f"[check={check_id}] Item {pos}/{len(items)}: "
                f"total={item_total}s (registry={t_registry}s, gisp={t_gisp}s)"
            )

            # Update progress (thread-safe)
            async with lock:
                processed_count += 1
                _progress[check_id] = {
                    "total": len(items),
                    "processed": processed_count,
                    "status": "processing",
                    "message": f"Проверено {processed_count} из {len(items)}",
                }

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

    _progress[check_id] = {
        "total": len(items),
        "processed": len(items),
        "status": "done",
        "message": f"Готово за {total_time}с (≈{timings['avg_per_item']}с/товар)",
        "timings": timings,
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
    if item.gisp_status in ("warning", "gisp_unavailable") or item.localization_status in ("score_missing", "okpd_not_found"):
        return "warning"
    return "ok"


def _set_error(check: RegimeCheck, db: Session, message: str) -> None:
    check.status = "error"
    db.commit()
