#!/usr/bin/env python3
"""Talent Tech Radar ingestion.

RSS feeds -> relevance filter -> dedupe -> classify -> site/data/items.json

Classifier, chosen automatically (first that applies):
  1. Claude          if ANTHROPIC_API_KEY is set (paid, best summaries)
  2. GitHub Models   if GITHUB_TOKEN is set and the workflow has `models: read` (free tier)
  3. Built-in rules  no AI, no cost (vendor tagging, stricter filter, rule-based categories)
If an AI provider errors, the run falls back to the built-in rules so the site keeps updating.
Only headline, link, source, date and a short summary are stored; article text is never saved.

Usage:  python ingest.py            normal run
        python ingest.py --check    test which feeds respond
"""
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
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
BATCH = 15          # items per Claude request
BATCH_GH = 10       # smaller for GitHub Models (2,000-token output cap on the free tier)
GH_SLEEP = 5        # seconds between GitHub Models calls (free tier: 15 requests/minute)
CLAUDE_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
GH_MODEL = os.getenv("GH_MODEL", "openai/gpt-4.1-mini")
GH_URL = "https://models.github.ai/inference/chat/completions"
USE_GH = os.getenv("USE_GITHUB_MODELS", "1") != "0"

CATEGORIES = [
    "Funding", "M&A", "Product", "Analyst", "Legal", "Event",
    "AI: HR orgs", "AI: Candidate experience", "AI: Recruiter experience",
    "AI: Trust & fraud", "AI: Regulation", "Other",
]

# ---- Relevance ---------------------------------------------------------------------------
BROAD = re.compile(
    r"applicant tracking|\bATS\b|recruit|talent acqui|talent intelligence|hiring|hr tech|"
    r"human capital|candidate|sourcing|interview|workforce|\bHCM\b", re.I)
STRONG = re.compile(
    r"applicant tracking|\bATS\b|recruiting (software|tech|platform|agent|tool)|talent acquisition|"
    r"talent intelligence|hr tech|ai recruit|agentic recruit|ai interview|ai hiring|"
    r"hiring (software|platform|tool|fraud|bias)|candidate (experience|fraud|sourcing)|"
    r"(deepfake|fake) candidate|human capital management|\bHCM\b|\brecruiters?\b", re.I)
NEG = re.compile(
    r"\bjob (fair|openings)\b|we.re hiring|now hiring|resume tips|interview questions|salary guide|"
    r"career advice|how to (get|land)|\bcandidates?\b.{0,60}\b(election|senate|governor|mayor|congress|primary)\b|"
    r"\b(election|senate|governor|mayor|congress)\b.{0,60}\bcandidates?\b", re.I)

VENDORS = [
    "Workday", "SAP SuccessFactors", "SmartRecruiters", "Oracle", "iCIMS", "Darwinbox", "Greenhouse",
    "Lever", "Employ", "Ashby", "Phenom", "Eightfold", "HireVue", "Paradox", "HiredScore", "Avature",
    "Cornerstone", "Bullhorn", "Jobvite", "Workable", "Recruitee", "Teamtailor", "BambooHR", "Rippling",
    "ADP", "UKG", "Indeed", "LinkedIn", "ZipRecruiter", "Glassdoor", "Metaview", "Juicebox", "Paraform",
    "Mercor", "Humanly", "Findem", "hireEZ", "SeekOut", "Beamery", "Fountain", "Joveo", "Spott", "Jooba",
    "Archer", "hackajob", "HeyMilo", "micro1", "Checkr", "Sterling", "HireRight", "Cielo", "Randstad",
    "Adecco", "ManpowerGroup", "Gartner", "Fosway", "Everest Group", "Forrester", "IDC", "OpenAI",
]
# Common words or non-recruiting giants: tagged as vendors but not enough on their own to pass the filter.
AMBIGUOUS = {"Lever", "Archer", "Oracle", "Indeed", "LinkedIn", "ADP", "UKG", "Rippling", "Employ",
             "Gartner", "Fosway", "Everest Group", "Forrester", "IDC", "OpenAI", "Sterling", "Checkr"}
VENDOR_RX = re.compile(r"\b(" + "|".join(re.escape(v) for v in sorted(VENDORS, key=len, reverse=True)) + r")\b")

