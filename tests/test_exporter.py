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


if __name__ == "__main__":
    unittest.main()
