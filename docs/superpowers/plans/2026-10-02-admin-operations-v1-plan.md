# Coursmith Admin Operations v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付单管理员网页运营后台，闭合商品准备、店铺订单、一次性发码、持久学习权益、访问恢复与售后的完整链路。

**Architecture:** 保留 FastAPI、Jinja 和 SQLite，按业务域拆分服务及路由；服务事务是状态转换和审计的唯一入口，网页与兼容 CLI 不各自写授权数据。商品销售政策、激活凭证、持久权益和设备会话分离；新增记录使用明确政策，旧记录经可恢复迁移和人工核验接入。课程正文不在本批编辑，交付路由按发行快照和内容安全检查读取既有文件。

**Tech Stack:** Python >=3.11；现有 FastAPI/Jinja2/Pydantic/SQLite/pytest；新增 `pwdlib[argon2]`、`tinycss2>=1.4,<2`；沿用 Playwright 做真实浏览器验收，开发依赖补 `build`。

**Spec:** [已确认的 M1 规格](../specs/2026-10-02-admin-operations-v1-design.md)。执行者先完整阅读规格与本计划；后续制作/版本发布见 [阶段路线](../../roadmaps/2026-10-02-coursmith-next-stage-roadmap.md)，不在本批实现。

状态：用户已批准实施计划及分任务实现、逐项独立审查方式，2026-10-02 开始执行。此文中的接口、文件、测试是验收约定，不表示全部功能已存在或测试已通过。

### Controller schema alignment ruling（Task 5 fix1）

历史 v001/v002 保持不可变。Task 5 fix1 新增 `migrations/v003_product_lifecycle.py`，本阶段最新版本为 3：允许 draft/archived 商品不绑定课程，active/paused 仍必须绑定真实课程；自动导入商品允许如实记录 `created_by=NULL`，导入可先于 owner 初始化，不伪造 owner，不赋予默认销售政策。人工商品写入仍必须通过 enabled owner 的 Actor。导入使用调用方事务，保留既有商品、历史 ID/路径/发行快照和引用图。已有库升级必须使用不覆盖的备份，重建和验证在同一事务内完成，保留外键、约束及父侧 link triggers。

Task 10 原定 `v003_legacy_delivery.py` 改为 `v004_legacy_delivery.py`，届时最新版本为 4；Task 10/11/12 中所有旧版 v3 legacy 迁移目标、示例和 fresh 启动要求均由 v4 取代（例如 `before-v3.db` → `before-v4.db`、`through_version=3` → `through_version=4`）。Task 11 的 fresh 扫描/商品导入先于 owner 初始化；Task 12 的打包、恢复及 runbook 按版本 4 验证。legacy 转换仍仅属于 Task 10，本轮不实现。AdminService 使用安装代码的 latest schema 检查就绪，拒绝较旧或未知历史。

Task 5 分类管理列表每页 20 项，选择器使用独立完整 lookup；无绑定草稿必须可以归档。

## Global Constraints

### Binding additive delivery storage amendment（Task 6, supersedes future version numbers above）

Task 5 的 lifecycle v003/latest3 不变。Task 6 新增不可变的 `migrations/v004_delivery_storage.py`，本阶段 latest4；历史 v001/v002/v003 不得修改。Task 10 的 legacy conversion 改为 `v005_legacy_delivery.py`/latest5；Task 11/12 的 fresh factory、restore、wheel 和 runbook 最终按 latest5 检查，所有较早 legacy v3/v4 名称和版本例子由此取代。Task 6 只实现 issuance，不实现 Task 7/9 的兑换、订单状态转换或 Task 10 的 legacy 转换。

已批准的存储契约：`code_batches/access_codes/entitlements/orders.issued_policy_json` 保存完整严格校验的 `IssuedPolicy`（商品/课程身份、slug/version/hash、title/support 和独立 online/PDF/ZIP/access/update-policy）。NULL 仅表示历史未核验，不生成默认快照，不写明文 CS/LK/session ID。Task 6 为新批次和新码填写快照；Task 7/9 分别填写权益、订单快照并消费原发行承诺。`code_batches.revision` 从 1 起；单码修改及批量修改递增批次修订号，Task 7 兑换也须递增源批次修订号以使批量表单失效。

订单保留历史 `status='paid'/'refunded'` 和所有外部退款字段，新增 nullable `paid_at`、`delivery_state`、`delivered_at`；`delivery_state` 允许 `recorded/code_ready/delivered/activated`，历史行保持 NULL。Task 7 的 activated 和 Task 9 的业务状态使用独立 delivery_state，退款仍通过原 status 表示；Task 9 自己实现状态转换，并核对订单唯一发行和一订单一份权益。`CodeService.issue_in_tx` 的调用方拥有 BEGIN IMMEDIATE、事务提交/回滚及回滚后的域拒绝审计，传入订单登记的原始 IssuedPolicy，不以商品当前政策替换；消费方不得绕过可售检查。

v004 为纯 additive DDL。已有 v003 升级必须先以不覆盖目标做 SQLite backup；升级/版本行/图验证原子提交，失败整体回滚，重跑保持幂等，保留所有历史记录、哈希、引用、时间、快照、索引/视图/触发器及迁移行；后台就绪检查拒绝旧版或未知 schema，不自动升级生产库。Task 6 的 `test_batch_replace_keeps_redeemed_items` 当前只模拟 used_at，Task 7 必须改为真实兑换，再复验权益/凭证/会话/进度均保留。

- 单管理员，`role=owner`；业务写入记录 `actor_admin_id`；不做邀请、注册、多人权限界面、站内支付或公开生成服务。
- 一商品对应一课程，一订单对应一个商品/一份权益；不删除课程、订单、已使用码和权益；M1 不改正文、不重排/删除章节、不承诺自动升级。
- 商品状态 `draft → active → paused → active`，另有归档；现有课程对应商品初始 `draft`；只有销售检查有效的 `active` 商品可新发码。暂停不妨碍已发码兑换和有效权益访问。
- 列表每页 20 项；访问期限显式选择 1–3650 天或无固定到期日；更新政策 `current_version`。激活有效期 1–365 天、默认 30 天；数量严格整数 1–200、默认 1；备注最多 1000 字。
- 激活码 `CS-` + `secrets.token_urlsafe(24)`；学习凭证 `LK-` + `secrets.token_urlsafe(32)`；管理/买家会话随机源 32 字节；只存 SHA-256 哈希，明文仅随首次成功响应交付，不存 CSV 文件/localStorage、不进入 URL/日志/审计。
- 买家会话默认 72 小时且不超过权益期限；同权益最多三个有效会话，第四个须确认退出最早会话；恢复、重置不清空进度、不延长权益。
- 密码 12–128 字符，Argon2id；管理员会话绝对 8 小时、闲置 30 分钟；登录预会话 10 分钟；更改密码验证当前密码并撤销全部旧管理会话。
- 管理 Cookie `coursmith_admin`，Path=/admin，HttpOnly，SameSite=Strict；买家 Cookie 继续 `course_session_{slug}`，Path=/，HttpOnly，SameSite=Lax。各自 CSRF Cookie 同作用域，生产环境全部 Secure；所有业务写操作 POST + CSRF，进度 API 也须验证。
- 管理员登录账号和来源各自五次失败/15 分钟，限制 15 分钟；兑换/恢复共用按来源十次请求/分钟的公开凭证限流桶，持久存库；默认不信任转发头。
- 流式先限制再解析：表单 64 KiB，进度 JSON 16 KiB；后台、凭证结果、授权页面及下载 `no-store`；后台脚本仅自有 `/static/admin/`，正文 `script-src 'none'`。
- 金额人民币分、严格非负整数；渠道+店铺+外部订单号唯一；“退款标记”仅记录已在店铺核验的退款，不调用支付接口。输入/存储 UTC，后台显示 Asia/Shanghai。
- 所有修改表单带修订号，陈旧写入 409；发码/补发/退款用幂等键；重放只能得到脱敏结果。成功审计同事务，拒绝审计在失败事务结束后独立提交。
- 幂等查重先于修订号/业务状态判断：相同键和规范化请求摘要返回原操作元数据，不被首次操作改变的 revision 阻断；同键不同请求409。幂等表不得缓存码或凭证结果HTML。
- 执行前重跑基线并盘点现有未提交改动，禁止 `git add .`、重置或覆盖已有改动；涉及同一文件仅暂存本任务改动块。阶段提交不代表可部署，中间状态不可写生产库。
- 若执行时采用隔离 worktree，先确保基线包含本轮之前尚未提交的修复，不能从旧 HEAD 开始而遗漏它们；基线归属或暂存范围不明确时先说明再确认。使用 `superpowers:using-git-worktrees` 创建隔离区，本计划阶段不创建。

