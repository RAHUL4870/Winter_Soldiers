import json
import urllib.request

req = urllib.request.Request(
    "http://localhost:8000/api/auth/login",
    data=json.dumps({"email": "admin@nexafreight.local", "password": "admin123"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
resp = urllib.request.urlopen(req)
token = json.loads(resp.read().decode("utf-8"))["access_token"]

req_sse = urllib.request.Request(
    "http://localhost:8000/api/map/positions/stream",
    headers={"Authorization": f"Bearer {token}"},
)

with urllib.request.urlopen(req_sse, timeout=10) as sse_resp:
    print("SSE connected! Reading first 5 lines:")
    for _ in range(5):
        line = sse_resp.readline().decode("utf-8")
        print("SSE line:", line.strip())
