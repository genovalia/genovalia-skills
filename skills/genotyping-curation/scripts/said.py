import json, base64, blake3

def said(obj):
    """CESR Blake3-256 SAID: 'd' placeholder of 44 '#', compact JSON in insertion order."""
    o = dict(obj); o["d"] = "#" * 44
    raw = json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode()
    return "E" + base64.urlsafe_b64encode(b"\x00" + blake3.blake3(raw).digest()).decode()[1:]
