import json
import tempfile
import unittest
from pathlib import Path

from vantage.gen_paper_dat import _cadence_run, _prom_value


class PaperDataTests(unittest.TestCase):
    def test_prom_value_selects_active_registry_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.prom"
            path.write_text(
                "vantage_entered_view 0\n"
                "vantage_entered_view 123\n"
            )
            self.assertEqual(_prom_value(path, "vantage_entered_view", None), 123)

    def test_cadence_uses_median_node_delta_and_request_divisor(self):
        spec = {
            "metric": "network_messages_received_total",
            "label": 'type="ConsensusRequest"',
            "counter_units_per_opportunity": 2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "summary.json").write_text(json.dumps({"window_s": 10}))
            for node, delta in enumerate((100, 200, 300)):
                metric = 'network_messages_received_total{type="ConsensusRequest"}'
                (run / f"baseline-node-{node}.prom").write_text(f"{metric} 50\n")
                (run / f"final-node-{node}.prom").write_text(f"{metric} {50 + delta}\n")
            result = _cadence_run(run, spec)

        self.assertEqual(result["nodes"], 3)
        self.assertEqual(result["median_counter_delta"], 200)
        self.assertEqual(result["opportunities_per_s"], 10)


if __name__ == "__main__":
    unittest.main()
