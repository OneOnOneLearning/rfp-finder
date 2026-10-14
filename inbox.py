"""
Reads the bot Gmail inbox (read-only) for bid portal alerts (DemandStar, BidNet,
PlanetBids, Bonfire...) and Google Alerts emails, and turns them into leads.
Uses the same SMTP_USER / SMTP_PASS secrets as the email step.
"""

import email
import imaplib
import json
import os
import re
from datetime import timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from common import CONFIG, DATA, TODAY, clean, log, make_item, row_context, score_text

STATE_NAMES = {"FL": ["florida", " fl "], "CA": ["california", " ca "], "TX": ["texas"], "GA": ["georgia"],
               "NY": ["new york"], "NJ": ["new jersey"], "IL": ["illinois"], "NC": ["north carolina"],
               "AZ": ["arizona"], "CO": ["colorado"], "LA": ["louisiana"], "TN": ["tennessee"],
               "MD": ["maryland"], "VA": ["virginia"], "PA": ["pennsylvania"], "OH": ["ohio"], "MI": ["michigan"]}
SENDERS = {"demandstar": "DemandStar", "bidnet": "BidNet Direct", "planetbids": "PlanetBids",
           "bonfire": "Bonfire", "procureware": "Procureware", "opengov": "OpenGov", "vendorlink": "VendorLink",
           "publicpurchase": "Public Purchase", "googlealerts": "Google Alerts", "google.com": "Google Alerts",
           "periscope": "Periscope", "bidsync": "Periscope", "ionwave": "IonWave", "logiq": "BidLogiQ"}


def _dec(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def guess_state(text):
    t = f" {text.lower()} "
    for st, names in STATE_NAMES.items():
        if any(n in t for n in names):
            return st
    return ""


def unwrap(link):
    if "google.com/url" in link:
        return parse_qs(urlparse(link).query).get("url", [link])[0]
    return link


def body_html(msg):
    html, text = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            s = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/html" and not html:
            html = s
        elif ctype == "text/plain" and not text:
            text = s
    return html or f"<pre>{text}</pre>"


def items_from_email(msg):
    subject = _dec(msg.get("Subject"))
    sender = parseaddr(_dec(msg.get("From")))[1].lower()
    portal = next((name for key, name in SENDERS.items() if key in sender), sender.split("@")[-1])
    soup = BeautifulSoup(body_html(msg), "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    full = clean(soup.get_text(" "))
    src = {"name": f"Email: {portal}", "state": ""}
    found, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = unwrap(a["href"])
        title = clean(a.get_text(" "))
        if not href.startswith("http") or href in seen or len(title) < 12:
            continue
        if re.search(r"(?i)unsubscribe|preferences|privacy|manage alert|log ?in|sign ?in|view in browser", title):
            continue
        seen.add(href)
        ctx = row_context(a)
        text = f"{title} {ctx} {subject}"
        score, kws, services, is_rfp = score_text(text)
        if score >= CONFIG.get("min_score", 5):
            kind = "Open RFP" if (is_rfp or portal != "Google Alerts") else "Web mention"
            item = make_item({**src, "state": guess_state(text)}, title, href, text, score, kws, services, kind)
            found.append(item)

    if not found:  # whole email as one lead (some portals send plain-text lists)
        score, kws, services, is_rfp = score_text(f"{subject} {full}")
        if score >= CONFIG.get("min_score", 5):
            link = next((unwrap(a["href"]) for a in soup.find_all("a", href=True)
                         if a["href"].startswith("http") and "unsubscribe" not in a["href"].lower()), "")
            found.append(make_item({**src, "state": guess_state(full)}, subject, link or f"mailto:{sender}",
                                   f"{subject} {full}", score, kws, services, "Open RFP"))
    return found


def read_inbox():
    """Returns (items, status_entry)."""
    cfg = CONFIG.get("inbox", {}) or {}
    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    status = {"name": "Bot inbox (portal + Google Alert emails)", "state": "", "type": "inbox",
              "url": f"mailto:{user or ''}", "found": 0}
    if not cfg.get("enabled", True) or not (user and pw):
        status.update(status="off", message="Email secrets not set up yet")
        return [], status

    state_file = DATA / "inbox_state.json"
    try:
        state = json.loads(state_file.read_text())
    except Exception:
        state = {}

    items = []
    try:
        M = imaplib.IMAP4_SSL(os.getenv("IMAP_HOST") or "imap.gmail.com")
        M.login(user, pw)
        M.select(cfg.get("folder", "INBOX"), readonly=True)
        uidvalidity = M.response("UIDVALIDITY")[1][0]
        uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)
        last = int(state.get("last_uid", 0)) if state.get("uidvalidity") == uidvalidity else 0
        if last:
            typ, data = M.uid("search", None, f"UID {last + 1}:*")
        else:
            since = (TODAY - timedelta(days=cfg.get("first_run_days", 14))).strftime("%d-%b-%Y")
            typ, data = M.uid("search", None, f"SINCE {since}")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > last][-300:]
        for uid in uids:
            typ, msgdata = M.uid("fetch", str(uid), "(BODY.PEEK[])")
            if not msgdata or not msgdata[0]:
                continue
            try:
                items += items_from_email(email.message_from_bytes(msgdata[0][1]))
            except Exception as e:
                log(f"    could not read email {uid}: {e}")
        M.logout()
        if uids:
            state = {"uidvalidity": uidvalidity, "last_uid": max(uids)}
        else:
            state.setdefault("uidvalidity", uidvalidity)
        state_file.write_text(json.dumps(state))
        status.update(status="ok", found=len(items), message=f"{len(uids)} new emails read")
    except Exception as e:
        status.update(status="error", message=f"{type(e).__name__}: {str(e)[:150]}")
    return items, status
