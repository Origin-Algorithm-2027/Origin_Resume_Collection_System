#!/usr/bin/env python3
"""Standalone IMAP -> SMTP reply test service.

This module deliberately does not import or call ``resume_pipeline``.  It
replies to every new, non-self email with the fixed acknowledgement below;
resume filtering, parsing, AI analysis, and Feishu writes are out of scope.
"""

from __future__ import annotations

import argparse
import imaplib
import json
import logging
import os
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.header import decode_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv


REPLY_BODY = """已收到您的简历，感谢报名！

请根据您的意向组别加入对应的培训群，后续培训安排、考核通知等信息将在 QQ 招新群内发布，请及时关注群消息。

机械组：781323068
嵌入式组（硬件 / 电控）：1121923815
算法组（导航 / 视觉 / 软件）：2159078941
运宣组：1108278071
期待在后续培训中见到你！"""
LOG = logging.getLogger("resume-reply-test")


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def decode_header_value(value: Any) -> str:
    """Decode an RFC 2047 header into readable text."""
    if value is None:
        return ""
    result: List[str] = []
    for part, charset in decode_header(str(value)):
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result).strip()


def sender_address(message: Message) -> str:
    value = message.get("Reply-To") or message.get("From")
    return parseaddr(decode_header_value(value))[1].strip()


def reply_subject(subject: str) -> str:
    subject = decode_header_value(subject)
    if not subject:
        return "简历邮件回执"
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def build_reply(original: Message, recipient: str, account: str) -> EmailMessage:
    """Build the fixed reply without inspecting attachments or body content."""
    outgoing = EmailMessage(policy=policy.SMTP)
    outgoing["From"] = account
    outgoing["To"] = recipient
    outgoing["Subject"] = reply_subject(original.get("Subject"))
    outgoing["Date"] = format_datetime(datetime.now(timezone.utc))
    outgoing["Message-ID"] = make_msgid()

    original_id = decode_header_value(original.get("Message-ID"))
    if original_id:
        outgoing["In-Reply-To"] = original_id
        outgoing["References"] = original_id

    outgoing.set_content(REPLY_BODY)
    return outgoing


@dataclass(frozen=True)
class Settings:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    username: str
    password: str
    folder: str
    poll_interval: int
    page_size: int
    state_path: Path
    process_existing: bool

    @classmethod
    def from_env(cls) -> "Settings":
        username = os.getenv("EMAIL_USERNAME", "").strip()
        password = os.getenv("EMAIL_PASSWORD", "").strip()
        if not username or not password:
            raise RuntimeError(
                "请在回信配置中填写 EMAIL_USERNAME 和 EMAIL_PASSWORD（163 客户端授权码）"
            )

        return cls(
            imap_host=os.getenv("EMAIL_IMAP_HOST", "imap.163.com").strip(),
            imap_port=int(os.getenv("EMAIL_IMAP_PORT", "993")),
            smtp_host=os.getenv("EMAIL_SMTP_HOST", "smtp.163.com").strip(),
            smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "465")),
            username=username,
            password=password,
            folder=os.getenv("EMAIL_FOLDER", "INBOX").strip() or "INBOX",
            poll_interval=max(1, int(os.getenv("REPLY_POLL_INTERVAL_SECONDS", "30"))),
            page_size=max(1, int(os.getenv("EMAIL_PAGE_SIZE", "50"))),
            state_path=Path(
                os.getenv("REPLY_STATE_PATH", "test/reply_state.json")
            ),
            process_existing=_as_bool(
                os.getenv("REPLY_PROCESS_EXISTING_ON_FIRST_RUN", "false")
            ),
        )


