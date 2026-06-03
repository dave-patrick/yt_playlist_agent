import sqlite3
import os

generic_categories = ["Entertainment", "Learning", "Uncategorized", "Music"]

db_path = "/home/ubuntu/youtube_playlist_agent/db.sqlite"
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in generic_categories)
    cursor.execute(f"DELETE FROM user_rules WHERE user_id = 1 AND target_category IN ({placeholders})", generic_categories)
    deleted_db = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"Deleted {deleted_db} rules mapping to generic categories from SQLite user_rules table for user 1.")

# Also clean local and VM text file
def clean_text_file(filepath):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    new_lines = []
    removed_count = 0
    generic_lower = [c.lower() for c in generic_categories]
    
    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            cat = parts[1].strip().lower()
            if cat in generic_lower:
                removed_count += 1
                continue
        new_lines.append(line)
        
    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"Cleaned {filepath}: Removed {removed_count} generic category channel rules.")

clean_text_file("/home/ubuntu/youtube_playlist_agent/yt_category_channel_map.txt")
clean_text_file("yt_category_channel_map.txt")
