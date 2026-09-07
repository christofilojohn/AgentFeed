"""Importing this package registers every adapter in sources.base.REGISTRY."""
from . import afp, europepmc, html_list, openalex, rss, search  # noqa: F401
from .base import REGISTRY, RawItem, get_adapter, register  # noqa: F401

__all__ = ["REGISTRY", "RawItem", "get_adapter", "register"]
