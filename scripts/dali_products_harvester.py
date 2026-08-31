#!/usr/bin/env python3
"""Rebuilds products.csv, the GTIN database the service reads at startup.

The service ships products.csv as /usr/share/wb-mqtt-dali/products.csv and uses it to turn a
GTIN read from a device into a brand and a model name. This script rebuilds it by scraping the
DALI Alliance product registry: the listing pages carry the DALI parts a product implements,
each per-product page carries everything else in its General information table. Column order
follows what gtin_db.DaliDatabase expects.

Products the registry no longer lists are carried over from the previous products.csv: they
are delisted, not uninstalled, and a bus still has them. Entries known to be wrong are dropped
by hand, see BROKEN_REGISTRY_ENTRIES.

Development tool, the Debian package does not install it. Needs requests and beautifulsoup4
from requirements-dev.txt.

    scripts/dali_products_harvester.py                      # full run, rewrites products.csv
    scripts/dali_products_harvester.py -p 2 -o /tmp/try.csv  # two pages into a scratch file

A full run is ~9400 requests and half an hour or so: product pages are fetched by a pool of
threads, and 8 of them already saturate the registry (16 are no faster). Review the result
with git diff before committing it. An unreachable page aborts the run with the file left
untouched, so a truncated database never reaches the repository.
"""

import argparse
import csv
import logging
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.dali-alliance.org"
# Empty filter values ask for the unfiltered list: registered and obsolete products alike.
PRODUCTS_URL = f"{BASE_URL}/products?family_products%5B%5D=&registered%5B%5D=&obsolete%5B%5D="

DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "products.csv"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_S = 5.0
# The registry drops a keep-alive connection every twentieth request or so. Waiting out
# --retry-delay for that costs more than the whole run: reconnecting is immediate.
CONNECTION_RETRY_DELAY_S = 0.2
DEFAULT_REQUEST_DELAY_S = 0.1
DEFAULT_WORKERS = 8
REQUEST_TIMEOUT_S = 30

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# "DALI Parts" keeps the header the published products.csv already has.
CSV_HEADER = ["dali_product_id", "brand_name", "product_name", "product_part_number", "gtin", "DALI Parts"]

# Rows of the General information table of a product page, by their exact label.
PRODUCT_PAGE_FIELDS = {
    "dali product id": "dali_product_id",
    "brand name": "brand_name",
    "product name": "product_name",
    "product part number": "product_part_number",
    "gtin": "gtin",
}

# Registry entries dropped by hand, by DALI product id. An entry whose status is "Registered:
# DALI version-1" is a self-declaration the Alliance never tested, and a few of them are broken
# beyond what any parsing can fix.
BROKEN_REGISTRY_ENTRIES = {
    # Je Woo EMDS3/xH/DA/MH: a half-filled template. Its name still carries the xH placeholder
    # where all 26 sibling entries spell out 1H or 3H, and its part number 63.22.00.411 is a
    # digit short of the family format (...4130 for 1H, ...4330 for 3H). Its GTIN is the one of
    # Je Woo LSP-SF4W/DA (id 64) copied over, and as the later row it wins the lookup, so a
    # perfectly ordinary LSP-SF4W/DA on the bus would report itself as this phantom. Neither
    # model appears anywhere outside the registry, and the registry does not index DALI-1
    # entries in its own GTIN search, so there is no second source to confirm this from.
    "66": "Je Woo EMDS3/xH/DA/MH, a template entry holding the GTIN of Je Woo LSP-SF4W/DA (id 64)",
}

logger = logging.getLogger("dali_products_harvester")


@dataclass
class Product:
    """One products.csv row; an empty string means the registry did not publish the field."""

    dali_product_id: str = ""
    brand_name: str = ""
    product_name: str = ""
    product_part_number: str = ""
    gtin: str = ""
    dali_parts: str = ""


@dataclass
class Columns:
    """Where the listing table keeps each field; None means the column is absent."""

    brand: Optional[int] = None
    product_name: Optional[int] = None
    parts: Optional[int] = None


class ListingRow(NamedTuple):
    """A product read from a listing row, and the page carrying the rest of its data."""

    product: Product
    url: str


class HarvestError(Exception):
    """A page could not be fetched after all retries, so the harvest is incomplete."""