# ---- Rule-based categories (used when no AI provider is available) -----------------------
BUSINESS_RULES = [
    ("Analyst", r"magic quadrant|peak matrix|9-grid|marketscape|forrester wave|critical capabilities"),
    ("Legal", r"lawsuit|\bsued\b|court|ruling|settlement|class action|collective action"),
    ("Funding", r"\braises?\b|funding round|series [a-e]\b|seed round|\bvaluation\b"),
    ("M&A", r"acquir|acquisition|merger|\bbuys\b|to buy\b|takeover"),
    ("Event", r"conference|summit|expo\b|webinar|symposium"),
]
REG = r"ai act|bias audit|local law 144|regulat|legislation|compliance|\bbill\b|state law"
FRAUD = r"deepfake|fraud|fake candidate|identity (theft|verification)|impersonat|north korea"
AI_WORD = re.compile(r"\bAI\b|artificial intelligence|agentic|\bagents?\b|machine learning|\bLLM\b|generative|\bGPT\b|chatbot|automat", re.I)
AI_RULES = [
    ("AI: Candidate experience", r"candidate experience|job seekers?|applicants?\b|candidates? (use|using|expect)"),
    ("AI: Recruiter experience", r"recruiters?|sourcing|screening|interview|talent acquisition|hiring manager"),
    ("AI: HR orgs", r"\bHR\b|human resources|employee|workforce|payroll|\bHCM\b|people team"),
]
LAUNCH = r"launch|announce|introduc|unveil|release|rolls? out|debut"
AMT = re.compile(r"\$\s?\d[\d.,]*\s?(?:[MBK]\b|million|billion)", re.I)
ROUND = re.compile(r"series [a-e]\b|pre-seed|seed", re.I)

SYSTEM = f"""You triage news for a recruitment-technology and applicant-tracking market tracker used by an HR-tech team.
You receive a JSON array of items (id, title, source, snippet). Return ONLY a JSON array (no prose, no code fences) with one object per id:
{{"id": "...", "relevant": true|false, "category": one of {CATEGORIES}, "vendors": ["company names mentioned"], "summary": "..."}}

Rules:
- relevant=true only for news about recruiting/ATS/HR technology vendors, funding, M&A, product launches, analyst ratings,
  events, legal/regulatory matters, or AI used in hiring and HR. Mark false for job ads, resume tips, listicles, SEO pages and unrelated news.
- category "AI: ..." applies when the main subject is AI in HR (HR orgs), candidate experience, recruiter experience, hiring fraud, or AI regulation.
  Otherwise use the business category (Funding, M&A, Product, Analyst, Legal, Event).
- summary: at most 30 words, in your own words, plain and factual. Never quote the source. No hype.
- Output JSON only."""


# ---- Helpers -----------------------------------------------------------------------------
def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def tkey(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def tag_vendors(blob):
    return list(dict.fromkeys(VENDOR_RX.findall(blob)))[:6]


def trim(text, n=200):
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def parse_json(text):
    s, e = text.find("["), text.rfind("]")
    data = json.loads(text[s : e + 1])
    return {r["id"]: r for r in data if isinstance(r, dict) and "id" in r}


# ---- Fetch -------------------------------------------------------------------------------
def fetch(sources, cutoff):
    for s in sources:
        try:
            feed = feedparser.parse(s["url"], agent="TalentTechRadar/1.0")
            if feed.bozo and not feed.entries:
                print(f"[warn] {s['name']}: no entries ({feed.bozo_exception})")
                continue
        except Exception as exc:
            print(f"[warn] {s['name']}: {exc}")
            continue
        for e in feed.entries[:MAX_PER_FEED]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            dt = datetime.fromtimestamp(mktime(ts), timezone.utc) if ts else datetime.now(timezone.utc)
            url = e.get("link")
            if not url or dt < cutoff:
                continue
            title, source = clean(e.get("title", "")), s["name"]
            if source.startswith("Google News") and " - " in title:
                title, pub = title.rsplit(" - ", 1)   # Google News appends the publisher to the title
                source = pub.strip()
            yield {
                "id": hashlib.sha1(url.encode()).hexdigest()[:16],
                "date": dt.date().isoformat(),
                "title": title, "url": url, "source": source,
                "snippet": clean(e.get("summary", ""))[:400],
            }


# ---- Classifiers -------------------------------------------------------------------------
def classify_claude(batch):
    import anthropic

    payload = [{k: i[k] for k in ("id", "title", "source", "snippet")} for i in batch]
    msg = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL, max_tokens=4000, system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload)}])
    return parse_json("".join(b.text for b in msg.content if b.type == "text"))


