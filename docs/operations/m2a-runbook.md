# Coursmith M2A 私有制作工作台交接手册

本期交付单 owner 的快速/精细课程草稿、独立持久 worker、Markdown 编辑、结构检查与不可变交接快照。原 M1 商品、订单、权益及公开课程树保持原政策。`admin-v1-runbook.md` 是历史 M1 手册，保留其旧验收记录；当前 schema 为6，M2A 能力和当前命令以本文为准。旧 CLI/Tkinter/飞书生成器继续独立存在，工作台不调用旧流水线。

自动生成、结构通过和快照均不代表事实、教学质量、来源授权或销售批准。M3A 正式审核/编译/发布、真实生产迁移与部署、付费供应商调用另需授权。本手册的迁移/恢复操作先在独立测试副本上演练。

## 安装与解释器

在源码树外创建独立运行目录。使用可信 Python3.11+ 创建 `.venv`；验收实际为 Windows amd64 Python3.11.9。之后每个 PowerShell 窗口先进入该目录，用显式解释器；不依赖激活、PATH 或 PYTHONPATH。设置 DB/内容目录为绝对路径，因为安装后的相对路径从包安装根解析。不要把开发源码、测试 `.env`、DB 或凭证混进 wheel。

```powershell
python -m venv .venv
$env:COURSMITH_WHEELHOUSE='C:/maintenance/verified-wheelhouse'
$env:PIP_FIND_LINKS=$env:COURSMITH_WHEELHOUSE
$env:PIP_NO_INDEX='1'
.venv/Scripts/python.exe -X utf8 -m pip --isolated install --no-cache-dir --no-index --find-links $env:COURSMITH_WHEELHOUSE -c C:/maintenance/m2a-validated-requirements.txt C:/maintenance/zhike_course_platform-0.1.0-py3-none-any.whl
.venv/Scripts/python.exe -X utf8 -m pip check
.venv/Scripts/python.exe -X utf8 -c "import sys,course_platform; print(sys.executable); print(course_platform.__file__)"
```

模块必须来自该 `.venv` 的 site-packages。完整运行/验收依赖版本见 `m2a-validated-requirements.txt`；constraints 不会自动安装 dev 工具。离线 wheelhouse 必须含产品 wheel、所有传递依赖和构建工具，并核对受控 SHA-256 清单，缺包应停止。验收 wheelhouse 的72个第三方文件/清单保留在忽略的 SDD 证据目录，下载成功不是安装成功。构建后端要求 setuptools>=75，本次离线构建提供84.0.0/wheel0.48.0；独立 venv 自带 setuptools65.5.0/pip24.0，二者用途不同。

## 服务配置与启动

在 Web 和 worker 的各自服务环境中显式提供同一套配置。密钥通过受控进程环境或维护配置注入，不写 argv、网页、任务、日志、备份清单或版本库。不使用真实 `.env` 做测试。

```powershell
$env:COURSE_DATABASE='C:/coursmith-private/data/course.db'
$env:COURSE_CONTENT_ROOT='C:/coursmith-release/courses'
$env:COURSE_ENVIRONMENT='test'
$env:COURSE_SITE_ORIGIN='http://127.0.0.1:8000'
$env:COURSE_TRUSTED_PROXY_CIDRS=''
$env:COURSE_BASE_URL='https://api.deepseek.com/'
$env:COURSE_MODEL='deepseek-flash'
$env:COURSE_MODEL_CONTEXT_TOKENS='' # 维护者填写经过确认的真实上下文整数；系统不猜测
$env:COURSE_TASK_TOKEN_LIMIT='200000'
$env:COURSE_MODEL_PRICE_JSON='' # 可选 dated/currency 单价，仅估算，非账单
$env:DEEPSEEK_API_KEY='' # 初次本地演练为空；实际调用需另获授权并安全注入
.venv/Scripts/python.exe -X utf8 -m course_platform.cli migrate --check-only
```

全新空库可明确执行 `.venv/Scripts/python.exe -X utf8 -m course_platform.cli migrate` 到 schema6，然后 `.venv/Scripts/python.exe -X utf8 -m course_platform.cli init-admin`，交互输入账号和12–128字符密码。既有 schema5 禁止直接启动升级；见下一节。worker 要求已有 latest6 和 enabled owner；无 owner/旧库/无效或不支持配置安全拒绝。

Web 窗口：`.venv/Scripts/python.exe -X utf8 -m course_platform.cli serve --host 127.0.0.1 --port 8000`。直接工厂也可用 `.venv/Scripts/python.exe -X utf8 -m uvicorn course_platform.app:create_app --factory --no-proxy-headers --host 127.0.0.1 --port 8000`。独立第二窗口：`.venv/Scripts/python.exe -X utf8 -m course_platform.cli worker`。固定入口没有 shell、模块、路径或密钥参数。Web 不启动/等待隐藏 worker，重启 Web 不重发任务。

