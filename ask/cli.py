"""Command-line entry point for the Ask module.

    python -m ask.cli init-schema        # apply schema to the shared DB
    python -m ask.cli migrate --dry-run  # preview the items -> documents migration
    python -m ask.cli migrate            # run the migration
    python -m ask.cli status             # show counts

All commands operate on AI Digest's shared database (ask.db.connection resolves
the path from config.yaml). Pass --db to override (e.g. for testing).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ask.db import migrate as m
from ask.db import persist
from ask.loaders import LoaderError, get_loader_for


def _cmd_init_schema(args) -> int:
    result = m.apply_schema(args.db)
    print("Schema applied to:", args.db or "(default AI Digest DB)")
    print("  created       :", ", ".join(result["tables_created"]) or "(none — all existed)")
    print("  already existed:", ", ".join(result["already_existed"]) or "(none)")
    return 0


def _cmd_migrate(args) -> int:
    result = m.migrate_items_to_documents(args.db, dry_run=args.dry_run)
    mode = "DRY RUN — nothing written" if result["dry_run"] else "MIGRATION COMPLETE"
    print(f"=== {mode} ===")
    print(f"  total items       : {result['total_items']}")
    print(f"  {'would migrate' if result['dry_run'] else 'migrated'}     : {result['migrated']}")
    print(f"  skipped duplicates: {result['skipped_duplicates']}")
    print(f"  skipped errors    : {result['skipped_errors']}")
    for e in result["sample_errors"]:
        print(f"    - error: {e['reason']} | {(e['title'] or '')[:60]}")
    return 0


def _cmd_status(args) -> int:
    s = m.status(args.db)
    if not s["schema_applied"]:
        print("Ask schema not applied yet. Run: python -m ask.cli init-schema")
    by_type = s["documents_by_type"]
    print(f"AI Digest items: {s['items']}")
    print(f"Ask documents: {s['documents']}")
    print(f"  - digest_archive: {by_type.get('digest_archive', 0)}")
    other = s["documents"] - by_type.get("digest_archive", 0)
    print(f"  - other types: {other}")
    for stype, n in sorted(by_type.items(), key=lambda x: -x[1]):
        if stype != "digest_archive":
            print(f"      ({stype}: {n})")
    print(f"chunks: {s['chunks']} (Phase 2)")
    print(f"vec_chunks: {s['vec_chunks']} (Phase 2)")
    return 0


def _short(doc_id: str) -> str:
    return f"{doc_id[:4]}...{doc_id[-4:]}"


def _cmd_ingest(args) -> int:
    if args.batch:
        return _ingest_batch(args)

    source = args.source
    print(f"Ingesting: {source}")
    try:
        loader = get_loader_for(source)
        docs = loader.load(source)
    except LoaderError as e:
        print(f"  ! error: {e}")
        return 1

    # Single file → detailed output; archive/multi → summary.
    if len(docs) == 1 and not source.startswith("digest_archive:"):
        doc = docs[0]
        print(f'  → Document loaded (title: "{doc.title}", {len(doc.content)} chars)')
        existed = persist.document_exists(doc.id, args.db)
        persist.save_document(doc, args.db)
        print(f"  → Saved as id {_short(doc.id)} "
              f"({'already exists' if existed else 'newly inserted'})")
    else:
        res = persist.save_documents(docs, args.db)
        print(f"  → Loaded {len(docs)} documents")
        print(f"  Summary: {res['inserted']} inserted, {res['duplicates']} duplicate, 0 errors")
    return 0


def _ingest_batch(args) -> int:
    folder = Path(args.source)
    print(f"Batch ingesting: {folder}")
    if not folder.is_dir():
        print(f"  ! error: not a directory: {folder}")
        return 1

    files = sorted({p for ext in ("*.txt", "*.md", "*.markdown") for p in folder.rglob(ext)})
    n_md = sum(1 for f in files if f.suffix in (".md", ".markdown"))
    n_txt = sum(1 for f in files if f.suffix == ".txt")
    print(f"  Found {len(files)} supported files ({n_md} .md, {n_txt} .txt)")

    inserted = duplicates = errors = 0
    for f in files:
        try:
            docs = get_loader_for(str(f)).load(str(f))
        except LoaderError as e:
            errors += 1
            print(f"  → {f.name}: error ({e})")
            continue
        for doc in docs:
            existed = persist.document_exists(doc.id, args.db)
            persist.save_document(doc, args.db)
            if existed:
                duplicates += 1
                print(f"  → {f.name}: already exists (id {_short(doc.id)})")
            else:
                inserted += 1
                print(f"  → {f.name}: inserted (id {_short(doc.id)})")
    print(f"  Summary: {inserted} inserted, {duplicates} duplicate, {errors} errors")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ask.cli", description="AI Digest Ask — admin CLI")
    parser.add_argument("--db", default=None, help="override database path (default: AI Digest's)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-schema", help="apply the Ask schema (idempotent)")

    p_migrate = sub.add_parser("migrate", help="migrate existing items into documents")
    p_migrate.add_argument("--dry-run", action="store_true", help="preview without writing")

    sub.add_parser("status", help="show item/document/chunk counts")

    p_ingest = sub.add_parser("ingest", help="ingest a file, folder, or digest_archive: source")
    p_ingest.add_argument("source", help="file path, folder (with --batch), or digest_archive:<filter>")
    p_ingest.add_argument("--batch", action="store_true",
                          help="treat source as a folder; ingest all .txt/.md/.markdown")

    args = parser.parse_args(argv)
    handlers = {
        "init-schema": _cmd_init_schema,
        "migrate": _cmd_migrate,
        "status": _cmd_status,
        "ingest": _cmd_ingest,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
