"""
OOOL RFP Finder - daily run.
Checks every source (sources.csv + sources_auto.csv from discover.py), the bot
inbox, and Google Alerts feeds. Scores everything and saves results to data/.

Run:  python scraper.py
"""

import csv
import json
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

import common
from common import (CONFIG, DATA, DOC_EXT, NOW_ISO, ROOT, TODAY, Blocked, clean, close_browser,
                    doc_text, log, make_item, not_a_bid, row_context, score_text, service_hits, strip_chrome)

MIN = CONFIG.get("min_score", 5)


def is_yes(v, default=False):
    return default if not v else v.strip().lower() in ("yes", "y", "true", "1")


# ------------------------------------------------------------ source types
def scrape_page(src):
    url = src["url"]
    html = common.fetch_rendered(url) if is_yes(src.get("render")) else common.fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    base = common.page_base(soup, url)
    soup = strip_chrome(soup)

    rows, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        title = clean(a.get_text(" ")) or clean(a.get("title", ""))
        ctx = row_context(a)
        if len(title) < 30 and len(ctx) > len(title):
            title = ctx[:160]  # short links like "RFP 27-014" get the whole row as the title
        if not_a_bid(title) or re.search(r"(?i)meet\.google|zoom\.us|teams\.microsoft|mailto:|facebook|twitter|instagram", href):
            continue
        text = f"{title} {ctx}"
        score, kws, services, is_rfp = score_text(text)
        is_doc = href.lower().split("?")[0].endswith(DOC_EXT)
        rows.append(dict(href=href, title=title, text=text, score=score, kws=kws,
                         services=services, is_rfp=is_rfp, is_doc=is_doc))

    # Open the most promising bid documents (titles are often vague, the details are inside)
    docs = [r for r in rows if r["is_doc"] and (r["is_rfp"] or r["score"] > 0)]
    docs.sort(key=lambda r: (-r["score"], not r["is_rfp"]))
    for r in docs[:CONFIG.get("max_docs_per_source", 15)]:
        try:
            body = doc_text(r["href"])
        except Blocked:
            continue
        except Exception as e:
            log(f"      doc failed {r['href']}: {e}")
            continue
        if body:
            s2, k2, sv2, rfp2 = score_text(body)
            # The document has to really be about our services: mentioned in the opening
            # (title/scope) or several times. One passing mention (like a lawyer RFQ that
            # says "advice on exceptional student education") doesn't count.
            really_about_us = service_hits(body[:1000]) >= 1 or service_hits(body) >= 3
            if not r["services"] and not really_about_us:
                continue
            if s2 > r["score"]:
                r.update(score=s2, kws=k2, services=sv2, is_rfp=r["is_rfp"] or rfp2,
                         text=r["text"] + " " + body[:6000])

    # Same bid listed twice (main document + addendum): keep one
    best_by_title = {}
    for r in rows:
        key = re.sub(r"(?i)\baddendum\b.*", "", r["title"]).strip().lower()[:80]
        cur = best_by_title.get(key)
        if cur is None or ("addendum" in cur["href"].lower() and "addendum" not in r["href"].lower()):
            best_by_title[key] = r
    rows = list(best_by_title.values())

    # From a district/portal page we only keep real bids: bid words + one of our services
    return [make_item(src, r["title"], r["href"], r["text"], r["score"], r["kws"], r["services"], "Open RFP")
            for r in rows if r["score"] >= MIN and r["is_rfp"] and r["services"] and len(r["title"]) > 3]