## Review Focus

1. 发码响应丢失/双击/刷新：只创建一批，不能重显明文，未使用项可安全补发，已兑换项不受影响。Task 6、9、12。
2. 退款、兑换、恢复或第四设备同时发生：只有合法状态提交，不留下退款后仍能访问的会话或第二份权益。Task 7–9。
3. 同课程多个旧会话、已过期进度和缺失文件：不合并买家、不猜归属、不延长权限、不清空证据；损坏内容不可交付。Task 1、2、10、11。
4. 中文商品名、表格公式前缀、恶意文案/URL、Asia/Shanghai 跨日：显示不失真、不执行脚本/公式、不把领取期当学习期。Task 5、6、9、12。
5. 分块超大请求、重复表单字段、代理头伪造、目录/符号链接/素材间接引用：提前拒绝，限流不能绕过，付费素材不因试学泄漏。Task 2、3、11。

---

## 文件职责、约定与执行顺序

| 边界 | 文件及职责 |
| --- | --- |
| 数据基础 | `domain.py` 共享值类型/业务异常；`database.py` 连接与事务；`migrations/` 增量迁移；`audit.py` 脱敏事件 |
| 内容检查 | `content.py` 原验证入口；`content_inspection.py` 包指纹、HTML/CSS 素材依赖、受控文件读取 |
| 请求安全 | `security.py` 请求体/Origin/来源/限流；`admin/security.py` 持久 CSRF；`admin/auth.py` 密码/管理会话 |
| 运营 | `operations/products.py` 商品与分类；`codes.py` 发码/补发；`orders.py` 订单与退款编排；`legacy.py` 旧记录人工核验 |
| 交付 | `delivery/entitlements.py` 兑换/统一授权/撤销；`recovery.py` 恢复/重置；`progress.py` 权益级进度 |
| HTTP | `admin/routes/{auth,products,codes,orders,entitlements,legacy,dashboard}.py`；`routes/{catalog,access,learning}.py`；`app.py` 仅组合、lifespan、公共错误/安全头 |
| 页面 | `templates/admin/` 内部运营；现有公开模板和新增恢复/凭证/章节页面；`static/admin/{admin.css,admin.js}` 只负责交互，不作授权决定 |
| 验证与运维 | `tests/` 单元/HTTP/迁移/浏览器/安装验证；`docs/operations/admin-v1-runbook.md`；`pyproject.toml` 包资源及依赖 |

新 Python 包均加空 `__init__.py`：`migrations`、`admin`、`admin/routes`、`operations`、`delivery`、`routes`，随首次使用的任务创建，禁止生成大量空占位模块。

共享类型在 Task 1 定义：`Clock = Callable[[], datetime]`；`Actor(admin_id: int, request_id: str)` 是可信后台/CLI身份；`BusinessError(code: str, message: str, status_code: int)` 是可展示的脱敏异常。服务构造统一 `Service(db_path: Path, *, clock: Clock = utc_now)`，拥有事务的方法使用 `with transaction(...)`；跨域原子操作调用明确的 `*_in_tx(connection, ...)`，不嵌套连接/事务。

测试时钟 `FrozenClock.now() -> datetime`、`advance(**timedelta_kwargs) -> None` 固定初始 `2026-10-02T00:00:00Z`。`tests/conftest.py` 按任务增加真实服务 fixture：`db_path`、`clock`、`actor`、`active_product`、各域 `*_service`；HTTP `admin_client`/`buyer_client` 使用不同 Cookie jar。fixture 不绕过销售检查、CSRF或权益验证；错误/过期场景可直接篡改测试库，不降低生产校验。

所有路由模块导出 `router: APIRouter`。HTTP测试在Task 11组合前用 `make_http_app(routers: list[APIRouter], settings: Settings, services: dict[str, object]) -> FastAPI` 挂载真实路由和模板；之后用生产工厂复验。生产和测试一致使用 `app.state.settings/templates/admin_service/csrf_service/rate_limiter/product_service/code_service/entitlement_service/recovery_service/progress_service/order_service/legacy_service`，不得以测试专用授权捷径代替域服务。

下面代码块给出每任务的最小红灯测试，文字列出的额外场景也须实现测试。统一 import 由各 Files/Interfaces 决定，fixture在所属任务补齐；不复制完整算法到计划中。

命令均从仓库根目录执行，以下使用 `.venv/Scripts/python.exe`；执行者先验证解释器可运行，受沙箱阻止时按平台权限机制处理，不误判为代码错误。每项测试先红后绿；只允许导入缺失或断言失败作为预期红灯，环境错误不是红灯。每项提交前查看 `git diff --cached`，新增文件用 `git add -- <本任务文件>`，已有文件用 `git add -p -- <本任务文件>`。

### Task 1: 可恢复的增量数据库与事务审计基础

**Files:** Create `course_platform/domain.py`、`audit.py`、`migrations/__init__.py`、`migrations/v001_baseline.py`、`migrations/v002_operations.py`、`tests/test_migrations.py`、`tests/test_audit.py`；Modify `database.py`、`cli.py`、`tests/conftest.py`。

**Interfaces:** `transaction(path: Path, *, immediate: bool = False) -> ContextManager[sqlite3.Connection]`；`backup_database(source: Path, target: Path) -> None`；`migrate_database(path: Path, *, backup_path: Path | None = None, through_version: int | None = None) -> MigrationReport(from_version: int, to_version: int, backup_path: Path | None)`；`append_event(connection: sqlite3.Connection, event: AuditEvent) -> None`；`record_denial(db_path: Path, event: AuditEvent) -> None`。`AuditEvent` 字段：`actor_admin_id: int | None, object_type: str, object_id: str, action: str, reason: str, outcome: Literal['success','denied'], request_id: str, changes: dict[str, object]`，按事件动作白名单允许字段，禁止任意原始请求进入 changes。

- [ ] 写失败测试 `test_migration_preserves_legacy_tables_and_is_repeatable`：用旧 SCHEMA 建六表并放课程/码/会话/进度，迁移至 v2 两次；断言六表记录及原哈希不变、版本只两条、`PRAGMA foreign_key_check` 空。`test_failed_migration_rolls_back` 注入中途异常，断言版本/业务行未部分提交、备份可打开。

```python
def test_migration_is_repeatable(legacy_db, tmp_path):
    first = migrate_database(legacy_db, backup_path=tmp_path / "before.db", through_version=2)
    second = migrate_database(legacy_db, through_version=2)
    assert (first.from_version, first.to_version) == (0, 2)
    assert (second.from_version, second.to_version) == (2, 2)
    with transaction(legacy_db) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_migrations.py tests/test_audit.py -q`，确认缺失接口/原子性断言失败。
