# undetected-testing

Testing [SeleniumBase](https://github.com/seleniumbase/SeleniumBase) stealth for bypassing bot-detection.

E.g. Scraping Walmart from GitHub Actions:

https://github.com/mdmintz/undetected-testing/actions/runs/37512407751/job/112436648975#step:12:13

<img width="650" alt="Image" src="https://github.com/user-attachments/assets/86545cd2-a00e-42bb-800a-53da59ca1bd0" />

--------

E.g. Scraping Etsy from GitHub Actions:

https://github.com/mdmintz/undetected-testing/actions/runs/40770290055/job/123105334967#step:13:15

<img width="660" alt="Image" src="https://github.com/user-attachments/assets/1b3c46c9-d05b-4c13-a901-532910ff6d99" />

--------

SeleniumBase CDP Mode bypasses bot-detection on multiple sites:

<img width="568" alt="Undetected SeleniumBase" src="https://user-images.githubusercontent.com/6788579/213031519-0220d19f-e210-4858-b6b5-9253b112efe4.png">

--------

## How the CI works

All checks live in one workflow, `.github/workflows/ci.yml`:

* A `lint` job runs flake8.
* A `stealth` matrix job runs groups of `raw_*.py` scripts on Ubuntu x86,
  Ubuntu ARM64, and macOS. Each group is one matrix entry with a list of
  scripts; add a script to CI by appending a line to the right group.
* Every script ends with an assertion (`sb.assert_true(...)`,
  `sb.assert_element(...)`, or a plain `assert`) that fails if the site
  blocked the browser or returned no data, so a green run means stealth
  actually worked, not just that the script exited.
* All scripts in a group run even if one fails; the failures are listed at
  the end of the step. Screenshots and logs are uploaded as artifacts.

Run a script locally the same way CI does:

```bash
pip install -r requirements.txt
seleniumbase install chromedriver
python raw_walmart.py --debug
```

--------

## SERP and local-SEO scraper (`seo/`)

The stealth browser these tests exercise is also wrapped as a small
scraping tool. Everything goes through `seo/browser.py`, which opens a
SeleniumBase CDP-mode Chrome with locale, device, proxy and emulated
geolocation set, dismisses Google's consent dialog, and raises
`ChallengeError` when Google serves its "unusual traffic" CAPTCHA page.

```bash
# Google web results for a query as seen from a city (uule + geolocation)
python -m seo serp "emergency plumber" --location "Austin, TX"
python -m seo serp "plumber near me" --location "Austin, TX" --mobile

# Google Maps listings, optionally visiting each place for phone/site/hours
python -m seo local "plumber" --location "Austin, TX" --details

# Local rank grid: where does one business rank across a 5x5 km grid?
python -m seo grid "plumber" --target "Joe's Plumbing" \
    --location "Austin, TX" --size 5 --spacing 1.5
# -> data/grid_joes_plumbing.html heatmap

# Competitor snapshot (top 10 on Maps) with a diff against the last run
python -m seo snapshot "plumber" --location "Austin, TX"

# NAP consistency: does each directory list the same phone and address?
python -m seo nap "Joe's Plumbing" --phone 512-555-0100 \
    --address "123 Main St, Austin, TX 78701" --city "Austin, TX"

# Everything in a JSON config, new browser every 3 jobs, stop on challenge
cp seo/config/keywords.example.json seo/config/keywords.json
python -m seo batch --config seo/config/keywords.json --per-session 3

# How often does Google challenge this IP? (exit 1 above 50%)
python -m seo canary --queries 20 --fail-above 50

python -m seo export --format csv     # data/*.csv from data/seo.sqlite
```

Results are stored in `data/seo.sqlite` (one table per record type) and
a query is skipped if it was already fetched today unless `--force` is
given. `.github/workflows/seo.yml` runs the canary plus the batch config
daily and commits the CSV exports. Set a `SEO_PROXY` repository secret
(`user:pass@host:port`) to route the scraper through a proxy.

Parsers are unit-tested offline against HTML in `tests/fixtures/`
(`pytest tests/`). The Maps feed, Maps place page and Google's
"unusual traffic" page are real captures (`live_*.html`); the web-search
SERP fixture is hand-written because Google challenged the datacenter
IP used for capture. Refresh fixtures from live pages with
`python -m seo capture serp "query" --out tests/fixtures/x.html` when
Google changes its markup.

On Linux the browser runs under Xvfb by default, which is harder to
detect than headless mode. Pass `--headless` only where no display is
possible at all.

Scraping Google is against its Terms of Service; keep volumes modest,
expect CAPTCHAs from datacenter IPs (including GitHub Actions), and
prefer the official Places API for Maps business details at scale.
