import importlib.util
import pathlib
import sys
import types
import unittest
from datetime import date
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


class ReliabilityHardeningTests(unittest.TestCase):
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

        intel_mod = _load_script(
            "intel_script_for_test",
            "intel.py",
            {
                "shared": _shared_stub(),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda *_args, **_kwargs: ""),
                "trace_utils": trace_utils_mod,
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


if __name__ == "__main__":
    unittest.main()
