import json
import os
import re
import typer
from core import add_video_to_playlist, remove_video_from_playlist, create_playlist, list_videos_in_playlist, get_all_playlists, move_video

app = typer.Typer(help="YT Playlist Agent CLI")

@app.command()
def add(url: str, playlist: str):
    """Add a video to a playlist."""
    typer.echo(f"Adding {url} to '{playlist}'...")
    try:
        added = add_video_to_playlist(url, playlist)
        if added:
            typer.secho("Success! Video added.", fg=typer.colors.GREEN)
        else:
            typer.secho("Video was already in the playlist.", fg=typer.colors.YELLOW)
    except Exception as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

@app.command()
def remove(url: str, playlist: str):
    """Remove a video from a playlist."""
    typer.echo(f"Removing {url} from '{playlist}'...")
    try:
        removed = remove_video_from_playlist(url, playlist)
        if removed:
            typer.secho("Success! Video removed.", fg=typer.colors.GREEN)
        else:
            typer.secho("Video was not in the playlist.", fg=typer.colors.YELLOW)
    except Exception as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

@app.command()
def create(url: str, name: str):
    """Create a new playlist with the given video."""
    typer.echo(f"Creating playlist '{name}' with video {url}...")
    try:
        create_playlist(url, name)
        typer.secho("Success! Playlist created.", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

@app.command()
def list(name: str, json_format: bool = typer.Option(False, "--json", help="Output as JSON")):
    """List all videos in a playlist."""
    if not json_format:
        typer.echo(f"Listing videos in playlist '{name}'...")
    try:
        videos = list_videos_in_playlist(name)
        if json_format:
            print(json.dumps(videos, ensure_ascii=False))
        else:
            if not videos:
                typer.secho("Playlist is empty or no videos found.", fg=typer.colors.YELLOW)
            else:
                typer.secho(f"Found {len(videos)} videos:", fg=typer.colors.GREEN)
                for i, v in enumerate(videos, 1):
                    typer.echo(f"{i}. {v['title']} - {v['url']}")
    except Exception as e:
        if json_format:
            print(json.dumps({"error": str(e)}))
        else:
            typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

@app.command()
def scan():
    """Scan all playlists and their videos."""
    typer.echo("Fetching all playlists...")
    try:
        if os.path.exists("playlists_urls.json"):
            with open("playlists_urls.json", "r", encoding="utf-8") as f:
                playlists = json.load(f)
        else:
            playlists = get_all_playlists()
        typer.secho(f"Found {len(playlists)} playlists. Beginning scan...", fg=typer.colors.GREEN)
        
        report = []
        if os.path.exists("playlists_report.json"):
            with open("playlists_report.json", "r", encoding="utf-8") as f:
                report = json.load(f)
        
        scanned_names = {
            p["name"] for p in report
            if (p.get("video_count", 0) > 0 or p.get("videos"))
            and all("published" in v for v in p.get("videos", []))
        }
        
        for p in playlists:
            if p["name"] in scanned_names:
                typer.echo(f"Skipping '{p['name']}' (already scanned)...")
                continue
                
            typer.echo(f"Scanning '{p['name']}'...")
            
            # Find existing entry in report for this playlist
            existing_p = next((ep for ep in report if ep["url"] == p["url"] or p["url"] in ep["url"]), None)
            existing_videos = existing_p.get("videos", []) if existing_p else []
            
            try:
                videos = list_videos_in_playlist(p["url"])
                
                # Never overwrite real cache with mock data
                is_mock = videos and any('mockvid' in v.get('url', '') or v.get('title', '').startswith('Mock Video') for v in videos)
                if is_mock:
                    typer.secho(f"  Warning: Scraper returned mock videos for '{p['name']}'. Skipping to preserve real cache.", fg=typer.colors.YELLOW)
                    p["videos"] = existing_videos
                    p["video_count"] = len(existing_videos)
                elif not videos and existing_videos:
                    typer.secho(f"  Warning: Scraper returned 0 videos for '{p['name']}', but cache has {len(existing_videos)} videos. Retaining cache to prevent data loss.", fg=typer.colors.YELLOW)
                    p["videos"] = existing_videos
                    p["video_count"] = len(existing_videos)
                else:
                    p["videos"] = videos
                    p["video_count"] = len(videos)
            except Exception as e:
                typer.secho(f"  Error scanning {p['name']}: {e}", fg=typer.colors.RED)
                if existing_videos:
                    typer.secho(f"  Retaining {len(existing_videos)} cached videos on error.", fg=typer.colors.YELLOW)
                    p["videos"] = existing_videos
                    p["video_count"] = len(existing_videos)
                else:
                    p["videos"] = []
                    p["video_count"] = 0
            
            # Update or append in report list to prevent duplicates
            updated = False
            for idx, existing_p in enumerate(report):
                if existing_p["url"] == p["url"] or p["url"] in existing_p["url"]:
                    report[idx] = p
                    updated = True
                    break
            if not updated:
                report.append(p)
            
            # Save incrementally
            with open("playlists_report.json", "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            
        typer.secho(f"\nScan complete! Saved full report to playlists_report.json", fg=typer.colors.GREEN)
        for p in report:
            typer.echo(f"- {p['name']}: {p['video_count']} videos")
            
    except Exception as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

def parse_rules():
    channel_map = {}
    try:
        with open("yt_category_channel_map.txt", "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    parts = line.strip().split(":")
                    if len(parts) == 2:
                        channel_map[parts[0].strip()] = parts[1].strip()
    except Exception as e:
        typer.secho(f"Warning: Could not read channel map: {e}", fg=typer.colors.YELLOW)
        
    category_to_id = {}
    try:
        with open("yt_rules.promptinclude.md", "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("|") and "`PL" in line:
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 4:
                        cat_name = parts[2].strip()
                        list_id_raw = parts[3].strip()
                        match = re.search(r'`(PL[^`]+)`', list_id_raw)
                        if match:
                            category_to_id[cat_name] = match.group(1)
    except Exception as e:
        typer.secho(f"Warning: Could not read rules: {e}", fg=typer.colors.YELLOW)
        
    # Also load from playlists_urls.json if it exists to support all active playlists
    if os.path.exists("playlists_urls.json"):
        try:
            with open("playlists_urls.json", "r", encoding="utf-8") as f:
                urls = json.load(f)
                for item in urls:
                    name = item.get("name")
                    url = item.get("url")
                    if name and url and "list=" in url:
                        list_id = url.split("list=")[1].split("&")[0]
                        category_to_id[name] = list_id
        except Exception as e:
            typer.secho(f"Warning: Could not read playlists_urls.json: {e}", fg=typer.colors.YELLOW)
            
    return channel_map, category_to_id

@app.command()
def auto_sort(input_file: str = None):
    """Scan Watch Later (or use input file) and move videos based on rules."""
    from core import get_browser
    channel_map, category_to_id = parse_rules()
    
    if not channel_map or not category_to_id:
        typer.secho("Missing rules or channel mappings!", fg=typer.colors.RED)
        raise typer.Exit(code=1)
        
    driver = get_browser()
    try:
        if input_file and os.path.exists(input_file):
            typer.echo(f"Loading videos from {input_file}...")
            with open(input_file, 'r', encoding='utf-8') as f:
                videos = json.load(f)
        else:
            typer.echo("Fetching full Watch Later playlist (WL)...")
            videos = list_videos_in_playlist("https://www.youtube.com/playlist?list=WL", driver=driver)
            if not videos:
                typer.secho("Watch Later is empty!", fg=typer.colors.GREEN)
                return
            with open("watch_later_snapshot.json", "w", encoding="utf-8") as f:
                json.dump(videos, f, indent=2, ensure_ascii=False)
            
        # Report channels with multiple videos
        from collections import Counter
        channel_counts = Counter([v['channel'] for v in videos if v.get('channel')])
        multi_channels = {ch: count for ch, count in channel_counts.items() if count > 1}
        
        if multi_channels:
            typer.secho("\nChannels with multiple videos in queue:", fg=typer.colors.CYAN, bold=True)
            for ch, count in sorted(multi_channels.items(), key=lambda x: x[1], reverse=True):
                typer.echo(f"  - {ch}: {count} videos")
            typer.echo("")

        typer.secho(f"Found {len(videos)} videos in Watch Later. Sorting...", fg=typer.colors.GREEN)
        
        def matches(keywords):
            for k in keywords:
                if " " in k or not k.isalnum(): # If keyword has a space or non-alphanumeric, use simple substring match
                    if k.lower() in title_lower: return True
                else: # Otherwise use word boundaries
                    if re.search(rf"\b{re.escape(k)}\b", title_lower):
                        return True
            return False

        for v in videos:
            title = v.get("title", "")
            channel = v.get("channel", "")
            url = v.get("url", "")
            
            target_cat = None
            title_lower = title.lower()
            
            channel_lower = channel.lower()
            channel_map_lower = {k.lower(): v for k, v in channel_map.items()}
            
            # 1. Exact channel mapping (Highest Priority)
            if channel_lower in channel_map_lower:
                target_cat = channel_map_lower[channel_lower]
                
            # 2. Keyword rules
            if not target_cat:
                is_tv_or_monitor = matches([" tv ", "television", "qled", "neo qled", "oled", "monitor", "soundbar"])
                is_star_wars = matches(["star wars", "jedi", "sith", "vader", "lightsaber", "galaxy's edge", "galaxy’s edge", "skeleton crew", "yoda", "grogu"])
                is_space = matches(["space", "nasa", "telescope", "hubble", "jwst", "astronomy", "milky way", "universe", "andromeda", "cosmic", "cosmology", "astrophysics", "orbit", "planet", "solar system", "constellation"])
                is_samsung_galaxy = (
                    (matches(["samsung"]) or matches(["galaxy"])) and not is_tv_or_monitor and not is_star_wars and not is_space
                )
                
                # Rule for Mobile (highest priority to match generate_maintenance.py and avoid hijacking)
                if (matches(["xteink", "ereader", "e-reader", "kindle", "boox", "remarkable", "smartphone", "iphone", "pixel 8", "s24 ultra", "android"]) or is_samsung_galaxy) and not is_star_wars and not is_space:
                    target_cat = "Mobile"
                # Rule for Tana
                elif matches(["tana"]):
                    target_cat = "Tana"
                # Rule 1: Arizona keywords
                elif matches(["arizona", "phoenix", " az ", ", az"]):
                    target_cat = "Arizona"
                # Rule 2: Music videos
                elif matches(["official music video", "official video", "music video", "lyric video", "official audio", "official lyric", "official visualizer", "musicvideo", "lyrics video", "(official)"]):
                    target_cat = "Music Videos"
                # Rule 3: AI keywords
                elif matches([" ai ", "gpt", "claude", "gemini", "notebooklm", "llm", "artificial intelligence"]):
                    target_cat = "AI"
                # Rule 4: Star Wars keywords
                elif matches(["star wars", "vader", "kenobi", "darth", "jedi", "maul", "coruscant", "skywalker", "ahsoka", "mandalorian", "grogu", "yoda", "sith", "lightsaber"]):
                    target_cat = "Star Wars"
                # Rule 6: 3D Printing keywords
                elif matches(["3d print", "slicing", "ender 3", "bambu", "voron", "3d printing"]):
                    target_cat = "3D Printing Watch"
                # Rule 7: Woodworking keywords
                elif matches(["woodworking", "carpentry", "woodworker"]):
                    target_cat = "Woodworking"
                # Rule 8: Smart Home keywords
                elif matches(["smart home", "home assistant", "zigbee", "z-wave", "matter"]):
                    target_cat = "Smart Home"
                # Rule 9: Aviation keywords
                elif matches(["dogfight", "airplane", " jet ", "f-22", "f-35", "f-16", "aviation", "aircraft"]):
                    target_cat = "Aviation"
                # Rule 10: Blackstone keywords
                elif matches(["blackstone", "griddle", "tortellini"]):
                    target_cat = "Blackstone"
                # Rule 11: Food keywords
                elif matches(["food", "cook", "recipe", "delicious", "tasty", "culinary"]):
                    target_cat = "Food"
                # Rule 13: Tech / Gadget keywords
                elif matches(["gadget", "unboxing", "tech review", "hardware"]):
                    target_cat = "Tech"
                # Rule 14: Bigfoot keywords
                elif matches(["bigfoot", "sasquatch", "cryptid", "yeti"]):
                    target_cat = "Bigfoot"
                # Rule 15: Auto keywords
                elif matches(["v8", "engine", "horsepower", "torque", "car review", "automotive"]):
                    target_cat = "Auto"
                # Rule 16: Football keywords
                elif matches([" nfl ", "49ers", "touchdown", "quarterback", "super bowl"]):
                    target_cat = "Football"
                # Rule 17: Overland keywords
                elif matches(["overland", "offroad", "4x4", "overlanding"]):
                    target_cat = "Overland"
                # Rule 18: Drones keywords
                elif matches([" dji ", "fpv drone", "mavic", "quadcopter"]):
                    target_cat = "Drones"

            # 3. Partial channel mapping (Lowest Priority)
            if not target_cat:
                for ch_key, cat in channel_map.items():
                    if ch_key and ch_key.lower() in channel_lower:
                        target_cat = cat
                        break
            
            if target_cat and target_cat in category_to_id:
                typer.echo(f"Moving '{title}' to {target_cat}...")
                try:
                    move_video(url, "Watch Later", target_cat, driver=driver)
                except Exception as e:
                    typer.secho(f"  Failed: {e}", fg=typer.colors.RED)
            else:
                channel_map_lower = {k.lower(): v for k, v in channel_map.items()}
                channel_lower = channel.lower()
                has_matched = (channel_lower in channel_map_lower)
                if not has_matched:
                    for ch_key in channel_map:
                        if ch_key and ch_key.lower() in channel_lower:
                            has_matched = True
                            break
                reason = f"No rule matched for channel '{channel}'" if not has_matched else f"Category '{target_cat}' not in playlist rules"
                typer.secho(f"Skipping '{title}': {reason}", fg=typer.colors.YELLOW)
                # Log unknown channels to a file for review
                if channel and not has_matched:
                    with open("pending_channels.txt", "a", encoding="utf-8") as pf:
                        pf.write(f"{channel}\n")
                
        typer.secho("Auto-sort complete!", fg=typer.colors.GREEN)
    finally:
        driver.quit()

if __name__ == "__main__":
    app()
