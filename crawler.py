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
import time
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

# --- AI-omskrivning (Claude ELLER Gemini - crawleren bruger den nøgle der findes) ---
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CLAUDE_MODEL = "claude-haiku-4-5"          # $1/$5 pr. mio. tokens
GEMINI_MODEL = "gemini-3.5-flash-lite"     # $0.30/$2.50 - billigst (lanceret 21/7-2026)
GEMINI_FALLBACK = "gemini-3.5-flash"       # bruges automatisk hvis Lite ikke svarer

# Er begge nøgler sat, vinder AI_UDBYDER ("claude" eller "gemini"), ellers Claude
UDBYDER = os.environ.get("AI_UDBYDER", "").strip().lower() \
    or ("claude" if CLAUDE_KEY else "gemini" if GEMINI_KEY else "")
API_KEY = CLAUDE_KEY if UDBYDER == "claude" else GEMINI_KEY

BATCH_STR = 10                   # artikler pr. API-kald (korte resuméer)
MAX_OMSKRIV_PR_KOERSEL = 200     # loft over API-forbrug pr. kørsel
GEMINI_PAUSE_SEK = 4             # pause mellem Gemini-kald (gratis-niveauets fartgrænse)

# --- Dybe briefs (hele artiklen hentes og genfortælles) ---
DYBDE_ANTAL = 250                # ALLE artikler får komplet brief (loft som sikkerhed)
BILLED_ANTAL = 30                # men kun de N nyeste får AI-billede (billeder koster)
MIN_TEKST = 400                  # mindste brugbare artikeltekst (tegn)
MAX_TEKST = 7000                 # så meget af artiklen sender vi til Claude

# --- AI-billeder til tophistorierne (kræver GEMINI_API_KEY + betaling slået til) ---
BILLED_MODEL = "gemini-3.1-flash-lite-image"   # ca. $0.034 pr. billede
BILLED_FALLBACK = "gemini-2.5-flash-image"     # bruges hvis Lite-billedmodellen afvises
BILLED_MAPPE = ROOT / "data" / "img"
MAX_BILLEDER_PR_KOERSEL = 35     # loft pr. kørsel
BILLED_BREDDE = 640              # nedskaleres til denne bredde (kræver pillow, ellers fuld str.)

_gemini_model = GEMINI_MODEL     # den model vi aktuelt bruger (kan falde tilbage)
_billed_model = BILLED_MODEL     # billedmodellen (kan også falde tilbage)

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


BILLED_STOPORD = ("logo", "avatar", "author", "icon", "badge", "headshot",
                  "profile", "gravatar", "sprite", ".svg")


def udtraek_billeder(html_raa: str, base_url: str) -> list[dict]:
    """Finder artiklens billeder med alt-tekst/billedtekst, så AI'en kan
    udvælge dem der viser benchmarks, grafer og tabeller."""
    from urllib.parse import urljoin
    kandidater = []
    # <figure> med billedtekst først - det er typisk grafikkerne
    for fig in re.findall(r"<figure[^>]*>(.*?)</figure>", html_raa, flags=re.S | re.I):
        m = re.search(r"<img[^>]+src=[\'\"]([^\'\"]+)", fig, flags=re.I)
        if not m:
            continue
        alt = re.search(r"alt=[\'\"]([^\'\"]*)", fig, flags=re.I)
        cap = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", fig, flags=re.S | re.I)
        tekst = rens_tekst((cap.group(1) if cap else "") or (alt.group(1) if alt else ""), 150)
        kandidater.append({"url": urljoin(base_url, m.group(1)), "tekst": tekst})
    # løse <img> med beskrivende alt-tekst som supplement
    for m in re.finditer(r"<img[^>]+src=[\'\"]([^\'\"]+)[\'\"][^>]*alt=[\'\"]([^\'\"]{15,})", html_raa, flags=re.I):
        kandidater.append({"url": urljoin(base_url, m.group(1)), "tekst": rens_tekst(m.group(2), 150)})
    # frasortér logoer, ikoner osv. og dubletter
    rene, set_urls = [], set()
    for k in kandidater:
        u = k["url"].split("?")[0].lower()
        if k["url"] in set_urls or any(o in u or o in k["tekst"].lower() for o in BILLED_STOPORD):
            continue
        set_urls.add(k["url"])
        rene.append(k)
    return rene[:8]


