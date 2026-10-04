# Coursmith M1 受控试用交接手册

交付范围是单 owner 的商品、订单、发行、权益核验、设备恢复、进度与受控下载。M2 制作、M3 发布、M4 公开运营尚未交付。测试课程正文不代表商业可售质量；经营者必须逐商品检查正文、来源授权、AI 披露、移动阅读及所承诺下载。

## 首次安装与初始化

使用 Python 3.11 或更新的独立环境安装经过验收的 wheel。运行依赖由 wheel 声明；验收工具使用项目 `dev` 依赖（含 build、Playwright、临时 TLS 证书生成工具）。本次确切安装版本见同目录的 `task12-validated-requirements.txt` 和 Task12 报告；不把开发目录或本地配置打进包。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install <经过验收的-wheel文件>
```

上面的裸 `python` 仅用于创建环境，不会激活它。此后每个新 PowerShell 窗口都先进入这个独立运行目录（不是源码仓库），所有经营、维护和服务器命令均明确使用 `.venv/Scripts/python.exe`。不要改用 PATH 中的 `python`；它可能没有安装本次 wheel。维护 Python 会话也从 `.venv/Scripts/python.exe` 启动，再输入下文 Python 示例。相对解释器路径以这个运行目录为准，DB/内容路径则始终使用绝对路径。

在服务账户的运行环境中显式设置 `COURSE_DATABASE`、`COURSE_CONTENT_ROOT` 为绝对路径。内容根目录放已验证且不可原地修改的课程发行包；一个商品绑定一课程，已有发行 ID 不替换正文、版本、章节顺序或文件。空库可显式执行 `.venv/Scripts/python.exe -m course_platform.cli migrate` 初始化 latest5。启动工厂可在空库建立 latest5 并扫描安全发行包为草稿，但不会自动升级已有旧库。

仅首次执行 `.venv/Scripts/python.exe -m course_platform.cli init-admin`，按交互提示输入单 owner 账号和 12–128 字符密码，并再次确认。密码不放命令行、配置或日志。日常经营通过 `/admin/login`；无需每日 CLI。遗失管理员密码不是注册入口或任意新建 owner 的理由，应停止后台操作并由授权维护者制定恢复方案。

## Origin、HTTPS、代理与启动

`COURSE_SITE_ORIGIN` 是课程站规范 origin（例如 `https://courses.example`，没有末尾斜杠/路径）；与模型端点 `COURSE_BASE_URL` 不同。发货文案必须使用 HTTPS origin，即便开发模式也不能用 HTTP origin 发店铺文案。`COURSE_ENVIRONMENT=production` 要求显式 HTTPS，并使管理/买家 Cookie 都带 Secure。`COURSE_SESSION_TTL_HOURS=72` 为买家会话上限，仍受原权益期限约束。

本地 HTTP 服务：`.venv/Scripts/python.exe -m course_platform.cli serve --host 127.0.0.1 --port 8000`。生产 HTTPS 由受控代理终止，后台应用仅可经该代理访问。直接工厂启动命令：

