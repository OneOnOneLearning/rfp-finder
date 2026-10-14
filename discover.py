"""
District discovery: builds the list of EVERY school district in the states in
config.yml, finds each one's website, its bids/purchasing page, the bid portal it
uses, and its board meeting site. Writes:
  data/districts.json   (the Coverage tab on the dashboard)
  sources_auto.csv      (what the daily scraper checks)

Run:  python discover.py          (only new districts or ones older than 30 days)
      python discover.py --all    (redo everything)
"""

import csv
import io
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import (CONFIG, DATA, NOW_ISO, ROOT, TODAY, Blocked, clean, fetch,
                    fetch_final_url, log)

STATES = [s.upper() for s in CONFIG.get("states", ["FL", "CA"])]
REFRESH_DAYS = 30

# NCES district types: 1 regular, 2 component, 4 regional/county office, 7 charter
TYPES = {"1", "2", "4"} | ({"7"} if CONFIG.get("include_charters") else set())

PORTALS = {
    "demandstar.com": "DemandStar", "bidnetdirect.com": "BidNet Direct", "planetbids.com": "PlanetBids",
    "bonfirehub.com": "Bonfire", "procureware.com": "Procureware", "opengov.com": "OpenGov",
    "vendorlink": "VendorLink", "publicpurchase.com": "Public Purchase", "ionwave.net": "IonWave",
    "bidsync.com": "Periscope/BidSync", "periscopeholdings.com": "Periscope/BidSync",
    "logiq.bid": "BidLogiQ", "jaggaer.com": "Jaggaer", "negometrix": "Mercell", "mercell.com": "Mercell",
    "bidexpress.com": "Bid Express", "biddingo.com": "Biddingo", "caleprocure.ca.gov": "Cal eProcure",
    "myfloridamarketplace.com": "MyFloridaMarketPlace", "esuppliersmyfloridamarketplace": "MyFloridaMarketPlace",
    "unisonmarketplace.com": "Unison", "govspend": "GovSpend", "bidsandtenders": "Bids&Tenders",
}
BOARDS = {
    "boarddocs.com": "BoardDocs", "eboardsolutions.com": "Simbli", "simbli": "Simbli",
    "diligent.community": "Diligent Community", "legistar.com": "Legistar", "granicus.com": "Granicus",
    "primegov.com": "PrimeGov", "civicclerk": "CivicClerk", "agendaonline.net": "AgendaOnline",
    "novusagenda.com": "NovusAgenda", "iqm2.com": "IQM2", "escribemeetings.com": "eSCRIBE",
}
GENERIC_HOSTS = ("google.com", "facebook.com", "wixsite.com", "weebly.com", "wordpress.com",
                 "edlioschool.com", "schoolloop.com", "blogspot.com", "instagram.com", "youtube.com",
                 "twitter.com", "x.com", "squarespace.com", "smore.com", "canva.site")

BID_WORDS = re.compile(r"purchas|procure|\bbids?\b|bidding|solicitation|\brfps?\b|\brfqs?\b|vendor|doing business|"
                       r"business with|contracting opportunit|bid opportunit|public notices", re.I)
BUSINESS = re.compile(r"business services|business office|fiscal services|business & fiscal|finance", re.I)
PAGE_BID_RX = re.compile(r"(?i)\b(rfps?|rfqs?|rfis?|itbs?|ifbs?|itns?|request for (proposals?|qualifications|quotes?|information)|"
                         r"invitation (to|for) bids?|solicitations?|sealed (bids|proposals)|bid (no|number|opening|#)|"
                         r"notice (inviting|to) (bids|proposals|bidders|vendors))\b")
STRONG_BID = re.compile(r"current (bids|solicitations)|open (bids|solicitations)|bid opportunit|"
                        r"solicitations|procurement|purchasing", re.I)
BAD_WORDS = re.compile(r"\bbus\b|transportation|employment|careers?\b|jobs?\b|policy|policies|calendar|"
                       r"lunch|menu|athletic|login|facebook|twitter|instagram|youtube", re.I)


