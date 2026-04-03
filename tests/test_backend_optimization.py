import importlib.util
import pathlib
import sys
import types
import unittest


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


class _FakeDataFrame:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for idx, row in enumerate(self._rows):
            yield idx, row


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
    shared.TEAM_ALIASES = {}
    shared.get_trend_lookup = lambda *args, **kwargs: {}
    shared.enrich_with_intel = lambda *args, **kwargs: None
    shared.enrich_with_trends = lambda *args, **kwargs: None
    shared.fetch_mlb_injuries = lambda *args, **kwargs: []
    return shared


class BackendOptimizationTests(unittest.TestCase):
    def test_get_available_players_uses_cached_pool(self):
        shared = _shared_stub()
        calls = {"waivers": 0, "free_agents": 0}

        class FakeLeague:
            def waivers(self):
                calls["waivers"] += 1
                return [{"player_id": "1", "name": "Waiver Bat", "eligible_positions": ["1B"], "percent_owned": 12}]

            def free_agents(self, group):
                calls["free_agents"] += 1
                return [{"player_id": "2", "name": "Free Agent", "eligible_positions": [group], "percent_owned": 9}]

        module = _load_script(
            "yahoo_fantasy_cache_test",
            "yahoo-fantasy.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "yahoo_oauth": types.SimpleNamespace(OAuth2=object),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda name, *args, **kwargs: "mlb-" + str(name)),
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
            },
        )
        module.get_league = lambda: (None, None, FakeLeague())

        first = module.get_available_players("B", 1)
        second = module.get_available_players("B", 1)

        self.assertEqual(first[0]["name"], "Free Agent")
        self.assertEqual(second[0]["name"], "Free Agent")
        self.assertEqual(calls["waivers"], 1)
        self.assertEqual(calls["free_agents"], 1)

    def test_cmd_lineup_optimize_can_skip_intel_for_json_preview(self):
        enrich_calls = []
        shared = _shared_stub()
        shared.enrich_with_intel = lambda players, *args, **kwargs: enrich_calls.append(len(players))

        module = _load_script(
            "season_manager_lineup_lite_test",
            "season-manager.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "statsapi": None,
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda name, *args, **kwargs: "mlb-" + str(name)),
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
                "valuations": types.SimpleNamespace(
                    get_player_zscore=lambda name: {"tier": "Solid", "z_final": 1.2}
                ),
            },
        )

        class FakeTeam:
            def roster(self):
                return [
                    {
                        "player_id": "10",
                        "name": "Off Day Starter",
                        "selected_position": "1B",
                        "eligible_positions": ["1B", "Util"],
                        "editorial_team_abbr": "OFF",
                        "status": "",
                    },
                    {
                        "player_id": "11",
                        "name": "Bench Bat",
                        "selected_position": "BN",
                        "eligible_positions": ["1B", "Util"],
                        "editorial_team_abbr": "PLAY",
                        "status": "",
                    },
                ]

        module.get_league_context = lambda: (None, None, None, FakeTeam())
        module.get_todays_schedule = lambda: [{"away_name": "Play", "home_name": "Elsewhere"}]

        saved = sys.modules.get("valuations")
        sys.modules["valuations"] = types.SimpleNamespace(
            get_player_zscore=lambda name: {"tier": "Solid", "z_final": 1.2}
        )
        try:
            payload = module.cmd_lineup_optimize([], as_json=True, include_intel=False)
        finally:
            if saved is None:
                sys.modules.pop("valuations", None)
            else:
                sys.modules["valuations"] = saved

        self.assertEqual(enrich_calls, [])
        self.assertEqual(len(payload["active_off_day"]), 1)
        self.assertEqual(len(payload["bench_playing"]), 1)
        self.assertEqual(len(payload["suggested_swaps"]), 1)

    def test_cmd_lineup_optimize_matches_abbreviations_against_full_schedule_names(self):
        shared = _shared_stub()
        shared.TEAM_ALIASES = {
            "NYY": "New York Yankees",
            "BOS": "Boston Red Sox",
        }
        shared.normalize_team_name = lambda value: (value or "").strip().lower()

        module = _load_script(
            "season_manager_lineup_abbr_test",
            "season-manager.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "statsapi": None,
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda name, *args, **kwargs: "mlb-" + str(name)),
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
                "valuations": types.SimpleNamespace(
                    get_player_zscore=lambda name: {"tier": "Solid", "z_final": 1.2}
                ),
            },
        )

        class FakeTeam:
            def roster(self):
                return [
                    {
                        "player_id": "10",
                        "name": "Active Yankee",
                        "selected_position": "OF",
                        "eligible_positions": ["OF", "Util"],
                        "editorial_team_abbr": "NYY",
                        "status": "",
                    },
                    {
                        "player_id": "11",
                        "name": "Bench Red Sox",
                        "selected_position": "BN",
                        "eligible_positions": ["OF", "Util"],
                        "editorial_team_abbr": "BOS",
                        "status": "",
                    },
                ]

        module.get_league_context = lambda: (None, None, None, FakeTeam())
        module.get_todays_schedule = lambda: [{"away_name": "New York Yankees", "home_name": "Boston Red Sox"}]

        saved = sys.modules.get("valuations")
        sys.modules["valuations"] = types.SimpleNamespace(
            get_player_zscore=lambda name: {"tier": "Solid", "z_final": 1.2}
        )
        try:
            payload = module.cmd_lineup_optimize([], as_json=True, include_intel=False)
        finally:
            if saved is None:
                sys.modules.pop("valuations", None)
            else:
                sys.modules["valuations"] = saved

        self.assertEqual(payload["games_today"], 1)
        self.assertEqual(payload["active_off_day"], [])
        self.assertEqual(payload["bench_playing"][0]["name"], "Bench Red Sox")

    def test_cmd_lineup_optimize_resolves_team_fields_from_sparse_live_roster_shape(self):
        shared = _shared_stub()
        shared.TEAM_ALIASES = {
            "PHI": "Philadelphia Phillies",
            "ATL": "Atlanta Braves",
        }
        shared.normalize_team_name = lambda value: (value or "").strip().lower()
        shared.mlb_fetch = lambda endpoint: (
            {
                "people": [
                    {"id": "mlb-Live Shape Starter", "currentTeam": {"id": 143, "name": "Philadelphia Phillies"}},
                    {"id": "mlb-Live Shape Bench", "currentTeam": {"id": 144, "name": "Atlanta Braves"}},
                ]
            }
            if endpoint.startswith("/people?personIds=")
            else {
                "teams": [
                    {"id": 143, "name": "Philadelphia Phillies", "abbreviation": "PHI"},
                    {"id": 144, "name": "Atlanta Braves", "abbreviation": "ATL"},
                ]
            }
        )

        module = _load_script(
            "season_manager_lineup_live_shape_test",
            "season-manager.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "statsapi": None,
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda name, *args, **kwargs: "mlb-" + str(name)),
                "yahoo_browser": types.SimpleNamespace(
                    is_scope_error=lambda *_args, **_kwargs: False,
                    write_method=lambda *_args, **_kwargs: None,
                ),
                "valuations": types.SimpleNamespace(
                    get_player_zscore=lambda name: {"tier": "Solid", "z_final": 1.2}
                ),
            },
        )

        class FakeTeam:
            def roster(self):
                return [
                    {
                        "player_id": "10",
                        "name": "Live Shape Starter",
                        "selected_position": "C",
                        "eligible_positions": ["C", "Util"],
                        "status": "",
                    },
                    {
                        "player_id": "11",
                        "name": "Live Shape Bench",
                        "selected_position": "BN",
                        "eligible_positions": ["1B", "Util"],
                        "status": "",
                    },
                ]

        module.get_league_context = lambda: (None, None, None, FakeTeam())
        module.get_todays_schedule = lambda: [{"away_name": "Philadelphia Phillies", "home_name": "Atlanta Braves"}]

        saved = sys.modules.get("valuations")
        sys.modules["valuations"] = types.SimpleNamespace(
            get_player_zscore=lambda name: {"tier": "Solid", "z_final": 1.2}
        )
        try:
            payload = module.cmd_lineup_optimize([], as_json=True, include_intel=False)
        finally:
            if saved is None:
                sys.modules.pop("valuations", None)
            else:
                sys.modules["valuations"] = saved

        self.assertEqual(payload["games_today"], 1)
        self.assertEqual(payload["active_off_day"], [])
        self.assertEqual(payload["bench_playing"][0]["name"], "Live Shape Bench")
        self.assertEqual(payload["bench_playing"][0]["team"], "ATL")

    def test_cmd_best_available_uses_lightweight_cached_lookup_path(self):
        shared = _shared_stub()
        enrich_calls = []
        shared.enrich_with_intel = lambda players, *args, **kwargs: enrich_calls.append(len(players))

        yahoo_stub = types.SimpleNamespace(
            get_available_players=lambda pos_type, count=None: [
                {"player_id": "1", "name": "Low Z", "eligible_positions": ["1B"], "team": "AAA", "percent_owned": 20},
                {"player_id": "2", "name": "High Z", "eligible_positions": ["1B"], "team": "BBB", "percent_owned": 10},
            ]
        )

        module = _load_script(
            "draft_assistant_lite_test",
            "draft-assistant.py",
            {
                "shared": shared,
                "yahoo_fantasy_api": _module("yahoo_fantasy_api"),
                "mlb_id_cache": types.SimpleNamespace(get_mlb_id=lambda name, *args, **kwargs: "mlb-" + str(name)),
                "yahoo-fantasy": yahoo_stub,
            },
        )

        module.DraftAssistant = lambda: (_ for _ in ()).throw(AssertionError("DraftAssistant should not be used"))
        module._get_cached_valuation_lookups = lambda: {
            "B": {"high z": 4.0, "low z": 1.0},
            "P": {},
            "source": "cached",
        }

        payload = module.cmd_best_available(["B", "2", "false"], as_json=True)

        self.assertEqual([player["name"] for player in payload["players"]], ["High Z", "Low Z"])
        self.assertEqual(enrich_calls, [])


if __name__ == "__main__":
    unittest.main()
