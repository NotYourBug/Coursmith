# 商业课程交付站第一版实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有课程生成脚本改造成一个可发布《Agent 开发入门到实战》、支持兑换码访问和移动端学习的第一版课程交付产品。

**Architecture:** 保留现有 Python 生成引擎作为内部生产工具，新增课程包格式、课程包验证器和 FastAPI 交付站。课程正文以文件形式存储，SQLite 仅保存课程索引、访问码、会话、进度和审计事件；第三方店铺收款仍通过人工发放兑换码完成。

**Tech Stack:** Python 3.11+、FastAPI、Jinja2、Pydantic、SQLite、pytest、httpx、Playwright、python-dotenv。

**Spec:** `docs/superpowers/specs/2026-09-21-commercial-course-v1-design.md`

## Global Constraints

- 产品底层支持通用课程分类，第一版只发布“技术开发 / AI 与大模型”分类。
- 首发课程名称为《Agent 开发入门到实战》，包含 8～10 章，每章有目标、讲解、案例、练习和总结。
- 第三方店铺负责收款，课程站负责兑换、访问和学习；第一版不接入支付接口和订单自动同步。
- 默认交付为在线课程站，PDF 为辅助下载格式，HTML/ZIP 仅作为专业用户或企业授权版本。
- 第一版不开放外部用户自行生成课程，不实现正式注册、找回密码、企业多租户和完整 DRM。
- 生成结果必须包含 `manifest.json`、版本信息、来源清单、授权文件和 AI 辅助生成说明。
- 不允许把 API Key、飞书密钥或其他凭证写入仓库配置文件。
- 代码和测试必须可以在不调用真实 AI API 的情况下运行。
- 任何失败生成结果都必须保留并标记失败，不得默认删除课程目录。
- 现有生成引擎必须逐步改为显式传递输入、输出目录，目标是移除业务流程中的 `os.chdir()` 依赖。

## Review Focus

- 同名或含非法字符的课程目录不能覆盖已有课程；由 Task 2 的路径碰撞测试覆盖。
- 缺章节、错误 manifest、坏 HTML 和外部资源引用必须阻止发布；由 Task 2 的课程包验证测试覆盖。
- 兑换码必须防止重复使用、过期使用和跨课程使用；由 Task 5 的访问服务测试覆盖。
- 未授权访问不能读取受保护章节或 PDF；由 Task 6 的 HTTP 集成测试覆盖。
- 手机宽度下长标题、代码块和目录不能溢出；由 Task 7 的模板快照和浏览器检查覆盖。

## 文件结构

```text
course_platform/
├── __init__.py              # 包版本和公共导出
├── settings.py              # 环境变量和运行时配置
├── content.py               # manifest、课程包加载、验证和发布
├── generation_adapter.py    # 现有 course_gen_core 的显式目录适配层
├── database.py               # SQLite schema、连接和基础查询
├── access.py                 # 兑换码、会话和学习进度服务
├── app.py                    # FastAPI 应用工厂和 HTTP 路由
├── export.py                 # PDF 和课程包导出
├── cli.py                    # 发布、创建兑换码和启动服务命令
├── templates/
│   ├── base.html
│   ├── home.html
│   ├── category.html
│   ├── course_detail.html
│   ├── access.html
│   ├── learn_home.html
│   └── help.html
└── static/
    └── site.css
tests/
├── fixtures/course-package/
├── test_settings.py
├── test_content.py
├── test_generation_adapter.py
├── test_access.py
├── test_app.py
└── test_export.py
content/
└── courses/agent-development/
```

---

### Task 1: 建立项目运行基础和密钥边界

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `course_platform/__init__.py`
- Create: `course_platform/settings.py`
- Create: `tests/test_settings.py`
- Modify: `config.json`
- Modify: `gui_course_gen.py`
- Modify: `course_gen_core.py`

**Interfaces:**
- Produces `course_platform.settings.Settings` and `load_settings(env: Mapping[str, str] | None = None) -> Settings`.
- `Settings` fields are `base_url: str`, `api_key: str`, `content_root: Path`, `database_path: Path`, `session_ttl_hours: int`, and `environment: str`.

- [ ] **Step 1: Write the failing configuration tests**

