# PST AI Exporter

PST AI Exporter combines one or many Outlook PST, MBOX, and EML sources entirely
on your computer into:

- `emails.jsonl`: one complete email record per line for bulk AI analysis
- `markdown/`: one readable Markdown file per email
- `attachments/<email-id>/`: attachments grouped with their source message
- `manifest.json`: source hashes, per-archive counts, folders, duplicates, and date range
- `errors.jsonl`: any message that could not be exported, so omissions are visible

The exporter preserves From, To, CC, BCC, Reply-To, Subject, Date, Message-ID,
thread references, the original headers, source archive, folder location, clean
text, optional HTML, attachment metadata, and SHA-256 hashes. It does not upload
mail or call an AI service.

Files beginning with `._` are macOS AppleDouble metadata sidecars and are ignored
when archives copied from a Mac are processed on Windows or Linux. If malformed
text contains non-serializable Unicode surrogate code points, the exporter keeps
the message, replaces only those code points with `U+FFFD`, and records a warning
on the message plus aggregate replacement counts in `manifest.json`. The
`content_sha256` remains the hash of the unmodified raw message bytes.

## Privacy

Email processing stays local. Source archives, individual EML files, and generated
export folders are excluded by `.gitignore` to reduce the risk of accidentally
committing private mail to this repository. Always choose an output folder outside
the project directory and review files before sharing them.

All selected archives feed one combined corpus. Exact duplicates are retained
and marked with `duplicate_of`, allowing analysis tools to count or omit them
without losing evidence that the message appeared in more than one export.

## Requirements

- Python 3.10 or newer
- `readpst` from libpst for PST input

On macOS with Homebrew:

```bash
brew install libpst
```

On Debian or Ubuntu:

```bash
sudo apt install pst-utils
```

MBOX and EML sources can be exported without `readpst`.

## Windows with WSL

PST processing on Windows is supported through Ubuntu in WSL. Keep the evidence
under a Windows path such as `C:\USC-Uber`; Ubuntu sees that folder at
`/mnt/c/USC-Uber`.

Install the required packages in Ubuntu:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv pst-utils
```

Clone and install the exporter:

```bash
mkdir -p /mnt/c/USC-Uber/tools
cd /mnt/c/USC-Uber/tools
git clone https://github.com/michael-savo/pst-ai-exporter.git
cd pst-ai-exporter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For a machine with 24 CPU cores, start with 16 extraction jobs so Windows and
the storage device retain capacity for filesystem work:

```bash
pst-ai-exporter \
  "/mnt/c/USC-Uber/input/Ed Telmany" \
  --output "/mnt/c/USC-Uber/output/Ed_Telmany_Complete_v2" \
  --jobs 16
```

Choose a new output directory for every evidentiary run. Do not use
`--overwrite` on a preserved export.

## Easiest use on macOS

After installing `libpst`, double-click `PST AI Exporter.command`. Choose either
individual PST, MBOX, or EML files, or select a folder containing a complete
Vault export. Then choose a destination folder. The launcher creates one
combined export folder and opens it when processing finishes.

macOS may ask for confirmation the first time you open the launcher because it
is a local script.

## Command-line use

No Python package installation is required. From this project folder:

```bash
./pst-ai-exporter "/path/to/archive.pst" --output "/path/to/archive-export"
```

Combine several Vault or Outlook exports into one corpus:

```bash
./pst-ai-exporter \
  "/path/to/export-part-1.pst" \
  "/path/to/export-part-2.pst" \
  "/path/to/gmail-export.mbox" \
  --output "/path/to/combined-email-corpus"
```

Passing a directory discovers its PST, MBOX, MBX, and loose EML files
recursively. The same source selected twice is processed only once.

An optional editable package installation is also supported:

```bash
python3 -m pip install -e .
```

Useful options:

```text
--format both|jsonl|markdown  Choose output formats; default is both
--include-html               Keep original HTML in JSONL
--keep-eml                   Keep extracted EML files for audit/reprocessing
--include-deleted            Include recoverable deleted PST items
--jobs N                     Limit parallel PST extraction jobs
--overwrite                  Replace a previous export in the output folder
--strict                     Stop at the first message that cannot be parsed
```

For the strongest audit trail, use `--keep-eml`. For the smallest AI-ready
dataset, use the defaults; clean plain text is retained while bulky HTML and EML
copies are omitted.

## JSONL shape

Each line in `emails.jsonl` is a standalone JSON object:

```json
{
  "schema_version": "1.1",
  "id": "stable-message-id",
  "content_sha256": "hash-of-original-eml",
  "duplicate_of": null,
  "warnings": [],
  "source": {
    "id": "source-id",
    "type": "pst",
    "archive": "archive.pst",
    "folder": "Top of Outlook data file/Inbox",
    "source_item": "Top of Outlook data file/Inbox/1.eml"
  },
  "subject": "Quarterly project update",
  "from": [{"name": "Alice Example", "address": "alice@example.com"}],
  "to": [{"name": "Bob Example", "address": "bob@example.com"}],
  "cc": [],
  "bcc": [],
  "reply_to": [],
  "date": {
    "raw": "Tue, 15 Jul 2025 14:30:00 -0400",
    "iso": "2025-07-15T14:30:00-04:00",
    "utc": "2025-07-15T18:30:00+00:00"
  },
  "message_id": "<update-123@example.com>",
  "in_reply_to": null,
  "references": [],
  "body": {"text": "Hello team..."},
  "attachments": [],
  "headers": [{"name": "From", "value": "Alice Example <alice@example.com>"}]
}
```

## Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
