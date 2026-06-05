import pathlib
import re
import urllib.request

base = "https://bcgame61.com"
root = pathlib.Path("data/bc_assets")
root.mkdir(exist_ok=True)

req = urllib.request.Request(base + "/game/crash", headers={"User-Agent": "Mozilla/5.0"})
html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
assets = sorted(set(re.findall(r"/assets/[^\"']+?\.js", html)))
print("ASSETS", len(assets))
for asset in assets:
    print(asset)

needles = ["twice-bet", "cashout", "betPayout", "/g/cm", "CRASH", "Crash"]
for asset in assets:
    name = asset.split("/")[-1]
    path = root / name
    if not path.exists() or path.stat().st_size < 1000:
        try:
            req = urllib.request.Request(base + asset, headers={"User-Agent": "Mozilla/5.0"})
            path.write_bytes(urllib.request.urlopen(req, timeout=25).read())
        except Exception as exc:
            print("FETCH_ERR", asset, exc)
            continue
    text = path.read_text(errors="ignore")
    hits = [needle for needle in needles if needle in text]
    if hits:
        print("HITS", name, hits, "size", path.stat().st_size)
        if "twice-bet" in hits:
            idx = text.find("twice-bet")
            print(text[max(0, idx - 1600):idx + 1600])
