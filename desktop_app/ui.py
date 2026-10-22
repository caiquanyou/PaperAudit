from __future__ import annotations

import os
import sys
import queue
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import pypdfium2 as pdfium
from PIL import Image, ImageTk

from .reports import PDF_LOCK
from .service import STATUS_LABELS


def attachment_view_kind(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".md", ".txt", ".json", ".csv", ".tsv", ".py", ".r", ".yaml", ".yml", ".xml"}:
        return "text"
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return "image"
    return "save_only"


class App:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.staged = []
        self.events = queue.Queue()
        self.api = None
        self.importing = False
        self._history_signature = None
        self.root = tk.Tk()
        self.root.title("论文自查")
        self.root.geometry("1080x740")
        self.root.minsize(900, 620)
        self.root.configure(bg="#f3f5f0")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f3f5f0", foreground="#203b36")
        style.configure("TFrame", background="#f3f5f0")
        style.configure("TNotebook", background="#f3f5f0", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(24, 12))
        style.configure("Treeview", rowheight=34, background="white", fieldbackground="white", borderwidth=1)
        style.configure("Treeview.Heading", padding=(12, 9), font=("Microsoft YaHei UI", 10))
        style.map("Treeview", background=[("selected", "#dae9e0")], foreground=[("selected", "#163d32")])
        style.configure("TButton", padding=(13, 8))
        style.configure("Primary.TButton", background="#266b59", foreground="white")
        style.map("Primary.TButton", background=[("active", "#1e5949"), ("disabled", "#a4b5a8")])
        header = ttk.Frame(self.root, padding=(26, 20, 26, 12))
        header.pack(fill="x")
        ttk.Label(header, text="论文自查", font=("Microsoft YaHei UI", 22, "bold")).pack(side="left")
        ttk.Button(header, text="设置", command=self.settings).pack(side="right")
        ttk.Button(header, text="使用说明", command=self.open_help).pack(side="right",padx=8)
        self.engine_label = ttk.Label(header, text="本地查验引擎已接入" if service.engine_ready else "核心查验组件待接入", foreground="#39745f" if service.engine_ready else "#a16e32")
        self.engine_label.pack(side="right", padx=18)
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        self.upload = ttk.Frame(self.tabs, padding=20)
        self.history = ttk.Frame(self.tabs, padding=20)
        self.tabs.add(self.upload, text="上传文献")
        self.tabs.add(self.history, text="历史记录")
        self._upload()
        self._history()
        self.status = tk.StringVar(value="逐篇检查正文和附件，独立复核后生成报告。" if service.engine_ready else "查验组件未就绪。")
        ttk.Label(self.root, textvariable=self.status, padding=(27, 5, 20, 14), foreground="#6f7f70").pack(fill="x")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(500, self.poll)
        self.service.start()

    @staticmethod
    def tree(parent, columns, labels, widths, height=8):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height, selectmode="browse")
        for key, label, width in zip(columns, labels, widths):
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=70, anchor="w")
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _upload(self):
        ttk.Label(self.upload, text="导入论文 PDF 和对应补充材料", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", pady=(0, 9))
        ttk.Label(self.upload, text="可一次选多篇。仅有正文时尝试取得对应的公开附件；初筛后重新核对疑点，再生成各篇报告。", wraplength=900).pack(anchor="w", pady=(0, 18))
        controls = ttk.Frame(self.upload)
        controls.pack(fill="x", pady=(0, 13))
        self.add_button = ttk.Button(controls, text="＋ 添加 PDF", style="Primary.TButton", command=self.add_pdfs)
        self.add_button.pack(side="left")
        ttk.Button(controls, text="为选中论文添加附件", command=self.add_supplements).pack(side="left", padx=10)
        ttk.Button(controls, text="移除选中项", command=self.remove_staged).pack(side="left")
        self.staged_tree = self.tree(self.upload, ("name", "attachments", "size"), ("论文 PDF", "关联附件", "大小"), (590, 120, 100))
        self.attachment_note = tk.StringVar(value="选中一篇后可查看或添加它的补充材料。")
        ttk.Label(self.upload, textvariable=self.attachment_note, wraplength=910, foreground="#6e7d70").pack(anchor="w", pady=12)
        self.staged_tree.bind("<<TreeviewSelect>>", self.show_staged)
        action = ttk.Frame(self.upload)
        action.pack(fill="x", pady=(10, 0))
        self.import_button = ttk.Button(action, text="保存并逐篇检查", style="Primary.TButton", command=lambda: self.import_all(True))
        self.import_button.pack(side="right")
        self.save_button = ttk.Button(action, text="仅保存文件", command=lambda: self.import_all(False))
        self.save_button.pack(side="right", padx=12)
        ttk.Label(action, text="报告格式：Markdown + PDF\n图片疑点自动附来源截图", foreground="#697b6c").pack(side="left")

    def _history(self):
        footer = ttk.Frame(self.history)
        footer.pack(side='bottom',fill="x", pady=(12, 0))
        ttk.Button(footer, text="打开选中文件", command=self.open_selected).pack(side="left")
        ttk.Button(footer, text="导出本篇报告资料包", command=self.export_bundle).pack(side="right")
        top = ttk.Frame(self.history)
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text="已上传文献与报告", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="right")
        ttk.Button(top, text="重新排队检查", command=self.retry).pack(side="right", padx=9)
        self.history_tree = self.tree(self.history, ("name", "time", "status", "reports"), ("论文", "导入时间", "状态", "报告"), (420, 185, 150, 70), height=4)
        self.history_tree.bind("<<TreeviewSelect>>", lambda _: self.show_files())
        self.history_note = tk.StringVar(value="选择文献查看原文件、补充材料和已生成的报告。")
        ttk.Label(self.history, textvariable=self.history_note, wraplength=910, foreground="#6d7d70").pack(anchor="w", pady=12)
        self.files_tree = self.tree(self.history, ("kind", "name"), ("类型", "文件或报告"), (145, 720), height=4)
        self.files_tree.bind("<Double-1>", lambda _: self.open_selected())
        self.file_paths = {}
        self.refresh()

    def add_pdfs(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="选择一篇或多篇论文PDF", filetypes=[("PDF文献", "*.pdf")])
        for path in paths:
            if any(item["path"] == path for item in self.staged):
                continue
            self.staged.append({"path": path, "supplements": []})
        self.refresh_staged()

    def refresh_staged(self):
        self.staged_tree.delete(*self.staged_tree.get_children())
        for i, item in enumerate(self.staged):
            size = Path(item["path"]).stat().st_size / 1024 / 1024
            self.staged_tree.insert("", "end", iid=str(i), values=(Path(item["path"]).name, f"{len(item['supplements'])} 份", f"{size:.1f} MB"))

    def add_supplements(self):
        selected = self.staged_tree.selection()
        if not selected:
            return messagebox.showinfo("选择论文", "请先选中要关联附件的那一篇论文。", parent=self.root)
        item = self.staged[int(selected[0])]
        paths = filedialog.askopenfilenames(parent=self.root, title="为这篇论文选择补充材料", filetypes=[("所有补充文件", "*.*")])
        item["supplements"] = list(dict.fromkeys(item["supplements"] + list(paths)))
        index = selected[0]
        self.refresh_staged()
        self.staged_tree.selection_set(index)
        self.show_staged()

    def show_staged(self, *_):
        selected = self.staged_tree.selection()
        if selected:
            item = self.staged[int(selected[0])]
            self.attachment_note.set("本篇附件：" + ("；".join(Path(p).name for p in item["supplements"]) or "尚未添加，可只导入正文并在报告说明范围。"))

    def remove_staged(self):
        selected = self.staged_tree.selection()
        if selected:
            self.staged.pop(int(selected[0]))
            self.refresh_staged()
            self.attachment_note.set("仅从待导入列表移除，未删除用户原文件。")

    def import_all(self, check):
        if not self.staged:
            return messagebox.showinfo("添加文献", "请先添加论文PDF。", parent=self.root)
        items = list(self.staged)
        self.importing = True
        self.import_button.state(["disabled"])
        self.save_button.state(["disabled"])
        self.add_button.state(["disabled"])

        def save():
            failures = []
            imported = []
            for index, item in enumerate(items, 1):
                self.events.put(("status", f"正在保存第{index}/{len(items)}篇及附件……"))
                try:
                    paper_id = self.store.import_paper(item["path"], item["supplements"])
                    imported.append(item["path"])
                    if check:
                        self.service.enqueue(paper_id)
                except Exception:
                    failures.append(Path(item["path"]).name)
            self.events.put(("imported", (imported, failures)))
        threading.Thread(target=save, name="material-copy", daemon=True).start()

    def refresh(self, force=False):
        selected = self.history_tree.selection()
        items = self.store.list_papers()
        signature = tuple((p["id"], p["status"], p["file_count"], p["report_count"], p["message"]) for p in items)
        if not force and signature == self._history_signature:
            return
        self._history_signature = signature
        self.history_tree.delete(*self.history_tree.get_children())
        for item in items:
            self.history_tree.insert("", "end", iid=item["id"], values=(item["title"], item["created_at"][:19].replace("T", " "), STATUS_LABELS.get(item["status"], item["status"]), f"{item['report_count']} 份"))
        if selected and self.history_tree.exists(selected[0]):
            self.history_tree.selection_set(selected[0])
            self.show_files()

    def show_files(self):
        self.files_tree.delete(*self.files_tree.get_children())
        self.file_paths = {}
        selected = self.history_tree.selection()
        if not selected:
            return
        paper_id = selected[0]
        self.history_note.set(self.store.paper(paper_id)["message"])
        for item in self.store.artifacts(paper_id):
            if item['role']=='archive_member' and 'word/media/' in item['name']:continue
            key = "a_" + item["id"]
            label={'manuscript':'论文正文','supplement':'补充材料','downloaded_supplement':'自动取得的补充材料','archive_member':'附件内文件'}.get(item['role'],'材料')
            self.files_tree.insert("", "end", iid=key, values=(label, item["name"]))
            self.file_paths[key] = self.store.resolve(item["relative_path"])
        for report in self.store.report_list(paper_id):
            directory = self.store.resolve(report["relative_dir"])
            for name, label in [("report.md", "Markdown报告"), ("report.pdf", "PDF报告"),('risk-assessment.json','风险分级与复核清单'),('checks.json','完整检查范围'), ("evidence.json", "截图来源记录"),('review-decisions.md','复核与筛除记录'),('first-pass-candidates.json','首轮候选'),('materials.json','本次材料来源')]:
                if not (directory/name).is_file():continue
                key = report["id"] + "_" + name
                self.files_tree.insert("", "end", iid=key, values=(label, report["created_at"][:19].replace("T", " ") + " / " + name))
                self.file_paths[key] = directory / name

    def retry(self):
        selected = self.history_tree.selection()
        if selected:
            self.service.enqueue(selected[0])
            self.refresh()

    def open_help(self):
        base=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]/'release'
        path=base/'使用说明.pdf'
        if path.is_file():return self.pdf_viewer(path)
        messagebox.showinfo('使用说明','请从完整发行包解压使用说明.pdf，与程序放在同一目录。',parent=self.root)

    def open_selected(self):
        selected = self.files_tree.selection()
        if not selected:
            return
        path = self.file_paths[selected[0]]
        if not path.is_file():
            return messagebox.showerror("文件不可用", "原文件或报告不存在，请检查材料库。", parent=self.root)
        kind = attachment_view_kind(path)
        if kind == "pdf":
            return self.pdf_viewer(path)
        if kind == "text":
            window = tk.Toplevel(self.root)
            window.title(path.name)
            window.geometry("850x650")
            text = scrolledtext.ScrolledText(window, wrap="word", font=("Microsoft YaHei UI", 11), padx=22, pady=20)
            text.pack(fill="both", expand=True)
            text.insert("1.0", path.read_text(encoding="utf-8", errors="replace"))
            text.configure(state="disabled")
            return
        if kind == "image":
            try:
                from PIL import ImageOps
                with Image.open(path) as source:
                    preview = ImageOps.exif_transpose(source).convert("RGB")
                    preview.thumbnail((1050, 750))
                window = tk.Toplevel(self.root)
                window.title(path.name)
                photo = ImageTk.PhotoImage(preview)
                label = ttk.Label(window, image=photo)
                label.image = photo
                label.pack(padx=12, pady=12)
            except Exception:
                messagebox.showerror("无法预览", "图片损坏或尺寸超限，请核对附件。", parent=self.root)
            return
        target = filedialog.asksaveasfilename(parent=self.root, title="保存附件副本", initialfile=path.name)
        if target:
            try:
                shutil.copyfile(path, target)
                self.status.set("已保存附件副本。")
            except OSError:
                messagebox.showerror("保存失败", "请检查保存位置。", parent=self.root)

    def pdf_viewer(self, path):
        window = tk.Toplevel(self.root)
        window.title("PDF预览 — " + path.name)
        window.geometry("850x760")
        controls = ttk.Frame(window, padding=8)
        controls.pack(fill="x")
        holder = ttk.Frame(window)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg="#e6e9e3", highlightthickness=0)
        scroll = ttk.Scrollbar(holder, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        page_number = [0]
        label = ttk.Label(controls)
        label.pack(side="left", padx=12)

        def show(delta=0):
            try:
                with PDF_LOCK:
                    doc = pdfium.PdfDocument(str(path))
                    try:
                        page_number[0] = min(max(page_number[0] + delta, 0), len(doc) - 1)
                        page = doc[page_number[0]]
                        bitmap = page.render(scale=min(1.5, 760 / max(page.get_width(), 1)))
                        try:
                            image = bitmap.to_pil().convert("RGB").copy()
                        finally:
                            bitmap.close()
                            page.close()
                        count = len(doc)
                    finally:
                        doc.close()
                photo = ImageTk.PhotoImage(image)
                canvas.delete("all")
                canvas.create_image(8, 8, anchor="nw", image=photo)
                canvas.image = photo
                canvas.configure(scrollregion=(0, 0, image.width + 16, image.height + 16))
                canvas.yview_moveto(0)
                label.configure(text=f"第{page_number[0] + 1} / {count}页")
            except Exception:
                messagebox.showerror("无法预览", "该PDF可能加密或已损坏，请核对原文件。", parent=window)
        ttk.Button(controls, text="上一页", command=lambda: show(-1)).pack(side="left")
        ttk.Button(controls, text="下一页", command=lambda: show(1)).pack(side="left", padx=8)
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        show()

    def export_bundle(self):
        selected = self.history_tree.selection()
        if not selected:
            return
        reports = self.store.report_list(selected[0])
        if not reports:
            return messagebox.showinfo("暂无报告", "该文献还没有生成报告。核心查验组件待接入时不会生成虚假结果。", parent=self.root)
        directory = self.store.resolve(reports[0]["relative_dir"])
        target = filedialog.asksaveasfilename(parent=self.root, title="导出本篇Markdown、PDF和证据图片", initialfile=self.store.paper(selected[0])["title"][:80] + "_查验报告.zip", defaultextension=".zip", filetypes=[("报告资料包", "*.zip")])
        if target:
            try:
                shutil.make_archive(str(Path(target).with_suffix("")), "zip", directory)
                self.status.set("已导出本篇报告与证据图片；Markdown图片使用包内相对路径。")
            except OSError:
                messagebox.showerror("导出失败", "请检查保存位置及磁盘空间。", parent=self.root)

    def settings(self):
        from .model_settings import ModelSettings
        existing = getattr(self, "settings_dialog", None)
        if existing is not None and existing.window.winfo_exists():
            existing.window.lift()
            return
        self.settings_dialog = ModelSettings(self)

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self.status.set(value)
                elif kind == "imported":
                    self.importing = False
                    imported, failures = value
                    self.staged = [item for item in self.staged if item["path"] not in imported]
                    self.refresh_staged()
                    for button in (self.import_button, self.save_button, self.add_button):
                        button.state(["!disabled"])
                    self.tabs.select(self.history)
                    self.status.set(f"已保存{len(imported)}篇文献" + (f"；{len(failures)}篇未成功，请核对文件" if failures else "") + ("。核心查验组件待接入，未执行真实查验。" if not self.service.engine_ready else "，正在逐篇检查。"))
        except queue.Empty:
            pass
        self.refresh()
        self.root.after(1200, self.poll)

    def close(self):
        if self.importing:
            return messagebox.showinfo("正在保存文件", "请等待当前材料保存完成后再关闭，避免文件复制中断。", parent=self.root)
        active = any(item["status"] in {"running", "queued", "exporting"} for item in self.store.list_papers())
        if active and not messagebox.askyesno("退出程序", "仍有排队或运行任务。退出后队列保留；当前调用可能中断，重启后需核对再继续。是否退出？", parent=self.root):
            return
        self.service.stop()
        if self.api:
            self.api.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
