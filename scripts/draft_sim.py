"""
Snake draft simulator.

Entry point: simulate_draft(batters, pitchers, ...)

Each player dict (from cmd_rankings bare) has at minimum:
  rank, name, team, pos, z_score, mlb_id, pos_type (added here), adp_score (added here)
"""

import random
from typing import Optional

# --- Position metadata ---

# Per-position elite/solid tier thresholds across a 12-team league.
# "elite" = top N players at that position league-wide. Scarcity fires when these are gone.
_POSITION_TIERS = {
    "C":  {"elite": 12, "solid": 24},
    "1B": {"elite": 18, "solid": 36},
    "2B": {"elite": 18, "solid": 36},
    "3B": {"elite": 18, "solid": 36},
    "SS": {"elite": 12, "solid": 24},
    "OF": {"elite": 36, "solid": 72},
    "DH": {"elite": 6,  "solid": 12},
    "SP": {"elite": 36, "solid": 72},
    "RP": {"elite": 24, "solid": 48},
}

# Default Yahoo roster composition (23 players)
DEFAULT_ROSTER_CONFIG = {
    "C": 2, "1B": 1, "2B": 1, "3B": 1, "SS": 1,
    "OF": 5, "SP": 5, "RP": 2,
}

# Pitcher Z-scores are discounted so mixed-pool ADP sort reflects reality.
# Hitter-heavy leagues: pitchers go ~15-20% later than raw Z would suggest.
_PITCHER_DISCOUNT = 0.80


def _canonical_pos(pos: str) -> str:
    p = pos.upper().strip()
    if p in ("LF", "CF", "RF"):
        return "OF"
    if p in ("UTIL", ""):
        return "1B"
    return p


def _build_pool(batters: list, pitchers: list) -> list:
    pool = []
    for p in batters:
        entry = dict(p)
        entry["pos_type"] = "B"
        entry["adp_score"] = float(p.get("z_score", 0))
        entry["canonical_pos"] = _canonical_pos(p.get("pos", "DH"))
        pool.append(entry)
    for p in pitchers:
        entry = dict(p)
        entry["pos_type"] = "P"
        entry["adp_score"] = _PITCHER_DISCOUNT * float(p.get("z_score", 0))
        entry["canonical_pos"] = _canonical_pos(p.get("pos", "SP"))
        pool.append(entry)
    pool.sort(key=lambda x: x["adp_score"], reverse=True)
    for i, p in enumerate(pool):
        p["adp_rank"] = i + 1
    return pool


def _snake_pick_order(num_teams: int, num_rounds: int) -> list:
    picks = []
    for r in range(1, num_rounds + 1):
        slots = range(1, num_teams + 1) if r % 2 == 1 else range(num_teams, 0, -1)
        for t in slots:
            picks.append((r, t))
    return picks


def _opponent_pick(available: list, noise: int, rng: random.Random) -> dict:
    candidates = available[:max(1, 1 + noise)]
    return rng.choice(candidates)


def _tier_label(player: dict, picked_so_far: list) -> str:
    pos = player["canonical_pos"]
    tiers = _POSITION_TIERS.get(pos, {"elite": 24, "solid": 48})
    pos_picked = sum(1 for p in picked_so_far if p["canonical_pos"] == pos)
    if pos_picked < tiers["elite"]:
        return "elite"
    elif pos_picked < tiers["solid"]:
        return "solid"
    return "depth"


def _position_needs(roster: list, roster_config: dict) -> dict:
    filled = {}
    for p in roster:
        pos = p["canonical_pos"]
        filled[pos] = filled.get(pos, 0) + 1
    needs = {}
    for pos, target in roster_config.items():
        if filled.get(pos, 0) < target:
            needs[pos] = target - filled.get(pos, 0)
    return needs


def _scarcity_flags(available: list, all_picked: list) -> list:
    flags = []
    for pos, tiers in _POSITION_TIERS.items():
        pos_picked = sum(1 for p in all_picked if p["canonical_pos"] == pos)
        remaining_elite = max(0, tiers["elite"] - pos_picked)
        if remaining_elite == 0:
            continue  # already flagged, stop repeating
        if remaining_elite <= 3:
            still_avail = sum(1 for p in available if p["canonical_pos"] == pos)
            if still_avail > 0:
                flags.append(pos + " elite almost gone (" + str(remaining_elite) + " left)")
    return flags


def _scarcity_timeline(all_picks: list) -> dict:
    timeline = {}
    pos_counts: dict = {}
    for pick_num, player in enumerate(all_picks, 1):
        pos = player["canonical_pos"]
        pos_counts[pos] = pos_counts.get(pos, 0) + 1
        tiers = _POSITION_TIERS.get(pos, {"elite": 24, "solid": 48})
        if pos not in timeline and pos_counts[pos] >= tiers["elite"]:
            timeline[pos] = pick_num
    return timeline


