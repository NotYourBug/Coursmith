# Coursmith M2A Dual-mode Production Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 单管理员在网页以题目自动制作或主题加可选资料的方式生成、编辑和检查课程草稿，可靠保存任务与内容，交接给后续审核发布阶段。

**Architecture:** 沿用 FastAPI/Jinja2/SQLite，在独立 production 与 jobs 包中建立修订、任务和注入式模型边界。源正文在数据库，网络调用在独立 worker，草稿不进入现有公开课程目录；所有自动结果遵守输入快照、租约和候选采用规则。

**Tech Stack:** Python >=3.11，现有 FastAPI/Jinja2/Pydantic/SQLite/OpenAI 兼容客户端、pytest/Playwright/build；安全 Markdown 渲染新增 markdown-it-py 与 nh3，随 Task 4 验证版本并记录锁定安装结果。

**Spec:** [M2A 书面规格](../specs/2026-10-08-production-workbench-m2a-design.md)，用户已在本对话明确批准，必须完整阅读；M1 代码基线为 `da46a21`，规格文档提交为 `95c0f80`。文件顶部审批状态尚未更新，本轮保留它及其未提交数值修改，执行前统一固化。不得用历史 M1 计划替代本计划的任务契约。

状态：2026-10-08 编写并自审，待用户审批；保留用户先前选择的“分任务实现＋逐项独立审查”方式。尚未写功能代码、安装依赖或调用模型。

规格一致性说明：工作区规格存在未提交的唯一数值调整：章节范围 `1–30` → `1–60`，默认仍为 10。本计划依据该工作版本（撰写时 SHA-256 `F7BA3ED1889E51EFABBA3C0398E533FC63277AC0F8A60B3DEEFA34E52175F616`），不覆盖或擅自提交该调整；用户审批本计划时一并确认 60 章上限，执行前固化规格。默认 200,000 token 额度不自动提高，60 章请求可能在预检中因额度或上下文不足被拒绝。

## Global Constraints

- 单 owner 内部制作，预留 actor_admin_id；不做多管理员页面、收费 SaaS、支付、订单采集、生产部署、真实库升级或 M3A 发布。
- 类型扩展方向 `course|guide|handbook`，本期只开放 course；参考资料可选，无素材上传/抓取/文件导入，不强制非技术课程编程练习。
- 快速模式只填题目，自动展开大纲到草稿，不伪造人工确认；精细模式当前大纲确认失效时不能批量生成；两种模式均不自动发布。
- 课程章数默认 10、范围 1–60（见上方调整说明）；题目 1–200 Unicode 字符，去除空白后不得为空。
- 单项需求文本最多 2,000 字符、全部需求文本最多 10,000 字符；参考资料最多 10 条、正文合计最多 20,000 字符、每条来源说明最多 1,000 字符。
- 单章源正文或候选 UTF-8 最多 200 KiB；请求体最多 2 MiB；上限针对解码正文与原始请求分别检查，不把 URL 编码膨胀误算为正文大小。
- 大纲模型输出最多 4,000 输出 token；章节每调用最多 4,000 输出 token；连接测试最多 32 输出 token、一次请求、无自动重试。
- 单任务 token 预留上限默认 200,000；输入后备估算为完整请求正文 UTF-8 字节数加 2,048 token 协议余量；缺上下文限额则拒绝生成，不按模型名猜测。
- 单次模型调用总截止 120 秒；SDK 隐式重试关闭；可证明未接受的短暂失败最多两次重试，等待 2 秒、8 秒；普通 5xx、超时/断流未知不自动重发。
- 一个 Web、一个独立 worker，同一时刻一个任务/模型请求；每项目最多一个 queued/running 修改任务；每 10 秒独立心跳、租约 60 秒、120 秒无心跳才标 interrupted。
- 状态 `queued → running → succeeded|failed|cancelled|interrupted`；终态不重新变 running，恢复创建关联新任务；部分失败不误报 succeeded，过期 token 不能写活动内容。
- SQLite 源正文及不可变修订为事实来源；增量 schema6，不改 v001–v005、不迁移启动中的旧库、不改 M1 课程身份/文件/政策/权益/进度。
- 后台 no-store、同站 Origin/CSRF/当前管理会话、对象归属校验；普通日志/审计不存题目、正文、资料、提示词、密钥、凭证或 SDK 异常原文。
- 模型配置服务端维护；任务不保存密钥，模型/端点/执行条件漂移停止，密钥轮换不把旧密钥复制到任务。
- 新批次只处理草稿和交接快照，生成完整/结构通过均不是销售审核；模型来源标待核查，不断言事实正确或授权合法。
- 保留桌面原工作区及规格的未提交调整；不得 git add .、reset/clean、覆盖旧生成入口或借实现任务修复范围外 M1.1 问题。

## Review Focus

1. 中文/emoji/百分号编码的 200 KiB 正文、chunked 请求、重复字段：可合法保存但超限及重复字段先拒绝，不能通过加大所有后台限额来解决。Task 2、9。
2. worker 被杀在“已提交供应商请求但尚未存响应”之间：计费保持未知、没有自动重发，重启只维护状态，旧 token 不覆盖人工编辑。Task 5、7、8、12。
3. 排队后模型/端点变化、流式响应持续有字节但超过 120 秒：停止而不是换模型/无限续命；新配置不吞掉既有预留。Task 6、7。
4. 大纲重排/重做、同标题章节和跨项目候选 ID：稳定身份、人工映射及归属检查阻止串章，候选不覆盖当前正文。Task 3、9、11。
5. 合法 60 章或 20,000 字参考资料超出默认额度、人工接受未知调用重试风险：明确区分输入合法与可执行、历史 unknown 不记零，不能静默截断或提高预算。Task 6、8、10。

---

## 文件职责与共享契约

| 边界 | 文件方向 |
| --- | --- |
| 生产文本模型 | production/models.py、briefs.py：严格输入、值类型、需求与参考修订 |
| 内容修改 | production/projects.py、drafts.py：项目、稳定章节、大纲确认、正文与候选 |
| 内容呈现/交接 | production/rendering.py、checks.py、snapshots.py：安全渲染、结构检查、不可变快照 |
| 持久任务 | jobs/models.py、repository.py、service.py：事务、幂等、租约、状态 |
| 外部调用 | production/config.py、prompts.py、llm.py；jobs/budget.py：配置快照、适配器、额度 |
| 执行/恢复 | jobs/worker.py、handlers.py、recovery.py：固定任务编排、取消与恢复 |
| 管理界面 | admin/routes/production.py、jobs.py、production_forms.py；templates/admin/production/、jobs/ |
| 运行/验收 | app.py、cli.py、migrations/v006_production.py；tests/production/、jobs/及现有回归 |

新包 __init__.py 随首次有实际消费者的任务创建，不提前生成空页面。production 不 import jobs；任务服务通过明确的 in_tx 接口协调两域。服务使用现有 Actor、Clock、BusinessError 与 transaction(db_path, immediate=True)；成功审计同事务，拒绝审计在回滚后独立保存，不嵌套写事务。

