# Repository Guidelines

## Project Structure & Module Organization

This Python 3.10+ application collects resume attachments from IMAP, extracts text locally, calls SiliconFlow for structured analysis, and uploads files and candidate records to Feishu Bitable.

- `resume_pipeline.py` contains configuration, normalization helpers, PDF/DOCX/DOC conversion, `FeishuClient`, `ImapMailClient`, SQLite state management, and CLI entry points.
- `requirements.txt` lists runtime dependencies; `pywin32` is Windows-only.
- `.env.example` documents configuration; `README.md` provides setup and operational instructions in Chinese.
- `install_linux.sh`, `install_windows.ps1`, and `resume-pipeline.service.example` support installation and deployment.
- `test/` contains the isolated SMTP/IMAP fixed-reply test service and its `unittest` checks; it intentionally does not call SiliconFlow or Feishu. Production reply delivery is integrated in `resume_pipeline.py`, with queued SMTP retries and the one-time `--backfill-replies` migration command. `.venv/`, `__pycache__/`, and runtime databases/state files are local artifacts.

## Build, Test, and Development Commands

There is no separate build step. On Linux, prepare the environment with:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` if it does not already exist, then configure credentials and field mappings. On Windows, use `py -m venv .venv` and `.\.venv\Scripts\Activate.ps1`.

- `python resume_pipeline.py --check`: report local converter availability.
- `python resume_pipeline.py --test-file ./sample.docx`: extract local text without contacting remote services.
- `python resume_pipeline.py --upload-file ./sample.docx`: parse a local resume and create one Feishu record; add `--upload-sender` to enable the post-ingestion reply.
- `python resume_pipeline.py --once`: run one mailbox polling cycle.
- `python resume_pipeline.py`: poll continuously.
- `python -m unittest discover -s test -p 'test_*.py'`: run the isolated reply-service unit tests.
- `python resume_pipeline.py --backfill-replies`: send the one-time acknowledgement to records already in Feishu.
- `python resume_pipeline.py --backfill-whitelist`: re-run the model on historical resumes and update only the whitelist column; it does not initialize SMTP or send replies.

The polling and backfill commands contact external services; use dedicated test resources for integration checks.

## Coding Style & Naming Conventions

Follow existing four-space indentation, type annotations, `snake_case` functions and variables, `PascalCase` classes, and `UPPER_SNAKE_CASE` configuration constants. Preserve the script’s section organization and Linux/Windows compatibility. No formatter or linter is configured. Document configuration changes in both `.env.example` and `README.md`.

## Testing Guidelines

The production pipeline has no coverage threshold. Run `--check` and exercise `--test-file` with synthetic PDF, DOCX, and DOC fixtures when changing extraction. The isolated reply service uses standard-library `unittest` under `test/`; mock IMAP/SMTP and remote services rather than contacting real accounts in tests.

## Commit & Pull Request Guidelines

The repository has no commits yet, so no historical convention exists. Use concise imperative subjects, such as `Fix DOCX paragraph extraction`. Describe behavior changes, validation performed, configuration implications, and related issues in pull requests.

## Security & Configuration

Never commit `.env`, credentials, real resumes, or runtime databases. Preserve SQLite deduplication behavior and the default `PROCESS_EXISTING_ON_FIRST_RUN=false` to avoid unexpected historical imports.
