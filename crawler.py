#!/usr/bin/env python3
"""
AI Radar - crawler + AI-omskrivning
===================================
1. Henter AI-nyheder fra RSS/Atom-feeds (feeds.json)
2. Omskriver hver artikel til ULTRAKORT, letlæst dansk med Claude API
   (springes over hvis ANTHROPIC_API_KEY ikke er sat - så vises originalen)
3. Gemmer alt i data/articles.json, som hjemmesiden læser

Kør:  python3 crawler.py
Kræver kun Pythons standardbibliotek - ingen pip install.

Omskrivninger CACHES: en artikel der én gang er omskrevet, omskrives
aldrig igen (nøglen er artiklens link). Det holder prisen på få øre.
"""

import json
import os
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
MAX_PER_FEED = 25            # max artikler pr. feed
MAX_DAGE_GAMMEL = 30         # smid artikler ældre end 30 dage væk
TIMEOUT_SEK = 20

# --- AI-omskrivning ---
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = "claude-haiku-4-5"    # hurtig og billig
BATCH_STR = 10                   # artikler pr. API-kald (korte resuméer)
MAX_OMSKRIV_PR_KOERSEL = 200     # loft over API-forbrug pr. kørsel

# --- Dybe briefs (hele artiklen hentes og genfortælles) ---
DYBDE_ANTAL = 30                 # de N nyeste artikler får komplet brief
MIN_TEKST = 400                  # mindste brugbare artikeltekst (tegn)
MAX_TEKST = 7000                 # så meget af artiklen sender vi til Claude

USER_AGENT = "Mozilla/5.0 (compatible; AIRadarCrawler/2.0; +https://github.com)"
NS = {"atom": "http://www.w3.org/2005/Atom"}


# ----- Hjælpefunktioner (crawl) ----------------------------------------------

def hent_url(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60 if data else TIMEOUT_SEK) as svar:
        return svar.read()


def rens_tekst(raa: str | None, max_laengde: int = 400) -> str:
    if not raa:
        return ""
    tekst = re.sub(r"<[^>]+>", " ", raa)
    tekst = html.unescape(tekst)
    tekst = re.sub(r"\s+", " ", tekst).strip()
    tekst = re.sub(r"^arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*", "", tekst)
    if len(tekst) > max_laengde:
        tekst = tekst[:max_laengde].rsplit(" ", 1)[0] + "…"
    return tekst


def parse_dato(dato_str: str | None) -> datetime | None:
    if not dato_str:
        return None
    dato_str = dato_str.strip()
    try:
        return parsedate_to_datetime(dato_str)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(dato_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_rss(rod: ET.Element) -> list[dict]:
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
    try:
        data = hent_url(feed["url"])
        rod = ET.fromstring(data)
    except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as fejl:
        return feed, [], f"{type(fejl).__name__}: {fejl}"

    artikler = parse_rss(rod) if (rod.tag == "rss" or rod.find("channel") is not None) \
        else parse_atom(rod)

    rensede = []
    for a in artikler[:MAX_PER_FEED]:
        if not a["titel"] or not a["link"]:
            continue
        a["kilde"] = feed["navn"]
        a["kategori"] = feed.get("kategori", "Andet")
        rensede.append(a)
    return feed, rensede, None


# ----- Artikeltekst-udtræk ----------------------------------------------------

def udtraek_tekst(html_raa: str) -> str:
    """Trækker brødteksten ud af en artikelside: alle <p>-afsnit af rimelig
    længde (frasorterer menuer, cookiebokse osv.). Simpelt men effektivt."""
    # væk med script/style/noscript
    html_raa = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ",
                      html_raa, flags=re.S | re.I)
    # hold os til <article>-blokken hvis den findes
    m = re.search(r"<article[^>]*>(.*?)</article>", html_raa, flags=re.S | re.I)
    if m:
        html_raa = m.group(1)
    afsnit = re.findall(r"<p[^>]*>(.*?)</p>", html_raa, flags=re.S | re.I)
    tekst_afsnit = []
    for p in afsnit:
        t = html.unescape(re.sub(r"<[^>]+>", " ", p))
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 60:                      # korte stumper er sjældent brødtekst
            tekst_afsnit.append(t)
    return "\n\n".join(tekst_afsnit)[:MAX_TEKST]


def hent_artikeltekst(a: dict) -> tuple[dict, str]:
    """Henter artiklens egen side og returnerer (artikel, brødtekst)."""
    try:
        raa = hent_url(a["link"]).decode("utf-8", errors="replace")
        return a, udtraek_tekst(raa)
    except Exception:                        # paywall, botblokering, timeout …
        return a, ""


# ----- AI-omskrivning til letlæst dansk --------------------------------------

