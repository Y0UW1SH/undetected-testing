# Product research: Amazon and eBay

`research` is the product-research side of this repository. It uses the
same stealth browser as the `seo` command but has its own command, config,
database (`data/research.sqlite`), schedule and report, because tracking
listings and prices is a different job from tracking search rankings.

## Quick start

```bash
python -m pip install --upgrade pip wheel setuptools
pip install "git+https://github.com/Y0UW1SH/undetected-testing@master"

seo doctor --browser                 # same environment check as the SEO tool
research init --amazon "wireless mouse" --ebay "wireless mouse"
research track                       # fetch everything in the config
research report                      # data/research_report.html
```

`research init` with no flags copies `seo/config/research.example.json`
to `seo/config/research.json`:

```json
{
  "amazon": ["wireless mouse", "mechanical keyboard"],
  "ebay": ["wireless mouse"],
  "pages": 1,
  "alert_threshold_pct": 10
}
```

## Commands

```bash
research amazon "wireless mouse" --pages 2      # one query, prints a table
research ebay "wireless mouse" --site www.ebay.co.uk
research track --threshold 5 --on-challenge skip
research alerts --threshold 5                   # compare the last two runs
research report --out data/research_report.html
research export --format csv                    # data/research_listings.csv
```

Global options: `--db PATH` (or `$RESEARCH_DB`), `--chrome-binary PATH`
(or `$SEO_CHROME`), `--version`. Per-command: `--proxy user:pass@host:port`,
`--headless`, `--force` (refetch a query already fetched today).

Exit codes: 0 something ran, 1 only errors, 2 missing config, 3 the site
served a bot wall for every session, 4 browser stack not installed (run
`seo doctor`).

## What you get

Each run stores every result with position, title, price, currency,
rating, review count, sponsored flag, Prime (Amazon), condition and
shipping (eBay), ASIN or eBay item number and a canonical URL.

**Alerts** compare the latest run of a query with the previous one:

* `price_up` / `price_down`: the same item moved more than the threshold
  (percent, default 10).
* `new`: an item is in the results now but was not last time.
* `gone`: an item dropped out of the results.

`research track` prints them, writes `data/research_alerts.json`, and on
GitHub Actions also emits `::warning::` annotations so they show on the
run summary.

## Schedule

`.github/workflows/research.yml` runs `research track` daily, then the
report and export, uploads `data/` as an artifact and commits the database,
alerts, CSV and report back to the repository so each run can compare
against the last. Dispatch it manually with a different config path or
threshold from the Actions tab. Set a `SEO_PROXY` secret
(`user:pass@host:port`) to route through a proxy.

## Notes

* Amazon shows a "Robot Check" page and eBay a "Pardon Our Interruption"
  page when they suspect automation. Both are detected and reported as
  exit code 3 instead of silently saving an empty result.
* Prices depend on the site's idea of your location; use `--proxy` from
  the market you care about.
