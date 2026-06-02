import os
import sys
import json
import time
import threading
import subprocess
from typing import Optional, List
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks, Cookie, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, HTMLResponse
from pydantic import BaseModel
import requests
import uuid
import db_helper

import scheduler
from core import add_video_to_playlist, remove_video_from_playlist, list_videos_in_playlist, move_video, get_browser

app = FastAPI(title="YouTube Playlist Agent API")

# Settings loader helper
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")
def get_settings():
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

def get_redirect_uri(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/api/auth/callback"

# Dependency to get current user
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

# Auth Endpoints
@app.get("/api/auth/config-check")
def auth_config_check():
    return {"configured": is_oauth_configured()}

@app.get("/api/auth/login")
def auth_login(request: Request):
    settings = get_settings()
    client_id = settings.get("google_client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail="Google Client ID is not configured in settings.json")
        
    redirect_uri = get_redirect_uri(request)
    scopes = "openid email profile https://www.googleapis.com/auth/youtube"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scopes}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return RedirectResponse(auth_url)

@app.get("/api/auth/callback")
def auth_callback(request: Request, code: str):
    settings = get_settings()
    client_id = settings.get("google_client_id")
    client_secret = settings.get("google_client_secret")
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="OAuth credentials not fully configured")
        
    redirect_uri = get_redirect_uri(request)
    
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }
    
    resp = requests.post(token_url, data=payload)
    if not resp.ok:
        return HTMLResponse(content=f"<h1>Authentication Failed</h1><p>{resp.text}</p>", status_code=400)
        
    tokens = resp.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    
    userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    userinfo_resp = requests.get(userinfo_url, headers=headers)
    if not userinfo_resp.ok:
        return HTMLResponse(content="<h1>Failed to fetch user info from Google</h1>", status_code=400)
        
    user_info = userinfo_resp.json()
    email = user_info.get("email")
    name = user_info.get("name", email)
    
    if not email:
        return HTMLResponse(content="<h1>Google account did not return an email address</h1>", status_code=400)
        
    user_id = db_helper.get_or_create_user(email, name)
    db_helper.save_user_credentials(user_id, access_token, refresh_token, expires_in)
    
    db_helper.import_default_rules_if_empty(user_id)
    
    session_id = str(uuid.uuid4())
    db_helper.create_session(session_id, user_id)
    
    response = RedirectResponse(url="/")
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=86400 * 30,
        samesite="lax",
        secure=False
    )
    return response

@app.post("/api/auth/logout")
def auth_logout(response: Response, session_id: Optional[str] = Cookie(None)):
    if session_id:
        db_helper.delete_session(session_id)
    response = JSONResponse(content={"success": True})
    response.delete_cookie("session_id")
    return response

@app.get("/api/auth/session")
def auth_session(user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    return {
        "logged_in": True if is_oauth_configured() else False,
        "local_mode": not is_oauth_configured(),
        "user_id": user_id,
        "email": user.get("email"),
        "name": user.get("name")
    }

def get_user_file_path(filename: str, user) -> str:
    if not is_oauth_configured():
        return os.path.join(os.path.dirname(__file__), filename)
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    base, ext = os.path.splitext(filename)
    return os.path.join(os.path.dirname(__file__), f"{base}_{user_id}{ext}")

# Shared Task Manager
class TaskManager:
    def __init__(self):
        self.process = None
        self.active_job = None
        self.queue = []  # list of dicts: {"job_name": str, "type": "subprocess"|"function", "args": list, "func": callable, "func_args": tuple}
        self.lock = threading.RLock()
        self.thread = None
        
        # Start background processor thread
        threading.Thread(target=self._process_queue_loop, daemon=True).start()
        
    def is_running(self):
        with self.lock:
            if scheduler.active_job:
                return True
            if self.process and self.process.poll() is None:
                return True
            if self.thread and self.thread.is_alive():
                return True
            return False
            
    def get_active_job(self):
        with self.lock:
            if scheduler.active_job:
                return scheduler.active_job
            return self.active_job
            
    def _process_queue_loop(self):
        while True:
            task = None
            with self.lock:
                if not self.is_running() and self.queue:
                    task = self.queue.pop(0)
            
            if task:
                if task["type"] == "subprocess":
                    self._execute_subprocess_task(task["job_name"], task["args"])
                elif task["type"] == "function":
                    self._execute_function_task(task["job_name"], task["func"], task["func_args"])
            
            time.sleep(1)
            
    def run_task(self, job_name: str, args: list):
        with self.lock:
            # Check for duplicate in queue or active running
            if self.active_job == job_name or any(t["job_name"] == job_name for t in self.queue):
                return False, f"Task '{job_name}' is already running or queued."
                
            self.queue.append({
                "job_name": job_name,
                "type": "subprocess",
                "args": args
            })
            return True, f"Task '{job_name}' successfully added to queue."
            
    def run_function(self, job_name: str, func, func_args):
        with self.lock:
            # Check for duplicate in queue or active running
            if self.active_job == job_name or any(t["job_name"] == job_name for t in self.queue):
                return False, f"Task '{job_name}' is already running or queued."
                
            self.queue.append({
                "job_name": job_name,
                "type": "function",
                "func": func,
                "func_args": func_args
            })
            return True, f"Task '{job_name}' successfully added to queue."
            
    def _execute_subprocess_task(self, job_name: str, args: list):
        with self.lock:
            with scheduler.job_lock:
                scheduler.active_job = job_name
            self.active_job = job_name
            
            log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- [START] Job '{job_name}' started at {timestamp} ---\n")
                
            log_file = open(log_path, "a", encoding="utf-8")
            
            cmd = [sys.executable] + args
            self.process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                cwd=os.path.dirname(__file__),
                text=True
            )
            
            # Start monitor thread
            threading.Thread(target=self._monitor_process, args=(self.process, job_name, log_file), daemon=True).start()
            
    def _execute_function_task(self, job_name: str, func, func_args):
        with self.lock:
            with scheduler.job_lock:
                scheduler.active_job = job_name
            self.active_job = job_name
            
            def runner():
                try:
                    func(*func_args)
                except Exception as e:
                    print(f"TaskManager: Function task '{job_name}' failed: {e}")
                finally:
                    with self.lock:
                        if self.active_job == job_name:
                            self.active_job = None
                        with scheduler.job_lock:
                            if scheduler.active_job == job_name:
                                scheduler.active_job = None
                                
            self.thread = threading.Thread(target=runner, daemon=True)
            self.thread.start()
            
    def _monitor_process(self, proc, job_name, log_file):
        proc.wait()
        log_file.close()
        
        log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- [END] Job '{job_name}' completed with code {proc.returncode} at {timestamp} ---\n")
        
        # Save last run
        try:
            with open(os.path.join(os.path.dirname(__file__), "last_run.txt"), "w") as f:
                f.write(timestamp)
        except:
            pass
            
        with self.lock:
            if self.active_job == job_name:
                self.active_job = None
            with scheduler.job_lock:
                if scheduler.active_job == job_name:
                    scheduler.active_job = None
                
    def stop_task(self):
        with self.lock:
            stopped = False
            msg = ""
            if self.process and self.process.poll() is None:
                try:
                    import subprocess as sp
                    sp.run(f"taskkill /F /T /PID {self.process.pid}", shell=True, capture_output=True)
                    self.process.kill()
                    stopped = True
                    msg = "Task process killed. "
                except Exception as e:
                    return False, f"Failed to stop task: {e}"
            
            # Reset active job state
            with scheduler.job_lock:
                if scheduler.active_job or self.active_job:
                    scheduler.active_job = None
                    self.active_job = None
                    stopped = True
                    msg += "Active job state cleared."
            
            # Also clear the queued tasks
            if self.queue:
                q_count = len(self.queue)
                self.queue.clear()
                stopped = True
                msg += f"Cleared {q_count} queued tasks."
                
            if stopped:
                return True, msg or "Task stopped successfully"
            return False, "No active task running or queued"


task_manager = TaskManager()

# Data models
class PlaylistRequest(BaseModel):
    video_url: str
    playlist_name: str

class RulesSaveRequest(BaseModel):
    rules_md: str
    channels_txt: str

class SettingsRequest(BaseModel):
    gemini_api_key: str
    notification_webhook: str

class MaintenanceApplyRequest(BaseModel):
    force: bool

class SingleActionRequest(BaseModel):
    vid: str

class AddChannelRuleRequest(BaseModel):
    channel: str
    category: str

class BatchMoveRequest(BaseModel):
    video_urls: List[str]
    source_playlist: str
    target_playlist: str

class SingleMoveRequest(BaseModel):
    video_url: str
    source_playlist: str
    target_playlist: str

class BatchDeleteRequest(BaseModel):
    video_urls: List[str]
    playlist: str

class AIClassificationActionRequest(BaseModel):
    vid: str
    action: str  # "approve" or "correct"
    category: str