def hent_artikeltekst(a: dict) -> tuple[dict, str, list[dict]]:
    """Henter artiklens egen side og returnerer (artikel, brødtekst, billeder).
    Billeder hentes OGSÅ fra historiens øvrige kilder ("+kilder") - det er tit
    dér, benchmark-graferne ligger."""
    try:
        raa = hent_url(a["link"]).decode("utf-8", errors="replace")
        tekst, billeder = udtraek_tekst(raa), udtraek_billeder(raa, a["link"])
    except Exception:                        # paywall, botblokering, timeout …
        return a, "", []
    for b in billeder:
        b["kilde"] = a["kilde"]
    for kilde in a.get("andre", [])[:2]:     # samme historie hos andre medier
        try:
            raa2 = hent_url(kilde["link"]).decode("utf-8", errors="replace")
            for b2 in udtraek_billeder(raa2, kilde["link"]):
                b2["kilde"] = kilde["kilde"]
                billeder.append(b2)
        except Exception:
            pass
    return a, tekst, billeder[:10]


# ----- AI-omskrivning til letlæst dansk --------------------------------------

SYSTEM_PROMPT = """Du omskriver tech-nyheder til danskere HELT uden teknisk baggrund.
For hver artikel laver du:
- "rubrik": fængende dansk overskrift, MAX 8 ord, ingen jargon
- "resume": 1-2 KORTE sætninger på hverdagsdansk. Forklar hvad der er sket,
  og hvorfor det er interessant for almindelige mennesker. Max 30 ord i alt.
  Forbudt: engelske låneord der har et dansk ord, forkortelser uden forklaring,
  og buzzwords. Skriv som til en klog nabo.
- Skriv ALTID "AI" - aldrig "kunstig intelligens" (det er for langt).

Svar KUN med et JSON-array, ét objekt pr. artikel, i samme rækkefølge som input:
[{"rubrik": "...", "resume": "..."}, ...]"""


def kald_ai(system: str, bruger_tekst: str, max_tokens: int) -> str:
    """Ét fælles AI-kald - taler med Claude eller Gemini alt efter hvilken
    nøgle der er sat. Returnerer modellens rå tekstsvar."""
    if UDBYDER == "claude":
        body = json.dumps({
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": bruger_tekst}],
        }).encode()
        svar = hent_url("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
        return json.loads(svar)["content"][0]["text"]

    # Gemini - prøv den billige Lite-model først, fald tilbage hvis den afvises
    global _gemini_model
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": bruger_tekst}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }).encode()
    try:
        svar = hent_url(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_gemini_model}:generateContent",
            data=body, headers={"x-goog-api-key": API_KEY, "content-type": "application/json"})
    except urllib.error.HTTPError as fejl:
        if fejl.code in (400, 404) and _gemini_model != GEMINI_FALLBACK:
            print(f"  ℹ️  {_gemini_model} ikke tilgængelig endnu - skifter til {GEMINI_FALLBACK}")
            _gemini_model = GEMINI_FALLBACK
            svar = hent_url(
                f"https://generativelanguage.googleapis.com/v1beta/models/{_gemini_model}:generateContent",
                data=body, headers={"x-goog-api-key": API_KEY, "content-type": "application/json"})
        else:
            raise
    tekst = json.loads(svar)["candidates"][0]["content"]["parts"][0]["text"]
    time.sleep(GEMINI_PAUSE_SEK)             # bliv under gratis-niveauets fartgrænse
    return tekst


def parse_json_svar(raa: str):
    """Fjerner evt. kodehegn og parser modellens JSON-svar."""
    raa = re.sub(r"^```(json)?\s*|\s*```$", "", raa.strip())
    return json.loads(raa)


