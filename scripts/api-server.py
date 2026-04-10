#!/usr/bin/env python3
"""Yahoo Fantasy Baseball JSON API Server

Routes match the TypeScript MCP Apps server's python-client.ts expectations.
"""

import sys
import os
import importlib
import time
import json
import hashlib
import threading
import urllib.request
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from mlb_id_cache import get_mlb_id

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, g
from position_batching import (
    best_available_position_tokens as _best_available_position_tokens,
    disagreement_position_tokens as _disagreement_position_tokens,
    grouped_all_payload as _grouped_all_payload,
    normalize_hitter_payload as _normalize_hitter_payload,
    parse_hitter_positions_csv as _parse_hitter_positions_csv,
    ranking_position_tokens as _ranking_position_tokens,
    safe_bool as _safe_bool,
)
from trace_utils import (
    clear_trace_context,
    get_trace_context,
    log_trace_event,
    monotonic_ms,
    start_request_trace,
    update_trace_context,
)

# Import modules (some have hyphens, need importlib)
yahoo_fantasy = importlib.import_module("yahoo-fantasy")
draft_assistant = importlib.import_module("draft-assistant")
mlb_data = importlib.import_module("mlb-data")
season_manager = importlib.import_module("season-manager")
import valuations
import history
import intel
import news
import yahoo_browser
import player_universe
import draft_sim

app = Flask(__name__)
_DASHBOARD_CACHE = {}
_DASHBOARD_CACHE_DIR = os.path.join(
    os.environ.get("DATA_DIR", "/app/data"), "dashboard-cache"
)
_OPERATOR_SCOREBOARD_TZ = ZoneInfo("America/New_York")
_MLB_MEDIA_GATEWAY_URL = "https://media-gateway.mlb.com/graphql"
_team_state_singleflight_guard = threading.Lock()
_TEAM_STATE_SINGLEFLIGHT = {}


