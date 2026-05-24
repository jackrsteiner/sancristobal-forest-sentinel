"""The lightweight web dashboard (Slice 2, E10).

A FastAPI backend serving JSON/GeoJSON over the PostGIS catalog, plus a static Leaflet
map page. The dashboard is read-only and, in this slice, unauthenticated.
"""

from forest_sentinel.dashboard.app import app, create_app

__all__ = ["app", "create_app"]
