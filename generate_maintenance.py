import sys
import json
import re
import os
from collections import defaultdict

# Global user_id parsing
user_id = None
for arg in sys.argv:
    if arg.startswith("--user-id="):
        user_id = arg.split("=")[1]
    elif arg == "--user-id" and sys.argv.index(arg) + 1 < len(sys.argv):
        user_id = sys.argv[sys.argv.index(arg) + 1]

def parse_rules():
    channel_map = {}
    category_to_id = {}
    
    if user_id:
        try:
            import db_helper
            db_helper.import_default_rules_if_empty(user_id)
            channel_map = db_helper.load_user_rules(user_id)
        except Exception as e:
            print(f"Warning: Could not load user rules from DB: {e}")
    else:
        try:
            with open("yt_category_channel_map.txt", "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        parts = line.strip().split(":")
                        if len(parts) == 2:
                            channel_map[parts[0].strip()] = parts[1].strip()
        except Exception as e:
            print(f"Warning: Could not read channel map: {e}")
            
    try:
        rules_path = os.path.join(os.path.dirname(__file__), "yt_rules.promptinclude.md")
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("|") and "`PL" in line:
                        parts = [p.strip() for p in line.split("|")]
                        if len(parts) >= 4:
                            cat_name = parts[2].strip()
                            category_to_id[cat_name] = True # Just mark it as valid
    except Exception as e:
        print(f"Warning: Could not read rules: {e}")
        
    # Also load from playlists_urls.json if it exists to support all active playlists
    urls_path = f"playlists_report_{user_id}.json" if user_id else "playlists_report.json"
    if os.path.exists(urls_path):
        try:
            with open(urls_path, "r", encoding="utf-8") as f:
                urls = json.load(f)
                for item in urls:
                    name = item.get("name")
                    if name:
                        category_to_id[name] = True
        except Exception as e:
            print(f"Warning: Could not read {urls_path}: {e}")
            
    return channel_map, category_to_id

from ai_classifier import classify_video_with_ai

def load_ai_classifications():
    classifications = {}
    class_path = f"ai_classifications_{user_id}.json" if user_id else "ai_classifications.json"
    if os.path.exists(class_path):
        try:
            with open(class_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for c in data:
                    vid = c.get("vid")
                    if vid:
                        classifications[vid] = c
        except Exception as e:
            print(f"Warning: Could not read {class_path}: {e}")
    return classifications

def get_target_cat(title, channel, channel_map, vid=None, ai_classifications=None, allow_ai=True):
    title_lower = title.lower()
    
    def matches(keywords):
        for k in keywords:
            if " " in k or not k.isalnum():
                if k.lower() in title_lower: return True
            else:
                if re.search(rf"\b{re.escape(k)}\b", title_lower):
                    return True
        return False

    # Mobile (highest priority to prevent AI/Tech/etc. from hijacking Samsung/Galaxy/phones)
    is_tv_or_monitor = matches([" tv ", "television", "qled", "neo qled", "oled", "monitor", "soundbar"])
    is_star_wars = matches(["star wars", "jedi", "sith", "vader", "lightsaber", "galaxy's edge", "galaxy’s edge", "skeleton crew", "yoda", "grogu"])
    is_space = matches(["space", "nasa", "telescope", "hubble", "jwst", "astronomy", "milky way", "universe", "andromeda", "cosmic", "cosmology", "astrophysics", "orbit", "planet", "solar system", "constellation"])
    is_samsung_galaxy = (
        (matches(["samsung"]) or matches(["galaxy"])) and not is_tv_or_monitor and not is_star_wars and not is_space
    )
    if (matches(["xteink", "ereader", "e-reader", "kindle", "boox", "remarkable", "smartphone", "iphone", "pixel 8", "s24 ultra", "android"]) or is_samsung_galaxy) and not is_star_wars and not is_space:
        return "Mobile", False

    # Tana
    if matches(["tana"]): return "Tana", False

    # TV (Trailers, except camper/overland trailers)
    is_camping_trailer = matches(["camper", "camping", "overland", "offroad", "off-road", "off road", "utility", "cargo", "teardrop", "tow", "towing", "rig", "trailer build", "trailer setup", "diy trailer"])
    if matches(["trailer"]) and not is_camping_trailer:
        return "TV", False

    # Truck (must go to Truck and nowhere else - not Auto, and not Overland)
    if matches(["tacoma", "truck", "pickup", "bed divider", "tailgate", "bed rack"]): return "Truck", False
    # Arizona
    if matches(["arizona", "phoenix", " az ", ", az"]): return "Arizona", False
    # Cosplay
    if matches(["cosplay"]): return "Cosplay", False
    # Music Videos
    if matches(["official music video", "official video", "music video", "lyric video", "official audio", "official lyric", "official visualizer", "musicvideo", "lyrics video", "(official)"]): return "Music Videos", False
    # AI
    if matches([" ai ", "gpt", "claude", "gemini", "notebooklm", "llm", "artificial intelligence"]): return "AI", False
    # Star Wars
    if matches(["star wars", "vader", "kenobi", "darth", "jedi", "darth maul", "coruscant", "skywalker", "ahsoka", "mandalorian", "grogu", "yoda", "sith", "galactic empire", "rebel alliance", "lightsaber"]): return "Star Wars", False
    # 3D Printing
    if matches(["3d print", "slicing", "ender 3", "bambu", "voron", "3d printing"]): return "3D Printing Watch", False
    # Woodworking
    if matches(["woodworking", "carpentry", "woodworker"]): return "Woodworking", False
    # Smart Home
    if matches(["smart home", "home assistant", "zigbee", "z-wave", "matter"]): return "Smart Home Stuff", False
    # Aviation
    if matches(["dogfight", "airplane", "airplanes", "plane", "planes", "jet", "jets", "f-22", "f-35", "f-16", "f-15", "f-18", "a-10", "sr-71", "aviation", "aircraft", "cockpit"]): return "Aviation", False
    # Blackstone
    if matches(["blackstone", "griddle", "tortellini"]): return "Blackstone", False
    # Food
    if matches(["food", "cook", "recipe", "delicious", "tasty", "culinary"]): return "Food", False
    # Tech
    if matches(["gadget", "unboxing", "tech review", "hardware"]): return "Tech", False
    # Bigfoot
    if matches(["bigfoot", "sasquatch", "cryptid", "yeti"]): return "Bigfoot", False
    # Auto
    if matches(["v8", "engine", "horsepower", "torque", "car review", "automotive"]): return "Auto", False
    # Football
    if matches([" nfl ", "49ers", "touchdown", "quarterback", "super bowl"]): return "Football", False
    # Overland
    if matches(["overland", "offroad", "4x4", "overlanding"]): return "Overland", False
    # Drones
    if matches([" dji ", "fpv drone", "mavic", "quadcopter"]): return "Drones", False
    
    # Channel map (case-insensitive)
    channel_lower = channel.lower()
    channel_map_lower = {k.lower(): v for k, v in channel_map.items()}
    if channel_lower in channel_map_lower:
        return channel_map_lower[channel_lower], False
        
    # Partial channel match (case-insensitive)
    for ch_key, cat in channel_map.items():
        if ch_key and ch_key.lower() in channel_lower:
            return cat, False
            
    # Check if we have an already approved or corrected AI classification in history
    if ai_classifications and vid in ai_classifications:
        c = ai_classifications[vid]
        status = c.get("status")
        if status in ["approved", "corrected"]:
            return c.get("category"), True
        elif status == "pending":
            # If it's already pending, do NOT call AI again, just return None, False immediately
            return None, False
            
    # AI Fallback (logs to history as pending, but does not execute yet)
    if allow_ai:
        classify_video_with_ai(title, channel, vid=vid, user_id=user_id)
    return None, False

def generate_maintenance():
    report_file = f"playlists_report_{user_id}.json" if user_id else "playlists_report.json"
    if not os.path.exists(report_file):
        print(f"{report_file} not found. Run scan first.")
        return

    with open(report_file, "r", encoding="utf-8") as f:
        playlists = json.load(f)

    channel_map, valid_cats = parse_rules()
    ai_classifications = load_ai_classifications()
    
    manual_moves = {}
    try:
        import db_helper
        manual_moves = db_helper.get_manual_moves(user_id)
    except Exception as e:
        print(f"Warning: Could not load manual moves: {e}")
    
    # Map video ID to list of playlists it's in
    vid_to_playlists = defaultdict(list)
    vid_to_info = {}

    for p in playlists:
        p_name = p['name']
        if p_name == "Watch later": continue # Skip WL for maintenance
        
        for v in p.get('videos', []):
            vid = v['url'].split("v=")[-1].split("&")[0]
            vid_to_playlists[vid].append(p_name)
            if vid not in vid_to_info:
                vid_to_info[vid] = v

    actions = []
    total_vids = len(vid_to_playlists)
    
    progress_file = f"generate_progress_{user_id}.json" if user_id else "generate_progress.json"
    # Write initial progress
    try:
        with open(progress_file, "w") as f:
            json.dump({"current": 0, "total": total_vids}, f)
    except:
        pass
        
    for idx, (vid, p_list) in enumerate(vid_to_playlists.items()):
        # Write progress
        try:
            with open(progress_file, "w") as f:
                json.dump({"current": idx + 1, "total": total_vids}, f)
        except:
            pass
            
        v = vid_to_info[vid]
        title = v['title']
        channel = v['channel']
        
        # Only query AI if the video resides in a general/sorting inbox playlist (Learning, Music, Uncategorized)
        # or if it is in a specific playlist like "Arizona" or "Star Wars" but has no keywords matching that playlist
        is_in_inbox = any(p in ["Learning", "Music", "Uncategorized"] for p in p_list)
        
        target_cat, is_ai = get_target_cat(
            title, channel, channel_map, 
            vid=vid, 
            ai_classifications=ai_classifications, 
            allow_ai=False
        )
        
        if not target_cat and not is_in_inbox:
            current_p = p_list[0]
            if current_p == "Arizona":
                has_kw = any(k in title.lower() for k in ["arizona", "phoenix", " az", ", az"])
                if not has_kw:
                    target_cat, is_ai = get_target_cat(
                        title, channel, channel_map,
                        vid=vid,
                        ai_classifications=ai_classifications,
                        allow_ai=True
                    )
            elif current_p == "Star Wars":
                has_kw = any(k in title.lower() for k in ["star wars", "jedi", "sith", "vader", "lightsaber", "galaxy's edge", "galaxy’s edge", "skeleton crew", "yoda", "grogu"])
                if not has_kw:
                    target_cat, is_ai = get_target_cat(
                        title, channel, channel_map,
                        vid=vid,
                        ai_classifications=ai_classifications,
                        allow_ai=True
                    )
        elif is_in_inbox and not target_cat:
            target_cat, is_ai = get_target_cat(
                title, channel, channel_map, 
                vid=vid, 
                ai_classifications=ai_classifications, 
                allow_ai=True
            )
        
        # 1. Handle Duplicates
        if len(p_list) > 1:
            # Check if manually moved target is in the current playlists
            manually_moved_target = manual_moves.get(vid)
            if manually_moved_target and manually_moved_target in p_list:
                keep = manually_moved_target
                remove = [p for p in p_list if p != keep]
                actions.append({
                    "type": "DUPLICATE",
                    "title": title,
                    "vid": vid,
                    "keep": keep,
                    "remove": remove,
                    "is_ai": is_ai,
                    "channel": channel
                })
            # If it matches a rule, keep the target category version
            elif target_cat and target_cat in p_list:
                keep = target_cat
                remove = [p for p in p_list if p != target_cat]
                actions.append({
                    "type": "DUPLICATE",
                    "title": title,
                    "vid": vid,
                    "keep": keep,
                    "remove": remove,
                    "is_ai": is_ai,
                    "channel": channel
                })
            else:
                # If target_cat doesn't match any of the current playlists,
                # check if there is a general/inbox playlist we can keep it in
                general_matches = [p for p in p_list if p in ["Entertainment", "Learning", "Music", "Uncategorized"]]
                if general_matches:
                    keep = general_matches[0]
                    remove = [p for p in p_list if p != keep]
                else:
                    # Keep the first one, remove others
                    keep = p_list[0]
                    remove = p_list[1:]
                    
                actions.append({
                    "type": "DUPLICATE_NO_TARGET",
                    "title": title,
                    "vid": vid,
                    "keep": keep,
                    "remove": remove,
                    "is_ai": is_ai,
                    "channel": channel
                })
                
        # 2. Handle Misplaced (only if NOT a duplicate we just handled)
        elif target_cat and target_cat not in p_list and target_cat in valid_cats:
            # If manually moved to one of the current playlists, don't move it back
            manually_moved_target = manual_moves.get(vid)
            if manually_moved_target and manually_moved_target in p_list:
                continue
                
            # Only move if it's currently in a general playlist like "Learning" or "Music"
            # or if it's clearly misplaced (e.g. Football in Star Wars)
            current_p = p_list[0]
            
            # High-confidence move: from Learning/Music/Uncategorized to a specific target
            if current_p in ["Learning", "Music", "Uncategorized"] or target_cat != current_p:
                actions.append({
                    "type": "MISPLACED",
                    "title": title,
                    "vid": vid,
                    "from": p_list,
                    "to": target_cat,
                    "is_ai": is_ai,
                    "channel": channel
                })

    # Filter out empty remove lists
    actions = [a for a in actions if a.get('remove') != [] or a.get('type') == 'MISPLACED']

    maint_file = f"maintenance_actions_{user_id}.json" if user_id else "maintenance_actions.json"
    with open(maint_file, "w", encoding="utf-8") as f:
        json.dump(actions, f, indent=2, ensure_ascii=False)
    
    # Clean up progress file
    if os.path.exists(progress_file):
        try:
            os.remove(progress_file)
        except:
            pass
            
    print(f"Generated {len(actions)} maintenance actions.")


if __name__ == "__main__":
    generate_maintenance()
