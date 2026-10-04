# Coursmith M1 受控试用交接手册

交付范围是单 owner 的商品、订单、发行、权益核验、设备恢复、进度与受控下载。M2 制作、M3 发布、M4 公开运营尚未交付。测试课程正文不代表商业可售质量；经营者必须逐商品检查正文、来源授权、AI 披露、移动阅读及所承诺下载。

## 首次安装与初始化

使用 Python 3.11 或更新的独立环境安装经过验收的 wheel。运行依赖由 wheel 声明；验收工具使用项目 `dev` 依赖（含 build、Playwright、临时 TLS 证书生成工具）。本次确切安装版本见同目录的 `task12-validated-requirements.txt` 和 Task12 报告；不把开发目录或本地配置打进包。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install <经过验收的-wheel文件>
```

在服务账户的运行环境中显式设置 `COURSE_DATABASE`、`COURSE_CONTENT_ROOT` 为绝对路径。内容根目录放已验证且不可原地修改的课程发行包；一个商品绑定一课程，已有发行 ID 不替换正文、版本、章节顺序或文件。空库可显式执行 `python -m course_platform.cli migrate` 初始化 latest5。启动工厂可在空库建立 latest5 并扫描安全发行包为草稿，但不会自动升级已有旧库。

仅首次执行 `python -m course_platform.cli init-admin`，按交互提示输入单 owner 账号和 12–128 字符密码，并再次确认。密码不放命令行、配置或日志。日常经营通过 `/admin/login`；无需每日 CLI。遗失管理员密码不是注册入口或任意新建 owner 的理由，应停止后台操作并由授权维护者制定恢复方案。

## Origin、HTTPS、代理与启动

`COURSE_SITE_ORIGIN` 是课程站规范 origin（例如 `https://courses.example`，没有末尾斜杠/路径）；与模型端点 `COURSE_BASE_URL` 不同。发货文案必须使用 HTTPS origin，即便开发模式也不能用 HTTP origin 发店铺文案。`COURSE_ENVIRONMENT=production` 要求显式 HTTPS，并使管理/买家 Cookie 都带 Secure。`COURSE_SESSION_TTL_HOURS=72` 为买家会话上限，仍受原权益期限约束。

本地 HTTP 服务：`python -m course_platform.cli serve --host 127.0.0.1 --port 8000`。生产 HTTPS 由受控代理终止，后台应用仅可经该代理访问。直接工厂启动命令：

```text
python -m uvicorn course_platform.app:create_app --factory --no-proxy-headers --host 127.0.0.1 --port 8000
```

无论是否启用可信代理，都必须保留 `--no-proxy-headers`；CLI 已固定 `factory=True, proxy_headers=False`。应用必须看到真实 socket peer 才能正确限流。`COURSE_TRUSTED_PROXY_CIDRS` 默认空，忽略全部转发头；需要代理时只列实际可信网络。代理须追加 X-Forwarded-For 中的真实上游 peer，不能仅转交客户端伪造值；从右向左经过可信跳，首个不可信地址决定来源。Forwarded/X-Real-IP 不授予信任。生产入口须阻止绕过代理直接访问应用。

所有写入要求规范同站 Origin 和对应 CSRF。管理 Cookie Path=/admin、HttpOnly、Strict；买家 Cookie Path=/、HttpOnly、Lax。原生表单需要 `Referrer-Policy: same-origin` 保留同站 Origin，禁止跨站 Referer；不要改回让正常浏览器 POST 变成 Origin:null 的 no-referrer。后台脚本只来自本站 `/static/admin/`；正文禁止脚本，敏感页面与下载 no-store。临时自签 TLS/忽略证书验证仅用于隔离验收，不能作为生产 HTTPS 配置。

## 日常网页交付

