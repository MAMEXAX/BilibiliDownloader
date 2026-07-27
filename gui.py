"""
B站视频下载工具 - 图形界面模块
基于 tkinter 构建的简洁 GUI，负责用户交互。
"""

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Any

import requests
from PIL import Image, ImageTk

from downloader import parse_video_info, DownloadTask, ParsedVideoData
from ffmpeg_utils import check_ffmpeg, get_ffmpeg_path, get_ffmpeg_error_message
from history_manager import DownloadHistory
from login_manager import LoginManager
from path_utils import get_data_path


class MainWindow:
    """主窗口，管理所有 UI 组件和用户交互。"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("BiliDownloader - B站视频下载工具")
        self.root.geometry("780x620")
        self.root.resizable(True, True)
        self.root.minsize(700, 550)

        # ── 数据状态 ──
        self._parsed_data: ParsedVideoData | None = None
        self._download_task: DownloadTask | None = None
        self._last_save_path: str = ""
        self._history = DownloadHistory()
        self._quality_var = tk.IntVar(value=0)
        self._audio_only_var = tk.BooleanVar(value=False)
        self._output_dir = os.path.join(get_data_path(""), "downloads")

        # FFmpeg 状态
        self._ffmpeg_available = check_ffmpeg()

        # ── 登录管理 ──
        self._login_manager = LoginManager()

        # ── 构建界面 ──
        self._build_menu()
        self._build_ui()
        self._update_ffmpeg_hint()
        self._update_login_status()

        # 启动后自动尝试加载 Cookie
        self.root.after(200, self._auto_load_cookies)

        # 首次运行标记：自动弹出使用说明
        self.root.after(400, self._check_first_run)

        # ── 绑定事件 ──
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ══════════════════════════════════════════════════════════════════════
    #  界面构建
    # ══════════════════════════════════════════════════════════════════════

    def _build_menu(self):
        """构建菜单栏。"""
        menubar = tk.Menu(self.root, font=("Microsoft YaHei", 9))
        self.root.config(menu=menubar)

        # 文件菜单
        file_menu = tk.Menu(menubar, tearoff=0, font=("Microsoft YaHei", 9))
        menubar.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="导入批量下载列表", command=self._import_batch_file)
        file_menu.add_command(label="设置下载目录", command=self._set_output_dir)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)

        # 账号菜单
        account_menu = tk.Menu(menubar, tearoff=0, font=("Microsoft YaHei", 9))
        menubar.add_cascade(label="账号", menu=account_menu)
        account_menu.add_command(label="登录B站", command=self._start_login)
        account_menu.add_command(label="退出登录", command=self._do_logout)

        # 历史菜单
        hist_menu = tk.Menu(menubar, tearoff=0, font=("Microsoft YaHei", 9))
        menubar.add_cascade(label="历史", menu=hist_menu)
        hist_menu.add_command(label="查看下载历史", command=self._show_history)
        hist_menu.add_command(label="清空历史记录", command=self._clear_history)

        # 帮助菜单
        help_menu = tk.Menu(menubar, tearoff=0, font=("Microsoft YaHei", 9))
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="使用说明", command=self._show_help)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self._show_about)

    def _build_ui(self):
        """构建主界面布局。"""
        root = self.root
        pad = {"padx": 10, "pady": 5}

        # ── 1. 顶部：URL 输入区 ──
        top_frame = ttk.LabelFrame(root, text="视频链接", padding=8)
        top_frame.pack(fill=tk.X, **pad)

        # URL 和按钮行
        url_row = ttk.Frame(top_frame)
        url_row.pack(fill=tk.X)

        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(url_row, textvariable=self.url_var, font=("Consolas", 11))
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        url_entry.bind("<Return>", lambda e: self._on_parse())
        url_entry.focus_set()

        ttk.Button(url_row, text="解析", command=self._on_parse, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Button(url_row, text="批量导入", command=self._import_batch_file, width=8).pack(side=tk.LEFT, padx=2)

        # 登录按钮和状态行
        login_row = ttk.Frame(top_frame)
        login_row.pack(fill=tk.X, pady=(5, 0))

        self._login_btn = ttk.Button(
            login_row, text="登录B站", command=self._start_login, width=10
        )
        self._login_btn.pack(side=tk.LEFT, padx=(0, 5))

        self._login_status_var = tk.StringVar(value="未登录")
        ttk.Label(login_row, textvariable=self._login_status_var,
                  font=("Microsoft YaHei", 9), foreground="gray").pack(side=tk.LEFT)

        # ── 2. 中部：视频信息 ──
        info_frame = ttk.LabelFrame(root, text="视频信息", padding=8)
        info_frame.pack(fill=tk.X, **pad)

        # 封面图片（左）
        self._cover_label = ttk.Label(info_frame)
        self._cover_label.grid(row=0, column=0, rowspan=4, padx=(0, 10), sticky=tk.NW)

        # 信息文本（右）
        self._title_var = tk.StringVar(value="标题：--")
        self._uploader_var = tk.StringVar(value="UP主：--")
        self._duration_var = tk.StringVar(value="时长：--")
        self._desc_var = tk.StringVar(value="")

        ttk.Label(info_frame, textvariable=self._title_var, font=("Microsoft YaHei", 10, "bold"),
                  wraplength=500).grid(row=0, column=1, sticky=tk.W, pady=1)
        ttk.Label(info_frame, textvariable=self._uploader_var, font=("Microsoft YaHei", 9)).grid(
            row=1, column=1, sticky=tk.W, pady=1)
        ttk.Label(info_frame, textvariable=self._duration_var, font=("Microsoft YaHei", 9)).grid(
            row=2, column=1, sticky=tk.W, pady=1)
        ttk.Label(info_frame, textvariable=self._desc_var, font=("Microsoft YaHei", 8),
                  wraplength=500, foreground="gray").grid(row=3, column=1, sticky=tk.W, pady=1)

        info_frame.columnconfigure(1, weight=1)

        # ── 3. 中部：下载设置 ──
        download_frame = ttk.LabelFrame(root, text="下载设置", padding=8)
        download_frame.pack(fill=tk.X, **pad)

        # 清晰度
        ttk.Label(download_frame, text="清晰度：").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self._quality_combo = ttk.Combobox(
            download_frame, state="readonly", font=("Microsoft YaHei", 9), width=22
        )
        self._quality_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        self._quality_combo.bind("<<ComboboxSelected>>", self._on_quality_selected)

        # 仅音频
        self._audio_cb = ttk.Checkbutton(
            download_frame, text="仅下载音频（MP3）", variable=self._audio_only_var,
            command=self._on_audio_only_toggle
        )
        self._audio_cb.grid(row=0, column=2, sticky=tk.W, padx=10)

        # 下载按钮行
        btn_frame = ttk.Frame(download_frame)
        btn_frame.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

        self._download_btn = ttk.Button(btn_frame, text="开始下载", command=self._on_download, width=12)
        self._download_btn.pack(side=tk.LEFT, padx=(0, 5))

        self._pause_resume_btn = ttk.Button(
            btn_frame, text="暂停", command=self._on_pause_resume, width=8, state=tk.DISABLED
        )
        self._pause_resume_btn.pack(side=tk.LEFT, padx=5)

        self._cancel_btn = ttk.Button(btn_frame, text="取消", command=self._on_cancel, width=8, state=tk.DISABLED)
        self._cancel_btn.pack(side=tk.LEFT, padx=5)

        self._open_folder_btn = ttk.Button(
            btn_frame, text="打开文件夹", command=self._open_folder, width=10
        )
        self._open_folder_btn.pack(side=tk.LEFT, padx=5)

        # ── 4. 底部：进度区 ──
        progress_frame = ttk.LabelFrame(root, text="下载进度", padding=8)
        progress_frame.pack(fill=tk.X, **pad)

        self._progress_bar = ttk.Progressbar(
            progress_frame, mode="determinate", length=100
        )
        self._progress_bar.pack(fill=tk.X, pady=(0, 5))

        self._progress_percent_var = tk.StringVar(value="0%")
        ttk.Label(progress_frame, textvariable=self._progress_percent_var,
                  font=("Consolas", 10)).pack(anchor=tk.W)

        self._speed_var = tk.StringVar(value="速度：--")
        self._remaining_var = tk.StringVar(value="剩余时间：--")
        speed_frame = ttk.Frame(progress_frame)
        speed_frame.pack(fill=tk.X)
        ttk.Label(speed_frame, textvariable=self._speed_var, font=("Microsoft YaHei", 9)).pack(
            side=tk.LEFT, padx=(0, 20))
        ttk.Label(speed_frame, textvariable=self._remaining_var, font=("Microsoft YaHei", 9)).pack(
            side=tk.LEFT)

        self._status_var = tk.StringVar(value="就绪 - 请输入视频链接")
        ttk.Label(progress_frame, textvariable=self._status_var,
                  font=("Microsoft YaHei", 9), foreground="gray").pack(anchor=tk.W, pady=(5, 0))

    # ══════════════════════════════════════════════════════════════════════
    #  事件处理
    # ══════════════════════════════════════════════════════════════════════

    def _on_parse(self):
        """点击解析按钮。"""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入B站视频链接或BV/AV号。")
            return

        self._set_ui_state(parsing=True)
        self._status_var.set("正在解析视频信息...")

        # 捕获当前登录凭证（线程安全）
        credential = self._login_manager.get_credential()

        # 在后台线程解析
        def do_parse():
            try:
                data = parse_video_info(url, credential=credential)
                self.root.after(0, lambda d=data: self._on_parse_done(d))
            except Exception as e:
                self.root.after(0, lambda e=e: self._on_parse_error(str(e)))

        threading.Thread(target=do_parse, daemon=True).start()

    def _on_parse_done(self, data: ParsedVideoData):
        """解析完成，更新UI。"""
        self._parsed_data = data
        info = data.info

        self._title_var.set(f"标题：{info.title}")
        self._uploader_var.set(f"UP主：{info.uploader}")
        self._duration_var.set(f"时长：{info.duration}")

        # 描述截取前100字
        desc = info.description.strip().replace("\n", " ")
        if len(desc) > 100:
            desc = desc[:100] + "..."
        self._desc_var.set(desc if desc else "")

        # 加载封面
        self._load_cover(info.cover_url)

        # 填充清晰度选项
        qualities: list[str] = []
        quality_ids: list[int] = []
        for vs in data.video_streams:
            qualities.append(vs.quality_desc)
            quality_ids.append(vs.quality_id)

        self._quality_combo["values"] = qualities
        self._quality_ids = quality_ids

        if qualities:
            self._quality_combo.current(len(qualities) - 1)  # 默认选最高
            self._quality_var.set(quality_ids[-1])

        # 启用/禁用清晰度选择（音频模式不需要）
        self._quality_combo.config(state="readonly" if not self._audio_only_var.get() else tk.DISABLED)

        self._status_var.set(f"解析完成 - {info.title}")
        self._set_ui_state(parsing=False, ready=True)

    def _on_parse_error(self, error_msg: str):
        """解析出错。"""
        messagebox.showerror("解析失败", error_msg)
        self._status_var.set("解析失败，请检查链接是否正确")
        self._set_ui_state(parsing=False)

    def _on_quality_selected(self, event=None):
        """清晰度选择变更。"""
        idx = self._quality_combo.current()
        if idx >= 0 and hasattr(self, "_quality_ids") and idx < len(self._quality_ids):
            self._quality_var.set(self._quality_ids[idx])

    def _on_audio_only_toggle(self):
        """音频模式切换。"""
        if self._audio_only_var.get():
            self._quality_combo.config(state=tk.DISABLED)
            self._status_var.set("仅音频模式 - 将下载为 MP3")
        else:
            self._quality_combo.config(state="readonly")
            self._status_var.set("就绪")

    def _on_download(self):
        """点击下载按钮。"""
        if not self._parsed_data:
            messagebox.showwarning("提示", "请先解析视频信息。")
            return

        quality_id = self._quality_var.get()
        audio_only = self._audio_only_var.get()

        if not audio_only and quality_id == 0:
            messagebox.showwarning("提示", "请选择清晰度。")
            return

        self._set_ui_state(downloading=True)

        self._download_task = DownloadTask(
            parsed_data=self._parsed_data,
            selected_quality=quality_id,
            output_dir=self._output_dir,
            audio_only=audio_only,
        )

        self._download_task.set_status_callback(
            lambda msg: self.root.after(0, self._on_status_update, msg)
        )
        self._download_task.set_progress_callback(
            lambda p, s, r: self.root.after(0, self._on_progress_update, p, s, r)
        )
        self._download_task.set_done_callback(
            lambda ok, msg, fp: self.root.after(0, self._on_download_done, ok, msg, fp)
        )

        self._download_task.start()

    def _on_cancel(self):
        """取消下载。批量模式下取消全部，单任务模式下只取消当前任务。"""
        if getattr(self, "_batch_mode", False):
            self._batch_cancelled = True
        if self._download_task:
            self._download_task.cancel()
            self._status_var.set("正在取消...")

    def _on_pause_resume(self):
        """暂停/恢复切换。根据当前状态切换任务暂停或恢复。"""
        if not self._download_task:
            return

        if self._download_task.is_paused():
            # 当前暂停 → 恢复
            self._download_task.resume()
            self._pause_resume_btn.config(text="暂停")
            self._status_var.set("正在恢复下载...")
        else:
            # 当前下载中 → 暂停
            self._download_task.pause()
            self._pause_resume_btn.config(text="恢复")
            self._status_var.set("正在暂停...")

    def _on_status_update(self, msg: str):
        """更新状态文本。"""
        self._status_var.set(msg)

    def _on_progress_update(self, percent: float, speed: str, remaining: str):
        """更新进度条和速度信息。"""
        self._progress_bar["value"] = percent
        self._progress_percent_var.set(f"{percent:.1f}%")
        self._speed_var.set(f"速度：{speed}" if speed else "速度：--")
        self._remaining_var.set(f"剩余时间：{remaining}" if remaining else "剩余时间：--")

    def _on_download_done(self, success: bool, msg: str, file_path: str):
        """下载完成。"""
        self._download_task = None
        saved_path = ""

        if success:
            self._progress_bar["value"] = 100
            self._progress_percent_var.set("100%")
            self._last_save_path = file_path
            saved_path = file_path

            # 记录历史
            if self._parsed_data:
                quality_desc = "音频" if self._audio_only_var.get() else (
                    self._quality_combo.get() if self._quality_combo.get() else "未知"
                )
                self._history.add_record(
                    url=self.url_var.get().strip(),
                    title=self._parsed_data.info.title,
                    quality=quality_desc,
                    file_path=file_path,
                )
        else:
            self._progress_bar["value"] = 0
            self._progress_percent_var.set("0%")

        # ── 批量模式：继续处理下一个链接 ──
        if getattr(self, "_batch_mode", False) and not getattr(self, "_batch_cancelled", False):
            self._current_batch_idx += 1
            if success:
                self._status_var.set(
                    f"批量下载 ({self._current_batch_idx}/{len(self._links_queue)}): "
                    f"{self._parsed_data.info.title if self._parsed_data else ''} 完成"
                )
            else:
                self._status_var.set(
                    f"批量下载 ({self._current_batch_idx}/{len(self._links_queue)}): "
                    f"跳过失败链接 ({msg[:50]})"
                )
            self.root.after(300, self._process_next_batch_link)
            return

        # ── 单任务模式：弹窗提示 ──
        if success:
            messagebox.showinfo("下载完成", msg)
            self._status_var.set("下载完成！")
        else:
            self._status_var.set(f"下载失败: {msg}")
            if "已取消" not in msg:
                messagebox.showerror("下载失败", msg)

        self._set_ui_state(downloading=False, ready=True)

    def _load_cover(self, url: str):
        """异步加载封面图片。"""
        if not url:
            return

        def fetch():
            try:
                resp = requests.get(url, timeout=15,
                                    headers={"Referer": "https://www.bilibili.com"})
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content))
                img = img.resize((150, 94), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.root.after(0, lambda: self._set_cover_image(photo))
            except Exception:
                # 封面加载失败不影响主流程
                pass

        threading.Thread(target=fetch, daemon=True).start()

    def _set_cover_image(self, photo):
        """设置封面图片。"""
        self._cover_label.configure(image=photo)
        self._cover_label.image = photo  # 保持引用

    # ══════════════════════════════════════════════════════════════════════
    #  菜单功能
    # ══════════════════════════════════════════════════════════════════════

    def _open_folder(self):
        """打开下载文件夹。"""
        target = self._last_save_path if self._last_save_path else os.path.abspath(self._output_dir)
        if os.path.isfile(target):
            target = os.path.dirname(target)
        if os.path.exists(target):
            subprocess.Popen(["explorer", os.path.abspath(target)])
        else:
            messagebox.showinfo("提示", "下载目录尚不存在，请先下载视频。")

    def _set_output_dir(self):
        """设置下载目录。"""
        d = filedialog.askdirectory(title="选择下载目录", initialdir=self._output_dir)
        if d:
            self._output_dir = d
            self._status_var.set(f"下载目录已设置为: {d}")

    def _import_batch_file(self):
        """从 txt 文件导入批量链接。"""
        file_path = filedialog.askopenfilename(
            title="选择批量下载列表",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                links = [line.strip() for line in f if line.strip()]
        except Exception as e:
            messagebox.showerror("错误", f"读取文件失败: {e}")
            return

        if not links:
            messagebox.showinfo("提示", "文件中没有找到有效的链接。")
            return

        self._links_queue = links
        self._current_batch_idx = 0
        self._batch_mode = True
        self._batch_cancelled = False
        self._status_var.set(f"批量模式 - 共 {len(links)} 个链接")
        self._process_next_batch_link()

    def _process_next_batch_link(self):
        """处理批量下载中的下一个链接。"""
        if self._current_batch_idx >= len(self._links_queue):
            self._batch_mode = False
            self._batch_cancelled = False
            self._status_var.set("批量下载全部完成！")
            self._set_ui_state(downloading=False, ready=True)
            return

        link = self._links_queue[self._current_batch_idx]
        self.url_var.set(link)
        self._status_var.set(f"批量下载 ({self._current_batch_idx + 1}/{len(self._links_queue)}): 正在解析...")

        # 捕获当前登录凭证
        credential = self._login_manager.get_credential()

        def do_parse():
            try:
                data = parse_video_info(link, credential=credential)
                self.root.after(0, lambda d=data: self._batch_parse_done(d))
            except Exception as e:
                self.root.after(0, lambda e=e: self._batch_parse_error(str(e)))

        threading.Thread(target=do_parse, daemon=True).start()

    def _batch_parse_done(self, data: ParsedVideoData):
        """批量模式解析完成，自动开始下载。"""
        self._on_parse_done(data)
        self._on_download()

    def _batch_parse_error(self, error_msg: str):
        """批量模式解析失败，跳过当前链接，继续下一个。"""
        self._current_batch_idx += 1
        self._status_var.set(
            f"批量下载 ({self._current_batch_idx}/{len(self._links_queue)}): "
            f"跳过失败链接 - {error_msg[:60]}"
        )
        self.root.after(300, self._process_next_batch_link)

    def _show_history(self):
        """显示下载历史窗口。"""
        records = self._history.get_all_records()
        if not records:
            messagebox.showinfo("下载历史", "暂无下载记录。")
            return

        win = tk.Toplevel(self.root)
        win.title("下载历史")
        win.geometry("680x400")
        win.transient(self.root)

        # 列表
        columns = ("time", "title", "quality", "file_path")
        tree = ttk.Treeview(win, columns=columns, show="headings", height=15)
        tree.heading("time", text="时间")
        tree.heading("title", text="标题")
        tree.heading("quality", text="清晰度")
        tree.heading("file_path", text="保存路径")

        tree.column("time", width=140)
        tree.column("title", width=260)
        tree.column("quality", width=100)
        tree.column("file_path", width=180)

        for r in records:
            tree.insert("", tk.END, values=(r["time"], r["title"], r["quality"], r["file_path"]))

        scrollbar = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=10)

        # 双击打开文件
        tree.bind("<Double-1>", lambda e: self._open_history_file(tree))

    def _open_history_file(self, tree: ttk.Treeview):
        """双击历史记录打开文件。"""
        sel = tree.selection()
        if sel:
            values = tree.item(sel[0], "values")
            file_path = values[3] if len(values) > 3 else ""
            if file_path and os.path.exists(file_path):
                subprocess.Popen(["explorer", "/select,", os.path.abspath(file_path)])
            elif os.path.exists(os.path.dirname(file_path)):
                subprocess.Popen(["explorer", os.path.abspath(os.path.dirname(file_path))])

    def _clear_history(self):
        """清空下载历史。"""
        if messagebox.askyesno("确认", "确定要清空所有下载历史记录吗？"):
            self._history.clear_all()
            self._status_var.set("历史记录已清空")

    def _show_about(self):
        """显示关于对话框。"""
        messagebox.showinfo(
            "关于 - B站视频下载器",
            "软件名称：B站视频下载器\n"
            "版本号：v1.0\n"
            "作者：明儿笑\n\n"
            "免责声明：\n"
            "本软件仅供个人学习、研究使用，严禁用于商业用途。\n"
            "请尊重版权，下载视频后仅限个人观看，\n"
            "不得传播或用于其他侵权用途。\n"
            "使用本软件所产生的任何法律责任由使用者自行承担。",
        )

    def _show_help(self):
        """弹出使用说明窗口（可滚动）。"""

        help_text = (
            "【B站视频下载器 使用说明】\n\n"

            "首次使用\n"
            "请确保 ffmpeg.exe 与本程序放在同一个文件夹中。\n"
            "（没有该文件无法合并高清视频，请向作者索要或自行下载）\n\n"

            "登录账号\n"
            "点击「登录B站」按钮，使用手机 B站 App 扫描二维码。\n"
            "（登录后可获取 720P 及以上高清画质，大会员账号可下载更高画质）\n\n"

            "下载视频\n"
            "1. 在输入框中粘贴 B站视频链接（如 https://www.bilibili.com/video/BV1xx...）\n"
            "2. 点击「解析」按钮\n"
            "3. 在下拉菜单中选择清晰度\n"
            "4. 点击「下载」按钮开始下载\n\n"

            "功能说明\n"
            "暂停/恢复：下载过程中可随时暂停或恢复任务\n"
            "取消下载：取消当前任务并清理临时文件\n"
            "批量导入：支持导入包含多个链接的 txt 文件（每行一个链接）\n\n"

            "文件保存位置\n"
            "下载的视频默认保存在程序所在目录下的 downloads 文件夹中。\n\n"

            "常见问题\n"
            "Q: 为什么只能看到 360P/480P？\n"
            "A: 请先点击「登录B站」扫码登录，登录后可看到更高清晰度选项。\n\n"
            "Q: 下载的视频没有声音？\n"
            "A: 高清视频音视频是分离的，请确保 ffmpeg.exe 放在正确位置。\n\n"
            "Q: 软件无法运行或报错？\n"
            "A: 请确保关闭杀毒软件或将本程序加入白名单（部分杀软会误报）。"
        )

        dialog = tk.Toplevel(self.root)
        dialog.title("使用说明 - B站视频下载器")
        dialog.geometry("580x520")
        dialog.resizable(True, True)
        dialog.minsize(450, 400)
        dialog.transient(self.root)

        # 滚动文本框
        text_frame = ttk.Frame(dialog)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        text_widget = tk.Text(
            text_frame,
            wrap=tk.WORD,
            font=("Microsoft YaHei", 10),
            yscrollcommand=scrollbar.set,
            padx=10,
            pady=10,
        )
        text_widget.pack(fill=tk.BOTH, expand=True)
        text_widget.insert("1.0", help_text)
        text_widget.config(state=tk.DISABLED)
        scrollbar.config(command=text_widget.yview)

        # 关闭按钮
        ttk.Button(dialog, text="关闭", command=dialog.destroy, width=10).pack(pady=(0, 10))

    def _check_first_run(self):
        """检测是否首次运行，首次运行则自动弹出使用说明。"""
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            return  # 非首次运行

        # 首次运行，弹出使用说明
        self._show_help()

        # 写入标记
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"first_run": False, "version": "1.0"}, f, ensure_ascii=False)
        except IOError:
            pass

    # ══════════════════════════════════════════════════════════════════════
    #  辅助方法
    # ══════════════════════════════════════════════════════════════════════

    def _set_ui_state(self, parsing=False, downloading=False, ready=False):
        """统一管理 UI 控件状态。"""
        if downloading:
            self._download_btn.config(state=tk.DISABLED)
            self._pause_resume_btn.config(state=tk.NORMAL, text="暂停")
            self._cancel_btn.config(state=tk.NORMAL)
            self._quality_combo.config(state=tk.DISABLED)
            self._audio_cb.config(state=tk.DISABLED)
            self._progress_bar["value"] = 0
            self._progress_percent_var.set("0%")
            self._speed_var.set("速度：--")
            self._remaining_var.set("剩余时间：--")
        elif parsing:
            self._download_btn.config(state=tk.DISABLED)
            self._pause_resume_btn.config(state=tk.DISABLED)
            self._cancel_btn.config(state=tk.DISABLED)
        elif ready:
            self._download_btn.config(state=tk.NORMAL)
            self._pause_resume_btn.config(state=tk.DISABLED, text="暂停")
            self._cancel_btn.config(state=tk.DISABLED)
            if self._parsed_data:
                self._quality_combo.config(
                    state="readonly" if not self._audio_only_var.get() else tk.DISABLED
                )
            self._audio_cb.config(state=tk.NORMAL)

    def _update_ffmpeg_hint(self):
        """更新 FFmpeg 安装提示。"""
        if not self._ffmpeg_available:
            self._status_var.set(get_ffmpeg_error_message())

    # ══════════════════════════════════════════════════════════════════════
    #  登录管理
    # ══════════════════════════════════════════════════════════════════════

    def _auto_load_cookies(self):
        """启动时后台自动加载 Cookie。"""
        def do_load():
            success = self._login_manager.load_cookies()
            self.root.after(0, lambda: self._update_login_status())
            if success:
                self.root.after(0, lambda: self._status_var.set(
                    f"已自动登录: {self._login_manager.nickname}"
                ))

        threading.Thread(target=do_load, daemon=True).start()

    def _update_login_status(self):
        """更新登录按钮和状态标签。"""
        if self._login_manager.is_logged_in:
            self._login_status_var.set(f"已登录: {self._login_manager.nickname}")
            self._login_btn.config(text="退出登录", command=self._do_logout)
        else:
            self._login_status_var.set("未登录（登录后可下载高清视频）")
            self._login_btn.config(text="登录B站", command=self._start_login)

    def _start_login(self):
        """开始扫码登录流程，弹出二维码窗口。"""
        if self._login_manager.is_logged_in:
            return

        self._login_btn.config(state=tk.DISABLED, text="登录中...")
        self._status_var.set("正在生成二维码...")

        def do_start():
            ok = self._login_manager.start_login()
            if ok:
                self.root.after(0, self._show_login_dialog)
            else:
                self.root.after(0, lambda: self._on_login_error("二维码生成失败，请检查网络后重试"))

        threading.Thread(target=do_start, daemon=True).start()

    def _show_login_dialog(self):
        """显示包含二维码的登录对话框。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("B站扫码登录")
        dialog.geometry("360x440")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        # 提示文本
        tip_var = tk.StringVar(value="请使用 B站手机客户端 扫描二维码")
        ttk.Label(dialog, textvariable=tip_var,
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=(15, 10))

        # 二维码图片
        qr_frame = ttk.Frame(dialog)
        qr_frame.pack(pady=5)

        qr_label = ttk.Label(qr_frame)
        qr_label.pack()

        # 状态文本
        status_var = tk.StringVar(value="等待扫码...")
        ttk.Label(dialog, textvariable=status_var,
                  font=("Microsoft YaHei", 9), foreground="blue").pack(pady=5)

        # 关闭按钮
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(10, 5))
        ttk.Button(btn_frame, text="取消", command=dialog.destroy, width=10).pack()

        # 加载二维码
        qr_image = self._login_manager.get_qr_image()
        if qr_image is not None:
            photo = ImageTk.PhotoImage(qr_image.resize((220, 220), Image.LANCZOS))
            qr_label.configure(image=photo)
            qr_label.image = photo
        else:
            tip_var.set("二维码加载失败，请关闭重试")
            return

        # 超时计时
        start_time = time.time()

        def poll():
            if not dialog.winfo_exists():
                return

            elapsed = time.time() - start_time
            if elapsed > LoginManager.QR_TIMEOUT:
                if dialog.winfo_exists():
                    tip_var.set("二维码已过期，请关闭窗口后重试")
                    status_var.set("已超时")
                    self._login_btn.config(state=tk.NORMAL, text="登录B站",
                                          command=self._start_login)
                return

            state = self._login_manager.check_login_state()

            if state == "scanned":
                status_var.set("已扫码，请在手机上确认登录...")
            elif state == "confirmed":
                status_var.set("已确认，正在完成登录...")
                dialog.after(500, poll)  # 加速轮询
                return
            elif state == "done":
                success = self._login_manager.finish_login()
                dialog.destroy()
                self._update_login_status()
                if success:
                    self._status_var.set(f"登录成功！欢迎 {self._login_manager.nickname}")
                    messagebox.showinfo("登录成功",
                                        f"已成功登录B站！\n用户: {self._login_manager.nickname}")
                else:
                    self._on_login_error("获取登录凭证失败，请重试")
                return
            elif state == "timeout":
                tip_var.set("二维码已过期，请关闭后重试")
                status_var.set("已超时")
                self._login_btn.config(state=tk.NORMAL, text="登录B站",
                                      command=self._start_login)
                return

            # 每 2 秒轮询
            dialog.after(2000, poll)

        dialog.after(500, poll)

        # 对话框关闭时的处理
        def on_dialog_close():
            self._login_btn.config(state=tk.NORMAL, text="登录B站",
                                  command=self._start_login)
            if not self._login_manager.is_logged_in:
                self._status_var.set("登录已取消")
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)

    def _on_login_error(self, msg: str):
        """登录出错处理。"""
        self._login_btn.config(state=tk.NORMAL, text="登录B站", command=self._start_login)
        self._status_var.set(msg)
        messagebox.showerror("登录失败", msg)

    def _do_logout(self):
        """退出登录。"""
        if not self._login_manager.is_logged_in:
            return
        if messagebox.askyesno("确认退出", "确定要退出B站登录吗？\n退出后将无法下载高清视频。"):

            self._login_manager.clear_cookies()
            self._update_login_status()
            self._status_var.set("已退出登录")

    def _on_close(self):
        """关闭窗口。"""
        if self._download_task:
            self._download_task.cancel()
        self.root.destroy()

    def run(self):
        """启动主循环。"""
        self.root.mainloop()
