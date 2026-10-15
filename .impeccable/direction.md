# Redesign direction · 2026-10-14

Code-led implementation. The Impeccable launcher and concept-seed were attempted but unavailable because the local engine is missing; this limitation was disclosed. No approved image comp exists.

## THESIS

清晰、安静、有书籍气质的学习网站，搭配适合日常制作与交付的运营工作区。

## OWN-WORLD

纸白 #f7f8f4、墨绿 #234b3e、墨色文字 #202c27。中文标题使用本地 Noto Serif SC 字形子集与衬线回退；英文、数字与界面文字使用本地 Manrope 与中文无衬线回退。不是照片或实体材质模拟；书册为明确的网页图形表达。

## STORY

首页：了解学习方式 → 探索真实课程 → 课程详情与试学 → 已购兑换。后台：分区导航 → 待办与近期审计 → 制作项目 → 清晰的表单和并排正文编辑预览。

## FIRST VIEWPORT

桌面首页为左侧中文衬线标题、明确行动入口，右侧以真实课程名称呈现书册图形。手机顺序为文案、行动入口、课程书册。后台桌面固定分区导航，内容拥有独立空间；手机导航可折叠，正文与预览纵向排列。

## FORM

concept-seed 无法运行；没有执行随机方向分配，也没有 seed key。本实现采用已说明的直接代码设计回退。

语义化 HTML、实际链接、服务器表单、可见 focus、减少动态偏好、本地字体。单一轻量书册进入动画；其余动效只回应交互。保持现有课程事实、访问控制和制作流程。

## Verification boundaries

Preview and tests use isolated local data. The host Python 3.12.14 exposes inconsistent lstat/fstat ctime on Windows; only the unshipped preview/test harness normalizes signatures to st_birthtime_ns while retaining identity, size, mtime and byte revalidation. Production inspection code is unchanged.
