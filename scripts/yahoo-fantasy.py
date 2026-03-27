#!/usr/bin/env python3
"""Yahoo Fantasy Baseball CLI for OpenClaw - Docker Version"""

import sys
import json
import os
import time
import datetime
import importlib
import yahoo_fantasy_api as yfa
from yahoo_oauth import OAuth2
from mlb_id_cache import get_mlb_id
from shared import (
    get_connection, get_league, get_league_context, get_team_key,
    get_league_settings,
    OAUTH_FILE, LEAGUE_ID, TEAM_ID, GAME_KEY, DATA_DIR,
    enrich_with_intel, enrich_with_trends, mlb_fetch,
)

_AVAILABLE_PLAYERS_CACHE = {}
_AVAILABLE_PLAYERS_CACHE_TTL = int(os.environ.get("AVAILABLE_PLAYERS_CACHE_TTL_SECONDS", "90"))
_AVAILABLE_PLAYERS_SNAPSHOT_TTL = int(os.environ.get("AVAILABLE_PLAYERS_SNAPSHOT_TTL_SECONDS", "300"))
_MLB_TEAM_ABBR_CACHE = {}


def _player_name(player):
    """Extract a stable display name from Yahoo player payloads."""
    if not isinstance(player, dict):
        return "Unknown"
    name = player.get("name", "Unknown")
    if isinstance(name, dict):
        full = name.get("full", "").strip()
        if full:
            return full
        first = name.get("first", "").strip()
        last = name.get("last", "").strip()
        combined = (first + " " + last).strip()
        return combined or "Unknown"
    return str(name or "Unknown")


def _selected_position(player):
    """Yahoo may return selected_position as a string or nested dict."""
    if not isinstance(player, dict):
        return "?"
    raw = player.get("selected_position", "?")
    if isinstance(raw, dict):
        return str(raw.get("position", "?") or "?")
    if isinstance(raw, str):
        return raw or "?"
    return str(raw or "?")


def _eligible_positions(player):
    """Normalize eligible positions to a string list."""
    if not isinstance(player, dict):
        return []
    raw = player.get("eligible_positions", [])
    if isinstance(raw, list):
        return [str(pos) for pos in raw if str(pos)]
    if isinstance(raw, str):
        return [raw] if raw else []
    return []


def _player_team_abbr(player):
    """Normalize the player's MLB team abbreviation from Yahoo payloads."""
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


def _mlb_team_abbr_lookup():
    cached = _MLB_TEAM_ABBR_CACHE.get("teams")
    now = time.time()
    if cached and now - cached[1] <= 3600:
        return cached[0]

    lookup = {}
    try:
        payload = mlb_fetch("/teams?sportId=1")
    except Exception:
        payload = {}

    for team in payload.get("teams", []) if isinstance(payload, dict) else []:
        abbr = str(team.get("abbreviation", "") or "").strip().upper()
        if not abbr:
            continue
        team_id = str(team.get("id", "") or "").strip()
        team_name = str(team.get("name", "") or "").strip().lower()
        if team_id:
            lookup[team_id] = abbr
        if team_name:
            lookup[team_name] = abbr

    _MLB_TEAM_ABBR_CACHE["teams"] = (lookup, now)
    return lookup


def _fill_missing_team_abbr(players):
    missing_ids = []
    for player in players or []:
        if not isinstance(player, dict):
            continue
        team_abbr = str(player.get("team_abbr", "") or player.get("team", "") or "").strip().upper()
        if team_abbr:
            player["team_abbr"] = team_abbr
            if not player.get("team"):
                player["team"] = team_abbr
            continue
        mlb_id = player.get("mlb_id")
        if mlb_id:
            missing_ids.append(str(mlb_id))

    if not missing_ids:
        return

    try:
        payload = mlb_fetch("/people?personIds=" + ",".join(sorted(set(missing_ids))) + "&hydrate=currentTeam")
    except Exception:
        return

    team_lookup = _mlb_team_abbr_lookup()
    resolved_by_id = {}
    for person in payload.get("people", []) if isinstance(payload, dict) else []:
        person_id = str(person.get("id", "") or "").strip()
        current_team = person.get("currentTeam", {}) if isinstance(person, dict) else {}
        team_id = str(current_team.get("id", "") or "").strip()
        team_name = str(current_team.get("name", "") or "").strip().lower()
        resolved = team_lookup.get(team_id, "") or team_lookup.get(team_name, "")
        if person_id and resolved:
            resolved_by_id[person_id] = resolved

    for player in players or []:
        if not isinstance(player, dict):
            continue
        if str(player.get("team_abbr", "") or "").strip():
            continue
        resolved = resolved_by_id.get(str(player.get("mlb_id", "") or "").strip(), "")
        if resolved:
            player["team_abbr"] = resolved
            if not player.get("team"):
                player["team"] = resolved


def _truthy(value):
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _infer_pos_type(eligible_positions):
    tokens = [str(pos or "").strip().upper() for pos in (eligible_positions or [])]
    pitcher_pos = {"SP", "RP", "P"}
    batter_pos = {"C", "1B", "2B", "3B", "SS", "OF", "LF", "CF", "RF", "DH", "UTIL"}
    pitcher_count = sum(1 for p in tokens if p in pitcher_pos)
    batter_count = sum(1 for p in tokens if p in batter_pos)
    if pitcher_count > batter_count:
        return "P"
    return "B"


def _matches_pos_type(player, pos_type):
    pos_type = str(pos_type or "B").upper()
    if pos_type == "ALL":
        return True
    return _infer_pos_type(_eligible_positions(player)) == pos_type


def _normalize_available_player(player, availability_type):
    name = _player_name(player)
    positions = _eligible_positions(player)
    team_abbr = str(_player_team_abbr(player) or "").strip().upper()
    return {
        "name": name,
        "player_id": str(player.get("player_id", "")),
        "positions": positions,
        "eligible_positions": positions,
        "percent_owned": player.get("percent_owned", 0),
        "status": player.get("status", ""),
        "team": team_abbr,
        "team_abbr": team_abbr,
        "mlb_id": get_mlb_id(name),
        "availability_type": availability_type,
    }


def _available_players_snapshot_path(pos_type):
    return os.path.join(DATA_DIR, "available-players-" + str(pos_type or "B").upper() + ".json")


def _read_available_players_snapshot(pos_type):
    path = _available_players_snapshot_path(pos_type)
    if not os.path.exists(path):
        return None
    try:
        age_seconds = time.time() - os.path.getmtime(path)
        if age_seconds > _AVAILABLE_PLAYERS_SNAPSHOT_TTL:
            return None
        with open(path) as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return payload
    except Exception:
        return None
    return None


def _write_available_players_snapshot(pos_type, players):
    path = _available_players_snapshot_path(pos_type)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(players, handle)
    except Exception:
        pass


def get_available_players(pos_type="B", count=None):
    """Return available players from waivers plus true free agents."""
    pos_type = str(pos_type or "B").upper()
    limit = int(count) if count else None
    now = time.time()

    cached = _AVAILABLE_PLAYERS_CACHE.get(pos_type)
    if cached and now - cached[1] <= _AVAILABLE_PLAYERS_CACHE_TTL:
        players = list(cached[0])
        _fill_missing_team_abbr(players)
        return players[:limit] if limit is not None else players

    if LEAGUE_ID:
        snapshot = _read_available_players_snapshot(pos_type)
        if snapshot is not None:
            players = list(snapshot)
            _fill_missing_team_abbr(players)
            _AVAILABLE_PLAYERS_CACHE[pos_type] = (list(players), now)
            return players[:limit] if limit is not None else players

    sc, gm, lg = get_league()

    combined = {}

    def merge_rows(rows, availability_type):
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            if not _matches_pos_type(raw, pos_type):
                continue
            normalized = _normalize_available_player(raw, availability_type)
            key = normalized["player_id"] or normalized["name"].lower()
            if not key:
                continue
            existing = combined.get(key)
            if existing is None:
                combined[key] = normalized
                continue
            if availability_type == "free_agent":
                existing["availability_type"] = "free_agent"
            existing["percent_owned"] = max(existing.get("percent_owned", 0), normalized.get("percent_owned", 0))
            if normalized.get("team"):
                existing["team"] = normalized["team"]
            if normalized.get("status"):
                existing["status"] = normalized["status"]
            if normalized.get("mlb_id"):
                existing["mlb_id"] = normalized["mlb_id"]
            if normalized.get("positions"):
                existing["positions"] = normalized["positions"]
                existing["eligible_positions"] = normalized["eligible_positions"]

    try:
        merge_rows(lg.waivers(), "waiver")
    except Exception:
        pass

    if pos_type == "ALL":
        free_agent_groups = ("B", "P")
    else:
        free_agent_groups = (pos_type,)
    for group in free_agent_groups:
        try:
            merge_rows(lg.free_agents(group), "free_agent")
        except Exception:
            pass

    players = list(combined.values())
    _fill_missing_team_abbr(players)
    players.sort(
        key=lambda p: (
            0 if p.get("availability_type") == "free_agent" else 1,
            -float(p.get("percent_owned", 0) or 0),
            p.get("name", ""),
        )
    )
    _AVAILABLE_PLAYERS_CACHE[pos_type] = (list(players), now)
    if LEAGUE_ID:
        _write_available_players_snapshot(pos_type, players)
    if limit is not None:
        players = players[:limit]
    return players