路径约定：Files 中 production/、jobs/、admin/、templates/、static/ 以及 app.py、cli.py、database.py、audit.py 都以 course_platform/ 为根；tests/、docs/、pyproject.toml、.env.example、CLAUDE.md 以仓库为根。同一个 Files 条目中省略目录的 test_*.py 位于该条明确列出的同域测试目录。例如 Task6 的 test_llm.py 是 tests/production/test_llm.py，不是根目录测试文件。执行者按此规则展开，不创建仓库根下的同名业务包。

### 类型与测试基础（定义于首次使用任务）

- Task 2 production/models.py：BriefInput(title, audience='', prerequisites='', goal='', difficulty='', chapter_count=10, category_id=None, content_kind='course')；ReferenceInput(text, source_note='', source_status='unprovided')；ProjectView(id:int, revision:int, mode:str, status:str, brief_revision:int, brief:BriefInput)；ReferenceView(id:int, revision:int, data:ReferenceInput)。source_status 仅 unprovided/pending/owner_noted，人工核查说明不能冒充法律认证。Pydantic 严格输入，禁止 bool 冒充整数、禁止额外字段。
- Task 3 同文件：OutlineInput(items:list[OutlineItem])，OutlineItem(chapter_id:str|None, title:str, goal:str)；OutlineView(id:int, revision:int, project_id:int, items:tuple[OutlineItem,...], confirmed:bool)；ChapterView(id:str, body_revision:int|None, markdown:str)；GeneratedResult(kind:str, revision_id:int, candidate_id:int|None)。模型返回的 chapter_id 丢弃，由服务端分配。
- Task 3：FrozenReference(id:int, revision:int, data:ReferenceInput)；ProductionInput(project_id:int, project_revision:int, mode:str, brief_revision:int, brief:BriefInput, references:tuple[FrozenReference,...], outline:OutlineView|None, chapters:tuple[ChapterView,...], input_hash:str)。hash 包含冻结文本及引用ID/修订，正文不进入普通审计。大纲确认只绑定 brief/reference/outline 修订的单独哈希，正文编辑不误使大纲确认失效。
- Task 4：Finding(code:str, chapter_id:str|None, blocking:bool)；RenderedChapter(html:str, findings:tuple[Finding,...])；DraftCheck(input_hash:str, findings:tuple[Finding,...], blocking:bool, human_review_required:bool=True)。不在错误 metadata 中带原正文。
- Task 5 production/models.py：ModelPublic(provider:str, endpoint_fingerprint:str, model:str, context_tokens:int|None, prompt_version:str, outline_output_tokens:int=4000, chapter_output_tokens:int=4000)。jobs/models.py：JobRequest(kind:str, project_id:int|None, project_revision:int|None, chapter_ids:tuple[str,...]=(), parent_job_id:int|None=None)；JobView(id:int, revision:int, state:str, reason_code:str|None)；Claim(job_id:int, attempt_id:int, attempt_token:str, lease_until:datetime, actor:Actor, input:ProductionInput|None, config:ModelPublic)。本任务已定义 ModelPublic，不能依赖尚不存在的 Task6 模块。
- Task 6 production/config.py：PriceSnapshot(input_per_million:Decimal, output_per_million:Decimal, currency:str, as_of:date)；ProductionSettings(public:ModelPublic, base_url:str, api_key:str, task_token_limit:int=200000, timeout_seconds:int=120, input_margin:int=2048, price:PriceSnapshot|None=None)。base_url/api_key 不使用默认 repr，不进入 job JSON；未配置可诊断，只有执行模型任务时拒绝。每个调用冻结非敏感price以估算金额，后来改价不重算历史为真实账单。
- Task 6 production/llm.py：Usage(input_tokens:int, output_tokens:int)；ModelRequest(messages:tuple[dict[str,str],...], max_output_tokens:int, config:ModelPublic)；ModelResult(text:str, usage:Usage|None, provider_request_id:str|None)。ModelFailure(code:str, accepted:bool|None) 不保存异常原文；调用状态区分 successful、rejected、unknown。ModelClient Protocol 声明 async generate(request:ModelRequest, *, deadline_seconds:int=120)->ModelResult，SDK与测试替身均实现它。
- Task 11：SnapshotView(id:int, project_id:int, hash:str, payload_json:str, check:DraftCheck)；数据库序列化为 UTF-8、sort_keys=True、separators=(',', ':'), ensure_ascii=False，禁 NaN/Infinity；数组按大纲次序，源字符串不隐式 Unicode 标准化。

测试新增局部 tests/production/conftest.py 与 tests/jobs/conftest.py，复用现有 db_path、clock、actor、product_owner、real_browser 和 server_factory，不大改全仓 conftest。ScriptedModel 为 Task 6 的测试替身：async generate(request:ModelRequest, *, deadline_seconds:int)->ModelResult，支持按调用次序结果/异常、屏障、请求计数和已发/未保存注入；不是生产配置入口。

下文核心测试片段中 fixture 在其任务内实现：Task2 project_service；Task3 draft_service；Task5 job_repository、claimed_job（有效queued任务领取后的Claim，已初始化owner）；Task6 model_config、token_budget；Task7 quick_run（worker/jobs/drafts/model/project_id/job_id，10章的确定性成功结果）；Task8 unknown_run（recovery/job_id/call_id/revision 与原调用status、reserved_tokens读取）；Task9 production_client（真实登录和同站Origin）、局部conftest；Task10 workbench_page（已登录并进入已有章编辑页面，real_browser）；Task11 snapshot_ready（snapshots/drafts/actor/project_id/project_revision/chapter_id/body_revision）；Task12 worker_help_result；Task13 installed_workbench（fresh wheel、运行目录、web_response）。片段省略正常import，但不省略关键断言；其他列出的测试也必须落实。

## 执行与审查规则

按 Task 1–13 顺序执行；任务间共享数据库、应用组合和安全边界，不并行写代码。每项真实 RED → 最小实现 → GREEN → 针对该项的独立规格/质量审查 → 必要修正 → 明确路径提交；审查未通过不开始下一项。整批再做全分支独立审查和完整验收。

执行前复核已绑定工作树及当前分支，重读规格/计划并核对未提交变更归属；固化通过审批的 60 章调整。复用现有托管 checkout，不从桌面旧 HEAD 开新实现基线。先做一次非浏览器/打包基线及既有环境盘点；完整验收留在最终整批，避免每任务重复 35 分钟全量。不将环境失败当 RED，不弱化原测试断言。

所有下列 pytest 命令在受控源码工作树执行，使用 .venv/Scripts/python.exe -X utf8；示例测试是验收形状，不是现已存在的实现。测试辅助构造器在拥有它的任务定义，不引用未落实的共享 helper。

## Task 1：增量 schema6 与 M1 数据保持

**Files:** Create course_platform/migrations/v006_production.py、tests/production/test_schema.py；Modify course_platform/migrations/__init__.py、database.py:164、tests/test_migrations.py、tests/test_app_lifecycle.py、tests/test_wheel_install.py 中仅 latest 断言；新增 tests/production/__init__.py。

**Interfaces:** Consumes migrate_database(path:Path, *, backup_path:Path|None=None, through_version:int|None=None)->MigrationReport。Produces v006_production.apply(connection:sqlite3.Connection)->None；既有迁移接口保持不变，LATEST_SCHEMA_VERSION=6。

