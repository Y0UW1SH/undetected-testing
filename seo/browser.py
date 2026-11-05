"""A single place that opens the stealth browser used by every scraper.

Wraps `seleniumbase.sb_cdp.Chrome` so that locale, device, proxy, and
geolocation are set consistently, and adds Google-specific handling for
the consent dialog and the 'unusual traffic' CAPTCHA page."""
import os
import random
import sys
import time
from contextlib import suppress

from seo.models import ChallengeError, MissingDependency

CONSENT_BUTTONS = (
    'button#L2AGLb',                       # "Accept all" (EU consent)
    'button:contains("Accept all")',
    'button:contains("Reject all")',
    'button[aria-label="Accept all"]',
    'form[action*="consent"] button',
)

CHALLENGE_MARKERS = (
    "/sorry/",
    "unusual traffic",
    "systems have detected",
)


class Session:
    """Thin wrapper over an sb_cdp.Chrome browser.

    Usage:
        with Session(location=(30.26, -97.74), mobile=False) as s:
            s.goto("https://www.google.com/...")
            html = s.html()
    """

    def __init__(
        self,
        geoloc=None,          # (lat, lng) or None
        mobile=False,
        proxy=None,
        lang="en",
        tzone=None,
        ad_block=True,
        min_pause=1.5,
        max_pause=4.0,
        max_solve_attempts=1,
        headless=False,
        browser=None,         # injectable for tests
        **chrome_kwargs,      # passed straight to sb_cdp.Chrome()
    ):
        self.geoloc = tuple(geoloc) if geoloc else None
        self.mobile = mobile
        self.proxy = proxy
        self.lang = lang
        self.tzone = tzone
        self.ad_block = ad_block
        self.min_pause = min_pause
        self.max_pause = max_pause
        self.max_solve_attempts = max_solve_attempts
        self.headless = headless
        self.chrome_kwargs = chrome_kwargs
        self.sb = browser
        self.pages_loaded = 0

    # ---- lifecycle -----------------------------------------------------
    def open(self):
        if self.sb is None:
            try:
                from seleniumbase import sb_cdp
            except ImportError as e:
                raise MissingDependency(
                    "SeleniumBase is not installed (%s). Run `seo doctor` "
                    "for the fix." % e) from e
            kwargs = dict(lang=self.lang, ad_block=self.ad_block)
            if sys.platform.startswith("linux") and hasattr(os, "geteuid") \
                    and os.geteuid() == 0:
                # Chrome refuses to start sandboxed as root (Docker, VPS).
                kwargs["sandbox"] = False
            if self.mobile:
                kwargs["mobile"] = True
            if self.proxy:
                kwargs["proxy"] = self.proxy
            if self.geoloc:
                kwargs["geoloc"] = list(self.geoloc)
            if self.tzone:
                kwargs["tzone"] = self.tzone
            if self.headless:
                # Headless is easier to detect; prefer Xvfb (the default
                # on Linux) unless there is really no display available.
                kwargs["headless"] = True
            kwargs.update(self.chrome_kwargs)
            try:
                self.sb = sb_cdp.Chrome(**kwargs)
            except SystemExit as e:
                # SeleniumBase's Xvfb path imports PyAutoGUI, which calls
                # sys.exit() when tkinter is missing. Turn that into an
                # error the CLI can explain instead of a silent exit 1.
                raise MissingDependency(
                    "The browser stack refused to start: %s. On Linux "
                    "install tkinter (sudo apt-get install -y python3-tk "
                    "python3-dev) or pass --headless." % str(e)[:200]
                ) from None
            except Exception as e:
                raise MissingDependency(
                    "Chrome could not be started (%s). Run `seo doctor "
                    "--browser`; on Linux without a desktop install xvfb "
                    "or pass --headless." % str(e)[:200]) from e
        return self

    def close(self):
        if self.sb is not None:
            with suppress(Exception):
                self.sb.quit()
            self.sb = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ---- navigation ----------------------------------------------------
    def pause(self, scale=1.0):
        """Human-ish random pause between actions."""
        time.sleep(random.uniform(self.min_pause, self.max_pause) * scale)

    def goto(self, url, check_google=True, blocked=None):
        """Load a page. `blocked(url, title, html)` may return a reason
        string when the site served a bot wall; it raises ChallengeError
        after one solve_captcha attempt, like the Google check does."""
        self.sb.goto(url)
        self.pages_loaded += 1
        self.pause(0.6)
        if check_google and "google." in url:
            self.dismiss_consent()
            self.ensure_not_challenged()
        if blocked is not None:
            reason = blocked(self.url(), self.title(), self.html())
            if reason:
                with suppress(Exception):
                    self.sb.solve_captcha()
                time.sleep(2.0)
                reason = blocked(self.url(), self.title(), self.html())
                if reason:
                    self.screenshot("blocked")
                    raise ChallengeError("%s at %s" % (reason, self.url()))
        return self

    def set_geolocation(self, lat, lng):
        """Change the emulated position mid-session (takes effect on the
        next page load). Used by the grid rank tracker."""
        self.geoloc = (lat, lng)
        self.sb.loop.run_until_complete(
            self.sb.page.set_geolocation((lat, lng))
        )

    def scroll(self, times=5, amount=20, pause=0.25):
        for _ in range(times):
            self.sb.scroll_down(amount)
            time.sleep(pause)

    # ---- page access ---------------------------------------------------
    def html(self):
        return self.sb.get_page_source()

    def url(self):
        return self.sb.get_current_url()

    def title(self):
        return self.sb.get_title()

    def screenshot(self, name="page", folder="downloaded_files"):
        with suppress(Exception):
            self.sb.save_screenshot("%s.png" % name, folder)

    # ---- Google-specific -----------------------------------------------
    def dismiss_consent(self):
        for selector in CONSENT_BUTTONS:
            with suppress(Exception):
                if self.sb.is_element_visible(selector):
                    self.sb.click(selector)
                    time.sleep(1.0)
                    return True
        return False

    def is_challenged(self):
        return is_challenge_page(self.url(), self.title(), self.html())

    def ensure_not_challenged(self):
        """Try to clear a Google challenge once; raise if it persists."""
        if not self.is_challenged():
            return
        for _ in range(self.max_solve_attempts):
            with suppress(Exception):
                self.sb.solve_captcha()
            time.sleep(2.0)
            if not self.is_challenged():
                return
        self.screenshot("challenge")
        raise ChallengeError("Google served a challenge at %s" % self.url())


def is_challenge_page(url, title="", html=""):
    """Pure function so it can be unit-tested without a browser."""
    url = url or ""
    title = title or ""
    if "/sorry/" in url or "google.com/sorry" in url:
        return True
    if "Before you continue" in title and "consent" in url:
        return False  # consent dialog, handled separately
    sample = (html or "")[:20000]
    return any(marker in sample for marker in CHALLENGE_MARKERS[1:])
