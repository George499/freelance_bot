import asyncio, sqlite3
from app.gpt import filter_project
from app.config_reader import Settings

c = Settings()

conn = sqlite3.connect('app/db/database/projects.db')
cur = conn.cursor()
cur.execute('SELECT title, description FROM project ORDER BY id DESC LIMIT 3')
rows = cur.fetchall()
conn.close()

async def test():
    for title, desc in rows:
        fit = await filter_project(title, desc or '', '60000', c.anthropic_api_key)
        print('fit=' + str(fit), '|', title[:70])

asyncio.run(test())
