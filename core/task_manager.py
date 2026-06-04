import os
import sys
import time
import threading
import subprocess
from datetime import datetime
from typing import List, Optional

import scheduler
import db_helper
from .actions import add_video_to_playlist, remove_video_from_playlist, list_videos_in_playlist, move_video, get_browser
from .utils import (
    append_agent_log,
    is_oauth_configured,
    get_playlist_id_by_name,
    get_playlist_item_id_from_cache,
    update_cache_for_move,
    update_cache_for_delete,
    find_title_in_cache,
    find_channel_in_cache,
    extract_video_id
)

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
            
            base_dir = os.path.dirname(os.path.dirname(__file__))
            log_path = os.path.join(base_dir, "agent_run.log")
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- [START] Job '{job_name}' started at {timestamp} ---\n")
                
            log_file = open(log_path, "a", encoding="utf-8")
            
            cmd = [sys.executable] + args
            self.process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                cwd=base_dir,
                text=True
            )
            
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
        
        base_dir = os.path.dirname(os.path.dirname(__file__))
        log_path = os.path.join(base_dir, "agent_run.log")
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- [END] Job '{job_name}' completed with code {proc.returncode} at {timestamp} ---\n")
        
        try:
            with open(os.path.join(base_dir, "last_run.txt"), "w") as f:
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
            
            with scheduler.job_lock:
                if scheduler.active_job or self.active_job:
                    scheduler.active_job = None
                    self.active_job = None
                    stopped = True
                    msg += "Active job state cleared."
            
            if self.queue:
                q_count = len(self.queue)
                self.queue.clear()
                stopped = True
                msg += f"Cleared {q_count} queued tasks."
                
            if stopped:
                return True, msg or "Task stopped successfully"
            return False, "No active task running or queued"

task_manager = TaskManager()


# Background execution functions

def execute_batch_move_background(video_urls: List[str], source_playlist: str, target_playlist: str, user_id=None):
    total = len(video_urls)
    append_agent_log(f"Starting batch move of {total} videos from '{source_playlist}' to '{target_playlist}' (user_id={user_id}).")
    
    if is_oauth_configured() and user_id is not None:
        import yt_api
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
                    success = yt_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
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

def execute_move_single_background(video_url: str, source_playlist: str, target_playlist: str, user_id=None):
    vid = extract_video_id(video_url)
    append_agent_log(f"Starting single move of {video_url} ({vid}) from '{source_playlist}' to '{target_playlist}' (user_id={user_id}).")
    
    current_job_name = f"Move Single ({vid})"
    with task_manager.lock:
        scheduler.active_job = current_job_name
        task_manager.active_job = current_job_name
        
    success = False
    if is_oauth_configured() and user_id is not None:
        import yt_api
        try:
            target_playlist_id = get_playlist_id_by_name(target_playlist, user_id)
            source_playlist_item_id = get_playlist_item_id_from_cache(video_url, source_playlist, user_id)
            if not target_playlist_id:
                raise ValueError(f"Target playlist '{target_playlist}' not found in user cache")
            if not source_playlist_item_id:
                raise ValueError(f"Source item ID for '{video_url}' not found in '{source_playlist}'")
            success = yt_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
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

def execute_batch_delete_background(video_urls: List[str], playlist: str, user_id=None):
    total = len(video_urls)
    append_agent_log(f"Starting batch delete of {total} videos from '{playlist}' (user_id={user_id}).")
    
    if is_oauth_configured() and user_id is not None:
        import yt_api
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
                    success = yt_api.remove_video_from_playlist(user_id, playlist_item_id)
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
        import yt_api
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
                    success = yt_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
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
        import yt_api
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
                    success = yt_api.remove_video_from_playlist(user_id, playlist_item_id)
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
    base_dir = os.path.dirname(os.path.dirname(__file__))
    
    if is_oauth_configured() and user_id is not None:
        report_path = os.path.join(base_dir, f"playlists_report_{user_id}.json")
    else:
        report_path = os.path.join(base_dir, "playlists_report.json")
        
    if not os.path.exists(report_path):
        append_agent_log(f"Error: report file {report_path} not found.")
        return
        
    try:
        import json
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
            import yt_api
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
                                        yt_api.remove_video_from_playlist(user_id, item_id)
                                    except Exception as ex:
                                        append_agent_log(f"Error removing item {item_id}: {ex}")
                                        deleted_all = False
                            
                            vid = extract_video_id(url)
                            if deleted_all:
                                yt_api.add_video_to_playlist(user_id, target_playlist_id, vid)
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
                refreshed_videos = yt_api.list_videos_in_playlist(user_id, target_playlist_id)
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

