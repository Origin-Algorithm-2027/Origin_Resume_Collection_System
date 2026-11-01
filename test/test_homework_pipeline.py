import copy
import tempfile
import unittest
import ssl
import os
import smtplib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests

import homework_pipeline as h
import pipeline_common as common


class FakeMail:
    def __init__(self, count=1):
        self.count = count
        self.fetched = []
        self.headers = []
        self.sender = "student@example.com"
        self.received = datetime(2026, 9, 21, tzinfo=h.BEIJING)
        self.validity = "mailbox:1"
        self.titles = []
        self.title_reads = []
        self.mail_subject = ""

    def scope(self):
        return self.validity

    def uids(self, start):
        return [str(i) for i in range(1, self.count + 1)]

    def header(self, uid):
        self.headers.append(uid)
        return self.sender, self.received

    def attachment_parts(self, uid):
        self.fetched.append(uid)
        return [(1, "代码.zip", b"archive"), (2, "answer.anything", b"answer")]

    def matching_titles(self, uid):
        self.title_reads.append(uid)
        return self.titles

    def subject(self, uid):
        return self.mail_subject


class FakeFeishu:
    def __init__(self):
        self.records = {
            "r1": {h.FIELD_SENDER: "Student <STUDENT@example.com>", "第1周": [{"file_token": "old"}], "姓名": "保留"},
            "r2": {h.FIELD_SENDER: [{"text": "student@"}, {"text": "example.com"}]},
        }
        self.uploads = []
        self.updates = []
        self.fields = []

    def list_records(self):
        return [{"record_id": key, "fields": copy.deepcopy(fields)} for key, fields in self.records.items()]

    def ensure_week(self, week):
        self.fields.append(week)
        return f"第{week}周"

    def upload_attachment(self, name, content):
        self.uploads.append((name, content))
        return f"token{len(self.uploads)}"

    def record_fields(self, record_id):
        return copy.deepcopy(self.records[record_id])

    def update_record_fields(self, record_id, fields):
        self.records[record_id].update(copy.deepcopy(fields))
        self.updates.append((record_id, fields))


class HomeworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "homework.db")
        self.db = h.HomeworkState(self.path)
        self.mail = FakeMail()
        self.feishu = FakeFeishu()
        self.start = date(2026, 9, 21)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_cycle(self, existing=True):
        return h.run_once(self.mail, self.feishu, self.db, self.start, existing)

    def test_week_boundaries(self):
        for value, expected in [
            ("2026-09-20T15:59:59+00:00", 0),
            ("2026-09-20T16:00:00+00:00", 1),
            ("2026-09-27T15:59:59+00:00", 1),
            ("2026-09-27T16:00:00+00:00", 1),
            ("2026-09-28T15:59:59+00:00", 1),
            ("2026-09-28T16:00:00+00:00", 2),
            ("2026-10-04T16:00:00+00:00", 2),
            ("2026-10-05T16:00:00+00:00", 3),
            ("2026-11-01T16:00:00+00:00", 6),
            ("2026-11-02T16:00:00+00:00", 7),
            ("2027-01-01T00:00:00+08:00", 15),
        ]:
            with self.subTest(value=value):
                self.assertEqual(h.week_number(datetime.fromisoformat(value), self.start), expected)
        with self.assertRaises(ValueError):
            h.week_number(datetime(2026, 9, 21), self.start)

    def test_subject_week_overrides_conflicting_filenames_and_received_time(self):
        self.mail.mail_subject = "张三-第2周补交"
        self.mail.attachment_parts = Mock(return_value=[
            (1, "第1周.zip", b"one"), (2, "代码.py", b"two"),
        ])
        with self.assertLogs(h.log, level="INFO") as logs:
            self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.records["r1"]["第1周"], [{"file_token": "old"}])
        self.assertEqual(len(self.feishu.records["r1"]["第2周"]), 2)
        self.assertEqual(self.feishu.fields, [2])
        self.assertIn("依据=邮件主题", "".join(logs.output))
        self.db.close()
        self.db = h.HomeworkState(self.path)
        self.mail.mail_subject = "第3周"
        self.assertEqual(self.run_cycle()["done"], 0)
        self.assertEqual(len(self.feishu.uploads), 2)
        self.assertNotIn("第3周", self.feishu.records["r1"])

    def test_each_filename_week_and_unlabelled_attachment_use_separate_columns(self):
        self.mail.mail_subject = "补交作业"
        self.mail.attachment_parts = Mock(return_value=[
            (1, "第二周.zip", b"two"), (2, "Week 3.py", b"three"), (3, "说明.txt", b"time"),
            (4, "第2周补充.csv", b"two-more"),
        ])
        with self.assertLogs(h.log, level="INFO") as logs:
            self.assertEqual(self.run_cycle()["done"], 1)
        fields = self.feishu.records["r1"]
        self.assertEqual(len(fields["第2周"]), 2)
        self.assertEqual(len(fields["第3周"]), 1)
        self.assertEqual(len(fields["第1周"]), 2)  # 既有附件 + 未注明周次的说明。
        self.assertEqual(self.feishu.fields, [2, 3, 1])
        self.assertEqual(len(self.feishu.updates), 6)  # 每个目标行、每周只更新一次。
        self.assertIn("依据=附件文件名", "".join(logs.output))
        self.assertIn("依据=收件时间", "".join(logs.output))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 6)

    def test_ambiguous_subject_falls_back_to_each_filename_then_time(self):
        self.mail.mail_subject = "第1周、第2周补交"
        self.mail.attachment_parts = Mock(return_value=[
            (1, "第三周.zip", b"three"), (2, "第2周_第3周.zip", b"ambiguous"),
        ])
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.fields, [3, 1])

    def test_pdf_filename_week_is_used_without_affecting_other_attachments(self):
        self.mail.attachment_parts = Mock(return_value=[
            (1, "第9周.PDF", b"pdf"), (2, "代码.zip", b"code"),
        ])
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.fields, [9, 1])
        self.assertEqual(self.feishu.uploads, [("第9周.PDF", b"pdf"), ("代码.zip", b"code")])

    def test_week_label_does_not_override_start_date_filter(self):
        self.mail.received = datetime(2026, 9, 20, tzinfo=h.BEIJING)
        self.mail.mail_subject = "第2周"
        self.assertEqual(self.run_cycle()["skipped"], 1)
        self.assertEqual(self.feishu.uploads, [])
        self.assertEqual(self.feishu.fields, [])

    def test_tuesday_time_fallback_with_ambiguous_labels(self):
        self.mail.received = datetime(2026, 9, 29, tzinfo=h.BEIJING)
        self.mail.mail_subject = "Week 1 and 3"
        self.mail.attachment_parts = Mock(return_value=[(1, "第1周第3周.zip", b"code")])
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.fields, [2])

    def test_first_assignment_filename_overrides_second_week_received_time(self):
        self.mail.received = datetime(2026, 9, 30, 18, 24, tzinfo=h.BEIJING)
        self.mail.attachment_parts = Mock(return_value=[
            (4, "机械-测试同学-第一次作业.zip", b"assignment"),
        ])
        with self.assertLogs(h.log, level="INFO") as logs:
            self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.fields, [1])
        self.assertNotIn("第2周", self.feishu.records["r1"])
        self.assertIn("依据=附件文件名", "".join(logs.output))

    def test_assignment_number_in_subject_still_wins_over_filename(self):
        self.mail.mail_subject = "第二次作业"
        self.mail.attachment_parts = Mock(return_value=[
            (1, "第一次作业.zip", b"assignment"),
        ])
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.fields, [2])

    def test_cross_week_partial_failure_forwards_once_without_ingestion_retry(self):
        self.mail.attachment_parts = Mock(return_value=[
            (1, "第2周.zip", b"two"), (2, "第3周.zip", b"three"),
        ])
        original_update = self.feishu.update_record_fields
        def fail_third_week(record_id, fields):
            if "第3周" in fields:
                raise RuntimeError("third week unavailable")
            original_update(record_id, fields)
        raw = EmailMessage()
        raw["Subject"] = "补交"
        raw.set_content("original")
        self.mail.raw_message = Mock(return_value=raw.as_bytes())
        forwarder = h.UnmatchedForwardClient.__new__(h.UnmatchedForwardClient)
        forwarder.username = "sender@example.com"
        forwarder.recipient = "reviewer@example.com"
        forwarder.send = Mock()
        with patch.object(self.feishu, "update_record_fields", side_effect=fail_third_week):
            result = h.run_once(self.mail, self.feishu, self.db, self.start, forwarder=forwarder)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["forwarded"], 1)
        self.assertEqual(len(self.feishu.records["r1"]["第2周"]), 1)
        self.assertNotIn("第3周", self.feishu.records["r1"])
        h.run_once(self.mail, self.feishu, self.db, self.start, forwarder=forwarder)
        forwarder.send.assert_called_once()
        self.assertEqual(len(self.feishu.uploads), 2)

    def test_backfill_all_messages_and_append_to_all_matching_rows(self):
        self.mail.count = 53
        result = self.run_cycle()
        self.assertEqual(result["done"], 53)
        self.assertEqual(len(self.mail.fetched), 53)
        self.assertEqual(len(self.feishu.records["r1"]["第1周"]), 107)
        self.assertEqual(len(self.feishu.records["r2"]["第1周"]), 106)
        self.assertEqual(self.feishu.records["r1"]["姓名"], "保留")
        self.assertEqual(self.feishu.records["r1"]["第1周"][0], {"file_token": "old"})
        self.db.close()
        self.db = h.HomeworkState(self.path)
        self.run_cycle()
        self.assertEqual(len(self.mail.headers), 53)
        self.assertEqual(len(self.feishu.uploads), 106)

    def test_new_tuesday_boundary_preserves_previously_ingested_records(self):
        self.mail.received = datetime(2026, 9, 28, 12, tzinfo=h.BEIJING)
        # 模拟旧规则已将周一邮件入库到第2周。
        with patch.object(h, "week_number", return_value=2):
            self.run_cycle()
        original = copy.deepcopy(self.feishu.records)
        self.db.close()
        self.db = h.HomeworkState(self.path)
        self.run_cycle()
        self.assertEqual(self.feishu.records, original)
        self.assertEqual(self.mail.fetched, ["1"])
        self.mail.count = 2
        self.run_cycle()
        self.assertEqual(len(self.feishu.records["r1"]["第1周"]), 3)
        self.assertEqual(self.feishu.records["r1"]["第2周"], original["r1"]["第2周"])
        self.mail.count = 3
        self.mail.received = datetime(2026, 9, 29, tzinfo=h.BEIJING)
        self.run_cycle()
        self.assertEqual(len(self.feishu.records["r1"]["第2周"]), 4)

    def test_unmatched_never_retries_ingestion_after_new_record(self):
        self.mail.sender = "new@example.com"
        self.assertEqual(self.run_cycle()["unmatched"], 1)
        self.assertEqual(self.mail.fetched, [])
        self.feishu.records["new"] = {h.FIELD_SENDER: "new@example.com"}
        self.assertEqual(self.run_cycle()["done"], 0)
        self.assertEqual(self.mail.headers, ["1"])

    def test_name_fallback_matches_subject_or_attachment_title(self):
        self.mail.sender = "new@example.com"
        self.feishu.records["r1"][h.FIELD_NAME] = "张三"
        self.mail.titles = ["第1周作业-张三", "代码"]
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual([rid for rid, _ in self.feishu.updates], ["r1"])
        self.mail.count = 2
        self.mail.titles = ["第二次作业", "算法组_张三_26010001"]
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.mail.title_reads, ["1", "2"])
        self.assertNotIn("第1周", self.feishu.records["r2"])

    def test_email_match_wins_without_reading_name_titles(self):
        self.mail.titles = ["李四-作业"]
        self.feishu.records["other"] = {h.FIELD_NAME: "李四"}
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.mail.title_reads, [])
        self.assertEqual({rid for rid, _ in self.feishu.updates}, {"r1", "r2"})

    def test_duplicate_names_require_forwarding_not_broadcast(self):
        self.mail.sender = "unknown@example.com"
        for fields in self.feishu.records.values():
            fields[h.FIELD_NAME] = "张三"
        self.mail.titles = ["张三-作业"]
        with self.assertLogs(h.log, level="WARNING") as logs:
            self.assertEqual(self.run_cycle()["unmatched"], 1)
        self.assertIn("重名", "".join(logs.output))
        self.assertEqual(self.feishu.uploads, [])
        self.assertEqual(self.db.conn.execute("SELECT status FROM messages").fetchone()[0], "forward_only")

    def test_conflicting_subject_and_filename_are_not_ingested(self):
        self.mail.sender = "unknown@example.com"
        self.feishu.records["r1"][h.FIELD_NAME] = "张三"
        self.feishu.records["r2"][h.FIELD_NAME] = "李四"
        self.mail.titles = ["张三-作业", "李四_代码"]
        self.assertEqual(self.run_cycle()["unmatched"], 1)
        self.assertEqual(self.feishu.uploads, [])
        # 已转入人工处理后，后续修改姓名也不重新入库。
        self.mail.titles = ["张三-作业"]
        self.assertEqual(self.run_cycle()["done"], 0)
        self.assertEqual(self.mail.title_reads, ["1"])

    def test_saved_target_precedes_changed_name_and_email(self):
        self.mail.sender = "unknown@example.com"
        self.db.targets("mailbox:1:1", ["r1"])
        self.mail.titles = ["李四-作业"]
        self.feishu.records["r2"][h.FIELD_NAME] = "李四"
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.mail.title_reads, [])
        self.assertEqual([rid for rid, _ in self.feishu.updates], ["r1"])

    def test_name_match_accepts_pdf(self):
        self.mail.sender = "unknown@example.com"
        self.mail.titles = ["张三-报名", "张三_报名表"]
        self.feishu.records["r1"][h.FIELD_NAME] = "张三"
        with patch.object(self.mail, "attachment_parts", return_value=[(1, "张三.pdf", b"pdf")]):
            self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.uploads, [("张三.pdf", b"pdf")])

    def test_skip_history_setting_and_uidvalidity_changes(self):
        self.assertEqual(self.run_cycle(False)["skipped"], 1)
        self.assertEqual(self.mail.headers, [])
        self.mail.count = 2
        self.assertEqual(self.run_cycle(False)["done"], 1)
        self.mail.validity = "mailbox:2"
        self.assertEqual(self.run_cycle()["done"], 2)

    def test_old_mail_skipped_and_seventh_week_created(self):
        self.mail.received = datetime(2026, 9, 20, tzinfo=h.BEIJING)
        self.assertEqual(self.run_cycle()["skipped"], 1)
        self.assertEqual(self.mail.fetched, [])
        self.mail.count = 2
        self.mail.received = datetime(2026, 11, 3, tzinfo=h.BEIJING)
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertIn("第7周", self.feishu.records["r1"])

    def test_write_response_lost_does_not_retry_after_restart(self):
        original = self.feishu.update_record_fields
        def response_lost(record_id, fields):
            original(record_id, fields)
            raise RuntimeError("response lost")
        with patch.object(self.feishu, "update_record_fields", side_effect=response_lost):
            self.assertEqual(self.run_cycle()["failed"], 1)
        self.db.close()
        self.db = h.HomeworkState(self.path)
        self.assertEqual(self.run_cycle()["done"], 0)
        self.assertEqual(len(self.feishu.uploads), 2)
        self.assertEqual(len(self.feishu.updates), 1)
        self.assertEqual(len(self.feishu.records["r1"]["第1周"]), 3)

    def test_partial_record_success_does_not_retry_remaining_targets(self):
        original = self.feishu.update_record_fields
        def fail_second(record_id, fields):
            if record_id == "r2":
                raise RuntimeError("temporarily unavailable")
            original(record_id, fields)
        with patch.object(self.feishu, "update_record_fields", side_effect=fail_second):
            self.assertEqual(self.run_cycle()["failed"], 1)
        self.feishu.records["r2"][h.FIELD_SENDER] = "changed@example.com"
        self.assertEqual(self.run_cycle()["done"], 0)
        self.assertEqual(len(self.feishu.updates), 1)
        self.assertNotIn("第1周", self.feishu.records["r2"])

    def test_partial_upload_failure_does_not_retry(self):
        original = self.feishu.upload_attachment
        def fail_second(name, content):
            if name.endswith("anything"):
                raise RuntimeError("upload denied")
            return original(name, content)
        with patch.object(self.feishu, "upload_attachment", side_effect=fail_second):
            self.assertEqual(self.run_cycle()["failed"], 1)
        self.assertEqual(self.feishu.updates, [])
        self.assertEqual(self.run_cycle()["done"], 0)
        self.assertEqual(len(self.feishu.uploads), 1)

    def test_empty_attachment_list_is_terminal(self):
        self.mail.attachment_parts = Mock(return_value=[])
        self.assertEqual(self.run_cycle()["skipped"], 1)
        self.run_cycle()
        self.mail.attachment_parts.assert_called_once()

    def test_accepts_all_attachment_extensions(self):
        filenames = ["报名.WORD", "报名.doc", "报名.DOCX", "报名.Pdf ", "作业.zip", "code.py", "无扩展名"]
        self.mail.attachment_parts = Mock(return_value=[
            (i, name, b"content") for i, name in enumerate(filenames)
        ])
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual([item[0] for item in self.feishu.uploads], filenames)
        self.assertEqual(len(self.feishu.records["r1"]["第1周"]), len(filenames) + 1)

    def test_pdf_only_mail_is_uploaded_once(self):
        self.mail.attachment_parts = Mock(return_value=[(1, "form.pdf", b"pdf")])
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.uploads, [("form.pdf", b"pdf")])
        self.assertEqual(len(self.feishu.updates), 2)
        self.run_cycle()
        self.mail.attachment_parts.assert_called_once()
        self.assertEqual(len(self.feishu.uploads), 1)

    def test_reimport_removes_owned_tokens_only_and_reprocesses(self):
        self.mail.attachment_parts = Mock(return_value=[(1, "form.pdf", b"pdf"), (2, "code.zip", b"zip")])
        self.run_cycle()
        self.feishu.records["r1"]["原始简历"] = [{"file_token": "token1"}]
        h.prepare_reimport(self.feishu, self.db, self.path)
        self.assertEqual(self.feishu.records["r1"]["第1周"], [{"file_token": "old"}])
        self.assertEqual(self.feishu.records["r1"]["原始简历"], [{"file_token": "token1"}])
        self.assertTrue(list(Path(self.tmp.name).glob("*.json")))
        self.assertEqual(self.run_cycle()["done"], 1)
        self.assertEqual(self.feishu.records["r1"]["第1周"], [{"file_token": "old"}, {"file_token": "token1"}, {"file_token": "token2"}])
        self.assertEqual(len(self.feishu.uploads), 2)

    def test_interrupted_reimport_cleanup_can_resume_without_losing_state(self):
        self.run_cycle()
        original = self.feishu.update_record_fields
        def interrupted(record_id, fields):
            original(record_id, fields)
            raise RuntimeError("lost response")
        with patch.object(self.feishu, "update_record_fields", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                h.prepare_reimport(self.feishu, self.db, self.path)
        self.assertEqual(self.db.reimport_phase(), "cleanup")
        self.assertTrue(self.db.finished("mailbox:1:1"))
        h.prepare_reimport(self.feishu, self.db, self.path)
        self.assertEqual(self.db.reimport_phase(), "processing")
        self.assertEqual(self.feishu.records["r2"]["第1周"], [])
        self.run_cycle()
        previous_updates = len(self.feishu.updates)
        h.prepare_reimport(self.feishu, self.db, self.path)
        self.assertEqual(len(self.feishu.updates), previous_updates)

    def test_reimport_cleans_successful_write_without_delivery_marker(self):
        self.db.save_token("message", 1, "digest", "owned")
        self.feishu.records["r1"]["第1周"].append({"file_token": "owned"})
        h.prepare_reimport(self.feishu, self.db, self.path)
        self.assertEqual(self.feishu.records["r1"]["第1周"], [{"file_token": "old"}])

    def test_warning_and_error_formatter_uses_red(self):
        import logging
        formatter = h.HomeworkLogFormatter("%(message)s")
        for level in (logging.WARNING, logging.ERROR):
            record = logging.LogRecord("homework", level, "", 0, "未入库", (), None)
            self.assertEqual(formatter.format(record), "\033[31m未入库\033[0m")

    def test_failed_record_listing_does_not_mark_messages(self):
        with patch.object(self.feishu, "list_records", side_effect=RuntimeError("permission")):
            with self.assertRaises(RuntimeError):
                self.run_cycle()
        self.assertEqual(self.mail.fetched, [])
        self.assertEqual(self.run_cycle()["done"], 1)

    def test_configuration_binding_and_instance_lock(self):
        self.db.bind({"start": "2026-09-21"})
        self.db.bind({"start": "2026-09-21"})
        with self.assertRaises(RuntimeError):
            self.db.bind({"start": "2027-09-21"})
        with h.instance_lock(self.path):
            with self.assertRaises(RuntimeError):
                with h.instance_lock(self.path):
                    pass
        with h.instance_lock(self.path):
            pass


class WeekTitleTest(unittest.TestCase):
    def test_explicit_forms_and_repeated_equivalent_week(self):
        for title, expected in [
            ("第1周", 1), ("第一周作业", 1), ("第十二周", 12), ("第九十九周", 99),
            ("第 ２ 周", 2), ("Week 2", 2), ("张三_WEEK3", 3), ("Ｗｅｅｋ　４", 4),
            ("第2周_第二周_week02", 2), ("第7周", 7),
            ("第一次作业", 1), ("第1次作业", 1), ("第 十二 次 作业", 12),
            ("第２次作业", 2), ("第一周第一次作业", 1),
        ]:
            with self.subTest(title=title):
                self.assertEqual(h.explicit_week(title), expected)

    def test_ambiguous_or_non_week_numbers_are_not_used(self):
        for title in ["第1周、第2周", "Week 1 / Week 2", "Week 1-2", "Week 1.5",
                      "Week 1 and 2", "第1-2周", "第一、二周", "第0周", "week -1",
                      "26013762", "2026-09-29", "第二次提交", "第1.5周", "weekend2", "",
                      "第1周第二次作业", "第一次作业第二次作业", "第0次作业"]:
            with self.subTest(title=title):
                self.assertIsNone(h.explicit_week(title))

    def test_filename_folder_is_not_week_evidence(self):
        self.assertEqual(h.attachment_week("", r"第2周\代码.zip", 3), (3, "收件时间"))
        self.assertEqual(h.attachment_week("第1周", "第二周.zip", 3), (1, "邮件主题"))


class NameMatchingTest(unittest.TestCase):
    def test_full_name_boundaries_and_common_chinese_titles(self):
        for title in ("算法组_张三_26010001", "张三第1周作业", "姓名张三", "张三的作业"):
            with self.subTest(title=title):
                self.assertEqual(h.match_name_titles([title], {"张三": ["r1"]})[0], ["r1"])
        for title in ("李张三-作业", "张三丰-作业", "代码", "Assignment"):
            with self.subTest(title=title):
                self.assertEqual(h.match_name_titles([title], {"张三": ["r1"], "ann": ["r2"]})[0], [])

    def test_rich_text_configured_column_and_unicode_names(self):
        with patch.object(h, "FIELD_NAME", "报名姓名"):
            names = h.name_index([
                {"record_id": "r1", "fields": {"报名姓名": [{"text": "张"}, {"text": "三"}]}},
                {"record_id": "r2", "fields": {"报名姓名": {"text": "John  Smith"}}},
                {"record_id": "r3", "fields": {"报名姓名": ""}},
            ])
        self.assertEqual(names, {"张三": ["r1"], "john smith": ["r2"]})
        self.assertEqual(h.match_name_titles(["ＪＯＨＮ　ＳＭＩＴＨ_week1"], names)[0], ["r2"])

    def test_multiple_mentions_of_same_person_are_allowed(self):
        self.assertEqual(h.match_name_titles(["张三-作业", "张三_代码"], {"张三": ["r1"]})[0], ["r1"])

    def test_longer_name_does_not_match_shorter_name(self):
        self.assertEqual(h.match_name_titles(["张三丰-作业"], {"张三": ["r1"], "张三丰": ["r2"]})[0], ["r2"])

    def test_mime_titles_and_upload_share_one_fetch(self):
        msg = EmailMessage()
        msg["Subject"] = "算法组-张三-第1周"
        msg.set_content("李四：正文不能用于姓名匹配")
        msg.add_attachment(b"zip", maintype="application", subtype="zip", filename="张三_代码.zip")
        msg.add_attachment(b"img", maintype="image", subtype="png", filename="李四.png", disposition="inline")
        nested = EmailMessage()
        nested["Subject"] = "王五-作业"
        nested.set_content("body")
        nested.add_attachment(b"zip", maintype="application", subtype="zip", filename="王五.zip")
        msg.add_attachment(nested, filename="参考.eml")
        client = h.HomeworkMailClient.__new__(h.HomeworkMailClient)
        client.conn = Mock()
        client.conn.uid.return_value = ("OK", [(b"BODY[]", msg.as_bytes())])
        self.assertEqual(client.matching_titles("1"), ["算法组-张三-第1周", "张三_代码", "参考"])
        self.assertEqual(client.subject("1"), "算法组-张三-第1周")
        self.assertEqual(len(list(client.attachment_parts("1"))), 2)
        client.conn.uid.assert_called_once()
        client.matching_titles("2")
        self.assertEqual(client.conn.uid.call_count, 2)
        client.conn = Mock()
        client.conn.uid.return_value = ("OK", [(b"BODY[]", msg.as_bytes())])
        client.matching_titles("2")
        client.conn.uid.assert_called_once()


class FeishuApiTest(unittest.TestCase):
    def client(self):
        client = h.HomeworkFeishuClient.__new__(h.HomeworkFeishuClient)
        client.app_token = "app"
        client.table_id = "table"
        client.week_fields = None
        client.api = Mock()
        return client

    def test_field_pagination_create_and_idempotence(self):
        client = self.client()
        client.api.side_effect = [
            {"items": [{"field_name": "第1周", "type": 17}], "has_more": True, "page_token": "p2"},
            {"items": [{"field_name": "第2周", "type": 17}]},
            {"field": {"field_name": "第3周", "type": 17}},
        ]
        self.assertEqual(client.ensure_week(2), "第2周")
        self.assertEqual(client.ensure_week(3), "第3周")
        self.assertEqual(client.ensure_week(3), "第3周")
        self.assertEqual(client.api.call_count, 3)
        self.assertEqual(client.api.call_args.kwargs["json"]["type"], 17)

    def test_invalid_field_type_never_overwritten(self):
        client = self.client()
        client.week_fields = {"第1周": {"type": 1}}
        with self.assertRaises(RuntimeError):
            client.ensure_week(1)
        client.api.assert_not_called()

    def test_failed_listing_cannot_trigger_create(self):
        client = self.client()
        client.api.side_effect = RuntimeError("permission denied")
        with self.assertRaises(RuntimeError):
            client.ensure_week(1)
        self.assertEqual(client.api.call_count, 1)
        self.assertEqual(client.api.call_args.args[0], "GET")

    def test_field_creation_response_lost_is_reconciled(self):
        client = self.client()
        client.week_fields = {}
        client.api.side_effect = [RuntimeError("lost"), {"items": [{"field_name": "第1周", "type": 17}]}]
        self.assertEqual(client.ensure_week(1), "第1周")

    def test_record_pagination(self):
        client = self.client()
        client.api.side_effect = [
            {"items": [{"record_id": "a"}], "has_more": True, "page_token": "p2"},
            {"items": [{"record_id": "b"}]},
        ]
        self.assertEqual([r["record_id"] for r in client.list_records()], ["a", "b"])
        self.assertEqual(client.api.call_args.kwargs["params"]["page_token"], "p2")

    def test_large_upload_splits_content_and_finishes(self):
        client = self.client()
        client.api.side_effect = [{"upload_id": "up", "block_size": 3, "block_num": 3}, {}, {}, {}, {"file_token": "large"}]
        with patch.object(h, "SINGLE_UPLOAD_LIMIT", 4):
            self.assertEqual(client.upload_attachment("test.zip", b"12345678"), "large")
        calls = client.api.call_args_list
        self.assertEqual([c.kwargs["files"]["file"][1] for c in calls[1:4]], [b"123", b"456", b"78"])
        self.assertEqual(calls[4].kwargs["json"], {"upload_id": "up", "block_num": 3})
        with self.assertRaises(ValueError):
            client.upload_attachment("empty", b"")

    def test_transient_part_failure_retries_same_block_then_continues(self):
        errors = [
            requests.HTTPError("temporary", response=Mock(status_code=status))
            for status in (408, 429, 500, 502, 503, 504)
        ] + [requests.Timeout("timeout"), requests.ConnectionError("disconnected")]
        for error in errors:
            with self.subTest(error=error, response=getattr(error, "response", None)):
                client = self.client()
                client.api.side_effect = [
                    {"upload_id": "up", "block_size": 3, "block_num": 3},
                    {}, error, {}, {}, {"file_token": "large"},
                ]
                with patch.object(h, "SINGLE_UPLOAD_LIMIT", 4), patch.object(h.time, "sleep") as sleep:
                    self.assertEqual(client.upload_attachment("test.zip", b"12345678"), "large")
                calls = client.api.call_args_list
                self.assertEqual(calls[2], calls[3])
                self.assertEqual([c.kwargs["data"]["seq"] for c in calls[1:5]], ["0", "1", "1", "2"])
                self.assertEqual(calls[-1].args[1], "drive/v1/medias/upload_finish")
                sleep.assert_called_once_with(2)

    def test_part_retry_exhaustion_preserves_error_and_does_not_finish(self):
        client = self.client()
        error = requests.HTTPError("Bad Gateway", response=requests.Response())
        error.response.status_code = 502
        client.api.side_effect = [
            {"upload_id": "up", "block_size": 3, "block_num": 3},
            error, error, error, error,
        ]
        with patch.object(h, "SINGLE_UPLOAD_LIMIT", 4), patch.object(h.time, "sleep") as sleep:
            with self.assertRaises(requests.HTTPError) as caught:
                client.upload_attachment("test.zip", b"12345678")
        self.assertIs(caught.exception, error)
        self.assertEqual(client.api.call_count, 5)
        self.assertTrue(all(c == client.api.call_args_list[1] for c in client.api.call_args_list[1:]))
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(8)])

    def test_permanent_part_errors_are_not_retried(self):
        errors = [
            requests.HTTPError("rejected", response=Mock(status_code=status))
            for status in (400, 401, 403, 404, 413)
        ] + [requests.HTTPError("no response"), requests.exceptions.SSLError("certificate"),
             RuntimeError("飞书接口失败：1061004 forbidden")]
        for error in errors:
            with self.subTest(error=error, response=getattr(error, "response", None)):
                client = self.client()
                client.api.side_effect = [
                    {"upload_id": "up", "block_size": 3, "block_num": 3}, error,
                ]
                with patch.object(h, "SINGLE_UPLOAD_LIMIT", 4), patch.object(h.time, "sleep") as sleep:
                    with self.assertRaises(type(error)) as caught:
                        client.upload_attachment("test.zip", b"12345678")
                self.assertIs(caught.exception, error)
                self.assertEqual(client.api.call_count, 2)
                sleep.assert_not_called()

    def test_api_permission_error_is_reported(self):
        client = self.client()
        client.auth_headers = Mock(return_value={})
        response = Mock()
        response.json.return_value = {"code": 99991672, "msg": "Access denied"}
        with patch.object(common.requests, "request", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "99991672"):
                h.HomeworkFeishuClient.api(client, "GET", "test")