```python
def test_environment_key_is_used_without_serializing_it(tmp_path):
    settings = load_settings({
        "DEEPSEEK_API_KEY": "secret-value",
        "COURSE_CONTENT_ROOT": str(tmp_path / "content"),
        "COURSE_DATABASE": str(tmp_path / "course.db"),
    })
    assert settings.api_key == "secret-value"
    assert "secret-value" not in settings.to_public_dict().__str__()


def test_missing_key_is_explicitly_reported(tmp_path):
    settings = load_settings({
        "COURSE_CONTENT_ROOT": str(tmp_path / "content"),
        "COURSE_DATABASE": str(tmp_path / "course.db"),
    })
    assert settings.api_key == ""
    assert settings.has_llm_credentials is False
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_settings.py -q`

Expected: FAIL because `course_platform.settings` does not exist.

- [ ] **Step 3: Add dependency metadata and secure defaults**

Use this dependency set in `pyproject.toml`:

```toml
[project]
name = "zhike-course-platform"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115,<1",
  "httpx>=0.28,<1",
  "jinja2>=3.1,<4",
  "pydantic>=2.10,<3",
  "python-dotenv>=1.0,<2",
  "uvicorn[standard]>=0.34,<1",
  "playwright>=1.50,<2"
]

[project.optional-dependencies]
dev = ["pytest>=8,<9", "pytest-cov>=6,<7"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Add `.env`, virtual environments, generated content, SQLite files, PNG files, ZIP files and secrets to `.gitignore`. Add only placeholder values to `.env.example`.

- [ ] **Step 4: Implement `Settings` and environment loading**

Implement `load_settings()` with this precedence: explicit mapping, process environment, `.env`, safe defaults. `to_public_dict()` must omit `api_key`. Resolve all relative paths from the repository root, create `content_root` and the parent directory of `database_path`, and reject negative or zero `session_ttl_hours`.

- [ ] **Step 5: Revoke the exposed credential before changing local configuration**

Revoke the API key currently committed in `config.json` through the provider console before testing any migrated configuration. Treat the value as compromised even if it is later deleted from the working tree.

- [ ] **Step 6: Remove the committed API key and GUI plaintext persistence**

Delete the real value from `config.json`, keep an empty `api_key` field only for backward compatibility during migration, and update the GUI settings flow so it writes `DEEPSEEK_API_KEY` to a local `.env` file or reports that the user must set the environment variable. Do not print the key or include it in error messages.

- [ ] **Step 7: Run the tests and commit the checkpoint**

Run: `python -m pytest tests/test_settings.py -q`

Expected: PASS. Check the repository with `rg -n "sk-[A-Za-z0-9_-]{20,}|api_key.*sk-" .` and expect no result. When Git metadata is available, commit with `git add pyproject.toml .env.example .gitignore course_platform tests config.json gui_course_gen.py course_gen_core.py && git commit -m "chore: establish secure course platform foundation"`.

### Task 2: 建立课程包格式、验证器和发布器

**Files:**
- Create: `course_platform/content.py`
- Create: `tests/fixtures/course-package/manifest.json`
- Create: `tests/fixtures/course-package/index.html`
- Create: `tests/fixtures/course-package/chapters/01.html`
- Create: `tests/fixtures/course-package/SOURCES.txt`
- Create: `tests/fixtures/course-package/LICENSE.txt`
- Create: `tests/test_content.py`

**Interfaces:**
- `ChapterManifest(number: int, title: str, path: str, free_preview: bool, duration_minutes: int | None)`.
- `CourseManifest(course_id: str, slug: str, title: str, category: str, version: str, status: Literal["draft", "published"], chapter_count: int, chapters: list[ChapterManifest], free_chapters: list[int], contains_ai_generated_content: bool, ai_disclosure: str, source_manifest: str, license_file: str)`.
- `CoursePackage(manifest: CourseManifest, root: Path, chapter_files: list[Path])`.
- `ValidationReport(ok: bool, errors: list[str], warnings: list[str])`.
- `load_course_package(path: Path) -> CoursePackage`.
- `validate_course_package(path: Path) -> ValidationReport`.
- `publish_course(source_dir: Path, content_root: Path, manifest: CourseManifest) -> Path`.

- [ ] **Step 1: Write tests for a valid package, missing chapter, path collision, and unsafe path**

```python
def test_valid_package_loads(fixture_package):
    package = load_course_package(fixture_package)
    assert package.manifest.slug == "fixture-course"
    assert package.chapter_files == [fixture_package / "chapters" / "01.html"]


