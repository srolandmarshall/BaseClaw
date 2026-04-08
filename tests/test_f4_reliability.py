import importlib.util
import json
import pathlib
import sys
import threading
import time
import types
import unittest
from datetime import date, datetime
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"


def _module(name):
    return types.ModuleType(name)


def _load_script(module_name, filename, stubs):
    saved = {}
    for name, module in stubs.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module
    try:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def _shared_stub():
    shared = _module("shared")
    shared.get_connection = lambda *args, **kwargs: None
    shared.get_league_context = lambda *args, **kwargs: (None, None, None, None)
    shared.get_league = lambda *args, **kwargs: (None, None, None)
    shared.get_team_key = lambda *args, **kwargs: ""
    shared.get_league_settings = lambda *args, **kwargs: {}
    shared.OAUTH_FILE = "/tmp/test_oauth.json"
    shared.LEAGUE_ID = ""
    shared.TEAM_ID = ""
    shared.GAME_KEY = "mlb"
    shared.DATA_DIR = "/tmp"
    shared.MLB_API = "https://statsapi.mlb.com/api/v1"
    shared.mlb_fetch = lambda *args, **kwargs: {}
    shared.normalize_team_name = lambda value: (value or "").strip().lower()
    shared.normalize_player_name = lambda value: (value or "").strip().lower()
    shared.TEAM_ALIASES = {}
    shared.get_trend_lookup = lambda *args, **kwargs: {}
    shared.enrich_with_intel = lambda *args, **kwargs: None
    shared.enrich_with_trends = lambda *args, **kwargs: None
    shared.fetch_mlb_injuries = lambda *args, **kwargs: []
    shared.USER_AGENT = "test-agent"
    shared.reddit_get = lambda *args, **kwargs: {}
    shared.cache_get = lambda cache, key, ttl: (
        cache.get(key, (None, 0))[0] if key in cache else None
    )
    shared.cache_set = lambda cache, key, data: cache.__setitem__(key, (data, 0))
    return shared


class _FakeDataFrame:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for idx, row in enumerate(self._rows):
            yield idx, row


def _position_batching_stub():
    module = _module("position_batching")
    module.best_available_position_tokens = lambda *args, **kwargs: []
    module.disagreement_position_tokens = lambda *args, **kwargs: []
    module.grouped_all_payload = lambda *args, **kwargs: {}
    module.normalize_hitter_payload = lambda payload, *args, **kwargs: payload
    module.parse_hitter_positions_csv = lambda *args, **kwargs: []
    module.ranking_position_tokens = lambda *args, **kwargs: []
    module.safe_bool = lambda value, default=False: default if value is None else bool(value)
    return module


def _trace_utils_stub():
    module = _module("trace_utils")
    module.clear_trace_context = lambda *args, **kwargs: None
    module.get_trace_context = lambda *args, **kwargs: {}
    module.log_trace_event = lambda **kwargs: None
    module.monotonic_ms = lambda: 1
    module.start_request_trace = lambda *args, **kwargs: "req-test"
    module.update_trace_context = lambda *args, **kwargs: None
    module.trace_config = lambda: {}
    return module


def _pandas_stub():
    module = _module("pandas")
    module.isna = lambda value: value is None
    module.DataFrame = lambda *args, **kwargs: []

    class _Series(list):
        pass

    module.Series = _Series
    return module


def _numpy_stub():
    module = _module("numpy")
    module.sqrt = lambda value: value ** 0.5
    module.std = lambda values: 0
    module.where = lambda condition, yes, no: yes if condition else no
    return module


def _flask_stub():
    module = _module("flask")

    class FakeFlask:
        def __init__(self, _name):
            self.view_functions = {}

        def route(self, path, **_kwargs):
            def decorator(func):
                self.view_functions[path] = func
                return func

            return decorator

        def before_request(self, func):
            return func

        def after_request(self, func):
            return func

        def teardown_request(self, func):
            return func

    module.Flask = FakeFlask
    module.jsonify = lambda payload: payload
    module.request = types.SimpleNamespace(
        args={},
        method="GET",
        headers={},
        path="",
        get_json=lambda silent=False: {},
    )
    module.g = types.SimpleNamespace()
    return module


