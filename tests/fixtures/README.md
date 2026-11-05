# Parser fixtures

Files prefixed `live_` were captured from real Google pages with a
headless browser (scripts, styles and image sources stripped):

* `live_maps_feed.html`: Maps results feed for "plumber austin tx"
* `live_maps_place.html`: a Maps place page (service-area business, so
  it has no street address)
* `live_google_sorry.html`: the "unusual traffic" challenge page that
  Google web search served to the same headless browser
* `live_google_serp.html`: a real web-search results page for "emergency
  plumber austin", captured by the `seo.yml` workflow's `capture_serp`
  input from a GitHub Actions runner (main column only). Note the
  `/goto?url=` redirect links and the label-based local pack.

The other files are **hand-written** to mirror the structural hooks the
parsers rely on (`a[href] h3`, `[data-q]`, `[data-cid]`, `#tads`,
`div[role="feed"]`, `data-item-id`, ...). Refresh fixtures whenever
Google changes markup, locally or by dispatching the SEO tracking
workflow with a `capture_serp` query and downloading the artifact:

    python -m seo capture serp "emergency plumber austin" \
        --out tests/fixtures/google_serp_desktop.html
    python -m seo capture maps "plumber austin tx" \
        --out tests/fixtures/google_maps_feed.html

then re-run `pytest tests/` and fix whichever selectors broke.