def cmd_roster(args, as_json=False):
    """Show current roster"""
    include_intel = _truthy(args[0]) if args else True
    sc, gm, lg, team = get_league_context()
    roster = team.roster()

    if not roster:
        if as_json:
            return {"players": []}
        print("Roster is empty (predraft)")
        return

    if as_json:
        players = []
        for p in roster:
            team_abbr = _player_team_abbr(p)
            positions = _eligible_positions(p)
            players.append(
                {
                    "name": _player_name(p),
                    "player_id": p.get("player_id", ""),
                    "position": _selected_position(p),
                    "eligible_positions": positions,
                    "positions": positions,
                    "status": p.get("status", ""),
                    "team": team_abbr,
                    "team_abbr": str(team_abbr or "").upper(),
                    "mlb_team": team_abbr,
                    "mlb_id": get_mlb_id(_player_name(p)),
                }
            )
        _fill_missing_team_abbr(players)
        if include_intel:
            enrich_with_intel(players)
        return {"players": players}

    print("Current Roster:")
    for p in roster:
        pos = _selected_position(p)
        name = _player_name(p)
        status = p.get("status", "")
        elig = ",".join(_eligible_positions(p))
        line = "  " + pos.ljust(4) + " " + name.ljust(25) + " " + elig
        if status:
            line += " [" + status + "]"
        print(line)


def cmd_free_agents(args, as_json=False):
    """List available players (waivers + true free agents)."""
    pos_type = args[0] if args else "B"
    count = int(args[1]) if len(args) > 1 else 20
    include_intel = _truthy(args[2]) if len(args) > 2 else False
    fa = get_available_players(pos_type, count)

    if as_json:
        players = [dict(p) for p in fa]
        if include_intel:
            enrich_with_intel(players)
            enrich_with_trends(players)
        return {"pos_type": pos_type, "count": count, "players": players}

    label = "Batters" if pos_type == "B" else ("Pitchers" if pos_type == "P" else "Players")
    print("Top " + str(count) + " Available " + label + ":")
    for p in fa:
        name = p.get("name", "Unknown")
        positions = ",".join(p.get("positions", ["?"]))
        pct = p.get("percent_owned", 0)
        pid = p.get("player_id", "?")
        status = p.get("status", "")
        source = p.get("availability_type", "")
        if status:
            status = " [" + status + "]"
        line = (
            "  "
            + name.ljust(25)
            + " "
            + positions.ljust(12)
            + " "
            + str(pct).rjust(3)
            + "% owned  (id:"
            + str(pid)
            + ")"
            + status
        )
        if source:
            line += " {" + source + "}"
        print(line)


def cmd_standings(args, as_json=False):
    """Show league standings"""
    sc, gm, lg = get_league()
    standings = lg.standings()

    if as_json:
        # Fetch teams for logo/avatar data
        team_meta = {}
        try:
            teams = lg.teams()
            for tk, td in teams.items():
                tname = td.get("name", "")
                logo_url, mgr_image = _extract_team_meta(td)
                team_meta[tname] = {"team_logo": logo_url, "manager_image": mgr_image}
        except Exception:
            pass
        result = []
        for i, team in enumerate(standings, 1):
            name = team.get("name", "Unknown")
            meta = team_meta.get(name, {})
            result.append(
                {
                    "rank": i,
                    "name": name,
                    "wins": team.get("outcome_totals", {}).get("wins", 0),
                    "losses": team.get("outcome_totals", {}).get("losses", 0),
                    "points_for": team.get("points_for", ""),
                    "team_logo": meta.get("team_logo", ""),
                    "manager_image": meta.get("manager_image", ""),
                }
            )
        return {"standings": result}

    print("League Standings:")
    for i, team in enumerate(standings, 1):
        name = team.get("name", "Unknown")
        wins = team.get("outcome_totals", {}).get("wins", 0)
        losses = team.get("outcome_totals", {}).get("losses", 0)
        pts = team.get("points_for", "")
        line = (
            "  "
            + str(i).rjust(2)
            + ". "
            + name.ljust(30)
            + " "
            + str(wins)
            + "-"
            + str(losses)
        )
        if pts:
            line += " (" + str(pts) + " pts)"
        print(line)


def cmd_info(args, as_json=False):
    """Show league and team info"""
    sc, gm, lg = get_league()
    my_team_key = get_team_key(lg)
    settings = lg.settings()
    team_name = "Unknown"
    try:
        team = lg.to_team(my_team_key)
        if hasattr(team, "team_data"):
            team_name = team.team_data.get("name", "Unknown")
    except Exception:
        pass
    if team_name == "Unknown":
        try:
            teams = lg.teams()
            for tk, td in teams.items():
                if tk == my_team_key:
                    team_name = td.get("name", "Unknown")
                    break
        except Exception:
            pass

    # Get team details (waiver priority, FAAB, moves)
    team_details = {}
    try:
        team = lg.to_team(my_team_key)
        raw_details = team.details() if hasattr(team, "details") else None
        if raw_details:
            if isinstance(raw_details, list) and len(raw_details) > 0:
                d = raw_details[0] if isinstance(raw_details[0], dict) else {}
            elif isinstance(raw_details, dict):
                d = raw_details
            else:
                d = {}
            team_details["waiver_priority"] = d.get("waiver_priority", d.get("priority", None))
            team_details["faab_balance"] = d.get("faab_balance", None)
            team_details["number_of_moves"] = d.get("number_of_moves", None)
            team_details["number_of_trades"] = d.get("number_of_trades", None)
            team_details["clinched_playoffs"] = d.get("clinched_playoffs", None)
    except Exception as e:
        print("Warning: could not fetch team details: " + str(e))

    # Get roster positions from league settings
    roster_positions = []
    try:
        raw_positions = lg.positions() if hasattr(lg, "positions") else None
        if raw_positions:
            for rp in raw_positions:
                pos_name = rp.get("position", "")
                count = int(rp.get("count", 1))
                pos_type = rp.get("position_type", "")
                roster_positions.append({
                    "position": pos_name,
                    "count": count,
                    "position_type": pos_type,
                })
    except Exception as e:
        print("Warning: could not fetch roster positions: " + str(e))

    # Waiver type detection
    league_settings = get_league_settings()
    waiver_type = league_settings.get("waiver_type", "unknown")
    scoring_type = league_settings.get("scoring_type", settings.get("scoring_type", ""))

    if as_json:
        result = {
            "name": settings.get("name", "Unknown"),
            "draft_status": settings.get("draft_status", "unknown"),
            "season": settings.get("season", "?"),
            "start_date": settings.get("start_date", "?"),
            "end_date": settings.get("end_date", "?"),
            "current_week": lg.current_week(),
            "num_teams": settings.get("num_teams", "?"),
            "playoff_teams": settings.get("num_playoff_teams", "?"),
            "max_weekly_adds": settings.get("max_weekly_adds", "?"),
            "team_name": team_name,
            "team_id": my_team_key,
            "waiver_type": waiver_type,
            "scoring_type": scoring_type,
        }
        if roster_positions:
            result["roster_positions"] = roster_positions
        for k, v in team_details.items():
            if v is not None:
                result[k] = v
        return result

    print("League Info:")
    print("  Name: " + settings.get("name", "Unknown"))
    print("  Draft Status: " + settings.get("draft_status", "unknown"))
    print("  Season: " + settings.get("season", "?"))
    print("  Start: " + settings.get("start_date", "?"))
    print("  End: " + settings.get("end_date", "?"))
    print("  Current Week: " + str(lg.current_week()))
    print("  Teams: " + str(settings.get("num_teams", "?")))
    print("  Playoff Teams: " + str(settings.get("num_playoff_teams", "?")))
    print("  Max Weekly Adds: " + str(settings.get("max_weekly_adds", "?")))
    print("  Waiver Type: " + waiver_type)
    print("  Scoring Type: " + scoring_type)
    print("  Your Team: " + team_name + " (" + my_team_key + ")")
    if roster_positions:
        slots = []
        for rp in roster_positions:
            pos = rp.get("position", "?")
            cnt = rp.get("count", 1)
            if cnt > 1:
                slots.append(pos + "x" + str(cnt))
            else:
                slots.append(pos)
        print("  Roster Slots: " + ", ".join(slots))
    if team_details.get("waiver_priority") is not None:
        print("  Waiver Priority: " + str(team_details.get("waiver_priority")))
    if team_details.get("faab_balance") is not None:
        print("  FAAB Balance: $" + str(team_details.get("faab_balance")))
    if team_details.get("number_of_moves") is not None:
        print("  Moves Made: " + str(team_details.get("number_of_moves")))
    if team_details.get("number_of_trades") is not None:
        print("  Trades Made: " + str(team_details.get("number_of_trades")))


