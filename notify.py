"""
Emails:
  Every day   - HOT heads-ups found today + new bids (only if there is something)
  Mondays     - weekly roundup of the other heads-ups from the last 7 days
Settings come from GitHub Secrets: SMTP_USER, SMTP_PASS, EMAIL_TO (+ optional SMTP_HOST, SMTP_PORT, DASHBOARD_URL)
"""

import html
import json
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CONFIG = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
PRIORITY = [s.upper() for s in CONFIG.get("priority_states", [])]
DASH = os.getenv("DASHBOARD_URL", "")
TODAY = date.today()
MAX_IN_EMAIL = 60
CAT_ORDER = ["Competitor", "Services", "New positions", "Funding"]


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def esc(x):
    return html.escape(str(x or ""))


def card(title, url, who, extra, snippet):
    return ('<div style="padding:10px 12px;margin:6px 0;border:1px solid #e3e3e3;border-radius:8px">'
            f'<a href="{esc(url)}" style="font-weight:600;color:#111;text-decoration:none">{esc(title)}</a><br>'
            f'<span style="color:#666;font-size:13px">{who}{extra}</span><br>'
            f'<span style="color:#444;font-size:13px">{esc(snippet)}</span></div>')


def sort_heads(items):
    return sorted(items, key=lambda h: (h.get("state") not in PRIORITY, not h.get("hot"),
                                        CAT_ORDER.index((h.get("categories") or ["Funding"])[0]),
                                        h.get("state", ""), h.get("district", "")))


def heads_html(items, heading, color):
    if not items:
        return ""
    out = [f'<h3 style="margin:22px 0 6px;color:{color}">{heading} ({len(items)})</h3>']
    for h in sort_heads(items)[:MAX_IN_EMAIL]:
        who = f'{esc(h.get("district"))}, {esc(h.get("state"))} &middot; {esc(h.get("source"))} {esc(h.get("date"))}'
        extra = f' &middot; <b>{esc(", ".join(h.get("categories", [])))}</b>'
        out.append(card(h.get("meeting") or "Board meeting", h.get("url"), who, extra, h.get("snippet")))
    if len(items) > MAX_IN_EMAIL:
        out.append(f'<p style="color:#666">+ {len(items) - MAX_IN_EMAIL} more on the dashboard.</p>')
    return "".join(out)


def bids_html(items):
    if not items:
        return ""
    out = [f'<h3 style="margin:22px 0 6px;color:#0b7a4b">New bids ({len(items)})</h3>']
    for i in items[:MAX_IN_EMAIL]:
        due = f' &middot; <b>Due {esc(i["due_date"])}</b>' if i.get("due_date") else ""
        who = f'{esc(i.get("state"))} &middot; {esc(i.get("source"))}'
        out.append(card(i.get("title"), i.get("url"), who, due + f' &middot; {esc(", ".join(i.get("services", [])))}',
                        i.get("snippet")))
    return "".join(out)


def wrap(title, body):
    link = f'<p><a href="{esc(DASH)}">Open the dashboard</a></p>' if DASH else ""
    return ('<div style="font-family:Segoe UI,Arial,sans-serif;max-width:720px">'
            f'<h2 style="margin:0">{esc(title)}</h2>{link}{body}'
            '<p style="color:#888;font-size:12px;margin-top:24px">Sent automatically by the OOOL RFP Finder.</p></div>')


def send(subject, html_body):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        print("Email secrets not set up yet. Skipping:", subject)
        return
    recipients = [x.strip() for x in to.split(",") if x.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, f"OOOL RFP Finder <{user}>", ", ".join(recipients)
    msg.attach(MIMEText("Open this email in a viewer that shows HTML, or visit the dashboard: " + DASH, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(os.getenv("SMTP_HOST") or "smtp.gmail.com", int(os.getenv("SMTP_PORT") or 587), timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, recipients, msg.as_string())
    print(f"Sent '{subject}' to {len(recipients)} recipient(s).")


def main():
    # ---- daily: hot heads-ups + new bids
    new_heads = load(DATA / "headsups_new.json", {"items": []}).get("items", [])
    hot = [h for h in new_heads if h.get("hot")]
    bids = load(DATA / "new_today.json", {"items": []}).get("items", [])
    if hot or bids:
        parts = []
        if hot:
            parts.append(f"{len(hot)} hot heads-up{'s' if len(hot) != 1 else ''}")
        if bids:
            parts.append(f"{len(bids)} new bid{'s' if len(bids) != 1 else ''}")
        subject = "RFP Finder: " + " + ".join(parts)
        send(subject, wrap(subject, heads_html(hot, "Hot heads-ups", "#b42318") + bids_html(bids)))
    else:
        print("Nothing hot or new today, no daily email.")

    # ---- Mondays: weekly roundup of everything else from the last 7 days
    if TODAY.weekday() == 0:
        week_ago = (TODAY - timedelta(days=7)).isoformat()
        items = []
        for p in (DATA / "headsups").glob("*.json"):
            if p.stem == "index":
                continue
            items += [h for h in load(p, {"items": []}).get("items", [])
                      if h.get("first_seen", "") > week_ago and not h.get("hot")]
        if items:
            subject = f"RFP Finder weekly roundup: {len(items)} heads-up{'s' if len(items) != 1 else ''}"
            by_cat = {}
            for h in items:
                by_cat.setdefault((h.get("categories") or ["Funding"])[0], []).append(h)
            body = "".join(heads_html(by_cat[c], c, "#1d4ed8") for c in CAT_ORDER if c in by_cat)
            send(subject, wrap(subject, body))
        else:
            print("No heads-ups for the weekly roundup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
