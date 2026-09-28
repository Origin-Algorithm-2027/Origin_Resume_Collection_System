"""公共通信和业务隔离回归检查；不连接真实邮箱或飞书。"""
import os
import subprocess
import sys
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

import pipeline_common as common
import resume_pipeline as resume


class BusinessIsolationTest(unittest.TestCase):
    def assert_isolated_import(self, module, blocked, environment):
        script = f"""
import importlib.abc
import sys
class BlockImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {blocked!r}:
            raise AssertionError('Unexpected dependency: ' + fullname)
sys.meta_path.insert(0, BlockImports())
import {module}
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, **environment},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_homework_needs_no_resume_parser_or_resume_configuration(self):
        self.assert_isolated_import(
            "homework_pipeline", ["resume_pipeline", "pymupdf"],
            {"MIN_EXTRACTED_CHARS": "invalid", "POLL_INTERVAL_SECONDS": "invalid"},
        )

    def test_resume_needs_no_homework_configuration(self):
        self.assert_isolated_import(
            "resume_pipeline", ["homework_pipeline"],
            {"HOMEWORK_POLL_INTERVAL_SECONDS": "invalid"},
        )

    def test_common_needs_no_business_or_dotenv_or_database(self):
        self.assert_isolated_import(
            "pipeline_common",
            ["resume_pipeline", "homework_pipeline", "dotenv", "pymupdf", "sqlite3"],
            {"EMAIL_IMAP_PORT": "invalid", "FEISHU_APP_ID": ""},
        )


class SharedTransportTest(unittest.TestCase):
    def client(self):
        with patch.dict(os.environ, {
            "FEISHU_APP_ID": "test-app", "FEISHU_APP_SECRET": "test-secret",
            "FEISHU_BITABLE_APP_TOKEN": "test-token", "FEISHU_TABLE_ID": "test-table",
        }):
            return resume.FeishuClient()

    def test_resume_finds_field_after_first_page(self):
        client = self.client()
        target = {"field_name": "目标字段", "type": 1}
        client.api = Mock(side_effect=[
            {"items": [{"field_name": f"字段{i}"} for i in range(100)],
             "has_more": True, "page_token": "page2"},
            {"items": [target], "has_more": False},
        ])
        self.assertTrue(client.ensure_text_field("目标字段"))
        self.assertEqual(client.field_definition("目标字段"), target)
        self.assertEqual(client.api.call_count, 2)
        self.assertEqual(client.api.call_args.kwargs["params"]["page_token"], "page2")

    def test_repeated_page_token_fails_instead_of_looping(self):
        client = self.client()
        client.api = Mock(return_value={"items": [], "has_more": True, "page_token": "same"})
        with self.assertRaisesRegex(RuntimeError, "page_token"):
            client.list_fields()
        self.assertEqual(client.api.call_count, 2)

    def test_token_is_cached_until_refresh_window(self):
        client = self.client()
        response = Mock()
        response.json.return_value = {"code": 0, "tenant_access_token": "token1", "expire": 7200}
        with patch.object(common.requests, "post", return_value=response) as post:
            with patch.object(common.time, "time", side_effect=[1000, 1001, 8100]):
                self.assertEqual(client.token(), "token1")
                self.assertEqual(client.token(), "token1")
                response.json.return_value["tenant_access_token"] = "token2"
                self.assertEqual(client.token(), "token2")
        self.assertEqual(post.call_count, 2)

    def test_smtp_uses_explicit_envelope_and_ignores_quit_failure(self):
        message = EmailMessage()
        message["To"] = "unrelated@example.com"
        message["Cc"] = "another@example.com"
        smtp = Mock()
        smtp.send_message.return_value = {}
        smtp.quit.side_effect = OSError("connection closed after DATA")
        with patch.object(common.smtplib, "SMTP_SSL", return_value=smtp):
            common.send_smtp("smtp.example.com", 465, "sender@example.com", "test",
                             "recipient@example.com", message)
        smtp.send_message.assert_called_once_with(
            message, from_addr="sender@example.com", to_addrs=["recipient@example.com"],
        )

    def test_smtp_refusal_remains_a_failure(self):
        smtp = Mock()
        smtp.sendmail.return_value = {"recipient@example.com": (550, b"refused")}
        with patch.object(common.smtplib, "SMTP_SSL", return_value=smtp):
            with self.assertRaisesRegex(RuntimeError, "拒绝"):
                common.send_smtp("smtp.example.com", 465, "sender@example.com", "test",
                                 "recipient@example.com", b"raw message")
        smtp.quit.assert_called_once()


class HistoricalSourceTest(unittest.TestCase):
    def test_candidate_fallback_reads_configured_major_and_only_updates_target(self):
        feishu = Mock()
        feishu.ensure_text_field.return_value = True
        feishu.list_records.return_value = [{
            "record_id": "rec1", "fields": {"自定义专业": "自动化"},
        }]
        with patch.object(resume, "FIELD_MAJOR", "自定义专业"):
            with patch.object(resume, "analyze_resume", return_value={"birthplace": "上海"}) as analyze:
                with patch("builtins.print"):
                    resume.backfill_candidate_fields(feishu, ["birthplace"])
        analyze.assert_called_once_with("自动化", "")
        feishu.update_record_fields.assert_called_once_with(
            "rec1", {resume.BACKFILL_FIELD_MAP["birthplace"]: "上海"},
        )

    def test_failed_attachment_continues_to_next_before_using_fallback(self):
        feishu = Mock()
        feishu.download_bitable_attachment.side_effect = [OSError("unavailable"), b"good"]
        fields = {
            resume.FIELD_ATTACHMENT: [
                {"file_token": "skip", "name": "homework.zip"},
                {"file_token": "bad", "name": "bad.pdf"},
                {"file_token": "good", "name": "good.docx"},
            ],
            resume.FIELD_SUMMARY: "fallback",
        }
        with patch.object(resume, "resume_to_text", return_value="original text"):
            with self.assertLogs(resume.log, level="WARNING"):
                source = resume._historical_resume_source(
                    feishu, "rec1", fields, [resume.FIELD_SUMMARY],
                )
        self.assertEqual(source, "original text")
        self.assertEqual(feishu.download_bitable_attachment.call_count, 2)


if __name__ == "__main__":
    unittest.main()
