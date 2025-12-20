#!/usr/bin/env python3
"""
People First Research updater

- Reads updater/journals.yml
- Resolves journal ISSNs via Crossref
- Pulls recent works per ISSN
- Applies person-focused inclusion/exclusion logic
- Writes papers.json (repo root) in the schema expected by your static site

Crossref API docs:
- https://www.crossref.org/documentation/retrieve-metadata/rest-api/
"""

from __future__ import annotations


import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
import yaml

ASD_BUFFER_MAX = 3

def assign_framing(title: str, abstract: str) -> str | None:
    """
    Assigns a framing label based purely on observable language use.
    No inference is made about author intent, epistemology, or quality.
    """

    text = f"{title or ''} {abstract or ''}".lower()

    # Identity-first language
    identity_first_patterns = [
        r"\bautistic person\b",
        r"\bautistic people\b",
        r"\bautistic adult[s]?\b",
        r"\bautistic child(?:ren)?\b",
        r"\bautistic individual[s]?\b",
        r"\bautistic student[s]?\b",
    ]

    # Person-first language
    person_first_patterns = [
        r"\bperson with autism\b",
        r"\bpeople with autism\b",
        r"\badult[s]? with autism\b",
        r"\bchild(?:ren)? with autism\b",
        r"\bindividual[s]? with autism\b",
        r"\bstudent[s]? with autism\b",
    ]

    # Diagnostic terminology
    asd_patterns = [
        r"\bautism spectrum disorder\b",
        r"\basd\b",
    ]

    def count_matches(patterns):
        return sum(len(re.findall(p, text)) for p in patterns)

    identity_count = count_matches(identity_first_patterns)
    person_first_count = count_matches(person_first_patterns)
    asd_count = count_matches(asd_patterns)

    # --- Rule hierarchy ---

    if identity_count > 0 and person_first_count > 0:
        return "Mixed or transitional framing"

    if identity_count > 0 and person_first_count == 0:
        if asd_count == 0:
            return "Neurodiversity-affirming"
        if asd_count <= ASD_BUFFER_MAX and identity_count >= asd_count + 1:
            return "Neurodiversity-affirming"
        return "Mixed or transitional framing"

    if person_first_count > 0 and identity_count == 0:
        if asd_count == 0:
            return "Person-first language used"
        return "Mixed or transitional framing"

    if asd_count > 0:
        return "Medical or diagnostic framing"

    return None

ROOT = Path(__file__).resolve().parents[1]
JOURNALS_YML = ROOT / "updater" / "journals.yml"
OUTPUT_JSON = ROOT / "papers.json"
CACHE_DIR = ROOT / "updater" / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
ISSN_CACHE = CACHE_DIR / "journal_issn_cache.json"

CROSSREF_BASE = "https://api.crossref.org"


# ----------------------------
# Config you can tune later
# ----------------------------

# How far back to look on each run. Start with 60–120 days for a manageable feed.
LOOKBACK_DAYS = 120

# Maximum items per ISSN query (Crossref supports up to 1000 rows; keep conservative).
ROWS_PER_ISSN = 200

# Rate limiting. Crossref asks users to be responsible; keep requests modest.
SLEEP_SECONDS_BETWEEN_REQUESTS = 0.2

# Crossref etiquette: set a descriptive UA and include a contact email if possible.
USER_AGENT = "PeopleFirstResearch/1.0 (GitHub Pages; contact: bpxr00@gmail.com)"
MAILTO = "bpxr00@gmail.com"


# ----------------------------
# Person-focused filter logic
# ----------------------------

NEURO_TERMS = [
    r"\bautis(m|tic)\b",
    r"\bASD\b",
    r"\bADHD\b",
    r"\battention[-\s]?deficit\b",
    r"\bAuDHD\b",
]

