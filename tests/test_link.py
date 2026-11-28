"""Two machines in one test: paired mailboxes over a shared directory and SSH (link-protocol.md, group N).

Each "machine" is its own REINS_HOME. The shared directory is a temporary
directory both see; SSH is a stand-in ``ssh`` that runs the other machine's
``reins _link-box`` for real. Carrying is done step by step in this process
(link_sync.step), so every intermediate state can be checked.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import unittest

from harness_reins.core import linkwire, messages as rules
from harness_reins.engine import link_sync, mailbox, messages, peering
from harness_reins.store import layout, link

from test_messages import CLAUDE_SESSION, CODEX_THREAD, MessageTestCase, inside

SRC = Path(__file__).resolve().parents[1] / "src"


class WireTests(unittest.TestCase):
    def test_addresses_and_aliases(self):
        self.assertEqual(linkwire.parse_address("reviewer"), ("reviewer", None))
        self.assertEqual(linkwire.parse_address("reviewer@gpu1"), ("reviewer", "gpu1"))
        with self.assertRaises(rules.Refused):
            linkwire.parse_address("reviewer@GPU 1")
        self.assertEqual(linkwire.alias_from("root@gpu1.lab.example"), "gpu1")
        self.assertEqual(linkwire.alias_from("10.0.0.5"), "10-0-0-5")
        self.assertEqual(linkwire.alias_from("@@@"), "peer")

    def test_receipts_only_move_forward(self):
        sent = {"state": "queued", "at": 1.0}
        self.assertTrue(linkwire.moves_forward(sent, "forwarded", 5.0))
        self.assertFalse(linkwire.moves_forward({"state": "published", "at": 5.0}, "forwarded", 9.0))
        self.assertTrue(linkwire.moves_forward({"state": "published", "at": 5.0}, "failed", 6.0))
        self.assertFalse(linkwire.moves_forward({"state": "failed", "at": 6.0}, "published", 5.0))
        self.assertFalse(linkwire.moves_forward({"state": "acked", "at": 6.0}, "failed", 9.0))

    def test_a_changed_byte_breaks_the_signature(self):
        key = linkwire.new_key()
        record = linkwire.signed(key, {"a": 1, "b": "text"})
        self.assertTrue(linkwire.verify(key, record))
        self.assertFalse(linkwire.verify(key, dict(record, b="texT")))
        self.assertFalse(linkwire.verify(linkwire.new_key(), record))

    def test_pair_codes(self):
        pair, key = linkwire.new_pair_id(), linkwire.new_key()
        self.assertEqual(linkwire.parse_pair_code(linkwire.pair_code(pair, key)), (pair, key))
        with self.assertRaises(rules.Refused):
            linkwire.parse_pair_code("reins-pair:p-1:2")


class DiagnosisTests(unittest.TestCase):
    def test_common_failures_read_as_their_cause(self):
        cases = {
            "ssh gpu1 exited 255: ssh: Could not resolve hostname gpu1: Name or service not known": "does not resolve",
            "ssh gpu1 exited 255: ssh: connect to host 10.0.0.5 port 22: Connection refused": "nothing answers SSH",
            "ssh gpu1 exited 255: ssh: connect to host 10.0.0.5 port 22: Connection timed out": "cannot be reached over",
            "ssh gpu1 exited 255: root@gpu1: Permission denied (publickey).": "refused the login",
            "ssh gpu1 exited 255: ssh: connect to host 10.0.0.5 port 22: Permission denied": "network policy",
            "ssh gpu1 exited 255: Host key verification failed.": "host key",
            "ssh gpu1 exited 127: sh: 1: exec: reins: not found": "Reins is not installed",
            "the shared directory /mnt/x/p-1 is not there": "is it mounted",
            "something new": "keeps retrying",
        }
        for text, cause in cases.items():
            self.assertIn(cause, link_sync.diagnose(text, "gpu1"), text)
        self.assertIn("`ssh gpu1 true`", link_sync.diagnose("Permission denied", "gpu1"))


class DirMailboxTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory(prefix="reins-mailbox-test-")
        self.addCleanup(tmp.cleanup)
        self.box = mailbox.DirMailbox(Path(tmp.name))
        self.compartment = linkwire.box("n-0123456789abcdef")

    def test_a_name_never_carries_two_contents(self):
        self.assertTrue(self.box.put(self.compartment, "b-1.json", b"{}"))
        self.assertFalse(self.box.put(self.compartment, "b-1.json", b"[]"))
        self.assertEqual(self.box.get(self.compartment, "b-1.json"), b"{}")

    def test_strays_and_half_written_files_are_invisible(self):
        directory = self.box.root / self.compartment
        directory.mkdir(parents=True)
        (directory / ".tmp-123-b-1.json").write_text("{", encoding="utf-8")
        (directory / "notes.json").write_text("{}", encoding="utf-8")
        (directory / "../escape.json").resolve()
        self.assertEqual(self.box.list(self.compartment), [])
        self.assertEqual(self.box.wait(self.compartment, 0), [])
        with self.assertRaises(rules.Refused):
            self.box.get(self.compartment, "../escape.json")
        with self.assertRaises(rules.Refused):
            self.box.list("to-somewhere-else")

    def test_batch_reports_each_operation(self):
        answers = self.box.batch([{"op": "put", "box": self.compartment, "name": "n-5.json", "data": mailbox._text(b"{}")},
                                  {"op": "list", "box": self.compartment},
                                  {"op": "get", "box": self.compartment, "name": "../x"},
                                  {"op": "nope", "box": self.compartment}])
        self.assertTrue(answers[0]["ok"])
        self.assertEqual(answers[1]["names"], ["n-5.json"])
        self.assertEqual(answers[2]["code"], "invalid_record")
        self.assertFalse(answers[3]["ok"])


class TwoMachines(MessageTestCase):
    """Machine A (this test's REINS_HOME) with Codex `planner`; machine B with Claude `reviewer`."""

    def setUp(self):
        super().setUp()
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.b_home = self.root / "b"
        self.b_queue = self.b_home / "queue"
        self.on_a = inside("codex", CODEX_THREAD)
        self.on_b = dict(inside("claude", CLAUDE_SESSION), REINS_HOME=str(self.b_home))
        self.shell_b = {"REINS_HOME": str(self.b_home)}
        self.assertEqual(self.reins(self.on_a, "session", "register", "planner")[0], 0)
        self.assertEqual(self.reins(self.on_b, "session", "register", "reviewer")[0], 0)

    def pair_dir(self, *wake):
        code, out, err = self.reins({}, "link", "connect", "--dir", str(self.shared), "--name", "b", *wake)
        self.assertEqual(code, 0, err)
        pairing = next(line for line in out.splitlines() if "--code" in line).split("--code ")[1].strip()
        code, out, err = self.reins(self.shell_b, "link", "connect", "--dir", str(self.shared), "--code", pairing,
                                    "--name", "a", *wake)
        self.assertEqual(code, 0, err)
        link_sync.step(self.queue, "b")  # A sees the acceptance
        return pairing

    def carry(self, rounds=3):
        for _ in range(rounds):
            for queue, alias in ((self.queue, "b"), (self.b_queue, "a")):
                messages.report(queue, now=time.time())
                link_sync.step(queue, alias)
                messages.import_inbound(queue)

    def sent_state(self, item):
        return link.load_sent(self.queue, item["id"])["sent"]


class SharedDirectoryTests(TwoMachines):
    def setUp(self):
        super().setUp()
        self.pairing = self.pair_dir()

    def test_the_handshake_agrees_on_both_sides(self):
        a, b = link.load_peer(self.queue, "b"), link.load_peer(self.b_queue, "a")
        self.assertEqual((a["state"], b["state"]), ("active", "active"))
        self.assertEqual(a["node"], link.node_id(self.b_queue))
        self.assertEqual(b["node"], link.node_id(self.queue))
        self.assertEqual((a["pair"], a["key"], a["protocol"]), (b["pair"], b["key"], b["protocol"]))
        self.assertEqual(a["features"], list(linkwire.FEATURES))
        self.assertFalse((Path(a["path"]) / "hello").exists() and any((Path(a["path"]) / "hello").iterdir()))
        mode = stat.S_IMODE(link.peer_path(self.queue, "b").stat().st_mode)
        if os.name != "nt":
            self.assertEqual(mode, 0o600)
        code, _, err = self.reins({"REINS_HOME": str(self.root / "c")}, "link", "connect", "--dir", str(self.shared),
                                  "--code", self.pairing)
        self.assertEqual(code, 2)  # a pairing code works once
        self.assertIn("no pairing offer", err)

    def test_a_join_cut_short_can_be_finished(self):
        alias_c = "c"
        c_home = {"REINS_HOME": str(self.root / "c")}
        code, out, err = self.reins({}, "link", "connect", "--dir", str(self.shared), "--name", alias_c)
        self.assertEqual(code, 0, err)
        pairing = next(line for line in out.splitlines() if "--code" in line).split("--code ")[1].strip()
        self.assertEqual(self.reins(c_home, "link", "connect", "--dir", str(self.shared), "--code", pairing)[0], 0)
        c_queue = self.root / "c" / "queue"
        link.remove_peer(c_queue, next(iter(link.peers(c_queue)))["alias"])  # as if it died before saving
        code, _, err = self.reins(c_home, "link", "connect", "--dir", str(self.shared), "--code", pairing)
        self.assertEqual(code, 0, err)
        link_sync.step(self.queue, alias_c)
        self.assertEqual(link.load_peer(self.queue, alias_c)["state"], "active")
        joined = next(iter(link.peers(c_queue)))
        self.assertEqual(joined["key"], link.load_peer(self.queue, alias_c)["key"])
        link.spool_write(link.outbox(c_queue, joined["alias"]), "b-1.json", b"{}")
        self.assertEqual(self.reins(c_home, "link", "connect", "--dir", str(self.shared), "--code", pairing)[0], 2)
        self.assertEqual(len(link.spool_files(link.outbox(c_queue, joined["alias"]))), 1)  # nothing was dropped

    def test_a_message_goes_there_and_its_state_comes_back(self):
        item = self.send(self.on_a, "reviewer@b", "--subject", "Review", "-m", "Please look at feature/queue.")
        self.assertEqual((item["state"], item["to"]), ("queued", "reviewer@b"))
        self.carry()
        self.assertEqual(self.sent_state(item)["state"], "forwarded")
        received = [m for m in link.all_messages(self.b_queue)]
        self.assertEqual(len(received), 1)
        message = received[0]
        self.assertEqual(message["origin_id"], item["id"])
        self.assertEqual(message["local"]["peer"], "a")
        self.assertEqual(rules.sender_text(message), "planner@a (codex)")
        request = json.loads(layout.request_path(self.b_queue, "pending", message["local"]["callback"]).read_text())
        self.assertEqual(request["callback_mode"], "cli")  # as on one machine: a closed Claude session is resumed
        self.assertIn("From: planner@a (codex)", request["prompt"])
        self.assertEqual(self.reins(self.shell_b, "ack", message["local"]["callback"])[0], 0)
        self.carry()
        self.assertEqual(self.sent_state(item)["state"], "acked")
        code, out, _ = self.reins(self.on_a, "inbox", "--sent")
        self.assertIn("acked", out)
        self.assertIn("to reviewer@b", out)

    def test_a_reply_finds_its_way_back_and_keeps_the_thread(self):
        item = self.send(self.on_a, "reviewer@b", "-m", "first")
        self.carry()
        message = link.all_messages(self.b_queue)[0]
        code, out, err = self.reins(self.on_b, "reply", message["id"], "--json", "-m", "looked at it")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["to"], "planner@a")
        self.carry()
        answer = [m for m in link.all_messages(self.queue) if m["local"].get("peer") == "b"]
        self.assertEqual(len(answer), 1)
        self.assertEqual((answer[0]["in_reply_to"], answer[0]["thread"], answer[0]["hop"]), (item["id"], item["thread"], 1))
        self.assertEqual(self.sent_state(item)["state"], "acked")  # the reply was also the ACK
        code, out, err = self.reins(self.on_a, "reply", answer[0]["id"], "--json", "-m", "thanks")
        self.assertEqual(code, 0, err)
        self.carry()
        third = [m for m in link.all_messages(self.b_queue) if m["hop"] == 2]
        self.assertEqual(len(third), 1)

    def test_an_unknown_name_comes_back_rejected(self):
        item = self.send(self.on_a, "nobody@b", "-m", "hello")
        self.carry()
        sent = self.sent_state(item)
        self.assertEqual((sent["state"], sent["code"]), ("rejected", "unknown_name"))
        self.assertEqual(link.all_messages(self.b_queue), [])

    def test_an_unpaired_machine_is_refused_at_once(self):
        code, _, err = self.reins(self.on_a, "send", "reviewer@elsewhere", "-m", "hi")
        self.assertEqual(code, 2)
        self.assertIn("unknown_peer", err)
        self.assertIn("Paired: b", err)

    def test_a_repeated_file_makes_no_second_callback(self):
        self.send(self.on_a, "reviewer@b", "-m", "once")
        peer = link.load_peer(self.queue, "b")
        files = link.spool_files(link.outbox(self.queue, "b"))
        copy = files[0].read_bytes()
        self.carry()
        for _ in range(2):  # the same file again, as a crash between fetch and remove would bring it
            link.spool_write(link.inbound(self.b_queue, "a"), files[0].name, copy)
            messages.import_inbound(self.b_queue)
        self.assertEqual(len(link.all_messages(self.b_queue)), 1)
        self.assertEqual(len(list((self.b_queue / "pending").glob("m-*.json"))), 1)
        self.assertEqual(peer["alias"], "b")

    def test_a_file_without_the_key_is_set_aside(self):
        b_peer = link.load_peer(self.b_queue, "a")
        forged = dict(b_peer, key=linkwire.new_key(), node=link.node_id(self.queue), pair=b_peer["pair"])
        stranger = linkwire.wrap(dict(forged, node=link.node_id(self.b_queue)), link.node_id(self.queue), "message",
                                 {"id": "m-0000000000000001"})
        mailbox.DirMailbox(Path(b_peer["path"])).put(linkwire.box(link.node_id(self.b_queue)), "m-0000000000000001.json",
                                                    linkwire.encode(stranger))
        link_sync.step(self.b_queue, "a")
        messages.import_inbound(self.b_queue)
        self.assertEqual(link.all_messages(self.b_queue), [])
        self.assertEqual([p.name for p in link.spool_files(link.quarantine(self.b_queue, "a"))], ["m-0000000000000001.json"])
        self.assertTrue(any("set aside" in str(alert.get("reason")) for alert in link.alerts(self.b_queue)))
        from harness_reins.install import diagnostics
        self.assertEqual([issue["code"] for issue in diagnostics.message_issues(self.b_queue)], ["link_file_set_aside"])

    def test_a_reply_cannot_lower_its_hop(self):
        a_peer = link.load_peer(self.queue, "b")
        node_a = link.node_id(self.queue)
        message = rules.new_message(node=node_a, sender_name="planner", sender_harness="codex", to="reviewer",
                                    subject="s", body="b", refs=[], wake="never", thread=None,
                                    in_reply_to="m-0000000000000abc", hop=0, ttl_days=7)
        message["to"]["node"] = a_peer["node"]
        link.spool_write(link.outbox(self.queue, "b"), f"{message['id']}.json",
                         linkwire.encode(linkwire.wrap(a_peer, node_a, "message", message)))
        link.save_sent(self.queue, dict(message, sent={"peer": "b", "state": "queued", "at": time.time()}))
        self.carry()
        sent = link.load_sent(self.queue, message["id"])["sent"]
        self.assertEqual((sent["state"], sent["code"]), ("rejected", "invalid_record"))

    def craft(self, **changes):
        """A message file from A to B written by hand, for what `reins send` would never produce."""
        a_peer = link.load_peer(self.queue, "b")
        node_a = link.node_id(self.queue)
        message = rules.new_message(node=node_a, sender_name="planner", sender_harness="codex", to="reviewer",
                                    subject="s", body="b", refs=[], wake="never", thread=None, in_reply_to=None,
                                    hop=0, ttl_days=7)
        message["to"]["node"] = a_peer["node"]
        message.update(changes)
        link.spool_write(link.outbox(self.queue, "b"), f"{message['id']}.json",
                         linkwire.encode(linkwire.wrap(a_peer, node_a, "message", message)))
        link.save_sent(self.queue, dict(message, sent={"peer": "b", "state": "queued", "at": time.time()}))
        return message

    def test_a_send_cut_short_is_repaired(self):
        item = self.send(self.on_a, "reviewer@b", "-m", "survives a crash")
        record = link.load_sent(self.queue, item["id"])
        outbox = link.outbox(self.queue, "b") / f"{item['id']}.json"
        self.assertNotIn("wire", record["sent"])
        record["sent"]["wire"] = outbox.read_text(encoding="utf-8")  # as if the process died right after the record
        link.save_sent(self.queue, record)
        outbox.unlink()
        messages.maintain(self.queue, now=time.time())
        self.assertTrue(outbox.exists())
        self.carry()
        self.assertEqual(len(link.all_messages(self.b_queue)), 1)
        self.assertEqual(self.sent_state(item)["state"], "forwarded")

    def test_a_message_that_expired_on_the_way_is_not_delivered(self):
        message = self.craft(expires_at="2020-01-01T00:00:00.000Z")
        self.carry()
        self.assertEqual(link.all_messages(self.b_queue), [])
        sent = link.load_sent(self.queue, message["id"])["sent"]
        self.assertEqual((sent["state"], sent["code"]), ("expired", "expired"))

    def test_a_message_that_left_is_not_expired_by_the_sender(self):
        item = self.send(self.on_a, "reviewer@b", "-m", "on its way")
        link_sync.step(self.queue, "b")  # in the mailbox now, not yet taken
        record = link.load_sent(self.queue, item["id"])
        messages.maintain(self.queue, now=time.time() + 30 * 86400)
        self.assertEqual(link.load_sent(self.queue, item["id"])["sent"]["state"], "queued")
        self.assertEqual(record["id"], item["id"])

    def test_unknown_requirements_are_refused(self):
        message = self.craft(requires=["spawn"])
        self.carry()
        self.assertEqual(link.all_messages(self.b_queue), [])
        self.assertEqual(link.load_sent(self.queue, message["id"])["sent"]["code"], "unsupported_capability")

    def test_the_receiver_limits_count_the_machine_not_the_name(self):
        path = layout.link_root(self.b_queue) / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"limits": {"sender_per_minute": 2}}), encoding="utf-8")
        crafted = [self.craft(**{"from": {"node": link.node_id(self.queue), "name": name, "harness": "codex"}})
                   for name in ("one", "two", "three")]
        self.carry()
        states = [link.load_sent(self.queue, m["id"])["sent"] for m in crafted]
        self.assertEqual([state["state"] for state in states], ["forwarded", "forwarded", "rejected"])
        self.assertEqual(states[2]["code"], "limit_exceeded")

    def test_a_file_too_large_or_too_deep_does_not_block_the_rest(self):
        b_peer = link.load_peer(self.b_queue, "a")
        compartment = linkwire.box(link.node_id(self.b_queue))
        root = Path(b_peer["path"]) / compartment
        root.mkdir(parents=True, exist_ok=True)
        (root / "m-00000000000000aa.json").write_bytes(b" " * (linkwire.FILE_LIMIT + 1))
        (root / "m-00000000000000ab.json").write_bytes(b"[" * 200000)
        (root / "m-00000000000000ac.json").write_bytes(b"\xff\xfe not text")
        self.send(self.on_a, "reviewer@b", "-m", "after the junk")
        self.carry()
        self.assertEqual([m["body"] for m in link.all_messages(self.b_queue)], ["after the junk"])
        self.assertEqual(sorted(p.name for p in root.iterdir()), [])
        self.assertEqual(sorted(p.name for p in link.spool_files(link.quarantine(self.b_queue, "a"))),
                         ["m-00000000000000aa.json", "m-00000000000000ab.json", "m-00000000000000ac.json"])
        self.assertTrue(any("m-00000000000000aa" in str(alert.get("reason")) or "set aside" in str(alert.get("reason"))
                            for alert in link.alerts(self.b_queue)))

    def test_an_older_names_list_never_replaces_a_newer_one(self):
        b_peer = link.load_peer(self.b_queue, "a")
        messages._import_names(self.b_queue, b_peer, {"names": [{"name": "new", "harness": "pi"}], "at": 200.0})
        messages._import_names(self.b_queue, b_peer, {"names": [{"name": "old", "harness": "pi"}], "at": 100.0})
        self.assertEqual([item["name"] for item in link.load_names(self.b_queue, "a")], ["new"])

    def helper(self):
        """A second session on machine A, a local Claude Code one."""
        env = inside("claude", "1f0e2d3c-4b5a-4987-8a6b-5c4d3e2f1a00")
        self.assertEqual(self.reins(env, "session", "register", "helper")[0], 0)

    def test_one_send_reaches_several_sessions_here_and_there(self):
        self.helper()
        code, out, err = self.reins(self.on_a, "send", "helper", "reviewer@b", "helper", "--json", "-m", "rebase")
        self.assertEqual(code, 0, err)
        sent = json.loads(out)
        self.assertEqual([item["to"] for item in sent], ["helper", "reviewer@b"])  # one each, the repeat dropped
        self.assertNotEqual(sent[0]["thread"], sent[1]["thread"])  # ordinary messages, each its own thread
        self.carry()
        self.assertEqual([m["body"] for m in link.all_messages(self.b_queue)], ["rebase"])
        self.assertEqual(self.sent_state(sent[1])["state"], "forwarded")
        self.assertEqual(len([m for m in link.all_messages(self.queue) if m["to"]["name"] == "helper"]), 1)

    def test_several_names_go_to_all_or_none(self):
        self.helper()
        code, _, err = self.reins(self.on_a, "send", "helper", "nobody", "reviewer@b", "-m", "x")
        self.assertEqual(code, 2)
        self.assertIn("nothing was sent to anyone", err)
        code, _, err = self.reins(self.on_a, "send", "helper", "reviewer@elsewhere", "-m", "x")
        self.assertEqual(code, 2)
        self.assertIn("unknown_peer", err)
        self.configure(sender_per_minute=2)
        self.assertEqual(self.send(self.on_a, "helper", "-m", "one")["to"], "helper")
        code, _, err = self.reins(self.on_a, "send", "helper", "reviewer@b", "-m", "two and three")
        self.assertEqual(code, 3)
        self.assertIn("Nothing was sent to anyone", err)
        self.assertEqual(len(link.all_messages(self.queue)), 1)
        self.assertEqual(link.all_sent(self.queue), [])
        self.assertEqual(link.spool_files(link.outbox(self.queue, "b")), [])
        self.configure(pending_messages=2)  # one already waits (helper's "one"): two more would pass the limit
        code, _, err = self.reins(self.on_a, "send", "helper", "planner", "-m", "both or neither")
        self.assertEqual(code, 3)
        self.assertIn("Nothing was sent to anyone", err)
        self.assertEqual(len(link.all_messages(self.queue)), 1)
        self.configure(pending_messages=2, sender_per_minute=100)
        code, out, err = self.reins(self.on_a, "send", "helper", "reviewer@b", "--json", "-m", "fills the last slot")
        self.assertEqual(code, 0, err)  # the remote one waits in the outbox, not as a callback here
        self.assertEqual([item["to"] for item in json.loads(out)], ["helper", "reviewer@b"])

    def test_session_names_travel_and_show_in_the_list(self):
        self.carry()
        rows = json.loads(self.reins(self.on_a, "session", "list", "--json")[1])
        self.assertIn({"name": "reviewer@b", "harness": "claude"},
                      [{"name": row["name"], "harness": row["harness"]} for row in rows])
        code, out, _ = self.reins({}, "link", "list")
        self.assertEqual(code, 0)
        self.assertIn("reviewer@b", out)

    def test_limits_count_the_sender_with_its_machine(self):
        self.configure(pair_per_hour=2)
        for text in ("1", "2"):
            self.send(self.on_a, "reviewer@b", "-m", text)
        code, _, err = self.reins(self.on_a, "send", "reviewer@b", "-m", "3")
        self.assertEqual(code, 3)
        self.assertIn("reviewer@b", err)

    def test_an_unmounted_shared_directory_is_reported_not_written_under(self):
        root = Path(link.load_peer(self.queue, "b")["path"])
        self.shared.rename(self.root / "unmounted")
        self.send(self.on_a, "reviewer@b", "-m", "waits")
        peer = link.load_peer(self.queue, "b")
        status = link_sync._Status(self.queue, "b", str(root))
        with self.assertRaises(mailbox.MailboxError) as raised:
            link_sync.send_once(self.queue, peer, peering.open_mailbox(self.queue, peer),
                                linkwire.box(peer["node"]))
        status.error("send", str(raised.exception))
        self.assertFalse(self.shared.exists())  # nothing recreated on the disk under the mount point
        self.assertIn("is it mounted", link_sync.problem(self.queue, "b"))
        self.assertEqual(len(link.spool_files(link.outbox(self.queue, "b"))), 1)  # still waiting to go

    def test_wake_is_a_setting_of_the_receiving_side(self):
        self.assertEqual(link.load_peer(self.b_queue, "a")["wake"], "resume")
        code, out, err = self.reins(self.shell_b, "link", "connect", "a", "--wake", "never")
        self.assertEqual(code, 0, err)
        self.assertEqual(link.load_peer(self.b_queue, "a")["wake"], "never")
        self.assertEqual(link.load_peer(self.queue, "b")["wake"], "resume")
        item = self.send(self.on_a, "reviewer@b", "-m", "may not wake")
        self.carry()
        message = link.all_messages(self.b_queue)[0]
        self.assertEqual(message["local"]["wake"], "never")  # Claude Code would resume, but B said never for A
        self.assertEqual(self.sent_state(item)["state"], "forwarded")
        self.assertIn("already paired", self.reins(self.shell_b, "link", "connect", "a")[1])

    def test_unpairing_reaches_the_other_side(self):
        self.send(self.on_a, "reviewer@b", "-m", "left behind")
        code, out, err = self.reins({}, "link", "remove", "b")
        self.assertEqual(code, 0, err)
        self.assertIn("it was told", out)
        self.assertIsNone(link.load_peer(self.queue, "b"))
        failed = [record["sent"] for record in link.all_sent(self.queue)]
        self.assertEqual([item["state"] for item in failed], ["failed"])
        link_sync.step(self.b_queue, "a")
        messages.import_inbound(self.b_queue)
        self.assertIsNone(link.load_peer(self.b_queue, "a"))

    def configure(self, **limits):
        path = layout.link_root(self.queue) / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"limits": limits}), encoding="utf-8")


