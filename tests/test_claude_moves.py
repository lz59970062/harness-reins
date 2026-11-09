from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.commands import goals as goal_commands
from harness_reins.engine import claude_moves
from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import dispatch
from harness_reins.engine import goals
from harness_reins.engine import reminders
from harness_reins.harness.claude import session as claude_session
from harness_reins.store import layout
from harness_reins.store import records as queue_store
from harness_reins.tower import main as tower

OLD = "11111111-aaaa-4aaa-8aaa-111111111111"
NEW = "22222222-bbbb-4bbb-8bbb-222222222222"
NEWER = "33333333-cccc-4ccc-8ccc-333333333333"


def stamp(at: float) -> str:
    return datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class MoveCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "reins" / "queue"
        self.claude = self.directory / "claude"
        patcher = mock.patch.dict(os.environ, {
            "REINS_HOME": str(self.directory / "reins"), "CLAUDE_CONFIG_DIR": str(self.claude),
            layout.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"), "CLAUDE_CODE_ENTRYPOINT": "cli",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        queue_store.ensure_daemon_dirs(self.root)
        (self.claude / "sessions").mkdir(parents=True)
        (self.claude / "projects" / "p").mkdir(parents=True)

    def window(self, session: str) -> None:
        identity = tower.daemon_process_identity(os.getpid()) or {}
        (self.claude / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": session, "procStart": str(identity.get("start_ticks", "")),
            "entrypoint": "cli"}), encoding="utf-8")

    def close_window(self) -> None:
        (self.claude / "sessions" / f"{os.getpid()}.json").unlink()

    def transcript(self, session: str, born_at: float, text: str = "hello", first_session: str | None = None) -> None:
        record = {"type": "user", "sessionId": first_session or session, "timestamp": stamp(born_at),
                  "message": {"role": "user", "content": text}}
        (self.claude / "projects" / "p" / f"{session}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    def seen(self, session: str, ago: float = 100.0) -> None:
        """The daemon saw the window on this id, ``ago`` seconds back."""
        self.window(session)
        claude_moves.sync(self.root)
        path = self.root / claude_moves.STATE_FILE
        state = json.loads(path.read_text(encoding="utf-8"))
        for value in state["windows"].values():
            value["seen_at"] = time.time() - ago
        path.write_text(json.dumps(state), encoding="utf-8")

    def moved(self, old: str, new: str, text: str = "hello") -> None:
        self.seen(old)
        self.transcript(new, time.time() - 5, text)
        self.window(new)
        claude_moves.sync(self.root)

    def enqueue(self, ident: str, session: str = OLD, **fields: object) -> Path:
        args = argparse.Namespace(
            agent="claude", session=session, last=False, task="train model", cwd=str(self.directory),
            command="python train.py", exit_code=0, message=None, queue_dir=str(self.root), _callback_id=ident,
            callback_mode="cli", strict=False, goal_id=None, dry_run=False, via_daemon=True, approvals_reviewer="user",
            approval_policy="on-request", sandbox_mode="workspace-write")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.queue_callback(args, "the result"), 0)
        path = self.root / "pending" / f"{ident}.json"
        if fields:
            request = json.loads(path.read_text(encoding="utf-8"))
            request.update(fields)
            path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def moves(self) -> dict[str, object]:
        return json.loads((self.root / claude_moves.STATE_FILE).read_text(encoding="utf-8"))["moves"]


