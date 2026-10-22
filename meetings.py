"""
Heads-ups from school board meetings, budgets and Title I plans.

Reads meeting_sources.csv (made by discover.py), visits each district's
board meeting site (BoardDocs or the district's own board page) and its
Title I / federal programs page, and saves anything that could turn into
work for OOOL to data/headsups/<STATE>.json.

Run:  python meetings.py
"""

import csv
import json
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import common
from common import CONFIG, DATA, DOC_EXT, NOW_ISO, ROOT, TODAY, Blocked, clean, log, strip_chrome
from signals import find_headsups

OUT = DATA / "headsups"
OUT.mkdir(exist_ok=True)
STATE_FILE = DATA / "meetings_state.json"

PRIORITY = [s.upper() for s in CONFIG.get("priority_states", [])]
EVERY = CONFIG.get("meetings_every_days", 3)
LOOKBACK = CONFIG.get("meetings_lookback_days", 90)
BUDGET_MIN = CONFIG.get("meetings_minutes", 180)
KEEP_DAYS = CONFIG.get("keep_days", 120)
STARTED = time.time()

MEETING_WORDS = re.compile(r"(?i)agenda|minutes|meeting|packet|board|session|workshop")
PLAN_WORDS = re.compile(r"(?i)title (i|1)\b|plan|budget|federal|improvement|grant|allocation|esser|schoolwide|"
                        r"parent and family|comprehensive needs|application")
SKIP_LINKS = re.compile(r"(?i)youtube|vimeo|facebook|twitter|instagram|mailto:|livestream|video|zoom\.us|"
                        r"meet\.google|calendar\.google|\.mp4|\.mp3|translate")
DATE_IN = [
    (re.compile(r"(20\d\d)[-_./](\d{1,2})[-_./](\d{1,2})"), lambda m: (m[1], m[2], m[3])),
    (re.compile(r"(\d{1,2})[-_./](\d{1,2})[-_./](20\d\d)"), lambda m: (m[3], m[1], m[2])),
    (re.compile(r"(\d{1,2})[-_./](\d{1,2})[-_./](\d\d)(?!\d)"), lambda m: ("20" + m[3], m[1], m[2])),
]
MONTH_RX = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_state_lock = threading.Lock()


def out_of_time():
    return (time.time() - STARTED) / 60 > BUDGET_MIN


def date_in(text):
    """Find a meeting date in a link's text or address."""
    text = text or ""
    m = MONTH_RX.search(text)
    if m:
        try:
            return datetime(int(m[3]), MONTHS[m[1].lower()[:3]], int(m[2])).date()
        except ValueError:
            pass
    for rx, parts in DATE_IN:
        m = rx.search(text)
        if m:
            y, mo, d = parts(m)
            try:
                return datetime(int(y), int(mo), int(d)).date()
            except ValueError:
                continue
    return None


# ------------------------------------------------------------ BoardDocs
def scan_boarddocs(src, st):
    m = re.search(r"boarddocs\.com/([^/]+/[^/]+)", src["url"])
    if not m:
        raise ValueError("not a BoardDocs link")
    base = f"https://go.boarddocs.com/{m.group(1)}/Board.nsf"
    get = lambda u, **kw: common.fetch(u, force=True, **kw)  # VP approved reading public BoardDocs agendas

    committee = st.get("committee")
    meetings = []
    if committee:
        try:
            meetings = json.loads(get(base + "/BD-GetMeetingsList?open", method="POST",
                                      data={"current_committee_id": committee}))
        except Exception:
            meetings, committee = [], None
    if not meetings:
        page = get(base + "/Public")
        for cid in list(dict.fromkeys(re.findall(r"\b([A-Z0-9]{12})\b", page)))[:12]:
            try:
                data = json.loads(get(base + "/BD-GetMeetingsList?open", method="POST",
                                      data={"current_committee_id": cid}))
            except Exception:
                continue
            if isinstance(data, list) and data:
                meetings, committee = data, cid
                break
    if not meetings:
        raise RuntimeError("could not find this district's meetings on BoardDocs")
    st["committee"] = committee

    since = TODAY - timedelta(days=LOOKBACK if not st.get("last_checked") else EVERY + 10)
    found, seen = [], st.setdefault("seen", {})
    for mt in meetings:
        try:
            d = datetime.strptime(str(mt.get("numberdate", ""))[:8], "%Y%m%d").date()
        except ValueError:
            continue
        mid = mt.get("unique") or mt.get("id")
        if not mid or d < since or d > TODAY + timedelta(days=45):
            continue
        upcoming = d >= TODAY - timedelta(days=1)   # agendas change until the meeting happens
        if mid in seen and not upcoming:
            continue
        if out_of_time():
            break
        html = get(base + "/PRINT-AgendaDetailed", method="POST",
                   data={"id": mid, "current_committee_id": committee})
        text = clean(BeautifulSoup(html, "html.parser").get_text(" "))
        seen[mid] = TODAY.isoformat()
        meeting = {"district": src["district"], "state": src["state"], "leaid": src["leaid"],
                   "title": clean(mt.get("name", "")) or "Board meeting", "date": d.isoformat(),
                   "url": f"{base}/goto?open&id={mid}"}
        found += find_headsups(text, meeting, "Board meeting")
    return found