- [ ] **Step 1 — RED tests:** test_v5_upgrade_preserves_every_m1_row：以 through_version=5 构造测试业务图，升级后 assert 所有 M1 表逐行相同、FK=[]、version=6、备份 version=5；test_v6_failure_rolls_back 与 test_snapshot_and_revision_rows_are_immutable 验证半途故障整体回滚及源/快照 UPDATE/DELETE 拒绝。test_schema.py 内定义 seed_v5_graph(path:Path)->dict[str,list[tuple]]，以现有 M1 合法 IssuedPolicy 构造并校验固定测试行，保存 owner/商品/订单/码/权益/凭证/会话/进度关联；不要调用升级后要求 latest6 的 AdminService 去初始化 schema5。测试数据无真实凭证。

```python
def test_v5_upgrade_preserves_every_m1_row(tmp_path):
    db, backup = tmp_path / "v5.db", tmp_path / "before-v6.db"
    migrate_database(db, through_version=5)
    before = seed_v5_graph(db)
    assert migrate_database(db, backup_path=backup).to_version == 6
    assert read_m1_graph(db) == before
    assert check_database(backup)["version"] == 5
    assert check_database(db)["foreign_keys"] == "ok"
```

read_m1_graph(path:Path)->dict[str,list[tuple]] 也在 test_schema.py 定义，按固定M1业务表名及PK排序只读全部列，不调用seed；不把schema_migrations算作不应变化的业务表。另断言迁移历史1–5行原样保留、只新增第6行。
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_schema.py -q`，应因未注册 v006/缺生产表失败，而不是 fixture 或安装错误。
- [ ] **Step 3 — Implement:** 在 v006 建 production_projects、brief_revisions、reference_revisions、project_reference_links、outline_revisions、outline_items、outline_confirmations、production_chapters、chapter_revisions、production_candidates、draft_checks、production_snapshots、production_snapshot_refs、production_jobs、job_attempts、job_steps、model_calls、token_reservations、job_requests、worker_heartbeats。FK/组合归属、整数 CHECK、活动项目槽位、幂等唯一键和领取索引在迁移中定义；全局partial unique约束最多一项running，避免误开第二worker同时花费。没有 project 的 connection_test 使用 NULL 项目。正文/快照 immutable，状态和当前指针单独存。v001–v005 bytes 不变。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_schema.py tests/test_migrations.py tests/test_app_lifecycle.py -q`；latest 断言改为 LATEST_SCHEMA_VERSION，测试历史5仍保持5，未来版本用 LATEST_SCHEMA_VERSION+1，不全局替换数字5。Expected: 0 failures。轮次评审检查未留下 source 表更新/跨项目 FK 漏洞。
- [ ] **Step 5 — Commit:** 仅暂存本项列出的迁移与测试路径，`git commit -m "feat: add immutable production schema migration"`。

## Task 2：制作项目、需求及可选参考资料

**Files:** Create production/__init__.py、models.py、briefs.py、projects.py、events.py、tests/production/conftest.py、test_projects.py；Modify audit.py 的动作白名单。

**Interfaces:** Consumes Actor、transaction、append_event。Produces ProjectService(db_path:Path, *, clock:Clock=utc_now)；create(actor:Actor, brief:BriefInput, *, mode:str)->ProjectView；update(actor:Actor, project_id:int, revision:int, brief:BriefInput)->ProjectView；set_mode(actor, project_id:int, revision:int, mode:str)->ProjectView；archive(actor, project_id:int, revision:int)->ProjectView；save_reference(actor, project_id:int, revision:int, reference_id:int|None, data:ReferenceInput)->ReferenceView；remove_reference(actor, project_id:int, revision:int, reference_id:int)->ProjectView；get(project_id:int)->ProjectView；list_projects(*, page:int=1, archived:bool=False)->tuple[list[ProjectView],int]。读取不返回其他管理员权限，本期 owner 入口核验。

- [ ] **Step 1 — RED tests:** test_title_only_quick_project_uses_defaults assert chapter_count=10/mode=quick、无人工确认；参数化 chapter_count=1/60 通过，0/61/True 拒绝；题目全空白及201字符拒绝；参考总量20,000通过、20,001和第11条拒绝；test_emoji_is_counted_as_unicode_but_markdown_size_as_utf8 验证字符限额与字节限额不同；test_reference_change_keeps_history_and_invalidates_confirmation；test_untrusted_actor_and_cross_project_reference_rejected；test_project_event_never_contains_input_text。

```python
def test_title_only_quick_project_uses_defaults(project_service, product_owner):
    project = project_service.create(product_owner, BriefInput(title="Agent开发"), mode="quick")
    assert project.mode == "quick"
    assert project.brief.chapter_count == 10
    assert BriefInput(title="资料", chapter_count=60).chapter_count == 60
    with pytest.raises(ValidationError):
        BriefInput(title="资料", chapter_count=61)
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_projects.py -q`，应缺服务或行为失败。
- [ ] **Step 3 — Implement:** 上述签名在 projects.py，输入校验在 briefs.py/models.py；创建 immutable 修订＋短事务推进指针。每页20，不硬删引用，course 以外当前拒绝。events.py 添加 record_production_denial(db_path:Path, actor:Actor|None, action:str, object_id:str, error:BusinessError, *, request_id:str)->None；固定动作 production.create/update/reference/mode/archive，仅 revision/error_code 元数据，不放正文到 reason。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_projects.py tests/test_audit.py -q`；Expected: 0 failures。评审校验只有 enabled owner 的业务 Actor 可写，归档/运行状态阻止不合法操作。
- [ ] **Step 5 — Commit:** 明确暂存本项 production 文件、audit.py 和测试，`git commit -m "feat: manage production briefs and reference revisions"`。

## Task 3：稳定章节、大纲确认与候选编辑

**Files:** Create production/drafts.py、tests/production/test_drafts.py；Modify production/models.py、projects.py 的输入变化失效逻辑、production/events.py、audit.py 的本项动作白名单。

**Interfaces:** Produces DraftService(db_path:Path, *, clock:Clock=utc_now)；save_outline(actor, project_id:int, revision:int, outline:OutlineInput)->OutlineView；confirm_outline(actor, project_id:int, revision:int)->OutlineView；adopt_outline(actor, project_id:int, revision:int, candidate_id:int, matches:dict[int,str|None])->OutlineView；save_chapter(actor, project_id:int, chapter_id:str, body_revision:int|None, markdown:str)->ChapterView；adopt_candidate(actor, project_id:int, revision:int, candidate_id:int, body_revision:int|None)->ChapterView；reject_candidate(actor, project_id:int, revision:int, candidate_id:int)->None；acknowledge_changes(actor, project_id:int, revision:int, chapter_ids:tuple[str,...])->None；freeze_input(project_id:int)->ProductionInput。

Worker-only interfaces（调用方先在同一事务验证 Claim，Task 7 才使用）：apply_outline_in_tx(connection, *, project_id:int, input_hash:str, outline:OutlineInput, job_id:int, attempt_id:int, auto_adopt:bool)->OutlineView；record_generated_in_tx(connection, *, project_id:int, chapter_id:str, body_revision:int|None, input_hash:str, markdown:str, job_id:int, attempt_id:int, adopt_empty:bool)->GeneratedResult。

- [ ] **Step 1 — RED tests:** test_reordering_identical_titles_preserves_chapter_ids assert 重排/改名后 ID 和正文对应不变；test_outline_regeneration_requires_explicit_mapping；test_reference_edit_invalidates_precise_confirmation；test_candidate_from_other_project_cannot_be_adopted；test_stale_save_keeps_current_revision；test_regeneration_creates_candidate_not_overwrite；test_200kib_body_is_valid_and_one_byte_more_rejected；运行中需求/参考/当前生成章禁止改，但已完成章可编辑。

```python
def test_200kib_body_is_valid_and_one_byte_more_rejected(project_service, draft_service, product_owner):
    p = project_service.create(product_owner, BriefInput(title="指南", chapter_count=1), mode="precise")
    o = draft_service.save_outline(product_owner, p.id, p.revision,
        OutlineInput(items=[OutlineItem(chapter_id=None, title="第一章", goal="入门")]))
    c = draft_service.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "x" * (200 * 1024))
    assert len(c.markdown.encode("utf-8")) == 204800
    with pytest.raises(BusinessError) as denied:
        draft_service.save_chapter(product_owner, p.id, c.id, c.body_revision, c.markdown + "x")
    assert denied.value.code == "chapter_too_large"
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_drafts.py -q`。
- [ ] **Step 3 — Implement:** 大纲项服务端 UUID，排序独立；模型响应不能指定既有 ID。确认绑定当前 brief/reference/outline hash，模式切换保留正文并失效确认。候选人工采用验证项目和基准正文修订，移除章保留历史；清除输入变化阻断须显式 acknowledge_changes，不等于正式审核。所有写入追加 production.outline/confirm/chapter/candidate/acknowledge 的受控审计。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_projects.py tests/production/test_drafts.py -q`；Expected: 0 failures。审查重点是同标题映射、确认失效、body_revision=None 空白条件，不能以单个全局 revision 掩盖跨章内容归属。
- [ ] **Step 5 — Commit:** `git commit -m "feat: add stable draft chapters and reviewed candidates"`，暂存仅本项列出的文件及对应 events.py/audit.py 白名单扩展。