def kald_ai_batch(artikler: list[dict]) -> list[dict] | None:
    """Sender en batch artikler til AI'en og får danske omskrivninger tilbage."""
    input_liste = [{"nr": i + 1, "titel": a["titel"], "tekst": a["resume"][:350],
                    "kilde": a["kilde"]} for i, a in enumerate(artikler)]
    try:
        resultat = parse_json_svar(kald_ai(
            SYSTEM_PROMPT,
            "Omskriv disse artikler:\n" + json.dumps(input_liste, ensure_ascii=False),
            4000))
        if isinstance(resultat, list) and len(resultat) == len(artikler):
            return resultat
        print(f"  ⚠️  AI-svar havde forkert længde ({len(resultat)} vs {len(artikler)})")
    except Exception as fejl:  # API nede, kvote opbrugt, ugyldigt JSON osv.
        print(f"  ⚠️  AI-kald fejlede: {type(fejl).__name__}: {fejl}")
    return None


SYSTEM_BRIEF = """Du er journalist på et dansk nyhedssite for almindelige mennesker
uden teknisk baggrund. Ud fra artikelteksten skriver du en SELVSTÆNDIG dansk
genfortælling i dine helt egne ord - oversæt ALDRIG sætninger direkte, og citér
ikke fra kilden. Kald teknologien "AI" - skriv ALDRIG "kunstig intelligens"
og opfind ALDRIG omskrivninger som "computerhjerner" eller "tænksom software".
Modelnavne (Gemini, GPT, Claude osv.) skrives præcis som i kilden.

Fremhæv de 1-2 vigtigste tal eller navne i hver sektion med **dobbelt-stjerner**.
Skriv levende og varieret - ALDRIG tre ens grå afsnit i træk.

UFRAVIGELIGT KRAV: Indeholder artiklen benchmarks, scores, procenter, priser
eller sammenligningstal, SKAL de konkrete tal med i genfortællingen - i
nøgletal-fliserne, detaljerne og/eller sektionerne. Tal må ALDRIG koges væk
til vage ord som "markant bedre".

Svar KUN med ét JSON-objekt:
{
 "rubrik":    fængende dansk overskrift, max 8 ord, ingen jargon,
 "resume":    1-2 korte sætninger (max 30 ord) til oversigten,
 "sektioner": 2-4 afsnit med hver sin KORTE, konkrete mini-overskrift (2-4 ord,
              fx "Det er sket", "Pengene bag", "Kritikerne siger", "Hvad nu?").
              Hvert afsnit 40-70 ord letlæst hverdagsdansk:
              [{"overskrift": "...", "tekst": "..."}, ...],
 "noegletal": 2-5 af artiklens vigtigste tal som fliser:
              [{"tal": "17 %", "label": "billigere end forgængeren"}, ...].
              Tom liste hvis artiklen ingen tal har,
 "detaljer":  4-7 punkter med de vigtigste fakta, tal og detaljer fra artiklen
              (hvert punkt én sætning, max 20 ord),
 "betydning": ét afsnit (50-80 ord): hvad kan det her betyde for almindelige
              mennesker, deres penge og deres fremtid,
 "pointer":   3-4 ultrakorte hovedpointer (hver max 12 ord),
 "figurer":   Fra listen KANDIDAT-BILLEDER udvælger du 0-3, der viser
              benchmarks, grafer, tabeller eller andre data - IKKE almindelige
              pressefotos. Returnér dem med en kort dansk billedtekst:
              [{"url": "...", "tekst": "..."}]. Tom liste hvis ingen er relevante.
}"""


def kald_ai_brief(a: dict, tekst: str, billeder: list[dict]) -> dict | None:
    """Laver et komplet dansk brief ud fra artiklens fulde tekst."""
    try:
        r = parse_json_svar(kald_ai(
            SYSTEM_BRIEF,
            f"KILDE: {a['kilde']}\nTITEL: {a['titel']}\n\nARTIKELTEKST:\n{tekst}",
            1500))
        if r.get("rubrik") and (r.get("sektioner") or r.get("brief")):
            return r
    except Exception as fejl:
        print(f"  ⚠️  Brief-kald fejlede ({a['kilde']}): {type(fejl).__name__}")
    return None


