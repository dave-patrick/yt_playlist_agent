import os
import json
import re
import requests
from datetime import datetime

def get_valid_categories():
    categories = []
    rules_path = os.path.join(os.path.dirname(__file__), "youtube_rules.promptinclude.md")
    if not os.path.exists(rules_path):
        return categories
        
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("|") and "`PL" in line:
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 4:
                        cat_name = parts[2].strip()
                        if cat_name not in categories:
                            categories.append(cat_name)
    except Exception as e:
        print(f"Error parsing categories from rules: {e}")
        
    # Also load from playlists_urls.json if it exists
    urls_path = os.path.join(os.path.dirname(__file__), "playlists_urls.json")
    if os.path.exists(urls_path):
        try:
            with open(urls_path, "r", encoding="utf-8") as f:
                urls = json.load(f)
                for item in urls:
                    name = item.get("name")
                    if name and name not in categories:
                        categories.append(name)
        except Exception as e:
            print(f"Error parsing categories from playlists_urls.json: {e}")
            
    return categories

AI_DISABLED = False

def classify_video_with_ai(title: str, channel: str, description: str = "", vid: str = None, user_id: str = None) -> str:
    global AI_DISABLED
    if AI_DISABLED:
        return None
        
    # 1. Load API key from settings
    settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
    if not os.path.exists(settings_path):
        print("AI Classifier: settings.json not found.")
        return None
        
    try:
        with open(settings_path, "r") as f:
            settings = json.load(f)
        api_key = settings.get("gemini_api_key")
    except Exception as e:
        print(f"AI Classifier: Failed to load API key: {e}")
        return None
        
    if not api_key:
        print("AI Classifier: Gemini API key is empty.")
        return None
        
    # 2. Get valid categories
    categories = get_valid_categories()
    if not categories:
        print("AI Classifier: No target categories found.")
        return None
        
    # 3. Read rules details for prompt guidance
    rules_text = ""
    try:
        rules_path = os.path.join(os.path.dirname(__file__), "youtube_rules.promptinclude.md")
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as rf:
                rules_text = rf.read()
    except Exception as re:
        print(f"AI Classifier: Failed to read rules file for prompt: {re}")

    # 4. Build prompt
    categories_str = ", ".join([f"'{c}'" for c in categories])
    prompt = f"""You are an intelligent assistant classifying YouTube videos into a user's existing playlists.

Here are the specific rules and category descriptions defined by the user:
{rules_text}

Valid Playlists:
{categories_str}

Please classify the following video:
Video Title: {title}
Channel Name: {channel}
Description: {description}

Rules:
- The category MUST be chosen from the list of Valid Playlists above. It must match the casing exactly.
- Make sure to respect the specific guidelines and exclusions defined in the rules section above (for example: Samsung Galaxy watches, phones, buds, or other wearables go to "Mobile", not "Tech").
- If the video does not clearly fit into any of the categories, set "category" to null.
- Provide a confidence score between 0.0 and 1.0. Only assign a category if you are highly confident (>= 0.8).

Return a JSON object in this format:
{{
  "category": "Selected Category Name" or null,
  "confidence": 0.95,
  "reasoning": "Why it belongs to this playlist"
}}
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    import time
    
    max_retries = 3
    response = None
    for attempt in range(max_retries):
        try:
            print(f"AI Classifier: Prompting Gemini to classify '{title}' by '{channel}' (attempt {attempt+1})...")
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            
            if response.status_code == 429:
                retry_delay = 15
                try:
                    res_json = response.json()
                    error_details = res_json.get("error", {}).get("details", [])
                    for d in error_details:
                        if "retryDelay" in d:
                            delay_str = d.get("retryDelay", "15s").replace("s", "")
                            retry_delay = float(delay_str) + 1.0
                            break
                except: pass
                print(f"AI Classifier: Rate limit (429) hit. Sleeping for {retry_delay}s before retry...")
                time.sleep(retry_delay)
                continue
                
            if response.status_code != 200:
                print(f"AI Classifier: Gemini API returned status {response.status_code}: {response.text}")
                if response.status_code in [400, 403]:
                    print("AI Classifier: Disabling subsequent AI calls during this process execution due to authentication/API key failure.")
                    AI_DISABLED = True
                return None
                
            # If we get here, status is 200, so break the retry loop
            break
        except Exception as conn_err:
            print(f"AI Classifier: Connection attempt {attempt+1} failed: {conn_err}")
            if attempt == max_retries - 1:
                return None
            time.sleep(2)
            
    if not response or response.status_code != 200:
        return None
        
    try:
        res_data = response.json()
        text_response = res_data['candidates'][0]['content']['parts'][0]['text']
        result = json.loads(text_response.strip())
        
        # Track AI Cache Hits
        try:
            cache_file = os.path.join(os.path.dirname(__file__), "ai_cache_hits.txt")
            hits = 0
            if os.path.exists(cache_file):
                with open(cache_file, "r") as f:
                    hits = int(f.read().strip() or "0")
            with open(cache_file, "w") as f:
                f.write(str(hits + 1))
        except:
            pass
            
        confidence = result.get("confidence", 0.0)
        category = result.get("category")
        reasoning = result.get("reasoning", "")
        
        print(f"AI Classifier Result: category='{category}', confidence={confidence}, reasoning='{reasoning}'")
        
        if category and confidence >= 0.8:
            if category in categories:
                # Write to ai_classifications.json
                if vid:
                    try:
                        filename = f"ai_classifications_{user_id}.json" if user_id else "ai_classifications.json"
                        class_path = os.path.join(os.path.dirname(__file__), filename)
                        history = []
                        if os.path.exists(class_path):
                            with open(class_path, "r", encoding="utf-8") as f:
                                history = json.load(f)
                        
                        # Prevent duplicates
                        if not any(item.get("vid") == vid for item in history):
                            history.append({
                                "vid": vid,
                                "title": title,
                                "channel": channel,
                                "category": category,
                                "confidence": confidence,
                                "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                "status": "pending"
                            })
                            with open(class_path, "w", encoding="utf-8") as f:
                                json.dump(history, f, indent=2, ensure_ascii=False)
                    except Exception as hist_err:
                        print(f"AI Classifier: Failed to log classification: {hist_err}")
                return category
            else:
                print(f"AI Classifier: Predicted category '{category}' is not in valid list.")
        return None
    except Exception as e:
        print(f"AI Classifier: API call failed: {e}")
        return None

if __name__ == "__main__":
    # Test classifier if API key is present
    cat = classify_video_with_ai("DeepMind AlphaFold 3: Protein Structures and Interaction", "Google DeepMind")
    print(f"Test result: {cat}")
