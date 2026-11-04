# Parser fixtures

Files prefixed `live_` were captured from real Google pages with a
headless browser (scripts, styles and image sources stripped):

* `live_maps_feed.html`: Maps results feed for "plumber austin tx"
* `live_maps_place.html`: a Maps place page (service-area business, so
  it has no street address)
* `live_google_sorry.html`: the "unusual traffic" challenge page that
  Google web search served to the same headless browser

The other files are **hand-written** to mirror the structural hooks the
parsers rely on (`a[href] h3`, `[data-q]`, `[data-cid]`, `#tads`,
`div[role="feed"]`, `data-item-id`, ...). No live web-search SERP has
been captured yet because Google challenged the datacenter IP used for
capture; the first successful `python -m seo capture serp ...` from a
clean IP should be added here. Refresh fixtures whenever Google changes
markup:

    python -m seo capture serp "emergency plumber austin" \
        --out tests/fixtures/google_serp_desktop.html
    python -m seo capture maps "plumber austin tx" \
        --out tests/fixtures/google_maps_feed.html

then re-run `pytest tests/` and fix whichever selectors broke.
