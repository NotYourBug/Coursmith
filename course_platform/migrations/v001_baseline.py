"""Mark/create the original six tables without replacing existing rows."""

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    from ..database import SCHEMA

    # The fixed baseline has no triggers or semicolons inside literals.
    # execute(), unlike executescript(), stays in the caller's transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)