class BatchAIClassificationRequest(BaseModel):
    vids: List[str]
    action: str  # "approve" or "correct"
    category: str

class BatchMaintenanceRequest(BaseModel):
    vids: List[str]

class MultiSourceMoveItem(BaseModel):
    video_url: str
    source_playlist: str

class MultiSourceMoveRequest(BaseModel):
    items: List[MultiSourceMoveItem]
    target_playlist: str

class MultiSourceDeleteRequest(BaseModel):
    items: List[MultiSourceMoveItem]

class RemoveDuplicatesRequest(BaseModel):
    playlist_name: str

class BatchAIDeleteRequest(BaseModel):
    vids: List[str]

class UpdateMaintTargetRequest(BaseModel):
    vid: str
    target: str



# Start the background scheduler thread on startup
@app.on_event("startup")
def startup_event():
    scheduler.start_scheduler()
    print("FastAPI: Background scheduler successfully initialized.")

# Serve SPA
@app.get("/")
def read_root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))

@app.get("/api/status")
def get_status(user=Depends(get_current_user)):
    total_playlists = 0
    total_videos = 0
    pending_actions = 0
    ai_cache_hits = 0
    
    # 1. Total Playlists & Videos
    report_path = get_user_file_path("playlists_report.json", user)
    if os.path.exists(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
                total_playlists = len(report)
                total_videos = sum(p.get("video_count", 0) for p in report)
        except: pass
        
    # 2. Pending Actions
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if os.path.exists(maint_path):
        try:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)
                pending_actions = len(actions)
        except: pass
        
    # 3. AI Cache Hits
    ai_pending = 0
    ai_reviewed = 0
    ai_total = 0
    class_path = get_user_file_path("ai_classifications.json", user)
    if os.path.exists(class_path):
        try:
            with open(class_path, "r", encoding="utf-8") as f:
                classifications = json.load(f)
                
            # Build current playlist map
            vid_to_playlist = {}
            if os.path.exists(report_path):
                try:
                    with open(report_path, "r", encoding="utf-8") as rf:
                        report = json.load(rf)
                    for p in report:
                        playlist_name = p.get("name")
                        for v in p.get("videos", []):
                            vid = extract_video_id(v.get("url", ""))
                            if vid:
                                vid_to_playlist[vid] = playlist_name
                except:
                    pass

            # Filter out redundant pending classifications
            filtered_classifications = []
            for c in classifications:
                status = c.get("status")
                if status == 'pending':
                    curr_pl = vid_to_playlist.get(c.get("vid"))
                    cat = c.get("category")
                    if curr_pl and cat and curr_pl.lower() == cat.lower():
                        continue  # Skip redundant suggestion
                filtered_classifications.append(c)

            ai_total = len(filtered_classifications)
            ai_pending = sum(1 for c in filtered_classifications if c.get("status") == "pending")
            ai_reviewed = ai_total - ai_pending
            ai_cache_hits = ai_total
        except: pass
    if ai_total == 0:
        cache_path = get_user_file_path("ai_cache_hits.txt", user)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    ai_cache_hits = int(f.read().strip() or "0")
                    ai_total = ai_cache_hits
            except: pass
        
    # 4. Last run
    last_run = "--:--"
    last_run_path = get_user_file_path("last_run.txt", user)
    if os.path.exists(last_run_path):
        try:
            with open(last_run_path, "r") as f:
                last_run = f.read().strip()
        except: pass
        
    engine_status = "running" if task_manager.is_running() else "idle"
    
    active_job = task_manager.get_active_job()
    if active_job == "generate_maintenance":
        progress_path = get_user_file_path("generate_progress.json", user)
        if os.path.exists(progress_path):
            try:
                with open(progress_path, "r") as f:
                    progress = json.load(f)
                    active_job = f"generate_maintenance ({progress['current']}/{progress['total']})"
            except:
                pass
                
    return {
        "total_playlists": total_playlists,
        "total_videos": total_videos,
        "pending_actions": pending_actions,
        "ai_cache_hits": ai_cache_hits,
        "ai_pending": ai_pending,
        "ai_reviewed": ai_reviewed,
        "ai_total": ai_total,
        "engine_status": engine_status,
        "active_job": active_job,
        "queued_jobs": [t["job_name"] for t in task_manager.queue],
        "last_run": last_run
    }

@app.get("/api/playlists")
def get_playlists(user=Depends(get_current_user)):
    report_path = get_user_file_path("playlists_report.json", user)
    if os.path.exists(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                playlists = json.load(f)
                if isinstance(playlists, list):
                    def sort_key(p):
                        name = p.get("name", "")
                        if name.lower() == "watch later":
                            return (0, "")
                        return (1, name.lower())
                    playlists.sort(key=sort_key)
                return playlists
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error reading playlists report: {e}")
    return []

# Helper functions for batch playlist operations
def extract_video_id(url: str) -> str:
    if "v=" in url:
        return url.split("v=")[1].split("&")[0]
    return url.split("/")[-1]

def find_title_in_cache(url: str, playlist_name: str, user_id=None) -> str:
    if user_id is None:
        report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
    else:
        report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
        
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
    if user_id is None:
        report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
    else:
        report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
        
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
    report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
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
    report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
    vid = extract_video_id(video_url)
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
    if user_id is None:
        report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
    else:
        report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
        
    if not os.path.exists(report_path):
        return
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        
        video_obj = None
        # Find video in source playlist and remove it
        for p in report:
            if p["name"].lower() == source_playlist.lower():
                matched_videos = [v for v in p.get("videos", []) if v["url"] == video_url or video_url in v["url"]]
                if matched_videos:
                    video_obj = matched_videos[0]
                p["videos"] = [v for v in p.get("videos", []) if v["url"] != video_url and video_url not in v["url"]]
                p["video_count"] = len(p["videos"])
        
        # Add to target playlist
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
    if user_id is None:
        report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
    else:
        report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
        
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
    log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n[Batch Operation] {timestamp} - {message}\n")

# Background thread execution functions
def execute_batch_move_background(video_urls: List[str], source_playlist: str, target_playlist: str, user_id=None):
    total = len(video_urls)
    append_agent_log(f"Starting batch move of {total} videos from '{source_playlist}' to '{target_playlist}' (user_id={user_id}).")
    
    if is_oauth_configured() and user_id is not None:
        import youtube_api
        success_count = 0
        try:
            target_playlist_id = get_playlist_id_by_name(target_playlist, user_id)
            if not target_playlist_id:
                raise ValueError(f"Target playlist '{target_playlist}' not found in user cache")
                
            for i, url in enumerate(video_urls):
                current_job_name = f"Batch Move ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                vid = extract_video_id(url)
                append_agent_log(f"[{i+1}/{total}] Moving: {url}...")
                
                success = False
                try:
                    source_playlist_item_id = get_playlist_item_id_from_cache(url, source_playlist, user_id)
                    if not source_playlist_item_id:
                        raise ValueError(f"Source item ID for '{url}' not found")
                    success = youtube_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
                except Exception as e:
                    append_agent_log(f"Error moving {url}: {e}")
                    
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully moved {url}.")
                    
                    title = find_title_in_cache(url, source_playlist, user_id)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "MISPLACED",
                        "from": [source_playlist],
                        "to": target_playlist
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action, user_id)
                    
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except: pass
                    
                    update_cache_for_move(url, source_playlist, target_playlist, user_id)
                else:
                    append_agent_log(f"Failed to move: {url}")
                    
            append_agent_log(f"Batch move completed. Successfully moved {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Batch move completed. Moved {success_count}/{total} videos from '{source_playlist}' to '{target_playlist}'.")
        except Exception as e:
            append_agent_log(f"Fatal error in API batch move: {e}")
            scheduler.send_webhook_notification(f"Batch move failed: {e}", is_error=True)
        finally:
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None
    else:
        driver = None
        try:
            driver = get_browser()
            success_count = 0
            
            for i, url in enumerate(video_urls):
                current_job_name = f"Batch Move ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                append_agent_log(f"[{i+1}/{total}] Moving: {url}...")
                
                success = False
                try:
                    success = move_video(url, source_playlist, target_playlist, driver=driver)
                except Exception as e:
                    append_agent_log(f"Error moving {url}: {e}")
                
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully moved {url}.")
                    
                    vid = extract_video_id(url)
                    title = find_title_in_cache(url, source_playlist)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "MISPLACED",
                        "from": [source_playlist],
                        "to": target_playlist
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action)
                    append_agent_log(f"History recorded. Action ID: {action_id}")
                    
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except Exception as ex:
                        append_agent_log(f"Discord report fail: {ex}")
                    
                    update_cache_for_move(url, source_playlist, target_playlist)
                else:
                    append_agent_log(f"Failed to move: {url}")
                    
                time.sleep(1)
                
            append_agent_log(f"Batch move completed. Successfully moved {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Batch move completed. Moved {success_count}/{total} videos from '{source_playlist}' to '{target_playlist}'.")
            
        except Exception as e:
            append_agent_log(f"Fatal error in batch move: {e}")
            scheduler.send_webhook_notification(f"Batch move failed: {e}", is_error=True)
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None