def _dashboard_cache_file(key):
    prefix = str(key[0] if isinstance(key, tuple) and key else key).replace("/", "_")
    digest = hashlib.sha1(
        json.dumps(key, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(_DASHBOARD_CACHE_DIR, prefix + "-" + digest + ".json")


def _dashboard_cache_peek(key):
    entry = _DASHBOARD_CACHE.get(key)
    if not entry:
        path = _dashboard_cache_file(key)
        try:
            if os.path.exists(path):
                fetched_at = os.path.getmtime(path)
                age = time.time() - fetched_at
                with open(path) as handle:
                    payload = json.load(handle)
                _DASHBOARD_CACHE[key] = (payload, fetched_at)
                return payload, age
        except Exception:
            return None
        return None
    payload, ts = entry
    return payload, time.time() - ts


def _dashboard_cache_get(key, ttl_seconds):
    entry = _dashboard_cache_peek(key)
    if entry is None:
        return None
    payload, age = entry
    if age > ttl_seconds:
        return None
    return payload


def _dashboard_cache_set(key, payload):
    _DASHBOARD_CACHE[key] = (payload, time.time())
    path = _dashboard_cache_file(key)
    try:
        os.makedirs(_DASHBOARD_CACHE_DIR, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(payload, handle)
    except Exception:
        pass


def _dashboard_cache_delete_prefix(prefix):
    for key in list(_DASHBOARD_CACHE.keys()):
        if isinstance(key, tuple) and key and key[0] == prefix:
            _DASHBOARD_CACHE.pop(key, None)
    try:
        if not os.path.isdir(_DASHBOARD_CACHE_DIR):
            return
        for filename in os.listdir(_DASHBOARD_CACHE_DIR):
            if filename.startswith(str(prefix).replace("/", "_") + "-"):
                os.remove(os.path.join(_DASHBOARD_CACHE_DIR, filename))
    except Exception:
        pass


def _singleflight_entry(key, lease_seconds):
    now = time.time()
    with _team_state_singleflight_guard:
        entry = _TEAM_STATE_SINGLEFLIGHT.get(key)
        if entry is None:
            entry = {"lock": threading.Lock(), "started_at": 0.0}
            _TEAM_STATE_SINGLEFLIGHT[key] = entry
            return entry

        lock = entry.get("lock")
        started_at = float(entry.get("started_at", 0.0) or 0.0)
        if lock is None:
            entry = {"lock": threading.Lock(), "started_at": 0.0}
            _TEAM_STATE_SINGLEFLIGHT[key] = entry
            return entry

        if lock.locked() and started_at and now - started_at > lease_seconds:
            entry = {"lock": threading.Lock(), "started_at": 0.0}
            _TEAM_STATE_SINGLEFLIGHT[key] = entry
            return entry

        if not lock.locked():
            entry["started_at"] = 0.0
        return entry


def _mark_singleflight_started(key, lock):
    with _team_state_singleflight_guard:
        entry = _TEAM_STATE_SINGLEFLIGHT.get(key)
        if entry and entry.get("lock") is lock:
            entry["started_at"] = time.time()


def _release_singleflight(key, lock):
    with _team_state_singleflight_guard:
        entry = _TEAM_STATE_SINGLEFLIGHT.get(key)
        if entry and entry.get("lock") is lock:
            entry["started_at"] = 0.0
    if lock.locked():
        lock.release()


def _taken_players_fallback(position):
    return {"players": [], "position": position or None, "count": 0, "degraded": True}


def _refresh_taken_players_async(cache_key, args, lock, done_event, result_holder):
    try:
        result = yahoo_fantasy.cmd_taken_players(args, as_json=True)
        result_holder["result"] = result
        if isinstance(result, dict) and "players" in result:
            _dashboard_cache_set(cache_key, result)
    except Exception as exc:
        result_holder["error"] = exc
    finally:
        done_event.set()
        _release_singleflight(cache_key, lock)


def _taken_players_fallback(position):
    return {"players": [], "position": position or None, "count": 0, "degraded": True}


def _refresh_taken_players_async(cache_key, args, lock, done_event, result_holder):
    try:
        result = yahoo_fantasy.cmd_taken_players(args, as_json=True)
        result_holder["result"] = result
        if isinstance(result, dict) and "players" in result:
            _dashboard_cache_set(cache_key, result)
    except Exception as exc:
        result_holder["error"] = exc
    finally:
        done_event.set()
        lock.release()


def _invalidate_team_state_caches():
    for prefix in (
        "roster",
        "lineup-optimize",
        "injury-report",
        "waiver-analyze",
        "hot-bat-free-agents",
        "hot-hand-free-agent-pitchers",
        "workflow-morning-briefing",
        "workflow-league-landscape",
        "workflow-roster-health",
        "workflow-waiver-recommendations",
    ):
        _dashboard_cache_delete_prefix(prefix)


def _mutation_succeeded(result):
    if not isinstance(result, dict):
        return True
    return bool(result.get("success", True))


def _safe_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _structured_api_error(message, status=500, extra=None):
    text = str(message or "")
    lower = text.lower()
    payload = {"error": text}

    if "yahoo oauth tokens are missing" in lower:
        payload.update(
            {
                "code": "yahoo_oauth_missing",
                "retryable": False,
                "action_required": True,
                "action": "configure_yahoo_oauth",
                "auth_type": "oauth",
            }
        )
    elif "invalid_client" in lower or "invalid client_id" in lower:
        payload.update(
            {
                "code": "yahoo_oauth_invalid_client",
                "retryable": False,
                "action_required": True,
                "action": "reauthorize_yahoo_oauth",
                "auth_type": "oauth",
            }
        )
    elif (
        "browser session not valid" in lower
        or "session expired" in lower
        or "redirected to login page" in lower
        or "browser-login" in lower
    ):
        payload.update(
            {
                "code": "yahoo_browser_session_expired",
                "retryable": False,
                "action_required": True,
                "action": "reauthorize_browser_session",
                "auth_type": "browser_session",
            }
        )
    elif "timeout" in lower or "timed out" in lower:
        payload.update(
            {
                "code": "upstream_timeout",
                "retryable": True,
                "action_required": False,
            }
        )

    if extra:
        payload.update(extra)
    return payload, status


def _json_error(exc, status=500, extra=None):
    payload, code = _structured_api_error(exc, status=status, extra=extra)
    return jsonify(payload), code


def _auth_status_payload():
    shared = importlib.import_module("shared")

    oauth_file = str(getattr(shared, "OAUTH_FILE", "") or "")
    read_oauth_file = getattr(shared, "_read_oauth_file", None)
    oauth_has_tokens = getattr(shared, "_oauth_has_tokens", None)
    bridge_url = str(
        getattr(
            shared,
            "YAHOO_OAUTH_BRIDGE_URL",
            os.environ.get("YAHOO_OAUTH_BRIDGE_URL", ""),
        )
        or ""
    )
    bridge_token = str(
        getattr(
            shared,
            "YAHOO_OAUTH_BRIDGE_TOKEN",
            os.environ.get("YAHOO_OAUTH_BRIDGE_TOKEN", ""),
        )
        or ""
    )

    oauth_payload = {}
    if callable(read_oauth_file):
        try:
            oauth_payload = read_oauth_file() or {}
        except Exception:
            oauth_payload = {}

    oauth_ready = False
    if callable(oauth_has_tokens):
        try:
            oauth_ready = bool(oauth_has_tokens(oauth_payload))
        except Exception:
            oauth_ready = False
    else:
        oauth_ready = bool(oauth_payload)

    oauth_status = {
        "ready": oauth_ready,
        "auth_type": "oauth",
        "oauth_file": oauth_file,
        "bridge_configured": bool(bridge_url and bridge_token),
        "consumer_key_present": bool((oauth_payload or {}).get("consumer_key")),
        "consumer_secret_present": bool((oauth_payload or {}).get("consumer_secret")),
        "token_present": bool((oauth_payload or {}).get("access_token")),
        "refresh_token_present": bool((oauth_payload or {}).get("refresh_token")),
        "guid_present": bool((oauth_payload or {}).get("guid")),
    }

    browser_status = yahoo_browser.is_session_valid()
    browser_payload = {
        "ready": bool(browser_status.get("valid")),
        "auth_type": "browser_session",
        "valid": bool(browser_status.get("valid")),
        "reason": str(browser_status.get("reason", "") or ""),
        "cookie_count": _safe_int(browser_status.get("cookie_count"), 0) or 0,
        "heartbeat": yahoo_browser.get_heartbeat_state(),
        "session_file": str(getattr(yahoo_browser, "SESSION_FILE", "") or ""),
    }

    return {
        "oauth_read": oauth_status,
        "browser_write": browser_payload,
        "recommended_action": (
            "configure_yahoo_oauth"
            if not oauth_status["ready"]
            else "reauthorize_browser_session" if not browser_payload["ready"] else None
        ),
    }


def _normalize_positions(value):
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    if isinstance(value, str):
        return [value] if value else []
    return []


def _player_team_abbr(player):
    if not isinstance(player, dict):
        return ""
    for key in ("editorial_team_abbr", "team_abbr", "team"):
        value = player.get(key, "")
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for subkey in ("abbr", "abbreviation", "shortName", "name"):
                subval = value.get(subkey, "")
                if subval:
                    return str(subval)
    return ""


def _recent_stat(game, *keys):
    for key in keys:
        if key in game and game.get(key) not in (None, ""):
            return _safe_float(game.get(key), 0.0)
    return 0.0


def _format_number(value):
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.1f" % value).rstrip("0").rstrip(".")


def _score_hot_bat(player):
    mlb_id = player.get("mlb_id") or get_mlb_id(player.get("name", ""))
    if not mlb_id:
        return None
    games = intel._fetch_mlb_game_log(mlb_id, "hitting", 7)
    if not games:
        return None

    hits = sum(_recent_stat(g, "hits") for g in games)
    homers = sum(_recent_stat(g, "homeRuns", "home_runs") for g in games)
    runs = sum(_recent_stat(g, "runs") for g in games)
    rbi = sum(_recent_stat(g, "rbi", "runsBattedIn") for g in games)
    steals = sum(_recent_stat(g, "stolenBases", "stolen_bases") for g in games)
    at_bats = sum(_recent_stat(g, "atBats", "at_bats") for g in games)
    avg = (hits / at_bats) if at_bats > 0 else 0.0

    if hits + homers + runs + rbi + steals <= 0:
        return None

    score = hits + homers * 4.0 + runs * 1.5 + rbi * 1.5 + steals * 3.0 + avg * 5.0
    summary_parts = [_format_number(hits) + " H"]
    if homers > 0:
        summary_parts.append(_format_number(homers) + " HR")
    if rbi > 0:
        summary_parts.append(_format_number(rbi) + " RBI")
    elif runs > 0:
        summary_parts.append(_format_number(runs) + " R")
    if steals > 0:
        summary_parts.append(_format_number(steals) + " SB")

    row = dict(player)
    row["mlb_id"] = mlb_id
    row["score"] = round(score, 1)
    row["summary"] = ", ".join(summary_parts[:4])
    return row


def _score_hot_hand(player):
    mlb_id = player.get("mlb_id") or get_mlb_id(player.get("name", ""))
    if not mlb_id:
        return None
    games = intel._fetch_mlb_game_log(mlb_id, "pitching", 45)
    if not games:
        return None
    recent = sorted(games, key=lambda g: str(g.get("date", "")), reverse=True)[:3]
    if not recent:
        return None

    appearances = len(recent)
    earned_runs = sum(_recent_stat(g, "earnedRuns", "earned_runs") for g in recent)
    strikeouts = sum(_recent_stat(g, "strikeOuts", "strikeouts") for g in recent)
    saves = sum(_recent_stat(g, "saves", "save") for g in recent)
    holds = sum(_recent_stat(g, "holds", "hold") for g in recent)
    clean_appearances = sum(
        1 for g in recent if _recent_stat(g, "earnedRuns", "earned_runs") == 0
    )

    score = (
        strikeouts * 2.5
        + saves * 5.0
        + holds * 3.0
        + clean_appearances * 2.0
        - earned_runs * 4.0
    )
    summary = (
        str(appearances)
        + " app, "
        + _format_number(earned_runs)
        + " ER, "
        + _format_number(strikeouts)
        + " K"
    )
    if saves + holds > 0:
        summary += ", " + _format_number(saves + holds) + " SV+H"

    row = dict(player)
    row["mlb_id"] = mlb_id
    row["score"] = round(score, 1)
    row["summary"] = summary
    return row


def _candidate_free_agents(pos_type, count):
    try:
        pool_size = max(count * 6, 40)
        pool_size = min(pool_size, 60)
        if hasattr(yahoo_fantasy, "get_available_players"):
            available_players = yahoo_fantasy.get_available_players(pos_type, pool_size)
        else:
            _, _, league = yahoo_fantasy.get_league()
            available_players = (
                league.free_agents(pos_type)[:pool_size] if league else []
            )
    except Exception:
        return []

    candidates = []
    for player in available_players or []:
        if not isinstance(player, dict):
            continue
        name = str(player.get("name", "Unknown"))
        team_abbr = (
            str(
                player.get("team_abbr")
                or player.get("team")
                or player.get("editorial_team_abbr")
                or ""
            )
            .strip()
            .upper()
        )
        candidates.append(
            {
                "name": name,
                "player_id": str(player.get("player_id", "")),
                "team": team_abbr,
                "team_abbr": team_abbr,
                "positions": _normalize_positions(
                    player.get("positions", player.get("eligible_positions", []))
                ),
                "percent_owned": _safe_int(player.get("percent_owned"), 0) or 0,
                "mlb_id": player.get("mlb_id") or get_mlb_id(name),
                "availability_type": str(player.get("availability_type", "")),
            }
        )
    return candidates


_INACTIVE_FANTASY_POSITIONS = {
    "BN",
    "BE",
    "BENCH",
    "IL",
    "IL+",
    "IL10",
    "IL15",
    "IL60",
    "DL",
    "DL10",
    "DL15",
    "DL60",
    "NA",
    "IR",
    "RES",
}


def _is_inactive_fantasy_position(position):
    token = str(position or "").strip().upper()
    if not token:
        return False
    if token in _INACTIVE_FANTASY_POSITIONS:
        return True
    return token.startswith("IL")


def _operator_generated_at():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _operator_inning_display(game):
    status = game.get("status", {}) if isinstance(game, dict) else {}
    detailed = str(status.get("detailedState", "") or "")
    abstract = str(status.get("abstractGameState", "") or "")
    linescore = game.get("linescore", {}) if isinstance(game, dict) else {}

    if abstract == "Live":
        half = str(
            linescore.get("inningHalf", "") or linescore.get("inningState", "") or ""
        ).strip()
        inning = (
            linescore.get("currentInningOrdinal")
            or linescore.get("currentInning")
            or ""
        )
        if half and inning:
            return str(half) + " " + str(inning)
        if inning:
            return str(inning)
        return detailed or "Live"

    if detailed in ("Final", "Game Over", "Completed Early"):
        return "Final"

    game_date = str(game.get("gameDate", "") or "")
    if game_date:
        try:
            parsed = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%-I:%M %p")
        except ValueError:
            pass

    return detailed or abstract or ""


def _operator_game_time(game):
    game_date = str(game.get("gameDate", "") or "")
    if not game_date:
        return ""
    try:
        parsed = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
        return parsed.astimezone().isoformat()
    except ValueError:
        return ""


def _operator_normalize_inning_half(value):
    token = str(value or "").strip().lower()
    if token.startswith("top"):
        return "Top"
    if token.startswith("bottom") or token.startswith("bot"):
        return "Bottom"
    if token.startswith("mid"):
        return "Mid"
    if token.startswith("end"):
        return "End"
    return ""


def _operator_status_sort_bucket(status):
    token = str(status or "").strip().lower()
    if token in {
        "in progress",
        "manager challenge",
        "review",
        "warmup",
        "delayed start",
        "delayed",
        "game advisory",
    }:
        return 0
    if token in {"pre-game", "scheduled"}:
        return 1
    if token in {"final", "game over", "completed early"}:
        return 2
    return 3


def _operator_sort_games(games):
    def _sort_key(game):
        return (
            -_safe_int(game.get("total_relevant_count"), 0),
            _operator_status_sort_bucket(game.get("status")),
            str(game.get("game_time", "") or ""),
            str(game.get("game_id", "") or ""),
        )

    return sorted(games, key=_sort_key)


def _operator_iter_schedule_games(schedule_data):
    for date_block in (
        schedule_data.get("dates", []) if isinstance(schedule_data, dict) else []
    ):
        for game in date_block.get("games", []) if isinstance(date_block, dict) else []:
            if isinstance(game, dict):
                yield game


def _operator_team_abbr_map(mlb_fetch):
    lookup = {}
    data = mlb_fetch("/teams?sportId=1")
    for team in data.get("teams", []) if isinstance(data, dict) else []:
        team_id = team.get("id")
        abbr = str(team.get("abbreviation", "") or "")
        name = str(team.get("name", "") or "")
        if team_id is not None and abbr:
            lookup[str(team_id)] = abbr
        if name and abbr:
            lookup[name.lower()] = abbr
    return lookup


def _operator_live_state(game, away_abbr, home_abbr):
    status = game.get("status", {}) if isinstance(game, dict) else {}
    abstract = str(status.get("abstractGameState", "") or "").strip().lower()
    linescore = game.get("linescore", {}) if isinstance(game, dict) else {}
    if not isinstance(linescore, dict) or not linescore:
        return None

    inning_half = _operator_normalize_inning_half(
        linescore.get("inningHalf") or linescore.get("inningState")
    )
    inning_number = _safe_int(linescore.get("currentInning"), None)
    outs = _safe_int(linescore.get("outs"), None)
    balls = _safe_int(linescore.get("balls"), None)
    strikes = _safe_int(linescore.get("strikes"), None)

    offense = (
        linescore.get("offense", {})
        if isinstance(linescore.get("offense"), dict)
        else {}
    )
    defense = (
        linescore.get("defense", {})
        if isinstance(linescore.get("defense"), dict)
        else {}
    )

    batter = (
        offense.get("batter", {}) if isinstance(offense.get("batter"), dict) else {}
    )
    pitcher = (
        defense.get("pitcher", {}) if isinstance(defense.get("pitcher"), dict) else {}
    )

    if inning_half == "Top":
        batter_team_abbr = away_abbr
        pitcher_team_abbr = home_abbr
    elif inning_half == "Bottom":
        batter_team_abbr = home_abbr
        pitcher_team_abbr = away_abbr
    else:
        batter_team_abbr = ""
        pitcher_team_abbr = ""

    if inning_half in {"Mid", "End"}:
        balls = None
        strikes = None
        offense = {}
        batter = {}
        pitcher = {}
        batter_team_abbr = ""
        pitcher_team_abbr = ""

    has_live_markers = (
        any(
            value is not None and value != ""
            for value in (inning_half, inning_number, outs, balls, strikes)
        )
        or any(key in offense for key in ("first", "second", "third", "batter"))
        or any(key in defense for key in ("pitcher",))
    )

    if abstract != "live" and not has_live_markers:
        return None

    return {
        "inning_half": inning_half or None,
        "inning_number": inning_number,
        "outs": outs,
        "balls": balls,
        "strikes": strikes,
        "bases": {
            "first": bool(offense.get("first")),
            "second": bool(offense.get("second")),
            "third": bool(offense.get("third")),
        },
        "batter": {
            "name": str(batter.get("fullName", "") or batter.get("name", "") or ""),
            "team_abbr": batter_team_abbr,
        },
        "pitcher": {
            "name": str(pitcher.get("fullName", "") or pitcher.get("name", "") or ""),
            "team_abbr": pitcher_team_abbr,
        },
    }


def _operator_normalize_game(
    game,
    abbr_lookup,
    my_team_name,
    opponent_team_name,
    include_players=True,
    include_live_state=True,
):
    teams = game.get("teams", {}) if isinstance(game, dict) else {}
    away = teams.get("away", {}) if isinstance(teams, dict) else {}
    home = teams.get("home", {}) if isinstance(teams, dict) else {}
    away_team = away.get("team", {}) if isinstance(away, dict) else {}
    home_team = home.get("team", {}) if isinstance(home, dict) else {}

    away_id = away_team.get("id")
    home_id = home_team.get("id")
    away_name = str(away_team.get("name", "") or "")
    home_name = str(home_team.get("name", "") or "")

    away_abbr = (
        str(away_team.get("abbreviation", "") or "")
        or abbr_lookup.get(str(away_id), "")
        or abbr_lookup.get(away_name.lower(), "")
    )
    home_abbr = (
        str(home_team.get("abbreviation", "") or "")
        or abbr_lookup.get(str(home_id), "")
        or abbr_lookup.get(home_name.lower(), "")
    )

    normalized = {
        "game_id": "mlb-" + str(game.get("gamePk", "")),
        "status": str(game.get("status", {}).get("detailedState", "") or ""),
        "inning": _operator_inning_display(game),
        "game_time": _operator_game_time(game),
        "away_team": {
            "name": away_name,
            "abbr": away_abbr,
            "score": _safe_int(away.get("score"), 0) or 0,
        },
        "home_team": {
            "name": home_name,
            "abbr": home_abbr,
            "score": _safe_int(home.get("score"), 0) or 0,
        },
        "my_team_name": my_team_name,
        "opponent_team_name": opponent_team_name,
        "my_active_count": 0,
        "my_inactive_count": 0,
        "opp_active_count": 0,
        "opp_inactive_count": 0,
        "total_relevant_count": 0,
    }
    if include_players:
        normalized["my_players"] = []
        normalized["opp_players"] = []
    if include_live_state:
        live_state = _operator_live_state(game, away_abbr, home_abbr)
        if live_state is not None:
            normalized["live_state"] = live_state
    return normalized


def _operator_extract_current_matchup(lg):
    raw = lg.matchups()
    league_data = (
        raw.get("fantasy_content", {}).get("league", [])
        if isinstance(raw, dict)
        else []
    )
    if len(league_data) < 2:
        return None

    scoreboard = league_data[1].get("scoreboard", {})
    matchup_block = scoreboard.get("0", {}).get("matchups", {})
    count = _safe_int(matchup_block.get("count"), 0) or 0

    for i in range(count):
        matchup = matchup_block.get(str(i), {}).get("matchup", {})
        teams_data = matchup.get("0", {}).get("teams", {})
        team1 = teams_data.get("0", {})
        team2 = teams_data.get("1", {})
        key1 = str(yahoo_fantasy._extract_team_key(team1) or "")
        key2 = str(yahoo_fantasy._extract_team_key(team2) or "")
        if yahoo_fantasy.TEAM_ID not in key1 and yahoo_fantasy.TEAM_ID not in key2:
            continue

        if yahoo_fantasy.TEAM_ID in key1:
            return {
                "my_team_key": key1,
                "opp_team_key": key2,
                "my_team_name": yahoo_fantasy._extract_team_name(team1),
                "opp_team_name": yahoo_fantasy._extract_team_name(team2),
            }

        return {
            "my_team_key": key2,
            "opp_team_key": key1,
            "my_team_name": yahoo_fantasy._extract_team_name(team2),
            "opp_team_name": yahoo_fantasy._extract_team_name(team1),
        }

    return None


def _operator_roster_rows(team_obj):
    rows = []
    if not team_obj:
        return rows

    for player in team_obj.roster() or []:
        fantasy_position = str(yahoo_fantasy._selected_position(player) or "")
        rows.append(
            {
                "name": yahoo_fantasy._player_name(player),
                "team_abbr": str(yahoo_fantasy._player_team_abbr(player) or "").upper(),
                "slot_status": (
                    "inactive"
                    if _is_inactive_fantasy_position(fantasy_position)
                    else "active"
                ),
                "fantasy_position": fantasy_position,
                "mlb_id": get_mlb_id(yahoo_fantasy._player_name(player)),
            }
        )
    return rows


def _operator_fill_missing_team_abbr(rows, mlb_fetch, abbr_lookup):
    missing_ids = []
    for row in rows:
        if row.get("team_abbr"):
            continue
        mlb_id = row.get("mlb_id")
        if mlb_id:
            missing_ids.append(str(mlb_id))

    if not missing_ids:
        return

    unique_ids = list(dict.fromkeys(missing_ids))
    try:
        data = mlb_fetch(
            "/people?personIds=" + ",".join(unique_ids) + "&hydrate=currentTeam"
        )
    except Exception:
        return

    current_team_by_id = {}
    for person in data.get("people", []) if isinstance(data, dict) else []:
        person_id = str(person.get("id", "") or "")
        current_team = person.get("currentTeam", {}) if isinstance(person, dict) else {}
        team_id = current_team.get("id")
        team_name = str(current_team.get("name", "") or "")
        current_team_by_id[person_id] = abbr_lookup.get(
            str(team_id), ""
        ) or abbr_lookup.get(team_name.lower(), "")

    for row in rows:
        if row.get("team_abbr"):
            continue
        resolved = current_team_by_id.get(str(row.get("mlb_id", "") or ""), "")
        if resolved:
            row["team_abbr"] = resolved


def _operator_game_pk_from_game_id(game_id):
    token = str(game_id or "").strip()
    if token.startswith("mlb-"):
        token = token[4:]
    return token if token.isdigit() else ""


def _mlb_media_feed_sort_key(feed):
    feed_type = str(feed.get("feedType", "") or "").upper()
    if feed_type == "HOME":
        return (0, str(feed.get("callSign", "") or ""))
    if feed_type == "AWAY":
        return (1, str(feed.get("callSign", "") or ""))
    if feed_type == "NETWORK":
        return (2, str(feed.get("callSign", "") or ""))
    return (3, str(feed.get("callSign", "") or ""))


def _mlb_media_feed_label(feed):
    feed_type = str(feed.get("feedType", "") or "").upper()
    call_sign = str(feed.get("callSign", "") or "").strip()
    if feed_type == "HOME":
        base = "Listen Home"
    elif feed_type == "AWAY":
        base = "Listen Away"
    elif feed_type == "NETWORK":
        base = "Listen Network"
    else:
        base = "Listen"
    return base + " (" + call_sign + ")" if call_sign else base


def _mlb_video_feed_label(feed):
    feed_type = str(feed.get("feedType", "") or "").upper()
    call_sign = str(feed.get("callSign", "") or "").strip()
    if feed_type == "HOME":
        base = "TV Home"
    elif feed_type == "AWAY":
        base = "TV Away"
    elif feed_type == "NETWORK":
        base = "TV Network"
    else:
        base = "TV"
    return base + " (" + call_sign + ")" if call_sign else base


def _mlb_media_links_query(game_pk, game_date):
    payload = {
        "operationName": "gameMedia",
        "query": (
            "query gameMedia($gamePk: Int!, $date: String!) { "
            "gameMedia(gamePk: $gamePk, date: $date) { "
            "gamePk gameDate content { contentId mediaId feedType callSign mediaState { state mediaType contentExperience } } "
            "} }"
        ),
        "variables": {"gamePk": int(game_pk), "date": str(game_date or "")},
    }
    request_obj = urllib.request.Request(
        _MLB_MEDIA_GATEWAY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "BaseClaw/1.0"},
    )
    with urllib.request.urlopen(request_obj, timeout=15) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body) if body else {}
    return data.get("data", {}).get("gameMedia", {}) if isinstance(data, dict) else {}


def _mlb_media_links_for_game(game_pk, game_date):
    if not game_pk:
        return {"watch_url": "", "watch_links": [], "audio_links": []}

    cache_key = ("mlb-media-links", str(game_pk), str(game_date or ""))
    cached = _dashboard_cache_get(cache_key, 300)
    if cached is not None:
        return cached

    result = {
        "watch_url": "https://www.mlb.com/tv/g" + str(game_pk),
        "watch_links": [],
        "audio_links": [],
    }
    try:
        game_media = _mlb_media_links_query(game_pk, game_date)
    except Exception:
        _dashboard_cache_set(cache_key, result)
        return result

    audio_links = []
    watch_links = []
    for feed in sorted(
        game_media.get("content", []) if isinstance(game_media, dict) else [],
        key=_mlb_media_feed_sort_key,
    ):
        if not isinstance(feed, dict):
            continue
        media_id = str(
            feed.get("mediaId", "") or feed.get("contentId", "") or ""
        ).strip()
        media_type = str(
            (feed.get("mediaState", {}) or {}).get("mediaType", "") or ""
        ).upper()
        if not media_id:
            continue
        if media_type == "AUDIO":
            audio_links.append(
                {
                    "label": _mlb_media_feed_label(feed),
                    "url": "https://www.mlb.com/tv/g" + str(game_pk) + "/v" + media_id,
                }
            )
        elif media_type == "VIDEO":
            watch_links.append(
                {
                    "label": _mlb_video_feed_label(feed),
                    "url": "https://www.mlb.com/tv/g" + str(game_pk) + "/v" + media_id,
                }
            )

    result["watch_links"] = watch_links
    result["audio_links"] = audio_links
    _dashboard_cache_set(cache_key, result)
    return result


def _operator_rows_by_team(rows):
    by_team = {}
    for row in rows:
        team_abbr = str(row.get("team_abbr", "") or "").upper()
        if not team_abbr:
            continue
        by_team.setdefault(team_abbr, []).append(
            {
                "name": row.get("name", ""),
                "team_abbr": team_abbr,
                "slot_status": row.get("slot_status", "active"),
                "fantasy_position": row.get("fantasy_position", ""),
            }
        )
    return by_team


def _operator_scoreboard_target_date(args=None):
    requested_date = ""
    if args is not None:
        try:
            requested_date = str(args.get("date", "") or "").strip()
        except Exception:
            requested_date = ""

    if requested_date:
        try:
            return date.fromisoformat(requested_date)
        except ValueError as exc:
            raise ValueError("Invalid date. Expected YYYY-MM-DD.") from exc

    return datetime.now(_OPERATOR_SCOREBOARD_TZ).date()


def _operator_scoreboard_context(scoreboard_date):
    from shared import mlb_fetch

    scoreboard_date_str = scoreboard_date.isoformat()
    schedule_data = mlb_fetch(
        "/schedule?sportId=1&date=" + scoreboard_date_str + "&hydrate=linescore,team"
    )
    abbr_lookup = _operator_team_abbr_map(mlb_fetch)

    my_rows = []
    opp_rows = []
    my_team_name = ""
    opponent_team_name = ""

    try:
        _, _, lg = yahoo_fantasy.get_league()
        matchup = _operator_extract_current_matchup(lg)
        if matchup:
            my_team_name = str(matchup.get("my_team_name", "") or "")
            opponent_team_name = str(matchup.get("opp_team_name", "") or "")
            my_rows = _operator_roster_rows(lg.to_team(matchup.get("my_team_key", "")))
            opp_rows = _operator_roster_rows(
                lg.to_team(matchup.get("opp_team_key", ""))
            )
            _operator_fill_missing_team_abbr(my_rows, mlb_fetch, abbr_lookup)
            _operator_fill_missing_team_abbr(opp_rows, mlb_fetch, abbr_lookup)
    except Exception:
        my_rows = []
        opp_rows = []

    return {
        "date": scoreboard_date_str,
        "schedule_data": schedule_data,
        "abbr_lookup": abbr_lookup,
        "my_team_name": my_team_name,
        "opponent_team_name": opponent_team_name,
        "my_by_team": _operator_rows_by_team(my_rows),
        "opp_by_team": _operator_rows_by_team(opp_rows),
    }


def _operator_attach_relevance(
    normalized, my_by_team, opp_by_team, include_players=True, scoreboard_date=""
):
    away_abbr = str(normalized.get("away_team", {}).get("abbr", "") or "").upper()
    home_abbr = str(normalized.get("home_team", {}).get("abbr", "") or "").upper()
    my_players = list(my_by_team.get(away_abbr, [])) + list(
        my_by_team.get(home_abbr, [])
    )
    opp_players = list(opp_by_team.get(away_abbr, [])) + list(
        opp_by_team.get(home_abbr, [])
    )

    if include_players:
        normalized["my_players"] = my_players
        normalized["opp_players"] = opp_players

    normalized["my_active_count"] = sum(
        1 for p in my_players if p.get("slot_status") == "active"
    )
    normalized["my_inactive_count"] = sum(
        1 for p in my_players if p.get("slot_status") != "active"
    )
    normalized["opp_active_count"] = sum(
        1 for p in opp_players if p.get("slot_status") == "active"
    )
    normalized["opp_inactive_count"] = sum(
        1 for p in opp_players if p.get("slot_status") != "active"
    )
    normalized["total_relevant_count"] = len(my_players) + len(opp_players)
    game_pk = _operator_game_pk_from_game_id(normalized.get("game_id"))
    if game_pk:
        normalized["media_links"] = _mlb_media_links_for_game(game_pk, scoreboard_date)
    return normalized


def _operator_scoreboard_summary_payload(scoreboard_date):
    context = _operator_scoreboard_context(scoreboard_date)
    games = []
    for game in _operator_iter_schedule_games(context["schedule_data"]):
        normalized = _operator_normalize_game(
            game,
            context["abbr_lookup"],
            context["my_team_name"],
            context["opponent_team_name"],
            include_players=False,
            include_live_state=False,
        )
        games.append(
            _operator_attach_relevance(
                normalized,
                context["my_by_team"],
                context["opp_by_team"],
                include_players=False,
                scoreboard_date=context["date"],
            )
        )

    return {
        "date": context["date"],
        "generated_at": _operator_generated_at(),
        "games": _operator_sort_games(games),
    }


def _operator_scoreboard_game_payload(scoreboard_date, requested_game_id):
    if not requested_game_id:
        raise ValueError("Missing game_id")

    context = _operator_scoreboard_context(scoreboard_date)
    requested_token = str(requested_game_id or "").strip()
    target_game = None
    for game in _operator_iter_schedule_games(context["schedule_data"]):
        if "mlb-" + str(game.get("gamePk", "") or "") == requested_token:
            target_game = game
            break

    if target_game is None:
        return {
            "date": context["date"],
            "generated_at": _operator_generated_at(),
            "game": None,
        }

    normalized = _operator_normalize_game(
        target_game,
        context["abbr_lookup"],
        context["my_team_name"],
        context["opponent_team_name"],
        include_players=True,
        include_live_state=True,
    )
    normalized = _operator_attach_relevance(
        normalized,
        context["my_by_team"],
        context["opp_by_team"],
        include_players=True,
        scoreboard_date=context["date"],
    )
    return {
        "date": context["date"],
        "generated_at": _operator_generated_at(),
        "game": normalized,
    }


def _operator_scoreboard_payload(scoreboard_date):
    context = _operator_scoreboard_context(scoreboard_date)
    games = []
    for game in _operator_iter_schedule_games(context["schedule_data"]):
        normalized = _operator_normalize_game(
            game,
            context["abbr_lookup"],
            context["my_team_name"],
            context["opponent_team_name"],
            include_players=True,
            include_live_state=True,
        )
        games.append(
            _operator_attach_relevance(
                normalized,
                context["my_by_team"],
                context["opp_by_team"],
                include_players=True,
                scoreboard_date=context["date"],
            )
        )

    return {
        "date": context["date"],
        "generated_at": _operator_generated_at(),
        "games": _operator_sort_games(games),
    }


def _mlb_stat_group_from_player_info(player_info):
    position_name = str((player_info or {}).get("position", "") or "").strip().lower()
    throws = str((player_info or {}).get("throws", "") or "").strip().upper()
    if "pitch" in position_name or position_name in {
        "p",
        "sp",
        "rp",
        "starter",
        "reliever",
    }:
        return "pitching"
    if throws in {"R", "L", "S"} and position_name in {"pitcher"}:
        return "pitching"
    return "hitting"


def _latest_game_log_entry(games, requested_date=""):
    if not isinstance(games, list) or not games:
        return None, False

    filtered = [
        g for g in games if isinstance(g, dict) and str(g.get("date", "") or "")
    ]
    if not filtered:
        return None, False

    filtered.sort(key=lambda game: str(game.get("date", "") or ""), reverse=True)
    if requested_date:
        for game in filtered:
            if str(game.get("date", "") or "") == requested_date:
                return game, True
    return filtered[0], False


def _mlb_latest_outing_summary(stat_group, entry):
    stat_group = str(stat_group or "")
    entry = entry if isinstance(entry, dict) else {}

    if stat_group == "pitching":
        parts = [
            str(
                entry.get("inningsPitched", "")
                or entry.get("innings_pitched", "")
                or "0.0"
            )
            + " IP",
            str(_safe_int(entry.get("earnedRuns"), 0) or 0) + " ER",
            str(_safe_int(entry.get("strikeOuts"), 0) or 0) + " K",
        ]
        walks = _safe_int(entry.get("baseOnBalls"), 0)
        if walks is not None:
            parts.append(str(walks) + " BB")
        return ", ".join(parts)

    hits = _safe_int(entry.get("hits"), 0) or 0
    runs = _safe_int(entry.get("runs"), 0) or 0
    rbi = _safe_int(entry.get("rbi"), 0) or 0
    homers = _safe_int(entry.get("homeRuns"), 0) or 0
    steals = _safe_int(entry.get("stolenBases"), 0) or 0
    at_bats = _safe_int(entry.get("atBats"), 0) or 0
    parts = [str(hits) + "-" + str(at_bats)]
    if homers:
        parts.append(str(homers) + " HR")
    if runs:
        parts.append(str(runs) + " R")
    if rbi:
        parts.append(str(rbi) + " RBI")
    if steals:
        parts.append(str(steals) + " SB")
    return ", ".join(parts)


def _mlb_latest_outing_payload(player_name="", player_id="", requested_date=""):
    resolved_player_id = str(player_id or "").strip()
    resolved_player_name = str(player_name or "").strip()

    if not resolved_player_id and resolved_player_name:
        resolved = get_mlb_id(resolved_player_name)
        if resolved:
            resolved_player_id = str(resolved)

    if not resolved_player_id:
        raise ValueError("Missing player_name or player_id")

    player_info = mlb_data.cmd_player([resolved_player_id], as_json=True)
    if not isinstance(player_info, dict) or player_info.get("error"):
        raise ValueError("Player not found")

    stat_group = _mlb_stat_group_from_player_info(player_info)
    games = intel._fetch_mlb_game_log(resolved_player_id, stat_group, 14)
    latest_entry, matched_requested_date = _latest_game_log_entry(
        games, requested_date=requested_date
    )
    if latest_entry is None:
        return {
            "player_name": player_info.get("name", resolved_player_name),
            "mlb_id": _safe_int(resolved_player_id, 0) or 0,
            "stat_group": stat_group,
            "requested_date": requested_date or None,
            "matched_requested_date": False,
            "outing": None,
            "summary": "No recent MLB outing found.",
        }

    outing = {
        "date": str(latest_entry.get("date", "") or ""),
        "opponent": str(latest_entry.get("opponent", "") or ""),
        "summary": str(
            latest_entry.get("summary", "")
            or _mlb_latest_outing_summary(stat_group, latest_entry)
        ),
    }

    if stat_group == "pitching":
        outing["innings_pitched"] = str(
            latest_entry.get("inningsPitched", "")
            or latest_entry.get("innings_pitched", "")
            or ""
        )
        outing["hits"] = _safe_int(latest_entry.get("hits"), 0) or 0
        outing["runs"] = _safe_int(latest_entry.get("runs"), 0) or 0
        outing["earned_runs"] = _safe_int(latest_entry.get("earnedRuns"), 0) or 0
        outing["walks"] = _safe_int(latest_entry.get("baseOnBalls"), 0) or 0
        outing["strikeouts"] = _safe_int(latest_entry.get("strikeOuts"), 0) or 0
        outing["home_runs"] = _safe_int(latest_entry.get("homeRuns"), 0) or 0
        outing["pitch_count"] = _safe_int(latest_entry.get("numberOfPitches"), 0)
    else:
        outing["at_bats"] = _safe_int(latest_entry.get("atBats"), 0) or 0
        outing["hits"] = _safe_int(latest_entry.get("hits"), 0) or 0
        outing["runs"] = _safe_int(latest_entry.get("runs"), 0) or 0
        outing["rbi"] = _safe_int(latest_entry.get("rbi"), 0) or 0
        outing["home_runs"] = _safe_int(latest_entry.get("homeRuns"), 0) or 0
        outing["walks"] = _safe_int(latest_entry.get("baseOnBalls"), 0) or 0
        outing["strikeouts"] = _safe_int(latest_entry.get("strikeOuts"), 0) or 0
        outing["stolen_bases"] = _safe_int(latest_entry.get("stolenBases"), 0) or 0

    return {
        "player_name": player_info.get("name", resolved_player_name),
        "mlb_id": _safe_int(resolved_player_id, 0) or 0,
        "team": player_info.get("team", ""),
        "position": player_info.get("position", ""),
        "stat_group": stat_group,
        "requested_date": requested_date or None,
        "matched_requested_date": bool(matched_requested_date),
        "outing": outing,
        "summary": outing["summary"],
    }


def _fantasy_matchup_record(matchup, team1_key, team2_key):
    wins1 = 0
    wins2 = 0
    ties = 0
    stat_winners = matchup.get("stat_winners", []) if isinstance(matchup, dict) else []
    for item in stat_winners if isinstance(stat_winners, list) else []:
        winner = item.get("stat_winner", {}) if isinstance(item, dict) else {}
        if (
            str(winner.get("is_tied", "") or "").lower() in {"1", "true", "yes"}
            or winner.get("is_tied") is True
        ):
            ties += 1
            continue
        winner_team_key = str(winner.get("winner_team_key", "") or "")
        if winner_team_key == team1_key:
            wins1 += 1
        elif winner_team_key == team2_key:
            wins2 += 1
    return wins1, wins2, ties


def _fantasy_scoreboard_summary_payload():
    try:
        _, _, lg = yahoo_fantasy.get_league()
        raw = lg.matchups()
    except Exception:
        return {"week": "", "my_matchup_summary": {}, "league_matchups": []}

    league_data = (
        raw.get("fantasy_content", {}).get("league", [])
        if isinstance(raw, dict)
        else []
    )
    if len(league_data) < 2:
        return {"week": "", "my_matchup_summary": {}, "league_matchups": []}

    scoreboard = league_data[1].get("scoreboard", {})
    week = scoreboard.get("week", "")
    matchup_block = scoreboard.get("0", {}).get("matchups", {})
    count = _safe_int(matchup_block.get("count"), 0) or 0

    league_matchups = []
    my_matchup_summary = {}
    for i in range(count):
        matchup = matchup_block.get(str(i), {}).get("matchup", {})
        matchup_root = matchup.get("0", {}) if isinstance(matchup, dict) else {}
        teams_data = (
            matchup_root.get("teams", {}) if isinstance(matchup_root, dict) else {}
        )
        team1 = teams_data.get("0", {}) if isinstance(teams_data, dict) else {}
        team2 = teams_data.get("1", {}) if isinstance(teams_data, dict) else {}
        team1_name = yahoo_fantasy._extract_team_name(team1)
        team2_name = yahoo_fantasy._extract_team_name(team2)
        team1_key = str(yahoo_fantasy._extract_team_key(team1) or "")
        team2_key = str(yahoo_fantasy._extract_team_key(team2) or "")
        status = str(matchup.get("status", "") or "")
        wins1, wins2, ties = _fantasy_matchup_record(matchup, team1_key, team2_key)
        league_matchups.append(
            {
                "team1": team1_name,
                "team2": team2_name,
                "status": status,
                "score_summary": str(wins1) + "-" + str(wins2) + "-" + str(ties),
            }
        )

        if (
            yahoo_fantasy.TEAM_ID not in team1_key
            and yahoo_fantasy.TEAM_ID not in team2_key
        ):
            continue

        if yahoo_fantasy.TEAM_ID in team1_key:
            my_matchup_summary = {
                "my_team_name": team1_name,
                "opponent_team_name": team2_name,
                "matchup_status": status,
                "wins": wins1,
                "losses": wins2,
                "ties": ties,
            }
        else:
            my_matchup_summary = {
                "my_team_name": team2_name,
                "opponent_team_name": team1_name,
                "matchup_status": status,
                "wins": wins2,
                "losses": wins1,
                "ties": ties,
            }

    return {
        "week": week,
        "my_matchup_summary": my_matchup_summary,
        "league_matchups": league_matchups,
    }


def _rank_hot_free_agents(pos_type, count, scorer):
    candidates = _candidate_free_agents(pos_type, count)
    if not candidates:
        return []

    ranked = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(scorer, candidate) for candidate in candidates]
        for future in futures:
            try:
                row = future.result()
            except Exception:
                row = None
            if row and row.get("summary"):
                ranked.append(row)

    ranked.sort(
        key=lambda row: (
            -_safe_float(row.get("score"), -9999.0),
            -_safe_int(row.get("percent_owned"), 0),
            str(row.get("name", "")),
        )
    )
    return ranked[:count]