def dybe_briefs(artikler: list[dict]) -> None:
    """Giver de DYBDE_ANTAL nyeste artikler et komplet dansk brief:
    henter artikelsiden, udtrækker brødteksten og lader Claude genfortælle."""
    if GENKOER_FILTER:
        kandidater = [a for a in artikler[:DYBDE_ANTAL]
                      if GENKOER_FILTER in (a.get("rubrik", "") + " " + a["titel"]).lower()]
        print(f"📰 Genkører {len(kandidater)} artikler der matcher '{GENKOER_FILTER}'")
    else:
        kandidater = [a for a in artikler[:DYBDE_ANTAL]
                      if GENKOER_ALT or not a.get("sektioner")]
    if not kandidater:
        print("📰 Alle topartikler har allerede et brief (cache)")
        return
    if not API_KEY:
        print("📰 Ingen AI-nøgle sat (ANTHROPIC_API_KEY/GEMINI_API_KEY) - springer dybe briefs over")
        return

    print(f"📰 Henter og genfortæller {len(kandidater)} artikler i fuld længde …")
    med_tekst = []
    with ThreadPoolExecutor(max_workers=6) as pool:      # hent siderne parallelt
        for job in as_completed([pool.submit(hent_artikeltekst, a) for a in kandidater]):
            a, tekst, billeder = job.result()
            if len(tekst) >= MIN_TEKST:
                med_tekst.append((a, tekst, billeder))
            else:
                print(f"   ⚠️  {a['kilde']}: kunne ikke hente brødtekst - beholder kort resumé")

    for i, (a, tekst, billeder) in enumerate(med_tekst, 1):
        r = kald_ai_brief(a, tekst, billeder)
        if r:
            a["rubrik"] = str(r["rubrik"]).strip()
            a["resume_da"] = str(r.get("resume", "")).strip() or a.get("resume_da", "")
            a["sektioner"] = [{"overskrift": str(x.get("overskrift", "")).strip(),
                               "tekst": str(x.get("tekst", "")).strip()}
                              for x in r.get("sektioner", []) if x.get("tekst")][:4]
            a["brief"] = str(r.get("brief", "")).strip()
            def _noegle(u): return str(u or "").split("?")[0].strip()
            kilde_af = {_noegle(b["url"]): (b["url"], b.get("kilde", a["kilde"])) for b in billeder}
            a["figurer"] = []
            for f in r.get("figurer", []):
                match = kilde_af.get(_noegle(f.get("url")))
                if match:
                    a["figurer"].append({"url": match[0],
                                         "tekst": str(f.get("tekst", "")).strip(),
                                         "kilde": match[1]})
            a["figurer"] = a["figurer"][:3]
            a["noegletal"] = [{"tal": str(n.get("tal", "")).strip(),
                               "label": str(n.get("label", "")).strip()}
                              for n in r.get("noegletal", []) if n.get("tal")][:5]
            a["detaljer"] = [str(d).strip() for d in r.get("detaljer", [])][:7]
            a["betydning"] = str(r.get("betydning", "")).strip()
            a["pointer"] = [str(p).strip() for p in r.get("pointer", [])][:4]
        print(f"   … {i}/{len(med_tekst)}")


# ----- Indholdskategorier (AI vælger kategori ud fra indholdet) ----------------

KATEGORIER = ["Lanceringer", "Benchmarks", "Hverdags-AI", "Penge & marked",
              "Politik & jura", "Samfund & etik", "Forskning"]

