import json
import tempfile
import unittest
from pathlib import Path

import yaml

from wanbench.record import record, record_matrix


class RecordTests(unittest.TestCase):
    def test_embedded_effective_config_is_recorded_without_source_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sweep = root / "sweep.json"
            sweep.write_text(json.dumps({
                "protocol": "vantage",
                "nodes": 4,
                "rates": [400],
                "warmup_s": 0,
                "window_s": 1,
                "drop_tolerance_pct": 5,
                "points": [{"run_id": "run-test", "rate": 400}],
                "status": "completed",
                "effective_config": {
                    "image": "registry/vantage@sha256:abc",
                    "instance_type": "m5d.2xlarge",
                    "region": "eu-north-1",
                    "protocol": "vantage",
                    "nodes": 4,
                },
            }))
            out = record(str(sweep), dest=str(root / "recorded"))
            config = yaml.safe_load((out / "config.yaml").read_text())
            readme_exists = (out / "README.md").is_file()
        self.assertEqual(config["image"], "registry/vantage@sha256:abc")
        self.assertTrue(readme_exists)

    def test_legacy_sweep_requires_an_explicit_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = Path(tmp) / "sweep.json"
            sweep.write_text(json.dumps({"points": []}))
            with self.assertRaisesRegex(ValueError, "pass --config"):
                record(str(sweep), dest=str(Path(tmp) / "recorded"))

    def test_completed_matrix_is_promoted_without_raw_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            variant = source / "n-4" / "vantage"
            variant.mkdir(parents=True)
            matrix = {
                "schema_version": 1,
                "kind": "committee-matrix",
                "name": "paper-scaling",
                "status": "completed",
                "committees": [
                    {"nodes": 4, "status": "completed", "output": "n-4"}
                ],
            }
            (source / "matrix.json").write_text(json.dumps(matrix))
            (source / "points.csv").write_text("nodes,variant\n4,vantage\n")
            (source / "points.json").write_text(
                json.dumps([{"nodes": 4, "variant": "vantage"}]))
            (source / "README.md").write_text("# Paper scaling\n")
            (source / "n-4" / "campaign.json").write_text(json.dumps({
                "status": "completed",
                "definition": {"image": "vantage@sha256:abc"},
                "variants": [{"name": "vantage", "output": "vantage"}],
            }))
            (variant / "sweep.json").write_text(json.dumps({
                "status": "completed",
                "points": [{"rate": 100, "tps_median": 100}],
            }))
            archive = source / "n-4" / "prometheus-tsdb.tar.gz"
            archive.write_bytes(b"raw database")
            config = root / "config.yaml"
            config.write_text("name: paper-scaling\n")

            out = record_matrix(
                str(source / "matrix.json"), str(config),
                str(root / "recorded"), "20260811")
            measurements = json.loads((out / "measurements.json").read_text())

            self.assertEqual(out.name, "paper-scaling-20260811")
            self.assertEqual(
                measurements["committees"][0]["variants"][0]["sweep"]
                ["points"][0]["tps_median"],
                100,
            )
            self.assertEqual(
                measurements["raw_prometheus_archives"][0]["bytes"],
                len(b"raw database"),
            )
            self.assertFalse((out / "prometheus-tsdb.tar.gz").exists())
            self.assertTrue((out / "SHA256SUMS").is_file())

    def test_incomplete_matrix_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = Path(tmp) / "matrix.json"
            matrix.write_text(json.dumps({
                "kind": "committee-matrix", "status": "running"}))
            with self.assertRaisesRegex(ValueError, "not completed"):
                record_matrix(str(matrix), dest=str(Path(tmp) / "recorded"))


if __name__ == "__main__":
    unittest.main()