def _emit_request_completion(status_code, error=None):
    if getattr(g, "_trace_completion_emitted", False):
        return

    started = getattr(g, "_trace_started_ms", None)
    duration_ms = 0
    if started is not None:
        duration_ms = max(monotonic_ms() - started, 0)

    status = status_code if status_code is not None else 500
    log_trace_event(
        event="request_complete",
        stage="http",
        duration_ms=duration_ms,
        cache_hit=None,
        status=status,
        gate="always",
        force=True,
        method=request.method,
        query_params={k: v for k, v in request.args.items()},
        research_run_id=get_trace_context().get("research_run_id", ""),
        error=error,
    )

    if request.path == "/api/rankings":
        stage_ms = dict(getattr(g, "_rankings_stage_ms", {}) or {})
        known_ms = sum(
            stage_ms.get(k, 0) for k in ("arg_parse", "cmd_rankings", "serialization")
        )
        response_write_ms = max(duration_ms - known_ms, 0)
        stage_ms["response_write"] = response_write_ms
        rankings_status = "ok" if int(status) < 400 else "error"
        log_trace_event(
            event="rankings_stage",
            stage="response_write",
            duration_ms=response_write_ms,
            cache_hit=None,
            status=rankings_status,
            gate="rankings",
        )
        log_trace_event(
            event="rankings_request_summary",
            stage="summary",
            duration_ms=duration_ms,
            cache_hit=None,
            status=status,
            gate="rankings",
            stage_durations_ms=stage_ms,
            error=error,
        )

    g._trace_completion_emitted = True


