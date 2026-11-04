# Parser fixtures

These HTML files are **hand-written** to mirror the structural hooks the
parsers rely on (`a[href] h3`, `[data-q]`, `[data-cid]`, `#tads`,
`div[role="feed"]`, `data-item-id`, ...). They are not captured from a
live browser. Refresh them with real pages whenever Google changes
markup:

    python -m seo capture serp "emergency plumber austin" \
        --out tests/fixtures/google_serp_desktop.html
    python -m seo capture maps "plumber austin tx" \
        --out tests/fixtures/google_maps_feed.html

then re-run `pytest tests/` and fix whichever selectors broke.
