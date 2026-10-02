# -*- coding: utf-8 -*-
"""
课程生成器 GUI（独立程序 #2）
tkinter 图形界面：选择 课程标题/ 下的 .txt 文件 → 运行批量生成管线。
"""

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk, filedialog
import os
import json
import webbrowser
from threading import Thread
from queue import Queue, Empty

from course_gen_core import (
    load_config, save_config, get_config, CONFIG_PATH,
    scan_course_files, run_batch_pipeline, run_fix_navigation,
    base_url, api_key, browser_path,
)


class AsyncExecutor:
    def __init__(self, root_widget, progress_widget, output_widget):
        self.root_widget = root_widget
        self.progress_widget = progress_widget
        self.output_widget = output_widget
        self.output_queue = Queue()
        self.running = False

    def execute(self, files_to_process, lessons, threads, concurrent, footer,
                do_png, do_zip, png_workers, fix_only, button):
        if self.running:
            messagebox.showwarning("警告", "已有任务正在运行")
            return

        def run():
            self.running = True
            try:
                import sys as _sys
                from io import StringIO
                old_stdout = _sys.stdout
                _sys.stdout = StringIO()
                try:
                    if fix_only:
                        self.output_queue.put(("output", "执行导航修复...\n"))
                        run_fix_navigation(footer)
                    else:
                        self.output_queue.put(("output",
                            f"开始处理 {len(files_to_process)} 个课程文件，每门课 {lessons} 节\n\n"))
                        run_batch_pipeline(
                            input_dir="课程标题",
                            input_files=files_to_process,
                            lessons_per_course=lessons,
                            footer_text=footer,
                            thread_num=threads,
                            max_concurrent_files=concurrent,
                            do_png=do_png,
                            do_zip=do_zip,
                            png_workers=png_workers,
                        )
                    output = _sys.stdout.getvalue()
                    self.output_queue.put(("output", output))
                finally:
                    _sys.stdout = old_stdout
                self.output_queue.put(("complete", "任务完成"))
            except Exception as e:
                self.output_queue.put(("error", f"执行失败: {e}"))
            finally:
                self.running = False
                self.output_queue.put(("button", button))

        button.config(state='disabled')
        self.progress_widget.pack(fill=tk.X, pady=(10, 0))
        Thread(target=run, daemon=True).start()
        self.root_widget.after(100, self.check_queue)

    def check_queue(self):
        try:
            while True:
                msg_type, content = self.output_queue.get_nowait()
                if msg_type == "output":
                    self.output_widget.config(state=tk.NORMAL)
                    self.output_widget.insert(tk.END, content)
                    self.output_widget.see(tk.END)
                    self.output_widget.config(state=tk.DISABLED)
                elif msg_type == "error":
                    messagebox.showerror("错误", content)
                elif msg_type == "complete":
                    messagebox.showinfo("完成", content)
                elif msg_type == "button":
                    content.config(state='normal')
                    self.progress_widget.pack_forget()
        except Empty:
            pass
        if self.running:
            self.root_widget.after(100, self.check_queue)


# ── 设置对话框 ────────────────────────────────────────────

def set_api_key_dialog(parent):
    dialog = tk.Toplevel(parent)
    dialog.title("设置API Key")
    dialog.geometry("600x300")
    main_frame = ttk.Frame(dialog)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
    form_frame = ttk.Frame(main_frame)
    form_frame.pack(fill=tk.X, pady=10)
    ttk.Label(form_frame, text="API基础URL:").grid(row=0, column=0, sticky=tk.W, pady=5)
    url_entry = ttk.Entry(form_frame, width=50)
    url_entry.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=5)
    ttk.Label(form_frame, text="API Key:").grid(row=1, column=0, sticky=tk.W, pady=5)
    key_entry = ttk.Entry(form_frame, width=50)
    key_entry.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=5)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        url_entry.insert(0, data.get('base_url', ''))
        # Credentials are intentionally not read from config.json or displayed.
    except Exception:
        pass
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(fill=tk.X, pady=10)
    ttk.Button(btn_frame, text="获取API KEYs",
               command=lambda: webbrowser.open("https://platform.deepseek.com/api_keys")).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="保存",
               command=lambda: _save_api(dialog, url_entry.get(), key_entry.get())).pack(side=tk.RIGHT, padx=5)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.wait_window(dialog)


