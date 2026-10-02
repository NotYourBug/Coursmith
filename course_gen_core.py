# -*- coding: utf-8 -*-
"""
课程生成核心库 —— 被 batch_course_gen.py / gui_course_gen.py / feishu_bot.py 共用。
提供：配置管理、提示词模板、HTML 模板、API 调用、后处理(ZIP/PNG)、管线函数。
"""

import json
import os
import re
import time
import base64
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from openai import OpenAI

# 修复 Windows 系统代理干扰 httpx 连接的问题
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.setdefault(_k, "")
os.environ.setdefault("no_proxy", "*")

_file_lock = threading.Lock()
_cwd_lock = threading.Lock()

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")

# ── 全局状态 ──────────────────────────────────────────────
dict_html_list = []
base_url = ""
api_key = ""
browser_path = "D:\\Program Files\\chromedriver-win64\\chromedriver.exe"
g_enable_retry = False
PROMPT_TEMPLATE_VERSION = 5

# ── 样式参考模式 ──────────────────────────────────────────
STYLE_REFERENCE_MODE_BUILTIN = "builtin"
STYLE_REFERENCE_MODE_TEXT = "text"
STYLE_REFERENCE_MODE_FILE = "file"

BUILTIN_STYLE_SAMPLE = """【讲师式文风示例（只用于学习风格，不要照抄）】
开场先把框架搭清楚：这节课我们要解决什么问题、为什么重要、学完能干什么。
一句话总结：把核心概念用"大白话"说清楚，给读者一个抓手。
再讲直觉，再讲原理，最后落到"怎么做/怎么写代码/怎么排查"。
穿插经验：我通常会怎么做、我踩过的坑、最容易搞错的点。
结尾收束：总结本节要点，干净利落地结束，不预告或衔接下一章内容。"""

style_reference_mode = STYLE_REFERENCE_MODE_BUILTIN
style_reference_text = ""
style_reference_file = ""

# ── 提示词模板 ────────────────────────────────────────────
question_gen_chapter = '''你是一名资深课程架构师、教学设计专家。现在需要为课程《aaaaaaaaaa》生成一套高质量课程大纲。

请严格遵守以下要求：
1. 只输出一个 Python dict，不要解释，不要 markdown 代码块。
2. 字典格式固定为：
{
    "name":"英文课程代号_下划线风格",
    "home":"《课程中文名》",
    "question":[
        "1、章节标题",
        "2、章节标题"
    ]
}
3. question 必须正好为 bbbbbbbbbb 节。
4. 每个章节标题尽量采用"主标题：关键词1、关键词2、关键词3"的课程目录风格。
5. 主标题必须简洁、专业，建议 4-10 个汉字；冒号后补充 2-4 个关键词即可，不要写成长句。
6. 章节之间要有循序渐进关系：基础认知 -> 核心概念 -> 方法流程 -> 实战应用 -> 进阶专题。
7. 标题要更像优秀培训教材目录，而不是普通问答句；多用"基础 / 原理 / 架构 / 流程 / 实战 / 案例 / 优化 / 运维 / 排障 / 总结"等课程化表述。
8. 如果适合工程技术类课程，章节组织尽量体现"概述、基础、模型、方法、实现、案例、优化、总结"的层次。
9. 每个章节的关键词要能直接用于课程目录卡片上的标签展示，因此必须短、准、清晰。
10. 不要生成"第一节课我们来学习……"这种口语化标题。
11. 不要生成过于相似的标题，不要出现明显重复。
12. 输出内容必须是中文课程目录，编号从 1 开始。

请直接输出 dict。'''

question_gen_catalog = "该字段当前仅用于兼容旧配置，实际课程目录页由本地脚本直接生成。"