1. 登录后台，创建分类，完善导入的草稿商品：课程绑定、简介、成果、来源/AI 披露、支持方式、HTTPS 店铺渠道及明确的学习期限。独立选择在线/PDF/ZIP，必须实际检查所承诺文件。完成五项销售检查后启用；暂停/归档会阻止新发行，有效旧买家依照原发行快照访问。
2. 在店铺实际核验付款、商品、金额和原购买者后登记订单；金额单位人民币分，一个订单一商品一权益。付款时间必须带时区；存储 UTC，后台显示 Asia/Shanghai。
3. 从订单“按登记政策发码”，立即复制首次响应中的码与发货文案。CSV 由本次响应 DOM 在浏览器生成（UTF-8 BOM、带公式防护）；服务不保存 CSV，历史页面不能重新取明文。核对独立的激活截止和兑换后的学习期限，将文案通过既有店铺交付，回订单确认已发送。
4. 买家从发货链接兑换一次，立即妥善保存首次响应 LK；不放 URL、浏览器 localStorage、日志、备注。在线章节可用原生表单标记/取消完成，无 JS 也可操作。PDF/ZIP-only 买家通过下载入口获得原承诺格式，不开放正文/进度。经营者暂停或归档不抹掉已购承诺。
5. 新设备在“恢复设备”重新输入 LK；进度/原到期日不变。最多三个有效设备，第四个页面必须重新输入空白 LK 并主动确认退出最早设备，不能靠旧隐藏字段或勾选状态授权。

## 丢失首次响应与售后

刷新、离开、网络响应丢失或幂等重放不会重新显示明文。未使用码丢失：进入原批次核对最新修订，再明确补发/作废原码；不能从哈希恢复。已激活 LK 丢失：先核验原订单/持有人、原发行政策和影响，再在该权益详情明确重置。重置立即撤销所有旧 LK/旧会话，保留进度和原期限；新 LK 仅本次成功响应，丢失需再次核验并明确再次重置。不能仅凭“同一课程”认定购买关系。

历史短 CS 只有真实迁移并经 owner 明确逐码核验用途/政策的记录可兑换，不会新造短码。独立历史会话/权益不可猜测码或订单归属，也不可合并进度；先逐权益核验持有人，保留未知原数据，完成后单独重置。未知 NULL 到期不是自动永久权益，未知政策不从当前商品推断。

店铺外部退款已经完成后，核验并登记“外部已退款”及原因。这只是外部核验记录，不调用支付接口；关联码链/权益/凭证/会话将撤销。业务表保留，不删除订单、用过的码、权益或历史。陈旧修订 409 应重读详情，勿盲重试不同操作；同幂等键改变内容也会 409。

## 旧库维护窗口、匹配备份、切换与回滚

1. 停止写入并停止旧服务；记录旧程序版本、schema 检查、完整 DB 和匹配的课程文件树快照。使用 SQLite backup API/`backup_database` 保存一致数据库副本，不单独拷贝正在写入的 DB 主文件（WAL 数据可能未落盘）。备份不能覆盖已存在备份。保留所有课程、章节、已发行快照、核验、码/凭证哈希、原日期及进度。
2. 将 DB 和该时点内容复制到独立演练目录，保留旧库/旧程序可读的原始兼容副本。先在副本执行 `python -m course_platform.cli migrate --check-only`；再设置环境指向副本，用 `migrate --backup <副本升级前的非覆盖备份>` 显式离线迁到 latest5。未知/更新 schema、失败迁移或 FK 不一致停止切换；工厂不会在线升级旧库。
3. 离线逐课程验证复制内容的 course_id/slug/version、所有原章节映射/顺序、原 package_hash 和全部文件 bytes。检查内容路径/软链接 containment。仅在验证完全匹配后，在**副本 DB**事务中将对应 `courses.content_path` 改为新根内该发行包绝对路径。不要调用 sync_course 改旧路径，不改 hash/版本/正文/已购快照。无原 hash 或无法取得原内容的历史记录保持未知，记录为不可用；不补造承诺、不替换 bytes。不可用记录也须逐项记录明确的新根内不可用位置，不能隐式回源或删除历史。
4. 检查 latest5、foreign_key_check、完整性及迁移历史；比较原/副本 IDs、原权益/激活截止、核验与快照、独立会话/进度。启动新工厂指向副本 DB/内容根；原内容仍另行存在时，也必须证明请求只读取匹配的新副本，并证明篡改副本会拒绝交付。工厂拒绝根外旧绝对路径，不能用该拒绝测试替代实际恢复。
5. 在演练站验证现代销售、已核验历史短码、独立迁移权益、学习进度、原到期、下载和重置/撤销。验收通过后由授权维护者决定切换；本任务没有切换生产、部署或公开运营授权。切换前再次确认停写的备份一致且配置只指向目标副本。
6. 失败时停止新服务；不要让旧程序读取已升级新库。恢复旧程序及旧兼容 DB+匹配内容副本，并使用其旧配置。切换后若已有新写入，停止并单独保全新库，先处理差异，不能用旧副本覆盖新订单/进度。保留失败副本供定位，不把旧未知历史补成已核验。

