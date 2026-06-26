"""
3R Model Human Capital Digest Agent
Runs every Monday morning (09:00 Kyiv), collects news, generates bilingual PDF,
saves to Google Drive, emails all subscribers from Google Sheet.
"""

import os, json, hashlib, datetime, time, logging
from pathlib import Path

import anthropic, requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler("logs/digest.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID      = "1vguK81gR39CNcTaZFMld8HCQ1i1odSHEPw0WAi-ayas"
DRIVE_FOLDER_NAME    = os.environ.get("DRIVE_FOLDER_NAME", "3R Human Capital Digest")
GMAIL_USER           = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

SENT_HISTORY_FILE = Path("sent_history/sent_urls.json")
WEEK_TAG  = datetime.date.today().strftime("%Y-W%V")
TODAY_STR = datetime.date.today().strftime("%d.%m.%Y")
OUTPUT_PDF_UA = f"3R_Digest_{WEEK_TAG}_UA.pdf"
OUTPUT_PDF_EN = f"3R_Digest_{WEEK_TAG}_EN.pdf"

SEARCH_QUERIES = [
    "ÑÐºÑÐ°ÑÐ½ÑÑÐºÐ° Ð´ÑÐ°ÑÐ¿Ð¾ÑÐ° Ð¿Ð¾Ð²ÐµÑÐ½ÐµÐ½Ð½Ñ 2025",
    "brain circulation Ukraine diaspora 2025",
    "Ukrainian professionals return home 2025",
    "Ð·Ð°Ð»ÑÑÐµÐ½Ð½Ñ ÑÐ¿ÐµÑÑÐ°Ð»ÑÑÑÑÐ² Ð£ÐºÑÐ°ÑÐ½Ð° 2025",
    "talent attraction Ukraine reconstruction",
    "structural skills gap Ukraine labor market",
    "Ð¿ÐµÑÐµÐºÐ²Ð°Ð»ÑÑÑÐºÐ°ÑÑÑ reskilling Ð£ÐºÑÐ°ÑÐ½Ð° 2025",
    "over-qualification brain waste Ukraine",
    "reskilling demand-driven workforce 2025",
    "human capital global trends 2025",
    "brain drain developing countries solutions",
    "workforce development reconstruction post-war",
    "ÑÐ¸Ð½Ð¾Ðº Ð¿ÑÐ°ÑÑ Ð£ÐºÑÐ°ÑÐ½Ð° 2025",
    "Ð»ÑÐ´ÑÑÐºÐ¸Ð¹ ÐºÐ°Ð¿ÑÑÐ°Ð» Ð£ÐºÑÐ°ÑÐ½Ð° Ð´ÐµÐ¼Ð¾Ð³ÑÐ°ÑÑÑ",
]

SEARX_INSTANCE = "https://searx.be"


# ââ Deduplication âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
        history[url_fingerprint(a["url"])] = {"url": a["url"], "title": a["title"], "sent_week": WEEK_TAG}
    return history


# ââ News collection âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def search_articles(query, max_results=5):
    articles = []
    try:
        params = {"q": query, "format": "json", "time_range": "week", "categories": "news,general"}
        headers = {"User-Agent": "Mozilla/5.0 (compatible; 3RDigestBot/1.0)"}
        resp = requests.get(f"{SEARX_INSTANCE}/search", params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        for r in resp.json().get("results", [])[:max_results]:
            articles.append({"title": r.get("title",""), "url": r.get("url",""),
                             "snippet": r.get("content",""), "source": r.get("engine","web")})
    except Exception as e:
        log.warning(f"Search failed for '{query}': {e}")
    time.sleep(1)
    return articles

def collect_all_articles():
    seen, all_articles = set(), []
    for query in SEARCH_QUERIES:
        log.info(f"Searching: {query}")
        for art in search_articles(query, max_results=6):
            if art["url"] not in seen and art["url"]:
                seen.add(art["url"])
                all_articles.append(art)
    log.info(f"Collected {len(all_articles)} raw articles")
    return all_articles


# ââ Claude analysis âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

SYSTEM_PROMPT = """You are an expert analyst for the 3R Model (Return, Recruit, Retain) â
a human capital management framework for Ukraine.
Analyse the provided news articles and produce a structured weekly digest.

3R Model:
- RETURN: restoring connection with Ukrainian human capital abroad (diaspora, dual engagement, knowledge circulation)
- RECRUIT: attracting professionals with competencies unavailable domestically (structural gaps, targeted attraction)
- RETAIN: creating conditions for skills application, reskilling, reducing brain waste, building R&D environment

Rules:
- Only include articles genuinely relevant to human capital, labor markets, education, migration, demographics, reskilling, or reconstruction
- Group under Return / Recruit / Retain + Global Context
- For each item: 1-2 sentence summary + why it matters for 3R + source link
- Be analytical, not descriptive
- Return ONLY valid JSON, no markdown fences"""

DIGEST_SCHEMA = """{
  "week": "string",
  "date_range": "string",
  "executive_summary_ua": "3-4 sentences Ukrainian",
  "executive_summary_en": "3-4 sentences English",
  "sections": {
    "return": [{"title_ua":"","title_en":"","summary_ua":"","summary_en":"","relevance_ua":"","relevance_en":"","url":"","source":""}],
    "recruit": [],
    "retain": [],
    "global_context": []
  },
  "key_insight_ua": "one sentence Ukrainian",
  "key_insight_en": "one sentence English"
}"""

def analyse_with_claude(articles):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    articles_text = "\n\n".join([
        f"[{i+1}] TITLE: {a['title']}\nURL: {a['url']}\nSNIPPET: {a['snippet']}"
        for i, a in enumerate(articles)
    ])
    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
            f"Here are {len(articles)} articles.\n\n{articles_text}\n\nReturn ONLY valid JSON:\n{DIGEST_SCHEMA}"}],
    )
    raw = response.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


