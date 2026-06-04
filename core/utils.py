import os
import json
import uuid
import threading
from datetime import datetime
from fastapi import Request, HTTPException
import db_helper

# Globals/Caches
PLAYLIST_REPORT_CACHE = {}
RULES_CACHE = {}
MAINTENANCE_CACHE = {}

# Locks for thread-safety
cache_lock = threading.Lock()

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")

def get_settings():
    with cache_lock:
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r") as f:
                    return json.load(f)
            except:
                pass
        return {}

def is_oauth_configured():
    settings = get_settings()
    return bool(settings.get("google_client_id") and settings.get("google_client_secret"))

def get_user_file_path(filename: str, user) -> str:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    if not is_oauth_configured():
        return os.path.join(base_dir, filename)
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    base, ext = os.path.splitext(filename)
    return os.path.join(base_dir, f"{base}_{user_id}{ext}")

def extract_video_id(url: str) -> str:
    if "v=" in url:
        return url.split("v=")[1].split("&")[0]
    return url.split("/")[-1]

def get_redirect_uri(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/api/auth/callback"

async def get_current_user(request: Request):
    if not is_oauth_configured():
        return {"user_id": 1, "email": "local@user.com", "name": "Local Admin"}
        
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user = db_helper.get_session_user(session_id)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user

def load_cached_playlist_report(report_path: str):
    global PLAYLIST_REPORT_CACHE
    with cache_lock:
        if not os.path.exists(report_path):
            return None
        try:
            mtime = os.path.getmtime(report_path)
            cached = PLAYLIST_REPORT_CACHE.get(report_path)
            if cached and cached.get("mtime") == mtime:
                return cached["data"]
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            PLAYLIST_REPORT_CACHE[report_path] = {
                "mtime": mtime,
                "data": data
            }
            return data
        except Exception as e:
            print(f"Error loading cached playlist report: {e}")
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return None

def load_cached_rules(user_id=None):
    global RULES_CACHE
    base_dir = os.path.dirname(os.path.dirname(__file__))
    rules_path = os.path.join(base_dir, "yt_rules.promptinclude.md")
    
    with cache_lock:
        rules_md = ""
        if os.path.exists(rules_path):
            mtime = os.path.getmtime(rules_path)
            cached_md = RULES_CACHE.get("rules_md")
            if cached_md and cached_md.get("mtime") == mtime:
                rules_md = cached_md["data"]
            else:
                try:
                    with open(rules_path, "r", encoding="utf-8") as f:
                        rules_md = f.read()
                    RULES_CACHE["rules_md"] = {"mtime": mtime, "data": rules_md}
                except:
                    pass
                    
        channels_txt = ""
        if not is_oauth_configured():
            chan_path = os.path.join(base_dir, "yt_category_channel_map.txt")
            if os.path.exists(chan_path):
                mtime = os.path.getmtime(chan_path)
                cached_chan = RULES_CACHE.get("channels_txt_local")
                if cached_chan and cached_chan.get("mtime") == mtime:
                    channels_txt = cached_chan["data"]
                else:
                    try:
                        with open(chan_path, "r", encoding="utf-8") as f:
                            channels_txt = f.read()
                        RULES_CACHE["channels_txt_local"] = {"mtime": mtime, "data": channels_txt}
                    except:
                        pass
        else:
            cache_key = f"user_rules_{user_id}"
            cached_user = RULES_CACHE.get(cache_key)
            if cached_user is not None:
                channels_txt = cached_user
            else:
                user_rules = db_helper.load_user_rules(user_id)
                channels_txt = "\n".join(f"{ch} : {cat}" for ch, cat in user_rules.items()) + "\n"
                RULES_CACHE[cache_key] = channels_txt
                
        return {"rules_md": rules_md, "channels_txt": channels_txt}

def invalidate_rules_cache(user_id=None):
    global RULES_CACHE
    with cache_lock:
        if "rules_md" in RULES_CACHE:
            del RULES_CACHE["rules_md"]
        if not is_oauth_configured():
            if "channels_txt_local" in RULES_CACHE:
                del RULES_CACHE["channels_txt_local"]
        else:
            cache_key = f"user_rules_{user_id}"
            if cache_key in RULES_CACHE:
                del RULES_CACHE[cache_key]

def find_title_in_cache(url: str, playlist_name: str, user_id=None) -> str:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    if user_id is None:
        report_path = os.path.join(base_dir, "playlists_report.json")
    else:
        report_path = os.path.join(base_dir, f"playlists_report_{user_id}.json")
        
    with cache_lock:
        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                for p in report:
                    if p["name"].lower() == playlist_name.lower():
                        for v in p.get("videos", []):
                            if url in v["url"] or v["url"] in url:
                                return v["title"]
            except: pass
    return "Unknown Video"

def find_channel_in_cache(url: str, playlist_name: str = None, user_id=None) -> str:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    if user_id is None:
        report_path = os.path.join(base_dir, "playlists_report.json")
    else:
        report_path = os.path.join(base_dir, f"playlists_report_{user_id}.json")
        
    with cache_lock:
        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                if playlist_name:
                    for p in report:
                        if p["name"].lower() == playlist_name.lower():
                            for v in p.get("videos", []):
                                if url in v["url"] or v["url"] in url:
                                    return v.get("channel", "")
                for p in report:
                    for v in p.get("videos", []):
                        if url in v["url"] or v["url"] in url:
                            return v.get("channel", "")
            except: pass
    return ""

def get_playlist_id_by_name(playlist_name: str, user_id) -> str:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    report_path = os.path.join(base_dir, f"playlists_report_{user_id}.json")
    with cache_lock:
        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                for p in report:
                    if p["name"].lower() == playlist_name.lower():
                        return p["id"]
            except: pass
    return None

def get_playlist_item_id_from_cache(video_url: str, playlist_name: str, user_id) -> str:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    report_path = os.path.join(base_dir, f"playlists_report_{user_id}.json")
    vid = extract_video_id(video_url)
    with cache_lock:
        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                for p in report:
                    if p["name"].lower() == playlist_name.lower():
                        for v in p.get("videos", []):
                            if extract_video_id(v.get("url", "")) == vid:
                                return v.get("playlist_item_id")
            except: pass
    return None

def update_cache_for_move(video_url: str, source_playlist: str, target_playlist: str, user_id=None):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    if user_id is None:
        report_path = os.path.join(base_dir, "playlists_report.json")
    else:
        report_path = os.path.join(base_dir, f"playlists_report_{user_id}.json")
        
    with cache_lock:
        if not os.path.exists(report_path):
            return
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            
            video_obj = None
            for p in report:
                if p["name"].lower() == source_playlist.lower():
                    matched_videos = [v for v in p.get("videos", []) if v["url"] == video_url or video_url in v["url"]]
                    if matched_videos:
                        video_obj = matched_videos[0]
                    p["videos"] = [v for v in p.get("videos", []) if v["url"] != video_url and video_url not in v["url"]]
                    p["video_count"] = len(p["videos"])
            
            if video_obj:
                for p in report:
                    if p["name"].lower() == target_playlist.lower():
                        if "videos" not in p:
                            p["videos"] = []
                        if not any(v["url"] == video_url for v in p["videos"]):
                            p["videos"].append(video_obj)
                            p["video_count"] = len(p["videos"])
            
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error updating cache for move: {e}")

def update_cache_for_delete(video_url: str, playlist: str, user_id=None):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    if user_id is None:
        report_path = os.path.join(base_dir, "playlists_report.json")
    else:
        report_path = os.path.join(base_dir, f"playlists_report_{user_id}.json")
        
    with cache_lock:
        if not os.path.exists(report_path):
            return
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            
            for p in report:
                if p["name"].lower() == playlist.lower():
                    p["videos"] = [v for v in p.get("videos", []) if v["url"] != video_url and video_url not in v["url"]]
                    p["video_count"] = len(p["videos"])
            
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error updating cache for delete: {e}")

def append_agent_log(message: str):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    log_path = os.path.join(base_dir, "agent_run.log")
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with cache_lock:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[Batch Operation] {timestamp} - {message}\n")