缺 key/context 时 Web/M1/本地检查和快照仍可用，worker 显示 `local_only` 并处理已入队 `draft_validate`；生成/连接测试仍被逐任务配置栅栏拒绝，不登记模型调用或调用预留。只支持 deepseek-flash，实际请求显式 thinking disabled；不静默替换旧任务模型。端点/模型/执行条件漂移应停止并新提交；同条件密钥轮换不复制旧 key 到任务。

关闭 SDK 隐式重试，单次总截止120秒。一个主机、一个 Web、一个 worker，单任务/单请求执行；每10秒独立心跳、租约60秒，120秒无心跳由 worker 维护或明确 POST 检查中断。GET 只读：60–119秒显示租约过期，>=120秒显示疑似失联，但数据库仍可能记录 running；历史空闲心跳不能证明当前在线。没有活动轮次时如实显示无法确认。页面存在不能证明 worker 正在执行。

Ctrl+C/SIGTERM 请求协作停止，不再启动下一请求；合法在途响应仍可保存/计费。硬终止可能留下 sent/unknown；不要把终止进程理解为立即停费。Windows 验收的隐藏子进程使用测试内 CRT 信号桥，证明 handler 协作，未认证外部服务管理器的信号交付；hard kill 单独实测。

生产 HTTPS、Secure Cookie、同站 Origin/CSRF、可信代理与 `--no-proxy-headers` 沿用 M1 要求。上述 localhost/test 是演练配置，不是上线配置。后台所有私有源/候选/快照和任务页面保持 owner/no-store；买家不能访问。内容 root 只放原发行包，不能作为私有草稿/备份目录。

## 离线 schema5→6、备份与恢复

1. 获得具体生产变更授权后，维护窗口停止 Web、worker 和所有写入；记录旧程序版本/schema、完整 DB 及匹配的 M1 发行文件树/哈希。使用 SQLite backup API，不能单独复制正在写入的主 DB 文件而遗漏 WAL。
2. 先在测试副本验证 schema5 业务图、迁移历史1–5和 FK，再执行 `.venv/Scripts/python.exe -X utf8 -m course_platform.cli migrate --backup C:/coursmith-private/backups/before-schema6.db`。备份目标必须新建且不同于源，已有目标拒绝覆盖。v001–v005 不变，只增 schema6；启动不会自动升级旧库。
3. 用 `migrate --check-only` 核对6、integrity/FK，再比较全部 M1 行：课程/章/文件指纹、owner/会话、分类/商品/销售快照、订单/码批次/码、权益/原期限/政策、恢复凭证/设备/进度、旧来源核验、审计及幂等历史。禁止为放行修改 M1 路径/政策/关联。
4. M2A Markdown、参考资料、候选、需求/大纲历史和快照都在私有 SQLite，备份包含完整源而非仅任务元数据。当前无额外私有派生产物；以后出现时需另附受控私有清单，不能声称本期已经验证它。不要把私有输入写入日志或公开课程文件。
5. 在独立恢复路径用 `backup_database` 恢复备份，检查完整表/行、源哈希、公共修订号与全局行ID、章节稳定ID、来源状态、快照父/子计数和全部 FK 前驱链。可信内部 `SnapshotService.read_snapshot_payload(snapshot_id)` 读取原引用，不跟随未来 current；`get(owner,snapshot_id)` 重验权限。对比原规范 payload_json UTF-8 bytes、独立 SHA-256、input_hash、check 和 human_review_required，必须与备份前记录一致。
6. 验证恢复副本中原历史快照和未来正文/参考修订都存在，未采用候选/任务账目也保留。只读核对不得重新创建快照、生成正文、写审计或发布。保留备份和事故库，核对完成后才做授权切换。旧程序只能配旧 schema5 兼容备份及匹配发行树，不能读取/降级 schema6 库。

从显式维护解释器进入 Python 会话：`.venv/Scripts/python.exe -X utf8`，在受控副本上使用真实 API：

```python
from pathlib import Path
from course_platform.database import backup_database, check_database
backup_database(Path('C:/test/source.db'), Path('C:/test/unused-backup.db'))
backup_database(Path('C:/test/unused-backup.db'), Path('C:/test/unused-restored.db'))
print(check_database(Path('C:/test/unused-restored.db')))
```

## 日常双模式、候选与异常

登录 `/admin/login` 后进入制作项目。快速模式只填题目，显示默认10章/最大60/额度摘要，明确提交才调用；系统展开大纲不伪造人工确认。精细模式题目必填，其余受众/目标/先修/分类/资料可选；先生成/编辑大纲并确认当前方向，再批量正文。需求/参考/大纲变更使精细确认失效。

