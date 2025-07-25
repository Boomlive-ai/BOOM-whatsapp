"""
Chatbot Performance Metrics Service
Tracks and analyzes chatbot performance metrics for insights and optimization.
"""

import time
from datetime import datetime, timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any
import statistics
import json
import logging

logger = logging.getLogger(__name__)

@dataclass
class MessageMetric:
    """Individual message performance record"""
    user_id: str
    message_id: str
    message_type: str
    timestamp: datetime
    processing_time: float
    llm_response_time: float
    whatsapp_send_time: float
    success: bool
    message_length: int
    response_length: int
    enhanced_by_twitter: bool = False
    error_type: Optional[str] = None
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data

@dataclass
class UserSession:
    """User session tracking"""
    user_id: str
    first_seen: datetime
    last_seen: datetime
    total_messages: int
    successful_messages: int
    total_response_time: float
    message_types: Dict[str, int]
    
    @property
    def success_rate(self) -> float:
        return (self.successful_messages / self.total_messages * 100) if self.total_messages > 0 else 0
    
    @property
    def avg_response_time(self) -> float:
        return self.total_response_time / self.total_messages if self.total_messages > 0 else 0
    
    @property
    def session_duration(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()

class ChatbotMetricsService:
    """Comprehensive chatbot performance analytics service"""
    
    def __init__(self, max_history: int = 10000):
        self.metrics: deque = deque(maxlen=max_history)
        self.user_sessions: Dict[str, UserSession] = {}
        self.hourly_cache: Dict[str, Dict] = {}
        self.cache_expiry: Dict[str, datetime] = {}
        
        # Performance thresholds
        self.FAST_RESPONSE_THRESHOLD = 2.0  # seconds
        self.SLOW_RESPONSE_THRESHOLD = 5.0  # seconds
        self.SUCCESS_RATE_THRESHOLD = 0.95  # 95%
        
    def start_message_processing(self, user_id: str, message_id: str, 
                                message_type: str, message_text: str) -> float:
        """Start timing message processing"""
        return time.time()
    
    def record_message_complete(self, user_id: str, message_id: str, message_type: str,
                              message_text: str, response_text: str, start_time: float,
                              llm_response_time: float, whatsapp_send_time: float,
                              success: bool, error_type: Optional[str] = None,
                              enhanced_by_twitter: bool = False, retry_count: int = 0):
        """Record completed message processing with full metrics"""
        
        processing_time = time.time() - start_time
        
        # Create metric record
        metric = MessageMetric(
            user_id=user_id,
            message_id=message_id,
            message_type=message_type,
            timestamp=datetime.now(),
            processing_time=processing_time,
            llm_response_time=llm_response_time,
            whatsapp_send_time=whatsapp_send_time,
            success=success,
            message_length=len(message_text),
            response_length=len(response_text),
            enhanced_by_twitter=enhanced_by_twitter,
            error_type=error_type,
            retry_count=retry_count
        )
        
        # Store metric
        self.metrics.append(metric)
        
        # Update user session
        self._update_user_session(metric)
        
        # Clear cache if needed
        self._clear_expired_cache()
        
        logger.info(f"Recorded metric for user {user_id}: {processing_time:.2f}s, success={success}")
    
    def _update_user_session(self, metric: MessageMetric):
        """Update user session data"""
        user_id = metric.user_id
        
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = UserSession(
                user_id=user_id,
                first_seen=metric.timestamp,
                last_seen=metric.timestamp,
                total_messages=0,
                successful_messages=0,
                total_response_time=0.0,
                message_types=defaultdict(int)
            )
        
        session = self.user_sessions[user_id]
        session.last_seen = metric.timestamp
        session.total_messages += 1
        session.total_response_time += metric.processing_time
        session.message_types[metric.message_type] += 1
        
        if metric.success:
            session.successful_messages += 1
    
    def _clear_expired_cache(self):
        """Clear expired cache entries"""
        now = datetime.now()
        expired_keys = [k for k, expiry in self.cache_expiry.items() if now > expiry]
        for key in expired_keys:
            self.hourly_cache.pop(key, None)
            self.cache_expiry.pop(key, None)
    
    def get_performance_metrics(self, hours: int = 24) -> Dict[str, Any]:
        """Get comprehensive performance metrics for specified time period"""
        
        # Check cache first
        cache_key = f"metrics_{hours}h"
        if cache_key in self.hourly_cache and cache_key in self.cache_expiry:
            if datetime.now() < self.cache_expiry[cache_key]:
                return self.hourly_cache[cache_key]
        
        cutoff = datetime.now() - timedelta(hours=hours)
        recent_metrics = [m for m in self.metrics if m.timestamp >= cutoff]
        
        if not recent_metrics:
            return {
                "error": "No data available for the specified time period",
                "time_period_hours": hours,
                "timestamp": datetime.now().isoformat()
            }
        
        # Calculate comprehensive metrics
        metrics_data = self._calculate_comprehensive_metrics(recent_metrics, hours)
        
        # Cache results for 5 minutes
        self.hourly_cache[cache_key] = metrics_data
        self.cache_expiry[cache_key] = datetime.now() + timedelta(minutes=5)
        
        return metrics_data
    
    def _calculate_comprehensive_metrics(self, metrics: List[MessageMetric], hours: int) -> Dict[str, Any]:
        """Calculate detailed performance metrics"""
        
        total_messages = len(metrics)
        successful_messages = sum(1 for m in metrics if m.success)
        failed_messages = total_messages - successful_messages
        
        # Response time analysis
        response_times = [m.processing_time for m in metrics]
        llm_times = [m.llm_response_time for m in metrics if m.llm_response_time > 0]
        whatsapp_times = [m.whatsapp_send_time for m in metrics if m.whatsapp_send_time > 0]
        
        # Message type distribution
        message_types = defaultdict(int)
        for m in metrics:
            message_types[m.message_type] += 1
        
        # Error analysis
        error_analysis = self._analyze_errors(metrics)
        
        # User engagement metrics
        user_metrics = self._calculate_user_metrics(metrics)
        
        # Performance categorization
        performance_categories = self._categorize_performance(response_times)
        
        # Hourly breakdown
        hourly_breakdown = self._calculate_hourly_breakdown(metrics)
        
        # Generate insights
        insights = self._generate_actionable_insights(metrics, hours)
        
        return {
            "time_period_hours": hours,
            "timestamp": datetime.now().isoformat(),
            "data_points": total_messages,
            
            # Overall Performance
            "overall": {
                "total_messages": total_messages,
                "successful_messages": successful_messages,
                "failed_messages": failed_messages,
                "success_rate": round((successful_messages / total_messages) * 100, 2),
                "failure_rate": round((failed_messages / total_messages) * 100, 2),
                "messages_per_hour": round(total_messages / hours, 2)
            },
            
            # Response Time Analysis
            "response_times": {
                "average": round(statistics.mean(response_times), 2),
                "median": round(statistics.median(response_times), 2),
                "p95": round(sorted(response_times)[int(len(response_times) * 0.95)], 2),
                "p99": round(sorted(response_times)[int(len(response_times) * 0.99)], 2),
                "min": round(min(response_times), 2),
                "max": round(max(response_times), 2),
                "std_dev": round(statistics.stdev(response_times) if len(response_times) > 1 else 0, 2)
            },
            
            # Component Performance
            "component_performance": {
                "llm": {
                    "average_time": round(statistics.mean(llm_times), 2) if llm_times else 0,
                    "median_time": round(statistics.median(llm_times), 2) if llm_times else 0,
                    "total_calls": len(llm_times)
                },
                "whatsapp": {
                    "average_send_time": round(statistics.mean(whatsapp_times), 2) if whatsapp_times else 0,
                    "median_send_time": round(statistics.median(whatsapp_times), 2) if whatsapp_times else 0,
                    "total_sends": len(whatsapp_times)
                }
            },
            
            # Performance Categories
            "performance_categories": performance_categories,
            
            # Message Types
            "message_types": dict(message_types),
            
            # Error Analysis
            "errors": error_analysis,
            
            # User Engagement
            "users": user_metrics,
            
            # Feature Usage
            "features": {
                "twitter_enhanced": sum(1 for m in metrics if m.enhanced_by_twitter),
                "twitter_enhancement_rate": round(sum(1 for m in metrics if m.enhanced_by_twitter) / total_messages * 100, 2),
                "retries": sum(m.retry_count for m in metrics),
                "avg_retries": round(sum(m.retry_count for m in metrics) / total_messages, 2)
            },
            
            # Time-based Analysis
            "hourly_breakdown": hourly_breakdown,
            
            # Actionable Insights
            "insights": insights,
            
            # Health Score (0-100)
            "health_score": self._calculate_health_score(metrics)
        }
    
    def _analyze_errors(self, metrics: List[MessageMetric]) -> Dict[str, Any]:
        """Analyze error patterns"""
        failed_metrics = [m for m in metrics if not m.success]
        
        if not failed_metrics:
            return {
                "error_rate": 0.0,
                "total_errors": 0,
                "error_types": {},
                "most_common_error": None
            }
        
        error_types = defaultdict(int)
        for m in failed_metrics:
            if m.error_type:
                error_types[m.error_type] += 1
            else:
                error_types["UNKNOWN"] += 1
        
        most_common_error = max(error_types.items(), key=lambda x: x[1]) if error_types else None
        
        return {
            "error_rate": round(len(failed_metrics) / len(metrics) * 100, 2),
            "total_errors": len(failed_metrics),
            "error_types": dict(error_types),
            "most_common_error": most_common_error[0] if most_common_error else None,
            "most_common_error_count": most_common_error[1] if most_common_error else 0
        }
    
    def _calculate_user_metrics(self, metrics: List[MessageMetric]) -> Dict[str, Any]:
        """Calculate user engagement metrics"""
        unique_users = set(m.user_id for m in metrics)
        active_users_1h = set(m.user_id for m in metrics 
                            if m.timestamp >= datetime.now() - timedelta(hours=1))
        
        # User message distribution
        user_message_counts = defaultdict(int)
        for m in metrics:
            user_message_counts[m.user_id] += 1
        
        message_counts = list(user_message_counts.values())
        
        return {
            "unique_users": len(unique_users),
            "active_users_1h": len(active_users_1h),
            "total_registered_users": len(self.user_sessions),
            "avg_messages_per_user": round(statistics.mean(message_counts), 2) if message_counts else 0,
            "median_messages_per_user": round(statistics.median(message_counts), 2) if message_counts else 0,
            "max_messages_per_user": max(message_counts) if message_counts else 0
        }
    
    def _categorize_performance(self, response_times: List[float]) -> Dict[str, Any]:
        """Categorize responses by performance"""
        fast = sum(1 for t in response_times if t <= self.FAST_RESPONSE_THRESHOLD)
        medium = sum(1 for t in response_times if self.FAST_RESPONSE_THRESHOLD < t <= self.SLOW_RESPONSE_THRESHOLD)
        slow = sum(1 for t in response_times if t > self.SLOW_RESPONSE_THRESHOLD)
        
        total = len(response_times)
        
        return {
            "fast_responses": fast,
            "medium_responses": medium,
            "slow_responses": slow,
            "fast_percentage": round(fast / total * 100, 2) if total > 0 else 0,
            "medium_percentage": round(medium / total * 100, 2) if total > 0 else 0,
            "slow_percentage": round(slow / total * 100, 2) if total > 0 else 0
        }
    
    def _calculate_hourly_breakdown(self, metrics: List[MessageMetric]) -> Dict[str, Dict]:
        """Calculate hourly performance breakdown"""
        hourly_stats = defaultdict(lambda: {
            'messages': 0, 'success': 0, 'errors': 0,
            'response_times': [], 'message_types': defaultdict(int)
        })
        
        for m in metrics:
            hour_key = m.timestamp.strftime('%Y-%m-%d %H:00')
            stats = hourly_stats[hour_key]
            stats['messages'] += 1
            stats['response_times'].append(m.processing_time)
            stats['message_types'][m.message_type] += 1
            
            if m.success:
                stats['success'] += 1
            else:
                stats['errors'] += 1
        
        # Process hourly stats
        processed_hourly = {}
        for hour, stats in hourly_stats.items():
            processed_hourly[hour] = {
                'messages': stats['messages'],
                'success': stats['success'],
                'errors': stats['errors'],
                'success_rate': round(stats['success'] / stats['messages'] * 100, 2),
                'avg_response_time': round(statistics.mean(stats['response_times']), 2),
                'message_types': dict(stats['message_types'])
            }
        
        return dict(sorted(processed_hourly.items())[-24:])  # Last 24 hours
    
    def _generate_actionable_insights(self, metrics: List[MessageMetric], hours: int) -> List[str]:
        """Generate actionable insights for chatbot optimization"""
        insights = []
        
        if not metrics:
            return ["No data available for analysis"]
        
        # Response time insights
        avg_time = statistics.mean([m.processing_time for m in metrics])
        if avg_time > self.SLOW_RESPONSE_THRESHOLD:
            insights.append(f"⚠️ High average response time ({avg_time:.1f}s). Consider optimizing LLM calls or caching.")
        elif avg_time <= self.FAST_RESPONSE_THRESHOLD:
            insights.append(f"✅ Excellent response times ({avg_time:.1f}s average)")
        
        # Success rate insights
        success_rate = sum(1 for m in metrics if m.success) / len(metrics)
        if success_rate < self.SUCCESS_RATE_THRESHOLD:
            insights.append(f"⚠️ Success rate is {success_rate:.1%}. Review error patterns and implement fixes.")
        else:
            insights.append(f"✅ High success rate ({success_rate:.1%})")
        
        # LLM performance insights
        llm_times = [m.llm_response_time for m in metrics if m.llm_response_time > 0]
        if llm_times:
            avg_llm_time = statistics.mean(llm_times)
            if avg_llm_time > 3.0:
                insights.append(f"🔍 LLM calls averaging {avg_llm_time:.1f}s. Consider prompt optimization or model selection.")
        
        # Usage pattern insights
        message_types = defaultdict(int)
        for m in metrics:
            message_types[m.message_type] += 1
        
        media_messages = sum(message_types.get(mt, 0) for mt in ['image', 'audio', 'video'])
        if media_messages > len(metrics) * 0.3:
            insights.append("📊 High media usage (>30%). Ensure media processing is optimized.")
        
        # User engagement insights
        unique_users = len(set(m.user_id for m in metrics))
        if len(metrics) / unique_users > 15:
            insights.append("🔄 Very high user engagement (>15 messages per user)")
        elif len(metrics) / unique_users < 2:
            insights.append("📈 Low user engagement (<2 messages per user). Consider improving onboarding.")
        
        # Error pattern insights
        failed_metrics = [m for m in metrics if not m.success]
        if failed_metrics:
            error_types = defaultdict(int)
            for m in failed_metrics:
                error_types[m.error_type or "UNKNOWN"] += 1
            
            most_common_error = max(error_types.items(), key=lambda x: x[1])
            insights.append(f"🚨 Most frequent error: {most_common_error[0]} ({most_common_error[1]} times)")
        
        # Twitter enhancement insights
        twitter_enhanced = sum(1 for m in metrics if m.enhanced_by_twitter)
        if twitter_enhanced > 0:
            enhancement_rate = twitter_enhanced / len(metrics) * 100
            insights.append(f"🐦 Twitter enhancement used in {enhancement_rate:.1f}% of messages")
        
        return insights[:8]  # Return top 8 insights
    
    def _calculate_health_score(self, metrics: List[MessageMetric]) -> int:
        """Calculate overall chatbot health score (0-100)"""
        if not metrics:
            return 0
        
        # Success rate weight (40%)
        success_rate = sum(1 for m in metrics if m.success) / len(metrics)
        success_score = success_rate * 40
        
        # Response time weight (30%)
        avg_time = statistics.mean([m.processing_time for m in metrics])
        if avg_time <= self.FAST_RESPONSE_THRESHOLD:
            time_score = 30
        elif avg_time <= self.SLOW_RESPONSE_THRESHOLD:
            time_score = 20
        else:
            time_score = max(0, 30 - (avg_time - self.SLOW_RESPONSE_THRESHOLD) * 5)
        
        # User engagement weight (20%)
        unique_users = len(set(m.user_id for m in metrics))
        messages_per_user = len(metrics) / unique_users
        if messages_per_user >= 5:
            engagement_score = 20
        elif messages_per_user >= 2:
            engagement_score = 15
        else:
            engagement_score = 10
        
        # Error diversity weight (10%) - fewer error types is better
        error_types = set(m.error_type for m in metrics if not m.success and m.error_type)
        if len(error_types) == 0:
            error_diversity_score = 10
        elif len(error_types) <= 2:
            error_diversity_score = 7
        else:
            error_diversity_score = max(0, 10 - len(error_types))
        
        total_score = success_score + time_score + engagement_score + error_diversity_score
        return min(100, max(0, int(total_score)))
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get quick metrics summary for dashboards"""
        metrics_1h = self.get_performance_metrics(1)
        metrics_24h = self.get_performance_metrics(24)
        
        return {
            "current_hour": {
                "messages": metrics_1h.get("overall", {}).get("total_messages", 0),
                "success_rate": metrics_1h.get("overall", {}).get("success_rate", 0),
                "avg_response_time": metrics_1h.get("response_times", {}).get("average", 0)
            },
            "last_24_hours": {
                "messages": metrics_24h.get("overall", {}).get("total_messages", 0),
                "success_rate": metrics_24h.get("overall", {}).get("success_rate", 0),
                "avg_response_time": metrics_24h.get("response_times", {}).get("average", 0),
                "unique_users": metrics_24h.get("users", {}).get("unique_users", 0)
            },
            "health_score": metrics_24h.get("health_score", 0),
            "top_insights": metrics_24h.get("insights", [])[:3],
            "status": "healthy" if metrics_24h.get("health_score", 0) >= 80 else 
                     "warning" if metrics_24h.get("health_score", 0) >= 60 else "critical"
        }
    
    def get_user_analytics(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get user-specific analytics"""
        if user_id and user_id in self.user_sessions:
            session = self.user_sessions[user_id]
            user_metrics = [m for m in self.metrics if m.user_id == user_id]
            
            return {
                "user_id": user_id,
                "session_info": {
                    "first_seen": session.first_seen.isoformat(),
                    "last_seen": session.last_seen.isoformat(),
                    "session_duration": round(session.session_duration, 2),
                    "total_messages": session.total_messages,
                    "success_rate": round(session.success_rate, 2),
                    "avg_response_time": round(session.avg_response_time, 2),
                    "message_types": dict(session.message_types)
                },
                "recent_activity": [m.to_dict() for m in user_metrics[-10:]]  # Last 10 messages
            }
        else:
            # Return aggregate user stats
            total_users = len(self.user_sessions)
            active_24h = len([s for s in self.user_sessions.values() 
                            if s.last_seen >= datetime.now() - timedelta(hours=24)])
            
            return {
                "total_users": total_users,
                "active_users_24h": active_24h,
                "avg_messages_per_user": round(sum(s.total_messages for s in self.user_sessions.values()) / total_users, 2) if total_users > 0 else 0,
                "top_users": sorted(
                    [(s.user_id, s.total_messages) for s in self.user_sessions.values()],
                    key=lambda x: x[1], reverse=True
                )[:10]
            }

# Global metrics service instance
metrics_service = ChatbotMetricsService()