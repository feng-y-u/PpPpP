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
| `fetcher.py` | Pixiv API wrapper: cookie/OAuth auth, search by tag/user/follow, illust detail |
| `models.py` | SQLAlchemy ORM: Illust, BlockedTag, DownloadLog, Collection, CollectionItem |
| `config.py` | Constants, env overrides, `instance/settings.json` override at import time |
| `templates/*.html` | 7 Jinja2 templates (index, gallery, bulk, downloads, detail, settings, settings_unlock) |
| `static/` | `app.js` (124 lines), `style.css` (171 lines), `vendor/bootstrap-5.3.3/` |
| `scripts/` | `pixiv-cleanup.sh` (cron disk cleanup, 30-day / <100 bookmarks) |

No `__init__.py` — modules import directly. No `setup.py`/`pyproject.toml`.

---

## Critical gotchas

- **Gunicorn MUST use `-w 1`**: download state (`_download_progress`, `_bulk_tasks`, etc.) lives in process memory. Multiple workers don't share it. The comment in `app.py:93-104` explains this.
- **settings.json requires server restart**: `config.py` reads `instance/settings.json` at import time. Changes via web UI only take effect after server restart.
- **Cookie hot-reload**: `fetcher.py` checks `cookies.txt` mtime on every API call — no restart needed on cookie refresh. Cookie expiry = silent empty search results. File supports `PHPSESSID=xxxxx` or bare `xxxxx` format.
- **OAuth auth also available**: Set `PIXIV_USERNAME`/`PIXIV_PASSWORD` env vars to use Pixiv OAuth Bearer tokens (auto-refreshes via refresh_token, no cookie maintenance). Client credentials (`MOBrBDS8blbauoSck0ZfDbtuzpyT`/`lsACyCD94FhDUtGTXi3QjcFE2uP2qW`) are public Pixiv app constants hardcoded in `fetcher.py:49-50`.
- **`popular_d` sort needs Pixiv Premium**: non-Premium accounts get empty results with no error. Discovery (`no query`) also uses `popular_d` by default.
- **All Pixiv image requests need `Referer: https://www.pixiv.net/`** or 403. Thumbnail proxy at `/thumb/<base64_url>` handles this.
- **No DB migrations**: SQLAlchemy `create_all()` on startup. `init_db()` has ad-hoc `ALTER TABLE` logic for specific columns (`file_size`, `description`, `is_favorite`, `favorited_at`). Other schema changes need manual intervention.
- **5-min bulk download cleanup**: completed bulk tasks removed from `_bulk_tasks` after 300s (`threading.Timer`).
- **Startup resets stuck downloads**: `_reset_stuck_downloads()` at import time clears any `downloading` status left from a crash and removes partial files.
- **Empty query → discovery**: When no search query is provided, `/search` falls back to `browse_discovery()` instead of `search_by_tag()`.
- **All POST endpoints require CSRF**: `X-CSRF-Token` header (from `GET /csrf-token` or embedded in page) must be sent. Returns 403 if missing/wrong. Implemented via `_csrf_required` decorator in `app.py`.
- **Rate limiting is per-worker in-memory**: `_rate_limit` decorator (saves timestamps per IP in a dict). With `-w 1` this works; with multiple workers each has its own counter. Only applied to `/api/settings/unlock` currently.
- **SSL verification disabled by default**: `SSL_VERIFY = False` in `config.py`. Pixiv's certificate chain may fail on some systems. Set `True` in production if CA certs are installed.
- **Secret key auto-generated**: written to `instance/.secret_key` on first startup. Deleting this file invalidates all sessions (users get logged out).
- **Favorites backed by Collection model**: `is_favorite` is a computed field derived from membership in the "我的收藏" collection. Toggling favorite adds/removes from that collection. `init_db()` migrates old `is_favorite=True` records on startup.

---

## Design tasks — use OpenDesign workflow

When user requests UI design, mockups, slides, brand, or design system work:

1. Check existing design system: `./opendesign/design-systems/*/`
2. Ask structured questions (audience, tone, fidelity, format, variants)
3. Output to `./opendesign/mockups/<task-slug>/` with `manifest.json`
4. Design constraints: no gradient abuse, no emoji icons, avoid Inter/Roboto/Arial, touch targets ≥44px