def execute_batch_delete_background(video_urls: List[str], playlist: str, user_id=None):
    total = len(video_urls)
    append_agent_log(f"Starting batch delete of {total} videos from '{playlist}' (user_id={user_id}).")
    
    if is_oauth_configured() and user_id is not None:
        import youtube_api
        success_count = 0
        try:
            for i, url in enumerate(video_urls):
                current_job_name = f"Batch Delete ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                vid = extract_video_id(url)
                append_agent_log(f"[{i+1}/{total}] Deleting: {url}...")
                
                success = False
                try:
                    playlist_item_id = get_playlist_item_id_from_cache(url, playlist, user_id)
                    if not playlist_item_id:
                        raise ValueError(f"playlistItem ID for '{url}' not found in '{playlist}'")
                    success = youtube_api.remove_video_from_playlist(user_id, playlist_item_id)
                except Exception as e:
                    append_agent_log(f"Error deleting {url}: {e}")
                    
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully deleted {url}.")
                    
                    title = find_title_in_cache(url, playlist, user_id)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "DUPLICATE_NO_TARGET",
                        "remove": [playlist]
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action, user_id)
                    
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except: pass
                    
                    update_cache_for_delete(url, playlist, user_id)
                else:
                    append_agent_log(f"Failed to delete: {url}")
                    
            append_agent_log(f"Batch delete completed. Successfully deleted {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Batch delete completed. Deleted {success_count}/{total} videos from '{playlist}'.")
        except Exception as e:
            append_agent_log(f"Fatal error in API batch delete: {e}")
            scheduler.send_webhook_notification(f"Batch delete failed: {e}", is_error=True)
        finally:
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None
    else:
        driver = None
        try:
            driver = get_browser()
            success_count = 0
            
            for i, url in enumerate(video_urls):
                current_job_name = f"Batch Delete ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                append_agent_log(f"[{i+1}/{total}] Deleting: {url}...")
                
                success = False
                try:
                    success = remove_video_from_playlist(url, playlist, driver=driver)
                except Exception as e:
                    append_agent_log(f"Error deleting {url}: {e}")
                
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully deleted {url}.")
                    
                    vid = extract_video_id(url)
                    title = find_title_in_cache(url, playlist)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "DUPLICATE_NO_TARGET",
                        "remove": [playlist]
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action)
                    append_agent_log(f"History recorded. Action ID: {action_id}")
                    
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except Exception as ex:
                        append_agent_log(f"Discord report fail: {ex}")
                    
                    update_cache_for_delete(url, playlist)
                else:
                    append_agent_log(f"Failed to delete: {url}")
                    
                time.sleep(1)
                
            append_agent_log(f"Batch delete completed. Successfully deleted {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Batch delete completed. Deleted {success_count}/{total} videos from '{playlist}'.")
            
        except Exception as e:
            append_agent_log(f"Fatal error in batch delete: {e}")
            scheduler.send_webhook_notification(f"Batch delete failed: {e}", is_error=True)
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None

def execute_multi_source_move_background(items: List[dict], target_playlist: str, user_id=None):
    total = len(items)
    append_agent_log(f"Starting multi-source batch move of {total} videos to '{target_playlist}' (user_id={user_id}).")
    
    if is_oauth_configured() and user_id is not None:
        import youtube_api
        success_count = 0
        try:
            target_playlist_id = get_playlist_id_by_name(target_playlist, user_id)
            if not target_playlist_id:
                raise ValueError(f"Target playlist '{target_playlist}' not found in user cache")
                
            for i, item in enumerate(items):
                url = item["video_url"]
                source_playlist = item["source_playlist"]
                
                current_job_name = f"Multi-Source Move ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                vid = extract_video_id(url)
                append_agent_log(f"[{i+1}/{total}] Moving from '{source_playlist}' to '{target_playlist}': {url}...")
                
                success = False
                try:
                    source_playlist_item_id = get_playlist_item_id_from_cache(url, source_playlist, user_id)
                    if not source_playlist_item_id:
                        raise ValueError(f"Source item ID for '{url}' not found")
                    success = youtube_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
                except Exception as e:
                    append_agent_log(f"Error moving {url}: {e}")
                    
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully moved {url}.")
                    
                    title = find_title_in_cache(url, source_playlist, user_id)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "MISPLACED",
                        "from": [source_playlist],
                        "to": target_playlist
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action, user_id)
                    
                    # Auto-learn rule for manual batch move
                    try:
                        channel = find_channel_in_cache(url, source_playlist, user_id)
                        if channel and target_playlist.lower() != "watch later":
                            db_helper.save_user_rule(user_id, channel, target_playlist)
                            append_agent_log(f"Auto-learned rule for batch move: {channel} -> {target_playlist}")
                    except Exception as ex:
                        append_agent_log(f"Failed to auto-learn rule for batch move: {ex}")
                        
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except: pass
                    
                    update_cache_for_move(url, source_playlist, target_playlist, user_id)
                else:
                    append_agent_log(f"Failed to move: {url}")
                    
            append_agent_log(f"Multi-source batch move completed. Successfully moved {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Multi-source batch move completed. Moved {success_count}/{total} videos to '{target_playlist}'.")
        except Exception as e:
            append_agent_log(f"Fatal error in API multi-source batch move: {e}")
            scheduler.send_webhook_notification(f"Multi-source batch move failed: {e}", is_error=True)
        finally:
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None
    else:
        driver = None
        try:
            driver = get_browser()
            success_count = 0
            
            for i, item in enumerate(items):
                url = item["video_url"]
                source_playlist = item["source_playlist"]
                
                current_job_name = f"Multi-Source Move ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                append_agent_log(f"[{i+1}/{total}] Moving from '{source_playlist}' to '{target_playlist}': {url}...")
                
                success = False
                try:
                    success = move_video(url, source_playlist, target_playlist, driver=driver)
                except Exception as e:
                    append_agent_log(f"Error moving {url}: {e}")
                
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully moved {url}.")
                    
                    # Record history
                    vid = extract_video_id(url)
                    title = find_title_in_cache(url, source_playlist)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "MISPLACED",
                        "from": [source_playlist],
                        "to": target_playlist
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action)
                    
                    # Auto-learn rule for manual batch move
                    try:
                        channel = find_channel_in_cache(url, source_playlist)
                        if channel and target_playlist.lower() != "watch later":
                            from apply_maintenance import learn_channel_rule
                            learn_channel_rule(channel, target_playlist)
                            append_agent_log(f"Auto-learned rule for batch move: {channel} -> {target_playlist}")
                    except Exception as ex:
                        append_agent_log(f"Failed to auto-learn rule for batch move: {ex}")
                    
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except Exception as ex:
                        append_agent_log(f"Discord report fail: {ex}")
                    
                    # Update cache
                    update_cache_for_move(url, source_playlist, target_playlist)
                else:
                    append_agent_log(f"Failed to move: {url}")
                    
                time.sleep(1)
                
            append_agent_log(f"Multi-source batch move completed. Successfully moved {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Multi-source batch move completed. Moved {success_count}/{total} videos to '{target_playlist}'.")
            
        except Exception as e:
            append_agent_log(f"Fatal error in multi-source batch move: {e}")
            scheduler.send_webhook_notification(f"Multi-source batch move failed: {e}", is_error=True)
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None

