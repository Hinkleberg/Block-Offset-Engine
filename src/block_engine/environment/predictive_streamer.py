"""
Predictive Streamer - Anticipate player movement and prefetch blocks from storage matrix.
Critical optimization for reducing I/O latency via predictive loading.
"""

from typing import Tuple, List, Optional
import logging

logger = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]


class PredictiveStreamer:
    """
    Predict player trajectory and prefetch blocks from NVMe storage matrix.
    Uses velocity to determine next accessed regions.
    """
    
    def __init__(self, prediction_horizon: float = 5.0, 
                 prefetch_radius: int = 8):
        """
        Initialize predictive streamer.
        
        Args:
            prediction_horizon: Seconds ahead to predict
            prefetch_radius: Block radius to prefetch around predicted position
        """
        self.prediction_horizon = prediction_horizon
        self.prefetch_radius = prefetch_radius
    
    def predict(self, pos: Vec3, vel: Vec3, 
                seconds: Optional[float] = None) -> Vec3:
        """
        Predict future position based on velocity.
        
        Args:
            pos: Current position
            vel: Current velocity
            seconds: Prediction horizon (uses instance default if None)
        
        Returns:
            Predicted position
        """
        t = seconds if seconds is not None else self.prediction_horizon
        return tuple(pos[i] + vel[i] * t for i in range(3))
    
    def predict_trajectory(self, pos: Vec3, vel: Vec3, 
                          num_samples: int = 10) -> List[Vec3]:
        """
        Get predicted trajectory waypoints.
        
        Args:
            pos: Current position
            vel: Current velocity
            num_samples: Number of waypoints along trajectory
        
        Returns:
            List of predicted positions
        """
        trajectory = [pos]
        dt = self.prediction_horizon / num_samples
        
        current = pos
        for _ in range(num_samples - 1):
            current = tuple(current[i] + vel[i] * dt for i in range(3))
            trajectory.append(current)
        
        return trajectory
    
    def get_prefetch_blocks(self, pos: Vec3, vel: Vec3) -> List[Tuple[int, int, int]]:
        """
        Get block addresses to prefetch.
        
        Args:
            pos: Current position
            vel: Current velocity
        
        Returns:
            List of (x, y, z) block coordinates to prefetch
        """
        predicted = self.predict(pos, vel)
        
        # Convert to block coordinates (assuming 1 block = 1 unit)
        px, py, pz = int(predicted[0]), int(predicted[1]), int(predicted[2])
        
        blocks = []
        for dx in range(-self.prefetch_radius, self.prefetch_radius + 1):
            for dy in range(-self.prefetch_radius, self.prefetch_radius + 1):
                for dz in range(-self.prefetch_radius, self.prefetch_radius + 1):
                    blocks.append((px + dx, py + dy, pz + dz))
        
        return blocks
    
    def should_prefetch(self, pos: Vec3, vel: Vec3, 
                       last_prefetch_pos: Vec3, 
                       distance_threshold: float = 2.0) -> bool:
        """
        Determine if prefetch is needed based on movement.
        
        Args:
            pos: Current position
            vel: Current velocity
            last_prefetch_pos: Position of last prefetch
            distance_threshold: Distance to move before refetching
        
        Returns:
            True if prefetch should occur
        """
        dx = pos[0] - last_prefetch_pos[0]
        dy = pos[1] - last_prefetch_pos[1]
        dz = pos[2] - last_prefetch_pos[2]
        
        distance = (dx*dx + dy*dy + dz*dz) ** 0.5
        return distance > distance_threshold
    
    def adaptive_horizon(self, speed: float, 
                        base_horizon: float = 5.0,
                        speed_factor: float = 0.5) -> float:
        """
        Adapt prediction horizon based on speed.
        
        Args:
            speed: Current speed magnitude
            base_horizon: Base prediction horizon
            speed_factor: Factor to scale with speed
        
        Returns:
            Adapted prediction horizon
        """
        return base_horizon + (speed * speed_factor)