@app.before_request
def _trace_before_request():
    if not request.path.startswith("/api/"):
        return

    ctx = start_request_trace(
        route=request.path,
        method=request.method,
        headers={k: v for k, v in request.headers.items()},
        args={k: v for k, v in request.args.items()},
    )
    g._trace_started_ms = ctx.get("request_started_ms", monotonic_ms())
    g._trace_completion_emitted = False
    g._rankings_stage_ms = {}
    log_trace_event(
        event="request_start",
        stage="http",
        duration_ms=0,
        cache_hit=None,
        status="start",
        gate="always",
        force=True,
        method=request.method,
        query_params={k: v for k, v in request.args.items()},
        research_run_id=ctx.get("research_run_id", ""),
    )


@app.after_request
def _trace_after_request(response):
    if not request.path.startswith("/api/"):
        return response

    ctx = get_trace_context()
    request_id = ctx.get("request_id")
    if request_id:
        response.headers["X-Request-Id"] = request_id

    _emit_request_completion(response.status_code)
    return response


@app.teardown_request
def _trace_teardown_request(exc):
    try:
        if request.path.startswith("/api/") and exc is not None:
            _emit_request_completion(500, error=str(exc))
    except Exception:
        pass
    finally:
        clear_trace_context()


# --- Session heartbeat (keeps Yahoo cookies alive) ---

HEARTBEAT_INTERVAL = int(os.environ.get("BROWSER_HEARTBEAT_HOURS", "6")) * 3600


def _run_heartbeat():
    """Background loop that refreshes the browser session periodically"""
    import time

    # Wait a bit for startup to settle
    time.sleep(30)
    while True:
        try:
            status = yahoo_browser.is_session_valid()
            if status.get("valid"):
                yahoo_browser.refresh_session()
        except Exception as e:
            print("Heartbeat error: " + str(e))
        time.sleep(HEARTBEAT_INTERVAL)


import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

_heartbeat_thread = threading.Thread(target=_run_heartbeat, daemon=True)
_heartbeat_thread.start()


# --- Optional startup projection warmup ---


def _startup_projections():
    """Background warmup for projections/rankings.

    Disabled by default because warming projections and enriched rankings on each
    cold boot makes operational API routes too slow for Fly auto-start traffic.
    """
    import time

    time.sleep(5)  # Let other startup tasks settle
    try:
        valuations.ensure_projections()
        print("Startup projections loaded successfully")
    except Exception as e:
        print("Startup projections failed: " + str(e))
        return

    # 1. Warm bare (no intel) pool — large count, fast, covers full draft pool
    try:
        draft_count = str(int(os.environ.get("DRAFT_POOL_COUNT", "250")))
        print(
            "Warming bare rankings cache (count=" + draft_count + ", enrich=False)..."
        )
        b_bare = valuations.cmd_rankings(["B", draft_count], as_json=True, enrich=False)
        p_bare = valuations.cmd_rankings(["P", draft_count], as_json=True, enrich=False)
        _set_cached_rankings("B", draft_count, False, [], b_bare, enrich=False)
        _set_cached_rankings("P", draft_count, False, [], p_bare, enrich=False)
        print(
            "Bare rankings cached ("
            + str(len(b_bare.get("players", [])))
            + "B / "
            + str(len(p_bare.get("players", [])))
            + "P)"
        )
    except Exception as e:
        print("Bare rankings warmup failed: " + str(e))

    # 2. Warm enriched pool — smaller count, includes statcast intel
    try:
        print(
            "Warming enriched rankings cache (count="
            + str(_RANKINGS_WARMUP_COUNT)
            + ", enrich=True)..."
        )
        b_result = valuations.cmd_rankings(
            ["B", str(_RANKINGS_WARMUP_COUNT)], as_json=True, enrich=True
        )
        p_result = valuations.cmd_rankings(
            ["P", str(_RANKINGS_WARMUP_COUNT)], as_json=True, enrich=True
        )
        from position_batching import (
            normalize_hitter_payload,
            ranking_position_tokens,
            grouped_all_payload,
        )

        b_norm = normalize_hitter_payload(
            b_result,
            "players",
            _RANKINGS_WARMUP_POSITIONS,
            True,
            ranking_position_tokens,
        )
        all_result = grouped_all_payload(b_norm, p_result)
        _set_cached_rankings(
            "ALL",
            _RANKINGS_WARMUP_COUNT,
            True,
            _RANKINGS_WARMUP_POSITIONS,
            all_result,
            enrich=True,
        )
        _set_cached_rankings(
            "B", _RANKINGS_WARMUP_COUNT, False, [], b_result, enrich=True
        )
        _set_cached_rankings(
            "P", _RANKINGS_WARMUP_COUNT, False, [], p_result, enrich=True
        )
        print(
            "Enriched rankings cached ("
            + str(len(b_result.get("players", [])))
            + "B / "
            + str(len(p_result.get("players", [])))
            + "P)"
        )
    except Exception as e:
        print("Enriched rankings warmup failed: " + str(e))


def _maybe_start_projection_warmup():
    raw = str(os.environ.get("ENABLE_STARTUP_WARMUP", "") or "").strip().lower()
    enabled = raw in {"1", "true", "yes", "on"}
    if not enabled:
        print("Startup rankings warmup disabled")
        return None

    print("Startup rankings warmup enabled")
    proj_thread = threading.Thread(target=_startup_projections, daemon=True)
    proj_thread.start()
    return proj_thread


_proj_thread = _maybe_start_projection_warmup()


# --- Rankings response cache ---

_RANKINGS_CACHE_TTL = int(
    os.environ.get("RANKINGS_CACHE_TTL_SECONDS", "600")
)  # 10 min default
_RANKINGS_WARMUP_COUNT = int(os.environ.get("RANKINGS_WARMUP_COUNT", "150"))
_RANKINGS_WARMUP_POSITIONS = ["C", "1B", "2B", "3B", "SS", "OF", "UTIL"]
_rankings_cache = (
    {}
)  # (variant, pos_type, group_by_position, positions, enrich) -> (expires_at, count, result_dict)
_rankings_cache_lock = threading.Lock()


def _rankings_base_key(
    pos_type, group_by_position, positions, enrich=True, variant="default"
):
    """Cache key — separate buckets for enriched vs bare rankings."""
    return (
        variant,
        pos_type,
        bool(group_by_position),
        tuple(sorted(positions or [])),
        bool(enrich),
    )


def _slice_rankings_result(result, requested_count):
    """Return a copy of result trimmed to requested_count players per group."""
    n = int(requested_count)
    if result.get("pos_type") == "ALL":
        sliced = dict(result)
        sliced["groups"] = {}
        for grp_key, grp in result.get("groups", {}).items():
            g = dict(grp)
            g["players"] = (g.get("players") or [])[:n]
            sliced["groups"][grp_key] = g
        return sliced
    r = dict(result)
    r["players"] = (r.get("players") or [])[:n]
    return r


def _get_cached_rankings(
    pos_type, count, group_by_position, positions, enrich=True, variant="default"
):
    import time

    key = _rankings_base_key(pos_type, group_by_position, positions, enrich, variant)
    with _rankings_cache_lock:
        entry = _rankings_cache.get(key)
        if entry and time.monotonic() < entry[0]:
            cached_count, cached_result = entry[1], entry[2]
            if not _rankings_result_has_players(cached_result):
                return None
            if cached_count >= int(count):
                return _slice_rankings_result(cached_result, count)
        return None


def _rankings_result_has_players(result):
    if not isinstance(result, dict):
        return False

    players = result.get("players")
    if isinstance(players, list) and len(players) > 0:
        return True

    groups = result.get("groups")
    if not isinstance(groups, dict):
        return False

    for group in groups.values():
        if _rankings_result_has_players(group):
            return True
        if not isinstance(group, dict):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, dict):
            continue
        for bucket_players in buckets.values():
            if isinstance(bucket_players, list) and len(bucket_players) > 0:
                return True

    return False


