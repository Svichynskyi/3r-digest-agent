# 3R Digest Agent

Щотижневий автоматичний дайджест людського капіталу для 3R Model (Return · Recruit · Retain).

**Запускається:** кожного понеділка о 09:00 за Києвом  
**Формат:** двомовний PDF (UA + EN)  
**Розсилка:** Google Sheet → https://docs.google.com/spreadsheets/d/1vguK81gR39CNcTaZFMld8HCQ1i1odSHEPw0WAi-ayas/

## Як це працює

1. Збирає ~80+ матеріалів за 14 пошуковими запитами (Return / Recruit / Retain / Global)
2. Перевіряє дедуплікацію — пропускає новини, що вже відправлялись
3. Аналізує через Claude Sonnet, групує по блоках 3R моделі
4. Генерує два PDF (українська + англійська версії)
5. Зберігає у папку "3R Human Capital Digest" на Google Drive
6. Зчитує актуальний список email з Google Sheet (оновлюйте коли завгодно)
7. Надсилає email з резюме + PDF вкладеннями

## Необхідні секрети (Settings → Secrets → Actions)

| Секрет | Де взяти |
|--------|----------|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `GMAIL_USER` | email відправника |
| `GMAIL_APP_PASSWORD` | myaccount.google.com/apppasswords |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud Console → Service Account → JSON key |

## Налаштування Google Service Account

1. [Google Cloud Console](https://console.cloud.google.com) → Create Project
2. Увімкнути: Google Drive API, Google Sheets API
3. IAM & Admin → Service Accounts → Create → завантажити JSON key
4. Поділитись Google Sheet з email сервісного акаунту (Viewer)
5. Поділитись папкою Drive з email сервісного акаунту (Editor)

## Запуск вручну

Actions → "3R Human Capital Digest" → Run workflow