# Terms that signal lived experience, outcomes, access, practice, services, rights, participation
PERSON_FOCUSED_TERMS = [
    r"\blived experience\b",
    r"\bexperience(s)?\b",
    r"\bquality of life\b",
    r"\bwellbeing\b",
    r"\bparticipation\b",
    r"\baccess(ible|ibility)?\b",
    r"\bbarrier(s)?\b",
    r"\bfacilitator(s)?\b",
    r"\bcommunication\b",
    r"\bsensory\b",
    r"\bhealthcare\b",
    r"\bemergency\b",
    r"\beducation(al)?\b",
    r"\bemployment\b",
    r"\bservice(s)?\b",
    r"\bservice design\b",
    r"\bintervention(s)?\b",
    r"\bsupport(s)?\b",
    r"\bdisclosure\b",
    r"\bstigma\b",
    r"\bdiscrimination\b",
    r"\btrauma[-\s]?informed\b",
    r"\bpatient\b",
    r"\bcare\b",
    r"\bneeds\b",
    r"\bpreferences\b",
    r"\bco[-\s]?produ(ced|ction)\b",
    r"\bparticipatory\b",
]

# Exclusion terms for mechanistic drift
MECHANISTIC_TERMS = [
    r"\bgen(et|omic|etics?)\b",
    r"\bpolymorphism\b",
    r"\bmethylation\b",
    r"\btranscriptom(e|ic)\b",
    r"\bproteom(e|ic)\b",
    r"\bmetabolom(e|ic)\b",
    r"\bbiomarker(s)?\b",
    r"\bneuroimaging\b",
    r"\bfMRI\b",
    r"\bMRI\b",
    r"\bPET\b",
    r"\bDTI\b",
    r"\bEEG\b",
    r"\bMEG\b",
    r"\bmicrobiome\b",
    r"\bcytokine(s)?\b",
    r"\banimal model\b",
    r"\bmouse\b",
    r"\brat\b",
    r"\bzebrafish\b",
]


DOMAIN_RULES: List[Tuple[str, List[str]]] = [
    ("Emergency care", [r"\bemergency department\b", r"\bED\b", r"\bA&E\b", r"\bemergency\b", r"\burgent\b"]),
    ("Healthcare", [r"\bhealthcare\b", r"\bclinic(al)?\b", r"\bhospital\b", r"\bprimary care\b", r"\bpatient\b"]),
    ("Education", [r"\bschool\b", r"\beducation\b", r"\bteacher\b", r"\buniversity\b", r"\bstudent\b"]),
    ("Employment", [r"\bemployment\b", r"\bwork(place)?\b", r"\bjob\b", r"\boccupation(al)?\b"]),
    ("Mental health", [r"\banxiety\b", r"\bdepression\b", r"\bmental health\b", r"\btrauma\b", r"\bstress\b"]),
    ("Communication", [r"\bcommunication\b", r"\blanguage\b", r"\binteraction\b"]),
    ("Sensory environment", [r"\bsensory\b", r"\bnoise\b", r"\blight(s|ing)?\b", r"\boverload\b"]),
    ("Service design", [r"\bservice design\b", r"\bpathway\b", r"\baccess\b", r"\bquality improvement\b", r"\bimplementation\b"]),
    ("Quality of life", [r"\bquality of life\b", r"\bwellbeing\b", r"\bparticipation\b"]),
]

TAG_RULES: List[Tuple[str, List[str]]] = [
    ("lived experience", [r"\blived experience\b", r"\bnarrative\b", r"\bqualitative\b", r"\binterview(s)?\b"]),
    ("access", [r"\baccess\b", r"\bbarrier(s)?\b", r"\bfacilitator(s)?\b"]),
    ("communication", [r"\bcommunication\b", r"\blanguage\b"]),
    ("sensory", [r"\bsensory\b", r"\boverload\b"]),
    ("service improvement", [r"\bquality improvement\b", r"\bservice design\b", r"\bimplementation\b", r"\bpathway\b"]),
    ("disclosure", [r"\bdisclosure\b"]),
    ("participatory", [r"\bco[-\s]?produ(ced|ction)\b", r"\bparticipatory\b"]),
]


def _rx_any(patterns: List[str]) -> re.Pattern:
    return re.compile("|".join(patterns), flags=re.IGNORECASE)


RX_NEURO = _rx_any(NEURO_TERMS)
RX_PERSON = _rx_any(PERSON_FOCUSED_TERMS)
RX_MECH = _rx_any(MECHANISTIC_TERMS)


@dataclass
class JournalItem:
    title: str
    tier: str  # "1A" or "1B"