def cmd_search(args, as_json=False):
    """Search for a player by name"""
    if not args:
        if as_json:
            return {"query": "", "results": []}
        print("Usage: search PLAYER_NAME")
        return
    name = " ".join(args)
    sc, gm, lg = get_league()

    results = []
    for pos_type in ["B", "P"]:
        fa = lg.free_agents(pos_type)
        for p in fa:
            if name.lower() in p.get("name", "").lower():
                results.append(p)

    if as_json:
        players = []
        for p in results[:10]:
            players.append(
                {
                    "name": p.get("name", "Unknown"),
                    "player_id": p.get("player_id", "?"),
                    "positions": p.get("eligible_positions", ["?"]),
                    "percent_owned": p.get("percent_owned", 0),
                    "mlb_id": get_mlb_id(p.get("name", "")),
                }
            )
        enrich_with_intel(players)
        return {"query": name, "results": players}

    if not results:
        print("No free agents found matching: " + name)
        return

    print("Free agents matching: " + name)
    for p in results[:10]:
        pname = p.get("name", "Unknown")
        positions = ",".join(p.get("eligible_positions", ["?"]))
        pct = p.get("percent_owned", 0)
        pid = p.get("player_id", "?")
        line = (
            "  "
            + pname.ljust(25)
            + " "
            + positions.ljust(12)
            + " "
            + str(pct).rjust(3)
            + "% owned  (id:"
            + str(pid)
            + ")"
        )
        print(line)


from yahoo_browser import (
    is_scope_error as _is_scope_error,
    write_method as _write_method,
)


def cmd_add(args, as_json=False):
    """Add a player by player_id"""
    if not args:
        if as_json:
            return {"success": False, "player_key": "", "message": "Missing player_id"}
        print("Usage: add PLAYER_ID")
        return
    player_id = args[0]
    player_key = GAME_KEY + ".p." + str(player_id)
    method = _write_method()

    # Try API first (unless browser-only mode)
    if method != "browser":
        try:
            sc, gm, lg, team = get_league_context()
            team.add_player(player_key)
            if as_json:
                return {
                    "success": True,
                    "player_key": player_key,
                    "message": "Added player " + player_key,
                }
            print("Added player " + player_key)
            return
        except Exception as e:
            if method == "api" or not _is_scope_error(e):
                if as_json:
                    return {
                        "success": False,
                        "player_key": player_key,
                        "message": "Error adding player: " + str(e),
                    }
                print("Error adding player: " + str(e))
                return
            # Fall through to browser

    # Browser fallback
    try:
        from yahoo_browser import add_player

        result = add_player(player_id)
        if as_json:
            result["player_key"] = player_key
            return result
        if result.get("success"):
            print(result.get("message", "Added player " + player_key + " via browser"))
        else:
            print(result.get("message", "Browser add failed"))
    except Exception as e:
        if as_json:
            return {
                "success": False,
                "player_key": player_key,
                "message": "Browser fallback error: " + str(e),
            }
        print("Browser fallback error: " + str(e))


def cmd_drop(args, as_json=False):
    """Drop a player by player_id"""
    if not args:
        if as_json:
            return {"success": False, "player_key": "", "message": "Missing player_id"}
        print("Usage: drop PLAYER_ID")
        return
    player_id = args[0]
    player_key = GAME_KEY + ".p." + str(player_id)
    method = _write_method()

    if method != "browser":
        try:
            sc, gm, lg, team = get_league_context()
            team.drop_player(player_key)
            if as_json:
                return {
                    "success": True,
                    "player_key": player_key,
                    "message": "Dropped player " + player_key,
                }
            print("Dropped player " + player_key)
            return
        except Exception as e:
            if method == "api" or not _is_scope_error(e):
                if as_json:
                    return {
                        "success": False,
                        "player_key": player_key,
                        "message": "Error dropping player: " + str(e),
                    }
                print("Error dropping player: " + str(e))
                return

    try:
        from yahoo_browser import drop_player

        result = drop_player(player_id)
        if as_json:
            result["player_key"] = player_key
            return result
        if result.get("success"):
            print(
                result.get("message", "Dropped player " + player_key + " via browser")
            )
        else:
            print(result.get("message", "Browser drop failed"))
    except Exception as e:
        if as_json:
            return {
                "success": False,
                "player_key": player_key,
                "message": "Browser fallback error: " + str(e),
            }
        print("Browser fallback error: " + str(e))


def _extract_team_name(team_data):
    """Extract team name from Yahoo's nested team structure"""
    if isinstance(team_data, dict):
        team_info = team_data.get("team", [])
        if isinstance(team_info, list) and len(team_info) > 0:
            for item in team_info[0] if isinstance(team_info[0], list) else team_info:
                if isinstance(item, dict) and "name" in item:
                    return item["name"]
    return "?"


def _extract_team_key(team_data):
    """Extract team key from Yahoo's nested team structure"""
    if isinstance(team_data, dict):
        team_info = team_data.get("team", [])
        if isinstance(team_info, list) and len(team_info) > 0:
            for item in team_info[0] if isinstance(team_info[0], list) else team_info:
                if isinstance(item, dict) and "team_key" in item:
                    return item.get("team_key", "")
    return ""


def _extract_team_meta(team_data):
    """Extract team_logo URL and manager image from lg.teams() entry"""
    logos = team_data.get("team_logos", [])
    logo_url = ""
    if logos:
        logo_url = logos[0].get("team_logo", {}).get("url", "")
    mgr_image = ""
    managers = team_data.get("managers", [])
    if managers:
        m = managers[0].get("manager", managers[0])
        mgr_image = m.get("image_url", "")
    return logo_url, mgr_image