class MailTest(unittest.TestCase):
    def test_uidvalidity_response_is_cached_only_for_current_connection(self):
        client = self.client()
        client.host, client.port = "imap.example.com", 993
        client.username, client.folder = "student@example.com", "INBOX"
        client.conn.response.side_effect = [("UIDVALIDITY", [b"123"]), ("UIDVALIDITY", [None])]
        first = client.scope()
        self.assertEqual(client.scope(), first)
        client.conn.response.assert_called_once()
        client.conn = Mock()
        client.conn.response.return_value = ("UIDVALIDITY", [b"456"])
        self.assertNotEqual(client.scope(), first)

    def test_connect_retries_transient_tls_failure(self):
        client = self.client()
        with patch.object(h.ImapConnection, "_connect", side_effect=[ssl.SSLEOFError("eof"), None]) as connect:
            with patch.object(h.time, "sleep") as sleep:
                client._connect()
        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_persistent_tls_failure_has_actionable_error(self):
        client = self.client()
        client.host, client.port = "imap.163.com", 993
        with patch.object(h.ImapConnection, "_connect", side_effect=ssl.SSLEOFError("eof")) as connect:
            with patch.object(h.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "连续3次失败"):
                    client._connect()
        self.assertEqual(connect.call_count, 3)

    def test_authentication_errors_are_not_retried(self):
        client = self.client()
        with patch.object(h.ImapConnection, "_connect", side_effect=RuntimeError("登录失败")) as connect:
            with patch.object(h.time, "sleep") as sleep:
                with self.assertRaises(RuntimeError):
                    client._connect()
        connect.assert_called_once()
        sleep.assert_not_called()

    def test_certificate_errors_are_not_retried(self):
        client = self.client()
        with patch.object(h.ImapConnection, "_connect", side_effect=ssl.SSLCertVerificationError("certificate")):
            with patch.object(h.time, "sleep") as sleep:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    client._connect()
        sleep.assert_not_called()

    def test_check_imap_does_not_initialize_clients_or_state(self):
        with patch("sys.argv", ["homework_pipeline.py", "--check-imap"]):
            with patch.object(h, "check_imap_transport") as check:
                with patch.object(h, "HomeworkFeishuClient") as feishu:
                    with patch.object(h, "HomeworkState") as state:
                        self.assertEqual(h.main(), 0)
        check.assert_called_once()
        feishu.assert_not_called()
        state.assert_not_called()

    def client(self):
        client = h.HomeworkMailClient.__new__(h.HomeworkMailClient)
        client.conn = Mock()
        return client

    def test_internaldate_overrides_date_and_reply_to_wins(self):
        client = self.client()
        client.conn.uid.return_value = ("OK", [(b'1 (INTERNALDATE "27-Sep-2026 16:00:00 +0000" BODY[HEADER.FIELDS (FROM REPLY-TO)] {120}',
            b'From: Other <other@example.com>\r\nReply-To: Student <student@example.com>\r\nDate: Mon, 21 Sep 2026 00:00:00 +0800\r\n\r\n')])
        sender, received = client.header("1")
        self.assertEqual(sender, "student@example.com")
        self.assertEqual(h.week_number(received, date(2026, 9, 21)), 1)
        client.conn.uid.return_value = ("OK", [(b"no internaldate", b"From: a@example.com\r\n")])
        with self.assertRaises(RuntimeError):
            client.header("1")

    def test_search_does_not_truncate_and_uses_date_margin(self):
        client = self.client()
        client.conn.uid.return_value = ("OK", [b" ".join(str(i).encode() for i in range(60, 0, -1))])
        self.assertEqual(client.uids(date(2026, 9, 21)), [str(i) for i in range(1, 61)])
        self.assertEqual(client.conn.uid.call_args.args[-1], "20-Sep-2026")

    def test_arbitrary_types_unnamed_and_inline_exclusion(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(b"zip", maintype="application", subtype="zip", filename="作业.zip")
        msg.add_attachment(b"anonymous", maintype="application", subtype="octet-stream")
        msg.add_attachment(b"inline", maintype="image", subtype="png", filename="logo.png", disposition="inline")
        client = self.client()
        client.conn.uid.return_value = ("OK", [(b"BODY[]", msg.as_bytes())])
        parts = list(client.attachment_parts("1"))
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0][1:], ("作业.zip", b"zip"))
        self.assertTrue(parts[1][1].startswith("attachment-"))
        self.assertEqual(parts[1][2], b"anonymous")

    def test_attached_email_does_not_duplicate_nested_attachments(self):
        nested = EmailMessage()
        nested.set_content("nested body")
        nested.add_attachment(b"nested", maintype="application", subtype="zip", filename="nested.zip")
        outer = EmailMessage()
        outer.set_content("body")
        outer.add_attachment(nested, filename="submission.eml")
        client = self.client()
        client.conn.uid.return_value = ("OK", [(b"BODY[]", outer.as_bytes())])
        parts = list(client.attachment_parts("1"))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][1], "submission.eml")
        self.assertIn(b"nested.zip", parts[0][2])


class UnmatchedForwardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "homework.db")
        self.db = h.HomeworkState(self.path)
        self.mail = FakeMail()
        self.feishu = FakeFeishu()
        original = EmailMessage()
        original["From"] = "Student <unknown@example.com>"
        original["Subject"] = "未匹配作业"
        original.set_content("原正文")
        original.add_attachment(b"pdf preserved", maintype="application", subtype="pdf", filename="original.pdf")
        self.raw = original.as_bytes(policy=policy.SMTP)
        self.mail.raw_message = Mock(return_value=self.raw)
        with patch.dict(os.environ, {
            "EMAIL_USERNAME": "collector@163.com", "EMAIL_PASSWORD": "test-password",
            "EMAIL_SMTP_HOST": "smtp.163.com", "EMAIL_SMTP_PORT": "465",
            "HOMEWORK_UNMATCHED_FORWARD_TO": "3464144391@qq.com",
        }):
            self.forwarder = h.UnmatchedForwardClient()
        self.forwarder.send = Mock()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_full_original_email_bytes_preserved_and_only_requested_recipient(self):
        message = self.forwarder.build_message(self.raw, "1")
        self.assertEqual(message["To"], "3464144391@qq.com")
        self.assertEqual(message["From"], "collector@163.com")
        attachment = list(message.iter_attachments())[0]
        self.assertEqual(attachment.get_filename(), "original.eml")
        self.assertEqual(attachment.get_payload(decode=True), self.raw)
        self.assertIn("未匹配作业", str(message["Subject"]))
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])

    def test_eight_recorded_messages_forward_once_across_restart(self):
        for i in range(1, 9):
            self.db.status(f"mailbox:1:{i}", "unmatched")
        self.assertEqual(h.forward_recorded_unmatched(self.mail, self.db, self.forwarder), 0)
        self.assertEqual(self.forwarder.send.call_count, 8)
        self.db.close()
        self.db = h.HomeworkState(self.path)
        self.assertEqual(h.forward_recorded_unmatched(self.mail, self.db, self.forwarder), 0)
        self.assertEqual(self.forwarder.send.call_count, 8)
        self.assertEqual(self.mail.raw_message.call_count, 8)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM forward_queue WHERE payload IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM messages WHERE status='forwarded'").fetchone()[0], 8)

    def test_forwarded_mail_never_retries_matching_or_ingestion(self):
        self.mail.sender = "unknown@example.com"
        first = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.assertEqual(first["forwarded"], 1)
        self.assertEqual(first["unmatched"], 1)
        second = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.assertEqual(second["forwarded"], 0)
        self.feishu.records["new"] = {h.FIELD_SENDER: "unknown@example.com"}
        third = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.assertEqual(third["done"], 0)
        self.assertEqual(self.mail.headers, ["1"])
        self.assertEqual(self.feishu.uploads, [])
        self.forwarder.send.assert_called_once()

    def test_smtp_failure_retries_same_payload_without_refetching(self):
        self.db.status("mailbox:1:1", "unmatched")
        self.forwarder.send.side_effect = smtplib.SMTPException("temporary failure")
        self.assertEqual(h.forward_recorded_unmatched(self.mail, self.db, self.forwarder), 1)
        first_payload = self.forwarder.send.call_args.args[0]
        self.db.close()
        self.db = h.HomeworkState(self.path)
        self.mail.raw_message.side_effect = RuntimeError("original removed")
        self.forwarder.send.side_effect = None
        self.assertEqual(h.forward_recorded_unmatched(self.mail, self.db, self.forwarder), 0)
        self.assertEqual(self.forwarder.send.call_args.args[0], first_payload)
        self.mail.raw_message.assert_called_once()
        parsed = BytesParser(policy=policy.default).parsebytes(first_payload)
        row = self.db.conn.execute("SELECT message_id, sent_at FROM forward_queue").fetchone()
        self.assertEqual(row[0], str(parsed["Message-ID"]))
        self.assertIsNotNone(row[1])

    def test_smtp_envelope_only_contains_target_and_quit_failure_is_not_retried(self):
        smtp = Mock()
        smtp.sendmail.return_value = {}
        smtp.quit.side_effect = OSError("quit failed")
        with patch.object(common.smtplib, "SMTP_SSL", return_value=smtp):
            h.UnmatchedForwardClient.send(self.forwarder, b"payload")
        smtp.login.assert_called_once_with("collector@163.com", "test-password")
        smtp.sendmail.assert_called_once_with("collector@163.com", ["3464144391@qq.com"], b"payload")

    def test_recorded_other_mailbox_is_not_fetched_or_sent(self):
        self.db.status("different-mailbox:1", "unmatched")
        self.assertEqual(h.forward_recorded_unmatched(self.mail, self.db, self.forwarder), 1)
        self.mail.raw_message.assert_not_called()
        self.forwarder.send.assert_not_called()

    def test_matched_pdf_mail_is_ingested_not_forwarded(self):
        self.mail.attachment_parts = Mock(return_value=[(1, "form.pdf", b"pdf")])
        result = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.assertEqual(result["done"], 1)
        self.forwarder.send.assert_not_called()

    def test_reimport_preserves_sent_forward_receipts(self):
        self.db.status("mailbox:1:1", "unmatched")
        h.forward_recorded_unmatched(self.mail, self.db, self.forwarder)
        h.prepare_reimport(self.feishu, self.db, self.path)
        self.assertTrue(self.db.forward_exists("mailbox:1:1", self.forwarder.recipient))
        self.assertTrue(self.db.finished("mailbox:1:1"))

    def test_ingestion_failure_forwards_once_and_never_retries_upload(self):
        with patch.object(self.feishu, "upload_attachment", side_effect=RuntimeError("平台拒绝附件")) as upload:
            result = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["forwarded"], 1)
            payload = self.forwarder.send.call_args.args[0]
            forwarded = BytesParser(policy=policy.default).parsebytes(payload)
            self.assertIn("平台拒绝附件", forwarded.get_body(preferencelist=("plain",)).get_content())
            self.db.close()
            self.db = h.HomeworkState(self.path)
            h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        upload.assert_called_once()
        self.forwarder.send.assert_called_once()

    def run_part_retry_cycle(self, recover):
        client = h.HomeworkFeishuClient.__new__(h.HomeworkFeishuClient)
        client.app_token = "test-app"
        error = requests.HTTPError("Bad Gateway", response=Mock(status_code=502))
        responses = [{"upload_id": "up", "block_size": 3, "block_num": 3}, error]
        responses += [{}, {}, {}, {"file_token": "large"}] if recover else [error] * 3
        client.api = Mock(side_effect=responses)
        self.mail.attachment_parts = Mock(return_value=[(1, "test.zip", b"12345678")])
        with patch.object(self.feishu, "upload_attachment", side_effect=client.upload_attachment):
            with patch.object(h, "SINGLE_UPLOAD_LIMIT", 4), patch.object(h.time, "sleep"):
                if recover:
                    result = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
                    self.assertEqual(result["done"], 1)
                    self.forwarder.send.assert_not_called()
                else:
                    with self.assertRaises(requests.HTTPError):
                        h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
                    self.assertEqual(client.api.call_count, 5)
                    self.forwarder.send.assert_called_once()
        status = self.db.conn.execute("SELECT status FROM messages WHERE key='mailbox:1:1'").fetchone()[0]
        self.assertEqual(status, "done" if recover else "forwarded")

    def test_part_retry_success_is_ingested_without_forwarding(self):
        self.run_part_retry_cycle(recover=True)

    def test_part_retry_exhaustion_forwards_failed_mail(self):
        self.run_part_retry_cycle(recover=False)

    def test_forward_failure_retries_only_smtp_even_if_matching_row_added(self):
        self.mail.sender = "unknown@example.com"
        self.forwarder.send.side_effect = smtplib.SMTPException("offline")
        h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.feishu.records["new"] = {h.FIELD_SENDER: self.mail.sender}
        self.forwarder.send.side_effect = None
        result = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.assertEqual(result["forwarded"], 1)
        self.assertEqual(result["done"], 0)
        self.assertEqual(self.mail.headers, ["1"])
        self.mail.raw_message.assert_called_once()
        self.assertEqual(self.feishu.uploads, [])

    def test_raw_fetch_failure_retries_forwarding_without_matching_again(self):
        self.mail.sender = "unknown@example.com"
        self.mail.raw_message.side_effect = OSError("disconnected")
        result = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.assertEqual(result["forward_failed"], 1)
        self.feishu.records["new"] = {h.FIELD_SENDER: self.mail.sender}
        self.mail.raw_message.side_effect = None
        result = h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.assertEqual(result["forwarded"], 1)
        self.assertEqual(self.mail.headers, ["1"])
        self.assertEqual(self.feishu.uploads, [])

    def test_old_failed_and_sent_unmatched_states_migrate(self):
        self.db.status("mailbox:1:1", "unmatched")
        self.db.status("mailbox:1:2", "failed", "旧错误")
        self.db.queue_forward("mailbox:1:1", self.forwarder.recipient, "id", b"payload")
        with self.db.conn:
            self.db.conn.execute("UPDATE forward_queue SET sent_at='2026-09-28', payload=NULL")
        self.db.close()
        self.db = h.HomeworkState(self.path)
        states = dict(self.db.conn.execute("SELECT key, status FROM messages"))
        self.assertEqual(states["mailbox:1:1"], "forwarded")
        self.assertEqual(states["mailbox:1:2"], "forward_only")
        h.forward_recorded_unmatched(self.mail, self.db, self.forwarder)
        self.mail.raw_message.assert_called_once_with("2")
        self.forwarder.send.assert_called_once()

    def test_pending_forward_sent_even_when_feishu_read_fails(self):
        self.db.status("mailbox:1:1", "forward_only")
        h.queue_unmatched_forward(self.mail, self.db, self.forwarder, "mailbox:1:1", "1")
        with patch.object(self.feishu, "list_records", side_effect=RuntimeError("Feishu offline")):
            with self.assertRaises(RuntimeError):
                h.run_once(self.mail, self.feishu, self.db, date(2026, 9, 21), forwarder=self.forwarder)
        self.forwarder.send.assert_called_once()

    def test_no_self_forwarding(self):
        with patch.dict(os.environ, {
            "EMAIL_USERNAME": "collector@163.com", "EMAIL_PASSWORD": "test-password",
            "HOMEWORK_UNMATCHED_FORWARD_TO": "collector@163.com",
        }):
            with self.assertRaisesRegex(ValueError, "循环转发"):
                h.UnmatchedForwardClient()


if __name__ == "__main__":
    unittest.main()
