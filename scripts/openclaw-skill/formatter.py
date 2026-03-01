#!/usr/bin/env python3
"""Message formatter for Yahoo Fantasy automation alerts.

Converts API JSON responses into condensed messages for Telegram/WhatsApp.

Each formatter accepts the JSON dict returned by the corresponding
Python API endpoint and returns a plain-text string suitable for chat
delivery (under 4096 chars for Telegram).

Coding conventions: string concatenation only (no f-strings),
.get() for all dict access, try/except with print() for errors.
"""

import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_MSG_LEN = 4096  # Telegram message limit

# Unicode symbols (safe for all chat platforms)
_UP = "\u2191"       # up arrow
_DOWN = "\u2193"     # down arrow
_DASH = "\u2014"     # em dash
_BULLET = "\u2022"   # bullet
_CHECK = "\u2713"    # checkmark
_CROSS = "\u2717"    # cross
_WARN = "\u26A0"     # warning triangle
_STAR = "\u2605"     # black star
_CIRCLE = "\u25CF"   # black circle

# Priority level indicators
_PRIORITY = {
    "critical": "\u2757",  # red exclamation
    "high": "\u2755",      # white exclamation
    "medium": "\u25CB",    # white circle
    "low": "\u25AB",       # small white square
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _truncate(text, max_len=30):
    """Truncate text with ellipsis if longer than max_len."""
    try:
        text = str(text)
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "\u2026"
    except Exception:
        return str(text)[:max_len]


def _trend_arrow(value):
    """Return trend arrow based on numeric value.

    Positive -> up arrow, negative -> down arrow, zero/None -> dash.
    """
    try:
        num = float(value)
        if num > 0:
            return _UP
        elif num < 0:
            return _DOWN
        return _DASH
    except (TypeError, ValueError):
        return _DASH


def _priority_indicator(priority):
    """Return indicator symbol for a priority level string."""
    try:
        key = str(priority).lower().strip()
        return _PRIORITY.get(key, _PRIORITY.get("medium"))
    except Exception:
        return _PRIORITY.get("medium")


def _today_str():
    """Return today's date formatted like 'Mon Mar 7'."""
    try:
        return datetime.date.today().strftime("%a %b %-d")
    except Exception:
        # Windows strftime does not support %-d
        try:
            return datetime.date.today().strftime("%a %b %d").replace(" 0", " ")
        except Exception:
            return str(datetime.date.today())


def _safe_str(value, default="?"):
    """Safely convert a value to string, returning default on failure."""
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _enforce_limit(text, limit=MAX_MSG_LEN):
    """Ensure message does not exceed the character limit."""
    if len(text) <= limit:
        return text
    # Truncate and append a notice
    cutoff = limit - 40
    return text[:cutoff] + "\n\n[Message truncated to " + str(limit) + " chars]"


# ---------------------------------------------------------------------------
# Lineup summary
# ---------------------------------------------------------------------------


def format_lineup_summary(data):
    """Format daily lineup optimization results.

    Input: JSON from cmd_lineup_optimize (as_json=True)
    Keys: games_today, active_off_day, bench_playing, il_players,
          suggested_swaps, applied

    Output: condensed message with lineup changes / suggestions.
    """
    try:
        if not data or not isinstance(data, dict):
            return "LINEUP: No data available"

        if data.get("error"):
            return "LINEUP ERROR: " + _safe_str(data.get("error"))

        lines = []
        applied = data.get("applied", False)
        header = "LINEUP SET" if applied else "LINEUP CHECK"
        lines.append(header + " (" + _today_str() + ")")
        lines.append("Games today: " + _safe_str(data.get("games_today", 0)))

        swaps = data.get("suggested_swaps", [])
        off_day = data.get("active_off_day", [])
        bench_playing = data.get("bench_playing", [])
        il_players = data.get("il_players", [])

        if swaps:
            lines.append("Changes: " + str(len(swaps)))
            for swap in swaps:
                pos = _safe_str(swap.get("position", "?"))
                start = _truncate(swap.get("start_player", "?"), 20)
                bench = _truncate(swap.get("bench_player", "?"), 20)
                lines.append(
                    "  IN: " + start + " (" + pos + ")"
                    + " | OUT: " + bench + " (" + pos + ")"
                )
        elif off_day:
            lines.append("Off-day starters: " + str(len(off_day)))
            for p in off_day[:5]:
                name = _truncate(p.get("name", "?"), 20)
                pos = _safe_str(p.get("position", "?"))
                lines.append("  " + _WARN + " " + name + " (" + pos + ") " + _DASH + " no game")
        else:
            lines.append(_CHECK + " All starters have games today")

        if bench_playing:
            lines.append("Bench with games: " + str(len(bench_playing)))
            for p in bench_playing[:3]:
                name = _truncate(p.get("name", "?"), 20)
                pos = _safe_str(p.get("position", "?"))
                lines.append("  " + _BULLET + " " + name + " (" + pos + ")")

        if il_players:
            lines.append("IL: " + str(len(il_players)) + " player(s)")

        return _enforce_limit("\n".join(lines))

    except Exception as e:
        print("format_lineup_summary error: " + str(e))
        return "LINEUP: Error formatting message"


# ---------------------------------------------------------------------------
# Injury alert
# ---------------------------------------------------------------------------


def format_injury_alert(data):
    """Format injury alert message.

    Input: JSON from cmd_injury_report (as_json=True)
    Keys: injured_active, healthy_il, injured_bench, il_proper

    Output: alert with player status and suggested actions.
    """
    try:
        if not data or not isinstance(data, dict):
            return "INJURY REPORT: No data available"

        if data.get("error"):
            return "INJURY ERROR: " + _safe_str(data.get("error"))

        injured_active = data.get("injured_active", [])
        healthy_il = data.get("healthy_il", [])
        injured_bench = data.get("injured_bench", [])
        il_proper = data.get("il_proper", [])

        total = (len(injured_active) + len(healthy_il)
                 + len(injured_bench) + len(il_proper))

        if total == 0:
            return _CHECK + " INJURY REPORT (" + _today_str() + ")\nRoster healthy " + _DASH + " no issues found"

        lines = []
        lines.append(_WARN + " INJURY REPORT (" + _today_str() + ")")

        # Critical: injured in active slots
        if injured_active:
            lines.append("")
            lines.append(_PRIORITY.get("critical") + " ACTIVE SLOT " + _DASH + " INJURED:")
            for p in injured_active:
                name = _truncate(p.get("name", "?"), 22)
                pos = _safe_str(p.get("position", "?"))
                status = _safe_str(p.get("status", ""))
                desc = p.get("injury_description", "")
                line = "  " + name + " (" + pos + ") [" + status + "]"
                if desc:
                    line += " " + _DASH + " " + _truncate(desc, 25)
                lines.append(line)
            lines.append("  " + _BULLET + " Action: Move to IL or bench")

        # Inefficiency: healthy on IL
        if healthy_il:
            lines.append("")
            lines.append(_PRIORITY.get("medium") + " ON IL " + _DASH + " MAY BE ACTIVATABLE:")
            for p in healthy_il:
                name = _truncate(p.get("name", "?"), 22)
                pos = _safe_str(p.get("position", "?"))
                lines.append("  " + name + " (" + pos + ")")
            lines.append("  " + _BULLET + " Action: Activate to lineup/bench")

        # Note: injured on bench
        if injured_bench:
            lines.append("")
            lines.append(_PRIORITY.get("low") + " BENCH " + _DASH + " INJURED:")
            for p in injured_bench:
                name = _truncate(p.get("name", "?"), 22)
                status = _safe_str(p.get("status", ""))
                lines.append("  " + name + " [" + status + "]")
            lines.append("  " + _BULLET + " Action: Move to IL to open bench spot")

        # Info: correctly placed
        if il_proper:
            lines.append("")
            lines.append(_CHECK + " Correctly on IL: " + str(len(il_proper)))

        return _enforce_limit("\n".join(lines))

    except Exception as e:
        print("format_injury_alert error: " + str(e))
        return "INJURY REPORT: Error formatting message"


# ---------------------------------------------------------------------------
# Waiver alert
# ---------------------------------------------------------------------------


def format_waiver_alert(data):
    """Format waiver wire recommendations.

    Input: JSON from cmd_waiver_analyze (as_json=True)
    Keys: pos_type, weak_categories, recommendations, drop_candidates

    Output: top targets with z-score, ownership, and category strengths.
    """
    try:
        if not data or not isinstance(data, dict):
            return "WAIVERS: No data available"

        if data.get("error"):
            return "WAIVER ERROR: " + _safe_str(data.get("error"))

        pos_type = data.get("pos_type", "?")
        label = "Batters" if pos_type == "B" else "Pitchers"
        weak_cats = data.get("weak_categories", [])
        recs = data.get("recommendations", [])
        drops = data.get("drop_candidates", [])

        lines = []
        lines.append("WAIVER TARGETS " + _DASH + " " + label + " (" + _today_str() + ")")

        # Weak categories
        if weak_cats:
            cats_str = ", ".join(
                c.get("name", "?") + " #" + _safe_str(c.get("rank", "?"))
                for c in weak_cats[:3]
            )
            lines.append("Weak cats: " + cats_str)

        lines.append("")

        # Recommendations
        if not recs:
            lines.append("No recommendations found")
        else:
            for i, p in enumerate(recs[:8]):
                rank = str(i + 1) + "."
                name = _truncate(p.get("name", "?"), 22)
                positions = _safe_str(p.get("positions", "?"))
                z = _safe_str(p.get("z_score", 0))
                pct = _safe_str(p.get("pct", 0))
                tier = _safe_str(p.get("tier", ""))
                score = _safe_str(p.get("score", ""))

                line = rank.ljust(3) + name + " (" + positions + ")"
                line += " z=" + z + " " + pct + "% owned"
                lines.append(line)

                # Extra detail line: tier, regression signal, intel
                details = []
                if tier and tier != "Unknown":
                    details.append(tier)
                regression = p.get("regression")
                if regression:
                    details.append(_trend_arrow(1 if "buy" in str(regression) else -1) + regression)
                intel_text = p.get("intel")
                if intel_text and isinstance(intel_text, str):
                    details.append(_truncate(intel_text, 40))
                if details:
                    lines.append("   " + " | ".join(details))

        # Drop candidates
        if drops:
            lines.append("")
            lines.append("Drop candidates:")
            for d in drops[:3]:
                name = _truncate(d.get("name", "?"), 22)
                z = _safe_str(d.get("z_score", 0))
                tier = _safe_str(d.get("tier", "?"))
                lines.append("  " + _DOWN + " " + name + " z=" + z + " [" + tier + "]")

        return _enforce_limit("\n".join(lines))

    except Exception as e:
        print("format_waiver_alert error: " + str(e))
        return "WAIVERS: Error formatting message"


# ---------------------------------------------------------------------------
# Weekly recap
# ---------------------------------------------------------------------------


def format_weekly_recap(data):
    """Format weekly matchup recap narrative.

    Input: dict with keys from matchup_detail / scoreboard results:
      matchup: {week, my_team, opponent, score: {wins, losses, ties}, categories}
      standings: optional standings data
      transactions: optional recent transactions

    Output: narrative summary of the week.
    """
    try:
        if not data or not isinstance(data, dict):
            return "WEEKLY RECAP: No data available"

        if data.get("error"):
            return "RECAP ERROR: " + _safe_str(data.get("error"))

        matchup = data.get("matchup", data)
        week = _safe_str(matchup.get("week", "?"))
        my_team = _safe_str(matchup.get("my_team", "My Team"))
        opponent = _safe_str(matchup.get("opponent", "Opponent"))
        score = matchup.get("score", {})
        wins = int(score.get("wins", 0))
        losses = int(score.get("losses", 0))
        ties = int(score.get("ties", 0))

        # Determine outcome
        if wins > losses:
            outcome = _CHECK + " WIN"
        elif losses > wins:
            outcome = _CROSS + " LOSS"
        else:
            outcome = _DASH + " TIE"

        lines = []
        lines.append("WEEK " + week + " RECAP " + _DASH + " " + outcome)
        lines.append(my_team + " vs " + opponent)
        lines.append("Score: " + str(wins) + "-" + str(losses) + "-" + str(ties))

        # Category breakdown
        categories = matchup.get("categories", [])
        if categories:
            lines.append("")
            cat_wins = []
            cat_losses = []
            cat_ties = []
            for cat in categories:
                name = cat.get("name", "?")
                result = cat.get("result", "tie")
                my_val = _safe_str(cat.get("my_value", "-"))
                opp_val = _safe_str(cat.get("opp_value", "-"))
                entry = name + " " + my_val + "-" + opp_val
                if result == "win":
                    cat_wins.append(entry)
                elif result == "loss":
                    cat_losses.append(entry)
                else:
                    cat_ties.append(entry)

            if cat_wins:
                lines.append(_CHECK + " Won: " + ", ".join(cat_wins))
            if cat_losses:
                lines.append(_CROSS + " Lost: " + ", ".join(cat_losses))
            if cat_ties:
                lines.append(_DASH + " Tied: " + ", ".join(cat_ties))

        # Standings update if provided
        standings = data.get("standings")
        if standings and isinstance(standings, dict):
            rank = standings.get("rank")
            record = standings.get("record")
            if rank:
                lines.append("")
                line = "Standings: #" + _safe_str(rank)
                if record:
                    line += " (" + _safe_str(record) + ")"
                lines.append(line)

        # Recent transactions if provided
        transactions = data.get("transactions")
        if transactions and isinstance(transactions, list):
            lines.append("")
            lines.append("Moves this week:")
            for t in transactions[:5]:
                t_type = _safe_str(t.get("type", "?"))
                player = _safe_str(t.get("player", "?"))
                lines.append("  " + _BULLET + " " + t_type + ": " + _truncate(player, 25))

        return _enforce_limit("\n".join(lines))

    except Exception as e:
        print("format_weekly_recap error: " + str(e))
        return "WEEKLY RECAP: Error formatting message"


# ---------------------------------------------------------------------------
# Morning briefing
# ---------------------------------------------------------------------------


def format_morning_briefing(data):
    """Format condensed morning briefing.

    Input: JSON from /api/workflow/morning-briefing
    Keys: action_items, injury, lineup, matchup, strategy,
          whats_new, waiver_batters, waiver_pitchers, edit_date

    Output: key highlights and action items.
    """
    try:
        if not data or not isinstance(data, dict):
            return "MORNING BRIEFING: No data available"

        if data.get("error"):
            return "BRIEFING ERROR: " + _safe_str(data.get("error"))

        lines = []
        lines.append(_STAR + " MORNING BRIEFING (" + _today_str() + ")")

        # Action items (priority-ranked)
        actions = data.get("action_items", [])
        if actions:
            lines.append("")
            lines.append("ACTION ITEMS:")
            for a in actions[:6]:
                priority = a.get("priority", 3)
                if priority <= 1:
                    indicator = _PRIORITY.get("critical")
                elif priority <= 2:
                    indicator = _PRIORITY.get("high")
                else:
                    indicator = _PRIORITY.get("medium")
                msg = _truncate(a.get("message", ""), 60)
                a_type = _safe_str(a.get("type", ""))
                lines.append("  " + indicator + " [" + a_type + "] " + msg)
        else:
            lines.append("")
            lines.append(_CHECK + " No urgent action items")

        # Current matchup snapshot
        matchup = data.get("matchup")
        if matchup and isinstance(matchup, dict) and not matchup.get("error"):
            opp = _safe_str(matchup.get("opponent", "?"))
            score = matchup.get("score", {})
            w = _safe_str(score.get("wins", 0))
            l = _safe_str(score.get("losses", 0))
            t = _safe_str(score.get("ties", 0))
            lines.append("")
            lines.append("MATCHUP: vs " + _truncate(opp, 25))
            lines.append("  Score: " + w + "-" + l + "-" + t)

        # Lineup status
        lineup = data.get("lineup")
        if lineup and isinstance(lineup, dict) and not lineup.get("error"):
            off_day = len(lineup.get("active_off_day", []))
            swaps = len(lineup.get("suggested_swaps", []))
            games = _safe_str(lineup.get("games_today", 0))
            lines.append("")
            line = "LINEUP: " + games + " games today"
            if off_day:
                line += ", " + str(off_day) + " off-day starter(s)"
            if swaps:
                line += ", " + str(swaps) + " swap(s) suggested"
            lines.append(line)

        # Injury snapshot
        injury = data.get("injury")
        if injury and isinstance(injury, dict) and not injury.get("error"):
            active_inj = len(injury.get("injured_active", []))
            healthy_il = len(injury.get("healthy_il", []))
            if active_inj or healthy_il:
                lines.append("")
                line = "INJURIES: "
                parts = []
                if active_inj:
                    parts.append(str(active_inj) + " injured in lineup")
                if healthy_il:
                    parts.append(str(healthy_il) + " activatable from IL")
                lines.append(line + ", ".join(parts))
            else:
                lines.append("")
                lines.append("INJURIES: " + _CHECK + " Roster healthy")

        # Top waiver picks (very condensed)
        for label, key in [("BAT", "waiver_batters"), ("PIT", "waiver_pitchers")]:
            waiver = data.get(key)
            if waiver and isinstance(waiver, dict) and not waiver.get("error"):
                recs = waiver.get("recommendations", [])
                if recs:
                    top = recs[0]
                    name = _truncate(top.get("name", "?"), 18)
                    z = _safe_str(top.get("z_score", "?"))
                    pct = _safe_str(top.get("pct", "?"))
                    if not lines or lines[-1] != "":
                        pass  # keep compact
                    lines.append(
                        "TOP " + label + ": " + name
                        + " z=" + z + " " + pct + "% owned"
                    )

        # Edit date
        edit_date = data.get("edit_date")
        if edit_date:
            lines.append("")
            lines.append("Next edit: " + _safe_str(edit_date))

        return _enforce_limit("\n".join(lines))

    except Exception as e:
        print("format_morning_briefing error: " + str(e))
        return "MORNING BRIEFING: Error formatting message"


# ---------------------------------------------------------------------------
# Trade alert
# ---------------------------------------------------------------------------


def format_trade_alert(data):
    """Format trade proposal or evaluation.

    Input: JSON from cmd_trade_eval (as_json=True)
    Keys: give_players, get_players, give_value, get_value,
          net_value, grade, warnings, position_impact

    Output: trade summary with grade and recommendation.
    """
    try:
        if not data or not isinstance(data, dict):
            return "TRADE: No data available"

        if data.get("error"):
            return "TRADE ERROR: " + _safe_str(data.get("error"))

        lines = []
        grade = _safe_str(data.get("grade", "?"))
        net = data.get("net_value", 0)
        trend = _trend_arrow(net)

        lines.append("TRADE EVAL " + _DASH + " " + grade + " " + trend)
        lines.append("")

        # Giving side
        give_players = data.get("give_players", [])
        give_value = _safe_str(data.get("give_value", 0))
        lines.append("GIVE (Z=" + give_value + "):")
        for p in give_players:
            name = _truncate(p.get("name", "?"), 22)
            positions = ", ".join(p.get("positions", []))
            if not positions:
                positions = "?"
            z = _safe_str(p.get("z_score", 0))
            tier = _safe_str(p.get("tier", "?"))
            lines.append(
                "  " + name + " (" + _truncate(positions, 12) + ")"
                + " Z=" + z + " [" + tier + "]"
            )

        lines.append("")

        # Getting side
        get_players = data.get("get_players", [])
        get_value = _safe_str(data.get("get_value", 0))
        lines.append("GET (Z=" + get_value + "):")
        for p in get_players:
            name = _truncate(p.get("name", "?"), 22)
            positions = ", ".join(p.get("positions", []))
            if not positions:
                positions = "?"
            z = _safe_str(p.get("z_score", 0))
            tier = _safe_str(p.get("tier", "?"))
            lines.append(
                "  " + name + " (" + _truncate(positions, 12) + ")"
                + " Z=" + z + " [" + tier + "]"
            )

        # Net value
        lines.append("")
        lines.append("Net Z-Score: " + _safe_str(data.get("net_value", 0)) + " " + trend)

        # Position impact
        pos_impact = data.get("position_impact", {})
        losing = pos_impact.get("losing", [])
        gaining = pos_impact.get("gaining", [])
        if losing or gaining:
            lines.append("")
            if losing:
                lines.append("Losing coverage: " + ", ".join(str(p) for p in losing))
            if gaining:
                lines.append("Gaining coverage: " + ", ".join(str(p) for p in gaining))

        # Warnings
        warnings = data.get("warnings", [])
        if warnings:
            lines.append("")
            for w in warnings[:4]:
                lines.append(_WARN + " " + _truncate(w, 60))

        return _enforce_limit("\n".join(lines))

    except Exception as e:
        print("format_trade_alert error: " + str(e))
        return "TRADE: Error formatting message"


# ---------------------------------------------------------------------------
# Generic formatter
# ---------------------------------------------------------------------------


def format_generic(title, data):
    """Generic formatter for any data dict.

    Renders a title header and key-value pairs from the dict.
    Lists and nested dicts are summarized compactly.
    """
    try:
        if not data:
            return str(title).upper() + "\nNo data available"

        lines = []
        lines.append(str(title).upper() + " (" + _today_str() + ")")
        lines.append("")

        if isinstance(data, dict):
            for key, value in data.items():
                if key.startswith("_"):
                    continue
                display_key = str(key).replace("_", " ").title()
                if isinstance(value, list):
                    lines.append(display_key + ": " + str(len(value)) + " item(s)")
                    for item in value[:5]:
                        if isinstance(item, dict):
                            name = item.get("name", item.get("player", ""))
                            if name:
                                lines.append("  " + _BULLET + " " + _truncate(str(name), 40))
                            else:
                                lines.append("  " + _BULLET + " " + _truncate(str(item), 50))
                        else:
                            lines.append("  " + _BULLET + " " + _truncate(str(item), 50))
                    if len(value) > 5:
                        lines.append("  ... and " + str(len(value) - 5) + " more")
                elif isinstance(value, dict):
                    lines.append(display_key + ":")
                    for k, v in list(value.items())[:8]:
                        lines.append("  " + str(k) + ": " + _truncate(_safe_str(v), 40))
                else:
                    lines.append(display_key + ": " + _truncate(_safe_str(value), 60))
        else:
            lines.append(str(data))

        return _enforce_limit("\n".join(lines))

    except Exception as e:
        print("format_generic error: " + str(e))
        return str(title).upper() + "\nError formatting message"


# ---------------------------------------------------------------------------
# Dispatch helper
# ---------------------------------------------------------------------------

# Maps alert type names to formatter functions
FORMATTERS = {
    "lineup": format_lineup_summary,
    "lineup_summary": format_lineup_summary,
    "injury": format_injury_alert,
    "injury_alert": format_injury_alert,
    "waiver": format_waiver_alert,
    "waiver_alert": format_waiver_alert,
    "recap": format_weekly_recap,
    "weekly_recap": format_weekly_recap,
    "briefing": format_morning_briefing,
    "morning_briefing": format_morning_briefing,
    "trade": format_trade_alert,
    "trade_alert": format_trade_alert,
}


def format_message(alert_type, data):
    """Dispatch to the appropriate formatter by alert type name.

    Falls back to format_generic if the alert_type is not recognized.

    Args:
        alert_type: string key (e.g. "lineup", "injury", "trade")
        data: dict of JSON data from the API

    Returns:
        Formatted message string.
    """
    try:
        formatter = FORMATTERS.get(alert_type)
        if formatter:
            return formatter(data)
        return format_generic(alert_type, data)
    except Exception as e:
        print("format_message error: " + str(e))
        return str(alert_type).upper() + ": Error formatting message"