lesson_html_prompt_template = '''你是一名顶级课程内容策划专家、资深讲师、技术写作者、资深前端设计师、信息可视化设计师。你正在为课程《__COURSE__》制作其中一节课，当前章节主题是：__TOPIC__。

你的任务是：输出一个"信息密度高、重视文本内容、像真实讲师讲义、排版成熟、适合培训资料/知识讲义"的单页 HTML。

下面是我给你的文风参考，请学习其叙述节奏、讲解方式、经验表达、案例组织方式，但绝对不要照抄原文：
__STYLE_SAMPLE__

一、总体目标
1. 只输出完整 HTML，不要输出 markdown 代码块，不要解释说明。
2. 页面重点是"文本讲解质量 + 结构化表达 + 清晰排版"，不是炫技海报。
3. 成品风格参考优秀课程讲义：
- 浅米白背景、白色或奶油色内容卡片、暖黄色描边、蓝色标题点缀
- 标题清晰、层级分明、阅读舒适，像成熟课程讲义而不是营销海报
- 重点内容通过提示框、表格、列表、分步说明呈现
- 整体接近"优秀技术课程目录页 + 讲义正文页"的审美

二、严格的视觉与布局要求
1. 必须使用纯 HTML + CSS + 内联 SVG；不要引用外部 CSS/JS/图片/字体。
2. 页面主体采用浅色、护眼、专业的阅读风格；禁止整页深色背景、禁止大面积纯黑背景。
3. 不要生成封面海报式巨型头图，不要在顶部留下大块空白。
4. 页面宽度以阅读舒适为核心，正文容器建议 920px-1080px。
5. body 背景和正文背景要协调，页面一打开就直接看到内容。
6. 整体要像"成熟讲义页面"：顶部标题区 + 导读摘要 + 分节正文 + 知识体系图/总结。
7. 保证移动端和桌面端都可读，使用响应式布局。
8. 建议使用这些视觉语言：
- 页面主色：浅米白、暖黄、浅蓝
- 一级标题前有竖向色条或小图标
- 提示框使用淡黄/淡蓝/淡红/淡绿底色
- 表格边框细致、颜色柔和
- 内容区圆角适中，避免夸张阴影

三、内容组织要求
1. 先输出一个简洁有力的主标题（title 和 h1 都要尽量简明，控制在 8-22 个汉字）。
2. 开头第一段要有"讲师开场感"，要自然切入主题，先搭框架，再展开。
3. 在开头或前 1/3 部分，必须有一个"用大白话解释概念"的短提示框。
4. 正文必须有 4-7 个清晰的小节（h2/h3）。
5. 每小节必须有实质性讲解文字。
6. 至少包含以下结构中的 5 项：要点列表、对比表格、分步流程、典型案例/场景、常见问题/误区、小结/记忆提示、内联 SVG 流程图/结构图、彩色提示框。
7. 如果主题适合，用简洁真实的工程案例解释。
8. 如果涉及术语，要解释"是什么、为什么、怎么做"。
9. 如果涉及流程，要讲顺序、输入输出、注意事项。
10. 尽量在正文中加入"我的经验 / 踩过的坑 / 实战提醒"风格的小块内容，自然克制。
11. 在正文结尾加入"本章知识体系"或"本节结构图"版块，使用内联 SVG 或流程结构块。
12. 结尾用一小段收束全文，总结本节核心要点即可。

四、写作质量要求
1. 内容必须是中文。
2. 语言专业、清晰、自然，像优秀讲师在写讲义。
3. 文风贴近"讲师式讲义"：有讲师陪伴感但不油腻，先讲直觉再讲概念再讲方法。
4. 不要堆砌空话，不要泛泛而谈。
5. 多写"有信息量"的句子。
6. 适当加入"经验提示/误区提醒/案例理解"。
7. 若章节标题较长，请自动提炼一个更短的页面标题作为 h1/title。

五、内容准确性与权威性要求（重要）
1. 涉及的技术概念、术语、流程必须准确无误，不得凭空编造。
2. 引用的数据、标准、规范必须真实存在，不确定的内容宁可省略也不编造。
3. 代码示例必须语法正确、逻辑合理。
4. 优先引用业界公认的最佳实践和权威来源（官方文档、RFC、ISO标准等）。
5. 对存在争议的技术观点，应客观呈现不同方案及其适用场景。
6. 避免使用"最新""最先进""最好"等无依据的绝对化表述。

六、视觉丰富度要求（重要）
1. 至少使用 3 种以上不同的视觉表达方式：提示框、表格、流程图、代码块、对比卡等。
2. 色彩使用丰富但不杂乱：在浅米白主色基础上搭配 2-3 种柔和辅助色。
3. 内联 SVG 图表必须有实际信息含量。
4. 重要概念使用彩色提示框突出，不同性质用不同颜色（重点=蓝、经验=绿、警告=橙、易错=红）。
5. 表格至少包含 3 列或 3 行以上，有表头，数据有意义。
6. 适当使用图标符号增强视觉节奏。

七、代码与输出约束
1. 输出必须是完整 HTML 文档，包含 <!doctype html>、<html>、<head>、<body>。
2. 只输出 HTML 本体，不要任何额外说明。
3. 不要使用网络图片，不要引用任何外部 URL。
4. 不要使用固定海报尺寸容器。
5. 代码示例必须使用 <pre><code> 保证可读性。
6. 页脚 footer 文本固定为：__FOOTER__
7. 不要生成"上一章/下一章/返回目录"按钮，这些由外层页面负责。
8. 不要生成左侧目录栏，这些由外层页面负责。
9. 页面打开后第一屏必须直接看到标题和正文开头。
10. 禁止输出"本文将介绍……"这种机械 AI 套话。
11. 在页面底部（footer 区域）生成一个"联系我们"超链接按钮：使用 <a> 标签（href="#" 或 href="contact_us.png"），圆角、暖色渐变背景、白色文字、居中显示，必须是可点击的链接元素。
12. 禁止在结尾做下一章预告或衔接（如"下一章我们将……""后续章节会……""在接下来的课程中……"等），每节只讲自己的内容，讲完干净收束，不要为后续章节做铺垫或过渡。

八、加分项
1. 用浅金、浅蓝、浅绿等柔和颜色做信息层次。
2. 让表格、提示框、SVG 图有统一设计语言。
3. 让页面看上去像真正能交付的课程讲义成品。
4. 如果章节适合工程类表达，让内容更像"培训讲义 + 实战笔记"的结合体。

请直接输出最终 HTML。'''

mulu_tishici = '''我有以下HHHHH门课程(课程列表如下所示)，帮忙创建一个目录页面HTML，点击课程封面或课程标题，就能直接跳转到课程标题命名的文件夹下的index.html中。
课程封面:默认编程主题封面（渐变色背景）.每个课程链接指向 课程名称/index.html（不要移除书名号）， 例如：《C#编程基础入门》课程链接指向 《C#编程基础入门》/index.html。 请生成这个html目录页.
+
课程列表：
KKKKK
+
'''

# ── 工具函数 ──────────────────────────────────────────────

def sanitize_filename(title):
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', title)


def append_to_file(content, filename="tishici.txt"):
    try:
        with _file_lock:
            with open(filename, 'a', encoding='utf-8') as file:
                file.write(content)
                file.write('\n')
    except IOError as e:
        print(f"写入文件时出错: {e}")


def load_style_reference(max_chars=2600):
    global style_reference_mode, style_reference_text, style_reference_file
    if style_reference_mode == STYLE_REFERENCE_MODE_TEXT:
        content = (style_reference_text or "").strip()
        return content[:max_chars] if content else BUILTIN_STYLE_SAMPLE
    if style_reference_mode == STYLE_REFERENCE_MODE_FILE:
        path = (style_reference_file or "").strip()
        if not path:
            path = os.path.join(os.getcwd(), "example.txt")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                content = re.sub(r"\n{3,}", "\n\n", content)
                return content[:max_chars] if content else BUILTIN_STYLE_SAMPLE
            except Exception:
                return BUILTIN_STYLE_SAMPLE
        return BUILTIN_STYLE_SAMPLE
    return BUILTIN_STYLE_SAMPLE


def build_lesson_prompt(course_title, topic, footer_text):
    prompt = lesson_html_prompt_template.replace("__COURSE__", str(course_title))
    prompt = prompt.replace("__TOPIC__", str(topic))
    prompt = prompt.replace("__FOOTER__", str(footer_text))
    style_sample = load_style_reference()
    prompt = prompt.replace("__STYLE_SAMPLE__", style_sample)
    return prompt


def build_outline_prompt(course_title, lesson_count, footer_text):
    prompt = question_gen_chapter.replace("aaaaaaaaaa", str(course_title).strip("《》"))
    prompt = prompt.replace("bbbbbbbbbb", str(lesson_count))
    return prompt


# ── 配置管理 ──────────────────────────────────────────────

def init_config_file():
    default_config = {
        "base_url": "https://api.deepseek.com/",
        "api_key": "",
        "browser_path": browser_path,
        "question_gen_chapter": question_gen_chapter,
        "question_gen_catalog": question_gen_catalog,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "style_reference_mode": style_reference_mode,
        "style_reference_text": style_reference_text,
        "style_reference_file": style_reference_file,
        "lessons_per_course": 30,
        "feishu_app_id": "",
        "feishu_app_secret": "",
    }
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, ensure_ascii=False, indent=4)


_cached_lessons = 30
_cached_feishu_app_id = ""
_cached_feishu_app_secret = ""

