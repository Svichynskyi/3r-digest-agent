import os, json, hashlib, datetime, time, logging, base64
from pathlib import Path

import anthropic, requests
from google.oauth2.service_account import Credentials
import gspread
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import Header

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler("logs/digest.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID      = "1vguK81gR39CNcTaZFMld8HCQ1i1odSHEPw0WAi-ayas"
GMAIL_USER           = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO          = "Svichynskyi/3r-digest-agent"

SENT_HISTORY_FILE = Path("sent_history/sent_urls.json")
WEEK_TAG  = datetime.date.today().strftime("%Y-W%V")
TODAY_STR = datetime.date.today().strftime("%d.%m.%Y")
OUTPUT_PDF_UA = f"3R_Digest_{WEEK_TAG}_UA.pdf"
OUTPUT_PDF_EN = f"3R_Digest_{WEEK_TAG}_EN.pdf"

SEARCH_QUERIES = [
    # --- Return: diaspora & circulation ---
    "Ukraine diaspora return home professionals 2026",
    "Ukrainian refugees return intentions Europe survey",
    "brain circulation Ukraine knowledge transfer diaspora",
    "Ukraine reconstruction workforce returnees",

    # --- Recruit: structural talent gaps ---
    "Ukraine skilled worker shortage reconstruction 2026",
    "Ukraine IT professionals talent market 2026",
    "Ukraine attract international experts specialists",
    "Ukraine veterans reskilling employment program",

    # --- Retain: conditions & environment ---
    "Ukraine brain drain education researchers leaving",
    "Ukraine reskilling retraining workforce program",
    "Ukraine R&D innovation human capital investment",
    "Ukraine university graduates employment 2026",

    # --- Global context: migration & labour policy ---
    "IOM Ukraine migration report 2026",
    "UNHCR Ukraine displacement return 2026",
    "OECD migration outlook skilled workers Europe",
    "EU labor market migration policy 2026",
    "VoxUkraine human capital labor market",
    "Cedos Ukraine education migration analysis",
]

# Specialized sources to scrape directly (RSS / pages)
RSS_SOURCES = [
    {"url": "https://www.iom.int/rss.xml",              "source": "IOM"},
    {"url": "https://ukraine.iom.int/rss.xml",          "source": "IOM Ukraine"},
    {"url": "https://www.unhcr.org/rss.xml",            "source": "UNHCR"},
    {"url": "https://reliefweb.int/updates/rss.xml?primary_country=244&theme=3", "source": "ReliefWeb"},
    {"url": "https://voxukraine.org/feed/",             "source": "VoxUkraine"},
    {"url": "https://cedos.org.ua/feed/",               "source": "Cedos"},
    {"url": "https://blogs.worldbank.org/rss.xml",      "source": "World Bank"},
    {"url": "https://www.oecd.org/migration/rss.xml",   "source": "OECD"},
    {"url": "https://www.ilo.org/rss.xml",              "source": "ILO"},
]

# NewsAPI domain-targeted queries (guaranteed coverage of specific outlets)
NEWSAPI_DOMAIN_QUERIES = [
    ("Ukraine migration workforce",         "iom.int,unhcr.org,reliefweb.int"),
    ("Ukraine human capital education",     "voxukraine.org,cedos.org.ua"),
    ("Ukraine labor market reconstruction", "worldbank.org,oecd.org"),
    ("Ukraine workforce skills employment", "ilo.org,migration.iom.int"),
]

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"


def load_sent_history():
    if SENT_HISTORY_FILE.exists():
        with open(SENT_HISTORY_FILE) as f:
            return json.load(f)
    return {}

def save_sent_history(history):
    SENT_HISTORY_FILE.parent.mkdir(exist_ok=True)
    with open(SENT_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def url_fingerprint(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]

def filter_new_articles(articles, history):
    return [a for a in articles if url_fingerprint(a["url"]) not in history]

def mark_articles_sent(articles, history):
    for a in articles:
        history[url_fingerprint(a["url"])] = {
            "url": a["url"], "title": a["title"], "sent_week": WEEK_TAG
        }
    return history


def search_articles(query, max_results=10):
    """Search using Serper.dev — Google results, whole web, no restrictions."""
    _key = os.environ.get("SERPER_API_KEY", "")
    if not _key:
        log.warning("SERPER_API_KEY not set — skipping")
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": _key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results, "gl": "ua", "hl": "en"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        articles = []
        for item in data.get("organic", []):
            title   = item.get("title", "")
            url     = item.get("link", "")
            snippet = item.get("snippet", "")[:400]
            source  = url.split("/")[2] if url else "web"
            if title and url:
                articles.append({"title": title, "url": url,
                                  "snippet": snippet, "source": source})
        # Also include news results if any
        for item in data.get("news", []):
            title   = item.get("title", "")
            url     = item.get("link", "")
            snippet = item.get("snippet", "")[:400]
            source  = url.split("/")[2] if url else "web"
            if title and url:
                articles.append({"title": title, "url": url,
                                  "snippet": snippet, "source": source})
        log.info(f"Serper '{query[:45]}': {len(articles)} results")
        return articles
    except Exception as e:
        log.warning(f"Serper search failed for '{query}': {e}")
        return []


def search_by_domains():
    """Serper site-targeted search for authoritative 3R sources."""
    _key = os.environ.get("SERPER_API_KEY", "")
    if not _key:
        return []

    SITE_QUERIES = [
        "site:iom.int Ukraine migration return workforce 2026",
        "site:unhcr.org Ukraine displacement skills employment",
        "site:voxukraine.org human capital labor market",
        "site:cedos.org.ua education migration skills",
        "site:worldbank.org Ukraine workforce reconstruction",
        "site:oecd.org migration skilled workers labor",
        "site:ilo.org Ukraine employment reskilling",
        "site:reliefweb.int Ukraine human capital workforce",
        "site:migrationpolicy.org Ukraine diaspora brain drain",
        "site:atlanticcouncil.org Ukraine human capital",
    ]

    articles = []
    for q in SITE_QUERIES:
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": _key, "Content-Type": "application/json"},
                json={"q": q, "num": 5, "hl": "en"},
                timeout=15,
            )
            resp.raise_for_status()
            for item in resp.json().get("organic", []):
                title  = item.get("title", "")
                url    = item.get("link", "")
                if title and url:
                    articles.append({
                        "title":   title,
                        "url":     url,
                        "snippet": item.get("snippet", "")[:400],
                        "source":  url.split("/")[2],
                    })
            log.info(f"Serper domain '{q[:50]}': {len(articles)} total")
        except Exception as e:
            log.warning(f"Serper domain search failed: {e}")
        time.sleep(0.2)
    return articles
def read_rss_sources():
    """Read RSS feeds from specialized sources: IOM, UNHCR, ReliefWeb, VoxUkraine, Cedos, etc."""
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse

    RELEVANT_KW = ["ukrain", "migr", "labour", "labor", "skill", "refugee", "return",
                   "diaspora", "workforce", "educat", "reconstruct", "human capital",
                   "employ", "veteran", "brain drain", "reskill"]
    SKIP_KW = ["privacy", "cookie", "terms", "advertis", "subscribe", "newsletter"]
    cutoff = datetime.date.today() - datetime.timedelta(days=30)

    articles = []
    for feed in RSS_SOURCES:
        url = feed["url"]
        src = feed["source"]
        try:
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; 3RDigestBot/1.0)",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            })
            if resp.status_code != 200:
                log.warning(f"RSS {src}: HTTP {resp.status_code}")
                continue

            root = ET.fromstring(resp.content)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            # Support both RSS <item> and Atom <entry>
            items = root.findall(".//item") or root.findall(".//atom:entry", ns) or root.findall(".//entry")
            added = 0
            for item in items:
                if added >= 5:
                    break

                title   = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "").strip()
                link    = (item.findtext("link")  or item.findtext("atom:link", namespaces=ns) or "").strip()
                desc    = (item.findtext("description") or item.findtext("summary") or
                           item.findtext("atom:summary", namespaces=ns) or "").strip()
                pub     = (item.findtext("pubDate") or item.findtext("published") or "")

                # atom:link is an attribute, not text
                if not link:
                    link_el = item.find("atom:link", ns) or item.find("link")
                    if link_el is not None:
                        link = link_el.get("href", "") or link_el.text or ""

                if not title or not link:
                    continue

                combined = (title + " " + desc).lower()
                if any(k in combined for k in SKIP_KW):
                    continue
                if not any(k in combined for k in RELEVANT_KW):
                    continue

                # Strip HTML tags from description
                import re
                clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()[:300]

                articles.append({
                    "title":   title[:200],
                    "url":     link,
                    "snippet": clean_desc or title,
                    "source":  src,
                })
                added += 1

            log.info(f"RSS {src}: {added} relevant articles")
        except Exception as e:
            log.warning(f"RSS {src} failed: {e}")
        time.sleep(0.5)
    return articles



