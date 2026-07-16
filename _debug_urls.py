import json, sqlite3, re
c = sqlite3.connect('instance/pixiv.db')
rows = c.execute('select pixiv_id, original_urls from illusts where original_urls is not null and length(original_urls) > 20 limit 5').fetchall()
for r in rows:
    urls = json.loads(r[1])
    for u in urls[:2]:
        m = re.match(r'(https://i\.pximg\.net/)img-original/img/(.+)\.(\w+)(\?.*)?$', u)
        if m:
            converted = f'{m.group(1)}c/800x800/img-master/img/{m.group(2)}_master1200.{m.group(3)}'
            print(f'#{r[0]}: OK  {converted[:150]}')
        else:
            print(f'#{r[0]}: FAIL {u[:150]}')