SYSTEM_KATEGORI = f"""Du analyserer AI-nyheder for en almindelig dansker, der vil
opdage muligheder for at tjene penge og være forberedt på fremtiden.

For hver artikel giver du:

1) "kategori" - PRÆCIS ÉN fra denne liste (skriv navnet nøjagtigt):
- Lanceringer: nye modeller, produkter og funktioner der udgives
- Benchmarks: tests, sammenligninger og målinger af modellers ydeevne
- Hverdags-AI: værktøjer og funktioner almindelige mennesker selv kan bruge
- Penge & marked: investeringer, opkøb, økonomi, aktier, forretning
- Politik & jura: lovgivning, retssager, ophavsret, sanktioner, regulering
- Samfund & etik: jobs, deepfakes, sikkerhed, strømforbrug, AI's påvirkning af samfundet
- Forskning: videnskabelige artikler, metoder og gennembrud

2) "prio" - vigtighed 1-10 for læseren:
- 9-10: store modellanceringer, ægte gennembrud, nye muligheder man selv kan
  udnytte NU, store markedsskift der påvirker almindelige menneskers økonomi
- 6-8: væsentlige produktnyheder, vigtige benchmarks, betydelig regulering,
  tendenser der er værd at forberede sig på
- 3-5: almindelige branchenyheder, mindre opdateringer
- 1-2: inkrementel/niche-forskning, akademiske detaljer, smalle tekniske emner

Svar KUN med et JSON-array i samme rækkefølge som input:
[{{"kategori": "Lanceringer", "prio": 9}}, ...]"""


def klassificer(artikler: list[dict]) -> None:
    """Giver hver artikel indholdskategori + vigtighedsscore via AI (én gang pr. artikel)."""
    mangler = [a for a in artikler if not a.get("kat_ai") or "prio" not in a]
    if not mangler:
        return
    if not API_KEY:
        print("🏷️  Ingen AI-nøgle - beholder kilde-kategorierne")
        return
    print(f"🏷️  Kategoriserer og prioriterer {len(mangler)} artikler …")
    for i in range(0, len(mangler), 30):
        batch = mangler[i:i + 30]
        liste = [{"nr": j + 1, "titel": a["titel"], "tekst": a["resume"][:200]}
                 for j, a in enumerate(batch)]
        try:
            svar = parse_json_svar(kald_ai(SYSTEM_KATEGORI,
                                           json.dumps(liste, ensure_ascii=False), 2500))
            if isinstance(svar, list) and len(svar) == len(batch):
                for a, r in zip(batch, svar):
                    k = str(r.get("kategori", "")).strip()
                    if k in KATEGORIER:
                        a["kategori"] = k
                        a["kat_ai"] = True
                    try:
                        a["prio"] = max(1, min(10, int(r.get("prio", 5))))
                    except (ValueError, TypeError):
                        a["prio"] = 5
        except Exception as fejl:
            print(f"  ⚠️  Kategorisering fejlede: {type(fejl).__name__}")


# ----- Dublet-historier (samme nyhed fra flere medier) -------------------------

SYSTEM_DUBLET = """Du får en nummereret liste af nyhedsoverskrifter fra forskellige medier.
Find grupper af artikler der dækker PRÆCIS SAMME nyhedsbegivenhed (fx samme
produktlancering, samme retssag, samme opkøb - omtalt af flere medier).

VIGTIGT: Kun artikler om den samme konkrete begivenhed må grupperes.
Artikler der blot handler om samme emne eller firma, er IKKE dubletter.
Er du i tvivl, så lad være med at gruppere.

Svar KUN med et JSON-array af grupper, hver gruppe et array af numre, fx:
[[3, 17, 41], [8, 22]]
Ingen grupper? Svar: []"""


