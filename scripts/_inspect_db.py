import sqlite3

con = sqlite3.connect('instance/pixiv.db')
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
print('tables:', [r[0] for r in cur.fetchall()])
for t in ['illusts', 'search_cache', 'collections', 'collection_items', 'download_logs', 'blocked_tags']:
    try:
        cur.execute(f'SELECT COUNT(*) FROM {t}')
        print(t, '=', cur.fetchone()[0])
    except Exception as e:
        print(t, 'ERR', e)
con.close()
