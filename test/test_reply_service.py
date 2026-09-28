import imaplib
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock

try:
    from .reply_service import (
        REPLY_BODY,
        ReplyService,
        ReplyState,
        Settings,
        build_reply,
        reply_subject,
        sender_address,
    )
except ImportError:  # Allows: python test/test_reply_service.py
    from reply_service import (
        REPLY_BODY,
        ReplyService,
        ReplyState,
        Settings,
        build_reply,
        reply_subject,
        sender_address,
    )


def incoming(subject="我的简历", sender="candidate@example.com"):
    message = EmailMessage(policy=policy.default)
    message["From"] = sender
    message["To"] = "origin@example.com"
    message["Subject"] = subject
    message["Message-ID"] = "<incoming@example.com>"
    message.set_content("简历正文不会被回信服务读取")
    return message


class ReplyHelpersTest(unittest.TestCase):
    def test_fixed_reply_body_and_thread_headers(self):
        result = build_reply(incoming(), "candidate@example.com", "origin@example.com")
        self.assertEqual(
            result.get_content().replace("\r\n", "\n").strip(),
            REPLY_BODY,
        )
        self.assertEqual(result["To"], "candidate@example.com")
        self.assertEqual(result["Subject"], "Re: 我的简历")
        self.assertEqual(result["In-Reply-To"], "<incoming@example.com>")

    def test_sender_prefers_reply_to(self):
        message = incoming(sender="from@example.com")
        message["Reply-To"] = "reply@example.com"
        self.assertEqual(sender_address(message), "reply@example.com")

    def test_subject_does_not_duplicate_re_prefix(self):
        self.assertEqual(reply_subject("Re: 简历"), "Re: 简历")
        self.assertEqual(reply_subject(""), "简历邮件回执")


class ReplyServiceTest(unittest.TestCase):
    def test_login_failure_clears_non_authenticated_connection(self):
        settings = Settings(
            "imap.example.com", 993, "smtp.example.com", 465,
            "origin@example.com", "wrong", "INBOX", 30, 50,
            Path("reply-state.json"), False,
        )
        imap = Mock()
        imap.login.side_effect = imaplib.IMAP4.error(b"LOGIN error")
        service = ReplyService(settings, lambda *a, **k: imap)

        with self.assertRaisesRegex(RuntimeError, "客户端授权码"):
            service.connect()

        self.assertIsNone(service.imap)

    def test_successful_reply_is_sent_and_can_be_marked_once(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ReplyState(Path(directory) / "state.json")
            settings = Settings(
                "imap.example.com", 993, "smtp.example.com", 465,
                "origin@example.com", "secret", "INBOX", 30, 50,
                Path(directory) / "state.json", True,
            )
            smtp = Mock()
            imap = Mock()
            imap.uid.side_effect = [
                ("OK", [b"1"]),
                ("OK", [(b"header", incoming().as_bytes())]),
            ]
            imap.select.return_value = ("OK", [b"1"])
            imap.login.return_value = ("OK", [b"logged in"])
            imap._simple_command.return_value = ("OK", [b"done"])
            service = ReplyService(settings, lambda *a, **k: imap, lambda *a, **k: smtp)

            result = service.process_once(state)

            self.assertEqual(result["replied"], 1)
            smtp.send_message.assert_called_once()
            self.assertEqual(
                smtp.send_message.call_args.args[0]
                .get_content()
                .replace("\r\n", "\n")
                .strip(),
                REPLY_BODY,
            )
            self.assertTrue(state.seen("imap:INBOX:1"))


if __name__ == "__main__":
    unittest.main()
