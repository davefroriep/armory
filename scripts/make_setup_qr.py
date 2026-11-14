"""Makes one-time Intervals.icu setup QR codes for Armory Fit (the key never goes into the repo).

  python scripts/make_setup_qr.py [--out DIR]

Key source (first that exists): env ARMORY_INTERVALS_KEY, then DEFAULT_API_KEY in
j:\\My Drive\\Misc\\Health\\Scripts\\sync_intervals.py (the same key the intervals-icu skill uses).
Scan the PNGs with the phone camera: the PWA one opens the web app, the APP one opens the Android app.
The PNGs contain your key - keep them local, delete after use (default folder is git-ignored).
"""
import argparse
import os
import re
import sys

import qrcode

SYNC = r"j:\My Drive\Misc\Health\Scripts\sync_intervals.py"
PWA = "https://davefroriep.github.io/armory/#setup={key}&aid={aid}"
APP = "armory://setup?key={key}&aid={aid}"


def find_key() -> tuple[str, str]:
    aid = "i758105"
    key = os.environ.get("ARMORY_INTERVALS_KEY", "").strip()
    if not key and os.path.exists(SYNC):
        src = open(SYNC, encoding="utf8").read()
        m = re.search(r'DEFAULT_API_KEY\s*=\s*"([A-Za-z0-9]{16,64})"', src)
        if m:
            key = m.group(1)
        a = re.search(r'"athlete_id"\s*:\s*"?(i\d+)', src) or re.search(r'DEFAULT_ATHLETE_ID\s*=\s*"(i\d+)"', src)
        if a:
            aid = a.group(1)
    if not key:
        sys.exit("No key found. Set ARMORY_INTERVALS_KEY or fix SYNC path in this script.")
    return key, aid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "local-secrets"))
    out = os.path.abspath(ap.parse_args().out)
    os.makedirs(out, exist_ok=True)
    key, aid = find_key()
    for name, tmpl in (("setup_pwa.png", PWA), ("setup_app.png", APP)):
        img = qrcode.make(tmpl.format(key=key, aid=aid), box_size=10, border=4)
        img.save(os.path.join(out, name))
        print("wrote", os.path.join(out, name))


if __name__ == "__main__":
    main()
