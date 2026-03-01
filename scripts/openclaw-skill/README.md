# Yahoo Fantasy Baseball -- OpenClaw Skill

Automation skill for Yahoo Fantasy Baseball league management. Four scripts handle daily lineup optimization, injury monitoring, waiver wire scouting, and weekly recaps. Each script reads its autonomy level from `config.yaml` and calls the Python API server for data.

## Scripts

| Script | Default Schedule | Action Name | Purpose |
|---|---|---|---|
| `daily-lineup.py` | 10:00 AM daily | `daily_lineup` | Set optimal lineup based on games today |
| `injury-monitor.py` | Every 4 hours | `injury_response` | Detect new injuries, find replacements |
| `waiver-scout.py` | 6:00 AM daily | `waiver_scout` | Scan free agents, cross-ref with punt strategy |
| `weekly-recap.py` | Sunday 8:00 PM | `weekly_recap` | Generate matchup recap with trends |

## Prerequisites

1. The Docker container (`yahoo-fantasy`) must be running
2. The Python API server must be reachable at `http://localhost:8766` (or the URL set in config)
3. Yahoo OAuth credentials must be valid (the API server handles auth)
4. Python 3.9+ with `pyyaml` installed (included in the container)

## Configuration

All settings live in `config.yaml`. Each action has an **autonomy level** that controls what the script does:

| Level | Behavior |
|---|---|
| `auto` | Execute the action automatically, print confirmation |
| `suggest` | Print a recommendation with full details, do not act |
| `alert` | Print a brief notification only |
| `off` | Disabled, script exits immediately |

Default config:

```yaml
actions:
  daily_lineup:
    autonomy: suggest
    schedule: "0 10 * * *"
  injury_response:
    autonomy: alert
    check_interval: 3600
  waiver_scout:
    autonomy: suggest
    schedule: "0 6 * * *"
  weekly_recap:
    autonomy: auto
    schedule: "0 20 * * 0"
  streaming_pitcher:
    autonomy: suggest
    schedule: "0 9 * * *"
```

Notification preferences and the API URL are also configurable:

```yaml
notifications:
  enabled: true
  channel: telegram   # telegram | whatsapp | stdout
  min_priority: medium

league:
  api_url: "http://localhost:8766"
  team_id: ""   # auto-detected from OAuth
```

## Quick Start

Run any script manually from the project root or from the skill directory:

```bash
# Daily lineup check (suggest mode by default)
python3 scripts/openclaw-skill/daily-lineup.py

# Dry run -- preview what auto mode would do without making changes
python3 scripts/openclaw-skill/daily-lineup.py --dry-run

# Injury monitor
python3 scripts/openclaw-skill/injury-monitor.py
python3 scripts/openclaw-skill/injury-monitor.py --dry-run

# Waiver scout
python3 scripts/openclaw-skill/waiver-scout.py
python3 scripts/openclaw-skill/waiver-scout.py --dry-run

# Weekly recap
python3 scripts/openclaw-skill/weekly-recap.py
python3 scripts/openclaw-skill/weekly-recap.py --dry-run
```

## Cron Setup

Add these to your crontab (`crontab -e`) or use the schedules in `manifest.json`:

```cron
# Daily lineup optimization at 10:00 AM
0 10 * * *   cd /path/to/yahoo-fantasy/scripts/openclaw-skill && python3 daily-lineup.py >> /var/log/yf-lineup.log 2>&1

# Injury monitor every 4 hours
0 */4 * * *  cd /path/to/yahoo-fantasy/scripts/openclaw-skill && python3 injury-monitor.py >> /var/log/yf-injury.log 2>&1

# Waiver scout at 6:00 AM
0 6 * * *    cd /path/to/yahoo-fantasy/scripts/openclaw-skill && python3 waiver-scout.py >> /var/log/yf-waivers.log 2>&1

# Weekly recap Sunday at 8:00 PM
0 20 * * 0   cd /path/to/yahoo-fantasy/scripts/openclaw-skill && python3 weekly-recap.py >> /var/log/yf-recap.log 2>&1
```

