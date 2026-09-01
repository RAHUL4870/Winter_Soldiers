"""Test live running map endpoints."""

from __future__ import annotations

import json
import urllib.request

req = urllib.request.Request(
    "http://localhost:8000/api/auth/login",
    data=json.dumps({"email": "admin@nexafreight.local", "password": "admin123"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
resp = urllib.request.urlopen(req)
token = json.loads(resp.read().decode("utf-8"))["access_token"]
print("Login successful! Token acquired.")

req_snap = urllib.request.Request(
    "http://localhost:8000/api/map/positions/snapshot",
    headers={"Authorization": f"Bearer {token}"},
)
resp_snap = urllib.request.urlopen(req_snap)
body = resp_snap.read().decode("utf-8")
print("Snapshot raw response:\n", body[:300])

req_routes = urllib.request.Request(
    "http://localhost:8000/api/map/routes",
    headers={"Authorization": f"Bearer {token}"},
)
resp_routes = urllib.request.urlopen(req_routes)
routes = json.loads(resp_routes.read().decode("utf-8"))
print(f"Routes features count: {len(routes.get('features', []))}")

req_health = urllib.request.Request(
    "http://localhost:8000/api/map/feed-health",
    headers={"Authorization": f"Bearer {token}"},
)
resp_health = urllib.request.urlopen(req_health)
health = json.loads(resp_health.read().decode("utf-8"))
print("Feed health:\n", json.dumps(health, indent=2))
