"""Synthetic-only tests for read-only sharing/settings previews and path guards."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from wb_account_sync import cli, core, preview, rehearsal, safety
from tests.test_core import TemporaryClientTestCase, SOURCE, TARGET

THIRD = "00000000-0000-0000-0000-000000000003"


class PathBoundaryTests(TemporaryClientTestCase):
    def test_reject_parent_traversal_before_any_write(self):
        attempted = self.root / "sibling" / ".." / self.home.name / "state"
        self.assert_refused_without_side_effects(
            lambda: safety.state_root(attempted, self.home), "Parent traversal")

    def test_equivalent_double_slash_root_cannot_bypass_containment(self):
        attempted = "//" + str(self.home / "state").lstrip("/")
        self.assert_refused_without_side_effects(
            lambda: safety.state_root(attempted, self.home), "outside the client home")

    def test_equivalent_double_slash_export_is_refused(self):
        output = "//" + str(self.home / "plan.json").lstrip("/")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = self.assert_no_side_effects(lambda: cli.main([
                "plan", "--home", str(self.home), "--source", SOURCE,
                "--target", TARGET, "--output", output]))
        self.assertEqual(2, code)

    def test_reject_client_home_with_parent_traversal(self):
        self.assert_refused_without_side_effects(
            lambda: core.doctor(self.home / ".." / self.home.name), "Parent traversal")

    def test_reject_json_through_symlinked_parent(self):
        alias = self.root / "alias"
        alias.symlink_to(self.snapshot.parent, target_is_directory=True)
        with self.assertRaisesRegex(safety.SafetyError, "Symlink"):
            safety.read_json(alias / self.snapshot.name)

    def test_reject_export_traversal(self):
        output = self.root / "sibling" / ".." / self.home.name / "plan.json"
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = self.assert_no_side_effects(lambda: cli.main([
                "plan", "--home", str(self.home), "--source", SOURCE,
                "--target", TARGET, "--output", str(output)]))
        self.assertEqual(2, code)

    def test_atomic_json_rejects_traversal(self):
        self.assert_refused_without_side_effects(
            lambda: safety.atomic_json(self.home / ".." / "new.json", {}), "Parent traversal")


class HistoryPreviewTests(TemporaryClientTestCase):
    def report(self, accounts=None, target=TARGET):
        return preview.history_preview(self.home, accounts or [SOURCE, TARGET], target)

    def test_union_keeps_original_owners_and_has_no_side_effects(self):
        result = self.assert_no_side_effects(self.report)
        self.assertEqual(3, result["ordinary_union_count"])
        owners = {s["id"]: s["original_owner"] for s in result["sessions"]}
        self.assertEqual({"session-a": SOURCE, "session-b": SOURCE, "session-target": TARGET}, owners)
        self.assertTrue(result["source_ownership_preserved"])
        self.assertFalse(result["can_apply"])
        self.assertFalse(result["client_sync_implemented"])
        self.assertNotIn("First ordinary session", json.dumps(result))

    def test_only_explicit_participants_are_included(self):
        self.execute("INSERT INTO sessions VALUES ('third', ?, NULL, 0, 'Must not include')", (THIRD,))
        result = self.report()
        self.assertEqual(3, result["ordinary_union_count"])
        self.assertNotIn(THIRD, json.dumps(result))

    def test_participant_order_does_not_change_preview(self):
        self.assertEqual(self.report(), self.report([TARGET, SOURCE]))

    def test_reject_duplicates(self):
        self.assert_refused_without_side_effects(lambda: self.report([SOURCE, SOURCE, TARGET]), "unique")

    def test_reject_unselected_target(self):
        self.assert_refused_without_side_effects(lambda: self.report([SOURCE, THIRD]), "include the target")

    def test_reject_wrong_current_account(self):
        self.assert_refused_without_side_effects(lambda: self.report(target=SOURCE), "current account")

    def test_reject_invalid_uid(self):
        self.assert_refused_without_side_effects(lambda: self.report(["short", TARGET]), "UUID")

    def test_single_participant_is_not_cross_account(self):
        self.assert_refused_without_side_effects(lambda: self.report([TARGET]), "between 2 and 100")

    def test_unknown_participant_is_reported_not_treated_as_synced(self):
        result = self.report([SOURCE, TARGET, THIRD])
        self.assertFalse(result["accounts"][-1]["has_local_rows"])
        self.assertTrue(any("no local rows" in w for w in result["warnings"]))

    def test_empty_union_is_not_reported_as_sync_success(self):
        self.execute("DELETE FROM sessions")
        result = self.report()
        self.assertEqual(0, result["ordinary_union_count"])
        self.assertTrue(any("not successful synchronization" in w for w in result["warnings"]))
        self.assertFalse(result["can_apply"])

    def test_preview_rejected_by_migration_executor(self):
        with self.assertRaisesRegex(safety.SafetyError, "Invalid plan structure"):
            core.validate_plan(self.report())

    def test_account_bound_metadata_requires_review_without_exporting_values(self):
        self.execute("ALTER TABLE sessions ADD COLUMN plugin_context_json TEXT")
        self.execute("UPDATE sessions SET plugin_context_json=? WHERE id='session-a'", ('{"secret":"SENTINEL"}',))
        result = self.report()
        self.assertEqual(1, result["account_metadata_review_count"])
        self.assertNotIn("SENTINEL", json.dumps(result))

    def test_snapshot_drift_refused(self):
        _, before = safety.snapshot_identity(self.home)
        with mock.patch.object(preview, "snapshot_identity", side_effect=[(TARGET, before), (SOURCE, "changed")]):
            self.assert_refused_without_side_effects(self.report, "identity changed")

    def test_wal_refused(self):
        Path(str(self.db) + "-wal").write_bytes(b"synthetic-active")
        self.assert_refused_without_side_effects(self.report, "WAL")

    def test_cli_history_preview_does_not_write(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = self.assert_no_side_effects(lambda: cli.main([
                "share-preview", "--home", str(self.home), "--accounts", SOURCE, TARGET,
                "--target", TARGET]))
        self.assertEqual(0, status)
        self.assertTrue(json.loads(output.getvalue())["read_only"])

    def test_doctor_explains_sync_is_not_implemented(self):
        result = core.doctor(self.home)
        self.assertFalse(result["automatic_account_sync"])
        self.assertFalse(result["user_settings_sync"])

    def test_empty_migration_verification_is_explicit_noop(self):
        result = core.verify(core.make_plan(self.home, THIRD, TARGET))
        self.assertTrue(result["no_op"])
        self.assertIn("no history", result["note"])


class PreferencePreviewTests(unittest.TestCase):
    def test_known_missing_keys_and_target_conflicts(self):
        source = {"language": "简体中文", "model": "source-model"}
        target = {"model": "target-model"}
        before = deepcopy((source, target))
        result = preview.preference_diff(source, target)
        self.assertEqual(["language"], [r["key"] for r in result["changes"]])
        self.assertEqual("keep_target", result["conflicts"][0]["action"])
        self.assertEqual(before, (source, target))
        self.assertFalse(result["can_apply"])

    def test_default_deny_sensitive_unknown_and_executable_fields(self):
        source = {key: {"value": "DO-NOT-EXPOSE"} for key in (
            "env", "hooks", "permissions", "mcpServers", "payment", "claw", "tokens", "trustAll", "unknown")}
        result = preview.preference_diff(source, source)
        self.assertEqual([], result["changes"])
        self.assertEqual(9, result["excluded_source_key_count"])
        self.assertNotIn("DO-NOT-EXPOSE", json.dumps(result))

    def test_allowed_key_values_are_also_redacted(self):
        result = preview.preference_diff({"model": "SENSITIVE-SENTINEL"}, {"model": "TARGET-SENTINEL"})
        self.assertNotIn("SENTINEL", json.dumps(result))

    def test_guessable_value_hashes_are_not_exported(self):
        result = preview.preference_diff({"showTokensCounter": True}, {"showTokensCounter": False})
        self.assertEqual([{"key": "showTokensCounter", "action": "keep_target"}], result["conflicts"])
        self.assertNotIn(safety.digest(True), json.dumps(result))
        self.assertNotIn(safety.digest(False), json.dumps(result))

    def test_invalid_types_are_not_proposed(self):
        result = preview.preference_diff({"model": {"token": "bad"}, "showTokensCounter": 1,
                                          "reasoningEffort": "unrecognized"}, {})
        self.assertEqual([], result["changes"])
        self.assertEqual(3, len(result["invalid_allowlisted_keys"]))

    def test_existing_identical_preferences_are_noop(self):
        source = {"model": "demo", "language": "English", "showTokensCounter": False}
        result = preview.preference_diff(source, source)
        self.assertEqual(3, result["unchanged"])
        self.assertEqual([], result["changes"])
        self.assertEqual([], result["conflicts"])

    def test_non_objects_refused(self):
        for bad in ([], None, "raw", 1):
            with self.subTest(bad=bad), self.assertRaises(safety.SafetyError):
                preview.preference_diff(bad, {})


class PreferenceFileTests(TemporaryClientTestCase):
    def test_explicit_files_only_no_side_effects(self):
        source = self.root / "source-settings.json"
        target = self.root / "target-settings.json"
        source.write_text('{"model":"source", "env":{"SECRET":"fake"}}', encoding="utf-8")
        target.write_text('{"language":"English"}', encoding="utf-8")
        report = self.assert_no_side_effects(lambda: preview.preferences_preview(source, target))
        self.assertEqual(1, len(report["changes"]))
        self.assertNotIn("fake", json.dumps(report))

    def test_cli_preferences_preview_is_read_only(self):
        source = self.root / "source-settings.json"
        target = self.root / "target-settings.json"
        source.write_text('{"model":"source", "env":{"SECRET":"synthetic"}}', encoding="utf-8")
        target.write_text('{"model":"target"}', encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            status = self.assert_no_side_effects(lambda: cli.main([
                "preferences-preview", "--source-file", str(source),
                "--target-file", str(target), "--json"]))
        self.assertEqual(0, status)
        report = json.loads(output.getvalue())
        self.assertEqual([{"key": "model", "action": "keep_target"}], report["conflicts"])
        self.assertNotIn("synthetic", output.getvalue())

    def test_broken_json_is_not_replaced(self):
        source = self.root / "source-settings.json"
        source.write_text("{broken", encoding="utf-8")
        self.assert_refused_without_side_effects(
            lambda: preview.preferences_preview(source, source), "Invalid JSON")


class RehearsalTests(unittest.TestCase):
    def test_fixed_fixture_never_uses_user_home_or_migration_writer(self):
        with mock.patch.object(Path, "home", side_effect=AssertionError("No user home access")), \
             mock.patch.object(core, "apply", side_effect=AssertionError("No apply")), \
             mock.patch.object(core, "require_offline", side_effect=AssertionError("No process guard bypass")):
            result = rehearsal.run()
        self.assertTrue(result["passed"])
        self.assertEqual(10, len(result["checks"]))
        self.assertFalse(result["real_account_data_accessed"])
        self.assertFalse(result["client_sync_implemented"])

    def test_demo_cli_and_no_real_home_option(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(0, cli.main(["demo", "--json"]))
        self.assertTrue(json.loads(stream.getvalue())["passed"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            cli.main(["demo", "--home", "/must-not-be-opened"])
        self.assertEqual(2, exc.exception.code)


if __name__ == "__main__":
    unittest.main()
