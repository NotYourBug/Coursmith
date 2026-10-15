# Coursmith · 智课工坊

这个项目可以帮你制作课程：填写课程题目，让 AI 写大纲和正文，再由你检查、修改和保存。也可以在后台管理已有课程、商品和订单。

下面按 **Rocky Linux 9.5、暂时没有域名和 HTTPS** 的情况说明。

## 先看这几句话

你平时在浏览器里使用项目，不需要一直输入命令。命令是用来安装和启动的。

首次使用按下面的 1 → 2 → 3 → 4 操作。已经装好并启动的，可以直接看第 4 步。

启动时需要三个窗口，别混淆它们：

| 窗口 | 在哪里打开 | 用来做什么 |
| --- | --- | --- |
| 服务器窗口 A | 云服务器的 SSH 终端 | 运行网页 |
| 服务器窗口 B | 同一台服务器、同一账号的第二个 SSH 终端 | 执行生成任务，后文叫 worker |
| 电脑窗口 C | 你自己电脑的 PowerShell 或终端 | 连接服务器，让电脑浏览器可以访问网页 |

这套方式先让你跑通项目。三个窗口保持运行，关掉对应窗口会影响使用。以后需要退出 SSH 后继续运行，再看文末的长期运行说明。

## 1. 首次安装：在服务器窗口 A 操作

如果已经完成本手册的安装，就跳过这一节。请使用准备运行项目的 Linux 账号；安装软件时需要 sudo 权限。

先复制执行：

```bash
sudo dnf install -y python3.12 python3.12-pip
```

然后进入你上传的项目文件夹。**下面只是示例**：如果实际位置不是 `/opt/Coursmith`，就换成自己的路径。

```bash
cd /opt/Coursmith
```

确认这里能看到 `pyproject.toml` 和 `course_platform/cli.py`：

```bash
ls pyproject.toml course_platform/cli.py
```

如果显示找不到文件，说明进错了文件夹，先找到正确位置，不要继续。

接着复制执行这一整段，安装程序：

```bash
COURSMITH_SOURCE="$(pwd -P)"
mkdir -p "$HOME/coursmith-runtime"
python3.12 -m venv "$HOME/coursmith-runtime/.venv"
"$HOME/coursmith-runtime/.venv/bin/python" -m pip install --upgrade pip
"$HOME/coursmith-runtime/.venv/bin/python" -m pip install \
  -c "$COURSMITH_SOURCE/docs/operations/m2a-validated-requirements.txt" \
  "$COURSMITH_SOURCE"
cd "$HOME/coursmith-runtime"
.venv/bin/python -m pip check
```

看到 `No broken requirements found.` 表示安装依赖检查通过。任何一步报错，先处理错误再继续。Windows 电脑里的 `.venv` 不能直接拿到 Linux 使用。

这里创建的 `coursmith-runtime` 是以后启动程序的位置，不是上传源码的文件夹。软件安装好后，仅修改上传的源码不会自动更新已安装程序。

## 2. 首次设置：还是在服务器窗口 A

下面为你创建一个新的本地数据库配置。**如果已经有业务数据库和账号，不要用这份新配置替换它们**；迁移已有数据的方法放在文末。

复制执行：

```bash
cd "$HOME/coursmith-runtime"
umask 077
if [ ! -e runtime.env ]; then
  cat > runtime.env <<EOF
COURSE_DATABASE="$PWD/data/course.db"
COURSE_CONTENT_ROOT="$PWD/courses"
COURSE_ENVIRONMENT=development
COURSE_SITE_ORIGIN=http://127.0.0.1:8000
COURSE_TRUSTED_PROXY_CIDRS=""
COURSE_BASE_URL=https://api.deepseek.com/
COURSE_MODEL=deepseek-flash
COURSE_MODEL_CONTEXT_TOKENS=""
COURSE_TASK_TOKEN_LIMIT=200000
COURSE_MODEL_PRICE_JSON=""
DEEPSEEK_API_KEY=""
EOF
fi
chmod 600 runtime.env
```

先不填写模型密钥也可以打开后台，只是暂时不能让 AI 写课程。后面第 5 步再配置。

