"""Local HTTP API for Restream Studio."""

from .routes import ApiDependencies, install_routes

__all__ = ["ApiDependencies", "install_routes"]
