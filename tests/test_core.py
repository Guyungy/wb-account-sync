"""Isolated contract tests; all client data and locks live in temporary homes."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from wb_account_sync import cli, core, safety


SOURCE = "00000000-0000-0000-0000-000000000001"
TARGET = "00000000-0000-0000-0000-000000000002"
PLANNED_IDS = ["session-a", "session-b"]
SESSION_ROWS = [
    ("session-a", SOURCE, None, 0, "First ordinary session"),
    ("session-b", SOURCE, None, 0, "Second ordinary session"),
    ("session-deleted", SOURCE, 123, 0, "Deleted session"),
    ("session-background", SOURCE, None, 1, "Background session"),
    ("session-unclassified", SOURCE, None, None, "Unknown background flag"),
    ("session-target", TARGET, None, 0, "Existing target session"),
]


class ObservedConnection:
    """Delegate to real SQLite, optionally failing the second ownership UPDATE."""

    def __init__(self, connection, fail_update=None):
        self.connection = connection
        self.fail_update = fail_update
        self.updates = []
        self.completed_updates = []
        self.commits = 0
        self.rollbacks = 0

    @property
    def row_factory(self):
        return self.connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.connection.row_factory = value

    def execute(self, sql, parameters=()):
        is_update = sql.lstrip().upper().startswith("UPDATE SESSIONS ")
        if is_update:
            self.updates.append((sql, parameters))
            if len(self.updates) == self.fail_update:
                raise sqlite3.OperationalError("injected second UPDATE failure")
        cursor = self.connection.execute(sql, parameters)
        if is_update:
            self.completed_updates.append((sql, parameters))
        return cursor

    def commit(self):
        self.connection.commit()
        self.commits += 1

    def rollback(self):
        self.connection.rollback()
        self.rollbacks += 1

    def close(self):
        self.connection.close()


class TemporaryClientTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wb-core-test-")
        self.addCleanup(temporary.cleanup)
        # Resolve macOS /var aliases before any path reaches the safety guards.
        self.root = Path(temporary.name).resolve()
        self.fake_user_home = self.root / "fake-user-home"
        self.fake_user_home.mkdir(mode=0o700)
        self.home = self.root / "synthetic-client"
        self.create_client(self.home)
        self.db = self.home / "workbuddy.db"
        self.snapshot = self.home / "storage" / "skeleton" / "account-snapshot.json"
        self.state = self.root / "operation-state"
        home_patch = mock.patch.object(safety.Path, "home", return_value=self.fake_user_home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        process_patch = mock.patch.object(
            safety.subprocess, "run",
            side_effect=AssertionError("Real process inspection is forbidden in core tests"),
        )
        self.process_run = process_patch.start()
        self.addCleanup(process_patch.stop)

    def tearDown(self):
        self.process_run.assert_not_called()

    def create_client(self, home):
        home.mkdir(mode=0o700)
        snapshot_dir = home / "storage" / "skeleton"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "account-snapshot.json").write_text(
            json.dumps({"primary": {"uid": TARGET}}), encoding="utf-8",
        )
        con = sqlite3.connect(home / "workbuddy.db")
        try:
            con.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, "
                "deleted_at INTEGER, is_background_automation INTEGER, title TEXT)"
            )
            con.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", SESSION_ROWS)
            con.commit()
        finally:
            con.close()

    def execute(self, sql, parameters=(), *, home=None):
        con = sqlite3.connect((home or self.home) / "workbuddy.db")
        try:
            con.execute(sql, parameters)
            con.commit()
        finally:
            con.close()

    def rows(self, *, home=None):
        db = (home or self.home) / "workbuddy.db"
        con = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return {row["id"]: dict(row) for row in con.execute("SELECT * FROM sessions ORDER BY id")}
        finally:
            con.close()

    def tree_snapshot(self):
        result = {}
        for path in [self.root, *sorted(self.root.rglob("*"))]:
            info = path.lstat()
            self.assertFalse(path.is_symlink(), str(path))
            payload = None if path.is_dir() else path.read_bytes()
            result[str(path.relative_to(self.root))] = (
                info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns, payload,
            )
        return result

    def assert_no_side_effects(self, action):
        before = self.tree_snapshot()
        try:
            return action()
        finally:
            self.assertEqual(before, self.tree_snapshot())

    def assert_refused_without_side_effects(self, action, message):
        with self.assertRaisesRegex(safety.SafetyError, message):
            self.assert_no_side_effects(action)

    def assert_database_unchanged_on_refusal(self, action, message):
        before = self.db.read_bytes()
        rows = self.rows()
        with self.assertRaisesRegex(safety.SafetyError, message):
            action()
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(rows, self.rows())

    def plan(self):
        return core.make_plan(self.home, SOURCE, TARGET)

    def read_operations(self, plan):
        return (
            ("plan", self.plan),
            ("doctor", lambda: core.doctor(self.home)),
            ("verify", lambda: core.verify(plan)),
        )

    def move_planned_owners(self, owner):
        self.execute(
            "UPDATE sessions SET user_id=? WHERE id IN (?, ?)",
            (owner, *PLANNED_IDS),
        )

    def replace_database_inode(self):
        old_inode = self.db.stat().st_ino
        replacement = self.home / "replacement.db"
        replacement.write_bytes(self.db.read_bytes())
        replacement.replace(self.db)
        self.assertNotEqual(old_inode, self.db.stat().st_ino)

    def journal_path(self, plan):
        return self.state / "runs" / plan["plan_id"] / "journal.json"

    def assert_journal(self, plan, status):
        record = core.journal_read(self.journal_path(plan), plan)
        self.assertEqual(status, record["status"])
        return record

    @contextmanager
    def observe_writes(self, *, fail_update=None):
        real_connect = sqlite3.connect
        connections = []

        def connect(database, *args, **kwargs):
            con = real_connect(database, *args, **kwargs)
            if "mode=rw" not in str(database):
                return con
            wrapped = ObservedConnection(con, fail_update)
            connections.append(wrapped)
            return wrapped

        with mock.patch.object(core.sqlite3, "connect", side_effect=connect):
            yield connections

    def assert_one_write_connection(self, connections, *, updates, commits, rollbacks):
        self.assertEqual(1, len(connections))
        connection = connections[0]
        self.assertEqual(updates, len(connection.updates))
        self.assertEqual(commits, connection.commits)
        self.assertEqual(rollbacks, connection.rollbacks)
        return connection


class ReadOnlyCoreTests(TemporaryClientTestCase):
    def test_plan_has_zero_side_effects_and_is_deterministic(self):
        first = self.assert_no_side_effects(self.plan)
        second = self.assert_no_side_effects(self.plan)
        self.assertEqual(first, second)
        self.assertEqual(first, core.validate_plan(first))
        self.assertFalse(self.state.exists())
        self.assertFalse((self.fake_user_home / ".wb-account-sync-locks").exists())
        self.assertEqual(str(self.home), first["identity"]["home"])

    def test_plan_selects_only_ordinary_undeleted_source_sessions(self):
        plan = self.plan()
        self.assertEqual(PLANNED_IDS, [change["id"] for change in plan["changes"]])
        rows = self.rows()
        for change in plan["changes"]:
            row = rows[change["id"]]
            self.assertEqual(core.row_hash(row), change["before"])
            self.assertEqual(core.row_hash(row, TARGET), change["after"])
            self.assertNotEqual(change["before"], change["after"])

    def test_doctor_preserves_bytes_and_all_directory_mtimes(self):
        result = self.assert_no_side_effects(lambda: core.doctor(self.home))
        self.assertEqual(TARGET, result["current_account"])
        self.assertEqual(
            [{"user_id": SOURCE, "count": 2}, {"user_id": TARGET, "count": 1}],
            result["ordinary_session_counts"],
        )
        self.assertFalse(self.state.exists())
        self.assertFalse((self.fake_user_home / ".wb-account-sync-locks").exists())

    def test_verify_before_is_read_only_and_not_verified(self):
        plan = self.plan()
        result = self.assert_no_side_effects(lambda: core.verify(plan))
        self.assertEqual("before", result["phase"])
        self.assertFalse(result["verified"])
        self.assertEqual(2, result["sessions"])

    def test_verify_after_preserves_bytes_and_all_directory_mtimes(self):
        plan = self.plan()
        self.move_planned_owners(TARGET)
        result = self.assert_no_side_effects(lambda: core.verify(plan))
        self.assertEqual("after", result["phase"])
        self.assertTrue(result["verified"])
        self.assertFalse(self.state.exists())
        self.assertFalse((self.fake_user_home / ".wb-account-sync-locks").exists())

    def test_read_operations_reject_nonempty_wal_without_touching_it(self):
        plan = self.plan()
        Path(str(self.db) + "-wal").write_bytes(b"synthetic outstanding WAL")
        for name, operation in self.read_operations(plan):
            with self.subTest(operation=name):
                self.assert_refused_without_side_effects(operation, "Active WAL/journal")

    def test_read_operations_reject_nonempty_rollback_journal(self):
        plan = self.plan()
        Path(str(self.db) + "-journal").write_bytes(b"synthetic outstanding journal")
        for name, operation in self.read_operations(plan):
            with self.subTest(operation=name):
                self.assert_refused_without_side_effects(operation, "Active WAL/journal")

    def test_read_operations_reject_unknown_columns(self):
        plan = self.plan()
        self.execute("ALTER TABLE sessions ADD COLUMN unknown_client_field TEXT")
        for name, operation in self.read_operations(plan):
            with self.subTest(operation=name):
                self.assert_refused_without_side_effects(operation, "Unsupported sessions schema")

    def test_read_operations_reject_session_triggers(self):
        plan = self.plan()
        # Only this schema-refusal test uses a trigger; fault injection never does.
        self.execute(
            "CREATE TRIGGER session_update_guard BEFORE UPDATE ON sessions "
            "BEGIN SELECT 1; END"
        )
        for name, operation in self.read_operations(plan):
            with self.subTest(operation=name):
                self.assert_refused_without_side_effects(operation, "Session triggers")

    def test_plan_rejects_non_uuid_account_ids_without_side_effects(self):
        invalid = ("", "not-a-uuid", SOURCE.replace("-", ""), " " + SOURCE, None, 1)
        for value in invalid:
            for position in ("source", "target"):
                with self.subTest(value=value, position=position):
                    source, target = (value, TARGET) if position == "source" else (SOURCE, value)
                    self.assert_refused_without_side_effects(
                        lambda: core.make_plan(self.home, source, target), "lowercase UUIDs",
                    )

    def test_plan_rejects_equal_accounts(self):
        self.assert_refused_without_side_effects(
            lambda: core.make_plan(self.home, TARGET, TARGET), "Source and target must differ",
        )

    def test_plan_rejects_target_snapshot_mismatch(self):
        self.snapshot.write_text(json.dumps({"primary": {"uid": SOURCE}}), encoding="utf-8")
        self.assert_refused_without_side_effects(self.plan, "Target must match")

    def test_plan_rejects_non_uuid_snapshot(self):
        self.snapshot.write_text(json.dumps({"primary": {"uid": "not-a-uuid"}}), encoding="utf-8")
        self.assert_refused_without_side_effects(self.plan, "lowercase UUIDs")

    def test_verify_rejects_supported_schema_drift(self):
        plan = self.plan()
        self.execute("ALTER TABLE sessions ADD COLUMN custom_title TEXT")
        self.assert_refused_without_side_effects(lambda: core.verify(plan), "Schema changed")

    def test_verify_rejects_row_content_drift(self):
        plan = self.plan()
        self.execute("UPDATE sessions SET title=? WHERE id=?", ("Edited title", PLANNED_IDS[0]))
        self.assert_refused_without_side_effects(lambda: core.verify(plan), "Planned content changed")

    def test_verify_rejects_missing_planned_row(self):
        plan = self.plan()
        self.execute("DELETE FROM sessions WHERE id=?", (PLANNED_IDS[0],))
        self.assert_refused_without_side_effects(lambda: core.verify(plan), "planned session is missing")

    def test_verify_rejects_account_snapshot_drift(self):
        plan = self.plan()
        self.snapshot.write_text(json.dumps({"primary": {"uid": SOURCE}}), encoding="utf-8")
        self.assert_refused_without_side_effects(lambda: core.verify(plan), "Account snapshot changed")

    def test_verify_rejects_database_inode_change(self):
        plan = self.plan()
        self.replace_database_inode()
        self.assert_refused_without_side_effects(lambda: core.verify(plan), "database identity changed")

    def test_verify_rejects_tampered_plan_checksum(self):
        plan = deepcopy(self.plan())
        plan["changes"][0]["after"] = "0" * 64
        self.assert_refused_without_side_effects(lambda: core.verify(plan), "Plan checksum mismatch")

    def test_plan_checksum_covers_entire_canonical_payload(self):
        plan = self.plan()
        payload = {key: value for key, value in plan.items() if key != "plan_id"}
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), plan["plan_id"])
        self.assertIs(plan, core.validate_plan(plan))

    def test_validate_plan_rejects_rechecksummed_duplicate_session_ids(self):
        plan = deepcopy(self.plan())
        plan["changes"].append(deepcopy(plan["changes"][0]))
        plan["plan_id"] = safety.digest({key: value for key, value in plan.items() if key != "plan_id"})
        self.assert_refused_without_side_effects(
            lambda: core.validate_plan(plan), "Invalid/duplicate session ID",
        )

    def test_read_operations_reject_missing_required_column(self):
        plan = self.plan()
        self.execute("ALTER TABLE sessions DROP COLUMN user_id")
        for name, operation in self.read_operations(plan):
            with self.subTest(operation=name):
                self.assert_refused_without_side_effects(operation, "Unsupported sessions schema")

    def test_read_db_enforces_query_only_without_file_changes(self):
        def attempt_write():
            with core.read_db(self.home) as con:
                self.assertEqual(1, con.execute("PRAGMA query_only").fetchone()[0])
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("UPDATE sessions SET title=? WHERE id=?", ("Must not persist", PLANNED_IDS[0]))

        self.assert_no_side_effects(attempt_write)

    def test_read_db_rejects_wal_appearing_during_inspection(self):
        before = self.db.read_bytes()
        wal = Path(str(self.db) + "-wal")
        with self.assertRaisesRegex(safety.SafetyError, "Active WAL/journal"):
            with core.read_db(self.home) as con:
                con.execute("SELECT id FROM sessions").fetchall()
                wal.write_bytes(b"synthetic WAL appeared during inspection")
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(b"synthetic WAL appeared during inspection", wal.read_bytes())
        self.assertFalse(self.state.exists())

    def test_plan_rejects_account_change_during_inspection(self):
        original_read_db = core.read_db
        before = self.db.read_bytes()

        @contextmanager
        def change_snapshot_after_read(home):
            with original_read_db(home) as con:
                yield con
            self.snapshot.write_text(json.dumps({"primary": {"uid": SOURCE}}), encoding="utf-8")

        with mock.patch.object(core, "read_db", side_effect=change_snapshot_after_read):
            with self.assertRaisesRegex(safety.SafetyError, "Account changed while building"):
                self.plan()
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse(self.state.exists())


class WriteCoreTests(TemporaryClientTestCase):
    def setUp(self):
        super().setUp()
        # Keep this patch out of read-only and safety.require_offline tests.
        offline_patch = mock.patch.object(core, "require_offline", return_value=None)
        self.offline = offline_patch.start()
        self.addCleanup(offline_patch.stop)

    def apply(self, plan):
        return core.apply(plan, self.state, plan["plan_id"])

    def restore(self, plan):
        return core.restore(self.journal_path(plan).parent, plan["plan_id"])

    def interrupt_apply_second_update(self, plan):
        before = self.db.read_bytes()
        with self.observe_writes(fail_update=2) as connections:
            with self.assertRaisesRegex(sqlite3.OperationalError, "second UPDATE failure"):
                self.apply(plan)
        connection = self.assert_one_write_connection(
            connections, updates=2, commits=0, rollbacks=1,
        )
        self.assertEqual(1, len(connection.completed_updates))
        self.assertEqual(before, self.db.read_bytes())
        self.assert_journal(plan, "prepared")
        self.assertEqual("before", core.verify(plan)["phase"])

    def interrupt_apply_second_journal_write(self, plan):
        original = core.journal_write
        calls = []

        def fail_second(path, written_plan, status):
            calls.append(status)
            if len(calls) == 2:
                raise OSError("injected second journal write failure")
            original(path, written_plan, status)

        with mock.patch.object(core, "journal_write", side_effect=fail_second) as writer:
            with self.observe_writes() as connections:
                with self.assertRaisesRegex(OSError, "second journal write failure"):
                    self.apply(plan)
        self.assertEqual(2, writer.call_count)
        self.assertEqual(["prepared", "committed"], calls)
        self.assert_one_write_connection(connections, updates=2, commits=1, rollbacks=0)
        self.assert_journal(plan, "prepared")
        self.assertEqual("after", core.verify(plan)["phase"])
        for sid in PLANNED_IDS:
            self.assertEqual(TARGET, self.rows()[sid]["user_id"])

    def test_apply_changes_only_eligible_user_ids(self):
        before = self.rows()
        snapshot = self.snapshot.read_bytes()
        plan = self.plan()
        result = self.apply(plan)
        expected = deepcopy(before)
        for sid in PLANNED_IDS:
            expected[sid]["user_id"] = TARGET
        self.assertEqual(expected, self.rows())
        self.assertEqual(snapshot, self.snapshot.read_bytes())
        self.assertEqual("committed", result["status"])
        self.assertEqual(2, result["sessions"])
        self.assertTrue(result["verified"])
        self.assertFalse(result["undo_is_full_backup"])
        self.assert_journal(plan, "committed")
        self.assertEqual(0o700, self.state.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.journal_path(plan).stat().st_mode & 0o777)
        locks = self.fake_user_home / ".wb-account-sync-locks"
        self.assertEqual(1, len(list(locks.glob("*.lock"))))
        self.assertEqual(3, self.offline.call_count)

    def test_apply_wrong_confirmation_has_zero_side_effects(self):
        plan = self.plan()
        for confirmation in ("", plan["plan_id"][:12], "0" * 64):
            with self.subTest(confirmation=confirmation):
                self.assert_refused_without_side_effects(
                    lambda: core.apply(plan, self.state, confirmation), "full reviewed plan ID",
                )
        self.offline.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_prewrite_journal_failure_prevents_updates_and_commit(self):
        plan = self.plan()
        before = self.db.read_bytes()
        with mock.patch.object(core, "journal_write", side_effect=OSError("prepared journal unavailable")) as writer:
            with self.observe_writes() as connections:
                with self.assertRaisesRegex(OSError, "prepared journal unavailable"):
                    self.apply(plan)
        writer.assert_called_once_with(self.journal_path(plan), plan, "prepared")
        self.assert_one_write_connection(connections, updates=0, commits=0, rollbacks=1)
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse(self.journal_path(plan).exists())
        self.assertEqual("before", core.verify(plan)["phase"])

    def test_second_update_failure_is_atomic(self):
        plan = self.plan()
        before = self.rows()
        self.interrupt_apply_second_update(plan)
        self.assertEqual(before, self.rows())
        self.assertFalse(Path(str(self.db) + "-journal").exists())

    def test_prepared_before_recovers_by_reapplying_all_rows(self):
        plan = self.plan()
        self.interrupt_apply_second_update(plan)
        with self.observe_writes() as connections:
            result = self.apply(plan)
        self.assert_one_write_connection(connections, updates=2, commits=1, rollbacks=0)
        self.assertTrue(result["verified"])
        self.assert_journal(plan, "committed")

    def test_second_journal_failure_leaves_prepared_after_and_can_resume(self):
        plan = self.plan()
        self.interrupt_apply_second_journal_write(plan)
        before = self.db.read_bytes()
        with self.observe_writes() as connections:
            result = self.apply(plan)
        self.assert_one_write_connection(connections, updates=0, commits=1, rollbacks=0)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual("committed", result["status"])
        self.assertTrue(result["verified"])
        self.assert_journal(plan, "committed")

    def test_repeated_apply_is_idempotent_without_updates(self):
        plan = self.plan()
        first = self.apply(plan)
        before = self.db.read_bytes()
        with self.observe_writes() as connections:
            second = self.apply(plan)
        self.assertEqual(first, second)
        self.assertEqual(before, self.db.read_bytes())
        self.assert_one_write_connection(connections, updates=0, commits=1, rollbacks=0)
        self.assert_journal(plan, "committed")

    def test_after_without_journal_cannot_claim_apply_success(self):
        plan = self.plan()
        self.move_planned_owners(TARGET)
        with self.observe_writes() as connections:
            self.assert_database_unchanged_on_refusal(
                lambda: self.apply(plan), "After-state without this tool's journal",
            )
        self.assert_one_write_connection(connections, updates=0, commits=0, rollbacks=1)
        self.assertFalse(self.journal_path(plan).exists())

    def test_committed_journal_with_before_rows_is_rejected(self):
        plan = self.plan()
        self.apply(plan)
        self.move_planned_owners(SOURCE)
        self.assert_database_unchanged_on_refusal(
            lambda: self.apply(plan), "Committed journal disagrees",
        )
        self.assert_journal(plan, "committed")

    def test_apply_rejects_supported_schema_drift(self):
        plan = self.plan()
        self.execute("ALTER TABLE sessions ADD COLUMN custom_title TEXT")
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "Schema changed")
        self.assertFalse(self.journal_path(plan).exists())

    def test_apply_rejects_unknown_column(self):
        plan = self.plan()
        self.execute("ALTER TABLE sessions ADD COLUMN unknown_client_field TEXT")
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "Unsupported sessions schema")
        self.assertFalse(self.journal_path(plan).exists())

    def test_apply_rejects_row_content_drift(self):
        plan = self.plan()
        self.execute("UPDATE sessions SET title=? WHERE id=?", ("New content", PLANNED_IDS[0]))
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "Planned content changed")
        self.assertFalse(self.journal_path(plan).exists())

    def test_apply_rejects_partial_ownership_drift(self):
        plan = self.plan()
        self.execute("UPDATE sessions SET user_id=? WHERE id=?", (TARGET, PLANNED_IDS[0]))
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "partially applied")
        self.assertFalse(self.journal_path(plan).exists())

    def test_apply_rejects_missing_planned_row(self):
        plan = self.plan()
        self.execute("DELETE FROM sessions WHERE id=?", (PLANNED_IDS[0],))
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "planned session is missing")
        self.assertFalse(self.journal_path(plan).exists())

    def test_apply_rejects_new_eligible_source_session(self):
        plan = self.plan()
        self.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            ("session-added", SOURCE, None, 0, "New source session"),
        )
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "Source sessions changed")
        self.assertFalse(self.journal_path(plan).exists())

    def test_apply_rejects_account_identity_drift_without_side_effects(self):
        plan = self.plan()
        self.snapshot.write_text(json.dumps({"primary": {"uid": SOURCE}}), encoding="utf-8")
        self.assert_refused_without_side_effects(lambda: self.apply(plan), "Account snapshot changed")
        self.offline.assert_not_called()

    def test_apply_rejects_snapshot_hash_drift_even_with_same_account(self):
        plan = self.plan()
        self.snapshot.write_text(
            json.dumps({"primary": {"uid": TARGET}, "synthetic_revision": 2}), encoding="utf-8",
        )
        self.assert_refused_without_side_effects(lambda: self.apply(plan), "Account snapshot changed")
        self.offline.assert_not_called()

    def test_apply_rejects_database_inode_change_without_side_effects(self):
        plan = self.plan()
        self.replace_database_inode()
        self.assert_refused_without_side_effects(lambda: self.apply(plan), "database identity changed")
        self.offline.assert_not_called()

    def test_apply_rejects_nonempty_wal_before_creating_state(self):
        plan = self.plan()
        Path(str(self.db) + "-wal").write_bytes(b"synthetic outstanding WAL")
        self.assert_refused_without_side_effects(lambda: self.apply(plan), "Active WAL/journal")
        self.assertFalse(self.state.exists())

    def test_state_directory_cannot_be_reused_across_client_homes(self):
        first_plan = self.plan()
        self.apply(first_plan)
        second_home = self.root / "second-synthetic-client"
        self.create_client(second_home)
        second_plan = core.make_plan(second_home, SOURCE, TARGET)
        self.assertNotEqual(first_plan["plan_id"], second_plan["plan_id"])
        self.assert_refused_without_side_effects(
            lambda: core.apply(second_plan, self.state, second_plan["plan_id"]),
            "different client home",
        )
        self.assert_journal(first_plan, "committed")
        for sid in PLANNED_IDS:
            self.assertEqual(SOURCE, self.rows(home=second_home)[sid]["user_id"])

    def test_state_directory_must_be_outside_client_home_and_ancestors(self):
        plan = self.plan()
        for state in (self.home, self.home / "state", self.root):
            with self.subTest(state=state.name):
                self.assert_refused_without_side_effects(
                    lambda: core.apply(plan, state, plan["plan_id"]), "outside the client home",
                )

    def test_restore_only_reverts_user_id_and_preserves_unrelated_target_content(self):
        original = self.rows()
        plan = self.plan()
        self.apply(plan)
        self.execute(
            "UPDATE sessions SET title=? WHERE id=?",
            ("Target's independently edited content", "session-target"),
        )
        before_restore = self.rows()
        with self.observe_writes() as connections:
            result = self.restore(plan)
        self.assert_one_write_connection(connections, updates=2, commits=1, rollbacks=0)
        expected = deepcopy(before_restore)
        for sid in PLANNED_IDS:
            expected[sid]["user_id"] = SOURCE
            self.assertEqual(original[sid], expected[sid])
        self.assertEqual(expected, self.rows())
        self.assertEqual("restored", result["status"])
        self.assert_journal(plan, "restored")
        self.assertEqual("before", core.verify(plan)["phase"])

    def test_restore_refuses_to_overwrite_drifted_planned_target_content(self):
        plan = self.plan()
        self.apply(plan)
        self.execute("UPDATE sessions SET title=? WHERE id=?", ("New target content", PLANNED_IDS[0]))
        before = self.rows()
        journal_before = self.journal_path(plan).read_bytes()
        with self.observe_writes() as connections:
            self.assert_database_unchanged_on_refusal(
                lambda: self.restore(plan), "Planned content changed",
            )
        self.assert_one_write_connection(connections, updates=0, commits=0, rollbacks=1)
        self.assertEqual(before, self.rows())
        self.assertEqual(journal_before, self.journal_path(plan).read_bytes())
        self.assertEqual("New target content", self.rows()[PLANNED_IDS[0]]["title"])
        self.assert_journal(plan, "committed")

    def test_repeated_restore_is_idempotent_without_updates(self):
        plan = self.plan()
        self.apply(plan)
        first = self.restore(plan)
        before = self.db.read_bytes()
        with self.observe_writes() as connections:
            second = self.restore(plan)
        self.assertEqual(first, second)
        self.assertEqual(before, self.db.read_bytes())
        self.assert_one_write_connection(connections, updates=0, commits=1, rollbacks=0)
        self.assert_journal(plan, "restored")

    def test_restore_wrong_confirmation_has_zero_side_effects(self):
        plan = self.plan()
        self.apply(plan)
        self.offline.reset_mock()
        self.assert_refused_without_side_effects(
            lambda: core.restore(self.journal_path(plan).parent, "0" * 64), "full reviewed plan ID",
        )
        self.offline.assert_not_called()
        self.assert_journal(plan, "committed")

    def test_restore_rejects_account_drift_without_side_effects(self):
        plan = self.plan()
        self.apply(plan)
        self.snapshot.write_text(json.dumps({"primary": {"uid": SOURCE}}), encoding="utf-8")
        self.assert_refused_without_side_effects(lambda: self.restore(plan), "Account snapshot changed")
        self.assert_journal(plan, "committed")

    def test_restore_rejects_schema_drift(self):
        plan = self.plan()
        self.apply(plan)
        self.execute("ALTER TABLE sessions ADD COLUMN custom_title TEXT")
        self.assert_database_unchanged_on_refusal(lambda: self.restore(plan), "Schema changed")
        self.assert_journal(plan, "committed")

    def test_restore_rejects_database_inode_change_without_side_effects(self):
        plan = self.plan()
        self.apply(plan)
        self.replace_database_inode()
        self.assert_refused_without_side_effects(lambda: self.restore(plan), "database identity changed")
        self.assert_journal(plan, "committed")

    def test_restore_without_journal_cannot_claim_success(self):
        plan = self.plan()
        self.move_planned_owners(TARGET)
        self.assert_refused_without_side_effects(lambda: self.restore(plan), "regular JSON file")
        self.offline.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_restore_rejects_unexplained_complete_ownership_reversal(self):
        plan = self.plan()
        self.apply(plan)
        self.move_planned_owners(SOURCE)
        self.assert_database_unchanged_on_refusal(lambda: self.restore(plan), "Unexplained ownership reversal")
        self.assert_journal(plan, "committed")

    def test_second_restore_update_failure_is_atomic_and_retryable(self):
        plan = self.plan()
        self.apply(plan)
        before = self.db.read_bytes()
        with self.observe_writes(fail_update=2) as connections:
            with self.assertRaisesRegex(sqlite3.OperationalError, "second UPDATE failure"):
                self.restore(plan)
        connection = self.assert_one_write_connection(connections, updates=2, commits=0, rollbacks=1)
        self.assertEqual(1, len(connection.completed_updates))
        self.assertEqual(before, self.db.read_bytes())
        self.assert_journal(plan, "restoring")
        self.assertEqual("after", core.verify(plan)["phase"])
        result = self.restore(plan)
        self.assertEqual("restored", result["status"])
        self.assert_journal(plan, "restored")
        self.assertEqual("before", core.verify(plan)["phase"])

    def test_restored_run_cannot_be_reapplied(self):
        plan = self.plan()
        self.apply(plan)
        self.restore(plan)
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "cannot be reapplied")
        self.assert_journal(plan, "restored")

    def test_apply_rejects_session_trigger_without_updates(self):
        plan = self.plan()
        self.execute(
            "CREATE TRIGGER synthetic_session_guard BEFORE UPDATE ON sessions "
            "BEGIN SELECT 1; END"
        )
        with self.observe_writes() as connections:
            self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "Session triggers")
        self.assert_one_write_connection(connections, updates=0, commits=0, rollbacks=1)
        self.assertFalse(self.journal_path(plan).exists())

    def test_restore_rejects_nonempty_wal_without_touching_journal(self):
        plan = self.plan()
        self.apply(plan)
        self.offline.reset_mock()
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"synthetic pending restore WAL")
        self.assert_refused_without_side_effects(lambda: self.restore(plan), "Active WAL/journal")
        self.offline.assert_called_once_with()
        self.assert_journal(plan, "committed")

    def test_failure_after_prepared_journal_is_durable_can_resume(self):
        plan = self.plan()
        before = self.db.read_bytes()
        original_write = core.journal_write

        def fail_after_write(path, written_plan, status):
            original_write(path, written_plan, status)
            raise OSError("synthetic failure after prepared metadata persisted")

        with mock.patch.object(core, "journal_write", side_effect=fail_after_write) as writer:
            with self.observe_writes() as connections:
                with self.assertRaisesRegex(OSError, "prepared metadata persisted"):
                    self.apply(plan)
        writer.assert_called_once_with(self.journal_path(plan), plan, "prepared")
        self.assert_one_write_connection(connections, updates=0, commits=0, rollbacks=1)
        self.assertEqual(before, self.db.read_bytes())
        self.assert_journal(plan, "prepared")
        self.assertTrue(self.apply(plan)["verified"])
        self.assert_journal(plan, "committed")

    def test_precommit_offline_refusal_rolls_back_all_updates(self):
        plan = self.plan()
        before = self.db.read_bytes()
        self.offline.side_effect = [None, None, safety.SafetyError("synthetic client appeared")]
        with self.observe_writes() as connections:
            self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "synthetic client appeared")
        self.assert_one_write_connection(connections, updates=2, commits=0, rollbacks=1)
        self.assertEqual(before, self.db.read_bytes())
        self.assert_journal(plan, "prepared")
        self.offline.side_effect = None
        self.assertTrue(self.apply(plan)["verified"])

    def test_restore_final_journal_failure_resumes_without_repeating_updates(self):
        plan = self.plan()
        original_rows = self.rows()
        self.apply(plan)
        original_write = core.journal_write
        statuses = []

        def fail_final_write(path, written_plan, status):
            statuses.append(status)
            if status == "restored":
                raise OSError("synthetic restored journal failure")
            original_write(path, written_plan, status)

        with mock.patch.object(core, "journal_write", side_effect=fail_final_write):
            with self.observe_writes() as connections:
                with self.assertRaisesRegex(OSError, "restored journal failure"):
                    self.restore(plan)
        self.assertEqual(["restoring", "restored"], statuses)
        self.assert_one_write_connection(connections, updates=2, commits=1, rollbacks=0)
        self.assertEqual(original_rows, self.rows())
        self.assert_journal(plan, "restoring")
        before = self.db.read_bytes()
        with self.observe_writes() as retry_connections:
            result = self.restore(plan)
        self.assert_one_write_connection(retry_connections, updates=0, commits=1, rollbacks=0)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual("restored", result["status"])
        self.assert_journal(plan, "restored")

    def test_prepared_after_can_be_restored_without_finishing_apply(self):
        plan = self.plan()
        before = self.rows()
        self.interrupt_apply_second_journal_write(plan)
        result = self.restore(plan)
        self.assertEqual("restored", result["status"])
        self.assertEqual(before, self.rows())
        self.assert_journal(plan, "restored")

    def test_empty_plan_apply_and_restore_are_idempotent_without_updates(self):
        self.move_planned_owners(TARGET)
        plan = self.plan()
        self.assertEqual([], plan["changes"])
        before = self.db.read_bytes()
        with self.observe_writes() as connections:
            first = self.apply(plan)
            second = self.apply(plan)
            first_restore = self.restore(plan)
            second_restore = self.restore(plan)
        self.assertEqual(first, second)
        self.assertEqual(first_restore, second_restore)
        self.assertEqual("empty", first["phase"])
        self.assertTrue(first["verified"])
        self.assertEqual(0, first["sessions"])
        self.assertEqual(4, len(connections))
        for connection in connections:
            self.assertEqual([], connection.updates)
            self.assertEqual(1, connection.commits)
            self.assertEqual(0, connection.rollbacks)
        self.assertEqual(before, self.db.read_bytes())
        self.assert_journal(plan, "restored")

    def test_empty_plan_rejects_new_eligible_source_session(self):
        # Regression: apply's empty phase currently skips the live source-set check.
        self.move_planned_owners(TARGET)
        plan = self.plan()
        self.assertEqual([], plan["changes"])
        self.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            ("session-added-after-empty-plan", SOURCE, None, 0, "Unreviewed source session"),
        )
        self.assert_database_unchanged_on_refusal(lambda: self.apply(plan), "Source sessions changed")
        self.assertFalse(self.journal_path(plan).exists())

    def test_same_home_lock_blocks_writes_from_a_different_state_directory(self):
        plan = self.plan()
        first_state = safety.state_root(self.state, self.home)
        second_state = self.root / "second-operation-state"
        with safety.state_lock(first_state, self.home):
            self.assert_database_unchanged_on_refusal(
                lambda: core.apply(plan, second_state, plan["plan_id"]), "Another operation holds",
            )
        locks = self.fake_user_home / ".wb-account-sync-locks"
        token = hashlib.sha256(str(self.home).encode()).hexdigest()
        self.assertEqual([token + ".lock"], [path.name for path in locks.iterdir()])
        self.assertEqual(0o700, locks.stat().st_mode & 0o777)
        self.assertFalse((second_state / "runs" / plan["plan_id"] / "journal.json").exists())
        self.assertTrue(core.apply(plan, second_state, plan["plan_id"])["verified"])

    def test_different_synthetic_homes_use_independent_locks(self):
        second_home = self.root / "second-synthetic-client"
        self.create_client(second_home)
        first_root = safety.state_root(self.state, self.home)
        second_root = safety.state_root(self.root / "second-operation-state", second_home)
        with safety.state_lock(first_root, self.home):
            with safety.state_lock(second_root, second_home):
                locks = self.fake_user_home / ".wb-account-sync-locks"
                expected = {
                    hashlib.sha256(str(home).encode()).hexdigest() + ".lock"
                    for home in (self.home, second_home)
                }
                self.assertEqual(expected, {path.name for path in locks.iterdir()})
        self.offline.assert_not_called()


class CliTests(TemporaryClientTestCase):
    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def plan_arguments(self):
        return ("plan", "--home", str(self.home), "--source", SOURCE, "--target", TARGET)

    def export_plan(self):
        plan = self.plan()
        path = self.root / "reviewed-plan.json"
        safety.atomic_json(path, plan, exclusive=True)
        return plan, path

    def test_doctor_status_and_plan_stdout_have_zero_file_changes(self):
        commands = [
            ("doctor", "--home", str(self.home), "--json"),
            ("status", "--home", str(self.home), "--json"),
            self.plan_arguments(),
        ]
        for arguments in commands:
            with self.subTest(command=arguments[0]):
                code, stdout, stderr = self.assert_no_side_effects(lambda: self.invoke(*arguments))
                self.assertEqual(0, code)
                self.assertEqual("", stderr)
                result = json.loads(stdout)
                expected = self.plan() if arguments[0] == "plan" else core.doctor(self.home)
                self.assertEqual(expected, result)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.fake_user_home / ".wb-account-sync-locks").exists())

    def test_verify_exit_codes_distinguish_before_and_after_without_writes(self):
        plan, path = self.export_plan()
        code, stdout, stderr = self.assert_no_side_effects(
            lambda: self.invoke("verify", "--plan", str(path), "--json"),
        )
        self.assertEqual(3, code)
        self.assertEqual("", stderr)
        self.assertEqual("before", json.loads(stdout)["phase"])
        self.move_planned_owners(TARGET)
        code, stdout, stderr = self.assert_no_side_effects(
            lambda: self.invoke("verify", "--plan", str(path), "--json"),
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(plan["plan_id"], json.loads(stdout)["plan_id"])
        self.assertTrue(json.loads(stdout)["verified"])

    def test_explicit_plan_export_is_restrictive_and_never_overwritten(self):
        path = self.root / "export.json"
        before = self.db.read_bytes()
        arguments = (*self.plan_arguments(), "--output", str(path), "--json")
        code, stdout, stderr = self.invoke(*arguments)
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(json.loads(stdout), core.load_plan(path))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        exported = path.read_bytes()
        code, stdout, stderr = self.invoke(*arguments)
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertFalse(json.loads(stderr)["ok"])
        self.assertIn("operation failed", json.loads(stderr)["error"])
        self.assertEqual(exported, path.read_bytes())
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual([], list(self.root.glob(".export.json.*.tmp")))

    def test_plan_export_inside_client_home_is_refused_without_file_changes(self):
        code, stdout, stderr = self.assert_no_side_effects(
            lambda: self.invoke(
                *self.plan_arguments(), "--output", str(self.home / "plan.json"), "--json",
            ),
        )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("outside the client data directory", json.loads(stderr)["error"])

    def test_apply_requires_explicit_full_confirmation(self):
        plan, path = self.export_plan()
        arguments = ("apply", "--plan", str(path), "--state-dir", str(self.state))
        with self.assertRaises(SystemExit) as missing:
            self.assert_no_side_effects(lambda: self.invoke(*arguments))
        self.assertEqual(2, missing.exception.code)
        for confirmation in ("yes", plan["plan_id"][:12]):
            with self.subTest(confirmation=confirmation):
                code, stdout, stderr = self.assert_no_side_effects(
                    lambda: self.invoke(*arguments, "--confirm", confirmation, "--json"),
                )
                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertIn("full reviewed plan ID", json.loads(stderr)["error"])

    def test_apply_rejects_tampered_plan_before_any_process_or_file_changes(self):
        plan, path = self.export_plan()
        plan["changes"][0]["after"] = "0" * 64
        path.write_text(json.dumps(plan), encoding="utf-8")
        code, stdout, stderr = self.assert_no_side_effects(
            lambda: self.invoke(
                "apply", "--plan", str(path), "--state-dir", str(self.state),
                "--confirm", plan["plan_id"], "--json",
            ),
        )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("Plan checksum mismatch", json.loads(stderr)["error"])

    def test_apply_verify_restore_round_trip_uses_only_synthetic_data(self):
        before = self.rows()
        plan, path = self.export_plan()
        with mock.patch.object(core, "require_offline", return_value=None) as offline:
            code, stdout, stderr = self.invoke(
                "apply", "--plan", str(path), "--state-dir", str(self.state),
                "--confirm", plan["plan_id"], "--json",
            )
            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            applied = json.loads(stdout)
            self.assertEqual("committed", applied["status"])
            self.assertTrue(applied["verified"])
            code, stdout, stderr = self.assert_no_side_effects(
                lambda: self.invoke("verify", "--plan", str(path), "--json"),
            )
            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertEqual("after", json.loads(stdout)["phase"])
            code, stdout, stderr = self.invoke(
                "restore", "--run-dir", applied["run_dir"], "--confirm", plan["plan_id"], "--json",
            )
            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertEqual("restored", json.loads(stdout)["status"])
            self.assertEqual(6, offline.call_count)
        self.assertEqual(before, self.rows())
        self.assert_journal(plan, "restored")
        self.assertTrue((self.fake_user_home / ".wb-account-sync-locks").is_dir())

    def test_sqlite_errors_are_sanitized_at_cli_boundary(self):
        sensitive_detail = "synthetic private SQL and session payload"
        with mock.patch.object(core, "doctor", side_effect=sqlite3.OperationalError(sensitive_detail)):
            code, stdout, stderr = self.assert_no_side_effects(
                lambda: self.invoke("doctor", "--home", str(self.home), "--json"),
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertFalse(json.loads(stderr)["ok"])
        self.assertIn("OperationalError: operation failed", json.loads(stderr)["error"])
        self.assertNotIn(sensitive_detail, stderr)

    def test_retired_commands_refuse_without_processes_or_file_changes(self):
        for command in sorted(cli.RETIRED):
            with self.subTest(command=command):
                code, stdout, stderr = self.assert_no_side_effects(lambda: self.invoke(command))
                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertIn("legacy command is disabled", stderr)


class OfflineSafetyTests(unittest.TestCase):
    def setUp(self):
        platform_patch = mock.patch.object(safety.sys, "platform", "darwin")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        pid_patch = mock.patch.object(safety.os, "getpid", return_value=100)
        pid_patch.start()
        self.addCleanup(pid_patch.stop)
        process_patch = mock.patch.object(safety.subprocess, "run")
        self.process_run = process_patch.start()
        self.addCleanup(process_patch.stop)
        self.process_run.return_value = subprocess.CompletedProcess(
            ["/bin/ps", "-Ao", "pid=,command="], 0, stdout="200 /usr/bin/synthetic-idle\n", stderr="",
        )

    def test_non_darwin_platforms_refuse_without_spawning_processes(self):
        for platform in ("linux", "win32", "freebsd"):
            with self.subTest(platform=platform), mock.patch.object(safety.sys, "platform", platform):
                with self.assertRaisesRegex(safety.SafetyError, "only on macOS"):
                    safety.require_offline()
        self.process_run.assert_not_called()

    def test_ps_nonzero_exit_refuses_writes(self):
        self.process_run.return_value.returncode = 1
        with self.assertRaisesRegex(safety.SafetyError, "Process inspection denied"):
            safety.require_offline()
        self.process_run.assert_called_once()

    def test_ps_empty_output_refuses_writes(self):
        self.process_run.return_value.stdout = " \n\t"
        with self.assertRaisesRegex(safety.SafetyError, "Process inspection denied"):
            safety.require_offline()
        self.process_run.assert_called_once()

    def test_ps_os_error_refuses_writes_without_retry(self):
        self.process_run.side_effect = OSError("synthetic permission denial")
        with self.assertRaisesRegex(safety.SafetyError, "Cannot inspect processes"):
            safety.require_offline()
        self.process_run.assert_called_once()

    def test_ps_timeout_refuses_writes_without_retry(self):
        self.process_run.side_effect = subprocess.TimeoutExpired("/bin/ps", 10)
        with self.assertRaisesRegex(safety.SafetyError, "Cannot inspect processes"):
            safety.require_offline()
        self.process_run.assert_called_once()

    def test_ps_malformed_output_refuses_writes(self):
        for output in ("not-a-pid /usr/bin/synthetic", "200", "200 /usr/bin/synthetic\nmalformed"):
            with self.subTest(output=output):
                self.process_run.return_value.stdout = output
                with self.assertRaisesRegex(safety.SafetyError, "Unexpected process output"):
                    safety.require_offline()

    def test_legacy_daemon_variants_refuse_writes(self):
        commands = (
            "/usr/bin/python3 /synthetic/wb-account-sync.py live --interval 10",
            "/bin/sh /synthetic/wb-account-sync.sh live",
            "/synthetic/wb-account-sync live --home /synthetic/client",
        )
        for command in commands:
            with self.subTest(command=command):
                self.process_run.return_value.stdout = "200 " + command + "\n"
                with self.assertRaisesRegex(safety.SafetyError, "legacy sync daemon is running"):
                    safety.require_offline()

    def test_workbuddy_application_variants_refuse_writes(self):
        commands = (
            "/synthetic/WorkBuddy.app/Contents/MacOS/WorkBuddy",
            "/synthetic/WorkBuddy AI.app/Contents/MacOS/WorkBuddy AI",
            "/synthetic/WORKBUDDY AI.app/Contents/Frameworks/Helper --type=renderer",
        )
        for command in commands:
            with self.subTest(command=command):
                self.process_run.return_value.stdout = "200 " + command + "\n"
                with self.assertRaisesRegex(safety.SafetyError, "WorkBuddy or the legacy"):
                    safety.require_offline()

    def test_clean_mock_process_list_allows_offline_guard(self):
        self.assertIsNone(safety.require_offline())
        self.process_run.assert_called_once_with(
            ["/bin/ps", "-Ao", "pid=,command="], capture_output=True,
            text=True, timeout=10, check=False,
        )

    def test_current_process_is_ignored_but_other_processes_are_checked(self):
        own_command = "100 /synthetic/wb-account-sync.py live\n"
        self.process_run.return_value.stdout = own_command + "200 /usr/bin/synthetic-idle\n"
        self.assertIsNone(safety.require_offline())
        self.process_run.return_value.stdout = own_command + "201 /synthetic/wb-account-sync.py live\n"
        with self.assertRaisesRegex(safety.SafetyError, "legacy sync daemon is running"):
            safety.require_offline()


class ScopeRegressionTests(TemporaryClientTestCase):
    def test_forged_background_plan_rejected_even_with_valid_checksum(self):
        plan = self.plan()
        row = self.rows()["session-background"]
        plan["changes"] = [{"id": row["id"], "before": core.row_hash(row),
                            "after": core.row_hash(row, TARGET)}]
        plan["plan_id"] = safety.digest({k: v for k, v in plan.items() if k != "plan_id"})
        with self.assertRaisesRegex(safety.SafetyError, "scope"):
            self.assert_no_side_effects(lambda: core.verify(plan))

    def test_forged_inverse_fingerprint_rejected(self):
        plan = self.plan()
        plan["changes"][0]["after"] = "0" * 64
        plan["plan_id"] = safety.digest({k: v for k, v in plan.items() if k != "plan_id"})
        with self.assertRaisesRegex(safety.SafetyError, "fingerprints"):
            self.assert_no_side_effects(lambda: core.verify(plan))

    def test_alternate_unique_key_refused(self):
        self.execute("CREATE UNIQUE INDEX unique_title ON sessions(title)")
        with self.assertRaisesRegex(safety.SafetyError, "Alternate unique"):
            self.assert_no_side_effects(lambda: core.doctor(self.home))


if __name__ == "__main__":
    unittest.main()