## Environment Variable Overrides

Override any config value without editing `config.yaml`. The config loader checks these env vars at startup:

### Autonomy levels

Pattern: `YF_ACTION_<ACTION_NAME>_AUTONOMY=<level>`

```bash
# Override daily lineup to full auto
export YF_ACTION_DAILY_LINEUP_AUTONOMY=auto

# Disable injury response
export YF_ACTION_INJURY_RESPONSE_AUTONOMY=off

# Override waiver scout to alert-only
export YF_ACTION_WAIVER_SCOUT_AUTONOMY=alert

# Override weekly recap
export YF_ACTION_WEEKLY_RECAP_AUTONOMY=suggest
```

### Notifications

```bash
export YF_NOTIFICATIONS_ENABLED=true
export YF_NOTIFICATIONS_CHANNEL=telegram
```

### API URL

```bash
export YF_LEAGUE_API_URL=http://localhost:8766
```

## Autonomy Behavior by Script

### daily-lineup.py

Calls `/api/lineup-optimize` to find off-day starters and bench players with games.

| Level | Behavior |
|---|---|
| `auto` | Calls `/api/set-lineup` to apply swaps, prints confirmation with changes made |
| `suggest` | Prints full optimization report: swaps, off-day starters, bench with games, IL list |
| `alert` | Prints one-line summary: "3 off-day starter(s), 2 swap(s) available" |

### injury-monitor.py

Calls `/api/injury-report` and tracks state in `.injury-state.json` to detect *new* injuries. For new injured-active players, calls `/api/waiver-analyze` to find replacements.

| Level | Behavior |
|---|---|
| `auto` | Prints full injury alert + auto-response plan with replacement candidates (bench the injured player, list top 3 FA replacements with z-scores) |
| `suggest` | Prints full injury alert + suggested actions: "Bench Player X, consider adding Player Y" |
| `alert` | Prints the formatted injury report (injured active, healthy IL, injured bench, IL proper) |

State tracking: the script saves seen injuries to `.injury-state.json` so it only alerts on *new* injuries. The state file is not updated during `--dry-run`.

### waiver-scout.py

Calls `/api/waiver-analyze` (batters + pitchers), `/api/optimal-moves`, and `/api/punt-advisor`. Cross-references waiver targets with category strategy to prioritize players that help targeted categories and deprioritize players aligned with punted categories.

| Level | Behavior |
|---|---|
| `auto` | If a clear upgrade exists (z-score improvement >= 0.5), executes the top add/drop move. Otherwise falls back to suggest-style output |
| `suggest` | Prints full scout report: strategy context, batter targets, pitcher targets, optimal add/drop moves with z-score and category impact |
| `alert` | Prints brief notification: "5 waiver targets found. Top: Player X (z=2.1)" or "Clear upgrade available: Player Y (+0.8 z)" |

### weekly-recap.py

Calls `/api/matchup-detail`, `/api/standings`, `/api/transactions`, and `/api/category-trends`. Assembles a narrative-style recap with win/loss/tie breakdown, category results, standings position, recent transactions, and trending categories.

| Level | Behavior |
|---|---|
| `auto` | Prints full narrative recap with category breakdown, standings, transactions, MVP category, and trending categories |
| `suggest` | Same as auto (the recap is read-only, so both levels print the full report) |
| `alert` | Prints one-line headline: "RECAP ALERT: Week 5 6-3-1 vs Opponent | #3 overall" |

## State Files

| File | Created By | Purpose |
|---|---|---|
| `.injury-state.json` | `injury-monitor.py` | Tracks previously seen injuries to avoid duplicate alerts |

## Shared Modules

| Module | Purpose |
|---|---|
| `config.py` | `AutomationConfig` class -- loads `config.yaml`, applies env var overrides, provides autonomy checks |
| `formatter.py` | Message formatters for Telegram/WhatsApp delivery (lineup, injury, waiver, recap, trade, morning briefing) |