class DetectionTests(MoveCase):
    def test_a_clear_is_a_move(self) -> None:
        self.moved(OLD, NEW)
        self.assertEqual(self.moves()[OLD]["to"], NEW)
        self.assertEqual(claude_moves.current_session(self.root, OLD), NEW)

    def test_a_new_id_with_no_transcript_yet_is_a_move(self) -> None:
        self.seen(OLD)
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertEqual(self.moves()[OLD]["to"], NEW)

    def test_resuming_an_older_conversation_is_not_a_move(self) -> None:
        self.seen(OLD)
        self.transcript(NEW, time.time() - 86400)
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertEqual(self.moves(), {})
        self.assertEqual(claude_moves.current_session(self.root, OLD), OLD)

    def test_a_fork_in_the_same_window_starts_with_the_old_ids_records(self) -> None:
        self.seen(OLD)
        self.transcript(NEW, time.time() - 86400, first_session=OLD)
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertEqual(self.moves()[OLD]["to"], NEW)

    def test_a_transcript_that_cannot_tell_yet_is_looked_at_again(self) -> None:
        self.seen(OLD)
        path = self.claude / "projects" / "p" / f"{NEW}.jsonl"
        path.write_text('{"type":"custom-title"}\n', encoding="utf-8")
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertEqual(self.moves(), {})
        self.transcript(NEW, time.time())
        claude_moves.sync(self.root)
        self.assertEqual(self.moves()[OLD]["to"], NEW)

    def test_a_transcript_that_never_can_tell_is_taken_as_no_move(self) -> None:
        self.seen(OLD, ago=5)
        (self.claude / "projects" / "p" / f"{NEW}.jsonl").write_text('{"type":"custom-title"}\n', encoding="utf-8")
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertTrue(claude_moves._settling(self.root, OLD))
        path = self.root / claude_moves.STATE_FILE
        state = json.loads(path.read_text(encoding="utf-8"))
        for window in state["windows"].values():
            window["undecided_at"] = time.time() - claude_moves.UNDECIDED_SECONDS - 1
        path.write_text(json.dumps(state), encoding="utf-8")
        claude_moves.sync(self.root)
        self.assertEqual(self.moves(), {})
        self.assertFalse(claude_moves._settling(self.root, OLD))

    def test_a_window_that_closed_while_undecided_holds_nothing_for_long(self) -> None:
        self.seen(OLD, ago=5)
        (self.claude / "projects" / "p" / f"{NEW}.jsonl").write_text('{"type":"custom-title"}\n', encoding="utf-8")
        self.window(NEW)
        claude_moves.sync(self.root)
        self.close_window()
        self.assertTrue(claude_moves._settling(self.root, OLD))
        with mock.patch.object(claude_moves.time, "time", return_value=time.time() + claude_moves.UNDECIDED_SECONDS + 1):
            self.assertFalse(claude_moves._settling(self.root, OLD))

    def test_a_stale_sight_proves_nothing_about_a_young_conversation(self) -> None:
        self.seen(OLD, ago=2 * 3600)
        self.transcript(NEW, time.time() - 1800)  # Younger than the sight, but resumed while the daemon was down.
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertEqual(self.moves(), {})
        self.assertEqual(claude_moves.current_session(self.root, OLD), OLD)

    def test_a_stale_sight_still_believes_a_transcript_that_starts_with_the_old_id(self) -> None:
        self.seen(OLD, ago=2 * 3600)
        self.transcript(NEW, time.time() - 1800, first_session=OLD)
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertEqual(self.moves()[OLD]["to"], NEW)

    def test_a_move_lasts_as_long_as_the_window_it_led_to(self) -> None:
        self.moved(OLD, NEW)
        path = self.root / claude_moves.STATE_FILE
        state = json.loads(path.read_text(encoding="utf-8"))
        state["moves"][OLD]["at"] = time.time() - claude_moves.KEEP_MOVE_SECONDS - 1
        path.write_text(json.dumps(state), encoding="utf-8")
        claude_moves.sync(self.root)
        self.assertEqual(self.moves()[OLD]["to"], NEW)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["moves"][OLD]["at"] = time.time() - claude_moves.KEEP_MOVE_SECONDS - 1
        path.write_text(json.dumps(state), encoding="utf-8")
        self.close_window()
        claude_moves.sync(self.root)
        self.assertEqual(self.moves(), {})

    def test_a_first_sight_of_a_window_is_no_move(self) -> None:
        self.transcript(NEW, time.time())
        self.window(NEW)
        claude_moves.sync(self.root)
        self.assertEqual(self.moves(), {})

    def test_a_headless_run_is_not_a_window_moving(self) -> None:
        self.moved(OLD, NEW)
        record = self.claude / "sessions" / f"{os.getpid()}.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        data["entrypoint"] = claude_session.HEADLESS_ENTRYPOINT
        data["sessionId"] = NEWER
        record.write_text(json.dumps(data), encoding="utf-8")
        claude_moves.sync(self.root)
        self.assertEqual(list(self.moves()), [OLD])

    def test_a_damaged_state_file_starts_over(self) -> None:
        (self.root / claude_moves.STATE_FILE).write_text("{not json", encoding="utf-8")
        self.window(OLD)
        claude_moves.sync(self.root)
        self.assertEqual(claude_moves.current_session(self.root, OLD), OLD)


