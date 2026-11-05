"""Test doubles standing in for sb_cdp.Chrome."""
import os

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class FakeBrowser:
    """Serves canned (title, html) per URL, or routes by URL pattern."""

    def __init__(self, pages=None, router=None):
        self.pages = pages or {}
        self.router = router
        self.current = ""
        self.visited = []
        self.clicked = []
        self.solved = 0
        self.quit_called = False

    def goto(self, url):
        self.current = url
        self.visited.append(url)

    def _page(self):
        if self.current in self.pages:
            return self.pages[self.current]
        if self.router is not None:
            return self.router(self.current)
        return ("", "")

    def get_current_url(self):
        return self.current

    def get_title(self):
        return self._page()[0]

    def get_page_source(self):
        return self._page()[1]

    def is_element_visible(self, selector):
        return False

    def click(self, selector):
        self.clicked.append(selector)

    def solve_captcha(self):
        self.solved += 1

    def save_screenshot(self, *a, **k):
        pass

    def scroll_down(self, n):
        pass

    def evaluate(self, js):
        return 3

    def quit(self):
        self.quit_called = True


def google_router(url):
    """Fixture pages for the URLs the scrapers build."""
    if "/maps/place/" in url:
        return ("Place", load_fixture("google_maps_place.html"))
    if "/maps/search/" in url:
        return ("Google Maps", load_fixture("google_maps_feed.html"))
    if "yelp.com" in url:
        return ("Yelp", load_fixture("yelp_listing.html"))
    if "google.com/search" in url:
        return ("q - Google Search", load_fixture("google_serp_desktop.html"))
    return ("", "<html></html>")