class SshTests(TwoMachines):
    """``ssh`` is a script that runs the remote command locally as machine B; everything after it is real."""

    def setUp(self):
        super().setUp()
        if os.name == "nt":
            raise unittest.SkipTest("the stand-in ssh is a shell script")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "reins"
        shim.write_text(f"#!/bin/sh\nPYTHONPATH={SRC} exec {sys.executable} -m harness_reins \"$@\"\n", encoding="utf-8")
        shim.chmod(0o755)
        fake = bin_dir / "fake-ssh"
        fake.write_text("#!/bin/sh\nfor last; do :; done\n"
                        f"HOME={self.b_home.parent / 'b-user'} REINS_HOME={self.b_home} PATH={bin_dir}:$PATH "
                        "exec sh -c \"$last\"\n", encoding="utf-8")
        fake.chmod(0o755)
        os.environ[mailbox.SSH_ENV] = str(fake)
        self.base[mailbox.SSH_ENV] = str(fake)

    def test_one_command_pairs_and_messages_flow_both_ways(self):
        code, out, err = self.reins({}, "link", "connect", "gpu-b", "--name", "b")
        self.assertEqual(code, 0, err)
        self.assertIn("this machine carries", out)
        a, b = link.load_peer(self.queue, "b"), next(iter(link.peers(self.b_queue)))
        self.assertEqual((a["role"], b["role"]), ("carrier", "host"))
        self.assertEqual(a["key"], b["key"])
        self.assertTrue(link.mailbox_root(self.b_queue, b["pair"]).is_dir())
        alias_on_b = b["alias"]
        item = self.send(self.on_a, "reviewer@b", "-m", "over ssh")
        for _ in range(3):
            messages.report(self.queue, now=time.time())
            link_sync.step(self.queue, "b")  # every move through the mailbox happens from A
            messages.import_inbound(self.queue)
            messages.report(self.b_queue, now=time.time())
            link_sync.step(self.b_queue, alias_on_b)  # B only reads and writes its own disk
            messages.import_inbound(self.b_queue)
        message = link.all_messages(self.b_queue)[0]
        self.assertEqual(self.sent_state(item)["state"], "forwarded")
        code, _, err = self.reins(self.on_b, "reply", message["id"], "-m", "got it over ssh")
        self.assertEqual(code, 0, err)
        for _ in range(3):
            link_sync.step(self.b_queue, alias_on_b)
            link_sync.step(self.queue, "b")
            messages.import_inbound(self.queue)
            messages.report(self.b_queue, now=time.time())
        replies = [m for m in link.all_messages(self.queue) if m["local"].get("peer") == "b"]
        self.assertEqual([m["body"] for m in replies], ["got it over ssh"])

    def test_a_restricted_key_cannot_pair_again(self):
        answer = mailbox.ssh_call("gpu-b", ["--hello", "--pair", linkwire.new_pair_id()],
                                  linkwire.hello(link.node_id(self.queue), "a"), 30)
        self.assertEqual(answer["code"], "permission_denied")

    def test_the_hosting_side_can_allow_wakes_without_ssh(self):
        self.assertEqual(self.reins({}, "link", "connect", "gpu-b", "--name", "b")[0], 0)
        alias_on_b = next(iter(link.peers(self.b_queue)))["alias"]
        code, out, err = self.reins(self.shell_b, "link", "connect", alias_on_b, "--wake", "never")
        self.assertEqual(code, 0, err)
        self.assertEqual(link.load_peer(self.b_queue, alias_on_b)["wake"], "never")

    def test_status_names_a_link_that_fails(self):
        self.assertEqual(self.reins({}, "link", "connect", "gpu-b", "--name", "b")[0], 0)
        os.environ[mailbox.SSH_ENV] = "false"
        peer = link.load_peer(self.queue, "b")
        status = link_sync._Status(self.queue, "b")
        try:
            link_sync.receive_once(self.queue, peer, link.node_id(self.queue), peering.open_mailbox(self.queue, peer),
                                   linkwire.box(link.node_id(self.queue)), [], 0)
        except mailbox.MailboxError as exc:
            status.error("receive", str(exc))
        code, out, _ = self.reins({}, "status")
        self.assertIn("[reins] link to b: receive:", out)
        self.assertIn("ssh gpu-b exited 1", out)
        self.assertIn("since ", out)
        code, _, err = self.reins(self.on_a, "send", "reviewer@b", "-m", "while it is down")
        self.assertEqual(code, 0, err)
        self.assertIn("b cannot be reached right now", err)
        self.assertIn("waits in the outbox", err)
        self.assertIn("NOT GETTING THROUGH", self.reins({}, "link", "list")[1])

    def test_unpairing_on_the_hosting_side_reaches_the_carrier(self):
        self.assertEqual(self.reins({}, "link", "connect", "gpu-b", "--name", "b")[0], 0)
        alias_on_b = next(iter(link.peers(self.b_queue)))["alias"]
        pair = link.load_peer(self.queue, "b")["pair"]
        self.send(self.on_a, "reviewer@b", "-m", "too late")
        code, out, err = self.reins(self.shell_b, "link", "remove", alias_on_b)
        self.assertEqual(code, 0, err)
        self.assertIn("finds out the next time it connects", out)
        self.assertFalse(link.mailbox_root(self.b_queue, pair).exists())
        waiting = link.inbound(self.queue, "b")
        link.spool_write(waiting, "n-1.json", b"{}")  # fetched before the host forgot the pairing
        link_sync.step(self.queue, "b")
        self.assertEqual(link.load_peer(self.queue, "b")["state"], "gone")
        messages.import_inbound(self.queue)  # imports (here: sets aside) what was fetched, then forgets
        self.assertEqual(link.spool_files(waiting), [])
        self.assertIsNone(link.load_peer(self.queue, "b"))
        self.assertEqual([record["sent"]["state"] for record in link.all_sent(self.queue)], ["failed"])
        self.assertTrue(any("no longer knows this pairing" in str(alert.get("reason")) for alert in link.alerts(self.queue)))

    def test_pairing_again_needs_an_unpairing_first(self):
        self.assertEqual(self.reins({}, "link", "connect", "gpu-b", "--name", "b")[0], 0)
        pair = link.load_peer(self.queue, "b")["pair"]
        code, _, err = self.reins({}, "link", "connect", "gpu-b")
        self.assertEqual(code, 2)
        self.assertIn("reins link remove b", err)
        self.assertIn("To pair again", self.reins({}, "link", "connect", "b")[1])
        self.assertEqual(link.load_peer(self.queue, "b")["pair"], pair)
        self.assertEqual(next(iter(link.peers(self.b_queue)))["pair"], pair)  # nothing changed on the other side
        self.send(self.on_a, "reviewer@b", "-m", "lost with the old pairing")
        self.assertEqual(self.reins({}, "link", "remove", "b")[0], 0)
        self.assertEqual([record["sent"]["state"] for record in link.all_sent(self.queue)], ["failed"])
        self.assertEqual(self.reins({}, "link", "connect", "gpu-b", "--name", "b")[0], 0)
        self.assertEqual(len(link.peers(self.b_queue)), 1)  # the host replaced the pairing this machine left

    def test_an_unreachable_host_says_what_to_check(self):
        os.environ[mailbox.SSH_ENV] = "false"
        self.base[mailbox.SSH_ENV] = "false"
        code, _, err = self.reins({}, "link", "connect", "nowhere")
        self.assertEqual(code, 2)
        self.assertIn("could not reach", err)
        self.assertEqual(link.peers(self.queue), [])


