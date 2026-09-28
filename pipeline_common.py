#!/usr/bin/env python3
"""两个业务入口共享的通信工具；不导入业务脚本、不加载 .env、不创建状态库。"""
from __future__ import annotations

import imaplib
import logging
import mimetypes
import os
import re
import smtplib
import ssl
import time
from contextlib import contextmanager
from email.header import decode_header
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)
FEISHU_BASE = "https://open.feishu.cn/open-apis"
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


def safe_filename(name: str) -> str:
    name = Path(name).name.strip()
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name) or "resume.bin"


def red_text(value: str) -> str:
    """在终端日志中用红色突出需要人工注意的提示。"""
    return f"{ANSI_RED}{value}{ANSI_RESET}"


def decode_mime_header(value: Any) -> str:
    if value is None:
        return ""
    result: list[str] = []
    for part, charset in decode_header(str(value)):
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result).strip()


class ImapConnection:
    """只负责连接、登录和只读选取文件夹，不定义扫描或入库策略。"""
    def __init__(self) -> None:
        self.host = os.getenv("EMAIL_IMAP_HOST", "imap.163.com").strip()
        self.port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
        self.username = required_env("EMAIL_USERNAME")
        self.password = required_env("EMAIL_PASSWORD")
        self.folder = os.getenv("EMAIL_FOLDER", "INBOX").strip() or "INBOX"
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self._connect()

    def _connect(self) -> None:
        conn = imaplib.IMAP4_SSL(
            self.host,
            self.port,
            timeout=60
        )

        try:
            # 1. 登录
            try:
                status, data = conn.login(
                    self.username,
                    self.password
                )
            except imaplib.IMAP4.error as exc:
                raise RuntimeError(
                    "163 邮箱 IMAP 登录失败，请确认 EMAIL_USERNAME 正确，"
                    "EMAIL_PASSWORD 使用的是客户端授权码"
                ) from exc

            if status != "OK":
                raise RuntimeError(
                    f"163 邮箱 IMAP 登录失败：{data!r}"
                )

            log.info("163 IMAP LOGIN 成功")

            # 2. 163 邮箱要求 LOGIN 后发送 IMAP ID
            # Python imaplib 默认没有注册 ID 命令
            imaplib.Commands["ID"] = ("AUTH",)

            status, data = conn._simple_command(
                "ID",
                '("name" "ResumeCollectionSystem" '
                '"version" "1.0.0" '
                '"vendor" "Python")'
            )

            if status != "OK":
                log.warning(
                    "163 IMAP ID 命令返回异常：%s %r",
                    status,
                    data,
                )
            else:
                log.info("163 IMAP ID 成功")

            # 3. ID 后再 SELECT
            status, data = conn.select(
                self.folder,
                readonly=True
            )

            if status != "OK":
                detail = " ".join(
                    item.decode(
                        "utf-8",
                        errors="replace"
                    )
                    if isinstance(item, bytes)
                    else str(item)
                    for item in (data or [])
                )

                raise RuntimeError(
                    f"无法打开 163 邮箱文件夹："
                    f"{self.folder} "
                    f"({detail or data!r})"
                )

            log.info(
                "163 IMAP INBOX 打开成功：%r",
                data
            )

        except Exception:
            try:
                conn.logout()
            except Exception:
                pass
            raise

        self.conn = conn

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def mailbox_identity(self) -> list[Any]:
        """缓存连接的 UIDVALIDITY；业务层决定状态键格式。"""
        if self.conn is None:
            self._connect()
        cached = getattr(self, "_identity_cache", None)
        if cached is not None and cached[0] is self.conn:
            return list(cached[1])
        _, values = self.conn.response("UIDVALIDITY")
        validity = values[0].decode("ascii") if values and values[0] else ""
        if not validity.isdigit():
            raise RuntimeError("IMAP 未返回有效 UIDVALIDITY，无法安全去重")
        identity = [self.host, self.port, self.username.casefold(), self.folder, validity]
        self._identity_cache = (self.conn, identity)
        return list(identity)

    def fetch_raw(self, uid: str) -> bytes:
        """读取一次邮件原文；重连/重试由各业务自行决定。"""
        if self.conn is None:
            raise RuntimeError("IMAP 尚未连接")
        status, data = self.conn.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError(f"读取邮件失败：UID={uid}")
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2:
                return bytes(item[1])
        raise RuntimeError(f"邮件正文不存在：UID={uid}")


