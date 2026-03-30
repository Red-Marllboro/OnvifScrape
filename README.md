# ONVIF Conformant Products Scraper

This repository contains a Python scraper for the ONVIF conformant products catalog and the resulting exported dataset.

## Included Files

- `scrape_onvif_products.py`: main scraper script
- `requirements.txt`: Python dependencies
- `onvif_conformant_products.csv`: normalized CSV export
- `onvif_conformant_products.json`: normalized JSON export

## What The Scraper Does

The scraper uses a layered approach because the ONVIF conformant products page is JavaScript-rendered and its backend behavior has changed over time:

1. Fetches `https://www.onvif.org/conformant-products/`
2. Extracts the nonce embedded in the page HTML
3. Probes the legacy WordPress `admin-ajax.php` actions
4. Falls back to the currently working ONVIF member-tools search flow
5. Falls back again to Playwright-driven CSV export if the HTTP-based methods fail

All products are normalized into this flat schema:

- `product_name`
- `manufacturer`
- `application_type`
- `profiles`
- `addons`
- `firmware_version`
- `certification_date`

## Run It

Install dependencies:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
```

Run the scraper:

```powershell
py scrape_onvif_products.py
```

## Output

Running the script writes:

- `onvif_conformant_products.csv`
- `onvif_conformant_products.json`

Both files are written to the repository root.

## Notes

- The script prints progress while it fetches records.
- It uses `requests` for the primary scraping path.
- Playwright is only used as a fallback.
- The included dataset was generated from the live ONVIF site during this project session.