def collect_all_articles():
    seen, all_articles = set(), []

    def add(art):
        if art["url"] and art["url"] not in seen:
            seen.add(art["url"])
            all_articles.append(art)

    # 1. Serper — broad Google search (whole web)
    log.info("Searching via Serper (Google)...")
    for query in SEARCH_QUERIES:
        for art in search_articles(query):
            add(art)

    # 2. Serper — domain-targeted for authoritative 3R sources
    log.info("Searching authoritative domains via Serper...")
    for art in search_by_domains():
        add(art)

    # 3. RSS feeds — direct from specialized sources
    log.info("Reading RSS feeds (IOM, UNHCR, ReliefWeb, VoxUkraine, Cedos, OECD, ILO)...")
    for art in read_rss_sources():
        add(art)

    log.info(f"Collected {len(all_articles)} raw articles total")
    return all_articles


SYSTEM_PROMPT = """You are an expert analyst for the 3R Model -- a human capital management framework for Ukraine based on brain circulation.

THE 3R MODEL:
The model's core unit is the CIRCULATION TRANSACTION: an interaction where a human capital carrier and an economic actor jointly produce a shared outcome (knowledge applied, decision implemented, collaboration formed). A contact or registration is NOT a transaction.

RETURN -- Restoring Connection:
Rebuilding trust and reactivating human capital temporarily outside Ukraine.
Covers: return of citizens, diaspora experience integration, knowledge circulation, restoration of professional networks.
Goal: not demographic return per se, but ECONOMIC INCLUSION -- human capital generating value regardless of physical location.
Key signals: dual engagement, diaspora-to-Ukraine knowledge transfer, professional network reactivation.

RECRUIT -- Structural Reinforcement:
Addressing structural competency gaps by attracting professionals unavailable domestically.
NOT about headcount -- about targeted adjustment of human capital configuration.
Covers: identifying competency deficits in strategic sectors, attracting carriers of those competencies, institutional integration.
Key signals: sector-specific skill shortages, targeted attraction programs, education-demand alignment.

RETAIN -- Environment for Application and Accumulation:
The CENTRAL element. Creating conditions where human capital is applied, accumulates, and compounds.
Key problem: BRAIN WASTE -- competencies not fully utilised (measured by over-qualification rate).
Key lever: RESKILLING -- only effective when DEMAND-COUPLED (tied to specific employer need). Reskilling without demand reproduces brain waste at higher level.
Reskilling connects to Return: diaspora as mentors and knowledge transfer agents.
Key signals: reskilling programs with employer demand, R&D environment, over-qualification data, veteran reintegration.

GLOBAL CONTEXT: international trends in migration policy, labor markets, brain drain/circulation in comparable countries.

ANALYTICAL RULES:
- Classify by what TRANSACTION TYPE the article signals, not just its topic
- Prioritise articles that indicate actual transactions or conditions enabling them
- Be analytical, not descriptive -- explain WHY it matters for brain circulation
- Return ONLY valid JSON, no markdown fences, no extra text"""

