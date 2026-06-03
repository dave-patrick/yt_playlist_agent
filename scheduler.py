import os
import sys
import time
import json
import subprocess
import threading
from datetime import datetime
import requests

# Shared state to communicate with server.py
active_job = None
job_lock = threading.Lock()

def send_webhook_notification(message, is_error=False):
    settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
    if not os.path.exists(settings_path):
        return
        
    try:
        with open(settings_path, "r") as f:
            settings = json.load(f)
            
        webhook_url = settings.get("notification_webhook")
        if not webhook_url:
            return
            
        payload = {
            "content": f"🔔 **YT Playlist Agent**: {message}" if not is_error else f"⚠️ **YT Playlist Agent Error**: {message}"
        }
        
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception as e:
        print(f"Failed to send webhook: {e}")

def run_pipeline_sequence():
    global active_job
    with job_lock:
        if active_job:
            print("Scheduler: Pipeline already running. Skipping scheduled trigger.")
            return
        active_job = "scheduled_pipeline"

    log_path = os.path.join(os.path.dirname(__file__), "agent_run.log")
    
    def log_message(msg):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[Scheduler Pipeline] {timestamp} - {msg}\n")
        print(f"[Scheduler Pipeline] {msg}")

    try:
        # Check if OAuth is configured
        settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
        is_oauth = False
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
                if settings.get("google_client_id") and settings.get("google_client_secret"):
                    is_oauth = True
            except:
                pass

        if is_oauth:
            log_message("OAuth configured. Running API-based multi-user scheduled pipeline...")
            import db_helper
            import yt_api
            import server
            
            # Fetch all users
            conn = db_helper.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id, email FROM users")
            users = [dict(row) for row in cursor.fetchall()]
            conn.close()
            
            for user in users:
                user_id = user["id"]
                email = user["email"]
                log_message(f"Running pipeline sequence for user {user_id} ({email})...")
                
                # 1. API Scan
                log_message(f"Step 1/4: Scanning playlists via API for user {user_id}...")
                try:
                    yt_api.run_api_scan_and_save(user_id)
                except Exception as scan_err:
                    log_message(f"API Scan failed for user {user_id}: {scan_err}")
                
                # 2. Skip Auto Sort (Handled by MISPLACED generation in maintenance)
                log_message(f"Step 2/4: Skipping browser auto-sort (handled by maintenance generation)...")
                
                # 3. Generate Maintenance
                log_message(f"Step 3/4: Generating maintenance plan for user {user_id}...")
                proc = subprocess.run([sys.executable, "generate_maintenance.py", "--user-id", str(user_id)], capture_output=True, text=True)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(proc.stdout)
                    f.write(proc.stderr)
                    
                # 4. Apply Maintenance via API
                log_message(f"Step 4/4: Applying maintenance actions via API for user {user_id}...")
                try:
                    server.execute_apply_maintenance_background(user_id, force=True)
                except Exception as apply_err:
                    log_message(f"API Maintenance Apply failed for user {user_id}: {apply_err}")
            
            log_message("Scheduled pipeline for all users completed successfully!")
        else:
            log_message("Starting scheduled pipeline: scan -> auto_sort -> generate_maintenance -> apply_maintenance")
            
            # 1. Scan
            log_message("Step 1/4: Scanning playlists...")
            proc = subprocess.run([sys.executable, "cli.py", "scan"], capture_output=True, text=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(proc.stdout)
                f.write(proc.stderr)
                
            # 2. Auto Sort
            log_message("Step 2/4: Auto-sorting Watch Later...")
            proc = subprocess.run([sys.executable, "cli.py", "auto-sort"], capture_output=True, text=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(proc.stdout)
                f.write(proc.stderr)
                
            # 3. Generate Maintenance
            log_message("Step 3/4: Generating maintenance plan...")
            proc = subprocess.run([sys.executable, "generate_maintenance.py"], capture_output=True, text=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(proc.stdout)
                f.write(proc.stderr)
                
            # 4. Apply Maintenance
            log_message("Step 4/4: Applying maintenance actions...")
            proc = subprocess.run([sys.executable, "apply_maintenance.py", "--force"], capture_output=True, text=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(proc.stdout)
                f.write(proc.stderr)
                
            log_message("Scheduled pipeline completed successfully!")
        
        # Save last run timestamp
        try:
            with open(os.path.join(os.path.dirname(__file__), "last_run.txt"), "w") as lf:
                lf.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except:
            pass
            
        send_webhook_notification("Scheduled playlist maintenance pipeline completed successfully!")
        
    except Exception as e:
        log_message(f"Error in scheduled pipeline: {e}")
        send_webhook_notification(f"Scheduled playlist maintenance pipeline failed: {e}", is_error=True)
    finally:
        with job_lock:
            active_job = None

def run_scheduler_loop():
    print("Scheduler thread started: Checking daily at 5:00 PM and 11:00 PM...")
    last_scheduled_hour = -1
    last_scheduled_day = -1
    
    while True:
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        current_day = now.day
        
        # 17 = 5 PM, 23 = 11 PM
        if current_hour in [17, 23] and current_minute == 0:
            if last_scheduled_hour != current_hour or last_scheduled_day != current_day:
                last_scheduled_hour = current_hour
                last_scheduled_day = current_day
                
                print(f"Scheduler: Triggering scheduled run for {current_hour}:00...")
                # Start pipeline in a separate thread so it doesn't block the scheduler loop
                threading.Thread(target=run_pipeline_sequence, daemon=True).start()
                
        time.sleep(30)

def start_scheduler():
    t = threading.Thread(target=run_scheduler_loop, daemon=True)
    t.start()
    return t