再复制执行，读取配置并创建数据库：

```bash
set -a
source ./runtime.env
set +a
.venv/bin/python -X utf8 -m course_platform.cli migrate
```

成功后创建管理员：

```bash
.venv/bin/python -X utf8 -m course_platform.cli init-admin
```

它会依次让你输入：

1. 管理员账号，例如 `admin`。
2. 密码，需要 12–128 个字符。
3. 再输入一次相同密码。

输入密码时屏幕不会显示字符，这是正常的。账号创建成功后记住它，以后不用重复初始化。

## 3. 启动：服务器窗口 A 和 B 各执行一次

**窗口 A：复制下面整段，启动网页。**

```bash
cd "$HOME/coursmith-runtime"
set -a
source ./runtime.env
set +a
.venv/bin/python -X utf8 -m course_platform.cli serve --host 127.0.0.1 --port 8000
```

看到类似 `Uvicorn running on http://127.0.0.1:8000` 的提示后，保持这个窗口打开。

**窗口 B：登录同一台服务器、同一个 Linux 账号，复制下面整段。**

```bash
cd "$HOME/coursmith-runtime"
set -a
source ./runtime.env
set +a
.venv/bin/python -X utf8 -m course_platform.cli worker
```

看到 `worker_ready` 表示执行任务的进程已启动。若还显示 `local_only`，说明模型配置没填全：可以打开后台、手动编辑和检查，暂时不能自动生成。

**以后每次启动，只执行这一节，不用再安装或创建管理员。**

## 4. 打开后台：在自己的电脑操作

现在才轮到**电脑窗口 C**。Windows 可以从开始菜单打开 PowerShell。

