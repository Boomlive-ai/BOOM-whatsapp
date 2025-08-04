from fastapi import FastAPI, Request, Response, HTTPException
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
from twitter_service import TwitterService
from chatbot_metrics import metrics_service
import time
from fastapi.staticfiles import StaticFiles


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
# Initialize the Twitter service (add this after your other initializations)
twitter_service = TwitterService(
    twitter_bearer_token=os.getenv("TWITTER_BEARER_TOKEN")  # Add this to your .env file
)
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
if not os.path.exists("media"):
    os.makedirs("media")
app.mount("/media", StaticFiles(directory="media"), name="media")

import uuid
import pathlib
from typing import Optional

async def download_and_host_media(media_id: str) -> Optional[str]:
    """
    1) Fetch the WhatsApp media URL
    2) Download the binary (with auth header!)
    3) Save as media/{uuid}.{ext}
    4) Return the hosted URL (/media/...)
    """
    async with httpx.AsyncClient() as client:
        # 1️⃣ get the ephemeral URL
        url_resp = await client.get(
            f"{WHATSAPP_API_URL}/{media_id}",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )
        if url_resp.status_code == 401:
            await refresh_access_token()
            url_resp = await client.get(
                f"{WHATSAPP_API_URL}/{media_id}",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
            )
        if url_resp.status_code != 200:
            logger.error(f"Failed to fetch media URL: {url_resp.status_code}")
            return None

        media_url = url_resp.json().get("url")
        if not media_url:
            logger.error("No URL field in WhatsApp media response")
            return None

        # 2️⃣ download the binary **with** the auth header
        data_resp = await client.get(media_url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        if data_resp.status_code == 401:
            # try refreshing once more
            await refresh_access_token()
            data_resp = await client.get(media_url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        if data_resp.status_code != 200:
            logger.error(f"Failed to download media: {data_resp.status_code}")
            return None

    # 3️⃣ build a filename with correct extension
    content_type = data_resp.headers.get("Content-Type", "")
    ext = content_type.split("/")[-1] or "bin"
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = pathlib.Path("media") / filename

    # 4️⃣ save to disk
    path.write_bytes(data_resp.content)

    # 5️⃣ return the public URL
    return f"/media/{filename}"

class WebhookRequest(BaseModel):
    object: str
    entry: List[Dict[str, Any]]
    
    
# async def analyze_image_url(image_url: str) -> Optional[str]:
#     """
#     Call your boomlive analyze-url endpoint and return the 'lens_context.context' string.
#     """
#     # params = {"image_url": image_url}
#     try:
#         print("Image URL is", image_url)
#         api = f"https://jscw8gocc0k4s00gkcskcokc.vps.boomlive.in/analyze-url/?image_url={image_url}"

#         print("API URL: ", api)
#         async with httpx.AsyncClient() as client:
#             resp = await client.post(api,  headers={"accept": "application/json"})
#         resp.raise_for_status()
#         body = resp.json()
#         print("RESPONSE:", body)
#         result = body.get("lens_context", {}).get("context")
#         print("Result: ",result)
#         return body.get("lens_context", {}).get("context")
#     except Exception as e:
#         logger.error(f"analyze_image_url failed for {image_url}: {e}")
#         return None

async def analyze_image_url(image_url: str) -> Optional[str]:
    """
    Call your boomlive analyze-url endpoint and return the 'lens_context.context' string.
    """
    try:
        print(f"=== analyze_image_url DEBUG START ===")
        print(f"Image URL: {image_url}")
        
        api = f"https://jscw8gocc0k4s00gkcskcokc.vps.boomlive.in/analyze-url/?image_url={image_url}"
        print(f"API URL: {api}")
        
        print("Creating HTTP client...")
        async with httpx.AsyncClient(timeout=60.0) as client:
            print("Making POST request...")
            resp = await client.post(api, headers={"accept": "application/json"})
            
            print(f"Response received:")
            print(f"  Status code: {resp.status_code}")
            print(f"  Headers: {dict(resp.headers)}")
            print(f"  Content length: {len(resp.content)} bytes")
            print(f"  Raw content (first 1000 chars): {resp.text[:1000]}")
            
            if resp.status_code != 200:
                print(f"ERROR: Non-200 status code: {resp.status_code}")
                print(f"ERROR: Response text: {resp.text}")
                return None
            
            print("Parsing JSON response...")
            try:
                body = resp.json()
                print(f"JSON parsed successfully: {body}")
            except Exception as json_e:
                print(f"JSON parsing failed: {type(json_e).__name__}: {json_e}")
                print(f"Raw response that failed to parse: {resp.text}")
                return None
            
            print("Extracting lens_context...")
            lens_context = body.get("lens_context")
            print(f"lens_context: {lens_context}")
            
            if lens_context is None:
                print("WARNING: No 'lens_context' key in response")
                print(f"Available keys: {list(body.keys())}")
                return None
            
            context = lens_context.get("context")
            print(f"Final context result: {context}")
            print(f"=== analyze_image_url DEBUG END ===")
            
            return context
            
    except httpx.TimeoutException as e:
        print(f"TIMEOUT ERROR: {e}")
        logger.error(f"analyze_image_url timeout for {image_url}: {e}")
        return None
    except httpx.HTTPStatusError as e:
        print(f"HTTP STATUS ERROR: {e}")
        print(f"Response status: {e.response.status_code}")
        print(f"Response text: {e.response.text}")
        logger.error(f"analyze_image_url HTTP error for {image_url}: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"REQUEST ERROR: {e}")
        logger.error(f"analyze_image_url request error for {image_url}: {e}")
        return None
    except Exception as e:
        print(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback
        print(f"Full traceback:")
        traceback.print_exc()
        logger.error(f"analyze_image_url unexpected error for {image_url}: {type(e).__name__}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return None
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
async def handle_image_message(to: str, media_id: str, msg_id: str):
    """
    Download from WhatsApp, host locally, call lens-context API,
    then feed either the context or OCR text into process_message().
    """
    try:
        # 1) Download & host
        hosted_path = await download_and_host_media(media_id)
        if hosted_path:
            full_url = f"https://bo0c8okoc8g8044wowgggk44.vps.boomlive.in{hosted_path}"
            logger.info("Hosted image URL: %s", full_url)

            # 2) Try lens-context API first
            try:
                logger.info("Calling analyze_image_url...")
                context = await analyze_image_url(full_url)
                logger.info(f"analyze_image_url returned: {context}")
                
                if context:
                    await process_message(to, context, msg_id, "image")
                    return
                else:
                    logger.warning("analyze_image_url returned None/empty")
                    
            except Exception as e:
                logger.error(f"Error in analyze_image_url call: {type(e).__name__}: {e}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")

        # 3) Fallback: OCR/text extraction
        logger.info("Falling back to OCR text extraction...")
        text = await fetch_and_extract_media_text(media_id)
        await process_message(to, text or "[no text]", msg_id, "image")
        
    except Exception as e:
        logger.error(f"Error in handle_image_message: {type(e).__name__}: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        # Still try to send some response
        await process_message(to, "[image processing failed]", msg_id, "image")
        
async def analyze_video_url(video_url: str) -> Optional[str]:
    """
    Call your boomlive analyze-video-url endpoint and return the 'concise_query' string.
    """
    try:
        logger.info(f"=== analyze_video_url DEBUG START ===")
        logger.info(f"Video URL: {video_url}")

        api = f"https://jscw8gocc0k4s00gkcskcokc.vps.boomlive.in/analyze-video-url/?video_url={video_url}"
        logger.info(f"API URL: {api}")

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(api, headers={"accept": "application/json"})

            logger.info(f"Response received: status={resp.status_code}, length={len(resp.content)} bytes")
            logger.debug(f"Response content (first 1000 chars): {resp.text[:1000]}")

            if resp.status_code != 200:
                logger.error(f"Error: Non-200 status code {resp.status_code}")
                logger.error(f"Response text: {resp.text}")
                return None

            body = resp.json()

            video_context = body.get("video_context")
            if video_context is None:
                logger.warning("No 'video_context' found in response")
                return None

            concise_query = video_context.get("concise_query")
            if concise_query is None:
                logger.warning("No 'concise_query' found in video_context")
                return None

            logger.info(f"Concise query extracted successfully: {concise_query}")
            logger.info(f"=== analyze_video_url DEBUG END ===")
            return concise_query

    except Exception as e:
        logger.error(f"Error in analyze_video_url: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def handle_video_message(to: str, media_id: str, msg_id: str):
    """
    Download video from WhatsApp, host locally, then feed URL or extracted info into process_message.
    """
    try:
        # Download and host the video file
        hosted_path = await download_and_host_media(media_id)
        if hosted_path:
            full_url = f"https://bo0c8okoc8g8044wowgggk44.vps.boomlive.in{hosted_path}"
            logger.info("Hosted video URL: %s", full_url)
            
            # Optionally, if you have a video analysis API, call it here
            video_context_str = await analyze_video_url(full_url)
            if video_context_str:
                await process_message(to, video_context_str, msg_id, "video")
            else:
                await process_message(to, f"[Video hosted at {full_url}]", msg_id, "video")

    except Exception as e:
        logger.error(f"Error in handle_video_message: {e}")
        await process_message(to, "[video processing failed]", msg_id, "video")

async def analyze_audio_url(audio_url: str) -> Optional[str]:
    """
    Call your boomlive analyze-audio-url endpoint and return the 'transcription' string.
    """
    try:
        logger.info(f"=== analyze_audio_url DEBUG START ===")
        logger.info(f"Audio URL: {audio_url}")

        api = f"https://jscw8gocc0k4s00gkcskcokc.vps.boomlive.in/analyze-audio-url/?audio_url={audio_url}"
        logger.info(f"API URL: {api}")

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(api, headers={"accept": "application/json"})

            logger.info(f"Response received: status={resp.status_code}, length={len(resp.content)} bytes")
            logger.debug(f"Response content (first 1000 chars): {resp.text[:1000]}")

            if resp.status_code != 200:
                logger.error(f"Error: Non-200 status code {resp.status_code}")
                logger.error(f"Response text: {resp.text}")
                return None

            body = resp.json()
            transcription = body.get("transcription")
            if not transcription:
                logger.warning("No 'transcription' found in response")
                return None

            logger.info(f"Transcription extracted successfully: {transcription}")
            logger.info(f"=== analyze_audio_url DEBUG END ===")
            return transcription

    except Exception as e:
        logger.error(f"Error in analyze_audio_url: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


async def handle_audio_message(to: str, media_id: str, msg_id: str):
    """
    Download audio from WhatsApp, host locally, then feed URL or transcription into process_message.
    """
    try:
        hosted_path = await download_and_host_media(media_id)
        if hosted_path:
            full_url = f"https://bo0c8okoc8g8044wowgggk44.vps.boomlive.in{hosted_path}"
            logger.info("Hosted audio URL: %s", full_url)

            transcription = await analyze_audio_url(full_url)
            if transcription:
                await process_message(to, transcription, msg_id, "audio")
            else:
                await process_message(to, f"[Audio hosted at {full_url}]", msg_id, "audio")

    except Exception as e:
        logger.error(f"Error in handle_audio_message: {e}")
        await process_message(to, "[audio processing failed]", msg_id, "audio")


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

# @app.post("/webhook")
# async def webhook_handler(req: WebhookRequest):
#     if req.object != "whatsapp_business_account":
#         return Response(status_code=404)

#     # Ensure we have a valid token before processing
#     if not await ensure_valid_token():
#         logger.error("Cannot process webhook - invalid token")
#         return Response(status_code=500)

#     cleanup_old_messages()

#     for entry in req.entry:
#         for change in entry.get("changes", []):
#             if change.get("field") != "messages":
#                 continue
#             data = change.get("value", {})
#             messages = data.get("messages", [])

#             for msg in messages:
#                 sender = msg.get("from")
#                 msg_id = msg.get("id")

#                 # skip if from business
#                 if sender == PHONE_NUMBER_ID:
#                     continue

#                 # dedupe
#                 if msg_id in processed_messages:
#                     logger.info(f"Skipping duplicate: {msg_id}")
#                     continue
#                 processed_messages[msg_id] = time.time()

#                 # mark read asynchronously
#                 asyncio.create_task(send_read_receipt(msg_id))

#                 # dispatch asynchronously

#                 mtype = msg.get("type")
#                 print("****************************************************************")
#                 print(f"Received message from {sender}: {msg_id} type={mtype}")
#                 print("****************************************************************")

#                 if mtype == "text":
#                     text = msg["text"]["body"]
#                     asyncio.create_task(process_message(sender, text))
#                 elif mtype in ["image","audio","video"]:
#                     media = msg[mtype]
#                     text = await fetch_and_extract_media_text(media.get("id"))
#                     if text:
#                         asyncio.create_task(process_message(sender, text))
#                     # content = await fetch_media_url(media.get("id"))
#                     # print(f"Media content fetched: {len(content)} bytes", content)
#                     # if content:
#                     #     text = await extract_text_from_media(content)
#                     #     asyncio.create_task(process_message(sender, text))

#     return {"status": "success"}

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
                mtype = msg.get("type")

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

                print("****************************************************************")
                print(f"Received message from {sender}: {msg_id} type={mtype}")
                print("****************************************************************")
                print(f"Unprocessed MSG: ", msg)
                print("****************************************************************")

                if mtype == "text":
                    text = msg["text"]["body"]
                    # Pass message_id and type to process_message
                    asyncio.create_task(process_message(sender, text, msg_id, "text"))
                elif mtype == "image":
                    # Notify user that processing has started
                    asyncio.create_task(send_whatsapp_message(sender, "Image received. Please wait while we process it..."))
                    # schedule the full image workflow; handler returns immediately
                    media_id = msg["image"]["id"]
                    asyncio.create_task(handle_image_message(sender, media_id, msg_id))

                    # Add an elif block inside the message loop for video type messages
                elif mtype == "document":
                    mime_type = msg["document"].get("mime_type", "")
                    media_id = msg["document"]["id"]

                    if mime_type.startswith("video/"):
                        asyncio.create_task(send_whatsapp_message(sender, "Video received. Please wait while we process it..."))
                        asyncio.create_task(handle_video_message(sender, media_id, msg_id))
                    else:
                        logger.info(f"Received document message with mime_type {mime_type} - no handler.")
                elif mtype in ["video"]:
                    media_id = media.get("id")
                    asyncio.create_task(handle_video_message(sender, media_id, msg_id))

                elif mtype in ["audio"]:
                    media = msg[mtype]
                    # asyncio.create_task(send_whatsapp_message(sender, "Audio received. Please wait while we process it..."))
                    media_id = media.get("id")
                    asyncio.create_task(handle_audio_message(sender, media_id, msg_id))
                    # if text:
                    #     # Pass message_id and actual media type
                    #     asyncio.create_task(process_message(sender, text, msg_id, mtype))
                    # else:
                    #     # Record failed media processing
                    #     metrics_service.record_message_complete(
                    #         user_id=sender,
                    #         message_id=msg_id,
                    #         message_type=mtype,
                    #         message_text=f"[{mtype.upper()} - processing failed]",
                    #         response_text="",
                    #         start_time=time.time(),
                    #         llm_response_time=0,
                    #         whatsapp_send_time=0,
                    #         success=False,
                    #         error_type="MEDIA_PROCESSING_FAILED"
                    #     )

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

# async def process_message(to: str, text: str):
#     try:
#         logger.info(f"LLM call for {to}: {text[:30]}...")
#         from datetime import datetime
#         import uuid

#         thread_id = f"{datetime.now().isoformat()}_{to}_{uuid.uuid4().hex}"
#         enhanced_message = await twitter_service.enhance_message(text)

#         print(f"Thread ID: {thread_id}")
#         async with httpx.AsyncClient(timeout=100) as client:
#             r = await client.get(LLM_API_URL, params={"question": enhanced_message, "thread_id": uuid.uuid4().hex, "using_Whatsapp": True})
#         if r.status_code == 200:
#             reply = r.json().get("response", "No response")
#         else:
#             reply = "Sorry, error processing your request."
#             logger.error(f"LLM error {r.status_code}: {r.text}")
#         await send_whatsapp_message(to, reply)
#     except Exception:
#         logger.exception("process_message exception")
#         try:
#             await send_whatsapp_message(to, "Sorry, I encountered an error processing your request.")
#         except Exception as send_exc:
#             logger.error(f"Failed to send error message: {send_exc}")

async def process_message(to: str, text: str, message_id: str = None, message_type: str = "text"):
    """Enhanced process_message with comprehensive metrics tracking"""
    
    # Start metrics tracking
    start_time = metrics_service.start_message_processing(to, message_id or f"msg_{int(time.time())}", message_type, text)
    
    # Initialize timing variables
    llm_start_time = None
    llm_end_time = None
    whatsapp_start_time = None
    whatsapp_end_time = None
    
    # Initialize response variables
    success = False
    error_type = None
    response = ""
    enhanced_by_twitter = False
    
    try:
        logger.info(f"Processing message from {to}: {text[:50]}...")
        
        from datetime import datetime
        import uuid

        thread_id = f"{datetime.now().isoformat()}_{to}_{uuid.uuid4().hex}"
        
        # Twitter enhancement
        try:
            enhanced_message = await twitter_service.enhance_message(text)
            enhanced_by_twitter = enhanced_message != text
        except Exception as e:
            logger.warning(f"Twitter enhancement failed: {e}")
            enhanced_message = text
            enhanced_by_twitter = False

        print(f"Thread ID: {thread_id}")
        
        # LLM API call with timing
        llm_start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=100) as client:
                r = await client.get(LLM_API_URL, params={
                    "question": enhanced_message, 
                    "thread_id": uuid.uuid4().hex, 
                    "using_Whatsapp": True
                })
            llm_end_time = time.time()
            
            if r.status_code == 200:
                response = r.json().get("response", "No response")
                success = True
            else:
                response = "Sorry, error processing your request."
                error_type = f"LLM_HTTP_{r.status_code}"
                logger.error(f"LLM API error {r.status_code}: {r.text}")
                
        except asyncio.TimeoutError:
            llm_end_time = time.time()
            response = "Sorry, request timed out. Please try again."
            error_type = "LLM_TIMEOUT"
            logger.error("LLM API timeout")
        except Exception as e:
            llm_end_time = time.time()
            response = "Sorry, error processing your request."
            error_type = f"LLM_EXCEPTION_{type(e).__name__}"
            logger.error(f"LLM API exception: {e}")
        
        # WhatsApp send with timing
        whatsapp_start_time = time.time()
        try:
            await send_whatsapp_message(to, response)
            whatsapp_end_time = time.time()
        except Exception as e:
            whatsapp_end_time = time.time()
            error_type = f"WHATSAPP_SEND_{type(e).__name__}"
            success = False
            logger.error(f"WhatsApp send failed: {e}")
            
    except Exception as e:
        error_type = f"PROCESS_EXCEPTION_{type(e).__name__}"
        success = False
        logger.exception("Unexpected error in process_message")
        
        # Try to send error message
        try:
            response = "Sorry, I encountered an error processing your request."
            whatsapp_start_time = time.time()
            await send_whatsapp_message(to, response)
            whatsapp_end_time = time.time()
        except Exception as send_exc:
            whatsapp_end_time = time.time() if whatsapp_start_time else None
            logger.error(f"Failed to send error message: {send_exc}")
    
    finally:
        # Calculate timing metrics
        llm_response_time = (llm_end_time - llm_start_time) if llm_start_time and llm_end_time else 0
        whatsapp_send_time = (whatsapp_end_time - whatsapp_start_time) if whatsapp_start_time and whatsapp_end_time else 0
        
        # Record comprehensive metrics
        metrics_service.record_message_complete(
            user_id=to,
            message_id=message_id or f"msg_{int(time.time())}",
            message_type=message_type,
            message_text=text,
            response_text=response,
            start_time=start_time,
            llm_response_time=llm_response_time,
            whatsapp_send_time=whatsapp_send_time,
            success=success,
            error_type=error_type,
            enhanced_by_twitter=enhanced_by_twitter
        )


# async def fetch_media_url(media_id: str) -> bytes:
#     if not await ensure_valid_token():
#         logger.error("Cannot fetch media - invalid token")
#         return b""
        
#     url = f"{WHATSAPP_API_URL}/{media_id}"
#     print(f"Fetching media from {url}")
#     headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
#     async with httpx.AsyncClient() as client:
#         r = await client.get(url, headers=headers)
#         if r.status_code == 401:
#             logger.warning("Media fetch failed with 401, attempting token refresh...")
#             if await refresh_access_token():
#                 headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
#                 r = await client.get(url, headers=headers)
                
#         if r.status_code != 200:
#             logger.error(f"Media fetch failed: {r.status_code}")
#             return b""
            
#         media_url = r.json().get("url")
#         r2 = await client.get(media_url, headers=headers)
#         return r2.content if r2.status_code == 200 else b""

# async def extract_text_from_media(content: bytes) -> str:
#     tmp = f"temp_{secrets.token_hex(8)}"
#     async with aiofiles.open(tmp, "wb") as f:
#         await f.write(content)
#     async with aiofiles.open(tmp, "rb") as f:
#         data = await f.read()
#     url = "https://k8ccccwccggk4gc4c0o4ggkg.vps.boomlive.in/media/process_input"
#     async with httpx.AsyncClient() as client:
#         r = await client.post(url, files={"file": (tmp, data)})
#     os.remove(tmp)
#     return r.json().get("extracted_text", "") if r.status_code == 200 else ""
async def fetch_whatsapp_media_url(media_id: str) -> Optional[str]:
    """Return the WhatsApp‑hosted URL for a media ID (no processing)."""
    if not await ensure_valid_token():
        return None

    r = await httpx.AsyncClient().get(
        f"{WHATSAPP_API_URL}/{media_id}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
    )
    if r.status_code == 401:
        await refresh_access_token()
        r = await httpx.AsyncClient().get(
            f"{WHATSAPP_API_URL}/{media_id}",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )
    if r.status_code == 200:
        return r.json().get("url")
    logger.error(f"Failed to fetch media URL ({r.status_code}): {r.text}")
    return None


async def fetch_and_extract_media_text(media_id: str) -> str:
    """Fetch media and extract text using the new unified API"""
    if not await ensure_valid_token():
        logger.error("Cannot fetch media - invalid token")
        return ""
        
    # First get the media URL from WhatsApp API
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
            return ""
            
        media_url = r.json().get("url")
        if not media_url:
            logger.error("No media URL found in response")
            return ""
        
        # Now use the new media processing API
        media_process_url = "https://k8ccccwccggk4gc4c0o4ggkg.vps.boomlive.in/media/whatsapp/process"
        params = {
            "url": media_url,
            "token": ACCESS_TOKEN
        }
        
        logger.info(f"Processing media with URL: {media_url}")
        
        r2 = await client.get(media_process_url, params=params)
        
        if r2.status_code != 200:
            logger.error(f"Media processing failed: {r2.status_code} - {r2.text}")
            return ""
            
        try:
            response_data = r2.json()
            if response_data.get("success") and response_data.get("data", {}).get("success"):
                extracted_text = response_data["data"]["result"]["text"]
                media_type = response_data["data"]["result"]["type"]
                file_size = response_data["data"]["result"]["file_size"]
                
                logger.info(f"Successfully extracted text from {media_type} ({file_size} bytes): {extracted_text[:100]}...")
                return extracted_text
            else:
                logger.error(f"Media processing API returned error: {response_data}")
                return ""
                
        except (KeyError, TypeError) as e:
            logger.error(f"Error parsing media processing response: {e}")
            return ""


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
    async with httpx.AsyncClient(timeout=100) as client:
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

@app.get("/metrics")
async def get_chatbot_metrics(hours: int = 24):
    """Get comprehensive chatbot performance metrics
    
    Args:
        hours: Time period in hours (default: 24)
    
    Returns:
        Detailed performance metrics including response times, success rates, user engagement
    """
    try:
        return metrics_service.get_performance_metrics(hours)
    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
        return {"error": "Failed to retrieve metrics", "details": str(e)}

@app.get("/metrics/summary")
async def get_metrics_summary():
    """Get quick metrics summary for dashboards"""
    try:
        return metrics_service.get_metrics_summary()
    except Exception as e:
        logger.error(f"Error getting metrics summary: {e}")
        return {"error": "Failed to retrieve metrics summary", "details": str(e)}

@app.get("/metrics/users")
async def get_user_analytics(user_id: str = None):
    """Get user analytics - specific user or aggregate stats
    
    Args:
        user_id: Optional specific user ID to analyze
    
    Returns:
        User engagement and behavior analytics
    """
    try:
        return metrics_service.get_user_analytics(user_id)
    except Exception as e:
        logger.error(f"Error getting user analytics: {e}")
        return {"error": "Failed to retrieve user analytics", "details": str(e)}

@app.get("/metrics/health")
async def get_health_check():
    """Get chatbot health status"""
    try:
        summary = metrics_service.get_metrics_summary()
        return {
            "status": summary.get("status", "unknown"),
            "health_score": summary.get("health_score", 0),
            "last_hour_messages": summary.get("current_hour", {}).get("messages", 0),
            "last_24h_success_rate": summary.get("last_24_hours", {}).get("success_rate", 0),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting health check: {e}")
        return {
            "status": "error", 
            "health_score": 0,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

from fastapi import HTTPException
from typing import Optional

@app.get("/analyze-image")
async def analyze_image(image_url: str) -> dict:
    result = await analyze_image_url(image_url)
    if result is None:
        raise HTTPException(status_code=500, detail="Analysis failed")
    return {"context": result}


@app.get("/status")
async def status():
    try:
        token_info = await token_status()
        metrics_summary = metrics_service.get_metrics_summary()
        
        return {
            "processed_messages": len(processed_messages),
            "server_time": datetime.now().isoformat(),
            "token_status": token_info,
            "chatbot_health": {
                "health_score": metrics_summary.get("health_score", 0),
                "status": metrics_summary.get("status", "unknown"),
                "messages_24h": metrics_summary.get("last_24_hours", {}).get("messages", 0),
                "success_rate_24h": metrics_summary.get("last_24_hours", {}).get("success_rate", 0)
            }
        }
    except Exception as e:
        logger.error(f"Error in status endpoint: {e}")
        return {
            "processed_messages": len(processed_messages),
            "server_time": datetime.now().isoformat(),
            "error": "Failed to retrieve complete status"
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