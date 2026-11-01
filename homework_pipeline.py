#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""优先按主题和附件名的周次、再按收件日期，将作业追加到飞书每周附件列。"""
from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import logging
import math
import os
import re
import sqlite3
import socket
import ssl
import time
import unicodedata
import zlib
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr, format_datetime, make_msgid
from pathlib import Path
from typing import Any, Iterator

import requests
from dotenv import load_dotenv
from pipeline_common import (
    FeishuTransport, ImapConnection, safe_filename, red_text, required_env, send_smtp, instance_lock,
)

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s | %(levelname)s | %(message)s")
EMAIL_PAGE_SIZE = int(os.getenv("EMAIL_PAGE_SIZE", "50"))
FIELD_SENDER = os.getenv("FIELD_SENDER", "邮件发件人")
FIELD_NAME = os.getenv("FIELD_NAME", "姓名")

log = logging.getLogger("homework")


class HomeworkLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return red_text(text) if record.levelno >= logging.WARNING else text


_handler = logging.StreamHandler()
_handler.setFormatter(HomeworkLogFormatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_handler)
log.propagate = False
BEIJING = timezone(timedelta(hours=8))
SINGLE_UPLOAD_LIMIT = 20 * 1024 * 1024
UPLOAD_PART_MAX_ATTEMPTS = 4
UPLOAD_PART_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
HOMEWORK_POLL_INTERVAL_SECONDS = int(os.getenv("HOMEWORK_POLL_INTERVAL_SECONDS", "900"))


def cell_text(value: Any) -> str:
    """飞书文本列可能由连续的富文本片段组成。"""
    if isinstance(value, list):
        value = "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in value
        )
    elif isinstance(value, dict):
        value = value.get("text", "")
    return str(value or "")


def normalized_email(value: Any) -> str:
    address = parseaddr(cell_text(value))[1].strip().casefold()
    return address if "@" in address else ""


