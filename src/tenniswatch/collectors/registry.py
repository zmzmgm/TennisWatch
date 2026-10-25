"""Default adapters. New sites are registered here without editing collection logic."""
from .base import CollectorRegistry
from .gangseo import GangseoCollector


def default_registry() -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(GangseoCollector())
    return registry