def classify_github(batch):
    payload = [{k: i[k] for k in ("id", "title", "source", "snippet")} for i in batch]
    body = {"model": GH_MODEL, "temperature": 0, "max_tokens": 1800,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(payload)}]}
    req = urllib.request.Request(
        GH_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Content-Type": "application/json", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=90) as resp:   # HTTPError.code carries 401/403/429
        data = json.load(resp)
    return parse_json(data["choices"][0]["message"]["content"])


def rules_classify(item):
    blob = f"{item['title']} {item['snippet']}"
    vendors = tag_vendors(blob)
    if NEG.search(blob) or not (STRONG.search(blob) or any(v not in AMBIGUOUS for v in vendors)):
        return {"relevant": False}
    cat = next((c for c, p in BUSINESS_RULES if re.search(p, blob, re.I)), None)
    if cat is None and re.search(REG, blob, re.I):
        cat = "AI: Regulation"
    if cat is None and re.search(FRAUD, blob, re.I):
        cat = "AI: Trust & fraud"
    if cat is None and AI_WORD.search(blob):
        cat = next((c for c, p in AI_RULES if re.search(p, blob, re.I)), None)
    if cat is None:
        cat = "Product" if re.search(LAUNCH, blob, re.I) else "Other"
    return {"relevant": True, "category": cat, "vendors": vendors, "summary": rules_summary(item, cat, vendors)}


def rules_summary(item, cat, vendors):
    snip, title = item["snippet"], item["title"]
    # Google News snippets often just repeat the headline; only reuse a snippet that adds information.
    same = difflib.SequenceMatcher(None, snip.lower()[: len(title) + 40], title.lower()).ratio()
    if len(snip) > 40 and same < 0.6:
        return trim(snip)
    blob = f"{title} {snip}"
    bits = []
    if vendors:
        bits.append(", ".join(vendors[:3]))
    amt, rnd = AMT.search(blob), ROUND.search(blob)
    if amt:
        bits.append(amt.group(0) + (f" {rnd.group(0)}" if rnd else ""))
    lead = f"{cat}: " + "; ".join(bits) + ". " if bits else ""
    return f"{lead}Reported by {item['source']}."


def check_feeds():
    for s in yaml.safe_load((ROOT / "sources.yml").read_text())["feeds"]:
        feed = feedparser.parse(s["url"], agent="TalentTechRadar/1.0")
        print(f"{'OK  ' if feed.entries else 'FAIL'} {s['name']}: {len(feed.entries)} entries")


# ---- Main --------------------------------------------------------------------------------
def pick_provider():
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude"
    if USE_GH and os.getenv("GITHUB_TOKEN"):
        return "github-models"
    return "rules"


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
        blob = f"{it['title']} {it['snippet']}"
        if not BROAD.search(blob) or NEG.search(blob):
            seen.append(it["id"])
            continue
        local.add(k)
        fresh.append(it)
    fresh = fresh[:MAX_NEW_PER_RUN]

    provider = pick_provider()
    print(f"{len(fresh)} new candidate items; classifier: {provider}")
    size = BATCH_GH if provider == "github-models" else BATCH
    added = []
    for i in range(0, len(fresh), size):
        batch = fresh[i : i + size]
        try:
            if provider == "claude":
                results = classify_claude(batch)
            elif provider == "github-models":
                results = classify_github(batch)
                time.sleep(GH_SLEEP)
            else:
                results = {}
        except Exception as exc:
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            print(f"[warn] {provider} failed ({status}): {exc}")
            if status == 429:
                print("Rate limited. Remaining items stay unseen and are retried next run.")
                break
            print("Falling back to built-in rules for the rest of this run.")
            provider, results = "rules", {}
        for b in batch:
            seen.append(b["id"])
            r = results.get(b["id"])
            via = provider
            if r is None:
                r, via = rules_classify(b), "rules"
            if not r.get("relevant"):
                continue
            cat = r.get("category") if r.get("category") in CATEGORIES else "Other"
            blob = f"{b['title']} {b['snippet']}"
            vendors = list(dict.fromkeys((r.get("vendors") or []) + tag_vendors(blob)))[:6]
            added.append({"id": b["id"], "date": b["date"], "title": b["title"], "url": b["url"],
                          "source": b["source"], "category": cat, "vendors": vendors,
                          "summary": (r.get("summary") or "")[:260], "via": via})

    items = sorted(existing + added, key=lambda x: x["date"], reverse=True)[:MAX_ITEMS]
    ITEMS.write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items},
        ensure_ascii=False, indent=1))
    SEEN.write_text(json.dumps(seen[-MAX_SEEN:]))
    print(f"added {len(added)}; total {len(items)}")


if __name__ == "__main__":
    main()