def _set_cached_rankings(
    pos_type,
    count,
    group_by_position,
    positions,
    result,
    enrich=True,
    variant="default",
):
    import time

    if not _rankings_result_has_players(result):
        return
    key = _rankings_base_key(pos_type, group_by_position, positions, enrich, variant)
    with _rankings_cache_lock:
        existing = _rankings_cache.get(key)
        # Only overwrite if new result has more players (or cache is empty/expired)
        if (
            existing is None
            or time.monotonic() >= existing[0]
            or int(count) >= existing[1]
        ):
            _rankings_cache[key] = (
                time.monotonic() + _RANKINGS_CACHE_TTL,
                int(count),
                result,
            )


# --- Health check ---


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/endpoints")
def api_endpoints():
    """List all registered API endpoints with methods"""
    endpoints = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.rule.startswith("/api/"):
            methods = sorted(rule.methods - {"OPTIONS", "HEAD"})
            endpoints.append(
                {
                    "path": rule.rule,
                    "methods": methods,
                }
            )
    return jsonify({"endpoints": endpoints})


@app.route("/api/browser-login-status")
def api_browser_login_status():
    try:
        result = yahoo_browser.is_session_valid()
        result["heartbeat"] = yahoo_browser.get_heartbeat_state()
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500, extra={"valid": False, "reason": str(e)})


