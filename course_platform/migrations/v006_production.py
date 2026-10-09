"""Add private production sources and durable jobs without rewriting M1 data.

Every statement runs on the migration runner's transaction. Do not use
executescript: sqlite3 would commit the caller's transaction before the script.
Current pointers and lifecycle state are mutable; source revisions and handoff
references are append-only. Composite FKs retain project/chapter/attempt identity.
"""

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE production_projects (
            id INTEGER PRIMARY KEY,
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            mode TEXT NOT NULL CHECK (mode IN ('quick','precise')),
            content_kind TEXT NOT NULL DEFAULT 'course' CHECK (content_kind IN ('course','guide','handbook')),
            category_id INTEGER REFERENCES categories(id),
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','archived')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision)='integer' AND revision>=1),
            current_brief_revision_id INTEGER,
            current_outline_revision_id INTEGER,
            current_confirmation_id INTEGER,
            acknowledged_input_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            updated_at TEXT,
            FOREIGN KEY (current_brief_revision_id,id) REFERENCES brief_revisions(id,project_id),
            FOREIGN KEY (current_outline_revision_id,id) REFERENCES outline_revisions(id,project_id),
            FOREIGN KEY (current_confirmation_id,id) REFERENCES outline_confirmations(id,project_id)
        )""",
        """CREATE TABLE brief_revisions (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES production_projects(id),
            revision INTEGER NOT NULL CHECK (typeof(revision)='integer' AND revision>=1),
            title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
            audience TEXT NOT NULL DEFAULT '' CHECK (length(audience)<=2000),
            prerequisites TEXT NOT NULL DEFAULT '' CHECK (length(prerequisites)<=2000),
            goal TEXT NOT NULL DEFAULT '' CHECK (length(goal)<=2000),
            difficulty TEXT NOT NULL DEFAULT '' CHECK (length(difficulty)<=2000),
            chapter_count INTEGER NOT NULL DEFAULT 10 CHECK (typeof(chapter_count)='integer' AND chapter_count BETWEEN 1 AND 60),
            category_id INTEGER REFERENCES categories(id),
            content_kind TEXT NOT NULL DEFAULT 'course' CHECK (content_kind IN ('course','guide','handbook')),
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id), UNIQUE (project_id,revision),
            CHECK (length(title)+length(audience)+length(prerequisites)+length(goal)+length(difficulty)<=10000)
        )""",
        """CREATE TABLE project_reference_links (
            project_id INTEGER NOT NULL REFERENCES production_projects(id),
            reference_id INTEGER NOT NULL CHECK (typeof(reference_id)='integer' AND reference_id>=1),
            current_revision_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1 CHECK (typeof(active)='integer' AND active IN (0,1)),
            position INTEGER NOT NULL DEFAULT 1 CHECK (typeof(position)='integer' AND position>=1),
            PRIMARY KEY (project_id,reference_id),
            FOREIGN KEY (current_revision_id,project_id,reference_id) REFERENCES reference_revisions(id,project_id,reference_id)
        )""",
        """CREATE TABLE reference_revisions (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            reference_id INTEGER NOT NULL,
            revision INTEGER NOT NULL CHECK (typeof(revision)='integer' AND revision>=1),
            text TEXT NOT NULL CHECK (length(text)<=20000),
            source_note TEXT NOT NULL DEFAULT '' CHECK (length(source_note)<=1000),
            source_status TEXT NOT NULL DEFAULT 'unprovided' CHECK (source_status IN ('unprovided','pending','owner_noted')),
            content_hash TEXT NOT NULL,
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id), UNIQUE (id,project_id,reference_id), UNIQUE (project_id,reference_id,revision),
            FOREIGN KEY (project_id,reference_id) REFERENCES project_reference_links(project_id,reference_id)
        )""",
        """CREATE TABLE outline_revisions (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES production_projects(id),
            revision INTEGER NOT NULL CHECK (typeof(revision)='integer' AND revision>=1),
            item_count INTEGER NOT NULL CHECK (typeof(item_count)='integer' AND item_count BETWEEN 1 AND 60),
            input_hash TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('manual','model')),
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            job_id INTEGER,
            attempt_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id), UNIQUE (project_id,revision),
            FOREIGN KEY (job_id,project_id) REFERENCES production_jobs(id,project_id),
            FOREIGN KEY (attempt_id,job_id) REFERENCES job_attempts(id,job_id),
            CHECK ((job_id IS NULL)=(attempt_id IS NULL))
        )""",
        """CREATE TABLE production_chapters (
            id TEXT PRIMARY KEY NOT NULL,
            project_id INTEGER NOT NULL REFERENCES production_projects(id),
            current_revision_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1 CHECK (typeof(active)='integer' AND active IN (0,1)),
            needs_review INTEGER NOT NULL DEFAULT 0 CHECK (typeof(needs_review)='integer' AND needs_review IN (0,1)),
            review_reason TEXT,
            acknowledged_input_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id),
            FOREIGN KEY (current_revision_id,project_id,id) REFERENCES chapter_revisions(id,project_id,chapter_id)
        )""",
        """CREATE TABLE outline_items (
            outline_revision_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK (typeof(position)='integer' AND position BETWEEN 1 AND 60),
            chapter_id TEXT NOT NULL,
            title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
            goal TEXT NOT NULL CHECK (length(goal)<=2000),
            PRIMARY KEY (outline_revision_id,position), UNIQUE (outline_revision_id,chapter_id),
            FOREIGN KEY (outline_revision_id,project_id) REFERENCES outline_revisions(id,project_id),
            FOREIGN KEY (chapter_id,project_id) REFERENCES production_chapters(id,project_id)
        )""",
        """CREATE TABLE outline_confirmations (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            outline_revision_id INTEGER NOT NULL,
            input_hash TEXT NOT NULL,
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id),
            FOREIGN KEY (outline_revision_id,project_id) REFERENCES outline_revisions(id,project_id)
        )""",
        """CREATE TABLE chapter_revisions (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            chapter_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (typeof(revision)='integer' AND revision>=1),
            markdown TEXT NOT NULL CHECK (length(CAST(markdown AS BLOB))<=204800),
            content_hash TEXT,
            input_hash TEXT,
            source TEXT NOT NULL CHECK (source IN ('manual','model','candidate')),
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            job_id INTEGER,
            attempt_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id), UNIQUE (id,project_id,chapter_id), UNIQUE (project_id,chapter_id,revision),
            FOREIGN KEY (chapter_id,project_id) REFERENCES production_chapters(id,project_id),
            FOREIGN KEY (job_id,project_id) REFERENCES production_jobs(id,project_id),
            FOREIGN KEY (attempt_id,job_id) REFERENCES job_attempts(id,job_id),
            CHECK ((job_id IS NULL)=(attempt_id IS NULL))
        )""",
        """CREATE TABLE production_candidates (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES production_projects(id),
            kind TEXT NOT NULL CHECK (kind IN ('outline','chapter')),
            chapter_id TEXT,
            chapter_revision_id INTEGER,
            outline_revision_id INTEGER,
            base_body_revision_id INTEGER,
            input_hash TEXT NOT NULL,
            job_id INTEGER,
            attempt_id INTEGER,
            state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','adopted','rejected')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision)='integer' AND revision>=1),
            decided_by INTEGER REFERENCES admins(id),
            decided_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id), UNIQUE (id,project_id,chapter_id),
            FOREIGN KEY (chapter_id,project_id) REFERENCES production_chapters(id,project_id),
            FOREIGN KEY (chapter_revision_id,project_id,chapter_id) REFERENCES chapter_revisions(id,project_id,chapter_id),
            FOREIGN KEY (base_body_revision_id,project_id,chapter_id) REFERENCES chapter_revisions(id,project_id,chapter_id),
            FOREIGN KEY (outline_revision_id,project_id) REFERENCES outline_revisions(id,project_id),
            FOREIGN KEY (job_id,project_id) REFERENCES production_jobs(id,project_id),
            FOREIGN KEY (attempt_id,job_id) REFERENCES job_attempts(id,job_id),
            CHECK ((job_id IS NULL)=(attempt_id IS NULL)),
            CHECK ((kind='chapter' AND chapter_id IS NOT NULL AND chapter_revision_id IS NOT NULL AND outline_revision_id IS NULL)
                OR (kind='outline' AND outline_revision_id IS NOT NULL AND chapter_id IS NULL AND chapter_revision_id IS NULL AND base_body_revision_id IS NULL))
        )""",
        """CREATE TABLE draft_checks (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES production_projects(id),
            project_revision INTEGER NOT NULL CHECK (typeof(project_revision)='integer' AND project_revision>=1),
            input_hash TEXT NOT NULL,
            findings_json TEXT NOT NULL CHECK (json_valid(findings_json) AND json_type(findings_json)='array'),
            blocking INTEGER NOT NULL CHECK (typeof(blocking)='integer' AND blocking IN (0,1)),
            human_review_required INTEGER NOT NULL DEFAULT 1 CHECK (human_review_required=1),
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id), UNIQUE (id,project_id,input_hash)
        )""",
        """CREATE TABLE production_snapshots (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES production_projects(id),
            project_revision INTEGER NOT NULL CHECK (typeof(project_revision)='integer' AND project_revision>=1),
            chapter_count INTEGER NOT NULL CHECK (typeof(chapter_count)='integer' AND chapter_count BETWEEN 1 AND 60),
            reference_count INTEGER NOT NULL CHECK (typeof(reference_count)='integer' AND reference_count BETWEEN 0 AND 10),
            chapter_ref_kind TEXT GENERATED ALWAYS AS ('chapter') VIRTUAL,
            reference_ref_kind TEXT GENERATED ALWAYS AS (CASE WHEN reference_count>0 THEN 'reference' END) VIRTUAL,
            brief_revision_id INTEGER NOT NULL,
            outline_revision_id INTEGER NOT NULL,
            check_id INTEGER NOT NULL,
            input_hash TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json)='object'),
            rendering_version TEXT NOT NULL DEFAULT '1',
            prompt_version TEXT NOT NULL DEFAULT '1',
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (id,project_id), UNIQUE (project_id,snapshot_hash),
            FOREIGN KEY (brief_revision_id,project_id) REFERENCES brief_revisions(id,project_id),
            FOREIGN KEY (outline_revision_id,project_id) REFERENCES outline_revisions(id,project_id),
            FOREIGN KEY (check_id,project_id,input_hash) REFERENCES draft_checks(id,project_id,input_hash),
            FOREIGN KEY (id,chapter_ref_kind,chapter_count)
                REFERENCES production_snapshot_refs(snapshot_id,kind,position) DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (id,reference_ref_kind,reference_count)
                REFERENCES production_snapshot_refs(snapshot_id,kind,position) DEFERRABLE INITIALLY DEFERRED
        )""",
        """CREATE TABLE production_snapshot_refs (
            snapshot_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('chapter','reference')),
            chapter_id TEXT,
            chapter_revision_id INTEGER,
            reference_revision_id INTEGER,
            position INTEGER NOT NULL DEFAULT 1 CHECK (typeof(position)='integer' AND position>=1),
            previous_position INTEGER GENERATED ALWAYS AS (CASE WHEN position>1 THEN position-1 END) VIRTUAL,
            FOREIGN KEY (snapshot_id,project_id) REFERENCES production_snapshots(id,project_id),
            FOREIGN KEY (chapter_revision_id,project_id,chapter_id) REFERENCES chapter_revisions(id,project_id,chapter_id),
            FOREIGN KEY (reference_revision_id,project_id) REFERENCES reference_revisions(id,project_id),
            FOREIGN KEY (snapshot_id,kind,previous_position)
                REFERENCES production_snapshot_refs(snapshot_id,kind,position) DEFERRABLE INITIALLY DEFERRED,
            CHECK ((kind='chapter' AND chapter_id IS NOT NULL AND chapter_revision_id IS NOT NULL AND reference_revision_id IS NULL)
                OR (kind='reference' AND reference_revision_id IS NOT NULL AND chapter_id IS NULL AND chapter_revision_id IS NULL)),
            UNIQUE (snapshot_id,kind,position), UNIQUE (snapshot_id,chapter_id), UNIQUE (snapshot_id,reference_revision_id)
        )""",
        """CREATE TABLE production_jobs (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES production_projects(id),
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            kind TEXT NOT NULL CHECK (kind IN ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate','draft_validate','connection_test')),
            project_revision INTEGER CHECK (project_revision IS NULL OR (typeof(project_revision)='integer' AND project_revision>=1)),
            parent_job_id INTEGER REFERENCES production_jobs(id),
            state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','succeeded','failed','cancelled','interrupted')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision)='integer' AND revision>=1),
            reason_code TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (typeof(cancel_requested)='integer' AND cancel_requested IN (0,1)),
            input_hash TEXT NOT NULL,
            input_json TEXT NOT NULL CHECK (json_valid(input_json) AND json_type(input_json)='object'),
            config_json TEXT NOT NULL CHECK (json_valid(config_json) AND json_type(config_json)='object'),
            chapter_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(chapter_ids_json) AND json_type(chapter_ids_json)='array'),
            request_digest TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            started_at TEXT,
            finished_at TEXT,
            UNIQUE (id,project_id),
            CHECK ((kind='connection_test' AND project_id IS NULL AND project_revision IS NULL)
                OR (kind!='connection_test' AND project_id IS NOT NULL)),
            CHECK (parent_job_id IS NULL OR parent_job_id!=id)
        )""",
        """CREATE UNIQUE INDEX production_jobs_project_slot ON production_jobs(project_id)
            WHERE state IN ('queued','running') AND kind IN ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate')""",
        "CREATE UNIQUE INDEX production_jobs_running_slot ON production_jobs((1)) WHERE state='running'",
        "CREATE INDEX production_jobs_claim ON production_jobs(state,id)",
        "CREATE INDEX production_jobs_project_page ON production_jobs(project_id,id DESC)",
        """CREATE TABLE job_attempts (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES production_jobs(id),
            attempt_number INTEGER NOT NULL CHECK (typeof(attempt_number)='integer' AND attempt_number>=1),
            attempt_token TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            finished_at TEXT,
            state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running','succeeded','failed','cancelled','interrupted')),
            UNIQUE (id,job_id), UNIQUE (job_id,attempt_number)
        )""",
        "CREATE UNIQUE INDEX job_attempts_active ON job_attempts(job_id) WHERE state='running'",
        "CREATE INDEX job_attempts_heartbeat ON job_attempts(state,heartbeat_at)",
        """CREATE TABLE job_steps (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES production_jobs(id),
            attempt_id INTEGER NOT NULL,
            project_id INTEGER,
            step_number INTEGER NOT NULL CHECK (typeof(step_number)='integer' AND step_number>=1),
            kind TEXT NOT NULL CHECK (kind IN ('outline','chapter','validate','connection_test')),
            chapter_id TEXT,
            input_hash TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','succeeded','failed','cancelled','interrupted')),
            reason_code TEXT,
            outline_revision_id INTEGER,
            chapter_revision_id INTEGER,
            candidate_id INTEGER,
            check_id INTEGER,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE (id,job_id), UNIQUE (attempt_id,step_number),
            FOREIGN KEY (attempt_id,job_id) REFERENCES job_attempts(id,job_id),
            FOREIGN KEY (job_id,project_id) REFERENCES production_jobs(id,project_id),
            FOREIGN KEY (chapter_id,project_id) REFERENCES production_chapters(id,project_id),
            FOREIGN KEY (outline_revision_id,project_id) REFERENCES outline_revisions(id,project_id),
            FOREIGN KEY (chapter_revision_id,project_id,chapter_id) REFERENCES chapter_revisions(id,project_id,chapter_id),
            FOREIGN KEY (candidate_id,project_id) REFERENCES production_candidates(id,project_id),
            FOREIGN KEY (candidate_id,project_id,chapter_id) REFERENCES production_candidates(id,project_id,chapter_id),
            FOREIGN KEY (check_id,project_id) REFERENCES draft_checks(id,project_id),
            CHECK ((kind='connection_test' AND project_id IS NULL AND chapter_id IS NULL)
                OR (kind!='connection_test' AND project_id IS NOT NULL)),
            CHECK ((kind='chapter' AND chapter_id IS NOT NULL) OR (kind!='chapter' AND chapter_id IS NULL AND chapter_revision_id IS NULL)),
            CHECK (project_id IS NOT NULL OR (outline_revision_id IS NULL AND chapter_revision_id IS NULL AND candidate_id IS NULL AND check_id IS NULL))
        )""",
        """CREATE TABLE model_calls (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES production_jobs(id),
            attempt_id INTEGER NOT NULL,
            step_id INTEGER,
            call_number INTEGER NOT NULL CHECK (typeof(call_number)='integer' AND call_number>=1),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            endpoint_fingerprint TEXT,
            prompt_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            request_hash TEXT,
            provider_request_id TEXT,
            status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','successful','rejected','unknown')),
            error_code TEXT,
            reserved_tokens INTEGER NOT NULL CHECK (typeof(reserved_tokens)='integer' AND reserved_tokens>=0),
            input_tokens INTEGER CHECK (input_tokens IS NULL OR (typeof(input_tokens)='integer' AND input_tokens>=0)),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR (typeof(output_tokens)='integer' AND output_tokens>=0)),
            price_json TEXT CHECK (price_json IS NULL OR (json_valid(price_json) AND json_type(price_json)='object')),
            response_text TEXT CHECK (response_text IS NULL OR length(CAST(response_text AS BLOB))<=204800),
            response_hash TEXT,
            response_disposition TEXT CHECK (response_disposition IS NULL OR response_disposition IN ('active','isolated')),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE (id,job_id), UNIQUE (attempt_id,call_number),
            FOREIGN KEY (attempt_id,job_id) REFERENCES job_attempts(id,job_id),
            FOREIGN KEY (step_id,job_id) REFERENCES job_steps(id,job_id),
            CHECK ((input_tokens IS NULL)=(output_tokens IS NULL))
        )""",
        "CREATE UNIQUE INDEX model_calls_sent_slot ON model_calls((1)) WHERE status='sent'",
        "CREATE INDEX model_calls_job_status ON model_calls(job_id,status,id)",
        """CREATE TABLE token_reservations (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES production_jobs(id),
            call_id INTEGER UNIQUE,
            reserved_tokens INTEGER NOT NULL CHECK (typeof(reserved_tokens)='integer' AND reserved_tokens>=0),
            used_tokens INTEGER CHECK (used_tokens IS NULL OR (typeof(used_tokens)='integer' AND used_tokens>=0)),
            released_tokens INTEGER NOT NULL DEFAULT 0 CHECK (typeof(released_tokens)='integer' AND released_tokens>=0 AND released_tokens<=reserved_tokens),
            token_limit INTEGER NOT NULL DEFAULT 200000 CHECK (typeof(token_limit)='integer' AND token_limit>=1),
            state TEXT NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved','settled','unknown','released')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            settled_at TEXT,
            FOREIGN KEY (call_id,job_id) REFERENCES model_calls(id,job_id)
        )""",
        "CREATE UNIQUE INDEX token_reservations_job_total ON token_reservations(job_id) WHERE call_id IS NULL",
        """CREATE TABLE job_requests (
            id INTEGER PRIMARY KEY,
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            action TEXT NOT NULL CHECK (action IN ('submit','cancel','resume','reconcile')),
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            job_id INTEGER NOT NULL REFERENCES production_jobs(id),
            call_id INTEGER,
            input_tokens INTEGER CHECK (input_tokens IS NULL OR (typeof(input_tokens)='integer' AND input_tokens>=0)),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR (typeof(output_tokens)='integer' AND output_tokens>=0)),
            accept_retry_risk INTEGER NOT NULL DEFAULT 0 CHECK (typeof(accept_retry_risk)='integer' AND accept_retry_risk IN (0,1)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
            UNIQUE (actor_admin_id,action,idempotency_key),
            FOREIGN KEY (call_id,job_id) REFERENCES model_calls(id,job_id),
            CHECK ((input_tokens IS NULL)=(output_tokens IS NULL)),
            CHECK ((action='reconcile' AND call_id IS NOT NULL) OR
                (action!='reconcile' AND call_id IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND accept_retry_risk=0))
        )""",
        """CREATE TABLE worker_heartbeats (
            worker_id TEXT PRIMARY KEY NOT NULL,
            heartbeat_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            job_id INTEGER REFERENCES production_jobs(id),
            attempt_id INTEGER,
            FOREIGN KEY (attempt_id,job_id) REFERENCES job_attempts(id,job_id),
            CHECK ((job_id IS NULL)=(attempt_id IS NULL))
        )""",
        "CREATE INDEX production_projects_page ON production_projects(status,id DESC)",
        "CREATE INDEX production_candidates_pending ON production_candidates(project_id,state,id)",
    )
    for statement in statements:
        connection.execute(statement)

    # Immutable declared cardinalities are the construction boundary: unique
    # positions 1..N can be filled once, then the collection cannot grow. Binding
    # an outline requires all N positions, without mutating a source row to seal.
    # Snapshot deferred FKs require the last position plus each predecessor:
    # incomplete assembly cannot COMMIT, and complete assembly cannot grow.
    connection.execute("""CREATE TRIGGER outline_items_collection_insert BEFORE INSERT ON outline_items
        WHEN NOT EXISTS (SELECT 1 FROM outline_revisions WHERE id=NEW.outline_revision_id
            AND project_id=NEW.project_id AND NEW.position<=item_count)
        BEGIN SELECT RAISE(ABORT,'outside immutable outline collection'); END""")
    connection.execute("""CREATE TRIGGER production_snapshot_refs_collection_insert
        BEFORE INSERT ON production_snapshot_refs
        WHEN NOT EXISTS (SELECT 1 FROM production_snapshots WHERE id=NEW.snapshot_id
            AND project_id=NEW.project_id AND NEW.position<=CASE NEW.kind
                WHEN 'chapter' THEN chapter_count WHEN 'reference' THEN reference_count ELSE 0 END)
        BEGIN SELECT RAISE(ABORT,'outside immutable snapshot collection'); END""")

    outline_bindings = {
        "production_projects": "current_outline_revision_id",
        "outline_confirmations": "outline_revision_id",
        "production_candidates": "outline_revision_id",
        "production_snapshots": "outline_revision_id",
        "job_steps": "outline_revision_id",
    }
    for table, column in outline_bindings.items():
        for operation in ("INSERT", "UPDATE"):
            connection.execute(f"""CREATE TRIGGER {table}_complete_outline_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN NEW.{column} IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM outline_revisions AS parent WHERE parent.id=NEW.{column}
                    AND parent.item_count=(SELECT COUNT(*) FROM outline_items WHERE outline_revision_id=parent.id))
                BEGIN SELECT RAISE(ABORT,'incomplete outline collection'); END""")
    connection.execute("""CREATE TRIGGER production_snapshots_outline_count_insert
        BEFORE INSERT ON production_snapshots
        WHEN NOT EXISTS (SELECT 1 FROM outline_revisions WHERE id=NEW.outline_revision_id
            AND project_id=NEW.project_id AND item_count=NEW.chapter_count)
        BEGIN SELECT RAISE(ABORT,'inconsistent snapshot outline collection'); END""")
    connection.execute("""CREATE TRIGGER production_snapshot_refs_chapter_insert
        BEFORE INSERT ON production_snapshot_refs WHEN NEW.kind='chapter' AND NOT EXISTS (
            SELECT 1 FROM production_snapshots AS snapshot JOIN outline_items AS item
            ON item.outline_revision_id=snapshot.outline_revision_id
            WHERE snapshot.id=NEW.snapshot_id AND item.position=NEW.position AND item.chapter_id=NEW.chapter_id)
        BEGIN SELECT RAISE(ABORT,'inconsistent snapshot chapter collection'); END""")

    # Immutable rows are the source of truth; status belongs on their containers.
    for table in ("brief_revisions", "reference_revisions", "outline_revisions", "outline_items",
                  "outline_confirmations", "chapter_revisions", "draft_checks", "production_snapshots",
                  "production_snapshot_refs", "job_requests"):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER {table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable production source'); END""")

    # Mutable containers must never move identity or replace their source data.
    frozen_columns = {
        "production_projects": ("id", "actor_admin_id", "created_at"),
        "project_reference_links": ("project_id", "reference_id"),
        "production_chapters": ("id", "project_id", "created_at"),
        "production_candidates": ("id", "project_id", "kind", "chapter_id", "chapter_revision_id",
            "outline_revision_id", "base_body_revision_id", "input_hash", "job_id", "attempt_id", "created_at"),
        "production_jobs": ("id", "project_id", "actor_admin_id", "kind", "project_revision", "parent_job_id",
            "input_hash", "input_json", "config_json", "chapter_ids_json", "request_digest", "created_at"),
        "job_attempts": ("id", "job_id", "attempt_number", "attempt_token", "worker_id", "started_at"),
        "job_steps": ("id", "job_id", "attempt_id", "project_id", "step_number", "kind", "chapter_id", "input_hash"),
        "model_calls": ("id", "job_id", "attempt_id", "step_id", "call_number", "provider", "model",
            "endpoint_fingerprint", "prompt_version", "input_hash", "request_hash", "reserved_tokens", "price_json", "started_at"),
        "token_reservations": ("id", "job_id", "call_id", "reserved_tokens", "token_limit", "created_at"),
    }
    for table, columns in frozen_columns.items():
        condition = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
        connection.execute(f"""CREATE TRIGGER {table}_identity_update BEFORE UPDATE ON {table}
            WHEN {condition} BEGIN SELECT RAISE(ABORT,'immutable production identity'); END""")
        connection.execute(f"""CREATE TRIGGER {table}_history_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'immutable production history'); END""")

    # REPLACE can silently delete a conflicting row without firing DELETE
    # triggers when recursive_triggers=OFF (SQLite's default). Protect all
    # primary/unique identities before insertion as well. These are schema
    # identifiers only, never caller-supplied SQL. Partial slots are below.
    for table in (*frozen_columns, "brief_revisions", "reference_revisions", "outline_revisions",
                  "outline_items", "outline_confirmations", "chapter_revisions", "draft_checks",
                  "production_snapshots", "production_snapshot_refs", "job_requests"):
        keys = []
        primary = [row[1] for row in sorted(connection.execute(f"PRAGMA table_info({table})"), key=lambda row: row[5]) if row[5]]
        if primary:
            keys.append(primary)
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if index[2] and not index[4]:
                keys.append([row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")])
        condition = " OR ".join("(" + " AND ".join(f"{col}=NEW.{col}" for col in key) + ")" for key in keys)
        connection.execute(f"""CREATE TRIGGER {table}_immutable_insert BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT,'immutable production identity'); END""")

    partial_slots = {
        "production_jobs": """(NEW.state='running' AND state='running') OR
            (NEW.state IN ('queued','running') AND state IN ('queued','running') AND project_id=NEW.project_id
             AND NEW.kind IN ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate')
             AND kind IN ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate'))""",
        "job_attempts": "NEW.state='running' AND state='running' AND job_id=NEW.job_id",
        "model_calls": "NEW.status='sent' AND status='sent'",
        "token_reservations": "NEW.call_id IS NULL AND call_id IS NULL AND job_id=NEW.job_id",
    }
    for table, condition in partial_slots.items():
        connection.execute(f"""CREATE TRIGGER {table}_slot_insert BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT,'occupied production slot'); END""")
        connection.execute(f"""CREATE TRIGGER {table}_slot_update BEFORE UPDATE ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE id!=OLD.id AND ({condition}))
            BEGIN SELECT RAISE(ABORT,'occupied production slot'); END""")

    connection.execute("""CREATE TRIGGER production_jobs_state_update BEFORE UPDATE OF state ON production_jobs
        WHEN NEW.state!=OLD.state AND NOT (
            (OLD.state='queued' AND NEW.state IN ('running','cancelled','failed')) OR
            (OLD.state='running' AND NEW.state IN ('succeeded','failed','cancelled','interrupted')))
        BEGIN SELECT RAISE(ABORT,'invalid production job transition'); END""")
    connection.execute("""CREATE TRIGGER production_jobs_parent_insert BEFORE INSERT ON production_jobs
        WHEN NEW.parent_job_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM production_jobs WHERE id=NEW.parent_job_id AND project_id IS NEW.project_id)
        BEGIN SELECT RAISE(ABORT,'inconsistent parent job project'); END""")
    connection.execute("""CREATE TRIGGER job_steps_project_insert BEFORE INSERT ON job_steps
        WHEN NOT EXISTS (SELECT 1 FROM production_jobs WHERE id=NEW.job_id AND project_id IS NEW.project_id)
        BEGIN SELECT RAISE(ABORT,'inconsistent step project'); END""")
    connection.execute("""CREATE TRIGGER job_steps_candidate_insert BEFORE INSERT ON job_steps
        WHEN NEW.candidate_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM production_candidates WHERE id=NEW.candidate_id
            AND project_id=NEW.project_id AND kind=NEW.kind AND chapter_id IS NEW.chapter_id)
        BEGIN SELECT RAISE(ABORT,'inconsistent step candidate'); END""")
    connection.execute("""CREATE TRIGGER job_steps_candidate_update BEFORE UPDATE OF candidate_id ON job_steps
        WHEN NEW.candidate_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM production_candidates WHERE id=NEW.candidate_id
            AND project_id=NEW.project_id AND kind=NEW.kind AND chapter_id IS NEW.chapter_id)
        BEGIN SELECT RAISE(ABORT,'inconsistent step candidate'); END""")
    connection.execute("""CREATE TRIGGER model_calls_step_insert BEFORE INSERT ON model_calls
        WHEN NEW.step_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM job_steps WHERE id=NEW.step_id AND job_id=NEW.job_id AND attempt_id=NEW.attempt_id)
        BEGIN SELECT RAISE(ABORT,'inconsistent call attempt'); END""")
    connection.execute("""CREATE TRIGGER model_calls_response_update BEFORE UPDATE ON model_calls
        WHEN OLD.response_text IS NOT NULL AND (
            NEW.response_text IS NOT OLD.response_text OR NEW.response_hash IS NOT OLD.response_hash OR
            NEW.response_disposition IS NOT OLD.response_disposition)
        BEGIN SELECT RAISE(ABORT,'immutable model response'); END""")
