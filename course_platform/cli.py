"""Command line tools for validating, publishing, and serving courses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .access import AccessService
from .content import load_course_package, publish_course, validate_course_package
from .database import check_database, migrate_database, sync_course
from .domain import BusinessError
from .settings import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="course-platform")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a course package")
    validate.add_argument("course_dir", type=Path)

    publish = subparsers.add_parser("publish", help="validate and publish a course")
    publish.add_argument("course_dir", type=Path)

    create_code = subparsers.add_parser("create-code", help="create a one-time access code")
    create_code.add_argument("course_slug")

    migrate = subparsers.add_parser("migrate", help="inspect or explicitly upgrade the database")
    migrate.add_argument("--backup", type=Path)
    migrate.add_argument("--check-only", action="store_true")

    serve = subparsers.add_parser("serve", help="start the local delivery site")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        report = validate_course_package(args.course_dir)
        print(json.dumps({"ok": report.ok, "errors": report.errors, "warnings": report.warnings}, ensure_ascii=False, indent=2))
        return 0 if report.ok else 1

    settings = load_settings()
    if args.command == "migrate":
        try:
            if args.check_only:
                result = check_database(settings.database_path)
            else:
                report = migrate_database(settings.database_path, backup_path=args.backup)
                result = {"from_version": report.from_version, "to_version": report.to_version,
                          "backup_path": str(report.backup_path) if report.backup_path else None}
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except BusinessError as exc:
            print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
            return 1

    if args.command == "publish":
        package = load_course_package(args.course_dir)
        target = publish_course(args.course_dir, settings.content_root, package.manifest)
        sync_course(package.manifest, target, settings.database_path)
        print(target)
        return 0

    if args.command == "create-code":
        package_path = settings.content_root / args.course_slug
        package = load_course_package(package_path)
        code = AccessService(settings.database_path, settings.session_ttl_hours).create_access_code(
            package.manifest.course_id
        )
        print(code)
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("course_platform.app:app", host=args.host, port=args.port, reload=False)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
