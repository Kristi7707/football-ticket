# Daily Football Ticket

Analyzes today's football fixtures, builds a ticket of **5-7 picks** with combined odds
of about 30-50 (stake **10+**), saves it as an HTML file, and **emails it to you every day**.

On days without enough usable matches you get a short "no ticket today" email instead,
so a quiet inbox never means a silent failure.

Markets used: **1X2, Double Chance, Over/Under goals, BTTS, Corners**.
Handicap markets are never used. Football only.

## Requirements

- Python 3.8+ (no extra packages needed — standard library only)
- A free token from [football-data.org](https://www.football-data.org/client/register)
  (free tier never expires: Premier League, Championship, Bundesliga, Serie A, La Liga,
  Ligue 1, Eredivisie, Primeira Liga, Brazilian Serie A, Champions League)

Probabilities are computed from each team's goals scored/conceded in the league table
(attack and defence strength vs. league average, plus home advantage). The older
API-Football provider is still supported: set `"provider": "api-football"` and `api_key`.

### European cups

- **Champions League (`CL`)** is on the free plan. A team has played only 1-3 cup games
  for most of the league phase, so cup matches are rated from each team's **domestic
  league table** instead. A match is skipped (and the log says why) when one of its teams
  plays in a league the app has no table for, e.g. Norway, Turkey or Scotland.
  This is an approximation: each team is measured against *its own* league's average, so
  the model does not know how strong one league is compared with another.
- **Europa League (`EL`)** and **Conference League (`UCL`)** are in the `competitions`
  list, but football-data.org only serves them on a paid plan. On the free plan they
  simply produce no fixtures (the log says "not available on your plan" if the API
  refuses them). To rate more teams, add their domestic leagues to `competitions`
  (also paid plans), e.g. `"SPL"` for Scotland or `"BJL"` for Belgium.

## Setup

1. **Token**: register at https://www.football-data.org/client/register, copy the token
   from the email they send, and paste it into `config.json` → `"football_data_token"`.
   For the GitHub Actions run, add it as the repository secret `FOOTBALL_DATA_TOKEN`.

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

## Daily automation (GitHub Actions + cron-job.org, 13:00 Albania time)

GitHub's own scheduler can start a run hours late, so the workflow has no `schedule:`.
Instead a free timer at [cron-job.org](https://cron-job.org) starts it at exactly 13:00
`Europe/Tirane` every day (daylight saving is handled by the timezone setting). A started
run takes about a minute, so the email arrives a minute or two after 13:00.

1. **GitHub token**: GitHub → Settings → Developer settings → Personal access tokens →
   *Fine-grained tokens* → Generate. Repository access: **only** `football-ticket`.
   Permissions → Repository → **Actions: Read and write**. Pick the longest expiry and
   note the date: when it expires the timer starts failing and must get a new token.
2. **cron-job.org** → Create cronjob:
   - URL: `https://api.github.com/repos/Kristi7707/football-ticket/actions/workflows/daily-ticket.yml/dispatches`
   - Schedule: *Every day* at 13:00, timezone **Europe/Tirane**
   - Advanced → Request method **POST**, request body `{"ref":"main"}`
   - Advanced → Headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your token>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - Turn on failure notifications. GitHub answers `204 No Content` on success.
3. **Test it**: use *Test run* in cron-job.org, or Actions → Daily Football Ticket →
   *Run workflow*. A new run should appear within seconds.

## Daily automation, alternative (Windows Task Scheduler)

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
