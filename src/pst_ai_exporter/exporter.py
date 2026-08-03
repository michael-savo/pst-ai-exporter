from __future__ import annotations

import hashlib
import json
import mailbox
import mimetypes
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence
from urllib.parse import quote

from . import __version__


SCHEMA_VERSION = "1.2"
MANAGED_OUTPUTS = (
    "manifest.json",
    "emails.jsonl",
    "errors.jsonl",
    "markdown",
    "attachments",
    "eml",
)


class ExportError(RuntimeError):
    """An expected export failure with a user-readable message."""


@dataclass(frozen=True)
class ExportOptions:
    formats: frozenset[str] = frozenset({"jsonl", "markdown"})
    include_html: bool = False
    keep_eml: bool = False
    include_deleted: bool = False
    jobs: int = 0
    overwrite: bool = False
    strict: bool = False


@dataclass(frozen=True)
class _SourceSpec:
    path: Path
    source_type: str
    source_id: str
    source_sha256: str | None = None
    eml_files: tuple[Path, ...] = ()


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"head", "script", "style"}:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[{alt}]")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"head", "script", "style"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
            return
        if not self.hidden_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts).replace("\xa0", " ")
        value = re.sub(r"[ \t\f\v]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", value).strip()
    return parser.text()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _email_id(relative_path: Path, source_id: str = "") -> str:
    relative_posix = relative_path.as_posix()
    identity = (
        source_id.encode("ascii")
        + b"\0"
        + relative_posix.encode("utf-8", errors="surrogateescape")
    )
    return hashlib.sha256(identity).hexdigest()[:24]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _sha256_bytes(encoded)


