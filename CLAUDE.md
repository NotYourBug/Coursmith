# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

智课工坊 is a course generation tool that uses the DeepSeek API to automatically produce structured course outlines and styled HTML lesson pages from course titles. It also converts generated HTML to PNG screenshots and packages courses into ZIP archives.

## Architecture

The project has three entry points, all sharing a single core library:

- **`course_gen_core.py`** — Shared library (~1160 lines): configuration, prompt templates, HTML templates, DeepSeek API calls, HTML→PNG conversion (Selenium/Chrome), ZIP packaging, and batch pipeline orchestration.
- **`batch_course_gen.py`** — CLI batch processor. Scans `课程标题/` for `.txt` files where each line is a course title, then runs the full pipeline.
- **`gui_course_gen.py`** — Tkinter GUI wrapping the same batch pipeline, with settings dialogs for API key and chromedriver path.
- **`feishu_bot.py`** — Feishu/Lark bot that parses structured messages (`每门节数:X / 主题：xxx / 《标题》`) and triggers generation.

## Pipeline

```
Course titles (.txt) → AI outlines (JSON via DeepSeek) → AI lesson HTML pages
  → Navigation wrapping (sidebar + prev/next) → Root index → PNG conversion + ZIP
```

Output structure:
```
课程标题/<category>/
  ├── index.html          (course chapter listing)
  ├── 01.html, 02.html... (individual lessons, wrapped with sidebar)
  ├── *.png               (optional, from HTML→PNG)
  └── *.zip               (optional)
```

## Commands

```bash
# Batch generation (CLI)
python batch_course_gen.py
python batch_course_gen.py --lessons 20 --threads 50 --concurrent 5
python batch_course_gen.py --fix-navigation          # rebuild nav only, no API calls

# GUI
python gui_course_gen.py

# Feishu bot
python feishu_bot.py
python feishu_bot.py --test-parse                   # test message parsing

# Install dependencies
pip install openai selenium webdriver-manager lark-oapi
```

## Configuration

`config.json` stores non-secret compatibility settings such as `base_url`, `browser_path`, and `lessons_per_course`. DeepSeek and Feishu credentials must be supplied through the local `.env` file or process environment and must never be committed. Prompt template versioning auto-updates the config when the built-in version is newer.

## Key Technical Details

- The DeepSeek API is called via the OpenAI-compatible Python client (`from openai import OpenAI`) pointed at `https://api.deepseek.com/`.
- Lesson HTML is generated as base64-encoded blobs embedded in wrapper pages with a sidebar navigator; the wrapper uses an `<iframe>` with `srcdoc` set via JS.
- Thread safety: file writes use `threading.Lock`, CWD changes (which the pipeline relies on) use a dedicated `_cwd_lock`.
- Chrome driver resolution: tries the configured path first, then falls back to `webdriver_manager`.
- HTTP proxies are zeroed out at import time to avoid httpx connection issues on Windows.
- Feishu bot uses `lark-oapi` WebSocket client with P2P IM message receiving.