def cmd_matchups(args, as_json=False):
    """Show weekly H2H matchup preview and scores"""
    sc, gm, lg = get_league()

    try:
        if args:
            week = int(args[0])
            raw = lg.matchups(week=week)
        else:
            raw = lg.matchups()
    except Exception as e:
        if as_json:
            return {"error": "Error fetching matchups: " + str(e)}
        print("Error fetching matchups: " + str(e))
        return

    if not raw:
        if as_json:
            return {"week": "", "matchups": []}
        print("No matchups available")
        return

    week_label = args[0] if args else "current"

    # Parse Yahoo's nested format: fantasy_content -> league[1] -> scoreboard -> 0 -> matchups
    try:
        league_data = raw.get("fantasy_content", {}).get("league", [])
        if len(league_data) < 2:
            if as_json:
                return {"week": week_label, "matchups": []}
            print("No matchup data in response")
            return
        sb = league_data[1].get("scoreboard", {})
        matchup_block = sb.get("0", {}).get("matchups", {})
        count = int(matchup_block.get("count", 0))

        # Fetch team logos
        team_meta = {}
        try:
            all_teams = lg.teams()
            for tk, td in all_teams.items():
                tname = td.get("name", "")
                logo_url, mgr_image = _extract_team_meta(td)
                team_meta[tname] = {"team_logo": logo_url, "manager_image": mgr_image}
        except Exception:
            pass

        matchup_list = []
        for i in range(count):
            matchup = matchup_block.get(str(i), {}).get("matchup", {})
            teams_data = matchup.get("0", {}).get("teams", {})
            team1 = teams_data.get("0", {})
            team2 = teams_data.get("1", {})
            name1 = _extract_team_name(team1)
            name2 = _extract_team_name(team2)
            status = matchup.get("status", "")
            m1_meta = team_meta.get(name1, {})
            m2_meta = team_meta.get(name2, {})
            matchup_list.append(
                {
                    "team1": name1,
                    "team2": name2,
                    "status": status,
                    "team1_logo": m1_meta.get("team_logo", ""),
                    "team2_logo": m2_meta.get("team_logo", ""),
                }
            )

        if as_json:
            return {"week": week_label, "matchups": matchup_list}

        print("Matchups (week " + str(week_label) + "):")
        for m in matchup_list:
            line = "  " + m["team1"].ljust(28) + " vs  " + m["team2"]
            if m["status"]:
                line += "  (" + m["status"] + ")"
            print(line)
    except Exception as e:
        if as_json:
            return {"error": "Error parsing matchups: " + str(e)}
        print("Error parsing matchups: " + str(e))


def cmd_scoreboard(args, as_json=False):
    """Show live scoring overview for current week (uses matchups data)"""
    sc, gm, lg = get_league()

    try:
        raw = lg.matchups()
    except Exception as e:
        if as_json:
            return {"error": "Error fetching scoreboard: " + str(e)}
        print("Error fetching scoreboard: " + str(e))
        return

    if not raw:
        if as_json:
            return {"week": "", "matchups": []}
        print("No scoreboard data available")
        return

    # Parse Yahoo's nested format (scoreboard comes from matchups endpoint)
    try:
        league_data = raw.get("fantasy_content", {}).get("league", [])
        if len(league_data) < 2:
            if as_json:
                return {"week": "?", "matchups": []}
            print("No scoreboard data in response")
            return
        sb = league_data[1].get("scoreboard", {})
        week = sb.get("week", "?")

        matchup_block = sb.get("0", {}).get("matchups", {})
        count = int(matchup_block.get("count", 0))

        # Fetch team logos
        team_meta = {}
        try:
            all_teams = lg.teams()
            for tk, td in all_teams.items():
                tname = td.get("name", "")
                logo_url, mgr_image = _extract_team_meta(td)
                team_meta[tname] = {"team_logo": logo_url, "manager_image": mgr_image}
        except Exception:
            pass

        matchup_list = []
        for i in range(count):
            matchup = matchup_block.get(str(i), {}).get("matchup", {})
            teams_data = matchup.get("0", {}).get("teams", {})
            team1 = teams_data.get("0", {})
            team2 = teams_data.get("1", {})
            name1 = _extract_team_name(team1)
            name2 = _extract_team_name(team2)

            # Extract win/loss/tie counts from stat_winners
            stat_winners = matchup.get("stat_winners", [])
            wins1 = 0
            wins2 = 0
            ties = 0
            for sw in stat_winners:
                w = sw.get("stat_winner", {})
                if w.get("is_tied"):
                    ties += 1
                elif w.get("winner_team_key", ""):
                    # Count wins per team
                    wins1 += 1  # simplified pre-season

            status = matchup.get("status", "")
            m1_meta = team_meta.get(name1, {})
            m2_meta = team_meta.get(name2, {})
            matchup_list.append(
                {
                    "team1": name1,
                    "team2": name2,
                    "status": status,
                    "team1_logo": m1_meta.get("team_logo", ""),
                    "team2_logo": m2_meta.get("team_logo", ""),
                }
            )

        if as_json:
            return {"week": week, "matchups": matchup_list}

        print("Scoreboard - Week " + str(week) + ":")
        print("")
        for m in matchup_list:
            line = (
                "  "
                + m["team1"].ljust(28)
                + " vs  "
                + m["team2"].ljust(28)
                + m["status"]
            )
            print(line)
    except Exception as e:
        if as_json:
            return {"error": "Error parsing scoreboard: " + str(e)}
        print("Error parsing scoreboard: " + str(e))


def cmd_matchup_detail(args, as_json=False):
    """Show detailed H2H matchup with per-category comparison"""
    sc, gm, lg = get_league()

    try:
        raw = lg.matchups()
    except Exception as e:
        if as_json:
            return {"error": "Error fetching matchup detail: " + str(e)}
        print("Error fetching matchup detail: " + str(e))
        return

    if not raw:
        if as_json:
            return {"error": "No matchup data available"}
        print("No matchup data available")
        return

    try:
        league_data = raw.get("fantasy_content", {}).get("league", [])
        if len(league_data) < 2:
            if as_json:
                return {"error": "No matchup data in response"}
            print("No matchup data in response")
            return

        sb = league_data[1].get("scoreboard", {})
        week = sb.get("week", "?")
        matchup_block = sb.get("0", {}).get("matchups", {})
        count = int(matchup_block.get("count", 0))

        # Also fetch stat categories for category names
        stat_cats = lg.stat_categories()
        stat_id_to_name = {}
        for cat in stat_cats:
            sid = str(cat.get("stat_id", ""))
            display = cat.get("display_name", cat.get("name", "Stat " + sid))
            stat_id_to_name[sid] = display

        # Fetch team logos
        team_meta = {}
        try:
            all_teams = lg.teams()
            for tk, td in all_teams.items():
                tname = td.get("name", "")
                logo_url, mgr_image = _extract_team_meta(td)
                team_meta[tname] = {"team_logo": logo_url, "manager_image": mgr_image}
        except Exception:
            pass

        # Find user's matchup
        for i in range(count):
            matchup = matchup_block.get(str(i), {}).get("matchup", {})
            teams_data = matchup.get("0", {}).get("teams", {})
            team1_data = teams_data.get("0", {})
            team2_data = teams_data.get("1", {})
            name1 = _extract_team_name(team1_data)
            name2 = _extract_team_name(team2_data)
            key1 = _extract_team_key(team1_data)
            key2 = _extract_team_key(team2_data)

            # Check if this is our matchup
            if TEAM_ID not in key1 and TEAM_ID not in key2:
                continue

            # Found our matchup - determine which team is ours
            if TEAM_ID in key1:
                my_data = team1_data
                opp_data = team2_data
                my_name = name1
                opp_name = name2
            else:
                my_data = team2_data
                opp_data = team1_data
                my_name = name2
                opp_name = name1

            # Extract team stats - Yahoo nests stats in team -> team_stats -> stats
            def _extract_team_stats(tdata):
                stats = {}
                team_info = tdata.get("team", [])
                if isinstance(team_info, list):
                    for block in team_info:
                        if isinstance(block, dict) and "team_stats" in block:
                            raw_stats = block.get("team_stats", {}).get("stats", [])
                            for s in raw_stats:
                                stat = s.get("stat", {})
                                sid = str(stat.get("stat_id", ""))
                                val = stat.get("value", "0")
                                stats[sid] = val
                return stats

            my_stats = _extract_team_stats(my_data)
            opp_stats = _extract_team_stats(opp_data)

            # Extract stat_winners for per-category results
            stat_winners = matchup.get("stat_winners", [])
            cat_results = {}
            my_key = _extract_team_key(my_data)
            for sw in stat_winners:
                w = sw.get("stat_winner", {})
                sid = str(w.get("stat_id", ""))
                if w.get("is_tied"):
                    cat_results[sid] = "tie"
                else:
                    winner_key = w.get("winner_team_key", "")
                    if winner_key == my_key:
                        cat_results[sid] = "win"
                    else:
                        cat_results[sid] = "loss"

            # Build categories list
            categories = []
            wins = 0
            losses = 0
            ties = 0

            for sid in sorted(
                cat_results.keys(), key=lambda x: int(x) if x.isdigit() else 0
            ):
                cat_name = stat_id_to_name.get(sid, "Stat " + sid)
                my_val = my_stats.get(sid, "-")
                opp_val = opp_stats.get(sid, "-")
                result = cat_results.get(sid, "tie")
                if result == "win":
                    wins += 1
                elif result == "loss":
                    losses += 1
                else:
                    ties += 1
                categories.append(
                    {
                        "name": cat_name,
                        "my_value": str(my_val),
                        "opp_value": str(opp_val),
                        "result": result,
                    }
                )

            my_meta = team_meta.get(my_name, {})
            opp_meta = team_meta.get(opp_name, {})
            result_data = {
                "week": week,
                "my_team": my_name,
                "opponent": opp_name,
                "my_team_logo": my_meta.get("team_logo", ""),
                "my_manager_image": my_meta.get("manager_image", ""),
                "opp_team_logo": opp_meta.get("team_logo", ""),
                "opp_manager_image": opp_meta.get("manager_image", ""),
                "score": {"wins": wins, "losses": losses, "ties": ties},
                "categories": categories,
            }

            if as_json:
                return result_data

            print("Week " + str(week) + " Matchup: " + my_name + " vs " + opp_name)
            print("Score: " + str(wins) + "-" + str(losses) + "-" + str(ties))
            for cat in categories:
                marker = (
                    "W"
                    if cat["result"] == "win"
                    else ("L" if cat["result"] == "loss" else "T")
                )
                print(
                    "  ["
                    + marker
                    + "] "
                    + cat["name"].ljust(10)
                    + " "
                    + cat["my_value"].rjust(8)
                    + " vs "
                    + cat["opp_value"].rjust(8)
                )
            return

        # No matchup found
        if as_json:
            return {"error": "Could not find your matchup"}
        print("Could not find your matchup")
    except Exception as e:
        if as_json:
            return {"error": "Error parsing matchup detail: " + str(e)}
        print("Error parsing matchup detail: " + str(e))


