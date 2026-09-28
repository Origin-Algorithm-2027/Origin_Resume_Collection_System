import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import resume_pipeline as pipeline
import pipeline_common as common


class PipelineReplyTest(unittest.TestCase):
    def test_imap_messages_can_skip_seen_uids_before_fetch(self):
        class FakeImapConnection:
            def __init__(self):
                self.fetches = []

            def response(self, name):
                return name, [b"123"]

            def uid(self, command, *args):
                if command == "search":
                    return "OK", [b"1 2"]
                self.fetches.append(args[0])
                raw = (
                    b"Subject: New resume\r\n"
                    b"From: candidate@example.com\r\n"
                    b"Date: Wed, 17 Sep 2026 12:00:00 +0000\r\n"
                    b"\r\n"
                    b"body"
                )
                return "OK", [(b"BODY[]", raw)]

        client = pipeline.ImapMailClient.__new__(pipeline.ImapMailClient)
        client.folder = "INBOX"
        client.host = "imap.example.com"
        client.port = 993
        client.username = "test@example.com"
        client.conn = FakeImapConnection()
        client._raw_by_id = {}

        messages = list(client.messages(
            should_fetch=lambda message_id: message_id.endswith(":2"),
        ))

        self.assertEqual(client.conn.fetches, ["2"])
        self.assertEqual([message["id"] for message in messages], [client.scope() + ":2"])

    def test_no_resume_attachment_message_is_red(self):
        self.assertEqual(
            pipeline.red_text("跳过无简历附件"),
            "\033[31m跳过无简历附件\033[0m",
        )

    def test_whitelist_backfill_updates_only_whitelist_without_reply_client(self):
        class FakeFeishu:
            def __init__(self):
                self.updated = []

            def ensure_text_field(self, field_name):
                self.field_name = field_name
                return True

            def list_records(self):
                return [
                    {
                        "record_id": "rec_old",
                        "fields": {
                            pipeline.FIELD_SUBJECT: "历史报名",
                            pipeline.FIELD_ATTACHMENT: [
                                {"file_token": "file_old", "name": "old.pdf"}
                            ],
                        },
                    }
                ]

            def download_bitable_attachment(self, file_token):
                self.downloaded = file_token
                return b"resume"

            def update_record_fields(self, record_id, fields):
                self.updated.append((record_id, fields))

        feishu = FakeFeishu()
        with patch.object(pipeline, "resume_to_text", return_value="resume text"):
            with patch.object(
                pipeline,
                "analyze_resume",
                return_value={"whitelist_items": ["上海生源", "线下看过比赛"]},
            ):
                pipeline.backfill_whitelist(feishu)

        self.assertEqual(feishu.field_name, pipeline.FIELD_WHITELIST)
        self.assertEqual(
            feishu.updated,
            [("rec_old", {pipeline.FIELD_WHITELIST: "上海生源、线下看过比赛"})],
        )

    def test_whitelist_items_are_taken_from_model_output(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"whitelist_items": ["上海生源", "线下看过比赛"]},
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        with patch.dict(
            os.environ,
            {
                "SILICONFLOW_API_KEY": "test-key",
                "SILICONFLOW_MODEL": "test-model",
            },
            clear=False,
        ):
            with patch.object(pipeline, "WHITELIST_RULES", "自定义白名单提示词"):
                with patch("resume_pipeline.requests.post", return_value=response) as post:
                    result = pipeline.analyze_resume("简历中明确写了相关经历", "报名")

        self.assertEqual(
            result["whitelist_items"],
            ["上海生源", "线下看过比赛"],
        )
        prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("自定义白名单提示词", prompt)

    def test_whitelist_normalization_does_not_apply_keyword_rules(self):
        self.assertEqual(
            pipeline.build_whitelist_items(
                {"birthplace": "上海", "hobbies": "骑车"},
            ),
            [],
        )
        self.assertEqual(
            pipeline.build_whitelist_items(
                {"whitelist_items": ["模型自定义标签"]},
            ),
            ["模型自定义标签"],
        )

    def test_upload_local_file_creates_record_then_replies(self):
        class FakeFeishu:
            def __init__(self):
                self.uploaded = []
                self.created = []

            def upload_resume(self, filename, data):
                self.uploaded.append((filename, data))
                return "file_token"

            def create_record(self, **kwargs):
                self.created.append(kwargs)
                return "rec_local"

        class FakeReply:
            username = "origin@example.com"

            def __init__(self):
                self.calls = []

            def send_reply(self, recipient, subject, original_message_id=""):
                self.calls.append((recipient, subject, original_message_id))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.pdf"
            path.write_bytes(b"fake pdf")
            db = pipeline.StateDB(str(Path(directory) / "state.db"))
            feishu = FakeFeishu()
            reply = FakeReply()
            with patch.object(pipeline, "resume_to_text", return_value="resume text"):
                with patch.object(
                    pipeline,
                    "analyze_resume",
                    return_value={
                        "name": "候选人",
                        "qq": "",
                        "intention_group": [],
                        "skills": [],
                        "college": "",
                        "projects": "",
                        "summary": "",
                    },
                ):
                    record_id = pipeline.upload_local_file(
                        path,
                        feishu,
                        db,
                        reply,
                        sender="candidate@example.com",
                        subject="本地报名",
                    )

        self.assertEqual(record_id, "rec_local")
        self.assertEqual(feishu.uploaded, [("candidate.pdf", b"fake pdf")])
        self.assertEqual(reply.calls, [("candidate@example.com", "本地报名", "")])

    def test_smtp_reply_uses_fixed_body(self):
        with patch.dict(
            os.environ,
            {"EMAIL_USERNAME": "origin@example.com", "EMAIL_PASSWORD": "secret"},
            clear=False,
        ):
            smtp = Mock()
            smtp.send_message.return_value = {}
            with patch.object(common.smtplib, "SMTP_SSL", return_value=smtp):
                client = pipeline.SmtpReplyClient()
                client.send_reply(
                    "candidate@example.com",
                    "我的简历",
                    "<incoming@example.com>",
                )

        sent = smtp.send_message.call_args.args[0]
        self.assertEqual(sent["To"], "candidate@example.com")
        self.assertEqual(sent["Subject"], "Re: 我的简历")
        self.assertEqual(
            sent.get_content().replace("\r\n", "\n").strip(),
            pipeline.REPLY_BODY,
        )
        self.assertEqual(sent["In-Reply-To"], "<incoming@example.com>")

    def test_historical_backfill_is_only_queued_once(self):
        class FakeFeishu:
            def list_records(self):
                return [
                    {
                        "record_id": "rec_1",
                        "fields": {
                            pipeline.FIELD_SENDER: "candidate@example.com",
                            pipeline.FIELD_SUBJECT: "报名简历",
                        },
                    }
                ]

        class FakeReply:
            username = "origin@example.com"

            def __init__(self):
                self.calls = []

            def send_reply(self, recipient, subject, original_message_id=""):
                self.calls.append((recipient, subject, original_message_id))

        with tempfile.TemporaryDirectory() as directory:
            db = pipeline.StateDB(str(Path(directory) / "state.db"))
            reply = FakeReply()
            pipeline.backfill_replies(FakeFeishu(), reply, db)
            pipeline.backfill_replies(FakeFeishu(), reply, db)

        self.assertEqual(reply.calls, [("candidate@example.com", "报名简历", "")])


if __name__ == "__main__":
    unittest.main()
