"""
Emails a digest of NEW items found in today's run.
Settings come from GitHub Secrets (see README):
  SMTP_USER, SMTP_PASS, EMAIL_TO  (required)
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587), DASHBOARD_URL (optional)
"""

import html
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

DATA = Path(__file__).parent / "data"
ORDER = ["Open RFP", "Board signal", "Web mention"]
COLORS = {"Open RFP": "#0b7a4b", "Board signal": "#8a5a00", "Web mention": "#3b5bdb"}


def build_html(items, dashboard):
    rows = []
    for kind in ORDER:
        group = [i for i in items if i["type"] == kind]
        if not group:
            continue
        rows.append(f'<h3 style="margin:22px 0 6px;color:{COLORS[kind]}">{kind} ({len(group)})</h3>')
        for i in sorted(group, key=lambda x: -x.get("score", 0)):
            due = f' &middot; <b>Due {i["due_date"]}</b>' if i.get("due_date") else ""
            rows.append(
                '<div style="padding:10px 12px;margin:6px 0;border:1px solid #e3e3e3;border-radius:8px">'
                f'<a href="{html.escape(i["url"])}" style="font-weight:600;color:#111;text-decoration:none">'
                f'{html.escape(i["title"])}</a><br>'
                f'<span style="color:#666;font-size:13px">{i.get("state","")} &middot; {html.escape(i["source"])}{due}'
                f' &middot; <b>{html.escape(", ".join(i.get("services", [])) or "Related")}</b></span><br>'
                f'<span style="color:#444;font-size:13px">{html.escape(i.get("snippet",""))}</span></div>'
            )
    link = f'<p><a href="{html.escape(dashboard)}">Open the full dashboard</a></p>' if dashboard else ""
    return (
        '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:720px">'
        f'<h2 style="margin:0">RFP Finder: {len(items)} new item{"s" if len(items) != 1 else ""}</h2>'
        f'{link}{"".join(rows)}'
        '<p style="color:#888;font-size:12px;margin-top:24px">Sent automatically by the OOOL RFP Finder.</p></div>'
    )


def main():
    try:
        items = json.loads((DATA / "new_today.json").read_text(encoding="utf-8")).get("items", [])
    except FileNotFoundError:
        items = []
    if not items:
        print("No new items today, no email sent.")
        return 0

    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        print("Email secrets not set up yet (SMTP_USER, SMTP_PASS, EMAIL_TO). Skipping email.")
        return 0

    recipients = [x.strip() for x in to.split(",") if x.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"RFP Finder: {len(items)} new tutoring lead{'s' if len(items) != 1 else ''}"
    msg["From"] = f"OOOL RFP Finder <{user}>"
    msg["To"] = ", ".join(recipients)
    plain = "\n\n".join(f"[{i['type']}] {i['title']}\n{i['url']}" for i in items)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(build_html(items, os.getenv("DASHBOARD_URL", "")), "html"))

    host, port = os.getenv("SMTP_HOST") or "smtp.gmail.com", int(os.getenv("SMTP_PORT") or 587)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, recipients, msg.as_string())
    print(f"Email sent to {len(recipients)} recipient(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