def cmd_transactions(args, as_json=False):
    """Show recent league transaction activity"""
    sc, gm, lg = get_league()

    trans_type = args[0] if args else None
    count = int(args[1]) if len(args) > 1 else 25

    try:
        if trans_type:
            transactions = lg.transactions(trans_type, count)
        else:
            transactions = []
            for t in ["add", "drop", "trade"]:
                try:
                    results = lg.transactions(t, 10)
                    if results:
                        transactions.extend(results)
                except Exception:
                    pass
    except Exception as e:
        if as_json:
            return {"error": "Error fetching transactions: " + str(e)}
        print("Error fetching transactions: " + str(e))
        return

    label = trans_type if trans_type else "all"

    if as_json:
        trans_list = []
        for t in transactions:
            if isinstance(t, dict):
                trans_list.append(
                    {
                        "type": t.get("type", "?"),
                        "player": t.get("player", t.get("name", "Unknown")),
                        "team": t.get("team", ""),
                    }
                )
            else:
                trans_list.append({"raw": str(t)})
        return {"type": label, "transactions": trans_list}

    if not transactions:
        print("No recent transactions found")
        return

    print("Recent transactions (" + label + "):")
    for t in transactions:
        if isinstance(t, dict):
            ttype = t.get("type", "?")
            player = t.get("player", t.get("name", "Unknown"))
            team = t.get("team", "")
            line = "  " + str(ttype).ljust(8) + " " + str(player).ljust(25)
            if team:
                line += " -> " + str(team)
            print(line)
        else:
            print("  " + str(t))


def cmd_stat_categories(args, as_json=False):
    """Show league scoring categories"""
    sc, gm, lg = get_league()

    try:
        categories = lg.stat_categories()
    except Exception as e:
        if as_json:
            return {"error": "Error fetching stat categories: " + str(e)}
        print("Error fetching stat categories: " + str(e))
        return

    if not categories:
        if as_json:
            return {"categories": []}
        print("No stat categories found")
        return

    if as_json:
        cat_list = []
        if isinstance(categories, list):
            for cat in categories:
                if isinstance(cat, dict):
                    cat_list.append(
                        {
                            "name": cat.get("display_name", cat.get("name", "?")),
                            "position_type": cat.get("position_type", ""),
                        }
                    )
        elif isinstance(categories, dict):
            for key, val in categories.items():
                cat_list.append({"name": str(key), "position_type": str(val)})
        return {"categories": cat_list}

    print("Stat Categories:")
    if isinstance(categories, list):
        for cat in categories:
            if isinstance(cat, dict):
                name = cat.get("display_name", cat.get("name", "?"))
                pos_type = cat.get("position_type", "")
                label = ""
                if pos_type:
                    label = " (" + pos_type + ")"
                print("  " + str(name) + label)
            else:
                print("  " + str(cat))
    elif isinstance(categories, dict):
        for key, val in categories.items():
            print("  " + str(key) + ": " + str(val))
    else:
        print("  " + str(categories))


def _parse_trend_players(raw_json):
    """Parse Yahoo's nested player response from sort=AR/DR endpoints"""
    players = []
    try:
        fc = raw_json.get("fantasy_content", {})
        game_data = fc.get("game", [])
        if len(game_data) < 2:
            return players
        players_block = game_data[1].get("players", {})
        count = int(players_block.get("count", 0))
        for i in range(count):
            p_data = players_block.get(str(i), {}).get("player", [])
            if not p_data or len(p_data) < 2:
                continue
            # First element is a list of info dicts
            info_list = p_data[0] if isinstance(p_data[0], list) else []
            name = ""
            player_id = ""
            team_abbrev = ""
            position = ""
            for item in info_list:
                if isinstance(item, dict):
                    if "name" in item:
                        name = item.get("name", {}).get("full", "")
                    if "player_id" in item:
                        player_id = str(item.get("player_id", ""))
                    if "editorial_team_abbr" in item:
                        team_abbrev = item.get("editorial_team_abbr", "")
                    if "display_position" in item:
                        position = item.get("display_position", "")
            # Second element has percent_owned
            pct_owned = 0
            delta = ""
            ownership = p_data[1] if len(p_data) > 1 else {}
            if isinstance(ownership, dict):
                po = ownership.get("percent_owned", [])
                if isinstance(po, list):
                    for po_item in po:
                        if isinstance(po_item, dict):
                            if "value" in po_item:
                                try:
                                    pct_owned = float(po_item.get("value", 0))
                                except (ValueError, TypeError):
                                    pct_owned = 0
                            if "delta" in po_item:
                                raw_delta = po_item.get("delta", "0")
                                try:
                                    d = float(raw_delta)
                                    delta = ("+" if d > 0 else "") + str(d)
                                except (ValueError, TypeError):
                                    delta = str(raw_delta)
                elif isinstance(po, dict):
                    try:
                        pct_owned = float(po.get("value", 0))
                    except (ValueError, TypeError):
                        pct_owned = 0
                    raw_delta = po.get("delta", "0")
                    try:
                        d = float(raw_delta)
                        delta = ("+" if d > 0 else "") + str(d)
                    except (ValueError, TypeError):
                        delta = str(raw_delta)
            entry = {
                "name": name,
                "player_id": player_id,
                "team": team_abbrev.upper(),
                "position": position,
                "percent_owned": pct_owned,
                "delta": delta,
            }
            mlb_id = get_mlb_id(name)
            if mlb_id:
                entry["mlb_id"] = mlb_id
            players.append(entry)
    except Exception as e:
        print("Warning: error parsing trend players: " + str(e))
    return players