章ID与次序分离；改标题/重排不换ID，全量新大纲采用需明确新旧对应关系。保存用显式按钮和修订，409保留本次文本与旧修订供复制/重新读取，不盲重试。正文无 localStorage/自动保存承诺；无JS可原生保存/预览/刷新，但无离开提醒。正在生成的章不能改；已完成章可编辑。重做已有正文只产生候选，明确采用/拒绝；过期轮次结果只隔离历史，不能采用到活动正文。

有 queued/running 修改任务先取消并等停止/明确中断，再切换模式或归档；切换不丢历史、不自行调用。排队取消无模型请求；运行取消是请求停止，已经发出的请求仍可能计费。任务完成/结构通过/人工待审核分别显示。快照需解决结构阻断、候选/输入变更，并满足精细当前确认；事实/深度/案例/练习/授权/销售承诺继续由人检查。

合法60章、20,000字符参考资料可能超上下文/默认200,000额度；页面提示减少输入或由维护者重新评估配置，不截断、不秘密提高额度。初始预留按逻辑请求计算，不三倍预乘互斥尝试。每实际尝试独立调用/预留；仅适配器证明未接受的短暂失败可 known0/rejected 释放，再独立预留重试，间隔2/8秒，最多两次。普通5xx、超时/断流和不明接受均 unknown，不自动重发、不释放原预留。

失联/失败后先查看成功章、冻结输入、未知调用、现有预留；明确检查中断是 POST，GET 不修复。核对未知调用可记录实际 usage 或明确接受再次调用风险；风险同意不把原 unknown 改零，不释放原预留。沿最新恢复子任务继续，不能绕回祖先跳过后代未知账目。确认恢复创建关联的新任务/新token/独立预留，只补未成功目标；输入已变则新制作并重新确认。快速首次大纲仅候选而未采用时，先人工处理再新提交，空待生成列表不等于完整成功。

## 当前验收与限制

当前源码 Python3.11.9，SDK openai2.54.0/httpx0.28.1，pytest8.4.2/Playwright1.63.0；实际 Chrome155.0.8059.40。新渲染依赖 markdown-it-py4.2.0/nh30.3.7/mdurl0.1.2。完整源/独立安装差异见依赖记录，不声称在线最新版。

2026-10-13 当前分支实测：非浏览器/非打包全量1920通过、1跳过、22未选中，1715.38秒；包含真实 Chrome 与离线 fresh wheel 的完整全量1942通过、1跳过，2516.03秒；独立打包复验6通过，323.04秒，无跳过。Ruff 全仓检查与独立 wheel 构建均 exit0；后者实际隔离构建后端为 setuptools84.0.0。唯一跳过是既有 `tests/test_content_inspection.py:260` 在 Windows 上创建文件软链接遭 WinError1314，目录 reparse-point 用例仍实际执行；浏览器和打包均未跳过。最终再次启动真实 Chrome 核实版本155.0.8059.40，源码依赖未升级。

实际命令、exit、durations、安装/恢复证明、源文件指纹与暂存前/后空白检查保留在 `.superpowers/sdd/2026-10-08-production-workbench-m2a-plan/task-13-report.md` 及原始日志。初始真实 RED 是完整安装依赖记录与恢复手册缺失；已有恢复/隐私能力没有被虚构成新运行缺陷。先前在96%被人为中断的全量缺少 traceback/exit，仍保留为未完成证据；新鲜完整验收使用独立日志，不将该中断记录算通过。最终整批独立复审与条件推送仍由控制器完成。

验收命令使用工作树 `.venv/Scripts/python.exe -X utf8`，明确 `COURSMITH_BROWSER_CHANNEL=chrome`、完整 `COURSMITH_WHEELHOUSE`、`PIP_FIND_LINKS`/`PIP_NO_INDEX`：先 `-m pytest -m "not browser and not packaging" -q --tb=short --durations=15 -ra`，再完整 `-m pytest -q --tb=short --durations=15 -ra`、`-m ruff check .`、`-m build --wheel`、`-m pytest tests/test_wheel_install.py tests/production/test_installed_workbench.py -m packaging -q` 及 `git diff --check`。缺 wheel/依赖/真实浏览器为环境失败，不能跳过、源码回退或使用生产 fake flag。

390×844是桌面窄屏模拟，非实体手机认证；仍需真实手机及真实课程人工反馈。没有付费供应商费用/商业内容质量/生产切换试验。已知 Windows symlink WinError1314 只能按实际最终输出披露。旧 M1 的1063结果不能当本分支结果。独立全分支复审、必要修正后最终验收和有条件非强制推送由控制器完成。