def execute_apply_maintenance_background(user_id, force=False):
    append_agent_log(f"Starting API-based maintenance queue execution (user_id={user_id}).")
    base_dir = os.path.dirname(os.path.dirname(__file__))
    
    import yt_api
    maint_path = os.path.join(base_dir, f"maintenance_actions_{user_id}.json")
    if not os.path.exists(maint_path):
        append_agent_log("No maintenance queue found.")
        return
        
    try:
        with open(maint_path, "r", encoding="utf-8") as f:
            actions = json.load(f)
            
        if not actions:
            append_agent_log("Maintenance queue is empty.")
            return
            
        total = len(actions)
        append_agent_log(f"Found {total} actions to apply.")
        
        success_count = 0
        applied_actions = []
        
        while actions:
            a = actions[0]
            vid = a.get("vid")
            url = f"https://www.youtube.com/watch?v={vid}"
            title = a.get("title", "Unknown")
            action_type = a.get("type")
            
            current_job_name = f"Apply Maint ({success_count+1}/{total})"
            with task_manager.lock:
                scheduler.active_job = current_job_name
                task_manager.active_job = current_job_name
                
            success = False
            try:
                if action_type in ["DUPLICATE", "DUPLICATE_NO_TARGET"]:
                    remove_playlists = a.get("remove", [])
                    for p in remove_playlists:
                        playlist_item_id = get_playlist_item_id_from_cache(url, p, user_id)
                        if playlist_item_id:
                            yt_api.remove_video_from_playlist(user_id, playlist_item_id)
                            update_cache_for_delete(url, p, user_id)
                            success = True
                elif action_type == "MISPLACED":
                    source_playlist = a.get("from", [None])[0]
                    target_playlist = a.get("to")
                    target_playlist_id = get_playlist_id_by_name(target_playlist, user_id)
                    source_playlist_item_id = get_playlist_item_id_from_cache(url, source_playlist, user_id)
                    if target_playlist_id and source_playlist_item_id:
                        yt_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
                        update_cache_for_move(url, source_playlist, target_playlist, user_id)
                        success = True
            except Exception as e:
                append_agent_log(f"Error applying action for {title}: {e}")
                success = False
                
            if success:
                success_count += 1
                applied_actions.append(a)
                append_agent_log(f"Successfully applied action for: {title}")
                
                from apply_maintenance import record_history
                action_id = record_history(a, user_id)
                a["action_id"] = action_id
                
                channel_name = a.get("channel")
                category_name = a.get("to") or a.get("keep")
                if channel_name and category_name:
                    db_helper.save_user_rule(user_id, channel_name, category_name)
            else:
                append_agent_log(f"Failed to apply action for: {title}")
                
            actions.pop(0)
            with open(maint_path, "w", encoding="utf-8") as f:
                json.dump(actions, f, indent=2, ensure_ascii=False)
                
            time.sleep(1)
            
        append_agent_log(f"Maintenance completed. Successfully applied {success_count} of {total} actions.")
        
        if applied_actions:
            try:
                from apply_maintenance import send_discord_history_report
                send_discord_history_report(applied_actions)
            except Exception as ex:
                append_agent_log(f"Failed to send Discord history report: {ex}")
                
        scheduler.send_webhook_notification(f"Maintenance queue completed. Applied {success_count}/{total} actions.")
        
    except Exception as e:
        append_agent_log(f"Fatal error in maintenance execution: {e}")
        scheduler.send_webhook_notification(f"Maintenance execution failed: {e}", is_error=True)
    finally:
        with task_manager.lock:
            task_manager.active_job = None
            with scheduler.job_lock:
                scheduler.active_job = None

def execute_single_action_background(action):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    log_path = os.path.join(base_dir, "agent_run.log")
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
                
                channel_name = action.get("channel")
                category_name = action.get("to") or action.get("keep")
                if channel_name and category_name:
                    learn_channel_rule(channel_name, category_name)
            except Exception as ex:
                log_message(f"Failed to log history/send Discord report: {ex}")
            
    except Exception as e:
        log_message(f"Error executing action: {e}")