def cmd_transaction_trends(args, as_json=False):
    """Show most added and most dropped players across all Yahoo leagues"""
    sc = get_connection()
    gm = yfa.Game(sc, "mlb")

    count = 25
    try:
        added_raw = gm.yhandler.get(
            "game/mlb/players;sort=AR;count=" + str(count) + "/percent_owned"
        )
        dropped_raw = gm.yhandler.get(
            "game/mlb/players;sort=DR;count=" + str(count) + "/percent_owned"
        )
    except Exception as e:
        if as_json:
            return {"error": "Error fetching transaction trends: " + str(e)}
        print("Error fetching transaction trends: " + str(e))
        return

    most_added = _parse_trend_players(added_raw) if added_raw else []
    most_dropped = _parse_trend_players(dropped_raw) if dropped_raw else []

    # Record ownership snapshots for trend tracking
    try:
        import sqlite3
        db_path = os.path.join(DATA_DIR, "season.db")
        db = sqlite3.connect(db_path)
        for p in most_added + most_dropped:
            pid = str(p.get("player_id", ""))
            pct_val = float(p.get("percent_owned", 0)) if p.get("percent_owned") is not None else 0
            if pid:
                db.execute(
                    "INSERT OR REPLACE INTO ownership_history (player_id, date, pct_owned) VALUES (?, date('now'), ?)",
                    (pid, pct_val)
                )
        db.commit()
        db.close()
    except Exception:
        pass

    if as_json:
        return {"most_added": most_added, "most_dropped": most_dropped}

    print("Most Added Players (across all Yahoo leagues):")
    for i, p in enumerate(most_added, 1):
        line = "  " + str(i).rjust(2) + ". " + p.get("name", "?").ljust(25)
        line += " " + p.get("team", "?").ljust(4)
        line += " " + p.get("position", "?").ljust(8)
        line += " " + str(p.get("percent_owned", 0)).rjust(5) + "%"
        line += " (" + p.get("delta", "?") + ")"
        print(line)

    print("")
    print("Most Dropped Players (across all Yahoo leagues):")
    for i, p in enumerate(most_dropped, 1):
        line = "  " + str(i).rjust(2) + ". " + p.get("name", "?").ljust(25)
        line += " " + p.get("team", "?").ljust(4)
        line += " " + p.get("position", "?").ljust(8)
        line += " " + str(p.get("percent_owned", 0)).rjust(5) + "%"
        line += " (" + p.get("delta", "?") + ")"
        print(line)


def cmd_swap(args, as_json=False):
    """Atomic add+drop (swap players)"""
    if len(args) < 2:
        if as_json:
            return {
                "success": False,
                "add_key": "",
                "drop_key": "",
                "message": "Usage: swap ADD_ID DROP_ID",
            }
        print("Usage: swap ADD_ID DROP_ID")
        return
    add_id = args[0]
    drop_id = args[1]
    add_key = GAME_KEY + ".p." + str(add_id)
    drop_key = GAME_KEY + ".p." + str(drop_id)
    method = _write_method()

    if method != "browser":
        try:
            sc, gm, lg, team = get_league_context()
            team.add_and_drop_players(add_key, drop_key)
            msg = "Swapped: added " + add_key + ", dropped " + drop_key
            if as_json:
                return {
                    "success": True,
                    "add_key": add_key,
                    "drop_key": drop_key,
                    "message": msg,
                }
            print(msg)
            return
        except Exception as e:
            if method == "api" or not _is_scope_error(e):
                msg = "Error swapping players: " + str(e)
                if as_json:
                    return {
                        "success": False,
                        "add_key": add_key,
                        "drop_key": drop_key,
                        "message": msg,
                    }
                print(msg)
                return

    try:
        from yahoo_browser import swap_players

        result = swap_players(add_id, drop_id)
        if as_json:
            result["add_key"] = add_key
            result["drop_key"] = drop_key
            return result
        if result.get("success"):
            print(result.get("message", "Swap completed via browser"))
        else:
            print(result.get("message", "Browser swap failed"))
    except Exception as e:
        msg = "Browser fallback error: " + str(e)
        if as_json:
            return {
                "success": False,
                "add_key": add_key,
                "drop_key": drop_key,
                "message": msg,
            }
        print(msg)


def cmd_waiver_claim(args, as_json=False):
    """Submit a waiver claim with optional FAAB bid"""
    if not args:
        if as_json:
            return {"success": False, "message": "Missing player_id"}
        print("Usage: waiver-claim PLAYER_ID [FAAB_BID]")
        return
    player_id = args[0]
    player_key = GAME_KEY + ".p." + str(player_id)
    faab = None
    if len(args) > 1:
        try:
            faab = int(args[1])
        except (ValueError, TypeError):
            if as_json:
                return {
                    "success": False,
                    "message": "Invalid FAAB bid: " + str(args[1]),
                }
            print("Invalid FAAB bid: " + str(args[1]))
            return
    method = _write_method()

    if method != "browser":
        try:
            sc, gm, lg, team = get_league_context()
            if faab is not None:
                team.claim_player(player_key, faab=faab)
                msg = (
                    "Waiver claim submitted for "
                    + player_key
                    + " with $"
                    + str(faab)
                    + " FAAB bid"
                )
            else:
                team.claim_player(player_key)
                msg = "Waiver claim submitted for " + player_key
            if as_json:
                return {
                    "success": True,
                    "player_key": player_key,
                    "faab": faab,
                    "message": msg,
                }
            print(msg)
            return
        except Exception as e:
            if method == "api" or not _is_scope_error(e):
                msg = "Error submitting waiver claim: " + str(e)
                if as_json:
                    return {
                        "success": False,
                        "player_key": player_key,
                        "faab": faab,
                        "message": msg,
                    }
                print(msg)
                return

    try:
        from yahoo_browser import waiver_claim

        result = waiver_claim(player_id, faab=faab)
        if as_json:
            result["player_key"] = player_key
            result["faab"] = faab
            return result
        if result.get("success"):
            print(result.get("message", "Waiver claim submitted via browser"))
        else:
            print(result.get("message", "Browser waiver claim failed"))
    except Exception as e:
        msg = "Browser fallback error: " + str(e)
        if as_json:
            return {
                "success": False,
                "player_key": player_key,
                "faab": faab,
                "message": msg,
            }
        print(msg)


def cmd_waiver_claim_swap(args, as_json=False):
    """Submit a waiver claim + drop with optional FAAB bid"""
    if len(args) < 2:
        if as_json:
            return {
                "success": False,
                "message": "Usage: waiver-claim-swap ADD_ID DROP_ID [FAAB_BID]",
            }
        print("Usage: waiver-claim-swap ADD_ID DROP_ID [FAAB_BID]")
        return
    add_id = args[0]
    drop_id = args[1]
    add_key = GAME_KEY + ".p." + str(add_id)
    drop_key = GAME_KEY + ".p." + str(drop_id)
    faab = None
    if len(args) > 2:
        try:
            faab = int(args[2])
        except (ValueError, TypeError):
            if as_json:
                return {
                    "success": False,
                    "message": "Invalid FAAB bid: " + str(args[2]),
                }
            print("Invalid FAAB bid: " + str(args[2]))
            return
    method = _write_method()

    if method != "browser":
        try:
            sc, gm, lg, team = get_league_context()
            if faab is not None:
                team.claim_and_drop_players(add_key, drop_key, faab=faab)
                msg = (
                    "Waiver claim+drop submitted: add "
                    + add_key
                    + ", drop "
                    + drop_key
                    + " with $"
                    + str(faab)
                    + " FAAB"
                )
            else:
                team.claim_and_drop_players(add_key, drop_key)
                msg = (
                    "Waiver claim+drop submitted: add " + add_key + ", drop " + drop_key
                )
            if as_json:
                return {
                    "success": True,
                    "add_key": add_key,
                    "drop_key": drop_key,
                    "faab": faab,
                    "message": msg,
                }
            print(msg)
            return
        except Exception as e:
            if method == "api" or not _is_scope_error(e):
                msg = "Error submitting waiver claim+drop: " + str(e)
                if as_json:
                    return {
                        "success": False,
                        "add_key": add_key,
                        "drop_key": drop_key,
                        "faab": faab,
                        "message": msg,
                    }
                print(msg)
                return

    try:
        from yahoo_browser import waiver_claim_swap

        result = waiver_claim_swap(add_id, drop_id, faab=faab)
        if as_json:
            result["add_key"] = add_key
            result["drop_key"] = drop_key
            result["faab"] = faab
            return result
        if result.get("success"):
            print(result.get("message", "Waiver claim+drop submitted via browser"))
        else:
            print(result.get("message", "Browser waiver claim+drop failed"))
    except Exception as e:
        msg = "Browser fallback error: " + str(e)
        if as_json:
            return {
                "success": False,
                "add_key": add_key,
                "drop_key": drop_key,
                "faab": faab,
                "message": msg,
            }
        print(msg)


