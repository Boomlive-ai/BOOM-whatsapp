import re
import os
import httpx
import asyncio
from typing import Optional, Dict, Any

class TwitterService:
    def __init__(self, twitter_bearer_token: Optional[str] = None):
        self.twitter_bearer_token = twitter_bearer_token or os.getenv("TWITTER_BEARER_TOKEN")
    
    def extract_tweet_id(self, text: str) -> Optional[str]:
        """Extract tweet ID from Twitter URL in text"""
        pattern = r'(?:twitter\.com|x\.com)/\w+/status/(\d+)'
        match = re.search(pattern, text)
        return match.group(1) if match else None
    
    async def fetch_tweet_content(self, tweet_id: str) -> Optional[str]:
        """Fetch tweet text using Twitter API"""
        if not self.twitter_bearer_token:
            return None
            
        try:
            url = f"https://api.twitter.com/2/tweets/{tweet_id}"
            headers = {"Authorization": f"Bearer {self.twitter_bearer_token}"}
            params = {"expansions": "author_id", "user.fields": "username,name"}
            
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, headers=headers, params=params)
                
            if response.status_code == 200:
                data = response.json()
                tweet = data.get("data", {})
                users = data.get("includes", {}).get("users", [])
                
                tweet_text = tweet.get("text", "")
                if users:
                    author = f"@{users[0].get('username', 'unknown')}"
                    return f"Tweet by {author}: {tweet_text}"
                return f"Tweet: {tweet_text}"
            return None
                
        except Exception:
            return None
    
    async def enhance_message(self, message_text: str) -> str:
        """Add Twitter content to message if URL present"""
        tweet_id = self.extract_tweet_id(message_text)
        
        if not tweet_id:
            return message_text
            
        tweet_content = await self.fetch_tweet_content(tweet_id)
        
        if tweet_content:
            return f"{message_text}\n\n{tweet_content}"
        return message_text

# Usage
async def process_message(message: str) -> str:
    twitter_service = TwitterService()
    return await twitter_service.enhance_message(message)