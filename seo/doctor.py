"""Environment checks so a first run fails with a reason, not a trace."""
import os
import platform
import shutil
import sys

PASS, WARN, FAIL = "ok", "warn", "fail"


def _chrome_path():
    try:
        from seleniumbase import undetected
        return undetected.find_chrome_executable()
    except Exception:
        return None


def _seleniumbase_version():
    try:
        import seleniumbase
        return seleniumbase.__version__
    except ImportError:
        return None


def _bs4_available():
    try:
        import bs4  # noqa: F401
        return True
    except ImportError:
        return False


def _tkinter_available():
    import importlib.util
    return importlib.util.find_spec("tkinter") is not None


def checks(config_path, data_dir="data"):
    """Return a list of dicts: name, status (ok/warn/fail), detail, fix."""
    out = []

    ver = sys.version_info
    out.append(dict(
        name="Python", status=PASS if ver >= (3, 9) else FAIL,
        detail=platform.python_version(),
        fix="Install Python 3.9 or newer."))

    sb_version = _seleniumbase_version()
    if sb_version:
        out.append(dict(name="SeleniumBase", status=PASS, detail=sb_version,
                        fix=""))
    else:
        out.append(dict(
            name="SeleniumBase", status=FAIL, detail="not installed",
            fix="pip install -U seleniumbase   (upgrade pip, wheel and "
                "setuptools first if the install fails to build)"))

    if _bs4_available():
        out.append(dict(name="BeautifulSoup", status=PASS, detail="", fix=""))
    else:
        out.append(dict(name="BeautifulSoup", status=FAIL,
                        detail="not installed",
                        fix="pip install beautifulsoup4"))

    chrome = _chrome_path()
    out.append(dict(
        name="Chrome", status=PASS if chrome else FAIL,
        detail=chrome or "not found",
        fix="Install Google Chrome, or pass --chrome-binary /path/to/chrome"))

    if sys.platform.startswith("linux"):
        xvfb = shutil.which("Xvfb")
        display = os.environ.get("DISPLAY")
        if xvfb or display:
            out.append(dict(name="Display", status=PASS,
                            detail=xvfb or "DISPLAY=%s" % display, fix=""))
        else:
            out.append(dict(
                name="Display", status=WARN,
                detail="no Xvfb and no DISPLAY",
                fix="sudo apt-get install -y xvfb   (or run with --headless, "
                    "which is easier for sites to detect)"))
        if _tkinter_available():
            out.append(dict(name="tkinter", status=PASS, detail="", fix=""))
        else:
            out.append(dict(
                name="tkinter", status=FAIL,
                detail="missing; SeleniumBase's Xvfb mode exits without it",
                fix="sudo apt-get install -y python3-tk python3-dev   "
                    "(or always pass --headless)"))
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            out.append(dict(
                name="Root user", status=WARN,
                detail="running as root; Chrome sandbox will be disabled "
                       "automatically", fix=""))

    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".write_test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        out.append(dict(name="Data directory", status=PASS,
                        detail=os.path.abspath(data_dir), fix=""))
    except OSError as e:
        out.append(dict(name="Data directory", status=FAIL, detail=str(e),
                        fix="Run from a writable folder or set --db"))

    if os.path.exists(config_path):
        out.append(dict(name="Config", status=PASS, detail=config_path,
                        fix=""))
    else:
        out.append(dict(
            name="Config", status=WARN, detail="%s not found" % config_path,
            fix='seo init --location "Austin, TX" --serp "plumber"'))
    return out


def browser_check(session_factory, **session_kwargs):
    """Open the stealth browser against SeleniumBase's own demo page."""
    try:
        with session_factory(**session_kwargs) as s:
            s.goto("https://seleniumbase.io/demo_page", check_google=False)
            title = s.title() or ""
            ua = ""
            try:
                ua = s.sb.get_user_agent()
            except Exception:
                pass
        if "Web Testing Page" in title:
            detail = "loaded demo page"
            if ua:
                detail += "; UA %s" % ua[:60]
            return dict(name="Browser", status=PASS, detail=detail, fix="")
        return dict(name="Browser", status=FAIL,
                    detail="unexpected title %r" % title,
                    fix="Check network access to seleniumbase.io")
    except (Exception, SystemExit) as e:
        return dict(
            name="Browser", status=FAIL, detail=str(e)[:200],
            fix="Make sure Chrome is installed; on Linux without a desktop "
                "install xvfb and python3-tk, or pass --headless")


def geocoder_check():
    from seo import geo
    try:
        lat, lng, _, canonical = geo.geocode("Austin, TX")
        return dict(name="Geocoder", status=PASS,
                    detail="%s -> %.2f,%.2f" % (canonical, lat, lng), fix="")
    except Exception as e:
        return dict(name="Geocoder", status=WARN, detail=str(e)[:120],
                    fix="Nominatim unreachable; pass --lat/--lng instead")


def render(results):
    lines = []
    width = max(len(r["name"]) for r in results)
    for r in results:
        mark = {PASS: "OK  ", WARN: "WARN", FAIL: "FAIL"}[r["status"]]
        lines.append("%s  %-*s  %s" % (mark, width, r["name"], r["detail"]))
        if r["status"] != PASS and r["fix"]:
            lines.append("      -> %s" % r["fix"])
    return "\n".join(lines)