def simulate_draft(
    batters: list,
    pitchers: list,
    draft_position: int = 1,
    num_teams: int = 12,
    rounds: int = 23,
    noise: int = 3,
    roster_config: Optional[dict] = None,
    seed: int = 42,
) -> dict:
    """
    Run a simulated snake draft and return per-user-pick recommendations.

    Returns a dict with:
      user_picks       — detail for each user pick (options, scarcity, needs)
      roster_projection — projected end-state roster
      scarcity_timeline — overall pick# when each position's elite tier exhausted
      position_targets  — suggested position per round
      meta              — input params + pool stats
    """
    rng = random.Random(seed)
    if roster_config is None:
        roster_config = dict(DEFAULT_ROSTER_CONFIG)

    pool = _build_pool(batters, pitchers)
    pick_schedule = _snake_pick_order(num_teams, rounds)
    available = list(pool)
    all_picks: list = []
    user_roster: list = []
    user_picks_detail: list = []

    for rnd, team_slot in pick_schedule:
        if not available:
            break

        if team_slot == draft_position:
            # --- User's turn ---
            flags = _scarcity_flags(available, all_picks)
            needs = _position_needs(user_roster, roster_config)

            # Score top-30 available by adp_score + need bonus
            def _opt_score(p, _needs=needs):
                bonus = 0.5 if p["canonical_pos"] in _needs else 0.0
                return p["adp_score"] + bonus

            top_5 = sorted(available[:30], key=_opt_score, reverse=True)[:5]
            options = []
            for idx, p in enumerate(top_5):
                tier = _tier_label(p, all_picks)
                pos_picked = sum(1 for x in all_picks if x["canonical_pos"] == p["canonical_pos"])
                tiers_cfg = _POSITION_TIERS.get(p["canonical_pos"], {"elite": 24, "solid": 48})
                remaining_elite = max(0, tiers_cfg["elite"] - pos_picked)
                note = ""
                if remaining_elite <= 1:
                    note = "last elite " + p["canonical_pos"]
                elif remaining_elite <= 3:
                    note = str(remaining_elite) + " elite " + p["canonical_pos"] + " left"
                elif p["canonical_pos"] in needs:
                    note = "fills " + p["canonical_pos"] + " need"
                options.append({
                    "name": p["name"],
                    "team": p.get("team", ""),
                    "pos": p.get("pos", p["canonical_pos"]),
                    "pos_type": p["pos_type"],
                    "z_score": round(float(p.get("z_score", 0)), 2),
                    "adp_rank": p["adp_rank"],
                    "position_tier": tier,
                    "scarcity_note": note,
                    "suggested": idx == 0,
                })

            user_picks_detail.append({
                "round": rnd,
                "overall_pick": len(all_picks) + 1,
                "top_options": options,
                "scarcity_flags": flags,
                "position_needs": list(needs.keys()),
            })

            # Auto-pick the suggested player for projection purposes
            chosen_name = options[0]["name"]
            chosen_idx = next(i for i, p in enumerate(available) if p["name"] == chosen_name)
            chosen = available.pop(chosen_idx)
            user_roster.append(chosen)
            all_picks.append(chosen)
        else:
            # --- Opponent's turn ---
            chosen = _opponent_pick(available, noise, rng)
            available.remove(chosen)
            all_picks.append(chosen)

    # Roster projection
    roster_projection = []
    for i, p in enumerate(user_roster, 1):
        pick_detail = user_picks_detail[i - 1] if i <= len(user_picks_detail) else {}
        roster_projection.append({
            "pick_num": i,
            "round": pick_detail.get("round"),
            "overall_pick": pick_detail.get("overall_pick"),
            "name": p["name"],
            "pos": p.get("pos", p["canonical_pos"]),
            "pos_type": p["pos_type"],
            "z_score": round(float(p.get("z_score", 0)), 2),
        })

    # Suggested position per round (based on what was auto-picked)
    position_targets = [
        {"round": d["round"], "overall_pick": d["overall_pick"], "suggested_position": d["top_options"][0]["pos"] if d["top_options"] else "BPA"}
        for d in user_picks_detail
    ]

    return {
        "user_picks": user_picks_detail,
        "roster_projection": roster_projection,
        "scarcity_timeline": _scarcity_timeline(all_picks),
        "position_targets": position_targets,
        "meta": {
            "draft_position": draft_position,
            "num_teams": num_teams,
            "rounds": rounds,
            "noise": noise,
            "total_picks_simulated": len(all_picks),
            "batters_in_pool": len(batters),
            "pitchers_in_pool": len(pitchers),
        },
    }
