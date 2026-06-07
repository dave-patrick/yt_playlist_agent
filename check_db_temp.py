import sqlite3
import os
import json

db_path = os.path.join(os.path.dirname(__file__), "db.sqlite")
print(f"Connecting to {db_path}...")
c = sqlite3.connect(db_path)

tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)

for t in tables:
    count = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"Table '{t}' count: {count}")

maint_file = os.path.join(os.path.dirname(__file__), "maintenance_actions_1.json")
if os.path.exists(maint_file):
    with open(maint_file, "r") as f:
        actions = json.load(f)
    print(f"\nFound {len(actions)} actions in {maint_file}")
    if actions:
        print("First 3 actions:")
        for a in actions[:3]:
            print(f" - Vid: {a.get('vid')}, Title: {a.get('title')}, Type: {a.get('type')}, Target/Removes: {a.get('to') or a.get('remove')}")
else:
    print(f"\n{maint_file} does not exist.")

