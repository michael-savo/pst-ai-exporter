from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pst_ai_exporter.batch import (
    _completed_export,
    _discover_psts,
    _safe_output_name,
    main,
)


class BatchExporterTests(unittest.TestCase):
    def test_discovers_real_psts_and_ignores_appledouble_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            real = root / "nested" / "Archive.PST"
            real.write_bytes(b"real")
            (root / "nested" / "._Archive.PST").write_bytes(b"metadata")
            (root / "message.eml").write_bytes(b"email")

            self.assertEqual(_discover_psts(root), [real])

    def test_output_name_is_windows_safe_and_content_identified(self) -> None:
        name = _safe_output_name(Path("Ed: Mailbox?.pst"), "a" * 64)

        self.assertEqual(name, "Ed_Mailbox__aaaaaaaaaaaa")

    def test_completed_export_requires_matching_single_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            manifest = {
                "status": "complete",
                "sources": [{"sha256": "expected"}],
            }
            (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            self.assertEqual(_completed_export(output, "expected"), manifest)
            self.assertIsNone(_completed_export(output, "different"))

    def test_batch_writes_auditable_manifest_for_concurrent_source_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "input"
            source_root.mkdir()
            (source_root / "one.pst").write_bytes(b"one")
            (source_root / "two.pst").write_bytes(b"two")
            output_root = root / "output"

            def successful_export(*args, **kwargs):
                return 0, {
                    "status": "complete",
                    "counts": {
                        "messages_found": 1,
                        "messages_exported": 1,
                        "messages_failed": 0,
                        "attachments": 2,
                        "attachment_bytes": 3,
                        "attachments_with_semantic_normalization": 0,
                    },
                }

            with (
                patch(
                    "pst_ai_exporter.batch._sha256_file",
                    side_effect=["a" * 64, "b" * 64],
                ),
                patch(
                    "pst_ai_exporter.batch._run_source",
                    side_effect=successful_export,
                ),
            ):
                result = main(
                    [
                        str(source_root),
                        "--output",
                        str(output_root),
                        "--workers",
                        "2",
                    ]
                )

            manifest = json.loads(
                (output_root / "batch-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result, 0)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["readpst_jobs_per_source"], 0)
            self.assertEqual(manifest["counts"]["pst_files_discovered"], 2)
            self.assertEqual(manifest["counts"]["sources_complete"], 2)
            self.assertEqual(manifest["counts"]["messages_exported"], 2)
            self.assertEqual(manifest["counts"]["attachments"], 4)


if __name__ == "__main__":
    unittest.main()