def load_config():
    global base_url, api_key, browser_path, question_gen_chapter, question_gen_catalog
    global style_reference_mode, style_reference_text, style_reference_file
    global _cached_lessons, _cached_feishu_app_id, _cached_feishu_app_secret
    init_config_file()
    load_dotenv(os.path.join(_CONFIG_DIR, '.env'), override=False)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        config_version = data.get('prompt_template_version', 0)
        if config_version < PROMPT_TEMPLATE_VERSION:
            data['question_gen_chapter'] = question_gen_chapter
            data['question_gen_catalog'] = question_gen_catalog
            data['prompt_template_version'] = PROMPT_TEMPLATE_VERSION
            data.setdefault('style_reference_mode', style_reference_mode)
            data.setdefault('style_reference_text', style_reference_text)
            data.setdefault('style_reference_file', style_reference_file)
            data.setdefault('lessons_per_course', 30)
            data.setdefault('feishu_app_id', '')
            data.setdefault('feishu_app_secret', '')
            save_config(data)
        base_url = os.environ.get('COURSE_BASE_URL', data.get('base_url', ''))
        api_key = os.environ.get('DEEPSEEK_API_KEY', '')
        browser_path = data.get('browser_path', browser_path)
        question_gen_chapter = data.get('question_gen_chapter', question_gen_chapter)
        question_gen_catalog = data.get('question_gen_catalog', question_gen_catalog)
        style_reference_mode = data.get('style_reference_mode', style_reference_mode)
        style_reference_text = data.get('style_reference_text', style_reference_text)
        style_reference_file = data.get('style_reference_file', style_reference_file)
        _cached_lessons = int(data.get('lessons_per_course', 30))
        _cached_feishu_app_id = os.environ.get('FEISHU_APP_ID', '')
        _cached_feishu_app_secret = os.environ.get('FEISHU_APP_SECRET', '')
    except Exception as e:
        print(f"加载配置文件失败: {str(e)}")


def save_config(data):
    try:
        data = dict(data)
        # Credentials belong in the local environment, never in config.json.
        data['api_key'] = ''
        data['feishu_app_secret'] = ''
        data.pop('feishu_encrypt_key', None)
        data.pop('feishu_verification_token', None)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"写入配置文件失败: {str(e)}")


def get_config():
    """返回当前已加载配置的副本（需先调用 load_config()）"""
    return {
        "base_url": base_url,
        "api_key": api_key,
        "browser_path": browser_path,
        "lessons_per_course": _cached_lessons,
        "feishu_app_id": _cached_feishu_app_id,
        "feishu_app_secret": _cached_feishu_app_secret,
    }


def _read_config_str(key, default=""):
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


def _read_config_int(key, default=30):
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            val = json.load(f).get(key, default)
            return int(val) if val is not None else default
    except Exception:
        return default


# ── HTML 工具 ─────────────────────────────────────────────

def _clean_chapter_title(text):
    return re.sub(r'^\s*\d+\s*[、\.．]\s*', '', str(text)).strip()


def _split_title_and_tags(text):
    cleaned = _clean_chapter_title(text)
    if "：" in cleaned:
        head, tail = cleaned.split("：", 1)
    elif ":" in cleaned:
        head, tail = cleaned.split(":", 1)
    else:
        return cleaned, []
    tags = []
    for item in re.split(r"[、,，/｜|；;]\s*", tail):
        item = item.strip()
        if not item:
            continue
        tags.append(_short_text(item, 10))
        if len(tags) >= 4:
            break
    return head.strip() or cleaned, tags


def _short_text(text, max_len=18):
    s = str(text).strip()
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1] + "…"


