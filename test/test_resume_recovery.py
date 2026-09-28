"""简历扫描、旧库迁移和附件入库恢复；所有外部服务使用内存替身。"""
import copy
import imaplib
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

import resume_pipeline as p


def resume_mail(files=()):
    message = EmailMessage()
    message["From"] = "candidate@example.com"
    message["Subject"] = "报名"
    message["Date"] = "Mon, 28 Sep 2026 10:00:00 +0800"
    message["Message-ID"] = "<candidate-message@example.com>"
    message.set_content("报名材料")
    for filename, data in files:
        message.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    return message.as_bytes()


class FakeImap:
    def __init__(self, messages=None, validity="1"):
        self.messages = messages or {}
        self.validity = validity
        self.fetches = []

    def response(self, name):
        return name, [self.validity.encode()]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [" ".join(self.messages).encode()]
        uid = args[0]
        self.fetches.append(uid)
        value = self.messages[uid]
        if isinstance(value, Exception):
            raise value
        return "OK", [(b"BODY[]", value)]

    def close(self):
        pass

    def logout(self):
        pass


def mail_client(connection, username="account@example.com"):
    client = p.ImapMailClient.__new__(p.ImapMailClient)
    client.host, client.port, client.username, client.folder = "imap.example.com", 993, username, "INBOX"
    client.conn = connection
    client._raw_by_id = {}
    return client


class FakeFeishu:
    app_token = "app"
    table_id = "table"

    def __init__(self):
        self.uploads = []
        self.records = {}
        self.submissions = []
        self.fail_upload = None
        self.lose_response = False
        self.hide_records = False
        self.lookup_error = False

    def upload_resume(self, filename, data):
        if filename == self.fail_upload:
            raise RuntimeError("upload rejected")
        self.uploads.append((filename, data))
        return f"file{len(self.uploads)}"

    def build_record_fields(self, **values):
        return {p.FIELD_ATTACHMENT: [{"file_token": values["file_token"]}],
                p.FIELD_NAME: values["ai"]["name"]}

    def submit_record_fields(self, fields, client_token):
        self.submissions.append((copy.deepcopy(fields), client_token))
        if client_token not in self.records:
            self.records[client_token] = (f"rec{len(self.records) + 1}", copy.deepcopy(fields))
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("server committed but response was lost")
        return self.records[client_token][0]

    def find_record_by_attachment(self, token, field_name):
        if self.lookup_error:
            raise RuntimeError("read permission denied")
        if not self.hide_records:
            for record_id, fields in self.records.values():
                if any(item["file_token"] == token for item in fields[field_name]):
                    return record_id
        return None


class ResumeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "resume.db")
        self.db = p.StateDB(self.path)
        self.addCleanup(lambda: self.db.conn.close())
        self.feishu = FakeFeishu()
        self.parse = patch.object(p, "resume_to_text", return_value="synthetic resume").start()
        self.analyze = patch.object(p, "analyze_resume", return_value={"name": "测试"}).start()
        self.addCleanup(patch.stopall)

    def cycle(self, client, process_existing=True):
        with patch.object(p, "PROCESS_EXISTING_ON_FIRST_RUN", process_existing):
            p.run_once(client, self.feishu, self.db)

    def reopen(self):
        self.db.conn.close()
        self.db = p.StateDB(self.path)

    def test_backlog_larger_than_batch_is_fully_processed_oldest_first(self):
        client = mail_client(FakeImap({str(i): resume_mail() for i in range(1, 8)}))
        with patch.object(p, "EMAIL_PAGE_SIZE", 2):
            self.cycle(client)
        self.assertEqual(client.conn.fetches, [str(i) for i in range(1, 8)])
        self.assertTrue(all(self.db.seen(mid) for mid in client.message_ids()))
        self.assertEqual(client._raw_by_id, {})
        self.cycle(client)
        self.assertEqual(len(client.conn.fetches), 7)

    def test_first_run_skips_entire_snapshot_without_downloading(self):
        client = mail_client(FakeImap({str(i): resume_mail() for i in range(1, 8)}))
        with patch.object(p, "EMAIL_PAGE_SIZE", 2):
            self.cycle(client, process_existing=False)
            self.assertEqual(client.conn.fetches, [])
            client.conn.messages.update({str(i): resume_mail() for i in range(8, 13)})
            self.cycle(client, process_existing=False)
        self.assertEqual(client.conn.fetches, ["8", "9", "10", "11", "12"])

    def test_failed_fetch_does_not_starve_later_uids_and_retries(self):
        client = mail_client(FakeImap({"1": ValueError("bad MIME fetch"), "2": resume_mail()}))
        self.cycle(client)
        self.assertFalse(self.db.seen(client.scope() + ":1"))
        self.assertTrue(self.db.seen(client.scope() + ":2"))
        client.conn.messages["1"] = resume_mail()
        self.cycle(client)
        self.assertEqual(client.conn.fetches, ["1", "2", "1"])

    def test_same_uid_in_different_account_or_validity_is_distinct(self):
        clients = [
            mail_client(FakeImap({"1": resume_mail()}, "10")),
            mail_client(FakeImap({"1": resume_mail()}, "20")),
            mail_client(FakeImap({"1": resume_mail()}, "10"), "other@example.com"),
        ]
        for client in clients:
            self.cycle(client)
            self.assertEqual(client.conn.fetches, ["1"])
        self.assertEqual(len({c.scope() for c in clients}), 3)

    def test_new_scope_respects_first_run_skip_setting(self):
        old = mail_client(FakeImap({"1": resume_mail()}, "10"))
        self.cycle(old)
        new = mail_client(FakeImap({"1": resume_mail()}, "20"))
        self.cycle(new, process_existing=False)
        self.assertEqual(new.conn.fetches, [])
        self.assertNotEqual(old.scope(), new.scope())

    def test_reconnect_with_changed_validity_never_fetches_old_uid(self):
        original = FakeImap({"1": imaplib.IMAP4.abort("disconnect")}, "10")
        replacement = FakeImap({"1": resume_mail()}, "20")
        client = mail_client(original)
        old_scope = client.scope()
        with patch.object(client, "_connect", side_effect=lambda: setattr(client, "conn", replacement)):
            with self.assertRaises(p.MailboxScopeChanged):
                self.cycle(client)
        self.assertEqual(replacement.fetches, [])
        self.assertFalse(self.db.seen(old_scope + ":1"))

    def test_missing_uidvalidity_fails_before_fetch(self):
        client = mail_client(FakeImap({"1": resume_mail()}, ""))
        with self.assertRaisesRegex(RuntimeError, "UIDVALIDITY"):
            self.cycle(client)
        self.assertEqual(client.conn.fetches, [])

    def test_partial_attachment_success_survives_restart_without_duplicates(self):
        client = mail_client(FakeImap({"1": resume_mail([("a.pdf", b"a"), ("b.docx", b"b")])}))
        self.feishu.fail_upload = "b.docx"
        self.cycle(client)
        self.assertEqual(len(self.feishu.records), 1)
        self.assertFalse(self.db.seen(client.scope() + ":1"))
        self.assertEqual(self.db.pending_replies(), [])
        self.reopen()
        self.feishu.fail_upload = None
        self.cycle(client)
        self.assertEqual([name for name, _ in self.feishu.uploads], ["a.pdf", "b.docx"])
        self.assertEqual(len(self.feishu.records), 2)
        self.assertEqual(self.analyze.call_count, 2)
        self.assertEqual(len(self.db.pending_replies()), 1)
        self.assertTrue(self.db.seen(client.scope() + ":1"))
        self.cycle(client)
        self.assertEqual(len(self.feishu.submissions), 2)

    def test_lost_response_is_reconciled_after_restart_without_posting_again(self):
        client = mail_client(FakeImap({"1": resume_mail([("a.pdf", b"a")])}))
        self.feishu.lose_response = True
        self.cycle(client)
        self.reopen()
        self.cycle(client)
        self.assertEqual(len(self.feishu.records), 1)
        self.assertEqual(len(self.feishu.submissions), 1)
        self.assertEqual(len(self.feishu.uploads), 1)
        self.assertTrue(self.db.seen(client.scope() + ":1"))

    def test_retry_reuses_identical_payload_and_uuid_when_record_not_visible_yet(self):
        client = mail_client(FakeImap({"1": resume_mail([("a.pdf", b"a")])}))
        self.feishu.lose_response = True
        self.cycle(client)
        self.reopen()
        self.feishu.hide_records = True
        self.analyze.return_value = {"name": "different model response"}
        self.cycle(client)
        self.assertEqual(len(self.feishu.records), 1)
        self.assertEqual(self.feishu.submissions[0], self.feishu.submissions[1])
        self.assertEqual(uuid.UUID(self.feishu.submissions[0][1]).version, 4)
        self.assertEqual(self.analyze.call_count, 1)

    def test_failed_remote_reconciliation_never_posts_blindly(self):
        client = mail_client(FakeImap({"1": resume_mail([("a.pdf", b"a")])}))
        self.feishu.lose_response = True
        self.cycle(client)
        self.feishu.lookup_error = True
        self.cycle(client)
        self.assertEqual(len(self.feishu.submissions), 1)
        self.assertFalse(self.db.seen(client.scope() + ":1"))

    def test_changed_target_table_does_not_reuse_or_recreate_partial_records(self):
        client = mail_client(FakeImap({"1": resume_mail([("a.pdf", b"a")])}))
        self.feishu.lose_response = True
        self.cycle(client)
        self.feishu.table_id = "another-table"
        self.cycle(client)
        self.assertEqual(len(self.feishu.submissions), 1)
        self.assertFalse(self.db.seen(client.scope() + ":1"))

    def test_old_schema_is_preserved_and_requires_explicit_adoption(self):
        # Construct a real legacy database without any v2 schema first.
        self.db.conn.close()
        old_path = str(Path(self.temp.name) / "legacy.db")
        with sqlite3.connect(old_path) as old:
            old.executescript("""
                CREATE TABLE seen_messages(message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
                CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
                INSERT INTO seen_messages VALUES ('imap:INBOX:4', 'old timestamp');
                INSERT INTO seen_messages VALUES ('imap:INBOX:6', 'old timestamp');
                INSERT INTO meta VALUES ('initialized', '1');
            """)
        self.db = p.StateDB(old_path)
        self.db.queue_reply("old-reply", "old@example.com", "old subject")
        client = mail_client(FakeImap({str(i): resume_mail() for i in range(1, 9)}))
        with self.assertRaisesRegex(RuntimeError, "adopt-legacy-state"):
            self.cycle(client)
        self.assertEqual(client.conn.fetches, [])
        self.db.initialize_mailbox(client.scope(), client.message_ids(), client.folder, False, True)
        self.cycle(client, False)
        self.assertEqual(client.conn.fetches, ["5", "7", "8"])
        self.assertTrue(self.db.seen("imap:INBOX:4"))
        self.assertEqual(self.db.pending_replies()[0]["reply_key"], "old-reply")
        self.assertEqual(self.db.meta("legacy_scope"), client.scope())

    def test_legacy_binding_cannot_be_reused_by_another_scope(self):
        self.db.mark("imap:INBOX:1")
        first = mail_client(FakeImap({"1": resume_mail()}, "10"))
        self.db.initialize_mailbox(first.scope(), first.message_ids(), "INBOX", True, True)
        second = mail_client(FakeImap({"1": resume_mail()}, "20"))
        self.cycle(second, True)
        self.assertEqual(second.conn.fetches, ["1"])

    def test_wrong_legacy_folder_rolls_back_migration(self):
        self.db.mark("imap:AnotherFolder:1")
        client = mail_client(FakeImap({"1": resume_mail()}))
        with self.assertRaisesRegex(RuntimeError, "EMAIL_FOLDER"):
            self.db.initialize_mailbox(client.scope(), client.message_ids(), "INBOX", False, True)
        self.assertIsNone(self.db.meta("legacy_scope"))
        self.assertIsNone(self.db.meta("initialized:" + client.scope()))

    def test_migration_cli_backs_up_without_uploading_or_sending(self):
        self.db.mark("imap:INBOX:1")
        client = mail_client(FakeImap({"1": resume_mail()}))
        with patch.object(p, "STATE_DB_PATH", self.path), \
                patch.object(p, "ImapMailClient", return_value=client), \
                patch.object(p, "FeishuClient") as feishu_factory, \
                patch.object(p, "SmtpReplyClient") as smtp_factory:
            p._run_stateful(SimpleNamespace(adopt_legacy_state=True))
        backups = list(Path(self.temp.name).glob("resume.db-legacy-*.db"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute("SELECT message_id FROM seen_messages").fetchall(),
                             [("imap:INBOX:1",)])
        feishu_factory.assert_not_called()
        smtp_factory.assert_not_called()
        self.assertTrue(self.db.seen("imap:INBOX:1"))

    def test_cli_refuses_second_process_using_same_database(self):
        with p.instance_lock(self.path):
            with patch.object(p, "STATE_DB_PATH", self.path), \
                    patch("sys.argv", ["resume_pipeline.py", "--once"]), \
                    patch.object(p, "_run_stateful") as run:
                with self.assertRaisesRegex(RuntimeError, "已有实例"):
                    p.main()
        run.assert_not_called()


class FeishuRecoveryContractTest(unittest.TestCase):
    def client(self):
        client = p.FeishuClient.__new__(p.FeishuClient)
        client.app_token, client.table_id = "app", "table"
        client.api = Mock(return_value={"record": {"record_id": "rec1"}})
        return client

    def test_creation_uses_client_token_query_parameter(self):
        client = self.client()
        token = str(uuid.uuid4())
        self.assertEqual(client.submit_record_fields({"姓名": "测试"}, token), "rec1")
        self.assertEqual(client.api.call_args.kwargs["params"], {"client_token": token})

    def test_success_without_record_id_is_not_marked_complete(self):
        client = self.client()
        client.api.return_value = {"record": {}}
        with self.assertRaisesRegex(RuntimeError, "record_id"):
            client.submit_record_fields({}, str(uuid.uuid4()))

    def test_reconciliation_requires_unique_attachment_match(self):
        client = self.client()
        client.list_records = Mock(return_value=[
            {"record_id": "rec1", "fields": {"原件": [{"file_token": "token1"}]}},
        ])
        self.assertEqual(client.find_record_by_attachment("token1", "原件"), "rec1")
        self.assertIsNone(client.find_record_by_attachment("token2", "原件"))
        client.list_records.return_value.append(
            {"record_id": "rec2", "fields": {"原件": [{"file_token": "token1"}]}}
        )
        with self.assertRaisesRegex(RuntimeError, "多条"):
            client.find_record_by_attachment("token1", "原件")


if __name__ == "__main__":
    unittest.main()
