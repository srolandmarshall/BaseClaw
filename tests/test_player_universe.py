import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import player_universe as pu


class PlayerUniverseTests(unittest.TestCase):
    def test_build_player_universe_dedupes_and_merges_source_tags(self):
        yahoo = types.SimpleNamespace(
            cmd_taken_players=lambda _args, as_json=False: {
                "players": [
                    {
                        "player_id": "100",
                        "name": "Mookie Betts",
                        "eligible_positions": ["2B", "OF"],
                        "status": "",
                        "percent_owned": 99,
                        "mlb_id": 605141,
                    }
                ]
            },
            cmd_waivers=lambda _args, as_json=False: {
                "players": [
                    {
                        "player_id": "200",
                        "name": "Rookie Arm",
                        "eligible_positions": ["SP"],
                        "status": "W",
                        "percent_owned": 22,
                        "mlb_id": 999001,
                    },
                    {
                        "player_id": "100",
                        "name": "Mookie Betts",
                        "eligible_positions": ["2B", "OF"],
                        "status": "",
                        "percent_owned": 99,
                        "mlb_id": 605141,
                    },
                ]
            },
            cmd_free_agents=lambda args, as_json=False: (
                {
                    "players": [
                        {
                            "player_id": "300",
                            "name": "Bench Bat",
                            "eligible_positions": ["1B"],
                            "status": "",
                            "percent_owned": 8,
                            "mlb_id": 999002,
                        },
                    ]
                }
                if args and args[0] == "B"
                else {
                    "players": [
                        {
                            "player_id": "200",
                            "name": "Rookie Arm",
                            "eligible_positions": ["SP"],
                            "status": "",
                            "percent_owned": 18,
                            "mlb_id": 999001,
                        },
                    ]
                }
            ),
        )

        payload = pu.build_player_universe(
            yahoo_fantasy=yahoo,
            league_context_fetcher=lambda: {"num_teams": 12},
            max_players_per_group=25,
        )

        players = payload["players"]
        self.assertEqual(len(players), 3)
        by_id = {p["player_id"]: p for p in players}

        self.assertEqual(by_id["100"]["source_tags"], ["taken_players", "waivers"])
        self.assertEqual(by_id["200"]["source_tags"], ["waivers", "free_agents_p"])
        self.assertEqual(by_id["200"]["status"], "W")
        self.assertEqual(by_id["200"]["pos_type"], "P")
        self.assertEqual(by_id["300"]["source_tags"], ["free_agents_b"])
        self.assertEqual(by_id["300"]["pos_type"], "B")
        self.assertEqual(payload["league_context"], {"num_teams": 12})


if __name__ == "__main__":
    unittest.main()
