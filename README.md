# 🛰️ AI Radar

En hjemmeside der automatisk samler AI-nyheder fra en række gode kilder (RSS-feeds) og præsenterer dem lækkert — med søgning, kategorifilter og mørkt tema.

**Sådan hænger det sammen:**

```
feeds.json  ──►  crawler.py  ──►  data/articles.json  ──►  index.html
(kilderne)      (henter nyt)      (alle artiklerne)        (den flotte side)
```

GitHub Actions kører crawleren automatisk hver 6. time, og GitHub Pages hoster siden gratis. Når først det er sat op, passer det sig selv.

---

## 🚀 Kom i gang (gøres af ÉN af jer)

### 1. Læg projektet på GitHub

1. Opret en konto på [github.com](https://github.com), hvis du ikke har en
2. Klik **New repository**, kald det f.eks. `ai-radar`, vælg **Public** (kræves for gratis GitHub Pages)
3. Åbn en terminal i denne mappe og kør:

```bash
git init
git add .
git commit -m "Første version af AI Radar"
git branch -M main
git remote add origin https://github.com/DIT-BRUGERNAVN/ai-radar.git
git push -u origin main
```

### 2. Tænd for GitHub Pages (gratis hosting)

1. Gå til dit repo på GitHub → **Settings** → **Pages**
2. Under *Build and deployment* → *Source*: vælg **Deploy from a branch**
3. Vælg branch **main** og mappen **/ (root)** → **Save**
4. Efter et minut ligger siden på `https://DIT-BRUGERNAVN.github.io/ai-radar/` 🎉

### 3. Tjek at automatikken kører

Gå til fanen **Actions** i dit repo. Workflowen "Crawl AI-nyheder" kører automatisk hver 6. time — og du kan altid starte den manuelt med **Run workflow**. Den henter nyheder og committer dem selv, hvorefter siden opdateres.

### 4. Invitér din makker

**Settings** → **Collaborators** → **Add people** → skriv makkerens GitHub-brugernavn. Når invitationen er accepteret, kan I begge pushe til projektet.

---

## 👯 Sådan koder I to samtidig (uden at ødelægge noget for hinanden)

Kernen er **Git + GitHub**: I har hver jeres lokale kopi og arbejder på hver jeres *branch*. Man kan aldrig komme til at overskrive hinandens arbejde ved et uheld.

### Første gang (gøres af jer begge)

```bash
git clone https://github.com/BRUGERNAVN/ai-radar.git
cd ai-radar
```

### Den daglige arbejdsgang

```bash
# 1. Hent altid det nyeste, før du går i gang
git checkout main
git pull

# 2. Lav en branch til det, du vil bygge
git checkout -b torben/moerkere-tema        # brug jeres eget navn/opgave

# 3. Kod løs, og gem undervejs
git add .
git commit -m "Gjorde det mørke tema mørkere"

# 4. Skub din branch op til GitHub
git push -u origin torben/moerkere-tema
```

Gå derefter ind på GitHub — den foreslår selv **"Compare & pull request"**. Opret pull requesten, lad makkeren kigge den igennem (eller merge selv, hvis det er småting), og klik **Merge**. Nu er din ændring en del af `main`, og makkeren får den med næste `git pull`.

### De tre gyldne regler

1. **Kod aldrig direkte på `main`** — lav altid en branch
2. **Start altid med `git pull`** — så bygger du oven på det nyeste
3. **Små, hyppige pull requests** er lettere at overskue end én kæmpestor

> 💡 **Konflikt?** Hvis I har ændret i *præcis samme linjer*, siger Git til ved merge. Filen får markeringer som `<<<<<<<` — vælg hvilken version der skal gælde, slet markeringerne, commit igen. Det sker sjældent, når I arbejder i hver jeres branches og laver små PR's.

> 💡 **Vil I kode live i samme fil samtidig** (som i Google Docs)? Installér extensionen **Live Share** i VS Code — så deler den ene sin editor, og den anden koder med i realtid. Godt til parprogrammering; Git-flowet ovenfor er stadig det, der gemmer arbejdet.

---

## 💻 Kør projektet lokalt

```bash
# Hent friske nyheder (kræver kun Python 3 - ingen pip install!)
python3 crawler.py

# Start en lille lokal webserver
python3 -m http.server

# Åbn http://localhost:8000 i browseren
```

> ⚠️ Åbn ikke `index.html` ved at dobbeltklikke på den — browseren blokerer så indlæsningen af JSON-filen. Brug altid `python3 -m http.server`.

---

## 🧩 Typiske ting at bygge videre på

| Idé | Hvor kigger du? |
|---|---|
| Tilføj/fjern nyhedskilder | `feeds.json` — tilføj bare en linje |
| Ændr farver og udseende | `index.html` — CSS-variablerne øverst i `:root` |
| Ændr hvor tit der crawles | `.github/workflows/crawl.yml` — cron-linjen |
| Flere kategorier | Sæt `kategori` i `feeds.json`; filterknapperne dannes automatisk |
| Nyt filter (f.eks. pr. kilde) | `index.html` — funktionen `filtrerede()` |
| Ældre/nyere artikler med | `crawler.py` — `MAX_DAGE_GAMMEL` og `MAX_PER_FEED` |

---

## 📁 Filerne i projektet

| Fil | Hvad den gør |
|---|---|
| `crawler.py` | Henter alle feeds, renser teksten, fjerner dubletter, gemmer JSON |
| `feeds.json` | Listen over nyhedskilder — projektets "indstillinger" |
| `data/articles.json` | Selve artiklerne (genereres automatisk — ret den aldrig i hånden) |
| `index.html` | Hele hjemmesiden: HTML + CSS + JavaScript i én fil |
| `.github/workflows/crawl.yml` | Automatikken: crawl hver 6. time + commit |

God fornøjelse! 🚀
