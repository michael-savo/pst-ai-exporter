from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .exporter import ExportError, ExportOptions, export_sources


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pst-ai-exporter",
        description=(
            "Combine local PST, MBOX, and EML sources into AI-friendly JSONL, "
            "Markdown, and organized attachments."
        ),
    )
    parser.add_argument(
        "sources",
        type=Path,
        nargs="+",
        help="One or more PST, MBOX, EML, or email directories to export together",
    )
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output directory")
    parser.add_argument(
        "--format",
        choices=("both", "jsonl", "markdown"),
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--include-html",
        action="store_true",
        help="Include original HTML bodies in JSONL in addition to clean text",
    )
    parser.add_argument(
        "--keep-eml",
        action="store_true",
        help="Keep a copy of each extracted EML for audit or reprocessing",
    )
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="Include recoverable deleted PST items",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help="Maximum parallel PST extraction jobs (readpst option)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace files from a previous export in the output directory",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Stop on the first message that cannot be exported",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    formats = frozenset({"jsonl", "markdown"} if args.format == "both" else {args.format})
    options = ExportOptions(
        formats=formats,
        include_html=args.include_html,
        keep_eml=args.keep_eml,
        include_deleted=args.include_deleted,
        jobs=args.jobs,
        overwrite=args.overwrite,
        strict=args.strict,
    )
    progress = (lambda message: None) if args.quiet else (lambda message: print(message))

    try:
        manifest = export_sources(args.sources, args.output, options, progress)
    except ExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Export cancelled.", file=sys.stderr)
        return 130

    counts = manifest["counts"]
    assert isinstance(counts, dict)
    print(
        f"Export complete: {counts['messages_exported']:,} messages and "
        f"{counts['attachments']:,} attachments saved to {args.output}"
    )
    if counts["messages_failed"] or counts["sources_failed"]:
        print(
            f"Warning: {counts['sources_failed']:,} source(s) and "
            f"{counts['messages_failed']:,} message(s) failed; see errors.jsonl for details.",
            file=sys.stderr,
        )
        return 1
    return 0