- [ ] 实现 v1 基线及 v2 加表/字段：按规格 §8 建全部运营表与 nullable 兼容字段，另加 `csrf_challenges`（nonce哈希、scope、到期/消耗时间）和 `operation_requests`（actor+action+幂等键唯一、请求摘要、对象 ID；无明文结果）。`products.course_id` 唯一且草稿可空；码编号唯一；权益 `source_code_id`/非空 `order_id` 唯一；恢复凭证每权益最多一项未撤销；表单可变实体含 `revision`。权益/旧码补 `legacy_state`、`purpose`、`verified_at/by/reason`，用于明确核验而非推测购买；`courses.package_hash`及发行包指纹可空仅兼容旧记录。订单金额/状态、进度 bool、FK及唯一性在库内约束，删除业务行不提供接口。
- [ ] 实现连接 `busy_timeout=3000` 毫秒、FK、确定关闭；保留 `connect(path) -> Connection` 供旧测试，新增事务工具承担 BEGIN/提交/回滚。迁移不用会隐式提交的 `executescript` 混入事务；既有库升级必须有未覆盖的备份目标，SQLite backup API 创建一致副本并校验，未知版本拒绝。CLI `migrate --backup PATH` 执行，`--check-only` 仅报告版本/完整性，不修改源库；Task 10 再接 v3 数据迁移。
- [ ] 写并通过 `test_business_and_success_audit_roll_back_together`、`test_denial_survives_failed_business_transaction`、`test_busy_returns_bounded_error`，断言失败后无成功事件、有脱敏拒绝事件；完整运行该任务测试，全部 PASS。
- [ ] 范围暂存并提交 `git commit -m "feat: add recoverable database migrations and atomic audit"`。

### Task 2: 可售内容指纹与试学素材边界

**Files:** Create `course_platform/content_inspection.py`、`tests/test_content_inspection.py`；Modify `content.py`、`pyproject.toml`、`tests/test_content.py`。

**Interfaces:** 沿用 `validate_course_package(path: Path) -> ValidationReport`；新增 `inspect_package(root: Path) -> PackageInspection(course_id: str, slug: str, version: str, fingerprint: str, preview_assets: frozenset[str], all_assets: frozenset[str], pdf_ready: bool, zip_ready: bool)`；`read_verified_file(root: Path, relative_path: str, expected_fingerprint: str | None) -> bytes`；验证失败抛 `BusinessError`，均不修改课程。

- [ ] 写失败测试 `test_preview_assets_include_only_transitive_free_dependencies`：免费章引用公共 CSS，CSS 引用免费图；付费章另有 secret 图。断言前两项进入 preview 白名单，secret 不进入；`test_package_fingerprint_changes_on_body_or_download_edit` 断言修改正文/PDF都变更指纹，读同一文件夹排序不同不改变结果。

