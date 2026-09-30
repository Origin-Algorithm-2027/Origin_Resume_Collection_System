# 华东理工大学简历与作业自动收集系统

基于 Python 的邮件附件收集工具，用于招新报名和培训作业管理。实现简历附件自动收取、本地文本提取、SiliconFlow 智能解析、飞书多维表格同步，以及作业按周归档、格式过滤和无法入库邮件自动转发。

项目包含两个独立运行的主程序：**`resume_pipeline.py` 负责简历报名，`homework_pipeline.py` 负责作业收集**。两者共用邮箱和飞书配置，使用不同的本地状态数据库。两个入口互不导入，通过 `pipeline_common.py` 复用基础通信功能。

支持 Python 3.10+、Linux 和 Windows。文档转换在本机完成，不依赖 Make、PDF.co 或 AnyCross；简历结构化分析会将提取的文本发送至 SiliconFlow，原始附件会上传到飞书。

## 目录

- [1. 两个主程序的作用](#1-两个主程序的作用)
- [2. 目录结构与安装](#2-目录结构与安装)
- [3. 首次配置流程](#3-首次配置流程)
- [4. env 参数完整说明](#4-env-参数完整说明)
- [5. 简历程序的运行方法](#5-简历程序的运行方法)
- [6. 作业程序的运行方法](#6-作业程序的运行方法)
- [7. 两个程序如何配合](#7-两个程序如何配合)
- [8. 常驻运行与日志](#8-常驻运行与日志)
- [9. 常见问题](#9-常见问题)
- [10. 测试与提交仓库](#10-测试与提交仓库)

## 1. 两个主程序的作用

### 1.1 简历收集：resume_pipeline.py

用于把报名邮件中的简历转换为飞书中的候选人记录。

```text
IMAP 收件箱 / 指定本地简历
  → 筛选 PDF、DOCX、DOC
  → 本机提取文本
  → SiliconFlow 提取姓名、QQ、学院、技能、项目经历等
  → 上传原始简历到飞书
  → 新建候选人记录
  → 可选：SMTP 发送固定报名回信
```

主要功能：

- 读取 `.pdf`、`.docx`、`.doc` 附件。PDF 使用 PyMuPDF，DOCX 直接解析 XML，旧版 DOC 使用本机转换工具。
- 每份简历调用一次模型获取结构化结果；一封邮件有多份支持格式的附件时，分别创建记录。
- 写入邮件元数据、原始附件和候选人信息，支持意向组别、技术栈、报名原因、兴趣爱好及自定义白名单项。
- 使用 SQLite 记录已处理邮件；报名回信单独排队，SMTP 失败不会因回信重试而重新创建候选人记录。
- 提供本地解析测试、本地文件入库，以及历史字段和报名回信回填命令。

**默认每轮完成后等待60秒；首次识别邮箱作用域时默认跳过全部现有邮件。** 简历解析或入库失败会保留邮件供后续轮询重试。这与作业程序的“失败后只转发”策略不同。

### 1.2 作业收集：homework_pipeline.py

用于把已有报名人员发送的作业附件归档到其候选人记录中。

```text
IMAP 邮件
  → 优先根据邮件发件人匹配现有飞书候选人
  → 邮箱未匹配时，从邮件主题/附件文件名匹配唯一姓名及记录
  ├─ 匹配成功
  │   → 过滤 Word/PDF 附件
  │   → 按邮件主题、附件文件名、收件时间依次确定周次
  │   → 将其他附件追加至“第N周”列
  │   → 单封处理失败则转发人工处理，不再尝试入库
  └─ 无法匹配
      → 使用同一邮箱转发完整原邮件
      → 不再重新匹配或尝试入库
```

主要功能：

- 不调用模型、不解析附件正文、不创建候选人记录，复用简历表中已有的“邮件发件人”和“姓名”字段。
- 启动时检查或创建至少“第1周”至“第6周”附件列，后续按收到的邮件自动扩展。
- 优先按邮件主题明确的周次归档，其次按各附件文件名；均不明确时按服务器收件时间兜底，使用北京时间、**每周二00:00切换周次**。
- 固定过滤 `.word`、`.doc`、`.docx`、`.pdf`，其他扩展名原样上传；同周多次提交追加保留。
- 无法匹配或单封邮件入库失败时，将完整原邮件作为 `original.eml` 转发给配置的接收人；停止该邮件的入库尝试。
- 被过滤、无法匹配、入库失败和转发失败的日志以红色显示。

**默认启动后立即检查一轮，每轮完成后等待15分钟；首次默认补收2026年9月21日起的邮件。** 转发失败只重试原邮件下载或 SMTP 发送，不重试飞书入库。

### 1.3 行为对照

| 项目 | 简历程序 | 作业程序 |
|---|---|---|
| 飞书操作 | 新增候选人记录 | 更新已有记录的“第N周”列 |
| 文件格式 | 仅 PDF、DOCX、DOC | 排除 WORD、DOC、DOCX、PDF |
| 模型调用 | 使用 SiliconFlow | 不调用模型 |
| 默认检查间隔 | 60秒 | 900秒，即15分钟 |
| 默认历史邮件策略 | 首次跳过当前邮箱全部旧邮件 | 首次补收起始日期以来邮件 |
| 邮件发送 | 给报名者发送固定回信，可关闭 | 无法入库时转发给指定接收人 |
| 单封入库失败 | 后续轮询重试 | 只转发，不再尝试入库 |
| 状态文件 | `resume_pipeline.db` | `homework_pipeline.db` |

## 2. 目录结构与安装

```text
Resume_collection_system/
├── resume_pipeline.py                 # 简历报名、解析、入库和回信
├── homework_pipeline.py               # 作业收集、按周归档和异常转发
├── pipeline_common.py                 # 无业务规则的 IMAP、SMTP 和飞书通信
├── CODE_REVIEW.md                     # 代码审查结果与已知限制
├── .env.example                       # 可公开的配置模板
├── .env                               # 本地真实配置，不提交
├── requirements.txt                   # Python 依赖
├── install_linux.sh                   # Linux 安装辅助脚本
├── install_windows.ps1                # Windows 安装辅助脚本
├── resume-pipeline.service.example    # systemd 服务示例
├── test/                              # 模拟测试和独立回信测试工具
└── README.md
```

运行生成的 `.venv/`、`__pycache__/`、SQLite 数据库、锁文件及备份不是源代码。

依赖关系为 `resume_pipeline.py → pipeline_common.py ← homework_pipeline.py`：

- `pipeline_common.py` 只提供 IMAP 连接/原文读取、SMTP 单次发送、飞书认证/分页/文件传输等基础功能；不加载 `.env`、不创建数据库、不决定处理哪些邮件。
- 简历入口保留文档解析、模型分析、候选人建档、历史回填和报名回信队列。
- 作业入口保留附件过滤、周次计算、已有记录匹配、附件追加、失败转发队列和进程锁。它不调用简历模型或报名回信逻辑。
- 两个入口分别加载配置、运行轮询、管理状态；业务数据库结构和失败处理策略保持各自独立。部署任一入口时，都要一同部署 `pipeline_common.py`。

`test/reply_service.py` 是刻意隔离的邮箱测试工具，其少量通信代码用于单独验证邮箱配置；它不是第三个生产入口。审查范围、验证结果和已知限制见 [代码审查记录](CODE_REVIEW.md)。

### 2.1 Linux

在项目目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

如需解析旧版 `.doc`，Ubuntu/Debian 可安装：

```bash
sudo apt update
sudo apt install antiword libreoffice
```

### 2.2 Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

也可以不激活虚拟环境，直接调用 `.\.venv\Scripts\python.exe`。解析 `.doc` 时安装 LibreOffice；已安装 Microsoft Word 的机器可通过 Windows 专用依赖 `pywin32` 调用 Word COM。

### 2.3 文档转换依赖

| 格式 | 实现方式 | 需要额外安装 Office 吗 |
|---|---|---|
| PDF | PyMuPDF 提取已有文本层 | 不需要；不支持 OCR |
| DOCX | Python 解包 ZIP，读取 Word XML | 不需要 |
| DOC | antiword、LibreOffice 或 Word COM | 至少需要一个可用后端 |

`DOC_BACKEND=auto` 时，Linux 依次尝试 antiword、LibreOffice；Windows 依次尝试 LibreOffice、Microsoft Word COM。

作业脚本不执行文档转换，也不导入简历脚本；单独运行时只需要 `requests` 和 `python-dotenv`。同时部署两个业务时，按上面的方式安装完整 `requirements.txt` 即可。

## 3. 首次配置流程

### 3.1 创建本地 .env

仅在 `.env` 尚不存在时复制，避免覆盖已经配置的凭据。

Linux：

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
```

Windows PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

然后用文本编辑器打开 `.env`。不要把真实密码、授权码或 API Key 填进 `.env.example`。

### 3.2 配置邮箱

本项目针对163邮箱实现并验证了 IMAP 登录及 IMAP ID 流程。

1. 登录163网页邮箱，在账户设置中找到 POP3/SMTP/IMAP 服务管理。
2. 启用 IMAP 和 SMTP，按页面要求完成验证并生成**客户端授权码**。
3. `EMAIL_USERNAME` 填完整邮箱地址；`EMAIL_PASSWORD` 填客户端授权码，而非网页登录密码。
4. 保留默认服务器及 SSL 端口，文件夹使用 `INBOX`。

```dotenv
EMAIL_IMAP_HOST=imap.163.com
EMAIL_IMAP_PORT=993
EMAIL_SMTP_HOST=smtp.163.com
EMAIL_SMTP_PORT=465
EMAIL_USERNAME=your_name@163.com
EMAIL_PASSWORD=你的客户端授权码
EMAIL_FOLDER=INBOX
```

代码使用 IMAP SSL 和 SMTP SSL，不自动切换到 STARTTLS；其他邮箱服务不能仅凭更换域名就保证兼容，尤其要核对 IMAP ID、认证方式及端口。

### 3.3 配置 SiliconFlow

只有简历分析及模型回填需要此服务。

1. 在 SiliconFlow 控制台创建 API Key，填入 `SILICONFLOW_API_KEY`。
2. 从账号可访问的模型列表复制完整模型 ID，填入 `SILICONFLOW_MODEL`。
3. 模型需兼容项目使用的 Chat Completions 请求及 JSON 输出。模板中的模型 ID 是示例，不保证所有账号均可使用。

密钥创建步骤可参考 [SiliconFlow 快速上手](https://docs.siliconflow.cn/docs/userguide/quickstart)，请求参数可参考 [Chat Completions 文档](https://docs.siliconflow.cn/docs/api/chat-completions-post)。

```dotenv
SILICONFLOW_API_KEY=你的API密钥
SILICONFLOW_MODEL=你账号可用的完整模型ID
SILICONFLOW_MAX_TOKENS=2500
SILICONFLOW_SEND_ENABLE_THINKING=true
```

仅运行作业脚本时，不需要可用的 SiliconFlow 密钥，也不会产生模型调用。

### 3.4 配置飞书应用和目标表

需要的是**飞书多维表格**，不是普通电子表格。

1. 在飞书开放平台创建或选择企业自建应用，从应用凭证页取得 App ID、App Secret。
2. 准备目标多维表格及其中的数据表，取得多维表格 `app_token` 和数据表 `table_id`。
3. 为应用开通读取记录、新增/更新记录、读取/创建字段及上传素材所需权限，并按平台流程发布生效。
4. 给应用授予目标多维表格的访问和编辑权限。应用开通接口权限与应用能访问具体文档是两个条件。
5. 按下一节字段映射表建立基础列，确保列名和类型与配置一致。

普通多维表格链接通常类似：

```text
https://你的租户.feishu.cn/base/多维表格token?table=tbl数据表ID&view=视图ID
```

`/base/` 后的标识对应 `FEISHU_BITABLE_APP_TOKEN`，`table=` 对应 `FEISHU_TABLE_ID`；`view` 不需要填写。知识库链接中的 `/wiki/` 标识不能直接当作多维表格 token，需先取得其对应的多维表格标识。

```dotenv
FEISHU_APP_ID=你的应用AppID
FEISHU_APP_SECRET=你的应用AppSecret
FEISHU_BITABLE_APP_TOKEN=你的多维表格app_token
FEISHU_TABLE_ID=你的数据表table_id
```

不要混淆应用 App ID、目标多维表格 `app_token` 和临时访问令牌。程序会自行获取 `tenant_access_token`，不需要手动填写临时令牌。接口权限以飞书开放平台当前的 [创建字段](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-field/create)、[新增记录](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create) 和 [上传素材](https://open.feishu.cn/document/server-docs/docs/drive-v1/media/upload_all) 文档为准。

### 3.5 推荐的首次验证顺序

```bash
# 本地检查，不收取邮件
python resume_pipeline.py --check
python resume_pipeline.py --test-file ./sample.docx

# 只检查邮箱 DNS/TLS，不登录，不连接飞书
python homework_pipeline.py --check-imap

# 创建/检查作业周次列，会修改飞书字段
python homework_pipeline.py --init-fields
```

先用合成简历和专用测试表验证需要的流程。配置好历史邮件策略后，再运行对应脚本的 `--once`；它会执行真实业务，不是预览模式。

## 4. env 参数完整说明

本节覆盖 `.env.example` 的全部参数，包括模板中保留但当前未使用的历史参数。

### 4.1 通用填写规则

- 一行一个 `变量名=值`，不在等号两侧添加不必要的空格。
- 布尔值建议统一写 `true` / `false`。代码也接受 `1`、`yes`、`y` 表示开启，比较时忽略大小写；不要写“是”“否”或带额外空格的布尔值。
- 长提示词、包含空格或 `#` 的值建议加引号。Windows 路径可使用正斜杠，例如 `C:/Program Files/LibreOffice/program/soffice.exe`。
- 项目通过 `python-dotenv` 加载 `.env`。已经存在的进程环境变量优先，`.env` 默认不会覆盖它们。
- 多数配置在启动或导入时读取。修改 `.env` 后，停止并重新启动脚本才会完整生效。
- 路径可为绝对路径或相对路径；数据库相对路径以进程工作目录为基准。建议始终从项目根目录运行。
- 表格中的“默认值”指代码未获得该环境变量时的值；“必填”表示相应功能启用时不能留空。模板里的占位文字不是真实凭据。

### 4.2 邮箱连接与邮件发送：两个脚本共用

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `EMAIL_IMAP_HOST` | `imap.163.com` | 收件服务器域名，163邮箱保留默认值。影响两个程序的 IMAP 连接。 |
| `EMAIL_IMAP_PORT` | `993` | IMAP SSL 端口，填写整数。不要填明文 IMAP 端口来替代 SSL。 |
| `EMAIL_SMTP_HOST` | `smtp.163.com` | 发送服务器域名，用于简历报名回信和作业异常转发。 |
| `EMAIL_SMTP_PORT` | `465` | SMTP SSL 端口，填写整数。当前发送代码不支持通过此项切换 STARTTLS。 |
| `EMAIL_USERNAME` | 必填 | 完整邮箱地址，例如 `your_name@163.com`；同时用于收信、SMTP 登录和转发发件地址。 |
| `EMAIL_PASSWORD` | 必填 | 对应邮箱的客户端授权码；不要填网页登录密码，不要提交仓库。 |
| `EMAIL_FOLDER` | `INBOX` | IMAP 文件夹名称，默认收件箱。不是本机下载目录；留空也会回退为 `INBOX`。 |
| `REPLY_ENABLED` | `true` | 仅控制简历正常入库后的固定报名回信。设为 `false` 可关闭；**不控制作业异常转发**。显式执行 `--backfill-replies` 仍会发送历史回信。 |
| `EMAIL_PAGE_SIZE` | `50` | 填正整数。仅控制批次大小，不限制每轮扫描总数。简历按 UID 从老到新完整遍历，每批最多缓存这么多封正文；作业完整扫描起始日期以来的 UID。 |

固定报名回信的正文在 `resume_pipeline.py` 的 `REPLY_BODY` 中，包含培训群信息；不是环境变量。部署到其他团队时，应检查并按实际情况修改该正文。

### 4.3 简历轮询与状态

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | 简历程序每轮结束后的等待秒数，填写正整数；不影响作业程序。 |
| `PROCESS_EXISTING_ON_FIRST_RUN` | `false` | 简历首次识别邮箱作用域时是否处理已有邮件。`false` 原子标记当前全部 UID 为已见，不下载正文；`true` 处理全部未处理邮件。更换账号或 UIDVALIDITY 改变后按新作用域再次应用此策略。 |
| `STATE_DB_PATH` | `resume_pipeline.db` | 简历 SQLite 状态库，保存带邮箱作用域的已见邮件、逐附件入库进度、元数据和报名回信队列。请使用可写路径，保持长期稳定，与作业库分开。 |

首次运行开关只在对应邮箱作用域尚未初始化时起作用。已有数据库里标记为已处理的邮件，不会因为改成 `true` 而再次导入。不要为了测试随意删除生产状态库，否则可能重复创建记录或发送回信。

### 4.4 本地简历文本提取

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `MIN_EXTRACTED_CHARS` | `80` | 文本提取有效性的最少字符数，填写正整数。低于此值视为提取不足；降低它不会为扫描 PDF 增加 OCR 能力。 |
| `DOC_BACKEND` | `auto` | `.doc` 解析方式：`auto` 自动尝试；`libreoffice` 只用 LibreOffice；`antiword` 只用 antiword；`word` 只用 Microsoft Word COM，后者仅适用于 Windows。 |
| `LIBREOFFICE_PATH` | 空，自动寻找 | `soffice` 可执行文件路径，不是安装目录。Linux 如 `/usr/bin/libreoffice`；Windows 如 `C:/Program Files/LibreOffice/program/soffice.exe`。自动检测成功时留空即可。 |
| `ANTIWORD_PATH` | 空，自动寻找 | `antiword` 可执行文件路径，例如 `/usr/bin/antiword`。仅影响使用该后端的 DOC 转换。 |

这些参数只影响简历提取，不改变作业的扩展名过滤规则。

### 4.5 SiliconFlow 模型参数

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `SILICONFLOW_API_KEY` | 分析简历时必填 | SiliconFlow 控制台生成的 API Key；仅用于请求认证。 |
| `SILICONFLOW_MODEL` | 分析简历时必填 | 完整模型 ID。模板为 `Pro/zai-org/GLM-5.1`，使用前请核对账号访问权限、模型可用性及请求参数兼容性。 |
| `SILICONFLOW_MAX_TOKENS` | `3500`；模板设为 `2500` | 单次模型输出的最大 token 数，填写模型支持范围内的正整数。复制模板运行时使用2500；完全不配置时才回退3500。输出被截断时可适当提高。 |
| `SILICONFLOW_SEND_ENABLE_THINKING` | `true` | 控制**是否发送** `enable_thinking` 参数。`true` 时程序发送的是 `enable_thinking=false`，尝试关闭模型思考；`false` 时省略该参数。它不是“开启思考”的开关。模型不支持此参数时可设为 `false`。 |
| `MAX_RESUME_CHARS` | `60000` | 发送给模型的简历正文最多保留多少字符，超过则截断。是输入字符数，不是输出 token 数，也不限制飞书原始附件大小。 |

请求使用非流式 Chat Completions、JSON 输出格式和固定温度 `0.1`，这些目前不是 `.env` 配置项。程序从模型读取姓名、QQ、意向组别、技术栈、学院、生源地、学号、性别、专业、项目经历、报名原因、兴趣爱好、摘要和白名单项；实际写入列以下面的字段表为准。

### 4.6 飞书连接：两个脚本共用

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `FEISHU_APP_ID` | 必填 | 自建应用 App ID，从应用凭证页复制。不是数据表 ID。 |
| `FEISHU_APP_SECRET` | 必填 | 自建应用 App Secret，与 App ID 配套，用于换取应用访问令牌。 |
| `FEISHU_BITABLE_APP_TOKEN` | 必填 | 目标多维表格的 `app_token`，不是临时访问令牌或普通电子表格 token。 |
| `FEISHU_TABLE_ID` | 必填 | 多维表格内具体数据表的 ID，通常以 `tbl` 开头；两个脚本应指向同一张候选人表。 |
| `RECEIVED_TIME_MODE` | `timestamp` | 简历“收件时间”列的写入格式。日期列使用 `timestamp`，写入 Unix 毫秒；文本列使用 `text`，写入 ISO 时间字符串。不影响作业周次计算。 |

### 4.7 当前生效的飞书字段映射

这些值是**飞书列名**，不是字段 ID。若列名为“报名邮箱”，应填写 `FIELD_SENDER=报名邮箱`，无需改代码。

基础列需事先建立。自动建列只覆盖下表注明的可选文本列、技术栈及作业周次列；程序不会自动补齐整张候选人表。

| 参数 | 默认列名 | 列类型及用途 |
|---|---|---|
| `FIELD_RECEIVED_TIME` | 收件时间 | 日期或文本，与 `RECEIVED_TIME_MODE` 一致。简历程序使用邮件 `Date` 解析结果，缺失或无效时回退当前时间；不是作业使用的 `INTERNALDATE`。 |
| `FIELD_SENDER` | 邮件发件人 | 文本。保存邮件联系地址，也是作业匹配的依据；简历优先取非空 `Reply-To`，否则取 `From`，作业优先取有效的 `Reply-To`。 |
| `FIELD_SUBJECT` | 邮件标题 | 文本，原邮件标题。 |
| `FIELD_ATTACHMENT` | 原始简历 | 附件，保存简历原文件；不用于作业附件。 |
| `FIELD_NAME` | 姓名 | 文本，候选人姓名；同时用于作业在邮箱未匹配时的姓名兜底。填写飞书实际姓名列名。 |
| `FIELD_QQ` | QQ号 | 文本，避免号码被数字格式改变。 |
| `FIELD_INTENTION_GROUP` | 意向组别 | 多选，预先建立允许的组别及“未定义”等选项。 |
| `FIELD_SKILLS` | 技术栈 | 多选，候选人最多保留5个技能；可自动建列及添加新选项。 |
| `FIELD_COLLEGE` | 学院 | 文本，简历中的学院信息。 |
| `FIELD_MAJOR` | 专业 | 仅在生源地/学号/性别历史回填无法读取原件时，将已有专业列作为分析输入。可填已有列名；不会自动建列或写入独立专业列。 |
| `FIELD_BIRTHPLACE` | 生源地 | 文本，只提取明确的生源地、籍贯等；可自动建列。 |
| `FIELD_STUDENT_ID` | 学号 | 文本，只提取明确标注的学号，保留前导零；可自动建列。 |
| `FIELD_GENDER` | 性别 | 文本，只提取明确内容，不根据姓名推断；可自动建列。 |
| `FIELD_PROJECTS` | 项目经历 | 多行文本，整理后的项目内容。 |
| `FIELD_APPLICATION_REASON` | 报名原因 | 文本，只使用明确内容，去除换行/Markdown并限制为最多49字符；可自动建列。 |
| `FIELD_HOBBIES` | 兴趣爱好 | 文本，只使用明确内容，去除换行/Markdown并限制为最多49字符；可自动建列。 |
| `FIELD_WHITELIST` | 白名单项 | 文本，存放按白名单提示词提取的正向特点；启用时可自动建列。 |
| `FIELD_SUMMARY` | 简历摘要 | 多行文本，模型整理的候选人摘要。 |
| `FIELD_STATUS` | 招聘状态 | 建议单选，预建 `DEFAULT_STATUS` 对应选项；也可使用接受字符串的文本列。程序写入固定初始状态。 |

作业列名固定为“第1周”“第2周”等，类型必须是附件，不能通过 `FIELD_ATTACHMENT` 改名。已存在同名非附件列时，作业程序会报错，避免错误覆盖。

### 4.8 组别、技术栈与自动建列

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `INTENTION_GROUP_OPTIONS` | `导航,自瞄,机械,软件,硬件,运宣,电控` | 允许的组别名称，用英文逗号分隔；飞书“意向组别”列应预建相同选项。 |
| `INTENTION_GROUP_UNKNOWN` | `未定义` | 模型返回未支持组别时的兜底值；飞书也应预建该选项。 |
| `AUTO_CREATE_SKILLS_FIELD` | `true` | 技术栈列不存在时是否尝试创建多选列；`false` 时不自动建列。 |
| `AUTO_ADD_SKILL_OPTIONS` | `true` | 技术栈出现新技能时是否向多选列追加选项；关闭时仅写已有选项。 |
| `AUTO_CREATE_SHORT_TEXT_FIELDS` | `true` | 是否自动创建生源地、学号、性别、报名原因、兴趣爱好及启用的白名单文本列；不适用于所有 `FIELD_*`，也不控制作业周次列。 |
| `DEFAULT_STATUS` | `待初筛` | 新增简历记录的招聘状态字符串。使用单选列时，需预建这个选项。 |

现有组别别名在代码中固定映射：嵌入式 → 硬件、电控；算法 → 导航、自瞄；视觉 → 自瞄。程序还会核对飞书列中的实际选项，不存在的选项不会直接写入。更改组别配置时应同步维护飞书选项。

可选字段创建失败时，简历程序通常记录警告并跳过该字段；基础字段不存在或类型不符仍可能导致整条记录创建失败。

### 4.9 白名单提示词

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `WHITELIST_ENABLED` | `true` | 是否写入白名单项。`false` 时正常入库不写此列，显式执行 `--backfill-whitelist` 也会跳过；需回填时先开启。 |
| `WHITELIST_RULES` | 上海生源、骑车/战雷爱好、看过比赛等规则 | 整段原样加入模型提示词，使用引号包裹。仅根据简历明确事实提取，默认无证据不填写。修改后重启；已有记录不会自动重算。 |

自定义示例：

```dotenv
WHITELIST_RULES="只根据简历明确事实提取正向特点：明确参加过机器人比赛，输出‘机器人比赛经历’；明确有嵌入式项目，输出‘嵌入式项目经历’。无明确证据时不输出，不作推断。"
```

这是模型辅助标注，不会自动录取、拒绝或删除候选人。需要重算历史列时使用 `--backfill-whitelist`，覆盖已有内容需额外加 `--force-whitelist`。

### 4.10 模板中保留、当前未生效的历史参数

以下参数仍出现在 `.env.example`，但两个主脚本**没有读取它们，也不会按它们创建或写入列**。保留或删除这些配置均不改变当前行为。

| 参数 | 模板值 | 当前情况 |
|---|---|---|
| `FIELD_PHONE` | 手机号 | 未读取、未写入独立手机号列。 |
| `FIELD_EMAIL` | 邮箱 | 未读取；作业匹配使用 `FIELD_SENDER`，不是本参数。 |
| `FIELD_SCHOOL` | 学校 | 未读取、未写入独立学校列。 |
| `FIELD_DEGREE` | 学历 | 未读取、未写入独立学历列。 |
| `FIELD_GRADUATION` | 毕业时间 | 未读取、未写入独立毕业时间列。 |
| `FIELD_POSITION` | 应聘岗位 | 未读取、未写入独立岗位列。 |
| `FIELD_EXPERIENCE` | 工作经历 | 未读取、未写入独立工作经历列。 |

如需这些独立列，需要修改简历分析输出及飞书写入逻辑，仅配置 `.env` 不会启用。

### 4.11 作业专用配置

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `HOMEWORK_START_DATE` | `2026-09-21` | 收取作业的最早日期，格式 `YYYY-MM-DD`。同时决定周次锚点；按北京时间判断，更早邮件跳过。改变此项需为新任务使用独立状态库。 |
| `HOMEWORK_INITIAL_WEEKS` | `6` | 启动时确保存在的周次列数量，必须是大于等于6的整数。这不是收取周数的上限。 |
| `HOMEWORK_PROCESS_EXISTING_ON_FIRST_RUN` | `true` | 作业状态库首次识别当前邮箱 UIDVALIDITY 时是否补收日期范围内的旧邮件。`false` 仅初始化时跳过已有 UID，之后正常处理新邮件。 |
| `HOMEWORK_STATE_DB_PATH` | `homework_pipeline.db` | 保存作业邮件状态、附件 token、逐行写入进度、转发队列等；不得与 `STATE_DB_PATH` 指向同一路径。 |
| `HOMEWORK_POLL_INTERVAL_SECONDS` | `900` | 作业每轮完成后的等待秒数，默认15分钟；启动立即执行首轮，`--once` 不等待。填写正整数，不受 `POLL_INTERVAL_SECONDS` 影响。 |
| `HOMEWORK_UNMATCHED_FORWARD_TO` | `3464144391@qq.com` | 无法匹配或单封入库失败时的接收邮箱。虽然名称包含 UNMATCHED，现也处理入库失败。仅配置一个完整地址，不能与收件账号相同；正常业务中没有单独关闭转发的开关。 |

周二分界、Word/PDF 排除列表、转发邮件格式在代码中固定实现，没有对应的 `.env` 开关。

作业库会绑定邮箱主机/端口、账号、文件夹、飞书表格、起始日期和发件人列名。更换这些配置时使用新的状态库。单纯修改检查间隔或 SMTP 设置无需更换数据库。

### 4.12 日志

| 参数 | 代码默认值 | 作用与配置方法 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 两个脚本的日志级别，建议日常 `INFO`，排错时临时 `DEBUG`；也可使用 `WARNING`、`ERROR`、`CRITICAL`。提高到 `ERROR` 会隐藏被过滤等 WARNING 级别提示。 |

日志默认输出到终端，不自动轮转或保存为文件。作业脚本的 WARNING 及以上日志使用 ANSI 红色，支持颜色的终端会显示红色；重定向到文件时可能保留颜色转义字符。

## 5. 简历程序的运行方法

以下命令中的 `python` 表示已激活的虚拟环境。Linux 不激活时替换为 `.venv/bin/python`，Windows 替换为 `.\.venv\Scripts\python.exe`。

### 5.1 日常命令

| 命令 | 作用及外部访问 |
|---|---|
| `python resume_pipeline.py --help` | 查看命令帮助，不执行业务。 |
| `python resume_pipeline.py --check` | 检查本机转换工具和配置是否存在；Windows 可能启动 Word COM 自检，不联系业务服务。 |
| `python resume_pipeline.py --test-file ./sample.docx` | 仅提取本地文本并输出；不联系邮箱、模型或飞书。 |
| `python resume_pipeline.py --once` | 一轮真实邮箱处理，可能调用模型、写飞书并发送报名回信。 |
| `python resume_pipeline.py` | 持续运行，按简历检查间隔轮询；按 `Ctrl+C` 停止。 |

简历使用服务器、端口、账号、文件夹、`UIDVALIDITY` 共同确定邮箱作用域；重连后作用域改变会结束当前快照，下轮重新初始化。每轮遍历全部未处理 UID，单封邮件下载或业务处理失败会留待下轮，后续邮件仍继续处理。

同一邮件的每个简历附件都有独立进度：保存分析结果、上传令牌、创建请求正文、UUID 请求标识和最终记录 ID。后续附件失败不会重建先前成功的记录。创建请求响应丢失时，先在飞书按原上传令牌核对；已存在则补记本地成功，尚未查到则使用原请求正文和同一个 `client_token` 重试。核对接口失败或匹配到多条记录时停止创建并记录错误。

简历和作业都对各自状态库加进程锁，不能同时启动两个使用同一库的实例。不同数据库无法相互去重，请勿为同一业务启动多个独立状态库。

#### 旧版简历状态库迁移

旧版只保存 `imap:文件夹:UID`，没有账号或 `UIDVALIDITY`，程序无法证明其归属。检测到这种旧库时，正常轮询会提示先迁移，**不会静默绑定或重新导入**。

停止旧简历进程，确认 `.env` 仍指向原邮箱、原文件夹，且邮箱没有重建或重置 UID 后执行：

```bash
.venv/bin/python resume_pipeline.py --adopt-legacy-state
.venv/bin/python resume_pipeline.py
```

迁移命令登录 IMAP 取得 UID 快照，自动生成 `状态库路径-legacy-随机标识.db` 备份，再在事务中建立作用域映射；不下载邮件正文、不访问飞书或模型、不发送邮件。旧的已见记录和回信队列保留不动。

默认 `PROCESS_EXISTING_ON_FIRST_RUN=false` 下，已记录的旧 UID 沿用已见状态；早于旧库最小已见 UID 的未知历史邮件继续跳过，以免把旧版未扫描的历史报名突然导入。最小已见 UID 之后的遗漏、失败和新增邮件会在日常轮询中处理。设为 `true` 迁移则允许处理全部未记录 UID，执行前需要确认历史范围。旧库若没有任何已见 UID，默认跳过当前快照。

更换过账号或邮箱已重建时，不应执行旧库认领；请保留原库，为新邮箱配置新的 `STATE_DB_PATH`。新版本开始记录的邮箱作用域能够区分 UID 重用，但不能反推旧版未保存的信息。升级前已产生的重复记录、以及旧版部分成功但未记账的邮件，需要先核对，迁移不会自动删除或合并飞书记录。

### 5.2 本地文件入库

```bash
python resume_pipeline.py --upload-file ./sample.docx --upload-sender candidate@example.com --upload-subject "报名简历" --upload-received "2026-09-21T10:00:00+08:00"
```

| 参数 | 说明 |
|---|---|
| `--upload-file` | 本地 PDF、DOCX 或 DOC 路径，跳过 IMAP，调用模型并创建一条飞书记录。 |
| `--upload-sender` | 可选联系邮箱；正常配置且 `REPLY_ENABLED=true` 时用于报名回信，也写入发件人字段供后续作业匹配。 |
| `--upload-subject` | 可选邮件标题，默认使用文件名。 |
| `--upload-received` | 可选 ISO 收件时间，默认当前时间，建议带 `+08:00` 等时区。 |

不填发件人仍能入库，但无法直接凭该空字段匹配后续作业。重复手动执行本地上传会创建新记录，不是“查重预览”。

### 5.3 历史数据回填

| 命令 | 行为 |
|---|---|
| `python resume_pipeline.py --backfill-birthplace` | 从历史简历重新分析，补充空生源地列；加 `--force-birthplace` 覆盖已有值。 |
| `python resume_pipeline.py --backfill-student-info` | 补充空学号、性别；加 `--force-student-info` 覆盖已有值。 |
| `python resume_pipeline.py --backfill-whitelist` | 按当前提示词补充空白名单项；加 `--force-whitelist` 覆盖已有值。 |
| `python resume_pipeline.py --backfill-replies` | 给历史飞书记录补发报名回信，使用简历状态库保存排队和发送记录；失败保留重试。 |

前三种字段回填访问飞书和模型，不初始化 IMAP/SMTP、不发送回信；下载不到有效简历时会尝试使用已有字段作为分析来源。`--force-*` 应与对应回填命令配套使用。每次只选择一种业务模式，避免组合多个主模式选项。

## 6. 作业程序的运行方法

### 6.1 日常命令

| 命令 | 行为 |
|---|---|
| `python homework_pipeline.py --help` | 显示帮助。 |
| `python homework_pipeline.py --check-imap` | 检查 DNS、TLS 和 IMAP 欢迎消息；不登录，不处理邮件，不连接飞书。 |
| `python homework_pipeline.py --init-fields` | 只创建/检查前若干周附件列，不连接邮箱。 |
| `python homework_pipeline.py --once` | 收取一轮，可能写飞书并发送异常转发邮件；处理或转发失败返回非零退出码。 |
| `python homework_pipeline.py` | 持续收取，每轮结束等待15分钟，启动先执行首轮。 |
| `python homework_pipeline.py --forward-unmatched` | 只转发状态库中已发现的无法入库邮件，不更新飞书；命令名兼容早期版本。 |
| `python homework_pipeline.py --reimport` | 备份并清理可重新入库的旧作业引用，再按当前规则扫描一轮。仅在明确需要重建时使用。 |

这些主模式互斥。**日常启动不需要 `--reimport`。**

### 6.2 周次规则

每个附件按以下优先级确定周次：

1. **邮件主题**：主题明确写“第2周”，整封邮件的附件均归入第2周，即使附件名写“第1周”。
2. **该附件文件名**：主题未明确注明时，按附件自己的文件名（不含扩展名）判断。同一邮件可以同时提交不同周的附件。
3. **服务器收件时间**：两处均不明确时，按 `INTERNALDATE` 转换为北京时间计算，不使用邮件 `Date` 或脚本运行时间。无周次附件不借用其他附件的周次。

支持 `第1周`、`第一周`、`第十二周`、`Week 2`、`week2`、`第一次作业`、`第1次作业` 等明确写法；第N次作业按第N周归档，兼容全角数字、大小写和数字周围空格；中文数字支持一至九十九，阿拉伯数字支持正整数。普通数字、学号、日期、“第二次提交”不视为周次。同一标题出现多个不同周次（如“第1周、第2周”）或范围（如“Week 1-2”）视为不明确，使用下一层依据；重复注明同一周不影响识别。

例如：9月30日收到“机械-测试同学-第一次作业.zip”，主题未明确周次时归第1周；主题“第2周补交”、附件“第1周.zip”归第2周；主题“补交”、附件“第1周.zip”和“第2周.zip”分别归档；同封邮件中的“说明.txt”按收件时间归档。被过滤的 Word/PDF 不参与周次归档，也不影响其他附件。日志会记录每个附件的归属周次和所用依据。

以下为未明确注明周次时的日期兜底，以默认起始日为例：

| 列名 | 北京时间收件日期（2026年） |
|---|---|
| 第1周 | 9月21日—9月28日，保留最初周一的收件起始日 |
| 第2周 | 9月29日—10月5日 |
| 第3周 | 10月6日—10月12日 |
| 第4周 | 10月13日—10月19日 |
| 第5周 | 10月20日—10月26日 |
| 第6周 | 10月27日—11月2日 |
| 第7周 | 11月3日—11月9日，收到相应邮件时按需建列 |

之后每周二00:00切换周次，跨年继续计数。一般规则为：以起始日期所在自然周的周二作第1周锚点；起始日若为周一，该日也并入第1周；早于起始日期的邮件始终跳过。迁入邮箱的历史邮件以服务器保留的 `INTERNALDATE` 为准。

此前已经入库的附件保留原位置，不自动搬动。已完成、已跳过、只待转发和已转发的旧邮件不因新规则自动重试；同封邮件跨周写入时若中途失败，保留已成功写入的部分，按现有策略转发人工处理。

### 6.3 匹配及文件处理

- 依据 `FIELD_SENDER` 匹配邮箱，去除显示名称和首尾空白、忽略大小写；兼容飞书普通文本及富文本。
- 同一邮箱对应多条候选人记录时，向所有匹配行追加该周附件。邮箱已匹配或已保存处理目标时，不启用姓名兜底。
- 邮箱未匹配时，读取邮件主题和普通附件文件名，将其中的完整姓名与飞书 `FIELD_NAME` 列比较。数据源是同一张飞书候选人表；本地 SQLite 只保存处理进度，不另建候选人库。
- 只有识别出一个姓名且该姓名唯一对应一条记录时才自动入库。重名、多条同名记录、主题与附件指向不同候选人或没有找到姓名，均红色提示原因并转发人工处理。
- 支持普通文本、富文本姓名及全角/半角、英文大小写归一化。建议主题/文件名使用 `算法组_张三_第1周`、`张三-26010001.zip`；也支持 `张三第1周作业`、`姓名张三` 等格式。不做错别字、拼音或相似姓名猜测，也不把“张三丰”当成“张三”。
- 姓名线索只来自外层邮件主题和附件文件名（含将被过滤的附件，以便发现冲突），不来自正文、发件人显示名、内嵌图片、文件夹路径、压缩包或附加邮件内部内容。匹配和上传共用当前邮件原文，避免重复下载。
- 不检查标题是否包含“作业”。按扩展名过滤 `.word`、`.doc`、`.docx`、`.pdf`，忽略大小写及文件名末尾空白。
- 一封邮件同时包含 PDF 和 ZIP 时，仅将 ZIP 写入飞书。匹配成功但附件全部被过滤或没有普通附件时，红色提示并标记跳过，不自动转发。
- 压缩包不解压、不检查内部文件；其他文件不执行、不解析，原样上传。正文内嵌资源排除，无文件名的显式附件使用 `attachment-序号.bin`。
- 单个附件大于20 MiB时使用分片上传，仍受飞书平台限制。空附件或平台拒绝会使该邮件转入人工处理。
- 同周多次提交全部追加，不覆盖已有附件；不同邮件中相同内容的文件也视为不同提交。

### 6.4 无法入库与自动转发

邮箱及姓名兜底仍未匹配，或单封处理出错后，立即停止该邮件的入库尝试；以后即使补上候选人记录，也不会再尝试该封邮件。同一人新发来的邮件仍正常判断。本次增加姓名兜底不会重新处理已标记为只转发、已转发、已跳过或已完成的旧邮件。

```text
无法匹配 / 单封入库失败
  → forward_only：只下载原邮件并排队转发
  → SMTP 成功
  → forwarded：已完成，不重复发送、不再入库
```

转发使用原收件账号发送，目标由 `HOMEWORK_UNMATCHED_FORWARD_TO` 指定。主题带 `[作业无法入库]`，正文说明原因、原发件人、原主题和 UID；`original.eml` 保存完整原邮件及全部附件。Word/PDF 过滤仅用于飞书入库，不会从转发原邮件中删除附件。

转发前先把待发邮件保存到 SQLite，失败时仅重试原邮件下载或 SMTP。成功后记录发送时间并释放正文缓存。SMTP 已接受但确认响应丢失时，协议层无法保证绝不重复，重试会沿用相同 Message-ID。

部分记录已经写入、剩余记录失败时，保留已有结果，不自动回滚或补写，转发人工检查。若邮箱连接、飞书全表读取等整轮操作失败，持续模式仍会等待下轮运行；“不再入库”针对已标记失败的具体邮件。

### 6.5 去重与重新入库

作业库保存邮箱/文件夹/UIDVALIDITY/UID、附件 token、写入进度和转发回执。不要删除数据库或锁文件；同一数据库一次只允许运行一个作业进程。对同一飞书表也不要启动多个使用不同状态库的作业实例。

`--reimport` 的步骤：

1. 确认邮箱可连接、可搜索。
2. 在数据库同目录保存 `数据库名-reimport-时间.db` 和 `.json` 备份。
3. 仅从“第N周”列移除该状态库可追溯且允许重新入库的附件引用，保留原始简历列和其他来源的附件；不删除云端素材。
4. 重置可重处理邮件的进度，从起始日期重新扫描，应用当前过滤规则并复用已上传 token。

`forward_only` / `forwarded` 邮件及其已有附件引用保留，不重新尝试入库。重建中断后再次执行同一命令继续；未完成时普通收取会提示先完成重建。数据库和备份包含邮件信息，只能作为本地运行数据保存。

## 7. 两个程序如何配合

先运行简历程序建立候选人表，确认“邮件发件人”正确，再运行作业程序匹配这些记录。

- 两个程序共用 `.env`，但检查间隔、首次运行策略和数据库各自独立。
- `REPLY_ENABLED=false` 只关闭简历自动回信，不关闭作业异常转发。
- 两个程序都以只读方式选择 IMAP 文件夹，处理邮件不会用“已读/未读”标志充当去重状态，也不会删除邮件。
- 简历程序不会因作业程序完成处理而跳过邮件；反过来也一样。若同时监控同一收件箱，PDF/DOCX/DOC 仍可能被简历程序视为简历，即使标题写的是作业。
- 招新结束后只需要作业功能时，可以停止简历程序，保留作业程序。不要把同一邮箱中的所有文件都视为必然属于某一阶段。
- 作业发生同一人多行或多人共享邮箱时，匹配基于邮箱而非姓名，需要人工维护候选人表。

## 8. 常驻运行与日志

### 8.1 前台运行

Linux 从项目根目录执行：

```bash
.venv/bin/python homework_pipeline.py
```

需要收简历时另开终端运行：

```bash
.venv/bin/python resume_pipeline.py
```

终端需保持开启；按 `Ctrl+C` 停止。更改配置或代码后重启对应进程。前台命令不是自动开机启动服务。

### 8.2 Linux systemd

仓库提供 [resume-pipeline.service.example](resume-pipeline.service.example)。根据实际用户和项目绝对路径修改模板中的 `WorkingDirectory` 和 `ExecStart`；建议指定有权限访问项目目录的非 root `User`。

作业服务可使用独立的 `/etc/systemd/system/homework-pipeline.service`，示例中的用户和路径需要替换：

```ini
[Unit]
Description=Homework Collection Pipeline
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_linux_user
WorkingDirectory=/absolute/path/Resume_collection_system
ExecStart=/absolute/path/Resume_collection_system/.venv/bin/python /absolute/path/Resume_collection_system/homework_pipeline.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

保存后由管理员执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now homework-pipeline
sudo systemctl status homework-pipeline
journalctl -u homework-pipeline -f
```

修改配置后重启：

```bash
sudo systemctl restart homework-pipeline
```

服务模式下不要再手工启动同一个作业实例。确保服务用户可读 `.env`、可写状态库；桌面代理不一定在开机时已就绪，需单独核对服务器网络。

### 8.3 Windows

可用任务计划程序或已有的服务管理工具启动：

- 程序：项目虚拟环境中的 `python.exe` 绝对路径。
- 参数：`homework_pipeline.py` 或 `resume_pipeline.py` 的绝对路径。
- 起始目录：项目根目录，确保数据库相对路径和配置定位一致。

## 9. 常见问题

| 现象 | 排查方法 |
|---|---|
| 缺少环境变量 | 检查 `.env` 是否存在、是否填真实值，以及进程环境变量是否覆盖本地文件；修改后重启。 |
| 163登录失败 / Unsafe Login | 确认 IMAP/SMTP 已开启、使用客户端授权码、账号安全设置允许客户端访问。 |
| TLS EOF / 握手被断开 | 先运行 `--check-imap`；检查代理/TUN、防火墙和邮箱端口，不要关闭证书验证来绕过问题。 |
| SMTP转发失败 | 检查 `EMAIL_SMTP_*`、授权码及转发目标；失败邮件在队列中，不会重新尝试飞书入库。 |
| 飞书权限不足或找不到表 | 检查 App ID/Secret、app_token/table_id、接口权限是否生效，以及应用是否获得目标表格访问权。 |
| 字段类型错误 | `FIELD_*` 需与列名逐字一致；附件列、日期列、多选列按字段说明建立，同名不同类型不会自动转换。 |
| 技术栈/可选文本为空 | 查看建列或添加选项的警告，核对自动建列开关与字段权限，不一定是整条记录入库失败。 |
| 模型返回非JSON或结果截断 | 核对模型可用性和 JSON 参数支持、输出 token 上限；若不支持 `enable_thinking`，将发送开关设为 `false`。 |
| 扫描PDF提取不足 | 当前没有 OCR；需要带文本层的 PDF 或可提取文字的 Word 文档。 |
| `.doc` 解析失败 | 运行 `--check`，确认转换后端可执行路径正确；Windows Word COM 需要已安装 Word。 |
| 简历历史邮件没有全收 | 默认在作用域首次初始化时跳过全部现有邮件；新版本不再截断扫描窗口。仅改首次开关不会重置已见状态，旧库迁移的历史边界见第5节。 |
| 提示 `--adopt-legacy-state` | 检测到未绑定邮箱的旧库；确认仍为原邮箱和原文件夹后，停止旧进程并按第5节迁移。 |
| 简历附件一直等待核对 | 检查飞书记录读取权限、网络、目标表配置及是否存在同一上传令牌的多条记录；不要删状态库绕过。 |
| 作业没有重新匹配某封邮件 | 已变成只转发/已转发状态，不再入库，这是当前预期行为。 |
| “已有实例使用此数据库” | 检查终端或系统服务是否已在运行；停止多余进程，不要通过删锁文件绕过。 |
| 修改了15分钟间隔但没生效 | 修改的是 `HOMEWORK_POLL_INTERVAL_SECONDS`，且已重启进程；外部环境变量可能覆盖 `.env`。 |

### 9.1 Clash/Mihomo 代理环境

若 DNS 返回 `198.18.x.x` / `198.19.x.x`，可能启用了 Fake-IP。TUN 会影响原生 IMAP/SMTP TCP 连接，单纯清除 `HTTP_PROXY` / `HTTPS_PROXY` 不一定有效。

经网络检查确认需要邮箱直连时，在**规则模式**下将以下规则置于前方：

```yaml
rules:
  - DOMAIN,imap.163.com,DIRECT
  - DOMAIN,smtp.163.com,DIRECT
  # 后面保留原有规则
```

全局模式不会按普通直连规则分流。通过客户端的规则扩展方式保存，避免订阅更新覆盖；参考 [Clash Verge 扩展说明](https://www.clashverge.dev/guide/extend.html) 与 [Mihomo DNS 文档](https://wiki.metacubex.one/config/dns/)。这些代理设置不属于项目 `.env`，也不会由正常收取命令自动修改。

## 10. 测试与提交仓库

运行模拟测试，不访问真实邮箱、模型或飞书：

```bash
python -m unittest discover -s test -p 'test_*.py'
```

`test/` 同时包含独立回信服务工具和主程序测试；生产运行应使用项目根目录的两个主脚本。修改文档提取时，还应使用合成 PDF/DOCX/DOC 执行 `--check` 与 `--test-file` 验证。真实轮询、上传、回填、重建和转发命令都可能产生外部写入，不属于单元测试。

提交 GitHub 前检查 `git status --short`，只提交代码、文档和脱敏模板。不要提交：

- `.env`、授权码、API Key、应用 Secret。
- 真实简历、作业、原始邮件和含个人信息的日志。
- `resume_pipeline.db`、`homework_pipeline.db`、SQLite 辅助文件、锁文件及备份。
- `.venv/`、`__pycache__/` 等本地产物。

默认作业数据库及 `homework_pipeline.db-*` 备份已有忽略规则；自定义数据库路径和其他运行数据需自行补充 `.gitignore`。项目没有承诺附件扩展名会被 Git 自动忽略，上传前应人工检查真实文件。
