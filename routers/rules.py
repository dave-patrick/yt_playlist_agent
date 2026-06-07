import os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db_helper
from core.utils import (
    is_oauth_configured,
    load_cached_rules,
    invalidate_rules_cache,
    get_current_user
)

router = APIRouter(prefix="/api/rules", tags=["rules"])

# Models
class RulesSaveRequest(BaseModel):
    rules_md: str
    channels_txt: str

class AddChannelRuleRequest(BaseModel):
    channel: str
    category: str


@router.get("")
def get_rules(user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    return load_cached_rules(user_id)

@router.post("")
def save_rules(req: RulesSaveRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    base_dir = os.path.dirname(os.path.dirname(__file__))
    try:
        rules_path = os.path.join(base_dir, "yt_rules.promptinclude.md")
        with open(rules_path, "w", encoding="utf-8") as f:
            f.write(req.rules_md)
            
        if not is_oauth_configured():
            chan_path = os.path.join(base_dir, "yt_category_channel_map.txt")
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
            
        invalidate_rules_cache(user_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/add-channel")
def add_channel_rule(req: AddChannelRuleRequest, user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    try:
        if not is_oauth_configured():
            from apply_maintenance import learn_channel_rule
            learn_channel_rule(req.channel, req.category, is_auto_learned=False)
        else:
            db_helper.save_user_rule(user_id, req.channel, req.category)
        invalidate_rules_cache(user_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
