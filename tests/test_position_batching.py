import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import position_batching as pb


class PositionBatchingTests(unittest.TestCase):
    def test_safe_bool(self):
        self.assertTrue(pb.safe_bool("1"))
        self.assertTrue(pb.safe_bool("true"))
        self.assertTrue(pb.safe_bool("YES"))
        self.assertFalse(pb.safe_bool("0"))
        self.assertFalse(pb.safe_bool(None, default=False))
        self.assertTrue(pb.safe_bool(None, default=True))

    def test_parse_hitter_positions_csv_normalizes_and_dedupes(self):
        result = pb.parse_hitter_positions_csv(" c,1b, OF,1B,,util ")
        self.assertEqual(result, ["C", "1B", "OF", "UTIL"])

    def test_parse_hitter_positions_csv_rejects_invalid_tokens(self):
        with self.assertRaises(ValueError):
            pb.parse_hitter_positions_csv("C,SP")

    def test_best_available_position_tokens_expands_multi_position_values(self):
        player = {"positions": ["1B/OF", " UTIL ", "C;1B"]}
        self.assertEqual(pb.best_available_position_tokens(player), ["1B", "OF", "UTIL", "C"])

    def test_normalize_hitter_payload_filters_and_groups(self):
        payload = {
            "pos_type": "B",
            "players": [
                {"name": "Catcher", "pos": "C"},
                {"name": "Corner", "pos": "1B/OF"},
                {"name": "Unknown", "pos": ""},
            ],
        }

        normalized = pb.normalize_hitter_payload(
            payload,
            "players",
            ["C", "1B", "UTIL"],
            True,
            pb.ranking_position_tokens,
        )

        names = [row["name"] for row in normalized["players"]]
        self.assertEqual(names, ["Catcher", "Corner", "Unknown"])
        self.assertEqual([row["name"] for row in normalized["buckets"]["C"]], ["Catcher"])
        self.assertEqual([row["name"] for row in normalized["buckets"]["1B"]], ["Corner"])
        self.assertEqual([row["name"] for row in normalized["buckets"]["UTIL"]], ["Catcher", "Corner"])

    def test_grouped_all_payload_shape(self):
        grouped = pb.grouped_all_payload({"players": [1]}, {"players": [2]})
        self.assertEqual(grouped["pos_type"], "ALL")
        self.assertIn("B", grouped["groups"])
        self.assertIn("P", grouped["groups"])
        self.assertEqual(grouped["groups"]["B"]["players"], [1])
        self.assertEqual(grouped["groups"]["P"]["players"], [2])


if __name__ == "__main__":
    unittest.main()