class FeishuTransport:
    """飞书认证、素材和表格基础接口，不包含候选人或作业规则。"""
    def __init__(self) -> None:
        self.app_id = required_env("FEISHU_APP_ID")
        self.app_secret = required_env("FEISHU_APP_SECRET")
        self.app_token = required_env("FEISHU_BITABLE_APP_TOKEN")
        self.table_id = required_env("FEISHU_TABLE_ID")
        self._token: Optional[str] = None
        self._expire_at = 0.0

    def token(self) -> str:
        now = time.time()
        if self._token and now < self._expire_at - 120:
            return self._token
        response = requests.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书认证失败：{body.get('code')} {body.get('msg', '')}")
        self._token = body["tenant_access_token"]
        self._expire_at = now + int(body.get("expire", 7200))
        return self._token

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def api(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        timeout = kwargs.pop("timeout", 120)
        response = requests.request(
            method, f"{FEISHU_BASE}/{path}", headers=self.auth_headers(), timeout=timeout, **kwargs
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书接口失败：{body.get('code')} {body.get('msg', '')}")
        return body.get("data") or {}

    def table_path(self) -> str:
        return f"bitable/v1/apps/{quote(self.app_token, safe='')}/tables/{quote(self.table_id, safe='')}"

    def _list_items(self, resource: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token = ""
        visited: set[str] = set()
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.api("GET", self.table_path() + "/" + resource, params=params, timeout=60)
            result.extend(data.get("items", []))
            if not data.get("has_more"):
                return result
            page_token = data.get("page_token")
            if not page_token or page_token in visited:
                raise RuntimeError(f"飞书 {resource} 分页响应缺少有效 page_token")
            visited.add(page_token)

    def list_records(self) -> list[dict[str, Any]]:
        return self._list_items("records")

    def list_fields(self) -> list[dict[str, Any]]:
        return self._list_items("fields")

    def record_fields(self, record_id: str) -> dict[str, Any]:
        data = self.api("GET", self.table_path() + f"/records/{quote(record_id, safe='')}")
        return data["record"]["fields"]

    def update_record_fields(self, record_id: str, fields: dict[str, Any]) -> None:
        self.api("PUT", self.table_path() + f"/records/{quote(record_id, safe='')}", json={"fields": fields}, timeout=60)

    def upload_file(self, filename: str, data: bytes) -> str:
        filename = safe_filename(filename)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        result = self.api("POST", "drive/v1/medias/upload_all", data={
            "file_name": filename, "parent_type": "bitable_file",
            "parent_node": self.app_token, "size": str(len(data)),
        }, files={"file": (filename, data, mime)})
        return result["file_token"]

    def download_bitable_attachment(self, file_token: str) -> bytes:
        response = requests.get(
            f"{FEISHU_BASE}/drive/v1/medias/{quote(file_token, safe='')}/download",
            headers=self.auth_headers(), timeout=120,
        )
        response.raise_for_status()
        return response.content


def send_smtp(host: str, port: int, username: str, password: str,
              recipient: str, message: EmailMessage | bytes) -> None:
    """只负责一次SMTP发送；正文、收件人和持久化重试策略由业务层决定。"""
    smtp = smtplib.SMTP_SSL(host, port, timeout=60, context=ssl.create_default_context())
    try:
        smtp.login(username, password)
        if isinstance(message, bytes):
            refused = smtp.sendmail(username, [recipient], message)
        else:
            refused = smtp.send_message(message, from_addr=username, to_addrs=[recipient])
        if refused:
            raise RuntimeError("SMTP 拒绝收件人")
    finally:
        try:
            smtp.quit()
        except Exception:
            # DATA已确认后的QUIT失败不改变发送结果。
            log.debug("关闭 SMTP 连接失败", exc_info=True)


@contextmanager
def instance_lock(db_path: str) -> Iterator[None]:
    """对同一数据库持有进程锁；进程退出后由操作系统释放。"""
    lock_path = str(Path(db_path).resolve()) + ".lock"
    with open(lock_path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("已有实例使用此数据库") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

