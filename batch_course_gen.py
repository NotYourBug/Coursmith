# -*- coding: utf-8 -*-
"""
批量课程生成脚本（独立程序 #1）
用法：python batch_course_gen.py
功能：扫描 课程标题/ 目录下的 .txt 文件，每份文件生成一套课程网站，最后 ZIP + PNG。
"""

import sys
import os
from course_gen_core import (
    load_config, get_config, run_batch_pipeline, run_fix_navigation,
)


def print_usage():
    print("""
批量课程生成脚本
================
用法: python batch_course_gen.py [选项]

选项:
  --lessons N        每门课程节数 (默认: 30)
  --threads N        API 调用线程数 (默认: 100)
  --concurrent N     最多同时处理的文件数 (默认: 10)
  --footer TEXT      页脚文本 (默认: "资料云集")
  --no-png           跳过 PNG 转换
  --no-zip           跳过 ZIP 压缩
  --png-workers N    PNG 转换线程数 (默认: 10)
  --fix-navigation   仅修复导航（不调 API）

工作目录结构:
  课程标题/
    ├── Python基础.txt    (文件名=课程类别, 内容=每行一个课程标题)
    ├── 数据分析.txt
    └── ...
""")


def main():
    if '--help' in sys.argv or '-h' in sys.argv:
        print_usage()
        return

    load_config()
    cfg = get_config()

    lessons = cfg.get("lessons_per_course", 30)
    threads = 100
    concurrent = 10
    footer = "资料云集"
    do_png = True
    do_zip = True
    png_workers = 10

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--lessons':
            i += 1; lessons = int(args[i])
        elif arg == '--threads':
            i += 1; threads = int(args[i])
        elif arg == '--concurrent':
            i += 1; concurrent = int(args[i])
        elif arg == '--footer':
            i += 1; footer = args[i]
        elif arg == '--no-png':
            do_png = False
        elif arg == '--no-zip':
            do_zip = False
        elif arg == '--png-workers':
            i += 1; png_workers = int(args[i])
        elif arg == '--fix-navigation':
            print("执行导航修复模式...")
            run_fix_navigation(footer)
            return
        i += 1

    print(f"配置: 每门课 {lessons} 节 | API线程 {threads} | 最大并发文件 {concurrent}")
    print(f"后处理: PNG={'是' if do_png else '否'} | ZIP={'是' if do_zip else '否'}")
    print()

    run_batch_pipeline(
        input_dir="课程标题",
        lessons_per_course=lessons,
        footer_text=footer,
        thread_num=threads,
        max_concurrent_files=concurrent,
        do_png=do_png,
        do_zip=do_zip,
        png_workers=png_workers,
    )


if __name__ == "__main__":
    main()