def load_watchlist() -> List[JournalItem]:
    raw = yaml.safe_load(JOURNALS_YML.read_text(encoding="utf-8"))
    out: List[JournalItem] = []
    for t in raw.get("journals", {}).get("tier_1a_person_focused", []):
        out.append(JournalItem(title=t, tier="1A"))
    for t in raw.get("journals", {}).get("tier_1b_strict_filter", []):
        out.append(JournalItem(title=t, tier="1B"))
    return out


def load_issn_cache() -> Dict[str, List[str]]:
    if ISSN_CACHE.exists():
        return json.loads(ISSN_CACHE.read_text(encoding="utf-8"))
    return {}


def save_issn_cache(cache: Dict[str, List[str]]) -> None:
    ISSN_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def crossref_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    if MAILTO:
        params = dict(params)
        params["mailto"] = MAILTO
    url = f"{CROSSREF_BASE}{path}"
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    time.sleep(SLEEP_SECONDS_BETWEEN_REQUESTS)
    return r.json()


def resolve_issns(journal_title: str, cache: Dict[str, List[str]]) -> List[str]:
    """
    Resolve a journal title to ISSNs using /journals?query=<title>.
    Crossref supports journal endpoints and querying for journal metadata. :contentReference[oaicite:1]{index=1}
    """
    if journal_title in cache:
        return cache[journal_title]

    data = crossref_get("/journals", {"query": journal_title, "rows": 5})
    items = data.get("message", {}).get("items", []) or []

    # Pick the best match by crude string containment
    best: Optional[Dict[str, Any]] = None
    jt = journal_title.lower()
    for it in items:
        title = (it.get("title") or "").lower()
        if title == jt:
            best = it
            break
        if jt in title or title in jt:
            best = it if best is None else best

    if best is None and items:
        best = items[0]

    issns: List[str] = []
    if best:
        issns = best.get("ISSN", []) or []
    issns = [i.strip() for i in issns if isinstance(i, str)]

    cache[journal_title] = issns
    return issns


def extract_title(item: Dict[str, Any]) -> str:
    t = item.get("title") or []
    if isinstance(t, list) and t:
        return t[0]
    if isinstance(t, str):
        return t
    return ""


def extract_authors(item: Dict[str, Any]) -> List[str]:
    authors = item.get("author") or []
    out = []
    for a in authors:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = " ".join(x for x in [given, family] if x).strip()
        if name:
            out.append(name)
    return out


def extract_journal(item: Dict[str, Any]) -> str:
    ct = item.get("container-title") or []
    if isinstance(ct, list) and ct:
        return ct[0]
    if isinstance(ct, str):
        return ct
    return ""