def cmd_who_owns(args, as_json=False):
    """Check who owns a player by player_id"""
    if not args:
        if as_json:
            return {"error": "Missing player_id"}
        print("Usage: who-owns PLAYER_ID")
        return
    player_id = args[0]
    player_key = GAME_KEY + ".p." + str(player_id)
    sc, gm, lg = get_league()
    try:
        ownership = lg.ownership([player_key])
        if not ownership:
            if as_json:
                return {
                    "player_key": player_key,
                    "ownership_type": "unknown",
                    "owner": "",
                }
            print("No ownership info for " + player_key)
            return
        info = ownership.get(player_key, ownership.get(player_id, {}))
        if not info and len(ownership) == 1:
            info = list(ownership.values())[0]
        own_type = info.get("ownership_type", "unknown")
        owner_name = info.get("owner_team_name", "")
        if as_json:
            return {
                "player_key": player_key,
                "ownership_type": own_type,
                "owner": owner_name,
            }
        if own_type == "team":
            print(player_key + " is owned by: " + owner_name)
        elif own_type == "freeagents":
            print(player_key + " is a free agent")
        elif own_type == "waivers":
            print(player_key + " is on waivers")
        else:
            print(player_key + " ownership: " + own_type)
    except Exception as e:
        if as_json:
            return {"error": "Error checking ownership: " + str(e)}
        print("Error checking ownership: " + str(e))


def cmd_league_pulse(args, as_json=False):
    """Show league activity - moves and trades per team"""
    sc, gm, lg = get_league()
    try:
        teams = lg.teams()
        team_list = []
        for team_key, team_data in teams.items():
            logo_url, mgr_image = _extract_team_meta(team_data)
            team_list.append(
                {
                    "team_key": team_key,
                    "name": team_data.get("name", "Unknown"),
                    "moves": team_data.get("number_of_moves", 0),
                    "trades": team_data.get("number_of_trades", 0),
                    "total": team_data.get("number_of_moves", 0)
                    + team_data.get("number_of_trades", 0),
                    "team_logo": logo_url,
                    "manager_image": mgr_image,
                }
            )
        team_list.sort(key=lambda t: t.get("total", 0), reverse=True)
        if as_json:
            return {"teams": team_list}
        print("League Activity Pulse:")
        print(
            "  "
            + "Team".ljust(30)
            + "Moves".rjust(6)
            + "Trades".rjust(7)
            + "Total".rjust(6)
        )
        print("  " + "-" * 49)
        for t in team_list:
            print(
                "  "
                + t.get("name", "?").ljust(30)
                + str(t.get("moves", 0)).rjust(6)
                + str(t.get("trades", 0)).rjust(7)
                + str(t.get("total", 0)).rjust(6)
            )
    except Exception as e:
        if as_json:
            return {"error": "Error fetching league pulse: " + str(e)}
        print("Error fetching league pulse: " + str(e))


def cmd_discover(args, as_json=False):
    """Discover your Yahoo Fantasy leagues and teams for the current season.
    Does not require LEAGUE_ID or TEAM_ID to be set."""
    sc = OAuth2(None, None, from_file=OAUTH_FILE)
    if not sc.token_is_valid():
        sc.refresh_access_token()
    gm = yfa.Game(sc, "mlb")
    game_id = str(gm.game_id())
    all_ids = gm.league_ids()
    current_ids = [lid for lid in all_ids if lid.startswith(game_id + ".")]

    if not current_ids:
        msg = "No MLB leagues found for the " + game_id + " season."
        if as_json:
            return {"game_id": game_id, "leagues": [], "message": msg}
        print(msg)
        print("Make sure you've joined a Yahoo Fantasy Baseball league for this year.")
        return

    leagues = []
    for lid in current_ids:
        try:
            lg = gm.to_league(lid)
            settings = lg.settings()
            league_name = settings.get("name", "Unknown")
            season = settings.get("season", "?")
            num_teams = settings.get("num_teams", "?")
            teams = lg.teams()
            my_team_key = ""
            my_team_name = ""
            for tk, td in teams.items():
                if td.get("is_owned_by_current_login"):
                    my_team_key = tk
                    my_team_name = td.get("name", "Unknown")
                    break
            leagues.append(
                {
                    "league_id": lid,
                    "league_name": league_name,
                    "season": season,
                    "num_teams": num_teams,
                    "team_id": my_team_key,
                    "team_name": my_team_name,
                }
            )
        except Exception as e:
            leagues.append(
                {
                    "league_id": lid,
                    "league_name": "Error: " + str(e),
                    "season": "?",
                    "num_teams": "?",
                    "team_id": "",
                    "team_name": "",
                }
            )

    if as_json:
        return {"game_id": game_id, "leagues": leagues}

    print("")
    print("Your " + game_id + " MLB Fantasy Leagues:")
    print("")
    for i, lg_info in enumerate(leagues, 1):
        print("  " + str(i) + ". " + lg_info["league_name"])
        print(
            "     Season: "
            + str(lg_info["season"])
            + "  |  Teams: "
            + str(lg_info["num_teams"])
        )
        print("     LEAGUE_ID=" + lg_info["league_id"])
        if lg_info["team_id"]:
            print(
                "     TEAM_ID="
                + lg_info["team_id"]
                + "  ("
                + lg_info["team_name"]
                + ")"
            )
        else:
            print("     (could not identify your team)")
        print("")

    if len(leagues) == 1 and leagues[0]["team_id"]:
        lg_info = leagues[0]
        print("Add these to your .env file:")
        print("")
        print("  LEAGUE_ID=" + lg_info["league_id"])
        print("  TEAM_ID=" + lg_info["team_id"])
        print("")


def cmd_player_stats(args, as_json=False):
    """Get player fantasy stats from Yahoo for a given period"""
    if not args:
        if as_json:
            return {"error": "Usage: player-stats <name> [period] [week|date]"}
        print("Usage: player-stats <name> [period] [week|date]")
        print("  period: season (default), average_season, lastweek, lastmonth, week, date")
        return

    # Parse arguments: name is first, period is optional second, week/date is optional third
    name = args[0]
    period = args[1] if len(args) > 1 else "season"
    extra = args[2] if len(args) > 2 else None

    sc, gm, lg = get_league()

    def _extract_name(p):
        """Extract player name string — handles both str and dict formats"""
        n = p.get("name", "")
        if isinstance(n, dict):
            return n.get("full", n.get("first", "") + " " + n.get("last", ""))
        return str(n)

    # Look up player — try roster first (cheap), then player_details (searches all)
    try:
        found = None

        # Check our own roster first (single API call, most common use case)
        team = lg.to_team(TEAM_ID)
        roster = team.roster()
        if roster:
            for p in roster:
                if name.lower() in _extract_name(p).lower():
                    found = p
                    break

        # Use player_details to search all players (avoids 2x free_agents calls)
        if not found:
            try:
                search_results = lg.player_details(name)
                if search_results:
                    found = search_results[0] if isinstance(search_results, list) else search_results
            except Exception:
                pass

        if not found:
            if as_json:
                return {"error": "Player not found: " + name}
            print("Player not found: " + name)
            return

        player_id = found.get("player_id", "")
        player_name = _extract_name(found) or name

        # Build the player_stats call
        kwargs = {}
        if period == "week" and extra:
            kwargs["req_type"] = "week"
            kwargs["week"] = int(extra)
        elif period == "date" and extra:
            kwargs["req_type"] = "date"
            kwargs["date"] = extra
        else:
            kwargs["req_type"] = period

        stats = lg.player_stats([player_id], **kwargs)
        if not stats:
            if as_json:
                return {"error": "No stats returned for " + player_name}
            print("No stats returned for " + player_name)
            return

        # stats is typically a list of player stat dicts
        player_stats = stats[0] if isinstance(stats, list) else stats

        if as_json:
            return {
                "player_name": player_name,
                "player_id": str(player_id),
                "period": period,
                "week": extra if period == "week" else None,
                "date": extra if period == "date" else None,
                "stats": player_stats,
                "mlb_id": get_mlb_id(player_name),
            }

        print("Stats for " + player_name + " (" + period + "):")
        if isinstance(player_stats, dict):
            for key, val in player_stats.items():
                if key not in ("player_id", "name"):
                    print("  " + str(key).ljust(20) + str(val))
        else:
            print("  " + str(player_stats))

    except Exception as e:
        if as_json:
            return {"error": "Error fetching player stats: " + str(e)}
        print("Error fetching player stats: " + str(e))