def saml_dublet_historier(artikler: list[dict]) -> list[dict]:
    """Finder nyheder som flere medier dækker, beholder den bedste udgave og
    gemmer de øvrige som ekstra kilder på historien ("andre")."""
    if not API_KEY:
        return artikler
    # forskningsartikler (arXiv) dublerer aldrig nyhedsmedierne - spring dem over
    kandidater = [a for a in artikler if a["kilde"] != "arXiv cs.AI"][:90]
    if len(kandidater) < 2:
        return artikler
    liste = "\n".join(f"{i+1}. [{a['kilde']}] {a['titel']}" for i, a in enumerate(kandidater))
    try:
        grupper = parse_json_svar(kald_ai(SYSTEM_DUBLET, liste, 1000))
        assert isinstance(grupper, list)
    except Exception as fejl:
        print(f"  ⚠️  Dublet-tjek fejlede: {type(fejl).__name__} - fortsætter uden")
        return artikler

    fjern: set[str] = set()
    samlet = 0
    for gruppe in grupper:
        try:
            medlemmer = [kandidater[int(n) - 1] for n in gruppe
                         if 1 <= int(n) <= len(kandidater)]
        except (ValueError, TypeError):
            continue
        if len(medlemmer) < 2:
            continue
        # behold den med mest indhold: brief > dansk rubrik > nyeste
        primaer = next((m for m in medlemmer if m.get("brief")), None) \
               or next((m for m in medlemmer if m.get("rubrik")), None) \
               or medlemmer[0]
        andre = [m for m in medlemmer if m is not primaer]
        primaer.setdefault("andre", [])
        primaer["andre"] += [{"kilde": m["kilde"], "link": m["link"]} for m in andre]
        fjern.update(m["link"] for m in andre)
        samlet += len(andre)
    if samlet:
        print(f"🔗 Samlede {samlet} dublet-artikler under deres hovedhistorier")
    return [a for a in artikler if a["link"] not in fjern]


# ----- AI-billeder til tophistorierne -----------------------------------------

def _billed_navn(link: str) -> str:
    import hashlib
    return hashlib.md5(link.encode()).hexdigest()[:16] + ".jpg"