```text
.venv/Scripts/python.exe -m uvicorn course_platform.app:create_app --factory --no-proxy-headers --host 127.0.0.1 --port 8000
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
2. 制作两套独立副本：**封存的旧兼容 DB＋该时点匹配内容树**（不覆盖、不迁移、不修改）及**工作 DB＋匹配内容树**。停写时逐文件建立并核对 bytes 对应记录，保留 course_id/slug/version 与章节映射。之后旧原路径的存在、消失或变化都不能影响转换。不能用临时从变化后的原目录重取文件来代替匹配备份。
3. 在当前运行窗口先把 `COURSE_DATABASE`、`COURSE_CONTENT_ROOT` 指向**工作副本**，再执行 `.venv/Scripts/python.exe -m course_platform.cli migrate --check-only`。按实测 schema 分支处理：已经 latest5 只走下面 latest5 relocation；受支持的 pre-v5（含原六表）先走下面 pre-v5 relocation，再转换。未知/更新 schema、FK/完整性错误须停止；工厂不会在线升级旧库。不要先迁移再试图补路径。

### latest5：已有发行 hash 的副本 relocation

不重新迁移或计算替代原 hash。逐课程核对工作副本内容的身份、原章节映射/顺序与既存 package_hash，然后仅更新工作 DB 路径。无法取得匹配内容、原 hash 未知或核对失败的记录不能套用此例放行，保留未知/不可用历史并单独处置。不要调用 sync_course 改旧路径，不改 hash/版本/正文/已购快照。

在上述明确启动的 `.venv/Scripts/python.exe` 维护会话逐项执行；所有字段/hash 匹配才提交：

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

### pre-v5：先核对并 relocation，后做内容敏感转换

v005 会读取 DB 内 courses.content_path，核对发行并派生原库缺失的 fingerprint，然后保存历史 availability/hash；故**所有工作副本路径必须在转换之前显式移入工作内容根**。原六表没有 package_hash 不代表内容丢失：以停写时封存的匹配 DB＋内容 bytes 为依据验证，不要求或手工补造旧 hash。已有 hash 必须匹配；不得修改 ownership、政策、会话到期、进度或独立历史归属。

在 `.venv/Scripts/python.exe` 维护会话用下面例子逐个处理可验证课程。示例只改工作 DB 的 content_path，不写 hash；转换之后由既有 v005 建立其实际检测结果。封存 DB 和匹配内容树只读，不能作为工作目标。

```python
from contextlib import closing
import hashlib
from pathlib import Path
from course_platform.content import load_course_package
from course_platform.content_inspection import inspect_package
from course_platform.database import check_database, open_readonly, transaction
copied_db = Path("<副本DB绝对路径>")
copied_root = Path("<副本内容根绝对路径>").resolve(strict=True)
compatible_db = Path("<兼容快照DB绝对路径>")
compatible_root = Path("<兼容快照内容根绝对路径>").resolve(strict=True)
assert copied_db.resolve() != compatible_db.resolve() and copied_root != compatible_root
assert check_database(copied_db)["version"] < 5
package = (copied_root / "<该课程目录>").resolve(strict=True)
sealed_package = (compatible_root / "<该课程目录>").resolve(strict=True)
package.relative_to(copied_root)
sealed_package.relative_to(compatible_root)
manifest = load_course_package(package).manifest
inspected = inspect_package(package)
inspect_package(sealed_package)  # also rejects unsafe backup paths/files
def file_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}
assert file_hashes(package) == file_hashes(sealed_package)
with closing(open_readonly(compatible_db)) as sealed:
    identity = tuple(sealed.execute("SELECT course_id,slug,version FROM courses WHERE course_id=?",
                                    (manifest.course_id,)).fetchone())
    sealed_chapters = [tuple(r) for r in sealed.execute(
        "SELECT chapter_number,title,path,free_preview FROM chapters WHERE course_id=? ORDER BY chapter_number",
        (manifest.course_id,),
    )]
assert identity == (manifest.course_id, manifest.slug, manifest.version)
assert sealed_chapters == sorted((c.number,c.title,c.path,int(c.free_preview)) for c in manifest.chapters)
with transaction(copied_db, immediate=True) as connection:
    assert tuple(connection.execute("SELECT course_id,slug,version FROM courses WHERE course_id=?",
                                    (manifest.course_id,)).fetchone()) == identity
    assert [tuple(r) for r in connection.execute(
        "SELECT chapter_number,title,path,free_preview FROM chapters WHERE course_id=? ORDER BY chapter_number",
        (manifest.course_id,),
    )] == sealed_chapters
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(courses)")}
    if "package_hash" in columns:
        old_hash = connection.execute("SELECT package_hash FROM courses WHERE course_id=?", (manifest.course_id,)).fetchone()[0]
        assert old_hash is None or old_hash == inspected.fingerprint
    connection.execute("UPDATE courses SET content_path=? WHERE course_id=?", (str(package), manifest.course_id))
