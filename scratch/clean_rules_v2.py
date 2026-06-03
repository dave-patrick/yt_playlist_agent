import sqlite3
import os

channels_to_remove = [
    "Mr Sunday Movies",
    "SWEROK+",
    "The Vintage Fame",
    "Kristin's Friends Cooking",
    "AI Golden Age Studios",
    "Michael Sasser"
]

db_path = "/home/ubuntu/youtube_playlist_agent/db.sqlite"
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in channels_to_remove)
    cursor.execute(f"DELETE FROM user_rules WHERE user_id = 1 AND channel_name IN ({placeholders})", channels_to_remove)
    deleted_db = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"Deleted {deleted_db} rules from SQLite user_rules table for user 1.")

# Also clean local and VM text file if they exist
def clean_text_file(filepath):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    new_lines = []
    removed_count = 0
    to_remove_lower = [c.lower() for c in channels_to_remove]
    
    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            ch = parts[0].strip().lower()
            if ch in to_remove_lower:
                removed_count += 1
                continue
        new_lines.append(line)
        
    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"Cleaned {filepath}: Removed {removed_count} channel rules.")

clean_text_file("/home/ubuntu/youtube_playlist_agent/yt_category_channel_map.txt")
clean_text_file("yt_category_channel_map.txt")
