from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import secrets
import logging
from pydantic import BaseModel
from typing import Dict, Any, List, Optional
import uvicorn
from dotenv import load_dotenv
import aiofiles
import time
from datetime import datetime, date, timedelta
import asyncio
import json

# Load env
load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config - Only essential items in .env
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", secrets.token_urlsafe(32))
LLM_API_URL = os.getenv("LLM_API_URL", "https://a8c4cosco0wc0gg8s40w8kco.vps.boomlive.in/query")
WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# App credentials (required for token refresh)
WHATSAPP_APP_ID = os.getenv("WHATSAPP_APP_ID")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")

# Initial access token (the only token needed in .env)
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")

# Dynamic token management - no manual configuration needed
TOKEN_EXPIRES_AT: Optional[datetime] = None
LONG_LIVED_TOKEN: Optional[str] = None

# In-memory deduplication store
processed_messages: Dict[str, float] = {}
MESSAGE_EXPIRY = 600  # seconds

# Token refresh lock to prevent concurrent refreshes
token_refresh_lock = asyncio.Lock()

# FastAPI setup
app = FastAPI(title="WhatsApp Webhook for LLM API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class WebhookRequest(BaseModel):
    object: str
    entry: List[Dict[str, Any]]

async def get_token_info(token: str) -> Optional[Dict]:
    """Get token information including expiration time"""
    try:
        url = f"{WHATSAPP_API_URL}/debug_token"
        params = {
            "input_token": token,
            "access_token": token  # Use the same token to debug itself
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            
        if response.status_code == 200:
            data = response.json()
            if data.get("data"):
                return data["data"]
        else:
            logger.warning(f"Token debug failed: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Error getting token info: {e}")
    
    return None

async def detect_token_expiration():
    """Automatically detect when the current token expires"""
    global TOKEN_EXPIRES_AT
    
    if not ACCESS_TOKEN:
        return False
        
    token_info = await get_token_info(ACCESS_TOKEN)
    if token_info:
        expires_at = token_info.get("expires_at")
        if expires_at:
            TOKEN_EXPIRES_AT = datetime.fromtimestamp(expires_at)
            logger.info(f"Detected token expiration: {TOKEN_EXPIRES_AT}")
            return True
        else:
            # Token doesn't expire (app access token)
            TOKEN_EXPIRES_AT = datetime.now() + timedelta(days=365)
            logger.info("Token appears to be long-lived or permanent")
            return True
    
    # If we can't detect expiration, assume it expires in 1 hour (safe default)
    TOKEN_EXPIRES_AT = datetime.now() + timedelta(hours=1)
    logger.warning("Could not detect token expiration, assuming 1 hour")
    return False

async def generate_long_lived_token() -> Optional[str]:
    """Generate a long-lived token from the current short-lived token"""
    global LONG_LIVED_TOKEN
    
    if not ACCESS_TOKEN or not WHATSAPP_APP_ID or not WHATSAPP_APP_SECRET:
        return None
        
    try:
        url = f"{WHATSAPP_API_URL}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": WHATSAPP_APP_ID,
            "client_secret": WHATSAPP_APP_SECRET,
            "fb_exchange_token": ACCESS_TOKEN
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            
        if response.status_code == 200:
            data = response.json()
            LONG_LIVED_TOKEN = data.get("access_token")
            logger.info("Successfully generated long-lived token")
            return LONG_LIVED_TOKEN
        else:
            logger.warning(f"Long-lived token generation failed: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Error generating long-lived token: {e}")
    
    return None

async def refresh_access_token() -> bool:
    """Refresh the WhatsApp access token automatically"""
    global ACCESS_TOKEN, TOKEN_EXPIRES_AT
    
    async with token_refresh_lock:
        # Check if token was already refreshed by another request
        if TOKEN_EXPIRES_AT and datetime.now() < TOKEN_EXPIRES_AT - timedelta(minutes=5):
            return True
            
        logger.info("Refreshing WhatsApp access token...")
        
        try:
            # Method 1: Use long-lived token if available
            if LONG_LIVED_TOKEN:
                success = await _refresh_with_long_lived_token()
                if success:
                    return True
            
            # Method 2: Generate long-lived token and use it
            if not LONG_LIVED_TOKEN and ACCESS_TOKEN:
                logger.info("Generating long-lived token for future refreshes...")
                await generate_long_lived_token()
                if LONG_LIVED_TOKEN:
                    success = await _refresh_with_long_lived_token()
                    if success:
                        return True
            
            # Method 3: Use app credentials
            success = await _refresh_with_app_credentials()
            if success:
                return True
                
        except Exception as e:
            logger.error(f"Token refresh exception: {e}")
            
        return False

async def _refresh_with_long_lived_token() -> bool:
    """Refresh using long-lived token"""
    global ACCESS_TOKEN, TOKEN_EXPIRES_AT
    
    try:
        url = f"{WHATSAPP_API_URL}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": WHATSAPP_APP_ID,
            "client_secret": WHATSAPP_APP_SECRET,
            "fb_exchange_token": LONG_LIVED_TOKEN
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            
        if response.status_code == 200:
            data = response.json()
            ACCESS_TOKEN = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            TOKEN_EXPIRES_AT = datetime.now() + timedelta(seconds=expires_in)
            
            logger.info(f"Token refreshed with long-lived token, expires at: {TOKEN_EXPIRES_AT}")
            return True
        else:
            logger.error(f"Long-lived token refresh failed: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Long-lived token refresh error: {e}")
    
    return False

async def _refresh_with_app_credentials() -> bool:
    """Refresh using app credentials"""
    global ACCESS_TOKEN, TOKEN_EXPIRES_AT
    
    try:
        url = f"{WHATSAPP_API_URL}/oauth/access_token"
        params = {
            "client_id": WHATSAPP_APP_ID,
            "client_secret": WHATSAPP_APP_SECRET,
            "grant_type": "client_credentials"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            
        if response.status_code == 200:
            data = response.json()
            ACCESS_TOKEN = data.get("access_token")
            TOKEN_EXPIRES_AT = datetime.now() + timedelta(days=365)  # App tokens are long-lived
            
            logger.info("Refreshed with app credentials")
            return True
        else:
            logger.error(f"App credentials refresh failed: {response.status_code}")
            
    except Exception as e:
        logger.error(f"App credentials refresh error: {e}")
    
    return False

async def ensure_valid_token() -> bool:
    """Ensure we have a valid token, refresh if necessary"""
    # First time setup - detect expiration
    if not TOKEN_EXPIRES_AT and ACCESS_TOKEN:
        await detect_token_expiration()
    
    # Check if token needs refresh
    if not TOKEN_EXPIRES_AT or datetime.now() >= TOKEN_EXPIRES_AT - timedelta(minutes=5):
        logger.info("Token expired or expiring soon, refreshing...")
        success = await refresh_access_token()
        if not success:
            logger.error("Failed to refresh token!")
            return False
    
    return True

@app.get("/")
async def root():
    return {"message": "Webhook running", "verify_token": VERIFY_TOKEN}

@app.get("/webhook")
async def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)

def cleanup_old_messages():
    """Remove expired message IDs."""
    now = time.time()
    expired = [mid for mid, ts in processed_messages.items() if now - ts > MESSAGE_EXPIRY]
    for mid in expired:
        processed_messages.pop(mid, None)
    if expired:
        logger.info(f"Cleaned up {len(expired)} expired IDs")

@app.post("/webhook")
async def webhook_handler(req: WebhookRequest):
    if req.object != "whatsapp_business_account":
        return Response(status_code=404)

    # Ensure we have a valid token before processing
    if not await ensure_valid_token():
        logger.error("Cannot process webhook - invalid token")
        return Response(status_code=500)

    cleanup_old_messages()

    for entry in req.entry:
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            data = change.get("value", {})
            messages = data.get("messages", [])

            for msg in messages:
                sender = msg.get("from")
                msg_id = msg.get("id")

                # skip if from business
                if sender == PHONE_NUMBER_ID:
                    continue

                # dedupe
                if msg_id in processed_messages:
                    logger.info(f"Skipping duplicate: {msg_id}")
                    continue
                processed_messages[msg_id] = time.time()

                # mark read asynchronously
                asyncio.create_task(send_read_receipt(msg_id))

                # dispatch asynchronously

                mtype = msg.get("type")
                print("****************************************************************")
                print(f"Received message from {sender}: {msg_id} type={mtype}")
                print("****************************************************************")

                if mtype == "text":
                    text = msg["text"]["body"]
                    asyncio.create_task(process_message(sender, text))
                elif mtype in ["image","audio","video"]:
                    media = msg[mtype]
                    content = await fetch_media_url(media.get("id"))
                    print(f"Media content fetched: {len(content)} bytes", content)
                    if content:
                        text = await extract_text_from_media(content)
                        asyncio.create_task(process_message(sender, text))

    return {"status": "success"}

async def send_read_receipt(message_id: str):
    if not await ensure_valid_token():
        logger.error("Cannot send read receipt - invalid token")
        return
        
    url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"}
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 401:
            logger.warning("Read receipt failed with 401, attempting token refresh...")
            if await refresh_access_token():
                headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
                resp = await client.post(url, headers=headers, json=payload)
        
        if resp.status_code != 200:
            text = resp.text
            logger.error(f"Read receipt failed: {resp.status_code} {text}")

async def process_message(to: str, text: str):
    try:
        logger.info(f"LLM call for {to}: {text[:30]}...")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(LLM_API_URL, params={"question": text, "thread_id": f"{date.today().isoformat()}_{to}"})
        if r.status_code == 200:
            reply = r.json().get("response", "No response")
        else:
            reply = "Sorry, error processing your request."
            logger.error(f"LLM error {r.status_code}: {r.text}")
        await send_whatsapp_message(to, reply)
    except Exception:
        logger.exception("process_message exception")
        try:
            await send_whatsapp_message(to, "Sorry, I encountered an error processing your request.")
        except Exception as send_exc:
            logger.error(f"Failed to send error message: {send_exc}")

async def fetch_media_url(media_id: str) -> bytes:
    if not await ensure_valid_token():
        logger.error("Cannot fetch media - invalid token")
        return b""
        
    url = f"{WHATSAPP_API_URL}/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 401:
            logger.warning("Media fetch failed with 401, attempting token refresh...")
            if await refresh_access_token():
                headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
                r = await client.get(url, headers=headers)
                
        if r.status_code != 200:
            logger.error(f"Media fetch failed: {r.status_code}")
            return b""
            
        media_url = r.json().get("url")
        r2 = await client.get(media_url, headers=headers)
        return r2.content if r2.status_code == 200 else b""

async def extract_text_from_media(content: bytes) -> str:
    tmp = f"temp_{secrets.token_hex(8)}"
    async with aiofiles.open(tmp, "wb") as f:
        await f.write(content)
    async with aiofiles.open(tmp, "rb") as f:
        data = await f.read()
    url = "https://k8ccccwccggk4gc4c0o4ggkg.vps.boomlive.in/media/process_input"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, files={"file": (tmp, data)})
    os.remove(tmp)
    return r.json().get("extracted_text", "") if r.status_code == 200 else ""

async def send_whatsapp_message(to: str, body: str):
    if not await ensure_valid_token():
        logger.error("Cannot send message - invalid token")
        return
        
    url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": body, "preview_url": False}
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code == 401:
            logger.warning("Message send failed with 401, attempting token refresh...")
            if await refresh_access_token():
                headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
                r = await client.post(url, headers=headers, json=payload)
        
        if r.status_code != 200:
            text = r.text
            logger.error(f"send_message failed: {r.status_code} {text}")

@app.get("/token")
async def get_token():
    return {"verify_token": VERIFY_TOKEN}

@app.get("/refresh-token")
async def manual_refresh_token():
    """Manually trigger token refresh"""
    success = await refresh_access_token()
    return {
        "success": success,
        "current_token": ACCESS_TOKEN[:20] + "..." if ACCESS_TOKEN else None,
        "expires_at": TOKEN_EXPIRES_AT.isoformat() if TOKEN_EXPIRES_AT else None,
        "has_long_lived_token": bool(LONG_LIVED_TOKEN)
    }

@app.get("/token-status")
async def token_status():
    """Check current token status - all dynamic detection"""
    # Ensure we have current token info
    if not TOKEN_EXPIRES_AT and ACCESS_TOKEN:
        await detect_token_expiration()
    
    now = datetime.now()
    is_valid = TOKEN_EXPIRES_AT and now < TOKEN_EXPIRES_AT
    time_until_expiry = None
    
    if TOKEN_EXPIRES_AT:
        time_until_expiry = (TOKEN_EXPIRES_AT - now).total_seconds()
    
    # Get additional token info
    token_info = None
    if ACCESS_TOKEN:
        token_info = await get_token_info(ACCESS_TOKEN)
    
    return {
        "has_token": bool(ACCESS_TOKEN),
        "expires_at": TOKEN_EXPIRES_AT.isoformat() if TOKEN_EXPIRES_AT else None,
        "is_valid": is_valid,
        "seconds_until_expiry": time_until_expiry,
        "needs_refresh": not is_valid or (time_until_expiry and time_until_expiry < 300),
        "has_long_lived_token": bool(LONG_LIVED_TOKEN),
        "token_info": token_info,
        "auto_detected": True  # Everything is auto-detected
    }

@app.get("/status")
async def status():
    token_info = await token_status()
    return {
        "processed_messages": len(processed_messages),
        "server_time": datetime.now().isoformat(),
        "token_status": token_info
    }

# Background task to periodically check and refresh token
async def token_refresh_background_task():
    """Background task that runs every 30 minutes to check token expiry"""
    while True:
        try:
            await asyncio.sleep(1800)  # 30 minutes
            if not await ensure_valid_token():
                logger.error("Background token refresh failed")
        except Exception as e:
            logger.error(f"Background token refresh error: {e}")

@app.on_event("startup")
async def startup_event():
    """Initialize everything dynamically on startup"""
    logger.info("Starting WhatsApp webhook with dynamic token management...")
    
    # Auto-detect token expiration
    if ACCESS_TOKEN:
        await detect_token_expiration()
        logger.info("Token expiration auto-detected")
    
    # Generate long-lived token for future refreshes
    if not LONG_LIVED_TOKEN and ACCESS_TOKEN:
        await generate_long_lived_token()
        if LONG_LIVED_TOKEN:
            logger.info("Long-lived token generated for automatic refreshes")
    
    # Ensure token is valid
    await ensure_valid_token()
    
    # Start background monitoring
    asyncio.create_task(token_refresh_background_task())
    logger.info("Dynamic token management initialized successfully")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT",8000)), reload=True)