"""Command line tools for validating, publishing, and serving courses."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import secrets
import socket
from pathlib import Path

from .access import AccessService
from .admin.auth import AdminService
from .content import load_course_package, publish_course, validate_course_package
from .database import check_database, migrate_database, sync_course
from .domain import Actor, BusinessError
from .settings import load_settings


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Invalid argv may contain a mistakenly supplied password.
        super().error("Invalid command arguments; use --help for supported options.")


def build_parser() -> argparse.ArgumentParser:
    parser = _PrivateArgumentParser(prog="course-platform")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-admin", help="initialize the single owner using interactive prompts")

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


def authenticate_owner(service: AdminService) -> Actor:
    """Credential-verified identity for later owner commands; no selectable ID."""
    request_id = secrets.token_hex(16)
    grant = service.login(input("Owner account: "), getpass.getpass("Password: "),
                          source="cli:" + hashlib.sha256(socket.gethostname().encode()).hexdigest(),
                          request_id=request_id)
    session = service.require_session(grant.token, request_id=request_id)
    actor = Actor(session.admin_id, request_id)
    service.logout(grant.token, actor)
    return actor


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        report = validate_course_package(args.course_dir)
        print(json.dumps({"ok": report.ok, "errors": report.errors, "warnings": report.warnings}, ensure_ascii=False, indent=2))
        return 0 if report.ok else 1

    settings = load_settings()
    if args.command == "init-admin":
        try:
            username = input("Owner account: ")
            password = getpass.getpass("Password (12–128 characters): ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise BusinessError("password_mismatch", "Password confirmation does not match.", 400)
            admin_id = AdminService(settings.database_path).initialize_owner(username, password)
            print(json.dumps({"initialized": True, "admin_id": admin_id}))
            return 0
        except BusinessError as exc:
            print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
            return 1
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