# ------------------------------------------------------------ 1. district list
def nces_districts():
    """Every district in our states from the federal NCES directory (official public API)."""
    state_list = ",".join(f"'{s}'" for s in STATES)
    fields = "LEAID,LEA_NAME,STABR,LEA_TYPE,LEA_TYPE_TEXT,LCITY,CONAME,MEMBER,SY_STATUS_TEXT,PHONE"
    for year in ("SY2425", "SY2324", "SY2223"):
        for layer in (1, 0):
            base = (f"https://nces.ed.gov/opengis/rest/services/School_District_Boundaries/"
                    f"EDGE_ADMINDATA_SCHOOLDISTRICTS_{year}/MapServer/{layer}/query")
            rows, offset = [], 0
            try:
                while True:
                    url = (f"{base}?where=STABR+IN+({state_list})&outFields={fields}&returnGeometry=false"
                           f"&f=json&resultOffset={offset}&resultRecordCount=1000&orderByFields=LEAID")
                    data = json.loads(fetch(url, timeout=60, force=True))
                    if "features" not in data:
                        raise ValueError(data.get("error", "no features"))
                    batch = [f["attributes"] for f in data["features"]]
                    rows += batch
                    if len(batch) < 1000:
                        break
                    offset += 1000
                if rows:
                    log(f"NCES {year}: {len(rows)} agencies in {', '.join(STATES)}")
                    return rows
            except Exception as e:
                log(f"  NCES {year}/{layer} not available ({str(e)[:80]})")
    raise RuntimeError("Could not download the NCES district list")


def clean_district(a):
    status = str(a.get("SY_STATUS_TEXT") or "").lower()
    if any(w in status for w in ("closed", "inactive", "future")):
        return None
    if str(a.get("LEA_TYPE")) not in TYPES:
        return None
    return {
        "leaid": str(a["LEAID"]),
        "name": (a.get("LEA_NAME") or "").title().replace("Usd", "USD").replace("Isd", "ISD"),
        "state": a.get("STABR"),
        "county": (a.get("CONAME") or "").title(),
        "city": (a.get("LCITY") or "").title(),
        "kind": a.get("LEA_TYPE_TEXT") or "",
        "students": int(a.get("MEMBER") or 0) if str(a.get("MEMBER") or "").lstrip("-").isdigit() else 0,
        "phone": a.get("PHONE") or "",
    }


# ------------------------------------------------------------ 2. websites
def base_domain(host):
    host = host.lower().split(":")[0]
    parts = host.split(".")
    if host.endswith((".k12.ca.us", ".k12.fl.us")) or re.search(r"\.k12\.[a-z]{2}\.us$", host):
        return ".".join(parts[-4:])
    if host.endswith(".us") and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def homepage_from(url):
    if not url:
        return ""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    p = urlparse(url)
    if not p.netloc or any(g in p.netloc.lower() for g in GENERIC_HOSTS):
        return ""
    return f"{p.scheme}://{p.netloc}/"


def wikidata_websites():
    """NCES district ID -> official website, from Wikidata (free public data)."""
    q = "SELECT ?id ?site WHERE { ?d wdt:P2483 ?id . ?d wdt:P856 ?site . }"
    try:
        raw = fetch("https://query.wikidata.org/sparql?format=json&query=" + requests_quote(q),
                    timeout=120, force=True)
        out = {}
        for b in json.loads(raw)["results"]["bindings"]:
            out.setdefault(b["id"]["value"].strip().zfill(7), b["site"]["value"])
        log(f"Wikidata: {len(out)} district websites")
        return out
    except Exception as e:
        log(f"Wikidata not available: {e}")
        return {}


def requests_quote(s):
    from urllib.parse import quote
    return quote(s, safe="")