## Task 4：安全 Markdown 与草稿结构检查

**Files:** Create production/rendering.py、checks.py、tests/production/test_rendering.py、test_checks.py；Modify pyproject.toml 与 docs/operations/m2a-validated-requirements.txt（本项实际安装后创建）。

**Interfaces:** render_markdown(markdown:str)->RenderedChapter；validate_draft(input:ProductionInput)->DraftCheck；validate_chapter(markdown:str, *, chapter_id:str)->tuple[Finding,...]。Consumes Task 3 的冻结输入；输出不包含正式发布 approval。

- [ ] **Step 1 — RED tests:** test_raw_html_and_code_examples_never_execute assert script/iframe/SVG/form/事件属性未进入活动 HTML、代码块里的标签转义可见；test_remote_images_are_placeholders；test_unsafe_links_and_credentials_in_url_rejected；test_render_preserves_chinese_tables_and_long_code；test_nontechnical_exercise_satisfies_course_structure；缺正文或必需部分 blocking=True，human_review_required 永远 True。

```python
def test_raw_html_and_code_examples_never_execute():
    result = render_markdown('<script>alert(1)</script>\n\n```html\n<script>example()</script>\n```')
    assert "<script" not in result.html
    assert "&lt;script&gt;" in result.html
    assert "example()" in result.html
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_rendering.py tests/production/test_checks.py -q`。缺依赖先记录环境准备，不当作业务 RED。
- [ ] **Step 3 — Implement:** 选定并安装兼容 Python3.11 的 markdown-it-py/nh3，锁定经过测试的实际版本到新 requirements 文档；不升级其他运行依赖。关闭原生 HTML，清洗标签/属性和 HTTP(S) URL；图片改待补提示，链接无认证、rel=noopener noreferrer。检查通过 Markdown token 层寻找五个 section：学习目标（或目标）、讲解、案例（或示例）、练习、总结（或小结）；不按正文包含词判断。输出审计只含 Finding.code/计数。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_rendering.py tests/production/test_checks.py -q`；Expected: 0 failures。新包无导入副作用在本项文件内另加测试，以无网络依赖确定性用例证明渲染不抓取资源。评审禁止 Markup 包装 raw 响应和放宽 CSP。
- [ ] **Step 5 — Commit:** `git commit -m "feat: safely render and validate course drafts"`，仅本项依赖记录/代码/测试。

## Task 5：持久任务、幂等、项目槽位与租约

**Files:** Create jobs/__init__.py、models.py、repository.py、service.py、tests/jobs/conftest.py、test_repository.py、test_service.py；Modify production/models.py 定义 ModelPublic，audit.py/production/events.py 增加 job.submit/cancel/interrupt 元数据白名单。

**Interfaces:** JobService(db_path:Path, drafts:DraftService, *, clock:Clock=utc_now)；submit(actor:Actor, request:JobRequest, config:ModelPublic, *, idempotency_key:str, reserved_tokens:int)->JobView；get(job_id:int)->JobView；list_jobs(*, page:int=1)->tuple[list[JobView],int]。JobRepository(db_path, *, clock)；claim_next(worker_id:str)->Claim|None；heartbeat(claim:Claim)->None；require_attempt_in_tx(connection, claim:Claim, now:datetime)->JobView；finish_in_tx(connection, claim:Claim, state:str, reason_code:str|None)->JobView；mark_interrupted(*, worker_id:str|None=None)->int。

- [ ] **Step 1 — RED tests:** test_duplicate_submit_returns_original_job_before_stale_revision assert 同键同摘要重复返回相同 id；同键异摘要409；test_two_tabs_cannot_queue_two_modifying_jobs；test_lease_expiry_fences_at_60_seconds_but_interrupts_at_120；test_worker_claim_is_exclusive_and_persistent；test_second_worker_cannot_run_other_project_simultaneously；test_precise_generation_requires_current_confirmation；拒绝未知 kind、越界章ID和归档项目，读取不改变 DB。

```python
def test_lease_expiry_fences_at_60_seconds_but_interrupts_at_120(job_repository, claimed_job, clock, db_path):
    clock.advance(seconds=60)
    with pytest.raises(BusinessError) as denied:
        with transaction(db_path, immediate=True) as conn:
            job_repository.require_attempt_in_tx(conn, claimed_job, clock.now())
    assert denied.value.code == "attempt_expired"
    assert job_repository.mark_interrupted() == 0
    clock.advance(seconds=60)
    assert job_repository.mark_interrupted() == 1
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs/test_repository.py tests/jobs/test_service.py -q`。
- [ ] **Step 3 — Implement:** 固定6种任务、规范化请求摘要、幂等先于陈旧修订判断；使用 BEGIN IMMEDIATE 与数据库唯一槽位，input/config 冻结不含密钥。审计单独增加 job_state 的严格枚举校验，不扩大既有 product/order 的 status 白名单。租约与执行 token 一起验证，过期禁止写，120秒后维护终结而不重新领取该任务；heartbeat 每10秒由 Task7执行器调度。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs/test_repository.py tests/jobs/test_service.py tests/production/test_drafts.py -q`；Expected: 0 failures。使用两真实 SQLite 连接/线程验证竞争，不能仅 mock transaction。
- [ ] **Step 5 — Commit:** `git commit -m "feat: persist fenced production jobs"`，仅本项路径及受控审计扩展。

