"""Download PP 719 registry CSV from Minpromtorg opendata and load into local DB."""
import csv
import io
import os
import re
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.orm import Session
from sqlmodel import Session as SMSession

from ..models import RegistryProduct
from ..database import engine

logger = logging.getLogger(__name__)

OPENDATA_PAGE_URL = "https://minpromtorg.gov.ru/opendata/1000000012-ReestrProducts"
OPENDATA_BASE_URL = "https://minpromtorg.gov.ru"

# Batch size for bulk inserts
BATCH_SIZE = 5000

# Browser-like headers to avoid 403
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def _get_session() -> SMSession:
    """Create a new session from the engine."""
    return SMSession(engine)


def _find_csv_url(html: str) -> Optional[str]:
    """Parse the opendata page to find CSV download link in row #8."""
    # The page has a table; row 8 contains the CSV file URL.
    # Look for links ending in .csv
    csv_links = re.findall(r'href=["\']([^"\']*\.csv[^"\']*)["\']', html, re.IGNORECASE)
    if csv_links:
        # Pick the first .csv link found (the data file)
        url = csv_links[0]
        if url.startswith("/"):
            url = OPENDATA_BASE_URL + url
        return url

    # Fallback: look for any link containing "data-" and "structure-" pattern
    data_links = re.findall(r'href=["\']([^"\']*data-\d+[^"\']*)["\']', html, re.IGNORECASE)
    for link in data_links:
        if link.startswith("/"):
            link = OPENDATA_BASE_URL + link
        return link

    return None


def _parse_float(val: str) -> Optional[float]:
    """Parse float from CSV value, return None for empty/dash values."""
    if not val or val.strip() in ("-", "", "None"):
        return None
    try:
        return float(val.strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


def _clean(val: str) -> Optional[str]:
    """Clean CSV value, return None for dash/empty."""
    if not val or val.strip() in ("-", ""):
        return None
    return val.strip()


async def fetch_csv_url() -> str:
    """Fetch the opendata page and extract the CSV download URL."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=_HEADERS) as client:
        resp = await client.get(OPENDATA_PAGE_URL)
        resp.raise_for_status()
        url = _find_csv_url(resp.text)
        if not url:
            raise RuntimeError("Could not find CSV link on opendata page")
        logger.info(f"Found CSV URL: {url}")
        return url


async def download_csv(url: str, dest_dir: str = None) -> str:
    """Download CSV file, return path to local file."""
    if dest_dir is None:
        dest_dir = tempfile.gettempdir()

    filename = url.split("/")[-1].split("?")[0]
    if not filename.endswith(".csv"):
        filename = f"registry_{datetime.now().strftime('%Y%m%d')}.csv"

    dest_path = str(Path(dest_dir) / filename)

    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True, headers=_HEADERS) as client:
        logger.info(f"Downloading CSV from {url}...")
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    logger.info(f"CSV downloaded to {dest_path}")
    return dest_path


def load_csv_to_db(csv_path: str, db: Session) -> int:
    """Read CSV and insert all rows into registryproduct table.
    Returns number of rows loaded."""

    # Clear existing data
    db.query(RegistryProduct).delete()
    db.commit()

    count = 0
    batch = []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            product = RegistryProduct(
                registry_number=_clean(row.get("Registernumber")),
                org_name=_clean(row.get("Nameoforg")),
                inn=_clean(row.get("INN")),
                ogrn=_clean(row.get("OGRN")),
                product_name=_clean(row.get("Productname")),
                okpd2=_clean(row.get("OKPD2")),
                tnved=_clean(row.get("TNVED")),
                doc_date=_clean(row.get("Docdate")),
                doc_valid_till=_clean(row.get("Docvalidtill")),
                end_date=_clean(row.get("Enddate")),
                score=_parse_float(row.get("Score", "")),
                percentage=_parse_float(row.get("Percentage", "")),
                score_desc=_clean(row.get("Scoredesc")),
                reg_number_pp=_clean(row.get("Regnumber")),
                doc_name=_clean(row.get("Docname")),
                doc_num=_clean(row.get("Docnum")),
                mpt_dep=_clean(row.get("Mptdep")),
                res_doc_num=_clean(row.get("Resdocnum")),
            )
            batch.append(product)
            count += 1

            if len(batch) >= BATCH_SIZE:
                db.bulk_save_objects(batch)
                db.commit()
                batch = []
                logger.info(f"Loaded {count} rows...")

    if batch:
        db.bulk_save_objects(batch)
        db.commit()

    logger.info(f"Finished loading {count} rows into registryproduct")
    return count


async def update_registry_data(data_dir: str = None) -> dict:
    """Full pipeline: find CSV URL -> download -> load into DB.
    Returns summary dict."""
    t0 = datetime.now()

    # Step 1: Find CSV URL
    csv_url = await fetch_csv_url()

    # Step 2: Download
    csv_path = await download_csv(csv_url, dest_dir=data_dir)

    # Step 3: Load into DB
    db = _get_session()
    try:
        row_count = load_csv_to_db(csv_path, db)
    finally:
        db.close()

    elapsed = (datetime.now() - t0).total_seconds()

    result = {
        "status": "ok",
        "csv_url": csv_url,
        "csv_path": csv_path,
        "rows_loaded": row_count,
        "elapsed_seconds": round(elapsed, 1),
        "updated_at": t0.isoformat(),
    }
    logger.info(f"Registry update complete: {result}")
    return result


def get_registry_stats(db: Session) -> dict:
    """Get basic stats about registry data in DB."""
    total = db.query(RegistryProduct).count()
    return {
        "total_products": total,
        "has_data": total > 0,
    }
