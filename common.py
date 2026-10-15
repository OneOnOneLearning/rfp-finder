"""Shared helpers: settings, polite web requests, document reading, scoring."""

import hashlib
import io
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

CONFIG = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
TODAY = date.today()
NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")
UA = f"OOOL-RFP-Finder/2.0 (+mailto:{CONFIG.get('contact_email', '')})"
DOC_EXT = (".pdf", ".doc", ".docx")


LOG_LINES = []


def log(msg):
    print(msg, flush=True)
    LOG_LINES.append(str(msg))


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def make_id(*parts):
    return hashlib.sha1("|".join(p or "" for p in parts).lower().encode()).hexdigest()[:16]


# ------------------------------------------------------------ web requests
_local = threading.local()
_lock = threading.Lock()
_next_ok = {}        # domain -> earliest time we may hit it again
_robots = {}         # base url -> RobotFileParser or None
_browser_lock = threading.Lock()
_browser = None


class Blocked(Exception):
    """The site's robots.txt asks bots not to visit this page."""


def session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        _local.s = s
    return _local.s


def polite_wait(url):
    domain = urlparse(url).netloc.lower()
    delay = CONFIG.get("delay_seconds", 2)
    with _lock:
        now = time.time()
        start = max(now, _next_ok.get(domain, 0))
        _next_ok[domain] = start + delay
    if start > now:
        time.sleep(start - now)


def robots_ok(url, force=False):
    if force or not CONFIG.get("respect_robots_txt", True):
        return True
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    with _lock:
        known = base in _robots
    if not known:
        rp = robotparser.RobotFileParser()
        try:
            r = session().get(base + "/robots.txt", timeout=15)
            if r.status_code >= 400:
                rp = None
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp = None
        with _lock:
            _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)


def fetch(url, method="GET", data=None, binary=False, max_bytes=None, timeout=40, force=False):
    if not robots_ok(url, force):
        raise Blocked(f"robots.txt asks bots not to visit {urlparse(url).netloc}")
    for attempt in range(3):
        polite_wait(url)
        r = session().request(method, url, data=data, timeout=timeout, stream=bool(max_bytes))
        if r.status_code != 429 or attempt == 2:
            break
        try:
            wait = min(int(r.headers.get("Retry-After", "20")), 60)
        except ValueError:
            wait = 20
        time.sleep(wait * (attempt + 1))  # site says "slow down": wait and try again
    r.raise_for_status()
    if max_bytes:
        buf = io.BytesIO()
        for chunk in r.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > max_bytes:
                break
        return buf.getvalue()
    if binary:
        return r.content
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def fetch_final_url(url, timeout=20):
    """GET a page and return (final_url, html) after redirects."""
    if not robots_ok(url):
        raise Blocked(f"robots.txt asks bots not to visit {urlparse(url).netloc}")
    polite_wait(url)
    r = session().get(url, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.url, r.text


def fetch_rendered(url):
    """Open a page in a headless browser (for sites built with JavaScript)."""
    global _browser
    if not robots_ok(url):
        raise Blocked(f"robots.txt asks bots not to visit {urlparse(url).netloc}")
    with _browser_lock:
        if _browser is None:
            from playwright.sync_api import sync_playwright
            _browser = sync_playwright().start().chromium.launch()
        polite_wait(url)
        page = _browser.new_page(user_agent=UA)
        try:
            page.goto(url, timeout=60000, wait_until="networkidle")
            page.wait_for_timeout(2500)
            return page.content()
        finally:
            page.close()


def close_browser():
    global _browser
    if _browser:
        _browser.close()
        _browser = None


def doc_text(url, pages=12):
    """Download a PDF/Word file and return its text (first pages)."""
    raw = fetch(url, binary=True, max_bytes=10_000_000)
    low = url.lower().split("?")[0]
    try:
        if low.endswith(".pdf") or raw[:4] == b"%PDF":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            return clean(" ".join((pg.extract_text() or "") for pg in reader.pages[:pages]))
        if low.endswith(".docx") or raw[:2] == b"PK":
            from docx import Document
            d = Document(io.BytesIO(raw))
            return clean(" ".join(p.text for p in d.paragraphs[:600]))
    except Exception as e:
        log(f"      could not read document: {e}")
    return ""


# ------------------------------------------------------------ scoring
def _rx(terms):
    terms = sorted({t.lower() for t in terms}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in terms) + r")(?![a-z0-9])")