class CurrentSessionTests(MoveCase):
    def test_a_window_back_on_the_old_id_keeps_it(self) -> None:
        self.moved(OLD, NEW)
        self.window(OLD)
        self.assertEqual(claude_moves.current_session(self.root, OLD), OLD)

    def test_several_moves_end_at_the_open_window(self) -> None:
        self.moved(OLD, NEW)
        self.moved(NEW, NEWER)
        self.assertEqual(claude_moves.current_session(self.root, OLD), NEWER)

    def test_a_loop_of_moves_ends(self) -> None:
        self.moved(OLD, NEW)
        self.moved(NEW, OLD)
        self.close_window()
        self.assertIn(claude_moves.current_session(self.root, OLD), (OLD, NEW))

    def test_with_the_window_closed_a_cleared_session_keeps_the_context_it_has(self) -> None:
        self.moved(OLD, NEW)
        self.close_window()
        self.assertEqual(claude_moves.current_session(self.root, OLD), OLD)

    def test_with_the_window_closed_a_continuation_takes_over(self) -> None:
        self.moved(OLD, NEW, claude_moves.CONTINUATION_TEXT + " that ran out of context.")
        self.close_window()
        self.assertEqual(claude_moves.current_session(self.root, OLD), NEW)


class RoutingTests(MoveCase):
    def test_a_queued_callback_goes_to_the_window(self) -> None:
        path = self.enqueue("cb-1", live_delivered_at=time.time() - 60, live_window_pid=1, next_attempt_at=time.time() + 999)
        self.moved(OLD, NEW)
        claude_moves.follow(self.root, force=True)
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request["target"]["session"], NEW)
        self.assertEqual(request["moved_from"], OLD)
        for mark in ("live_delivered_at", "live_window_pid", "next_attempt_at"):
            self.assertNotIn(mark, request)

    def test_the_window_is_told_where_it_came_from(self) -> None:
        self.enqueue("cb-1")
        self.moved(OLD, NEW)
        claude_moves.follow(self.root, force=True)
        with mock.patch.object(claude_session, "_held", return_value=False):
            prompts = claude_session._claim_pending(self.root, "claude", NEW, None)
        self.assertEqual(len(prompts), 1)
        self.assertIn(f"bound to session {OLD}", prompts[0])

    def test_nothing_is_resumed_beside_the_window(self) -> None:
        self.enqueue("cb-1")
        self.moved(OLD, NEW)
        self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
        claude_moves.follow(self.root, force=True)
        self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))

    def test_the_last_check_before_a_resume_sees_a_move_the_daemon_has_not(self) -> None:
        path = self.enqueue("cb-1")
        self.seen(OLD)
        self.transcript(NEW, time.time() - 5)
        self.window(NEW)
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(delivery_engine.waits_for_open_window(self.root, request, "cb-1", look_again=True))

    def test_the_last_check_waits_for_a_look_in_progress(self) -> None:
        path = self.enqueue("cb-1")
        request = json.loads(path.read_text(encoding="utf-8"))
        with mock.patch.object(queue_store, "acquire_owner_lock", wraps=queue_store.acquire_owner_lock) as acquire:
            delivery_engine.waits_for_open_window(self.root, request, "cb-1", look_again=True)
        self.assertIn(mock.call(self.root, claude_moves.STATE_LOCK_ID, blocking=True), acquire.mock_calls)

    def test_nothing_is_resumed_while_a_move_cannot_be_told_yet(self) -> None:
        self.enqueue("cb-1")
        self.seen(OLD, ago=5)
        (self.claude / "projects" / "p" / f"{NEW}.jsonl").write_text('{"type":"custom-title"}\n', encoding="utf-8")
        self.window(NEW)
        claude_moves.follow(self.root, force=True)
        self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))

    def test_a_session_left_for_another_conversation_still_resumes(self) -> None:
        self.enqueue("cb-1")
        self.seen(OLD)
        self.transcript(NEW, time.time() - 86400)
        self.window(NEW)
        claude_moves.follow(self.root, force=True)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()).stem, "cb-1")

    def test_a_cleared_session_with_its_window_closed_still_resumes_where_the_context_is(self) -> None:
        path = self.enqueue("cb-1")
        self.moved(OLD, NEW)
        self.close_window()
        claude_moves.follow(self.root, force=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["target"]["session"], OLD)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()).stem, "cb-1")

    def test_a_callback_sent_to_the_window_goes_back_when_a_cleared_window_closes(self) -> None:
        path = self.enqueue("cb-1")
        self.moved(OLD, NEW)
        claude_moves.follow(self.root, force=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["target"]["session"], NEW)
        self.close_window()
        # Until the daemon has looked, it may not be resumed under the id with no context.
        self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
        claude_moves.follow(self.root, force=True)
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request["target"]["session"], OLD)
        self.assertNotIn("moved_from", request)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()).stem, "cb-1")

    def test_a_callback_stays_with_a_continuation_when_the_window_closes(self) -> None:
        path = self.enqueue("cb-1")
        self.moved(OLD, NEW, claude_moves.CONTINUATION_TEXT + " that ran out of context.")
        claude_moves.follow(self.root, force=True)
        self.close_window()
        claude_moves.follow(self.root, force=True)
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request["target"]["session"], NEW)
        self.assertEqual(request["moved_from"], OLD)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()).stem, "cb-1")

    def test_a_headless_resume_is_told_where_it_came_from_too(self) -> None:
        path = self.enqueue("cb-1")
        self.moved(OLD, NEW, claude_moves.CONTINUATION_TEXT + " that ran out of context.")
        claude_moves.follow(self.root, force=True)
        request = json.loads(path.read_text(encoding="utf-8"))
        prompt = reminders.select_delivery_prompt({"prompt": request["prompt"], "request": request})
        self.assertIn(f"bound to session {OLD}", prompt)

    def test_a_window_back_on_the_old_id_gets_it_back(self) -> None:
        path = self.enqueue("cb-1")
        self.moved(OLD, NEW)
        claude_moves.follow(self.root, force=True)
        self.window(OLD)
        claude_moves.follow(self.root, force=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["target"]["session"], OLD)

    def test_one_malformed_record_does_not_stop_the_rest(self) -> None:
        bad = self.enqueue("cb-bad", agent="bogus-agent")
        good = self.enqueue("cb-good")
        self.moved(OLD, NEW)
        claude_moves.follow(self.root, force=True)
        self.assertEqual(json.loads(good.read_text(encoding="utf-8"))["target"]["session"], NEW)
        self.assertEqual(json.loads(bad.read_text(encoding="utf-8"))["target"]["session"], OLD)

    def test_a_record_the_daemon_will_not_move_is_held_only_beside_a_window(self) -> None:
        path = self.enqueue("cb-1", retain_target_lease=True)
        self.moved(OLD, NEW, claude_moves.CONTINUATION_TEXT + " that ran out of context.")
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(claude_moves.holds(self.root, request))
        self.close_window()
        self.assertFalse(claude_moves.holds(self.root, request))

    def test_another_sessions_callback_stays(self) -> None:
        path = self.enqueue("cb-1", session=NEWER)
        self.moved(OLD, NEW)
        claude_moves.follow(self.root, force=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["target"]["session"], NEWER)

    def test_a_callback_being_delivered_is_left_alone(self) -> None:
        path = self.enqueue("cb-1")
        self.moved(OLD, NEW)
        lock = queue_store.acquire_owner_lock(self.root, layout.delivery_lock_id("cb-1"), blocking=False)
        self.addCleanup(queue_store.release_owner_lock, lock, remove=False)
        claude_moves._retarget(self.root, path, os.environ)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["target"]["session"], OLD)

    def test_a_new_submission_binds_to_the_id_the_window_has_now(self) -> None:
        self.moved(OLD, NEW)
        args = argparse.Namespace(session=None, agent="claude", queue_dir=str(self.root))
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OLD}), contextlib.redirect_stderr(errors):
            target, source = dispatch.resolve_target(args)
        self.assertEqual(target["session"], NEW)
        self.assertEqual(source, "CLAUDE_CODE_SESSION_ID")
        self.assertIn("moved to", errors.getvalue())

    def test_a_goal_follows_so_its_reminders_reach_the_window(self) -> None:
        plan = self.directory / "plan.yaml"
        plan.write_text("version: 1\nrevision: 1\ngoal: Ship it\npath:\n  - id: build\n    title: Build\n    status: pending\n"
                        "amendments:\n  - revision: 1\n    reason: Initial path\n", encoding="utf-8")
        args = argparse.Namespace(queue_dir=str(self.root), id="g1", session=OLD, agent="claude", last=False,
                                  cwd=str(self.directory), task="ship it", plan_file=str(plan), idle_seconds=60.0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(goal_commands.goal_start(args), 0)
        self.moved(OLD, NEW)
        claude_moves.follow(self.root, force=True)
        goal = goals.load_goal(self.root, "g1")
        self.assertEqual(goal["target"]["session"], NEW)
        self.assertEqual(goal["moved_from"], OLD)
        goal["last_external_callback_at"] = time.time() - 120
        goals.write_goal(self.root, goal)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(goals.process_goal_reminders(self.root))
        (reminder,) = (self.root / "pending").glob("*.json")
        self.assertEqual(dispatch.load_request(reminder)["target"]["session"], NEW)

    def test_the_daemon_does_not_look_on_every_pass(self) -> None:
        with mock.patch.object(claude_moves, "_next_track", time.monotonic() + 60), \
                mock.patch.object(claude_moves, "sync") as sync:
            claude_moves.follow(self.root)
        sync.assert_not_called()

    def test_a_failure_to_look_does_not_stop_the_daemon(self) -> None:
        with mock.patch.object(claude_moves, "sync", side_effect=OSError("gone")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            claude_moves.follow(self.root, force=True)
        self.assertIn("could not follow", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