def test_missing_chapter_blocks_publish(tmp_path, fixture_package):
    manifest = json.loads((fixture_package / "manifest.json").read_text(encoding="utf-8"))
    manifest["chapter_count"] = 2
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    shutil.copy(fixture_package / "index.html", tmp_path / "index.html")
    report = validate_course_package(tmp_path)
    assert report.ok is False
    assert "chapters/02.html" in report.errors[0]


def test_publish_does_not_overwrite_existing_slug(tmp_path, fixture_package):
    target = tmp_path / "content" / "fixture-course"
    target.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        publish_course(fixture_package, tmp_path / "content", load_course_package(fixture_package).manifest)


def test_manifest_rejects_parent_path(fixture_package):
    data = json.loads((fixture_package / "manifest.json").read_text(encoding="utf-8"))
    data["chapters"][0]["path"] = "../secrets.html"
    (fixture_package / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    report = validate_course_package(fixture_package)
    assert report.ok is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_content.py -q`

Expected: FAIL because manifest models and package functions are missing.

- [ ] **Step 3: Implement manifest models and safe path resolution**

Use Pydantic models with strict positive chapter numbers, unique chapter numbers, safe relative paths, semantic version strings, non-empty titles, and `free_chapters` values that exist in the chapter list. Resolve a chapter path only after checking that its resolved path remains below the package root.

The fixture `manifest.json` must contain this minimal valid object:

```json
{
  "course_id": "fixture-course",
  "slug": "fixture-course",
  "title": "Fixture Course",
  "category": "technical",
  "version": "0.1.0",
  "status": "published",
  "chapter_count": 1,
  "chapters": [
    {"number": 1, "title": "第一章", "path": "chapters/01.html", "free_preview": true, "duration_minutes": 10}
  ],
  "free_chapters": [1],
  "contains_ai_generated_content": true,
  "ai_disclosure": "本课程部分内容由人工智能辅助生成，并经过人工审核与编辑",
  "source_manifest": "SOURCES.txt",
  "license_file": "LICENSE.txt"
}
```

- [ ] **Step 4: Implement package validation**

`validate_course_package()` must check manifest parsing, required files, chapter count, duplicate chapter numbers, HTML document markers, missing `SOURCES.txt`, missing `LICENSE.txt`, external `<script src>`, external stylesheet links, and references to `http://` or `https://` images. Return `ValidationReport(ok: bool, errors: list[str], warnings: list[str])` without deleting or modifying the package.

- [ ] **Step 5: Implement collision-safe publishing**

`publish_course()` must validate first, create a temporary sibling directory, copy only manifest-listed files plus `assets/` and `downloads/`, write the final directory atomically, and raise `FileExistsError` when the target slug already exists. Add `overwrite=False` as the only accepted default.

- [ ] **Step 6: Run focused tests and commit the checkpoint**

Run: `python -m pytest tests/test_content.py -q`

Expected: PASS. When Git metadata is available, commit with `git add course_platform/content.py tests/fixtures/course-package tests/test_content.py && git commit -m "feat: add validated course package format"`.

### Task 3: 移除生成流程的工作目录耦合

**Files:**
- Create: `course_platform/generation_adapter.py`
- Create: `tests/test_generation_adapter.py`
- Modify: `course_gen_core.py: process_course_file, run_full_pipeline_for_titles, run_post_pipeline, run_batch_pipeline`
- Modify: `batch_course_gen.py`
- Modify: `feishu_bot.py`
- Modify: `gui_course_gen.py`

**Interfaces:**
- `GenerationOptions(lessons_per_course: int, footer_text: str, thread_num: int, do_png: bool, do_zip: bool, png_workers: int)`.
- `BatchResult(processed_files: list[Path], failures: list[tuple[Path, str]])`.
- `generate_to_directory(course_titles: list[str], output_dir: Path, options: GenerationOptions) -> list[dict[str, object]]`.
- `run_batch_pipeline(input_files: list[Path], options: GenerationOptions) -> BatchResult`.
- `run_post_pipeline(base_dir: Path, do_png: bool, do_zip: bool, png_workers: int) -> list[Path]`.
- Existing CLI, GUI and Feishu behavior continues to call the adapter without changing user-facing flags.

- [ ] **Step 1: Add path-isolation tests with two temporary output directories**

```python
def test_generation_adapter_keeps_outputs_in_requested_directory(tmp_path, monkeypatch):
    calls = []

    def fake_generate(course_titles, lessons_per_course, footer_text, thread_num, output_dir, **kwargs):
        calls.append(Path(output_dir))
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / "manifest-input.json").write_text("{}", encoding="utf-8")
        return []

    monkeypatch.setattr("course_platform.generation_adapter._legacy_generate", fake_generate)
    generate_to_directory(["课程 A"], tmp_path / "a", GenerationOptions(2, "资料云集", 1, False, False, 1))
    generate_to_directory(["课程 B"], tmp_path / "b", GenerationOptions(2, "资料云集", 1, False, False, 1))
    assert calls == [tmp_path / "a", tmp_path / "b"]
    assert not (tmp_path / "b" / "课程 A").exists()


def test_batch_pipeline_processes_only_explicit_files(tmp_path, monkeypatch):
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("课程一\n", encoding="utf-8")
    second.write_text("课程二\n", encoding="utf-8")
    result = run_batch_pipeline(input_files=[first], options=GenerationOptions(1, "", 1, False, False, 1))
    assert result.processed_files == [first]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_generation_adapter.py -q`

Expected: FAIL because the adapter and explicit `input_files` interface do not exist.

- [ ] **Step 3: Refactor core file operations to accept explicit paths**

Change `write_root_index`, `run_post_pipeline`, `tran_all_html_to_png`, `zip_folders_in_directory`, `process_course_file`, and `run_full_pipeline_for_titles` to receive `Path` values. Pass `output_dir` through lesson generation and navigation creation. Keep the legacy CLI defaults by resolving `课程标题` from the configured project root.

- [ ] **Step 4: Remove `os.chdir()` from the production paths**

Replace all `os.chdir()` calls in `course_gen_core.py`, `feishu_bot.py`, and the batch path used by the GUI with explicit paths. Do not weaken the existing file locks; retain them only around shared file writes. Make post-processing operate on the category path passed as an argument.

- [ ] **Step 5: Make GUI selection effective and make CLI parsing strict**

Pass the selected absolute file list from `gui_course_gen.py` into `run_batch_pipeline(input_files=...)`. In `batch_course_gen.py`, reject unknown flags, missing values, non-positive numeric values, and invalid combinations with a non-zero exit code and a readable message.

- [ ] **Step 6: Run focused and legacy smoke tests**

Run: `python -m pytest tests/test_generation_adapter.py -q`

Expected: PASS. Also run `python batch_course_gen.py --help` and `python feishu_bot.py --test-parse`; both must exit without an AI call. When Git metadata is available, commit with `git add course_gen_core.py course_platform/generation_adapter.py batch_course_gen.py feishu_bot.py gui_course_gen.py tests/test_generation_adapter.py && git commit -m "refactor: isolate generation outputs from process cwd"`.

### Task 4: 实现课程数据库、兑换码和学习进度

**Files:**
- Create: `course_platform/database.py`
- Create: `course_platform/access.py`
- Create: `tests/test_access.py`

**Interfaces:**
- `initialize_database(path: Path) -> None`.
- `AccessService(database_path: Path)` with methods `create_access_code(course_id: str, expires_at: datetime | None = None) -> str`, `redeem_access_code(raw_code: str, course_id: str) -> AccessSession`, `get_session(raw_session_id: str) -> AccessSession | None`, `record_progress(session_id: str, course_id: str, chapter_number: int, completed: bool) -> None`, and `get_progress(session_id: str, course_id: str) -> dict[int, bool]`.
- `sync_course(manifest: CourseManifest, content_path: Path, database_path: Path) -> None`.

- [ ] **Step 1: Write tests for database initialization and access behavior**

```python
def test_redeem_code_once_and_reject_second_use(access_service):
    code = access_service.create_access_code("agent-development")
    session = access_service.redeem_access_code(code, "agent-development")
    assert session.course_id == "agent-development"
    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "agent-development")


def test_code_cannot_be_used_for_another_course(access_service):
    code = access_service.create_access_code("agent-development")
    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "ai-infra")


def test_expired_code_is_rejected(access_service, freezer):
    code = access_service.create_access_code("agent-development", expires_at=freezer.now() - timedelta(minutes=1))
    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "agent-development")


def test_progress_is_scoped_to_session_and_course(access_service):
    code = access_service.create_access_code("agent-development")
    session = access_service.redeem_access_code(code, "agent-development")
    access_service.record_progress(session.session_id, "agent-development", 2, True)
    assert access_service.get_progress(session.session_id, "agent-development") == {2: True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_access.py -q`

Expected: FAIL because the schema and access service are missing.

- [ ] **Step 3: Create the SQLite schema**

Create tables `courses`, `access_codes`, `sessions`, `progress`, and `events` with foreign keys enabled. Store only SHA-256 hashes for raw access codes and session identifiers. Add unique constraints for `(course_id, chapter_number)` and `(course_id, code_hash)`.

- [ ] **Step 4: Implement access code and session services**

Generate codes with a readable prefix and cryptographically secure random bytes. On redemption, check course existence, code hash, unused state, expiration and course match inside one transaction; mark the code used and create a session. Set session expiration from `Settings.session_ttl_hours`.

- [ ] **Step 5: Implement progress and audit events**

Record chapter completion with an upsert. Record `code_redeemed`, `course_opened`, `chapter_opened`, `progress_updated`, and `access_denied` events without storing raw codes. Return a clean domain exception for invalid or expired access.

- [ ] **Step 6: Run focused tests and commit the checkpoint**

Run: `python -m pytest tests/test_access.py -q`

Expected: PASS. When Git metadata is available, commit with `git add course_platform/database.py course_platform/access.py tests/test_access.py && git commit -m "feat: add course access and progress persistence"`.

### Task 5: 建立 FastAPI 课程交付站

**Files:**
- Create: `course_platform/app.py`
- Create: `tests/test_app.py`
- Modify: `course_platform/settings.py`

**Interfaces:**
- `create_app(settings: Settings | None = None) -> FastAPI`.
- Public routes: `GET /`, `GET /categories/{slug}`, `GET /courses/{slug}`, `GET /access`, `POST /access/redeem`, `GET /help`.
- Protected routes: `GET /learn/{course_slug}`, `GET /learn/{course_slug}/chapters/{number}`, `GET /learn/{course_slug}/downloads/course.pdf`, `POST /api/progress`.

- [ ] **Step 1: Write HTTP tests for public, protected, redemption, and progress flows**

```python
def test_course_detail_is_public(client):
    response = client.get("/courses/agent-development")
    assert response.status_code == 200
    assert "Agent 开发入门到实战" in response.text


def test_chapter_requires_access(client):
    response = client.get("/learn/agent-development/chapters/1")
    assert response.status_code == 403


def test_redeem_redirects_and_sets_http_only_cookie(client, access_code):
    response = client.post("/access/redeem", data={"course_slug": "agent-development", "code": access_code})
    assert response.status_code == 303
    assert "/learn/agent-development" in response.headers["location"]
    assert "HttpOnly" in response.headers["set-cookie"]


def test_authorized_user_can_open_chapter_and_save_progress(client, access_code):
    client.post("/access/redeem", data={"course_slug": "agent-development", "code": access_code})
    page = client.get("/learn/agent-development/chapters/1")
    assert page.status_code == 200
    saved = client.post("/api/progress", json={"course_slug": "agent-development", "chapter_number": 1, "completed": True})
    assert saved.status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_app.py -q`

Expected: FAIL because the application factory and routes are missing.

- [ ] **Step 3: Implement application factory and repository injection**

`create_app()` must accept temporary content and database paths from `Settings`, initialize the schema, load catalog data from published manifests, mount `/static`, and use dependency injection so tests never touch production files.

- [ ] **Step 4: Implement public catalog and course detail routes**

Render categories from data rather than hardcoded route branches. Course detail pages must expose title, category, description, target audience, objectives, chapter list, free chapters, version, update date, AI disclosure and customer support link.

- [ ] **Step 5: Implement redemption and protected content routes**

Use an HTTP-only, SameSite=Lax cookie named `course_session`. Protected routes must validate the session, course ID and expiration before reading any chapter or PDF file. Invalid sessions return HTTP 403 with a user-readable page, never a filesystem traceback.

- [ ] **Step 6: Implement progress endpoint and error pages**

Accept only a positive integer chapter number that exists in the manifest. Return HTTP 204 on success, HTTP 400 for invalid input, HTTP 403 for invalid sessions and HTTP 404 for unknown courses or chapters.

- [ ] **Step 7: Run focused tests and commit the checkpoint**

Run: `python -m pytest tests/test_app.py -q`

Expected: PASS. Start a local smoke server with `uvicorn course_platform.app:app --reload` and verify `/`, `/courses/agent-development`, `/access`, and an authorized chapter manually. When Git metadata is available, commit with `git add course_platform/app.py course_platform/settings.py tests/test_app.py && git commit -m "feat: add protected course delivery site"`.

### Task 6: 完成移动端课程站界面

**Files:**
- Create: `course_platform/templates/base.html`
- Create: `course_platform/templates/home.html`
- Create: `course_platform/templates/category.html`
- Create: `course_platform/templates/course_detail.html`
- Create: `course_platform/templates/access.html`
- Create: `course_platform/templates/learn_home.html`
- Create: `course_platform/templates/help.html`
- Create: `course_platform/static/site.css`
- Create: `tests/test_templates.py`

**Interfaces:**
- Templates receive typed view models from `app.py`, not raw database rows.
- Every page uses `base.html` for viewport, navigation, footer, AI disclosure and support link.

- [ ] **Step 1: Write template tests for mobile-critical content**

```python
def test_base_template_has_mobile_viewport(rendered_pages):
    for page in rendered_pages:
        assert 'name="viewport"' in page
        assert "width=device-width" in page


def test_course_detail_exposes_learning_outcome(course_detail_html):
    assert "学完后你将能够" in course_detail_html
    assert "免费试学" in course_detail_html


def test_chapter_styles_allow_code_overflow_without_page_overflow(site_css):
    assert ".code-block" in site_css
    assert "overflow-x: auto" in site_css
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_templates.py -q`

Expected: FAIL because templates and stylesheet are missing.

- [ ] **Step 3: Implement the shared layout and course discovery pages**

Build a calm, readable visual system using the existing warm course style as reference. The homepage must show the product promise, featured course and categories. The course detail page must show outcomes before chapter details and expose one free preview CTA.

- [ ] **Step 4: Implement access and learning pages**

The access page must have one clear code input and a help link. The learning home must show progress, chapter status, current version and next chapter. For the first version, the chapter route returns the validated generated HTML as an authenticated `HTMLResponse`; the learning home supplies the consistent course-site header, progress summary and chapter navigation, avoiding unsafe HTML rewriting during the pilot.

- [ ] **Step 5: Add responsive CSS and accessibility basics**

Use a mobile-first layout, visible focus states, semantic headings, readable line length, minimum 44px interactive targets, `prefers-reduced-motion`, and horizontal overflow only inside code blocks and wide tables.

- [ ] **Step 6: Run template and HTTP tests and commit the checkpoint**

Run: `python -m pytest tests/test_templates.py tests/test_app.py -q`

Expected: PASS. When Git metadata is available, commit with `git add course_platform/templates course_platform/static tests/test_templates.py && git commit -m "feat: add mobile-first course learning experience"`.

### Task 7: 实现 PDF、课程包导出和发布 CLI

**Files:**
- Create: `course_platform/export.py`
- Create: `tests/test_export.py`
- Modify: `course_platform/cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- `export_course_pdf(course_dir: Path, output_pdf: Path, browser_executable: Path | None = None) -> Path`.
- `export_course_zip(course_dir: Path, output_zip: Path) -> Path`.
- CLI commands: `python -m course_platform.cli validate <course_dir>`, `publish <course_dir>`, `create-code <course_slug>`, `serve`.

- [ ] **Step 1: Write export tests with mocked browser and zip assertions**

```python
def test_zip_export_contains_manifest_sources_and_license(fixture_package, tmp_path):
    output = export_course_zip(fixture_package, tmp_path / "fixture.zip")
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    assert "manifest.json" in names
    assert "SOURCES.txt" in names
    assert "LICENSE.txt" in names


def test_pdf_export_requires_a_valid_course_package(fixture_package, tmp_path, monkeypatch):
    def fake_print(_course_dir, output_pdf, _browser_executable=None):
        Path(output_pdf).write_bytes(b"%PDF-1.7 test")

    monkeypatch.setattr("course_platform.export._print_with_playwright", fake_print)
    output = export_course_pdf(fixture_package, tmp_path / "fixture.pdf")
    assert output == tmp_path / "fixture.pdf"
    assert output.read_bytes().startswith(b"%PDF")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_export.py -q`

Expected: FAIL because export functions and CLI commands are missing.

- [ ] **Step 3: Implement validated ZIP export**

Run package validation before export. Include manifest-listed files, `SOURCES.txt`, `LICENSE.txt`, `CHANGELOG.md`, HTML, assets and downloads. Exclude PNG previews unless the caller explicitly requests them. Never include `.env`, SQLite files, logs or prompt files containing secrets.

- [ ] **Step 4: Implement Playwright PDF export**

Open the course index in headless Chromium, wait for fonts and local assets, use print CSS, write to the requested path, and raise a readable `ExportError` when Playwright or Chromium is unavailable. Do not silently claim a PDF was created.

- [ ] **Step 5: Implement CLI commands and help text**

Use `argparse` subcommands with required positional paths and explicit errors. `create-code` prints the raw code once and stores only its hash. `publish` validates before copying and calls `sync_course()` to update SQLite.

- [ ] **Step 6: Run export tests and CLI smoke tests**

Run: `python -m pytest tests/test_export.py -q`

Expected: PASS. Also run `python -m course_platform.cli --help` and `python -m course_platform.cli validate tests/fixtures/course-package`. When Git metadata is available, commit with `git add course_platform/export.py course_platform/cli.py tests/test_export.py pyproject.toml && git commit -m "feat: add course export and publishing commands"`.

### Task 8: 制作首发课程内容包

**Files:**
- Create: `content/courses/agent-development/manifest.json`
- Create: `content/courses/agent-development/index.html`
- Create: `content/courses/agent-development/chapters/01.html` through `10.html`
- Create: `content/courses/agent-development/SOURCES.txt`
- Create: `content/courses/agent-development/LICENSE.txt`
- Create: `content/courses/agent-development/CHANGELOG.md`
- Create: `tests/test_agent_course_package.py`

**Interfaces:**
- The package must pass `load_course_package()` and `validate_course_package()` without network access.
- Chapter paths and titles must match the manifest exactly.

- [ ] **Step 1: Create the course metadata and chapter outline**

Use these exact chapter titles in the manifest:

```text
01 Agent、工作流与大语言模型的关系
02 Prompt、结构化输出与工具调用
03 单 Agent 的基本执行循环
04 上下文管理、记忆与状态
05 RAG 与外部知识接入
06 规划、反思与任务分解
07 多 Agent 协作模式
08 Agent 评测、日志与可观测性
09 安全、成本和失败处理
10 综合实战：构建一个可用的 Agent 应用
```

- [ ] **Step 2: Write each chapter against the content checklist**

Each chapter must contain a learning objective block, 4～7 subsections, at least one concrete example, one visual structure, one exercise, a summary and no forward teaser. Technical examples must identify their runtime assumptions and version where relevant.

- [ ] **Step 3: Record sources and licenses**

For every external reference, record title, URL, author or organization, access date, use type and license condition in `SOURCES.txt`. Use self-authored examples whenever a source cannot be commercially reused.

- [ ] **Step 4: Add AI disclosure and product metadata**

Set `contains_ai_generated_content` to `true`, include the approved disclosure text, set version `0.1.0`, mark chapter 1 as free preview, and include the support link and update date in the course detail metadata.

- [ ] **Step 5: Validate the commercial course package**

Run: `python -m pytest tests/test_agent_course_package.py -q`

Expected: PASS, with ten chapters, valid links, required files and no external scripts or images.

- [ ] **Step 6: Commit the content checkpoint when Git metadata is available**

Run: `git add content/courses/agent-development tests/test_agent_course_package.py && git commit -m "content: add Agent development pilot course"`.

### Task 9: 完成端到端验收、运营手册和交付准备

**Files:**
- Create: `tests/test_end_to_end.py`
- Create: `docs/course-operations.md`
- Create: `docs/customer-support.md`
- Modify: `使用手册.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- A clean temporary directory can be populated, published, accessed by code, progressed through, and exported without touching repository secrets or unrelated files.

- [ ] **Step 1: Write the end-to-end test**

```python
def test_publish_redeem_learn_progress_and_export(tmp_path, fixture_course):
    settings = make_test_settings(tmp_path)
    published = publish_course(fixture_course, settings.content_root, load_course_package(fixture_course).manifest)
    sync_course(load_course_package(published).manifest, published, settings.database_path)
    access_service = AccessService(settings.database_path)
    raw_code = access_service.create_access_code("fixture-course")
    client = TestClient(create_app(settings))

    assert client.get("/courses/fixture-course").status_code == 200
    assert client.get("/learn/fixture-course/chapters/1").status_code == 403
    assert client.post("/access/redeem", data={"course_slug": "fixture-course", "code": raw_code}).status_code == 303
    assert client.get("/learn/fixture-course/chapters/1").status_code == 200
    assert client.post("/api/progress", json={"course_slug": "fixture-course", "chapter_number": 1, "completed": True}).status_code == 204
    assert export_course_zip(published, tmp_path / "fixture.zip").exists()
```

- [ ] **Step 2: Run the complete test suite**

Run: `python -m pytest -q --cov=course_platform --cov-report=term-missing`

Expected: all tests pass; the new package has coverage for manifest validation, access control, public routes, protected routes, progress and export behavior.

- [ ] **Step 3: Write the operating runbook**

Document exact commands for creating the virtual environment, installing dependencies, configuring `.env`, validating a course, publishing a course, creating a code, starting the site, exporting PDF/ZIP, rotating secrets and backing up SQLite/content directories.

- [ ] **Step 4: Write customer support procedures**

Document responses for invalid code, code already used, course not visible, mobile layout issue, PDF download issue, course update, refund or access revocation. Include the minimum information support staff should request without collecting API keys or unnecessary personal data.

- [ ] **Step 5: Perform the pilot acceptance checklist**

Test the course on a narrow mobile viewport, a desktop browser, a private browser window, a second browser session, an invalid code, a reused code, an expired code and a missing chapter. Record results in `docs/course-operations.md`.

- [ ] **Step 6: Update project guidance and create the final checkpoint**

Update `CLAUDE.md` with the new commands, directories, content package rules and secret boundary. Run `python -m course_platform.cli validate content/courses/agent-development`, then commit with `git add course_platform tests content docs pyproject.toml .env.example .gitignore CLAUDE.md 使用手册.md && git commit -m "feat: ship commercial course delivery MVP"` when Git metadata is available.

## Plan Self-Review

- Spec coverage: Tasks 1–3 cover security, explicit paths and existing-engine integration; Tasks 2, 4 and 7 cover course packages, versions, sources, license and exports; Tasks 5–6 cover catalog, redemption, protected learning, progress and responsive UI; Task 8 covers the ten-chapter pilot; Task 9 covers operations and commercial acceptance.
- Placeholder scan: the plan contains no `TODO`, `TBD`, “implement later”, or unspecified implementation step.
- Interface consistency: `Settings`, `CourseManifest`, `CoursePackage`, `ValidationReport`, `GenerationOptions`, access service methods and `create_app()` are defined before consumers use them.
- Review focus coverage: every listed failure mode has an owning task and an explicit test.
- Repository state: the current workspace has no `.git` metadata, so commit commands are documented as checkpoints to run after repository initialization; implementation can proceed without those commands until then.
