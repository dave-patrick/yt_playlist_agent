import sqlite3
import os
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite")
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")

def get_settings():
    import json
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        created_at TEXT NOT NULL
    )
    """)
    
    # 2. Credentials Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS credentials (
        user_id INTEGER PRIMARY KEY,
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        token_expiry INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)
    
    # 3. User Rules Table (Channel mappings)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_rules (
        user_id INTEGER,
        channel_name TEXT NOT NULL,
        target_category TEXT NOT NULL,
        PRIMARY KEY (user_id, channel_name),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)
    
    # 4. Sessions Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expiry INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)
    
    # 5. Manual Moves Table (Tracks user manually moved videos to prevent auto-restoring)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS manual_moves (
        user_id INTEGER,
        video_id TEXT NOT NULL,
        target_playlist TEXT NOT NULL,
        moved_at TEXT NOT NULL,
        PRIMARY KEY (user_id, video_id)
    )
    """)
    
    conn.commit()
    conn.close()

# User Management
def get_or_create_user(email, name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    if row:
        user_id = row["id"]
    else:
        created_at = datetime.now().isoformat()
        cursor.execute("INSERT INTO users (email, name, created_at) VALUES (?, ?, ?)", (email, name, created_at))
        user_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return user_id

def get_user_by_id(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# Credentials Management
def save_user_credentials(user_id, access_token, refresh_token, expires_in):
    conn = get_db_connection()
    cursor = conn.cursor()
    token_expiry = int(time.time()) + expires_in
    
    # Check if exists
    cursor.execute("SELECT user_id FROM credentials WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        if refresh_token:
            cursor.execute("""
            UPDATE credentials SET access_token = ?, refresh_token = ?, token_expiry = ? WHERE user_id = ?
            """, (access_token, refresh_token, token_expiry, user_id))
        else:
            cursor.execute("""
            UPDATE credentials SET access_token = ?, token_expiry = ? WHERE user_id = ?
            """, (access_token, token_expiry, user_id))
    else:
        cursor.execute("""
        INSERT INTO credentials (user_id, access_token, refresh_token, token_expiry) VALUES (?, ?, ?, ?)
        """, (user_id, access_token, refresh_token, token_expiry))
        
    conn.commit()
    conn.close()

def get_user_credentials(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM credentials WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# Session Management
def create_session(session_id, user_id, expires_in=86400*30):
    conn = get_db_connection()
    cursor = conn.cursor()
    expiry = int(time.time()) + expires_in
    cursor.execute("INSERT OR REPLACE INTO sessions (session_id, user_id, expiry) VALUES (?, ?, ?)", (session_id, user_id, expiry))
    conn.commit()
    conn.close()

def get_session_user(session_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT s.user_id, u.email, u.name FROM sessions s 
    JOIN users u ON s.user_id = u.id 
    WHERE s.session_id = ? AND s.expiry > ?
    """, (session_id, int(time.time())))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_session(session_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()

# User-Specific Rules
def load_user_rules(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT channel_name, target_category FROM user_rules WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return {row["channel_name"]: row["target_category"] for row in rows}

def save_user_rule(user_id, channel_name, target_category, is_auto_learned=False):
    if is_auto_learned:
        print(f"Skipping auto-learned rule for {channel_name} -> {target_category} (automatic rule learning is disabled)")
        return


            
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO user_rules (user_id, channel_name, target_category) VALUES (?, ?, ?)
    """, (user_id, channel_name, target_category))
    conn.commit()
    conn.close()


def import_default_rules_if_empty(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM user_rules WHERE user_id = ?", (user_id,))
    count = cursor.fetchone()["cnt"]
    if count == 0:
        map_path = os.path.join(os.path.dirname(__file__), "yt_category_channel_map.txt")
        if os.path.exists(map_path):
            try:
                rules = []
                with open(map_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if ":" in line:
                            parts = line.strip().split(":")
                            if len(parts) == 2:
                                rules.append((user_id, parts[0].strip(), parts[1].strip()))
                if rules:
                    cursor.executemany("""
                    INSERT OR REPLACE INTO user_rules (user_id, channel_name, target_category) VALUES (?, ?, ?)
                    """, rules)
            except Exception as e:
                print(f"Error importing default rules: {e}")

def save_manual_move(user_id, video_id, target_playlist):
    user_id = user_id or 1
    conn = get_db_connection()
    cursor = conn.cursor()
    moved_at = datetime.now().isoformat()
    cursor.execute("""
    INSERT OR REPLACE INTO manual_moves (user_id, video_id, target_playlist, moved_at) VALUES (?, ?, ?, ?)
    """, (user_id, video_id, target_playlist, moved_at))
    conn.commit()
    conn.close()

def get_manual_moves(user_id):
    user_id = user_id or 1
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT video_id, target_playlist FROM manual_moves WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return {row["video_id"]: row["target_playlist"] for row in rows}

# Initialize DB on load
init_db()