DIGEST_SCHEMA = """{
  "week": "Week 26, 2026",
  "date_range": "June 22-28, 2026",
  "executive_summary_ua": "3-4 sentences in Ukrainian: what circulation signals dominated this week and why they matter",
  "executive_summary_en": "3-4 sentences in English: what circulation signals dominated this week and why they matter",
  "sections": {
    "return": [{"title_ua":"","title_en":"","summary_ua":"","summary_en":"","relevance_ua":"what circulation transaction type this signals","relevance_en":"what circulation transaction type this signals","url":"","source":""}],
    "recruit": [],
    "retain": [],
    "global_context": []
  },
  "key_insight_ua": "The single most important brain circulation signal this week -- Ukrainian",
  "key_insight_en": "The single most important brain circulation signal this week -- English"
}"""

def clean_for_json(text):
    """Remove characters that break JSON strings."""
    if not text:
        return ""
    # Remove control characters and normalize quotes
    result = []
    for ch in str(text):
        if ch == '"':
            result.append('\'')  # replace double quotes with single
        elif ord(ch) < 32 and ch not in ('\n', '\t'):
            pass  # skip control chars
        else:
            result.append(ch)
    return "".join(result)[:500]  # limit length


def select_top_articles(client, articles, top_n=25):
    """Step 1: Ask Claude to select the top N most relevant articles for 3R analysis."""
    clean = []
    for a in articles:
        clean.append({
            "title":   clean_for_json(a.get("title", "")),
            "url":     a.get("url", ""),
            "snippet": clean_for_json(a.get("snippet", "")),
            "source":  clean_for_json(a.get("source", "")),
        })

    articles_text = "\n".join([
        f"[{i+1}] {a['title']} | {a['source']} | {a['snippet'][:120]}"
        for i, a in enumerate(clean)
    ])

    prompt = f"""You are a 3R Model analyst. Below are {len(clean)} articles collected this week.

3R Model focuses on brain circulation: Return (diaspora/knowledge reactivation), Recruit (structural competency gaps), Retain (reskilling, brain waste reduction, R&D environment).

Select the {top_n} most relevant articles for 3R analysis. An article is relevant if it signals:
- A circulation transaction or condition enabling one
- A policy, program, or data point about Ukrainian human capital
- A global trend directly comparable to Ukraine's situation

Return ONLY a JSON array of the selected article numbers, nothing else.
Example: [1, 3, 7, 12, 15]

ARTICLES:
{articles_text}

Return ONLY the JSON array of {top_n} numbers:"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    # Parse array
    import re
    nums = re.findall(r'\d+', raw)
    selected = [int(n) - 1 for n in nums if 0 < int(n) <= len(clean)][:top_n]
    if not selected:
        log.warning("Pre-filter returned no articles — using first 25")
        selected = list(range(min(top_n, len(clean))))
    log.info(f"Pre-filter: {len(clean)} → {len(selected)} articles selected")
    return [articles[i] for i in selected]


def analyse_with_claude(articles):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Step 1: pre-filter to top 25 most relevant
    if len(articles) > 30:
        articles = select_top_articles(client, articles, top_n=25)

    # Step 2: deep analysis of selected articles
    clean_articles = []
    for a in articles:
        clean_articles.append({
            "title":   clean_for_json(a.get("title", "")),
            "url":     a.get("url", ""),
            "snippet": clean_for_json(a.get("snippet", "")),
            "source":  clean_for_json(a.get("source", "")),
        })
    articles_text = "\n\n".join([
        f"[{i+1}] TITLE: {a['title']}\nURL: {a['url']}\nSNIPPET: {a['snippet']}"
        for i, a in enumerate(clean_articles)
    ])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
            f"Here are {len(clean_articles)} pre-selected articles for this week's 3R digest.\n\n{articles_text}\n\n"
            f"Produce the digest. Select 10-15 best articles total across all sections. "
            f"IMPORTANT: Return ONLY valid JSON. Escape any double quotes inside string values with backslash. No trailing commas.\n{DIGEST_SCHEMA}"}],
    )
    raw = response.content[0].text.strip()
    log.info(f"Claude response length: {len(raw)} chars, starts: {raw[:100]}")
    # Write raw response to debug file for inspection
    try:
        Path("sent_history").mkdir(exist_ok=True)
        with open("sent_history/debug_last_response.txt", "w") as _f:
            _f.write(raw[:5000])
    except Exception:
        pass

    # Strip markdown fences
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                raw = p
                break
    raw = raw.strip()

    # Find outermost JSON object
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]

    # Fix common Claude JSON issues:
    # 1. Replace curly quotes
    raw = raw.replace('\u201c', "'").replace('\u201d', "'")
    raw = raw.replace('\u2018', "'").replace('\u2019', "'")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e} at pos {e.pos}")
        log.error(f"Context: {raw[max(0,e.pos-100):e.pos+100]}")
        # Try removing trailing commas
        try:
            import re as _re2
            fixed = _re2.sub(r',\s*([}\]])', r'\1', raw)
            return json.loads(fixed)
        except Exception:
            pass
        # Return minimal fallback digest
        return {
            "week": WEEK_TAG,
            "date_range": TODAY_STR,
            "executive_summary_ua": "Digest generation encountered a parsing error this week.",
            "executive_summary_en": "Digest generation encountered a parsing error this week.",
            "sections": {"return": [], "recruit": [], "retain": [], "global_context": []},
            "key_insight_en": "System error during digest generation - will retry next week.",
            "key_insight_ua": "System error during digest generation - will retry next week.",
        }


ACCENT = colors.HexColor("#5B4FCF")
LIGHT  = colors.HexColor("#EEF2FF")
MUTED  = colors.HexColor("#6B7280")
SECTION_COLORS = {
    "return":         colors.HexColor("#0891B2"),   # cyan
    "recruit":        colors.HexColor("#7C3AED"),   # violet
    "retain":         colors.HexColor("#059669"),   # emerald
    "global_context": colors.HexColor("#D97706"),   # amber
}
SECTION_LABELS = {
    "return":         "RETURN",
    "recruit":        "RECRUIT",
    "retain":         "RETAIN",
    "global_context": "GLOBAL CONTEXT",
}
SECTION_STYLES = {
    "return":         {"color": "#0891B2", "bg": "#ECFEFF",  "label": "RETURN"},
    "recruit":        {"color": "#7C3AED", "bg": "#F5F3FF",  "label": "RECRUIT"},
    "retain":         {"color": "#059669", "bg": "#ECFDF5",  "label": "RETAIN"},
    "global_context": {"color": "#D97706", "bg": "#FFFBEB",  "label": "GLOBAL CONTEXT"},
}

def build_pdf(digest, filename):
    C_DARK   = colors.HexColor("#111827")
    C_SEC    = colors.HexColor("#4B5563")
    C_MUTED  = colors.HexColor("#6B7280")
    C_XMUTED = colors.HexColor("#9CA3AF")

    doc = SimpleDocTemplate(filename, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    S = getSampleStyleSheet()
    def ps(name, **kw):
        return ParagraphStyle(name, parent=S["Normal"], **kw)

    brand_s  = ps("br",  fontSize=9,  fontName="Helvetica-Bold", textColor=C_DARK,
                  spaceAfter=1*mm, letterSpacing=2)
    sub_s    = ps("su",  fontSize=10, fontName="Helvetica", textColor=C_MUTED, spaceAfter=6*mm)
    h1_s     = ps("h1",  fontSize=26, fontName="Helvetica-Bold", textColor=C_DARK,
                  leading=30, spaceAfter=5*mm)
    ins_s    = ps("ins", fontSize=13, fontName="Helvetica", textColor=colors.HexColor("#1F2937"),
                  leading=18, spaceAfter=6*mm, backColor=LIGHT,
                  leftIndent=6*mm, borderPad=4*mm)
    ititle_s = ps("it",  fontSize=13, fontName="Helvetica-Bold", textColor=C_DARK,
                  leading=17, spaceAfter=2*mm)
    ibody_s  = ps("ib",  fontSize=11, fontName="Helvetica", textColor=C_SEC,
                  leading=16, spaceAfter=2*mm)
    irel_s   = ps("ir",  fontSize=10, fontName="Helvetica-Oblique", textColor=C_MUTED,
                  leading=14, spaceAfter=2*mm)
    src_s    = ps("sr",  fontSize=8,  fontName="Helvetica", textColor=C_XMUTED,
                  spaceAfter=4*mm, letterSpacing=0.5)
    foot_s   = ps("ft",  fontSize=9,  fontName="Helvetica", textColor=C_XMUTED, alignment=1)
    pill_s   = ps("pl",  fontSize=8,  fontName="Helvetica-Bold", textColor=colors.white,
                  spaceBefore=6*mm, spaceAfter=3*mm, letterSpacing=1.5)

    story = []

    # Purple top bar
    bar = Table([[""]], colWidths=[170*mm], rowHeights=[3*mm])
    bar.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), ACCENT)]))
    story.append(bar)
    story.append(Spacer(1, 4*mm))

    # Brand header
    story.append(Paragraph("3R MODEL  ·  Human Capital Digest", brand_s))
    story.append(Paragraph(f"{TODAY_STR}  ·  Weekly Intelligence", sub_s))

    # Big H1
    headline = digest.get("key_insight_en", "3R Human Capital Digest")
    story.append(Paragraph(headline, h1_s))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#E5E7EB"),
                             spaceAfter=4*mm))

    # Key insight callout box
    insight = digest.get("key_insight_en", "")
    if insight:
        ins_table = Table([[Paragraph(insight, ins_s)]], colWidths=[170*mm])
        ins_table.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), LIGHT),
            ("LEFTPADDING",   (0,0), (-1,-1), 12),
            ("RIGHTPADDING",  (0,0), (-1,-1), 12),
            ("TOPPADDING",    (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
            ("LINEBEFORE",    (0,0), (0,-1),  3, ACCENT),
        ]))
        story.append(ins_table)
        story.append(Spacer(1, 4*mm))

    # Articles by section
    for sec_key in ["return", "recruit", "retain", "global_context"]:
        items = digest.get("sections", {}).get(sec_key, [])
        if not items:
            continue
        sec_color = SECTION_COLORS[sec_key]
        sec_label = SECTION_LABELS[sec_key]

        # Coloured pill header
        pill = Table([[Paragraph(sec_label, pill_s)]], colWidths=[40*mm])
        pill.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), sec_color),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("ROUNDEDCORNERS", [3]),
        ]))
        story.append(pill)
        story.append(Spacer(1, 2*mm))

        for item in items:
            t   = item.get("title_en")    or item.get("title_ua", "")
            s   = item.get("summary_en")  or item.get("summary_ua", "")
            rel = item.get("relevance_en")or item.get("relevance_ua", "")
            u   = item.get("url", "")
            src = item.get("source", "")
            story.append(Paragraph(t, ititle_s))
            story.append(Paragraph(s, ibody_s))
            if rel:
                story.append(Paragraph(f"3R: {rel}", irel_s))
            if src or u:
                display = src if src else u[:60]
                lnk = (f'<a href="{u}" color="#9CA3AF">{display}</a>') if u else display
                story.append(Paragraph(lnk.upper(), src_s))
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=colors.HexColor("#E5E7EB"), spaceAfter=3*mm))

    story.append(Spacer(1, 6*mm))
    story.append(Paragraph(
        f"3R Digest  ·  Weekly Intelligence on Ukraine's Human Capital  ·  {TODAY_STR}",
        foot_s
    ))
    doc.build(story)
    log.info(f"PDF generated: {filename}")


def upload_to_github(filepath, week_tag):
    if not GITHUB_TOKEN:
        log.warning("No GITHUB_TOKEN -- skipping upload")
        return ""
    filename = Path(filepath).name
    with open(filepath, "rb") as f:
        encoded_content = base64.b64encode(f.read()).decode()
    path_in_repo = f"digests/{week_tag}/{filename}"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path_in_repo}"
    hdrs = {"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json"}
    check = requests.get(api_url, headers=hdrs)
    body = {"message": f"Add digest {week_tag}", "content": encoded_content}
    if check.status_code == 200:
        body["sha"] = check.json()["sha"]
    resp = requests.put(api_url, headers=hdrs, json=body)
    if resp.ok:
        url = f"https://github.com/{GITHUB_REPO}/blob/main/{path_in_repo}"
        log.info(f"Uploaded: {url}")
        return url
    log.warning(f"GitHub upload failed: {resp.status_code}")
    return ""


def get_email_list():
    if TEST_MODE:
        log.info("TEST MODE: sending only to svichinskiy@gmail.com")
        return ["svichinskiy@gmail.com"]
    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly"])
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)
    emails = [v.strip() for v in ws.col_values(5) if "@" in v]
    log.info(f"Loaded {len(emails)} emails")
    return emails


def build_email_body(digest, link_pdf):
    insight  = digest.get("key_insight_en", "")
    headline = digest.get("key_insight_en", "3R Human Capital Digest")
    articles_html = ""
    for sec_key in ["return", "recruit", "retain", "global_context"]:
        items = digest.get("sections", {}).get(sec_key, [])
        if not items:
            continue
        s = SECTION_STYLES[sec_key]
        color = s["color"]
        label = s["label"]
        for item in items:
            t   = item.get("title_en")    or item.get("title_ua", "")
            txt = item.get("summary_en")  or item.get("summary_ua", "")
            rel = item.get("relevance_en")or item.get("relevance_ua", "")
            u   = item.get("url", "")
            src = item.get("source", "")
            pill = '<span style="display:inline-block;padding:3px 9px;font-size:10px;font-weight:700;letter-spacing:0.09em;text-transform:uppercase;color:#fff;background:' + color + ';border-radius:3px">' + label + '</span>'
            art  = '<div style="padding:22px 0;border-top:1px solid #E5E7EB">' + pill
            art += '<h2 style="margin:12px 0 0;font-size:16px;line-height:1.35;font-weight:700;color:#111827">' + t + '</h2>'
            art += '<p style="margin:7px 0 0;font-size:14px;line-height:1.55;color:#4B5563">' + txt + '</p>'
            if rel:
                art += '<p style="margin:10px 0 0;font-size:13px;font-style:italic;line-height:1.5;color:#6B7280">3R: ' + rel + '</p>'
            if src or u:
                display = src if src else u[:60]
                lnk = ('<a href="' + u + '" style="color:#9CA3AF;text-decoration:none">' + display + '</a>') if u else display
                art += '<div style="margin-top:10px;font-size:10.5px;letter-spacing:0.07em;text-transform:uppercase;color:#9CA3AF">' + lnk + '</div>'
            art += '</div>'
            articles_html += art
    btn = '<a href="' + link_pdf + '" style="display:inline-block;padding:10px 22px;background:#5B4FCF;color:#fff;font-size:13px;font-weight:600;text-decoration:none;border-radius:4px">Download PDF</a>'
    return (
        '<html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;background:#f3f4f6;font-family:Helvetica Neue,Helvetica,Arial,sans-serif">'
        '<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">'
        '<div style="background:#FFFFFF;max-width:660px;width:100%">'
        '<div style="height:4px;background:#5B4FCF"></div>'
        '<div style="padding:28px 40px 0">'
        '<div style="display:flex;justify-content:space-between;align-items:baseline">'
        '<div style="font-size:13px;font-weight:700;letter-spacing:0.2em;color:#111827">3R MODEL</div>'
        '<div style="font-size:13px;color:#6B7280">Human Capital Digest</div>'
        '</div>'
        '<div style="margin-top:6px;font-size:12px;color:#6B7280">' + TODAY_STR + ' &middot; Weekly Intelligence</div>'
        '</div>'
        '<div style="padding:26px 40px 0">'
        '<h1 style="margin:0;font-size:27px;line-height:1.22;font-weight:800;letter-spacing:-0.015em;color:#111827">' + headline + '</h1>'
        '</div>'
        '<div style="padding:20px 40px 0">'
        '<div style="background:#EEF2FF;border-left:3px solid #5B4FCF;padding:16px 18px;font-size:14px;line-height:1.55;color:#1F2937">' + insight + '</div>'
        '</div>'
        '<div style="padding:6px 40px 0">' + articles_html + '</div>'
        '<div style="padding:20px 40px 28px;border-top:1px solid #E5E7EB;margin-top:8px">' + btn + '</div>'
        '<div style="padding:16px 40px 28px;font-size:11px;line-height:1.7;color:#9CA3AF;border-top:1px solid #E5E7EB">'
        "3R Digest &middot; Weekly Intelligence on Ukraine's Human Capital"
        '</div>'
        '</div>'
        '</td></tr></table>'
        '</body></html>'
    )


def send_emails(emails, digest, pdf_path, link_pdf):
    body = build_email_body(digest, link_pdf)
    subject = f"3R Human Capital Digest -- {TODAY_STR}"
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for email in emails:
            msg = MIMEMultipart("mixed")
            msg["From"]    = GMAIL_USER
            msg["To"]      = email
            msg["Subject"] = Header(subject, "utf-8").encode()
            msg.attach(MIMEText(body, "html", "utf-8"))
            with open(pdf_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f"attachment; filename={Path(pdf_path).name}")
            msg.attach(part)
            server.sendmail(GMAIL_USER, email, msg.as_string())
            log.info(f"Sent to {email}")
            time.sleep(0.5)


def main():
    log.info(f"=== 3R Digest Agent starting -- {WEEK_TAG} ===")
    log.info(f"TEST MODE: {TEST_MODE}")
    articles = collect_all_articles()
    if not articles:
        log.error("No articles found. Aborting.")
        return
    history      = load_sent_history()
    new_articles = filter_new_articles(articles, history)
    log.info(f"New articles: {len(new_articles)} / {len(articles)}")
    if len(new_articles) < 5:
        log.warning("Fewer than 5 new articles -- using all collected.")
        new_articles = articles
    digest  = analyse_with_claude(new_articles)
    pdf_file = f"3R_Digest_{WEEK_TAG}.pdf"
    build_pdf(digest, pdf_file)
    link_pdf = upload_to_github(pdf_file, WEEK_TAG)
    emails  = get_email_list()
    if emails:
        send_emails(emails, digest, pdf_file, link_pdf)
    save_sent_history(mark_articles_sent(new_articles, history))
    log.info(f"=== Done. Sent to {len(emails)} recipients. ===")

if __name__ == "__main__":
    main()