# ------------------------------------------------------------ district website board pages
def page_links(url, render=False):
    html = common.fetch_rendered(url) if render else common.fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    base = common.page_base(soup, url)
    strip_chrome(soup)
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if not href.startswith("http") or SKIP_LINKS.search(href):
            continue
        txt = clean(a.get_text(" ")) or clean(a.get("title", ""))
        ctx = common.row_context(a)
        out.append((href, txt, ctx))
    return out, clean(soup.get_text(" "))


def read_any(url):
    """Text of a PDF/Word file or a web page."""
    if url.lower().split("?")[0].endswith(DOC_EXT) or "/files/" in url.lower() and "pdf" in url.lower():
        return common.doc_text(url, pages=40)
    html = common.fetch(url)
    soup = strip_chrome(BeautifulSoup(html, "html.parser"))
    return clean(soup.get_text(" "))


def scan_boardpage(src, st):
    links, _ = page_links(src["url"], render=src.get("render") == "yes")
    since = TODAY - timedelta(days=LOOKBACK if not st.get("last_checked") else EVERY + 10)
    seen = st.setdefault("seen", {})
    cands = []
    for href, txt, ctx in links:
        if not (MEETING_WORDS.search(txt) or MEETING_WORDS.search(urlparse(href).path)):
            continue
        d = date_in(txt) or date_in(href) or date_in(ctx)
        if d and (d < since or d > TODAY + timedelta(days=45)):
            continue
        if href in seen and not (d and d >= TODAY - timedelta(days=1)):
            continue
        cands.append((d, href, txt))
    # newest first; links without a date only on the first look (and only a few)
    dated = sorted([c for c in cands if c[0]], key=lambda c: c[0], reverse=True)
    undated = [c for c in cands if not c[0]][: (6 if not st.get("last_checked") else 3)]
    found = []
    for d, href, txt in (dated[:15] + undated):
        if out_of_time():
            break
        try:
            text = read_any(href)
        except Exception:
            continue
        seen[href] = TODAY.isoformat()
        if len(text) < 80:
            continue
        meeting = {"district": src["district"], "state": src["state"], "leaid": src["leaid"],
                   "title": txt[:120] or "Board meeting", "date": (d or TODAY).isoformat(), "url": href}
        found += find_headsups(text, meeting, "Board meeting")
    return found


def scan_funding(src, st):
    """Title I / federal programs / school improvement pages and the plans linked from them."""
    if st.get("last_checked") and st["last_checked"] >= (TODAY - timedelta(days=30)).isoformat():
        return []  # plans change rarely: once a month is plenty
    links, page_text = page_links(src["url"])
    seen = st.setdefault("seen", {})
    meeting = {"district": src["district"], "state": src["state"], "leaid": src["leaid"],
               "title": "Title I / federal programs page", "date": TODAY.isoformat(), "url": src["url"]}
    found = find_headsups(page_text, meeting, "Budget / plan")
    docs = [(h, t) for h, t, _ in links if h.lower().split("?")[0].endswith(DOC_EXT)
            and PLAN_WORDS.search(t + " " + h) and h not in seen]
    for href, txt in docs[:8]:
        if out_of_time():
            break
        try:
            text = common.doc_text(href, pages=40)
        except Exception:
            continue
        seen[href] = TODAY.isoformat()
        m = {**meeting, "title": txt[:120] or "Plan / budget document", "url": href,
             "date": (date_in(txt) or date_in(href) or TODAY).isoformat()}
        found += find_headsups(text, m, "Budget / plan")
    for h in found:
        h["hot"] = False   # plans and budgets are good leads but not urgent: weekly roundup
    return found


SCANNERS = {"boarddocs": scan_boarddocs, "boardpage": scan_boardpage, "funding": scan_funding}


