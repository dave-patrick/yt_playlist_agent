import sqlite3

conn = sqlite3.connect('/home/ubuntu/youtube_playlist_agent/db.sqlite')
conn.row_factory = sqlite3.Row
channels = [
    "Michael Sasser",
    "Kristin's Friends Cooking",
    "The Vintage Fame",
    "SWEROK+",
    "Mr Sunday Movies",
    "AI Golden Age Studios"
]
placeholders = ",".join("?" for _ in channels)
rules = conn.execute(f"SELECT * FROM user_rules WHERE user_id = 1 AND channel_name IN ({placeholders})", channels).fetchall()
conn.close()

print(f"Rules found: {len(rules)}")
for r in rules:
    print(f"  {r['channel_name']} -> {r['target_category']}")