def extract_dates(item: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    # Crossref date fields vary; prioritise "published-print", then "published-online", then "issued"
    for key in ["published-print", "published-online", "issued"]:
        obj = item.get(key)
        if not isinstance(obj, dict):
            continue
        parts = obj.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            dp = parts[0]
            y = int(dp[0]) if len(dp) >= 1 else None
            m = int(dp[1]) if len(dp) >= 2 else 1
            d = int(dp[2]) if len(dp) >= 3 else 1
            try:
                iso = date(y, m, d).isoformat()
            except Exception:
                iso = None
            return iso, y
    return None, None


def extract_abstract(item: Dict[str, Any]) -> Optional[str]:
    """
    Crossref abstracts, when present, are often JATS XML fragments.
    We strip tags and compress whitespace.
    """
    ab = item.get("abstract")
    if not isinstance(ab, str) or not ab.strip():
        return None
    # Remove XML/HTML tags
    text = re.sub(r"<[^>]+>", " ", ab)
    text = re.sub(r"\s+", " ", text).strip()
    # Keep it modest for a one-page UI
    if len(text) > 1200:
        text = text[:1200].rstrip() + "…"
    return text


def classify_neurotype(text: str) -> List[str]:
    out: Set[str] = set()
    t = text.lower()
    if "audhd" in t:
        out.add("AuDHD")
    if "adhd" in t or "attention deficit" in t:
        out.add("ADHD")
    if "autis" in t or "asd" in t:
        out.add("Autism")
    return sorted(out)


def apply_rules(text: str, journal_tier: str) -> Tuple[bool, Dict[str, List[str]]]:
    """
    Returns (include, annotations) where annotations include domains and tags.
    """
    t = text or ""

    has_neuro = bool(RX_NEURO.search(t))
    if not has_neuro:
        return False, {}

    has_person = bool(RX_PERSON.search(t))
    has_mech = bool(RX_MECH.search(t))

    # Tiering behaviour:
    # - Tier 1A: include if neuro present AND (person-focused OR not mechanistic)
    # - Tier 1B: include only if neuro AND person-focused AND not mechanistic
    if journal_tier == "1B":
        if not has_person:
            return False, {}
        if has_mech:
            return False, {}
    else:
        # 1A
        if has_mech and not has_person:
            return False, {}

    domains: List[str] = []
    tags: List[str] = []

    for name, pats in DOMAIN_RULES:
        if re.search("|".join(pats), t, flags=re.IGNORECASE):
            domains.append(name)

    for name, pats in TAG_RULES:
        if re.search("|".join(pats), t, flags=re.IGNORECASE):
            tags.append(name)

    return True, {"domains": sorted(set(domains)), "tags": sorted(set(tags))}


def fetch_recent_works_for_issn(issn: str, from_date: str) -> List[Dict[str, Any]]:
    """
    Uses Crossref's /journals/{issn}/works endpoint. :contentReference[oaicite:2]{index=2}
    """
    filt = f"from-pub-date:{from_date}"
    data = crossref_get(
        f"/journals/{issn}/works",
        {
            "filter": filt,
            "sort": "published",
            "order": "desc",
            "rows": ROWS_PER_ISSN,
        },
    )
    return data.get("message", {}).get("items", []) or []


def main() -> None:
    watchlist = load_watchlist()
    issn_cache = load_issn_cache()

    from_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    seen_doi: Set[str] = set()
    papers_out: List[Dict[str, Any]] = []

    for j in watchlist:
        issns = resolve_issns(j.title, issn_cache)
        if not issns:
            # Keep going; journal title resolution can fail for some entries
            continue

        for issn in issns:
            try:
                works = fetch_recent_works_for_issn(issn, from_date)
            except requests.HTTPError:
                continue

            for item in works:
                doi = (item.get("DOI") or "").strip().lower()
                if not doi:
                    continue
                if doi in seen_doi:
                    continue

                title = extract_title(item).strip()
                if not title:
                    continue

                journal_name = extract_journal(item).strip() or j.title
                authors = extract_authors(item)
                url = f"https://doi.org/{doi}"
                published_date, year = extract_dates(item)
                abstract = extract_abstract(item)

                # Build a text blob for rules
                blob = " ".join([title, abstract or "", journal_name, " ".join(authors)])

                include, ann = apply_rules(blob, j.tier)
                if not include:
                    continue

                neurotype = classify_neurotype(blob)

                paper_obj: Dict[str, Any] = {
                    "id": doi,
                    "title": title,
                    "authors": authors,
                    "journal": journal_name,
                    "year": year or (int(published_date[:4]) if published_date else None),
                    "published_date": published_date,
                    "doi": doi,
                    "url": url,
                    "neurotype": neurotype,
                    "domains": ann.get("domains", []),
                    "tags": ann.get("tags", []),

                if abstract:
                    paper_obj["abstract"] = abstract
                
                # NEW: language framing tag (based on observable wording in title/abstract)
                framing = assign_framing(title=title, abstract=abstract or "")
                if framing:
                    paper_obj["framing"] = framing
                
                # Clean None fields
                paper_obj = {k: v for k, v in paper_obj.items() if v not in (None, "", [])}


                papers_out.append(paper_obj)
                seen_doi.add(doi)

    # Sort newest first
    def sort_key(p: Dict[str, Any]) -> Tuple[int, str]:
        y = int(p.get("year", 0) or 0)
        pd = p.get("published_date", "") or ""
        return (y, pd)

    papers_out.sort(key=sort_key, reverse=True)

    out = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_summary": "Updated daily from Crossref using a journal watchlist plus person-focused filters",
        "schema_version": "1.0",
        "papers": papers_out,
    }

    OUTPUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    save_issn_cache(issn_cache)


if __name__ == "__main__":
    main()