class ReliabilityHardeningTests(unittest.TestCase):
    def test_live_rankings_blend_exposes_projection_and_season_deltas(self):
        valuations_module = _load_script(
            "valuations_live_blend_for_test",
            "valuations.py",
            {
                "pandas": _pandas_stub(),
                "numpy": _numpy_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda name, *_args, **_kwargs: "mlb-" + str(name)),
                "shared": types.SimpleNamespace(enrich_with_intel=lambda *_args, **_kwargs: None),
                "trace_utils": _trace_utils_stub(),
            },
        )

        players = valuations_module._build_live_rankings_from_lookups(
            {
                "projection riser": {"name": "Projection Riser", "team": "ATL", "pos": "SP", "z_score": 3.0, "mlb_id": "mlb-1"},
                "steady bat": {"name": "Steady Bat", "team": "LAD", "pos": "OF", "z_score": 5.0, "mlb_id": "mlb-2"},
            },
            {
                "projection riser": {"name": "Projection Riser", "team": "ATL", "pos": "SP", "z_score": 9.0, "mlb_id": "mlb-1"},
                "live only": {"name": "Live Only", "team": "MIA", "pos": "SP", "z_score": 8.5, "mlb_id": "mlb-3"},
            },
            "P",
            3,
            0.7,
        )

        self.assertEqual(players[0]["name"], "Projection Riser")
        self.assertEqual(players[0]["projection_z_score"], 3.0)
        self.assertEqual(players[0]["season_z_score"], 9.0)
        self.assertEqual(players[0]["delta_z"], 6.0)
        self.assertEqual(players[0]["rank"], 1)
        self.assertEqual(players[1]["name"], "Live Only")
        self.assertEqual(players[1]["projection_z_score"], 0.0)
        self.assertEqual(players[1]["season_z_score"], 8.5)

    def test_live_rows_to_z_lookup_does_not_resolve_mlb_ids_for_full_universe(self):
        calls = []

        valuations_module = _load_script(
            "valuations_live_lookup_defers_mlb_ids_for_test",
            "valuations.py",
            {
                "pandas": _pandas_stub(),
                "numpy": _numpy_stub(),
                "mlb_id_cache": types.SimpleNamespace(
                    get_mlb_id=lambda name, *_args, **_kwargs: calls.append(name) or ("mlb-" + str(name))
                ),
                "shared": types.SimpleNamespace(enrich_with_intel=lambda *_args, **_kwargs: None),
                "trace_utils": _trace_utils_stub(),
            },
        )

        df = _FakeDataFrame(
            [
                {"Name": "Alpha", "Team": "ATL", "Pos": "SP", "Z_Final": 6.0},
                {"Name": "Beta", "Team": "LAD", "Pos": "SP", "Z_Final": 5.0},
                {"Name": "Gamma", "Team": "NYM", "Pos": "SP", "Z_Final": 4.0},
            ]
        )

        lookup = valuations_module._rows_to_z_lookup(df)
        self.assertEqual(calls, [])
        players = valuations_module._build_live_rankings_from_lookups(lookup, {}, "P", 2, 0.45)
        valuations_module._resolve_mlb_ids_for_players(players)
        self.assertEqual([player["name"] for player in players], ["Alpha", "Beta"])
        self.assertEqual(calls, ["Alpha", "Beta"])
        self.assertEqual(players[0]["mlb_id"], "mlb-Alpha")

    def test_live_stats_failures_are_negative_cached(self):
        valuations_module = _load_script(
            "valuations_live_stats_negative_cache_for_test",
            "valuations.py",
            {
                "pandas": _pandas_stub(),
                "numpy": _numpy_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "shared": types.SimpleNamespace(enrich_with_intel=lambda *_args, **_kwargs: None),
                "trace_utils": _trace_utils_stub(),
            },
        )

        calls = {"pit": 0}
        pybaseball_mod = _module("pybaseball")

        def fake_pitching_stats(_year, qual=1):
            calls["pit"] += 1
            raise RuntimeError("upstream 403")

        pybaseball_mod.pitching_stats = fake_pitching_stats
        pybaseball_mod.batting_stats = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("batting_stats should not run for pit-only requests")
        )

        saved_pybaseball = sys.modules.get("pybaseball")
        sys.modules["pybaseball"] = pybaseball_mod
        try:
            valuations_module._LIVE_STATS_NEGATIVE_TTL = 999
            valuations_module._live_stats_cache = {
                "bat": {"data": None, "time": 0.0, "status": "empty"},
                "pit": {"data": None, "time": 0.0, "status": "empty"},
            }
            with patch("builtins.print"):
                first = valuations_module.load_live_stats("pit")
                second = valuations_module.load_live_stats("pit")
            self.assertEqual(first, (None, None))
            self.assertEqual(second, (None, None))
            self.assertEqual(calls["pit"], 1)
        finally:
            if saved_pybaseball is None:
                sys.modules.pop("pybaseball", None)
            else:
                sys.modules["pybaseball"] = saved_pybaseball

    def test_live_stats_timeout_returns_promptly_without_waiting_for_worker(self):
        valuations_module = _load_script(
            "valuations_live_stats_timeout_for_test",
            "valuations.py",
            {
                "pandas": _pandas_stub(),
                "numpy": _numpy_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "shared": types.SimpleNamespace(enrich_with_intel=lambda *_args, **_kwargs: None),
                "trace_utils": _trace_utils_stub(),
            },
        )

        pybaseball_mod = _module("pybaseball")

        def fake_pitching_stats(_year, qual=1):
            time.sleep(0.2)
            return _FakeDataFrame([])

        pybaseball_mod.pitching_stats = fake_pitching_stats
        pybaseball_mod.batting_stats = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("batting_stats should not run for pit-only requests")
        )

        saved_pybaseball = sys.modules.get("pybaseball")
        sys.modules["pybaseball"] = pybaseball_mod
        try:
            valuations_module._LIVE_STATS_FETCH_TIMEOUT = 0.01
            valuations_module._LIVE_STATS_NEGATIVE_TTL = 999
            valuations_module._live_stats_cache = {
                "bat": {"data": None, "time": 0.0, "status": "empty"},
                "pit": {"data": None, "time": 0.0, "status": "empty"},
            }
            started = time.monotonic()
            with patch("builtins.print"):
                result = valuations_module.load_live_stats("pit")
            elapsed = time.monotonic() - started
            self.assertEqual(result, (None, None))
            self.assertLess(elapsed, 0.1)
        finally:
            if saved_pybaseball is None:
                sys.modules.pop("pybaseball", None)
            else:
                sys.modules["pybaseball"] = saved_pybaseball

    def test_compute_live_scored_frames_only_fetches_requested_type(self):
        valuations_module = _load_script(
            "valuations_live_frames_type_selective_for_test",
            "valuations.py",
            {
                "pandas": _pandas_stub(),
                "numpy": _numpy_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "shared": types.SimpleNamespace(enrich_with_intel=lambda *_args, **_kwargs: None),
                "trace_utils": _trace_utils_stub(),
            },
        )

        live_calls = []
        valuations_module.load_pitchers_csv = lambda: "proj-pitchers"
        valuations_module.derive_pitcher_stats = lambda df: ("derived-pitchers", df)
        valuations_module.compute_pitcher_zscores = lambda df: ("projection-scored", df)
        valuations_module.load_live_stats = lambda stats_type="both": (
            live_calls.append(stats_type) or (None, "live-pitchers")
        )
        valuations_module._compute_pitcher_zscores_with_threshold = (
            lambda df, min_ip: ("live-scored", df, min_ip)
        )

        proj_scored, live_scored = valuations_module._compute_live_scored_frames("P")

        self.assertEqual(live_calls, ["pit"])
        self.assertEqual(proj_scored, ("projection-scored", ("derived-pitchers", "proj-pitchers")))
        self.assertEqual(live_scored, ("live-scored", ("derived-pitchers", "live-pitchers"), 8))

    def test_roster_cmd_accepts_string_selected_position(self):
        mlb_cache_mod = _module("mlb_id_cache")
        mlb_cache_mod.get_mlb_id = lambda name, *args, **kwargs: "mlb-" + str(name)

        module = _load_script(
            "yahoo_fantasy_roster_script_for_test",
            "yahoo-fantasy.py",
            {
                "shared": _shared_stub(),
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "yahoo_oauth": types.SimpleNamespace(OAuth2=object),
                "mlb_id_cache": mlb_cache_mod,
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
            },
        )

        class FakeTeam:
            def roster(self):
                return [
                    {
                        "player_id": 7,
                        "name": "Ben Rice",
                        "selected_position": "C",
                        "eligible_positions": ["C", "1B", "Util"],
                        "status": "",
                    }
                ]

        module.get_league_context = lambda: (None, None, None, FakeTeam())

        payload = module.cmd_roster([], as_json=True)

        self.assertEqual(len(payload["players"]), 1)
        self.assertEqual(payload["players"][0]["position"], "C")
        self.assertEqual(payload["players"][0]["eligible_positions"], ["C", "1B", "Util"])
        self.assertEqual(payload["players"][0]["mlb_id"], "mlb-Ben Rice")

    def test_roster_cmd_can_skip_intel_and_returns_dashboard_fields(self):
        mlb_cache_mod = _module("mlb_id_cache")
        mlb_cache_mod.get_mlb_id = lambda name, *args, **kwargs: "mlb-" + str(name)

        shared = _shared_stub()
        enrich_calls = []
        shared.enrich_with_intel = lambda players, *args, **kwargs: enrich_calls.append(len(players))

        module = _load_script(
            "yahoo_fantasy_roster_lite_script_for_test",
            "yahoo-fantasy.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "yahoo_oauth": types.SimpleNamespace(OAuth2=object),
                "mlb_id_cache": mlb_cache_mod,
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
            },
        )

        class FakeTeam:
            def roster(self):
                return [
                    {
                        "player_id": 9,
                        "name": "Ty France",
                        "selected_position": "1B",
                        "eligible_positions": ["1B", "Util"],
                        "editorial_team_abbr": "MIN",
                        "status": "",
                    }
                ]

        module.get_league_context = lambda: (None, None, None, FakeTeam())

        payload = module.cmd_roster(["false"], as_json=True)

        self.assertEqual(enrich_calls, [])
        self.assertEqual(payload["players"][0]["team"], "MIN")
        self.assertEqual(payload["players"][0]["team_abbr"], "MIN")
        self.assertEqual(payload["players"][0]["mlb_team"], "MIN")
        self.assertEqual(payload["players"][0]["positions"], ["1B", "Util"])

    def test_league_settings_accept_string_roster_positions(self):
        shared = _shared_stub()
        shared.cache_get = lambda *_args, **_kwargs: None
        shared.cache_set = lambda cache, key, data: cache.__setitem__(key, (data, 0))

        trace_utils_mod = _module("trace_utils")
        trace_utils_mod.log_trace_event = lambda **_kwargs: None
        trace_utils_mod.monotonic_ms = lambda: 1

        module = _load_script(
            "shared_script_for_test",
            "shared.py",
            {
                "trace_utils": trace_utils_mod,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "yahoo_oauth": types.SimpleNamespace(OAuth2=object),
            },
        )

        class FakeLeague:
            def settings(self):
                return {"scoring_type": "head", "num_teams": 12, "max_weekly_adds": 6}

            def stat_categories(self):
                return []

            def positions(self):
                return ["C", "1B", "BN"]

        module.get_league = lambda: (None, None, FakeLeague())
        module.get_team_key = lambda _lg=None: ""
        module._league_settings_cache = {}

        payload = module.get_league_settings()

        self.assertEqual(
            payload["roster_positions"],
            [
                {"position": "C", "count": 1, "position_type": ""},
                {"position": "1B", "count": 1, "position_type": ""},
                {"position": "BN", "count": 1, "position_type": ""},
            ],
        )

    def test_fetch_mlb_injuries_uses_team_roster_statuses(self):
        trace_utils_mod = _module("trace_utils")
        trace_utils_mod.log_trace_event = lambda **_kwargs: None
        trace_utils_mod.monotonic_ms = lambda: 1

        module = _load_script(
            "shared_injuries_script_for_test",
            "shared.py",
            {
                "trace_utils": trace_utils_mod,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "yahoo_oauth": types.SimpleNamespace(OAuth2=object),
            },
        )

        def fake_mlb_fetch(endpoint):
            if endpoint == "/teams?sportId=1":
                return {"teams": [{"id": 147, "name": "New York Yankees"}]}
            if endpoint == "/teams/147/roster?rosterType=fullRoster&hydrate=person":
                return {
                    "roster": [
                        {
                            "person": {"fullName": "Healthy Guy"},
                            "status": {"code": "A", "description": "Active"},
                        },
                        {
                            "person": {"fullName": "Injured Guy"},
                            "status": {"code": "D60", "description": "Injured 60-Day"},
                        },
                        {
                            "person": {"fullName": "Day To Day Guy"},
                            "status": {"code": "DTD", "description": "Day-To-Day"},
                        },
                        {
                            "person": {"fullName": "Minors Guy"},
                            "status": {"code": "RM", "description": "Reassigned to Minors"},
                        },
                    ]
                }
            return {}

        module.mlb_fetch = fake_mlb_fetch
        module._mlb_injuries_cache = {}

        payload = module.fetch_mlb_injuries()

        self.assertEqual(
            payload,
            [
                {
                    "player": "Injured Guy",
                    "team": "New York Yankees",
                    "team_id": 147,
                    "description": "Injured 60-Day",
                    "status": "D60",
                },
                {
                    "player": "Day To Day Guy",
                    "team": "New York Yankees",
                    "team_id": 147,
                    "description": "Day-To-Day",
                    "status": "DTD",
                },
            ],
        )

    def test_discover_cmd_uses_oauth_symbols_and_returns_league_payload(self):
        yahoo_mod = _module("yahoo_fantasy_api")
        yahoo_oauth_mod = _module("yahoo_oauth")
        yahoo_oauth_mod.OAuth2 = object
        mlb_cache_mod = _module("mlb_id_cache")
        mlb_cache_mod.get_mlb_id = lambda *args, **kwargs: ""

        module = _load_script(
            "yahoo_fantasy_script_for_test",
            "yahoo-fantasy.py",
            {
                "shared": _shared_stub(),
                "yahoo_fantasy_api": yahoo_mod,
                "yahoo_oauth": yahoo_oauth_mod,
                "mlb_id_cache": mlb_cache_mod,
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
            },
        )

        oauth_files = []

        class FakeOAuth2:
            def __init__(self, _key, _secret, from_file=None):
                oauth_files.append(from_file)

            def token_is_valid(self):
                return True

            def refresh_access_token(self):
                return None

        class FakeLeague:
            def settings(self):
                return {"name": "Alpha League", "season": "2026", "num_teams": 12}

            def teams(self):
                return {
                    "422.l.1.t.7": {
                        "is_owned_by_current_login": True,
                        "name": "My Team",
                    }
                }

        class FakeGame:
            def __init__(self, _sc, _game_code):
                return None

            def game_id(self):
                return 422

            def league_ids(self):
                return ["422.l.1"]

            def to_league(self, _lid):
                return FakeLeague()

        module.OAuth2 = FakeOAuth2
        module.yfa = types.SimpleNamespace(Game=FakeGame)

        payload = module.cmd_discover([], as_json=True)

        self.assertEqual(oauth_files, [module.OAUTH_FILE])
        self.assertEqual(payload["game_id"], "422")
        self.assertEqual(len(payload["leagues"]), 1)
        self.assertEqual(payload["leagues"][0]["team_id"], "422.l.1.t.7")

    def test_schedule_helpers_do_not_pass_hydrate_to_statsapi(self):
        season_mod = _load_script(
            "season_manager_script_for_test",
            "season-manager.py",
            {
                "shared": _shared_stub(),
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
                "statsapi": _module("statsapi"),
            },
        )

        schedule_calls = []

        class FakeStatsApi:
            def schedule(self, **kwargs):
                schedule_calls.append(kwargs)
                return []

        class FakeLeague:
            def current_week(self):
                return 1

            def week_date_range(self, _week_num):
                return (date(2026, 3, 2), date(2026, 3, 8))

        class FakeTeam:
            def roster(self):
                return [
                    {
                        "name": "Pitcher A",
                        "player_id": "1",
                        "eligible_positions": ["SP"],
                        "editorial_team_full_name": "Yankees",
                    }
                ]

        season_mod.statsapi = FakeStatsApi()
        season_mod.get_schedule_for_range("2026-03-01", "2026-03-07")
        self.assertNotIn("hydrate", schedule_calls[0])
        schedule_calls.clear()
        season_mod.get_schedule_for_range = lambda _start, _end: []
        season_mod.get_league_context = lambda: (None, None, FakeLeague(), FakeTeam())

        with patch("builtins.print"):
            result = season_mod.cmd_pitcher_matchup([], as_json=True)
        self.assertIn("pitchers", result)
        self.assertEqual(len(schedule_calls), 1)
        self.assertNotIn("hydrate", schedule_calls[0])

    def test_rss_parser_returns_source_warning_metadata(self):
        news_mod = _load_script(
            "news_script_for_test",
            "news.py",
            {"shared": _shared_stub()},
        )

        clean_xml = (
            "\x00garbage<rss><channel><item><title>Player A - News</title>"
            "<link>https://example.com</link><description>Update</description>"
            "<pubDate>Sat, 07 Mar 2026 12:00:00 +0000</pubDate></item></channel></rss>"
        )
        items, warning = news_mod._parse_rss_items(clean_xml, source_name="ESPN MLB")
        self.assertEqual(len(items), 1)
        self.assertIsNone(warning)

        bad_xml = "<rss><channel><item><title>bad</title></channel></rss>"
        items, warning = news_mod._parse_rss_items(bad_xml, source_name="ESPN MLB")
        self.assertEqual(items, [])
        self.assertEqual(warning["source"], "ESPN MLB")
        self.assertEqual(warning["warning_type"], "rss_parse_error")

        news_mod.fetch_aggregated_news = lambda **_kwargs: []
        with patch("builtins.print"):
            news_mod._record_feed_warning("ESPN MLB", "rss_parse_error", "malformed xml")
        payload = news_mod.cmd_news_feed(["--source=espn", "--limit=1"], as_json=True)
        self.assertTrue(payload["warnings"])
        self.assertEqual(payload["warnings"][0]["source"], "ESPN MLB")

    def test_fangraphs_regression_falls_back_when_current_year_parser_breaks(self):
        trace_utils_mod = _module("trace_utils")
        trace_utils_mod.log_trace_event = lambda **_kwargs: None
        trace_utils_mod.monotonic_ms = lambda: 1
        trace_utils_mod.trace_config = lambda: {}
        s3_cache_mod = _module("s3_cache")
        s3_cache_mod.s3_cache = types.SimpleNamespace(get=lambda *_args, **_kwargs: None, put=lambda *_args, **_kwargs: False)

        intel_mod = _load_script(
            "intel_script_for_test",
            "intel.py",
            {
                "shared": _shared_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "trace_utils": trace_utils_mod,
                "s3_cache": s3_cache_mod,
            },
        )

        pybaseball_mod = _module("pybaseball")

        def fake_pitching_stats(year, qual=25):
            if year == intel_mod.YEAR:
                raise ValueError("393 columns passed, passed data had 1 columns")
            return _FakeDataFrame(
                [
                    {
                        "Name": "Fallback Arm",
                        "ERA": 3.1,
                        "FIP": 3.3,
                        "xFIP": 3.4,
                        "BABIP": 0.281,
                        "LOB%": 75.0,
                        "SIERA": 3.45,
                        "IP": 160.0,
                    }
                ]
            )

        pybaseball_mod.pitching_stats = fake_pitching_stats
        saved_pybaseball = sys.modules.get("pybaseball")
        sys.modules["pybaseball"] = pybaseball_mod
        try:
            intel_mod._cache.clear()
            with patch("builtins.print") as mock_print:
                rows = intel_mod._fetch_fangraphs_regression_pitching()
            self.assertIn("fallback arm", rows)
            self.assertEqual(rows["fallback arm"]["data_season"], intel_mod.YEAR - 1)
            self.assertEqual(mock_print.call_count, 0)
        finally:
            if saved_pybaseball is None:
                sys.modules.pop("pybaseball", None)
            else:
                sys.modules["pybaseball"] = saved_pybaseball

    def test_fangraphs_regression_pitching_uses_s3_json_cache_before_pybaseball(self):
        trace_events = []
        trace_utils_mod = _module("trace_utils")
        trace_utils_mod.log_trace_event = lambda **kwargs: trace_events.append(kwargs)
        trace_utils_mod.monotonic_ms = lambda: 1
        trace_utils_mod.trace_config = lambda: {}

        payload = {
            "cached arm": {
                "era": 3.2,
                "fip": 3.4,
                "xfip": 3.5,
                "babip": 0.281,
                "lob_pct": 74.0,
                "siera": 3.44,
                "ip": 111.0,
                "data_season": 2025,
            }
        }
        s3_cache_mod = _module("s3_cache")
        s3_cache_mod.s3_cache = types.SimpleNamespace(
            get=lambda *_args, **_kwargs: json.dumps(payload).encode("utf-8"),
            put=lambda *_args, **_kwargs: False,
        )

        intel_mod = _load_script(
            "intel_script_s3_cache_for_test",
            "intel.py",
            {
                "shared": _shared_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "trace_utils": trace_utils_mod,
                "s3_cache": s3_cache_mod,
            },
        )

        pybaseball_mod = _module("pybaseball")
        pybaseball_mod.pitching_stats = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not call pybaseball"))
        saved_pybaseball = sys.modules.get("pybaseball")
        sys.modules["pybaseball"] = pybaseball_mod
        try:
            intel_mod._cache.clear()
            rows = intel_mod._fetch_fangraphs_regression_pitching()
            self.assertEqual(rows, payload)
            self.assertEqual(trace_events[-1]["cache_layer"], "s3")
            self.assertTrue(trace_events[-1]["cache_hit"])
        finally:
            if saved_pybaseball is None:
                sys.modules.pop("pybaseball", None)
            else:
                sys.modules["pybaseball"] = saved_pybaseball

    def test_fangraphs_regression_pitching_dedupes_parallel_cold_misses(self):
        trace_utils_mod = _module("trace_utils")
        trace_utils_mod.log_trace_event = lambda **_kwargs: None
        trace_utils_mod.monotonic_ms = lambda: 1
        trace_utils_mod.trace_config = lambda: {}
        s3_cache_mod = _module("s3_cache")
        s3_cache_mod.s3_cache = types.SimpleNamespace(get=lambda *_args, **_kwargs: None, put=lambda *_args, **_kwargs: True)

        intel_mod = _load_script(
            "intel_script_singleflight_for_test",
            "intel.py",
            {
                "shared": _shared_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "trace_utils": trace_utils_mod,
                "s3_cache": s3_cache_mod,
            },
        )

        call_count = {"count": 0}
        load_started = threading.Event()

        pybaseball_mod = _module("pybaseball")

        def fake_pitching_stats(_year, qual=25):
            self.assertEqual(qual, 25)
            call_count["count"] += 1
            load_started.set()
            time.sleep(0.05)
            return _FakeDataFrame(
                [
                    {
                        "Name": "Single Flight",
                        "ERA": 3.1,
                        "FIP": 3.3,
                        "xFIP": 3.4,
                        "BABIP": 0.281,
                        "LOB%": 75.0,
                        "SIERA": 3.45,
                        "IP": 160.0,
                    }
                ]
            )

        pybaseball_mod.pitching_stats = fake_pitching_stats
        saved_pybaseball = sys.modules.get("pybaseball")
        sys.modules["pybaseball"] = pybaseball_mod
        try:
            intel_mod._cache.clear()
            results = []
            errors = []

            def worker():
                try:
                    results.append(intel_mod._fetch_fangraphs_regression_pitching())
                except Exception as exc:  # pragma: no cover - diagnostic path
                    errors.append(exc)

            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            self.assertTrue(load_started.wait(timeout=1.0))
            second.start()
            threads = [first, second]
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(call_count["count"], 1)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertIn("single flight", results[0])
        finally:
            if saved_pybaseball is None:
                sys.modules.pop("pybaseball", None)
            else:
                sys.modules["pybaseball"] = saved_pybaseball

    def test_hot_bat_free_agents_endpoint_returns_ranked_players(self):
        yahoo_module = _module("yahoo-fantasy")
        yahoo_module._player_name = lambda player: player["name"]

        class FakeLeague:
            def free_agents(self, pos_type):
                self.last_pos_type = pos_type
                return [
                    {
                        "player_id": 11,
                        "name": "Hot Bat",
                        "editorial_team_abbr": "CLE",
                        "eligible_positions": ["OF"],
                        "percent_owned": 14,
                    },
                    {
                        "player_id": 12,
                        "name": "Cold Bat",
                        "editorial_team_abbr": "PIT",
                        "eligible_positions": ["1B"],
                        "percent_owned": 8,
                    },
                ]

        fake_league = FakeLeague()
        yahoo_module.get_league = lambda: (None, None, fake_league)

        intel_module = _module("intel")
        intel_module._fetch_mlb_game_log = lambda mlb_id, group, days: (
            [
                {
                    "hits": 5,
                    "homeRuns": 2,
                    "runs": 4,
                    "rbi": 6,
                    "stolenBases": 1,
                    "atBats": 14,
                }
            ]
            if mlb_id == 101 and group == "hitting" and days == 7
            else []
        )

        api_module = _load_script(
            "api_server_hot_bat_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "yahoo-fantasy": yahoo_module,
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": intel_module,
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(
                    get_mlb_id=lambda name, *args, **kwargs: {
                        "Hot Bat": 101,
                        "Cold Bat": 102,
                    }.get(name)
                ),
            },
        )

        api_module.request.args = {"count": "1"}
        payload = api_module.api_hot_bat_free_agents()

        self.assertEqual(payload["window"], "Last 7 days")
        self.assertEqual(len(payload["players"]), 1)
        self.assertEqual(fake_league.last_pos_type, "B")
        self.assertEqual(payload["players"][0]["name"], "Hot Bat")
        self.assertEqual(payload["players"][0]["summary"], "5 H, 2 HR, 6 RBI, 1 SB")
        self.assertEqual(payload["players"][0]["team"], "CLE")
        self.assertEqual(payload["players"][0]["team_abbr"], "CLE")
        self.assertEqual(payload["players"][0]["positions"], ["OF"])
        self.assertEqual(payload["players"][0]["percent_owned"], 14)
        self.assertEqual(payload["players"][0]["mlb_id"], 101)

    def test_hot_hand_free_agent_pitchers_endpoint_degrades_to_empty_players(self):
        yahoo_module = _module("yahoo-fantasy")
        yahoo_module._player_name = lambda player: player["name"]

        class FakeLeague:
            def free_agents(self, _pos_type):
                return [
                    {
                        "player_id": 21,
                        "name": "No Data Arm",
                        "editorial_team_abbr": "ATL",
                        "eligible_positions": ["RP"],
                        "percent_owned": 18,
                    }
                ]

        yahoo_module.get_league = lambda: (None, None, FakeLeague())

        intel_module = _module("intel")
        intel_module._fetch_mlb_game_log = lambda *args, **kwargs: []

        api_module = _load_script(
            "api_server_hot_hand_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "yahoo-fantasy": yahoo_module,
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": intel_module,
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: 301),
            },
        )

        api_module.request.args = {"count": "5"}
        payload = api_module.api_hot_hand_free_agent_pitchers()

        self.assertEqual(payload, {"window": "Last 3 appearances", "players": []})

    def test_operator_scoreboard_endpoint_enriches_games_with_matchup_counts(self):
        shared_module = _shared_stub()

        def fake_mlb_fetch(endpoint, *_args, **_kwargs):
            if endpoint.startswith("/teams?sportId=1"):
                return {
                    "teams": [
                        {"id": 10, "name": "New York Yankees", "abbreviation": "NYY"},
                        {"id": 11, "name": "Boston Red Sox", "abbreviation": "BOS"},
                        {"id": 12, "name": "Los Angeles Dodgers", "abbreviation": "LAD"},
                        {"id": 13, "name": "Chicago Cubs", "abbreviation": "CHC"},
                        {"id": 14, "name": "Seattle Mariners", "abbreviation": "SEA"},
                        {"id": 15, "name": "Houston Astros", "abbreviation": "HOU"},
                    ]
                }
            if endpoint.startswith("/schedule?sportId=1&date=2026-03-24"):
                return {
                    "dates": [
                        {
                            "date": "2026-03-24",
                            "games": [
                                {
                                    "gamePk": 11,
                                    "gameDate": "2026-03-24T23:10:00Z",
                                    "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
                                    "linescore": {
                                        "inningHalf": "Top",
                                        "currentInningOrdinal": "6th",
                                        "currentInning": 6,
                                        "outs": 1,
                                        "balls": 2,
                                        "strikes": 1,
                                        "offense": {
                                            "first": {"id": 1},
                                            "third": {"id": 3},
                                            "batter": {"fullName": "Aaron Judge"},
                                        },
                                        "defense": {
                                            "pitcher": {"fullName": "Garrett Crochet"},
                                        },
                                    },
                                    "teams": {
                                        "away": {"score": 4, "team": {"id": 10, "name": "New York Yankees"}},
                                        "home": {"score": 2, "team": {"id": 11, "name": "Boston Red Sox"}},
                                    },
                                },
                                {
                                    "gamePk": 22,
                                    "gameDate": "2026-03-25T00:05:00Z",
                                    "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                                    "teams": {
                                        "away": {"score": 0, "team": {"id": 12, "name": "Los Angeles Dodgers"}},
                                        "home": {"score": 0, "team": {"id": 13, "name": "Chicago Cubs"}},
                                    },
                                },
                                {
                                    "gamePk": 33,
                                    "gameDate": "2026-03-25T01:10:00Z",
                                    "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                                    "teams": {
                                        "away": {"score": 0, "team": {"id": 14, "name": "Seattle Mariners"}},
                                        "home": {"score": 0, "team": {"id": 15, "name": "Houston Astros"}},
                                    },
                                },
                            ],
                        }
                    ]
                }
            return {}

        shared_module.mlb_fetch = fake_mlb_fetch

        yahoo_module = _module("yahoo-fantasy")
        yahoo_module.TEAM_ID = "422.l.1.t.8"
        yahoo_module._extract_team_key = lambda team_data: team_data.get("team_key", "")
        yahoo_module._extract_team_name = lambda team_data: team_data.get("name", "")
        yahoo_module._selected_position = lambda player: player.get("selected_position", "")
        yahoo_module._player_name = lambda player: player.get("name", "")
        yahoo_module._player_team_abbr = lambda player: player.get("editorial_team_abbr", "")

        class FakeRosterTeam:
            def __init__(self, players):
                self._players = players

            def roster(self):
                return list(self._players)

        class FakeLeague:
            def matchups(self):
                return {
                    "fantasy_content": {
                        "league": [
                            None,
                            {
                                "scoreboard": {
                                    "0": {
                                        "matchups": {
                                            "count": 1,
                                            "0": {
                                                "matchup": {
                                                    "0": {
                                                        "teams": {
                                                            "0": {"team_key": "422.l.1.t.8", "name": "Marsh'n Monsters"},
                                                            "1": {"team_key": "422.l.1.t.4", "name": "Bobby Bonilla's IRA"},
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    }
                                }
                            },
                        ]
                    }
                }

            def to_team(self, team_key):
                if team_key == "422.l.1.t.8":
                    return FakeRosterTeam(
                        [
                            {"name": "My Starter", "selected_position": "OF", "editorial_team_abbr": "NYY"},
                            {"name": "My Bench Bat", "selected_position": "BN", "editorial_team_abbr": "CHC"},
                        ]
                    )
                if team_key == "422.l.1.t.4":
                    return FakeRosterTeam(
                        [
                            {"name": "Opp Starter", "selected_position": "Util", "editorial_team_abbr": "BOS"},
                            {"name": "Opp IL Arm", "selected_position": "IL", "editorial_team_abbr": "LAD"},
                        ]
                    )
                return FakeRosterTeam([])

        yahoo_module.get_league = lambda: (None, None, FakeLeague())

        api_module = _load_script(
            "api_server_operator_scoreboard_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": shared_module,
                "yahoo-fantasy": yahoo_module,
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module.sys.modules["shared"] = shared_module
        api_module._mlb_media_links_for_game = lambda game_pk, game_date: {
            "watch_url": "https://www.mlb.com/tv/g" + str(game_pk),
            "watch_links": [
                {"label": "TV Home (YES)", "url": "https://www.mlb.com/tv/g" + str(game_pk) + "/vvideo"}
            ],
            "audio_links": [
                {"label": "Listen Home (WFAN)", "url": "https://www.mlb.com/tv/g" + str(game_pk) + "/vhome"}
            ],
        }
        api_module.request.args = {"date": "2026-03-24"}
        payload = api_module.api_operator_scoreboard()

        self.assertEqual(payload["date"], "2026-03-24")
        self.assertIn("generated_at", payload)
        self.assertEqual(len(payload["games"]), 3)
        self.assertEqual([game["game_id"] for game in payload["games"]], ["mlb-11", "mlb-22", "mlb-33"])

        live_game = next(game for game in payload["games"] if game["game_id"] == "mlb-11")
        self.assertEqual(live_game["status"], "In Progress")
        self.assertEqual(live_game["inning"], "Top 6th")
        self.assertTrue(live_game["game_time"].endswith("+00:00") or "T" in live_game["game_time"])
        self.assertEqual(
            live_game["live_state"],
            {
                "inning_half": "Top",
                "inning_number": 6,
                "outs": 1,
                "balls": 2,
                "strikes": 1,
                "bases": {"first": True, "second": False, "third": True},
                "batter": {"name": "Aaron Judge", "team_abbr": "NYY"},
                "pitcher": {"name": "Garrett Crochet", "team_abbr": "BOS"},
            },
        )
        self.assertEqual(live_game["away_team"]["abbr"], "NYY")
        self.assertEqual(live_game["home_team"]["abbr"], "BOS")
        self.assertEqual(live_game["my_team_name"], "Marsh'n Monsters")
        self.assertEqual(live_game["opponent_team_name"], "Bobby Bonilla's IRA")
        self.assertEqual(live_game["my_active_count"], 1)
        self.assertEqual(live_game["my_inactive_count"], 0)
        self.assertEqual(live_game["opp_active_count"], 1)
        self.assertEqual(live_game["opp_inactive_count"], 0)
        self.assertEqual(live_game["total_relevant_count"], 2)
        self.assertEqual(live_game["my_players"][0]["slot_status"], "active")
        self.assertEqual(live_game["my_players"][0]["team_abbr"], "NYY")
        self.assertEqual(live_game["opp_players"][0]["fantasy_position"], "Util")
        self.assertEqual(live_game["opp_players"][0]["team_abbr"], "BOS")
        self.assertEqual(live_game["media_links"]["watch_url"], "https://www.mlb.com/tv/g11")
        self.assertEqual(live_game["media_links"]["watch_links"][0]["label"], "TV Home (YES)")
        self.assertEqual(live_game["media_links"]["audio_links"][0]["label"], "Listen Home (WFAN)")

        scheduled_game = next(game for game in payload["games"] if game["game_id"] == "mlb-22")
        self.assertNotIn("live_state", scheduled_game)
        self.assertEqual(scheduled_game["my_inactive_count"], 1)
        self.assertEqual(scheduled_game["opp_inactive_count"], 1)
        self.assertEqual(scheduled_game["total_relevant_count"], 2)
        self.assertEqual(scheduled_game["my_players"][0]["team_abbr"], "CHC")
        self.assertEqual(scheduled_game["opp_players"][0]["team_abbr"], "LAD")
        self.assertEqual(scheduled_game["media_links"]["watch_url"], "https://www.mlb.com/tv/g22")

        empty_game = next(game for game in payload["games"] if game["game_id"] == "mlb-33")
        self.assertEqual(empty_game["total_relevant_count"], 0)
        self.assertEqual(empty_game["my_players"], [])
        self.assertEqual(empty_game["opp_players"], [])

    def test_operator_scoreboard_endpoint_filters_single_game(self):
        api_module = _load_script(
            "api_server_operator_scoreboard_filter_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module._DASHBOARD_CACHE = {}
        api_module.request.args = {"date": "2026-03-24", "game_id": "mlb-22"}
        api_module._operator_scoreboard_payload = lambda scoreboard_date: {
            "date": "2026-03-24",
            "generated_at": "2026-03-24T16:00:00Z",
            "games": [
                {"game_id": "mlb-11", "status": "In Progress"},
                {"game_id": "mlb-22", "status": "Scheduled"},
            ],
        }
        payload = api_module.api_operator_scoreboard()

        self.assertEqual(payload["date"], "2026-03-24")
        self.assertEqual(payload["generated_at"], "2026-03-24T16:00:00Z")
        self.assertEqual(payload["games"], [{"game_id": "mlb-22", "status": "Scheduled"}])

    def test_mlb_media_links_endpoint_returns_links_for_game(self):
        api_module = _load_script(
            "api_server_mlb_media_links_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module._mlb_media_links_for_game = lambda game_pk, game_date: {
            "watch_url": "https://www.mlb.com/tv/g" + str(game_pk),
            "watch_links": [
                {"label": "TV Home (SNY)", "url": "https://www.mlb.com/tv/g" + str(game_pk) + "/vvideo"}
            ],
            "audio_links": [
                {"label": "Listen Home (WHSQ 880AM)", "url": "https://www.mlb.com/tv/g" + str(game_pk) + "/vhome"},
                {"label": "Listen Away (KDKA)", "url": "https://www.mlb.com/tv/g" + str(game_pk) + "/vaway"},
            ],
        }

        api_module.request.args = {"game_id": "mlb-823649", "date": "2026-03-26"}
        payload = api_module.api_mlb_media_links()

        self.assertEqual(payload["game_id"], "mlb-823649")
        self.assertEqual(payload["game_pk"], "823649")
        self.assertEqual(payload["media_links"]["watch_url"], "https://www.mlb.com/tv/g823649")
        self.assertEqual(payload["media_links"]["watch_links"][0]["label"], "TV Home (SNY)")
        self.assertEqual(len(payload["media_links"]["audio_links"]), 2)
        self.assertEqual(payload["media_links"]["audio_links"][0]["label"], "Listen Home (WHSQ 880AM)")

    def test_operator_scoreboard_endpoint_degrades_when_fantasy_linkage_fails(self):
        shared_module = _shared_stub()
        shared_module.mlb_fetch = lambda endpoint, *_args, **_kwargs: (
            {
                "teams": [{"id": 10, "name": "New York Yankees", "abbreviation": "NYY"}]
            }
            if endpoint.startswith("/teams?sportId=1")
            else {
                "dates": [
                    {
                        "date": "2026-03-24",
                        "games": [
                            {
                                "gamePk": 44,
                                "gameDate": "2026-03-24T23:10:00Z",
                                "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                                "teams": {
                                    "away": {"score": 0, "team": {"id": 10, "name": "New York Yankees"}},
                                    "home": {"score": 0, "team": {"id": 10, "name": "New York Yankees"}},
                                },
                            }
                        ],
                    }
                ]
            }
        )

        yahoo_module = _module("yahoo-fantasy")
        yahoo_module.TEAM_ID = "422.l.1.t.8"
        yahoo_module.get_league = lambda: (_ for _ in ()).throw(RuntimeError("yahoo down"))
        yahoo_module._extract_team_key = lambda *_args, **_kwargs: ""
        yahoo_module._extract_team_name = lambda *_args, **_kwargs: ""
        yahoo_module._selected_position = lambda *_args, **_kwargs: ""
        yahoo_module._player_name = lambda *_args, **_kwargs: ""
        yahoo_module._player_team_abbr = lambda *_args, **_kwargs: ""

        api_module = _load_script(
            "api_server_operator_scoreboard_degraded_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": shared_module,
                "yahoo-fantasy": yahoo_module,
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module.sys.modules["shared"] = shared_module

        api_module.request.args = {"date": "2026-03-24"}
        payload = api_module.api_operator_scoreboard()

        self.assertEqual(payload["date"], "2026-03-24")
        self.assertEqual(len(payload["games"]), 1)
        self.assertIn("game_time", payload["games"][0])
        self.assertNotIn("live_state", payload["games"][0])
        self.assertEqual(payload["games"][0]["total_relevant_count"], 0)
        self.assertEqual(payload["games"][0]["my_players"], [])
        self.assertEqual(payload["games"][0]["opp_players"], [])
        self.assertEqual(payload["games"][0]["my_team_name"], "")
        self.assertEqual(payload["games"][0]["opponent_team_name"], "")

    def test_operator_scoreboard_target_date_defaults_to_eastern_day(self):
        api_module = _load_script(
            "api_server_operator_scoreboard_date_default_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        class FakeDateTime:
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 3, 25, 21, 30, tzinfo=tz)

        with patch.object(api_module, "datetime", FakeDateTime):
            resolved = api_module._operator_scoreboard_target_date({})

        self.assertEqual(resolved.isoformat(), "2026-03-25")

    def test_operator_scoreboard_endpoint_rejects_invalid_date_param(self):
        api_module = _load_script(
            "api_server_operator_scoreboard_invalid_date_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module.request.args = {"date": "03/25/2026"}
        payload, status = api_module.api_operator_scoreboard()

        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "Invalid date. Expected YYYY-MM-DD."})

    def test_fantasy_scoreboard_summary_returns_compact_matchup_payload(self):
        yahoo_module = _module("yahoo-fantasy")
        yahoo_module.TEAM_ID = "422.l.1.t.8"
        yahoo_module._extract_team_key = lambda team_data: team_data.get("team_key", "")
        yahoo_module._extract_team_name = lambda team_data: team_data.get("name", "")

        class FakeLeague:
            def matchups(self):
                return {
                    "fantasy_content": {
                        "league": [
                            None,
                            {
                                "scoreboard": {
                                    "week": 1,
                                    "0": {
                                        "matchups": {
                                            "count": 2,
                                            "0": {
                                                "matchup": {
                                                    "0": {
                                                        "teams": {
                                                            "0": {"team_key": "422.l.1.t.8", "name": "Marsh'n Monsters"},
                                                            "1": {"team_key": "422.l.1.t.4", "name": "Bobby Bonilla's IRA"},
                                                        }
                                                    },
                                                    "status": "midevent",
                                                    "stat_winners": [
                                                        {"stat_winner": {"winner_team_key": "422.l.1.t.8"}},
                                                        {"stat_winner": {"winner_team_key": "422.l.1.t.4"}},
                                                        {"stat_winner": {"is_tied": 1}},
                                                    ],
                                                }
                                            },
                                            "1": {
                                                "matchup": {
                                                    "0": {
                                                        "teams": {
                                                            "0": {"team_key": "422.l.1.t.2", "name": "Bo Knows"},
                                                            "1": {"team_key": "422.l.1.t.3", "name": "The Boys of Summer"},
                                                        }
                                                    },
                                                    "status": "midevent",
                                                    "stat_winners": [
                                                        {"stat_winner": {"winner_team_key": "422.l.1.t.2"}},
                                                        {"stat_winner": {"winner_team_key": "422.l.1.t.2"}},
                                                    ],
                                                }
                                            },
                                        }
                                    },
                                }
                            },
                        ]
                    }
                }

        yahoo_module.get_league = lambda: (None, None, FakeLeague())

        api_module = _load_script(
            "api_server_fantasy_scoreboard_summary_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": yahoo_module,
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module._DASHBOARD_CACHE = {}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: None
        payload = api_module.api_fantasy_scoreboard_summary()

        self.assertEqual(payload["week"], 1)
        self.assertEqual(
            payload["my_matchup_summary"],
            {
                "my_team_name": "Marsh'n Monsters",
                "opponent_team_name": "Bobby Bonilla's IRA",
                "matchup_status": "midevent",
                "wins": 1,
                "losses": 1,
                "ties": 1,
            },
        )
        self.assertEqual(payload["league_matchups"][0]["score_summary"], "1-1-1")
        self.assertEqual(payload["league_matchups"][1]["score_summary"], "2-0-0")

    def test_operator_scoreboard_summary_excludes_player_arrays_and_live_state(self):
        shared_module = _shared_stub()

        def fake_mlb_fetch(endpoint, *_args, **_kwargs):
            if endpoint.startswith("/teams?sportId=1"):
                return {
                    "teams": [
                        {"id": 10, "name": "New York Yankees", "abbreviation": "NYY"},
                        {"id": 11, "name": "Boston Red Sox", "abbreviation": "BOS"},
                    ]
                }
            if endpoint.startswith("/schedule?sportId=1&date=2026-03-24"):
                return {
                    "dates": [
                        {
                            "date": "2026-03-24",
                            "games": [
                                {
                                    "gamePk": 11,
                                    "gameDate": "2026-03-24T23:10:00Z",
                                    "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
                                    "linescore": {
                                        "inningHalf": "Top",
                                        "currentInningOrdinal": "6th",
                                        "currentInning": 6,
                                        "outs": 1,
                                        "balls": 2,
                                        "strikes": 1,
                                        "offense": {"first": {"id": 1}, "batter": {"fullName": "Aaron Judge"}},
                                        "defense": {"pitcher": {"fullName": "Garrett Crochet"}},
                                    },
                                    "teams": {
                                        "away": {"score": 4, "team": {"id": 10, "name": "New York Yankees"}},
                                        "home": {"score": 2, "team": {"id": 11, "name": "Boston Red Sox"}},
                                    },
                                }
                            ],
                        }
                    ]
                }
            return {}

        shared_module.mlb_fetch = fake_mlb_fetch

        yahoo_module = _module("yahoo-fantasy")
        yahoo_module.TEAM_ID = "422.l.1.t.8"
        yahoo_module._extract_team_key = lambda team_data: team_data.get("team_key", "")
        yahoo_module._extract_team_name = lambda team_data: team_data.get("name", "")
        yahoo_module._selected_position = lambda player: player.get("selected_position", "")
        yahoo_module._player_name = lambda player: player.get("name", "")
        yahoo_module._player_team_abbr = lambda player: player.get("editorial_team_abbr", "")

        class FakeRosterTeam:
            def __init__(self, players):
                self._players = players

            def roster(self):
                return list(self._players)

        class FakeLeague:
            def matchups(self):
                return {
                    "fantasy_content": {
                        "league": [
                            None,
                            {
                                "scoreboard": {
                                    "0": {
                                        "matchups": {
                                            "count": 1,
                                            "0": {
                                                "matchup": {
                                                    "0": {
                                                        "teams": {
                                                            "0": {"team_key": "422.l.1.t.8", "name": "Marsh'n Monsters"},
                                                            "1": {"team_key": "422.l.1.t.4", "name": "Bobby Bonilla's IRA"},
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    }
                                }
                            },
                        ]
                    }
                }

            def to_team(self, team_key):
                if team_key == "422.l.1.t.8":
                    return FakeRosterTeam([{"name": "My Starter", "selected_position": "OF", "editorial_team_abbr": "NYY"}])
                if team_key == "422.l.1.t.4":
                    return FakeRosterTeam([{"name": "Opp Starter", "selected_position": "Util", "editorial_team_abbr": "BOS"}])
                return FakeRosterTeam([])

        yahoo_module.get_league = lambda: (None, None, FakeLeague())

        api_module = _load_script(
            "api_server_operator_scoreboard_summary_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": shared_module,
                "yahoo-fantasy": yahoo_module,
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module._DASHBOARD_CACHE = {}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: None
        api_module.request.args = {"date": "2026-03-24"}
        payload = api_module.api_operator_scoreboard_summary()

        self.assertEqual(payload["date"], "2026-03-24")
        self.assertGreaterEqual(len(payload["games"]), 1)
        game = next(game for game in payload["games"] if game["game_id"] == "mlb-11")
        self.assertEqual(game["my_active_count"], 1)
        self.assertEqual(game["opp_active_count"], 1)
        self.assertEqual(game["total_relevant_count"], 2)
        self.assertNotIn("my_players", game)
        self.assertNotIn("opp_players", game)
        self.assertNotIn("live_state", game)

    def test_operator_scoreboard_game_returns_flat_single_game_detail(self):
        shared_module = _shared_stub()

        def fake_mlb_fetch(endpoint, *_args, **_kwargs):
            if endpoint.startswith("/teams?sportId=1"):
                return {
                    "teams": [
                        {"id": 10, "name": "New York Yankees", "abbreviation": "NYY"},
                        {"id": 11, "name": "Boston Red Sox", "abbreviation": "BOS"},
                        {"id": 12, "name": "Los Angeles Dodgers", "abbreviation": "LAD"},
                        {"id": 13, "name": "Chicago Cubs", "abbreviation": "CHC"},
                    ]
                }
            if endpoint.startswith("/schedule?sportId=1&date=2026-03-24"):
                return {
                    "dates": [
                        {
                            "date": "2026-03-24",
                            "games": [
                                {
                                    "gamePk": 11,
                                    "gameDate": "2026-03-24T23:10:00Z",
                                    "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
                                    "linescore": {
                                        "inningHalf": "Top",
                                        "currentInningOrdinal": "6th",
                                        "currentInning": 6,
                                        "outs": 1,
                                        "balls": 2,
                                        "strikes": 1,
                                        "offense": {"first": {"id": 1}, "batter": {"fullName": "Aaron Judge"}},
                                        "defense": {"pitcher": {"fullName": "Garrett Crochet"}},
                                    },
                                    "teams": {
                                        "away": {"score": 4, "team": {"id": 10, "name": "New York Yankees"}},
                                        "home": {"score": 2, "team": {"id": 11, "name": "Boston Red Sox"}},
                                    },
                                },
                                {
                                    "gamePk": 22,
                                    "gameDate": "2026-03-25T00:05:00Z",
                                    "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                                    "teams": {
                                        "away": {"score": 0, "team": {"id": 12, "name": "Los Angeles Dodgers"}},
                                        "home": {"score": 0, "team": {"id": 13, "name": "Chicago Cubs"}},
                                    },
                                },
                            ],
                        }
                    ]
                }
            return {}

        shared_module.mlb_fetch = fake_mlb_fetch

        yahoo_module = _module("yahoo-fantasy")
        yahoo_module.TEAM_ID = "422.l.1.t.8"
        yahoo_module._extract_team_key = lambda team_data: team_data.get("team_key", "")
        yahoo_module._extract_team_name = lambda team_data: team_data.get("name", "")
        yahoo_module._selected_position = lambda player: player.get("selected_position", "")
        yahoo_module._player_name = lambda player: player.get("name", "")
        yahoo_module._player_team_abbr = lambda player: player.get("editorial_team_abbr", "")

        class FakeRosterTeam:
            def __init__(self, players):
                self._players = players

            def roster(self):
                return list(self._players)

        class FakeLeague:
            def matchups(self):
                return {
                    "fantasy_content": {
                        "league": [
                            None,
                            {
                                "scoreboard": {
                                    "0": {
                                        "matchups": {
                                            "count": 1,
                                            "0": {
                                                "matchup": {
                                                    "0": {
                                                        "teams": {
                                                            "0": {"team_key": "422.l.1.t.8", "name": "Marsh'n Monsters"},
                                                            "1": {"team_key": "422.l.1.t.4", "name": "Bobby Bonilla's IRA"},
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    }
                                }
                            },
                        ]
                    }
                }

            def to_team(self, team_key):
                if team_key == "422.l.1.t.8":
                    return FakeRosterTeam([{"name": "My Starter", "selected_position": "OF", "editorial_team_abbr": "NYY"}])
                if team_key == "422.l.1.t.4":
                    return FakeRosterTeam([{"name": "Opp Starter", "selected_position": "Util", "editorial_team_abbr": "BOS"}])
                return FakeRosterTeam([])

        yahoo_module.get_league = lambda: (None, None, FakeLeague())

        api_module = _load_script(
            "api_server_operator_scoreboard_game_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": shared_module,
                "yahoo-fantasy": yahoo_module,
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module._DASHBOARD_CACHE = {}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: None
        api_module._operator_scoreboard_payload = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not build full day payload"))
        api_module.request.args = {"date": "2026-03-24", "game_id": "mlb-11"}
        payload = api_module.api_operator_scoreboard_game()

        self.assertEqual(payload["date"], "2026-03-24")
        self.assertIn("generated_at", payload)
        self.assertEqual(payload["game"]["game_id"], "mlb-11")
        self.assertIn("my_players", payload["game"])
        self.assertIn("opp_players", payload["game"])
        self.assertIn("live_state", payload["game"])
        self.assertEqual(payload["game"]["my_active_count"], 1)
        self.assertEqual(payload["game"]["opp_active_count"], 1)

    def test_workflow_morning_briefing_uses_lightweight_helpers(self):
        api_module = _load_script(
            "api_server_workflow_morning_briefing_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        calls = []
        cached_payloads = []
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda key, payload: cached_payloads.append((key, payload))
        api_module._safe_injury_report = lambda include_intel=False: calls.append(("injury", include_intel)) or {"injured_active": []}
        api_module._safe_lineup_preview = lambda include_intel=False: calls.append(("lineup", include_intel)) or {"active_off_day": []}
        api_module._safe_whats_new = lambda include_intel=False: calls.append(("whats_new", include_intel)) or {"pending_trades": []}
        api_module._safe_waiver_analyze = lambda pos_type, count, include_intel=False: calls.append(("waiver", pos_type, str(count), include_intel)) or {
            "recommendations": [],
            "weak_categories": [],
        }
        api_module._safe_call = lambda fn, args=None: fn(args or [], as_json=True)
        api_module._synthesize_morning_actions = lambda *_args, **_kwargs: [{"priority": 1, "type": "noop"}]
        api_module.yahoo_fantasy.get_league = lambda: (None, None, types.SimpleNamespace(edit_date=lambda: "2026-04-03"))
        api_module.yahoo_fantasy.cmd_matchup_detail = lambda args=None, as_json=False: {"matchup": "ok"}
        api_module.season_manager.cmd_matchup_strategy = lambda args=None, as_json=False: {"strategy": "ok"}

        payload = api_module.workflow_morning_briefing()

        self.assertEqual(payload["edit_date"], "2026-04-03")
        self.assertEqual(payload["action_items"], [{"priority": 1, "type": "noop"}])
        self.assertEqual(
            calls,
            [
                ("injury", False),
                ("lineup", False),
                ("whats_new", False),
                ("waiver", "B", "5", False),
                ("waiver", "P", "5", False),
            ],
        )
        self.assertEqual(len(cached_payloads), 1)
        self.assertEqual(cached_payloads[0][0][0], "workflow-morning-briefing")
        self.assertIs(cached_payloads[0][1], payload)

    def test_workflow_morning_briefing_uses_cached_payload_when_available(self):
        api_module = _load_script(
            "api_server_workflow_morning_briefing_cache_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        expected = {"action_items": [{"priority": 1}], "cached": True}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: expected
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not write cache"))
        api_module._safe_injury_report = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not compute injury"))
        api_module._safe_lineup_preview = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not compute lineup"))
        api_module._safe_whats_new = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not compute digest"))
        api_module._safe_waiver_analyze = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not compute waivers"))

        payload = api_module.workflow_morning_briefing()

        self.assertIs(payload, expected)

    def test_workflow_waiver_recommendations_uses_lightweight_waiver_helpers(self):
        api_module = _load_script(
            "api_server_workflow_waiver_recommendations_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        calls = []
        api_module.request.args = {"count": "7"}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: None
        api_module._safe_waiver_analyze = lambda pos_type, count, include_intel=False: calls.append((pos_type, str(count), include_intel)) or {
            "recommendations": [],
            "weak_categories": [],
        }
        api_module._safe_roster = lambda include_intel=False: calls.append(("roster", include_intel)) or {"players": []}
        api_module._safe_call = lambda fn, args=None: fn(args or [], as_json=True)
        api_module._synthesize_waiver_pairs = lambda waiver_b, waiver_p: [{"batters": waiver_b, "pitchers": waiver_p}]
        api_module.season_manager.cmd_category_check = lambda args=None, as_json=False: {"categories": []}

        payload = api_module.workflow_waiver_recommendations()

        self.assertEqual(
            calls,
            [("B", "7", False), ("P", "7", False), ("roster", False)],
        )
        self.assertIn("pairs", payload)
        self.assertEqual(payload["category_check"], {"categories": []})
        self.assertEqual(payload["roster"], {"players": []})

    def test_workflow_roster_health_uses_lightweight_roster_and_helpers(self):
        api_module = _load_script(
            "api_server_workflow_roster_health_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        calls = []
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: None
        api_module._safe_injury_report = lambda include_intel=False: calls.append(("injury", include_intel)) or {"injured_active": []}
        api_module._safe_lineup_preview = lambda include_intel=False: calls.append(("lineup", include_intel)) or {"active_off_day": []}
        api_module._safe_roster = lambda include_intel=False: calls.append(("roster", include_intel)) or {"players": [{"name": "Bench Bat"}]}
        api_module._safe_call = lambda fn, args=None: fn(args or [], as_json=True)
        api_module._synthesize_roster_issues = lambda injury, lineup, roster, busts: [{"severity": "info", "count": len(roster.get("players", [])) + len(busts.get("candidates", []))}]
        api_module.intel.cmd_busts = lambda args=None, as_json=False: {"candidates": [{"name": "Bench Bat"}]}

        payload = api_module.workflow_roster_health()

        self.assertEqual(
            calls,
            [("injury", False), ("lineup", False), ("roster", False)],
        )
        self.assertEqual(payload["issues"], [{"severity": "info", "count": 2}])
        self.assertEqual(payload["roster"], {"players": [{"name": "Bench Bat"}]})

    def test_api_value_caches_payload_by_normalized_player_name(self):
        api_module = _load_script(
            "api_server_value_cache_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        calls = []
        cached_payloads = []
        api_module.request.args = {"player_name": " Aaron Judge "}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda key, payload: cached_payloads.append((key, payload))
        api_module.valuations.cmd_value = lambda args=None, as_json=False: calls.append((args or [], as_json)) or {"players": [{"name": args[0]}]}

        payload = api_module.api_value()

        self.assertEqual(calls, [([" Aaron Judge "], True)])
        self.assertEqual(payload["players"], [{"name": " Aaron Judge "}])
        self.assertEqual(cached_payloads[0][0], ("value", "aaron judge"))
        self.assertIs(cached_payloads[0][1], payload)

    def test_api_value_uses_cached_payload_when_available(self):
        api_module = _load_script(
            "api_server_value_cache_hit_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        expected = {"players": [{"name": "Aaron Judge"}], "cached": True}
        api_module.request.args = {"player_name": "Aaron Judge"}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: expected
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not write cache"))
        api_module.valuations.cmd_value = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute value"))

        payload = api_module.api_value()

        self.assertIs(payload, expected)

    def test_api_injury_report_caches_payload(self):
        api_module = _load_script(
            "api_server_injury_report_cache_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        calls = []
        cached_payloads = []
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda key, payload: cached_payloads.append((key, payload))
        api_module.season_manager.cmd_injury_report = lambda args=None, as_json=False: calls.append((args or [], as_json)) or {"injured_active": []}

        payload = api_module.api_injury_report()

        self.assertEqual(calls, [([], True)])
        self.assertEqual(payload, {"injured_active": []})
        self.assertEqual(cached_payloads[0][0][0], "injury-report")
        self.assertIs(cached_payloads[0][1], payload)

    def test_api_injury_report_uses_cached_payload_when_available(self):
        api_module = _load_script(
            "api_server_injury_report_cache_hit_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        expected = {"injured_active": [{"name": "Cached"}], "cached": True}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: expected
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not write cache"))
        api_module.season_manager.cmd_injury_report = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute injury report"))

        payload = api_module.api_injury_report()

        self.assertIs(payload, expected)

    def test_api_waiver_analyze_caches_payload_by_pos_type_and_count(self):
        api_module = _load_script(
            "api_server_waiver_analyze_cache_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        calls = []
        cached_payloads = []
        api_module.request.args = {"pos_type": "p", "count": "7"}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: None
        api_module._dashboard_cache_set = lambda key, payload: cached_payloads.append((key, payload))
        api_module.season_manager.cmd_waiver_analyze = lambda args=None, as_json=False: calls.append((args or [], as_json)) or {"recommendations": []}

        payload = api_module.api_waiver_analyze()

        self.assertEqual(calls, [(["P", "7"], True)])
        self.assertEqual(payload, {"recommendations": []})
        self.assertEqual(cached_payloads[0][0][0], "waiver-analyze")
        self.assertEqual(cached_payloads[0][0][2:], ("P", "7"))
        self.assertIs(cached_payloads[0][1], payload)

    def test_api_waiver_analyze_uses_cached_payload_when_available(self):
        api_module = _load_script(
            "api_server_waiver_analyze_cache_hit_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        expected = {"recommendations": [{"name": "Cached"}], "cached": True}
        api_module.request.args = {"pos_type": "B", "count": "5"}
        api_module._dashboard_cache_get = lambda *_args, **_kwargs: expected
        api_module._dashboard_cache_set = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not write cache"))
        api_module.season_manager.cmd_waiver_analyze = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute waiver analysis"))

        payload = api_module.api_waiver_analyze()

        self.assertIs(payload, expected)

    def test_api_set_lineup_invalidates_team_state_caches(self):
        api_module = _load_script(
            "api_server_set_lineup_cache_invalidation_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        invalidations = []
        api_module.request.get_json = lambda silent=True: {"moves": [{"player_id": "123", "position": "BN"}]}
        api_module._invalidate_team_state_caches = lambda: invalidations.append("team-state")
        api_module.season_manager.cmd_set_lineup = lambda args=None, as_json=False: {"success": True, "moves": args or []}

        payload = api_module.api_set_lineup()

        self.assertEqual(invalidations, ["team-state"])
        self.assertEqual(payload["moves"], ["123:BN"])

    def test_api_set_lineup_skips_team_state_invalidation_on_failure(self):
        api_module = _load_script(
            "api_server_set_lineup_cache_failure_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        invalidations = []
        api_module.request.get_json = lambda silent=True: {"moves": [{"player_id": "123", "position": "BN"}]}
        api_module._invalidate_team_state_caches = lambda: invalidations.append("team-state")
        api_module.season_manager.cmd_set_lineup = lambda args=None, as_json=False: {"success": False, "moves": args or [], "message": "failed"}

        payload = api_module.api_set_lineup()

        self.assertEqual(invalidations, [])
        self.assertFalse(payload["success"])

    def test_api_accept_trade_invalidates_team_state_caches_on_success(self):
        api_module = _load_script(
            "api_server_accept_trade_cache_invalidation_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        invalidations = []
        api_module.request.get_json = lambda silent=True: {"transaction_key": "tx-1"}
        api_module._invalidate_team_state_caches = lambda: invalidations.append("team-state")
        api_module.season_manager.cmd_accept_trade = lambda args=None, as_json=False: {"success": True, "transaction_key": (args or [""])[0]}

        payload = api_module.api_accept_trade()

        self.assertEqual(invalidations, ["team-state"])
        self.assertEqual(payload["transaction_key"], "tx-1")

    def test_auth_status_reports_oauth_and_browser_readiness(self):
        shared_module = _shared_stub()
        shared_module.OAUTH_FILE = "/tmp/yahoo_oauth.json"
        shared_module.YAHOO_OAUTH_BRIDGE_URL = "https://dashboard.example/internal/baseclaw/yahoo_oauth"
        shared_module.YAHOO_OAUTH_BRIDGE_TOKEN = "secret"
        shared_module._read_oauth_file = lambda: {
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "refresh_token": "rt",
            "guid": "guid-1",
        }
        shared_module._oauth_has_tokens = lambda payload: bool(payload.get("access_token") and payload.get("refresh_token"))

        yahoo_browser_module = _module("yahoo_browser")
        yahoo_browser_module.SESSION_FILE = "/tmp/yahoo_session.json"
        yahoo_browser_module.is_session_valid = lambda: {"valid": False, "reason": "No session file found"}
        yahoo_browser_module.get_heartbeat_state = lambda: {"last_ok": None, "last_error": None}

        api_module = _load_script(
            "api_server_auth_status_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": shared_module,
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": yahoo_browser_module,
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )
        api_module.sys.modules["shared"] = shared_module

        payload = api_module.api_auth_status()

        self.assertTrue(payload["oauth_read"]["ready"])
        self.assertTrue(payload["oauth_read"]["bridge_configured"])
        self.assertTrue(payload["oauth_read"]["token_present"])
        self.assertFalse(payload["browser_write"]["ready"])
        self.assertEqual(payload["browser_write"]["reason"], "No session file found")
        self.assertEqual(payload["recommended_action"], "reauthorize_browser_session")

    def test_structured_api_error_classifies_browser_session_expiry(self):
        api_module = _load_script(
            "api_server_structured_error_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": types.SimpleNamespace(
                    is_session_valid=lambda: {"valid": False, "reason": "No session file found"},
                    get_heartbeat_state=lambda: {},
                ),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        payload, status = api_module._json_error(
            RuntimeError("Browser session expired - redirected to login page. Run './yf browser-login' to refresh your session."),
            status=500,
        )

        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "yahoo_browser_session_expired")
        self.assertTrue(payload["action_required"])
        self.assertEqual(payload["action"], "reauthorize_browser_session")
        self.assertEqual(payload["auth_type"], "browser_session")

    def test_mlb_latest_outing_returns_latest_pitching_line_by_player_name(self):
        mlb_data_module = _module("mlb-data")
        mlb_data_module.cmd_player = lambda args, as_json=False: {
            "name": "Logan Webb",
            "position": "Pitcher",
            "team": "San Francisco Giants",
            "bats": "R",
            "throws": "R",
            "age": 29,
            "mlb_id": 657277,
        }

        intel_module = _module("intel")
        intel_module._fetch_mlb_game_log = lambda mlb_id, stat_group, days: [
            {
                "date": "2026-03-24",
                "opponent": "San Diego Padres",
                "summary": "5.0 IP, 6 ER, 7 K, BB",
                "inningsPitched": "5.0",
                "hits": 9,
                "runs": 7,
                "earnedRuns": 6,
                "baseOnBalls": 1,
                "strikeOuts": 7,
                "homeRuns": 0,
                "numberOfPitches": 86,
            }
        ]

        api_module = _load_script(
            "api_server_mlb_latest_outing_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": mlb_data_module,
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": intel_module,
                "news": _module("news"),
                "yahoo_browser": types.SimpleNamespace(
                    is_session_valid=lambda: {"valid": False, "reason": "No session file found"},
                    get_heartbeat_state=lambda: {},
                ),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda name, *_args, **_kwargs: 657277 if name == "Logan Webb" else None),
            },
        )

        api_module.request.args = {"player_name": "Logan Webb"}
        payload = api_module.api_mlb_latest_outing()

        self.assertEqual(payload["player_name"], "Logan Webb")
        self.assertEqual(payload["mlb_id"], 657277)
        self.assertEqual(payload["stat_group"], "pitching")
        self.assertEqual(payload["outing"]["date"], "2026-03-24")
        self.assertEqual(payload["outing"]["opponent"], "San Diego Padres")
        self.assertEqual(payload["outing"]["innings_pitched"], "5.0")
        self.assertEqual(payload["outing"]["earned_runs"], 6)
        self.assertEqual(payload["outing"]["strikeouts"], 7)
        self.assertEqual(payload["summary"], "5.0 IP, 6 ER, 7 K, BB")

    def test_mlb_latest_outing_rejects_invalid_date(self):
        api_module = _load_script(
            "api_server_mlb_latest_outing_invalid_date_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": types.SimpleNamespace(
                    is_session_valid=lambda: {"valid": False, "reason": "No session file found"},
                    get_heartbeat_state=lambda: {},
                ),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module.request.args = {"player_name": "Logan Webb", "date": "03/24/2026"}
        payload, status = api_module.api_mlb_latest_outing()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Invalid date. Expected YYYY-MM-DD.")

    def test_operator_live_state_normalizes_mid_inning_transitions(self):
        api_module = _load_script(
            "api_server_operator_live_state_mid_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        game = {
            "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
            "linescore": {
                "inningState": "Middle",
                "currentInning": 7,
                "outs": 3,
                "balls": 2,
                "strikes": 1,
                "offense": {
                    "first": {"id": 1},
                    "batter": {"fullName": "Should Clear"},
                },
                "defense": {
                    "pitcher": {"fullName": "Should Also Clear"},
                },
            },
        }

        payload = api_module._operator_live_state(game, "NYY", "BOS")

        self.assertEqual(
            payload,
            {
                "inning_half": "Mid",
                "inning_number": 7,
                "outs": 3,
                "balls": None,
                "strikes": None,
                "bases": {"first": False, "second": False, "third": False},
                "batter": {"name": "", "team_abbr": ""},
                "pitcher": {"name": "", "team_abbr": ""},
            },
        )

    def test_best_available_skips_refresh_for_free_agent_list(self):
        valuations_module = _module("valuations")
        valuations_module.load_all = lambda: ([], [], "test")
        valuations_module.get_player_by_name = lambda *_args, **_kwargs: []

        draft_module = _load_script(
            "draft_assistant_for_best_available_test",
            "draft-assistant.py",
            {
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "valuations": valuations_module,
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: 77),
                "shared": types.SimpleNamespace(
                    enrich_with_intel=lambda *_args, **_kwargs: None,
                    get_team_key=lambda *_args, **_kwargs: "422.l.1.t.7",
                    get_connection=lambda *_args, **_kwargs: object(),
                    LEAGUE_ID="422.l.1",
                ),
                "yahoo-fantasy": types.SimpleNamespace(
                    get_available_players=lambda *_args, **_kwargs: [],
                    _infer_pos_type=lambda *_args, **_kwargs: "B",
                ),
            },
        )

        refresh_calls = []

        class FakeDraftAssistant:
            def __init__(self):
                pass

            def refresh(self):
                refresh_calls.append(True)

            def get_available(self, pos_type, count):
                self.last = (pos_type, count)
                return [{"name": "Fast FA", "eligible_positions": ["OF"], "z_score": 2.75}]

        draft_module.DraftAssistant = FakeDraftAssistant
        payload = draft_module.cmd_best_available(["B", "1", "false"], as_json=True)

        self.assertEqual(refresh_calls, [])
        self.assertEqual(payload["players"][0]["name"], "Fast FA")
        self.assertEqual(payload["players"][0]["z_score"], 2.75)

    def test_search_uses_cached_available_players_pool_instead_of_live_free_agent_scans(self):
        yahoo_api_mod = _module("yahoo_fantasy_api")
        yahoo_oauth_mod = _module("yahoo_oauth")
        yahoo_oauth_mod.OAuth2 = object
        mlb_cache_mod = _module("mlb_id_cache")
        mlb_cache_mod.get_mlb_id = lambda name, *_args, **_kwargs: 668901 if name == "Mark Vientos" else None

        available_calls = []
        shared = _shared_stub()
        shared.get_available_players = None

        module = _load_script(
            "yahoo_fantasy_search_cache_test",
            "yahoo-fantasy.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": yahoo_api_mod,
                "yahoo_oauth": yahoo_oauth_mod,
                "mlb_id_cache": mlb_cache_mod,
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
            },
        )

        def fake_available_players(pos_type="B", count=None):
            available_calls.append((pos_type, count))
            return [
                {
                    "name": "Mark Vientos",
                    "player_id": "123",
                    "positions": ["3B", "Util"],
                    "eligible_positions": ["3B", "Util"],
                    "percent_owned": 87,
                    "team": "NYM",
                    "team_abbr": "NYM",
                    "status": "",
                    "mlb_id": 668901,
                    "availability_type": "free_agent",
                },
                {
                    "name": "Other Player",
                    "player_id": "456",
                    "eligible_positions": ["OF"],
                    "percent_owned": 12,
                },
            ]

        module.get_available_players = fake_available_players
        module.get_league = lambda: (_ for _ in ()).throw(AssertionError("live Yahoo league scan should not run"))

        payload = module.cmd_search(["Mark", "Vientos"], as_json=True)

        self.assertEqual(available_calls, [("ALL", None)])
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["name"], "Mark Vientos")
        self.assertEqual(payload["results"][0]["player_id"], "123")
        self.assertEqual(payload["results"][0]["eligible_positions"], ["3B", "Util"])
        self.assertEqual(payload["results"][0]["team"], "NYM")
        self.assertEqual(payload["results"][0]["mlb_id"], 668901)

    def test_rankings_endpoint_bypasses_cached_empty_payload_after_recovery(self):
        valuations_module = _module("valuations")
        call_count = {"count": 0}

        def fake_cmd_rankings(_args, as_json=False, enrich=True):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return {"players": [], "pos_type": "B", "source": "json"}
            return {
                "players": [
                    {"rank": 1, "name": "Recovered Bat", "team": "ATL", "pos": "OF", "z_score": 4.2}
                ],
                "pos_type": "B",
                "source": "csv",
            }

        valuations_module.cmd_rankings = fake_cmd_rankings
        valuations_module.ensure_projections = lambda *_args, **_kwargs: {}

        api_module = _load_script(
            "api_server_rankings_cache_recovery_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": valuations_module,
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module.request.args = {"pos_type": "B", "count": "3"}
        first_payload = api_module.api_rankings()
        second_payload = api_module.api_rankings()

        self.assertEqual(first_payload["players"], [])
        self.assertEqual(second_payload["players"][0]["name"], "Recovered Bat")
        self.assertEqual(call_count["count"], 2)

    def test_rankings_cache_skips_empty_grouped_all_payloads(self):
        api_module = _load_script(
            "api_server_rankings_empty_grouped_cache_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": _module("valuations"),
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        empty_grouped = {
            "groups": {
                "B": {"players": [], "buckets": {"C": [], "1B": []}, "pos_type": "B", "source": "json"},
                "P": {"players": [], "pos_type": "P", "source": "json"},
            },
            "pos_type": "ALL",
        }

        api_module._set_cached_rankings("ALL", 3, True, ["C", "1B"], empty_grouped, True)
        cached = api_module._get_cached_rankings("ALL", 3, True, ["C", "1B"], True)

        self.assertIsNone(cached)

    def test_live_rankings_endpoint_uses_live_variant_cache_and_handler(self):
        valuations_module = _module("valuations")
        call_count = {"count": 0}

        def fake_cmd_rankings_live(_args, as_json=False, enrich=True):
            call_count["count"] += 1
            return {
                "players": [
                    {
                        "rank": 1,
                        "name": "Sandy Alcantara",
                        "team": "MIA",
                        "pos": "SP",
                        "z_score": 8.8,
                        "projection_z_score": 4.2,
                        "season_z_score": 11.1,
                        "delta_z": 6.9,
                    }
                ],
                "pos_type": "P",
                "source": "live_blend",
                "weights": {"season_to_date": 0.7, "projection": 0.3},
            }

        valuations_module.cmd_rankings = lambda *_args, **_kwargs: {"players": []}
        valuations_module.cmd_rankings_live = fake_cmd_rankings_live
        valuations_module.ensure_projections = lambda *_args, **_kwargs: {}

        api_module = _load_script(
            "api_server_live_rankings_for_test",
            "api-server.py",
            {
                "flask": _flask_stub(),
                "position_batching": _position_batching_stub(),
                "trace_utils": _trace_utils_stub(),
                "shared": _shared_stub(),
                "yahoo-fantasy": _module("yahoo-fantasy"),
                "draft-assistant": _module("draft-assistant"),
                "mlb-data": _module("mlb-data"),
                "season-manager": _module("season-manager"),
                "valuations": valuations_module,
                "history": _module("history"),
                "intel": _module("intel"),
                "news": _module("news"),
                "yahoo_browser": _module("yahoo_browser"),
                "player_universe": _module("player_universe"),
                "draft_sim": _module("draft_sim"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: None),
            },
        )

        api_module.request.args = {"pos_type": "P", "count": "5"}
        first_payload = api_module.api_rankings_live()
        second_payload = api_module.api_rankings_live()

        self.assertEqual(first_payload["players"][0]["name"], "Sandy Alcantara")
        self.assertEqual(second_payload["players"][0]["delta_z"], 6.9)
        self.assertEqual(call_count["count"], 1)

    def test_free_agents_combines_waivers_and_true_free_agents(self):
        mlb_cache_mod = _module("mlb_id_cache")
        mlb_cache_mod.get_mlb_id = lambda name, *args, **kwargs: {
            "Waiver Bat": 101,
            "True Free Agent": 102,
        }.get(name, 0)

        shared = _shared_stub()
        shared.enrich_with_intel = lambda *_args, **_kwargs: None
        shared.enrich_with_trends = lambda *_args, **_kwargs: None

        module = _load_script(
            "yahoo_fantasy_available_script_for_test",
            "yahoo-fantasy.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "yahoo_oauth": types.SimpleNamespace(OAuth2=object),
                "mlb_id_cache": mlb_cache_mod,
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
            },
        )

        class FakeLeague:
            def waivers(self):
                return [
                    {
                        "player_id": "1",
                        "name": "Waiver Bat",
                        "eligible_positions": ["OF", "Util"],
                        "percent_owned": 12,
                        "editorial_team_abbr": "SEA",
                    }
                ]

            def free_agents(self, pos_type):
                if pos_type == "B":
                    return [
                        {
                            "player_id": "2",
                            "name": "True Free Agent",
                            "eligible_positions": ["1B", "Util"],
                            "percent_owned": 4,
                            "editorial_team_abbr": "PIT",
                        }
                    ]
                return []

        module.get_league = lambda: (None, None, FakeLeague())

        payload = module.cmd_free_agents(["B", "5", "false"], as_json=True)

        self.assertEqual(len(payload["players"]), 2)
        self.assertEqual(payload["players"][0]["availability_type"], "free_agent")
        self.assertEqual(payload["players"][1]["availability_type"], "waiver")
        self.assertEqual(payload["players"][0]["team"], "PIT")
        self.assertEqual(payload["players"][0]["team_abbr"], "PIT")
        self.assertEqual(payload["players"][1]["team"], "SEA")
        self.assertEqual(payload["players"][1]["team_abbr"], "SEA")

    def test_best_available_uses_combined_available_pool(self):
        valuations_module = _module("valuations")
        valuations_module.load_all = lambda: ([], [], "test")
        valuations_module.get_player_by_name = lambda name, *_args, **_kwargs: [
            {"Z_Final": 3.5}
        ] if name == "Waiver Arm" else [{"Z_Final": 2.1}]

        yahoo_module = _module("yahoo-fantasy")
        yahoo_module._infer_pos_type = lambda positions: "P" if "SP" in positions else "B"
        yahoo_module.get_available_players = lambda pos_type, _count=None: [
            {
                "player_id": "9",
                "name": "Waiver Arm",
                "eligible_positions": ["SP", "P"],
                "team": "ATL",
                "availability_type": "waiver",
            },
            {
                "player_id": "10",
                "name": "Free Arm",
                "eligible_positions": ["SP", "P"],
                "team": "MIA",
                "availability_type": "free_agent",
            },
        ] if pos_type == "P" else []

        draft_module = _load_script(
            "draft_assistant_available_pool_test",
            "draft-assistant.py",
            {
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "valuations": valuations_module,
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: 88),
                "shared": types.SimpleNamespace(
                    enrich_with_intel=lambda *_args, **_kwargs: None,
                    get_team_key=lambda *_args, **_kwargs: "422.l.1.t.7",
                    get_connection=lambda *_args, **_kwargs: object(),
                    LEAGUE_ID="422.l.1",
                ),
                "yahoo-fantasy": yahoo_module,
            },
        )

        class FakeDraftAssistant:
            def __init__(self):
                self.drafted_players = set()

            def get_available(self, _pos_type="P", _limit=20):
                return [
                    {
                        "player_id": "9",
                        "name": "Waiver Arm",
                        "eligible_positions": ["SP", "P"],
                        "team": "ATL",
                        "availability_type": "waiver",
                        "z_score": 3.5,
                    },
                    {
                        "player_id": "10",
                        "name": "Free Arm",
                        "eligible_positions": ["SP", "P"],
                        "team": "MIA",
                        "availability_type": "free_agent",
                        "z_score": 2.1,
                    },
                ]

        draft_module.DraftAssistant = FakeDraftAssistant
        payload = draft_module.cmd_best_available(["P", "2", "false"], as_json=True)

        self.assertEqual(payload["players"][0]["name"], "Waiver Arm")
        self.assertEqual(payload["players"][0]["availability_type"], "waiver")
        self.assertEqual(payload["players"][0]["team"], "ATL")
        self.assertEqual(payload["players"][0]["team_abbr"], "ATL")


if __name__ == "__main__":
    unittest.main()
