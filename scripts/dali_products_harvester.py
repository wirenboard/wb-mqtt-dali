#!/usr/bin/env python3
"""Rebuilds products.csv, the GTIN database the service reads at startup.

The service ships products.csv as /usr/share/wb-mqtt-dali/products.csv and uses it to turn a
GTIN read from a device into a brand and a model name. This script rebuilds it by scraping the
DALI Alliance product registry: the listing pages carry the brand, the product name and the
DALI parts a product implements, each per-product page carries the DALI product id, the GTIN
and the part number. Column order follows what gtin_db.DaliDatabase expects.

Development tool, imported from wirenboard/wb-dali-playground; the Debian package does not
install it. Needs requests and beautifulsoup4 from requirements-dev.txt.

    scripts/dali_products_harvester.py                      # full run, rewrites products.csv
    scripts/dali_products_harvester.py -p 2 -o /tmp/try.csv  # two pages into a scratch file

A full run is ~9000 requests and takes about an hour; review the result with git diff before
committing it. An unreachable page aborts the run with the file left untouched, so a truncated
database never reaches the repository.
"""

import argparse
import csv
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.dali-alliance.org"
# Empty filter values ask for the unfiltered list: registered and obsolete products alike.
PRODUCTS_URL = f"{BASE_URL}/products?family_products%5B%5D=&registered%5B%5D=&obsolete%5B%5D="

DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "products.csv"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_S = 5.0
DEFAULT_REQUEST_DELAY_S = 0.1
REQUEST_TIMEOUT_S = 30

# "DALI Parts" keeps the header the published products.csv already has.
CSV_HEADER = ["dali_product_id", "brand_name", "product_name", "product_part_number", "gtin", "DALI Parts"]

MAX_PART_NUMBER_LEN = 80

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


@dataclass
class ProductDetails:
    """The three fields that only a per-product page carries."""

    dali_product_id: str = ""
    gtin: str = ""
    product_part_number: str = ""


class HarvestError(Exception):
    """A page could not be fetched after all retries, so the harvest is incomplete."""