def cmd_waivers(args, as_json=False):
    """Show players currently on waivers (not yet free agents)"""
    sc, gm, lg = get_league()
    try:
        waivers = lg.waivers()
        if not waivers:
            if as_json:
                return {"players": []}
            print("No players on waivers")
            return

        if as_json:
            players = []
            for p in waivers:
                players.append({
                    "name": p.get("name", "Unknown"),
                    "player_id": str(p.get("player_id", "")),
                    "eligible_positions": p.get("eligible_positions", []),
                    "percent_owned": p.get("percent_owned", 0),
                    "status": p.get("status", ""),
                    "mlb_id": get_mlb_id(p.get("name", "")),
                })
            enrich_with_intel(players)
            return {"players": players}

        print("Players on Waivers:")
        for p in waivers:
            pname = p.get("name", "Unknown")
            positions = ",".join(p.get("eligible_positions", ["?"]))
            pct = p.get("percent_owned", 0)
            pid = p.get("player_id", "?")
            status = p.get("status", "")
            line = "  " + pname.ljust(25) + " " + positions.ljust(12) + " " + str(pct).rjust(3) + "% owned  (id:" + str(pid) + ")"
            if status:
                line += " [" + status + "]"
            print(line)

    except Exception as e:
        if as_json:
            return {"error": "Error fetching waivers: " + str(e)}
        print("Error fetching waivers: " + str(e))


def cmd_taken_players(args, as_json=False):
    """Show all rostered players across the league"""
    sc, gm, lg = get_league()
    position = args[0] if args else None

    try:
        taken = lg.taken_players()
        if not taken:
            if as_json:
                return {"players": []}
            print("No taken players found")
            return

        # Filter by position if specified
        if position:
            filtered = []
            for p in taken:
                elig = p.get("eligible_positions", [])
                if position.upper() in [pos.upper() for pos in elig]:
                    filtered.append(p)
            taken = filtered

        if as_json:
            players = []
            for p in taken:
                players.append({
                    "name": p.get("name", "Unknown"),
                    "player_id": str(p.get("player_id", "")),
                    "eligible_positions": p.get("eligible_positions", []),
                    "percent_owned": p.get("percent_owned", 0),
                    "status": p.get("status", ""),
                    "owner": p.get("owner", ""),
                    "mlb_id": get_mlb_id(p.get("name", "")),
                })
            return {"players": players, "position": position, "count": len(players)}

        print("All Rostered Players" + (" (" + position + ")" if position else "") + ":")
        for p in taken:
            pname = p.get("name", "Unknown")
            positions = ",".join(p.get("eligible_positions", ["?"]))
            pct = p.get("percent_owned", 0)
            owner = p.get("owner", "")
            line = "  " + pname.ljust(25) + " " + positions.ljust(12) + " " + str(pct).rjust(3) + "% owned"
            if owner:
                line += "  -> " + owner
            print(line)

    except Exception as e:
        if as_json:
            return {"error": "Error fetching taken players: " + str(e)}
        print("Error fetching taken players: " + str(e))


def cmd_roster_history(args, as_json=False):
    """Show roster for a past week or date"""
    if not args:
        if as_json:
            return {"error": "Usage: roster-history <week|date> [team_key]"}
        print("Usage: roster-history <week_number|YYYY-MM-DD> [team_key]")
        return

    lookup = args[0]
    team_key = args[1] if len(args) > 1 else None

    sc, gm, lg = get_league()
    team = lg.to_team(team_key or TEAM_ID)

    try:
        # Determine if lookup is a date or week number
        if "-" in lookup:
            # Date format: YYYY-MM-DD
            d = datetime.date.fromisoformat(lookup)
            roster = team.roster(day=d)
            label = "date " + lookup
        else:
            # Week number
            week = int(lookup)
            roster = team.roster(week=week)
            label = "week " + str(week)

        if not roster:
            if as_json:
                return {"players": [], "lookup": lookup}
            print("No roster data for " + label)
            return

        if as_json:
            players = []
            for p in roster:
                players.append({
                    "name": _player_name(p),
                    "player_id": str(p.get("player_id", "")),
                    "position": _selected_position(p),
                    "eligible_positions": _eligible_positions(p),
                    "status": p.get("status", ""),
                    "mlb_id": get_mlb_id(_player_name(p)),
                })
            return {"players": players, "lookup": lookup, "label": label}

        print("Roster for " + label + ":")
        for p in roster:
            pos = _selected_position(p)
            pname = _player_name(p)
            status = p.get("status", "")
            elig = ",".join(_eligible_positions(p))
            line = "  " + pos.ljust(4) + " " + pname.ljust(25) + " " + elig
            if status:
                line += " [" + status + "]"
            print(line)

    except Exception as e:
        if as_json:
            return {"error": "Error fetching roster history: " + str(e)}
        print("Error fetching roster history: " + str(e))


def cmd_percent_owned(args, as_json=False):
    """Get percent owned for specific players by player ID"""
    if not args:
        if as_json:
            return {"error": "Usage: percent-owned <player_id> [player_id ...]"}
        print("Usage: percent-owned <player_id> [player_id ...]")
        return
    sc, gm, lg = get_league()
    try:
        player_ids = [int(pid) for pid in args]
        result = lg.percent_owned(player_ids)
        if not result:
            if as_json:
                return {"players": []}
            print("No ownership data returned")
            return
        if as_json:
            players = []
            for p in result:
                players.append({
                    "player_id": str(p.get("player_id", "")),
                    "name": p.get("name", "Unknown"),
                    "percent_owned": p.get("percent_owned", 0),
                })
            return {"players": players}
        print("Percent Owned:")
        for p in result:
            name = p.get("name", "Unknown")
            pct = p.get("percent_owned", 0)
            pid = p.get("player_id", "?")
            print("  " + name.ljust(25) + " " + str(pct).rjust(5) + "%  (id:" + str(pid) + ")")
    except Exception as e:
        if as_json:
            return {"error": "Error fetching percent owned: " + str(e)}
        print("Error fetching percent owned: " + str(e))


COMMANDS = {
    "discover": cmd_discover,
    "roster": cmd_roster,
    "free-agents": cmd_free_agents,
    "standings": cmd_standings,
    "info": cmd_info,
    "search": cmd_search,
    "add": cmd_add,
    "drop": cmd_drop,
    "matchups": cmd_matchups,
    "scoreboard": cmd_scoreboard,
    "matchup-detail": cmd_matchup_detail,
    "transactions": cmd_transactions,
    "stat-categories": cmd_stat_categories,
    "swap": cmd_swap,
    "transaction-trends": cmd_transaction_trends,
    "waiver-claim": cmd_waiver_claim,
    "waiver-claim-swap": cmd_waiver_claim_swap,
    "who-owns": cmd_who_owns,
    "league-pulse": cmd_league_pulse,
    "player-stats": cmd_player_stats,
    "waivers": cmd_waivers,
    "taken-players": cmd_taken_players,
    "roster-history": cmd_roster_history,
    "percent-owned": cmd_percent_owned,
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Yahoo Fantasy Baseball CLI (Docker)")
        print("Usage: yahoo-fantasy.py <command> [args]")
        print("\nCommands: " + ", ".join(COMMANDS.keys()))
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd in COMMANDS:
        COMMANDS[cmd](args)
    else:
        print("Unknown command: " + cmd)
