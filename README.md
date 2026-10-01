# Daily Football Ticket

Analyzes today's football fixtures, builds a ticket of up to **7 high-confidence picks**
(stake **10+**), saves it as an HTML file, and **emails it to you every day**.

Markets used: **1X2, Double Chance, Over/Under goals, BTTS, Corners**.
Handicap markets are never used. Football only.

## Requirements

- Python 3.8+ (no extra packages needed — standard library only)
- A free API key from [API-Football](https://dashboard.api-football.com) (100 requests/day free)

## Setup

1. **API key**: create a free account at https://dashboard.api-football.com,
   copy your API key, and paste it into `config.json` → `"api_key"`.

2. **Email** (optional but recommended): in `config.json` → `"email"`:
   - Set `"enabled": true`
   - Easiest option: a **Gmail account with an App Password**
     (Google Account → Security → 2-Step Verification → App passwords).
     Use the app password as `"password"`, your Gmail address as `"username"` and `"from"`.
   - `"to"` is already set to your address.
   - Note: Outlook/Live personal accounts no longer allow simple SMTP login,
     which is why Gmail is the recommended sender.

3. **Try it** (works even before you have an API key):
   ```
   python main.py --demo
   ```

4. **Real run** (takes a few minutes — the free API plan allows 10 requests/minute):
   ```
   python main.py
   ```

## Daily automation (Windows Task Scheduler)

Run this once in a terminal (adjust the path if you move the folder, and the time if you
want a different hour — `09:00` means the ticket is emailed at 9 AM daily):

```
schtasks /Create /SC DAILY /TN "DailyFootballTicket" /TR "\"C:\path\to\football-ticket\run_daily.bat\"" /ST 09:00
```

Output of each run is appended to `run_log.txt`; every ticket is also saved in `tickets\`.

## Tuning (config.json)

| Setting | Meaning |
|---|---|
| `stake` | Ticket stake (minimum 10 is enforced) |
| `min_picks` / `max_picks` | Picks per ticket (5 to 7, one per match) |
| `target_odds_min` / `target_odds_max` | Combined odds the ticket should land in (default 30-50) |
| `min_pick_probability` | Only picks at/above this confidence are considered (default 0.42) |
| `min_odds_per_pick` | Skips picks whose odds would be too tiny (default 1.3) |
| `include_corners` / `max_corners_picks` | Allow corner picks and cap how many |
| `league_ids` | Which leagues to scan (API-Football league IDs); empty = all |
| `max_matches_analyzed` | Cap on fixtures analyzed per day (keeps within free API limits) |

## Honest note on high-odds tickets

Combined odds of 30-50 across 5-7 matches need each pick at roughly 1.6-2.2 odds, i.e.
about 42-57% likely. Multiplied together, such a ticket wins only around 1-3% of the time.
The email shows the **ticket hit probability** so you always see the real number.
For tickets that win more often, set `target_odds_min`/`target_odds_max` lower
(e.g. 3-6) and raise `min_pick_probability` (e.g. 0.68). Corner probabilities are
league-average priors, not per-match data. Odds are estimates; check your bookmaker.