# ------------------------------------------------------------ main
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_sources():
    rows = []
    p = ROOT / "meeting_sources.csv"
    if p.exists():
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    # hand-set board links from district_overrides.csv
    o = ROOT / "district_overrides.csv"
    if o.exists():
        with open(o, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
                url = r.get("board_url", "")
                if url.startswith("http"):
                    rows = [x for x in rows if not (x["state"] == r.get("state") and x["type"] in ("boarddocs", "boardpage")
                                                    and r.get("district", "").lower() in x["district"].lower())]
                    rows.append({"state": r.get("state", ""), "leaid": "", "district": r.get("district", ""),
                                 "type": "boarddocs" if "boarddocs.com" in url else "boardpage",
                                 "url": url, "render": r.get("render", "") or "no"})
    if not CONFIG.get("check_boarddocs", False):
        rows = [r for r in rows if r["type"] != "boarddocs"]
    return rows


def main():
    sources = load_sources()
    state = load_json(STATE_FILE, {})
    due = (TODAY - timedelta(days=EVERY)).isoformat()

    def wanted(s):
        st = state.get(s["url"], {})
        return not st.get("last_checked") or st["last_checked"] <= due

    todo = [s for s in sources if wanted(s)]
    # priority states first, then whatever was checked longest ago, BoardDocs before other sites
    todo.sort(key=lambda s: (s["state"] not in PRIORITY, state.get(s["url"], {}).get("last_checked", ""),
                             s["type"] != "boarddocs"))
    log(f"{len(sources)} meeting/plan sources, {len(todo)} due today")

    found, stats, errors = [], Counter(), []

    def run(src):
        if out_of_time():
            return src, None, "skipped"
        st = dict(state.get(src["url"], {}))
        try:
            items = SCANNERS[src["type"]](src, st)
            msg = "ok"
        except Blocked as e:
            items, msg = [], f"blocked: {e}"
        except Exception as e:
            items, msg = [], f"{type(e).__name__}: {str(e)[:150]}"
        st["last_checked"] = TODAY.isoformat()
        st["status"] = msg
        # forget meetings older than we keep
        cutoff = (TODAY - timedelta(days=KEEP_DAYS + 30)).isoformat()
        st["seen"] = {k: v for k, v in st.get("seen", {}).items() if v >= cutoff}
        with _state_lock:
            state[src["url"]] = st
        return src, items, msg

    renders = [s for s in todo if s.get("render") == "yes"]
    plain = [s for s in todo if s.get("render") != "yes"]
    with ThreadPoolExecutor(max_workers=CONFIG.get("parallel_sites", 12)) as pool:
        futs = [pool.submit(run, s) for s in plain]
        for i, fut in enumerate(as_completed(futs), 1):
            src, items, msg = fut.result()
            stats[msg if msg in ("ok", "skipped") else "problem"] += 1
            if items:
                found += items
            if msg not in ("ok", "skipped"):
                errors.append({"district": src["district"], "state": src["state"], "type": src["type"],
                               "url": src["url"], "message": msg})
            if i % 200 == 0:
                log(f"  {i}/{len(plain)}  heads-ups so far: {len(found)}")
    for s in renders:
        src, items, msg = run(s)
        stats[msg if msg in ("ok", "skipped") else "problem"] += 1
        found += items or []
    common.close_browser()

    # ---- merge into per-state files
    by_state = {}
    for h in found:
        by_state.setdefault(h["state"] or "XX", []).append(h)
    new_items = []
    cutoff = (TODAY - timedelta(days=KEEP_DAYS)).isoformat()
    touched = set(by_state) | {p.stem for p in OUT.glob("*.json") if p.stem != "index"}
    index = {}
    for stt in sorted(touched):
        path = OUT / f"{stt}.json"
        cur = {h["id"]: h for h in load_json(path, {"items": []}).get("items", [])}
        for h in by_state.get(stt, []):
            if h["id"] in cur:
                cur[h["id"]].update({k: v for k, v in h.items() if v not in (None, "", [])})
                cur[h["id"]]["last_seen"] = TODAY.isoformat()
            else:
                h["first_seen"] = h["last_seen"] = TODAY.isoformat()
                cur[h["id"]] = h
                new_items.append(h)
        items = [h for h in cur.values() if max(h.get("date", ""), h.get("first_seen", "")) >= cutoff]
        items.sort(key=lambda h: (h.get("first_seen", ""), h.get("hot", False), h.get("date", "")), reverse=True)
        path.write_text(json.dumps({"updated": NOW_ISO, "items": items}, separators=(",", ":")), encoding="utf-8")
        index[stt] = {"count": len(items), "hot": sum(1 for h in items if h.get("hot"))}

    checked_ever = sum(1 for s in sources if state.get(s["url"], {}).get("last_checked"))
    (OUT / "index.json").write_text(json.dumps({
        "updated": NOW_ISO, "states": index, "sources": len(sources), "checked_ever": checked_ever,
        "run": dict(stats), "new": len(new_items)}, indent=1), encoding="utf-8")
    (DATA / "headsups_new.json").write_text(json.dumps({"updated": NOW_ISO, "items": new_items}, indent=1),
                                            encoding="utf-8")
    (DATA / "meetings_problems.json").write_text(json.dumps(errors[:500], indent=1), encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    log(f"\nDone: {len(new_items)} new heads-ups ({sum(1 for h in new_items if h['hot'])} hot), "
        f"run {dict(stats)}, {checked_ever}/{len(sources)} sources checked so far")


if __name__ == "__main__":
    sys.exit(main())