def california_websites():
    """CA Dept of Education school directory: district website, or the domain most schools use."""
    if "CA" not in STATES:
        return {}
    try:
        raw = fetch("https://www.cde.ca.gov/schooldirectory/report?rid=dl1&tp=txt", timeout=180, force=True)
    except Exception as e:
        log(f"CDE directory not available: {e}")
        return {}
    log(f"CDE file: {len(raw)} chars, starts with: {raw[:200]!r}")
    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")), delimiter="\t")
    log(f"CDE columns: {reader.fieldnames[:12] if reader.fieldnames else None}")
    direct, domains = {}, {}
    for r in reader:
        nces = (r.get("NCESDist") or "").strip()
        site = homepage_from(r.get("Website") or "")
        if not nces or not site or (r.get("StatusType") or "") not in ("Active", "Pending"):
            continue
        nces = ("06" + nces[-5:]) if len(nces) <= 5 else nces.zfill(7)
        school = (r.get("School") or "").strip()
        if school in ("", "No Data") or "district office" in (r.get("SOCType") or "").lower():
            direct.setdefault(nces, site)
        domains.setdefault(nces, Counter())[base_domain(urlparse(site).netloc)] += 1
    out = dict(direct)
    for nces, c in domains.items():
        if nces not in out and c:
            dom, n = c.most_common(1)[0]
            if n >= 2 or sum(c.values()) == 1:
                out[nces] = f"https://www.{dom}/"
    log(f"CDE: {len(out)} California district websites")
    return out


def florida_guess(d):
    """Most Florida districts still own <county>.k12.fl.us (usually redirects to the new site)."""
    if d["state"] != "FL":
        return ""
    name = re.sub(r"(?i)\b(county|school|district|schools|public)\b", "", d["name"])
    slug = re.sub(r"[^a-z]", "", name.lower())
    alts = {slug, slug.replace("saint", "st"), slug.replace("miamidade", "dade")}
    for s in alts:
        if not s:
            continue
        for url in (f"https://www.{s}.k12.fl.us/", f"https://{s}.k12.fl.us/"):
            try:
                final, html = fetch_final_url(url, timeout=15)
                if len(html) > 2000:
                    return homepage_from(final)
            except Exception:
                continue
    return ""


def load_overrides():
    out = []
    p = ROOT / "district_overrides.csv"
    if p.exists():
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
                if r.get("district"):
                    out.append(r)
    return out


def match_override(d, overrides):
    for o in overrides:
        if o.get("state", "").upper() in ("", d["state"]) and o["district"].lower() in d["name"].lower():
            return o
    return None


# ------------------------------------------------------------ 3. bids page
def which(url, table):
    low = url.lower()
    for key, name in table.items():
        if key in low:
            return name
    return None


def links_of(html, base):
    soup = BeautifulSoup(html, "html.parser")
    from common import page_base
    base = page_base(soup, base)
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if href.startswith("http"):
            out.append((href, clean(a.get_text(" ")) or clean(a.get("title", ""))))
    return out, clean(soup.get_text(" "))


def candidate_score(href, text, site_domain):
    s = 0
    if BID_WORDS.search(text or ""):
        s += 3
    if STRONG_BID.search(text or ""):
        s += 2
    if BID_WORDS.search(urlparse(href).path):
        s += 2
    if BAD_WORDS.search(text or "") or BAD_WORDS.search(urlparse(href).path):
        s -= 3
    if base_domain(urlparse(href).netloc) != site_domain and not which(href, PORTALS):
        s -= 2
    return s


