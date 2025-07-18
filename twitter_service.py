import re
import os
import httpx
import asyncio
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
import uuid

logger = logging.getLogger(__name__)

class TwitterService:
    def __init__(self, llm_api_url: str, twitter_bearer_token: Optional[str] = None):
        self.llm_api_url = llm_api_url
        self.twitter_bearer_token = twitter_bearer_token or os.getenv("TWITTER_BEARER_TOKEN")
        
    def extract_twitter_urls(self, text: str) -> List[str]:
        """Extract all Twitter/X URLs from text"""
        url_patterns = [
            r'https?://(?:www\.)?(?:twitter\.com|x\.com|mobile\.twitter\.com)/\S+',
            r'https?://t\.co/\w+'  # Shortened URLs that might be Twitter links
        ]
        
        urls = []
        for pattern in url_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            urls.extend(matches)
        
        return urls
    
    def extract_tweet_id(self, url: str) -> Optional[str]:
        """Extract tweet ID from various Twitter URL formats"""
        try:
            twitter_patterns = [
                r'(?:twitter\.com|x\.com|mobile\.twitter\.com)/\w+/status/(\d+)',
                r'twitter\.com/\w+/statuses/(\d+)',
            ]
            
            for pattern in twitter_patterns:
                match = re.search(pattern, url)
                if match:
                    return match.group(1)
            
            return None
            
        except Exception as e:
            logger.error(f"Error extracting tweet ID from URL {url}: {e}")
            return None
    
    async def resolve_shortened_url(self, url: str) -> str:
        """Resolve shortened URLs (like t.co links)"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, follow_redirects=True)
                return str(response.url)
        except Exception as e:
            logger.error(f"Error resolving URL {url}: {e}")
            return url
    
    async def fetch_tweet_content(self, tweet_id: str) -> Optional[Dict[str, Any]]:
        """Fetch tweet content using Twitter API"""
        if not self.twitter_bearer_token:
            logger.warning("No Twitter Bearer token found, cannot fetch tweet content")
            return None
            
        try:
            url = f"https://api.twitter.com/2/tweets/{tweet_id}"
            headers = {
                "Authorization": f"Bearer {self.twitter_bearer_token}",
                "Content-Type": "application/json"
            }
            
            params = {
                "expansions": "author_id,attachments.media_keys",
                "media.fields": "type,url,alt_text,preview_image_url",
                "user.fields": "username,name,verified",
                "tweet.fields": "created_at,public_metrics,context_annotations"
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, headers=headers, params=params)
                
            if response.status_code == 200:
                data = response.json()
                return self._parse_tweet_data(data)
            else:
                logger.error(f"Failed to fetch tweet {tweet_id}: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching tweet content: {e}")
            return None
    
    def _parse_tweet_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Twitter API response into structured data"""
        tweet = data.get("data", {})
        includes = data.get("includes", {})
        
        # Extract author info
        author = {"username": "unknown", "name": "Unknown", "verified": False}
        if includes.get("users"):
            user = includes["users"][0]
            author = {
                "username": user.get("username", "unknown"),
                "name": user.get("name", "Unknown"),
                "verified": user.get("verified", False)
            }
        
        # Extract media info
        media_info = []
        if includes.get("media"):
            for media in includes["media"]:
                media_info.append({
                    "type": media.get("type", "unknown"),
                    "url": media.get("url"),
                    "preview_url": media.get("preview_image_url"),
                    "alt_text": media.get("alt_text", "")
                })
        
        # Extract metrics
        metrics = tweet.get("public_metrics", {})
        
        return {
            "id": tweet.get("id"),
            "text": tweet.get("text", ""),
            "author": author,
            "created_at": tweet.get("created_at"),
            "media": media_info,
            "metrics": {
                "retweets": metrics.get("retweet_count", 0),
                "likes": metrics.get("like_count", 0),
                "replies": metrics.get("reply_count", 0),
                "quotes": metrics.get("quote_count", 0)
            },
            "context_annotations": tweet.get("context_annotations", [])
        }
    
    async def process_twitter_urls_in_message(self, message_text: str) -> Dict[str, Any]:
        """Process Twitter URLs in message and return structured data"""
        urls = self.extract_twitter_urls(message_text)
        
        if not urls:
            return {
                "has_twitter_urls": False,
                "tweet_contents": [],
                "processed_text": message_text,
                "context_summary": ""
            }
        
        logger.info(f"Found {len(urls)} Twitter URLs in message")
        
        # Resolve shortened URLs
        resolved_urls = []
        for url in urls:
            if "t.co" in url:
                resolved_url = await self.resolve_shortened_url(url)
                resolved_urls.append(resolved_url)
            else:
                resolved_urls.append(url)
        
        # Extract tweet content
        tweet_contents = []
        for url in resolved_urls:
            tweet_id = self.extract_tweet_id(url)
            if tweet_id:
                tweet_content = await self.fetch_tweet_content(tweet_id)
                if tweet_content:
                    tweet_contents.append(tweet_content)
                    logger.info(f"Successfully processed tweet: {tweet_id}")
        
        # Clean message text
        processed_text = message_text
        for url in urls:
            processed_text = processed_text.replace(url, "").strip()
        
        processed_text = re.sub(r'\s+', ' ', processed_text).strip()
        if not processed_text:
            processed_text = "Please analyze the shared tweet(s)."
        
        # Create context summary
        context_summary = self._create_context_summary(tweet_contents)
        
        return {
            "has_twitter_urls": True,
            "tweet_contents": tweet_contents,
            "processed_text": processed_text,
            "context_summary": context_summary
        }
    
    def _create_context_summary(self, tweet_contents: List[Dict[str, Any]]) -> str:
        """Create a structured context summary for the LLM"""
        if not tweet_contents:
            return ""
            
        context_summary = f"SHARED TWEETS ({len(tweet_contents)} tweet(s)):\n\n"
        
        for i, tweet in enumerate(tweet_contents, 1):
            author = tweet["author"]
            verified_badge = "✓" if author["verified"] else ""
            
            context_summary += f"Tweet {i} by @{author['username']} ({author['name']}{verified_badge}):\n"
            context_summary += f"Content: \"{tweet['text']}\"\n"
            
            # Add metrics if significant
            metrics = tweet["metrics"]
            if any(metrics.values()):
                context_summary += f"Engagement: {metrics['likes']} likes, {metrics['retweets']} retweets, {metrics['replies']} replies\n"
            
            # Add media info
            if tweet["media"]:
                media_types = [media["type"] for media in tweet["media"]]
                context_summary += f"Media: {len(tweet['media'])} file(s) ({', '.join(media_types)})\n"
            
            # Add context annotations (topics/entities)
            if tweet["context_annotations"]:
                topics = [ann.get("entity", {}).get("name", "") for ann in tweet["context_annotations"][:3]]
                topics = [t for t in topics if t]
                if topics:
                    context_summary += f"Topics: {', '.join(topics)}\n"
            
            context_summary += "\n"
        
        return context_summary
    
    def build_enhanced_context(self, original_message: str, twitter_data: Dict[str, Any]) -> str:
        """Build enhanced context including Twitter content for LLM"""
        if not twitter_data["has_twitter_urls"]:
            return original_message
        
        context_parts = []
        
        # Add Twitter content
        if twitter_data["context_summary"]:
            context_parts.append(twitter_data["context_summary"])
        
        # Add user's actual message/request
        if twitter_data["processed_text"]:
            context_parts.append(f"USER REQUEST: {twitter_data['processed_text']}")
        
        return "\n".join(context_parts)
    
    async def send_to_llm(self, enhanced_context: str, thread_id: str, sender: str) -> str:
        """Send enhanced context to LLM API"""
        try:
            params = {
                "question": enhanced_context,
                "thread_id": thread_id,
                "using_Whatsapp": True
            }
            
            async with httpx.AsyncClient(timeout=100) as client:
                response = await client.get(self.llm_api_url, params=params)
                
            if response.status_code == 200:
                return response.json().get("response", "No response from LLM")
            else:
                logger.error(f"LLM API error {response.status_code}: {response.text}")
                return "Sorry, I encountered an error processing your request."
                
        except Exception as e:
            logger.error(f"Error sending to LLM: {e}")
            return "Sorry, I encountered an error processing your request."
    
    async def process_message_with_twitter_support(self, message_text: str, sender: str) -> str:
        """Main method to process message with Twitter URL support"""
        try:
            # Process Twitter URLs
            twitter_data = await self.process_twitter_urls_in_message(message_text)
            
            # Generate thread ID
            thread_id = f"{datetime.now().isoformat()}_{sender}_{uuid.uuid4().hex}"
            
            # Build enhanced context if Twitter URLs found
            if twitter_data["has_twitter_urls"]:
                enhanced_context = self.build_enhanced_context(message_text, twitter_data)
                logger.info(f"Enhanced context with Twitter data: {len(enhanced_context)} chars")
                
                # Send to LLM
                response = await self.send_to_llm(enhanced_context, thread_id, sender)
                
                # Add note about processed tweets
                tweet_count = len(twitter_data["tweet_contents"])
                if tweet_count > 0:
                    response += f"\n\n[Analyzed {tweet_count} shared tweet(s)]"
                
                return response
            else:
                # No Twitter URLs, send original message
                return await self.send_to_llm(message_text, thread_id, sender)
                
        except Exception as e:
            logger.error(f"Error in process_message_with_twitter_support: {e}")
            return "Sorry, I encountered an error processing your request."