class DaliProductsHarvester:
    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        delay_s: float = DEFAULT_REQUEST_DELAY_S,
        workers: int = DEFAULT_WORKERS,
    ) -> None:
        self.max_retries = max_retries
        self.retry_delay_s = retry_delay_s
        self.delay_s = delay_s
        self.workers = workers
        # requests.Session is not thread-safe, so every worker thread keeps its own.
        self._thread_state = threading.local()

    def harvest_all_products(
        self, start_url: str = PRODUCTS_URL, max_pages: Optional[int] = None
    ) -> list[Product]:
        """Walks the listing pages until one comes back empty. Raises HarvestError on a dead page."""
        products: list[Product] = []
        page = 0
        # One pool for the whole run: its threads keep their sessions, and with them the
        # connections to the registry, alive from page to page.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while max_pages is None or page < max_pages:
                page += 1
                url = f"{start_url}&page={page}"
                logger.info("Processing page %d: %s", page, url)
                soup = self._get_page(url)
                if soup is None:
                    raise HarvestError(f"page {page} unreachable after {self.max_retries} retries: {url}")
                page_products = self._extract_products_from_page(soup, pool)
                logger.info("Extracted %d products from page %d", len(page_products), page)
                if not page_products:
                    logger.info("Reached last page")
                    break
                products.extend(page_products)
        logger.info("Total collected %d products from %d pages", len(products), page)
        return products

    def save_to_csv(self, products: list[Product], path: Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            for product in products:
                writer.writerow(
                    [
                        product.dali_product_id,
                        product.brand_name,
                        product.product_name,
                        product.product_part_number,
                        product.gtin,
                        product.dali_parts,
                    ]
                )
        logger.info("Data saved to file %s", path)

    # --- Private ---

    def _session(self) -> requests.Session:
        session = getattr(self._thread_state, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(HTTP_HEADERS)
            self._thread_state.session = session
        return session

    def _get_page(self, url: str) -> Optional[BeautifulSoup]:
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    logger.info("Retry attempt %d/%d: %s", attempt, self.max_retries, url)
                else:
                    logger.info("Fetching page: %s", url)
                time.sleep(self.delay_s)
                resp = self._session().get(url, timeout=REQUEST_TIMEOUT_S)
                resp.raise_for_status()
                return BeautifulSoup(resp.content, "html.parser")
            except requests.exceptions.RequestException as e:
                if attempt >= self.max_retries:
                    logger.error("Failed to fetch %s after %d attempts: %s", url, self.max_retries, e)
                    return None
                connection_lost = isinstance(e, requests.exceptions.ConnectionError)
                wait_s = (CONNECTION_RETRY_DELAY_S if connection_lost else self.retry_delay_s) * (attempt + 1)
                logger.warning("Error %s for %s. Retrying in %s s", e, url, wait_s)
                time.sleep(wait_s)
        return None

    def _extract_products_from_page(self, soup: BeautifulSoup, pool: ThreadPoolExecutor) -> list[Product]:
        table = self._find_product_table(soup)
        if table is None:
            logger.warning("No product table on the page, the registry layout has changed")
            return []
        rows = table.find_all("tr")
        if len(rows) < 2:
            logger.info("The product table holds nothing but its header, the products are over")
            return []
        columns = self._find_columns(rows[0])
        products = []
        pending: list[ListingRow] = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            product = Product()
            product.brand_name = self._cell_text(cells, columns.brand)
            product.product_name = self._cell_text(cells, columns.product_name)
            product.dali_parts = self._extract_parts(self._cell_text(cells, columns.parts, " "))
            product_url = self._find_product_url(cells)
            if product_url:
                pending.append(ListingRow(product, product_url))
            if product.brand_name or product.product_name:
                products.append(product)
        # The product pages of one listing page are fetched at once; each task writes only into
        # its own Product, so the rows keep the order the registry lists them in.
        logger.info("Fetching %d product pages", len(pending))
        list(pool.map(lambda row: self._fill_details(row.product, row.url), pending))
        return products

    @staticmethod
    def _find_columns(header_row) -> Columns:
        headers = [cell.get_text(strip=True).lower() for cell in header_row.find_all(["th", "td"])]
        logger.info("Table headers: %s", headers)
        columns = Columns()
        for i, header in enumerate(headers):
            if "brand" in header:
                columns.brand = i
            elif "product name" in header:
                columns.product_name = i
            elif "parts" in header:
                columns.parts = i
        return columns

    @staticmethod
    def _cell_text(cells, index: Optional[int], separator: str = "") -> str:
        if index is None or index >= len(cells):
            return ""
        return cells[index].get_text(separator, strip=True)

    @staticmethod
    def _find_product_table(soup: BeautifulSoup):
        """The listing table, recognised by the width of its header: the page also carries a
        one-cell table with the disclaimer, and the page past the last one carries the listing
        table with the header alone, which is how a run learns the products are over."""
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if rows and len(rows[0].find_all(["th", "td"])) > 5:
                return table
        return None

    @staticmethod
    def _find_product_url(cells) -> Optional[str]:
        for cell in cells:
            link = cell.find("a")
            if link and link.get("href"):
                return urljoin(BASE_URL, link.get("href"))
        return None

    @staticmethod
    def _extract_parts(cell_text: str) -> str:
        """Part numbers of IEC 62386 (101, 207, 251, ...), in listing order, without repetitions."""
        codes = []
        for code in re.findall(r"\b\d{2,4}\b", re.sub(r"\s+", " ", cell_text)):
            if code not in codes:
                codes.append(code)
        return ",".join(codes)

    def _fill_details(self, product: Product, product_url: str) -> None:
        """The listing cuts long brand and product names to 27 characters, the product page has
        them in full, so its values win over the ones already read from the listing row."""
        soup = self._get_page(product_url)
        if soup is None:
            logger.warning("No product page for %s, keeping the listing values", product_url)
            return
        rows = self._read_spec_rows(soup)
        if not rows.keys() & PRODUCT_PAGE_FIELDS.keys():
            logger.warning("No General information table at %s, keeping the listing values", product_url)
            return
        for label, attribute in PRODUCT_PAGE_FIELDS.items():
            value = rows.get(label, "")
            if value:
                setattr(product, attribute, value)
        product.gtin = self._clean_gtin(product.gtin)

    @staticmethod
    def _read_spec_rows(soup: BeautifulSoup) -> dict[str, str]:
        """Label to value of every "<th>label</th><td>value</td>" row, first occurrence wins.

        The two-cell shape also tells a product page from the listing the site serves instead
        when a product id no longer exists: there the rows are nine cells wide.
        """
        rows: dict[str, str] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) == 2 and cells[0].name == "th":
                label = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)).lower()
                rows.setdefault(label, cells[1].get_text(" ", strip=True))
        return rows

    @staticmethod
    def _clean_gtin(value: str) -> str:
        match = re.search(r"(\d{8,14})", value)
        return match.group(1) if match else ""