SERVICES = CONFIG["services"]
_SERVICE_RX = {cat: _rx(terms) for cat, terms in SERVICES.items()}
_SIGNAL_RX = _rx(CONFIG["signals"])
_RFP_RX = _rx(CONFIG["rfp_words"])
_NEG_RX = _rx(CONFIG["negative"])
_NOTBID_RX = _rx(CONFIG.get("not_bids", []) or ["zzzz"])


def not_a_bid(title):
    """True if a title is clearly a plan/report/menu/etc, not a bid."""
    return bool(_NOTBID_RX.search((title or "").lower()))


def score_text(text):
    """Returns (score, keywords, services, is_rfp)."""
    t = (text or "").lower()
    services, strong = [], set()
    for cat, rx in _SERVICE_RX.items():
        hits = set(rx.findall(t))
        if hits:
            services.append(cat)
            strong |= hits
    signals = set(_SIGNAL_RX.findall(t)) - strong
    is_rfp = bool(_RFP_RX.search(t))
    neg = bool(_NEG_RX.search(t))
    score = 5 * min(len(strong), 6) + 2 * min(len(signals), 5)
    if strong and is_rfp:
        score += 3
    if neg and not strong:
        score = 0
    return score, sorted(strong) + sorted(signals), services, is_rfp


MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RX = re.compile(
    rf"\b(\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|(?:{MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+\d{{4}})",
    re.I,
)
DUE_RX = re.compile(r"(due|deadline|clos|opening|submi|no later than|received by|responses)", re.I)


def parse_date(s):
    s = s.replace(",", " ").replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(?i)^sept\b", "Sep", s)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def find_due_date(text):
    """Best guess at a due date: a date right after words like 'due' or 'closing'."""
    if not text:
        return None
    best, latest = None, None
    for m in DATE_RX.finditer(text[:20000]):
        d = parse_date(m.group(1))
        if not d or d.year < TODAY.year - 1 or d.year > TODAY.year + 3:
            continue
        if DUE_RX.search(text[max(0, m.start() - 90): m.start()]):
            return d.isoformat()
        if d >= TODAY and (best is None or d < best):
            best = d
        if latest is None or d > latest:
            latest = d
    # no future dates at all usually means the bid already closed
    return (best or latest).isoformat() if (best or latest) else None


def snippet_around(text, terms, width=240):
    low = text.lower()
    for k in terms:
        i = low.find(k.lower())
        if i >= 0:
            start = max(0, i - width // 3)
            return ("..." if start else "") + text[start:start + width].strip() + "..."
    return text[:width]


def row_context(a):
    """Text of the table row / list item / block around a link."""
    node = a
    for _ in range(5):
        node = node.parent
        if node is None:
            break
        if node.name in ("tr", "li", "article", "p") or (node.name == "div" and len(node.get_text()) > 40):
            txt = clean(node.get_text(" "))
            return txt[:800] if len(txt) <= 1500 else clean(a.get_text(" "))  # giant blocks are menus
    return clean(a.get_text(" "))


NAV_HINT = re.compile(r"nav|menu|header|footer|breadcrumb|sidebar|skip|dropdown|megamenu|quicklinks|social", re.I)


def page_base(soup, url):
    """Respect <base href> so relative links resolve the way the browser does."""
    from urllib.parse import urljoin
    b = soup.find("base", href=True)
    return urljoin(url, b["href"]) if b else url


def strip_chrome(soup):
    """Remove menus, headers and footers so we only read the page's real content."""
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "aside"]):
        t.decompose()
    for t in soup.find_all(True):
        if t.attrs is None:
            continue
        ident = " ".join(t.get("class", []) or []) + " " + (t.get("id") or "") + " " + (t.get("role") or "")
        if NAV_HINT.search(ident) and t.name in ("div", "ul", "section", "aside"):
            t.decompose()
    return soup


def make_item(src, title, url, text, score, keywords, services, kind):
    return {
        "id": make_id(url, title if kind != "Board signal" else ""),
        "title": clean(title)[:240],
        "url": url,
        "source": src.get("name", ""),
        "state": src.get("state", ""),
        "type": kind,
        "score": score,
        "services": services,
        "keywords": keywords[:12],
        "snippet": snippet_around(text, keywords),
        "due_date": find_due_date(text) if kind != "Board signal" else None,
    }