def find_bids(website):
    """Returns dict with bids_page, portal, portal_url, board_platform, board_url, note."""
    res = {"bids_page": "", "portal": "", "portal_url": "", "board_platform": "", "board_url": "", "note": ""}
    final, html = fetch_final_url(website)
    res["website"] = homepage_from(final) or website
    dom = base_domain(urlparse(final).netloc)
    links, _ = links_of(html, final)

    def note_extras(all_links):
        for href, _t in all_links:
            if not res["portal"] and (p := which(href, PORTALS)):
                res["portal"], res["portal_url"] = p, href
            if not res["board_platform"] and (b := which(href, BOARDS)):
                res["board_platform"], res["board_url"] = b, href

    note_extras(links)
    cands, business = {}, []
    for href, text in links:
        sc = candidate_score(href, text, dom)
        if sc >= 3 and not which(href, PORTALS):
            cands[href] = max(cands.get(href, 0), sc)
        elif BUSINESS.search(text or "") and base_domain(urlparse(href).netloc) == dom:
            business.append(href)

    if not cands:  # look in the sitemap
        for sm in ("sitemap.xml", "sitemap_index.xml"):
            try:
                xml = fetch(urljoin(final, "/" + sm), timeout=25)
                for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)[:3000]:
                    path = urlparse(u).path
                    if BID_WORDS.search(path.replace("-", " ")) and not BAD_WORDS.search(path):
                        cands[u] = 3 + (2 if STRONG_BID.search(path.replace("-", " ")) else 0)
                if cands:
                    break
            except Exception:
                continue

    checked, best = set(), ("", 0.0, 0)  # url, score, bid mentions

    def evaluate(href, base_sc, depth):
        nonlocal best
        if href in checked or len(checked) >= 10:
            return
        checked.add(href)
        try:
            page_html = fetch(href, timeout=25)
        except Exception:
            return
        sub_links, page_text = links_of(page_html, href)
        note_extras(sub_links)
        mentions = len(PAGE_BID_RX.findall(page_text))
        score = base_sc + min(mentions, 20) / 2
        if mentions >= 2 and score > best[1]:
            best = (href, score, mentions)
        if depth < 1:
            deeper = [(h, t) for h, t in sub_links if BID_WORDS.search(t or "") and not BAD_WORDS.search(t or "")
                      and base_domain(urlparse(h).netloc) == dom and h != href]
            deeper.sort(key=lambda x: -candidate_score(x[0], x[1], dom))
            for h, t in deeper[:3]:
                evaluate(h, candidate_score(h, t, dom) + (2 if re.search(r"(?i)current|open|active|opportunit", t) else 0),
                         depth + 1)

    for href, sc in sorted(cands.items(), key=lambda x: -x[1])[:5]:
        evaluate(href, sc, 0)
    if not best[0]:
        for href in business[:2]:
            evaluate(href, 1, 0)

    res["bids_page"] = best[0]
    if not best[0]:
        res["note"] = "bids posted on " + res["portal"] if res["portal"] else "bids page not found"
    return res


def guess_website(d):
    """Try common district web addresses (e.g. lodiusd.net, tvusd.k12.ca.us) and check the page is theirs."""
    import socket
    name = re.sub(r"(?i)\b(school district|district|schools?|county|public|city)\b", " ", d["name"])
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if w not in ("of", "the", "and")]
    if not words:
        return ""
    kind = {"unified": "usd", "elementary": "esd", "union": "usd", "high": "uhsd", "joint": ""}
    core = [w for w in words if w not in kind]
    if not core:
        return ""
    initials, joined = "".join(w[0] for w in core), "".join(core)
    suffixes = ["usd", "sd", "esd", "uhsd", "hsd", ""] if d["state"] == "CA" else ["schools", "", "sd", "k12"]
    stems = []
    for base in (joined, initials if len(initials) >= 2 else None, core[0]):
        if base:
            stems += [base + suf for suf in suffixes]
    tlds = [".org", ".net", ".us", ".k12.ca.us", ".com"] if d["state"] == "CA" else [".org", ".net", ".k12.fl.us", ".com"]
    hosts = list(dict.fromkeys(f"{st}{t}" for st in stems for t in tlds))
    must = core[0] if len(core[0]) > 3 else " ".join(core)
    for host in hosts[:60]:
        try:
            socket.getaddrinfo(host, 443)
        except Exception:
            continue
        for url in (f"https://www.{host}/", f"https://{host}/"):
            try:
                final, html = fetch_final_url(url, timeout=12)
            except Exception:
                continue
            soup = BeautifulSoup(html[:200000], "html.parser")
            head = clean((soup.title.get_text(" ") if soup.title else "") + " " + soup.get_text(" ")[:3000]).lower()
            if must in head and re.search(r"school|district|student|education", head):
                return homepage_from(final)
            break
    return ""


