"""Download finished matches from football-data.org for back-testing the model.

Usage:  python tools/fetch_history.py OUTDIR SEASON [SEASON ...]
        (SEASON is the year the season starts, e.g. 2025 for 2025/26)

Writes OUTDIR/<competition>_<season>.json with the fields the back-test needs and
prints how many matches each competition/season returned, or why it was refused.
Needs the same token as the daily run (FOOTBALL_DATA_TOKEN or config.json).
"""

import json
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as app  # noqa: E402  (uses its load_config / fd_get, incl. retries)

CODES = ["PL", "ELC", "BL1", "SA", "PD", "FL1", "DED", "PPL", "BSA", "CL"]


def slim(m):
    ft = (m.get("score") or {}).get("fullTime") or {}
    ht = (m.get("score") or {}).get("halfTime") or {}
    return {
        "date": m.get("utcDate"), "matchday": m.get("matchday"), "stage": m.get("stage"),
        "status": m.get("status"),
        "home": {"id": m["homeTeam"].get("id"), "name": m["homeTeam"].get("shortName")
                 or m["homeTeam"].get("name")},
        "away": {"id": m["awayTeam"].get("id"), "name": m["awayTeam"].get("shortName")
                 or m["awayTeam"].get("name")},
        "hg": ft.get("home"), "ag": ft.get("away"),
        "hhg": ht.get("home"), "hag": ht.get("away"),
        "winner": (m.get("score") or {}).get("winner"),
    }


def main():
    out = Path(sys.argv[1])
    seasons = sys.argv[2:]
    out.mkdir(parents=True, exist_ok=True)
    cfg = app.load_config()
    first = True
    for season in seasons:
        for code in CODES:
            if not first:
                time.sleep(6.5)  # free plan: 10 requests/minute
            first = False
            try:
                data = app.fd_get(cfg, "/competitions/{}/matches".format(code),
                                  {"season": season})
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:160]
                except Exception:
                    pass
                print("{} {}: refused HTTP {} {}".format(code, season, e.code, body))
                continue
            except Exception as e:  # keep going: report and move on
                print("{} {}: failed {}".format(code, season, e))
                continue
            matches = [slim(m) for m in data.get("matches", [])]
            done = [m for m in matches if m["status"] == "FINISHED" and m["hg"] is not None]
            (out / "{}_{}.json".format(code, season)).write_text(
                json.dumps(matches, separators=(",", ":")), encoding="utf-8")
            print("{} {}: {} matches, {} finished".format(code, season, len(matches), len(done)))


if __name__ == "__main__":
    main()
