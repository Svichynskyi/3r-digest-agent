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
    "Ukraine diaspora return skilled workers 2026",
    "Ukraine labor market workforce 2026",
    "human capital brain drain Ukraine",
    "Ukraine reskilling retraining workforce",
    "Ukraine reconstruction talent professionals",
    "skilled migration labor market Europe 2026",
    "reskilling upskilling workforce trends 2026",
    "human capital development policy 2026",
    "Ukraine veterans employment reintegration",
    "brain drain developing countries solutions 2026",
    "workforce skills gap global 2026",
    "diaspora investment knowledge transfer",
    "Ukraine education skills training 2026",
    "labor market migration EU trends 2026",
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

def collect_all_articles():
    seen, all_articles = set(), []
    for query in SEARCH_QUERIES:
        log.info(f"Searching: {query}")
        for art in search_articles(query):
            if art["url"] not in seen:
                seen.add(art["url"])
                all_articles.append(art)
    log.info(f"Collected {len(all_articles)} raw articles")
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

def analyse_with_claude(articles):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    articles_text = "\n\n".join([
        f"[{i+1}] TITLE: {a['title']}\nURL: {a['url']}\nSNIPPET: {a['snippet']}"
        for i, a in enumerate(articles)
    ])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
            f"Here are {len(articles)} articles collected this week.\n\n{articles_text}\n\n"
            f"Analyse through 3R lens. Return ONLY valid JSON:\n{DIGEST_SCHEMA}"}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip().rstrip("```").strip())


ACCENT = colors.HexColor("#1B4F72")
LIGHT  = colors.HexColor("#EBF5FB")
MUTED  = colors.HexColor("#5D6D7E")
SECTION_COLORS = {
    "return":         colors.HexColor("#1A5276"),
    "recruit":        colors.HexColor("#145A32"),
    "retain":         colors.HexColor("#6E2F1A"),
    "global_context": colors.HexColor("#4A235A"),
}
SECTION_LABELS = {
    "return":         "RETURN -- Restoring Connection",
    "recruit":        "RECRUIT -- Structural Reinforcement",
    "retain":         "RETAIN -- Environment for Accumulation",
    "global_context": "GLOBAL CONTEXT",
}