def scrape_boarddocs(src):
    """Board meeting agendas on BoardDocs (early signals before an RFP is posted)."""
    m = re.search(r"boarddocs\.com/([^/]+/[^/]+)", src["url"])
    site = m.group(1) if m else src["url"].strip("/")
    base = f"https://go.boarddocs.com/{site}/Board.nsf"
    get = lambda u, **kw: common.fetch(u, force=True, **kw)  # only runs when check_boarddocs is on

    committees = [src["extra"]] if src.get("extra") else []
    if not committees:
        committees = list(dict.fromkeys(re.findall(r"\b([A-Z0-9]{12})\b", get(base + "/Public"))))[:15]
    meetings, committee = [], None
    for cid in committees:
        try:
            data = json.loads(get(base + "/BD-GetMeetingsList?open", method="POST",
                                  data={"current_committee_id": cid}))
            if isinstance(data, list) and data:
                meetings, committee = data, cid
                break
        except Exception:
            continue
    if not meetings:
        raise RuntimeError("could not find meetings (put the committee ID in the 'extra' column)")

    found = []
    for mt in meetings:
        try:
            d = datetime.strptime(str(mt.get("numberdate", ""))[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if d < TODAY - timedelta(days=21) or d > TODAY + timedelta(days=30):
            continue
        html = get(base + "/PRINT-AgendaDetailed", method="POST",
                   data={"id": mt.get("unique"), "current_committee_id": committee})
        text = clean(BeautifulSoup(html, "html.parser").get_text(" "))
        score, kws, services, _ = score_text(text)
        if score >= MIN and services:
            title = f"{src['name'].replace(' - Board meetings', '')} board meeting {d.strftime('%b %d, %Y')}: " \
                    f"{', '.join(services)}"
            item = make_item(src, title, f"{base}/goto?open&id={mt.get('unique')}", text, score, kws,
                             services, "Board signal")
            item["meeting_date"] = d.isoformat()
            found.append(item)
    return found


def scrape_rss(src):
    """Google Alerts (or any RSS/Atom feed)."""
    import feedparser
    feed = feedparser.parse(common.fetch(src["url"]))
    found = []
    for e in feed.entries[:80]:
        title = clean(BeautifulSoup(e.get("title", ""), "html.parser").get_text(" "))
        summary = clean(BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" "))
        link = e.get("link", "")
        if "google.com/url" in link:
            link = parse_qs(urlparse(link).query).get("url", [link])[0]
        text = f"{title} {summary}"
        score, kws, services, is_rfp = score_text(text)
        if score >= MIN:
            found.append(make_item(src, title, link, text, score, kws, services,
                                   "Open RFP" if is_rfp else "Web mention"))
    return found


SCRAPERS = {"page": scrape_page, "boarddocs": scrape_boarddocs, "rss": scrape_rss}


# ------------------------------------------------------------ main
def load_sources():
    """Manual sources.csv wins over sources_auto.csv when the same URL is in both."""
    out = {}
    for fname in ("sources_auto.csv", "sources.csv"):
        p = ROOT / fname
        if not p.exists():
            continue
        with open(p, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                url = row.get("url", "")
                if not url.startswith("http"):
                    continue
                row["type"] = (row.get("type") or "page").lower()
                if row["type"] == "boarddocs":
                    row["active"] = "yes" if CONFIG.get("check_boarddocs") and row.get("active") != "no" else "no"
                out[url.rstrip("/").lower()] = row
    states = {x.upper() for x in CONFIG.get("states", [])}
    return [r for r in out.values() if is_yes(r.get("active"), True)
            and (not states or not r.get("state") or r["state"].upper() in states)]


def run_source(src):
    entry = {"name": src["name"], "state": src.get("state", ""), "type": src["type"],
             "url": src["url"], "checked_at": NOW_ISO}
    try:
        items = SCRAPERS.get(src["type"], scrape_page)(src)
        entry.update(status="ok", found=len(items), message="")
        return items, entry
    except Blocked as e:
        entry.update(status="blocked", found=0, message=str(e))
    except Exception as e:
        entry.update(status="error", found=0, message=f"{type(e).__name__}: {str(e)[:200]}")
    return [], entry


def dead_links(urls):
    """Returns the set of links that come back 404/410 (page gone)."""
    urls = sorted({u for u in urls if u.startswith("http")})

    def check(u):
        try:
            common.polite_wait(u)
            r = common.session().head(u, allow_redirects=True, timeout=15)
            if r.status_code in (400, 403, 405, 501):
                r = common.session().get(u, allow_redirects=True, timeout=20, stream=True)
                r.close()
            return u if r.status_code in (404, 410) else None
        except Exception:
            return None  # can't tell (timeout etc): keep it

    with ThreadPoolExecutor(max_workers=CONFIG.get("parallel_sites", 12)) as pool:
        return {u for u in pool.map(check, urls) if u}


def load_json(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def main():
    sources = load_sources()
    log(f"{len(sources)} sources to check")
    existing = {i["id"]: i for i in load_json("rfps.json", {"items": []}).get("items", [])}
    status, new_items, all_found = [], [], []

    plain = [s for s in sources if not is_yes(s.get("render"))]
    rendered = [s for s in sources if is_yes(s.get("render"))]

    with ThreadPoolExecutor(max_workers=CONFIG.get("parallel_sites", 12)) as pool:
        futures = {pool.submit(run_source, s): s for s in plain}
        for i, fut in enumerate(as_completed(futures), 1):
            items, entry = fut.result()
            all_found += items
            status.append(entry)
            if entry["status"] != "ok" or items:
                log(f"  [{i}/{len(plain)}] {entry['name']}: {entry['status']} {len(items)} {entry['message'][:80]}")
    for s in rendered:  # headless browser runs one at a time
        items, entry = run_source(s)
        all_found += items
        status.append(entry)
        log(f"  [browser] {entry['name']}: {entry['status']} {len(items)} {entry['message'][:80]}")
    close_browser()

    try:
        from inbox import read_inbox
        items, entry = read_inbox()
        entry["checked_at"] = NOW_ISO
        all_found += items
        status.append(entry)
        log(f"  inbox: {entry['status']} {len(items)} {entry.get('message', '')}")
    except Exception as e:
        log(f"  inbox failed: {e}")
        traceback.print_exc(limit=1)

    # Drop leads whose link is dead (404 / gone)
    dead = dead_links([i["url"] for i in all_found] + [i["url"] for i in existing.values()])
    if dead:
        log(f"  removing {len(dead)} leads with dead links")
    all_found = [i for i in all_found if i["url"] not in dead]
    existing = {k: v for k, v in existing.items() if v["url"] not in dead}

    for it in all_found:
        old = existing.get(it["id"])
        if old:
            old.update({k: v for k, v in it.items() if v not in (None, "", [])})
            old["last_seen"] = TODAY.isoformat()
        else:
            it["first_seen"] = it["last_seen"] = TODAY.isoformat()
            existing[it["id"]] = it
            new_items.append(it)

    states = {x.upper() for x in CONFIG.get("states", [])}
    if states:  # only keep leads for the states we cover (or leads with no state)
        existing = {k: v for k, v in existing.items() if not v.get("state") or v["state"].upper() in states}
        new_items = [i for i in new_items if not i.get("state") or i["state"].upper() in states]

    cutoff = (TODAY - timedelta(days=CONFIG.get("keep_days", 120))).isoformat()
    items = [i for i in existing.values()
             if i.get("last_seen", "") >= cutoff or (i.get("due_date") or "") >= TODAY.isoformat()]
    items.sort(key=lambda i: (i.get("first_seen", ""), i.get("score", 0)), reverse=True)

    status.sort(key=lambda s: (s["status"] == "ok", s["name"]))
    (DATA / "rfps.json").write_text(json.dumps({"updated": NOW_ISO, "items": items}, indent=1), encoding="utf-8")
    (DATA / "status.json").write_text(json.dumps({"updated": NOW_ISO, "sources": status}, indent=1),
                                      encoding="utf-8")
    (DATA / "new_today.json").write_text(json.dumps({"updated": NOW_ISO, "items": new_items}, indent=1),
                                         encoding="utf-8")
    bad = sum(s["status"] in ("error", "blocked") for s in status)
    log(f"\nDone: {len(new_items)} new, {len(items)} total, {bad} sources with problems")


if __name__ == "__main__":
    sys.exit(main())
