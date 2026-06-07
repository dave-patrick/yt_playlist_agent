import os
import sys
import json
import subprocess
from typing import List, Optional
from fastapi import APIRouter, Cookie, Depends, Request, HTTPException
from pydantic import BaseModel

import scheduler
from core import get_all_playlists, task_manager
from core.utils import (
    is_oauth_configured,
    get_user_file_path,
    load_cached_playlist_report,
    extract_video_id,
    get_current_user,
    append_agent_log
)
from core.task_manager import (
    execute_batch_move_background,
    execute_batch_delete_background,
    execute_multi_source_move_background,
    execute_multi_source_delete_background,
    execute_remove_duplicates_background,
    execute_move_single_background
)

router = APIRouter(prefix="/api/playlists", tags=["playlists"])

# Data models
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


@router.get("")
def get_playlists(user=Depends(get_current_user)):
    report_path = get_user_file_path("playlists_report.json", user)
    playlists = load_cached_playlist_report(report_path)
    if playlists:
        try:
            if isinstance(playlists, list):
                def sort_key(p):
                    name = p.get("name", "")
                    if name.lower() == "watch later":
                        return (0, "")
                    return (1, name.lower())
                playlists = list(playlists)
                playlists.sort(key=sort_key)
            return playlists
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error reading playlists report: {e}")
    return []

@router.get("/videos")
def get_playlist_videos(playlist_url: str, refresh: bool = False, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    report_path = get_user_file_path("playlists_report.json", user)
    
    playlist_id = None
    if "list=" in playlist_url:
        playlist_id = playlist_url.split("list=")[1].split("&")[0]
    else:
        playlist_id = playlist_url

    cached_videos = []
    playlist_name = ""
    
    if os.path.exists(report_path):
        try:
            report = load_cached_playlist_report(report_path)
            if report:
                for p in report:
                    if p.get("url") == playlist_url or (p.get("url") and playlist_url in p["url"]) or p.get("id") == playlist_id:
                        cached_videos = p.get("videos", [])
                        playlist_name = p["name"]
                        break
        except: pass
        
    if not refresh and cached_videos:
        return {"videos": cached_videos}

    if is_oauth_configured() and playlist_id:
        import yt_api
        try:
            videos = yt_api.list_videos_in_playlist(user_id, playlist_id)
            
            # Save the new videos to the cache file so playlist_item_id is preserved!
            scanned_name = playlist_name
            if not scanned_name:
                scanned_name = "Scanned Playlist"
                
            report = []
            if os.path.exists(report_path):
                try:
                    report = load_cached_playlist_report(report_path)
                except: pass
                
            # Filter out the old playlist by url or id
            report = [p for p in report if not (p.get("url") == playlist_url or (p.get("url") and playlist_url in p["url"]) or p.get("id") == playlist_id)]
            report.append({
                "id": playlist_id,
                "name": scanned_name,
                "url": playlist_url if "list=" in playlist_url else f"https://www.youtube.com/playlist?list={playlist_id}",
                "videos": videos,
                "video_count": len(videos)
            })
            
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
                
            return {"videos": videos}
        except Exception as e:
            print(f"YT API failed to fetch videos, falling back to cache: {e}")
            if cached_videos:
                return {"videos": cached_videos}
            raise HTTPException(status_code=500, detail=f"API fetch failed and no cache available: {e}")

    need_refresh = refresh
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
                cwd=os.path.dirname(os.path.dirname(__file__)),
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
            
            scanned_name = playlist_name
            if not scanned_name:
                try:
                    all_p = get_all_playlists()
                    for item in all_p:
                        if item["url"] == playlist_url or playlist_url in item["url"]:
                            scanned_name = item["name"]
                            break
                except: pass
            if not scanned_name:
                scanned_name = "Scanned Playlist"
            
            if not videos and cached_videos:
                append_agent_log(f"Warning: Scraper returned 0 videos for '{scanned_name}', but cache has {len(cached_videos)} videos. Retaining cache to prevent data loss.")
                videos = cached_videos
            
            is_mock = any('mockvid' in v.get('url', '') or v.get('title', '').startswith('Mock Video') for v in videos)
            if is_mock:
                append_agent_log("Warning: Scraper returned mock videos — skipping cache write to prevent polluting real data.")
                return {"videos": videos}
            
            report = []
            if os.path.exists(report_path):
                try:
                    with open(report_path, "r", encoding="utf-8") as f:
                        report = json.load(f)
                except: pass
                    
            p_id = None
            if "list=" in playlist_url:
                p_id = playlist_url.split("list=")[-1].split("&")[0]
                
            report = [p for p in report if not (p.get("url") == playlist_url or (p.get("url") and playlist_url in p["url"]) or p.get("id") == p_id)]
            report.append({
                "id": p_id,
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

@router.post("/batch-move")
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

@router.post("/move-single")
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

@router.post("/batch-delete")
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

@router.post("/batch-move-multi-source")
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

@router.post("/batch-delete-multi-source")
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

@router.post("/remove-duplicates")
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
