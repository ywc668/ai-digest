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

from ask.db import migrate as m


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ask.cli", description="AI Digest Ask — admin CLI")
    parser.add_argument("--db", default=None, help="override database path (default: AI Digest's)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-schema", help="apply the Ask schema (idempotent)")

    p_migrate = sub.add_parser("migrate", help="migrate existing items into documents")
    p_migrate.add_argument("--dry-run", action="store_true", help="preview without writing")

    sub.add_parser("status", help="show item/document/chunk counts")

    args = parser.parse_args(argv)
    handlers = {
        "init-schema": _cmd_init_schema,
        "migrate": _cmd_migrate,
        "status": _cmd_status,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
