import os
import sys
import json
import time
import threading
from typing import List, Optional
from fastapi import APIRouter, Cookie, Depends, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import scheduler
import db_helper
from core import task_manager
from core.utils import (
    is_oauth_configured,
    get_user_file_path,
    extract_video_id,
    get_current_user,
    append_agent_log,
    cache_lock,
    MAINTENANCE_CACHE
)
from core.task_manager import (
    execute_apply_maintenance_background,
    execute_batch_maintenance_api_background,
    execute_single_action_background,
    execute_single_action_delete_background,
    execute_batch_maintenance_delete_background,
    execute_batch_maintenance_delete_api_background,
    execute_multi_source_delete_background
)

router = APIRouter(tags=["maintenance"])

# Initialize templates
base_dir = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(base_dir, "templates"))

# Models
class MaintenanceApplyRequest(BaseModel):
    force: bool

class SingleActionRequest(BaseModel):
    vid: str

class BatchMaintenanceRequest(BaseModel):
    vids: List[str]

class BatchAIDeleteRequest(BaseModel):
    vids: List[str]

class AIClassificationActionRequest(BaseModel):
    vid: str
    action: str  # "approve" or "correct" or "skip" or "discard"
    category: str

class BatchAIClassificationRequest(BaseModel):
    vids: List[str]
    action: str  # "approve" or "correct" or "skip" or "discard"
    category: str

class UpdateMaintTargetRequest(BaseModel):
    vid: str
    target: str


