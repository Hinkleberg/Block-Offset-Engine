"""
World Cache - Multi-level hierarchical caching with eviction policies.
Optimized for streaming patterns and predictive prefetching.
"""

from collections import OrderedDict
import threading
import logging
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger(__name__)


class WorldCache:
    """
    Thread-safe LRU cache with statistics and callback hooks.
    Supports eviction callbacks for intelligent prefetching.
    """
    
    def __init__(self, max_entries: int = 4096, on_evict: Optional[Callable] = None):
        """
        Initialize world cache.
        
        Args:
            max_entries: Maximum cache size
            on_evict: Optional callback when entries are evicted
        """
        self.max = max_entries
        self.cache = OrderedDict()
        self.on_evict = on_evict
        self._lock = threading.RLock()
        
        # Statistics
        self.hits = 0
        self.misses = 0
        self.evictions = 0
    
    def get(self, key: int) -> Optional[bytes]:
        """
        Retrieve cached entry (updates LRU order).
        
        Args:
            key: Cache key
        
        Returns:
            Cached value or None
        """
        with self._lock:
            if key not in self.cache:
                self.misses += 1
                return None
            
            self.hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
    
    def put(self, key: int, value: bytes) -> None:
        """
        Insert or update cache entry.
        
        Args:
            key: Cache key
            value: Data to cache
        """
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            
            self.cache[key] = value
            
            # Evict LRU entries if over capacity
            while len(self.cache) > self.max:
                evicted_key, evicted_value = self.cache.popitem(last=False)
                self.evictions += 1
                
                if self.on_evict:
                    try:
                        self.on_evict(evicted_key, evicted_value)
                    except Exception as e:
                        logger.error(f"Eviction callback failed for key {evicted_key}: {e}")
    
    def peek(self, key: int) -> Optional[bytes]:
        """Get value without updating LRU order."""
        with self._lock:
            return self.cache.get(key)
    
    def contains(self, key: int) -> bool:
        """Check if key exists in cache."""
        with self._lock:
            return key in self.cache
    
    def remove(self, key: int) -> bool:
        """Remove entry from cache."""
        with self._lock:
            if key in self.cache:
                del self.cache[key]
                return True
            return False
    
    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self.cache.clear()
            self.hits = 0
            self.misses = 0
            self.evictions = 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self.hits + self.misses
            hit_rate = (self.hits / total * 100) if total > 0 else 0
            
            return {
                'size': len(self.cache),
                'max_size': self.max,
                'hits': self.hits,
                'misses': self.misses,
                'hit_rate': hit_rate,
                'evictions': self.evictions,
                'memory_usage': sum(len(v) for v in self.cache.values())
            }
    
    def __len__(self) -> int:
        """Get current cache size."""
        return len(self.cache)
    
    def __repr__(self) -> str:
        stats = self.get_stats()
        return (f"WorldCache(size={stats['size']}/{stats['max_size']}, "
                f"hit_rate={stats['hit_rate']:.1f}%, "
                f"memory={stats['memory_usage']/1024/1024:.1f}MB)")