def build_pdf(digest, filename):
    doc = SimpleDocTemplate(filename, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    S = getSampleStyleSheet()
    def ps(name, **kw):
        return ParagraphStyle(name, parent=S["Normal"], **kw)

    title_s   = ps("ti", fontSize=22, textColor=ACCENT, spaceAfter=2*mm, fontName="Helvetica-Bold")
    sub_s     = ps("su", fontSize=11, textColor=MUTED,  spaceAfter=8*mm, fontName="Helvetica")
    exec_s    = ps("ex", fontSize=10.5, leading=16, spaceAfter=6*mm, fontName="Helvetica",
                   backColor=LIGHT, borderColor=ACCENT, borderWidth=1, borderPad=4*mm)
    sech_s    = ps("sh", fontSize=13, textColor=colors.white, fontName="Helvetica-Bold",
                   spaceAfter=4*mm, spaceBefore=6*mm)
    ititle_s  = ps("it", fontSize=10.5, fontName="Helvetica-Bold", textColor=ACCENT, spaceAfter=1*mm)
    ibody_s   = ps("ib", fontSize=9.5, leading=14, fontName="Helvetica", spaceAfter=1*mm)
    irel_s    = ps("ir", fontSize=9, leading=13, fontName="Helvetica-Oblique",
                   textColor=MUTED, spaceAfter=1*mm)
    link_s    = ps("lk", fontSize=8.5, fontName="Helvetica",
                   textColor=colors.HexColor("#2471A3"), spaceAfter=4*mm)
    insight_s = ps("ins", fontSize=11, leading=16, fontName="Helvetica-Bold",
                   textColor=ACCENT, backColor=LIGHT,
                   borderColor=ACCENT, borderWidth=1.5, borderPad=4*mm, spaceAfter=4*mm)
    footer_s  = ps("ft", fontSize=8, textColor=MUTED, fontName="Helvetica", alignment=1)

    story = []
    story.append(Paragraph("3R Model -- Human Capital", title_s))
    story.append(Paragraph(f"Weekly Digest - {TODAY_STR} - {digest.get('date_range', '')}", sub_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=5*mm))

    exec_text = digest.get("executive_summary_en", "")
    if exec_text:
        story.append(Paragraph(exec_text, exec_s))

    insight = digest.get("key_insight_en", "")
    if insight:
        story.append(Paragraph(f"<b>Key insight this week:</b> {insight}", insight_s))

    story.append(Spacer(1, 4*mm))

    for sec_key in ["return", "recruit", "retain", "global_context"]:
        items = digest.get("sections", {}).get(sec_key, [])
        if not items:
            continue
        sec_label = SECTION_LABELS[sec_key]
        hdr = Table([[Paragraph(sec_label, sech_s)]], colWidths=[170*mm])
        hdr.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), SECTION_COLORS[sec_key]),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 2*mm))

        for item in items:
            t   = item.get("title_en")    or item.get("title_ua", "")
            s   = item.get("summary_en")  or item.get("summary_ua", "")
            rel = item.get("relevance_en")or item.get("relevance_ua", "")
            u   = item.get("url", "")
            src = item.get("source", "")
            story.append(Paragraph(f"> {t}", ititle_s))
            story.append(Paragraph(s, ibody_s))
            if rel:
                story.append(Paragraph(f"<i>Relevance for 3R:</i> {rel}", irel_s))
            if u:
                story.append(Paragraph(f'<a href="{u}" color="#2471A3">{src or u[:60]}</a>', link_s))
            story.append(HRFlowable(width="100%", thickness=0.3,
                                    color=colors.lightgrey, spaceAfter=3*mm))

    story.append(Spacer(1, 6*mm))
    story.append(Paragraph(
        f"Digest generated automatically - {TODAY_STR} - 3R Model: Return - Recruit - Retain",
        footer_s
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


def build_email_body(digest, link_ua, link_en):
    exec_ua    = digest.get("executive_summary_ua", "")
    insight_ua = digest.get("key_insight_ua", "")
    exec_en    = digest.get("executive_summary_en", "")
    return (
        "<html>\n<head><meta charset=\"utf-8\"></head>\n"
        "<body style=\"font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#222\">\n"
        "<div style=\"background:#1B4F72;padding:20px 24px;border-radius:8px 8px 0 0\">\n"
        "  <h2 style=\"color:white;margin:0\">3R Model &#8212; Human Capital Digest</h2>\n"
        f"  <p style=\"color:#AED6F1;margin:4px 0 0\">{TODAY_STR} &#183; Weekly Digest</p>\n"
        "</div>\n"
        "<div style=\"padding:20px 24px;background:#f9f9f9;border:1px solid #ddd;border-top:none\">\n"
        f"  <p style=\"font-size:15px;line-height:1.6\">{exec_ua}</p>\n"
        "  <div style=\"background:#EBF5FB;border-left:4px solid #1B4F72;padding:12px 16px;"
        "margin:16px 0;border-radius:0 6px 6px 0\">\n"
        f"    <strong>Key insight:</strong> {insight_ua}\n"
        "  </div>\n"
        f"  <p style=\"font-size:13px;color:#555;line-height:1.5\">{exec_en}</p>\n"
        "  <div style=\"margin-top:20px\">\n"
        f"    <a href=\"{link_ua}\" style=\"background:#1B4F72;color:white;padding:10px 20px;"
        "border-radius:6px;text-decoration:none;font-size:14px;margin-right:10px\">PDF (UA)</a>\n"
        f"    <a href=\"{link_en}\" style=\"background:#145A32;color:white;padding:10px 20px;"
        "border-radius:6px;text-decoration:none;font-size:14px\">PDF (EN)</a>\n"
        "  </div>\n"
        "</div>\n"
        "<div style=\"padding:12px 24px;background:#eee;border-radius:0 0 8px 8px;"
        "font-size:11px;color:#888;text-align:center\">\n"
        "  3R Digest Agent &#183; Return &#183; Recruit &#183; Retain\n"
        "</div>\n"
        "</body></html>"
    )

def send_emails(emails, digest, pdf_ua, pdf_en, link_ua, link_en):
    body = build_email_body(digest, link_ua, link_en)
    subject = f"3R Human Capital Digest -- {TODAY_STR}"
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for email in emails:
            msg = MIMEMultipart("mixed")
            msg["From"]    = GMAIL_USER
            msg["To"]      = email
            msg["Subject"] = Header(subject, "utf-8").encode()
            msg.attach(MIMEText(body, "html", "utf-8"))
            for pdf_path in [pdf_ua, pdf_en]:
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
    build_pdf(digest, OUTPUT_PDF_UA)
    build_pdf(digest, OUTPUT_PDF_EN)
    link_ua = upload_to_github(OUTPUT_PDF_UA, WEEK_TAG)
    link_en = upload_to_github(OUTPUT_PDF_EN, WEEK_TAG)
    emails  = get_email_list()
    if emails:
        send_emails(emails, digest, OUTPUT_PDF_UA, OUTPUT_PDF_EN, link_ua, link_en)
    save_sent_history(mark_articles_sent(new_articles, history))
    log.info(f"=== Done. Sent to {len(emails)} recipients. ===")

if __name__ == "__main__":
    main()
