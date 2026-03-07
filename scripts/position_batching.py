"""Helpers for hitter position filtering and ALL-mode grouped payloads."""


def safe_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_hitter_positions_csv(value):
    valid = {"C", "1B", "2B", "3B", "SS", "OF", "UTIL"}
    if value is None:
        return []
    raw = str(value).strip()
    if raw == "":
        return []

    parsed = []
    for token in raw.split(","):
        pos = token.strip().upper()
        if not pos:
            continue
        if pos not in valid:
            raise ValueError("Invalid hitter position token: " + token.strip())
        parsed.append(pos)
    return list(dict.fromkeys(parsed))


def split_position_tokens(value):
    if value is None:
        return []
    text = str(value).upper().replace("/", ",").replace(";", ",")
    return [p.strip() for p in text.split(",") if p.strip()]


def ranking_position_tokens(player):
    return split_position_tokens(player.get("pos"))


def best_available_position_tokens(player):
    tokens = []
    for entry in player.get("positions", []) or []:
        tokens.extend(split_position_tokens(entry))
    return list(dict.fromkeys(tokens))


def disagreement_position_tokens(player):
    return split_position_tokens(player.get("pos"))


def matches_hitter_positions(tokens, requested_positions):
    if not requested_positions:
        return True
    if not tokens:
        return "UTIL" in requested_positions
    token_set = set(tokens)
    if "UTIL" in requested_positions:
        return True
    for pos in requested_positions:
        if pos in token_set:
            return True
    return False


def filter_rows_by_positions(rows, requested_positions, token_fn):
    if not requested_positions:
        return list(rows or [])
    filtered = []
    for row in rows or []:
        if matches_hitter_positions(token_fn(row), requested_positions):
            filtered.append(row)
    return filtered


def group_rows_by_positions(rows, requested_positions, token_fn):
    buckets = {pos: [] for pos in requested_positions}
    if not requested_positions:
        return buckets

    for row in rows or []:
        tokens = token_fn(row)
        token_set = set(tokens)
        for pos in requested_positions:
            if pos == "UTIL":
                if tokens:
                    buckets[pos].append(row)
            elif pos in token_set:
                buckets[pos].append(row)
    return buckets


def normalize_hitter_payload(payload, rows_key, requested_positions, group_by_position, token_fn):
    normalized = dict(payload or {})
    rows = normalized.get(rows_key, []) or []
    rows = filter_rows_by_positions(rows, requested_positions, token_fn)
    normalized[rows_key] = rows
    if group_by_position:
        normalized["buckets"] = group_rows_by_positions(rows, requested_positions, token_fn)
    return normalized


def grouped_all_payload(b_payload, p_payload):
    return {
        "pos_type": "ALL",
        "groups": {
            "B": b_payload,
            "P": p_payload,
        },
    }