def _gem_billede(raa: bytes, sti: Path) -> None:
    """Gemmer billedet - nedskaleret til web-størrelse hvis pillow findes."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raa)).convert("RGB")
        h = int(img.height * BILLED_BREDDE / img.width)
        img.resize((BILLED_BREDDE, h)).save(sti, "JPEG", quality=78)
    except ImportError:
        sti.write_bytes(raa)


def lav_billeder(artikler: list[dict]) -> None:
    """Genererer ét AI-billede pr. tophistorie. Billedet laves kun én gang
    (filnavn = hash af linket) og bruges for altid. Kræver GEMINI_API_KEY,
    og at betaling er slået til - ellers springes trinnet bare over."""
    global _billed_model
    if not GEMINI_KEY:
        print("🎨 GEMINI_API_KEY ikke sat - springer AI-billeder over")
        return
    BILLED_MAPPE.mkdir(parents=True, exist_ok=True)

    top = [a for a in artikler[:BILLED_ANTAL] if a.get("rubrik")]
    lavet, fejl_i_traek = 0, 0
    for a in top:
        navn = _billed_navn(a["link"])
        sti = BILLED_MAPPE / navn
        if sti.exists():                                  # allerede lavet
            a["billede"] = f"data/img/{navn}"
            continue
        if lavet >= MAX_BILLEDER_PR_KOERSEL or fejl_i_traek >= 2:
            continue
        prompt = (f"Redaktionel nyhedsillustration i moderne, enkel, flad stil med bløde "
                  f"farver og god kontrast. INGEN tekst, bogstaver, tal eller logoer. "
                  f"Motiv: {a['rubrik']}. Kontekst: {a.get('resume_da', '')[:150]}")
        body = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": "16:9"}},
        }).encode()
        try:
            try:
                svar = hent_url(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{_billed_model}:generateContent",
                    data=body, headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"})
            except urllib.error.HTTPError as f:
                if f.code in (400, 404) and _billed_model != BILLED_FALLBACK:
                    print(f"  ℹ️  {_billed_model} ikke tilgængelig - prøver {BILLED_FALLBACK}")
                    _billed_model = BILLED_FALLBACK
                    svar = hent_url(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{_billed_model}:generateContent",
                        data=body, headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"})
                else:
                    raise
            import base64
            for del_ in json.loads(svar)["candidates"][0]["content"]["parts"]:
                data64 = del_.get("inlineData", del_.get("inline_data", {})).get("data")
                if data64:
                    _gem_billede(base64.b64decode(data64), sti)
                    a["billede"] = f"data/img/{navn}"
                    lavet += 1
                    fejl_i_traek = 0
                    break
            time.sleep(GEMINI_PAUSE_SEK)
        except Exception as f:
            fejl_i_traek += 1
            print(f"  ⚠️  Billede fejlede ({a['kilde']}): {type(f).__name__} "
                  f"{'- er betaling slået til på Google-kontoen?' if fejl_i_traek >= 2 else ''}")
    if lavet:
        print(f"🎨 Genererede {lavet} nye artikelbilleder")

    # ryd op: slet billeder for artikler der er røget ud af listen
    brugte = {_billed_navn(a["link"]) for a in artikler}
    for fil in BILLED_MAPPE.glob("*.jpg"):
        if fil.name not in brugte:
            fil.unlink(missing_ok=True)


def omskriv_nye(artikler: list[dict], cache: dict) -> None:
    """Sætter rubrik/resume_da på artiklerne - fra cache, seed-fil eller Claude."""
    for a in artikler:                       # 1) genbrug alt vi allerede har betalt for
        gammel = cache.get(a["link"])
        if gammel:
            a["rubrik"] = gammel.get("rubrik", "")
            a["resume_da"] = gammel.get("resume_da", "")
            if gammel.get("brief") or gammel.get("sektioner"):
                a["brief"] = gammel.get("brief", "")
                a["sektioner"] = gammel.get("sektioner", [])
                if gammel.get("noegletal") is not None:
                    a["noegletal"] = gammel["noegletal"]
                if gammel.get("figurer") is not None:
                    a["figurer"] = gammel["figurer"]
                a["detaljer"] = gammel.get("detaljer", [])
                a["betydning"] = gammel.get("betydning", "")
                a["pointer"] = gammel.get("pointer", [])
            if gammel.get("billede"):
                a["billede"] = gammel["billede"]
            if gammel.get("kat_ai") and gammel.get("kategori"):
                a["kategori"] = gammel["kategori"]
                a["kat_ai"] = True
            if gammel.get("prio") is not None:
                a["prio"] = gammel["prio"]

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
        print(f"✍️  Ingen AI-nøgle sat (ANTHROPIC_API_KEY/GEMINI_API_KEY) - springer omskrivning over "
              f"({len(mangler)} artikler vises på engelsk)")
        return

    mangler = mangler[:MAX_OMSKRIV_PR_KOERSEL]
    print(f"✍️  Omskriver {len(mangler)} nye artikler til letlæst dansk …")
    for i in range(0, len(mangler), BATCH_STR):
        batch = mangler[i:i + BATCH_STR]
        resultat = kald_ai_batch(batch)
        if not resultat:
            continue
        for a, r in zip(batch, resultat):
            rubrik = str(r.get("rubrik", "")).strip()
            resume = str(r.get("resume", "")).strip()
            if rubrik and resume:
                a["rubrik"] = rubrik
                a["resume_da"] = resume
        print(f"   … {min(i + BATCH_STR, len(mangler))}/{len(mangler)}")


# ----- Benchmarks fra Artificial Analysis --------------------------------------

AA_KEY = os.environ.get("AA_API_KEY", "").strip()
# Manuel genkørsel (workflow-input): "ja" = genskriv HELE arkivet i nyeste format,
# et søgeord (fx "computerhjerner") = genskriv kun artikler hvis rubrik/titel matcher.
# Ellers behandles kun artikler, der aldrig er behandlet.
_GENKOER_RAW = os.environ.get("GENKOER_ALT", "").strip()
GENKOER_ALT = _GENKOER_RAW.lower() in ("ja", "1", "true")
GENKOER_FILTER = "" if _GENKOER_RAW.lower() in ("", "ja", "1", "true", "nej", "no", "false")     else _GENKOER_RAW.lower()
BENCH_FIL = ROOT / "data" / "benchmarks.json"


def find_tal(obj, *noegleord):
    """Leder rekursivt efter det første tal, hvis nøgle indeholder alle ordene.
    Gør os robuste over for små ændringer i API'ets feltnavne."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and all(o in k.lower() for o in noegleord):
                return v
        for v in obj.values():
            r = find_tal(v, *noegleord)
            if r is not None:
                return r
    return None