def normalized_title(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", cell_text(value)).casefold().split())


def name_index(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for record in records:
        name = normalized_title((record.get("fields") or {}).get(FIELD_NAME))
        record_id = record.get("record_id")
        if len(name) >= 2 and record_id:
            targets = result.setdefault(name, [])
            if record_id not in targets:
                targets.append(record_id)
    return result


def title_has_name(title: str, name: str) -> bool:
    """识别完整姓名；不将短姓名当成长姓名或英文单词的一部分。"""
    for match in re.finditer(re.escape(name), title):
        before, after = title[:match.start()], title[match.end():]
        left_ok = not before or not before[-1].isalpha()
        right_ok = not after or not after[0].isalpha()
        # 兼容“姓名张三”“张三第1周作业”等常见连续中文标题。
        left_ok = left_ok or bool(re.search(r"(?:姓名|提交人|作者)[:：]?$", before))
        right_ok = right_ok or bool(re.match(
            r"(?:同学|的)?(?:第[0-9一二三四五六七八九十]+周|作业|周报|练习|提交)", after,
        ))
        if left_ok and right_ok:
            return True
    return False


def match_name_titles(titles: list[str], names: dict[str, list[str]]) -> tuple[list[str], str]:
    """只接受所有标题共同指向的唯一姓名、唯一记录；冲突交给人工处理。"""
    normalized = [normalized_title(title) for title in titles]
    matches = [name for name in names if any(title_has_name(title, name) for title in normalized)]
    if not matches:
        return [], "邮箱及标题姓名均未匹配"
    if len(matches) != 1:
        return [], "标题中匹配到多个姓名：" + "、".join(sorted(matches))
    name = matches[0]
    if len(names[name]) != 1:
        return [], f"姓名重名或对应多条记录：{name}"
    return list(names[name]), f"标题姓名匹配：{name}"


def week_number(received: datetime, start: date) -> int:
    if received.tzinfo is None:
        raise ValueError("收件时间必须包含时区")
    received_date = received.astimezone(BEIJING).date()
    if received_date < start:
        return 0
    # 起始日期所在自然周的周二为锚点；起始日若为周一，也归入第1周。
    # 保留原起始日期和状态库绑定，已完成邮件不重新计算或搬动。
    first_tuesday = start + timedelta(days=1 - start.weekday())
    return max(1, (received_date - first_tuesday).days // 7 + 1)


def explicit_week(title: str) -> int | None:
    """标题只明确指向一个正整数周次时返回结果，否则留给下一层依据。"""
    text = normalized_title(title)
    digits = "一二三四五六七八九"
    chinese = {char: value for value, char in enumerate(digits, 1)}
    chinese["十"] = 10
    for value in range(11, 100):
        tens, units = divmod(value, 10)
        label = (digits[tens - 1] if tens > 1 else "") + "十"
        if units:
            label += digits[units - 1]
        chinese[label] = value
    weeks = set()
    pattern = r"第\s*([0-9零〇一二三四五六七八九十百千两]+)\s*(?:周|次\s*作业)|(?<![a-z0-9])week\s*([0-9]+)(?![0-9a-z])"
    for match in re.finditer(pattern, text):
        number = match.group(1) or match.group(2)
        # Week 1-2 / Week 1.5 等不应截取为第1周。
        if match.group(2) and re.match(
            r"\s*(?:[./~～\-–—、&,]|to\b|and\b|至|到|和|及)\s*(?:week\s*)?[0-9]",
            text[match.end():],
        ):
            return None
        value = int(number) if number.isascii() and number.isdigit() else chinese.get(number)
        if value is None or value <= 0:
            return None
        weeks.add(value)
    return next(iter(weeks)) if len(weeks) == 1 else None


def attachment_week(subject: str, filename: str, received_week: int) -> tuple[int, str]:
    subject_week = explicit_week(subject)
    if subject_week is not None:
        return subject_week, "邮件主题"
    title = Path(re.split(r"[/\\]", filename)[-1]).stem
    filename_week = explicit_week(title)
    if filename_week is not None:
        return filename_week, "附件文件名"
    return received_week, "收件时间"


def record_index(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for record in records:
        address = normalized_email((record.get("fields") or {}).get(FIELD_SENDER))
        if address and record.get("record_id"):
            result.setdefault(address, []).append(record["record_id"])
    return result




class HomeworkState:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                key TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS uploads (
                message_key TEXT NOT NULL, part INTEGER NOT NULL, digest TEXT NOT NULL,
                token TEXT NOT NULL, PRIMARY KEY (message_key, part)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                message_key TEXT NOT NULL, record_id TEXT NOT NULL, field TEXT NOT NULL,
                PRIMARY KEY (message_key, record_id, field)
            );
            CREATE TABLE IF NOT EXISTS targets (
                message_key TEXT PRIMARY KEY, record_ids TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS forward_queue (
                message_key TEXT NOT NULL, recipient TEXT NOT NULL,
                message_id TEXT NOT NULL, payload BLOB,
                queued_at TEXT NOT NULL, sent_at TEXT, last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (message_key, recipient)
            );
        """)
        self.migrate_forward_only()

    def migrate_forward_only(self) -> None:
        """旧版未匹配/失败邮件改为只转发，已有发送回执直接视为终态。"""
        with self.conn:
            self.conn.execute(
                "UPDATE messages SET status='forward_only' WHERE status IN ('unmatched', 'failed')"
            )
            self.conn.execute(
                "UPDATE messages SET status='forwarded' WHERE status='forward_only' "
                "AND EXISTS (SELECT 1 FROM forward_queue f WHERE f.message_key=messages.key AND f.sent_at IS NOT NULL)"
            )

    def close(self) -> None:
        self.conn.close()

    def bind(self, settings: dict[str, Any]) -> None:
        value = json.dumps(settings, sort_keys=True)
        row = self.conn.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        if row and row[0] != value:
            raise RuntimeError("邮箱、飞书表格或起始日期已改变，请为新配置使用独立作业数据库")
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('binding', ?)", (value,))

    def initialized(self, scope: str) -> bool:
        return self.conn.execute("SELECT 1 FROM meta WHERE key=?", (scope,)).fetchone() is not None

    def initialize(self, scope: str, skipped: list[str]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO messages VALUES (?, 'skipped', '首次启动跳过')",
                [(key,) for key in skipped],
            )
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, '1')", (scope,))

    def finished(self, key: str) -> bool:
        row = self.conn.execute("SELECT status FROM messages WHERE key=?", (key,)).fetchone()
        return bool(row and row[0] in {"done", "skipped", "forward_only", "forwarded"})

    def status(self, key: str, status: str, detail: str = "") -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO messages VALUES (?, ?, ?)", (key, status, detail))

    def token(self, key: str, part: int, digest: str) -> str | None:
        row = self.conn.execute(
            "SELECT digest, token FROM uploads WHERE message_key=? AND part=?", (key, part)
        ).fetchone()
        if row and row[0] != digest:
            raise RuntimeError("同一邮件附件内容发生变化，停止以避免错误去重")
        return row[1] if row else None

    def save_token(self, key: str, part: int, digest: str, token: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO uploads VALUES (?, ?, ?, ?)", (key, part, digest, token))

    def targets(self, key: str, matches: list[str]) -> list[str]:
        # 一旦开始上传就固定目标集合，部分成功后的重试仍能找回全部目标。
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO targets VALUES (?, ?)", (key, json.dumps(matches)))
        return json.loads(self.conn.execute(
            "SELECT record_ids FROM targets WHERE message_key=?", (key,)
        ).fetchone()[0])

    def saved_targets(self, key: str) -> list[str] | None:
        row = self.conn.execute("SELECT record_ids FROM targets WHERE message_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def delivered(self, key: str, record_id: str, field: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM deliveries WHERE message_key=? AND record_id=? AND field=?",
            (key, record_id, field),
        ).fetchone() is not None

    def mark_delivery(self, key: str, record_id: str, field: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)", (key, record_id, field))

    def reimport_phase(self) -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key='reimport_phase'").fetchone()
        return row[0] if row else ""

    def set_reimport_phase(self, phase: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('reimport_phase', ?)", (phase,))

    def forward_exists(self, key: str, recipient: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM forward_queue WHERE message_key=? AND recipient=?", (key, recipient)
        ).fetchone() is not None

    def queue_forward(self, key: str, recipient: str, message_id: str, payload: bytes) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO forward_queue "
                "(message_key, recipient, message_id, payload, queued_at) VALUES (?, ?, ?, ?, ?)",
                (key, recipient, message_id, payload, datetime.now(timezone.utc).isoformat()),
            )


class UnmatchedForwardClient:
    """使用同一邮箱，将完整原邮件作为 .eml 附件转发给指定接收人。"""
    def __init__(self) -> None:
        self.host = os.getenv("EMAIL_SMTP_HOST", "smtp.163.com").strip()
        self.port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        self.username = required_env("EMAIL_USERNAME")
        self.password = required_env("EMAIL_PASSWORD")
        self.recipient = normalized_email(os.getenv("HOMEWORK_UNMATCHED_FORWARD_TO", "3464144391@qq.com"))
        if not self.recipient:
            raise ValueError("HOMEWORK_UNMATCHED_FORWARD_TO 必须是有效邮箱")
        if self.recipient == normalized_email(self.username):
            raise ValueError("未匹配邮件转发地址不能与收件邮箱相同，以免循环转发")

    def build_message(self, raw: bytes, uid: str, reason: str = "未匹配邮件发件人") -> EmailMessage:
        original = BytesParser(policy=policy.default).parsebytes(raw)
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = self.username
        message["To"] = self.recipient
        subject = str(original.get("Subject", "无主题")).replace("\r", " ").replace("\n", " ")
        message["Subject"] = f"[作业无法入库] Fwd: {subject}"
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid()
        message["X-Homework-Forward"] = "ingestion-failed"
        message.set_content(
            "此邮件无法入库，现转发供人工处理，系统不再尝试将其入库。\n"
            f"原因：{reason}\n"
            f"原发件人：{original.get('From', '')}\n"
            f"原 Reply-To：{original.get('Reply-To', '')}\n"
            f"原主题：{subject}\n"
            f"原邮件日期：{original.get('Date', '')}\n"
            f"邮箱 UID：{uid}\n\n"
            "完整原邮件见 original.eml 附件，其中保留原正文及全部附件。\n"
        )
        # 按字节附加，避免重新序列化原邮件导致签名或附件编码变化。
        message.add_attachment(raw, maintype="application", subtype="octet-stream", filename="original.eml")
        return message

    def send(self, payload: bytes) -> None:
        send_smtp(self.host, self.port, self.username, self.password, self.recipient, payload)


def queue_unmatched_forward(mail: HomeworkMailClient, db: HomeworkState,
                            forwarder: UnmatchedForwardClient, key: str, uid: str) -> None:
    if db.forward_exists(key, forwarder.recipient):
        return
    row = db.conn.execute("SELECT detail FROM messages WHERE key=?", (key,)).fetchone()
    message = forwarder.build_message(mail.raw_message(uid), uid, row[0] if row else "无法入库")
    db.queue_forward(key, forwarder.recipient, str(message["Message-ID"]), message.as_bytes(policy=policy.SMTP))
    log.info("无法入库邮件已加入转发队列：UID=%s，目标=%s", uid, forwarder.recipient)


def deliver_pending_forwards(db: HomeworkState, forwarder: UnmatchedForwardClient) -> tuple[int, int]:
    sent = failed = 0
    rows = db.conn.execute(
        "SELECT message_key FROM forward_queue WHERE recipient=? AND sent_at IS NULL ORDER BY queued_at",
        (forwarder.recipient,),
    ).fetchall()
    for (key,) in rows:
        try:
            payload = db.conn.execute(
                "SELECT payload FROM forward_queue WHERE message_key=? AND recipient=?",
                (key, forwarder.recipient),
            ).fetchone()[0]
            if payload is None:
                raise RuntimeError("转发队列缺少邮件内容")
            forwarder.send(payload)
            with db.conn:
                db.conn.execute(
                    "UPDATE forward_queue SET sent_at=?, payload=NULL, last_error='' "
                    "WHERE message_key=? AND recipient=?",
                    (datetime.now(timezone.utc).isoformat(), key, forwarder.recipient),
                )
                db.conn.execute(
                    "UPDATE messages SET status='forwarded' WHERE key=? AND status IN ('unmatched', 'failed', 'forward_only')",
                    (key,),
                )
            sent += 1
            log.info("无法入库邮件已转发，不再尝试入库：UID=%s，目标=%s", key.rsplit(":", 1)[-1], forwarder.recipient)
        except Exception as exc:
            with db.conn:
                db.conn.execute(
                    "UPDATE forward_queue SET last_error=? WHERE message_key=? AND recipient=?",
                    (str(exc), key, forwarder.recipient),
                )
            failed += 1
            log.error("无法入库邮件转发失败，仅重试转发：UID=%s，%s", key.rsplit(":", 1)[-1], exc)
    return sent, failed


def forward_only_messages(mail: HomeworkMailClient, db: HomeworkState,
                          forwarder: UnmatchedForwardClient) -> tuple[int, int]:
    db.migrate_forward_only()
    scope = None
    failed = 0
    keys = db.conn.execute("SELECT key FROM messages WHERE status='forward_only' ORDER BY key").fetchall()
    for (key,) in keys:
        if db.forward_exists(key, forwarder.recipient):
            continue
        try:
            if scope is None:
                scope = mail.scope()
            prefix, uid = key.rsplit(":", 1)
            if prefix != scope or not uid.isdigit():
                raise RuntimeError("历史邮件不属于当前邮箱 UIDVALIDITY，无法安全下载")
            queue_unmatched_forward(mail, db, forwarder, key, uid)
        except Exception as exc:
            failed += 1
            log.error("无法取得待转发原邮件，仅重试转发，不再入库：UID=%s，%s", key.rsplit(":", 1)[-1], exc)
    sent, send_failed = deliver_pending_forwards(db, forwarder)
    return sent, failed + send_failed


def forward_recorded_unmatched(mail: HomeworkMailClient, db: HomeworkState,
                              forwarder: UnmatchedForwardClient) -> int:
    sent, failed = forward_only_messages(mail, db, forwarder)
    log.info("无法入库邮件转发：本次发送=%d，失败=%d", sent, failed)
    return 1 if failed else 0


def prepare_reimport(feishu: HomeworkFeishuClient, db: HomeworkState, db_path: str) -> None:
    """仅移除本数据库上传的附件；清理中断可重试，备份先于远端修改。"""
    if db.reimport_phase() == "processing":
        log.info("继续上次未完成的重新入库")
        return
    owned = {row[0] for row in db.conn.execute(
        "SELECT token FROM uploads WHERE message_key NOT IN "
        "(SELECT key FROM messages WHERE status IN ('forward_only', 'forwarded'))"
    )}
    changes = []
    for record in feishu.list_records():
        for field, attachments in (record.get("fields") or {}).items():
            if not re.fullmatch(r"第[1-9][0-9]*周", field) or not attachments:
                continue
            if not isinstance(attachments, list) or any(
                not isinstance(item, dict) or not item.get("file_token") for item in attachments
            ):
                raise RuntimeError(f"周次列附件结构异常，停止重新入库：{field}")
            if any(item["file_token"] in owned for item in attachments):
                changes.append({"record_id": record["record_id"], "field": field, "attachments": attachments})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_prefix = str(Path(db_path).resolve()) + "-reimport-" + stamp
    with sqlite3.connect(backup_prefix + ".db") as backup:
        db.conn.backup(backup)
    with open(backup_prefix + ".json", "x", encoding="utf-8") as handle:
        json.dump(changes, handle, ensure_ascii=False, indent=2)
    log.info("重新入库备份已保存：%s，待清理单元格=%d", backup_prefix, len(changes))
    db.set_reimport_phase("cleanup")
    removed = 0
    for change in changes:
        record_id, field = change["record_id"], change["field"]
        current = feishu.record_fields(record_id).get(field) or []
        # 更新前重新读取，保留期间新增的非本脚本附件。
        remaining = [{"file_token": item["file_token"]} for item in current if item["file_token"] not in owned]
        if len(remaining) != len(current):
            feishu.update_record_fields(record_id, {field: remaining})
            removed += len(current) - len(remaining)
    with db.conn:
        db.conn.execute("DELETE FROM messages WHERE status NOT IN ('forward_only', 'forwarded')")
        for table in ("deliveries", "targets"):
            db.conn.execute(f"DELETE FROM {table}")
        db.conn.execute("DELETE FROM meta WHERE key NOT IN ('binding', 'reimport_phase')")
        db.conn.execute("UPDATE meta SET value='processing' WHERE key='reimport_phase'")
    log.info("已移除旧作业附件引用%d个；开始按当前归档规则重新入库", removed)


class HomeworkFeishuClient(FeishuTransport):
    def __init__(self) -> None:
        super().__init__()
        self.week_fields: dict[str, dict[str, Any]] | None = None


    def load_fields(self) -> None:
        self.week_fields = {item["field_name"]: item for item in self.list_fields()}

    def ensure_week(self, week: int) -> str:
        if self.week_fields is None:
            self.load_fields()
        assert self.week_fields is not None
        name = f"第{week}周"
        field = self.week_fields.get(name)
        if field is None:
            try:
                data = self.api("POST", self.table_path() + "/fields", json={"field_name": name, "type": 17})
                field = data.get("field")
                if not field:
                    raise RuntimeError("飞书创建字段未返回字段定义")
                self.week_fields[name] = field
            except Exception:
                # 创建成功但响应丢失、或另一个程序刚创建了同名列。
                self.load_fields()
                field = self.week_fields.get(name)
                if field is None:
                    raise
        if int(field.get("type", 0)) != 17:
            raise RuntimeError(f"{name} 已存在但不是附件字段，请人工修正类型")
        return name


    def _upload_part(self, upload_id: str, seq: int, block: bytes) -> None:
        """只重传失败分片，保持上传事务、序号及内容不变。"""
        for attempt in range(1, UPLOAD_PART_MAX_ATTEMPTS + 1):
            try:
                self.api("POST", "drive/v1/medias/upload_part", data={
                    "upload_id": upload_id, "seq": str(seq), "size": str(len(block)),
                    "checksum": str(zlib.adler32(block) & 0xffffffff),
                }, files={"file": ("chunk", block, "application/octet-stream")})
                return
            except requests.RequestException as exc:
                if isinstance(exc, requests.HTTPError):
                    retryable = (exc.response is not None
                                 and exc.response.status_code in UPLOAD_PART_RETRY_STATUSES)
                else:
                    retryable = (isinstance(exc, (requests.Timeout, requests.ConnectionError))
                                 and not isinstance(exc, requests.exceptions.SSLError))
                if not retryable or attempt == UPLOAD_PART_MAX_ATTEMPTS:
                    raise
                delay = 2 ** attempt
                log.warning(
                    "飞书分片上传临时失败：分片序号=%d，本次尝试=%d/%d，%d秒后重试：%s",
                    seq, attempt, UPLOAD_PART_MAX_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)

    def upload_attachment(self, filename: str, content: bytes) -> str:
        if not content:
            raise ValueError(f"无法上传空附件：{filename}")
        if len(content) <= SINGLE_UPLOAD_LIMIT:
            return self.upload_file(filename, content)
        data = self.api("POST", "drive/v1/medias/upload_prepare", json={
            "file_name": safe_filename(filename), "parent_type": "bitable_file",
            "parent_node": self.app_token, "size": len(content),
        })
        upload_id = data["upload_id"]
        block_size = int(data["block_size"])
        block_num = int(data["block_num"])
        if block_size <= 0 or block_num != math.ceil(len(content) / block_size):
            raise RuntimeError("飞书分片上传返回了无效的块大小或数量")
        for seq in range(block_num):
            block = content[seq * block_size:(seq + 1) * block_size]
            self._upload_part(upload_id, seq, block)
        result = self.api("POST", "drive/v1/medias/upload_finish", json={
            "upload_id": upload_id, "block_num": block_num,
        })
        return result["file_token"]


class HomeworkMailClient(ImapConnection):
    def _connect(self) -> None:
        for attempt in range(1, 4):
            try:
                super()._connect()
                return
            except ssl.SSLCertVerificationError:
                raise
            except (OSError, imaplib.IMAP4.abort) as exc:
                if attempt == 3:
                    raise RuntimeError(
                        f"IMAP 连接 {self.host}:{self.port} 连续3次失败（{type(exc).__name__}）。"
                        "若启用了 Clash/Mihomo 全局代理或 TUN，请将邮箱域名设为直连，"
                        "并使用 --check-imap 检查网络；TLS 握手失败发生在登录之前。"
                    ) from exc
                delay = 2 ** attempt
                log.warning("IMAP 连接失败（%d/3，%s），%d秒后重试", attempt, type(exc).__name__, delay)
                time.sleep(delay)

    def scope(self) -> str:
        # 保留原 JSON 格式，既有作业状态键无需迁移。
        return json.dumps(self.mailbox_identity())

    def uids(self, start: date) -> list[str]:
        assert self.conn is not None
        # SINCE 按服务器本地日期过滤，提前一天检索再用 INTERNALDATE 精确过滤。
        since = start - timedelta(days=1)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        criterion = f'{since.day:02d}-{months[since.month - 1]}-{since.year}'
        status, data = self.conn.uid("search", None, "SINCE", criterion)
        if status != "OK":
            raise RuntimeError("搜索作业邮件失败")
        return sorted((item.decode("ascii") for item in data[0].split()), key=int) if data and data[0] else []

    def header(self, uid: str) -> tuple[str, datetime]:
        assert self.conn is not None
        status, data = self.conn.uid("fetch", uid, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO)])")
        if status != "OK":
            raise RuntimeError(f"读取邮件头失败：UID={uid}")
        for item in data:
            if not isinstance(item, tuple):
                continue
            match = re.search(rb'INTERNALDATE "([^"]+)"', item[0])
            if not match:
                continue
            # 明确解析英语月份，不依赖 Linux/Windows 当前 locale。
            raw_date = match[1].decode("ascii").strip()
            parts = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})", raw_date)
            if not parts:
                raise ValueError(f"无效的 INTERNALDATE：UID={uid}")
            day, month, year, hour, minute, second, sign, tz_hour, tz_min = parts.groups()
            month_id = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(month.lower()) + 1
            offset = timedelta(hours=int(tz_hour), minutes=int(tz_min)) * (1 if sign == "+" else -1)
            received = datetime(int(year), month_id, int(day), int(hour), int(minute), int(second), tzinfo=timezone(offset))
            message = BytesParser(policy=policy.default).parsebytes(item[1])
            sender = normalized_email(message.get("Reply-To")) or normalized_email(message.get("From"))
            return sender, received
        raise RuntimeError(f"邮件缺少服务器收件时间：UID={uid}")

    def raw_message(self, uid: str) -> bytes:
        # 不在 FETCH 中重连，避免 UIDVALIDITY 改变后按旧 UID 取错邮件。
        cached = getattr(self, "_raw_cache", None)
        if cached is not None and cached[0] is self.conn and cached[1] == uid:
            return cached[2]
        raw = self.fetch_raw(uid)
        # 只保留当前一封，姓名匹配、上传或转发共用同一份原文。
        self._raw_cache = (self.conn, uid, raw)
        return raw

    @staticmethod
    def ordinary_parts(message: Message) -> Iterator[tuple[int, Message]]:
        def walk_parts(part: Message) -> Iterator[Message]:
            yield part
            # 附加 .eml 的内部主题和文件名不作为外层作业的身份依据。
            if (part.is_multipart() and not part.get_filename()
                    and part.get_content_disposition() not in {"attachment", "inline"}):
                for child in part.get_payload():
                    yield from walk_parts(child)

        for index, part in enumerate(walk_parts(message)):
            disposition = part.get_content_disposition()
            if disposition != "inline" and (disposition == "attachment" or part.get_filename()):
                yield index, part

    def matching_titles(self, uid: str) -> list[str]:
        message = BytesParser(policy=policy.default).parsebytes(self.raw_message(uid))
        titles = [str(message.get("Subject") or "")]
        for _, part in self.ordinary_parts(message):
            filename = part.get_filename()
            if filename:
                # 不使用目录名、正文、压缩包内文件或发件人显示名猜姓名。
                titles.append(Path(re.split(r"[/\\]", filename)[-1]).stem)
        return titles

    def subject(self, uid: str) -> str:
        message = BytesParser(policy=policy.default).parsebytes(self.raw_message(uid))
        return str(message.get("Subject") or "")

    def attachment_parts(self, uid: str) -> Iterator[tuple[int, str, bytes]]:
        message = BytesParser(policy=policy.default).parsebytes(self.raw_message(uid))
        for index, part in self.ordinary_parts(message):
            filename = part.get_filename()
            content = part.get_payload(decode=True)
            if content is None and part.get_content_type() == "message/rfc822":
                content = b"\r\n".join(child.as_bytes(policy=policy.SMTP) for child in part.get_payload())
            if content is None:
                raise ValueError(f"无法解码附件：UID={uid} part={index}")
            basename = re.split(r"[/\\]", filename)[-1] if filename else f"attachment-{index}.bin"
            yield index, safe_filename(basename), content


def process_message(
    key: str, uid: str, week: int, targets: list[str],
    mail: HomeworkMailClient, feishu: HomeworkFeishuClient, db: HomeworkState,
) -> bool:
    targets = db.targets(key, targets)
    subject = mail.subject(uid)
    groups: dict[int, list[tuple[int, str, bytes]]] = {}
    for part, filename, content in mail.attachment_parts(uid):
        target_week, source = attachment_week(subject, filename, week)
        groups.setdefault(target_week, []).append((part, filename, content))
        log.info("作业周次判定：UID=%s，附件=%s，第%d周，依据=%s", uid, filename, target_week, source)
    if not groups:
        reason = "无普通附件"
        db.status(key, "skipped", reason)
        log.warning("邮件未入库：UID=%s，原因=%s", uid, reason)
        return False
    for target_week, parts in groups.items():
        field = feishu.ensure_week(target_week)
        tokens = []
        for part, filename, content in parts:
            digest = hashlib.sha256(content).hexdigest()
            token = db.token(key, part, digest)
            if token is None:
                token = feishu.upload_attachment(filename, content)
                db.save_token(key, part, digest, token)
            tokens.append(token)
        for record_id in targets:
            if db.delivered(key, record_id, field):
                continue
            existing = feishu.record_fields(record_id).get(field) or []
            if not isinstance(existing, list) or any(not isinstance(item, dict) or not item.get("file_token") for item in existing):
                raise RuntimeError(f"记录 {record_id} 的 {field} 附件格式无效")
            merged = list(dict.fromkeys([item["file_token"] for item in existing] + tokens))
            if merged != [item["file_token"] for item in existing]:
                feishu.update_record_fields(record_id, {field: [{"file_token": token} for token in merged]})
            db.mark_delivery(key, record_id, field)
        log.info("作业附件归档成功：UID=%s，%s，附件=%d，匹配记录=%d", uid, field, len(tokens), len(targets))
    db.status(key, "done")
    log.info("作业入库成功：UID=%s，涉及周次=%s", uid, sorted(groups))
    return True


def run_once(mail: HomeworkMailClient, feishu: HomeworkFeishuClient, db: HomeworkState,
             start: date, process_existing: bool = True,
             forwarder: UnmatchedForwardClient | None = None) -> dict[str, int]:
    db.migrate_forward_only()
    try:
        counters = ingest_once(mail, feishu, db, start, process_existing)
    finally:
        # 即使飞书读取失败或 IMAP 断线，也尝试发送已缓存的待转发邮件。
        if forwarder is not None:
            sent, failed = forward_only_messages(mail, db, forwarder)
    if forwarder is not None:
        counters["forwarded"] = sent
        counters["forward_failed"] = failed
        counters["failed"] += failed
    log.info("本轮作业收取：%s", counters)
    return counters


def ingest_once(mail: HomeworkMailClient, feishu: HomeworkFeishuClient, db: HomeworkState,
                start: date, process_existing: bool = True) -> dict[str, int]:
    scope = mail.scope()
    uids = mail.uids(start)
    keys = {uid: f"{scope}:{uid}" for uid in uids}
    counters = {"done": 0, "skipped": 0, "unmatched": 0, "failed": 0}
    if not db.initialized(scope):
        db.initialize(scope, [] if process_existing else list(keys.values()))
        if not process_existing:
            counters["skipped"] = len(uids)
            log.warning("邮件已跳过：首次启动不补收，共%d封", len(uids))
            return counters
    records = feishu.list_records()
    index = record_index(records)
    names = name_index(records)
    batch_size = max(1, EMAIL_PAGE_SIZE)
    for offset in range(0, len(uids), batch_size):
        for uid in uids[offset:offset + batch_size]:
            key = keys[uid]
            if db.finished(key):
                continue
            try:
                sender, received = mail.header(uid)
                week = week_number(received, start)
                if week <= 0:
                    db.status(key, "skipped", "早于起始日期")
                    counters["skipped"] += 1
                    log.warning("邮件已滤除：UID=%s，原因=早于起始日期", uid)
                    continue
                targets = db.saved_targets(key) or index.get(sender, [])
                reason = "邮箱及标题姓名均未匹配"
                if not targets and names:
                    targets, reason = match_name_titles(mail.matching_titles(uid), names)
                    if targets:
                        log.info("邮箱未匹配，使用姓名兜底：UID=%s，%s，记录=%s", uid, reason, targets[0])
                if not targets:
                    db.status(key, "forward_only", reason)
                    counters["unmatched"] += 1
                    log.warning("邮件无法入库：UID=%s，发件人=%s，原因=%s", uid, sender or "空", reason)
                    continue
                success = process_message(key, uid, week, targets, mail, feishu, db)
                counters["done" if success else "skipped"] += 1
            except (imaplib.IMAP4.abort, OSError) as exc:
                # 连接失效时结束本轮，下轮重新取得 UIDVALIDITY。
                db.status(key, "forward_only", str(exc))
                log.error("邮件无法入库，改为只转发：UID=%s，%s", uid, exc)
                raise
            except Exception as exc:
                db.status(key, "forward_only", str(exc))
                counters["failed"] += 1
                log.error("作业处理失败，改为只转发，不再尝试入库：UID=%s，%s", uid, exc)
    return counters


def check_imap_transport() -> None:
    """仅检查 DNS、TLS 和欢迎消息；不登录、不访问飞书或处理状态库。"""
    host = os.getenv("EMAIL_IMAP_HOST", "imap.163.com").strip()
    port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    log.info("IMAP 网络检查：%s:%d，DNS=%s", host, port, ", ".join(addresses))
    if any(address.startswith(("198.18.", "198.19.")) for address in addresses):
        log.warning("DNS 返回可能的代理 Fake-IP，请检查 Clash/Mihomo 的全局模式及邮箱直连规则")
    with socket.create_connection((host, port), timeout=15) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as conn:
            with conn.makefile("rb") as stream:
                greeting = stream.readline(4096)
            if not greeting.startswith((b"* OK", b"* PREAUTH")):
                raise RuntimeError("TLS 已连接，但未收到 IMAP 成功欢迎消息")
            log.info("IMAP TLS 检查成功：%s，已收到欢迎消息（未登录）", conn.version())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init-fields", action="store_true", help="仅创建或检查前6周附件列")
    mode.add_argument("--once", action="store_true", help="执行一轮收取后退出")
    mode.add_argument("--check-imap", action="store_true", help="只检查邮箱 DNS/TLS，不登录或收取邮件")
    mode.add_argument("--reimport", action="store_true", help="备份并清理本脚本写入的周次附件，按新规则重新收取一轮")
    mode.add_argument("--forward-unmatched", action="store_true", help="只转发状态库中无法入库的邮件，不更新飞书")
    args = parser.parse_args()
    try:
        if args.check_imap:
            check_imap_transport()
            return 0
        start = date.fromisoformat(os.getenv("HOMEWORK_START_DATE", "2026-09-21"))
        weeks = int(os.getenv("HOMEWORK_INITIAL_WEEKS", "6"))
        if weeks < 6:
            raise ValueError("HOMEWORK_INITIAL_WEEKS 不得少于6")
        process_existing = os.getenv("HOMEWORK_PROCESS_EXISTING_ON_FIRST_RUN", "true").lower() in {"1", "true", "yes", "y"}
        db_path = os.getenv("HOMEWORK_STATE_DB_PATH", "homework_pipeline.db")
        if Path(db_path).resolve() == Path(os.getenv("STATE_DB_PATH", "resume_pipeline.db")).resolve():
            raise ValueError("作业数据库不能与简历数据库相同")
        with instance_lock(db_path):
            if args.forward_unmatched:
                db = HomeworkState(db_path)
                mail = None
                try:
                    forwarder = UnmatchedForwardClient()
                    mail = HomeworkMailClient()
                    return forward_recorded_unmatched(mail, db, forwarder)
                finally:
                    if mail is not None:
                        mail.close()
                    db.close()
            feishu = HomeworkFeishuClient()
            for week in range(1, weeks + 1):
                feishu.ensure_week(week)
            log.info("已检查第1周至第%d周附件列", weeks)
            if args.init_fields:
                return 0
            db = HomeworkState(db_path)
            try:
                db.bind({"host": os.getenv("EMAIL_IMAP_HOST", "imap.163.com"),
                         "port": os.getenv("EMAIL_IMAP_PORT", "993"),
                         "username": os.getenv("EMAIL_USERNAME", "").casefold(),
                         "folder": os.getenv("EMAIL_FOLDER", "INBOX"),
                         "app": feishu.app_token, "table": feishu.table_id,
                         "start": start.isoformat(), "sender_field": FIELD_SENDER})
                if db.reimport_phase() in {"cleanup", "processing"} and not args.reimport:
                    raise RuntimeError("重新入库尚未完成，请使用 --reimport 继续")
                forwarder = UnmatchedForwardClient()
                while True:
                    mail = None
                    try:
                        mail = HomeworkMailClient()
                        if args.reimport:
                            # 清理前确认能够搜索邮箱，避免网络不可用时先移除旧附件。
                            mail.scope()
                            mail.uids(start)
                            prepare_reimport(feishu, db, db_path)
                        counters = run_once(mail, feishu, db, start, True if args.reimport else process_existing, forwarder)
                        if args.reimport and not counters["failed"]:
                            db.set_reimport_phase("complete")
                        if args.once or args.reimport:
                            return 1 if counters["failed"] else 0
                    except Exception as exc:
                        log.error("作业轮询失败，%s：%s", "本次退出" if args.once or args.reimport else "下轮重试", exc)
                        log.debug("作业轮询异常详情", exc_info=True)
                        if args.once or args.reimport:
                            return 1
                    finally:
                        if mail is not None:
                            mail.close()
                    time.sleep(max(1, HOMEWORK_POLL_INTERVAL_SECONDS))
            finally:
                db.close()
    except KeyboardInterrupt:
        log.info("作业收取已停止")
        return 0
    except Exception:
        log.exception("作业收取启动失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