class ReplyState:
    """Small JSON state store independent from the production SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.data: Dict[str, Any] = json.loads(self.path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {"initialized": False, "seen": []}
        self.data.setdefault("initialized", False)
        self.data.setdefault("seen", [])

    @property
    def initialized(self) -> bool:
        return bool(self.data.get("initialized"))

    def mark_initialized(self) -> None:
        self.data["initialized"] = True
        self.save()

    def seen(self, message_id: str) -> bool:
        return message_id in self.data["seen"]

    def mark_seen(self, message_id: str) -> None:
        if not self.seen(message_id):
            self.data["seen"].append(message_id)
            self.save()

    def save(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class ReplyService:
    def __init__(
        self,
        settings: Settings,
        imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
        smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        self.settings = settings
        self.imap_factory = imap_factory
        self.smtp_factory = smtp_factory
        self.imap: Optional[Any] = None

    def connect(self) -> None:
        try:
            self.close()
            self.imap = self.imap_factory(
                self.settings.imap_host,
                self.settings.imap_port,
                timeout=60,
            )
            try:
                status, data = self.imap.login(
                    self.settings.username,
                    self.settings.password,
                )
            except imaplib.IMAP4.error as exc:
                raise RuntimeError(
                    "IMAP 登录失败，请确认 EMAIL_USERNAME 正确，"
                    "EMAIL_PASSWORD 使用的是 163 客户端授权码"
                ) from exc
            if status != "OK":
                raise RuntimeError(f"IMAP 登录失败：{data!r}")

            # 163 accepts this ID command after LOGIN. Other IMAP servers may not
            # implement it, so an unsupported response is only logged.
            try:
                imaplib.Commands["ID"] = ("AUTH",)
                status, data = self.imap._simple_command(
                    "ID",
                    '("name" "ResumeReplyTest" "version" "1.0.0" "vendor" "Python")',
                )
                if status != "OK":
                    LOG.debug("IMAP ID command was not accepted: %s %r", status, data)
            except (AttributeError, imaplib.IMAP4.error):
                LOG.debug("IMAP server does not support ID", exc_info=True)

            status, data = self.imap.select(self.settings.folder, readonly=True)
            if status != "OK":
                raise RuntimeError(
                    f"无法打开邮箱文件夹 {self.settings.folder!r}：{data!r}"
                )
        except Exception:
            # Do not retain a NONAUTH connection. Otherwise the next polling
            # cycle would issue SEARCH on the failed connection.
            self.close()
            raise

    def close(self) -> None:
        if self.imap is None:
            return
        try:
            self.imap.close()
        except Exception:
            LOG.debug("关闭 IMAP 文件夹失败", exc_info=True)
        try:
            self.imap.logout()
        except Exception:
            LOG.debug("退出 IMAP 失败", exc_info=True)
        self.imap = None

    def fetch_messages(self) -> List[Dict[str, Any]]:
        state = getattr(self.imap, "state", None) if self.imap is not None else None
        if self.imap is None or (isinstance(state, str) and state != "SELECTED"):
            self.close()
            self.connect()
        assert self.imap is not None
        status, data = self.imap.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError(f"搜索邮件失败：{data!r}")

        raw_uids = data[0].split() if data and data[0] else []
        raw_uids = raw_uids[-self.settings.page_size :]
        messages: List[Dict[str, Any]] = []
        for raw_uid in raw_uids:
            uid = raw_uid.decode("ascii", errors="replace")
            status, fetched = self.imap.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError(f"读取邮件失败：UID={uid}，响应={fetched!r}")
            raw = next(
                (bytes(item[1]) for item in fetched if isinstance(item, tuple)),
                None,
            )
            if raw is None:
                raise RuntimeError(f"读取邮件内容为空：UID={uid}")
            messages.append(
                {
                    "id": f"imap:{self.settings.folder}:{uid}",
                    "uid": uid,
                    "message": BytesParser(policy=policy.default).parsebytes(raw),
                }
            )
        return messages

    def send_reply(self, original: Message, recipient: str) -> None:
        outgoing = build_reply(original, recipient, self.settings.username)
        smtp = self.smtp_factory(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=60,
        )
        try:
            smtp.login(self.settings.username, self.settings.password)
            smtp.send_message(outgoing)
        finally:
            try:
                smtp.quit()
            except Exception:
                LOG.debug("关闭 SMTP 连接失败", exc_info=True)

    def process_once(self, state: ReplyState, process_existing: Optional[bool] = None) -> Dict[str, int]:
        messages = self.fetch_messages()
        process_existing = (
            self.settings.process_existing
            if process_existing is None
            else process_existing
        )
        if not state.initialized:
            if not process_existing:
                for item in messages:
                    state.mark_seen(item["id"])
                state.mark_initialized()
                LOG.info("首次启动跳过当前 %d 封历史邮件", len(messages))
                return {"checked": len(messages), "replied": 0, "skipped": len(messages)}
            state.mark_initialized()

        replied = 0
        skipped = 0
        # IMAP search returns oldest -> newest; retain that order.
        for item in messages:
            message_id = item["id"]
            if state.seen(message_id):
                continue
            original = item["message"]
            recipient = sender_address(original)
            if not recipient or recipient.casefold() == self.settings.username.casefold():
                LOG.info("跳过无效或本账号发件人：%s", message_id)
                state.mark_seen(message_id)
                skipped += 1
                continue
            try:
                self.send_reply(original, recipient)
            except Exception:
                LOG.exception("回信失败，保留 UID 以便下次重试：%s", message_id)
                continue
            state.mark_seen(message_id)
            replied += 1
            LOG.info("已回复 %s (%s)", recipient, decode_header_value(original.get("Subject")))
        return {"checked": len(messages), "replied": replied, "skipped": skipped}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只回信、不筛选、不写入飞书的测试服务")
    parser.add_argument("--env-file", type=Path, help="回信配置文件，默认读取当前目录 .env")
    parser.add_argument("--once", action="store_true", help="只检查一次邮件后退出")
    parser.add_argument(
        "--process-existing",
        action="store_true",
        help="首次运行也处理已有邮件（默认只处理服务启动后收到的新邮件）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    else:
        load_dotenv()

    try:
        settings = Settings.from_env()
        service = ReplyService(settings)
        state = ReplyState(settings.state_path)
        try:
            if args.once:
                LOG.info("本次回信结果：%s", service.process_once(state, args.process_existing or None))
                return 0
            LOG.info("启动回信测试服务，每 %d 秒检查一次", settings.poll_interval)
            while True:
                try:
                    service.process_once(state, args.process_existing or None)
                except Exception:
                    LOG.exception("本轮检查失败，稍后重试")
                time.sleep(settings.poll_interval)
        except KeyboardInterrupt:
            LOG.info("收到退出信号")
            return 0
        finally:
            service.close()
    except Exception as exc:
        LOG.error("回信服务启动失败：%s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    sys.exit(main())