SYSTEM_PROMPT = """Du omskriver tech-nyheder til danskere HELT uden teknisk baggrund.
For hver artikel laver du:
- "rubrik": fængende dansk overskrift, MAX 8 ord, ingen jargon
- "resume": 1-2 KORTE sætninger på hverdagsdansk. Forklar hvad der er sket,
  og hvorfor det er interessant for almindelige mennesker. Max 30 ord i alt.
  Forbudt: engelske låneord der har et dansk ord, forkortelser uden forklaring,
  og buzzwords. Skriv som til en klog nabo.

Svar KUN med et JSON-array, ét objekt pr. artikel, i samme rækkefølge som input:
[{"rubrik": "...", "resume": "..."}, ...]"""


def kald_claude(artikler: list[dict]) -> list[dict] | None:
    """Sender en batch artikler til Claude og får danske omskrivninger tilbage."""
    input_liste = [{"nr": i + 1, "titel": a["titel"], "tekst": a["resume"][:350],
                    "kilde": a["kilde"]} for i, a in enumerate(artikler)]
    body = json.dumps({
        "model": AI_MODEL,
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content":
                      "Omskriv disse artikler:\n" + json.dumps(input_liste, ensure_ascii=False)}],
    }).encode()
    try:
        svar = hent_url("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
        tekst = json.loads(svar)["content"][0]["text"].strip()
        tekst = re.sub(r"^```(json)?\s*|\s*```$", "", tekst)   # fjern evt. kodehegn
        resultat = json.loads(tekst)
        if isinstance(resultat, list) and len(resultat) == len(artikler):
            return resultat
        print(f"  ⚠️  AI-svar havde forkert længde ({len(resultat)} vs {len(artikler)})")
    except Exception as fejl:  # API nede, kvote opbrugt, ugyldigt JSON osv.
        print(f"  ⚠️  AI-kald fejlede: {type(fejl).__name__}: {fejl}")
    return None


SYSTEM_BRIEF = """Du er journalist på et dansk nyhedssite for almindelige mennesker
uden teknisk baggrund. Ud fra artikelteksten skriver du et SELVSTÆNDIGT dansk
brief i dine helt egne ord - genfortæl, oversæt ALDRIG sætninger direkte, og
citér ikke fra kilden.

Svar KUN med ét JSON-objekt:
{
 "rubrik":  fængende dansk overskrift, max 8 ord, ingen jargon,
 "resume":  1-2 korte sætninger (max 30 ord) til oversigten,
 "brief":   150-200 ord letlæst hverdagsdansk i 2-3 afsnit adskilt af \\n\\n.
            Forklar hvad der er sket, hvorfor det er interessant, og hvad det
            kan betyde for almindelige mennesker,
 "pointer": liste med 3-4 korte hovedpointer (hver max 12 ord)
}"""


def kald_claude_brief(a: dict, tekst: str) -> dict | None:
    """Laver et komplet dansk brief ud fra artiklens fulde tekst."""
    body = json.dumps({
        "model": AI_MODEL,
        "max_tokens": 1500,
        "system": SYSTEM_BRIEF,
        "messages": [{"role": "user", "content":
                      f"KILDE: {a['kilde']}\nTITEL: {a['titel']}\n\nARTIKELTEKST:\n{tekst}"}],
    }).encode()
    try:
        svar = hent_url("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
        raa = json.loads(svar)["content"][0]["text"].strip()
        raa = re.sub(r"^```(json)?\s*|\s*```$", "", raa)
        r = json.loads(raa)
        if r.get("rubrik") and r.get("brief"):
            return r
    except Exception as fejl:
        print(f"  ⚠️  Brief-kald fejlede ({a['kilde']}): {type(fejl).__name__}")
    return None


def dybe_briefs(artikler: list[dict]) -> None:
    """Giver de DYBDE_ANTAL nyeste artikler et komplet dansk brief:
    henter artikelsiden, udtrækker brødteksten og lader Claude genfortælle."""
    kandidater = [a for a in artikler[:DYBDE_ANTAL] if not a.get("brief")]
    if not kandidater:
        print("📰 Alle topartikler har allerede et brief (cache)")
        return
    if not API_KEY:
        print("📰 ANTHROPIC_API_KEY ikke sat - springer dybe briefs over")
        return

    print(f"📰 Henter og genfortæller {len(kandidater)} artikler i fuld længde …")
    med_tekst = []
    with ThreadPoolExecutor(max_workers=6) as pool:      # hent siderne parallelt
        for job in as_completed([pool.submit(hent_artikeltekst, a) for a in kandidater]):
            a, tekst = job.result()
            if len(tekst) >= MIN_TEKST:
                med_tekst.append((a, tekst))
            else:
                print(f"   ⚠️  {a['kilde']}: kunne ikke hente brødtekst - beholder kort resumé")

    for i, (a, tekst) in enumerate(med_tekst, 1):
        r = kald_claude_brief(a, tekst)
        if r:
            a["rubrik"] = str(r["rubrik"]).strip()
            a["resume_da"] = str(r.get("resume", "")).strip() or a.get("resume_da", "")
            a["brief"] = str(r["brief"]).strip()
            a["pointer"] = [str(p).strip() for p in r.get("pointer", [])][:4]
        print(f"   … {i}/{len(med_tekst)}")


def omskriv_nye(artikler: list[dict], cache: dict) -> None:
    """Sætter rubrik/resume_da på artiklerne - fra cache, seed-fil eller Claude."""
    for a in artikler:                       # 1) genbrug alt vi allerede har betalt for
        gammel = cache.get(a["link"])
        if gammel:
            a["rubrik"] = gammel.get("rubrik", "")
            a["resume_da"] = gammel.get("resume_da", "")
            if gammel.get("brief"):
                a["brief"] = gammel["brief"]
                a["pointer"] = gammel.get("pointer", [])

    # 2) håndlavede omskrivninger fra seeds_da.json (matcher på titel-prefix)
    seed_fil = ROOT / "seeds_da.json"
    if seed_fil.exists():
        try:
            seeds = json.loads(seed_fil.read_text(encoding="utf-8"))
            for a in artikler:
                if a.get("rubrik"):
                    continue
                for s in seeds:
                    if a["titel"].startswith(s["titel_prefix"]):
                        a["rubrik"] = s["rubrik"]
                        a["resume_da"] = s["resume"]
                        break
        except (json.JSONDecodeError, KeyError):
            print("  ⚠️  seeds_da.json kunne ikke læses - springer over")

    mangler = [a for a in artikler if not a.get("rubrik")]
    if not mangler:
        print("✍️  Alle artikler er allerede omskrevet (cache)")
        return
    if not API_KEY:
        print(f"✍️  ANTHROPIC_API_KEY ikke sat - springer omskrivning over "
              f"({len(mangler)} artikler vises på engelsk)")
        return

    mangler = mangler[:MAX_OMSKRIV_PR_KOERSEL]
    print(f"✍️  Omskriver {len(mangler)} nye artikler til letlæst dansk …")
    for i in range(0, len(mangler), BATCH_STR):
        batch = mangler[i:i + BATCH_STR]
        resultat = kald_claude(batch)
        if not resultat:
            continue
        for a, r in zip(batch, resultat):
            rubrik = str(r.get("rubrik", "")).strip()
            resume = str(r.get("resume", "")).strip()
            if rubrik and resume:
                a["rubrik"] = rubrik
                a["resume_da"] = resume
        print(f"   … {min(i + BATCH_STR, len(mangler))}/{len(mangler)}")


# ----- Hovedprogram ----------------------------------------------------------

def main() -> None:
    feeds = json.loads(FEEDS_FIL.read_text(encoding="utf-8"))["feeds"]
    print(f"Crawler {len(feeds)} feeds …\n")

    alle: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(crawl_feed, feed) for feed in feeds]
        for job in as_completed(jobs):
            feed, artikler, fejl = job.result()
            print(f"  {'⚠️ ' if fejl else '✅'} {feed['navn']}: "
                  f"{fejl if fejl else str(len(artikler)) + ' artikler'}")
            alle.extend(artikler)

    # Dubletter væk (samme link)
    set_links: set[str] = set()
    unikke = []
    for a in alle:
        if a["link"] in set_links:
            continue
        set_links.add(a["link"])
        unikke.append(a)

    # For gamle væk + nyeste først
    nu = datetime.now(timezone.utc)
    unikke = [a for a in unikke
              if a["dato"] is None or (nu - a["dato"]).days <= MAX_DAGE_GAMMEL]
    unikke.sort(key=lambda a: a["dato"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)

    # Cache af tidligere omskrivninger (nøgle = link)
    cache: dict = {}
    if OUTPUT_FIL.exists():
        try:
            for a in json.loads(OUTPUT_FIL.read_text(encoding="utf-8"))["artikler"]:
                if a.get("rubrik"):
                    cache[a["link"]] = {"rubrik": a["rubrik"],
                                        "resume_da": a.get("resume_da", ""),
                                        "brief": a.get("brief", ""),
                                        "pointer": a.get("pointer", [])}
        except (json.JSONDecodeError, KeyError):
            pass

    print()
    omskriv_nye(unikke, cache)
    dybe_briefs(unikke)

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
    omskrevet = sum(1 for a in unikke if a.get("rubrik"))
    print(f"\n💾 Gemte {len(unikke)} artikler ({omskrevet} på dansk) i "
          f"{OUTPUT_FIL.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