def read_products(path: Path) -> list[Product]:
    """Reads back what save_to_csv wrote."""
    with open(path, newline="", encoding="utf-8") as f:
        return [
            Product(
                dali_product_id=row["dali_product_id"],
                brand_name=row["brand_name"],
                product_name=row["product_name"],
                product_part_number=row["product_part_number"],
                gtin=row["gtin"],
                dali_parts=row["DALI Parts"],
            )
            for row in csv.DictReader(f)
        ]


def merge_with_previous(products: list[Product], previous: list[Product]) -> list[Product]:
    """Updates the previous file in place: every row keeps the line it was on, rows the registry
    no longer lists stay, and products it has gained go to the end.

    Row order follows the previous file rather than the listing, which is sorted by date: a
    product edited in the registry would move to the top of the listing and read in the diff as
    deleted here and added there.

    Products the registry has dropped are kept, they are delisted rather than uninstalled and a
    bus can still have them. One exception: a delisted product whose GTIN a listed one now
    carries, which happens when a product is rebranded. DaliDatabase keys on the GTIN and keeps
    the last row holding it, so keeping such a row could hide the product that is on sale.
    """
    listed_by_id = {product.dali_product_id: product for product in products}
    listed_by_gtin = {product.gtin: product for product in products if product.gtin}
    merged = []
    kept = 0
    dropped = 0
    for product in previous:
        listed = listed_by_id.get(product.dali_product_id)
        if listed is not None:
            merged.append(listed)
            continue
        owner = listed_by_gtin.get(product.gtin)
        if owner is not None:
            # Both rows in full: the operator has to be able to judge the case from the log alone.
            logger.warning(
                "GTIN %s belongs to two products, keeping the one the registry lists:", product.gtin
            )
            logger.warning("    dropped, gone from the registry: %s", product)
            logger.warning("    kept, listed by the registry:    %s", owner)
            dropped += 1
            continue
        merged.append(product)
        kept += 1
    known_ids = {product.dali_product_id for product in previous}
    added = [product for product in products if product.dali_product_id not in known_ids]
    merged.extend(added)
    logger.info(
        "%d products are gone from the registry, %d of them stay in the database: a product that "
        "is no longer sold is still installed somewhere, and its GTIN still has to resolve",
        kept + dropped,
        kept,
    )
    if dropped:
        logger.warning("%d of the gone products had to be dropped on a GTIN clash, see above", dropped)
    logger.info("%d products are new in the registry", len(added))
    return merged