def _normalize_libpst_calendar(content: bytes) -> tuple[bytes, list[str]]:
    """Remove only conversion-time artifacts from LibPST-generated calendars."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content, []

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not re.search(r"(?mi)^PRODID:LibPST v[^\n]*$", normalized):
        return content, []

    ignored: list[str] = []
    lines: list[str] = []
    for line in normalized.split("\n"):
        if line.upper().startswith("DTSTAMP:"):
            lines.append("DTSTAMP:<LIBPST-CONVERSION-TIME>")
            ignored.append("DTSTAMP")
        else:
            lines.append(line)
    return "\n".join(lines).encode("utf-8"), sorted(set(ignored))


def _replace_unicode_surrogates(value: str) -> tuple[str, int]:
    """Replace non-serializable surrogate code points with U+FFFD."""
    replacements = sum(0xD800 <= ord(character) <= 0xDFFF for character in value)
    if not replacements:
        return value, 0
    return "".join(
        "\N{REPLACEMENT CHARACTER}"
        if 0xD800 <= ord(character) <= 0xDFFF
        else character
        for character in value
    ), replacements


def _sanitize_record_value(value: object) -> tuple[object, int]:
    """Make JSON-bound record values valid UTF-8 and report replacements."""
    if isinstance(value, str):
        return _replace_unicode_surrogates(value)
    if isinstance(value, list):
        sanitized_items: list[object] = []
        replacements = 0
        for item in value:
            sanitized, item_replacements = _sanitize_record_value(item)
            sanitized_items.append(sanitized)
            replacements += item_replacements
        return sanitized_items, replacements
    if isinstance(value, dict):
        sanitized_mapping: dict[object, object] = {}
        replacements = 0
        for key, item in value.items():
            sanitized_key, key_replacements = _sanitize_record_value(key)
            sanitized_item, item_replacements = _sanitize_record_value(item)
            sanitized_mapping[sanitized_key] = sanitized_item
            replacements += key_replacements + item_replacements
        return sanitized_mapping, replacements
    return value, 0


def _is_appledouble_sidecar(path: Path) -> bool:
    """Return whether a path is a macOS AppleDouble metadata sidecar."""
    return any(part.startswith("._") for part in path.parts)


def _clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(line.rstrip() for line in value.split("\n"))
    return re.sub(r"\n{4,}", "\n\n\n", value).strip()


def _decode_text_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw_payload = part.get_payload()
        return raw_payload if isinstance(raw_payload, str) else ""

    charsets = [part.get_content_charset(), "utf-8", "windows-1252", "latin-1"]
    for charset in charsets:
        if not charset:
            continue
        try:
            return payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _part_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload

    raw_payload = part.get_payload()
    if isinstance(raw_payload, list):
        return b"\n".join(item.as_bytes(policy=policy.default) for item in raw_payload)
    if isinstance(raw_payload, str):
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw_payload.encode(charset, errors="replace")
        except LookupError:
            return raw_payload.encode("utf-8", errors="replace")
    return b""


def _classify_parts(message: Message) -> tuple[list[str], list[str], list[Message]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Message] = []

    def visit(part: Message) -> None:
        content_type = part.get_content_type().lower()
        disposition = part.get_content_disposition()
        filename = part.get_filename()

        if (
            disposition == "attachment"
            or filename is not None
            or content_type == "message/rfc822"
        ):
            attachments.append(part)
            return

        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    visit(child)
            return

        if content_type == "text/plain":
            plain_parts.append(_decode_text_part(part))
        elif content_type == "text/html":
            html_parts.append(_decode_text_part(part))
        else:
            attachments.append(part)

    visit(message)
    return plain_parts, html_parts, attachments


def _addresses(message: Message, header_name: str) -> list[dict[str, str]]:
    headers = message.get_all(header_name, [])
    results: list[dict[str, str]] = []
    for name, address in getaddresses([str(value) for value in headers]):
        if name or address:
            results.append({"name": name, "address": address})
    return results


def _date_fields(message: Message) -> dict[str, str | None]:
    raw = str(message.get("Date", ""))
    if not raw:
        return {"raw": "", "iso": None, "utc": None}
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return {"raw": raw, "iso": None, "utc": None}
    if parsed is None:
        return {"raw": raw, "iso": None, "utc": None}
    utc_value = None
    if parsed.tzinfo is not None:
        utc_value = parsed.astimezone(timezone.utc).isoformat()
    return {"raw": raw, "iso": parsed.isoformat(), "utc": utc_value}


def _reference_ids(message: Message) -> list[str]:
    values = " ".join(str(value) for value in message.get_all("References", []))
    return re.findall(r"<[^>]+>|\S+", values)


def _safe_filename(name: str, fallback: str) -> str:
    value, _replacements = _replace_unicode_surrogates(name or fallback)
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("/", "_").replace("\\", "_").replace("\x00", "")
    value = "".join(char for char in value if unicodedata.category(char) != "Cc")
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback

    suffix = Path(value).suffix
    stem = value[: -len(suffix)] if suffix else value
    max_stem = max(1, 160 - len(suffix))
    return f"{stem[:max_stem]}{suffix[:30]}"


def _unique_filename(name: str, used: set[str]) -> str:
    candidate = name
    suffix = Path(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _attachment_filename(part: Message, index: int) -> tuple[str, str | None]:
    original = part.get_filename()
    if original:
        return _safe_filename(str(original), f"attachment_{index:03d}"), str(original)
    extension = mimetypes.guess_extension(part.get_content_type(), strict=False) or ""
    if part.get_content_type() == "message/rfc822":
        extension = ".eml"
    fallback = f"attachment_{index:03d}{extension}"
    return fallback, None


def _write_attachments(
    parts: Iterable[Message], output_dir: Path, email_id: str
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    calendar_candidates: list[tuple[int, str, list[str], bool, bool]] = []
    used_names: set[str] = set()
    message_dir = output_dir / "attachments" / email_id

    for index, part in enumerate(parts, start=1):
        base_name, original_name = _attachment_filename(part, index)
        saved_name = _unique_filename(base_name, used_names)
        content = _part_bytes(part)
        message_dir.mkdir(parents=True, exist_ok=True)
        destination = message_dir / saved_name
        destination.write_bytes(content)
        relative_path = destination.relative_to(output_dir).as_posix()
        content_type = part.get_content_type()
        raw_sha256 = _sha256_bytes(content)
        if content_type.lower() == "text/calendar":
            semantic_content, ignored_properties = _normalize_libpst_calendar(content)
            if ignored_properties:
                generated_filename = bool(
                    original_name
                    and re.fullmatch(r"i\d+\.ics", original_name, flags=re.IGNORECASE)
                )
                calendar_candidates.append(
                    (
                        len(records),
                        _sha256_bytes(semantic_content),
                        ignored_properties,
                        generated_filename,
                        original_name is None,
                    )
                )

        records.append(
            {
                "original_filename": original_name,
                "saved_filename": saved_name,
                "path": relative_path,
                "content_type": content_type,
                "content_disposition": part.get_content_disposition(),
                "content_id": str(part.get("Content-ID", "")) or None,
                "size_bytes": len(content),
                "sha256": raw_sha256,
                "semantic_sha256": raw_sha256,
                "semantic_normalization": None,
            }
        )

    candidate_groups: dict[str, list[tuple[int, list[str], bool, bool]]] = {}
    for index, semantic_sha256, ignored, generated_filename, unnamed in calendar_candidates:
        candidate_groups.setdefault(semantic_sha256, []).append(
            (index, ignored, generated_filename, unnamed)
        )
    for semantic_sha256, candidates in candidate_groups.items():
        if not (
            len(candidates) >= 2
            and any(item[2] for item in candidates)
            and any(item[3] for item in candidates)
        ):
            continue
        for index, ignored, generated_filename, _unnamed in candidates:
            records[index]["semantic_sha256"] = semantic_sha256
            records[index]["semantic_normalization"] = {
                "type": "libpst_generated_calendar_pair_v1",
                "ignored_properties": ignored,
                "generated_filename": generated_filename,
                "detection": "matching inline and generated-filename calendar parts",
            }
    return records


def _semantic_message_sha256(record: dict[str, object]) -> str:
    structural_headers = {
        "content-type",
        "content-transfer-encoding",
        "mime-version",
    }
    headers = record.get("headers", [])
    assert isinstance(headers, list)
    semantic_headers = sorted(
        (
            str(item.get("name", "")).casefold(),
            _clean_text(str(item.get("value", ""))),
        )
        for item in headers
        if isinstance(item, dict)
        and str(item.get("name", "")).casefold() not in structural_headers
    )

    attachments = record.get("attachments", [])
    assert isinstance(attachments, list)
    semantic_attachments: list[dict[str, object]] = []
    for item in attachments:
        assert isinstance(item, dict)
        normalization = item.get("semantic_normalization")
        generated_filename = bool(
            isinstance(normalization, dict)
            and normalization.get("generated_filename")
        )
        semantic_attachments.append(
            {
                "filename": None if generated_filename else item.get("original_filename"),
                "content_type": item.get("content_type"),
                "content_disposition": item.get("content_disposition"),
                "content_id": item.get("content_id"),
                "semantic_sha256": item.get("semantic_sha256"),
            }
        )

    payload = {
        "subject": record.get("subject"),
        "from": record.get("from"),
        "to": record.get("to"),
        "cc": record.get("cc"),
        "bcc": record.get("bcc"),
        "reply_to": record.get("reply_to"),
        "date": record.get("date"),
        "message_id": record.get("message_id"),
        "in_reply_to": record.get("in_reply_to"),
        "references": record.get("references"),
        "body": record.get("body"),
        "attachments": semantic_attachments,
        "headers": semantic_headers,
    }
    return _canonical_sha256(payload)


def _message_record(
    raw: bytes,
    relative_path: Path,
    archive_name: str,
    source_id: str,
    source_type: str,
    output_dir: Path,
    include_html: bool,
) -> dict[str, object]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    relative_posix = relative_path.as_posix()
    email_id = _email_id(relative_path, source_id)
    plain_parts, html_parts, attachment_parts = _classify_parts(message)
    html_body = _clean_text("\n\n".join(part for part in html_parts if part))
    text_body = _clean_text("\n\n".join(part for part in plain_parts if part))
    if not text_body and html_body:
        text_body = html_to_text(html_body)

    attachment_records = _write_attachments(attachment_parts, output_dir, email_id)
    folder = relative_path.parent.as_posix()
    if folder == ".":
        folder = ""

    body: dict[str, str] = {"text": text_body}
    if include_html:
        body["html"] = html_body

    headers = [
        {"name": str(name), "value": str(value)}
        for name, value in message.raw_items()
    ]
    normalized_calendar_attachments = sum(
        bool(item.get("semantic_normalization")) for item in attachment_records
    )
    warnings: list[dict[str, object]] = []
    if normalized_calendar_attachments:
        warnings.append(
            {
                "type": "libpst_generated_calendar_artifacts_normalized",
                "attachment_count": normalized_calendar_attachments,
                "raw_values_preserved": True,
            }
        )

    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "id": email_id,
        "content_sha256": _sha256_bytes(raw),
        "source": {
            "id": source_id,
            "type": source_type,
            "archive": archive_name,
            "folder": folder,
            "source_item": relative_posix,
        },
        "subject": str(message.get("Subject", "")),
        "from": _addresses(message, "From"),
        "to": _addresses(message, "To"),
        "cc": _addresses(message, "Cc"),
        "bcc": _addresses(message, "Bcc"),
        "reply_to": _addresses(message, "Reply-To"),
        "date": _date_fields(message),
        "message_id": str(message.get("Message-ID", "")) or None,
        "in_reply_to": str(message.get("In-Reply-To", "")) or None,
        "references": _reference_ids(message),
        "body": body,
        "attachments": attachment_records,
        "headers": headers,
        "warnings": warnings,
    }
    sanitized_record, replacements = _sanitize_record_value(record)
    assert isinstance(sanitized_record, dict)
    if replacements:
        sanitized_warnings = sanitized_record["warnings"]
        assert isinstance(sanitized_warnings, list)
        sanitized_warnings.append(
            {
                "type": "invalid_unicode_replaced",
                "replacement_count": replacements,
                "replacement_character": "U+FFFD",
            }
        )
    sanitized_record["semantic_sha256"] = _semantic_message_sha256(sanitized_record)
    return sanitized_record


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _markdown(record: dict[str, object]) -> str:
    source = record["source"]
    date = record["date"]
    body = record["body"]
    attachments = record["attachments"]
    assert isinstance(source, dict)
    assert isinstance(date, dict)
    assert isinstance(body, dict)
    assert isinstance(attachments, list)

    frontmatter = {
        "schema_version": record["schema_version"],
        "id": record["id"],
        "duplicate_of": record.get("duplicate_of"),
        "source_id": source["id"],
        "source_type": source["type"],
        "source_archive": source["archive"],
        "source_folder": source["folder"],
        "source_item": source["source_item"],
        "subject": record["subject"],
        "from": record["from"],
        "to": record["to"],
        "cc": record["cc"],
        "bcc": record["bcc"],
        "date_raw": date["raw"],
        "date_iso": date["iso"],
        "date_utc": date["utc"],
        "message_id": record["message_id"],
        "in_reply_to": record["in_reply_to"],
        "references": record["references"],
        "attachments": attachments,
    }
    lines = ["---"]
    lines.extend(f"{key}: {_json_value(value)}" for key, value in frontmatter.items())
    subject = str(record["subject"] or "(no subject)").replace("\n", " ").strip()
    lines.extend(["---", "", f"# {subject}", "", str(body.get("text", "")).strip()])

    if attachments:
        lines.extend(["", "## Attachments", ""])
        for attachment in attachments:
            assert isinstance(attachment, dict)
            label = str(
                attachment.get("original_filename")
                or attachment.get("saved_filename")
                or "attachment"
            )
            target = "../" + quote(str(attachment["path"]), safe="/")
            lines.append(f"- [{label}]({target})")
    return "\n".join(lines).rstrip() + "\n"


def _prepare_output(output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise ExportError(f"Output must be a directory: {output_dir}")
    existing = [output_dir / name for name in MANAGED_OUTPUTS if (output_dir / name).exists()]
    if existing and not overwrite:
        names = ", ".join(path.name for path in existing)
        raise ExportError(
            f"Output already contains a previous export ({names}). "
            "Choose a new folder or use --overwrite."
        )
    if overwrite:
        for path in existing:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)


def _extract_pst(
    pst_path: Path,
    destination: Path,
    options: ExportOptions,
    progress: Callable[[str], None],
) -> None:
    executable = _readpst_executable()

    command = [executable, "-e", "-8", "-q", "-t", "e", "-j", "0"]
    if options.include_deleted:
        command.append("-D")
    command.extend(["-o", str(destination), str(pst_path)])
    progress(f"Extracting {pst_path.name} locally...")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown readpst error"
        raise ExportError(f"Could not read {pst_path.name}: {detail}")


def _readpst_executable() -> str:
    executable = shutil.which("readpst")
    if executable is None:
        raise ExportError(
            "The PST reader is not installed. On macOS, install it with "
            "'brew install libpst', then run this command again."
        )
    return executable


def _readpst_version_output(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "-V"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    output = (result.stdout or result.stderr).strip()
    return output or None


def _find_eml_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".eml"),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )


def _copy_eml(raw: bytes, relative_path: Path, output_dir: Path, source_id: str) -> None:
    destination = output_dir / "eml" / source_id / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)


def _directory_source_fingerprint(path: Path, eml_files: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(b"eml_directory\0")
    for eml_path in eml_files:
        relative_path = eml_path.relative_to(path).as_posix()
        digest.update(relative_path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(_sha256_file(eml_path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _source_spec(path: Path, source_type: str, eml_files: tuple[Path, ...] = ()) -> _SourceSpec:
    source_sha256: str | None = None
    if path.is_file():
        source_sha256 = _sha256_file(path)
        identity = f"{source_type}\0{path.name.casefold()}\0{source_sha256}"
    else:
        directory_fingerprint = _directory_source_fingerprint(path, eml_files)
        identity = f"{source_type}\0{directory_fingerprint}"
    return _SourceSpec(
        path=path,
        source_type=source_type,
        source_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
        source_sha256=source_sha256,
        eml_files=eml_files,
    )


def _expand_sources(sources: Sequence[Path], output_dir: Path) -> list[_SourceSpec]:
    if not sources:
        raise ExportError("Choose at least one PST, MBOX, EML, or email directory.")

    output_resolved = output_dir.resolve()
    archive_types = {".pst": "pst", ".mbox": "mbox", ".mbx": "mbox"}
    specs: list[_SourceSpec] = []
    seen_files: set[Path] = set()
    seen_directories: set[Path] = set()

    def add_file(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen_files:
            return
        if _is_appledouble_sidecar(path):
            return
        suffix = path.suffix.lower()
        if suffix in archive_types:
            specs.append(_source_spec(path, archive_types[suffix]))
        elif suffix == ".eml":
            specs.append(_source_spec(path, "eml_file"))
        else:
            raise ExportError(f"Unsupported email source: {path}")
        seen_files.add(resolved)

    for original in sources:
        source = original.expanduser()
        if not source.exists():
            raise ExportError(f"Source does not exist: {source}")
        if source.is_file():
            add_file(source)
            continue
        if not source.is_dir():
            raise ExportError(f"Unsupported email source: {source}")

        resolved_directory = source.resolve()
        if resolved_directory == output_resolved:
            raise ExportError("The output directory must be different from a source directory.")
        if resolved_directory in seen_directories:
            continue
        seen_directories.add(resolved_directory)

        discovered = sorted(
            (
                path
                for path in source.rglob("*")
                if path.is_file()
                and not _is_appledouble_sidecar(path.relative_to(source))
                and path.suffix.lower() in {*archive_types, ".eml"}
                and not path.resolve().is_relative_to(output_resolved)
            ),
            key=lambda path: path.relative_to(source).as_posix().casefold(),
        )
        archive_files = [path for path in discovered if path.suffix.lower() in archive_types]
        eml_files = tuple(
            path
            for path in discovered
            if path.suffix.lower() == ".eml" and path.resolve() not in seen_files
        )
        for archive_path in archive_files:
            add_file(archive_path)
        if eml_files:
            specs.append(_source_spec(source, "eml_directory", eml_files))
            seen_files.update(path.resolve() for path in eml_files)
        if not archive_files and not eml_files:
            raise ExportError(f"No PST, MBOX, or EML files were found in {source}.")

    if not specs:
        raise ExportError(
            "No usable PST, MBOX, or EML sources were found. "
            "macOS AppleDouble files beginning with '._' are metadata and are excluded."
        )

    unique_specs: list[_SourceSpec] = []
    seen_source_ids: set[str] = set()
    for spec in specs:
        if spec.source_id in seen_source_ids:
            continue
        seen_source_ids.add(spec.source_id)
        unique_specs.append(spec)
    specs = unique_specs

    managed_roots = [(output_resolved / name).resolve() for name in MANAGED_OUTPUTS]
    for spec in specs:
        resolved = spec.path.resolve()
        if spec.path.is_file() and any(resolved.is_relative_to(root) for root in managed_roots):
            raise ExportError("A source file cannot be inside the export's managed output folders.")
    return specs


def _source_details(spec: _SourceSpec) -> dict[str, object]:
    details: dict[str, object] = {
        "id": spec.source_id,
        "type": spec.source_type,
        "name": spec.path.name,
        "path": str(spec.path.resolve()),
    }
    if spec.path.is_file():
        details["size_bytes"] = spec.path.stat().st_size
        details["sha256"] = spec.source_sha256
    elif spec.eml_files:
        details["file_count"] = len(spec.eml_files)
        details["size_bytes"] = sum(path.stat().st_size for path in spec.eml_files)
    return details


def _iter_source_messages(
    spec: _SourceSpec,
    temporary_root: Path,
    options: ExportOptions,
    progress: Callable[[str], None],
) -> Iterator[tuple[bytes, Path]]:
    if spec.source_type == "pst":
        eml_root = temporary_root / spec.source_id
        eml_root.mkdir(parents=True)
        _extract_pst(spec.path, eml_root, options, progress)
        for eml_path in _find_eml_files(eml_root):
            yield eml_path.read_bytes(), eml_path.relative_to(eml_root)
        return

    if spec.source_type == "mbox":
        try:
            source_mbox = mailbox.mbox(str(spec.path), factory=None, create=False)
        except Exception as exc:
            raise ExportError(f"Could not open {spec.path.name} as MBOX: {exc}") from exc
        try:
            for index, key in enumerate(source_mbox.iterkeys(), start=1):
                raw = source_mbox.get_bytes(key, from_=False)
                yield raw, Path(f"{index:08d}.eml")
        finally:
            source_mbox.close()
        return

    if spec.source_type == "eml_file":
        yield spec.path.read_bytes(), Path(spec.path.name)
        return

    for eml_path in spec.eml_files:
        yield eml_path.read_bytes(), eml_path.relative_to(spec.path)


def export_sources(
    sources: Sequence[Path],
    output_dir: Path,
    options: ExportOptions | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    options = options or ExportOptions()
    progress = progress or (lambda _message: None)
    output_dir = output_dir.expanduser()

    if not options.formats or not options.formats.issubset({"jsonl", "markdown"}):
        raise ExportError("Formats must include jsonl, markdown, or both.")
    if options.jobs != 0:
        raise ExportError(
            "Unsafe ReadPST parallelism is disabled for evidentiary exports. "
            "Use --jobs 0; parallelize separate PST files instead."
        )

    specs = _expand_sources(sources, output_dir)
    readpst_details: dict[str, object] | None = None
    if any(spec.source_type == "pst" for spec in specs):
        readpst_executable = _readpst_executable()
        readpst_arguments = ["-e", "-8", "-q", "-t", "e", "-j", "0"]
        if options.include_deleted:
            readpst_arguments.append("-D")
        readpst_details = {
            "name": "readpst",
            "version_output": _readpst_version_output(readpst_executable),
            "parallel_jobs": 0,
            "mode": "serial",
            "arguments": readpst_arguments,
        }
    _prepare_output(output_dir, options.overwrite)
    started_at = datetime.now(timezone.utc)

    folder_counts: Counter[str] = Counter()
    errors: list[dict[str, object]] = []
    source_stats: dict[str, dict[str, object]] = {
        spec.source_id: {
            "messages_found": 0,
            "messages_exported": 0,
            "messages_failed": 0,
            "failed": False,
            "folders": Counter(),
        }
        for spec in specs
    }
    first_semantic_content_ids: dict[str, str] = {}
    raw_content_hashes: set[str] = set()
    message_count = 0
    message_failures = 0
    source_failures = 0
    messages_found = 0
    attachment_count = 0
    attachment_bytes = 0
    messages_with_unicode_replacements = 0
    unicode_replacement_characters = 0
    attachments_with_semantic_normalization = 0
    date_values: list[str] = []

    jsonl_stream = None
    if "jsonl" in options.formats:
        jsonl_stream = (output_dir / "emails.jsonl").open("w", encoding="utf-8")
    if "markdown" in options.formats:
        (output_dir / "markdown").mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="pst-ai-exporter-") as temporary:
            temporary_root = Path(temporary)
            for source_index, spec in enumerate(specs, start=1):
                stats = source_stats[spec.source_id]
                progress(
                    f"Source {source_index:,} of {len(specs):,}: {spec.path.name} "
                    f"({spec.source_type})"
                )
                source_message_count = 0
                try:
                    for raw, relative_path in _iter_source_messages(
                        spec, temporary_root, options, progress
                    ):
                        source_message_count += 1
                        messages_found += 1
                        stats["messages_found"] = int(stats["messages_found"]) + 1
                        try:
                            record = _message_record(
                                raw,
                                relative_path,
                                spec.path.name,
                                spec.source_id,
                                spec.source_type,
                                output_dir,
                                options.include_html,
                            )
                            warnings = record.get("warnings", [])
                            if isinstance(warnings, list):
                                for warning in warnings:
                                    if (
                                        isinstance(warning, dict)
                                        and warning.get("type") == "invalid_unicode_replaced"
                                    ):
                                        messages_with_unicode_replacements += 1
                                        unicode_replacement_characters += int(
                                            warning.get("replacement_count", 0)
                                        )
                            attachments = record["attachments"]
                            assert isinstance(attachments, list)
                            semantic_hash = str(record["semantic_sha256"])
                            record["duplicate_of"] = first_semantic_content_ids.get(
                                semantic_hash
                            )

                            if options.keep_eml:
                                _copy_eml(raw, relative_path, output_dir, spec.source_id)
                            if jsonl_stream is not None:
                                json.dump(
                                    record,
                                    jsonl_stream,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                jsonl_stream.write("\n")
                            if "markdown" in options.formats:
                                markdown_path = output_dir / "markdown" / f"{record['id']}.md"
                                markdown_path.write_text(_markdown(record), encoding="utf-8")

                            first_semantic_content_ids.setdefault(
                                semantic_hash, str(record["id"])
                            )
                            raw_content_hashes.add(str(record["content_sha256"]))
                            attachments_with_semantic_normalization += sum(
                                bool(
                                    isinstance(item, dict)
                                    and item.get("semantic_normalization")
                                )
                                for item in attachments
                            )
                            message_count += 1
                            stats["messages_exported"] = int(stats["messages_exported"]) + 1
                            attachment_count += len(attachments)
                            attachment_bytes += sum(
                                int(item["size_bytes"]) for item in attachments
                            )
                            source_info = record["source"]
                            assert isinstance(source_info, dict)
                            folder = str(source_info["folder"] or "(root)")
                            folder_counts[f"{spec.path.name}:{folder}"] += 1
                            source_folders = stats["folders"]
                            assert isinstance(source_folders, Counter)
                            source_folders[folder] += 1
                            date_info = record["date"]
                            assert isinstance(date_info, dict)
                            if date_info["utc"]:
                                date_values.append(str(date_info["utc"]))
                        except Exception as exc:
                            failed_id = _email_id(relative_path, spec.source_id)
                            shutil.rmtree(
                                output_dir / "attachments" / failed_id, ignore_errors=True
                            )
                            (output_dir / "markdown" / f"{failed_id}.md").unlink(
                                missing_ok=True
                            )
                            (output_dir / "eml" / spec.source_id / relative_path).unlink(
                                missing_ok=True
                            )
                            message_failures += 1
                            stats["messages_failed"] = int(stats["messages_failed"]) + 1
                            errors.append(
                                {
                                    "scope": "message",
                                    "source_id": spec.source_id,
                                    "source_archive": spec.path.name,
                                    "source_item": relative_path.as_posix(),
                                    "error_type": type(exc).__name__,
                                    "message": str(exc),
                                }
                            )
                            if options.strict:
                                raise ExportError(
                                    f"Failed to export {spec.path.name}/"
                                    f"{relative_path.as_posix()}: {exc}"
                                ) from exc

                        if messages_found % 100 == 0:
                            progress(f"Processed {messages_found:,} messages...")

                    if source_message_count == 0:
                        raise ExportError(f"No email messages were found in {spec.path.name}.")
                    progress(
                        f"Finished {spec.path.name}: {source_message_count:,} message(s)."
                    )
                except Exception as exc:
                    source_failures += 1
                    stats["failed"] = True
                    errors.append(
                        {
                            "scope": "source",
                            "source_id": spec.source_id,
                            "source_archive": spec.path.name,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                    if options.strict:
                        if isinstance(exc, ExportError):
                            raise
                        raise ExportError(f"Failed to process {spec.path.name}: {exc}") from exc
    finally:
        if jsonl_stream is not None:
            jsonl_stream.close()

    if errors:
        with (output_dir / "errors.jsonl").open("w", encoding="utf-8") as stream:
            for error in errors:
                json.dump(error, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")

    source_details: list[dict[str, object]] = []
    for spec in specs:
        details = _source_details(spec)
        stats = source_stats[spec.source_id]
        source_folders = stats.pop("folders")
        assert isinstance(source_folders, Counter)
        details["counts"] = {
            "messages_found": stats["messages_found"],
            "messages_exported": stats["messages_exported"],
            "messages_failed": stats["messages_failed"],
        }
        details["status"] = "failed" if stats["failed"] else "complete"
        details["folders"] = dict(sorted(source_folders.items()))
        source_details.append(details)

    finished_at = datetime.now(timezone.utc)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": "pst-ai-exporter", "version": __version__},
        "identity_profiles": {
            "source": "source-type-name-content-v1",
            "message": "source-id-and-source-item-v1",
            "semantic_message": "parsed-evidence-content-v1",
            "libpst_calendar": "matching-generated-calendar-pair-v1",
        },
        "pst_reader": readpst_details,
        "status": "complete" if not errors else "complete_with_errors",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "source": source_details[0] if len(source_details) == 1 else None,
        "sources": source_details,
        "options": {
            "formats": sorted(options.formats),
            "include_html": options.include_html,
            "keep_eml": options.keep_eml,
            "include_deleted": options.include_deleted,
            "jobs": options.jobs,
        },
        "counts": {
            "sources": len(specs),
            "sources_failed": source_failures,
            "messages_found": messages_found,
            "messages_exported": message_count,
            "messages_failed": message_failures,
            "unique_message_content": len(first_semantic_content_ids),
            "exact_duplicate_messages": message_count
            - len(first_semantic_content_ids),
            "unique_raw_message_content": len(raw_content_hashes),
            "attachments": attachment_count,
            "attachment_bytes": attachment_bytes,
            "attachments_with_semantic_normalization": (
                attachments_with_semantic_normalization
            ),
            "messages_with_unicode_replacements": messages_with_unicode_replacements,
            "unicode_replacement_characters": unicode_replacement_characters,
        },
        "date_range_utc": {
            "earliest": min(date_values) if date_values else None,
            "latest": max(date_values) if date_values else None,
        },
        "folders": dict(sorted(folder_counts.items())),
        "files": {
            "jsonl": "emails.jsonl" if "jsonl" in options.formats else None,
            "markdown_directory": "markdown" if "markdown" in options.formats else None,
            "attachments_directory": "attachments" if attachment_count else None,
            "eml_directory": "eml" if options.keep_eml else None,
            "errors": "errors.jsonl" if errors else None,
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def export_source(
    source: Path,
    output_dir: Path,
    options: ExportOptions | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    return export_sources([source], output_dir, options, progress)
