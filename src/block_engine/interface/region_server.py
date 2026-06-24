"""
Region Server - Matrix engine coordinator managing regional block distribution.
Handles subscription, broadcasting, and consistency across storage engines.
"""

import logging
import threading
from typing import Set, Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class RegionUpdate:
    """Atomic region update for consistency."""
    region_id: int
    timestamp: datetime
    updates: Dict[int, bytes] = field(default_factory=dict)  # {morton_key: block_data}
    version: int = 0


class RegionServer:
    """
    Manages regional block distribution and client subscriptions.
    Coordinates between storage matrix engines and connected clients.
    """
    
    def __init__(self):
        """Initialize region server."""
        self.regions: Dict[int, Set[str]] = {}  # region_id -> set of client_ids
        self._lock = threading.RLock()
        self._callbacks: Dict[int, List[Callable]] = {}  # region_id -> callbacks
        self._region_versions: Dict[int, int] = {}
    
    def subscribe(self, client_id: str, region_id: int) -> bool:
        """
        Subscribe client to region updates.
        
        Args:
            client_id: Unique client identifier
            region_id: Region to subscribe to
        
        Returns:
            True if subscription successful
        """
        with self._lock:
            if region_id not in self.regions:
                self.regions[region_id] = set()
            
            self.regions[region_id].add(client_id)
            logger.info(f"Client {client_id} subscribed to region {region_id}")
            return True
    
    def unsubscribe(self, client_id: str, region_id: int) -> bool:
        """Unsubscribe client from region."""
        with self._lock:
            if region_id in self.regions:
                self.regions[region_id].discard(client_id)
                if not self.regions[region_id]:
                    del self.regions[region_id]
                logger.info(f"Client {client_id} unsubscribed from region {region_id}")
                return True
            return False
    
    def broadcast_update(self, region_id: int, update: RegionUpdate) -> int:
        """
        Broadcast regional update to all subscribers.
        
        Args:
            region_id: Region being updated
            update: Region update containing block changes
        
        Returns:
            Number of clients notified
        """
        with self._lock:
            if region_id not in self.regions:
                return 0
            
            client_ids = self.regions[region_id].copy()
            self._region_versions[region_id] = update.version
            
            # Call registered callbacks
            if region_id in self._callbacks:
                for callback in self._callbacks[region_id]:
                    try:
                        callback(update)
                    except Exception as e:
                        logger.error(f"Callback error for region {region_id}: {e}")
        
        return len(client_ids)
    
    def register_callback(self, region_id: int, callback: Callable[[RegionUpdate], None]) -> None:
        """Register callback for region updates."""
        with self._lock:
            if region_id not in self._callbacks:
                self._callbacks[region_id] = []
            self._callbacks[region_id].append(callback)
    
    def get_subscribers(self, region_id: int) -> List[str]:
        """Get list of clients subscribed to region."""
        with self._lock:
            return list(self.regions.get(region_id, set()))
    
    def get_subscriber_count(self, region_id: int) -> int:
        """Get number of subscribers for region."""
        with self._lock:
            return len(self.regions.get(region_id, set()))
    
    def get_stats(self) -> Dict[str, Any]:
        """Get server statistics."""
        with self._lock:
            total_regions = len(self.regions)
            total_subscribers = sum(len(clients) for clients in self.regions.values())
            
            return {
                'total_regions': total_regions,
                'total_subscribers': total_subscribers,
                'avg_subscribers_per_region': total_subscribers / max(total_regions, 1),
                'regions': {
                    region_id: len(clients) 
                    for region_id, clients in self.regions.items()
                }
            }
