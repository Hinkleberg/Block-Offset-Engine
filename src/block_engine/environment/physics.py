"""
Physics Engine - Client-side physics simulation with deterministic integration.
Runs on frontend, sends I/O operations to backend matrix engines.
"""

from typing import Tuple
import logging

logger = logging.getLogger(__name__)

# Vector type alias
Vec3 = Tuple[float, float, float]


def integrate(position: Vec3, velocity: Vec3, dt: float) -> Vec3:
    """
    Perform Verlet integration step (semi-implicit Euler).
    
    Args:
        position: Current position (x, y, z)
        velocity: Current velocity (vx, vy, vz)
        dt: Delta time
    
    Returns:
        New position after integration
    """
    return tuple(position[i] + velocity[i] * dt for i in range(3))


def apply_acceleration(velocity: Vec3, acceleration: Vec3, dt: float) -> Vec3:
    """Apply acceleration to velocity over time step."""
    return tuple(velocity[i] + acceleration[i] * dt for i in range(3))


def apply_gravity(velocity: Vec3, dt: float, gravity: float = 9.81) -> Vec3:
    """Apply downward gravity to velocity."""
    vx, vy, vz = velocity
    return (vx, vy - gravity * dt, vz)


def clamp_velocity(velocity: Vec3, max_speed: float) -> Vec3:
    """Clamp velocity to maximum speed."""
    vx, vy, vz = velocity
    speed_sq = vx*vx + vy*vy + vz*vz
    max_speed_sq = max_speed * max_speed
    
    if speed_sq > max_speed_sq:
        scale = max_speed / (speed_sq ** 0.5)
        return (vx * scale, vy * scale, vz * scale)
    
    return velocity


def apply_friction(velocity: Vec3, friction: float = 0.95) -> Vec3:
    """Apply friction damping to velocity."""
    return tuple(v * friction for v in velocity)


def distance_sq(p1: Vec3, p2: Vec3) -> float:
    """Squared distance between two positions."""
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    dz = p1[2] - p2[2]
    return dx*dx + dy*dy + dz*dz


def distance(p1: Vec3, p2: Vec3) -> float:
    """Euclidean distance between two positions."""
    return distance_sq(p1, p2) ** 0.5


class PhysicsUpdate:
    """Physics state update for client->server I/O."""
    
    def __init__(self, entity_id: int, position: Vec3, velocity: Vec3):
        self.entity_id = entity_id
        self.position = position
        self.velocity = velocity
    
    def to_dict(self):
        """Serialize for I/O operations."""
        return {
            'entity_id': self.entity_id,
            'position': self.position,
            'velocity': self.velocity
        }