def execute_batch_maintenance_api_background(user_id, selected_vids):
    append_agent_log(f"Starting API-based batch maintenance of {len(selected_vids)} videos (user_id={user_id}).")
    base_dir = os.path.dirname(os.path.dirname(__file__))
    
    import yt_api
    maint_path = os.path.join(base_dir, f"maintenance_actions_{user_id}.json")
    if not os.path.exists(maint_path):
        append_agent_log("No maintenance queue found.")
        return
        
    try:
        with open(maint_path, "r", encoding="utf-8") as f:
            actions = json.load(f)
            
        total = len(selected_vids)
        success_count = 0
        applied_actions = []
        
        remaining_actions = []
        for a in actions:
            vid = a.get("vid")
            if vid in selected_vids:
                url = f"https://www.youtube.com/watch?v={vid}"
                title = a.get("title", "Unknown")
                action_type = a.get("type")
                
                current_job_name = f"Apply Batch Maint ({success_count+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                    
                success = False
                try:
                    if action_type in ["DUPLICATE", "DUPLICATE_NO_TARGET"]:
                        remove_playlists = a.get("remove", [])
                        for p in remove_playlists:
                            playlist_item_id = get_playlist_item_id_from_cache(url, p, user_id)
                            if playlist_item_id:
                                yt_api.remove_video_from_playlist(user_id, playlist_item_id)
                                update_cache_for_delete(url, p, user_id)
                                success = True
                    elif action_type == "MISPLACED":
                        source_playlist = a.get("from", [None])[0]
                        target_playlist = a.get("to")
                        target_playlist_id = get_playlist_id_by_name(target_playlist, user_id)
                        source_playlist_item_id = get_playlist_item_id_from_cache(url, source_playlist, user_id)
                        if target_playlist_id and source_playlist_item_id:
                            yt_api.move_video(user_id, source_playlist_item_id, target_playlist_id, vid)
                            update_cache_for_move(url, source_playlist, target_playlist, user_id)
                            success = True
                except Exception as e:
                    append_agent_log(f"Error applying action for {title}: {e}")
                    success = False
                    
                if success:
                    success_count += 1
                    applied_actions.append(a)
                    append_agent_log(f"Successfully applied action for: {title}")
                    
                    from apply_maintenance import record_history
                    action_id = record_history(a, user_id)
                    a["action_id"] = action_id
                    
                    channel_name = a.get("channel")
                    category_name = a.get("to") or a.get("keep")
                    if channel_name and category_name:
                        db_helper.save_user_rule(user_id, channel_name, category_name)
                else:
                    append_agent_log(f"Failed to apply action for: {title}")
                    remaining_actions.append(a)
            else:
                remaining_actions.append(a)
                
        with open(maint_path, "w", encoding="utf-8") as f:
            json.dump(remaining_actions, f, indent=2, ensure_ascii=False)
            
        append_agent_log(f"Batch maintenance completed. Successfully applied {success_count} of {total} actions.")
        
        if applied_actions:
            try:
                from apply_maintenance import send_discord_history_report
                send_discord_history_report(applied_actions)
            except Exception as ex:
                append_agent_log(f"Failed to send Discord history report: {ex}")
                
        scheduler.send_webhook_notification(f"Batch maintenance completed. Applied {success_count}/{total} actions.")
        
    except Exception as e:
        append_agent_log(f"Fatal error in batch maintenance execution: {e}")
        scheduler.send_webhook_notification(f"Batch maintenance execution failed: {e}", is_error=True)
    finally:
        with task_manager.lock:
            task_manager.active_job = None
            with scheduler.job_lock:
                scheduler.active_job = None

