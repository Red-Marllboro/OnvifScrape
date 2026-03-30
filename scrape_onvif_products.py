#!/usr/bin/env python3
"""Scrape ONVIF conformant products and save JSON/CSV outputs.

This script follows a layered strategy:
1. Extract the public-page nonce from the live HTML.
2. Probe the legacy WordPress admin-ajax actions exactly as requested.
3. Fall back to the current live member-tools JSON flows.
4. Fall back to Playwright download automation if all requests-based paths fail.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PUBLIC_PAGE_URL = "https://www.onvif.org/conformant-products/"
LEGACY_AJAX_URL = "https://www.onvif.org/wp-admin/admin-ajax.php"
DEFAULT_MEMBER_TOOLS_URL = "https://www.onvif.org/member-tools"
DEFAULT_MEMBER_TOOLS_SLASH_URL = "https://www.onvif.org/member-tools/"
OUTPUT_CSV = Path("onvif_conformant_products.csv")
OUTPUT_JSON = Path("onvif_conformant_products.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
LEGACY_ACTIONS = [
    "get_conformant_products",
    "search_conformant_products",
    "onvif_product_search",
    "conformant_products_search",
    "filter_products",
    "load_products",
]
REQUIRED_KEYS = [
    "product_name",
    "manufacturer",
    "application_type",
    "profiles",
    "addons",
    "firmware_version",
    "certification_date",
]


class ScrapeError(RuntimeError):
    """Raised when scraping fails across all supported strategies."""


@dataclass
class LiveConfig:
    ajaxurl: str = LEGACY_AJAX_URL
    member_portal_ajaxurl: str = "https://www.onvif.org/member-tools/wp-admin/admin-ajax.php"
    api_url: str = DEFAULT_MEMBER_TOOLS_URL
    nonce: str = ""


def sanitize_member_tools_url(url: str) -> str:
    url = (url or DEFAULT_MEMBER_TOOLS_URL).strip()
    if "?rest_route=" in url:
        url = url.split("?", 1)[0]
    return url.rstrip("/")


def log(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        safe = message.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(safe, flush=True)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": PUBLIC_PAGE_URL,
            "Origin": "https://www.onvif.org",
        }
    )
    return session


def fetch_public_page(session: requests.Session) -> tuple[str, LiveConfig]:
    log(f"Fetching public page: {PUBLIC_PAGE_URL}")
    response = session.get(PUBLIC_PAGE_URL, timeout=30)
    response.raise_for_status()
    html = response.text

    nonce_match = re.search(r'"nonce"\s*:\s*"([0-9a-fA-F]+)"', html)
    if not nonce_match:
        raise ScrapeError("Could not find ONVIF nonce in public page HTML.")

    globals_match = re.search(r"var\s+globals\s*=\s*(\{.*?\});", html, re.DOTALL)
    config = LiveConfig(nonce=nonce_match.group(1))
    if globals_match:
        try:
            globals_payload = json.loads(globals_match.group(1))
            config = LiveConfig(
                ajaxurl=globals_payload.get("ajaxurl", config.ajaxurl),
                member_portal_ajaxurl=globals_payload.get(
                    "memberPortalAjaxurl", config.member_portal_ajaxurl
                ),
                api_url=sanitize_member_tools_url(
                    globals_payload.get("apiUrl", config.api_url)
                ),
                nonce=globals_payload.get("nonce", config.nonce),
            )
        except json.JSONDecodeError:
            log("Warning: failed to parse `var globals`, continuing with defaults.")
    config.api_url = sanitize_member_tools_url(config.api_url)

    log(f"Nonce found: {config.nonce}")
    log(f"Live API base: {config.api_url}")
    return html, config


def parse_possible_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def probe_legacy_actions(
    session: requests.Session, nonce: str, ajax_url: str
) -> list[dict[str, str]]:
    log("Probing legacy admin-ajax actions")
    working_action: str | None = None

    for action in LEGACY_ACTIONS:
        payload = {
            "action": action,
            "app_type": "",
            "profiles": "",
            "manufacturer": "",
            "product_name": "",
            "offset": 0,
            "limit": 10,
            "nonce": nonce,
        }
        log(f"Trying legacy action: {action}")
        try:
            response = session.post(ajax_url, data=payload, timeout=30)
        except requests.RequestException as exc:
            log(f"  -> request error: {exc}")
            continue

        if response.status_code >= 400:
            log(f"  -> HTTP {response.status_code}")
            continue

        parsed = parse_possible_json(response)
        if parsed is None:
            log("  -> invalid JSON response")
            continue
        if not isinstance(parsed, list):
            log(f"  -> JSON was {type(parsed).__name__}, not an array")
            continue
        if not parsed:
            log("  -> empty JSON array")
            continue

        log(f"  -> usable legacy action found: {action} ({len(parsed)} records in probe)")
        working_action = action
        break

    if not working_action:
        log("No working legacy action found.")
        return []

    all_records: list[dict[str, str]] = []
    offset = 0
    limit = 500

    while True:
        payload = {
            "action": working_action,
            "app_type": "",
            "profiles": "",
            "manufacturer": "",
            "product_name": "",
            "offset": offset,
            "limit": limit,
            "nonce": nonce,
        }
        try:
            response = session.post(ajax_url, data=payload, timeout=60)
            response.raise_for_status()
            parsed = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ScrapeError(f"Legacy pagination failed at offset {offset}: {exc}") from exc

        if not isinstance(parsed, list):
            raise ScrapeError(
                f"Legacy pagination expected a JSON array at offset {offset}, got {type(parsed).__name__}."
            )

        if not parsed:
            log(f"Legacy pagination complete at offset {offset}.")
            break

        batch = [normalize_record(item) for item in parsed]
        all_records.extend(batch)
        log(
            f"Fetched {len(batch)} legacy records at offset {offset}. "
            f"Running total: {len(all_records)}"
        )
        offset += 500

    return dedupe_records(all_records)


def get_live_search_filters(session: requests.Session, ajax_url: str) -> dict[str, Any]:
    log("Fetching live search filters")
    try:
        response = session.get(
            ajax_url,
            params={"action": "get_search_filters"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        log(f"Unable to fetch live search filters: {exc}")
        return {}

    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
    return {}


def try_live_export(session: requests.Session, api_url: str) -> list[dict[str, str]]:
    log("Trying live export_all_products endpoint")
    candidate_urls = [api_url.rstrip("/") + "/", api_url.rstrip("/")]

    for url in candidate_urls:
        try:
            response = session.get(
                url,
                params={"action": "export_all_products"},
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=60,
            )
        except requests.RequestException as exc:
            log(f"  -> request error for {url}: {exc}")
            continue

        if response.status_code >= 400:
            log(f"  -> {url} returned HTTP {response.status_code}")
            continue

        parsed = parse_possible_json(response)
        if not isinstance(parsed, dict):
            log(f"  -> {url} did not return JSON object data")
            continue

        results = (((parsed.get("data") or {}).get("results")) if parsed else None)
        if isinstance(results, list) and results:
            normalized = [normalize_record(item) for item in results]
            log(f"  -> export returned {len(normalized)} records")
            return dedupe_records(normalized)

        log(f"  -> {url} returned JSON but no usable results")

    return []


def build_default_search_params() -> dict[str, Any]:
    return {
        "application": [],
        "product_name": "",
        "profiles": [],
        "addons": [],
        "company": {},
        "conformance_age": {"id": 3, "label": "3 years"},
        "order_by": "post_title",
        "order": "ASC",
    }


def iterate_company_candidates(filters: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    possible_keys = ["companies", "company", "manufacturers", "manufacturer"]

    for key in possible_keys:
        value = filters.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item:
                    candidates.append(item)
            if candidates:
                break

    unique: list[dict[str, Any]] = []
    seen = set()
    for item in candidates:
        marker = json.dumps(item, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def run_live_product_search(
    session: requests.Session,
    api_url: str,
    search_params: dict[str, Any],
    label: str,
    action: str = "product_search",
) -> list[dict[str, str]]:
    log(f"Trying live search path: {label} (action={action})")
    results: list[dict[str, str]] = []
    page_number = 1
    posts_per_page = 500
    max_num_pages: int | None = None

    while True:
        try:
            response = session.post(
                api_url.rstrip("/"),
                params={
                    "action": action,
                    "searchParams": json.dumps(search_params, separators=(",", ":")),
                    "pageNumber": page_number,
                    "postsPerPage": posts_per_page,
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=60,
            )
        except requests.RequestException as exc:
            log(f"  -> request error on page {page_number}: {exc}")
            break

        if response.status_code >= 400:
            log(f"  -> HTTP {response.status_code} on page {page_number}")
            break

        payload = parse_possible_json(response)
        if not isinstance(payload, dict):
            log(f"  -> non-JSON response on page {page_number}")
            break

        if not payload.get("success"):
            log(f"  -> JSON response indicated failure on page {page_number}")
            break

        data = payload.get("data") or {}
        companies = data.get("results")
        if not isinstance(companies, list) or not companies:
            if page_number == 1:
                log("  -> no results")
            else:
                log(f"  -> no more results after page {page_number - 1}")
            break

        batch = flatten_live_company_results(companies)
        results.extend(batch)
        max_num_pages = safe_int(data.get("max_num_pages")) or max_num_pages
        total_results = safe_int(data.get("total_results"))
        total_label = total_results if total_results is not None else "unknown"
        log(
            f"Fetched {len(batch)} live records from page {page_number}. "
            f"Running total: {len(results)} / {total_label}"
        )

        if max_num_pages is not None and page_number >= max_num_pages:
            break
        page_number += 1

    return dedupe_records(results)


def try_live_paths(session: requests.Session, config: LiveConfig) -> list[dict[str, str]]:
    export_records = try_live_export(session, config.api_url)
    if export_records:
        return export_records

    log("Trying live `product_search_empty` and `product_search` paths")
    filters = get_live_search_filters(session, config.ajaxurl)

    empty_search_results = run_live_product_search(
        session=session,
        api_url=config.api_url,
        search_params=build_default_search_params(),
        label="empty/default search",
        action="product_search_empty",
    )
    if empty_search_results:
        return empty_search_results

    companies = iterate_company_candidates(filters)
    if companies:
        log(f"Falling back to company-by-company live search across {len(companies)} companies")
        combined: list[dict[str, str]] = []
        for index, company in enumerate(companies, start=1):
            company_name = (
                company.get("label")
                or company.get("post_title")
                or company.get("title")
                or company.get("name")
                or f"company #{index}"
            )
            log(f"Searching company {index}/{len(companies)}: {company_name}")
            search_params = build_default_search_params()
            search_params["company"] = company
            batch = run_live_product_search(
                session=session,
                api_url=config.api_url,
                search_params=search_params,
                label=f"company={company_name}",
            )
            if batch:
                combined.extend(batch)
                log(f"Company search added {len(batch)} records. Running total: {len(combined)}")

        deduped = dedupe_records(combined)
        if deduped:
            return deduped

    log("Falling back to broad product-name bootstrap search")
    combined = []
    for seed in list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"):
        search_params = build_default_search_params()
        search_params["product_name"] = seed
        batch = run_live_product_search(
            session=session,
            api_url=config.api_url,
            search_params=search_params,
            label=f"product_name starts with {seed}",
        )
        if batch:
            combined.extend(batch)
            log(f"Seed {seed} added {len(batch)} records. Running total: {len(combined)}")

    return dedupe_records(combined)


def flatten_live_company_results(companies: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    flattened: list[dict[str, str]] = []
    for company in companies:
        if not isinstance(company, dict):
            continue

        products = company.get("products")
        if isinstance(products, list):
            for product in products:
                if isinstance(product, dict):
                    merged = dict(product)
                    merged.setdefault("company_title", first_non_empty(company, "post_title", "company_title", "label", "title", "name"))
                    merged.setdefault("manufacturer", merged.get("company_title"))
                    flattened.append(normalize_record(merged))
        else:
            flattened.append(normalize_record(company))
    return flattened


def try_playwright_download() -> list[dict[str, str]]:
    log("Falling back to Playwright")
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeError(
            "Playwright fallback is unavailable because the `playwright` package is not installed."
        ) from exc

    with TemporaryDirectory(prefix="onvif_playwright_") as tmpdir:
        download_dir = Path(tmpdir)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(PUBLIC_PAGE_URL, wait_until="networkidle", timeout=120_000)

            for selector in [
                "button:has-text('Agree')",
                "button:has-text('I agree')",
                "button[aria-label='Agree']",
            ]:
                try:
                    page.locator(selector).first.click(timeout=2_000)
                    break
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue

            search_clicked = False
            for selector in [
                "button:has-text('Search')",
                "input[type='submit'][value='Search']",
                "#submit-search",
            ]:
                try:
                    page.locator(selector).first.click(timeout=5_000)
                    search_clicked = True
                    break
                except Exception:
                    continue
            if search_clicked:
                log("Playwright clicked Search")
                page.wait_for_load_state("networkidle", timeout=120_000)

            with page.expect_download(timeout=120_000) as download_info:
                clicked = False
                for selector in [
                    "a:has-text('Export results to CSV')",
                    "button:has-text('Export results to CSV')",
                    "text='Export results to CSV'",
                ]:
                    try:
                        page.locator(selector).first.click(timeout=5_000)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise ScrapeError("Playwright could not find the export button.")

            download = download_info.value
            target_path = download_dir / (download.suggested_filename or "onvif_export.csv")
            download.save_as(str(target_path))
            log(f"Playwright downloaded CSV: {target_path}")

            browser.close()

        return parse_export_csv(target_path)


def parse_export_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [normalize_csv_row(row) for row in reader]
    return dedupe_records(rows)


def normalize_csv_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "product_name": stringify(row.get("Product name")),
        "manufacturer": stringify(row.get("Company name") or row.get("Manufacturer")),
        "application_type": stringify(row.get("Application type")),
        "profiles": stringify(row.get("Profiles supported")),
        "addons": stringify(row.get("Add-ons") or row.get("Addons")),
        "firmware_version": stringify(row.get("Firmware Version")),
        "certification_date": stringify(row.get("Date certified")),
    }


def normalize_record(record: dict[str, Any]) -> dict[str, str]:
    profiles = join_value(
        first_non_none(
            record.get("product_profiles"),
            record.get("profiles"),
            record.get("profile"),
        )
    )
    addons = join_value(
        first_non_none(
            record.get("product_addons"),
            record.get("addons"),
            record.get("add_ons"),
            record.get("product_add_ons"),
        )
    )
    return {
        "product_name": stringify(
            first_non_empty(record, "product_name", "post_title", "title", "name")
        ),
        "manufacturer": stringify(
            first_non_empty(
                record,
                "manufacturer",
                "company_title",
                "company_name",
                "member_company",
                "post_parent_title",
            )
        ),
        "application_type": stringify(
            first_non_empty(record, "application_type", "type", "app_type", "category")
        ),
        "profiles": profiles,
        "addons": addons,
        "firmware_version": stringify(
            first_non_empty(
                record,
                "firmware_version",
                "product_firmware_version",
                "firmware",
                "software_version",
            )
        ),
        "certification_date": stringify(
            first_non_empty(
                record,
                "certification_date",
                "test_date",
                "date_certified",
                "certified_on",
            )
        ),
    }


def first_non_empty(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return ""


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("label", "title", "name", "post_title", "value", "id"):
            if key in value and value[key] not in (None, ""):
                return stringify(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        return join_value(value)
    return str(value).strip()


def join_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [stringify(item) for item in value]
        return ", ".join(part for part in parts if part)
    return stringify(value)


def safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def dedupe_records(records: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen = set()
    for record in records:
        normalized = {key: stringify(record.get(key, "")) for key in REQUIRED_KEYS}
        marker = tuple(normalized[key] for key in REQUIRED_KEYS)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(normalized)
    return deduped


def write_outputs(records: list[dict[str, str]]) -> None:
    OUTPUT_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_KEYS)
        writer.writeheader()
        writer.writerows(records)

    log(f"Wrote {len(records)} records to {OUTPUT_JSON.resolve()}")
    log(f"Wrote {len(records)} records to {OUTPUT_CSV.resolve()}")


def main() -> int:
    session = build_session()

    try:
        _, config = fetch_public_page(session)

        records = probe_legacy_actions(session, config.nonce, config.ajaxurl)
        if not records:
            records = try_live_paths(session, config)
        if not records:
            records = try_playwright_download()
        if not records:
            raise ScrapeError("No records were fetched from any strategy.")

        records = dedupe_records(records)
        write_outputs(records)
        log(f"Done. Total unique records: {len(records)}")
        return 0
    except KeyboardInterrupt:
        log("Interrupted by user.")
        return 130
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