class DaliProductsHarvester:
    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        delay_s: float = DEFAULT_REQUEST_DELAY_S,
    ) -> None:
        self.max_retries = max_retries
        self.retry_delay_s = retry_delay_s
        self.delay_s = delay_s
        self.session = requests.Session()
        self.session.headers.update(
            {
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
        )

    def harvest_all_products(
        self, start_url: str = PRODUCTS_URL, max_pages: Optional[int] = None
    ) -> list[Product]:
        """Walks the listing pages until one comes back empty. Raises HarvestError on a dead page."""
        products: list[Product] = []
        page = 0
        while max_pages is None or page < max_pages:
            page += 1
            url = f"{start_url}&page={page}"
            logger.info("Processing page %d: %s", page, url)
            soup = self._get_page(url)
            if soup is None:
                raise HarvestError(f"page {page} unreachable after {self.max_retries} retries: {url}")
            page_products = self._extract_products_from_page(soup)
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

    def _get_page(self, url: str) -> Optional[BeautifulSoup]:
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    logger.info("Retry attempt %d/%d: %s", attempt, self.max_retries, url)
                else:
                    logger.info("Fetching page: %s", url)
                time.sleep(self.delay_s)
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT_S)
                resp.raise_for_status()
                return BeautifulSoup(resp.content, "html.parser")
            except requests.exceptions.RequestException as e:
                if attempt >= self.max_retries:
                    logger.error("Failed to fetch %s after %d attempts: %s", url, self.max_retries, e)
                    return None
                wait_s = self.retry_delay_s * (attempt + 1)
                logger.warning("Error %s for %s. Retrying in %s s", e, url, wait_s)
                time.sleep(wait_s)
        return None

    def _extract_products_from_page(self, soup: BeautifulSoup) -> list[Product]:
        table = self._find_product_table(soup)
        if table is None:
            logger.warning("Main product table not found")
            return []
        rows = table.find_all("tr")
        columns = self._find_columns(rows[0])
        products = []
        for row_idx, row in enumerate(rows[1:], 1):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            product = Product()
            product.brand_name = self._cell_text(cells, columns.brand)
            product.product_name = self._cell_text(cells, columns.product_name)
            product.dali_parts = self._extract_parts(self._cell_text(cells, columns.parts, " "))
            product_url = self._find_product_url(cells)
            if product_url:
                logger.info("Fetching detailed info for product %d", row_idx)
                self._fill_details(product, product_url)
            if product.brand_name or product.product_name:
                products.append(product)
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
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) > 1 and len(rows[0].find_all(["th", "td"])) > 5:
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
        details = self._extract_details(product_url)
        product.dali_product_id = details.dali_product_id or product.dali_product_id
        product.gtin = details.gtin or product.gtin
        product.product_part_number = details.product_part_number or product.product_part_number

    def _extract_details(self, product_url: str) -> ProductDetails:
        soup = self._get_page(product_url)
        if soup is None:
            return ProductDetails()
        details = ProductDetails()
        text = soup.get_text()
        patterns = {
            "dali_product_id": [r"DALI Product ID[:\s]*(\d+)", r"Product ID[:\s]*(\d+)", r"ID[:\s]*(\d+)"],
            "gtin": [
                r"GTIN[:\s]*(\d{8,14})",
                r"Global Trade Item Number[:\s]*(\d{8,14})",
                r"Barcode[:\s]*(\d{8,14})",
            ],
            "product_part_number": [
                r"Part Number[:\s]*([^\n\r\t,]+)",
                r"Model Number[:\s]*([^\n\r\t,]+)",
                r"SKU[:\s]*([^\n\r\t,]+)",
                r"Article Number[:\s]*([^\n\r\t,]+)",
            ],
        }
        for field, field_patterns in patterns.items():
            for pattern in field_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    setattr(details, field, match.group(1).strip())
                    break
        self._fill_details_from_tables(soup, details)
        details.product_part_number = self._clean_part_number(details.product_part_number)
        details.gtin = self._clean_gtin(details.gtin)
        return details

    @staticmethod
    def _fill_details_from_tables(soup: BeautifulSoup, details: ProductDetails) -> None:
        """Fills whatever the free-text patterns missed from the specification tables."""
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue
                label = cells[0].get_text(strip=True).lower()
                value = cells[1].get_text(strip=True)
                if "gtin" in label and not details.gtin:
                    details.gtin = value
                elif ("product id" in label or "dali id" in label) and not details.dali_product_id:
                    details.dali_product_id = value
                elif ("part number" in label or "model" in label) and not details.product_part_number:
                    details.product_part_number = value

    @staticmethod
    def _clean_part_number(value: str) -> str:
        if not value:
            return ""
        # The page text runs the following sections into the part number, cut them off.
        value = re.split(r"GTIN\d{8,14}", value)[0]
        value = re.split(r"Bus unit configuration|TestingTest conditions|Testing|Test conditions", value)[0]
        value = re.sub(r"[^A-Za-z0-9_\-\. /]+", "", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value[:MAX_PART_NUMBER_LEN]

    @staticmethod
    def _clean_gtin(value: str) -> str:
        match = re.search(r"(\d{8,14})", value)
        return match.group(1) if match else ""


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
        help=f"delay between requests, s (default {DEFAULT_REQUEST_DELAY_S})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    harvester = DaliProductsHarvester(args.max_retries, args.retry_delay, args.delay)
    try:
        products = harvester.harvest_all_products(max_pages=args.max_pages)
    except HarvestError as e:
        # Writing a truncated file would silently shrink the shipped database.
        logger.error("Harvest aborted, %s is left untouched: %s", args.output, e)
        return 1
    if not products:
        logger.error("No products collected, %s is left untouched", args.output)
        return 1
    harvester.save_to_csv(products, args.output)
    logger.info("Done, %d products in %s", len(products), args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