## Task 6：模型适配、冻结配置、提示词与费用预留

**Files:** Create production/config.py、prompts.py、llm.py、jobs/budget.py、tests/production/test_model_config.py、test_llm.py、tests/jobs/test_budget.py、tests/jobs/fakes.py；Modify jobs/service.py 接入同事务额度预检/预留，tests/jobs/conftest.py 为既有任务fixture注入执行配置与预算，.env.example（非秘密示例）。

**Interfaces:** load_production_settings(settings:Settings, env:Mapping[str,str]|None=None)->ProductionSettings；public_model_config(config:ProductionSettings)->ModelPublic；build_outline_request(input:ProductionInput, config:ModelPublic)->ModelRequest；build_chapter_request(input:ProductionInput, chapter_id:str, config:ModelPublic)->ModelRequest；build_connection_request(config:ModelPublic)->ModelRequest；build_execution_plan(input:ProductionInput|None, *, kind:str, chapter_ids:tuple[str,...], config:ModelPublic)->tuple[ModelRequest,...]；serialize_model_payload(request:ModelRequest)->bytes（与适配器请求体一致，不含Authorization）；OpenAIModel(config:ProductionSettings).generate(request:ModelRequest, *, deadline_seconds:int=120)->ModelResult（async）；retry_delay(error:ModelFailure, retry_index:int)->int|None；parse_outline(text:str, *, chapter_count:int)->OutlineInput。TokenBudget(db_path:Path, *, clock:Clock=utc_now)；estimate(request:ModelRequest)->int（输入估算＋请求输出上限，不含重试倍数）；reserve_in_tx(connection, *, job_id:int, planned_tokens:int, limit:int)->None；begin_call_in_tx(connection, claim:Claim, request:ModelRequest)->int；settle_in_tx(connection, *, call_id:int, usage:Usage|None, status:str)->None。JobService 新增 configure_execution(config:ProductionSettings, budget:TokenBudget)->None（仅组合时注入，不是网页操作），提交在同事务按冻结输入重算额度，不信任调用方 reserved_tokens 更不信任表单金额。

- [ ] **Step 1 — RED tests:** test_config_drift_blocks_but_key_rotation_does_not；test_missing_context_limit_refuses_execution_not_m1_startup；test_utf8_fallback_is_payload_bytes_plus_2048；test_valid_60_chapters_can_exceed_200000_budget_without_truncation；test_retry_proven_not_accepted_uses_2_then_8_seconds；test_continuous_stream_has_total_120_second_deadline；test_unknown_usage_keeps_reservation；test_price_snapshot_is_optional_validated_and_historical；认证/5xx/超时不盲重试、坏JSON/空/200KiB+响应拒绝、repr/日志无key。

```python
def test_utf8_fallback_is_payload_bytes_plus_2048(model_config, token_budget):
    request = ModelRequest(messages=({"role": "user", "content": "中文😀"},),
                           max_output_tokens=32, config=model_config)
    assert token_budget.estimate(request) == len(serialize_model_payload(request)) + 2048 + 32
    assert retry_delay(ModelFailure("not_sent", accepted=False), 0) == 2
    assert retry_delay(ModelFailure("not_sent", accepted=False), 1) == 8
    assert retry_delay(ModelFailure("response_unknown", accepted=None), 0) is None
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_model_config.py tests/production/test_llm.py tests/jobs/test_budget.py -q`；测试时注入可控时间和传输，不付费、不实际等待120秒。
- [ ] **Step 3 — Implement:** 新 env COURSE_MODEL 默认 deepseek-chat、COURSE_MODEL_CONTEXT_TOKENS 无猜测默认、COURSE_TASK_TOKEN_LIMIT=200000；生产缺设置仅禁生成不禁M1启动。AsyncOpenAI max_retries=0，asyncio.timeout(120) 包围整次读响应，不把每包网络 timeout 当总截止；generate 一次只发一次请求，retry_delay 对已知未接受且非认证/结构失败返回2、8或None，由Task7逐次记录再调用。提示词仅生产输入，不附带任意 tools。计算请求输入＋max_output预留，每允许重试也预留；unknown保留额度。build_execution_plan 的快速模式以受限的大纲响应包络预估尚未确定的章节请求，大纲出现后重算，不把模型SDK或jobs类型导入production。费用单价由服务端可选的 COURSE_MODEL_PRICE_JSON 配置注入，字段 input_per_million/output_per_million 为非负 Decimal 字符串、currency/as_of 为非敏感币种/日期；仅金额估算，不做利润指标。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_model_config.py tests/production/test_llm.py tests/jobs/test_budget.py tests/jobs/test_service.py -q`；Expected: 0 failures。适配器不支持可靠输出限制/配置缺上下文应拒绝，usage严格非负整数；供应商已接收但usage未知不释放预留。
- [ ] **Step 5 — Commit:** `git commit -m "feat: bound model calls and track uncertain usage"`，明确暂存代码、测试和 .env.example，不暂存真实 .env。

## Task 7：独立 worker 与双模式生成编排

**Files:** Create jobs/worker.py、handlers.py、tests/jobs/test_worker.py、test_handlers.py；Modify生产 events.py/audit.py 固定 worker 事件；不在 app 中启动 worker。

**Interfaces:** Worker(repository:JobRepository, drafts:DraftService, budget:TokenBudget, model:ModelClient, config:ProductionSettings, *, worker_id:str, clock:Clock=utc_now)；run_once()->bool（async）；run_forever(stop:asyncio.Event)->None（async）。Handlers.execute(claim:Claim, *, drafts:DraftService, repository:JobRepository, budget:TokenBudget, model:ModelClient, config:ProductionSettings)->None（async）；每步调用前/结果保存前在同一写事务 require_attempt_in_tx，结合 Task3 in_tx 写正文与Task6调用账目。

- [ ] **Step 1 — RED tests:** test_quick_title_only_creates_outline_and_draft_without_confirmation；test_precise_outline_stops_before_chapter_calls；test_first_blank_body_adopts_but_existing_body_only_candidate；test_partial_failure_keeps_completed_chapters_and_job_failed；test_long_call_heartbeats_at_10_seconds；test_crash_after_send_marks_unknown_without_replay；test_late_response_after_lease_loss_only_isolated_history；每次新调用前核验 fence，参数漂移不发送。

```python
def test_quick_title_only_creates_outline_and_draft_without_confirmation(quick_run):
    assert asyncio.run(quick_run.worker.run_once()) is True
    draft = quick_run.drafts.freeze_input(quick_run.project_id)
    assert len(draft.chapters) == 10
    assert all(chapter.markdown for chapter in draft.chapters)
    assert draft.outline.confirmed is False
    assert quick_run.jobs.get(quick_run.job_id).state == "succeeded"
    assert len(quick_run.model.calls) == 11
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs/test_worker.py tests/jobs/test_handlers.py -q`。
- [ ] **Step 3 — Implement:** 单请求执行＋独立心跳 asyncio task（短 SQLite事务放线程池），运行网络时不持有DB锁。快速自动校验/采用首次大纲，冻结派生章节输入并重算额度；已有大纲重做只候选不自动替换。精细仅确认后批量章；固定其他 handler 包含重做、draft_validate和32token connection_test。每次发送前先写 model_calls.sent，仅已知未接受失败依retry_delay等待2/8再登记新call并复验token/取消/剩余额度；崩溃剩余 sent 变 unknown，绝不发出自动补请求。增加 test_safe_retries_have_distinct_call_rows_and_backoff。正常候选与过期隔离响应不同表状态，不让历史结果获得当前写权限。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs tests/production/test_drafts.py -q`；Expected: 0 failures。屏障同步的真实事务检验保存/取消/过期竞态，业务指针、调用结算和审计原子更新；审查无模拟线性百分比或默认网络重试。
- [ ] **Step 5 — Commit:** `git commit -m "feat: run durable dual-mode draft generation"`。

