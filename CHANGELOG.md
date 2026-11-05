# Changelog

## v0.1.0 (2026-11-05)

First release of the `seo` tool on top of the SeleniumBase stealth
test-suite in this fork.

### Install

```bash
python -m pip install --upgrade pip wheel setuptools
pip install "git+https://github.com/Y0UW1SH/undetected-testing@v0.1.0"
# or, without a tag: ...undetected-testing@master
seo doctor --browser
```

### What it does

* `seo serp QUERY --location "City, ST"`: Google web results as seen from
  that city (uule + emulated geolocation, desktop or `--mobile`), parsed
  into organic results, ads, People Also Ask, featured snippet and the
  local 3-pack.
* `seo local QUERY --location ...`: Google Maps listings with rating,
  reviews, category, address, phone, website, CID and coordinates;
  `--details` visits each place page for hours and full address.
* `seo grid QUERY --target "Your Business"`: organic rank across an
  N x N grid of points around the city, with an HTML heatmap.
* `seo snapshot QUERY`: top competitors on Maps and what changed since
  the last run.
* `seo nap "Business" --phone ... --address ...`: phone/address
  consistency across directory sites found through Google.
* `seo batch`: everything in a JSON config, one browser per
  location/device group, rotated every N jobs, stop on challenge.
* `seo canary`: how often Google challenges this IP.
* `seo doctor`, `seo init`, `seo report`, `seo export`, `seo capture`.

### Verified

* 57 offline tests against hand-written and real captured Google pages
  (Maps feed, Maps place page, Google's challenge page).
* Live: Maps feed and place parsing on 15 Austin plumbers; mid-session
  geolocation override; challenge detection on the real "unusual
  traffic" page.

### Known limits

* The web-search SERP parser has only been tested against hand-written
  HTML; Google challenged every datacenter IP used for capture. Capture
  one from a home connection with
  `seo capture serp "query" --out tests/fixtures/live_google_serp.html`.
* Scraping Google is against its Terms of Service. Keep volumes low,
  expect CAPTCHAs from cloud IPs, and use the official Places API for
  Maps business data at scale.
* GitHub Actions runners are cloud IPs: Maps generally works, web search
  is often challenged. The daily `seo.yml` workflow records the rate.
* bot.sannysoft.com reports one failed fingerprint check on GitHub
  runners (x86 and ARM64), so `raw_cdp_mode.py` is not part of the CI
  matrix; run it locally.

### Product research tool (`research`)

* `research amazon QUERY` / `research ebay QUERY`: search results with
  ASIN or item number, price, rating, review count, sponsored flag
  (Amazon: Prime; eBay: condition and shipping).
* `research track` runs a config of queries on a schedule and raises
  price-change alerts between runs (`research alerts`, threshold in
  percent); `research report` renders them. Own database
  (`data/research.sqlite`) and workflow (`research.yml`).

### Bittensor subnet tool (`tao`)

* `tao subnets` collects every subnet's price, 24h/7d change, market
  cap, emission and pool liquidity from taostats.io; `tao watch`
  compares with the previous run and warns on moves beyond a threshold;
  `tao report` renders the table and alerts. Own database
  (`data/tao.sqlite`) and 4-hourly workflow (`tao.yml`).
* The taostats parser is structural (header-name based) and was
  written before a real page capture; the `seo capture taostats`
  command and the SEO workflow's `capture_extra` input exist to refresh
  it against the live page.

### Reddit (under `seo`)

* `seo reddit QUERY [--subreddit] [--sort] [--time]`: posts from
  Reddit's search (JSON endpoint in the stealth browser, HTML fallback)
  with subreddit, score, comments and permalink; `reddit` jobs in the
  batch config; a Reddit section in `seo report`.

### Also in this release

* The stealth test scripts now assert success, and the ten CI workflows
  are one matrix workflow with lint and unit-test jobs. `SB()` scripts
  pass `rtf=True` so a failed assertion exits non-zero (SeleniumBase's
  test mode otherwise prints the failure and exits 0). Each script gets
  a 10-minute limit in CI; Etsy's slider captcha once hung a macOS job
  for 45 minutes.
