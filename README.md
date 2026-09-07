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
| `max_picks` | Picks per ticket (max 7) |
| `min_pick_probability` | Only picks at/above this confidence are used (default 0.68) |
| `min_odds_per_pick` | Skips picks whose odds would be too tiny (default 1.15) |
| `include_corners` / `max_corners_picks` | Allow corner picks and cap how many |
| `league_ids` | Which leagues to scan (API-Football league IDs); empty = all |
| `max_matches_analyzed` | Cap on fixtures analyzed per day (keeps within free API limits) |

## Honest note on the "very low losing rate"

Each pick is chosen at roughly 68–95% confidence, but a 7-leg accumulator multiplies
the risks: seven picks at 80% each win together only ~21% of the time. The email shows
the **ticket hit probability** so you always see the real number. Lower `max_picks`
(e.g. 3–4) if you want tickets that win more often at lower odds. Never bet more than
you can afford to lose.