def drop_broken_entries(products: list[Product]) -> list[Product]:
    """Removes what BROKEN_REGISTRY_ENTRIES lists, and reports entries the registry has fixed."""
    kept = []
    for product in products:
        reason = BROKEN_REGISTRY_ENTRIES.get(product.dali_product_id)
        if reason is None:
            kept.append(product)
            continue
        logger.info("Dropping known bad entry %s: %s", product.dali_product_id, reason)
        logger.info("    %s", product)
    obsolete = BROKEN_REGISTRY_ENTRIES.keys() - {product.dali_product_id for product in products}
    if obsolete:
        logger.warning(
            "The registry no longer has %s, take them out of BROKEN_REGISTRY_ENTRIES",
            ", ".join(sorted(obsolete)),
        )
    return kept


def log_duplicate_gtins(products: list[Product]) -> None:
    """Warns about GTINs the registry gave to products that are not the same.

    A GTIN carried by several entries of one and the same product is the usual case: a
    manufacturer certifies a driver more than once, and whichever entry a lookup answers with
    names the same thing. Two different products behind one GTIN cannot be told apart at all,
    so those are reported in full.
    """
    sharing_gtin = defaultdict(list)
    for product in products:
        if product.gtin:
            sharing_gtin[product.gtin].append(product)
    repeated = 0
    for gtin, sharing in sharing_gtin.items():
        if len(sharing) < 2:
            continue
        if len({(product.brand_name, product.product_name) for product in sharing}) == 1:
            repeated += 1
            continue
        logger.warning(
            "GTIN %s belongs to products that differ, a lookup answers with the last of them:", gtin
        )
        for product in sharing:
            logger.warning("    %s", product)
    if repeated:
        logger.info(
            "%d GTINs cover one product registered several times over, which a lookup cannot get wrong",
            repeated,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-p", "--pages", "--max-pages", dest="max_pages", type=int, help="stop after N listing pages"
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="output CSV (default: products.csv)"
    )
    parser.add_argument(
        "-r",
        "--retries",
        "--max-retries",
        dest="max_retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"retry attempts per request (default {DEFAULT_MAX_RETRIES})",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY_S,
        help=f"delay between retries, s (default {DEFAULT_RETRY_DELAY_S})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_S,
        help=f"delay before each request, s, per worker (default {DEFAULT_REQUEST_DELAY_S})",
    )
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"product pages to fetch in parallel (default {DEFAULT_WORKERS}, 1 disables the pool)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    harvester = DaliProductsHarvester(args.max_retries, args.retry_delay, args.delay, args.workers)
    try:
        products = harvester.harvest_all_products(max_pages=args.max_pages)
    except HarvestError as e:
        # Writing a truncated file would silently shrink the shipped database.
        logger.error("Harvest aborted, %s is left untouched: %s", args.output, e)
        return 1
    if not products:
        logger.error("No products collected, %s is left untouched", args.output)
        return 1
    if args.output.exists():
        products = merge_with_previous(products, read_products(args.output))
    # After the merge: a bad entry carried over from the previous file has to go as well.
    products = drop_broken_entries(products)
    log_duplicate_gtins(products)
    harvester.save_to_csv(products, args.output)
    logger.info("Done, %d products in %s", len(products), args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
