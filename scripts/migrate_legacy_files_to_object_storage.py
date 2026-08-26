"""Upload legacy STORAGE_DIR files into the configured S3/R2 bucket.

Legacy layout supported:
  <firm>/<invoice_id>_<filename>             -> invoices
  <firm>/docs/<doc_id>_<filename>            -> documents
  <firm>/expenses/<attachment_id>_<filename> -> expenses
  <firm>/od/<entry_id>_<filename>            -> od

The script never deletes its source. After verifying object-store uploads, the
backup can be archived separately.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow direct execution: python scripts/migrate_legacy_files_to_object_storage.py ...
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.storage import content_type_for, get_storage, object_key


def parse_legacy(relative: Path):
    parts = relative.parts
    if len(parts) < 2:
        return None
    firm_id = parts[0]
    if parts[1] in {"docs", "expenses", "od"}:
        if len(parts) != 3:
            return None
        legacy_kind = parts[1]
        kind = {"docs": "documents", "expenses": "expenses", "od": "od"}[legacy_kind]
        stored_name = parts[2]
    else:
        if len(parts) != 2:
            return None
        kind = "invoices"
        stored_name = parts[1]
    if "_" not in stored_name:
        return None
    entity_id, filename = stored_name.split("_", 1)
    if not entity_id or not filename:
        return None
    return firm_id, kind, entity_id, filename


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="legacy files directory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    storage = None if args.dry_run else get_storage()
    uploaded = skipped = 0
    for path in sorted(p for p in args.source.rglob("*") if p.is_file()):
        parsed = parse_legacy(path.relative_to(args.source))
        if not parsed:
            print(f"SKIP unrecognized: {path}")
            skipped += 1
            continue
        firm_id, kind, entity_id, filename = parsed
        key = object_key(kind, firm_id, entity_id, filename)
        if args.dry_run:
            print(f"DRY  {path} -> {key}")
        else:
            storage.put_bytes(key, path.read_bytes(), content_type=content_type_for(filename))
            print(f"OK   {path} -> {key}")
        uploaded += 1
    print(f"files={uploaded + skipped} transferable={uploaded} skipped={skipped} dry_run={args.dry_run}")
    return 0 if skipped == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