class RealDaemonTests(TwoMachines):
    """Two real daemons: each starts its own _link-sync, which carries without any help from the test."""

    def setUp(self):
        super().setUp()
        if os.name == "nt":
            raise unittest.SkipTest("POSIX process handling")
        import subprocess
        self.pair_dir("--wake", "never")  # these daemons must not start a real Claude
        self.daemons = []
        for home in (self.root / "reins", self.b_home):
            env = dict(self.base, REINS_HOME=str(home), PYTHONPATH=str(SRC))
            daemon = subprocess.Popen([sys.executable, "-m", "harness_reins", "daemon", "--interval", "0.2"], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.daemons.append(daemon)
            self.addCleanup(self.stop, daemon)

    @staticmethod
    def stop(daemon):
        if daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=10)
        daemon.stderr.close()

    def wait_for(self, check, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.2)
        self.fail("timed out; daemons: " + "; ".join(str(d.poll()) for d in self.daemons))

    def test_messages_and_receipts_travel_on_their_own(self):
        item = self.send(self.on_a, "reviewer@b", "-m", "carried by the daemons")
        received = self.wait_for(lambda: link.all_messages(self.b_queue))
        self.assertEqual(received[0]["body"], "carried by the daemons")
        self.wait_for(lambda: self.sent_state(item)["state"] == "forwarded")
        self.assertEqual(self.reins(self.shell_b, "ack", received[0]["local"]["callback"])[0], 0)
        self.wait_for(lambda: self.sent_state(item)["state"] == "acked")
        self.assertTrue(link_sync.running(self.queue, "b"))
        self.assertTrue(link_sync.running(self.b_queue, "a"))
        for daemon in self.daemons:
            self.stop(daemon)
        # A carrier ends with the daemon that started it.
        self.wait_for(lambda: not link_sync.running(self.queue, "b") and not link_sync.running(self.b_queue, "a"),
                      timeout=10)


if __name__ == "__main__":
    unittest.main()
