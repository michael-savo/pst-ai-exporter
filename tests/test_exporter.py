from __future__ import annotations

import hashlib
import json
import mailbox
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from pst_ai_exporter.exporter import (
    ExportError,
    ExportOptions,
    _extract_pst,
    export_source,
    export_sources,
    html_to_text,
)


class ExporterTests(unittest.TestCase):
    def _sample_message(self) -> EmailMessage:
        message = EmailMessage()
        message["From"] = "Alice Example <alice@example.com>"
        message["To"] = "Bob Example <bob@example.com>, team@example.com"
        message["Cc"] = "Carol Example <carol@example.com>"
        message["Subject"] = "Quarterly project update"
        message["Date"] = "Tue, 15 Jul 2025 14:30:00 -0400"
        message["Message-ID"] = "<update-123@example.com>"
        message["In-Reply-To"] = "<earlier@example.com>"
        message["References"] = "<first@example.com> <earlier@example.com>"
        message["X-Archive-Tag"] = "important"
        message.set_content("Hello team,\n\nThe project is on schedule.\n")
        message.add_alternative(
            "<html><body><p>Hello team,</p><p>The <b>project</b> is on schedule.</p></body></html>",
            subtype="html",
        )
        message.add_attachment(
            b"first report",
            maintype="application",
            subtype="octet-stream",
            filename="report.txt",
        )
        message.add_attachment(
            b"second report",
            maintype="application",
            subtype="octet-stream",
            filename="report.txt",
        )
        return message

    def _libpst_calendar_message(
        self,
        boundary: str,
        dtstamp: str,
        generated_filename: str,
        include_inline_part: bool = True,
    ) -> bytes:
        calendar = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "PRODID:LibPST v0.6.76\n"
            "METHOD:REQUEST\n"
            "BEGIN:VEVENT\n"
            "UID:0x1234\n"
            f"DTSTAMP:{dtstamp}\n"
            "DTSTART;VALUE=DATE-TIME:20250715T183000Z\n"
            "DTEND;VALUE=DATE-TIME:20250715T190000Z\n"
            "SUMMARY:Evidence review\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        inline_part = (
            f"--{boundary}\n"
            'Content-Type: text/calendar; charset="utf-8"\n'
            "\n"
            f"{calendar}"
            if include_inline_part
            else ""
        )
        return (
            "From: Alice Example <alice@example.com>\n"
            "To: Bob Example <bob@example.com>\n"
            "Subject: Calendar test\n"
            "Date: Tue, 15 Jul 2025 14:30:00 -0400\n"
            "Message-ID: <calendar-test@example.com>\n"
            "MIME-Version: 1.0\n"
            f'Content-Type: multipart/mixed; boundary="{boundary}"\n'
            "\n"
            f"--{boundary}\n"
            'Content-Type: text/plain; charset="utf-8"\n'
            "\n"
            "Calendar invitation attached.\n"
            f"{inline_part}"
            f"--{boundary}\n"
            f'Content-Type: text/calendar; charset="utf-8"; name="{generated_filename}"\n'
            f'Content-Disposition: attachment; filename="{generated_filename}"\n'
            "\n"
            f"{calendar}"
            f"--{boundary}--\n"
        ).encode("utf-8")

    def test_exports_jsonl_markdown_headers_and_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            inbox = source / "Top of Outlook data file" / "Inbox"
            inbox.mkdir(parents=True)
            (inbox / "1.eml").write_bytes(self._sample_message().as_bytes())
            output = root / "export"

            manifest = export_source(
                source,
                output,
                ExportOptions(include_html=True, keep_eml=True),
            )

            self.assertEqual(manifest["counts"]["messages_exported"], 1)
            self.assertEqual(manifest["counts"]["attachments"], 2)
            record = json.loads((output / "emails.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["subject"], "Quarterly project update")
            self.assertEqual(record["from"][0]["address"], "alice@example.com")
            self.assertEqual(len(record["to"]), 2)
            self.assertEqual(record["cc"][0]["address"], "carol@example.com")
            self.assertEqual(record["date"]["utc"], "2025-07-15T18:30:00+00:00")
            self.assertEqual(record["source"]["folder"], "Top of Outlook data file/Inbox")
            self.assertIn("The project is on schedule.", record["body"]["text"])
            self.assertIn("<html>", record["body"]["html"])
            self.assertIn(
                {"name": "X-Archive-Tag", "value": "important"},
                record["headers"],
            )

            attachments = record["attachments"]
            self.assertEqual([item["saved_filename"] for item in attachments], ["report.txt", "report-2.txt"])
            first_attachment = output / attachments[0]["path"]
            self.assertEqual(first_attachment.read_bytes(), b"first report")
            self.assertEqual(
                attachments[0]["sha256"],
                hashlib.sha256(b"first report").hexdigest(),
            )
            markdown = (output / "markdown" / f"{record['id']}.md").read_text(encoding="utf-8")
            self.assertIn("# Quarterly project update", markdown)
            self.assertIn("## Attachments", markdown)
            source_id = record["source"]["id"]
            self.assertTrue(
                (
                    output
                    / "eml"
                    / source_id
                    / "Top of Outlook data file"
                    / "Inbox"
                    / "1.eml"
                ).exists()
            )

    def test_html_only_message_gets_clean_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "message.eml"
            message = EmailMessage()
            message["Subject"] = "HTML only"
            message.set_content(
                "<html><head><style>.hidden {display:none}</style></head>"
                "<body><h1>Status</h1><p>All systems ready.</p></body></html>",
                subtype="html",
            )
            source.write_bytes(message.as_bytes())

            export_source(source, root / "export")
            record = json.loads((root / "export" / "emails.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["body"]["text"], "Status\n\nAll systems ready.")
            self.assertNotIn("hidden", record["body"]["text"])

    def test_previous_export_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "message.eml"
            source.write_bytes(self._sample_message().as_bytes())
            output = root / "export"
            export_source(source, output)

            with self.assertRaises(ExportError):
                export_source(source, output)

            manifest = export_source(source, output, ExportOptions(overwrite=True))
            self.assertEqual(manifest["counts"]["messages_exported"], 1)

    def test_html_converter_ignores_script_and_style(self) -> None:
        result = html_to_text(
            "<style>bad</style><p>Hello <b>there</b>.</p><script>worse</script><p>Next</p>"
        )
        self.assertEqual(result, "Hello there.\n\nNext")

    def test_missing_pst_reader_does_not_replace_previous_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "archive.pst"
            source.write_bytes(b"dummy PST data")
            output = root / "export"
            output.mkdir()
            manifest = output / "manifest.json"
            manifest.write_text("previous export\n", encoding="utf-8")

            with patch("pst_ai_exporter.exporter.shutil.which", return_value=None):
                with self.assertRaises(ExportError):
                    export_source(source, output, ExportOptions(overwrite=True))

            self.assertEqual(manifest.read_text(encoding="utf-8"), "previous export\n")

    def test_source_directory_cannot_also_be_the_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "message.eml").write_bytes(self._sample_message().as_bytes())

            with self.assertRaises(ExportError):
                export_source(source, source, ExportOptions(overwrite=True))

            self.assertTrue((source / "message.eml").exists())

    def test_combines_sources_and_marks_exact_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self._sample_message().as_bytes()
            first = root / "first.eml"
            second = root / "second.eml"
            first.write_bytes(raw)
            second.write_bytes(raw)
            output = root / "combined"

            manifest = export_sources([first, second, first], output)
            records = [
                json.loads(line)
                for line in (output / "emails.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(manifest["counts"]["sources"], 2)
            self.assertEqual(manifest["counts"]["messages_exported"], 2)
            self.assertEqual(manifest["counts"]["unique_message_content"], 1)
            self.assertEqual(manifest["counts"]["exact_duplicate_messages"], 1)
            self.assertNotEqual(records[0]["id"], records[1]["id"])
            self.assertIsNone(records[0]["duplicate_of"])
            self.assertEqual(records[1]["duplicate_of"], records[0]["id"])
            self.assertEqual(
                {record["source"]["archive"] for record in records},
                {"first.eml", "second.eml"},
            )

    def test_exports_google_style_mbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mbox_path = root / "vault-export.mbox"
            first = self._sample_message()
            first["X-Gmail-Labels"] = "Inbox,Important"
            second = EmailMessage()
            second["From"] = "sender@example.com"
            second["To"] = "recipient@example.com"
            second["Subject"] = "Second MBOX message"
            second["Date"] = "Wed, 16 Jul 2025 09:00:00 +0000"
            second.set_content("A second message from the Vault export.")

            source_mbox = mailbox.mbox(str(mbox_path), create=True)
            try:
                source_mbox.add(first)
                source_mbox.add(second)
                source_mbox.flush()
            finally:
                source_mbox.close()

            output = root / "export"
            manifest = export_source(mbox_path, output)
            records = [
                json.loads(line)
                for line in (output / "emails.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(manifest["counts"]["messages_exported"], 2)
            self.assertEqual(manifest["sources"][0]["type"], "mbox")
            self.assertTrue(all(record["source"]["type"] == "mbox" for record in records))
            self.assertIn(
                {"name": "X-Gmail-Labels", "value": "Inbox,Important"},
                records[0]["headers"],
            )

    def test_combines_multiple_pst_extractions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_pst = root / "vault-part-1.pst"
            second_pst = root / "vault-part-2.pst"
            first_pst.write_bytes(b"first PST placeholder")
            second_pst.write_bytes(b"second PST placeholder")
            raw = self._sample_message().as_bytes()

            def fake_extract(pst_path, destination, options, progress):
                inbox = destination / "Top of Outlook data file" / "Inbox"
                inbox.mkdir(parents=True)
                message = raw.replace(
                    b"Quarterly project update",
                    f"Message from {pst_path.name}".encode("ascii"),
                )
                (inbox / "1.eml").write_bytes(message)

            output = root / "combined"
            with patch(
                "pst_ai_exporter.exporter._readpst_executable",
                return_value="/usr/local/bin/readpst",
            ), patch("pst_ai_exporter.exporter._extract_pst", side_effect=fake_extract):
                manifest = export_sources([first_pst, second_pst], output)

            records = [
                json.loads(line)
                for line in (output / "emails.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(manifest["counts"]["sources"], 2)
            self.assertEqual(manifest["counts"]["messages_exported"], 2)
            self.assertEqual(
                {record["source"]["archive"] for record in records},
                {"vault-part-1.pst", "vault-part-2.pst"},
            )
            self.assertTrue(all(record["source"]["type"] == "pst" for record in records))

    def test_readpst_is_forced_to_true_serial_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pst_path = root / "archive.pst"
            destination = root / "readpst-output"
            pst_path.write_bytes(b"PST placeholder")
            destination.mkdir()

            completed = type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )()
            with patch(
                "pst_ai_exporter.exporter.shutil.which",
                return_value="/usr/bin/readpst",
            ), patch(
                "pst_ai_exporter.exporter.subprocess.run",
                return_value=completed,
            ) as run:
                _extract_pst(
                    pst_path,
                    destination,
                    ExportOptions(jobs=0),
                    lambda _message: None,
                )

            command = run.call_args.args[0]
            self.assertEqual(
                command[:8],
                ["/usr/bin/readpst", "-e", "-8", "-q", "-t", "e", "-j", "0"],
            )

    def test_rejects_unsafe_readpst_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "message.eml"
            source.write_bytes(self._sample_message().as_bytes())

            with self.assertRaisesRegex(ExportError, "Unsafe ReadPST parallelism"):
                export_source(source, root / "export", ExportOptions(jobs=1))

    def test_source_and_message_ids_are_stable_across_computers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self._sample_message().as_bytes()
            records = []

            for computer in ("computer-a", "computer-b"):
                source = root / computer / "same-message.eml"
                source.parent.mkdir()
                source.write_bytes(raw)
                output = root / f"{computer}-output"
                export_source(source, output)
                records.append(
                    json.loads((output / "emails.jsonl").read_text(encoding="utf-8"))
                )

            self.assertEqual(records[0]["source"]["id"], records[1]["source"]["id"])
            self.assertEqual(records[0]["id"], records[1]["id"])
            self.assertEqual(
                records[0]["semantic_sha256"], records[1]["semantic_sha256"]
            )

    def test_libpst_calendar_artifacts_have_stable_semantic_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pst_path = root / "calendar.pst"
            pst_path.write_bytes(b"stable PST evidence")
            raw_messages = iter(
                [
                    self._libpst_calendar_message(
                        "boundary-run-one", "20260803T120000Z", "i111111.ics"
                    ),
                    self._libpst_calendar_message(
                        "boundary-run-two", "20260803T130000Z", "i222222.ics"
                    ),
                ]
            )

            def fake_extract(_pst_path, destination, _options, _progress):
                inbox = destination / "Personal folders" / "Inbox"
                inbox.mkdir(parents=True)
                (inbox / "1.eml").write_bytes(next(raw_messages))

            records = []
            manifests = []
            with patch(
                "pst_ai_exporter.exporter._readpst_executable",
                return_value="/usr/bin/readpst",
            ), patch(
                "pst_ai_exporter.exporter._readpst_version_output",
                return_value="ReadPST / LibPST v0.6.76",
            ), patch(
                "pst_ai_exporter.exporter._extract_pst",
                side_effect=fake_extract,
            ):
                for run_number in (1, 2):
                    output = root / f"run-{run_number}"
                    manifests.append(export_source(pst_path, output))
                    records.append(
                        json.loads(
                            (output / "emails.jsonl").read_text(encoding="utf-8")
                        )
                    )

            first, second = records
            self.assertEqual(first["id"], second["id"])
            self.assertNotEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(first["semantic_sha256"], second["semantic_sha256"])
            self.assertEqual(
                [item["semantic_sha256"] for item in first["attachments"]],
                [item["semantic_sha256"] for item in second["attachments"]],
            )
            self.assertNotEqual(
                [item["sha256"] for item in first["attachments"]],
                [item["sha256"] for item in second["attachments"]],
            )
            self.assertEqual(
                manifests[0]["counts"]["attachments_with_semantic_normalization"],
                2,
            )
            self.assertEqual(manifests[0]["pst_reader"]["parallel_jobs"], 0)

    def test_unpaired_calendar_attachment_is_not_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "attached-calendar.eml"
            source.write_bytes(
                self._libpst_calendar_message(
                    "original-boundary",
                    "20250715T183000Z",
                    "i111111.ics",
                    include_inline_part=False,
                )
            )

            export_source(source, root / "export")
            record = json.loads(
                (root / "export" / "emails.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(len(record["attachments"]), 1)
            attachment = record["attachments"][0]
            self.assertIsNone(attachment["semantic_normalization"])
            self.assertEqual(attachment["semantic_sha256"], attachment["sha256"])

    def test_semantic_duplicates_survive_libpst_randomization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_pst = root / "part-one.pst"
            second_pst = root / "part-two.pst"
            first_pst.write_bytes(b"first PST")
            second_pst.write_bytes(b"second PST")

            def fake_extract(pst_path, destination, _options, _progress):
                inbox = destination / "Personal folders" / "Inbox"
                inbox.mkdir(parents=True)
                if pst_path == first_pst:
                    raw = self._libpst_calendar_message(
                        "boundary-one", "20260803T120000Z", "i111111.ics"
                    )
                else:
                    raw = self._libpst_calendar_message(
                        "boundary-two", "20260803T130000Z", "i222222.ics"
                    )
                (inbox / "1.eml").write_bytes(raw)

            output = root / "combined"
            with patch(
                "pst_ai_exporter.exporter._readpst_executable",
                return_value="/usr/bin/readpst",
            ), patch(
                "pst_ai_exporter.exporter._readpst_version_output",
                return_value="ReadPST / LibPST v0.6.76",
            ), patch(
                "pst_ai_exporter.exporter._extract_pst",
                side_effect=fake_extract,
            ):
                manifest = export_sources([first_pst, second_pst], output)

            records = [
                json.loads(line)
                for line in (output / "emails.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(manifest["counts"]["unique_message_content"], 1)
            self.assertEqual(manifest["counts"]["exact_duplicate_messages"], 1)
            self.assertIsNone(records[0]["duplicate_of"])
            self.assertEqual(records[1]["duplicate_of"], records[0]["id"])

    def test_discovers_mixed_sources_in_a_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "vault-export"
            source.mkdir()
            (source / "loose-message.eml").write_bytes(self._sample_message().as_bytes())

            mbox_path = source / "mail-part-1.mbox"
            mbox_message = EmailMessage()
            mbox_message["From"] = "vault@example.com"
            mbox_message["To"] = "review@example.com"
            mbox_message["Subject"] = "Discovered MBOX message"
            mbox_message.set_content("Found inside the selected export folder.")
            source_mbox = mailbox.mbox(str(mbox_path), create=True)
            try:
                source_mbox.add(mbox_message)
                source_mbox.flush()
            finally:
                source_mbox.close()

            manifest = export_source(source, root / "combined")

            self.assertEqual(manifest["counts"]["sources"], 2)
            self.assertEqual(manifest["counts"]["messages_exported"], 2)
            self.assertEqual(
                {item["type"] for item in manifest["sources"]},
                {"mbox", "eml_directory"},
            )

    def test_ignores_macos_appledouble_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "copied-from-mac"
            source.mkdir()
            raw = self._sample_message().as_bytes()
            (source / "message.eml").write_bytes(raw)
            (source / "._message.eml").write_bytes(b"AppleDouble metadata")
            hidden_directory = source / "._metadata"
            hidden_directory.mkdir()
            (hidden_directory / "other.eml").write_bytes(b"AppleDouble metadata")

            output = root / "export"
            manifest = export_source(source, output)
            records = [
                json.loads(line)
                for line in (output / "emails.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(manifest["counts"]["sources"], 1)
            self.assertEqual(manifest["counts"]["messages_exported"], 1)
            self.assertEqual(records[0]["subject"], "Quarterly project update")

    def test_rejects_explicit_appledouble_sidecar_as_only_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sidecar = root / "._archive.pst"
            sidecar.write_bytes(b"AppleDouble metadata")

            with self.assertRaisesRegex(ExportError, "AppleDouble"):
                export_source(sidecar, root / "export")

    def test_replaces_invalid_unicode_without_omitting_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "message.eml"
            source.write_bytes(self._sample_message().as_bytes())
            parsed_message = self._sample_message()
            original_raw_items = parsed_message.raw_items

            def raw_items_with_surrogate():
                yield from original_raw_items()
                yield "X-Damaged-Text", "invalid-\udce0-value"

            parsed_message.raw_items = raw_items_with_surrogate  # type: ignore[method-assign]

            with patch(
                "pst_ai_exporter.exporter.BytesParser.parsebytes",
                return_value=parsed_message,
            ):
                manifest = export_source(source, root / "export")

            record = json.loads(
                (root / "export" / "emails.jsonl").read_text(encoding="utf-8")
            )
            repaired_header = next(
                item for item in record["headers"] if item["name"] == "X-Damaged-Text"
            )

            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["counts"]["messages_failed"], 0)
            self.assertEqual(
                manifest["counts"]["messages_with_unicode_replacements"], 1
            )
            self.assertEqual(manifest["counts"]["unicode_replacement_characters"], 1)
            self.assertEqual(repaired_header["value"], "invalid-\N{REPLACEMENT CHARACTER}-value")
            self.assertEqual(record["warnings"][0]["type"], "invalid_unicode_replaced")
            self.assertEqual(record["warnings"][0]["replacement_count"], 1)


if __name__ == "__main__":
    unittest.main()
