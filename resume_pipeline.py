#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cross-platform Resume Pipeline
==============================

163 邮箱 / IMAP
    -> 下载 PDF / DOCX / DOC
    -> 本地转 TXT（不使用 PDF.co）
    -> SiliconFlow 一次性结构化解析
    -> 飞书上传原始简历
    -> 飞书多维表格新增候选人记录
    -> SMTP 发送报名回信

支持:
- Linux
- Windows

本地文档解析:
- PDF  : PyMuPDF
- DOCX : 直接解析 DOCX(zip+xml)，无需 Office
- DOC  :
    auto 模式按当前系统自动尝试
    Linux   : antiword -> LibreOffice
    Windows : LibreOffice -> Microsoft Word COM
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import imaplib
import io
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
import uuid
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import (
    format_datetime,
    make_msgid,
    parseaddr,
    parsedate_to_datetime,
)
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import quote
from xml.etree import ElementTree as ET
import pymupdf  # PyMuPDF
import requests
from dotenv import load_dotenv
from pipeline_common import (
    FEISHU_BASE, FeishuTransport, ImapConnection, decode_mime_header,
    red_text, required_env, safe_filename, send_smtp, instance_lock,
)


# ---------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------

load_dotenv()

SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}
MAX_SKILLS = 5
MAX_SHORT_TEXT_CHARS = 49


POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
EMAIL_PAGE_SIZE = int(
    os.getenv("EMAIL_PAGE_SIZE", "50")
)
MIN_EXTRACTED_CHARS = int(os.getenv("MIN_EXTRACTED_CHARS", "80"))
MAX_RESUME_CHARS = int(os.getenv("MAX_RESUME_CHARS", "60000"))
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "resume_pipeline.db")
PROCESS_EXISTING_ON_FIRST_RUN = os.getenv(
    "PROCESS_EXISTING_ON_FIRST_RUN", "false"
).lower() in {"1", "true", "yes", "y"}

DOC_BACKEND = os.getenv("DOC_BACKEND", "auto").strip().lower()
LIBREOFFICE_PATH = os.getenv("LIBREOFFICE_PATH", "").strip()
ANTIWORD_PATH = os.getenv("ANTIWORD_PATH", "").strip()

RECEIVED_TIME_MODE = os.getenv("RECEIVED_TIME_MODE", "timestamp").strip().lower()

# 飞书列名（按当前招新表格结构）
# 期望列顺序：
# 收件时间 | 邮件发件人 | 邮件标题 | 原始简历 | 姓名 | QQ号 |
# 意向组别 | 技术栈 | 学院 | 生源地 | 学号 | 性别 | 专业 |
# 项目经历 | 报名原因 | 兴趣爱好 | 白名单项 |
# 简历摘要 | 招聘状态 | 备注
FIELD_RECEIVED_TIME = os.getenv("FIELD_RECEIVED_TIME", "收件时间")
FIELD_SENDER = os.getenv("FIELD_SENDER", "邮件发件人")
FIELD_SUBJECT = os.getenv("FIELD_SUBJECT", "邮件标题")
FIELD_ATTACHMENT = os.getenv("FIELD_ATTACHMENT", "原始简历")
FIELD_NAME = os.getenv("FIELD_NAME", "姓名")
FIELD_QQ = os.getenv("FIELD_QQ", "QQ号")
FIELD_INTENTION_GROUP = os.getenv("FIELD_INTENTION_GROUP", "意向组别")
FIELD_SKILLS = os.getenv("FIELD_SKILLS", "技术栈")
FIELD_COLLEGE = os.getenv("FIELD_COLLEGE", "学院")
FIELD_MAJOR = os.getenv("FIELD_MAJOR", "专业")
FIELD_BIRTHPLACE = os.getenv("FIELD_BIRTHPLACE", "生源地")
FIELD_STUDENT_ID = os.getenv("FIELD_STUDENT_ID", "学号")
FIELD_GENDER = os.getenv("FIELD_GENDER", "性别")
FIELD_PROJECTS = os.getenv("FIELD_PROJECTS", "项目经历")
FIELD_APPLICATION_REASON = os.getenv("FIELD_APPLICATION_REASON", "报名原因")
FIELD_HOBBIES = os.getenv("FIELD_HOBBIES", "兴趣爱好")
FIELD_SUMMARY = os.getenv("FIELD_SUMMARY", "简历摘要")
FIELD_STATUS = os.getenv("FIELD_STATUS", "招聘状态")
DEFAULT_STATUS = os.getenv("DEFAULT_STATUS", "待初筛")

AUTO_CREATE_SKILLS_FIELD = os.getenv(
    "AUTO_CREATE_SKILLS_FIELD", "true"
).lower() in {"1", "true", "yes", "y"}
AUTO_ADD_SKILL_OPTIONS = os.getenv(
    "AUTO_ADD_SKILL_OPTIONS", "true"
).lower() in {"1", "true", "yes", "y"}
AUTO_CREATE_SHORT_TEXT_FIELDS = os.getenv(
    "AUTO_CREATE_SHORT_TEXT_FIELDS", "true"
).lower() in {"1", "true", "yes", "y"}

# 飞书入库成功后发送给候选人的固定回信。回信不依赖简历筛选结果。
REPLY_ENABLED = os.getenv("REPLY_ENABLED", "true").lower() in {
    "1", "true", "yes", "y"
}
REPLY_BODY = """已收到您的简历，感谢报名！

请根据您的意向组别加入对应的培训群，后续培训安排、考核通知等信息将在 QQ 招新群内发布，请及时关注群消息。

机械组：781323068
嵌入式组（硬件 / 电控）：1121923815
算法组（导航 / 视觉 / 软件）：2159078941
运宣组：1108278071


期待在后续培训中与你相遇！"""

FIELD_WHITELIST = os.getenv("FIELD_WHITELIST", "白名单项")
WHITELIST_ENABLED = os.getenv("WHITELIST_ENABLED", "true").lower() in {
    "1", "true", "yes", "y"
}
# 这段规则会原样放入模型提示词，修改 .env 后即可调整白名单标准，
# 不需要改代码或重新实现关键词匹配。
WHITELIST_RULES = os.getenv(
    "WHITELIST_RULES",
    """只根据简历中明确出现的事实提取以下正向特点：
1. 生源地明确为上海，输出“上海生源”。
2. 兴趣爱好明确包含骑车，输出“爱好：骑车”。
3. 兴趣爱好明确包含战雷，输出“爱好：战雷”。
4. 明确看过比赛，输出“看过比赛”；如果明确去线下或现场观看，输出“线下看过比赛”。
没有明确证据时不要输出，不得根据学校、专业、项目或常识推断。""",
).strip()