def execute_multi_source_delete_background(items: List[dict], user_id=None):
    total = len(items)
    append_agent_log(f"Starting multi-source batch delete of {total} videos (user_id={user_id}).")
    
    if is_oauth_configured() and user_id is not None:
        import youtube_api
        success_count = 0
        try:
            for i, item in enumerate(items):
                url = item["video_url"]
                playlist = item["source_playlist"]
                
                current_job_name = f"Multi-Source Delete ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                vid = extract_video_id(url)
                append_agent_log(f"[{i+1}/{total}] Deleting from '{playlist}': {url}...")
                
                success = False
                try:
                    playlist_item_id = get_playlist_item_id_from_cache(url, playlist, user_id)
                    if not playlist_item_id:
                        raise ValueError(f"playlistItem ID for '{url}' not found in '{playlist}'")
                    success = youtube_api.remove_video_from_playlist(user_id, playlist_item_id)
                except Exception as e:
                    append_agent_log(f"Error deleting {url}: {e}")
                    
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully deleted {url}.")
                    
                    title = find_title_in_cache(url, playlist, user_id)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "DUPLICATE_NO_TARGET",
                        "remove": [playlist]
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action, user_id)
                    
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except: pass
                    
                    update_cache_for_delete(url, playlist, user_id)
                else:
                    append_agent_log(f"Failed to delete: {url}")
                    
            append_agent_log(f"Multi-source batch delete completed. Successfully deleted {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Multi-source batch delete completed. Deleted {success_count}/{total} videos.")
        except Exception as e:
            append_agent_log(f"Fatal error in API multi-source batch delete: {e}")
            scheduler.send_webhook_notification(f"Multi-source batch delete failed: {e}", is_error=True)
        finally:
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None
    else:
        driver = None
        try:
            driver = get_browser()
            success_count = 0
            
            for i, item in enumerate(items):
                url = item["video_url"]
                playlist = item["source_playlist"]
                
                current_job_name = f"Multi-Source Delete ({i+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                
                append_agent_log(f"[{i+1}/{total}] Deleting from '{playlist}': {url}...")
                
                success = False
                try:
                    success = remove_video_from_playlist(url, playlist, driver=driver)
                except Exception as e:
                    append_agent_log(f"Error deleting {url}: {e}")
                
                if success:
                    success_count += 1
                    append_agent_log(f"Successfully deleted {url}.")
                    
                    # Record history
                    vid = extract_video_id(url)
                    title = find_title_in_cache(url, playlist)
                    action = {
                        "vid": vid,
                        "title": title,
                        "type": "DUPLICATE_NO_TARGET",
                        "remove": [playlist]
                    }
                    from apply_maintenance import record_history
                    action_id = record_history(action)
                    
                    try:
                        from apply_maintenance import send_discord_history_report
                        send_discord_history_report([{**action, "action_id": action_id}])
                    except Exception as ex:
                        append_agent_log(f"Discord report fail: {ex}")
                    
                    # Update cache
                    update_cache_for_delete(url, playlist)
                else:
                    append_agent_log(f"Failed to delete: {url}")
                    
                time.sleep(1)
                
            append_agent_log(f"Multi-source batch delete completed. Deleted {success_count} of {total} videos.")
            scheduler.send_webhook_notification(f"Multi-source batch delete completed. Deleted {success_count}/{total} videos.")
            
        except Exception as e:
            append_agent_log(f"Fatal error in multi-source batch delete: {e}")
            scheduler.send_webhook_notification(f"Multi-source batch delete failed: {e}", is_error=True)
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None

def execute_remove_duplicates_background(playlist_name: str, user_id=None):
    append_agent_log(f"Starting duplicate cleanup for playlist '{playlist_name}' (user_id={user_id}).")
    
    if is_oauth_configured() and user_id is not None:
        report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
    else:
        report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
        
    if not os.path.exists(report_path):
        append_agent_log(f"Error: report file {report_path} not found.")
        return
        
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
            
        target_playlist = next((p for p in report if p["name"].lower() == playlist_name.lower()), None)
        if not target_playlist or not target_playlist.get("videos"):
            append_agent_log(f"No videos cached for playlist '{playlist_name}'.")
            return
            
        videos = target_playlist["videos"]
        from collections import Counter
        urls = [v["url"] for v in videos]
        duplicates = [url for url, count in Counter(urls).items() if count > 1]
        
        if not duplicates:
            append_agent_log(f"No duplicate URLs found in '{playlist_name}'.")
            return
            
        total = len(duplicates)
        append_agent_log(f"Found {total} duplicate videos to resolve in '{playlist_name}'.")
        
        if is_oauth_configured() and user_id is not None:
            import youtube_api
            success_count = 0
            try:
                target_playlist_id = get_playlist_id_by_name(playlist_name, user_id)
                if not target_playlist_id:
                    raise ValueError(f"Playlist ID for '{playlist_name}' not found")
                    
                for i, url in enumerate(duplicates):
                    current_job_name = f"Clean Dupes ({i+1}/{total})"
                    with task_manager.lock:
                        scheduler.active_job = current_job_name
                        task_manager.active_job = current_job_name
                        
                    title = next((v["title"] for v in videos if v["url"] == url), "Unknown Video")
                    append_agent_log(f"[{i+1}/{total}] Resolving duplicate for: {title}")
                    
                    try:
                        matching_items = [v for v in videos if v["url"] == url]
                        if len(matching_items) > 1:
                            deleted_all = True
                            for item in matching_items:
                                item_id = item.get("playlist_item_id")
                                if item_id:
                                    try:
                                        youtube_api.remove_video_from_playlist(user_id, item_id)
                                    except Exception as ex:
                                        append_agent_log(f"Error removing item {item_id}: {ex}")
                                        deleted_all = False
                            
                            vid = extract_video_id(url)
                            if deleted_all:
                                youtube_api.add_video_to_playlist(user_id, target_playlist_id, vid)
                                success_count += 1
                                append_agent_log(f"Successfully resolved duplicate for '{title}' via API.")
                                
                                action = {
                                    "vid": vid,
                                    "title": title,
                                    "type": "DUPLICATE",
                                    "keep": playlist_name,
                                    "remove": [playlist_name]
                                }
                                from apply_maintenance import record_history
                                action_id = record_history(action, user_id)
                                try:
                                    from apply_maintenance import send_discord_history_report
                                    send_discord_history_report([{**action, "action_id": action_id}])
                                except: pass
                    except Exception as e:
                        append_agent_log(f"Error resolving duplicate for '{title}': {e}")
                
                append_agent_log(f"Duplicate cleanup completed. Successfully resolved {success_count} of {total} duplicates in '{playlist_name}'.")
                
                append_agent_log(f"Refreshing live video list for '{playlist_name}'...")
                refreshed_videos = youtube_api.list_videos_in_playlist(user_id, target_playlist_id)
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                for p in report:
                    if p["name"].lower() == playlist_name.lower():
                        p["videos"] = refreshed_videos
                        p["video_count"] = len(refreshed_videos)
                        break
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, ensure_ascii=False)
                    
                scheduler.send_webhook_notification(f"Duplicate cleanup completed for '{playlist_name}'. Resolved {success_count}/{total} duplicates.")
            except Exception as e:
                append_agent_log(f"Fatal error in API duplicate cleanup: {e}")
                scheduler.send_webhook_notification(f"Duplicate cleanup failed: {e}", is_error=True)
            finally:
                with task_manager.lock:
                    task_manager.active_job = None
                    with scheduler.job_lock:
                        scheduler.active_job = None
        else:
            driver = None
            try:
                driver = get_browser()
                success_count = 0
                
                for i, url in enumerate(duplicates):
                    current_job_name = f"Clean Dupes ({i+1}/{total})"
                    with task_manager.lock:
                        scheduler.active_job = current_job_name
                        task_manager.active_job = current_job_name
                        
                    title = next((v["title"] for v in videos if v["url"] == url), "Unknown Video")
                    append_agent_log(f"[{i+1}/{total}] Resolving duplicate for: {title}")
                    
                    try:
                        if remove_video_from_playlist(url, playlist_name, driver=driver):
                            time.sleep(2)
                            if add_video_to_playlist(url, playlist_name, driver=driver):
                                success_count += 1
                                append_agent_log(f"Successfully resolved duplicate for '{title}'.")
                                
                                vid = extract_video_id(url)
                                action = {
                                    "vid": vid,
                                    "title": title,
                                    "type": "DUPLICATE",
                                    "keep": playlist_name,
                                    "remove": [playlist_name]
                                }
                                from apply_maintenance import record_history
                                action_id = record_history(action)
                                
                                try:
                                    from apply_maintenance import send_discord_history_report
                                    send_discord_history_report([{**action, "action_id": action_id}])
                                except Exception as ex:
                                    append_agent_log(f"Discord report fail: {ex}")
                            else:
                                append_agent_log(f"Failed to re-add '{title}' to '{playlist_name}'.")
                        else:
                            append_agent_log(f"Failed to remove '{title}' from '{playlist_name}'.")
                    except Exception as e:
                        append_agent_log(f"Error resolving duplicate for '{title}': {e}")
                        
                    time.sleep(1)
                    
                append_agent_log(f"Duplicate cleanup completed. Successfully resolved {success_count} of {total} duplicates in '{playlist_name}'.")
                
                refreshed_videos = list_videos_in_playlist(target_playlist["url"], driver=driver)
                
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                for p in report:
                    if p["name"].lower() == playlist_name.lower():
                        p["videos"] = refreshed_videos
                        p["video_count"] = len(refreshed_videos)
                        break
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, ensure_ascii=False)
                    
                scheduler.send_webhook_notification(f"Duplicate cleanup completed for '{playlist_name}'. Resolved {success_count}/{total} duplicates.")
            except Exception as e:
                append_agent_log(f"Fatal error in duplicate cleanup: {e}")
                scheduler.send_webhook_notification(f"Duplicate cleanup failed: {e}", is_error=True)
            finally:
                if driver:
                    try: driver.quit()
                    except: pass
                    
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None
    except Exception as e:
        append_agent_log(f"Error in duplicate cleanup script: {e}")
    finally:
        with task_manager.lock:
            task_manager.active_job = None
            with scheduler.job_lock:
                scheduler.active_job = None