@app.route("/api/auth-status")
def api_auth_status():
    try:
        return jsonify(_auth_status_payload())
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/change-team-name", methods=["POST"])
def api_change_team_name():
    try:
        data = request.get_json(force=True) if request.is_json else request.form
        new_name = data.get("new_name", "")
        if not new_name:
            return jsonify({"error": "Missing new_name"}), 400
        result = yahoo_browser.change_team_name(new_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/change-team-logo", methods=["POST"])
def api_change_team_logo():
    try:
        data = request.get_json(force=True) if request.is_json else request.form
        image_path = data.get("image_path", "")
        if not image_path:
            return jsonify({"error": "Missing image_path"}), 400
        result = yahoo_browser.change_team_logo(image_path)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Yahoo Fantasy (yahoo-fantasy.py) ---
# TS tools call: /api/roster, /api/free-agents, /api/standings, etc.


@app.route("/api/roster")
def api_roster():
    cache_key = ("roster",)
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        return jsonify(cached)
    try:
        result = yahoo_fantasy.cmd_roster(["false"], as_json=True)
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/free-agents")
def api_free_agents():
    try:
        pos_type = request.args.get("pos_type", "B")
        count = request.args.get("count", "20")
        result = yahoo_fantasy.cmd_free_agents([pos_type, count], as_json=True)
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/hot-bat-free-agents")
def api_hot_bat_free_agents():
    count = _safe_int(request.args.get("count"), 8) or 8
    count = max(1, min(count, 25))
    cache_key = ("hot-bat-free-agents", count)
    cached = _dashboard_cache_get(cache_key, 120)
    if cached is not None:
        return jsonify(cached)
    try:
        result = {
            "window": "Last 7 days",
            "players": _rank_hot_free_agents("B", count, _score_hot_bat),
        }
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception:
        return jsonify({"window": "Last 7 days", "players": []})


@app.route("/api/hot-hand-free-agent-pitchers")
def api_hot_hand_free_agent_pitchers():
    count = _safe_int(request.args.get("count"), 8) or 8
    count = max(1, min(count, 25))
    cache_key = ("hot-hand-free-agent-pitchers", count)
    cached = _dashboard_cache_get(cache_key, 120)
    if cached is not None:
        return jsonify(cached)
    try:
        result = {
            "window": "Last 3 appearances",
            "players": _rank_hot_free_agents("P", count, _score_hot_hand),
        }
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception:
        return jsonify({"window": "Last 3 appearances", "players": []})


@app.route("/api/standings")
def api_standings():
    try:
        result = yahoo_fantasy.cmd_standings([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/info")
def api_info():
    try:
        result = yahoo_fantasy.cmd_info([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/league-context")
def api_league_context():
    try:
        from shared import (
            get_league_settings,
            get_league_context,
            normalize_team_details,
        )

        settings = get_league_settings()
        result = {
            "waiver_type": settings.get("waiver_type", "unknown"),
            "uses_faab": settings.get("uses_faab", False),
            "scoring_type": settings.get("scoring_type", ""),
            "stat_categories": settings.get("stat_categories", []),
            "roster_positions": settings.get("roster_positions", []),
            "num_teams": settings.get("num_teams", 0),
            "max_weekly_adds": settings.get("max_weekly_adds", 0),
        }
        # Include FAAB balance only for FAAB leagues
        if settings.get("uses_faab"):
            try:
                sc, gm, lg, team = get_league_context()
                d = normalize_team_details(team)
                fb = d.get("faab_balance")
                if fb is not None:
                    result["faab_balance"] = fb
            except Exception as e:
                print(
                    "Warning: could not fetch FAAB balance for league-context: "
                    + str(e)
                )
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/search")
def api_search():
    try:
        # TS tool sends "name" param
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "Missing name parameter"}), 400
        result = yahoo_fantasy.cmd_search([name], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/add", methods=["POST"])
def api_add():
    try:
        # TS tool sends JSON body: { player_id: "..." }
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id", "")
        if not player_id:
            player_id = request.args.get("player_id", "")
        if not player_id:
            return jsonify({"error": "Missing player_id"}), 400
        result = yahoo_fantasy.cmd_add([player_id], as_json=True)
        if _mutation_succeeded(result):
            _invalidate_team_state_caches()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/drop", methods=["POST"])
def api_drop():
    try:
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id", "")
        if not player_id:
            player_id = request.args.get("player_id", "")
        if not player_id:
            return jsonify({"error": "Missing player_id"}), 400
        result = yahoo_fantasy.cmd_drop([player_id], as_json=True)
        if _mutation_succeeded(result):
            _invalidate_team_state_caches()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/swap", methods=["POST"])
def api_swap():
    try:
        data = request.get_json(silent=True) or {}
        add_id = data.get("add_id", "")
        drop_id = data.get("drop_id", "")
        if not add_id:
            add_id = request.args.get("add_id", "")
        if not drop_id:
            drop_id = request.args.get("drop_id", "")
        if not add_id or not drop_id:
            return jsonify({"error": "Missing add_id and/or drop_id"}), 400
        result = yahoo_fantasy.cmd_swap([add_id, drop_id], as_json=True)
        if _mutation_succeeded(result):
            _invalidate_team_state_caches()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/matchups")
def api_matchups():
    try:
        args = []
        week = request.args.get("week", "")
        if week:
            args.append(week)
        result = yahoo_fantasy.cmd_matchups(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/scoreboard")
def api_scoreboard():
    try:
        args = []
        week = request.args.get("week", "")
        if week:
            args.append(week)
        result = yahoo_fantasy.cmd_scoreboard(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/operator-scoreboard")
def api_operator_scoreboard():
    try:
        scoreboard_date = _operator_scoreboard_target_date(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    cache_key = ("operator-scoreboard", scoreboard_date.isoformat())
    requested_game_id = str(request.args.get("game_id", "") or "").strip()
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        if requested_game_id:
            filtered = dict(cached)
            filtered["games"] = [
                game
                for game in (cached.get("games") or [])
                if str(game.get("game_id", "") or "") == requested_game_id
            ]
            return jsonify(filtered)
        return jsonify(cached)
    try:
        result = _operator_scoreboard_payload(scoreboard_date)
        _dashboard_cache_set(cache_key, result)
        if requested_game_id:
            filtered = dict(result)
            filtered["games"] = [
                game
                for game in (result.get("games") or [])
                if str(game.get("game_id", "") or "") == requested_game_id
            ]
            return jsonify(filtered)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fantasy-scoreboard-summary")
def api_fantasy_scoreboard_summary():
    cache_key = ("fantasy-scoreboard-summary", "current")
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        return jsonify(cached)
    try:
        result = _fantasy_scoreboard_summary_payload()
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/operator-scoreboard-summary")
def api_operator_scoreboard_summary():
    try:
        scoreboard_date = _operator_scoreboard_target_date(request.args)
    except ValueError as e:
        return _json_error(e, status=400)

    cache_key = ("operator-scoreboard-summary", scoreboard_date.isoformat())
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        return jsonify(cached)
    try:
        result = _operator_scoreboard_summary_payload(scoreboard_date)
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/operator-scoreboard-game")
def api_operator_scoreboard_game():
    requested_game_id = str(request.args.get("game_id", "") or "").strip()
    if not requested_game_id:
        return _json_error("Missing game_id", status=400)

    try:
        scoreboard_date = _operator_scoreboard_target_date(request.args)
    except ValueError as e:
        return _json_error(e, status=400)

    cache_key = (
        "operator-scoreboard-game",
        scoreboard_date.isoformat(),
        requested_game_id,
    )
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        return jsonify(cached)
    try:
        result = _operator_scoreboard_game_payload(scoreboard_date, requested_game_id)
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except ValueError as e:
        return _json_error(e, status=400)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/transactions")
def api_transactions():
    try:
        args = []
        tx_type = request.args.get("type", "")
        if tx_type:
            args.append(tx_type)
        count = request.args.get("count", "")
        if count:
            args.append(count)
        result = yahoo_fantasy.cmd_transactions(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stat-categories")
def api_stat_categories():
    try:
        result = yahoo_fantasy.cmd_stat_categories([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transaction-trends")
def api_transaction_trends():
    try:
        result = yahoo_fantasy.cmd_transaction_trends([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/matchup-detail")
def api_matchup_detail():
    try:
        result = yahoo_fantasy.cmd_matchup_detail([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return _json_error(e, status=500)


# --- Draft Assistant (draft-assistant.py) ---
# TS tools call: /api/draft-status, /api/draft-recommend, /api/draft-cheatsheet, /api/best-available


@app.route("/api/draft-status")
def api_draft_status():
    try:
        da = draft_assistant.DraftAssistant()
        result = da.status(as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/draft-recommend")
def api_draft_recommend():
    try:
        da = draft_assistant.DraftAssistant()
        result = da.recommend(as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/draft-cheatsheet")
def api_draft_cheatsheet():
    try:
        result = draft_assistant.cmd_cheatsheet([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/best-available")
def api_best_available():
    _TIMEOUT = int(os.environ.get("BEST_AVAILABLE_TIMEOUT_SECONDS", "30"))
    try:
        pos_type = request.args.get("pos_type", "B").upper()
        count = request.args.get("count", "25")
        include_intel = request.args.get("include_intel", "false")
        group_by_position = _safe_bool(request.args.get("group_by_position", "false"))
        positions = _parse_hitter_positions_csv(request.args.get("positions", ""))
        cache_key = (
            "best-available",
            pos_type,
            str(count),
            str(include_intel).lower(),
            bool(group_by_position),
            tuple(positions),
        )
        cached = _dashboard_cache_get(cache_key, 45)
        if cached is not None:
            return jsonify(cached)

        def _fetch_best_available():
            if pos_type == "ALL":
                hitters = draft_assistant.cmd_best_available(
                    ["B", count, include_intel], as_json=True
                )
                pitchers = draft_assistant.cmd_best_available(
                    ["P", count, include_intel], as_json=True
                )
                hitters = _normalize_hitter_payload(
                    hitters,
                    "players",
                    positions,
                    group_by_position,
                    _best_available_position_tokens,
                )
                return _grouped_all_payload(hitters, pitchers)
            else:
                result = draft_assistant.cmd_best_available(
                    [pos_type, count, include_intel], as_json=True
                )
                if pos_type == "B":
                    result = _normalize_hitter_payload(
                        result,
                        "players",
                        positions,
                        group_by_position,
                        _best_available_position_tokens,
                    )
                return result

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_fetch_best_available)
            done, _ = wait([future], timeout=_TIMEOUT)
            if not done:
                empty_b = {"pos_type": "B", "players": []}
                empty_p = {"pos_type": "P", "players": []}
                fallback = (
                    _grouped_all_payload(empty_b, empty_p)
                    if pos_type == "ALL"
                    else {"pos_type": pos_type, "players": []}
                )
                return jsonify(fallback)
            result = future.result()
            _dashboard_cache_set(cache_key, result)
            return jsonify(result)
        finally:
            pool.shutdown(wait=False)
    except ValueError as e:
        return _json_error(e, status=400)
    except Exception as e:
        return _json_error(e, status=500)


# --- Valuations (valuations.py) ---
# TS tools call: /api/rankings, /api/compare, /api/value


# --- Draft Sim (draft_sim.py) ---


@app.route("/api/draft-sim")
def api_draft_sim():
    """
    Run a snake draft simulation and return per-pick recommendations.

    Query params:
      draft_position  int  required  your pick slot (1-indexed)
      num_teams       int  optional  default 12
      rounds          int  optional  default 23
      noise           int  optional  default 3  (ADP variance per opponent pick)
      seed            int  optional  default 42
    """
    try:
        draft_position = int(request.args.get("draft_position", 3))
        num_teams = int(request.args.get("num_teams", 12))
        rounds = int(request.args.get("rounds", 23))
        noise = int(request.args.get("noise", 3))
        seed = int(request.args.get("seed", 42))

        if not (1 <= draft_position <= num_teams):
            return (
                jsonify({"error": "draft_position must be between 1 and num_teams"}),
                400,
            )

        # Use the pre-warmed bare rankings pool (enrich=False, count=250+)
        draft_count = str(int(os.environ.get("DRAFT_POOL_COUNT", "250")))
        b_cached = _get_cached_rankings("B", int(draft_count), False, [], enrich=False)
        p_cached = _get_cached_rankings("P", int(draft_count), False, [], enrich=False)

        # Fall back to live fetch if cache is cold
        if b_cached is None:
            b_cached = valuations.cmd_rankings(
                ["B", draft_count], as_json=True, enrich=False
            )
        if p_cached is None:
            p_cached = valuations.cmd_rankings(
                ["P", draft_count], as_json=True, enrich=False
            )

        batters = b_cached.get("players", []) if isinstance(b_cached, dict) else []
        pitchers = p_cached.get("players", []) if isinstance(p_cached, dict) else []

        result = draft_sim.simulate_draft(
            batters=batters,
            pitchers=pitchers,
            draft_position=draft_position,
            num_teams=num_teams,
            rounds=rounds,
            noise=noise,
            seed=seed,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Valuations (valuations.py) ---
# TS tools call: /api/rankings, /api/compare, /api/value


@app.route("/api/rankings")
def api_rankings():
    def _timed_stage(stage_name, fn):
        started = monotonic_ms()
        status = "ok"
        try:
            return fn()
        except Exception:
            status = "error"
            raise
        finally:
            elapsed = max(monotonic_ms() - started, 0)
            stage_map = dict(getattr(g, "_rankings_stage_ms", {}) or {})
            stage_map[stage_name] = elapsed
            g._rankings_stage_ms = stage_map
            log_trace_event(
                event="rankings_stage",
                stage=stage_name,
                duration_ms=elapsed,
                cache_hit=None,
                status=status,
                gate="rankings",
            )

    try:
        pos_type, count, group_by_position, positions, enrich = _timed_stage(
            "arg_parse",
            lambda: (
                request.args.get("pos_type", "B").upper(),
                request.args.get("count", "25"),
                _safe_bool(request.args.get("group_by_position", "false")),
                _parse_hitter_positions_csv(request.args.get("positions", "")),
                _safe_bool(request.args.get("enrich", "true")),
            ),
        )
        update_trace_context(pos_type=pos_type, count=_safe_int(count, None))

        cached = _get_cached_rankings(
            pos_type, count, group_by_position, positions, enrich
        )
        if cached is not None:
            log_trace_event(
                event="rankings_cache_hit",
                stage="api_rankings",
                duration_ms=0,
                cache_hit=True,
                status="ok",
                gate="rankings",
            )
            return jsonify(cached)

        if pos_type == "ALL":
            with ThreadPoolExecutor(max_workers=2) as pool:
                hitters_future = pool.submit(
                    valuations.cmd_rankings, ["B", count], True, enrich
                )
                pitchers_future = pool.submit(
                    valuations.cmd_rankings, ["P", count], True, enrich
                )
                hitters = hitters_future.result()
                pitchers = pitchers_future.result()
            hitters = _normalize_hitter_payload(
                hitters,
                "players",
                positions,
                group_by_position,
                _ranking_position_tokens,
            )
            result = _grouped_all_payload(hitters, pitchers)
        else:
            result = _timed_stage(
                "cmd_rankings",
                lambda: valuations.cmd_rankings(
                    [pos_type, count], as_json=True, enrich=enrich
                ),
            )
            if pos_type == "B":
                result = _normalize_hitter_payload(
                    result,
                    "players",
                    positions,
                    group_by_position,
                    _ranking_position_tokens,
                )

        response = _timed_stage("serialization", lambda: jsonify(result))
        _set_cached_rankings(
            pos_type, count, group_by_position, positions, result, enrich
        )
        return response
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rankings/live")
def api_rankings_live():
    try:
        pos_type = request.args.get("pos_type", "B").upper()
        count = request.args.get("count", "25")
        enrich = _safe_bool(request.args.get("enrich", "true"))

        cached = _get_cached_rankings(
            pos_type, count, False, [], enrich, variant="live"
        )
        if cached is not None:
            return jsonify(cached)

        if pos_type == "ALL":
            with ThreadPoolExecutor(max_workers=2) as pool:
                hitters_future = pool.submit(
                    valuations.cmd_rankings_live, ["B", count], True, enrich
                )
                pitchers_future = pool.submit(
                    valuations.cmd_rankings_live, ["P", count], True, enrich
                )
                hitters = hitters_future.result()
                pitchers = pitchers_future.result()
            result = _grouped_all_payload(hitters, pitchers)
        else:
            result = valuations.cmd_rankings_live(
                [pos_type, count], as_json=True, enrich=enrich
            )

        _set_cached_rankings(pos_type, count, False, [], result, enrich, variant="live")
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/compare")
def api_compare():
    try:
        # TS tool sends player1 and player2 params
        player1 = request.args.get("player1", "")
        player2 = request.args.get("player2", "")
        if not player1 or not player2:
            return jsonify({"error": "Missing player1 and/or player2 parameters"}), 400
        result = valuations.cmd_compare([player1, player2], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/value")
def api_value():
    try:
        # TS tool sends "player_name" param
        name = request.args.get("player_name", "")
        if not name:
            name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "Missing player_name parameter"}), 400
        cache_key = ("value", str(name).strip().lower())
        cached = _dashboard_cache_get(cache_key, 300)
        if cached is not None:
            return jsonify(cached)
        result = valuations.cmd_value([name], as_json=True)
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projections-update", methods=["POST"])
def api_projections_update():
    try:
        data = request.get_json(silent=True) or {}
        proj_type = data.get("proj_type", "steamer")
        result = valuations.ensure_projections(proj_type=proj_type, force=True)
        _dashboard_cache_delete_prefix("value")
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projection-disagreements", methods=["GET"])
def api_projection_disagreements():
    try:
        pos_type = request.args.get("pos_type", "B").upper()
        count = int(request.args.get("count", "20"))
        group_by_position = _safe_bool(request.args.get("group_by_position", "false"))
        positions = _parse_hitter_positions_csv(request.args.get("positions", ""))

        if pos_type == "ALL":
            hitters = {
                "pos_type": "B",
                "disagreements": valuations.compute_projection_disagreements(
                    stats_type="bat", count=count
                ),
            }
            pitchers = {
                "pos_type": "P",
                "disagreements": valuations.compute_projection_disagreements(
                    stats_type="pit", count=count
                ),
            }
            hitters = _normalize_hitter_payload(
                hitters,
                "disagreements",
                positions,
                group_by_position,
                _disagreement_position_tokens,
            )
            return jsonify(_grouped_all_payload(hitters, pitchers))

        stats_type = "bat" if pos_type == "B" else "pit"
        result = {
            "pos_type": pos_type,
            "disagreements": valuations.compute_projection_disagreements(
                stats_type=stats_type, count=count
            ),
        }
        if pos_type == "B":
            result = _normalize_hitter_payload(
                result,
                "disagreements",
                positions,
                group_by_position,
                _disagreement_position_tokens,
            )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/zscore-shifts", methods=["GET"])
def api_zscore_shifts():
    try:
        count = int(request.args.get("count", "25"))
        result = valuations.compute_zscore_shifts(count=count)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/park-factors", methods=["GET"])
def api_park_factors():
    try:
        factors = []
        for team, factor in sorted(valuations.PARK_FACTORS.items()):
            factors.append({"team": team, "factor": factor})
        return jsonify({"park_factors": factors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Season Manager (season-manager.py) ---
# TS tools call: /api/lineup-optimize, /api/category-check, etc.


@app.route("/api/lineup-optimize")
def api_lineup_optimize():
    try:
        args = []
        apply_flag = request.args.get("apply", "false")
        include_intel = _safe_bool(request.args.get("include_intel", "false"))
        if apply_flag.lower() != "true":
            cache_key = (
                "lineup-optimize",
                date.today().isoformat(),
                bool(include_intel),
            )
            cached = _dashboard_cache_get(cache_key, 60)
            if cached is not None:
                return jsonify(cached)
        if apply_flag.lower() == "true":
            args.append("--apply")
        result = season_manager.cmd_lineup_optimize(
            args, as_json=True, include_intel=include_intel
        )
        if apply_flag.lower() == "true":
            _dashboard_cache_delete_prefix("lineup-optimize")
        else:
            _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/category-check")
def api_category_check():
    try:
        result = season_manager.cmd_category_check([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/injury-report")
def api_injury_report():
    try:
        cache_key = ("injury-report", date.today().isoformat())
        cached = _dashboard_cache_get(cache_key, 30)
        if cached is not None:
            return jsonify(cached)
        result = season_manager.cmd_injury_report([], as_json=True)
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/waiver-analyze")
def api_waiver_analyze():
    try:
        pos_type = request.args.get("pos_type", "B").upper()
        count = request.args.get("count", "15")
        cache_key = ("waiver-analyze", date.today().isoformat(), pos_type, str(count))
        cached = _dashboard_cache_get(cache_key, 30)
        if cached is not None:
            return jsonify(cached)
        result = season_manager.cmd_waiver_analyze([pos_type, count], as_json=True)
        _dashboard_cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/streaming")
def api_streaming():
    try:
        args = []
        week = request.args.get("week", "")
        if week:
            args.append(week)
        result = season_manager.cmd_streaming(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade-eval", methods=["POST"])
def api_trade_eval():
    try:
        # TS tool sends JSON body: { give_ids: "...", get_ids: "..." }
        data = request.get_json(silent=True) or {}
        give_ids = data.get("give_ids", "")
        get_ids = data.get("get_ids", "")
        if not give_ids or not get_ids:
            return jsonify({"error": "Missing give_ids and/or get_ids"}), 400
        result = season_manager.cmd_trade_eval([give_ids, get_ids], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/category-simulate")
def api_category_simulate():
    try:
        add_name = request.args.get("add_name", "")
        drop_name = request.args.get("drop_name", "")
        if not add_name:
            return jsonify({"error": "Missing add_name parameter"}), 400
        args = [add_name]
        if drop_name:
            args.append(drop_name)
        result = season_manager.cmd_category_simulate(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scout-opponent")
def api_scout_opponent():
    try:
        result = season_manager.cmd_scout_opponent([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/matchup-strategy")
def api_matchup_strategy():
    try:
        result = season_manager.cmd_matchup_strategy([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily-update")
def api_daily_update():
    try:
        result = season_manager.cmd_daily_update([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pending-trades")
def api_pending_trades():
    try:
        result = season_manager.cmd_pending_trades([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/propose-trade", methods=["POST"])
def api_propose_trade():
    try:
        data = request.get_json(silent=True) or {}
        their_team_key = data.get("their_team_key", "")
        your_player_ids = data.get("your_player_ids", "")
        their_player_ids = data.get("their_player_ids", "")
        note = data.get("note", "")
        if not their_team_key or not your_player_ids or not their_player_ids:
            return (
                jsonify(
                    {
                        "error": "Missing their_team_key, your_player_ids, or their_player_ids"
                    }
                ),
                400,
            )
        args = [their_team_key, your_player_ids, their_player_ids]
        if note:
            args.append(note)
        result = season_manager.cmd_propose_trade(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accept-trade", methods=["POST"])
def api_accept_trade():
    try:
        data = request.get_json(silent=True) or {}
        transaction_key = data.get("transaction_key", "")
        note = data.get("note", "")
        if not transaction_key:
            return jsonify({"error": "Missing transaction_key"}), 400
        args = [transaction_key]
        if note:
            args.append(note)
        result = season_manager.cmd_accept_trade(args, as_json=True)
        if _mutation_succeeded(result):
            _invalidate_team_state_caches()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reject-trade", methods=["POST"])
def api_reject_trade():
    try:
        data = request.get_json(silent=True) or {}
        transaction_key = data.get("transaction_key", "")
        note = data.get("note", "")
        if not transaction_key:
            return jsonify({"error": "Missing transaction_key"}), 400
        args = [transaction_key]
        if note:
            args.append(note)
        result = season_manager.cmd_reject_trade(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/set-lineup", methods=["POST"])
def api_set_lineup():
    try:
        data = request.get_json(silent=True) or {}
        moves = data.get("moves", [])
        if not moves:
            return jsonify({"error": "Missing moves array"}), 400
        # Convert moves to "player_id:position" arg format
        args = []
        for m in moves:
            pid = m.get("player_id", "")
            pos = m.get("position", "")
            if pid and pos:
                args.append(str(pid) + ":" + str(pos))
        if not args:
            return jsonify({"error": "No valid moves provided"}), 400
        result = season_manager.cmd_set_lineup(args, as_json=True)
        if _mutation_succeeded(result):
            _invalidate_team_state_caches()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Waiver Claims (yahoo-fantasy.py) ---


@app.route("/api/waiver-claim", methods=["POST"])
def api_waiver_claim():
    try:
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id", "")
        if not player_id:
            return jsonify({"error": "Missing player_id"}), 400
        args = [player_id]
        faab = data.get("faab")
        if faab is not None:
            args.append(str(faab))
        result = yahoo_fantasy.cmd_waiver_claim(args, as_json=True)
        if _mutation_succeeded(result):
            _invalidate_team_state_caches()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/waiver-claim-swap", methods=["POST"])
def api_waiver_claim_swap():
    try:
        data = request.get_json(silent=True) or {}
        add_id = data.get("add_id", "")
        drop_id = data.get("drop_id", "")
        if not add_id or not drop_id:
            return jsonify({"error": "Missing add_id and/or drop_id"}), 400
        args = [add_id, drop_id]
        faab = data.get("faab")
        if faab is not None:
            args.append(str(faab))
        result = yahoo_fantasy.cmd_waiver_claim_swap(args, as_json=True)
        if _mutation_succeeded(result):
            _invalidate_team_state_caches()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Who Owns / League Pulse (yahoo-fantasy.py) ---


@app.route("/api/who-owns")
def api_who_owns():
    try:
        player_id = request.args.get("player_id", "")
        if not player_id:
            return jsonify({"error": "Missing player_id parameter"}), 400
        result = yahoo_fantasy.cmd_who_owns([player_id], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/percent-owned")
def api_percent_owned():
    try:
        ids = request.args.get("ids", "")
        if not ids:
            return (
                jsonify(
                    {"error": "Missing ids parameter (comma-separated player IDs)"}
                ),
                400,
            )
        args = [pid.strip() for pid in ids.split(",") if pid.strip()]
        result = yahoo_fantasy.cmd_percent_owned(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/league-pulse")
def api_league_pulse():
    try:
        result = yahoo_fantasy.cmd_league_pulse([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Phase 3: What's New & Trade Finder ---


@app.route("/api/whats-new")
def api_whats_new():
    try:
        result = season_manager.cmd_whats_new([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade-finder")
def api_trade_finder():
    try:
        target = request.args.get("target", "")
        args = [target] if target else []
        result = season_manager.cmd_trade_finder(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Phase 4: Power Rankings, Week Planner, Season Pace ---


@app.route("/api/power-rankings")
def api_power_rankings():
    try:
        result = season_manager.cmd_power_rankings([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/week-planner")
def api_week_planner():
    try:
        args = []
        week = request.args.get("week", "")
        if week:
            args.append(week)
        result = season_manager.cmd_week_planner(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/season-pace")
def api_season_pace():
    try:
        result = season_manager.cmd_season_pace([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Phase 5: Closer Monitor ---


@app.route("/api/closer-monitor")
def api_closer_monitor():
    try:
        result = season_manager.cmd_closer_monitor([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Phase 5: Pitcher Matchup ---


@app.route("/api/pitcher-matchup")
def api_pitcher_matchup():
    try:
        week = request.args.get("week", "")
        args = [week] if week else []
        result = season_manager.cmd_pitcher_matchup(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- New Yahoo API Tools ---


@app.route("/api/player-stats")
def api_player_stats():
    try:
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "Missing name parameter"}), 400
        period = request.args.get("period", "season")
        week = request.args.get("week", "")
        date_str = request.args.get("date", "")
        args = [name, period]
        if period == "week" and week:
            args.append(week)
        elif period == "date" and date_str:
            args.append(date_str)
        result = yahoo_fantasy.cmd_player_stats(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/roster-stats")
def api_roster_stats():
    try:
        period = request.args.get("period", "season")
        week = request.args.get("week", "")
        team_key = request.args.get("team_key", "")
        args = []
        if period:
            args.append("--period=" + period)
        if week:
            args.append("--week=" + week)
        if team_key:
            args.append("--team=" + team_key)
        result = season_manager.cmd_roster_stats(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/faab-recommend")
def api_faab_recommend():
    try:
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "name parameter required"}), 400
        result = season_manager.cmd_faab_recommend([name], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ownership-trends")
def api_ownership_trends():
    try:
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "name parameter required"}), 400
        result = season_manager.cmd_ownership_trends([name], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/category-trends")
def api_category_trends():
    try:
        result = season_manager.cmd_category_trends([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/punt-advisor")
def api_punt_advisor():
    try:
        result = season_manager.cmd_punt_advisor([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/il-stash-advisor")
def api_il_stash_advisor():
    try:
        result = season_manager.cmd_il_stash_advisor([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/playoff-planner")
def api_playoff_planner():
    try:
        result = season_manager.cmd_playoff_planner([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/optimal-moves")
def api_optimal_moves():
    try:
        count = request.args.get("count", "5")
        result = season_manager.cmd_optimal_moves([count], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/weekly-narrative")
def api_weekly_narrative():
    try:
        result = season_manager.cmd_weekly_narrative([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/roster-history")
def api_roster_history():
    try:
        week = request.args.get("week", "")
        date_str = request.args.get("date", "")
        team_key = request.args.get("team_key", "")
        lookup = week or date_str
        if not lookup:
            return jsonify({"error": "Missing week or date parameter"}), 400
        args = [lookup]
        if team_key:
            args.append(team_key)
        result = yahoo_fantasy.cmd_roster_history(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/waivers")
def api_waivers():
    try:
        result = yahoo_fantasy.cmd_waivers([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/taken-players")
def api_taken_players():
    _TIMEOUT = int(os.environ.get("TAKEN_PLAYERS_TIMEOUT_SECONDS", "15"))
    _FRESH_TTL = int(os.environ.get("TAKEN_PLAYERS_CACHE_TTL_SECONDS", "120"))
    _STALE_TTL = int(os.environ.get("TAKEN_PLAYERS_STALE_CACHE_TTL_SECONDS", "900"))
    _LEASE_TTL = int(os.environ.get("TAKEN_PLAYERS_REFRESH_LEASE_SECONDS", str(max(_TIMEOUT * 4, 60))))
    try:
        position = request.args.get("position", "")
        normalized_position = str(position or "").upper()
        cache_key = ("taken-players", normalized_position)
        cached = _dashboard_cache_get(cache_key, _FRESH_TTL)
        if cached is not None:
            return jsonify(cached)

        stale = _dashboard_cache_get(cache_key, _STALE_TTL)
        entry = _singleflight_entry(cache_key, _LEASE_TTL)
        lock = entry["lock"]
        if not lock.acquire(blocking=False):
            if stale is not None:
                return jsonify(stale)
            return jsonify(_taken_players_fallback(position))
        _mark_singleflight_started(cache_key, lock)

        cached = _dashboard_cache_get(cache_key, _FRESH_TTL)
        if cached is not None:
            _release_singleflight(cache_key, lock)
            return jsonify(cached)

        args = [position] if position else []
        worker_started = False
        try:
            done_event = threading.Event()
            result_holder = {}
            worker = threading.Thread(
                target=_refresh_taken_players_async,
                args=(cache_key, args, lock, done_event, result_holder),
                daemon=True,
            )
            worker.start()
            worker_started = True
            if not done_event.wait(timeout=_TIMEOUT):
                if stale is not None:
                    return jsonify(stale)
                return jsonify(_taken_players_fallback(position))

            error = result_holder.get("error")
            if error is not None:
                if stale is not None:
                    return jsonify(stale)
                raise error
            result = result_holder.get("result")
            return jsonify(result)
        except Exception:
            if not worker_started:
                _release_singleflight(cache_key, lock)
            raise
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/player-universe")
def api_player_universe():
    try:
        max_players = _safe_int(request.args.get("count"), 120)
        if max_players is None:
            max_players = 120

        from shared import get_league_settings

        result = player_universe.build_player_universe(
            yahoo_fantasy=yahoo_fantasy,
            league_context_fetcher=get_league_settings,
            max_players_per_group=max_players,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- MLB Data (mlb-data.py) ---
# TS tools call: /api/mlb/teams, /api/mlb/roster, etc. (these already match)


@app.route("/api/mlb/teams")
def api_mlb_teams():
    try:
        result = mlb_data.cmd_teams([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/roster")
def api_mlb_roster():
    try:
        team = request.args.get("team", "")
        if not team:
            return jsonify({"error": "Missing team parameter"}), 400
        result = mlb_data.cmd_roster([team], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/player")
def api_mlb_player():
    try:
        player_id = request.args.get("player_id", "")
        if not player_id:
            return jsonify({"error": "Missing player_id parameter"}), 400
        result = mlb_data.cmd_player([player_id], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/stats")
def api_mlb_stats():
    try:
        player_id = request.args.get("player_id", "")
        if not player_id:
            return jsonify({"error": "Missing player_id parameter"}), 400
        args = [player_id]
        season = request.args.get("season", "")
        if season:
            args.append(season)
        result = mlb_data.cmd_stats(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/latest-outing")
def api_mlb_latest_outing():
    try:
        player_name = request.args.get("player_name", "")
        player_id = request.args.get("player_id", "")
        requested_date = request.args.get("date", "")
        if requested_date:
            try:
                date.fromisoformat(requested_date)
            except ValueError as e:
                return _json_error("Invalid date. Expected YYYY-MM-DD.", status=400)
        result = _mlb_latest_outing_payload(
            player_name=player_name, player_id=player_id, requested_date=requested_date
        )
        return jsonify(result)
    except ValueError as e:
        return _json_error(e, status=400)
    except Exception as e:
        return _json_error(e, status=500)


@app.route("/api/mlb/injuries")
def api_mlb_injuries():
    try:
        result = mlb_data.cmd_injuries([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/standings")
def api_mlb_standings():
    try:
        result = mlb_data.cmd_standings([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/draft")
def api_mlb_draft():
    try:
        year = request.args.get("year", "")
        args = [year] if year else []
        result = mlb_data.cmd_draft(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/schedule")
def api_mlb_schedule():
    try:
        args = []
        date_arg = request.args.get("date", "")
        if date_arg:
            args.append(date_arg)
        result = mlb_data.cmd_schedule(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/media-links")
def api_mlb_media_links():
    try:
        game_id = str(request.args.get("game_id", "") or "").strip()
        game_pk = str(request.args.get("game_pk", "") or "").strip()
        requested_date = str(request.args.get("date", "") or "").strip()

        if game_id and not game_pk:
            game_pk = _operator_game_pk_from_game_id(game_id)
        if not game_pk:
            return jsonify({"error": "Missing game_id or game_pk"}), 400

        if not requested_date:
            requested_date = _operator_scoreboard_target_date(request.args).isoformat()

        result = _mlb_media_links_for_game(game_pk, requested_date)
        return jsonify(
            {
                "date": requested_date,
                "game_id": "mlb-" + str(game_pk),
                "game_pk": str(game_pk),
                "media_links": result,
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mlb/weather")
def api_mlb_weather():
    try:
        game_date = request.args.get("date", "")
        args = [game_date] if game_date else []
        result = mlb_data.cmd_weather(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- History (history.py) ---
# TS tools call: /api/league-history, /api/record-book, /api/past-standings, etc.


@app.route("/api/league-history")
def api_league_history():
    try:
        result = history.cmd_league_history([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/record-book")
def api_record_book():
    try:
        result = history.cmd_record_book([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/past-standings")
def api_past_standings():
    try:
        year = request.args.get("year", "")
        if not year:
            return jsonify({"error": "Missing year parameter"}), 400
        result = history.cmd_past_standings([year], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/past-draft")
def api_past_draft():
    try:
        year = request.args.get("year", "")
        if not year:
            return jsonify({"error": "Missing year parameter"}), 400
        args = [year]
        count = request.args.get("count", "")
        if count:
            args.append(count)
        result = history.cmd_past_draft(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/past-teams")
def api_past_teams():
    try:
        year = request.args.get("year", "")
        if not year:
            return jsonify({"error": "Missing year parameter"}), 400
        result = history.cmd_past_teams([year], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/past-trades")
def api_past_trades():
    try:
        year = request.args.get("year", "")
        if not year:
            return jsonify({"error": "Missing year parameter"}), 400
        args = [year]
        count = request.args.get("count", "")
        if count:
            args.append(count)
        result = history.cmd_past_trades(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/past-matchup")
def api_past_matchup():
    try:
        year = request.args.get("year", "")
        week = request.args.get("week", "")
        if not year or not week:
            return jsonify({"error": "Missing year and/or week parameters"}), 400
        result = history.cmd_past_matchup([year, week], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Intel (intel.py) ---


@app.route("/api/intel/player")
def api_intel_player():
    try:
        name = request.args.get("name", "")
        if not name:
            name = request.args.get("player_name", "")
        name = str(name).strip()
        if not name:
            return jsonify({"error": "Missing name or player_name parameter"}), 400
        result = intel.cmd_player_report([name], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/breakouts")
def api_intel_breakouts():
    try:
        pos_type = request.args.get("pos_type", "B")
        count = request.args.get("count", "15")
        result = intel.cmd_breakouts([pos_type, count], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/busts")
def api_intel_busts():
    try:
        pos_type = request.args.get("pos_type", "B")
        count = request.args.get("count", "15")
        result = intel.cmd_busts([pos_type, count], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/reddit")
def api_intel_reddit():
    try:
        result = intel.cmd_reddit_buzz([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/trending")
def api_intel_trending():
    try:
        result = intel.cmd_trending([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/prospects")
def api_intel_prospects():
    try:
        result = intel.cmd_prospect_watch([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/transactions")
def api_intel_transactions():
    try:
        days = request.args.get("days", "7")
        result = intel.cmd_transactions([days], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/batch")
def api_intel_batch():
    try:
        names = request.args.get("names", "")
        if not names:
            return jsonify({"error": "Missing names parameter (comma-separated)"}), 400
        name_list = [n.strip() for n in names.split(",") if n.strip()]
        include_str = request.args.get("include", "statcast")
        include = [s.strip() for s in include_str.split(",") if s.strip()]
        result = intel.batch_intel(name_list, include=include)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/statcast-history")
def api_intel_statcast_history():
    try:
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "Missing name parameter"}), 400
        days = request.args.get("days", "30")
        result = intel.cmd_statcast_compare([name, days], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Workflow endpoints (aggregate multiple calls for token efficiency) ---


def _safe_call(fn, args=None):
    """Call a cmd_* function with as_json=True, returning error dict on failure"""
    try:
        return fn(args or [], as_json=True)
    except Exception as e:
        return {"_error": str(e)}


def _safe_lineup_preview(include_intel=False):
    """Request a lightweight lineup preview for workflow aggregates by default."""
    try:
        return season_manager.cmd_lineup_optimize(
            [], as_json=True, include_intel=include_intel
        )
    except Exception as e:
        return {"_error": str(e)}


def _safe_injury_report(include_intel=False):
    """Request a lightweight injury payload for workflow aggregates by default."""
    try:
        return season_manager.cmd_injury_report(
            [], as_json=True, include_intel=include_intel
        )
    except Exception as e:
        return {"_error": str(e)}


def _safe_waiver_analyze(pos_type, count, include_intel=False):
    """Request waiver recommendations without heavy intel/trend enrichment when possible."""
    try:
        return season_manager.cmd_waiver_analyze(
            [pos_type, str(count)], as_json=True, include_intel=include_intel
        )
    except Exception as e:
        return {"_error": str(e)}


def _safe_roster(include_intel=False):
    """Request a lightweight roster payload for workflow aggregates by default."""
    try:
        return yahoo_fantasy.cmd_roster([str(include_intel).lower()], as_json=True)
    except Exception as e:
        return {"_error": str(e)}


def _safe_whats_new(include_intel=False):
    """Request a lightweight digest payload for workflow aggregates by default."""
    try:
        return season_manager.cmd_whats_new(
            [], as_json=True, include_intel=include_intel
        )
    except Exception as e:
        return {"_error": str(e)}


def _synthesize_morning_actions(injury, lineup, whats_new, waiver_b, waiver_p):
    """Build priority-ranked action items from morning briefing data"""
    actions = []

    # Critical: injured players in active slots
    for p in (injury or {}).get("injured_active", []):
        actions.append(
            {
                "priority": 1,
                "type": "injury",
                "message": str(p.get("name", "?"))
                + " ("
                + str(p.get("status", ""))
                + ") injured in active slot - move to IL or bench",
                "player_id": str(p.get("player_id", "")),
            }
        )

    # Lineup: off-day starters or bench with games
    off_day = (lineup or {}).get("active_off_day", [])
    bench_playing = (lineup or {}).get("bench_playing", [])
    if off_day or bench_playing:
        msg = str(len(off_day)) + " starter(s) off today"
        if bench_playing:
            msg += ", " + str(len(bench_playing)) + " bench player(s) have games"
        actions.append(
            {
                "priority": 2,
                "type": "lineup",
                "message": msg + " - run yahoo_auto_lineup",
            }
        )

    # Pending trades need attention
    for t in (whats_new or {}).get("pending_trades", []):
        actions.append(
            {
                "priority": 2,
                "type": "trade",
                "message": "Pending trade from "
                + str(t.get("trader_team_name", "?"))
                + " - review and respond",
                "transaction_key": str(t.get("transaction_key", "")),
            }
        )

    # Waiver opportunities: top picks
    for label, waiver in [("batter", waiver_b), ("pitcher", waiver_p)]:
        recs = (waiver or {}).get("recommendations", [])
        if recs:
            top = recs[0]
            actions.append(
                {
                    "priority": 3,
                    "type": "waiver",
                    "message": "Top "
                    + label
                    + " pickup: "
                    + str(top.get("name", "?"))
                    + " (id:"
                    + str(top.get("pid", "?"))
                    + ") score="
                    + str(top.get("score", "?")),
                    "player_id": str(top.get("pid", "")),
                }
            )

    # Healthy players stuck on IL
    for p in (injury or {}).get("healthy_il", []):
        actions.append(
            {
                "priority": 3,
                "type": "il_activation",
                "message": str(p.get("name", "?"))
                + " on IL with no injury status - may be activatable",
                "player_id": str(p.get("player_id", "")),
            }
        )

    actions.sort(key=lambda a: a.get("priority", 99))
    return actions


@app.route("/api/workflow/morning-briefing")
def workflow_morning_briefing():
    cache_key = ("workflow-morning-briefing", date.today().isoformat())
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        return jsonify(cached)
    try:
        injury = _safe_injury_report(include_intel=False)
        lineup = _safe_lineup_preview(include_intel=False)
        matchup = _safe_call(yahoo_fantasy.cmd_matchup_detail)
        strategy = _safe_call(season_manager.cmd_matchup_strategy)
        whats_new = _safe_whats_new(include_intel=False)
        waiver_b = _safe_waiver_analyze("B", 5, include_intel=False)
        waiver_p = _safe_waiver_analyze("P", 5, include_intel=False)

        action_items = _synthesize_morning_actions(
            injury, lineup, whats_new, waiver_b, waiver_p
        )

        # Include next lineup edit date
        edit_date = None
        try:
            _sc, _gm, _lg = yahoo_fantasy.get_league()
            edit_date = str(_lg.edit_date())
        except Exception:
            pass

        payload = {
            "action_items": action_items,
            "injury": injury,
            "lineup": lineup,
            "matchup": matchup,
            "strategy": strategy,
            "whats_new": whats_new,
            "waiver_batters": waiver_b,
            "waiver_pitchers": waiver_p,
            "edit_date": edit_date,
        }
        _dashboard_cache_set(cache_key, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/league-landscape")
def workflow_league_landscape():
    cache_key = ("workflow-league-landscape", date.today().isoformat())
    cached = _dashboard_cache_get(cache_key, 60)
    if cached is not None:
        return jsonify(cached)
    try:
        standings = _safe_call(yahoo_fantasy.cmd_standings)
        pace = _safe_call(season_manager.cmd_season_pace)
        power = _safe_call(season_manager.cmd_power_rankings)
        pulse = _safe_call(yahoo_fantasy.cmd_league_pulse)
        transactions = _safe_call(yahoo_fantasy.cmd_transactions, ["", "15"])
        trade_finder = _safe_call(season_manager.cmd_trade_finder)
        scoreboard = _safe_call(yahoo_fantasy.cmd_scoreboard)

        payload = {
            "standings": standings,
            "pace": pace,
            "power_rankings": power,
            "league_pulse": pulse,
            "transactions": transactions,
            "trade_finder": trade_finder,
            "scoreboard": scoreboard,
        }
        _dashboard_cache_set(cache_key, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _synthesize_roster_issues(injury, lineup, roster, busts):
    """Build severity-ranked roster issues"""
    issues = []

    # Critical: injured in active slots
    for p in (injury or {}).get("injured_active", []):
        issues.append(
            {
                "severity": "critical",
                "type": "injury",
                "message": str(p.get("name", "?"))
                + " ("
                + str(p.get("status", ""))
                + ") injured in active slot",
                "fix": "Move to IL or bench",
                "player_id": str(p.get("player_id", "")),
            }
        )

    # Warning: healthy players on IL
    for p in (injury or {}).get("healthy_il", []):
        issues.append(
            {
                "severity": "warning",
                "type": "il_waste",
                "message": str(p.get("name", "?")) + " on IL with no injury status",
                "fix": "Activate to free IL slot",
                "player_id": str(p.get("player_id", "")),
            }
        )

    # Warning: off-day starters
    for p in (lineup or {}).get("active_off_day", []):
        issues.append(
            {
                "severity": "warning",
                "type": "off_day",
                "message": str(p.get("name", "?")) + " starting but has no game today",
                "fix": "Bench and start an active player",
            }
        )

    # Info: bust candidates on roster
    roster_names = set()
    for p in (roster or {}).get("players", []):
        roster_names.add(str(p.get("name", "")).lower())
    for b in (busts or {}).get("candidates", []):
        if str(b.get("name", "")).lower() in roster_names:
            issues.append(
                {
                    "severity": "info",
                    "type": "bust_risk",
                    "message": str(b.get("name", "?"))
                    + " is a bust candidate (underperforming Statcast metrics)",
                    "fix": "Consider replacing if better options available",
                }
            )

    return issues


@app.route("/api/workflow/roster-health")
def workflow_roster_health():
    cache_key = ("workflow-roster-health", date.today().isoformat())
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        return jsonify(cached)
    try:
        injury = _safe_injury_report(include_intel=False)
        lineup = _safe_lineup_preview(include_intel=False)
        roster = _safe_roster(include_intel=False)
        busts = _safe_call(intel.cmd_busts, ["B", "20"])

        issues = _synthesize_roster_issues(injury, lineup, roster, busts)

        payload = {
            "issues": issues,
            "injury": injury,
            "lineup": lineup,
            "roster": roster,
            "busts": busts,
        }
        _dashboard_cache_set(cache_key, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _synthesize_waiver_pairs(waiver_b, waiver_p):
    """Pair waiver recommendations with position type labels"""
    pairs = []

    for label, waiver in [("B", waiver_b), ("P", waiver_p)]:
        for rec in (waiver or {}).get("recommendations", [])[:5]:
            pair = {
                "add": {
                    "name": str(rec.get("name", "?")),
                    "player_id": str(rec.get("pid", "")),
                    "positions": str(rec.get("positions", "")),
                    "score": rec.get("score", 0),
                    "percent_owned": rec.get("pct", 0),
                },
                "pos_type": label,
                "weak_categories": [
                    c.get("name", "") for c in (waiver or {}).get("weak_categories", [])
                ],
            }
            pairs.append(pair)

    return pairs


@app.route("/api/workflow/waiver-recommendations")
def workflow_waiver_recommendations():
    count = request.args.get("count", "5")
    cache_key = (
        "workflow-waiver-recommendations",
        date.today().isoformat(),
        str(count),
    )
    cached = _dashboard_cache_get(cache_key, 30)
    if cached is not None:
        return jsonify(cached)
    try:
        cat_check = _safe_call(season_manager.cmd_category_check)
        waiver_b = _safe_waiver_analyze("B", count, include_intel=False)
        waiver_p = _safe_waiver_analyze("P", count, include_intel=False)
        roster = _safe_roster(include_intel=False)

        pairs = _synthesize_waiver_pairs(waiver_b, waiver_p)

        payload = {
            "pairs": pairs,
            "category_check": cat_check,
            "waiver_batters": waiver_b,
            "waiver_pitchers": waiver_p,
            "roster": roster,
        }
        _dashboard_cache_set(cache_key, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/trade-analysis", methods=["POST"])
def workflow_trade_analysis():
    try:
        data = request.get_json(silent=True) or {}
        give_names = data.get("give_names", [])
        get_names = data.get("get_names", [])
        if not give_names or not get_names:
            return jsonify({"error": "Missing give_names and/or get_names arrays"}), 400

        # Resolve player names to IDs via value lookup
        give_players = []
        get_players = []
        give_ids = []
        get_ids = []

        # Fetch roster once for give-player ID lookups
        roster = _safe_call(yahoo_fantasy.cmd_roster)
        roster_players = (roster or {}).get("players", [])

        for name in give_names:
            try:
                val = valuations.cmd_value([name], as_json=True)
                players = val.get("players", [])
                if players:
                    p = players[0]
                    give_players.append(p)
                    for rp in roster_players:
                        if (
                            str(rp.get("name", "")).lower()
                            == str(p.get("name", "")).lower()
                        ):
                            give_ids.append(str(rp.get("player_id", "")))
                            break
            except Exception:
                give_players.append({"name": name, "_error": "not found"})

        for name in get_names:
            try:
                val = valuations.cmd_value([name], as_json=True)
                players = val.get("players", [])
                if players:
                    p = players[0]
                    get_players.append(p)
                    # Try search for player ID
                    search = _safe_call(yahoo_fantasy.cmd_search, [name])
                    for rp in (search or {}).get("results", []):
                        if (
                            str(rp.get("name", "")).lower()
                            == str(p.get("name", "")).lower()
                        ):
                            get_ids.append(str(rp.get("player_id", "")))
                            break
            except Exception:
                get_players.append({"name": name, "_error": "not found"})

        # Run trade eval if we have IDs
        trade_eval = None
        if give_ids and get_ids:
            try:
                trade_eval = season_manager.cmd_trade_eval(
                    [",".join(give_ids), ",".join(get_ids)], as_json=True
                )
            except Exception as e:
                trade_eval = {"_error": str(e)}

        # Get intel for each player
        all_names = give_names + get_names
        intel_data = {}
        for name in all_names:
            try:
                intel_data[name] = intel.cmd_player_report([name], as_json=True)
            except Exception:
                intel_data[name] = {"_error": "unavailable"}

        return jsonify(
            {
                "give_players": give_players,
                "get_players": get_players,
                "give_ids": give_ids,
                "get_ids": get_ids,
                "trade_eval": trade_eval,
                "intel": intel_data,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/game-day-manager")
def workflow_game_day_manager():
    try:
        data = season_manager.cmd_game_day_manager([], as_json=True)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/waiver-deadline-prep")
def workflow_waiver_deadline_prep():
    try:
        count = request.args.get("count", "5")
        data = season_manager.cmd_waiver_deadline_prep([count], as_json=True)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/trade-pipeline")
def workflow_trade_pipeline():
    try:
        data = season_manager.cmd_trade_pipeline([], as_json=True)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/weekly-digest")
def workflow_weekly_digest():
    try:
        data = season_manager.cmd_weekly_digest([], as_json=True)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/season-checkpoint")
def workflow_season_checkpoint():
    try:
        data = season_manager.cmd_season_checkpoint([], as_json=True)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- News (RotoWire RSS) ---


@app.route("/api/news")
def api_news():
    try:
        limit = request.args.get("limit", "20")
        result = news.cmd_news([limit], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news/player")
def api_news_player():
    try:
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "Missing name parameter"}), 400
        result = news.cmd_news_player([name], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news/feed")
def api_news_feed():
    try:
        sources = request.args.get("sources", None)
        player = request.args.get("player", None)
        limit = int(request.args.get("limit", "30"))
        entries = news.fetch_aggregated_news(
            sources=sources, player=player, limit=limit
        )
        source_set = sorted(
            set(e.get("source", "") for e in entries if e.get("source"))
        )
        return jsonify(
            {"entries": entries, "sources": source_set, "count": len(entries)}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news/sources")
def api_news_sources():
    try:
        result = news.cmd_news_sources([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Strategy / Advanced Analysis ---


@app.route("/api/probable-pitchers")
def api_probable_pitchers():
    try:
        days = request.args.get("days", "7")
        cache_key = ("probable-pitchers", str(days))
        cached = _dashboard_cache_get(cache_key, 300)
        if cached is not None:
            return jsonify(cached)
        result = season_manager.fetch_probable_pitchers(int(days))
        payload = {"pitchers": result}
        _dashboard_cache_set(cache_key, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedule-analysis")
def api_schedule_analysis():
    try:
        team_name = request.args.get("team", "")
        days = request.args.get("days", "14")
        if not team_name:
            return jsonify({"error": "Missing team parameter"}), 400
        result = season_manager.analyze_schedule_density(team_name, int(days))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/category-impact", methods=["POST"])
def api_category_impact():
    try:
        data = request.get_json(silent=True) or {}
        add_players = data.get("add_players", [])
        drop_players = data.get("drop_players", [])
        result = valuations.project_category_impact(add_players, drop_players)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/regression-candidates")
def api_regression_candidates():
    try:
        result = intel.detect_regression_candidates()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/player-tier")
def api_player_tier():
    try:
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "Missing name parameter"}), 400
        result = valuations.get_player_zscore(name)
        if result is None:
            return jsonify({"error": "Player not found: " + name}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cache-stats", methods=["GET"])
def api_cache_stats():
    try:
        from intel import _cache_manager

        return jsonify(_cache_manager.stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cache-clear", methods=["POST"])
def api_cache_clear():
    try:
        from intel import _cache_manager

        data = request.get_json(silent=True) or {}
        key = data.get("key")
        _cache_manager.clear(key)
        return jsonify({"cleared": key or "all"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trash-talk")
def api_trash_talk():
    try:
        intensity = request.args.get("intensity", "competitive")
        result = season_manager.cmd_trash_talk([intensity], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rival-history")
def api_rival_history():
    try:
        opponent = request.args.get("opponent", "")
        args = [opponent] if opponent else []
        result = season_manager.cmd_rival_history(args, as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/achievements")
def api_achievements():
    try:
        result = season_manager.cmd_achievements([], as_json=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", "8766"))
    app.run(host="0.0.0.0", port=port, threaded=True)