def hent_benchmarks() -> None:
    """Henter aktuelle model-benchmarks (intelligens, fart, pris) fra
    Artificial Analysis' gratis API og gemmer dem til Benchmarks-fanen."""
    if not AA_KEY:
        print("📊 AA_API_KEY ikke sat - springer benchmarks over")
        return
    try:
        raa = json.loads(hent_url(
            "https://artificialanalysis.ai/api/v2/language/models/free",
            headers={"x-api-key": AA_KEY}))
        modeller_raa = raa.get("data", raa if isinstance(raa, list) else [])
        modeller = []
        for m in modeller_raa:
            navn = m.get("name") or m.get("model_name") or m.get("slug") or ""
            skaber = m.get("model_creator") or m.get("creator") or {}
            udbyder = skaber.get("name") if isinstance(skaber, dict) else str(skaber)
            indeks = find_tal(m, "intelligence")
            if not navn or indeks is None:
                continue
            modeller.append({
                "navn": navn,
                "udbyder": udbyder or "",
                "indeks": round(indeks, 1),
                "hastighed": find_tal(m, "output", "second"),
                "pris_ind": find_tal(m, "price", "input") or find_tal(m, "input", "token"),
                "pris_ud": find_tal(m, "price", "output") or find_tal(m, "output", "token"),
            })
        modeller.sort(key=lambda x: x["indeks"], reverse=True)
        modeller = modeller[:50]
        if modeller:
            BENCH_FIL.write_text(json.dumps({
                "opdateret": datetime.now(timezone.utc).isoformat(),
                "kilde": "Artificial Analysis",
                "modeller": modeller,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"📊 Hentede benchmarks for {len(modeller)} modeller")
        else:
            print("📊 ⚠️ Benchmark-svar uden brugbare modeller - beholder gamle tal")
    except Exception as fejl:
        print(f"📊 ⚠️ Benchmarks fejlede: {type(fejl).__name__} - beholder gamle tal")


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
                                        "sektioner": a.get("sektioner", []),
                                        "noegletal": a.get("noegletal"),
                                        "figurer": a.get("figurer"),
                                        "detaljer": a.get("detaljer", []),
                                        "betydning": a.get("betydning", ""),
                                        "pointer": a.get("pointer", []),
                                        "billede": a.get("billede", ""),
                                        "kategori": a.get("kategori", ""),
                                        "kat_ai": a.get("kat_ai", False),
                                        "prio": a.get("prio")}
        except (json.JSONDecodeError, KeyError):
            pass

    print()
    omskriv_nye(unikke, cache)
    klassificer(unikke)
    unikke = saml_dublet_historier(unikke)
    dybe_briefs(unikke)
    lav_billeder(unikke)
    hent_benchmarks()

    # "kunstig intelligens" -> "AI" i alle tekster (også gamle, cachede)
    def kort_ai(t: str) -> str:
        t = re.sub(r"[Dd]en kunstige intelligens", "AI'en", t)
        t = re.sub(r"[Kk]unstige intelligenser", "AI'er", t)
        t = re.sub(r"[Kk]unstig(?:e)? intelligens", "AI", t)
        return t
    for a in unikke:
        for felt in ("rubrik", "resume_da", "brief", "betydning"):
            if a.get(felt):
                a[felt] = kort_ai(a[felt])
        for felt in ("pointer", "detaljer"):
            if a.get(felt):
                a[felt] = [kort_ai(p) for p in a[felt]]
        if a.get("sektioner"):
            for sek in a["sektioner"]:
                sek["overskrift"] = kort_ai(sek["overskrift"])
                sek["tekst"] = kort_ai(sek["tekst"])
        if a.get("noegletal"):
            for n in a["noegletal"]:
                n["label"] = kort_ai(n["label"])
        if a.get("figurer"):
            for f in a["figurer"]:
                f["tekst"] = kort_ai(f["tekst"])

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