# ââ PDF generation ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

ACCENT = colors.HexColor("#1B4F72")
LIGHT  = colors.HexColor("#EBF5FB")
MUTED  = colors.HexColor("#5D6D7E")
SECTION_COLORS = {
    "return": colors.HexColor("#1A5276"),
    "recruit": colors.HexColor("#145A32"),
    "retain": colors.HexColor("#6E2F1A"),
    "global_context": colors.HexColor("#4A235A"),
}
SECTION_LABELS = {
    "return":         ("RETURN â ÐÑÐ´Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ Ð·Ð²'ÑÐ·ÐºÑ",    "RETURN â Restoring Connection"),
    "recruit":        ("RECRUIT â Ð¡ÑÑÑÐºÑÑÑÐ½Ðµ Ð¿ÑÐ´ÑÐ¸Ð»ÐµÐ½Ð½Ñ", "RECRUIT â Structural Reinforcement"),
    "retain":         ("RETAIN â Ð¡ÐµÑÐµÐ´Ð¾Ð²Ð¸ÑÐµ Ð½Ð°ÐºÐ¾Ð¿Ð¸ÑÐµÐ½Ð½Ñ", "RETAIN â Environment for Accumulation"),
    "global_context": ("ÐÐÐÐÐÐÐ¬ÐÐÐ ÐÐÐÐ¢ÐÐÐ¡Ð¢",             "GLOBAL CONTEXT"),
}