def _save_api(dialog, url, key):
    if not url or not key:
        messagebox.showwarning("警告", "URL和Key不能为空!")
        return
    env_path = os.path.join(os.path.dirname(CONFIG_PATH), '.env')
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        updates = {'COURSE_BASE_URL': url, 'DEEPSEEK_API_KEY': key}
        written = set()
        output = []
        for line in lines:
            if '=' not in line or line.lstrip().startswith('#'):
                output.append(line)
                continue
            name = line.split('=', 1)[0].strip()
            if name in updates:
                output.append(f'{name}={updates[name]}\n')
                written.add(name)
            else:
                output.append(line)
        for name, value in updates.items():
            if name not in written:
                output.append(f'{name}={value}\n')
        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(output)
        os.environ.update(updates)
    except OSError as exc:
        messagebox.showerror("保存失败", f"无法写入本地 .env：{exc}")
        return

    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    data['base_url'] = url
    data['api_key'] = ''
    save_config(data)
    load_config()
    messagebox.showinfo("成功", "API 配置已保存到本机 .env 文件")
    dialog.destroy()


def set_browser_dialog(parent):
    dialog = tk.Toplevel(parent)
    dialog.title("设置浏览器驱动路径")
    dialog.geometry("800x150")
    main_frame = ttk.Frame(dialog)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
    path_frame = ttk.Frame(main_frame)
    path_frame.pack(fill=tk.X, pady=10)
    entry = ttk.Entry(path_frame, width=70)
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
    ttk.Button(path_frame, text="浏览...",
               command=lambda: _browse_exe(entry)).pack(side=tk.RIGHT, padx=5)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            entry.insert(0, json.load(f).get('browser_path', browser_path))
    except Exception:
        entry.insert(0, browser_path)
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(fill=tk.X, pady=10)
    ttk.Button(btn_frame, text="保存",
               command=lambda: _save_browser(dialog, entry.get())).pack(side=tk.RIGHT, padx=5)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.wait_window(dialog)


def _browse_exe(entry_widget):
    path = filedialog.askopenfilename(title="选择 chromedriver.exe",
                                       filetypes=(("Executable files", "*.exe"), ("All files", "*.*")))
    if path:
        entry_widget.delete(0, tk.END)
        entry_widget.insert(0, path)


def _save_browser(dialog, path):
    if not path:
        messagebox.showwarning("警告", "路径不能为空!")
        return
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    data['browser_path'] = path
    save_config(data)
    load_config()
    messagebox.showinfo("成功", "浏览器路径已保存!")
    dialog.destroy()


# ── 主界面 ────────────────────────────────────────────────

def refresh_file_list():
    file_listbox.delete(0, tk.END)
    files = scan_course_files("课程标题")
    for fp in files:
        file_listbox.insert(tk.END, os.path.basename(fp))
    if not files:
        file_listbox.insert(tk.END, "（暂无 .txt 文件，请在 课程标题/ 目录下创建）")


def get_selected_files():
    files = scan_course_files("课程标题")
    selected = file_listbox.curselection()
    return [files[i] for i in selected if i < len(files)]


