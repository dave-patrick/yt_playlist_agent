import os
import uuid
import shutil
import requests
from typing import Optional
from fastapi import APIRouter, Cookie, Depends, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db_helper
from core.utils import (
    is_oauth_configured,
    get_settings,
    get_redirect_uri,
    get_current_user
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.get("/config-check")
def auth_config_check():
    return {"configured": is_oauth_configured()}

@router.get("/login")
def auth_login(request: Request):
    settings = get_settings()
    client_id = settings.get("google_client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail="Google Client ID is not configured in settings.json")
        
    redirect_uri = get_redirect_uri(request)
    scopes = "openid email profile https://www.googleapis.com/auth/youtube"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scopes}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return RedirectResponse(auth_url)

@router.get("/callback")
def auth_callback(request: Request, code: str):
    settings = get_settings()
    client_id = settings.get("google_client_id")
    client_secret = settings.get("google_client_secret")
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="OAuth credentials not fully configured")
        
    redirect_uri = get_redirect_uri(request)
    
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }
    
    resp = requests.post(token_url, data=payload)
    if not resp.ok:
        return HTMLResponse(content=f"<h1>Authentication Failed</h1><p>{resp.text}</p>", status_code=400)
        
    tokens = resp.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    
    userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    userinfo_resp = requests.get(userinfo_url, headers=headers)
    if not userinfo_resp.ok:
        return HTMLResponse(content="<h1>Failed to fetch user info from Google</h1>", status_code=400)
        
    user_info = userinfo_resp.json()
    email = user_info.get("email")
    name = user_info.get("name", email)
    
    if not email:
        return HTMLResponse(content="<h1>Google account did not return an email address</h1>", status_code=400)
        
    user_id = db_helper.get_or_create_user(email, name)
    db_helper.save_user_credentials(user_id, access_token, refresh_token, expires_in)
    
    db_helper.import_default_rules_if_empty(user_id)
    
    session_id = str(uuid.uuid4())
    db_helper.create_session(session_id, user_id)
    
    response = RedirectResponse(url="/")
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=86400 * 30,
        samesite="lax",
        secure=False
    )
    return response

@router.get("/demo")
def auth_demo(request: Request):
    email = "demo@user.com"
    name = "Demo Reviewer"
    user_id = db_helper.get_or_create_user(email, name)
    
    db_helper.save_user_credentials(user_id, "mock_access_token", "mock_refresh_token", 3600)
    db_helper.import_default_rules_if_empty(user_id)
    
    base_dir = os.path.dirname(os.path.dirname(__file__))
    for base_file in ["playlists_report.json", "maintenance_actions.json", "ai_classifications.json", "categorized_playlists.json", "ai_cache_hits.txt"]:
        src = os.path.join(base_dir, base_file)
        if os.path.exists(src):
            base, ext = os.path.splitext(base_file)
            dest = os.path.join(base_dir, f"{base}_{user_id}{ext}")
            try:
                shutil.copy2(src, dest)
            except Exception as e:
                print(f"Error copying {base_file} for demo user: {e}")
                
    session_id = str(uuid.uuid4())
    db_helper.create_session(session_id, user_id)
    
    response = RedirectResponse(url="/")
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=86400 * 30,
        samesite="lax",
        secure=False
    )
    return response

@router.post("/logout")
def auth_logout(response: Response, session_id: Optional[str] = Cookie(None)):
    if session_id:
        db_helper.delete_session(session_id)
    response = JSONResponse(content={"success": True})
    response.delete_cookie("session_id")
    return response

@router.get("/session")
def auth_session(user=Depends(get_current_user)):
    user_id = user.get("user_id") if "user_id" in user else user.get("id")
    return {
        "logged_in": True if is_oauth_configured() else False,
        "local_mode": not is_oauth_configured(),
        "user_id": user_id,
        "email": user.get("email"),
        "name": user.get("name")
    }