def build_pdf(digest, filename, lang="ua"):
    doc = SimpleDocTemplate(filename, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    S = getSampleStyleSheet()
    def ps(name, **kw): return ParagraphStyle(name, parent=S["Normal"], **kw)

    title_s   = ps("ti", fontSize=22, textColor=ACCENT, spaceAfter=2*mm,  fontName="Helvetica-Bold")
    sub_s     = ps("su", fontSize=11, textColor=MUTED,  spaceAfter=8*mm,  fontName="Helvetica")
    exec_s    = ps("ex", fontSize=10.5, leading=16, spaceAfter=6*mm, fontName="Helvetica",
                   backColor=LIGHT, borderColor=ACCENT, borderWidth=1, borderPad=4*mm)
    sech_s    = ps("sh", fontSize=13, textColor=colors.white, fontName="Helvetica-Bold",
                   spaceAfter=4*mm, spaceBefore=6*mm)
    ititle_s  = ps("it", fontSize=10.5, fontName="Helvetica-Bold", textColor=ACCENT, spaceAfter=1*mm)
    ibody_s   = ps("ib", fontSize=9.5,  leading=14, fontName="Helvetica", spaceAfter=1*mm)
    irel_s    = ps("ir", fontSize=9,    leading=13, fontName="Helvetica-Oblique",
                   textColor=MUTED, spaceAfter=1*mm)
    link_s    = ps("lk", fontSize=8.5, fontName="Helvetica",
                   textColor=colors.HexColor("#2471A3"), spaceAfter=4*mm)
    insight_s = ps("ins", fontSize=11, leading=16, fontName="Helvetica-Bold",
                   textColor=ACCENT, backColor=LIGHT,
                   borderColor=ACCENT, borderWidth=1.5, borderPad=4*mm, spaceAfter=4*mm)
    footer_s  = ps("ft", fontSize=8, textColor=MUTED, fontName="Helvetica", alignment=1)

    story = []
    label = "Ð©Ð¾ÑÐ¸Ð¶Ð½ÐµÐ²Ð¸Ð¹ Ð´Ð°Ð¹Ð´Ð¶ÐµÑÑ" if lang == "ua" else "Weekly Digest"
    story.append(Paragraph("3R Model â Human Capital", title_s))
    story.append(Paragraph(f"{label} Â· {TODAY_STR} Â· {digest.get('date_range','')}", sub_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=5*mm))

    exec_text = digest.get(f"executive_summary_{lang}", "")
    if exec_text:
        story.append(Paragraph(exec_text, exec_s))

    insight = digest.get(f"key_insight_{lang}", "")
    if insight:
        prefix = "ÐÐ¾Ð»Ð¾Ð²Ð½Ð¸Ð¹ Ð²Ð¸ÑÐ½Ð¾Ð²Ð¾Ðº ÑÐ¸Ð¶Ð½Ñ:" if lang == "ua" else "Key insight this week:"
        story.append(Paragraph(f"<b>{prefix}</b> {insight}", insight_s))

    story.append(Spacer(1, 4*mm))

    for sec_key in ["return", "recruit", "retain", "global_context"]:
        items = digest.get("sections", {}).get(sec_key, [])
        if not items:
            continue
        sec_label = SECTION_LABELS[sec_key][0 if lang == "ua" else 1]
        hdr = Table([[Paragraph(sec_label, sech_s)]], colWidths=[170*mm])
        hdr.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), SECTION_COLORS.get(sec_key, ACCENT)),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 2*mm))

        for item in items:
            t = item.get(f"title_{lang}")    or item.get("title_ua", "")
            s = item.get(f"summary_{lang}")  or item.get("summary_ua", "")
            r = item.get(f"relevance_{lang}")or item.get("relevance_ua", "")
            u = item.get("url", "")
            src = item.get("source", "")
            story.append(Paragraph(f"\u25b8 {t}", ititle_s))
            story.append(Paragraph(s, ibody_s))
            if r:
                pref = "ÐÐ½Ð°ÑÐµÐ½Ð½Ñ Ð´Ð»Ñ 3R:" if lang == "ua" else "Relevance for 3R:"
                story.append(Paragraph(f"<i>{pref}</i> {r}", irel_s))
            if u:
                story.append(Paragraph(f'<a href="{u}" color="#2471A3">{src or u[:60]}</a>', link_s))
            story.append(HRFlowable(width="100%", thickness=0.3, color=colors.lightgrey, spaceAfter=3*mm))

    story.append(Spacer(1, 6*mm))
    ft = (f"ÐÐ°Ð¹Ð´Ð¶ÐµÑÑ ÑÑÐ¾ÑÐ¼Ð¾Ð²Ð°Ð½Ð¾ Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐ½Ð¾ Â· {TODAY_STR} Â· 3R Model: Return Â· Recruit Â· Retain"
          if lang == "ua" else
          f"Digest generated automatically Â· {TODAY_STR} Â· 3R Model: Return Â· Recruit Â· Retain")
    story.append(Paragraph(ft, footer_s))
    doc.build(story)
    log.info(f"PDF generated: {filename}")


# ââ Google Drive ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def get_drive_service():
    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)

def get_or_create_folder(service, folder_name):
    q = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    files = service.files().list(q=q, fields="files(id)").execute().get("files", [])
    if files:
        return files[0]["id"]
    f = service.files().create(
        body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id").execute()
    return f["id"]

def upload_to_drive(service, filepath, folder_id):
    meta = {"name": Path(filepath).name, "parents": [folder_id]}
    media = MediaFileUpload(filepath, mimetype="application/pdf")
    f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    log.info(f"Uploaded: {f.get('webViewLink')}")
    return f.get("webViewLink", "")


# ââ Email list ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def get_email_list():
    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly"])
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)
    emails = [v.strip() for v in ws.col_values(5) if "@" in v]
    log.info(f"Loaded {len(emails)} emails")
    return emails


