import json
import os

base_dir = os.path.dirname(__file__)
report_path = os.path.join(base_dir, "playlists_report_1.json")

if not os.path.exists(report_path):
    print("Report file not found.")
    exit(1)

with open(report_path, "r", encoding="utf-8") as f:
    data = json.load(f)

titles = [
    '6 Months with the Oura Ring 4',
    'Oura Ring 4',
    'T-Mobile',
    'Tundra camping setup',
    'Collegiate Peaks',
    'Get Engaged',
    'Expanse of Coronado'
]

print("Scanning report for titles...")
for p in data:
    for v in p.get('videos', []):
        for t in titles:
            if t.lower() in v['title'].lower():
                print(f"Playlist: {p['name']}, Video: {v['title']}, ID: {v.get('playlist_item_id')}")
