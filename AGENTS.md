# Pixiv Viewer — Agent Guide

## What this is

Flask web app that searches/browses/downloads Pixiv illustrations via Pixiv's internal Ajax API (unofficial). One-user self-hosted service for a low-end server (4C/4GB/40GB/3Mbps).

**Tech**: Python 3.9+ / Flask 3.1+ / SQLAlchemy 2.0 / SQLite (WAL) / Bootstrap 5.3 / vanilla JS. No build pipeline, no tests, no linter, no type checker.

---

## Commands

```bash
# setup
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt

# dev
flask run --debug

# production (MUST use -w 1 — see gotcha)
gunicorn -w 1 --timeout 300 -b 127.0.0.1:8000 app:app

# optional: pixiv-api-http (Node.js Express proxy with SNI bypass)
cd pixiv-api-http-main && npm install && npm start  # → :1145
```

No test/lint/typecheck/format commands exist.

---

## Architecture

| File | Role |
|------|------|
| `app.py` | Flask entrypoint: all routes, download engine, background tasks, CSRF, rate limiting |
| `fetcher.py` | Pixiv API wrapper: cookie auth, search by tag/user/follow, illust detail |
| `models.py` | SQLAlchemy ORM: Illust, BlockedTag, DownloadLog, Collection, CollectionItem |
| `config.py` | Constants, env overrides, `instance/settings.json` override at import time |
| `templates/*.html` | 7 Jinja2 templates (index, gallery, bulk, downloads, logs, detail, settings, settings_unlock) |
| `static/` | `app.js` (124 lines), `style.css` (171 lines) |

No `__init__.py` — modules import directly. No `setup.py`/`pyproject.toml`.

---

## Critical gotchas

- **Gunicorn MUST use `-w 1`**: download state (`_download_progress`, `_bulk_tasks`, etc.) lives in process memory. Multiple workers don't share it.
- **settings.json restart required**: `config.py` reads `instance/settings.json` at import time. Changes via web UI only take effect after server restart.
- **Cookie hot-reload**: `fetcher.py` checks `cookies.txt` mtime on every API call — no restart needed on cookie refresh. Cookie expiry = silent empty search results.
- **`popular_d` sort needs Pixiv Premium**: non-Premium accounts get empty results with no error.
- **All Pixiv image requests need `Referer: https://www.pixiv.net/`** or 403. Thumbnail proxy at `/thumb/<base64_url>` handles this.
- **No DB migrations**: SQLAlchemy `create_all()` on startup. Schema changes need manual `ALTER TABLE` or DB rebuild.
- **5-min bulk download cleanup**: completed bulk tasks removed from `_bulk_tasks` after 300s (`threading.Timer`).

---

## Design tasks — use OpenDesign workflow

When user requests UI design, mockups, slides, brand, or design system work:

1. Check existing design system: `./opendesign/design-systems/*/`
2. Ask structured questions (audience, tone, fidelity, format, variants)
3. Output to `./opendesign/mockups/<task-slug>/` with `manifest.json`
4. Design constraints: no gradient abuse, no emoji icons, avoid Inter/Roboto/Arial, touch targets ≥44px