**以前没有连过也可以，不需要先“互发密钥”。**服务器允许密码登录时，用服务器的 Linux 账号和密码就能连接。若服务器只允许密钥登录，需要先将电脑的公钥登记到服务器对应账号；私钥留在电脑。登录云厂商网页控制台，并不代表这台电脑已经能用 SSH 登录服务器。[OpenSSH 登录方式说明](https://man.openbsd.org/ssh.1)。

先测试一次普通登录，把下面的账号和 IP 换成自己的：

```bash
ssh 你的SSH账号@你的服务器IP
```

这里的 SSH 账号是**服务器 Linux 账号**，不是第 2 步创建的项目管理员。账号、IP 和 SSH 端口可在云厂商控制台查看。

首次连接可能提示服务器指纹；先与云控制台或服务器管理员提供的指纹核对，再输入 `yes`。若出现密码提示，输入服务器账号密码，屏幕不显示字符是正常的。若出现 `Permission denied (publickey)`，表示本次密钥认证没有成功，需要检查或配置密钥登录；不要继续尝试项目管理员密码。若连接超时，先检查 IP、SSH 端口与云安全组。

成功进入服务器后输入 `exit` 回到电脑，再复制下面命令建立网页连接：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8000:127.0.0.1:8000 你的SSH账号@你的服务器IP
```

例如账号为 `alice`、服务器地址为 `203.0.113.10`，最后一项就是 `alice@203.0.113.10`；这个 IP 只是示例，不能直接使用。如果 SSH 端口不是 22，在命令中加 `-p 你的SSH端口`。

首次连接确认服务器身份后，按提示登录。连接成功后窗口可能一直没有新输出，这是正常的，保持它打开。这条命令把电脑和服务器连起来，你不用先买域名或配置 HTTPS。

电脑浏览器打开：

**<http://127.0.0.1:8000/admin/login>**

使用第 2 步创建的管理员账号和密码登录。这里访问的是你电脑的 `127.0.0.1`，经 SSH 转到服务器；不要把地址改成服务器公网 IP 或 `localhost`。

如果提示本机端口已经被占用，按 [详细说明](docs/operations/rocky-linux-guide.md#无域名时访问后台) 同时调整端口与访问地址配置。

## 5. 想让 AI 写课程：填写模型配置

先让第 4 步的登录页面正常打开，再做这一节。实际生成和连接测试可能产生 DeepSeek 费用。

需要准备两个信息：DeepSeek API Key，以及 `deepseek-flash` 实际支持的上下文 token 上限。**后者要查供应商说明；不是这里的单任务额度 `200000`。**

在服务器窗口 A、B 分别按 `Ctrl+C` 停止程序，然后在 A 中打开配置文件：

```bash
cd "$HOME/coursmith-runtime"
vi runtime.env
```

编辑方法：

1. 按键盘 `i`，进入编辑状态。
2. 将 `DEEPSEEK_API_KEY=""` 的双引号里面填上自己的密钥。
3. 将 `COURSE_MODEL_CONTEXT_TOKENS=""` 的双引号里面填上已确认的正整数上限。
4. 按 `Esc`，输入 `:wq`，再按回车，保存并退出。

其他设置先保持原样。密钥不用发给别人，也不要上传到 Git。

**然后在 A、B 两个窗口重新执行第 3 步的完整命令。**只改文件、没有重启，正在运行的程序不会更新配置。

## 6. 制作第一门课程

登录后台后，按下面操作即可。

### 先试快速制作

1. 点击「制作项目」→「新建课程」。
2. 选择「快速制作」，填写题目，例如「零基础 Excel 入门」。
3. 第一次建议填 **3 章**，方便检查效果；默认是 10 章，最多 60 章。
4. 点击「创建课程项目」。
5. 找到「生成与检查任务」，选择「开始制作草稿」，点击「提交制作任务」。
6. 点击任务链接查看进度。生成完成后，点击章节检查正文。
7. 不满意的地方直接修改，再点击保存；使用「只读安全预览」查看效果。

如果一直排队，先检查服务器窗口 B 是否还在运行。若提示模型未配置，回到第 5 步。

### 想先改好大纲，再写正文

新建时选择「精细制作」，填写题目、受众、目标和参考资料。先提交「生成大纲候选」，等待完成后打开候选检查并采用，接着点击「确认当前大纲生成方向」，最后提交「生成章节正文」。

改变需求、参考资料或大纲后，需要重新确认。第一次不熟悉时，可以先使用快速模式。

### 日常最常用的几个功能

| 想做什么 | 在哪里操作 |
| --- | --- |
| 找回之前的课程 | 「制作项目」列表 |
| 看生成到哪一步 | 「任务中心」或项目中的任务链接 |
| 改正文 | 点击对应章节，编辑后明确保存 |
| 重写某一章 | 章节页面发起重做，再检查新候选并明确采用或拒绝 |
| 看课程效果 | 项目的「只读安全预览」 |
| 检查遗漏 | 「记录草稿检查」，按提示补齐并保存 |
| 固定当前版本供交接 | 处理检查问题和候选后，点击「生成交接快照」 |

没有自动保存，修改后记得点击保存。交接快照保存当时的版本，以后继续编辑不会改变旧快照。课程草稿不会自动上架销售，内容质量和来源授权仍要人工检查。

生成失败时，先看任务详情，**不要不停点击重试**。有些请求虽没收到结果，供应商可能已接受并计费；系统会保留这类未知用量，需要核对后明确恢复。

## 需要时再看这些

- **停止程序**：服务器 A、B 各按 `Ctrl+C`。电脑 C 也可按 `Ctrl+C` 关闭连接。已经发出的模型请求仍可能计费。
- **退出 SSH 后继续运行、开机启动、看日志**：[长期运行步骤](docs/operations/rocky-linux-guide.md#systemd-长期运行)。配置好后，服务器 A、B 不用一直打开。
- **搬迁旧数据库、升级、备份或恢复**：[详细部署与维护说明](docs/operations/rocky-linux-guide.md#数据库维护与升级)。已经有业务数据时先看这里，不要删除数据库重来。
- **已有商品、订单和课程交付**：[M1 操作手册](docs/operations/admin-v1-runbook.md)。
- **公开给学员访问**：需要另行配置 HTTPS；本文先解决你自己使用后台的问题。
- **ChromeDriver**：启动网页和制作工作台不需要它；旧版 HTML 转 PNG 功能另有要求。

本手册的启动命令已检查语法；服务器上的安装和启动仍以你实际执行的结果为准。完整配置和故障对照表保存在 [Rocky Linux 详细说明](docs/operations/rocky-linux-guide.md)。
