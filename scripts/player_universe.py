#!/usr/bin/env python3
"""Build a league-relevant player universe for research pipelines."""

from datetime import datetime, timezone


def _safe_int(value, default):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _normalize_name(value):
    return str(value or "").strip().lower()


def _source_priority(tags):
    order = {
        "taken_players": 0,
        "waivers": 1,
        "free_agents_p": 2,
        "free_agents_b": 3,
    }
    if not tags:
        return 999
    return min(order.get(tag, 999) for tag in tags)


def _infer_pos_type(eligible_positions):
    tokens = [str(pos or "").strip().upper() for pos in (eligible_positions or [])]
    pitcher_pos = {"SP", "RP", "P"}
    batter_pos = {"C", "1B", "2B", "3B", "SS", "OF", "LF", "CF", "RF", "DH"}
    pitcher_count = sum(1 for p in tokens if p in pitcher_pos)
    batter_count = sum(1 for p in tokens if p in batter_pos)
    # Two-way players: classify by majority; tie goes to batter
    if pitcher_count > batter_count:
        return "P"
    return "B"


def _normalize_player(raw, source_tag):
    player_id = str(raw.get("player_id", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not player_id and not name:
        return None

    eligible_positions = raw.get("eligible_positions")
    if not isinstance(eligible_positions, list):
        eligible_positions = raw.get("positions", [])
    if not isinstance(eligible_positions, list):
        eligible_positions = []

    percent_owned = raw.get("percent_owned")
    try:
        percent_owned = float(percent_owned) if percent_owned is not None else 0.0
    except Exception:
        percent_owned = 0.0

    return {
        "player_id": player_id,
        "name": name,
        "team": str(raw.get("team", "")).strip(),
        "eligible_positions": [str(pos).strip().upper() for pos in eligible_positions if str(pos).strip()],
        "status": str(raw.get("status", "")).strip(),
        "percent_owned": percent_owned,
        "source_tags": [source_tag],
        "mlb_id": raw.get("mlb_id"),
    }


def _merge_players(existing, incoming):
    merged_tags = list(dict.fromkeys(existing.get("source_tags", []) + incoming.get("source_tags", [])))
    merged = dict(existing)
    merged["source_tags"] = merged_tags

    if _source_priority(incoming.get("source_tags", [])) < _source_priority(existing.get("source_tags", [])):
        for field in ("team", "eligible_positions", "status", "mlb_id"):
            value = incoming.get(field)
            if value not in (None, "", []):
                merged[field] = value

    merged["percent_owned"] = max(float(existing.get("percent_owned", 0.0)), float(incoming.get("percent_owned", 0.0)))
    merged["pos_type"] = _infer_pos_type(merged.get("eligible_positions", []))
    return merged


def _merge_rows(rows):
    players = {}
    for item in rows:
        key = item.get("player_id") or _normalize_name(item.get("name"))
        if not key:
            continue
        if key not in players:
            player = dict(item)
            player["pos_type"] = _infer_pos_type(player.get("eligible_positions", []))
            players[key] = player
            continue
        players[key] = _merge_players(players[key], item)

    return list(players.values())


def _extract_players(payload):
    if isinstance(payload, dict):
        players = payload.get("players")
        if isinstance(players, list):
            return players
    return []


def build_player_universe(yahoo_fantasy, league_context_fetcher=None, max_players_per_group=120):
    limit = _safe_int(max_players_per_group, 120)

    taken_rows = _extract_players(yahoo_fantasy.cmd_taken_players([], as_json=True))
    waiver_rows = _extract_players(yahoo_fantasy.cmd_waivers([], as_json=True))
    fa_b_rows = _extract_players(yahoo_fantasy.cmd_free_agents(["B", str(limit)], as_json=True))
    fa_p_rows = _extract_players(yahoo_fantasy.cmd_free_agents(["P", str(limit)], as_json=True))

    normalized = []
    normalized.extend(filter(None, (_normalize_player(row, "taken_players") for row in taken_rows)))
    normalized.extend(filter(None, (_normalize_player(row, "waivers") for row in waiver_rows)))
    normalized.extend(filter(None, (_normalize_player(row, "free_agents_b") for row in fa_b_rows)))
    normalized.extend(filter(None, (_normalize_player(row, "free_agents_p") for row in fa_p_rows)))

    players = _merge_rows(normalized)
    players.sort(key=lambda row: (-float(row.get("percent_owned", 0.0)), row.get("name", "")))

    league_context = {}
    if callable(league_context_fetcher):
        try:
            context = league_context_fetcher()
            if isinstance(context, dict):
                league_context = context
        except Exception:
            league_context = {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league_context": league_context,
        "players": players,
    }
