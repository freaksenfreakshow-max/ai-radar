#!/usr/bin/env python3
"""
AI News Crawler
===============
Henter AI-nyheder fra RSS/Atom-feeds (defineret i feeds.json)
og gemmer dem samlet i data/articles.json, som hjemmesiden læser.

Kør den med:  python3 crawler.py

Bruger KUN Pythons standardbibliotek - ingen pip install nødvendig.
"""

import json
import re
import html
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----- Indstillinger ---------------------------------------------------------

ROOT = Path(__file__).parent
FEEDS_FIL = ROOT / "feeds.json"
OUTPUT_FIL = ROOT / "data" / "articles.json"
MAX_PER_FEED = 25          # max artikler vi tager fra hvert feed
MAX_DAGE_GAMMEL = 30       # smid artikler væk der er ældre end 30 dage
TIMEOUT_SEK = 20

USER_AGENT = "Mozilla/5.0 (compatible; AINewsCrawler/1.0; +https://github.com)"

# Atom-feeds bruger namespaces - dem skal vi kende for at finde felterne
NS = {"atom": "http://www.w3.org/2005/Atom"}


# ----- Hjælpefunktioner ------------------------------------------------------

def hent_url(url: str) -> bytes:
    """Henter indholdet af en URL."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEK) as svar:
        return svar.read()


def rens_tekst(raa: str | None, max_laengde: int = 300) -> str:
    """Fjerner HTML-tags og forkorter teksten til et pænt resumé."""
    if not raa:
        return ""
    tekst = re.sub(r"<[^>]+>", " ", raa)          # fjern HTML-tags
    tekst = html.unescape(tekst)                   # &amp; -> &  osv.
    tekst = re.sub(r"\s+", " ", tekst).strip()     # ryd op i mellemrum
    # arXiv-resuméer starter med "arXiv:1234.5678v1 Announce Type: new Abstract: ..."
    tekst = re.sub(r"^arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*", "", tekst)
    if len(tekst) > max_laengde:
        tekst = tekst[:max_laengde].rsplit(" ", 1)[0] + "…"
    return tekst


def parse_dato(dato_str: str | None) -> datetime | None:
    """Prøver at forstå de datoformater, feeds typisk bruger."""
    if not dato_str:
        return None
    dato_str = dato_str.strip()
    # RFC 822: "Tue, 21 Jul 2026 08:00:00 +0000"  (RSS)
    try:
        return parsedate_to_datetime(dato_str)
    except (ValueError, TypeError):
        pass
    # ISO 8601: "2026-07-21T08:00:00Z"  (Atom)
    try:
        return datetime.fromisoformat(dato_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_rss(rod: ET.Element) -> list[dict]:
    """Læser artikler ud af et RSS 2.0-feed (<rss><channel><item>...)."""
    artikler = []
    for item in rod.iter("item"):
        artikler.append({
            "titel": rens_tekst(item.findtext("title"), 200),
            "link": (item.findtext("link") or "").strip(),
            "resume": rens_tekst(item.findtext("description")),
            "dato": parse_dato(item.findtext("pubDate")),
        })
    return artikler


def parse_atom(rod: ET.Element) -> list[dict]:
    """Læser artikler ud af et Atom-feed (<feed><entry>...)."""
    artikler = []
    for entry in rod.findall("atom:entry", NS):
        link = ""
        for l in entry.findall("atom:link", NS):
            if l.get("rel") in (None, "alternate"):
                link = l.get("href", "")
                break
        resume = entry.findtext("atom:summary", default="", namespaces=NS) \
              or entry.findtext("atom:content", default="", namespaces=NS)
        dato_str = entry.findtext("atom:published", default="", namespaces=NS) \
                or entry.findtext("atom:updated", default="", namespaces=NS)
        artikler.append({
            "titel": rens_tekst(entry.findtext("atom:title", default="", namespaces=NS), 200),
            "link": link.strip(),
            "resume": rens_tekst(resume),
            "dato": parse_dato(dato_str),
        })
    return artikler


def crawl_feed(feed: dict) -> tuple[dict, list[dict], str | None]:
    """Henter og parser ét feed. Returnerer (feed, artikler, evt. fejl)."""
    try:
        data = hent_url(feed["url"])
        rod = ET.fromstring(data)
    except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as fejl:
        return feed, [], f"{type(fejl).__name__}: {fejl}"

    # RSS eller Atom? Roden afslører det.
    if rod.tag == "rss" or rod.find("channel") is not None:
        artikler = parse_rss(rod)
    else:
        artikler = parse_atom(rod)

    # Sæt kilde/kategori på og filtrér ubrugelige poster fra
    rensede = []
    for a in artikler[:MAX_PER_FEED]:
        if not a["titel"] or not a["link"]:
            continue
        a["kilde"] = feed["navn"]
        a["kategori"] = feed.get("kategori", "Andet")
        rensede.append(a)
    return feed, rensede, None


# ----- Hovedprogram ----------------------------------------------------------

def main() -> None:
    feeds = json.loads(FEEDS_FIL.read_text(encoding="utf-8"))["feeds"]
    print(f"Crawler {len(feeds)} feeds …\n")

    alle: list[dict] = []
    # Hent alle feeds parallelt, så det går hurtigt
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(crawl_feed, feed) for feed in feeds]
        for job in as_completed(jobs):
            feed, artikler, fejl = job.result()
            if fejl:
                print(f"  ⚠️  {feed['navn']}: {fejl}")
            else:
                print(f"  ✅ {feed['navn']}: {len(artikler)} artikler")
            alle.extend(artikler)

    # Fjern dubletter (samme link) - kan ske når feeds overlapper
    set_links: set[str] = set()
    unikke = []
    for a in alle:
        if a["link"] in set_links:
            continue
        set_links.add(a["link"])
        unikke.append(a)

    # Smid for gamle artikler væk og sortér nyeste først
    nu = datetime.now(timezone.utc)
    def alder_ok(a: dict) -> bool:
        return a["dato"] is None or (nu - a["dato"]).days <= MAX_DAGE_GAMMEL
    unikke = [a for a in unikke if alder_ok(a)]
    unikke.sort(key=lambda a: a["dato"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)

    # datetime -> tekst, så det kan gemmes som JSON
    for a in unikke:
        a["dato"] = a["dato"].isoformat() if a["dato"] else None

    resultat = {
        "opdateret": nu.isoformat(),
        "antal": len(unikke),
        "artikler": unikke,
    }
    OUTPUT_FIL.parent.mkdir(exist_ok=True)
    OUTPUT_FIL.write_text(json.dumps(resultat, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"\n💾 Gemte {len(unikke)} artikler i {OUTPUT_FIL.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