def execute_single_action_delete_background(action):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    log_path = os.path.join(base_dir, "agent_run.log")
    def log_message(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[Single Action Delete] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
            
    vid_url = f"https://www.youtube.com/watch={action['vid']}" if "watch=" in action['vid'] else f"https://www.youtube.com/watch?v={action['vid']}"
    
    success = False
    driver = None
    try:
        log_message(f"Starting inline delete for '{action['title']}'...")
        driver = get_browser()
        playlists_to_remove = []
        if action['type'] in ['DUPLICATE', 'DUPLICATE_NO_TARGET']:
            playlists_to_remove.extend(action.get('remove', []))
            if action.get('keep'):
                playlists_to_remove.append(action['keep'])
        elif action['type'] == 'MISPLACED':
            playlists_to_remove.extend(action.get('from', []))
            
        for p in playlists_to_remove:
            log_message(f"Removing from '{p}'...")
            try:
                remove_video_from_playlist(vid_url, p, driver=driver)
                success = True
            except Exception as re:
                log_message(f"Failed to remove from '{p}': {re}")
                
        if success:
            try:
                from apply_maintenance import record_history, send_discord_history_report
                action_id = record_history({**action, "type": f"DELETE_FROM_{action['type']}"})
                log_message(f"Recorded action ID: {action_id}")
                send_discord_history_report([{**action, "action_id": action_id, "type": f"DELETE_FROM_{action['type']}"}])
            except Exception as ex:
                log_message(f"Failed to log history/send Discord report: {ex}")
    except Exception as e:
        log_message(f"Error applying delete for {action['title']}: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

def execute_batch_maintenance_delete_background(actions):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    log_path = os.path.join(base_dir, "agent_run.log")
    def log_message(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[Batch Maintenance Delete] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
            
    total = len(actions)
    log_message(f"Starting batch maintenance deletion of {total} actions.")
    
    driver = None
    applied_actions = []
    try:
        driver = get_browser()
        for idx, action in enumerate(actions):
            current_job_name = f"Delete Batch ({idx+1}/{total})"
            with task_manager.lock:
                scheduler.active_job = current_job_name
                task_manager.active_job = current_job_name
                
            vid_url = f"https://www.youtube.com/watch={action['vid']}" if "watch=" in action['vid'] else f"https://www.youtube.com/watch?v={action['vid']}"
            log_message(f"[{idx+1}/{total}] Deleting '{action['title']}' ({action['type']})...")
            
            success = False
            try:
                playlists_to_remove = []
                if action['type'] in ['DUPLICATE', 'DUPLICATE_NO_TARGET']:
                    playlists_to_remove.extend(action.get('remove', []))
                    if action.get('keep'):
                        playlists_to_remove.append(action['keep'])
                elif action['type'] == 'MISPLACED':
                    playlists_to_remove.extend(action.get('from', []))
                    
                for p in playlists_to_remove:
                    log_message(f"Removing from '{p}'...")
                    remove_video_from_playlist(vid_url, p, driver=driver)
                    success = True
            except Exception as e:
                log_message(f"Error deleting: {e}")
                
            if success:
                applied_actions.append(action)
                try:
                    from apply_maintenance import record_history
                    record_history({**action, "type": f"DELETE_FROM_{action['type']}"})
                except:
                    pass
                    
        if applied_actions:
            try:
                from apply_maintenance import send_discord_history_report
                send_discord_history_report([{**a, "type": f"DELETE_FROM_{a['type']}"} for a in applied_actions])
            except:
                pass
    finally:
        if driver:
            try: driver.quit()
            except: pass

def execute_batch_maintenance_delete_api_background(user_id, selected_vids):
    append_agent_log(f"Starting API-based batch deletion from maintenance of {len(selected_vids)} videos (user_id={user_id}).")
    base_dir = os.path.dirname(os.path.dirname(__file__))
    
    import yt_api
    maint_path = os.path.join(base_dir, f"maintenance_actions_{user_id}.json")
    if not os.path.exists(maint_path):
        append_agent_log("No maintenance queue found.")
        return
        
    try:
        with open(maint_path, "r", encoding="utf-8") as f:
            actions = json.load(f)
            
        total = len(selected_vids)
        success_count = 0
        applied_actions = []
        
        remaining_actions = []
        for a in actions:
            vid = a.get("vid")
            if vid in selected_vids:
                url = f"https://www.youtube.com/watch?v={vid}"
                title = a.get("title", "Unknown")
                action_type = a.get("type")
                
                current_job_name = f"Delete Batch Maint ({success_count+1}/{total})"
                with task_manager.lock:
                    scheduler.active_job = current_job_name
                    task_manager.active_job = current_job_name
                    
                success = False
                try:
                    playlists_to_remove = []
                    if action_type in ["DUPLICATE", "DUPLICATE_NO_TARGET"]:
                        remove_playlists = a.get("remove", [])
                        keep_playlist = a.get("keep")
                        playlists_to_remove.extend(remove_playlists)
                        if keep_playlist:
                            playlists_to_remove.append(keep_playlist)
                    elif action_type == "MISPLACED":
                        from_playlists = a.get("from", [])
                        playlists_to_remove.extend(from_playlists)
                        
                    for p in playlists_to_remove:
                        playlist_item_id = get_playlist_item_id_from_cache(url, p, user_id)
                        if playlist_item_id:
                            yt_api.remove_video_from_playlist(user_id, playlist_item_id)
                            update_cache_for_delete(url, p, user_id)
                            success = True
                except Exception as e:
                    append_agent_log(f"Error deleting video {title}: {e}")
                    success = False
                    
                if success:
                    success_count += 1
                    applied_actions.append(a)
                    append_agent_log(f"Successfully deleted video: {title}")
                    
                    from apply_maintenance import record_history
                    delete_action_record = {**a, "type": f"DELETE_FROM_{action_type}"}
                    action_id = record_history(delete_action_record, user_id)
                    a["action_id"] = action_id
            else:
                remaining_actions.append(a)
                
        with open(maint_path, "w", encoding="utf-8") as f:
            json.dump(remaining_actions, f, indent=2, ensure_ascii=False)
            
        scheduler.send_webhook_notification(f"Batch maintenance deletion completed. Deleted {success_count}/{total} videos.")
    except Exception as e:
        append_agent_log(f"Fatal error in batch maintenance deletion execution: {e}")

def execute_batch_maintenance_background(actions):
    base_dir = os.path.dirname(os.path.dirname(__file__))
    log_path = os.path.join(base_dir, "agent_run.log")
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
