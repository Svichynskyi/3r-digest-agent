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
DIRECT_SOURCES = [
    # IOM Ukraine
    "https://ukraine.iom.int/news",
    # UNHCR Ukraine
    "https://www.unhcr.org/ua/en/news",
    # VoxUkraine (English)
    "https://voxukraine.org/en/category/labour-market/",
    # Cedos think tank
    "https://cedos.org.ua/en/researches/",
    # OECD migration
    "https://www.oecd.org/en/topics/migration.html",
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


def search_articles(query, max_results=6):
    articles = []
    try:
        _key = os.environ.get("NEWSAPI_KEY", "")
        if _key:
            params = {
                "q": query,
                "apiKey": _key,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": max_results,
                "from": (datetime.date.today() - datetime.timedelta(days=30)).isoformat(),
            }
            resp = requests.get("https://newsapi.org/v2/everything", params=params, timeout=15)
            resp.raise_for_status()
            for r in resp.json().get("articles", [])[:max_results]:
                if r.get("title") and r.get("url"):
                    articles.append({
                        "title": r["title"],
                        "url": r["url"],
                        "snippet": (r.get("description") or r.get("content") or "")[:300],
                        "source": r.get("source", {}).get("name", "web"),
                    })
    except Exception as e:
        log.warning(f"Search failed for '{query}': {e}")
    time.sleep(0.5)
    return articles


def scrape_direct_sources():
    """Scrape specialized sources (IOM, UNHCR, VoxUkraine, Cedos, OECD) directly."""
    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self, base_url):
            super().__init__()
            self.base_url = base_url
            self.links = []
            self._cur_href = None
            self._cur_text = ""
            self._in_a = False

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                attrs_d = dict(attrs)
                href = attrs_d.get("href", "")
                if href and not href.startswith("#") and not href.startswith("javascript"):
                    if href.startswith("/"):
                        from urllib.parse import urlparse
                        p = urlparse(self.base_url)
                        href = f"{p.scheme}://{p.netloc}{href}"
                    elif not href.startswith("http"):
                        href = self.base_url.rstrip("/") + "/" + href
                    self._cur_href = href
                    self._cur_text = ""
                    self._in_a = True

        def handle_data(self, data):
            if self._in_a:
                self._cur_text += data.strip()

        def handle_endtag(self, tag):
            if tag == "a" and self._in_a and self._cur_href and len(self._cur_text) > 20:
                self.links.append((self._cur_href, self._cur_text))
                self._in_a = False
                self._cur_href = None
                self._cur_text = ""

    SOURCE_META = {
        "ukraine.iom.int":   "IOM Ukraine",
        "unhcr.org":         "UNHCR",
        "voxukraine.org":    "VoxUkraine",
        "cedos.org.ua":      "Cedos",
        "oecd.org":          "OECD",
    }

    articles = []
    for url in DIRECT_SOURCES:
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                log.warning(f"Direct source {url} returned {resp.status_code}")
                continue
            parser = LinkParser(url)
            parser.feed(resp.text)

            # Pick source name
            src_name = "web"
            for domain, name in SOURCE_META.items():
                if domain in url:
                    src_name = name
                    break

            # Filter links that look like article URLs (contain year or keywords)
            cutoff = datetime.date.today() - datetime.timedelta(days=30)
            year_str = str(datetime.date.today().year)
            prev_year = str(datetime.date.today().year - 1)
            added = 0
            for href, text in parser.links:
                if added >= 5:
                    break
                # Basic relevance filter on URL/text
                combined = (href + " " + text).lower()
                skip_keywords = ["privacy", "cookie", "contact", "about", "subscribe",
                                 "login", "register", "donate", "career", "jobs"]
                if any(k in combined for k in skip_keywords):
                    continue
                # Prefer links with year in URL or relevant keywords
                relevant_kw = ["ukrain", "migr", "labour", "labor", "skill", "refugee",
                               "return", "diaspora", "workforce", "educat", "reconstruct",
                               "human capital", "employ", "veter"]
                is_relevant = any(k in combined for k in relevant_kw)
                has_year = year_str in href or prev_year in href
                if is_relevant or has_year:
                    articles.append({
                        "title": text[:200],
                        "url": href,
                        "snippet": f"From {src_name}: {text[:250]}",
                        "source": src_name,
                    })
                    added += 1
            log.info(f"Direct scrape {src_name}: {added} articles")
        except Exception as e:
            log.warning(f"Direct source scrape failed for {url}: {e}")
        time.sleep(1)
    return articles


def collect_all_articles():
    seen, all_articles = set(), []

    # 1. NewsAPI searches
    for query in SEARCH_QUERIES:
        log.info(f"Searching: {query}")
        for art in search_articles(query):
            if art["url"] not in seen:
                seen.add(art["url"])
                all_articles.append(art)

    # 2. Direct specialized sources
    log.info("Scraping specialized sources (IOM, UNHCR, VoxUkraine, Cedos, OECD)...")
    for art in scrape_direct_sources():
        if art["url"] not in seen:
            seen.add(art["url"])
            all_articles.append(art)

    log.info(f"Collected {len(all_articles)} raw articles total")
    return all_articles


SYSTEM_PROMPT = """You are an expert analyst for the 3R Model (Return, Recruit, Retain) -- a human capital management framework for Ukraine.
Analyse the provided news articles and produce a structured weekly digest.

3R Model:
- RETURN: restoring connection with Ukrainian human capital abroad (diaspora, dual engagement, knowledge circulation)
- RECRUIT: attracting professionals with competencies unavailable domestically (structural gaps, targeted attraction)
- RETAIN: conditions for skills application, reskilling, reducing brain waste, building R&D environment

Rules:
- Include ONLY articles relevant to human capital, labor markets, education, migration, demographics, reskilling, or reconstruction
- If fewer than 3 articles are relevant, still produce the digest with what you have and note the limitation
- Group under Return / Recruit / Retain + Global Context sections
- For each item: 1-2 sentence summary + why it matters for 3R + source link
- Be analytical, not descriptive
- Return ONLY valid JSON, no markdown fences, no extra text"""

DIGEST_SCHEMA = """{
  "week": "Week 26, 2026",
  "date_range": "June 22-28, 2026",
  "executive_summary_ua": "3-4 sentences in Ukrainian about this week's key findings",
  "executive_summary_en": "3-4 sentences in English about this week's key findings",
  "sections": {
    "return": [{"title_ua":"","title_en":"","summary_ua":"","summary_en":"","relevance_ua":"","relevance_en":"","url":"","source":""}],
    "recruit": [],
    "retain": [],
    "global_context": []
  },
  "key_insight_ua": "The single most important takeaway this week in Ukrainian",
  "key_insight_en": "The single most important takeaway this week in English"
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


def analyse_with_claude(articles):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    # Clean article content before sending
    clean_articles = []
    for a in articles:
        clean_articles.append({
            "title": clean_for_json(a.get("title", "")),
            "url": a.get("url", ""),
            "snippet": clean_for_json(a.get("snippet", "")),
            "source": clean_for_json(a.get("source", "")),
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
            f"Here are {len(clean_articles)} articles collected this week.\n\n{articles_text}\n\n"
            f"Analyse through 3R lens. IMPORTANT: Return ONLY valid JSON. "
            f"Do not use double quotes inside string values - use single quotes or rephrase instead.\n{DIGEST_SCHEMA}"}],
    )
    raw = response.content[0].text.strip()
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
    # Try to find JSON object
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}")
        log.error(f"Raw response (first 500): {raw[:500]}")
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
