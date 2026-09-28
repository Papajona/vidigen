"""Catch accidental duplicate route blocks before they reach deployment."""
import os
from collections import Counter

os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN', 'test-token')

from fastapi.routing import APIRoute
from gateway.server import app


def test_api_routes_are_registered_once():
    registrations = Counter(
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    )
    duplicates = {key: count for key, count in registrations.items() if count > 1}
    assert not duplicates, f'Duplicate API routes: {duplicates}'


def test_openapi_covers_each_registered_operation():
    schema = app.openapi()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            for method in route.methods:
                assert method.lower() in schema['paths'][route.path_format]
