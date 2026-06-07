import requests
import time
import db_helper

def get_valid_access_token(user_id):
    """
    Retrieves the user's access token from the database. 
    If the token has expired, it uses the refresh token to get a new one from Google.
    """
    creds = db_helper.get_user_credentials(user_id)
    if not creds:
        raise ValueError(f"No credentials found for user {user_id}")
        
    access_token = creds["access_token"]
    refresh_token = creds["refresh_token"]
    token_expiry = creds["token_expiry"]
    
    # If expired or expiring in less than 60 seconds, refresh it
    if token_expiry <= int(time.time()) + 60:
        if not refresh_token:
            raise ValueError(f"Access token expired for user {user_id} and no refresh token is available.")
            
        print(f"Refreshing access token for user {user_id}...")
        settings = db_helper.get_settings()
        client_id = settings.get("google_client_id")
        client_secret = settings.get("google_client_secret")
        
        token_url = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        
        resp = requests.post(token_url, data=payload)
        if not resp.ok:
            raise RuntimeError(f"Failed to refresh OAuth token: {resp.text}")
            
        data = resp.json()
        access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        
        # Save new credentials (reusing existing refresh token if not returned)
        new_refresh = data.get("refresh_token", refresh_token)
        db_helper.save_user_credentials(user_id, access_token, new_refresh, expires_in)
        
    return access_token

def get_headers(user_id):
    token = get_valid_access_token(user_id)
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

def list_user_playlists(user_id):
    """
    Lists the authenticated user's playlists.
    """
    url = "https://www.googleapis.com/youtube/v3/playlists"
    params = {
        "part": "snippet,contentDetails",
        "mine": "true",
        "maxResults": 50
    }
    
    playlists = []
    headers = get_headers(user_id)
    
    while url:
        resp = requests.get(url, headers=headers, params=params)
        if not resp.ok:
            raise RuntimeError(f"Error fetching playlists: {resp.text}")
            
        data = resp.json()
        for item in data.get("items", []):
            playlists.append({
                "id": item["id"],
                "name": item["snippet"]["title"],
                "video_count": item["contentDetails"]["itemCount"]
            })
            
        next_page_token = data.get("nextPageToken")
        if next_page_token:
            params["pageToken"] = next_page_token
        else:
            url = None
            
    return playlists

def list_videos_in_playlist(user_id, playlist_id):
    """
    Lists all videos in a specific playlist.
    """
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": 50
    }
    
    videos = []
    headers = get_headers(user_id)
    
    while url:
        resp = requests.get(url, headers=headers, params=params)
        if not resp.ok:
            raise RuntimeError(f"Error fetching playlist items: {resp.text}")
            
        data = resp.json()
        for item in data.get("items", []):
            snippet = item["snippet"]
            content_details = item["contentDetails"]
            video_id = content_details.get("videoId")
            
            videos.append({
                "playlist_item_id": item["id"], # Required to delete/remove the item
                "id": video_id,
                "title": snippet.get("title", ""),
                "channel": snippet.get("videoOwnerChannelTitle", snippet.get("channelTitle", "Unknown")),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "duration": "" # API duration requires another call; we can leave empty or query if needed
            })
            
        next_page_token = data.get("nextPageToken")
        if next_page_token:
            params["pageToken"] = next_page_token
        else:
            url = None
            
    return videos

def add_video_to_playlist(user_id, playlist_id, video_id):
    """
    Adds a video to a specific playlist.
    """
    url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
    headers = get_headers(user_id)
    payload = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id
            }
        }
    }
    
    resp = requests.post(url, headers=headers, json=payload)
    if not resp.ok:
        raise RuntimeError(f"Error adding video to playlist: {resp.text}")
    return resp.json()

def remove_video_from_playlist(user_id, playlist_item_id):
    """
    Removes a video from a playlist using its playlistItem ID.
    """
    url = f"https://www.googleapis.com/youtube/v3/playlistItems?id={playlist_item_id}"
    headers = get_headers(user_id)
    
    resp = requests.delete(url, headers=headers)
    if not resp.ok:
        raise RuntimeError(f"Error removing video from playlist: {resp.text}")
    return True

def move_video(user_id, source_playlist_item_id, target_playlist_id, video_id):
    """
    Moves a video by adding it to the target playlist and then deleting it from the source.
    """
    add_video_to_playlist(user_id, target_playlist_id, video_id)
    remove_video_from_playlist(user_id, source_playlist_item_id)
    return True

def run_api_scan_and_save(user_id):
    import os
    import json
    playlists = list_user_playlists(user_id)
    report = []
    for p in playlists:
        print(f"Scanning playlist: {p['name']} ({p['id']})...")
        try:
            videos = list_videos_in_playlist(user_id, p["id"])
            report.append({
                "id": p["id"],
                "name": p["name"],
                "url": f"https://www.youtube.com/playlist?list={p['id']}",
                "video_count": len(videos),
                "videos": videos
            })
        except Exception as e:
            print(f"Failed to scan playlist {p['name']}: {e}")
            if "quotaExceeded" in str(e):
                raise e
            
    report_path = os.path.join(os.path.dirname(__file__), f"playlists_report_{user_id}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        
    print(f"Scan complete. Saved {len(playlists)} playlists to {report_path}")
    return report