INTENTION_GROUP_UNKNOWN = os.getenv("INTENTION_GROUP_UNKNOWN", "未定义").strip()
INTENTION_GROUP_OPTIONS = {
    item.strip()
    for item in os.getenv(
        "INTENTION_GROUP_OPTIONS",
        "导航,自瞄,机械,软件,硬件,运宣,电控",
    ).split(",")
    if item.strip()
}
INTENTION_GROUP_ALIASES = {
    "嵌入式": ["硬件", "电控"],
    "算法": ["导航", "自瞄"],
    "视觉": ["自瞄"],
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("resume-pipeline")


# ---------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def parse_iso_datetime(value: str) -> datetime:
    value = (value or "").strip()
    if not value:
        return datetime.now(timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def model_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("模型输出不是 JSON object")
    return obj


def normalize_short_text(value: Any) -> str:
    """清理短文本字段，确保无 Markdown 且长度严格小于 50 字。"""
    if value is None:
        return ""

    if isinstance(value, list):
        parts = [
            str(item).strip()
            for item in value
            if item is not None and str(item).strip()
        ]
        text = "、".join(parts)
    elif isinstance(value, dict):
        text = "、".join(
            f"{str(key).strip()}：{str(item).strip()}"
            for key, item in value.items()
            if str(key).strip() and str(item).strip()
        )
    else:
        text = str(value)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```(?:[A-Za-z0-9_-]+)?", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(
        r"(?m)^\s*(?:[-*+#>]+\s*|\d+[.)]\s+)",
        "",
        text,
    )
    text = re.sub(
        r"^\s*(?:报名原因|申请原因|兴趣爱好|爱好)\s*[:：]\s*",
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_SHORT_TEXT_CHARS].strip()


def normalize_intention_groups(value: Any) -> List[str]:
    """把模型输出统一成飞书多选字段需要的字符串数组。"""
    if value is None:
        return []

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    value = parsed
            except json.JSONDecodeError:
                pass

    candidates = value if isinstance(value, list) else [value]
    result: List[str] = []
    seen = set()

    for candidate in candidates:
        if not isinstance(candidate, (str, int, float)):
            continue
        parts = re.split(r"[/、,，;；]+", str(candidate))
        for part in parts:
            group = part.strip().strip('"\'[]')
            if not group:
                continue
            mapped_groups = INTENTION_GROUP_ALIASES.get(group, [group])
            for mapped_group in mapped_groups:
                if mapped_group not in INTENTION_GROUP_OPTIONS:
                    mapped_group = INTENTION_GROUP_UNKNOWN
                if mapped_group not in seen:
                    seen.add(mapped_group)
                    result.append(mapped_group)

    return result


def normalize_skills(value: Any) -> List[str]:
    """把模型输出统一成简短的技能/工具字符串数组。"""
    if value is None:
        return []

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    value = parsed
            except json.JSONDecodeError:
                pass

    candidates = value if isinstance(value, list) else [value]
    result: List[str] = []
    seen = set()
    aliases = {
        "c++": "cpp",
        "c plus plus": "cpp",
        "python3": "python",
        "solid works": "solidworks",
    }

    for candidate in candidates:
        if not isinstance(candidate, (str, int, float)):
            continue
        parts = re.split(r"[/、,，;；|\n]+", str(candidate))
        for part in parts:
            skill = re.sub(r"\s+", " ", part).strip().strip('"\'[]')
            if not skill or len(skill) > 40:
                continue
            normalized = aliases.get(skill.lower(), skill)
            if re.fullmatch(r"[A-Za-z0-9+#. -]+", normalized):
                normalized = normalized.lower()
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)

    return result[:MAX_SKILLS]


def normalize_whitelist_items(value: Any) -> List[str]:
    """把模型返回的白名单项统一为短字符串数组。"""
    if value is None:
        return []

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    value = parsed
            except json.JSONDecodeError:
                pass

    candidates = value if isinstance(value, list) else [value]
    result: List[str] = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, (str, int, float)):
            continue
        for part in re.split(r"[\n|；;、,，]+", str(candidate)):
            item = re.sub(r"\s+", " ", part).strip().strip('"\'[]')
            item = re.sub(r"^(?:[-*#]+|\d+[.)])\s*", "", item).strip()
            if not item or len(item) > 80 or item in seen:
                continue
            seen.add(item)
            result.append(item)
    return result[:20]


# ---------------------------------------------------------------------
# PDF -> TXT
# ---------------------------------------------------------------------

def pdf_to_text(data: bytes) -> str:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        pages = []
        for idx, page in enumerate(doc):
            txt = page.get_text("text", sort=True)
            pages.append(f"\n--- 第 {idx + 1} 页 ---\n{txt}")
    finally:
        doc.close()

    result = normalize_text("\n".join(pages))
    if len(result) < MIN_EXTRACTED_CHARS:
        raise ValueError(
            f"PDF 只提取到 {len(result)} 个字符。可能是扫描件/图片 PDF。"
            "当前版本按你的要求不使用在线 OCR。"
        )
    return result


# ---------------------------------------------------------------------
# DOCX -> TXT（纯 Python）
# ---------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"


def _word_xml_text(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    lines: List[str] = []

    for p in root.iter(W + "p"):
        parts: List[str] = []
        for node in p.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in {W + "br", W + "cr"}:
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            lines.append(text)

    return "\n".join(lines)


def docx_to_text(data: bytes) -> str:
    chunks: List[str] = []

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise ValueError("无效 DOCX：缺少 word/document.xml")

        chunks.append(_word_xml_text(zf.read("word/document.xml")))

        extras = sorted(
            n for n in names
            if (
                re.fullmatch(r"word/header\d+\.xml", n)
                or re.fullmatch(r"word/footer\d+\.xml", n)
                or n in {"word/footnotes.xml", "word/endnotes.xml"}
            )
        )

        for name in extras:
            try:
                chunks.append(_word_xml_text(zf.read(name)))
            except Exception:
                log.debug("跳过无法解析的 DOCX XML: %s", name)

    result = normalize_text("\n".join(chunks))
    if len(result) < MIN_EXTRACTED_CHARS:
        raise ValueError(f"DOCX 只提取到 {len(result)} 个字符")
    return result


# ---------------------------------------------------------------------
# DOC -> TXT（跨平台）
# ---------------------------------------------------------------------

def find_libreoffice() -> Optional[str]:
    if LIBREOFFICE_PATH:
        p = Path(LIBREOFFICE_PATH)
        if p.exists():
            return str(p)

    for cmd in ("libreoffice", "soffice"):
        found = shutil.which(cmd)
        if found:
            return found

    if os.name == "nt":
        candidates = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                return c

    return None


def find_antiword() -> Optional[str]:
    if ANTIWORD_PATH:
        p = Path(ANTIWORD_PATH)
        if p.exists():
            return str(p)
    return shutil.which("antiword")


def decode_text_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "utf-16", "gb18030", "cp936", "latin1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def doc_via_antiword(path: Path) -> Optional[str]:
    exe = find_antiword()
    if not exe:
        return None

    p = subprocess.run(
        [exe, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if p.returncode != 0:
        return None

    return decode_text_bytes(p.stdout)


def doc_via_libreoffice(path: Path, outdir: Path) -> Optional[str]:
    exe = find_libreoffice()
    if not exe:
        return None

    # LibreOffice 在 Linux/Windows 都支持 headless CLI
    cmd = [
        exe,
        "--headless",
        "--convert-to",
        "txt:Text",
        "--outdir",
        str(outdir),
        str(path),
    ]

    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if p.returncode != 0:
        return None

    expected = outdir / f"{path.stem}.txt"
    if expected.exists():
        return decode_text_bytes(expected.read_bytes())

    txts = list(outdir.glob("*.txt"))
    if txts:
        return decode_text_bytes(txts[0].read_bytes())

    return None


def doc_via_word_com(path: Path, outdir: Path) -> Optional[str]:
    if os.name != "nt":
        return None

    try:
        import win32com.client  # type: ignore
    except Exception:
        return None

    out = outdir / f"{path.stem}.txt"
    word = None
    doc = None

    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(
            str(path.resolve()),
            ReadOnly=True,
            AddToRecentFiles=False,
            ConfirmConversions=False,
        )

        # 直接读取文档正文，避免部分 Word 版本在 SaveAs2 转换旧版 .doc
        # 时因编码或文件扩展名问题失败。
        text = doc.Content.Text
        if text and text.strip():
            return text

        # wdFormatUnicodeText = 7
        doc.SaveAs2(str(out.resolve()), FileFormat=7)
        doc.Close(False)
        doc = None
        word.Quit()
        word = None

        if out.exists():
            return out.read_text(encoding="utf-16", errors="replace")
        return None
    except Exception:
        log.debug("Microsoft Word COM 解析 .doc 失败：%s", path, exc_info=True)
        return None
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass


def doc_to_text(data: bytes, filename: str) -> str:
    backend = DOC_BACKEND

    with tempfile.TemporaryDirectory(prefix="resume_doc_") as td:
        outdir = Path(td)
        path = outdir / safe_filename(filename)
        path.write_bytes(data)

        if backend == "auto":
            if os.name == "nt":
                methods = [
                    ("LibreOffice", lambda: doc_via_libreoffice(path, outdir)),
                    ("Microsoft Word COM", lambda: doc_via_word_com(path, outdir)),
                ]
            else:
                methods = [
                    ("antiword", lambda: doc_via_antiword(path)),
                    ("LibreOffice", lambda: doc_via_libreoffice(path, outdir)),
                ]
        elif backend == "libreoffice":
            methods = [("LibreOffice", lambda: doc_via_libreoffice(path, outdir))]
        elif backend == "antiword":
            methods = [("antiword", lambda: doc_via_antiword(path))]
        elif backend == "word":
            methods = [("Microsoft Word COM", lambda: doc_via_word_com(path, outdir))]
        else:
            raise ValueError(
                "DOC_BACKEND 只能是 auto/libreoffice/antiword/word"
            )

        tried = []
        for name, fn in methods:
            tried.append(name)
            try:
                text = fn()
                if text:
                    text = normalize_text(text)
                    if len(text) >= MIN_EXTRACTED_CHARS:
                        log.info(".doc 使用 %s 解析成功", name)
                        return text
            except Exception:
                log.exception(".doc 解析后端 %s 失败", name)

    raise RuntimeError(
        "旧版 .doc 本地解析失败。已尝试: "
        + ", ".join(tried)
        + "。Linux 推荐安装 antiword 或 LibreOffice；"
          "Windows 推荐安装 LibreOffice，或 Microsoft Word + pywin32。"
    )


def resume_to_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        return pdf_to_text(data)
    if ext == ".docx":
        return docx_to_text(data)
    if ext == ".doc":
        return doc_to_text(data, filename)

    raise ValueError(f"不支持文件类型：{ext}")


# ---------------------------------------------------------------------
# SiliconFlow
# ---------------------------------------------------------------------

RESUME_KEYS = [
    "name",
    "qq",
    "intention_group",
    "skills",
    "college",
    "birthplace",
    "student_id",
    "gender",
    "major",
    "projects",
    "application_reason",
    "hobbies",
    "summary",
    "whitelist_items",
]


def analyze_resume(text: str, mail_subject: str) -> Dict[str, Any]:
    """
    一次 SiliconFlow 调用，输出当前飞书招新表所需的 AI 字段。

    注意：
    - 邮件元数据（收件时间、发件人、标题、附件）由程序直接填写，不让模型重复判断。
    - 招聘状态固定由程序填写为“待初筛”。
    - 备注默认留空，供后续人工初筛填写。
    """
    api_key = required_env("SILICONFLOW_API_KEY")
    model = required_env("SILICONFLOW_MODEL")

    if len(text) > MAX_RESUME_CHARS:
        log.warning(
            "简历文本过长：%d 字符，仅发送前 %d 字符",
            len(text),
            MAX_RESUME_CHARS,
        )
        text = text[:MAX_RESUME_CHARS]

    system_prompt = (
        "你是 RoboMaster 战队招新简历信息整理助手。"
        "你的任务只是把候选人简历和邮件标题中的明确事实整理成结构化信息，"
        "不得虚构、不得夸大、不得替候选人补全不存在的经历。"
        "如果候选人明确说明某项经历参与程度有限、成果一般、主要工作由他人完成，"
        "必须忠实保留这一限制，不能包装成其独立完成。"
        "不要做录用、淘汰、能力等级、人格、心理、健康、政治、宗教等判断。"
        "最终必须只输出一个合法 JSON object，不要输出 Markdown 代码块或解释文字。"
    )

    user_prompt = f"""
请根据下面的【邮件标题】和【简历正文】，一次性整理招新表格需要的信息。

只返回以下 14 个 JSON 键，键名必须完全一致：
name
qq
intention_group
skills
college
birthplace
student_id
gender
major
projects
application_reason
hobbies
summary
whitelist_items

====================
一、字段提取规则
====================

1. name
候选人姓名。
只有原文能够明确识别时填写，否则返回空字符串。

2. qq
QQ号。
只有原文明确出现“QQ”“QQ号”“腾讯QQ”等含义时才能填写。
不得把手机号、微信号、邮箱用户名、学号或其他数字猜成 QQ。
找不到返回空字符串。

3. intention_group
候选人的意向组别/报名组别。
只允许使用以下组别名称：导航、自瞄、机械、软件、硬件、运宣、电控。
候选人可能填写别名：嵌入式映射为“硬件”和“电控”；算法映射为“导航”和“自瞄”；视觉映射为“自瞄”。
返回时必须使用映射后的标准组别名称，不要返回这些别名。
优先使用候选人明确填写的“意向组别”“报名组别”“志愿组别”等内容；
如果正文没有但邮件标题明确写出组别，可以从邮件标题提取。
多个组别返回字符串数组，并按原文顺序排列。
如果明确识别到其他组别，返回 ["未定义"]；如果没有明确填写组别，返回 []。
不得根据项目经历自行推测组别。

4. skills
候选人明确使用过的工具、编程语言、框架或软件名称。
必须返回字符串数组，只写简短名称，不要写使用经历、熟练程度或完整句子。
最多返回 5 个最重要的技能，并按重要性从高到低排列；不要为了凑数添加普通办公软件或泛化能力。
例如：["solidworks", "cpp", "python"]。
优先使用常见简短写法：C++ 写成 cpp，Python3 写成 python，Solid Works 写成 solidworks。
只能根据简历明确出现的内容填写，不得根据项目名称猜测技能；找不到返回 []。

5. college
学院名称。
例如“信息科学与工程学院”。
只填写明确出现的学院名称。
不要因为出现专业名称就擅自推断学院。
找不到返回空字符串。

6. birthplace
生源地/籍贯/户籍所在地/家乡等信息。
只填写原文明确出现的地点，尽量使用省、市、区县等简短地名。
不得根据学校所在地、手机号、身份证号、口音或项目经历推断。
如果原文没有明确出现，返回空字符串。

7. student_id
学号或学籍号。
只填写原文明确标注为“学号”“学籍号”的内容，必须保留原始字母和数字。
不得把身份证号、手机号、QQ号、邮箱、班级或录取号当作学号。
如果原文没有明确出现，返回空字符串。

8. gender
性别。
只填写原文明确出现的性别文本，例如“男”“女”；不得根据姓名、照片或称谓推断。
如果原文没有明确出现，返回空字符串。

9. major
专业名称。
只填写明确出现的专业名称，例如“人工智能”“自动化”“计算机科学与技术”。
“信息科学与工程学院”是学院，不应直接当成专业。
如果原文只写了学院而没写具体专业，major 必须返回空字符串。

====================
10. projects 项目经历格式
====================

projects 必须是一个字符串，用自然换行组织，格式尽量如下：

项目经历整理

项目一
项目名称：……
时间：……
候选人职责：……
使用技术：……
主要成果：……

项目二
项目名称：……
时间：……
候选人职责：……
使用技术：……
主要成果：……

备注：
1. ……
2. ……

具体要求：
- 只整理简历中真实存在的项目、比赛、科研或较明确的实践经历。
- 每个项目尽量包含：项目名称、时间、候选人职责、使用技术、主要成果。
- 原文没有的信息写“未注明”或“未明确提及”，不要编造。
- “候选人职责”必须区分候选人本人实际完成的工作和团队/学长学姐完成的工作。
- 如果候选人明确说“主要由学长学姐完成”“水平有限”“论文很水”“只了解基础功能”等，
  必须在对应项目或备注中客观保留，不要美化。
- 仅仅“接触过某软件”“了解基础功能”但没有形成独立项目的，不必强行单列项目，
  可以放在备注或 summary 的技术能力中。
- 不要把普通课程学习凭空包装成项目。
- 如果确实没有项目经历，返回：
  项目经历整理

  暂无明确项目经历

====================
11. application_reason 报名原因
====================

只根据简历或邮件正文中候选人明确表达的报名/申请原因填写。
必须是纯文本，内容清晰凝练，少于 50 个字，不得输出 Markdown、标题、项目符号、换行、引号或“报名原因：”前缀。
如果没有明确表达，返回空字符串。不要根据意向组别或项目经历自行推断原因。

====================
12. hobbies 兴趣爱好
====================

只填写简历或邮件正文中明确出现的兴趣爱好，可用“摄影、跑步、音乐”这样的短语。
必须是纯文本，内容清晰凝练，少于 50 个字，不得输出 Markdown、标题、项目符号、换行、引号或“兴趣爱好：”前缀。
如果没有明确出现，返回空字符串。不要把技能、课程或项目经历当作兴趣爱好。

====================
13. summary 候选人摘要格式
====================

summary 必须是一个字符串，使用自然换行，整体结构如下：

候选人摘要

姓名：{{候选人姓名}} | 意向组别：{{意向组别}}

专业：
{{专业；如果专业未知但学院明确，可写“学院：XXX；专业未注明”}}

【技术能力】
核心技能：
- ……
- ……
- ……

【主要经历】
1. ……
2. ……
3. ……

【岗位相关亮点】
- ……
- ……
- ……

具体要求：
- “技术能力”只能写简历明确体现的技能、软件、算法、硬件、科研/工程能力。
- “主要经历”概括项目、比赛、科研、社团或其他与报名相关的经历。
- “岗位相关亮点”只能做基于事实的岗位相关性整理，例如：
  “有 yolov5 目标识别项目实践，与算法方向存在直接交叉”。
- 不得写“优秀”“能力很强”“潜力巨大”“强烈推荐”等主观招聘结论。
- 不得因为学校、性别、年龄、籍贯等与岗位无关因素给出优劣判断。
- 如果候选人实际参与程度有限，要在摘要中如实体现。
- 不得把“学院”伪造成“专业”。
- 不要为了填满格式而编造经历。
- summary 不要求限制在 200 字；应以信息完整、便于招新初筛阅读为优先，
  但避免重复和冗长。

====================
14. whitelist_items 白名单项
====================

按照【白名单规则】判断简历中是否有明确的正向特点。只根据简历原文和邮件标题中
明确写出的事实返回字符串数组；没有明确证据时返回 []，不要猜测或补全。
如果明确去线下或现场观看比赛，使用规则中指定的“线下看过比赛”标签，不要同时输出普通标签。

【白名单规则】
{WHITELIST_RULES}

====================
五、输出要求
====================

严格输出 JSON，例如：
{{
  "name": "吕家宝",
  "qq": "",
  "intention_group": ["运宣"],
  "skills": ["solidworks", "cpp", "python"],
  "college": "信息科学与工程学院",
  "birthplace": "山东省济南市",
  "student_id": "2023001234",
  "gender": "男",
  "major": "",
  "projects": "项目经历整理\n\n项目一\n项目名称：……",
  "application_reason": "希望参与机器人项目并提升实战能力",
  "hobbies": "摄影、跑步",
  "summary": "候选人摘要\n\n姓名：吕家宝 | 意向组别：运宣\n\n专业：\n学院：信息科学与工程学院；专业未注明\n\n【技术能力】……",
  "whitelist_items": ["看过比赛"]
}}

不要输出这 14 个键之外的任何其他键。
缺失的普通字段统一使用空字符串。

【邮件标题】
{mail_subject}

【简历正文】
{text}
""".strip()

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "temperature": 0.1,
        "max_tokens": int(os.getenv("SILICONFLOW_MAX_TOKENS", "3500")),
        "response_format": {"type": "json_object"},
    }

    if os.getenv("SILICONFLOW_SEND_ENABLE_THINKING", "true").lower() in {
        "1", "true", "yes", "y"
    }:
        payload["enable_thinking"] = False

    r = requests.post(
        SILICONFLOW_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    raw = r.json()

    try:
        content = raw["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"SiliconFlow 响应结构异常：{raw}") from e

    obj = model_json(content)

    normalized: Dict[str, Any] = {}
    for key in RESUME_KEYS:
        value = obj.get(key, "")

        if key == "intention_group":
            normalized[key] = normalize_intention_groups(value)
            continue

        if key == "skills":
            normalized[key] = normalize_skills(value)
            continue

        if key == "whitelist_items":
            normalized[key] = normalize_whitelist_items(value)
            continue

        if key in {
            "birthplace",
            "student_id",
            "gender",
            "application_reason",
            "hobbies",
        }:
            normalized[key] = normalize_short_text(value)
            continue

        if value is None:
            value = ""
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        normalized[key] = str(value).strip()

    return normalized


def build_whitelist_items(
    ai: Dict[str, Any],
) -> List[str]:
    """使用模型按 WHITELIST_RULES 返回的结果，不在代码中硬匹配。"""
    if not WHITELIST_ENABLED:
        return []
    return normalize_whitelist_items(ai.get("whitelist_items", []))


# ---------------------------------------------------------------------
# 飞书
# ---------------------------------------------------------------------

class FeishuClient(FeishuTransport):
    """简历字段和候选人记录写入策略。"""
    def __init__(self) -> None:
        super().__init__()
        self._field_definitions: Dict[str, Optional[Dict[str, Any]]] = {}
        self._fields_load_failed = False

    def upload_resume(self, filename: str, data: bytes) -> str:
        return self.upload_file(filename, data)

    def field_definition(
        self,
        field_name: str,
    ) -> Optional[Dict[str, Any]]:
        """读取并缓存字段定义。返回 None 表示字段不存在或读取失败。"""
        if field_name in self._field_definitions:
            return self._field_definitions[field_name]

        try:
            items = self.list_fields()
            field = next(
                (
                    item for item in items
                    if item.get("field_name") == field_name
                ),
                None,
            )
            if field is None:
                log.warning(
                    "飞书中找不到字段 %r，将跳过该字段以避免写入失败",
                    field_name,
                )
                self._field_definitions[field_name] = None
                return None

            self._field_definitions[field_name] = field
            return field
        except Exception:
            self._fields_load_failed = True
            log.warning(
                "无法读取飞书字段 %r，将跳过该字段以避免写入失败",
                field_name,
                exc_info=True,
            )
            self._field_definitions[field_name] = None
            return None

    def multiselect_options(
        self,
        field_name: str,
    ) -> Optional[set[str]]:
        """读取飞书实际的多选选项，避免非法选项导致整条记录失败。"""
        field = self.field_definition(field_name)
        if field is None:
            return None

        if int(field.get("type", 0)) != 4:
            log.warning(
                "飞书字段 %r 不是多选类型，将跳过该字段以避免写入失败",
                field_name,
            )
            return None

        options = (field.get("property") or {}).get("options", [])
        return {
            str(option.get("name", "")).strip()
            for option in options
            if str(option.get("name", "")).strip()
        }

    def ensure_text_field(self, field_name: str) -> bool:
        """确保短文本字段存在；权限不足时跳过该字段而不影响整条记录。"""
        field = self.field_definition(field_name)
        if field is not None:
            if int(field.get("type", 0)) != 1:
                log.warning(
                    "飞书字段 %r 不是文本类型，将跳过该字段",
                    field_name,
                )
                return False
            return True

        if self._fields_load_failed or not AUTO_CREATE_SHORT_TEXT_FIELDS:
            return False

        url = (
            f"{FEISHU_BASE}/bitable/v1/apps/"
            f"{quote(self.app_token, safe='')}/tables/"
            f"{quote(self.table_id, safe='')}/fields"
        )
        try:
            r = requests.post(
                url,
                headers={
                    **self.auth_headers(),
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "field_name": field_name,
                    "type": 1,
                    "ui_type": "Text",
                },
                timeout=60,
            )
            r.raise_for_status()
            response = r.json()
            if response.get("code") != 0:
                raise RuntimeError(response)
            field = response.get("data", {}).get("field")
            if not field:
                raise RuntimeError(f"飞书创建文本字段响应异常：{response}")
            self._field_definitions[field_name] = field
            log.info("已自动创建飞书文本字段：%s", field_name)
            return True
        except Exception:
            log.warning(
                "无法自动创建飞书文本字段 %r，本条记录将跳过该字段",
                field_name,
                exc_info=True,
            )
            self._field_definitions[field_name] = None
            return False


    def ensure_skill_options(
        self,
        skills: List[str],
    ) -> Optional[set[str]]:
        """确保技术栈多选字段存在，并追加本次识别到的新技能。"""
        field = self.field_definition(FIELD_SKILLS)

        if field is None:
            if self._fields_load_failed or not AUTO_CREATE_SKILLS_FIELD:
                return None

            url = (
                f"{FEISHU_BASE}/bitable/v1/apps/"
                f"{quote(self.app_token, safe='')}/tables/"
                f"{quote(self.table_id, safe='')}/fields"
            )
            body = {
                "field_name": FIELD_SKILLS,
                "type": 4,
                "ui_type": "MultiSelect",
                "property": {
                    "options": [
                        {"name": skill, "color": index % 10}
                        for index, skill in enumerate(skills)
                    ]
                },
            }
            try:
                r = requests.post(
                    url,
                    headers={
                        **self.auth_headers(),
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=body,
                    timeout=60,
                )
                r.raise_for_status()
                response = r.json()
                if response.get("code") != 0:
                    raise RuntimeError(response)
                field = response.get("data", {}).get("field")
                if not field:
                    raise RuntimeError(f"飞书创建技术栈字段响应异常：{response}")
                self._field_definitions[FIELD_SKILLS] = field
            except Exception:
                log.warning(
                    "无法自动创建飞书多选字段 %r，将跳过技能字段",
                    FIELD_SKILLS,
                    exc_info=True,
                )
                self._field_definitions[FIELD_SKILLS] = None
                return None

        if int(field.get("type", 0)) != 4:
            log.warning(
                "飞书字段 %r 不是多选类型，将跳过技能字段",
                FIELD_SKILLS,
            )
            return None

        property_data = field.get("property") or {}
        option_list = property_data.get("options", [])
        existing = {
            str(option.get("name", "")).strip()
            for option in option_list
            if str(option.get("name", "")).strip()
        }
        missing = [skill for skill in skills if skill not in existing]

        if missing and AUTO_ADD_SKILL_OPTIONS:
            field_id = field.get("field_id")
            if not field_id:
                log.warning("飞书技术栈字段缺少 field_id，将跳过新技能选项")
                return existing

            updated_options = list(option_list)
            for index, skill in enumerate(missing, start=len(updated_options)):
                updated_options.append(
                    {"name": skill, "color": index % 10}
                )

            url = (
                f"{FEISHU_BASE}/bitable/v1/apps/"
                f"{quote(self.app_token, safe='')}/tables/"
                f"{quote(self.table_id, safe='')}/fields/"
                f"{quote(str(field_id), safe='')}"
            )
            try:
                r = requests.put(
                    url,
                    headers={
                        **self.auth_headers(),
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "field_name": FIELD_SKILLS,
                        "type": 4,
                        "ui_type": "MultiSelect",
                        "property": {"options": updated_options},
                    },
                    timeout=60,
                )
                r.raise_for_status()
                response = r.json()
                if response.get("code") != 0:
                    raise RuntimeError(response)
                existing.update(missing)
                field["property"] = {"options": updated_options}
            except Exception:
                log.warning(
                    "无法自动添加飞书技术栈选项 %s，本次只写入已有选项",
                    missing,
                    exc_info=True,
                )

        return existing

    def filter_multiselect_values(
        self,
        field_name: str,
        values: List[str],
    ) -> Optional[List[str]]:
        available = self.multiselect_options(field_name)
        if available is None:
            log.warning(
                "无法安全写入飞书多选字段 %r，本条记录将不提交该字段",
                field_name,
            )
            return None

        unsupported = [value for value in values if value not in available]
        if unsupported:
            log.warning(
                "飞书多选字段 %r 不支持选项 %s，将忽略这些选项",
                field_name,
                unsupported,
            )
        return [value for value in values if value in available]

    def build_record_fields(
        self,
        received_datetime: str,
        sender: str,
        subject: str,
        file_token: str,
        ai: Dict[str, Any],
    ) -> Dict[str, Any]:
        dt = parse_iso_datetime(received_datetime)

        if RECEIVED_TIME_MODE == "text":
            received_value: Any = dt.isoformat()
        else:
            received_value = int(dt.timestamp() * 1000)

        # 只向当前招新表发送下面这些列。
        # 邮件信息/附件由程序直接填写；候选人信息由一次 LLM 结构化结果填写；
        # 招聘状态默认“待初筛”；备注留空供人工初筛填写。
        intention_groups = normalize_intention_groups(
            ai.get("intention_group", [])
        )
        intention_groups = self.filter_multiselect_values(
            FIELD_INTENTION_GROUP,
            intention_groups,
        )
        skill_values = normalize_skills(ai.get("skills", []))
        available_skills = self.ensure_skill_options(skill_values)
        if available_skills is None:
            skills = None
        else:
            skills = [
                skill for skill in skill_values
                if skill in available_skills
            ]

        application_reason_available = self.ensure_text_field(
            FIELD_APPLICATION_REASON
        )
        hobbies_available = self.ensure_text_field(FIELD_HOBBIES)
        birthplace_available = self.ensure_text_field(FIELD_BIRTHPLACE)
        student_id_available = self.ensure_text_field(FIELD_STUDENT_ID)
        gender_available = self.ensure_text_field(FIELD_GENDER)
        whitelist_available = (
            WHITELIST_ENABLED and self.ensure_text_field(FIELD_WHITELIST)
        )

        fields = {
            FIELD_RECEIVED_TIME: received_value,
            FIELD_SENDER: sender,
            FIELD_SUBJECT: subject,
            FIELD_ATTACHMENT: [{"file_token": file_token}],
            FIELD_NAME: ai["name"],
            FIELD_QQ: ai["qq"],
            FIELD_COLLEGE: ai["college"],
        }

        if birthplace_available:
            fields[FIELD_BIRTHPLACE] = normalize_short_text(
                ai.get("birthplace", "")
            )
        if student_id_available:
            fields[FIELD_STUDENT_ID] = normalize_short_text(
                ai.get("student_id", "")
            )
        if gender_available:
            fields[FIELD_GENDER] = normalize_short_text(
                ai.get("gender", "")
            )

        fields.update({
            FIELD_PROJECTS: ai["projects"],
            FIELD_SUMMARY: ai["summary"],
            FIELD_STATUS: DEFAULT_STATUS,
        })

        if intention_groups is not None:
            fields[FIELD_INTENTION_GROUP] = intention_groups
        if skills is not None:
            fields[FIELD_SKILLS] = skills
        if application_reason_available:
            fields[FIELD_APPLICATION_REASON] = normalize_short_text(
                ai.get("application_reason", "")
            )
        if hobbies_available:
            fields[FIELD_HOBBIES] = normalize_short_text(
                ai.get("hobbies", "")
            )
        if whitelist_available:
            fields[FIELD_WHITELIST] = "、".join(
                build_whitelist_items(ai)
            )

        return fields

    def create_record(self, **kwargs: Any) -> str:
        return self.submit_record_fields(self.build_record_fields(**kwargs))

    def submit_record_fields(
        self, fields: Dict[str, Any], client_token: Optional[str] = None,
    ) -> str:
        # client_token is an official query parameter; keep it stable across retries.
        data = self.api(
            "POST", self.table_path() + "/records", json={"fields": fields},
            params={"client_token": client_token} if client_token else {}, timeout=60,
        )
        record_id = (data.get("record") or {}).get("record_id")
        if not record_id:
            raise RuntimeError("飞书未返回 record_id，不能确认入库成功")
        return str(record_id)

    def find_record_by_attachment(self, file_token: str, field_name: str) -> Optional[str]:
        matches = []
        for record in self.list_records():
            attachments = (record.get("fields") or {}).get(field_name) or []
            if isinstance(attachments, dict):
                attachments = [attachments]
            if any(isinstance(item, dict) and item.get("file_token") == file_token
                   for item in attachments):
                if not record.get("record_id"):
                    raise RuntimeError("飞书核对结果缺少 record_id")
                matches.append(str(record["record_id"]))
        if len(matches) > 1:
            raise RuntimeError("同一上传附件匹配多条飞书记录，请人工核对；停止再次创建")
        return matches[0] if matches else None


# ---------------------------------------------------------------------
# 163 邮箱 IMAP
# ---------------------------------------------------------------------

class MailboxScopeChanged(RuntimeError):
    """连接重建后 UID 不再属于当前扫描快照。"""


class ImapMailClient(ImapConnection):
    """简历邮件的完整扫描、分批正文缓存和附件筛选。"""
    def __init__(self) -> None:
        self._raw_by_id: Dict[str, bytes] = {}
        super().__init__()

    def scope(self) -> str:
        identity = self.mailbox_identity()
        identity[0] = identity[0].casefold()
        serialized = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        return "imap-v2:" + hashlib.sha256(serialized.encode()).hexdigest()

    def _check_scope(self, expected: str) -> None:
        if self.scope() != expected:
            raise MailboxScopeChanged("IMAP 邮箱作用域已改变，结束本轮并重新初始化，避免误用旧 UID")

    _decode_header = staticmethod(decode_mime_header)

    def _fetch_raw(self, uid: str, expected_scope: str) -> bytes:
        for attempt in range(2):
            if self.conn is None:
                self._connect()
            assert self.conn is not None

            self._check_scope(expected_scope)
            try:
                return self.fetch_raw(uid)
            except (imaplib.IMAP4.abort, OSError):
                self.close()
                if attempt == 1:
                    raise
                self._connect()

        raise RuntimeError(f"读取邮件失败：UID={uid}")

    @staticmethod
    def _received_datetime(message: Message) -> str:
        try:
            received = parsedate_to_datetime(str(message.get("Date", "")))
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            return received.isoformat()
        except (TypeError, ValueError, OverflowError):
            return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _has_attachments(message: Message) -> bool:
        return any(
            part.get_filename() and part.get_content_disposition() != "inline"
            for part in message.walk()
        )

    def _message_uids(self) -> List[str]:
        for attempt in range(2):
            if self.conn is None:
                self._connect()
            assert self.conn is not None
            try:
                status, data = self.conn.uid("search", None, "ALL")
                if status != "OK":
                    raise RuntimeError(f"搜索 163 邮件失败：{data!r}")
                break
            except (imaplib.IMAP4.abort, OSError):
                self.close()
                if attempt == 1:
                    raise
                self._connect()

        raw_uids = data[0].split() if data and data[0] else []
        return [
            raw_uid.decode("ascii", errors="replace")
            for raw_uid in sorted(raw_uids, key=int)
        ]

    def message_ids(self) -> List[str]:
        """取得整份 UID 快照；不下载正文，不截断历史积压。"""
        uids = self._message_uids()
        scope = self.scope()
        return [f"{scope}:{uid}" for uid in uids]

    def messages(
        self,
        should_fetch: Optional[Callable[[str], bool]] = None,
        message_ids: Optional[List[str]] = None,
    ) -> Iterator[Dict[str, Any]]:
        ids = self.message_ids() if message_ids is None else message_ids
        scope = self.scope()
        pending = [mid for mid in ids if should_fetch is None or should_fetch(mid)]
        batch_size = max(1, EMAIL_PAGE_SIZE)
        # 从老到新完整遍历；只保留一个批次的原文。
        for offset in range(0, len(pending), batch_size):
            batch = []
            try:
                for message_id in pending[offset:offset + batch_size]:
                    prefix = scope + ":"
                    if not message_id.startswith(prefix):
                        raise RuntimeError("邮件不属于当前邮箱作用域")
                    try:
                        raw = self._fetch_raw(message_id[len(prefix):], scope)
                        self._raw_by_id[message_id] = raw
                        message = BytesParser(policy=policy.default).parsebytes(raw)
                        batch.append({
                            "id": message_id,
                            "subject": self._decode_header(message.get("Subject")),
                            "from": {"emailAddress": {"address": parseaddr(
                                self._decode_header(message.get("From")))[1]}},
                            "replyTo": parseaddr(self._decode_header(message.get("Reply-To")))[1],
                            "receivedDateTime": self._received_datetime(message),
                            "hasAttachments": self._has_attachments(message),
                            "internetMessageId": self._decode_header(message.get("Message-ID")),
                        })
                    except MailboxScopeChanged:
                        raise
                    except Exception:
                        log.exception("下载邮件失败，保留待重试：%s", message_id)
                        continue
                yield from batch
            finally:
                self._raw_by_id.clear()

    def attachments(self, message_id: str) -> List[Dict[str, Any]]:
        raw = self._raw_by_id.get(message_id)
        if raw is None:
            scope = self.scope()
            prefix = scope + ":"
            if not message_id.startswith(prefix):
                raise RuntimeError(f"无效的 IMAP 邮件 ID：{message_id}")
            raw = self._fetch_raw(message_id[len(prefix):], scope)
            self._raw_by_id[message_id] = raw

        message = BytesParser(policy=policy.default).parsebytes(raw)
        result: List[Dict[str, Any]] = []
        for part in message.walk():
            filename = part.get_filename()
            disposition = part.get_content_disposition()
            if not filename or disposition == "inline":
                continue

            content = part.get_payload(decode=True)
            if not content:
                continue

            result.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "isInline": False,
                    "name": self._decode_header(filename),
                    "contentBytes": base64.b64encode(content).decode("ascii"),
                }
            )

        return result


# ---------------------------------------------------------------------
# SMTP 回信
# ---------------------------------------------------------------------

def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    if not subject:
        return "简历报名回执"
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


class SmtpReplyClient:
    """发送固定报名回信；不读取或判断简历内容。"""

    def __init__(self) -> None:
        self.host = os.getenv("EMAIL_SMTP_HOST", "smtp.163.com").strip()
        self.port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        self.username = required_env("EMAIL_USERNAME")
        self.password = required_env("EMAIL_PASSWORD")

    def send_reply(
        self,
        recipient: str,
        subject: str,
        original_message_id: str = "",
    ) -> None:
        recipient = parseaddr(recipient)[1].strip()
        if not recipient:
            raise ValueError("回信收件人地址为空")

        message = EmailMessage(policy=policy.SMTP)
        message["From"] = self.username
        message["To"] = recipient
        message["Subject"] = reply_subject(subject)
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid()
        if original_message_id:
            message["In-Reply-To"] = original_message_id
            message["References"] = original_message_id
        message.set_content(REPLY_BODY)

        send_smtp(self.host, self.port, self.username, self.password, recipient, message)


# ---------------------------------------------------------------------
# SQLite 去重
# ---------------------------------------------------------------------

class StateDB:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_messages (
                message_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
            """
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )
            """
        )

        # SMTP 回信队列。先入库再入队，SMTP 暂时失败时可在后续轮询重试，
        # 同时不会因为重试而重复创建飞书记录。
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_queue (
                reply_key TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                original_message_id TEXT NOT NULL DEFAULT '',
                queued_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )

        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS resume_attachments "
            "(attachment_key TEXT PRIMARY KEY, progress TEXT NOT NULL)"
        )

        self.conn.commit()

    def initialize_mailbox(
        self, scope: str, message_ids: List[str], folder: str,
        process_existing: bool, adopt_legacy: bool = False,
    ) -> bool:
        """原子建立作用域快照；旧格式仅在显式确认归属后迁移。"""
        key = "initialized:" + scope
        if self.meta(key) == "1":
            return False
        legacy = self.conn.execute(
            "SELECT message_id, processed_at FROM seen_messages WHERE message_id LIKE 'imap:%'"
        ).fetchall()
        unbound_legacy = (legacy or self.meta("initialized") == "1") and not self.meta("legacy_scope")
        if unbound_legacy and not adopt_legacy:
            raise RuntimeError(
                "检测到未绑定邮箱的旧简历状态库。确认仍为原邮箱和原文件夹后，"
                "先执行 --adopt-legacy-state；请勿删除数据库或直接重导历史邮件。"
            )
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            if unbound_legacy:
                prefix = f"imap:{folder}:"
                old = [(mid[len(prefix):], stamp) for mid, stamp in legacy
                       if mid.startswith(prefix) and mid[len(prefix):].isdigit()]
                if legacy and not old:
                    raise RuntimeError("旧状态库没有当前文件夹的 UID，请核对 EMAIL_FOLDER")
                self.conn.executemany(
                    "INSERT OR IGNORE INTO seen_messages VALUES (?, ?)",
                    [(f"{scope}:{uid}", stamp) for uid, stamp in old],
                )
                # 旧版首次仅跳过末尾一页；更早的未知历史不能因升级突然导入。
                floor = min((int(uid) for uid, _ in old), default=None)
                skip = [] if process_existing else [
                    mid for mid in message_ids
                    if floor is None or int(mid.rsplit(":", 1)[1]) < floor
                ]
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('legacy_scope', ?)", (scope,))
            else:
                skip = [] if process_existing else message_ids
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen_messages VALUES (?, ?)", [(mid, now) for mid in skip],
            )
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, '1')", (key,))
        log.info("邮箱作用域初始化完成：跳过历史=%d，旧状态迁移=%s", len(skip), bool(unbound_legacy))
        return True

    def attachment_progress(self, key: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT progress FROM resume_attachments WHERE attachment_key=?", (key,),
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def save_attachment_progress(self, key: str, progress: Dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO resume_attachments VALUES (?, ?)",
                (key, json.dumps(progress, ensure_ascii=False)),
            )

    def seen(self, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        return row is not None

    def mark(self, message_id: str) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO seen_messages(message_id, processed_at)
            VALUES (?, ?)
            """,
            (
                message_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def meta(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT v FROM meta WHERE k=?",
            (key,),
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def queue_reply(
        self,
        reply_key: str,
        recipient: str,
        subject: str,
        original_message_id: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO reply_queue(
                reply_key, recipient, subject, original_message_id, queued_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                reply_key,
                recipient,
                subject,
                original_message_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def pending_replies(self) -> List[Dict[str, str]]:
        rows = self.conn.execute(
            """
            SELECT reply_key, recipient, subject, original_message_id
            FROM reply_queue
            WHERE sent_at IS NULL
            ORDER BY queued_at ASC
            """
        ).fetchall()
        return [
            {
                "reply_key": str(row[0]),
                "recipient": str(row[1]),
                "subject": str(row[2]),
                "original_message_id": str(row[3] or ""),
            }
            for row in rows
        ]

    def mark_reply_sent(self, reply_key: str) -> None:
        self.conn.execute(
            "UPDATE reply_queue SET sent_at=? WHERE reply_key=?",
            (datetime.now(timezone.utc).isoformat(), reply_key),
        )
        self.conn.commit()


# ---------------------------------------------------------------------
# 业务流程
# ---------------------------------------------------------------------

def get_sender(message: Dict[str, Any]) -> str:
    reply_to = str(message.get("replyTo") or "").strip()
    if reply_to:
        return reply_to
    return (
        message.get("from", {})
        .get("emailAddress", {})
        .get("address", "")
        .strip()
    )


def extract_supported_files(
    mail_client: ImapMailClient,
    message_id: str,
) -> List[tuple[str, bytes]]:
    result: List[tuple[str, bytes]] = []

    for att in mail_client.attachments(message_id):
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue

        if att.get("isInline"):
            continue

        filename = safe_filename(att.get("name") or "attachment.bin")
        ext = Path(filename).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            continue

        content = att.get("contentBytes")
        if not content:
            continue

        result.append(
            (
                filename,
                base64.b64decode(content, validate=False),
            )
        )

    return result


def ingest_resume_file(
    filename: str,
    data: bytes,
    subject: str,
    sender: str,
    received: str,
    feishu: FeishuClient,
    db: Optional[StateDB] = None,
    attachment_key: str = "",
) -> str:
    """解析一份简历；邮箱处理通过附件进度保存已完成步骤。"""
    if db is not None and not attachment_key:
        raise ValueError("持久化简历入库必须提供附件标识")
    progress = db.attachment_progress(attachment_key) if db is not None else {}
    if db is not None:
        target = [feishu.app_token, feishu.table_id]
        if progress and progress["target"] != target:
            raise RuntimeError("该附件已绑定另一张飞书表，请恢复配置后继续，避免重复建档")
        if progress.get("record_id"):
            return progress["record_id"]
        if not progress:
            progress = {"target": target, "client_token": str(uuid.uuid4()),
                        "attachment_field": FIELD_ATTACHMENT}
            db.save_attachment_progress(attachment_key, progress)
        if progress.get("attempted"):
            existing = feishu.find_record_by_attachment(
                progress["file_token"], progress["attachment_field"],
            )
            if existing:
                progress["record_id"] = existing
                db.save_attachment_progress(attachment_key, progress)
                return existing
        if progress.get("fields"):
            return _submit_resume_progress(feishu, db, attachment_key, progress)

    log.info("处理简历：%s | %s", subject, filename)

    ai = progress.get("ai")
    if ai is None:
        text = resume_to_text(filename, data)
        log.info("本地转 TXT 成功：%d 字符", len(text))
        ai = analyze_resume(text, subject)
        if db is not None:
            progress["ai"] = ai
            db.save_attachment_progress(attachment_key, progress)

    file_token = progress.get("file_token")
    if not file_token:
        file_token = feishu.upload_resume(filename, data)
        if db is not None:
            progress["file_token"] = file_token
            db.save_attachment_progress(attachment_key, progress)
    values = dict(received_datetime=received, sender=sender, subject=subject,
                  file_token=file_token, ai=ai)
    if db is None:
        record_id = feishu.create_record(**values)
    else:
        progress["attachment_field"] = FIELD_ATTACHMENT
        progress["fields"] = feishu.build_record_fields(**values)
        db.save_attachment_progress(attachment_key, progress)
        record_id = _submit_resume_progress(feishu, db, attachment_key, progress)
    log.info("飞书记录创建完成：%s", record_id)
    return record_id


def _submit_resume_progress(
    feishu: FeishuClient, db: StateDB, key: str, progress: Dict[str, Any],
) -> str:
    # 先持久化尝试标记。响应丢失或进程退出后，先远端核对再重用相同请求。
    progress["attempted"] = True
    db.save_attachment_progress(key, progress)
    record_id = feishu.submit_record_fields(progress["fields"], progress["client_token"])
    if not record_id:
        raise RuntimeError("飞书未返回 record_id，保留附件进度待核对")
    progress["record_id"] = record_id
    db.save_attachment_progress(key, progress)
    return record_id


def _record_value_to_text(value: Any) -> str:
    """把飞书记录中的常见单元格值转成可供模型参考的文本。"""
    if value is None:
        return ""
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("name") or item.get("value")
            else:
                text = item
            if text is not None and str(text).strip():
                parts.append(str(text).strip())
        return "、".join(parts)
    if isinstance(value, dict):
        return "、".join(
            f"{key}：{item}"
            for key, item in value.items()
            if str(key).strip() and str(item).strip()
        )
    return str(value).strip()


def _record_resume_attachments(
    fields: Dict[str, Any],
) -> List[tuple[str, str]]:
    """返回记录中原始简历附件的 (file_token, filename)。"""
    value = fields.get(FIELD_ATTACHMENT)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    result: List[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        token = item.get("file_token") or item.get("token")
        if not token:
            continue
        filename = safe_filename(
            str(item.get("name") or item.get("file_name") or "resume.pdf")
        )
        result.append((str(token), filename))
    return result


def _historical_resume_source(
    feishu: FeishuClient,
    record_id: str,
    fields: Dict[str, Any],
    fallback_fields: List[str],
) -> str:
    """优先读取历史简历原件，解析失败时使用本次回填指定的已有字段。"""
    for file_token, filename in _record_resume_attachments(fields):
        if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            source = resume_to_text(filename, feishu.download_bitable_attachment(file_token))
            if source:
                return source
        except Exception:
            log.warning(
                "历史记录附件解析失败：%s (%s)", record_id, filename, exc_info=True,
            )
    parts = [_record_value_to_text(fields.get(name)) for name in fallback_fields]
    return "\n".join(part for part in parts if part)


BACKFILL_FIELD_MAP = {
    "birthplace": FIELD_BIRTHPLACE,
    "student_id": FIELD_STUDENT_ID,
    "gender": FIELD_GENDER,
}


def backfill_candidate_fields(
    feishu: FeishuClient,
    target_keys: List[str],
    force: bool = False,
) -> None:
    """重新解析历史简历，并只回填指定的候选人字段。"""
    unknown_keys = [key for key in target_keys if key not in BACKFILL_FIELD_MAP]
    if unknown_keys:
        raise ValueError(f"不支持的历史回填字段：{unknown_keys}")

    target_keys = list(dict.fromkeys(target_keys))
    target_fields = {
        key: BACKFILL_FIELD_MAP[key]
        for key in target_keys
    }
    for field_name in target_fields.values():
        if not feishu.ensure_text_field(field_name):
            raise RuntimeError(
                f"飞书字段 {field_name!r} 不存在，且当前无法自动创建"
            )

    records = feishu.list_records()
    counters = {
        "total": len(records),
        "updated_records": 0,
        "updated_fields": 0,
        "skipped_existing": 0,
        "skipped_no_source": 0,
        "no_target_value": 0,
        "failed": 0,
    }

    for index, record in enumerate(records, start=1):
        record_id = str(record.get("record_id") or "")
        fields = record.get("fields") or {}
        pending_keys = [
            key
            for key, field_name in target_fields.items()
            if force or not _record_value_to_text(fields.get(field_name))
        ]
        if not pending_keys:
            counters["skipped_existing"] += 1
            continue

        subject = _record_value_to_text(fields.get(FIELD_SUBJECT))
        source = _historical_resume_source(
            feishu, record_id, fields,
            [FIELD_SUBJECT, FIELD_NAME, FIELD_COLLEGE, FIELD_MAJOR,
             FIELD_PROJECTS, FIELD_SUMMARY],
        )

        if not record_id or not source:
            counters["skipped_no_source"] += 1
            continue

        try:
            ai = analyze_resume(source, subject)
            update_fields = {}
            for key in pending_keys:
                value = normalize_short_text(ai.get(key, ""))
                if value:
                    update_fields[target_fields[key]] = value

            if not update_fields:
                counters["no_target_value"] += 1
                continue

            feishu.update_record_fields(record_id, update_fields)
            counters["updated_records"] += 1
            counters["updated_fields"] += len(update_fields)
            log.info(
                "历史字段回填 %d/%d：%s -> %s",
                index,
                len(records),
                record_id,
                update_fields,
            )
        except Exception:
            counters["failed"] += 1
            log.exception("历史记录字段回填失败：%s", record_id)

    print(json.dumps(counters, ensure_ascii=False, indent=2))


def backfill_birthplace(
    feishu: FeishuClient,
    force: bool = False,
) -> None:
    """兼容旧命令：回填历史记录的生源地。"""
    backfill_candidate_fields(feishu, ["birthplace"], force=force)


def backfill_student_info(
    feishu: FeishuClient,
    force: bool = False,
) -> None:
    """回填历史记录的学号和性别。"""
    backfill_candidate_fields(
        feishu,
        ["student_id", "gender"],
        force=force,
    )


def backfill_whitelist(
    feishu: FeishuClient,
    force: bool = False,
) -> None:
    """重新解析历史简历，只回填白名单项，不创建或发送任何回信。"""
    if not WHITELIST_ENABLED:
        log.info("WHITELIST_ENABLED=false，跳过白名单回填")
        print(json.dumps({"total": 0, "skipped_disabled": 1}, ensure_ascii=False, indent=2))
        return

    if not feishu.ensure_text_field(FIELD_WHITELIST):
        raise RuntimeError(
            f"飞书字段 {FIELD_WHITELIST!r} 不存在，且当前无法自动创建"
        )

    records = feishu.list_records()
    counters = {
        "total": len(records),
        "updated": 0,
        "skipped_existing": 0,
        "skipped_no_source": 0,
        "no_whitelist": 0,
        "failed": 0,
    }

    for index, record in enumerate(records, start=1):
        record_id = str(record.get("record_id") or "")
        fields = record.get("fields") or {}
        if not force and _record_value_to_text(fields.get(FIELD_WHITELIST)):
            counters["skipped_existing"] += 1
            continue

        subject = _record_value_to_text(fields.get(FIELD_SUBJECT))
        source = _historical_resume_source(
            feishu, record_id, fields,
            [FIELD_SUBJECT, FIELD_NAME, FIELD_COLLEGE, FIELD_BIRTHPLACE,
             FIELD_HOBBIES, FIELD_PROJECTS, FIELD_SUMMARY],
        )

        if not record_id or not source:
            counters["skipped_no_source"] += 1
            continue

        try:
            ai = analyze_resume(source, subject)
            value = "、".join(build_whitelist_items(ai))
            if not value:
                counters["no_whitelist"] += 1
                continue
            feishu.update_record_fields(record_id, {FIELD_WHITELIST: value})
            counters["updated"] += 1
            log.info(
                "白名单项回填 %d/%d：%s -> %s",
                index,
                len(records),
                record_id,
                value,
            )
        except Exception:
            counters["failed"] += 1
            log.exception("白名单项回填失败：%s", record_id)

    print(json.dumps(counters, ensure_ascii=False, indent=2))


def process_mail(
    message: Dict[str, Any],
    mail_client: ImapMailClient,
    feishu: FeishuClient,
    db: StateDB,
) -> Optional[Dict[str, str]]:
    message_id = message["id"]
    subject = message.get("subject") or ""
    sender = get_sender(message)
    received = message.get("receivedDateTime") or ""

    if not message.get("hasAttachments"):
        log.warning(red_text("跳过无简历附件：%s"), subject)
        return None

    files = extract_supported_files(mail_client, message_id)

    if not files:
        log.warning(red_text("跳过无简历附件（没有 PDF/DOCX/DOC）：%s"), subject)
        return None

    for index, (filename, data) in enumerate(files):
        identity = json.dumps([message_id, index, filename, hashlib.sha256(data).hexdigest()])
        attachment_key = hashlib.sha256(identity.encode()).hexdigest()
        ingest_resume_file(
            filename=filename,
            data=data,
            subject=subject,
            sender=sender,
            received=received,
            feishu=feishu,
            db=db,
            attachment_key=attachment_key,
        )

    if not sender:
        log.warning("飞书记录已创建，但原邮件没有可用发件人地址，跳过回信")
        return None

    return {
        "reply_key": f"message:{message_id}",
        "recipient": sender,
        "subject": subject,
        "original_message_id": str(message.get("internetMessageId") or ""),
    }


def upload_local_file(
    path: Path,
    feishu: FeishuClient,
    db: StateDB,
    reply_client: Optional[SmtpReplyClient] = None,
    sender: str = "",
    subject: str = "",
    received: str = "",
) -> str:
    """将本地简历按生产流程解析、入库，并按需排队回信。"""
    filename = safe_filename(path.name)
    if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"不支持的简历格式：{path.suffix or '(无扩展名)'}，支持 {supported}")

    sender = sender.strip()
    subject = subject.strip() or path.name
    received = received.strip() or datetime.now(timezone.utc).isoformat()
    record_id = ingest_resume_file(
        filename=filename,
        data=path.read_bytes(),
        subject=subject,
        sender=sender,
        received=received,
        feishu=feishu,
    )

    if reply_client and sender:
        recipient = parseaddr(sender)[1].strip()
        if recipient and recipient.casefold() != reply_client.username.casefold():
            db.queue_reply(
                reply_key=f"record:{record_id}",
                recipient=recipient,
                subject=subject,
            )
            deliver_pending_replies(reply_client, db)
        else:
            log.info("本地简历入库完成，但发件人地址无效或为本账号，跳过回信")
    elif sender and not reply_client:
        log.info("本地简历入库完成，但回信功能未启用，跳过回信")

    return record_id


def deliver_pending_replies(
    reply_client: Optional[SmtpReplyClient],
    db: StateDB,
) -> Dict[str, int]:
    """发送已完成飞书入库的回信队列，成功后才标记为已发送。"""
    counters = {"sent": 0, "failed": 0, "pending": 0}
    if reply_client is None:
        counters["pending"] = len(db.pending_replies())
        return counters

    for item in db.pending_replies():
        try:
            reply_client.send_reply(
                recipient=item["recipient"],
                subject=item["subject"],
                original_message_id=item["original_message_id"],
            )
        except Exception:
            counters["failed"] += 1
            log.exception("回信失败，下一轮继续重试：%s", item["recipient"])
            continue

        db.mark_reply_sent(item["reply_key"])
        counters["sent"] += 1
        log.info("回信发送成功：%s", item["recipient"])

    counters["pending"] = len(db.pending_replies())
    return counters


def backfill_replies(
    feishu: FeishuClient,
    reply_client: SmtpReplyClient,
    db: StateDB,
) -> None:
    """给迁移回信功能前已入库的记录发送一次回信。"""
    migration_key = "historical_replies_completed"
    if db.meta(migration_key) == "1":
        counters = deliver_pending_replies(reply_client, db)
        log.info("历史回信迁移已经执行过，本次只重试未成功的回信：%s", counters)
        print(json.dumps(counters, ensure_ascii=False, indent=2))
        return

    records = feishu.list_records()
    queued = 0
    skipped = 0
    for record in records:
        record_id = str(record.get("record_id") or "").strip()
        fields = record.get("fields") or {}
        recipient = parseaddr(
            _record_value_to_text(fields.get(FIELD_SENDER))
        )[1].strip()
        if not record_id or not recipient:
            skipped += 1
            continue
        if recipient.casefold() == reply_client.username.casefold():
            skipped += 1
            continue

        db.queue_reply(
            reply_key=f"record:{record_id}",
            recipient=recipient,
            subject=_record_value_to_text(fields.get(FIELD_SUBJECT)),
        )
        queued += 1

    # 先记录迁移已完成。发送失败的项目仍留在队列，下一次该命令只会重试它们，
    # 不会重新扫描并重复排队历史记录。
    db.set_meta(migration_key, "1")
    counters = deliver_pending_replies(reply_client, db)
    counters.update({"records": len(records), "queued": queued, "skipped": skipped})
    print(json.dumps(counters, ensure_ascii=False, indent=2))


def run_once(
    mail_client: ImapMailClient,
    feishu: FeishuClient,
    db: StateDB,
    reply_client: Optional[SmtpReplyClient] = None,
) -> None:
    message_ids = mail_client.message_ids()
    db.initialize_mailbox(mail_client.scope(), message_ids, mail_client.folder,
                          PROCESS_EXISTING_ON_FIRST_RUN)
    deliver_pending_replies(reply_client, db)
    messages = mail_client.messages(
        should_fetch=lambda message_id: not db.seen(message_id), message_ids=message_ids,
    )
    for message in messages:
        msg_id = message.get("id")

        if not msg_id or db.seen(msg_id):
            continue

        try:
            reply_request = process_mail(message, mail_client, feishu, db)
            if reply_request:
                db.queue_reply(**reply_request)
        except Exception:
            log.exception(
                "处理失败，保留为未处理，下次轮询自动重试：%r",
                message.get("subject"),
            )
            continue

        db.mark(msg_id)

    deliver_pending_replies(reply_client, db)


# ---------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------

def environment_report() -> Dict[str, Any]:
    report = {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "email_backend": "imap",
        "email_imap_host": os.getenv("EMAIL_IMAP_HOST", "imap.163.com"),
        "email_smtp_host": os.getenv("EMAIL_SMTP_HOST", "smtp.163.com"),
        "reply_enabled": REPLY_ENABLED,
        "email_username_configured": bool(os.getenv("EMAIL_USERNAME", "").strip()),
        "email_password_configured": bool(os.getenv("EMAIL_PASSWORD", "").strip()),
        "libreoffice": find_libreoffice(),
        "antiword": find_antiword(),
        "word_com_available": False,
    }

    if os.name == "nt":
        try:
            import win32com.client  # noqa
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            word.Quit()
            report["word_com_available"] = True
        except Exception:
            pass

    return report


def test_local_file(path: Path) -> None:
    data = path.read_bytes()
    text = resume_to_text(path.name, data)
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="只轮询一次后退出",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查 Linux/Windows 本地环境",
    )
    parser.add_argument(
        "--test-file",
        type=Path,
        help="仅测试本地 PDF/DOCX/DOC -> TXT，不连接邮箱/飞书",
    )
    parser.add_argument(
        "--upload-file",
        type=Path,
        help="解析并将本地 PDF/DOCX/DOC 简历创建为一条飞书记录",
    )
    parser.add_argument(
        "--upload-sender",
        default="",
        help="本地简历对应的候选人邮箱；填写后会在入库成功后发送回信",
    )
    parser.add_argument(
        "--upload-subject",
        default="",
        help="本地简历对应的邮件标题，默认使用文件名",
    )
    parser.add_argument(
        "--upload-received",
        default="",
        help="收件时间 ISO 格式，默认使用当前时间",
    )
    parser.add_argument(
        "--backfill-birthplace",
        action="store_true",
        help="重新解析飞书历史记录并回填生源地",
    )
    parser.add_argument(
        "--force-birthplace",
        action="store_true",
        help="回填时覆盖已有生源地；默认只填空字段",
    )
    parser.add_argument(
        "--backfill-student-info",
        action="store_true",
        help="重新解析飞书历史记录并回填学号、性别",
    )
    parser.add_argument(
        "--force-student-info",
        action="store_true",
        help="回填时覆盖已有学号、性别；默认只填空字段",
    )
    parser.add_argument(
        "--backfill-replies",
        action="store_true",
        help="给回信功能接入前已入库的历史记录发送一次报名回信",
    )
    parser.add_argument(
        "--backfill-whitelist",
        action="store_true",
        help="使用模型重新解析历史简历，只回填白名单项，不发送回信",
    )
    parser.add_argument(
        "--force-whitelist",
        action="store_true",
        help="回填时覆盖已有白名单项；默认只填空字段",
    )
    parser.add_argument(
        "--adopt-legacy-state", action="store_true",
        help="确认旧简历库属于当前邮箱，备份并绑定 UID 作用域后退出；不上传或回信",
    )
    args = parser.parse_args()

    if args.check:
        print(json.dumps(
            environment_report(),
            ensure_ascii=False,
            indent=2,
        ))
        return

    if args.test_file:
        test_local_file(args.test_file)
        return

    if Path(STATE_DB_PATH).resolve() == Path(os.getenv("HOMEWORK_STATE_DB_PATH", "homework_pipeline.db")).resolve():
        raise RuntimeError("简历与作业不能使用同一个状态数据库")
    with instance_lock(STATE_DB_PATH):
        _run_stateful(args)


def _run_stateful(args: argparse.Namespace) -> None:
    if args.adopt_legacy_state:
        mail = ImapMailClient()
        try:
            db = StateDB(STATE_DB_PATH)
            try:
                backup_path = str(Path(STATE_DB_PATH).resolve()) + "-legacy-" + uuid.uuid4().hex + ".db"
                with sqlite3.connect(backup_path) as backup:
                    db.conn.backup(backup)
                ids = mail.message_ids()
                db.initialize_mailbox(mail.scope(), ids, mail.folder,
                                      PROCESS_EXISTING_ON_FIRST_RUN, adopt_legacy=True)
                log.info("旧状态迁移完成，备份：%s；未上传或发送邮件", backup_path)
            finally:
                db.conn.close()
        finally:
            mail.close()
        return

    if args.upload_file:
        feishu = FeishuClient()
        db = StateDB(STATE_DB_PATH)
        sender = args.upload_sender.strip()
        reply_client = SmtpReplyClient() if REPLY_ENABLED and sender else None
        record_id = upload_local_file(
            path=args.upload_file,
            feishu=feishu,
            db=db,
            reply_client=reply_client,
            sender=sender,
            subject=args.upload_subject,
            received=args.upload_received,
        )
        print(json.dumps({"record_id": record_id}, ensure_ascii=False, indent=2))
        return

    if args.backfill_birthplace:
        feishu = FeishuClient()
        backfill_birthplace(
            feishu,
            force=args.force_birthplace,
        )
        return

    if args.backfill_student_info:
        feishu = FeishuClient()
        backfill_student_info(
            feishu,
            force=args.force_student_info,
        )
        return

    if args.backfill_replies:
        feishu = FeishuClient()
        reply_client = SmtpReplyClient()
        db = StateDB(STATE_DB_PATH)
        backfill_replies(feishu, reply_client, db)
        return

    if args.backfill_whitelist:
        # 这里刻意不初始化 ImapMailClient、SmtpReplyClient 或回信队列。
        feishu = FeishuClient()
        backfill_whitelist(feishu, force=args.force_whitelist)
        return

    mail_client = ImapMailClient()
    try:
        feishu = FeishuClient()
        db = StateDB(STATE_DB_PATH)
        reply_client = SmtpReplyClient() if REPLY_ENABLED else None

        if reply_client is None:
            log.info("REPLY_ENABLED=false，成功入库后不发送回信")

        if args.once:
            run_once(mail_client, feishu, db, reply_client)
            return

        log.info(
            "启动 IMAP 轮询 | OS=%s | interval=%ss",
            platform.system(),
            POLL_INTERVAL_SECONDS,
        )

        while True:
            try:
                run_once(mail_client, feishu, db, reply_client)
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("主循环异常")

            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        mail_client.close()


if __name__ == "__main__":
    main()