# Usage example for integration with your WhatsApp bot
async def example_usage():
    """Example of how to integrate with your WhatsApp bot"""
    
    # Initialize the service
    LLM_API_URL = "https://a8c4cosco0wc0gg8s40w8kco.vps.boomlive.in/query"
    twitter_service = TwitterService(LLM_API_URL)
    
    # Example message with Twitter URL
    message = "What do you think about this tweet? https://twitter.com/elonmusk/status/1234567890"
    sender = "1234567890"
    
    # Process the message
    response = await twitter_service.process_message_with_twitter_support(message, sender)
    print(f"Response: {response}")


# if __name__ == "__main__":
#     # Test the Twitter URL extraction
#     test_messages = [
#         "Check out this tweet: https://twitter.com/elonmusk/status/1234567890",
#         "What do you think about this? https://x.com/openai/status/9876543210",
#         "Look at this thread: https://twitter.com/user/status/1111111111 and this too https://x.com/other/status/2222222222",
#         "Just a regular message without Twitter links",
#         "Shortened link: https://t.co/abc123xyz"
#     ]
    
#     service = TwitterService("http://localhost:8000")
    
#     for msg in test_messages:
#         urls = service.extract_twitter_urls(msg)
#         print(f"Message: {msg[:50]}...")
#         print(f"Found URLs: {urls}")
#         for url in urls:
#             tweet_id = service.extract_tweet_id(url)
#             print(f"  Tweet ID: {tweet_id}")
#         print("-" * 50)