# ââ Email sending âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def build_email_body(digest, link_ua, link_en):
    exec_ua    = digest.get("executive_summary_ua", "")
    insight_ua = digest.get("key_insight_ua", "")
    exec_en    = digest.get("executive_summary_en", "")
    return f"""<html><body style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#222">
<div style="background:#1B4F72;padding:20px 24px;border-radius:8px 8px 0 0">
  <h2 style="color:white;margin:0">3R Model â Human Capital Digest</h2>
  <p style="color:#AED6F1;margin:4px 0 0">{TODAY_STR} Â· Ð©Ð¾ÑÐ¸Ð¶Ð½ÐµÐ²Ð¸Ð¹ Ð´Ð°Ð¹Ð´Ð¶ÐµÑÑ</p>
</div>
<div style="padding:20px 24px;background:#f9f9f9;border:1px solid #ddd;border-top:none">
  <p style="font-size:15px;line-height:1.6">{exec_ua}</p>
  <div style="background:#EBF5FB;border-left:4px solid #1B4F72;padding:12px 16px;margin:16px 0;border-radius:0 6px 6px 0">
    <strong>ÐÐ¾Ð»Ð¾Ð²Ð½Ð¸Ð¹ Ð²Ð¸ÑÐ½Ð¾Ð²Ð¾Ðº:</strong> {insight_ua}
  </div>
  <p style="font-size:13px;color:#555;line-height:1.5">{exec_en}</p>
  <div style="margin-top:20px">
    <a href="{link_ua}" style="background:#1B4F72;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-size:14px;margin-right:10px">PDF (UA)</a>
    <a href="{link_en}" style="background:#145A32;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-size:14px">PDF (EN)</a>
  </div>
</div>
<div style="padding:12px 24px;background:#eee;border-radius:0 0 8px 8px;font-size:11px;color:#888;text-align:center">
  3R Digest Agent Â· Return Â· Recruit Â· Retain
</div></body></html>"""

def send_emails(emails, digest, pdf_ua, pdf_en, link_ua, link_en):
    body = build_email_body(digest, link_ua, link_en)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for email in emails:
            msg = MIMEMultipart("mixed")
            msg["From"]    = GMAIL_USER
            msg["To"]      = email
            msg["Subject"] = f"3R Human Capital Digest â {TODAY_STR}"
            msg.attach(MIMEText(body, "html"))
            for pdf_path in [pdf_ua, pdf_en]:
                with open(pdf_path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={Path(pdf_path).name}")
                msg.attach(part)
            server.sendmail(GMAIL_USER, email, msg.as_string())
            log.info(f"Sent to {email}")
            time.sleep(0.5)


# ââ Main ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def main():
    log.info(f"=== 3R Digest Agent starting â {WEEK_TAG} ===")
    articles = collect_all_articles()
    if not articles:
        log.error("No articles found. Aborting.")
        return
    history      = load_sent_history()
    new_articles = filter_new_articles(articles, history)
    log.info(f"New articles: {len(new_articles)} / {len(articles)}")
    if len(new_articles) < 5:
        log.warning("Fewer than 5 new articles â using all collected.")
        new_articles = articles
    digest    = analyse_with_claude(new_articles)
    build_pdf(digest, OUTPUT_PDF_UA, lang="ua")
    build_pdf(digest, OUTPUT_PDF_EN, lang="en")
    drive     = get_drive_service()
    folder_id = get_or_create_folder(drive, DRIVE_FOLDER_NAME)
    link_ua   = upload_to_drive(drive, OUTPUT_PDF_UA, folder_id)
    link_en   = upload_to_drive(drive, OUTPUT_PDF_EN, folder_id)
    emails    = get_email_list()
    if emails:
        send_emails(emails, digest, OUTPUT_PDF_UA, OUTPUT_PDF_EN, link_ua, link_en)
    save_sent_history(mark_articles_sent(new_articles, history))
    log.info(f"=== Done. Sent to {len(emails)} recipients. ===")

if __name__ == "__main__":
    main()