def build_gui():
    global root, output_text, progress_label, file_listbox

    load_config()
    root = tk.Tk()
    root.title("课程批量生成工具")
    root.geometry("900x750")

    menubar = tk.Menu(root)
    settings_menu = tk.Menu(menubar, tearoff=0)
    settings_menu.add_command(label="设置API Key", command=lambda: set_api_key_dialog(root))
    settings_menu.add_command(label="设置浏览器驱动", command=lambda: set_browser_dialog(root))
    menubar.add_cascade(label="设置", menu=settings_menu)
    root.config(menu=menubar)

    main_frame = ttk.Frame(root)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

    ttk.Label(main_frame, text="课程标题文件 (课程标题/ 目录下的 .txt 文件，可多选 Ctrl+点击):").pack(anchor=tk.W)

    list_frame = ttk.Frame(main_frame)
    list_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

    scrollbar = ttk.Scrollbar(list_frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    file_listbox = tk.Listbox(list_frame, selectmode=tk.MULTIPLE, yscrollcommand=scrollbar.set,
                               font=('Arial', 10), height=8)
    file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.config(command=file_listbox.yview)

    refresh_frame = ttk.Frame(main_frame)
    refresh_frame.pack(fill=tk.X, pady=(5, 0))
    ttk.Button(refresh_frame, text="刷新文件列表", command=refresh_file_list).pack(side=tk.LEFT)

    # 参数
    param_frame = ttk.Frame(main_frame)
    param_frame.pack(fill=tk.X, pady=(12, 0))

    cfg = get_config()
    ttk.Label(param_frame, text="节数:").pack(side=tk.LEFT, padx=(0, 5))
    lessons_entry = ttk.Entry(param_frame, width=6)
    lessons_entry.pack(side=tk.LEFT)
    lessons_entry.insert(0, str(cfg.get("lessons_per_course", 30)))

    ttk.Label(param_frame, text="线程:").pack(side=tk.LEFT, padx=(10, 5))
    threads_entry = ttk.Entry(param_frame, width=6)
    threads_entry.pack(side=tk.LEFT)
    threads_entry.insert(0, "100")

    ttk.Label(param_frame, text="并发文件:").pack(side=tk.LEFT, padx=(10, 5))
    concurrent_entry = ttk.Entry(param_frame, width=6)
    concurrent_entry.pack(side=tk.LEFT)
    concurrent_entry.insert(0, "10")

    ttk.Label(param_frame, text="PNG线程:").pack(side=tk.LEFT, padx=(10, 5))
    png_entry = ttk.Entry(param_frame, width=6)
    png_entry.pack(side=tk.LEFT)
    png_entry.insert(0, "10")

    ttk.Label(param_frame, text="页脚:").pack(side=tk.LEFT, padx=(10, 5))
    footer_entry = ttk.Entry(param_frame, width=15)
    footer_entry.pack(side=tk.LEFT)
    footer_entry.insert(0, "资料云集")

    # 复选框
    check_frame = ttk.Frame(main_frame)
    check_frame.pack(fill=tk.X, pady=(10, 0))
    do_png_var = tk.BooleanVar(value=True)
    do_zip_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(check_frame, text="生成后转PNG", variable=do_png_var).pack(side=tk.LEFT)
    ttk.Checkbutton(check_frame, text="生成后压缩ZIP", variable=do_zip_var).pack(side=tk.LEFT, padx=(10, 0))

    executor = None  # 延后到 widget 创建完成后初始化

    def on_full_pipeline():
        files = get_selected_files()
        if not files:
            messagebox.showwarning("警告", "请先在列表中选中至少一个课程文件！")
            return
        executor.execute(files, int(lessons_entry.get()), int(threads_entry.get()),
                         int(concurrent_entry.get()), footer_entry.get(),
                         do_png_var.get(), do_zip_var.get(), int(png_entry.get()),
                         False, btn_full)

    def on_fix_nav():
        executor.execute([], int(lessons_entry.get()), int(threads_entry.get()),
                         int(concurrent_entry.get()), footer_entry.get(),
                         False, False, int(png_entry.get()), True, btn_fix)

    btn_frame = tk.Frame(main_frame)
    btn_frame.pack(pady=15)

    btn_full = ttk.Button(btn_frame, text="一键全流程", command=on_full_pipeline)
    btn_full.pack(side=tk.LEFT, padx=5)

    btn_fix = ttk.Button(btn_frame, text="仅修复导航", command=on_fix_nav)
    btn_fix.pack(side=tk.LEFT, padx=5)

    # 进度
    progress_label = ttk.Label(main_frame, text="正在执行任务，请耐心等待...")

    # 输出
    output_frame = ttk.Frame(main_frame)
    output_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
    ttk.Label(output_frame, text="执行输出:").pack(anchor=tk.W)

    output_text = scrolledtext.ScrolledText(output_frame, wrap=tk.WORD, width=60, height=0.1,
                                             font=('Arial', 10), padx=10, pady=10, state=tk.DISABLED)
    output_text.pack(fill=tk.BOTH, expand=True)

    executor = AsyncExecutor(root, progress_label, output_text)

    progress_label.pack_forget()
    refresh_file_list()
    root.mainloop()


root = None
output_text = None
progress_label = None
file_listbox = None

if __name__ == "__main__":
    build_gui()