## Task 8：取消、未知调用核对与显式恢复

**Files:** Create jobs/recovery.py、tests/jobs/test_recovery.py；Modify jobs/service.py、jobs/worker.py 仅取消/恢复协调接口、production/events.py/audit.py 的 job.reconcile/job.resume 白名单。

**Interfaces:** RecoveryService(db_path:Path, jobs:JobService, drafts:DraftService, budget:TokenBudget, *, clock:Clock=utc_now)；cancel(actor:Actor, job_id:int, revision:int)->JobView；recovery_preview(job_id:int)->RecoveryPreview；reconcile(actor:Actor, job_id:int, call_id:int, revision:int, *, usage:Usage|None, accept_retry_risk:bool)->None；resume(actor, job_id:int, revision:int, config:ModelPublic, *, idempotency_key:str, reserved_tokens:int, confirm:bool)->JobView。RecoveryPreview 定义在 jobs/models.py，含 unchanged_input、completed_chapter_ids、pending_chapter_ids、unknown_call_ids；不带密钥。

- [ ] **Step 1 — RED tests:** test_cancel_queued_makes_zero_calls；test_cancel_inflight_stops_next_step_and_keeps_charged_candidate；test_resume_new_job_skips_successful_steps；test_changed_input_requires_new_submission_not_fake_resume；test_accept_retry_risk_preserves_original_unknown_and_reservation；同核对请求重复不覆盖新结算、陈旧revision409，误用别项目call ID拒绝。