@app.get("/api/playlists/videos")
def get_playlist_videos(playlist_url: str, refresh: bool = False, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    report_path = get_user_file_path("playlists_report.json", user)
    
    playlist_id = None
    if "list=" in playlist_url:
        playlist_id = playlist_url.split("list=")[1].split("&")[0]
    else:
        playlist_id = playlist_url

    if is_oauth_configured() and playlist_id:
        import youtube_api
        try:
            videos = youtube_api.list_videos_in_playlist(user_id, playlist_id)
            return {"videos": videos}
        except Exception as e:
            print(f"YouTube API failed to fetch videos, falling back to cache: {e}")

    need_refresh = refresh
    cached_videos = []
    playlist_name = ""
    
    if os.path.exists(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            for p in report:
                if p["url"] == playlist_url or playlist_url in p["url"] or p.get("id") == playlist_id:
                    cached_videos = p.get("videos", [])
                    playlist_name = p["name"]
                    break
        except: pass
        
    if not need_refresh and cached_videos:
        if any("published" not in v for v in cached_videos):
            need_refresh = True
            
    if not cached_videos and not playlist_name:
        need_refresh = True
        
    if need_refresh:
        with task_manager.lock:
            if scheduler.active_job or (task_manager.process and task_manager.process.poll() is None):
                if cached_videos:
                    append_agent_log("Browser engine busy; falling back to cached videos.")
                    return {"videos": cached_videos, "warning": "Browser engine busy. Showing cached videos."}
                raise HTTPException(status_code=400, detail="The browser engine is currently busy with another task.")
            scheduler.active_job = "Live Fetch Videos"
            task_manager.active_job = "Live Fetch Videos"
            
        driver = None
        try:
            append_agent_log(f"Starting live video fetch via subprocess for playlist URL: {playlist_url}")
            cmd = [sys.executable, "cli.py", "list", playlist_url, "--json"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=os.path.dirname(__file__),
                encoding="utf-8"
            )
            
            if result.returncode != 0:
                raise Exception(f"Subprocess returned exit code {result.returncode}. Stderr: {result.stderr.strip()}")
                
            try:
                stdout_str = result.stdout.strip()
                json_start = stdout_str.find("[")
                json_end = stdout_str.rfind("]")
                if json_start == -1 or json_end == -1 or json_end <= json_start:
                    raise ValueError(f"No JSON array found in output. Stderr: {result.stderr.strip()}. Stdout: {stdout_str}")
                json_data = stdout_str[json_start:json_end+1]
                videos = json.loads(json_data)
            except Exception as json_err:
                raise Exception(f"Failed to parse JSON output from subprocess: {json_err}. Stderr: {result.stderr.strip()}. Stdout: {result.stdout.strip()}")
            
            # Find and update name
            scanned_name = playlist_name
            if not scanned_name:
                # Fallback list of playlists
                try:
                    all_p = get_all_playlists()
                    for item in all_p:
                        if item["url"] == playlist_url or playlist_url in item["url"]:
                            scanned_name = item["name"]
                            break
                except: pass
            if not scanned_name:
                scanned_name = "Scanned Playlist"
            
            # Prevent data erasure if browser returns empty videos list but we have cached videos
            if not videos and cached_videos:
                append_agent_log(f"Warning: Scraper returned 0 videos for '{scanned_name}', but cache has {len(cached_videos)} videos. Retaining cache to prevent data loss.")
                videos = cached_videos
            
            # Never persist mock data to disk (guard against MOCK_YOUTUBE being accidentally set)
            is_mock = any('mockvid' in v.get('url', '') or v.get('title', '').startswith('Mock Video') for v in videos)
            if is_mock:
                append_agent_log("Warning: Scraper returned mock videos — skipping cache write to prevent polluting real data.")
                return {"videos": videos}
            
            # Update report cache
            report = []
            if os.path.exists(report_path):
                try:
                    with open(report_path, "r", encoding="utf-8") as f:
                        report = json.load(f)
                except: pass
                    
            # Filter out existing entries to prevent duplication
            report = [p for p in report if not (p["url"] == playlist_url or playlist_url in p["url"])]
            report.append({
                "name": scanned_name,
                "url": playlist_url,
                "videos": videos,
                "video_count": len(videos)
            })
                
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            
            return {"videos": videos}
        except Exception as e:
            append_agent_log(f"Error in live video fetch: {e}")
            if cached_videos:
                append_agent_log("Falling back to cached videos list after error.")
                return {"videos": cached_videos}
            raise HTTPException(status_code=500, detail=f"Failed to fetch videos live: {str(e)}")
        finally:
            if driver:
                try: driver.quit()
                except: pass
            with task_manager.lock:
                task_manager.active_job = None
                with scheduler.job_lock:
                    scheduler.active_job = None
    else:
        return {"videos": cached_videos}

@app.post("/api/playlists/batch-move")
def api_batch_move(req: BatchMoveRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    success, msg = task_manager.run_function(
        f"Batch Move ({len(req.video_urls)} items)",
        execute_batch_move_background,
        (req.video_urls, req.source_playlist, req.target_playlist, user_id)
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": "Batch move successfully added to queue."}

def update_cache_move_video(video_url, source_name, target_name):
    report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
    if not os.path.exists(report_path):
        return
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            playlists = json.load(f)
            
        video_obj = None
        # Remove from source
        for p in playlists:
            if p.get("name", "").lower() == source_name.lower():
                videos = p.get("videos", [])
                for idx, v in enumerate(videos):
                    if v.get("url") == video_url:
                        video_obj = videos.pop(idx)
                        p["video_count"] = len(videos)
                        break
                break
                
        # Add to target
        if video_obj:
            for p in playlists:
                if p.get("name", "").lower() == target_name.lower():
                    if "videos" not in p:
                        p["videos"] = []
                    p["videos"].append(video_obj)
                    p["video_count"] = len(p["videos"])
                    break
                    
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(playlists, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error updating report cache: {e}")

def execute_move_single_background(video_url: str, source_playlist: str, target_playlist: str, user_id=None):
    vid = extract_video_id(video_url)
    append_agent_log(f"Starting single move of {video_url} ({vid}) from '{source_playlist}' to '{target_playlist}' (user_id={user_id}).")
    
    current_job_name = f"Move Single ({vid})"
    with task_manager.lock:
        scheduler.active_job = current_job_name
        task_manager.active_job = current_job_name
        
    success = False
    if is_oauth_configured() and user_id is not None:
        import youtube_api
        try:
            target_playlist_id = get_playlist_id_by_name(target_playlist, user_id)
            source_playlist_item_id = get_playlist_item_id_from_cache(video_url, source_playlist, user_id)
            if not target_playlist_id:
                raise ValueError(f"Target playlist '{target_playlist}' not found in user cache")
            if not source_playlist_item_id:
                raise ValueError(f"Source item ID for '{video_url}' not found in '{source_playlist}'")
            success = youtube_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
        except Exception as e:
            append_agent_log(f"OAuth single move failed: {e}")
            success = False
    else:
        driver = None
        try:
            driver = get_browser()
            success = move_video(video_url, source_playlist, target_playlist, driver=driver)
        except Exception as e:
            append_agent_log(f"Browser single move failed: {e}")
            success = False
        finally:
            if driver:
                try: driver.quit()
                except: pass

    try:
        if success:
            append_agent_log(f"Successfully moved single video {video_url}.")
            
            # Record to history
            title = find_title_in_cache(video_url, source_playlist, user_id)
            action = {
                "vid": vid,
                "title": title,
                "type": "MISPLACED",
                "from": [source_playlist],
                "to": target_playlist
            }
            from apply_maintenance import record_history
            action_id = record_history(action, user_id)
            append_agent_log(f"History recorded. Action ID: {action_id}")
            
            # Auto-learn rule for manual single move
            try:
                channel = find_channel_in_cache(video_url, source_playlist, user_id)
                if channel and target_playlist.lower() != "watch later":
                    if not is_oauth_configured():
                        from apply_maintenance import learn_channel_rule
                        learn_channel_rule(channel, target_playlist)
                    else:
                        db_helper.save_user_rule(user_id, channel, target_playlist)
                    append_agent_log(f"Auto-learned rule for single move: {channel} -> {target_playlist}")
            except Exception as ex:
                append_agent_log(f"Failed to auto-learn rule for single move: {ex}")
            
            # Send Discord report
            try:
                from apply_maintenance import send_discord_history_report
                send_discord_history_report([{**action, "action_id": action_id}])
            except Exception as ex:
                append_agent_log(f"Discord report fail: {ex}")
                
            # Update cache
            update_cache_for_move(video_url, source_playlist, target_playlist, user_id)
        else:
            append_agent_log(f"Failed to move single video: {video_url}")
    except Exception as e:
        append_agent_log(f"Fatal error in single move: {e}")
    finally:
        with task_manager.lock:
            task_manager.active_job = None
            with scheduler.job_lock:
                scheduler.active_job = None

@app.post("/api/playlists/move-single")
def api_move_single(req: SingleMoveRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    success, msg = task_manager.run_function(
        f"Move Single ({extract_video_id(req.video_url)})",
        execute_move_single_background,
        (req.video_url, req.source_playlist, req.target_playlist, user_id)
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": "Single move successfully added to queue."}

@app.post("/api/playlists/batch-delete")
def api_batch_delete(req: BatchDeleteRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    success, msg = task_manager.run_function(
        f"Batch Delete ({len(req.video_urls)} items)",
        execute_batch_delete_background,
        (req.video_urls, req.playlist, user_id)
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": "Batch delete successfully added to queue."}

@app.post("/api/playlists/batch-move-multi-source")
def api_batch_move_multi_source(req: MultiSourceMoveRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    items_list = [item.dict() for item in req.items]
    success, msg = task_manager.run_function(
        f"Multi-Source Move ({len(req.items)} items)",
        execute_multi_source_move_background,
        (items_list, req.target_playlist, user_id)
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": "Multi-source batch move successfully added to queue."}

@app.post("/api/playlists/batch-delete-multi-source")
def api_batch_delete_multi_source(req: MultiSourceDeleteRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    items_list = [item.dict() for item in req.items]
    success, msg = task_manager.run_function(
        f"Multi-Source Delete ({len(req.items)} items)",
        execute_multi_source_delete_background,
        (items_list, user_id)
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": "Multi-source batch delete successfully added to queue."}

@app.post("/api/playlists/remove-duplicates")
def api_remove_duplicates(req: RemoveDuplicatesRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    success, msg = task_manager.run_function(
        f"Clean Dupes ({req.playlist_name})",
        execute_remove_duplicates_background,
        (req.playlist_name, user_id)
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": "Duplicate cleanup successfully added to queue."}


@app.get("/api/rules")
def get_rules(user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    
    rules_md = ""
    rules_path = os.path.join(os.path.dirname(__file__), "youtube_rules.promptinclude.md")
    if os.path.exists(rules_path):
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                rules_md = f.read()
        except: pass
        
    if not is_oauth_configured():
        channels_txt = ""
        chan_path = os.path.join(os.path.dirname(__file__), "youtube_category_channel_map.txt")
        if os.path.exists(chan_path):
            try:
                with open(chan_path, "r", encoding="utf-8") as f:
                    channels_txt = f.read()
            except: pass
    else:
        user_rules = db_helper.load_user_rules(user_id)
        channels_txt = "\n".join(f"{ch} : {cat}" for ch, cat in user_rules.items()) + "\n"
        
    return {"rules_md": rules_md, "channels_txt": channels_txt}

@app.post("/api/rules")
def save_rules(req: RulesSaveRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    try:
        rules_path = os.path.join(os.path.dirname(__file__), "youtube_rules.promptinclude.md")
        with open(rules_path, "w", encoding="utf-8") as f:
            f.write(req.rules_md)
            
        if not is_oauth_configured():
            chan_path = os.path.join(os.path.dirname(__file__), "youtube_category_channel_map.txt")
            with open(chan_path, "w", encoding="utf-8") as f:
                f.write(req.channels_txt)
        else:
            conn = db_helper.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_rules WHERE user_id = ?", (user_id,))
            
            rules = []
            for line in req.channels_txt.splitlines():
                if ":" in line:
                    parts = line.strip().split(":")
                    if len(parts) == 2:
                        rules.append((user_id, parts[0].strip(), parts[1].strip()))
            if rules:
                cursor.executemany("""
                INSERT OR REPLACE INTO user_rules (user_id, channel_name, target_category) VALUES (?, ?, ?)
                """, rules)
            conn.commit()
            conn.close()
            
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rules/add-channel")
def add_channel_rule(req: AddChannelRuleRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    try:
        if not is_oauth_configured():
            from apply_maintenance import learn_channel_rule
            learn_channel_rule(req.channel, req.category)
        else:
            db_helper.save_user_rule(user_id, req.channel, req.category)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/maintenance")
def get_maintenance():
    maint_path = os.path.join(os.path.dirname(__file__), "maintenance_actions.json")
    if os.path.exists(maint_path):
        try:
            with open(maint_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return []

@app.post("/api/maintenance/generate")
def api_generate_maintenance():
    success, msg = task_manager.run_task("generate_maintenance", ["generate_maintenance.py"])
    if success:
        return {"success": True, "message": "Regenerating queue in background..."}
    else:
        raise HTTPException(status_code=400, detail=msg)

@app.post("/api/maintenance/apply")
def api_apply_maintenance(req: MaintenanceApplyRequest):
    args = ["apply_maintenance.py"]
    if req.force:
        args.append("--force")
        
    success, msg = task_manager.run_task("apply_maintenance", args)
    if success:
        return {"success": True, "message": "Executing maintenance in background..."}
    else:
        raise HTTPException(status_code=400, detail=msg)

@app.post("/api/maintenance/update-target")
def update_maint_target(req: UpdateMaintTargetRequest):
    maint_path = os.path.join(os.path.dirname(__file__), "maintenance_actions.json")
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
    try:
        with open(maint_path, "r", encoding="utf-8") as f:
            actions = json.load(f)
            
        action = next((a for a in actions if a.get("vid") == req.vid), None)
        if not action:
            raise HTTPException(status_code=404, detail="Action not found in queue")
            
        action["to"] = req.target
        
        with open(maint_path, "w", encoding="utf-8") as f:
            json.dump(actions, f, indent=2, ensure_ascii=False)
            
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Discard a specific maintenance action
@app.post("/api/maintenance/discard")
def discard_action(req: SingleActionRequest):
    maint_path = os.path.join(os.path.dirname(__file__), "maintenance_actions.json")
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    try:
        with open(maint_path, "r", encoding="utf-8") as f:
            actions = json.load(f)

        # Learn the rule (since user skips, they reject the move, meaning current playlist is correct)
        action = next((a for a in actions if a.get("vid") == req.vid), None)
        if action and action.get("type") == "MISPLACED":
            channel = action.get("channel")
            current_playlist = action.get("from")[0] if action.get("from") else None
            if channel and current_playlist:
                from apply_maintenance import learn_channel_rule
                learn_channel_rule(channel, current_playlist)

        # Filter out the action with this vid
        updated_actions = [a for a in actions if a.get("vid") != req.vid]
        
        with open(maint_path, "w", encoding="utf-8") as f:
            json.dump(updated_actions, f, indent=2, ensure_ascii=False)
            
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Apply a single maintenance action immediately (inline thread)
def execute_single_action_background(action):
    log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
    def log_message(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[Single Action] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
            
    vid_url = f"https://www.youtube.com/watch={action['vid']}" if "watch=" in action['vid'] else f"https://www.youtube.com/watch?v={action['vid']}"
    
    success = False
    try:
        log_message(f"Starting inline apply for '{action['title']}'...")
        if action['type'] == 'MISPLACED':
            target = action['to']
            old_ps = action['from']
            log_message(f"Adding to '{target}'...")
            if add_video_to_playlist(vid_url, target):
                for old_p in old_ps:
                    log_message(f"Removing from '{old_p}'...")
                    remove_video_from_playlist(vid_url, old_p)
                log_message("Success!")
                success = True
            else:
                log_message(f"Failed to add to target '{target}'. Skipping removals.")
                
        elif action['type'] in ['DUPLICATE', 'DUPLICATE_NO_TARGET']:
            remove_from = action['remove']
            for p_name in remove_from:
                log_message(f"Removing duplicate from '{p_name}'...")
                remove_video_from_playlist(vid_url, p_name)
            log_message("Success!")
            success = True
            
        if success:
            try:
                from apply_maintenance import record_history, send_discord_history_report, learn_channel_rule
                action_id = record_history(action)
                log_message(f"Recorded action ID: {action_id}")
                send_discord_history_report([{**action, "action_id": action_id}])
                
                # Auto-learn rule
                channel_name = action.get("channel")
                category_name = action.get("to") or action.get("keep")
                if channel_name and category_name:
                    learn_channel_rule(channel_name, category_name)
            except Exception as ex:
                log_message(f"Failed to log history/send Discord report: {ex}")
            
    except Exception as e:
        log_message(f"Error executing action: {e}")

@app.post("/api/maintenance/apply-single")
def apply_single_action(req: SingleActionRequest, background_tasks: BackgroundTasks):
    maint_path = os.path.join(os.path.dirname(__file__), "maintenance_actions.json")
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    try:
        with open(maint_path, "r", encoding="utf-8") as f:
            actions = json.load(f)
            
        action = next((a for a in actions if a.get("vid") == req.vid), None)
        if not action:
            raise HTTPException(status_code=404, detail="Action not found in queue")
            
        # Queue the browser operation in FastAPI background tasks
        background_tasks.add_task(execute_single_action_background, action)
        
        # Remove from queue immediately
        updated_actions = [a for a in actions if a.get("vid") != req.vid]
        with open(maint_path, "w", encoding="utf-8") as f:
            json.dump(updated_actions, f, indent=2, ensure_ascii=False)
            
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def execute_batch_maintenance_background(actions):
    log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
    def log_message(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[Batch Maintenance] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
            
    total = len(actions)
    log_message(f"Starting batch maintenance execution of {total} actions.")
    
    driver = None
    try:
        driver = get_browser()
        for idx, action in enumerate(actions):
            current_job_name = f"Batch Action ({idx+1}/{total})"
            with task_manager.lock:
                scheduler.active_job = current_job_name
                task_manager.active_job = current_job_name
                
            vid_url = f"https://www.youtube.com/watch={action['vid']}" if "watch=" in action['vid'] else f"https://www.youtube.com/watch?v={action['vid']}"
            log_message(f"[{idx+1}/{total}] Applying '{action['title']}' ({action['type']})...")
            
            success = False
            try:
                if action['type'] == 'MISPLACED':
                    target = action['to']
                    old_ps = action['from']
                    log_message(f"Adding to '{target}'...")
                    if add_video_to_playlist(vid_url, target, driver=driver):
                        for old_p in old_ps:
                            log_message(f"Removing from '{old_p}'...")
                            remove_video_from_playlist(vid_url, old_p, driver=driver)
                        log_message("Success!")
                        success = True
                    else:
                        log_message(f"Failed to add to target '{target}'. Skipping removals.")
                        
                elif action['type'] in ['DUPLICATE', 'DUPLICATE_NO_TARGET']:
                    remove_from = action['remove']
                    for p_name in remove_from:
                        log_message(f"Removing duplicate from '{p_name}'...")
                        remove_video_from_playlist(vid_url, p_name, driver=driver)
                    log_message("Success!")
                    success = True
                    
                if success:
                    try:
                        from apply_maintenance import record_history, send_discord_history_report, learn_channel_rule
                        action_id = record_history(action)
                        log_message(f"Recorded action ID: {action_id}")
                        send_discord_history_report([{**action, "action_id": action_id}])
                        
                        # Auto-learn rule
                        channel_name = action.get("channel")
                        category_name = action.get("to") or action.get("keep")
                        if channel_name and category_name:
                            learn_channel_rule(channel_name, category_name)
                    except Exception as ex:
                        log_message(f"Failed to log history/send Discord report: {ex}")
            except Exception as item_err:
                log_message(f"Error executing action for '{action['title']}': {item_err}")
                
            time.sleep(1)
            
        log_message(f"Completed batch maintenance of {total} actions.")
    except Exception as e:
        log_message(f"Fatal error in batch maintenance: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        with task_manager.lock:
            task_manager.active_job = None
            with scheduler.job_lock:
                scheduler.active_job = None

@app.post("/api/maintenance/batch-apply")
def api_batch_apply_maintenance(req: BatchMaintenanceRequest):
    maint_path = os.path.join(os.path.dirname(__file__), "maintenance_actions.json")
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    try:
        with open(maint_path, "r", encoding="utf-8") as f:
            actions = json.load(f)
            
        selected_actions = [a for a in actions if a.get("vid") in req.vids]
        if not selected_actions:
            raise HTTPException(status_code=400, detail="No matching actions found in queue")
            
        # Queue the batch process in the background task manager
        success, msg = task_manager.run_function(
            f"Batch Maintenance ({len(selected_actions)} items)", 
            execute_batch_maintenance_background, 
            (selected_actions,)
        )
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        
        # Remove them from queue immediately
        updated_actions = [a for a in actions if a.get("vid") not in req.vids]
        with open(maint_path, "w", encoding="utf-8") as f:
            json.dump(updated_actions, f, indent=2, ensure_ascii=False)
            
        return {"success": True, "message": f"Successfully queued batch execution of {len(selected_actions)} actions."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/maintenance/batch-discard")
def api_batch_discard_maintenance(req: BatchMaintenanceRequest):
    maint_path = os.path.join(os.path.dirname(__file__), "maintenance_actions.json")
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    try:
        # Learn rules for any discarded misplaced actions (retains current playlist category)
        from apply_maintenance import learn_channel_rule
        for a in actions:
            if a.get("vid") in req.vids and a.get("type") == "MISPLACED":
                channel = a.get("channel")
                current_playlist = a.get("from")[0] if a.get("from") else None
                if channel and current_playlist:
                    learn_channel_rule(channel, current_playlist)

        updated_actions = [a for a in actions if a.get("vid") not in req.vids]
        with open(maint_path, "w", encoding="utf-8") as f:
            json.dump(updated_actions, f, indent=2, ensure_ascii=False)
            
        return {"success": True, "message": f"Discarded {len(actions) - len(updated_actions)} actions."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/run-scan")
def run_scan(user=Depends(get_current_user)):
    if not is_oauth_configured():
        success, msg = task_manager.run_task("scan", ["cli.py", "scan"])
    else:
        user_id = user.get("user_id") if "user_id" in user else user.get("id")
        import youtube_api
        success, msg = task_manager.run_function(
            "scan",
            youtube_api.run_api_scan_and_save,
            (user_id,)
        )
    if success:
        return {"success": True, "message": "Starting scan in background..."}
    else:
        raise HTTPException(status_code=400, detail=msg)

@app.post("/api/run-sort")
def run_sort():
    success, msg = task_manager.run_task("sort", ["cli.py", "auto-sort"])
    if success:
        return {"success": True, "message": "Starting auto-sort in background..."}
    else:
        raise HTTPException(status_code=400, detail=msg)

@app.post("/api/tasks/stop")
def stop_current_task():
    success, msg = task_manager.stop_task()
    if success:
        return {"success": True, "message": msg}
    else:
        raise HTTPException(status_code=400, detail=msg)

@app.get("/api/logs")
def get_logs():
    log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                # Return last 2000 lines/characters to save bandwidth
                content = f.read()
                return {"logs": content[-100000:]} # return up to last 100kb
        except Exception as e:
            return {"logs": f"Error reading log file: {e}"}
    return {"logs": "No log output recorded yet."}

@app.post("/api/logs/clear")
def clear_logs():
    log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/screenshots/{name}")
def get_screenshot(name: str):
    # Sanitize name
    name = os.path.basename(name)
    path = os.path.join(os.path.dirname(__file__), name)
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="Screenshot not found")

@app.get("/api/settings")
def get_settings():
    settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r") as f:
                data = json.load(f)
                # Hide key slightly
                key = data.get("gemini_api_key", "")
                masked_key = key[:4] + "*" * (len(key) - 8) + key[-4:] if len(key) > 8 else key
                return {
                    "gemini_api_key": masked_key,
                    "notification_webhook": data.get("notification_webhook", "")
                }
        except: pass
    return {"gemini_api_key": "", "notification_webhook": ""}

@app.post("/api/settings")
def save_settings(req: SettingsRequest):
    settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
    
    # Load old key if user passed a masked key
    old_key = ""
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r") as f:
                old_key = json.load(f).get("gemini_api_key", "")
        except: pass
        
    api_key = req.gemini_api_key
    if "*" in api_key and old_key:
        api_key = old_key
        
    try:
        with open(settings_path, "w") as f:
            json.dump({
                "gemini_api_key": api_key,
                "notification_webhook": req.notification_webhook
            }, f, indent=2)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from fastapi.responses import HTMLResponse

@app.get("/api/maintenance/rollback", response_class=HTMLResponse)
def get_rollback(action_id: str):
    history_path = os.path.join(os.path.dirname(__file__), "maintenance_history.json")
    if not os.path.exists(history_path):
        return HTMLResponse(content="<h1>Error: No history found.</h1>", status_code=404)
        
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            history = json.load(f)
            
        action = next((a for a in history if a.get("action_id") == action_id), None)
        if not action:
            return HTMLResponse(content="<h1>Error: Action ID not found or already rolled back.</h1>", status_code=404)
            
        # Execute rollback operations
        vid = action["vid"]
        vid_url = f"https://www.youtube.com/watch?v={vid}"
        title = action.get("title", "Unknown Video")
        action_type = action.get("type")
        
        rollback_details = []
        success = False
        
        # Get browser session
        from core import get_browser
        driver = get_browser()
        try:
            if action_type == 'MISPLACED':
                target = action['to']
                old_ps = action['from']
                # Add back to source
                for old_p in old_ps:
                    if add_video_to_playlist(vid_url, old_p, driver=driver):
                        rollback_details.append(f"Restored to '{old_p}'")
                # Remove from target
                if remove_video_from_playlist(vid_url, target, driver=driver):
                    rollback_details.append(f"Removed from '{target}'")
                success = True
                
            elif action_type in ['DUPLICATE', 'DUPLICATE_NO_TARGET']:
                remove_from = action['remove']
                # Add back to remove list
                for p_name in remove_from:
                    if add_video_to_playlist(vid_url, p_name, driver=driver):
                        rollback_details.append(f"Restored to '{p_name}'")
                success = True
        finally:
            driver.quit()
            
        if success:
            # Remove action from history
            updated_history = [a for a in history if a.get("action_id") != action_id]
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(updated_history, f, indent=2, ensure_ascii=False)
                
            # Log success
            log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[Rollback] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Rolled back action '{title}' ({action_id}). Details: {', '.join(rollback_details)}\n")
                
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Rollback Successful</title>
                <style>
                    body {{
                        background: radial-gradient(circle at top right, #1e1b4b, #0f172a, #020617);
                        color: #f8fafc;
                        font-family: system-ui, -apple-system, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        height: 100vh;
                        margin: 0;
                    }}
                    .card {{
                        background: rgba(255, 255, 255, 0.03);
                        backdrop-filter: blur(16px);
                        -webkit-backdrop-filter: blur(16px);
                        border: 1px solid rgba(255, 255, 255, 0.08);
                        padding: 2.5rem;
                        border-radius: 16px;
                        max-width: 500px;
                        text-align: center;
                        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
                    }}
                    h1 {{ color: #10b981; margin-top: 0; }}
                    p {{ color: #94a3b8; line-height: 1.6; }}
                    .video-title {{ font-weight: bold; color: #f8fafc; }}
                    .btn {{
                        background: linear-gradient(135deg, #6366f1, #4f46e5);
                        color: white;
                        border: none;
                        padding: 10px 24px;
                        border-radius: 8px;
                        cursor: pointer;
                        text-decoration: none;
                        font-weight: 600;
                        display: inline-block;
                        margin-top: 1.5rem;
                        transition: all 0.3s ease;
                    }}
                    .btn:hover {{
                        transform: translateY(-2px);
                        box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
                    }}
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>Rollback Complete</h1>
                    <p>Successfully rolled back action for video:</p>
                    <p class="video-title">"{title}"</p>
                    <p style="font-size: 0.9rem; color: #64748b;">{", ".join(rollback_details)}</p>
                    <a href="/" class="btn">Return to Dashboard</a>
                </div>
            </body>
            </html>
            """
            return HTMLResponse(content=html_content)
        else:
            return HTMLResponse(content="<h1>Rollback failed to complete. See agent logs.</h1>", status_code=500)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Error running rollback: {e}</h1>", status_code=500)

@app.get("/api/ai-classifications")
def get_ai_classifications():
    class_path = os.path.join(os.path.dirname(__file__), "ai_classifications.json")
    classifications = []
    if os.path.exists(class_path):
        try:
            with open(class_path, "r", encoding="utf-8") as f:
                classifications = json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error reading classifications: {e}")
            
    # Resolve current playlist for each classification item
    report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
    vid_to_playlist = {}
    if os.path.exists(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            for p in report:
                playlist_name = p.get("name")
                for v in p.get("videos", []):
                    vid = extract_video_id(v.get("url", ""))
                    if vid:
                        vid_to_playlist[vid] = playlist_name
        except:
            pass
            
    for item in classifications:
        item["current_playlist"] = vid_to_playlist.get(item.get("vid"), "Unknown")
        
    return classifications

@app.post("/api/ai-classifications/delete")
def api_ai_classifications_delete(req: BatchAIDeleteRequest):
    class_path = os.path.join(os.path.dirname(__file__), "ai_classifications.json")
    if not os.path.exists(class_path):
        raise HTTPException(status_code=404, detail="Classifications log not found")
        
    try:
        with open(class_path, "r", encoding="utf-8") as f:
            classifications = json.load(f)
            
        targets = [c for c in classifications if c.get("vid") in req.vids]
        if not targets:
            raise HTTPException(status_code=404, detail="No matching classifications found")
            
        # Resolve current playlist for each target
        report_path = os.path.join(os.path.dirname(__file__), "playlists_report.json")
        vid_to_playlist = {}
        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                for p in report:
                    playlist_name = p.get("name")
                    for v in p.get("videos", []):
                        vid = extract_video_id(v.get("url", ""))
                        if vid:
                            vid_to_playlist[vid] = playlist_name
            except:
                pass
                
        delete_items = []
        for c in targets:
            current_p = vid_to_playlist.get(c.get("vid"))
            if current_p and current_p != "Unknown":
                delete_items.append({
                    "video_url": f"https://www.youtube.com/watch?v={c.get('vid')}",
                    "source_playlist": current_p
                })
                
        if delete_items:
            success, msg = task_manager.run_function(
                f"Multi-Source Delete ({len(delete_items)} items)",
                execute_multi_source_delete_background,
                (delete_items,)
            )
            if not success:
                raise HTTPException(status_code=400, detail=msg)
                
        updated_classifications = [c for c in classifications if c.get("vid") not in req.vids]
        with open(class_path, "w", encoding="utf-8") as f:
            json.dump(updated_classifications, f, indent=2, ensure_ascii=False)
            
        return {"success": True, "message": f"Successfully queued deletion of {len(delete_items)} videos and updated classifications."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ai-classifications/action")
def api_ai_classification_action(req: AIClassificationActionRequest):
    class_path = os.path.join(os.path.dirname(__file__), "ai_classifications.json")
    if not os.path.exists(class_path):
        raise HTTPException(status_code=404, detail="AI Classifications log not found")
        
    try:
        with open(class_path, "r", encoding="utf-8") as f:
            classifications = json.load(f)
            
        target = next((item for item in classifications if item.get("vid") == req.vid), None)
        if not target:
            raise HTTPException(status_code=404, detail="Classification not found")
            
        # Update the classification status and category
        if req.action == "approve":
            target["status"] = "approved"
        elif req.action == "correct":
            target["status"] = "corrected"
            target["category"] = req.category
        elif req.action in ["skip", "discard"]:
            classifications = [c for c in classifications if c.get("vid") != req.vid]
            with open(class_path, "w", encoding="utf-8") as f:
                json.dump(classifications, f, indent=2, ensure_ascii=False)
            return {"success": True, "message": "Classification skipped/removed successfully"}
        else:
            raise HTTPException(status_code=400, detail="Invalid action")
            
        # Save updated classifications
        with open(class_path, "w", encoding="utf-8") as f:
            json.dump(classifications, f, indent=2, ensure_ascii=False)
            
        # Pin rule: channel -> category
        channel_name = target.get("channel")
        target_category = target.get("category")
        if channel_name and target_category:
            from apply_maintenance import learn_channel_rule
            learn_channel_rule(channel_name, target_category)
                        
        return {"success": True, "message": f"Successfully {req.action}d classification and pinned channel rule"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ai-classifications/batch-action")
def api_batch_ai_classification_action(req: BatchAIClassificationRequest):
    class_path = os.path.join(os.path.dirname(__file__), "ai_classifications.json")
    if not os.path.exists(class_path):
        raise HTTPException(status_code=404, detail="AI Classifications log not found")
        
    try:
        with open(class_path, "r", encoding="utf-8") as f:
            classifications = json.load(f)
            
        if req.action in ["skip", "discard"]:
            classifications = [c for c in classifications if c.get("vid") not in req.vids]
            with open(class_path, "w", encoding="utf-8") as f:
                json.dump(classifications, f, indent=2, ensure_ascii=False)
            return {"success": True, "message": f"Successfully skipped {len(req.vids)} classifications"}
            
        success_count = 0
        new_channel_rules = []
        
        for item in classifications:
            if item.get("vid") in req.vids:
                # Update status
                if req.action == "approve":
                    item["status"] = "approved"
                elif req.action == "correct":
                    item["status"] = "corrected"
                    item["category"] = req.category
                else:
                    raise HTTPException(status_code=400, detail="Invalid action")
                
                success_count += 1
                
                # Gather channel mapping
                channel_name = item.get("channel")
                target_category = item.get("category")
                if channel_name and target_category:
                    new_channel_rules.append((channel_name.strip(), target_category.strip()))
                    
        # Save updated classifications
        with open(class_path, "w", encoding="utf-8") as f:
            json.dump(classifications, f, indent=2, ensure_ascii=False)
            
        # Bulk learn/update channel rules
        if new_channel_rules:
            from apply_maintenance import learn_channel_rule
            for channel, category in new_channel_rules:
                learn_channel_rule(channel, category)
                        
        return {"success": True, "message": f"Successfully processed {success_count} classifications."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Mount static files (must be after endpoints to avoid shadowing)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