# ------------------------------------------------------------ main
def discover_one(d, overrides):
    for k in ("bids_page", "portal", "portal_url", "board_platform", "board_url", "note"):
        d.setdefault(k, "")
    o = match_override(d, overrides)
    if o:
        d["website"] = homepage_from(o.get("website")) or d.get("website", "")
    if not d.get("website"):
        d["website"] = florida_guess(d) or guess_website(d)
        d["website_source"] = "guessed" if d["website"] else ""
    if not d.get("website"):
        d.update(status="no website", bids_page="", note="website not found")
    else:
        try:
            d.update(find_bids(d["website"]))
            d["status"] = "found" if d["bids_page"] else ("portal only" if d["portal"] else "no bids page")
        except Blocked as e:
            d.update(status="blocked", note=str(e))
        except Exception as e:
            d.update(status="error", note=f"{type(e).__name__}: {str(e)[:120]}")
    if o:
        if o.get("bids_page"):
            d["bids_page"], d["status"], d["note"] = o["bids_page"], "found", "set by hand"
        if o.get("board_url"):
            d["board_url"], d["board_platform"] = o["board_url"], which(o["board_url"], BOARDS) or "Other"
    d["checked"] = TODAY.isoformat()
    return d


def write_sources(districts):
    rows = []
    check_bd = bool(CONFIG.get("check_boarddocs"))
    for d in districts:
        if d.get("bids_page"):
            portal = which(d["bids_page"], PORTALS)
            rows.append({"active": "yes", "name": f"{d['name']} - Bids", "state": d["state"], "type": "page",
                         "url": d["bids_page"], "render": "yes" if portal else "no",
                         "extra": "", "notes": f"auto; leaid {d['leaid']}"})
        if d.get("board_platform") == "BoardDocs" and d.get("board_url"):
            rows.append({"active": "yes" if check_bd else "no", "name": f"{d['name']} - Board meetings",
                         "state": d["state"], "type": "boarddocs", "url": d["board_url"], "render": "no",
                         "extra": "", "notes": f"auto; leaid {d['leaid']}"})
    with open(ROOT / "sources_auto.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["active", "name", "state", "type", "url", "render", "extra", "notes"])
        w.writeheader()
        w.writerows(rows)
    log(f"sources_auto.csv: {len(rows)} sources")


def main():
    redo_all = "--all" in sys.argv
    old = {}
    try:
        old = {d["leaid"]: d for d in json.loads((DATA / "districts.json").read_text())["districts"]}
    except Exception:
        pass

    districts = [d for d in (clean_district(a) for a in nces_districts()) if d]
    log(f"{len(districts)} districts to cover")

    websites = wikidata_websites()
    websites.update(california_websites())
    overrides = load_overrides()

    todo, keep = [], []
    stale = (TODAY - timedelta(days=REFRESH_DAYS)).isoformat()
    for d in districts:
        prev = old.get(d["leaid"], {})
        if prev and not redo_all and prev.get("checked", "") >= stale and prev.get("status") == "found":
            keep.append({**prev, **{k: d[k] for k in ("name", "students", "county", "city")}})
            continue
        d["website"] = homepage_from(websites.get(d["leaid"], "")) or prev.get("website", "")
        d["website_source"] = "directory" if d["website"] else ""
        todo.append(d)

    log(f"Checking {len(todo)} districts ({len(keep)} up to date)")
    done = []
    with ThreadPoolExecutor(max_workers=CONFIG.get("parallel_sites", 12)) as pool:
        futures = [pool.submit(discover_one, d, overrides) for d in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            d = fut.result()
            done.append(d)
            if i % 50 == 0 or i == len(todo):
                log(f"  {i}/{len(todo)}")

    allds = sorted(keep + done, key=lambda d: (d["state"], -d.get("students", 0)))
    summary = Counter(d.get("status", "") for d in allds)
    (DATA / "districts.json").write_text(json.dumps(
        {"updated": NOW_ISO, "states": STATES, "summary": summary, "districts": allds}, indent=1), encoding="utf-8")
    write_sources(allds)
    log("Summary: " + ", ".join(f"{k}: {v}" for k, v in summary.most_common()))
    import common
    (DATA / "discover_log.txt").write_text("\n".join(common.LOG_LINES[-3000:]), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