```python
def test_accept_retry_risk_preserves_original_unknown_and_reservation(unknown_run, product_owner):
    before = unknown_run.read_call()  # fixture只读原调用，返回(status, reserved_tokens)
    unknown_run.recovery.reconcile(product_owner, unknown_run.job_id, unknown_run.call_id,
        unknown_run.revision, usage=None, accept_retry_risk=True)
    assert before[0] == "unknown"
    assert unknown_run.read_call() == before
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs/test_recovery.py -q`。
- [ ] **Step 3 — Implement:** 取消标志在请求前和采用前共同检查；未知核对追加 record 而不把unknown改零。明确给实际usage时按账务规则结算，接受重试风险本身不释放未知额度；恢复有新任务/new token/独立预留，保留parent与原历史。以 job_steps 已保存的成功大纲/正文修订推导预期任务后状态，任务自身产出不算外部输入变化；额外人工改动则返回 input_changed，UI引导新制作及精细确认。恢复仅补未成功章。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs/test_recovery.py tests/jobs/test_worker.py tests/jobs/test_budget.py -q`；Expected: 0 failures。审查不能对terminal行改running，不能以用户勾选绕过输入/项目/费用状态。
- [ ] **Step 5 — Commit:** `git commit -m "feat: explicitly recover interrupted production jobs"`。

## Task 9：受保护的制作与任务网页接口

**Files:** Create admin/routes/production.py、jobs.py、production_forms.py、templates/admin/production/{projects,project,editor}.html、templates/admin/jobs/{list,detail}.html、tests/production/test_routes.py、test_form_security.py；Modify admin/routes/auth.py:166、security.py:29、app.py 组合、templates/admin/base.html 导航和 tests/production/conftest.py 的 production_client fixture。

**Interfaces:** 两路由导出 router:APIRouter；app.state 增 production_service（ProjectService）、draft_service、job_service、recovery_job_service、production_settings、token_budget。require_admin_post(request:Request, *, body_limit:int=65536)->tuple[Actor,dict[str,str]]；read_limited_body(request:Request, limit:int, *, form_limit:int=65536)->bytes，原JSON16KiB不变，仅制作路由显式 body_limit=2097152，内部传 form_limit=body_limit。production_forms.recover_editor_values(request, form:dict[str,str])->dict[str,str] 只在完整安全门通过后调用；在200KiB范围内保留本次正文，仅允许正文/需求字段、排除具名secret字段和已知Cookie/API Key/CS/LK片段，不把长代码/Base64片段自动当成凭证，使用textarea自动转义。

固定路由清单（参数只接受服务端生成ID；动作到审计action的映射由注册路由决定）：

| Method | Paths |
| --- | --- |
| GET | /admin/production，/admin/production/new，/admin/production/{project_id}，/admin/production/{project_id}/chapters/{chapter_id}，/admin/production/{project_id}/candidates/{candidate_id}，/admin/production/{project_id}/preview |
| POST 项目 | /admin/production/new；/admin/production/{project_id}/brief、/mode、/archive；/admin/production/{project_id}/references/save、/references/{reference_id}/remove |
| POST 大纲/章节 | /admin/production/{project_id}/outline/save、/outline/confirm、/outline/adopt；/admin/production/{project_id}/chapters/{chapter_id}/save、/chapters/{chapter_id}/regenerate；/admin/production/{project_id}/candidates/{candidate_id}/adopt、/reject；/admin/production/{project_id}/acknowledge |
| POST 任务 | /admin/production/{project_id}/generate；/admin/jobs/{job_id}/cancel、/resume、/calls/{call_id}/reconcile；/admin/jobs/check-interrupted；/admin/jobs/model/test-connection |
| GET 任务 | /admin/jobs，/admin/jobs/{job_id}，/admin/jobs/model（静态路径优先注册，避免被job_id捕获） |

上述 /mode 等简写都展开为本行同一个 /admin/production/{project_id} 前缀；候选/references/chapters 必须复验项目归属。Task9 先用基本 project/editor 页提供完整原生表单，Task10再拆分增强模板，不提前import尚未创建的模板。业务冲突仅提供输入复制与显式重新读取，不把新revision偷偷放入原陈旧重试表单。

- [ ] **Step 1 — RED tests:** test_large_chinese_markdown_survives_save_and_stale_conflict；test_2mib_chunked_body_rejected_before_parse；test_duplicate_decoded_field_rejected；test_m1_forms_still_limited_to_65536；test_unauthorized_or_bad_csrf_never_echoes_markdown；test_cross_project_candidate_and_chapter_id_rejected；每个POST统一认证/Origin/CSRF、405/错误/no-store。

```python
def test_duplicate_decoded_field_rejected(production_client):
    response = production_client.post("/admin/production/new",
        content=b"csrf_token=x&csrf_token=y&title=course&mode=quick",
        headers={"Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert "course" not in response.text  # 没通过完整安全门，不回显题目
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_routes.py tests/production/test_form_security.py tests/test_admin_auth_routes.py -q`。
- [ ] **Step 3 — Implement:** /admin/production 与 /admin/jobs 下原生表单 routes：项目new/detail/brief/mode/archive/reference，大纲save/confirm/candidate，章节save/regenerate/candidate，任务submit/cancel/resume/reconcile/check-interrupted，model诊断/test-connection；GET不生成或维护状态。ProductionRoute/AdminRoute派生封装处理400/409安全输入恢复（保留旧M1恢复策略），不直接套用只允许16,000字符的 safe_form_values；CSRF拒绝不回显。正文/refs只经已授权页面显示；任务列表省略原prompt。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_routes.py tests/production/test_form_security.py tests/test_request_security.py tests/test_admin_auth_routes.py tests/test_app_lifecycle.py -q`；Expected: 0 failures。生产工厂注入新服务但不启动worker/联网、不因未配置模型阻断M1。不注册静态草稿路径，不修改销售逻辑。
- [ ] **Step 5 — Commit:** `git commit -m "feat: expose protected production administration"`。

## Task 10：编辑体验、候选比较与真实浏览器流程

**Files:** Create templates/admin/production/{outline,candidate,preview}.html、static/admin/production.js、production.css、tests/production/test_browser_workbench.py、test_editor_script.py；Modify Task9模板、admin/base.html 的仅制作页面资源加载及 tests/production/conftest.py 的 workbench_page fixture。真实Web进程共享测试DB，测试worker可在独立测试线程跑既有Worker＋ScriptedModel；Task12再验证实际worker子进程，不在生产增加fake env。

**Interfaces:** 页面动作消费 Task9 原生POST及明确对象/修订字段；JS仅 attachDirtyGuard(form:HTMLFormElement)->void、insertMarkdown(editor:HTMLTextAreaElement, before:string, after:string)->void，不作授权判断。首版选择手动刷新任务，不实现可选轮询，因此不增加 CSP connect-src。

- [ ] **Step 1 — RED tests:** 浏览器 test_title_only_to_draft_and_precise_confirmation；test_unsaved_edit_prompt_and_clear_after_successful_save；test_conflict_preserves_200kib_text_without_localstorage；test_candidate_is_escaped_and_explicitly_adopted；test_nojs_native_save_preview_and_task_refresh；390×844下表格/长代码可横向局部滚动，按钮可达。test_60_chapter_budget_refusal_has_next_step 检查合法章数不被误报输入错误、不自动提高额度。

```python
@pytest.mark.browser
def test_narrow_editor_does_not_store_draft_in_localstorage(workbench_page):
    workbench_page.set_viewport_size({"width": 390, "height": 844})
    workbench_page.locator('textarea[name="markdown"]').fill("# 中文课程😀")
    assert workbench_page.evaluate("Object.keys(localStorage)") == []
    assert workbench_page.get_by_role("button", name="保存正文", exact=True).is_visible()
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_browser_workbench.py tests/production/test_editor_script.py -q`；缺真实浏览器是环境失败，不静默skip。
- [ ] **Step 3 — Implement:** 中文提示分别显示AI推定、结构通过、人工作业待审核、未知费用；两栏源文/预览在窄屏堆叠，候选采用/拒绝清晰，归档仅隐藏。工具栏提供课程section模板和基础Markdown；脚本只本站，无inline script、localStorage和自动保存承诺。费用超限显示建议减少章数/参考输入或联系维护者调整额度，不秘密截断。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_browser_workbench.py tests/production/test_editor_script.py tests/production/test_routes.py -q`；Expected: 0 failures。拦截预览外部网络和 JS执行探针，证明raw响应未执行，不能靠字符串assert替代浏览器；无JS仍通过核心保存/预览/任务流程。
- [ ] **Step 5 — Commit:** `git commit -m "feat: add usable draft editing and candidate review"`。

## Task 11：不可变检查结果与 M3A 交接快照

**Files:** Create production/snapshots.py、templates/admin/production/snapshot.html、tests/production/test_snapshots.py；Modify production路由/项目模板，仅新增检查和交接操作。

**Interfaces:** SnapshotService(db_path:Path, drafts:DraftService, *, clock:Clock=utc_now)；check(actor:Actor, project_id:int, revision:int)->DraftCheck；create(actor:Actor, project_id:int, revision:int)->SnapshotView；get(actor:Actor, snapshot_id:int)->SnapshotView；read_snapshot_payload(snapshot_id:int)->ProductionInput（仅可信服务使用，无公共HTTP）。既有固定快照模型及序列化规则见类型段。

- [ ] **Step 1 — RED tests:** test_snapshot_hash_does_not_change_after_edit；test_snapshot_blocked_by_job_missing_body_or_unacknowledged_change；test_precise_requires_current_confirmation_but_quick_does_not_forge_it；test_old_snapshot_keeps_source_and_reference_revisions；test_check_does_not_create_sales_approval_or_publish_course；test_cross_project_revision_cannot_enter_snapshot。

```python
def test_snapshot_hash_does_not_change_after_edit(snapshot_ready):
    r = snapshot_ready
    before = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    r.drafts.save_chapter(r.actor, r.project_id, r.chapter_id, r.body_revision, "人工新正文")
    after = r.snapshots.get(r.actor, before.id)
    assert after.hash == before.hash
    assert after.payload_json == before.payload_json
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_snapshots.py -q`。
- [ ] **Step 3 — Implement:** create 持写事务冻结/重查输入hash并验结构，引用revision不得NULL漂移，存规范payload/hash/check，不读未来current。路由新增 POST /admin/production/{project_id}/check、/snapshots，以及 GET /admin/production/{project_id}/snapshots/{snapshot_id}，所有snapshot再次校验project关联。GET显示只读交接/人工待审核，不提供销售或版本切换按钮。现有content_root/courses/products/orders/entitlements bytes/rows保持。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_snapshots.py tests/production/test_projects.py tests/production/test_drafts.py tests/production/test_checks.py tests/production/test_routes.py -q`；Expected: 0 failures。快速大纲系统展开与精细人工确认都有明确记录，不以结构合格替代内容来源检查。
- [ ] **Step 5 — Commit:** `git commit -m "feat: freeze validated draft handoff snapshots"`。

## Task 12：独立 worker 启动、失联维护与实际重启

**Files:** Modify cli.py、app.py 中仅就绪提示；Create tests/jobs/test_worker_cli.py、tests/production/test_process_restart.py；Extend tests/jobs/fakes.py 的本地假模型传输与 process harness（测试专用，不在生产env开启fake）。

**Interfaces:** cli 增 worker 子命令（维护用，无任意模块/路径/命令参数）；jobs.worker.main(argv:list[str]|None=None)->int 读取明确配置/同一DB，校验latest6、显式run_forever。启动无owner或旧schema拒绝、不自动迁移；模型未配置时显示安全状态并不执行生成，M1 Web仍可启动。

- [ ] **Step 1 — RED tests:** test_worker_cli_import_and_help_do_not_start_or_migrate；test_real_web_restart_retains_queued_job_and_draft；test_kill_worker_after_vendor_accept_does_not_replay_on_restart；test_maintenance_fences_after_60_and_marks_after_120；test_stopped_worker_keeps_ui_cancel_and_recovery_available。使用本地真实HTTP假供应商、独立子进程和测试目录，所有网络拒绝离开loopback。

```python
def test_worker_help_uses_fixed_entrypoint(worker_help_result):
    assert worker_help_result.returncode == 0
    assert "worker" in worker_help_result.stdout
    assert "--api-key" not in worker_help_result.stdout
    assert "--shell" not in worker_help_result.stdout
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs/test_worker_cli.py tests/production/test_process_restart.py tests/test_app_lifecycle.py -q`。
- [ ] **Step 3 — Implement:** CLI只允许固定 worker 入口，no shell/密钥argv；运行主循环维护租约、SIGINT/终止协作停止新请求，heartbeat轮次正确终止。Web就绪不等待worker，管理页如实呈现失联。重新启动仅领取queued新任务，不将terminal或失联旧task自动重发。
- [ ] **Step 4 — GREEN:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/jobs/test_worker_cli.py tests/production/test_process_restart.py tests/jobs/test_worker.py tests/jobs/test_recovery.py tests/test_app_lifecycle.py -q`；Expected: 0 failures。进程证据须包含供应商请求计数、DB状态及旧token迟到尝试，不只mock worker被kill。
- [ ] **Step 5 — Commit:** `git commit -m "feat: operate and restart the isolated production worker"`。

## Task 13：安装、恢复、隐私扫描和整批交接

**Files:** Create docs/operations/m2a-runbook.md、tests/production/test_installed_workbench.py、test_restore.py、test_privacy.py；Modify Task4依赖记录与现有 tests/test_wheel_install.py 的本期安装覆盖、CLAUDE.md 的新旧入口说明；不改旧M1 runbook为虚假的M2实测报告。

**Interfaces:** Consumes真实CLI、Web factory、worker、backup_database、read_snapshot_payload，无新业务接口；记录实际依赖、Python/浏览器版本、测试结果与限制。

- [ ] **Step 1 — RED tests:** test_fresh_wheel_runs_workbench_outside_source_tree；test_backup_restore_contains_draft_reference_and_snapshot_sources；test_logs_events_and_artifacts_do_not_contain_known_secret_sentinels；test_model_configuration_and_fake_credentials_not_in_wheel。允许源DB包含该私有生产输入，但日志/审计/wheel不得含它；测试备份允许保存源，密钥不在DB任务。

```python
@pytest.mark.packaging
def test_fresh_wheel_runs_workbench_outside_source_tree(installed_workbench):
    assert installed_workbench.web_response.status_code == 200
    assert installed_workbench.module_path.is_relative_to(installed_workbench.venv)
    assert not installed_workbench.module_path.is_relative_to(installed_workbench.source_root)
    assert "制作项目" in installed_workbench.web_response.text
```
- [ ] **Step 2 — Run RED:** `.venv/Scripts/python.exe -X utf8 -m pytest tests/production/test_installed_workbench.py tests/production/test_restore.py tests/production/test_privacy.py -q`；缺wheel/dependency/browser明确失败，不允许源码回退。
- [ ] **Step 3 — Implement handoff:** 手册给维护者明确解释器、env/context/额度/worker启动、离线5→6备份和恢复、日常双模式与未知调用处置；实际snapshot只读恢复比对hash/IDs，不改M1原路径/政策以放行。fresh安装用完整wheel依赖集合，不依赖源码cwd/PYTHONPATH；没有新自动备份按钮/公开部署。
- [ ] **Step 4 — GREEN and full acceptance:** 在隔离测试数据上执行下文完整命令，记录durations与所有skip，wheel安装/真实browser包含在全量内；全分支独立复审通过后若有修正，再跑覆盖修正的测试和最终完整验收。不得将M1旧1063项结果当作新分支通过。
- [ ] **Step 5 — Commit:** `git commit -m "test: verify installed production and recovery handoff"`，只暂存本项路径。用户已有完成后推送GitHub的项目授权，但推送必须在本计划获批、实现/审查/验收完成后执行；无PR/main merge、真实调用或生产部署授权。

## 最终验收命令与环境边界

```powershell
.venv/Scripts/python.exe -X utf8 -m pytest -m "not browser and not packaging" -q --tb=short --durations=15 -ra
$env:COURSMITH_BROWSER_CHANNEL='chrome' # 仅本机已有授权的安装Chrome，仍保留bundled默认
# 离线安装时明确设置完整 COURSMITH_WHEELHOUSE 与 PIP_FIND_LINKS；不靠缓存漏包或源码回退
.venv/Scripts/python.exe -X utf8 -m pytest -q --tb=short --durations=15 -ra
.venv/Scripts/python.exe -X utf8 -m ruff check .
.venv/Scripts/python.exe -X utf8 -m build --wheel
.venv/Scripts/python.exe -X utf8 -m pytest tests/test_wheel_install.py tests/production/test_installed_workbench.py -m packaging -q
git diff --check
```

源码当前 .venv 已具备M1验收依赖；新渲染依赖在Task4准备，不在计划阶段安装。Windows软链接权限、浏览器下载或运行器故障真实记录，不以跳过/换成mock报告通过。实际窄屏浏览器不是实体手机认证，公开试运营仍需真实手机反馈。无实际供应商费用试验或生产切换，本批不宣称它们验证通过。

## 规格覆盖与计划自审

| 规格章节 | 实施/验收任务 |
| --- | --- |
| 1–3 意图、范围、架构 | 2–4、9–13及全局排除项 |
| 4 双模式/身份 | 2、3、5、7、10 |
| 5 页面/编辑 | 9、10、11 |
| 6 数据/修订 | 1–3、5、11、13 |
| 7 任务/并发/恢复 | 5、7、8、12 |
| 8 模型/费用/输入限制 | 2、3、6、7、8、9、10 |
| 9 来源与安全 | 2、4、6、9、10、13 |
| 10 检查/快照 | 4、7、11 |
| 11 异常/迁移/运行 | 1、6、9、12、13 |
| 12 验收矩阵 | 每项RED/GREEN＋10/12/13真实流程和全量验收 |
| 13 审批边界 | 本计划待批，不开始实现；执行保持逐项独立审查 |

自审检查：每项有明确Files/Interfaces、失败测试与断言、RED/GREEN命令、独立交付和提交点；引用类型在首次使用任务定义，后继签名不改名。Review Focus五项均有归属测试，未遗漏64KiB现有限额、现有16,000字符回显上限、schema5历史测试与未知费用风险。实现步骤决定边界/算法选择而不代写完整函数体。

书面计划审批后沿用分任务子代理实现＋逐项独立审查，审批同时确认工作版60章上限；实施者与审查者完整读取本计划和规格。M1.1/M3A、站点上线和真正课程商业质量仍是独立后续工作，不能借“完成任务”扩张范围。
