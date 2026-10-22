"""
Heads-up detector: reads meeting agendas, minutes, budgets and plans and finds
mentions that could turn into work for OOOL.

Categories
  Funding             Title I / federal money / grants
  New positions       interventionists, tutors, teacher assistants, paras being hired or added
  Services            tutoring / intervention / after school / therapy services being bought
  Competitor          a competitor named (contracts, renewals)
Each heads-up is "hot" when there is action language (approve, contract, hire, add
positions, RFP...) next to it, otherwise it's a regular heads-up for the weekly roundup.
"""

import re

from common import CONFIG, clean, make_id

SIG = CONFIG.get("headsup", {}) or {}


def _rx(terms):
    terms = sorted({t.lower() for t in terms if t}, key=len, reverse=True)
    if not terms:
        return re.compile(r"(?!x)x")
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in terms) + r")(?![a-z0-9])")


FUNDING = _rx(SIG.get("funding", []))
POSITIONS = _rx(SIG.get("positions", []))
SERVICES = _rx(SIG.get("services", []))
COMPETITORS = _rx(SIG.get("competitors", []))
HIRE = _rx(SIG.get("hiring_words", []))
BUY = _rx(SIG.get("buying_words", []))
IGNORE = _rx(SIG.get("ignore", []))

CATEGORY_ORDER = ["Competitor", "Services", "New positions", "Funding"]

ITEM_SPLIT = re.compile(r"(?<=[.;:!?])\s+|\s+(?=(?:item\s+)?\d{1,2}(?:\.\d{1,2}){0,2}\.?\s+[A-Z])|\s+(?=[A-Z]\.\s+[A-Z])")


def _chunks(text):
    """Split into sentences / numbered agenda items, then group each match with its
    neighbors so 'Approve ... ' and the details right after it stay together."""
    parts = [p for p in ITEM_SPLIT.split(text) if p and p.strip()]
    low_parts = [p.lower() for p in parts]
    rxs = [FUNDING, POSITIONS, SERVICES, COMPETITORS]
    hits = [i for i, p in enumerate(low_parts) if any(rx.search(p) for rx in rxs)]
    groups = []
    for i in hits:
        if groups and i - groups[-1][-1] <= 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    for g in groups:
        a, b = max(0, g[0] - 1), min(len(parts), g[-1] + 2)  # one sentence of context each side
        chunk = " ".join(parts[a:b])
        if len(chunk) > 1400:
            chunk = " ".join(parts[g[0]:g[-1] + 1])[:1400]
        yield chunk


def _snippet(chunk, term, width=320):
    i = chunk.lower().find(term.lower())
    if i < 0:
        return chunk[:width]
    start = max(0, i - width // 2)
    return ("..." if start else "") + chunk[start:start + width].strip() + "..."


def find_headsups(text, meeting, kind="Board meeting"):
    """
    text:    plain text of an agenda / minutes / plan
    meeting: dict with district, state, leaid, title, date, url
    Returns a list of heads-up dicts.
    """
    text = clean(text)
    out, seen = [], set()
    for chunk in _chunks(IGNORE.sub(" ", text) if IGNORE.pattern != "(?!x)x" else text):
        chunk_low = chunk.lower()
        comp = sorted(set(COMPETITORS.findall(chunk_low)))
        serv = sorted(set(SERVICES.findall(chunk_low)))
        pos = sorted(set(POSITIONS.findall(COMPETITORS.sub(" ", chunk_low))))  # "Varsity Tutors" isn't a job
        fund = sorted(set(FUNDING.findall(chunk_low)))
        hire = bool(HIRE.search(chunk_low))
        buy = bool(BUY.search(chunk_low))

        cats, hot = [], False
        if comp:
            cats.append("Competitor")
            hot = hot or buy
        if serv:
            cats.append("Services")
            hot = hot or buy
        if pos:
            cats.append("New positions")
            hot = hot or hire
        if fund:
            cats.append("Funding")
            hot = hot or bool(serv or pos)   # Title I money tied to tutoring / interventionists
        if not cats:
            continue
        # A lone Title I mention with nothing else around it is too common to be useful
        if cats == ["Funding"] and not SIG.get("keep_plain_funding", True):
            continue

        terms = comp + serv + pos + fund
        lead = (comp or serv or pos or fund)[0]
        key = make_id(meeting.get("url", ""), lead, chunk_low[:120])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": key,
            "district": meeting.get("district", ""),
            "state": meeting.get("state", ""),
            "leaid": meeting.get("leaid", ""),
            "source": kind,
            "meeting": meeting.get("title", ""),
            "date": meeting.get("date", ""),
            "url": meeting.get("url", ""),
            "categories": sorted(cats, key=CATEGORY_ORDER.index),
            "hot": hot,
            "terms": terms[:10],
            "snippet": _snippet(chunk, lead),
        })
    # keep the strongest few per document so one huge agenda doesn't flood the list
    out.sort(key=lambda h: (not h["hot"], CATEGORY_ORDER.index(h["categories"][0])))
    return out[: SIG.get("max_per_document", 8)]
