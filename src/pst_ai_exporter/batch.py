from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


BATCH_SCHEMA_VERSION = "1.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discover_psts(input_root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in input_root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() == ".pst"
            and not path.name.startswith("._")
        ),
        key=lambda path: path.relative_to(input_root).as_posix().casefold(),
    )


def _safe_output_name(source: Path, source_sha256: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._-")
    return f"{(stem or 'archive')[:120]}__{source_sha256[:12]}"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _completed_export(output_dir: Path, source_sha256: str) -> dict[str, Any] | None:
    manifest = _read_json(output_dir / "manifest.json")
    if not manifest or manifest.get("status") != "complete":
        return None
    sources = manifest.get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        return None
    source = sources[0]
    if not isinstance(source, dict) or source.get("sha256") != source_sha256:
        return None
    return manifest


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _manifest_counts(manifest: dict[str, Any] | None) -> dict[str, int]:
    counts = manifest.get("counts", {}) if manifest else {}
    if not isinstance(counts, dict):
        counts = {}
    names = (
        "messages_found",
        "messages_exported",
        "messages_failed",
        "attachments",
        "attachment_bytes",
        "attachments_with_semantic_normalization",
    )
    return {name: int(counts.get(name, 0) or 0) for name in names}


def _run_source(
    source: Path,
    output_dir: Path,
    log_path: Path,
    overwrite: bool,
    keep_eml: bool,
) -> tuple[int, dict[str, Any] | None]:
    command = [
        sys.executable,
        "-m",
        "pst_ai_exporter",
        str(source),
        "--output",
        str(output_dir),
        "--jobs",
        "0",
        "--quiet",
    ]
    if overwrite:
        command.append("--overwrite")
    if keep_eml:
        command.append("--keep-eml")

    output_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"Started: {_utc_now()}\n")
        log.write(f"Source: {source}\n")
        log.write(f"Command: {' '.join(command)}\n\n")
        log.flush()
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        log.write(f"\nFinished: {_utc_now()}\nExit code: {result.returncode}\n")
    return result.returncode, _read_json(output_dir / "manifest.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pst-ai-exporter-batch",
        description=(
            "Export separate PST files concurrently while keeping each ReadPST "
            "invocation in repeatable -j 0 mode."
        ),
    )
    parser.add_argument("input", type=Path, help="Directory containing PST files")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Batch output root")
    parser.add_argument("--logs", type=Path, help="Log directory (default: OUTPUT/logs)")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of separate serial PST exports to run concurrently (default: 4)",
    )
    parser.add_argument(
        "--keep-eml",
        action="store_true",
        help="Keep reader-generated EML files in each source export",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    input_root = args.input.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    logs_root = (args.logs or output_root / "logs").expanduser().resolve()

    if not input_root.is_dir():
        print(f"Error: input directory does not exist: {input_root}", file=sys.stderr)
        return 2
    if args.workers < 1 or args.workers > 16:
        print("Error: --workers must be between 1 and 16.", file=sys.stderr)
        return 2
    if output_root == input_root or input_root in output_root.parents:
        print("Error: output must not be inside the input directory.", file=sys.stderr)
        return 2

    sources = _discover_psts(input_root)
    if not sources:
        print(f"Error: no usable PST files found under {input_root}", file=sys.stderr)
        return 2

    output_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "batch-manifest.json"
    previous = _read_json(manifest_path) or {}
    started_at = str(previous.get("started_at") or _utc_now())

    print(f"Found {len(sources):,} PST file(s). Computing source hashes...")
    records: list[dict[str, Any]] = []
    seen_identities: dict[tuple[str, str], str] = {}
    for index, source in enumerate(sources, start=1):
        print(f"Hashing {index:,} of {len(sources):,}: {source.name}", flush=True)
        source_sha256 = _sha256_file(source)
        relative = source.relative_to(input_root).as_posix()
        output_name = _safe_output_name(source, source_sha256)
        identity = (source.name.casefold(), source_sha256)
        duplicate_of = seen_identities.get(identity)
        if duplicate_of is None:
            seen_identities[identity] = relative
        output_dir = output_root / "by_source" / output_name
        log_path = logs_root / f"{output_name}.log"
        completed = _completed_export(output_dir, source_sha256)
        records.append(
            {
                "relative_path": relative,
                "filename": source.name,
                "size_bytes": source.stat().st_size,
                "sha256": source_sha256,
                "output_directory": f"by_source/{output_name}",
                "log": log_path.name,
                "duplicate_of": duplicate_of,
                "status": (
                    "duplicate"
                    if duplicate_of
                    else "complete"
                    if completed
                    else "pending"
                ),
                "exit_code": 0 if completed else None,
                "counts": _manifest_counts(completed),
            }
        )

    def write_batch(status: str) -> None:
        totals = {
            name: sum(int(record["counts"].get(name, 0)) for record in records)
            for name in _manifest_counts(None)
        }
        _write_json_atomic(
            manifest_path,
            {
                "schema_version": BATCH_SCHEMA_VERSION,
                "generator": {"name": "pst-ai-exporter-batch", "version": __version__},
                "status": status,
                "started_at": started_at,
                "updated_at": _utc_now(),
                "input_root": str(input_root),
                "output_root": str(output_root),
                "workers": args.workers,
                "readpst_jobs_per_source": 0,
                "counts": {
                    "pst_files_discovered": len(records),
                    "unique_pst_sources": sum(not record["duplicate_of"] for record in records),
                    "sources_complete": sum(record["status"] == "complete" for record in records),
                    "sources_failed": sum(record["status"] == "failed" for record in records),
                    "sources_pending": sum(record["status"] == "pending" for record in records),
                    **totals,
                },
                "sources": records,
            },
        )

    write_batch("running")
    pending = [record for record in records if record["status"] == "pending"]
    if not pending:
        write_batch("complete")
        print("All unique PST sources already have verified complete exports.")
        return 0

    print(
        f"Starting {len(pending):,} pending source(s) with {args.workers} "
        "concurrent serial worker(s)."
    )
    future_records = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for record in pending:
            source = input_root / str(record["relative_path"])
            output_dir = output_root / str(record["output_directory"])
            log_path = logs_root / str(record["log"])
            overwrite = output_dir.exists() and _completed_export(
                output_dir, str(record["sha256"])
            ) is None
            future = executor.submit(
                _run_source, source, output_dir, log_path, overwrite, args.keep_eml
            )
            future_records[future] = record

        completed_count = sum(record["status"] == "complete" for record in records)
        for future in as_completed(future_records):
            record = future_records[future]
            try:
                exit_code, manifest = future.result()
            except Exception as exc:  # defensive batch boundary
                exit_code, manifest = 1, None
                record["error"] = f"{type(exc).__name__}: {exc}"
            record["exit_code"] = exit_code
            record["counts"] = _manifest_counts(manifest)
            record["status"] = (
                "complete"
                if exit_code == 0 and manifest and manifest.get("status") == "complete"
                else "failed"
            )
            completed_count += 1
            print(
                f"[{completed_count:,}/{len(records):,}] {record['status'].upper()}: "
                f"{record['filename']} "
                f"({record['counts']['messages_exported']:,} messages)",
                flush=True,
            )
            write_batch("running")

    failures = sum(record["status"] == "failed" for record in records)
    write_batch("complete" if failures == 0 else "complete_with_errors")
    if failures:
        print(
            f"Batch finished with {failures:,} failed source(s); see batch-manifest.json "
            "and the individual logs.",
            file=sys.stderr,
        )
        return 1
    totals = _read_json(manifest_path)["counts"]
    print(
        f"Batch complete: {totals['messages_exported']:,} messages and "
        f"{totals['attachments']:,} attachments across "
        f"{totals['sources_complete']:,} source(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
