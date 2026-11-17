"""Independent cadence contracts; tests are executed by the parent."""
import argparse
import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

from harness_reins import cli
from harness_reins.engine import dispatch
from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import reminders as reminder_policy
from harness_reins.store import layout
from test_cli import patch_inprocess_delivery_worker


def _allocate_in_process(root, policy, callback_id, results):
    request = {"id": callback_id, "agent": "codex",
               "target": {"kind": "session", "session": "concurrent-session"}}
    try:
        results.put(reminder_policy.allocate_prompt_cadence(Path(root), request, Path(policy)))
    except Exception as exc:
        results.put({"error": repr(exc)})


class PromptCadenceTests(unittest.TestCase):
    def setUp(self):
        patch_inprocess_delivery_worker(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "queue"
        self.policy = Path(temp.name) / "callback-prompts.yaml"

    def request(self, ident, session="parent", agent="codex"):
        return {"id": ident, "agent": agent, "target_source": "--session",
                "target": {"kind": "session", "session": session}}

    def allocate(self, request):
        return reminder_policy.allocate_prompt_cadence(self.root, request, policy_path=self.policy)

    def test_default_schedule_counts_callbacks_not_retries(self):
        expected_system = {1, 5, 9}
        expected_user = {1, 4, 7}
        for sequence in range(1, 10):
            request = self.request(f"callback-{sequence}")
            result = self.allocate(request)
            self.assertEqual(result["sequence"], sequence)
            self.assertEqual(result["system_every"], 4)
            self.assertEqual(result["user_every"], 3)
            self.assertEqual(result["system_due"], sequence in expected_system)
            self.assertEqual(result["user_due"], sequence in expected_user)
            # Fresh dict models a request reloaded from disk after daemon restart.
            self.assertEqual(self.allocate(json.loads(json.dumps(request))), result)

    def test_sessions_agents_and_queues_have_independent_sequences(self):
        self.assertEqual(self.allocate(self.request("a"))["sequence"], 1)
        self.assertEqual(self.allocate(self.request("b"))["sequence"], 2)
        for request in (self.request("c", session="other"), self.request("d", agent="claude")):
            self.assertEqual(self.allocate(request)["sequence"], 1)
        self.root = self.root.parent / "another-queue"
        self.assertEqual(self.allocate(self.request("e"))["sequence"], 1)

    def test_target_without_a_bound_session_preserves_full_without_counting(self):
        self.assertIsNone(self.allocate({"id": "unbound", "agent": "codex", "target": {"kind": "future-unknown"}}))
        self.assertEqual(self.allocate(self.request("bound"))["sequence"], 1)

    def test_corrupt_counter_falls_back_instead_of_restarting_sequence(self):
        self.allocate(self.request("first"))
        counters = list((self.root / "prompt-counters").glob("*.json"))
        self.assertEqual(len(counters), 1)
        counters[0].write_text("{broken json", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(self.allocate(self.request("second")))
        self.assertEqual(counters[0].read_text(), "{broken json")

    def test_concurrent_processes_allocate_unique_sequences_and_stable_retry(self):
        context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        results = context.Queue()
        processes = [context.Process(target=_allocate_in_process,
                                     args=(str(self.root), str(self.policy), f"parallel-{i}", results))
                     for i in range(4)]
        try:
            for process in processes:
                process.start()
            received = [results.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sorted(item["sequence"] for item in received), [1, 2, 3, 4])
            for i in range(4):
                retry = self.allocate(self.request(f"parallel-{i}", session="concurrent-session"))
                self.assertIn(retry, received)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            results.close()

    def payload(self, ident, compact=True):
        request = self.request(ident)
        if compact:
            request["prompt_compact"] = "COMPACT RESULT ACK TASK-SPECIFIC"
        hook = self.policy.parent / "callback-prompt.md"
        return {"prompt": "FULL RESULT ACK TASK-SPECIFIC", "request": request,
                "queue_dir": str(self.root), "callback_hook_path": str(hook)}

    def test_concurrent_same_callback_consumes_only_one_sequence(self):
        context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        results = context.Queue()
        processes = [context.Process(target=_allocate_in_process,
                                     args=(str(self.root), str(self.policy), "same-id", results))
                     for _ in range(4)]
        try:
            for process in processes:
                process.start()
            received = [results.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(item == received[0] for item in received))
            self.assertEqual(received[0]["sequence"], 1)
            following = self.allocate(self.request("next-id", session="concurrent-session"))
            self.assertEqual(following["sequence"], 2)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            results.close()

    def test_delivery_worker_sends_selected_compact_text_to_both_transports(self):
        self.allocate(self.request("first"))
        for transport in ("desktop", "cli"):
            with self.subTest(transport=transport):
                payload = self.payload("non-due-worker")
                payload.update(command=[sys.executable], timeout=1, cwd=str(self.root.parent),
                               ack_path=str(self.root.parent / "absent-ack"),
                               canceled_path=str(self.root.parent / "absent-cancel"))
                Path(payload["callback_hook_path"]).write_text("SUPPRESSED HOOK")
                desktop = mock.Mock()
                desktop.wait_for_completion.return_value = True
                child = mock.Mock(returncode=0)
                with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": "[]"}), \
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                        mock.patch.object(cli.signal, "signal"), \
                        mock.patch.object(delivery_engine, "start_desktop_app_server_turn",
                                          return_value=desktop if transport == "desktop" else None) as start, \
                        mock.patch.object(cli.subprocess, "Popen", return_value=child) as popen, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
                selected = start.call_args.args[0]["prompt"]
                self.assertIn("COMPACT RESULT ACK TASK-SPECIFIC", selected)
                self.assertNotIn("FULL", selected)
                self.assertNotIn("SUPPRESSED HOOK", selected)
                if transport == "desktop":
                    popen.assert_not_called()
                    desktop.close.assert_called_once()
                else:
                    child.communicate.assert_called_once_with(input=selected, timeout=1.0)

    def run_hosted(self, ident, mode, turn_id):
        """The worker for a Codex callback whose thread is loaded on the daemon (native_queue.hosted_socket)."""
        from harness_reins.engine import native_queue

        self.allocate(self.request(ident))
        payload = self.payload(ident)
        payload["request"].update(agent="codex", callback_mode=mode)
        payload.update(command=["unused-codex"], timeout=1, cwd=str(self.root.parent),
                       ack_path=str(self.root.parent / f"{ident}-ack"),
                       canceled_path=str(self.root.parent / f"{ident}-cancel"))
        turn = mock.Mock(turn_id=turn_id)
        with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": "[]"}), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(cli.signal, "signal"), \
                mock.patch.object(native_queue, "hosted_socket", return_value=Path("/s.sock")), \
                mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=turn), \
                mock.patch.object(delivery_engine, "release_retained_target_lease") as release, \
                mock.patch.object(delivery_engine, "wait_for_remote_delivery",
                                  return_value={"returncode": 0}) as wait, \
                mock.patch.object(native_queue, "publish") as publish, \
                mock.patch.object(cli.subprocess, "Popen") as child, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            delivery_engine.delivery_worker_main()
        child.assert_not_called()  # Never a headless resume beside the daemon.
        publish.assert_not_called()
        return json.loads(output.getvalue()), release, wait

    def test_a_queue_route_turn_on_the_daemon_is_published_once(self):
        # K3 PR 14: a message (or Desktop callback) started as a turn is handed over once, like `codex queue`:
        # 121 (awaiting its ACK, never re-sent), and its lease does not hold later deliveries back.
        from harness_reins.engine import native_queue

        result, release, wait = self.run_hosted("hosted-queue", "codex-queue", "turn-1")
        self.assertEqual(result["returncode"], native_queue.PUBLISHED_RETURN_CODE)
        release.assert_called()
        wait.assert_not_called()
        # A turn whose id is unknown is waited on for its ACK instead, never published again.
        result, release, wait = self.run_hosted("hosted-unknown", "codex-queue", None)
        wait.assert_called_once()
        # A cli callback waits for its ACK or the turn's end, as a resume does.
        result, release, wait = self.run_hosted("hosted-cli", "cli", "turn-1")
        wait.assert_called_once()

    def test_claude_resume_names_the_target_its_worker_holds(self):
        from harness_reins.harness.claude import session as claude_session

        for agent, environment in (("claude", None), ("claude", {"SESSION_ONLY": "1"}), ("codex", None)):
            with self.subTest(agent=agent, environment=environment):
                payload = self.payload(f"marker-{agent}-{bool(environment)}")
                payload["request"]["agent"] = agent
                payload.update(command=[sys.executable], timeout=1, cwd=str(self.root.parent),
                               ack_path=str(self.root.parent / "absent-ack"),
                               canceled_path=str(self.root.parent / "absent-cancel"), environment=environment)
                child = mock.Mock(returncode=0)
                child.communicate.return_value = ("", "")
                with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": "[]"}), \
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                        mock.patch.object(cli.signal, "signal"), \
                        mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=None), \
                        mock.patch.object(cli.subprocess, "Popen", return_value=child) as popen, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
                resume_env = popen.call_args.kwargs["env"]
                if agent == "claude":
                    self.assertEqual(resume_env[claude_session.RESUMED_TARGET_ENV], "session:parent")
                    if environment:
                        self.assertEqual(resume_env["SESSION_ONLY"], "1")
                else:
                    self.assertNotIn(claude_session.RESUMED_TARGET_ENV, resume_env or {})

    def test_worker_startup_markers_skip_allocation_and_transport(self):
        for marker, expected in (("ack", "already_acknowledged"), ("cancel", "canceled")):
            with self.subTest(marker=marker):
                payload = self.payload(f"skip-{marker}")
                ack = self.root.parent / f"{marker}-ack"
                cancel = self.root.parent / f"{marker}-cancel"
                (ack if marker == "ack" else cancel).touch()
                payload.update(command=["unused-codex"], timeout=1, cwd=str(self.root.parent),
                               ack_path=str(ack), canceled_path=str(cancel))
                with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": "[]"}), \
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                        mock.patch.object(cli.signal, "signal"), \
                        mock.patch.object(reminder_policy, "select_delivery_prompt") as select, \
                        mock.patch.object(delivery_engine, "start_desktop_app_server_turn") as desktop, \
                        mock.patch.object(cli.subprocess, "Popen") as child, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result["returncode"], 0)
                self.assertEqual(result["skipped"], expected)
                select.assert_not_called()
                desktop.assert_not_called()
                child.assert_not_called()
                self.assertFalse((self.root / "prompt-counters").exists())

    def test_selection_applies_independent_schedules_and_updates_transport_payload(self):
        hook = Path(self.payload("unused")["callback_hook_path"])
        hook.write_text("USER HOOK")
        for sequence in range(1, 10):
            payload = self.payload(f"select-{sequence}")
            selected = reminder_policy.select_delivery_prompt(payload)
            self.assertEqual(payload["prompt"], selected)
            self.assertEqual("FULL" in selected, sequence in (1, 5, 9))
            self.assertEqual("USER HOOK" in selected, sequence in (1, 4, 7))
            self.assertIn("RESULT ACK TASK-SPECIFIC", selected)
        hook.write_text("EDITED USER HOOK")
        retry = reminder_policy.select_delivery_prompt(self.payload("select-1"))
        self.assertIn("EDITED USER HOOK", retry)
        self.assertIn("FULL", retry)
        self.assertNotIn("EDITED USER HOOK", reminder_policy.select_delivery_prompt(self.payload("select-2")))

    def test_policy_changes_do_not_change_existing_allocations(self):
        first = self.allocate(self.request("first"))
        second = self.allocate(self.request("second"))
        self.policy.write_text("system_every: 1\nuser_every: 1\n")
        self.assertEqual(self.allocate(self.request("first")), first)
        self.assertEqual(self.allocate(self.request("second")), second)
        third = self.allocate(self.request("third"))
        self.assertTrue(third["system_due"])
        self.assertTrue(third["user_due"])
        self.assertEqual(third["sequence"], 3)

    def test_invalid_policy_and_unknown_target_preserve_full_hook_without_ledger(self):
        hook = Path(self.payload("unused")["callback_hook_path"])
        hook.write_text("USER HOOK")
        for invalid in ("system_every: 0", "system_every: true", "user_every: -1",
                        "system_every: 1.5", "[]", "system_every: ["):
            with self.subTest(invalid=invalid), contextlib.redirect_stderr(io.StringIO()):
                self.policy.write_text(invalid)
                selected = reminder_policy.select_delivery_prompt(self.payload("invalid"))
                self.assertIn("FULL", selected)
                self.assertIn("USER HOOK", selected)
                self.assertFalse(list((self.root / "prompt-counters").glob("*.json")))
        self.policy.unlink()
        payload = self.payload("unknown")
        payload["request"]["target"] = {"kind": "future-unknown"}
        selected = reminder_policy.select_delivery_prompt(payload)
        self.assertIn("FULL", selected)
        self.assertIn("USER HOOK", selected)
        self.assertFalse(list((self.root / "prompt-counters").glob("*.json")))

    def test_legacy_request_keeps_full_system_text_on_non_due_callback(self):
        self.allocate(self.request("first"))
        payload = self.payload("legacy", compact=False)
        selected = reminder_policy.select_delivery_prompt(payload)
        self.assertIn("FULL RESULT ACK TASK-SPECIFIC", selected)

    def test_policy_show_is_read_only_and_partial_updates_preserve_other_interval(self):
        state = self.policy.parent / "fresh-state"
        with mock.patch.object(layout, "daemon_state_dir", return_value=state), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(reminder_policy.callback_prompt_policy(argparse.Namespace(system_every=None, user_every=None)), 0)
            self.assertFalse(state.exists())
            self.assertIn("system_every: 4", output.getvalue())
            self.assertIn("user_every: 3", output.getvalue())
            self.assertEqual(reminder_policy.callback_prompt_policy(argparse.Namespace(system_every=2, user_every=None)), 0)
            self.assertEqual(reminder_policy.callback_prompt_policy(argparse.Namespace(system_every=None, user_every=7)), 0)
            saved = (state / "callback-prompts.yaml").read_text()
            self.assertIn("system_every: 2", saved)
            self.assertIn("user_every: 7", saved)
            for invalid in (0, -2, True):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(reminder_policy.callback_prompt_policy(argparse.Namespace(system_every=invalid, user_every=None)), 0)
                self.assertEqual((state / "callback-prompts.yaml").read_text(), saved)

    def test_compact_preparation_preserves_message_routing_and_ack_without_allocating(self):
        message = "Please inspect the result and any relevant artifacts.\nRecovery: inspect checkpoints.\nTemplate: author tests only."
        args = argparse.Namespace(agent="codex", task="retain-data", cwd="/tmp/work", command="false",
                                  exit_code=7, message=message)
        prompt = dispatch.build_prompt(args, duration=12.5)
        request = dispatch.prepare_request_for_queue(self.root, self.request("prepare-only"), prompt)
        compact = request["prompt_compact"]
        self.assertLess(len(compact), len(request["prompt"]))
        for retained in (message, "retain-data", "/tmp/work", "false", "7", "12.5", "parent",
                         "ack", "prepare-only", str(self.root)):
            self.assertIn(retained, compact)
        self.assertEqual(compact.count("Please inspect the result and any relevant artifacts."), 1)
        self.assertFalse((self.root / "prompt-counters").exists())


if __name__ == "__main__":
    unittest.main()