```

确实缺失内容的历史课程不能因原库无 hash 就猜测不可用，也不能被可验证课程示例覆盖。逐项记录不可用依据，在工作 DB 中将该条路径明确指向工作内容根内**不存在**的对应位置（检查新路径 containment、确认没有替代文件），只更新 content_path；保留原行、NULL hash、未知政策/核验、独立期限与进度。转换将真实保留不可用状态，不删除或补造承诺。所有可验证/不可用记录处理完后，逐行核对路径均在工作内容根内，不允许任何原路径留到转换。

退出维护会话。在仍指向工作副本的运行窗口执行 `.venv/Scripts/python.exe -m course_platform.cli migrate --backup <副本升级前的非覆盖备份>`，显式离线转换至 latest5。这个升级前备份是在 relocation 后另存的，不代替 relocation 前封存的旧兼容 DB＋匹配内容。转换失败停止，不在线重试/自动补 hash。转换后逐课程比较 legacy origins 的实际 availability/hash、身份、原期限、独立 sessions/进度及未知政策；完整匹配内容不应因原路径消失而变成 unavailable。

### 校验、受控切换与兼容回滚

1. 再执行 `.venv/Scripts/python.exe -m course_platform.cli migrate --check-only`，核对 latest5、foreign_key_check、完整性及迁移历史；比较封存/工作 DB 的 IDs、原权益/激活截止、核验与快照、独立会话/进度。启动新工厂只指向工作 DB/内容根；原目录即使另行存在，也须证明只读匹配的新副本，并证明篡改副本拒绝交付。根外旧路径拒绝不能替代实际恢复。
2. 演练现代销售、已核验历史短码、独立迁移权益、进度、原到期、下载及重置/撤销。授权维护者仅在演练通过后决定受控切换；本任务没有生产切换、部署或公开运营授权。切换前再次确认停写备份一致、配置只指向工作副本。
3. 失败则停止新服务；旧程序不能读取已升级 DB。恢复旧程序和**封存的旧兼容 DB＋该时点匹配内容**及其旧配置。若其内容路径需适配回滚位置，另做兼容工作副本、验证匹配后显式 relocation，不修改封存快照。若切换后有新写入，先独立保全新库并处理差异，不能覆盖新订单/进度。保留失败副本定位。

停写一致备份可在 `.venv/Scripts/python.exe` 维护会话使用：

```python
from pathlib import Path
from course_platform.database import backup_database, check_database
backup_database(Path("<停写源DB绝对路径>"), Path("<新的非覆盖备份绝对路径>"))
print(check_database(Path("<新程序验证副本DB绝对路径>")))
```

Task12 演练复制匹配 DB＋文件，核对 FK/身份/hash/原承诺/期限/进度，先 relocation 后转换旧库，并实读复制 bytes；篡改现代副本拒绝交付。时间与结果见同计划报告；这是测试副本证据，不是生产备份、切换或旧程序回滚重启证据。

原始兼容备份、匹配文件树和新副本应各自保存，不将测试日志、CSV 或首次响应凭据当作备份材料。本轮 fresh 安装使用 Python3.11.9，开发测试用 Chrome154.0.8037.93、Node24.16.0；fresh 安装的 tzdata2026.5/MarkupSafe3.0.4/websockets17.2 与开发 venv 的2026.3/3.0.3/17.1分开记录。构建隔离环境使用 setuptools84.0.0；fresh 安装环境自带 setuptools65.5.0，不混称为构建版本。

## 验收命令及环境限制

以下是**源码仓库根目录的开发验收 venv** 命令，不能混用上面的独立安装运营目录；开发工具需要 `dev` 依赖。原始整轮完整验收命令如下，不能以默认跳过 browser/packaging 代替。审查修复轮按 controller 指定 covering cases 验证，不声称重跑未执行的全量：

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