```python
def test_paid_only_asset_is_not_public(package_with_free_and_paid_assets):
    result = inspect_package(package_with_free_and_paid_assets)
    assert {"assets/shared.css", "assets/free.png"} <= result.preview_assets
    assert "assets/paid.png" not in result.preview_assets
    assert "assets/paid.png" in result.all_assets
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_content_inspection.py tests/test_content.py -q`，确认预期失败。
- [ ] 实现 `inspect_package`：以包内排序后的 POSIX 相对路径+文件字节形成 SHA-256，排除目录自身时间戳；HTML 解析器提取 src/srcset/link 等本地资源，CSS 使用 `tinycss2` 解析 url/@import并递归闭包，加入运行依赖。CSS库只提供语法树，是否允许URL由本项目白名单判定。[tinycss2 官方说明](https://doc.courtbouillon.org/tinycss2/stable/)
- [ ] 引用先解码再规范化，拒绝根外、反斜杠、协议外链和符号链接；HTML解析后继续校验禁用标签、事件属性及危险URL，不能只依赖匹配带引号的正则。只把被正文实际嵌入的资源纳入试学闭包，不将任意 `<a>` 附件下载误当公开素材。PDF 检查非空 PDF 文件头，ZIP 检查可打开且含清单要求项、无危险成员路径；不在本批重建 PDF/ZIP。
- [ ] 实现 `read_verified_file`：检查包安全及非空预期指纹相等，规范化路径后读取受控文件；拒绝任何路径段符号链接，返回实际读到的字节。读取与核验期间文件变动则报不可用，不校验旧字节后再让 FileResponse 重新打开未经核验文件。
- [ ] 增加参数测试 `test_asset_path_escape_is_rejected`（`../`、编码 parent、反斜杠、symlink、CSS循环）和 `test_unsafe_changed_body_is_not_served`；重复 CSS 依赖可去重而循环不死锁。上述测试及旧内容测试全部 PASS。
- [ ] 范围暂存并提交 `git commit -m "feat: inspect course snapshots and restrict preview assets"`。

### Task 3: 共享请求保护、持久 CSRF 与限流

**Files:** Create `course_platform/security.py`、`admin/__init__.py`、`admin/security.py`、`tests/test_request_security.py`；Modify `settings.py`、`.env.example`、`tests/test_settings.py`。

**Interfaces:** 消费 Task 1 事务/拒绝审计；`read_limited_body(request: Request, limit: int) -> bytes`（async）；`parse_unique_form(body: bytes) -> dict[str, str]`；`source_key(request: Request, trusted_proxy_cidrs: tuple[str, ...]) -> str`；`check_origin(request: Request, site_origin: str) -> None`。`CsrfService.issue_challenge(scope: str) -> str`；`consume_challenge(scope: str, form_token: str, cookie_token: str) -> None`；`verify_bound_csrf(form_token: str, cookie_token: str, stored_hash: str) -> None`。`RateLimiter.check_login(account_key: str, source_key: str) -> None`、`record_login_failure(account_key: str, source_key: str) -> None`、`check_public(source_key: str) -> None`。

- [ ] 写失败测试 `test_stream_stops_before_parsing_oversize_body`，断言 64 KiB+1 表单/16 KiB+1 JSON返回 413、不继续消费后续分块；`test_duplicate_security_fields_are_rejected`，重复 product_id/code/csrf_token 均 400，不选择首项绕过校验。

```python
def test_public_limit_survives_restart(db_path, clock):
    limiter = RateLimiter(db_path, clock=clock.now)
    for _ in range(10):
        limiter.check_public("source-hash")
    with pytest.raises(BusinessError) as err:
        RateLimiter(db_path, clock=clock.now).check_public("source-hash")
    assert err.value.status_code == 429
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_request_security.py tests/test_settings.py -q`，确认预期失败。
- [ ] 实现流式限制、严格 URL-encoded 表单和同源 POST 校验；不接受未知 Content-Type 或把 JSON 当表单。CSRF 比较使用恒定时间函数，校验 cookie/表单/token哈希三者；挑战十分钟到期，成功消费不能重放。会话绑定 nonce 更新由认证/恢复域的事务负责，不提供“只要有 CSRF 就有权限”的路径。
- [ ] 失败表单响应重新发有效公开挑战，保留仅非敏感字段；登录/兑换/恢复不能把已消费的nonce再放回错误页而导致下一次提交必失败。GET产生技术挑战、更新会话活动时间不等于修改商品/订单等业务状态。
- [ ] 持久限流按 DB 时间窗原子计数；五次失败后登录账号和来源各阻断 15 分钟，公开凭证请求第十一次 429；返回 Retry-After、拒绝审计不含输入凭证。新增 `Settings.site_origin: str`/`trusted_proxy_cidrs: tuple[str,...]`，默认开发 origin `http://127.0.0.1:8000` 和空代理列表；环境键 `COURSE_SITE_ORIGIN`、`COURSE_TRUSTED_PROXY_CIDRS`，生产要求显式 HTTPS origin。不能复用指向 DeepSeek 的 `base_url` 作为课程站地址。
- [ ] 增加并通过 `test_limits_survive_service_restart`、`test_account_and_source_limits_are_independent`、`test_forwarded_headers_are_ignored_unless_peer_is_trusted`、`test_csrf_expiry_and_cross_session_reuse_fail`；代理链从右向左只跳过受信 CIDR，恶意左端值不能覆盖首个不受信来源。设置测试改为生产显式 origin，保留 API Key 不外露断言。
- [ ] 范围暂存并提交 `git commit -m "feat: enforce bounded requests csrf and persistent rate limits"`。

### Task 4: 单 owner 身份与可用的后台壳

**Files:** Create `admin/auth.py`、`admin/routes/__init__.py`、`admin/routes/auth.py`、`templates/admin/{base,login,password,error}.html`、`static/admin/admin.css`、`tests/test_admin_auth.py`、`tests/test_admin_auth_routes.py`；Modify `cli.py`、`pyproject.toml`、`tests/conftest.py`。花括号表示逐个真实文件。

**Interfaces:** 消费 Task 3 CSRF/限流。`AdminService.initialize_owner(username: str, password: str) -> int`；`login(username: str, password: str, *, source: str, request_id: str) -> AdminSessionGrant(token: str, csrf_token: str, expires_at: datetime)`；`require_session(raw_token: str, *, request_id: str) -> AdminSession(admin_id: int, csrf_hash: str, expires_at: datetime)`；`logout(raw_token: str, actor: Actor) -> None`；`change_password(actor: Actor, current_password: str, new_password: str) -> None`。路由依赖 `require_owner(request: Request) -> AdminSession`、`require_admin_post(request: Request) -> tuple[Actor, dict[str,str]]`（async）统一校验身份、Origin、CSRF及限额请求体。

- [ ] 写失败测试 `test_password_change_revokes_all_admin_sessions`，初始化 12 字符密码、两次登录，改密码后旧 token 全失败且需重新登录；边界 11/129 字符拒绝、12/128 允许。`test_admin_routes_reject_anonymous_and_buyer_cookie` 断言匿名、买家 Cookie、退出/过期管理员不能访问后台。

```python
def test_admin_idle_timeout(admin_service, clock):
    admin_service.initialize_owner("owner", "example-pass-123")
    grant = admin_service.login("owner", "example-pass-123", source="source-hash", request_id="r1")
    clock.advance(minutes=30)
    with pytest.raises(BusinessError):
        admin_service.require_session(grant.token, request_id="r2")
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_admin_auth.py tests/test_admin_auth_routes.py -q`，确认预期失败。
- [ ] 用 `pwdlib[argon2]` 实现 Argon2id、大小写规范化账号唯一、无公开注册且第二次初始化拒绝；登录每次新 token，存哈希及 CSRF哈希、绝对/闲置时限，数据库读取 clock。CLI `init-admin` 用 input/getpass确认密码，不接受密码参数；CLI 后续业务身份也通过账号+getpass核验，不选择任意 owner ID 当认证。
- [ ] 实现 GET/POST `/admin/login`、POST `/admin/logout`、GET/POST `/admin/account/password`；未初始化 503，已初始化匿名页面跳登录、业务 POST 无身份 401/403，不执行操作。Cookie 精确按 Global Constraints；所有管理响应含 no-store；后台壳含导航、错误关联 ID 和后续阶段文字，无不可用制作按钮。认证服务记录失败事件，绝不记录密码及 token。
- [ ] 增加并通过 `test_admin_absolute_and_idle_expiry`、`test_login_pre_csrf_and_origin_are_required`、`test_login_rotates_token_and_production_cookie_is_secure`、`test_init_admin_password_never_appears_in_argv_or_output`；运行本任务测试全 PASS。
- [ ] 范围暂存并提交 `git commit -m "feat: add owner login and secure admin session lifecycle"`。

### Task 5: 商品、分类与可售检查页面

**Files:** Create `operations/__init__.py`、`operations/products.py`、`admin/routes/products.py`、`templates/admin/{products,product_form,product_detail,categories}.html`、`tests/test_products.py`、`tests/test_product_routes.py`；Modify `tests/conftest.py`。

**Interfaces:** 消费 `Actor`、`PackageInspection`。在 products.py 定义严格 Pydantic `AccessPolicy(access_mode: Literal['days','no_fixed_expiry'], access_days: int | None, online: bool, pdf: bool, zip: bool, update_policy: Literal['current_version'])`；days 必须 1–3650，no_fixed_expiry 必须 null。`ProductInput(title: str, category_id: int, synopsis: str, audience: str, prerequisites: str, outcomes: list[str], course_id: str | None, ai_disclosure: str, support_text: str, channels: list[SalesChannel(name: str, url: str)], policy: AccessPolicy | None)`，草稿可缺销售项。`ProductRecord(id: int, revision: int, status: str, data: ProductInput)`；`IssuedPolicy(product_id: int, course_id: str, course_slug: str, version: str, package_hash: str, access: AccessPolicy, title: str, support_text: str)`；`SalesChecklist(quality: bool, sources: bool, ai: bool, mobile: bool, downloads: bool)`。

`ProductService.create(actor: Actor, data: ProductInput) -> ProductRecord`；`update(actor: Actor, product_id: int, revision: int, data: ProductInput) -> ProductRecord`；`activate(actor: Actor, product_id: int, revision: int, checks: SalesChecklist) -> ProductRecord`；`set_status(actor: Actor, product_id: int, revision: int, status: Literal['paused','archived']) -> ProductRecord`；`require_sale_ready_in_tx(connection: sqlite3.Connection, product_id: int) -> IssuedPolicy`；`list_products(*, status: str | None, category_id: int | None, title: str, page: int) -> tuple[list[ProductRecord], int]`；`save_category(actor: Actor, category_id: int | None, revision: int | None, slug: str, name: str, sort_order: int, enabled: bool) -> int`；`ensure_draft_in_tx(connection: sqlite3.Connection, course_id: str) -> ProductRecord`仅供导入，既有商品不覆盖。

- [ ] 写失败测试 `test_sale_requires_explicit_policy_and_matching_approval`，草稿缺政策不能 activate；1/3650天与显式无固定到期日可通过；编辑成果/PDF或修改包后 `require_sale_ready_in_tx` 拒绝。`test_stale_product_revision_returns_conflict`：相同 revision 两次编辑第二次 409、第一次数据不变。

```python
def test_stale_product_revision_returns_conflict(product_service, active_product, actor):
    data = active_product.data.model_copy(update={"title": "新的商品标题"})
    updated = product_service.update(actor, active_product.id, active_product.revision, data)
    with pytest.raises(BusinessError) as err:
        product_service.update(actor, active_product.id, active_product.revision, data)
    assert err.value.status_code == 409
    assert updated.revision == active_product.revision + 1
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_products.py tests/test_product_routes.py -q`，确认预期失败。
- [ ] 实现商品服务：检查安全包、published、实际下载文件和全部 checklist；销售检查保存包指纹及规范化承诺字段哈希。恢复 active 也重新确认；绑定/文案/格式/期限变更使旧检查失效，但不改已发码快照。无历史绑定的草稿可选择课程；历史快照引用的旧 course_id/path 必须保留，不能从当前商品关系推导旧买家的课程。
- [ ] 实现 `/admin/products`、`/new`、`/{id}`、`/{id}/activate`、`/pause`，补 `/archive`；分类页创建/改名/排序/停用，只允许安全 slug 和 HTTPS 渠道链接，被引用分类不删除。详情显示校验失败原因、格式和领取/学习期限区别；GET 不转换业务状态。含 revision 的 POST 用比较更新，HTML全自动转义。
- [ ] 增加并通过 `test_paused_product_preserves_issued_snapshots`、`test_category_rename_does_not_change_course_id`、`test_markup_and_javascript_shop_urls_are_not_executable`、`test_product_listing_has_twenty_items_per_page`；`active_product` fixture 按真实检查产生可售课程商品。
- [ ] 范围暂存并提交 `git commit -m "feat: manage products categories and sale readiness in admin"`。

### Task 6: 一次性明文发码、批次与安全补发

**Files:** Create `operations/codes.py`、`admin/routes/codes.py`、`templates/admin/{code_batches,code_batch_form,code_batch_detail,issued_codes}.html`、`static/admin/admin.js`、`tests/test_codes.py`、`tests/test_code_routes.py`。

**Interfaces:** 消费 `IssuedPolicy`/`ProductService.require_sale_ready_in_tx`。`BatchInput(product_id: int, count: StrictInt = 1, purpose: Literal['sale','test','gift'], activation_days: StrictInt = 30, note: str = '')`；`IssuedCode(public_id: str, raw_code: str, expires_at: datetime)`，raw_code 禁止 repr/log/序列化入审计；`BatchReceipt(batch_id: int, codes: tuple[IssuedCode,...], replayed: bool)`，重放 codes 空。`CodeService.issue_batch(actor: Actor, data: BatchInput, idempotency_key: str) -> BatchReceipt`；`issue_in_tx(connection: sqlite3.Connection, actor: Actor, policy: IssuedPolicy, data: BatchInput, *, order_id: int | None, idempotency_key: str) -> BatchReceipt`；`revoke(actor: Actor, code_id: int, revision: int, reason: str) -> None`；`replace(actor: Actor, code_id: int, revision: int, reason: str, idempotency_key: str) -> BatchReceipt`；`replace_unused(actor: Actor, batch_id: int, revision: int, reason: str, idempotency_key: str) -> BatchReceipt`；`revoke_unused(actor: Actor, batch_id: int, revision: int, reason: str) -> int`。

- [ ] 写失败测试 `test_issue_replay_never_returns_plaintext_twice`：同 actor/action/key同数据重放返回同 batch_id、空 codes、只一批；同键不同数据409。参数测试 count 1/200成功，0/201/True/1.5拒绝；activation_days 1/365成功，0/366拒绝。

```python
def test_issue_replay_never_returns_plaintext_twice(code_service, active_product, actor):
    data = BatchInput(product_id=active_product.id, purpose="sale", count=200)
    first = code_service.issue_batch(actor, data, "batch-key-1")
    replay = code_service.issue_batch(actor, data, "batch-key-1")
    assert len(first.codes) == 200
    assert len({c.raw_code for c in first.codes}) == 200
    assert replay.batch_id == first.batch_id and replay.replayed
    assert replay.codes == ()
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_codes.py tests/test_code_routes.py -q`，确认预期失败。
- [ ] 实现 `BEGIN IMMEDIATE` 下销售检查、幂等记录、政策/课程指纹快照、随机哈希码、批次与审计；不得提交后才补审计。码默认有效期30天，学习期不从发码日起计算。作废仅 unused；补发原 unused 作废和替代码插入同事务、关联 replacement ID并继承原发行快照，不偷换已承诺政策；商品暂停/检查失效时不新发替代码，提示先处理可售检查。
- [ ] 实现批次列表/表单/详情以及码 revoke/replace；补 `/admin/code-batches/{id}/revoke-unused`、`/replace-unused`。批量补发仅当前未使用项，不撤已兑换权益；说明明文丢失不可找回。结果页有复制与 CSV，外部 JS读取已自动转义的 DOM当前数据、Blob下载后释放URL；不内联JSON/JS、不写存储、不增加历史明文下载端点。CSV 引号、换行、UTF-8 中文正确；首个有效字符为 `= + - @` 的危险文本字段加安全前缀。
- [ ] 增加并通过 `test_batch_replace_keeps_redeemed_items`（已兑换行留待 Task 7 用真实兑换复验）、`test_issue_audit_failure_leaves_no_codes`、`test_code_response_and_history_do_not_leak_secrets`、`test_csv_formula_prefix_is_neutralized`；数据库/日志/输出目录检索原始码为空，仅首次响应含码且 no-store。
- [ ] 范围暂存并提交 `git commit -m "feat: issue one-time code batches with audited replacement"`。

### Task 7: 持久权益、原子兑换与进度服务

**Files:** Create `delivery/__init__.py`、`delivery/entitlements.py`、`delivery/progress.py`、`tests/test_entitlements.py`、`tests/test_entitlement_progress.py`。

**Interfaces:** 消费 `IssuedPolicy` 和 `read_verified_file`。`SessionGrant(session_id: str, csrf_token: str, entitlement_id: int, course_id: str, session_expires_at: datetime)`；`RedemptionReceipt(session: SessionGrant, raw_recovery_key: str, entitlement_expires_at: datetime | None)`，两类敏感字段禁 repr/log。`AuthorizedSession(session_hash: str, entitlement_id: int, course_id: str, session_expires_at: datetime, entitlement_expires_at: datetime | None, csrf_hash: str | None, issued_policy: IssuedPolicy | None)`，null仅旧未核验权益。`EntitlementService.redeem(raw_code: str, *, expected_course_id: str | None, request_id: str) -> RedemptionReceipt`；`require_session(raw_token: str, course_id: str) -> AuthorizedSession`；`revoke(actor: Actor, entitlement_id: int, revision: int, reason: str, idempotency_key: str) -> None`；`revoke_in_tx(connection: sqlite3.Connection, actor: Actor, entitlement_id: int, reason: str) -> None`；`ProgressService.set_completed(raw_token: str, course_id: str, chapter_number: int, completed: bool) -> None`；`get_progress(raw_token: str, course_id: str) -> dict[int,bool]`。

跨域复用另定义 `require_session_in_tx(connection: sqlite3.Connection, raw_token: str, course_id: str) -> AuthorizedSession`、`create_session_in_tx(connection: sqlite3.Connection, entitlement_id: int, *, evict_oldest: bool) -> SessionGrant`。前者用于同事务进度写入，后者供恢复域复用设备上限/撤销检查；`EntitlementService`/`RecoveryService`构造另接 `session_ttl_hours: int = 72`，由已校验Settings传入，不各自重实现会话算法。

- [ ] 写失败测试 `test_concurrent_redeem_creates_exactly_one_entitlement`：两个独立连接线程争同码，断言一成功一通用拒绝、一权益/活跃凭证/初始会话，used_at一致。`test_access_expiry_uses_issued_policy_not_current_product`：发码后修改政策并暂停，码仍按旧政策从兑换时到期，恢复不改变该日期。

```python
def test_completed_value_is_a_real_bool(entitlement_service, progress_service, issued_code):
    receipt = entitlement_service.redeem(issued_code.raw_code, expected_course_id=None, request_id="redeem-1")
    token, course = receipt.session.session_id, receipt.session.course_id
    progress_service.set_completed(token, course, 1, True)
    assert progress_service.get_progress(token, course) == {1: True}
    with pytest.raises(BusinessError):
        progress_service.set_completed(token, course, 1, "false")
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_entitlements.py tests/test_entitlement_progress.py -q`，确认预期失败。
- [ ] 实现兑换单事务：核对码、绑定课程/快照、领取到期、未作废/未用、内容未破坏；写 used_at、权益、LK凭证哈希、会话/CSRF哈希、订单 activated（如有）、成功事件。课程来源是码发行快照，不是商品当前绑定；订单 refunded不可兑换。并发由条件更新/唯一约束+立即事务兜底；失败结束后独立写拒绝事件。
- [ ] 实现统一授权：会话、权益到期/撤销、课程ID、发行版本指纹全检查；暂停不影响旧权益。会话截止 `min(now+session_ttl_hours, entitlement_expiry)`，默认72h。进度在同一立即事务内重新授权并写权益主键，严格拒绝 bool章节号/非bool完成值及不存在章节；撤销保留进度，撤凭证及所有会话并同事务审计。旧 null政策权益路径限在线且受原截止限制，Task 10 补明确核验。
- [ ] 增加并通过 `test_expired_revoked_and_cross_course_codes_are_denied_and_audited`、`test_revoke_then_progress_write_is_denied_without_new_progress`、`test_policy_without_fixed_expiry_requires_explicit_choice`、`test_progress_is_owned_by_entitlement_not_browser`；Task 6 已兑换批次测试改用真实服务。兑换与退款竞争归Task 9，不在权益尚未创建时凭空要求撤销其ID。
- [ ] 范围暂存并提交 `git commit -m "feat: persist learner entitlements and transactional progress"`。

### Task 8: 凭证恢复、设备上限与网页售后

**Files:** Create `delivery/recovery.py`、`admin/routes/entitlements.py`、`templates/admin/{entitlement_detail,credential_result}.html`、`tests/test_recovery.py`、`tests/test_entitlement_routes.py`。

**Interfaces:** 消费 Task 7 类型/授权/撤销。`RecoveryService.restore(raw_key: str, *, evict_oldest: bool = False, request_id: str) -> SessionGrant`；`reset(actor: Actor, entitlement_id: int, revision: int, reason: str, idempotency_key: str) -> CredentialReceipt(entitlement_id: int, raw_key: str | None, replayed: bool)`；会话满且未确认抛 `BusinessError('device_confirmation_required', ..., 409)`。重放 raw_key为空；敏感字段禁 repr/log。

- [ ] 写失败测试 `test_fourth_device_requires_confirmation_and_preserves_progress`：前三次有效登录（含首次兑换）成功，第四次未确认409且不新增；确认后只有最早会话失效，最多三个；新会话看到同权益进度。`test_reset_revokes_old_credentials_and_sessions_without_extending_access`：旧凭证/旧会话全失败、新凭证成功、进度和截止不变。

```python
def test_fourth_session_needs_confirmation(recovery_service, redeemed):
    recovery_service.restore(redeemed.raw_recovery_key, request_id="restore-2")
    recovery_service.restore(redeemed.raw_recovery_key, request_id="restore-3")
    with pytest.raises(BusinessError) as err:
        recovery_service.restore(redeemed.raw_recovery_key, request_id="restore-4")
    assert err.value.code == "device_confirmation_required"
    confirmed = recovery_service.restore(redeemed.raw_recovery_key, evict_oldest=True, request_id="restore-4-ok")
    assert confirmed.entitlement_id == redeemed.session.entitlement_id
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_recovery.py tests/test_entitlement_routes.py -q`，确认预期失败。
- [ ] 实现恢复和重置 `BEGIN IMMEDIATE`：有效设备只计未过期且未撤销，调用 Task 7同事务会话创建；最早排序 created_at/session_hash作为确定并列规则。满设备确认页不放凭证URL/隐藏“永久确认”状态，不服务器保存明文；要求再次输入凭证和勾选确认。重置要求经营者填写核验依据/原因，展示影响范围；sale权益须有已核验订单/核验记录，test/gift须核对发行用途及人工确认；legacy未核验不可直接重置，关联订单见Task 9、核验补录见Task 10。
- [ ] 实现 GET `/admin/entitlements/{id}`、POST `/reset-credential` 和 `/revoke`：详情展示进度、政策、原期限、会话数量和核验来源；重置/撤销都 revision + 幂等 + 确认。明文结果仅成功响应、no-store；重放展示已完成说明并允许明确重新重置，不暗中补凭证。
- [ ] 增加并通过 `test_concurrent_restores_never_exceed_three_sessions`、`test_reset_response_loss_does_not_reveal_old_key`、`test_restore_after_revoke_is_generic_and_contains_no_order_id`、`test_owner_reset_requires_recorded_purchase_verification`；所有本任务测试 PASS。
- [ ] 范围暂存并提交 `git commit -m "feat: restore learner access and handle credential support"`。

### Task 9: 店铺订单、发货文案与退款原子编排

**Files:** Create `operations/orders.py`、`admin/routes/orders.py`、`templates/admin/{orders,order_form,order_detail}.html`、`tests/test_orders.py`、`tests/test_order_routes.py`；Modify `templates/admin/issued_codes.html`。

**Interfaces:** 消费 Task 6 `issue_in_tx`、Task 7 `revoke_in_tx`。`OrderInput(channel: str, shop_id: str, external_order_id: str, product_id: int, paid_cents: StrictInt, paid_at: datetime, note: str)`；`OrderRecord(id: int, revision: int, status: str, data: OrderInput)`；`OrderService.record(actor: Actor, data: OrderInput, idempotency_key: str) -> OrderRecord`；`issue(actor: Actor, order_id: int, revision: int, idempotency_key: str) -> BatchReceipt`；`confirm_delivery(actor: Actor, order_id: int, revision: int, idempotency_key: str) -> OrderRecord`；`attach_code(actor: Actor, order_id: int, revision: int, public_code_id: str, verification_reason: str, idempotency_key: str) -> OrderRecord`；`record_refund(actor: Actor, order_id: int, revision: int, reason: str, idempotency_key: str) -> OrderRecord`；`delivery_text(policy: IssuedPolicy, code: IssuedCode, site_origin: str) -> str`。

- [ ] 写失败测试 `test_order_identity_and_issue_are_unique`：同 channel/shop/order禁止第二条，同订单 issue重放只返回原批次元数据；金额0接受，-1/True/1.2拒绝。`test_confirm_delivery_after_activation_does_not_downgrade`：code_ready直接兑换后activated，确认只补 delivered_at。

```python
def test_order_identity_is_unique(order_service, active_product, actor, clock):
    data = OrderInput(channel="taobao", shop_id="shop-1", external_order_id="order-1",
                      product_id=active_product.id, paid_cents=0, paid_at=clock.now(), note="已核验")
    first = order_service.record(actor, data, "record-1")
    with pytest.raises(BusinessError) as err:
        order_service.record(actor, data, "record-2")
    assert err.value.status_code == 409
    assert first.status == "recorded"
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_orders.py tests/test_order_routes.py -q`，确认预期失败。
- [ ] 实现手工订单、20项分页/查询、状态机及金额/政策快照。发码政策来自订单登记快照，而不是商品后来的政策；仍确认商品可售及原发行内容可履约，不能换课程。补关联必须显式核验且检查商品/发行课程/政策、码原关联、订单是否已有授权；已兑换码需在同一事务补关联其权益并检查唯一性，不能造第二权益。补 POST `/admin/orders/{id}/attach-code`；不支持多商品订单的 UI 明示。
- [ ] 实现 issue/确认/退款标记事务：所有幂等记录、修订号、变更及审计共同提交。退款要求外部确认和原因，作废该订单码链所有 unused码、撤已领取权益及会话；不调用退款API；重复请求只读已完成元数据。退款与兑换争同库锁，退款完成后任何分支不能持有可用授权。
- [ ] 实现页面：订单号仅在授权详情/表单显示，不放日志/URL/审计；列表可遮盖。一次发货结果文案含中文商品、HTTPS课程站入口、原码、两种期限、格式、售后；按Asia/Shanghai标注激活截止，不把null学习截止写成“永久”。页面显示“待人工确认已在店铺发送”。
- [ ] 增加并通过 `test_order_issue_keeps_recorded_policy_after_product_edit`、`test_refund_and_redeem_race_leaves_no_live_access`、`test_refund_audit_failure_rolls_back_revocation`、`test_attach_redeemed_code_keeps_one_entitlement`、`test_delivery_copy_distinguishes_shanghai_activation_and_access_dates`；运行 Task 6–9 测试全 PASS。
- [ ] 范围暂存并提交 `git commit -m "feat: manage store orders delivery and audited refund records"`。

### Task 10: 旧数据保全、核验与人工恢复入口

**Files:** Create `migrations/v003_legacy_delivery.py`、`operations/legacy.py`、`admin/routes/legacy.py`、`templates/admin/legacy_codes.html`、`tests/test_legacy_delivery.py`；Modify `migrations/__init__.py`、`tests/test_migrations.py`、`templates/admin/entitlement_detail.html`。

**Interfaces:** 消费 Task 2 指纹、Task 5 政策、Task 9订单。迁移最新版本改为3；`LegacyService.resolve_code(actor: Actor, code_id: int, revision: int, policy: AccessPolicy | None, purpose: Literal['sale','test','gift'], verification_reason: str, *, activation_days: int | None = None) -> None`；activation_days仅处理旧无领取截止码，必须显式1–365。`verify_entitlement(actor: Actor, entitlement_id: int, revision: int, order_id: int | None, policy: AccessPolicy, expires_at: datetime | None, purpose: Literal['sale','test','gift'], verification_reason: str, idempotency_key: str) -> None`。无固定到期政策要求null新截止，有期限则显示并确认明确截止；不得静默由默认政策延长。“测试”也要理由；校验成功不自动返回明文凭证，仍使用 Task 8 reset。

- [ ] 写失败测试 `test_legacy_sessions_are_never_merged_or_extended`：同课程两个旧会话及一过期会话各建一权益，原expires_at相等保留、三份进度独立、旧 progress还在；已用码不能按时间归属；未用码未核验不能兑换。

```python
def test_legacy_sessions_are_not_merged(legacy_db_with_three_sessions, tmp_path):
    db = legacy_db_with_three_sessions
    migrate_database(db, backup_path=tmp_path / "before-v3.db", through_version=3)
    with transaction(db) as conn:
        rows = conn.execute("SELECT entitlement_id, expires_at FROM sessions").fetchall()
        assert len({r["entitlement_id"] for r in rows}) == 3
        assert conn.execute("SELECT COUNT(*) FROM recovery_credentials").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM entitlement_progress").fetchone()[0] == 3
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_legacy_delivery.py tests/test_migrations.py -q`，确认预期失败。
- [ ] 实现v3：按已有分类名称创建安全slug分类并调用 `ensure_draft_in_tx`导入草稿商品，草稿无隐式政策；旧会话逐个迁权益，标记 legacy/pending_verification、无恢复凭证、原截止不变，复制全部进度含过期；旧 session_hash保持可识别，nullable CSRF留待学习GET补发。存在安全包保存课程/权益迁移时指纹，缺失/不安全包保留记录并标不可用，不删除源数据；已用码需核验，不能填猜测source_code_id。
- [ ] 实现 `/admin/legacy-codes` 与 `/{id}/resolve`，补 `/admin/entitlements/{id}/verify-legacy`；旧未用码要求商品政策和可售检查后可兑，保留其原领取截止（若原无截止由管理员显式选1–365天）且留审计。已用码仅标核验用途，不自动关联会话；关联/延长权益须明确选择已有权益/已核验订单/期限，并显示原值与新值；禁止因同课程合并。
- [ ] 增加并通过 `test_expired_legacy_progress_requires_verified_support`、`test_missing_course_keeps_history_but_blocks_delivery`、`test_legacy_migration_failure_can_restore_backup`；迁移源副本/新库分别校验 row counts/FK/时限，不用生产库演练。
- [ ] 范围暂存并提交 `git commit -m "feat: migrate legacy access with explicit ownership verification"`。

### Task 11: 公开交付链路、应用组合与后台总览

**Files:** Create `routes/__init__.py`、`routes/catalog.py`、`routes/access.py`、`routes/learning.py`、`admin/routes/dashboard.py`、`templates/admin/{dashboard,audit}.html`、`templates/{access_restore,access_result,learn_chapter}.html`、`tests/test_delivery_routes.py`、`tests/test_app_lifecycle.py`；Modify `app.py`、`access.py`、`database.py`、`cli.py`、`templates/{home,category,course_detail,access,learn_home,help}.html`、`static/site.css`、`tests/test_app.py`、`tests/test_access.py`、`tests/test_templates.py`。

**Interfaces:** `create_app(settings: Settings | None = None) -> FastAPI` 保持工厂签名，初始化移到 lifespan；删除 module-level `app=create_app()`；启动 `uvicorn course_platform.app:create_app --factory`/CLI factory=True。`AccessService` 保留 `require_session(raw_session_id: str, course_id: str) -> AuthorizedSession`、`get_session(raw_session_id: str) -> AuthorizedSession | None`、`get_progress(session_id: str, course_id: str) -> dict[int,bool]`、`record_progress(session_id: str, course_id: str, chapter_number: int, completed: bool) -> None` 并委托新域；`redeem_access_code(raw_code: str, course_id: str) -> RedemptionReceipt`有意替换旧只返回会话契约以交付学习凭证；`create_access_code(course_id: str, *, actor: Actor, activation_days: int = 30) -> str`有意改为严格领取天数并要求认证actor，走商品/码服务，不能生成无政策码。旧异常 `InvalidAccessCode/InvalidSession`可在包装层转换保留，但不能保留旧SQL授权逻辑；过期码测试改在测试库设置时间，不开放生成已过期码的生产接口。CLI create-code改交互认证后委托；不接受任意管理员ID。

- [ ] 写失败测试 `test_importing_app_does_not_touch_database`：新进程仅 import app，库/内容目录及已有库mtime不变；TestClient lifespan才初始化。`test_all_legacy_and_current_routes_enforce_entitlement_revocation` 参数遍历数字章/文件名章/index alias/素材/PDF/ZIP/进度/恢复，撤销后全拒绝，不留旧绕过。

```python
def test_paid_asset_is_not_available_from_preview(client_with_active_product):
    client = client_with_active_product
    free = client.get("/courses/fixture-course/assets/free.png")
    paid = client.get("/courses/fixture-course/assets/paid.png")
    assert free.status_code == 200
    assert paid.status_code in (403, 404)
    assert "script-src 'none'" in free.headers["content-security-policy"]
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_delivery_routes.py tests/test_app_lifecycle.py tests/test_app.py tests/test_access.py -q`，确认预期失败；旧兑换303测试有意改为一次结果200+课程入口，不能删掉cookie安全断言。
- [ ] 组合全部管理/公开router；fresh库按v3初始化，存在未升级库启动失败提示离线备份迁移，不启动旧授权逻辑。fresh库扫描课程后调用Task 5 `ensure_draft_in_tx`导入对应草稿商品，不能先做空库v3后遗漏导入。`sync_course`保持原签名但禁止已有ID的版本/章节/路径覆盖，只允许完全相同清单重同步；内容指纹在首次导入/迁移保存，不能启动时覆盖检查基准。CLI publish先查已有ID再复制，拒绝重复ID/改版，防止落地文件后才报错。迁移期间不自动用新清单覆盖原进度映射。lifespan测试使用TestClient上下文触发。[FastAPI lifespan 测试说明](https://fastapi.tiangolo.com/advanced/testing-events/)
- [ ] 后台首页只显示实际待核验/草稿/交付统计和近期脱敏审计，GET `/admin/audit` 分页；未实现阶段仅文字说明。各业务列表/详情的查询仅选择模板必需字段，不返回任何凭证哈希或原文；订单明文仅授权页可见，审计按 actor/action/object/time过滤，不能输出包含密钥的任意数据库行。
- [ ] 公开目录只显示 active/paused 商品实际文案，draft/archived不暴露；暂停隐藏购买且保留售后。兑换页允许选择或按输入码安全识别发行商品/课程；POST校验公开挑战、Origin、限流及期望course_id，200一次显示LK和进入课程按钮。恢复页使用同样挑战/限流及显式设备确认；结果不带凭证query，所有失败消息通用且不回填秘密；输入凭证不出现在模板context调试日志。
- [ ] 学习GET使用统一授权，补旧会话CSRF并存哈希，不改变业务权益；首页/章节页有真实无JS完成/取消按钮和上一章/下一章/目录。章节通过受控读取呈现，不把模型原始HTML塞进后台；章节布局复用经验证的 head/body和已校验资源路径，不用iframe/脚本。数字/文件名入口一致、相对素材URL指向正确受保护路径；公开试学只能取Task 2白名单，draft/archived试学不公开，但旧买家的learn不依赖公开商品状态。
- [ ] POST章progress及 `/api/progress`严格校验会话、课程、CSRF和正文类型；学习正文/进度/素材查online开关，PDF/ZIP分别查权益快照格式开关及文件/指纹，不只查存在。POST授权失败403、无资源404、指纹损坏503/明确不可用；安全头按管理script-self/正文script-none区分，凭证/授权响应无缓存。普通异常有request_id、日志仅固定错误类型/业务ID，不打印包含原始请求的traceback局部值。
- [ ] 增加并通过 `test_no_js_progress_and_device_restore_share_progress`、`test_paid_asset_is_not_available_from_preview`、`test_download_exists_but_policy_denies_it`、`test_csrf_api_cannot_bypass_form_protection`、`test_cli_create_code_requires_owner_and_sale_ready_product`；全文单元/HTTP/旧流水线测试全部 PASS。原“重同步变版本不丢进度”测试保留数据断言，同时新增M1拒绝原地改版的测试，不为绿灯放行内容变更。
- [ ] 范围暂存并提交 `git commit -m "feat: integrate secure browser delivery and operations dashboard"`。

### Task 12: 浏览器闭环、安装包与交接演练

**Files:** Create `tests/test_browser_admin_flow.py`、`tests/test_wheel_install.py`、`tests/test_release_safety.py`、`docs/operations/admin-v1-runbook.md`；Modify `pyproject.toml`、`使用手册.md`、`.env.example`、`tests/conftest.py`。

**Interfaces:** 使用Task 11工厂与Task 4初始化接口；包内templates/static通过包相对路径定位；不引入新的业务API或真实模型账户。新增pytest marker `browser`/`packaging`，CI/本地完整验收不能靠默认skip伪装通过。

- [ ] 写失败浏览器测试 `test_owner_and_buyer_complete_delivery_without_daily_cli`：仅初始化一次用CLI/服务，网页登录→完善真实商品检查→录入订单→发码/复制文案/CSV→不同浏览器买家兑换保存LK→完成章节→新设备恢复→后台重置→撤销。断言真实状态、CSV只来自首次响应、不能从历史取明文；`test_mobile_preview_and_no_js_progress`用390px视口和JS禁用上下文检查可读/可操作，无横向溢出或完成按钮失效。

```python
@pytest.mark.browser
def test_mobile_preview_has_no_horizontal_overflow(mobile_page, live_site):
    # mobile_page fixture: viewport 390x844，真实浏览器、已激活商品的测试站。
    mobile_page.goto(live_site + "/courses/fixture-course/chapters/1")
    assert mobile_page.locator("body").is_visible()
    assert mobile_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
```

- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_browser_admin_flow.py -m browser -q`，准备本地测试服务器与已安装 Chromium；缺浏览器属环境准备失败而非测试通过。
- [ ] 声明 setuptools package-data含全部 `templates/**/*.html`/`static/**/*`，加入build开发依赖、固定这次验收实际依赖版本的安装记录（不导出本地路径/密钥）。`test_wheel_installed_site_has_all_pages_and_assets`在pytest临时目录构建wheel、临时venv安装，从仓库外cwd启工厂，断言后台/公开模板及admin.css/admin.js加载，无源码树回退；构建产物不提交。
- [ ] 写并通过 `test_secrets_are_absent_from_persistence_logs_and_artifacts`扫描模拟流程后的库/捕获日志/持久输出目录，排除仅测试持有的首次响应；`test_every_mutating_route_rejects_missing_csrf`逐个列举所有POST路径，以匿名/买家/owner身份作黑盒请求，断言非法请求无业务变更；登录/兑换/恢复也不得漏过挑战/限流。重放、事务故障和恢复竞争按Review Focus再对照，不用测试数量替代覆盖。
- [ ] 执行 `.venv/Scripts/python.exe -m pytest -m "not browser and not packaging" -q`、`.venv/Scripts/python.exe -m pytest -m "browser or packaging" -q`、`.venv/Scripts/python.exe -m ruff check .`；三者成功方可完成，警告有归因。构建 `.venv/Scripts/python.exe -m build --wheel` 并执行安装测试；本批无真实LLM调用。
- [ ] 完成runbook：首次初始化、站点origin/代理/HTTPS配置、旧库维护窗口备份→副本迁移→新程序校验→切换、失败恢复旧兼容副本、密钥响应丢失处理、核验购买后重置、外部退款标记、下载政策、日常网页流程。备份包含库及匹配课程文件快照；在测试副本做一次恢复演练，写明M1可受控试用但M2制作/M3发布/M4公开运营尚未交付。
- [ ] 范围暂存并提交 `git commit -m "test: verify admin delivery browser flow and packaged installation"`，再做一次全分支独立评审；有未解决授权/迁移阻断项不宣告完成。

## 验收覆盖与交付检查

规格 §11 的编号映射：1→Task 3/4/11；2→3/5/6/11；3/4→6/12；5/6→7；7/8→7/8/11；9→5/7/11；10→6/9；11→1/10/11；12→1/6/7/9；13→2/11/12；14→11/12；15→12。Review Focus五项均有指定失败测试，不存在单靠人工感觉判定的授权要求。

可交接结果：单owner后台、商品/分类、订单/批次、权益/旧码核验、审计、恢复/进度/受控下载及真实浏览器演示；迁移备份与恢复记录、测试/安装结果、操作手册。这里不承诺样例正文已达到商业可售质量，内容审查由经营者逐商品确认。

执行依赖：1→2→3→4→5→6→7→8→9→10→11→12。内容检查与请求保护虽可独立开发，本计划先按顺序交付，避免多个执行者同时改共享数据/路由。Task 12后仍有独立全分支检查，不能以阶段提交或页面能打开替代安全/业务验收。

## 计划自审记录

已逐节对照已批准规格，核对：政策/码/权益/会话的分离；所有写操作的身份、CSRF和审计；无历史明文重显；订单状态竞态；旧会话与旧码不猜归属；发行快照不依赖当前商品；M1禁原地改版；wheel资源和import副作用。尚未执行任何本计划的实现或测试。评审后按所选执行方式开始Task 1，若实施中发现需求缺口先记录，不扩大到M2/M3。