def _html_escape(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _b64_encode_utf8(s):
    if s is None:
        s = ""
    return base64.b64encode(str(s).encode("utf-8")).decode("ascii")


def _b64_decode_utf8(s):
    return base64.b64decode(s.encode("ascii")).decode("utf-8", errors="replace")


# ── HTML 模板 ─────────────────────────────────────────────

def build_course_index_html(course_title, questions, footer_text):
    cards = []
    for i, q in enumerate(questions, start=1):
        full_title = _clean_chapter_title(q)
        main_title, tags = _split_title_and_tags(full_title)
        title = _short_text(main_title, 16)
        href = f"{i:02d}.html"
        tags_html = "".join([f'<span class="tag">{_html_escape(tag)}</span>' for tag in tags[:3]])
        cards.append(
            f'''<a class="card" href="{href}" title="{_html_escape(full_title)}">
  <div class="row1">
    <div class="no">{i:02d}</div>
    <div class="t">{_html_escape(title)}</div>
  </div>
  <div class="tags">{tags_html}</div>
  <div class="file">{href}</div>
</a>'''
        )
    card_html = "\n".join(cards)
    safe_title_full = _html_escape(course_title)
    safe_title = _html_escape(_short_text(course_title, 26))
    safe_footer = _html_escape(footer_text)
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title_full}</title>
  <style>
    :root {{
      --bg:#f8f3e9; --text:#2f3a4b; --muted:#8a7f6a;
      --shadow: 0 18px 46px rgba(109, 89, 55, .10);
      --shadow2: 0 10px 24px rgba(109, 89, 55, .08);
      --radius: 22px; --cardbg: rgba(255,255,255,.92); --line: #eadfcb;
      --gold:#e2b54b; --blue:#2f74df; --pill:#eef5ff;
    }}
    body {{
      margin:0; font-family: ui-sans-serif, system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", Arial;
      background: radial-gradient(1100px 520px at 20% 6%, rgba(247,190,74,.10), transparent),
                  radial-gradient(900px 520px at 80% 16%, rgba(69,135,255,.08), transparent),
                  linear-gradient(180deg, #fffdfa, var(--bg));
      color: var(--text);
    }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 24px 18px 34px; }}
    .top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:14px;
            background: rgba(255,255,255,.92); border:1px solid var(--line);
            box-shadow: var(--shadow2); border-radius: var(--radius); padding:18px 18px 16px; position:relative; }}
    .top::after {{ content:""; position:absolute; left:18px; right:18px; bottom:-10px;
                   border-bottom:3px dotted rgba(226,181,75,.7); }}
    .title-row {{ display:flex; align-items:center; gap:10px; }}
    .title-icon {{ width:28px; height:28px; display:flex; align-items:center; justify-content:center;
                  border-radius:10px; background:linear-gradient(135deg,#58b7ff,#2f74df);
                  color:#fff; font-size:16px; box-shadow:0 8px 18px rgba(47,116,223,.18); }}
    .h1 {{ font-size:22px; font-weight:950; letter-spacing:.4px; line-height:1.25; color:#2b6fb5; }}
    .sub {{ margin-top:6px; color:var(--muted); font-size:13px; line-height:1.4; }}
    .badge {{ flex:0 0 auto; font-size:12px; color:#8b6508; background:#f5cd5a;
              border:1px solid rgba(226,181,75,.35); padding:7px 12px; border-radius:999px;
              box-shadow:0 8px 18px rgba(226,181,75,.18); white-space:nowrap; font-weight:800; }}
    .grid {{ margin-top:24px; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }}
    @media (max-width:1100px) {{ .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media (max-width:700px) {{ .grid {{ grid-template-columns:repeat(1,minmax(0,1fr)); }} }}
    .card {{ display:flex; flex-direction:column; align-items:flex-start; min-height:136px;
             padding:16px 16px 14px; border-radius:var(--radius); background:var(--cardbg);
             border:1px solid var(--line); box-shadow:0 12px 28px rgba(109,89,55,.08);
             text-decoration:none; color:inherit; transition:transform .12s,border-color .12s; position:relative; overflow:hidden; }}
    .card:hover {{ transform:translateY(-1px); box-shadow:0 16px 36px rgba(109,89,55,.10); border-color:rgba(47,116,223,.20); }}
    .row1 {{ display:flex; align-items:flex-start; gap:10px; width:100%; }}
    .no {{ width:38px; height:38px; border-radius:14px; display:flex; align-items:center; justify-content:center;
           font-weight:950; color:#8b6508; background:linear-gradient(135deg,#ffedb9,#ffd781);
           border:1px solid rgba(226,181,75,.24); flex:0 0 auto; }}
    .t {{ font-size:21px; font-weight:900; line-height:1.25; color:#466983; margin-top:2px;
          display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden; }}
    .tags {{ margin-top:14px; display:flex; flex-wrap:wrap; gap:7px; min-height:28px; }}
    .tag {{ display:inline-flex; align-items:center; height:24px; padding:0 10px; border-radius:999px;
            background:var(--pill); border:1px solid #dce9fb; color:#7b97b9; font-size:12px; font-weight:700; }}
    .file {{ margin-top:auto; align-self:flex-end; color:#aab7c9; font-size:12px; font-weight:700; }}
    footer {{ margin-top:16px; text-align:center; color:var(--muted); font-size:12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div><div class="title-row"><div class="title-icon">📘</div><div class="h1">{safe_title}</div></div>
      <div class="sub">点击任意章节卡片进入对应页面</div></div>
      <div class="badge">{len(questions)}节 · 动手学</div>
    </div>
    <div class="grid">{card_html}</div>
    <footer>{safe_footer}</footer>
  </div>
</body>
</html>'''


def build_chapter_wrapper_html(course_title, questions, chapter_index, chapter_title,
                               footer_text, raw_html_b64, prev_href, next_href):
    nav_items = []
    for i, q in enumerate(questions, start=1):
        full_title = _clean_chapter_title(q)
        title = _short_text(full_title, 18)
        href = f"{i:02d}.html"
        active = " active" if i == chapter_index else ""
        nav_items.append(
            f'<a class="nav-item{active}" href="{href}" title="{_html_escape(full_title)}">'
            f'<span class="nav-no">{i:02d}</span><span class="nav-t">{_html_escape(title)}</span></a>'
        )
    nav_html = "\n".join(nav_items)
    safe_course_full = _html_escape(course_title)
    safe_course = _html_escape(_short_text(course_title, 20))
    safe_chapter_full = _html_escape(chapter_title)
    safe_chapter = _html_escape(_short_text(chapter_title, 24))
    safe_footer = _html_escape(footer_text)
    prev_btn = f'<a class="btn" href="{prev_href}">← 上一章</a>' if prev_href else '<span class="btn disabled">← 上一章</span>'
    next_btn = f'<a class="btn" href="{next_href}">下一章 →</a>' if next_href else '<span class="btn disabled">下一章 →</span>'
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="x-wrapper" content="sidebar-v2" />
  <title>{safe_course_full} - {chapter_index:02d} {safe_chapter_full}</title>
  <style>
    :root {{ --bg:#f6f7fb; --panel:#fff; --text:#1f2937; --muted:#6b7280; --line:#e6e9f2;
            --shadow:0 18px 46px rgba(17,24,39,.10); --shadow2:0 10px 24px rgba(17,24,39,.08); --radius:18px; }}
    body {{ margin:0; font-family:ui-sans-serif,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",Arial;
            background:radial-gradient(1100px 520px at 20% 6%,rgba(37,99,235,.14),transparent),
                       radial-gradient(900px 520px at 80% 16%,rgba(34,197,94,.12),transparent),
                       linear-gradient(180deg,#fff,var(--bg)); color:var(--text); }}
    .wrap {{ max-width:1280px; margin:0 auto; padding:22px 16px 30px; }}
    .shell {{ background:rgba(255,255,255,.75); border:1px solid rgba(255,255,255,.7);
              box-shadow:var(--shadow); border-radius:22px; overflow:hidden; }}
    .layout {{ display:grid; grid-template-columns:280px 1fr; min-height:calc(100vh - 60px); }}
    @media (max-width:980px) {{ .layout {{ grid-template-columns:1fr; }} }}
    .sidebar {{ background:rgba(255,255,255,.95); border-right:1px solid var(--line); padding:16px 14px; }}
    .side-top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:10px;
                 padding:12px 12px; border-radius:16px; background:linear-gradient(135deg,rgba(37,99,235,.10),rgba(34,197,94,.10));
                 border:1px solid rgba(37,99,235,.14); }}
    .side-title {{ font-size:14px; font-weight:950; letter-spacing:.3px; }}
    .side-sub {{ margin-top:4px; font-size:12px; color:var(--muted); line-height:1.35; }}
    .side-badge {{ flex:0 0 auto; font-size:12px; color:#0f172a; background:rgba(255,255,255,.85);
                   border:1px solid rgba(0,0,0,.06); padding:6px 10px; border-radius:999px; box-shadow:var(--shadow2); white-space:nowrap; }}
    .nav {{ margin-top:12px; display:flex; flex-direction:column; gap:8px; padding-right:6px; }}
    .nav-item {{ display:flex; align-items:center; gap:10px; padding:10px 10px; border-radius:14px;
                 text-decoration:none; color:inherit; border:1px solid rgba(0,0,0,.05); background:#fff;
                 box-shadow:0 8px 18px rgba(17,24,39,.06); transition:transform .12s,border-color .12s; }}
    .nav-item:hover {{ transform:translateY(-1px); border-color:rgba(37,99,235,.30); box-shadow:0 12px 26px rgba(17,24,39,.10); }}
    .nav-item.active {{ border-color:rgba(37,99,235,.34); box-shadow:0 14px 28px rgba(37,99,235,.14); }}
    .nav-no {{ width:34px; height:34px; display:flex; align-items:center; justify-content:center; border-radius:12px;
               font-weight:950; color:#0f172a; background:linear-gradient(135deg,rgba(37,99,235,.18),rgba(34,197,94,.18));
               border:1px solid rgba(0,0,0,.06); flex:0 0 auto; }}
    .nav-t {{ font-size:13px; font-weight:750; line-height:1.25; }}
    .main {{ padding:18px 18px 26px; }}
    .top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px;
            background:rgba(255,255,255,.92); border:1px solid rgba(0,0,0,.05);
            box-shadow:var(--shadow2); border-radius:18px; padding:16px 16px; }}
    .h1 {{ font-size:20px; font-weight:950; letter-spacing:.2px; line-height:1.25; }}
    .sub {{ margin-top:6px; color:var(--muted); font-size:13px; line-height:1.4; }}
    .badge {{ flex:0 0 auto; font-size:12px; color:#0f172a; background:rgba(255,255,255,.92);
              border:1px solid rgba(0,0,0,.06); padding:6px 10px; border-radius:999px; box-shadow:var(--shadow2); white-space:nowrap; }}
    .frame {{ margin-top:14px; border-radius:18px; overflow:hidden; border:1px solid rgba(0,0,0,.06);
              box-shadow:0 14px 34px rgba(17,24,39,.10); background:#fff; }}
    .frame iframe {{ width:100%; height:600px; border:0; background:#fff; display:block; }}
    .bottom {{ margin-top:12px; display:flex; justify-content:center; align-items:center; gap:10px;
               padding:10px 10px; border-radius:18px; background:rgba(255,255,255,.92);
               border:1px solid rgba(0,0,0,.06); box-shadow:var(--shadow2); position:sticky; bottom:10px; flex-wrap:wrap; }}
    .btn {{ display:inline-flex; align-items:center; justify-content:center; min-width:108px; height:36px;
            padding:0 14px; border-radius:999px; text-decoration:none; color:#0f172a; background:#fff;
            border:1px solid rgba(0,0,0,.08); box-shadow:0 10px 20px rgba(17,24,39,.08); font-size:13px; font-weight:800; }}
    .btn:hover {{ transform:translateY(-1px); }}
    .btn.disabled {{ opacity:.45; cursor:not-allowed; pointer-events:none; }}
    footer {{ margin-top:14px; text-align:center; color:var(--muted); font-size:12px; }}
  </style>
</head>
<body>
  <div class="wrap"><div class="shell"><div class="layout">
    <aside class="sidebar">
      <div class="side-top"><div><div class="side-title">课程目录</div>
      <div class="side-sub" title="{safe_course_full}">{safe_course}</div></div>
      <div class="side-badge">{len(questions)} 节</div></div>
      <div class="nav">{nav_html}</div>
    </aside>
    <main class="main">
      <div class="top"><div><div class="h1">{chapter_index:02d} · {safe_chapter}</div>
      <div class="sub">本页直接展示完整内容</div></div><div class="badge">章节页</div></div>
      <div class="frame"><iframe id="lesson-frame" loading="eager" scrolling="no"></iframe></div>
      <div class="bottom">
        {prev_btn}
        <a class="btn" href="index.html">返回目录</a>
        {next_btn}
      </div>
      <footer>{safe_footer}</footer>
    </main>
  </div></div></div>
  <script>
    (function() {{
      const b64 = "{raw_html_b64}";
      const frame = document.getElementById("lesson-frame");
      function m() {{ try {{ const d=frame.contentDocument;if(!d)return;const b=d.body,de=d.documentElement;
      const h=Math.max(b?b.scrollHeight:0,b?b.offsetHeight:0,de?de.scrollHeight:0,de?de.offsetHeight:0);
      if(h>0)frame.style.height=(h+2)+"px"; }} catch(e){{}} }}
      function bindContact() {{
        try {{
          const doc = frame.contentDocument;
          if (!doc) return;
          doc.addEventListener('click', function(e) {{
            var el = e.target;
            while (el && el !== doc.body && el !== doc.documentElement) {{
              var txt = (el.textContent || '').replace(/\\s/g,'');
              if (txt.indexOf('联系我们') !== -1 || txt.indexOf('获取帮助') !== -1) {{
                e.preventDefault();
                window.location.href = 'contact_us.png';
                return;
              }}
              el = el.parentElement;
            }}
          }});
        }} catch(e){{}}
      }}
      frame.addEventListener("load",function(){{
        setTimeout(m,30);setTimeout(m,300);setTimeout(m,1200);
        setTimeout(bindContact, 100);
      }});
      try {{ const bin=atob(b64),bytes=new Uint8Array(bin.length);
      for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
      frame.srcdoc=new TextDecoder("utf-8").decode(bytes); }} catch(e){{ try{{frame.srcdoc=atob(b64);}}catch(e2){{frame.srcdoc="";}} }}
    }})();
  </script>
</body>
</html>'''


def build_root_index_html(courses, footer_text):
    cards = []
    for c in courses:
        title = _html_escape(_short_text(c["title"], 22))
        href = f'{c["dir"]}/index.html'
        cards.append(
            f'<a class="c" href="{href}" title="{_html_escape(c["title"])}">'
            f'<div class="ct">{title}</div><div class="cs"><span class="dot"></span>{c["count"]} 节</div></a>'
        )
    safe_footer = _html_escape(footer_text)
    cards_html = "\n".join(cards)
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>课程总目录</title>
  <style>
    :root {{ --bg:#f8f3e9; --text:#2f3a4b; --muted:#8a7f6a; --line:#eadfcb;
            --shadow:0 18px 46px rgba(109,89,55,.10); --shadow2:0 10px 24px rgba(109,89,55,.08);
            --radius:20px; --accent:#2f74df; --accent2:#f0bc43; }}
    body {{ margin:0; font-family:ui-sans-serif,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",Arial;
            background:radial-gradient(1200px 520px at 20% 6%,rgba(240,188,67,.10),transparent),
                       radial-gradient(900px 520px at 80% 16%,rgba(47,116,223,.08),transparent),
                       linear-gradient(180deg,#fffdfa,var(--bg)); color:var(--text); }}
    .wrap {{ max-width:1280px; margin:0 auto; padding:22px 16px 30px; }}
    .hero {{ background:rgba(255,255,255,.85); border:1px solid var(--line); box-shadow:var(--shadow);
             border-radius:22px; padding:18px 18px; position:relative; }}
    .hero::after {{ content:""; position:absolute; left:18px; right:18px; bottom:-10px;
                    border-bottom:3px dotted rgba(226,181,75,.7); }}
    .hero-row {{ display:flex; align-items:center; gap:10px; }}
    .hero-icon {{ width:30px; height:30px; border-radius:10px; display:flex; align-items:center; justify-content:center;
                  background:linear-gradient(135deg,#ffcc62,#ff9b5e); color:#fff; font-size:16px;
                  box-shadow:0 8px 18px rgba(240,188,67,.18); }}
    .h1 {{ font-size:26px; font-weight:950; letter-spacing:.5px; line-height:1.2; color:#ae6f22; }}
    .sub {{ margin-top:6px; color:var(--muted); font-size:13px; line-height:1.45; }}
    .grid {{ margin-top:24px; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
    @media (max-width:980px) {{ .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media (max-width:600px) {{ .grid {{ grid-template-columns:repeat(1,minmax(0,1fr)); }} }}
    .c {{ display:flex; flex-direction:column; gap:10px; padding:16px 16px; border-radius:var(--radius);
          background:rgba(255,255,255,.92); text-decoration:none; color:inherit; border:1px solid var(--line);
          box-shadow:var(--shadow2); transition:transform .12s,border-color .12s; overflow:hidden; position:relative; }}
    .c::before {{ content:""; position:absolute; inset:0; background:linear-gradient(135deg,rgba(255,247,220,.88),rgba(238,245,255,.65)); opacity:.95; pointer-events:none; }}
    .c:hover {{ transform:translateY(-2px); box-shadow:0 16px 36px rgba(17,24,39,.12); border-color:rgba(47,116,223,.22); }}
    .ct {{ position:relative; font-size:16px; font-weight:900; line-height:1.25; color:#466983; }}
    .cs {{ position:relative; font-size:12px; color:rgba(31,41,55,.75); display:flex; align-items:center; gap:8px; }}
    .dot {{ width:8px; height:8px; border-radius:999px; background:linear-gradient(135deg,var(--accent),var(--accent2)); box-shadow:0 8px 16px rgba(37,99,235,.25); }}
    footer {{ margin-top:14px; text-align:center; color:var(--muted); font-size:12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero"><div class="hero-row"><div class="hero-icon">🗂</div><div class="h1">课程总目录</div></div>
    <div class="sub">点击课程卡片进入该课程的章节目录</div></div>
    <div class="grid">{cards_html}</div>
    <footer>{safe_footer}</footer>
  </div>
</body>
</html>'''


# ── 文件操作 ──────────────────────────────────────────────

def _ensure_contact_png_in_dir(target_dir):
    """确保 target_dir 下有 contact_us.png"""
    contact_path = os.path.join(target_dir, "contact_us.png")
    if not os.path.exists(contact_path):
        project_root = os.path.dirname(os.path.abspath(__file__))
        root_contact = os.path.join(project_root, "contact_us.png")
        if os.path.exists(root_contact):
            try:
                import shutil
                shutil.copy2(root_contact, contact_path)
            except Exception:
                pass


def wrap_course_chapters_with_sidebar(course_dir, course_title, questions, footer_text):
    course_dir = os.path.abspath(course_dir)
    if not os.path.isdir(course_dir):
        return
    _ensure_contact_png_in_dir(course_dir)
    total = len(questions)
    for i, q in enumerate(questions, start=1):
        chapter_title = _clean_chapter_title(q)
        chapter_filename = f"{i:02d}.html"
        chapter_path = os.path.join(course_dir, chapter_filename)
        if not os.path.exists(chapter_path):
            continue
        try:
            with open(chapter_path, "r", encoding="utf-8") as f:
                current = f.read()
        except Exception:
            current = ""
        raw_html = None
        delete_candidate = None
        if 'name="x-wrapper" content="sidebar-v2"' in current:
            m = re.search(r'const b64 = "([^"]+)"', current)
            if m:
                try:
                    raw_html = _b64_decode_utf8(m.group(1))
                except Exception:
                    raw_html = None
        elif 'name="x-wrapper" content="sidebar-v1"' in current:
            m = re.search(r'<iframe[^>]+src="([^"]+)"', current)
            if m:
                candidate = os.path.join(course_dir, m.group(1))
                if os.path.exists(candidate):
                    try:
                        with open(candidate, "r", encoding="utf-8") as f:
                            raw_html = f.read()
                        delete_candidate = candidate
                    except Exception:
                        raw_html = None
        if raw_html is None:
            raw_html = current
        raw_b64 = _b64_encode_utf8(raw_html)
        prev_href = f"{i-1:02d}.html" if i > 1 else ""
        next_href = f"{i+1:02d}.html" if i < total else ""
        wrapper_html = build_chapter_wrapper_html(
            course_title, questions, i, chapter_title, footer_text,
            raw_b64, prev_href, next_href
        )
        try:
            with open(chapter_path, "w", encoding="utf-8") as f:
                f.write(wrapper_html)
            if delete_candidate:
                try:
                    os.remove(delete_candidate)
                except Exception:
                    pass
        except Exception as e:
            print(f"章节包装失败: {course_dir}/{chapter_filename} -> {e}")
    raw_pattern = re.compile(r'^\d{2}\.raw\.html$', re.IGNORECASE)
    for name in os.listdir(course_dir):
        if raw_pattern.match(name):
            try:
                os.remove(os.path.join(course_dir, name))
            except Exception:
                pass


def write_course_index(course_dir, course_title, questions, footer_text):
    os.makedirs(course_dir, exist_ok=True)
    html = build_course_index_html(course_title, questions, footer_text)
    with open(os.path.join(course_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def write_root_index(courses, footer_text):
    html = build_root_index_html(courses, footer_text)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)


# ── API 调用 ──────────────────────────────────────────────

def _extract_html_content(text):
    if "```html" in text:
        start_idx = text.find("```html") + len("```html")
        end_idx = text.find("```", start_idx)
        return text[start_idx:end_idx].strip()
    if "```" in text:
        start_idx = text.find("```") + len("```")
        end_idx = text.find("```", start_idx)
        return text[start_idx:end_idx].strip()
    return text


def save_html_response(question, index, output_dir, retry_count=0):
    global base_url, api_key, g_enable_retry
    max_retries = 3
    try:
        append_to_file(question)
        thread_id = threading.current_thread().ident
        print(f"[{thread_id}] 正在处理......")
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        completion = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": question}],
            max_tokens=8192,
            temperature=0.1,
        )
        first_answer = completion.choices[0].message.content
        html_content = _extract_html_content(first_answer)
        if g_enable_retry and "</html>" not in html_content:
            if retry_count < max_retries:
                print(f"[{thread_id}] 检测到不完整的HTML，重试 {retry_count + 1}/{max_retries}...")
                return save_html_response(question, index, output_dir, retry_count + 1)
            else:
                print(f"[{thread_id}] 已达最大重试次数 {max_retries}，使用当前内容。")
        if index == 0:
            filename = "index.html"
        else:
            filename = f"{index:02d}.html"
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"[{thread_id}] 成功生成 {output_dir}/{filename}")
        return True
    except Exception as e:
        print(f"处理问题{index}时出错: {str(e)}")
        return False


def gen_tishici(lock, question, out_list):
    global base_url, api_key
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        completion = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": question}],
            max_tokens=8192,
            temperature=0.1,
        )
        content = completion.choices[0].message.content
        json_str1 = content.strip("```python").strip("```").strip()
        json_str = json_str1.strip("```json").strip("```").strip()
        data = json.loads(json_str)
        with lock:
            if isinstance(data, dict):
                out_list.append(data)
            elif isinstance(data, list) and len(data) > 0:
                out_list.append(data[0])
            else:
                print(f"无法处理的数据类型: {type(data)}，内容: {data}")
    except Exception as e:
        print(f"处理问题时出错: {str(e)}")


def generate_outlines(course_titles, lesson_count, footer_text, thread_num):
    lock = threading.Lock()
    list_html = []
    all_tasks = []
    for item in course_titles:
        question = build_outline_prompt(item, lesson_count, footer_text)
        all_tasks.append((lock, question, list_html))
    max_workers = min(max(1, thread_num), len(all_tasks), 200)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(lambda args: gen_tishici(*args), all_tasks)
    return list_html


def generate_lessons_from_outlines(dict_html_list_local, footer_text, thread_num):
    all_tasks = []
    for d in dict_html_list_local:
        home_dir = sanitize_filename(d['home'])
        for i, question in enumerate(d['question'], start=1):
            all_tasks.append((build_lesson_prompt(d['home'], question, footer_text), i, home_dir))
    if not all_tasks:
        return
    max_workers = min(max(1, thread_num), len(all_tasks), 200)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(lambda args: save_html_response(*args), all_tasks)


def build_navigation_and_index(dict_html_list_local, footer_text):
    courses = []
    for d in dict_html_list_local:
        home_dir = sanitize_filename(d['home'])
        write_course_index(home_dir, d['home'], d['question'], footer_text)
        wrap_course_chapters_with_sidebar(home_dir, d['home'], d['question'], footer_text)
        courses.append({"title": d["home"], "dir": home_dir, "count": len(d.get("question", []))})
    write_root_index(courses, footer_text)
    return courses


# ── 后处理：PNG + ZIP ─────────────────────────────────────

def _resolve_chromedriver_path(driver_path):
    if driver_path:
        dp = os.path.abspath(driver_path)
        if os.path.exists(dp):
            return dp
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        return ChromeDriverManager().install()
    except Exception:
        return None


def html_to_png(html_file, output_png, driver_path, delay=0.5):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    resolved_driver = _resolve_chromedriver_path(driver_path)
    if not resolved_driver:
        raise RuntimeError("未找到可用的 chromedriver，请在 config.json 设置 browser_path，或安装 webdriver_manager。")
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--hide-scrollbars")
    chrome_options.add_argument("--window-size=1920,1080")
    driver = webdriver.Chrome(service=Service(executable_path=resolved_driver), options=chrome_options)
    try:
        html_path = os.path.abspath(html_file)
        driver.get(f"file://{html_path}")
        time.sleep(delay)
        total_height = driver.execute_script(
            "return Math.max(document.body.scrollHeight, document.body.offsetHeight,"
            "document.documentElement.clientHeight, document.documentElement.scrollHeight,"
            "document.documentElement.offsetHeight);"
        )
        total_width = driver.execute_script(
            "return Math.max(document.body.scrollWidth, document.body.offsetWidth,"
            "document.documentElement.clientWidth, document.documentElement.scrollWidth,"
            "document.documentElement.offsetWidth);"
        )
        driver.set_window_size(total_width, total_height + 80)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(0.2)
        driver.save_screenshot(output_png)
    finally:
        driver.quit()


def get_html_files(directory=".", include_numbered=False):
    html_files = []
    directory = os.path.abspath(directory)
    skip_dirs = {'build', '_static', 'temp', '1111111'}
    for root_dir, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for file in files:
            low = file.lower()
            if not low.endswith('.html'):
                continue
            if include_numbered:
                if low == 'index.html' or (len(low) == 7 and low[0:2].isdigit()):
                    html_files.append(os.path.join(root_dir, file))
            else:
                if low in {'index.html', '01.html', '02.html', '03.html', '04.html'}:
                    html_files.append(os.path.join(root_dir, file))
    return html_files


def tran_all_html_to_png(max_workers, driver_path, include_numbered=False, directory="."):
    html_files = get_html_files(directory=directory, include_numbered=include_numbered)
    if not html_files:
        print("未找到可转换的 HTML 文件")
        return
    work_num = min(len(html_files), max_workers)
    print(f"发现 {len(html_files)} 个 HTML 文件，准备创建 {work_num} 个线程开始转 PNG...")
    def _one(f):
        png_file = os.path.splitext(f)[0] + ".png"
        if os.path.exists(png_file):
            return
        try:
            html_to_png(f, png_file, driver_path=driver_path)
            print(f"转换成功: {png_file}")
        except Exception as e:
            print(f"转换失败: {f} -> {e}")
    with ThreadPoolExecutor(max_workers=work_num) as executor:
        executor.map(_one, html_files)


def zip_folders_in_directory(base_dir):
    current_dir = os.path.abspath(base_dir)
    items = os.listdir(current_dir)
    folders = [item for item in items if os.path.isdir(os.path.join(current_dir, item))]
    if not folders:
        print("当前目录下没有找到任何文件夹。")
        return []
    zipped_files = []
    for folder_name in folders:
        try:
            zip_filename = f"{folder_name}.zip"
            folder_path = os.path.join(current_dir, folder_name)
            zip_path = os.path.join(current_dir, zip_filename)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root_dir, dirs, files in os.walk(folder_path):
                    for file in files:
                        low = file.lower()
                        if low.endswith(".png"):
                            continue
                        file_path = os.path.join(root_dir, file)
                        arcname = os.path.relpath(file_path, current_dir)
                        zipf.write(file_path, arcname)
            zipped_files.append(zip_path)
            print(f"成功压缩: {zip_filename}")
        except Exception as e:
            print(f"压缩失败: {folder_name}, 错误: {e}")
    return zipped_files


def run_post_pipeline(base_dir=None, do_png=True, do_zip=True, png_workers=10):
    global browser_path
    base_dir = os.path.abspath(base_dir or os.getcwd())
    if do_zip:
        zip_folders_in_directory(base_dir)
    if do_png:
        try:
            tran_all_html_to_png(
                max_workers=png_workers,
                driver_path=browser_path,
                include_numbered=False,
                directory=base_dir,
            )
        except Exception as e:
            print(f"转 PNG 失败: {e}")


# ── 高层管线 ──────────────────────────────────────────────

def run_full_pipeline_for_titles(course_titles, lessons_per_course, footer_text, thread_num,
                                 do_png=True, do_zip=True, png_workers=10,
                                 output_dir=None):
    """完整管线：课程标题 → 大纲 → HTML → 导航 → 后处理"""
    if output_dir is not None:
        target_dir = os.path.abspath(os.fspath(output_dir))
        os.makedirs(target_dir, exist_ok=True)
        with _cwd_lock:
            original_cwd = os.getcwd()
            os.chdir(target_dir)
            try:
                return run_full_pipeline_for_titles(
                    course_titles=course_titles,
                    lessons_per_course=lessons_per_course,
                    footer_text=footer_text,
                    thread_num=thread_num,
                    do_png=do_png,
                    do_zip=do_zip,
                    png_workers=png_workers,
                    output_dir=None,
                )
            finally:
                os.chdir(original_cwd)
    load_config()
    global dict_html_list
    append_to_file("\n\n\n\n")
    append_to_file("==========================================")
    print("********************(生成提示词)*******************")
    dict_html_list = generate_outlines(course_titles, lessons_per_course, footer_text, thread_num)
    with open("auto_html.json", "w", encoding="utf-8") as f:
        json.dump(dict_html_list, f, ensure_ascii=False, indent=4)
    print("********************(根据提示词生成网页)*******************")
    generate_lessons_from_outlines(dict_html_list, footer_text, thread_num)
    build_navigation_and_index(dict_html_list, footer_text)
    run_post_pipeline(os.getcwd(), do_png=do_png, do_zip=do_zip, png_workers=png_workers)
    print("全部课程生成完毕！")
    return dict_html_list


def run_fix_navigation(footer_text):
    """仅修复导航（不调用 API）"""
    load_config()
    global dict_html_list
    with open('auto_html.json', 'r', encoding='utf-8') as f:
        dict_html_list = json.load(f)
    courses = build_navigation_and_index(dict_html_list, footer_text)
    print("导航修复完成！")
    return courses


# ── 批量课程入口 ──────────────────────────────────────────

def process_course_file(filepath, lessons_per_course, footer_text, thread_num):
    """处理单个课程文件：读取 → 生成大纲 → 生成HTML → 导航包装。"""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    course_titles = [line.strip() for line in lines if line.strip()]
    if not course_titles:
        print(f"文件 {filepath} 中没有读取到任何课程标题，跳过。")
        return []
    base_name = os.path.splitext(os.path.basename(filepath))[0]
    target_dir = os.path.join(os.path.dirname(filepath), base_name)
    os.makedirs(target_dir, exist_ok=True)
    outlines = run_full_pipeline_for_titles(
        course_titles=course_titles,
        lessons_per_course=lessons_per_course,
        footer_text=footer_text,
        thread_num=thread_num,
        do_png=False,
        do_zip=False,
        output_dir=target_dir,
    )
    return outlines


def scan_course_files(input_dir="课程标题", extensions=(".txt",)):
    """扫描输入目录下的课程文件。"""
    if not os.path.isdir(input_dir):
        os.makedirs(input_dir, exist_ok=True)
        print(f"已创建目录: {input_dir}，请将课程标题文件放入该目录后重新运行。")
        return []
    files = []
    for name in os.listdir(input_dir):
        path = os.path.join(input_dir, name)
        if os.path.isfile(path) and name.lower().endswith(extensions):
            files.append(os.path.abspath(path))
    return sorted(files)


def check_and_clean_incomplete_courses(input_dir="课程标题", expected_lessons=30,
                                       category_paths=None):
    """检查课程完整性并保留失败产物，便于诊断和继续生成。

    函数名为兼容旧调用保留；它不再删除任何课程目录。
    """
    if not os.path.isdir(input_dir):
        return [], []
    print("\n==================== 课程完整性检查 ====================")
    failed = []
    passed = []
    if category_paths is None:
        paths = [os.path.join(input_dir, name) for name in sorted(os.listdir(input_dir))]
    else:
        paths = sorted({os.path.abspath(path) for path in category_paths})
    for category_path in paths:
        if not os.path.isdir(category_path):
            continue
        for course_name in sorted(os.listdir(category_path)):
            course_path = os.path.join(category_path, course_name)
            if not os.path.isdir(course_path):
                continue
            html_count = 0
            for fname in os.listdir(course_path):
                if re.match(r'^\d{2}\.html$', fname):
                    html_count += 1
            if html_count >= expected_lessons:
                passed.append((course_path, html_count))
                print(f"  [OK] {course_path} ({html_count}/{expected_lessons} 节)")
            else:
                print(f"  [FAIL] {course_path} ({html_count}/{expected_lessons} 节) -- 已保留")
                failed.append((course_path, html_count))
    print(f"\n检查完成: {len(passed)} 门课程通过, {len(failed)} 门未完成并已保留。")
    return passed, failed


def run_batch_pipeline(input_dir="课程标题", lessons_per_course=30, footer_text="资料云集",
                       thread_num=100, max_concurrent_files=10, do_png=True, do_zip=True,
                       png_workers=10, input_files=None):
    """批量处理：扫描课程标题/ .txt → 并发生成 → 完整性检查 → 后处理。"""
    load_config()
    input_dir = os.path.abspath(input_dir)
    if input_files is None:
        files = scan_course_files(input_dir)
    else:
        files = sorted({os.path.abspath(os.fspath(path)) for path in input_files})
        invalid = [path for path in files if not os.path.isfile(path) or not path.lower().endswith(".txt")]
        if invalid:
            raise ValueError(f"无效的课程标题文件: {invalid[0]}")
    if not files:
        print(f"在 {input_dir}/ 目录下未找到任何课程文件。")
        return
    print(f"发现 {len(files)} 个课程文件，每门课 {lessons_per_course} 节，最多同时处理 {max_concurrent_files} 个文件。")
    max_workers = min(len(files), max_concurrent_files)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_course_file, fp, lessons_per_course, footer_text, thread_num)
                   for fp in files]
        for future in futures:
            try:
                future.result()
            except Exception as e:
                print(f"处理文件失败: {e}")
    print("\n==================== 全部课程文件处理完毕，开始完整性检查 ====================")
    category_paths = [os.path.splitext(path)[0] for path in files]
    check_and_clean_incomplete_courses(
        input_dir, lessons_per_course, category_paths=category_paths
    )
    print("\n==================== 检查完成，开始后处理（每个类别目录下 ZIP + PNG） ====================")
    # 后处理在每个类别目录内执行，确保 ZIP 对象是生成的课程文件夹
    if do_zip or do_png:
        for category_path in sorted(set(category_paths)):
            if not os.path.isdir(category_path):
                continue
            print(f"\n--- 后处理: {category_path} ---")
            run_post_pipeline(
                category_path,
                do_png=do_png,
                do_zip=do_zip,
                png_workers=png_workers,
            )
    print("全部任务完成！")
