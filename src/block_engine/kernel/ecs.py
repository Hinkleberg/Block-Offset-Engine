"""
Entity Component System (ECS) - Core game world state management.
Optimized for distributed simulation across matrix engines.
"""

import threading
import logging
from typing import Dict, Any, List, Optional, Type
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Component:
    """Base class for all components."""
    pass


class World:
    """
    Thread-safe ECS world managing entities and components.
    Supports efficient component queries and batch operations.
    """
    
    def __init__(self):
        """Initialize ECS world."""
        self.entities: Dict[int, Dict[Type, Component]] = {}
        self.next_id = 1
        self._lock = threading.RLock()
        self._component_indices: Dict[Type, Dict[int, Component]] = {}
    
    def create(self, **components) -> int:
        """
        Create new entity with components.
        
        Args:
            **components: Component instances (keyed by type name)
        
        Returns:
            New entity ID
        """
        with self._lock:
            eid = self.next_id
            self.next_id += 1
            
            # Store components by type for efficient queries
            entity_components: Dict[Type, Component] = {}
            for component in components.values():
                comp_type = type(component)
                entity_components[comp_type] = component
                
                # Index by component type
                if comp_type not in self._component_indices:
                    self._component_indices[comp_type] = {}
                self._component_indices[comp_type][eid] = component
            
            self.entities[eid] = entity_components
            return eid
    
    def add_component(self, eid: int, component: Component) -> bool:
        """Add component to existing entity."""
        with self._lock:
            if eid not in self.entities:
                logger.warning(f"Entity {eid} not found")
                return False
            
            comp_type = type(component)
            self.entities[eid][comp_type] = component
            
            if comp_type not in self._component_indices:
                self._component_indices[comp_type] = {}
            self._component_indices[comp_type][eid] = component
            
            return True
    
    def remove_component(self, eid: int, component_type: Type) -> bool:
        """Remove component from entity."""
        with self._lock:
            if eid not in self.entities:
                return False
            
            entity_comps = self.entities[eid]
            if component_type not in entity_comps:
                return False
            
            del entity_comps[component_type]
            if component_type in self._component_indices:
                self._component_indices[component_type].pop(eid, None)
            
            return True
    
    def get_component(self, eid: int, component_type: Type) -> Optional[Component]:
        """Get component from entity."""
        with self._lock:
            if eid not in self.entities:
                return None
            return self.entities[eid].get(component_type)
    
    def has_component(self, eid: int, component_type: Type) -> bool:
        """Check if entity has component."""
        with self._lock:
            return (eid in self.entities and 
                    component_type in self.entities[eid])
    
    def delete(self, eid: int) -> bool:
        """Delete entity and all components."""
        with self._lock:
            if eid not in self.entities:
                return False
            
            entity_comps = self.entities.pop(eid)
            
            # Remove from component indices
            for comp_type in entity_comps:
                if comp_type in self._component_indices:
                    self._component_indices[comp_type].pop(eid, None)
            
            return True
    
    def query(self, *component_types: Type) -> List[int]:
        """
        Find all entities with specific components.
        
        Args:
            *component_types: Types of components to search for
        
        Returns:
            List of matching entity IDs
        """
        if not component_types:
            with self._lock:
                return list(self.entities.keys())
        
        with self._lock:
            # Start with entities containing first component type
            if component_types[0] not in self._component_indices:
                return []
            
            matches = set(self._component_indices[component_types[0]].keys())
            
            # Intersect with other component types
            for comp_type in component_types[1:]:
                if comp_type not in self._component_indices:
                    return []
                matches &= set(self._component_indices[comp_type].keys())
            
            return list(matches)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get world statistics."""
        with self._lock:
            return {
                'total_entities': len(self.entities),
                'component_types': len(self._component_indices),
                'next_entity_id': self.next_id
            }
