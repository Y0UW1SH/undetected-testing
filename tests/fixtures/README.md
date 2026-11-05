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
* `live_amazon_search.html`: the 16 result blocks of a real Amazon
  search for "wireless mouse" (attributes pruned to those the parser
  uses), captured with the workflow's `capture_extra` input.
* `live_taostats_subnets.html`: the AG Grid of taostats.io/subnets (one
  virtualised page of 25 rows), same capture run. eBay and Reddit were
  captured too but both served block pages to the Actions IP, so their
  fixtures remain hand-written.

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
