import json
import os
import time
import uuid
from datetime import datetime
from core import get_browser, add_video_to_playlist, remove_video_from_playlist

def record_history(action, user_id=None):
    if user_id is None:
        history_path = os.path.join(os.path.dirname(__file__), "maintenance_history.json")
    else:
        history_path = os.path.join(os.path.dirname(__file__), f"maintenance_history_{user_id}.json")
        
    history = []
    if os.path.exists(history_path):
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except: pass
        
    action_id = str(uuid.uuid4())[:8]
    history_entry = {
        "action_id": action_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **action
    }
    history.append(history_entry)
    
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
        
    return action_id

def send_discord_history_report(applied_actions):
    settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
    if not os.path.exists(settings_path):
        return
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        webhook_url = settings.get("notification_webhook")
        if not webhook_url:
            return
            
        import requests
        embeds = []
        
        # Max 5 embeds in one message to avoid exceeding Discord limits
        for a in applied_actions[:5]:
            action_id = a["action_id"]
            title = a.get("title", "Unknown Video")
            vid = a.get("vid")
            video_url = f"https://www.youtube.com/watch?v={vid}"
            
            description = ""
            if a["type"] == "MISPLACED":
                description = f"Moved to **{a['to']}** (from **{', '.join(a['from'])}**)\n"
            elif a["type"] in ["DUPLICATE", "DUPLICATE_NO_TARGET"]:
                description = f"Removed duplicate from **{', '.join(a['remove'])}**\n"
                
            # Rollback URL uses local FastAPI server address
            rollback_url = f"http://127.0.0.1:8000/api/maintenance/rollback?action_id={action_id}"
            description += f"[↩️ Rollback Change]({rollback_url})"
            
            embeds.append({
                "title": title,
                "url": video_url,
                "description": description,
                "color": 3447003 if a["type"] == "MISPLACED" else 15158332 # blue/red
            })
            
        payload = {
            "content": f"🔔 **YouTube Playlist Agent Maintenance Report**\nSuccessfully applied {len(applied_actions)} sorting actions.",
            "embeds": embeds
        }
        
        requests.post(webhook_url, json=payload, timeout=5)
        
        # If there are more than 5, send a follow up content
        if len(applied_actions) > 5:
            extra_msg = "**Other applied actions:**\n"
            for a in applied_actions[5:]:
                action_id = a["action_id"]
                title = a.get("title", "Unknown Video")
                rollback_url = f"http://127.0.0.1:8000/api/maintenance/rollback?action_id={action_id}"
                extra_msg += f"- *{title}* (Type: {a['type']}) [↩️ Rollback]({rollback_url})\n"
            
            requests.post(webhook_url, json={"content": extra_msg}, timeout=5)
            
    except Exception as e:
        print(f"Failed to send Discord report: {e}")


def is_in_window():
    # Window is 10 PM (22:00) to 6 AM (06:00)
    now = datetime.now().hour
    return now >= 22 or now < 6


def learn_channel_rule(channel, category):
    if not channel or not category:
        return
    chan_path = os.path.join(os.path.dirname(__file__), "youtube_category_channel_map.txt")
    try:
        lines = []
        if os.path.exists(chan_path):
            with open(chan_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        
        updated = False
        channel_lower = channel.strip().lower()
        for i, line in enumerate(lines):
            if ":" in line:
                ch_part, cat_part = line.split(":", 1)
                if ch_part.strip().lower() == channel_lower:
                    lines[i] = f"{ch_part.strip()}:{category.strip()}\n"
                    updated = True
                    break
                    
        if not updated:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
            lines.append(f"{channel.strip()}:{category.strip()}\n")
            
        with open(chan_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"Auto-learned/updated channel rule: {channel} -> {category}")
    except Exception as e:
        print(f"Failed to auto-learn/update rule: {e}")



def apply_maintenance(filename='maintenance_actions.json', force=False):
    if not os.path.exists(filename):
        print(f"File {filename} not found.")
        return
        
    with open(filename, 'r', encoding='utf-8') as f:
        actions = json.load(f)

    if not actions:
        print("No actions to apply.")
        return

    print(f"Applying {len(actions)} actions...")
    
    applied_actions = []
    # We should use ONE browser session for all moves to be efficient
    driver = get_browser()
    try:
        count = 0
        # Process actions one by one and save progress
        while actions:
            if not force and not is_in_window():
                print("Outside allowed window (10 PM - 6 AM). Stopping.")
                break
            
            a = actions[0]
            count += 1
            print(f"[{count}] Processing: {a['title']}")
            
            vid_url = f"https://www.youtube.com/watch?v={a['vid']}"
            success = False
            
            try:
                if a['type'] == 'MISPLACED':
                    target = a['to']
                    old_ps = a['from']
                    print(f"  Adding to '{target}'...")
                    if add_video_to_playlist(vid_url, target, driver=driver):
                        for old_p in old_ps:
                            print(f"  Removing from '{old_p}'...")
                            try:
                                remove_video_from_playlist(vid_url, old_p, driver=driver)
                            except Exception as re:
                                print(f"    Failed to remove from '{old_p}': {re}")
                        success = True
                    else:
                        print(f"  Failed to add to '{target}', skipping removals.")
                
                elif a['type'] in ['DUPLICATE', 'DUPLICATE_NO_TARGET']:
                    remove_from = a['remove']
                    for p_name in remove_from:
                        print(f"  Removing duplicate from '{p_name}'...")
                        try:
                            remove_video_from_playlist(vid_url, p_name, driver=driver)
                        except Exception as re:
                            print(f"    Failed to remove from '{p_name}': {re}")
                    success = True
            except Exception as action_err:
                print(f"  Error processing action for '{a['title']}': {action_err}")
                success = False
            
            if success:
                act_id = record_history(a)
                print(f"  Action recorded in history with ID: {act_id}")
                applied_actions.append({**a, "action_id": act_id})
                
                # Auto-learn rule
                channel_name = a.get("channel")
                category_name = a.get("to") or a.get("keep")
                if channel_name and category_name:
                    learn_channel_rule(channel_name, category_name)
            else:
                print(f"  Action for '{a['title']}' failed and was skipped.")
                
            # Remove the action (completed or failed) and save progress
            actions.pop(0)
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(actions, f, indent=2)
            
            time.sleep(1)
            
        # Send webhook report at the end of the run
        if applied_actions:
            print(f"Sending Discord report for {len(applied_actions)} applied actions...")
            send_discord_history_report(applied_actions)
            
    finally:
        driver.quit()

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    force = "--force" in args
    if force:
        args.remove("--force")
        
    target_file = args[0] if len(args) > 0 else 'maintenance_actions.json'
    
    # If not forced and outside window, don't even start
    if not force and not is_in_window():
        print("Outside allowed window (10 PM - 6 AM). Use --force to run anyway.")
    else:
        apply_maintenance(target_file, force=force)
