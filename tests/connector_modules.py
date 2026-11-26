"""Connectors live in connectors/<id>/, outside the app package, and are loaded by the registry.

Tests get a connector's module (and so its classes and parse functions) the same way the service
does, instead of importing it by a dotted path.
"""
import sys

from app.connectors import registry


def module_of(retailer_id: str):
    registry.refresh()
    return sys.modules[type(registry.ENTRIES[retailer_id].instance).__module__]
