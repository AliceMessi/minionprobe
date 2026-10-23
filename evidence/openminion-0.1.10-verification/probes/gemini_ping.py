import os, time, httpx
key = os.environ.get("GEMINI_API_KEY", "")
t = time.time()
r = httpx.post(
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    headers={"Authorization": "Bearer " + key},
    json={"model": "gemini-3.1-flash-lite", "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 5},
    timeout=60,
)
print("status", r.status_code, "latency_ms", int((time.time() - t) * 1000))
raise SystemExit(0 if r.status_code == 200 else 1)
