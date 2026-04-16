"""Probe Garant HTML for ПП 1875 Приложение 1."""
import urllib.request
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

url = "https://base.garant.ru/411197447/53f89421bbdaf741eb2d1ecc4ddb4c33/"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read()

html = raw.decode("cp1251", errors="replace")
print(f"Total chars: {len(html)}")

# Find main document body
m = re.search(r'<div[^>]*id="document"[^>]*>', html)
if m:
    print(f"'document' div starts at: {m.start()}")
else:
    print("no 'document' div")

# Look for table tags
tables = re.findall(r"<table[^>]*>", html)
print(f"Tables found: {len(tables)}")

# Look for distinctive markers
for marker in ["\u041f\u0435\u0440\u0435\u0447\u0435\u043d\u044c", "\u0437\u0430\u043f\u0440\u0435\u0442",
               "23.12.2024", "1875", "\u041e\u041a\u041f\u0414", "\u041f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435"]:
    idx = html.find(marker)
    print(f"  {marker!r}: {idx}")

# Extract a portion of the main document content
start = html.find("<h2")
if start == -1:
    start = html.find("\u041f\u0435\u0440\u0435\u0447\u0435\u043d\u044c")
print()
print("=== Sample from start of content ===")
print(html[start : start + 3000])