Task12 自动演练确实复制了匹配 DB+文件、核验 FK/身份/hash/原承诺/期限/进度、只更新副本 relocation metadata，改变原文件后仍读取副本，篡改副本则拒绝。运行时间及结果见同计划 Task12 报告；这是测试副本证据，不是生产备份已完成。

停写后的备份可在维护 Python 会话使用：

```python
from pathlib import Path
from course_platform.database import backup_database, check_database
backup_database(Path("<停写源DB绝对路径>"), Path("<新的非覆盖备份绝对路径>"))
print(check_database(Path("<新程序验证副本DB绝对路径>")))
```

已经离线迁至 latest5 的**可验证课程**，以下模式可用于逐项核验后只改副本路径；所有字段/hash 都应匹配才提交。这里只演示有原始 hash 的匹配发行。未知 hash、不可用记录或身份差异须单独留痕处理，不把例子改成自动补 hash/默认政策工具。

```python
from pathlib import Path
from course_platform.content import load_course_package
from course_platform.content_inspection import inspect_package
from course_platform.database import transaction
copied_db = Path("<副本DB绝对路径>")
copied_root = Path("<副本内容根绝对路径>").resolve(strict=True)
package = (copied_root / "<该课程目录>").resolve(strict=True)
package.relative_to(copied_root)
manifest = load_course_package(package).manifest
inspected = inspect_package(package)
with transaction(copied_db, immediate=True) as connection:
    saved = connection.execute(
        "SELECT course_id,slug,version,package_hash FROM courses WHERE course_id=?",
        (manifest.course_id,),
    ).fetchone()
    assert saved is not None and saved["package_hash"] is not None
    assert tuple(saved) == (manifest.course_id, manifest.slug, manifest.version, inspected.fingerprint)
    saved_chapters = [tuple(r) for r in connection.execute(
        "SELECT chapter_number,title,path,free_preview FROM chapters WHERE course_id=? ORDER BY chapter_number",
        (manifest.course_id,),
    )]
    assert saved_chapters == sorted((c.number,c.title,c.path,int(c.free_preview)) for c in manifest.chapters)
    connection.execute("UPDATE courses SET content_path=? WHERE course_id=?", (str(package), manifest.course_id))
```

原始兼容备份、匹配文件树和新副本应各自保存，不将测试日志、CSV 或首次响应凭据当作备份材料。本轮 fresh 安装使用 Python3.11.9，开发测试用 Chrome154.0.8037.93、Node24.16.0；fresh 安装的 tzdata2026.5/MarkupSafe3.0.4/websockets17.2 与开发 venv 的2026.3/3.0.3/17.1分开记录。构建隔离环境使用 setuptools84.0.0；fresh 安装环境自带 setuptools65.5.0，不混称为构建版本。

## 验收命令及环境限制

完整命令必须运行，不能以默认跳过 browser/packaging 代替：

```powershell
.venv/Scripts/python.exe -X utf8 -m pytest -m "not browser and not packaging" -q --durations=15
$env:COURSMITH_BROWSER_CHANNEL='chrome' # 本机既有 ruling 允许已安装 Chrome；不设置则用 bundled Chromium
$env:COURSMITH_WHEELHOUSE='<可选的完整依赖wheel目录>' # 仅隔离安装使用真实wheel，无源码回退
.venv/Scripts/python.exe -X utf8 -m pytest -m "browser or packaging" -q --durations=15
.venv/Scripts/python.exe -X utf8 -m ruff check .
.venv/Scripts/python.exe -X utf8 -m build --wheel
.venv/Scripts/python.exe -X utf8 -m pytest tests/test_wheel_install.py -m packaging -q
```

缺浏览器/下载失败/安装失败是环境准备失败，不能报告通过。本机 bundled Chromium CDN 下载曾失败，实际使用 Chrome；保留 bundled 默认选择。现有 CSV/clipboard 脚本单测还需要 Node，本次 v24.16.0。Windows 无文件软链接创建权限的原有一项 skip 仍是未验证限制，目录 junction/ZIP-link 的通过不能替代它；未安装 WSL/改 OS 权限。Task9 总体变慢原因仍未确定，本轮 durations 只记录测量，不授予性能或公开部署批准。独立 task review 和全分支 review 由 controller 负责。
