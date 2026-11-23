#!/usr/bin/env python3
"""Talent Tech Radar ingestion.

RSS feeds -> keyword pre-filter -> dedupe -> Claude classification -> site/data/items.json

Only the headline, link, source, date and a short model-written summary are stored.
Article text is never saved, so the site links out instead of republishing.

Usage:
  python ingest.py            # normal run (needs ANTHROPIC_API_KEY for classification)
  python ingest.py --check    # just test which feeds respond
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import mktime

import feedparser
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "site" / "data"
ITEMS = DATA / "items.json"
SEEN = DATA / "seen.json"

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "10"))
MAX_PER_FEED = 40
MAX_NEW_PER_RUN = 120
MAX_ITEMS = 600
MAX_SEEN = 6000
BATCH = 15
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

CATEGORIES = [
    "Funding", "M&A", "Product", "Analyst", "Legal", "Event",
    "AI: HR orgs", "AI: Candidate experience", "AI: Recruiter experience",
    "AI: Trust & fraud", "AI: Regulation", "Other",
]

RELEVANT = re.compile(
    r"applicant tracking|\bATS\b|recruit|talent acqui|talent intelligence|hiring|hr tech|"
    r"human capital|candidate|sourcing|interview|workforce|\bHCM\b",
    re.I,
)

# Used only when no API key is set, so the pipeline still produces something.
RULES = [
    ("Analyst", r"magic quadrant|peak matrix|9-grid|marketscape"),
    ("Funding", r"raises|funding|series [a-e]\b|seed round"),
    ("M&A", r"acquir|acquisition|merger|buys "),
    ("Legal", r"lawsuit|court|ruling|settlement"),
    ("AI: Regulation", r"ai act|regulation|bias audit|compliance"),
    ("AI: Trust & fraud", r"deepfake|fraud|fake candidate"),
    ("Event", r"conference|summit|expo\b"),
    ("Product", r"launch|announce|introduc|unveil"),
]

SYSTEM = f"""You triage news for a recruitment-technology and applicant-tracking market tracker used by an HR-tech team.
You receive a JSON array of items (id, title, source, snippet). Return ONLY a JSON array with one object per id:
{{"id": "...", "relevant": true|false, "category": one of {CATEGORIES}, "vendors": ["company names mentioned"], "summary": "..."}}

Rules:
- relevant=true only for news about recruiting/ATS/HR technology vendors, funding, M&A, product launches, analyst ratings,
  events, legal/regulatory matters, or AI used in hiring and HR. Mark false for job ads, resume tips, listicles, SEO pages and unrelated news.
- category "AI: ..." applies when the main subject is AI in HR (HR orgs), candidate experience, recruiter experience, hiring fraud, or AI regulation.
  Otherwise use the business category (Funding, M&A, Product, Analyst, Legal, Event).
- summary: at most 30 words, in your own words, plain and factual. Never quote the source. No hype.
- Output JSON only, no commentary."""


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def tkey(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower().rsplit(" - ", 1)[0]).strip()


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch(sources, cutoff):
    for s in sources:
        try:
            feed = feedparser.parse(s["url"], agent="TalentTechRadar/1.0")
            if feed.bozo and not feed.entries:
                print(f"[warn] {s['name']}: no entries ({feed.bozo_exception})")
                continue
        except Exception as exc:  # network or parse failure should not stop the run
            print(f"[warn] {s['name']}: {exc}")
            continue
        for e in feed.entries[:MAX_PER_FEED]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            dt = datetime.fromtimestamp(mktime(ts), timezone.utc) if ts else datetime.now(timezone.utc)
            url = e.get("link")
            if not url or dt < cutoff:
                continue
            yield {
                "id": hashlib.sha1(url.encode()).hexdigest()[:16],
                "date": dt.date().isoformat(),
                "title": clean(e.get("title", "")),
                "url": url,
                "source": s["name"],
                "snippet": clean(e.get("summary", ""))[:400],
            }


def classify(batch):
    import anthropic

    client = anthropic.Anthropic()
    payload = [{k: i[k] for k in ("id", "title", "source", "snippet")} for i in batch]
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return {r["id"]: r for r in json.loads(text)}


def heuristic(item):
    blob = f"{item['title']} {item['snippet']}"
    for cat, pat in RULES:
        if re.search(pat, blob, re.I):
            return {"relevant": True, "category": cat, "vendors": [], "summary": item["snippet"][:160]}
    return {"relevant": True, "category": "Other", "vendors": [], "summary": item["snippet"][:160]}


def check_feeds():
    for s in yaml.safe_load((ROOT / "sources.yml").read_text())["feeds"]:
        feed = feedparser.parse(s["url"], agent="TalentTechRadar/1.0")
        print(f"{'OK ' if feed.entries else 'FAIL'} {s['name']}: {len(feed.entries)} entries")


def main():
    if "--check" in sys.argv:
        return check_feeds()

    sources = yaml.safe_load((ROOT / "sources.yml").read_text())["feeds"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    DATA.mkdir(parents=True, exist_ok=True)

    existing = load(ITEMS, {"items": []})["items"]
    seen = list(load(SEEN, []))
    known_ids = {i["id"] for i in existing} | set(seen)
    known_titles = {tkey(i["title"]) for i in existing}

    fresh, local = [], set()
    for it in fetch(sources, cutoff):
        k = tkey(it["title"])
        if it["id"] in known_ids or k in known_titles or k in local:
            continue
        if not RELEVANT.search(f"{it['title']} {it['snippet']}"):
            seen.append(it["id"])
            continue
        local.add(k)
        fresh.append(it)
    fresh = fresh[:MAX_NEW_PER_RUN]
    print(f"{len(fresh)} new candidate items")

    use_model = bool(os.getenv("ANTHROPIC_API_KEY"))
    added = []
    for i in range(0, len(fresh), BATCH):
        batch = fresh[i : i + BATCH]
        try:
            results = classify(batch) if use_model else {b["id"]: heuristic(b) for b in batch}
        except Exception as exc:  # leave the batch unseen so the next run retries it
            print(f"[warn] classification failed, will retry next run: {exc}")
            continue
        for b in batch:
            seen.append(b["id"])
            r = results.get(b["id"])
            if not r or not r.get("relevant"):
                continue
            cat = r.get("category") if r.get("category") in CATEGORIES else "Other"
            added.append({
                "id": b["id"], "date": b["date"], "title": b["title"], "url": b["url"],
                "source": b["source"], "category": cat,
                "vendors": r.get("vendors", [])[:6], "summary": r.get("summary", "")[:260],
            })

    items = sorted(existing + added, key=lambda x: x["date"], reverse=True)[:MAX_ITEMS]
    ITEMS.write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items},
        ensure_ascii=False, indent=1))
    SEEN.write_text(json.dumps(seen[-MAX_SEEN:]))
    print(f"added {len(added)}; total {len(items)}")


if __name__ == "__main__":
    main()
