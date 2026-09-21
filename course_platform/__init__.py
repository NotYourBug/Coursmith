"""Commercial course delivery platform."""

__version__ = "0.1.0"

from .settings import Settings, load_settings

__all__ = ["Settings", "load_settings", "__version__"]