@router.get("/api/maintenance")
def get_maintenance(user=Depends(get_current_user)):
    global MAINTENANCE_CACHE
    maint_path = get_user_file_path("maintenance_actions.json", user)
    
    with cache_lock:
        if os.path.exists(maint_path):
            try:
                mtime = os.path.getmtime(maint_path)
                cached = MAINTENANCE_CACHE.get(maint_path)
                if cached and cached.get("mtime") == mtime:
                    return cached["data"]
                with open(maint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                MAINTENANCE_CACHE[maint_path] = {
                    "mtime": mtime,
                    "data": data
                }
                return data
            except: pass
    return []

@router.post("/api/maintenance/generate")
def api_generate_maintenance(user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    args = ["generate_maintenance.py"]
    if is_oauth_configured():
        args.extend(["--user-id", str(user_id)])
    success, msg = task_manager.run_task("generate_maintenance", args)
    if success:
        return {"success": True, "message": "Regenerating queue in background..."}
    else:
        raise HTTPException(status_code=400, detail=msg)

@router.post("/api/maintenance/apply")
def api_apply_maintenance(req: MaintenanceApplyRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    if is_oauth_configured():
        success, msg = task_manager.run_function(
            "Apply Maintenance Queue",
            execute_apply_maintenance_background,
            (user_id, req.force)
        )
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        return {"success": True, "message": "Executing maintenance queue in background..."}
    else:
        args = ["apply_maintenance.py"]
        if req.force:
            args.append("--force")
        success, msg = task_manager.run_task("apply_maintenance", args)
        if success:
            return {"success": True, "message": "Executing maintenance in background..."}
        else:
            raise HTTPException(status_code=400, detail=msg)

@router.post("/api/maintenance/update-target")
def update_maint_target(req: UpdateMaintTargetRequest, user=Depends(get_current_user)):
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
    try:
        with cache_lock:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)
                
            action = next((a for a in actions if a.get("vid") == req.vid), None)
            if not action:
                raise HTTPException(status_code=404, detail="Action not found in queue")
                
            action["to"] = req.target
            
            with open(maint_path, "w", encoding="utf-8") as f:
                json.dump(actions, f, indent=2, ensure_ascii=False)
                
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/maintenance/discard")
def discard_action(req: SingleActionRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    try:
        with cache_lock:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)

            action = next((a for a in actions if a.get("vid") == req.vid), None)
            if action and action.get("type") == "MISPLACED":
                channel = action.get("channel")
                current_playlist = action.get("from")[0] if action.get("from") else None
                if channel and current_playlist:
                    if is_oauth_configured():
                        db_helper.save_user_rule(user_id, channel, current_playlist)
                    else:
                        from apply_maintenance import learn_channel_rule
                        learn_channel_rule(channel, current_playlist)

            updated_actions = [a for a in actions if a.get("vid") != req.vid]
            
            with open(maint_path, "w", encoding="utf-8") as f:
                json.dump(updated_actions, f, indent=2, ensure_ascii=False)
                
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/maintenance/apply-single")
def apply_single_action(req: SingleActionRequest, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    if is_oauth_configured():
        success, msg = task_manager.run_function(
            f"Apply Single Action ({req.vid})",
            execute_batch_maintenance_api_background,
            (user_id, [req.vid])
        )
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        return {"success": True, "message": "Executing action in background..."}
        
    try:
        with cache_lock:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)
                
            action = next((a for a in actions if a.get("vid") == req.vid), None)
            if not action:
                raise HTTPException(status_code=404, detail="Action not found in queue")
                
            background_tasks.add_task(execute_single_action_background, action)
            
            updated_actions = [a for a in actions if a.get("vid") != req.vid]
            with open(maint_path, "w", encoding="utf-8") as f:
                json.dump(updated_actions, f, indent=2, ensure_ascii=False)
                
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/maintenance/batch-apply")
def api_batch_apply_maintenance(req: BatchMaintenanceRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    if is_oauth_configured():
        success, msg = task_manager.run_function(
            f"Batch Maintenance ({len(req.vids)} items)",
            execute_batch_maintenance_api_background,
            (user_id, req.vids)
        )
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        return {"success": True, "message": f"Successfully queued batch execution of {len(req.vids)} actions."}
        
    try:
        with cache_lock:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)
                
            selected_actions = [a for a in actions if a.get("vid") in req.vids]
            if not selected_actions:
                raise HTTPException(status_code=400, detail="No matching actions found in queue")
                
            success, msg = task_manager.run_function(
                f"Batch Maintenance ({len(selected_actions)} items)", 
                execute_batch_maintenance_background, 
                (selected_actions,)
            )
            if not success:
                raise HTTPException(status_code=400, detail=msg)
            
            updated_actions = [a for a in actions if a.get("vid") not in req.vids]
            with open(maint_path, "w", encoding="utf-8") as f:
                json.dump(updated_actions, f, indent=2, ensure_ascii=False)
                
        return {"success": True, "message": f"Successfully queued batch execution of {len(selected_actions)} actions."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/maintenance/batch-discard")
def api_batch_discard_maintenance(req: BatchMaintenanceRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    try:
        with cache_lock:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)
                
            if is_oauth_configured():
                for a in actions:
                    if a.get("vid") in req.vids and a.get("type") == "MISPLACED":
                        channel = a.get("channel")
                        current_playlist = a.get("from")[0] if a.get("from") else None
                        if channel and current_playlist:
                            db_helper.save_user_rule(user_id, channel, current_playlist)
            else:
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

@router.post("/api/maintenance/delete-single")
def delete_single_action(req: SingleActionRequest, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    if is_oauth_configured():
        success, msg = task_manager.run_function(
            f"Delete Single Video ({req.vid})",
            execute_batch_maintenance_delete_api_background,
            (user_id, [req.vid])
        )
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        return {"success": True, "message": "Executing video deletion in background..."}
        
    try:
        with cache_lock:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)
                
            action = next((a for a in actions if a.get("vid") == req.vid), None)
            if not action:
                raise HTTPException(status_code=404, detail="Action not found in queue")
                
            background_tasks.add_task(execute_single_action_delete_background, action)
            
            updated_actions = [a for a in actions if a.get("vid") != req.vid]
            with open(maint_path, "w", encoding="utf-8") as f:
                json.dump(updated_actions, f, indent=2, ensure_ascii=False)
                
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/maintenance/batch-delete")
def batch_delete_maintenance(req: BatchMaintenanceRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    maint_path = get_user_file_path("maintenance_actions.json", user)
    if not os.path.exists(maint_path):
        raise HTTPException(status_code=404, detail="Queue file not found")
        
    if is_oauth_configured():
        success, msg = task_manager.run_function(
            f"Delete Batch Videos ({len(req.vids)} items)",
            execute_batch_maintenance_delete_api_background,
            (user_id, req.vids)
        )
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        return {"success": True, "message": f"Successfully queued batch deletion of {len(req.vids)} videos."}
        
    try:
        with cache_lock:
            with open(maint_path, "r", encoding="utf-8") as f:
                actions = json.load(f)
                
            selected_actions = [a for a in actions if a.get("vid") in req.vids]
            if not selected_actions:
                raise HTTPException(status_code=400, detail="No matching actions found in queue")
                
            success, msg = task_manager.run_function(
                f"Delete Batch Videos ({len(selected_actions)} items)", 
                execute_batch_maintenance_delete_background, 
                (selected_actions,)
            )
            if not success:
                raise HTTPException(status_code=400, detail=msg)
            
            updated_actions = [a for a in actions if a.get("vid") not in req.vids]
            with open(maint_path, "w", encoding="utf-8") as f:
                json.dump(updated_actions, f, indent=2, ensure_ascii=False)
                
        return {"success": True, "message": f"Successfully queued batch deletion of {len(selected_actions)} videos."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/maintenance/rollback", response_class=HTMLResponse)
def get_rollback(request: Request, action_id: str):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    history_path = os.path.join(base_dir, "maintenance_history.json")
    if not os.path.exists(history_path):
        return HTMLResponse(content="<h1>Error: No history found.</h1>", status_code=404)
        
    try:
        with cache_lock:
            with open(history_path, "r", encoding="utf-8") as f:
                history = json.load(f)
                
            action = next((a for a in history if a.get("action_id") == action_id), None)
            if not action:
                return HTMLResponse(content="<h1>Error: Action ID not found or already rolled back.</h1>", status_code=404)
                
            vid = action["vid"]
            vid_url = f"https://www.youtube.com/watch?v={vid}"
            title = action.get("title", "Unknown Video")
            action_type = action.get("type")
            
            rollback_details = []
            success = False
            
            from core import get_browser, add_video_to_playlist, remove_video_from_playlist
            driver = get_browser()
            try:
                if action_type == 'MISPLACED':
                    target = action['to']
                    old_ps = action['from']
                    for old_p in old_ps:
                        if add_video_to_playlist(vid_url, old_p, driver=driver):
                            rollback_details.append(f"Restored to '{old_p}'")
                    if remove_video_from_playlist(vid_url, target, driver=driver):
                        rollback_details.append(f"Removed from '{target}'")
                    success = True
                    
                elif action_type in ['DUPLICATE', 'DUPLICATE_NO_TARGET']:
                    remove_from = action['remove']
                    for p_name in remove_from:
                        if add_video_to_playlist(vid_url, p_name, driver=driver):
                            rollback_details.append(f"Restored to '{p_name}'")
                    success = True
            finally:
                driver.quit()
                
            if success:
                updated_history = [a for a in history if a.get("action_id") != action_id]
                with open(history_path, "w", encoding="utf-8") as f:
                    json.dump(updated_history, f, indent=2, ensure_ascii=False)
                    
                log_path = os.path.join(base_dir, "agent_run.log")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n[Rollback] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Rolled back action '{title}' ({action_id}). Details: {', '.join(rollback_details)}\n")
                    
                return templates.TemplateResponse("rollback_success.html", {
                    "request": request,
                    "title": title,
                    "details": ", ".join(rollback_details)
                })
            else:
                return HTMLResponse(content="<h1>Rollback failed to complete. See agent logs.</h1>", status_code=500)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Error running rollback: {e}</h1>", status_code=500)

@router.get("/api/ai-classifications")
def get_ai_classifications(user=Depends(get_current_user)):
    class_path = get_user_file_path("ai_classifications.json", user)
    classifications = []
    if os.path.exists(class_path):
        try:
            with cache_lock:
                with open(class_path, "r", encoding="utf-8") as f:
                    classifications = json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error reading classifications: {e}")
            
    report_path = get_user_file_path("playlists_report.json", user)
    vid_to_playlist = {}
    if os.path.exists(report_path):
        try:
            with cache_lock:
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

@router.post("/api/ai-classifications/delete")
def api_ai_classifications_delete(req: BatchAIDeleteRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    class_path = get_user_file_path("ai_classifications.json", user)
    if not os.path.exists(class_path):
        raise HTTPException(status_code=404, detail="Classifications log not found")
        
    try:
        with cache_lock:
            with open(class_path, "r", encoding="utf-8") as f:
                classifications = json.load(f)
                
            targets = [c for c in classifications if c.get("vid") in req.vids]
            if not targets:
                raise HTTPException(status_code=404, detail="No matching classifications found")
                
            report_path = get_user_file_path("playlists_report.json", user)
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
                    (delete_items, user_id)
                )
                if not success:
                    raise HTTPException(status_code=400, detail=msg)
                    
            updated_classifications = [c for c in classifications if c.get("vid") not in req.vids]
            with open(class_path, "w", encoding="utf-8") as f:
                json.dump(updated_classifications, f, indent=2, ensure_ascii=False)
                
        return {"success": True, "message": f"Successfully queued deletion of {len(delete_items)} videos and updated classifications."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/ai-classifications/action")
def api_ai_classification_action(req: AIClassificationActionRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    class_path = get_user_file_path("ai_classifications.json", user)
    if not os.path.exists(class_path):
        raise HTTPException(status_code=404, detail="AI Classifications log not found")
        
    try:
        with cache_lock:
            with open(class_path, "r", encoding="utf-8") as f:
                classifications = json.load(f)
                
            target = next((item for item in classifications if item.get("vid") == req.vid), None)
            if not target:
                raise HTTPException(status_code=404, detail="Classification not found")
                
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
                
            with open(class_path, "w", encoding="utf-8") as f:
                json.dump(classifications, f, indent=2, ensure_ascii=False)
                
            channel_name = target.get("channel")
            target_category = target.get("category")
            if channel_name and target_category:
                if is_oauth_configured():
                    db_helper.save_user_rule(user_id, channel_name, target_category)
                else:
                    from apply_maintenance import learn_channel_rule
                    learn_channel_rule(channel_name, target_category)
                            
        return {"success": True, "message": f"Successfully {req.action}d classification and pinned channel rule"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/ai-classifications/batch-action")
def api_batch_ai_classification_action(req: BatchAIClassificationRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    class_path = get_user_file_path("ai_classifications.json", user)
    if not os.path.exists(class_path):
        raise HTTPException(status_code=404, detail="AI Classifications log not found")
        
    try:
        with cache_lock:
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
                    if req.action == "approve":
                        item["status"] = "approved"
                    elif req.action == "correct":
                        item["status"] = "corrected"
                        item["category"] = req.category
                    else:
                        raise HTTPException(status_code=400, detail="Invalid action")
                    
                    success_count += 1
                    
                    channel_name = item.get("channel")
                    target_category = item.get("category")
                    if channel_name and target_category:
                        new_channel_rules.append((channel_name.strip(), target_category.strip()))
                        
            with open(class_path, "w", encoding="utf-8") as f:
                json.dump(classifications, f, indent=2, ensure_ascii=False)
                
            if new_channel_rules:
                if is_oauth_configured():
                    for channel, category in new_channel_rules:
                        db_helper.save_user_rule(user_id, channel, category)
                else:
                    from apply_maintenance import learn_channel_rule
                    for channel, category in new_channel_rules:
                        learn_channel_rule(channel, category)
                            
        return {"success": True, "message": f"Successfully processed {success_count} classifications."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
