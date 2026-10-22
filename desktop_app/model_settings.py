"""Small native settings form with optional details and isolated provider drafts."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from .api import Api
from .model_presets import PRESETS, initial_profile, preset_profile
from .models_client import ModelClient, ModelError, endpoint_url


class ModelSettings:
    def __init__(self, app):
        self.app, self.service = app, app.service
        self.window = w = tk.Toplevel(app.root)
        w.title("模型与 API 设置")
        w.geometry("700x650");w.minsize(620, 540);w.transient(app.root)
        self.original = self.service.store.setting("model_profile", {})
        profile = initial_profile(self.original)
        self.provider_id = profile["provider_id"]
        self.drafts = {};self.responses = queue.Queue();self.request_id = 0;self.poll_id = None
        self.entries = {};self.loading = True;self.address_seen = ""
        header = ttk.Frame(w, padding=(24, 20, 24, 8));header.pack(fill="x")
        ttk.Label(header, text="连接你的模型", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(header, text="选择服务商，填入 Key，即可使用预置模型。", foreground="#697b70").pack(anchor="w", pady=(6, 0))
        footer = ttk.Frame(w, padding=(24, 10, 24, 18));footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="保存设置", style="Primary.TButton", command=self.save).pack(side="right")
        ttk.Button(footer, text="取消", command=self.close).pack(side="right", padx=10)
        self.feedback = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self.feedback, wraplength=360, foreground="#65756d").pack(side="left")
        body = ttk.Frame(w);body.pack(fill="both", expand=True, padx=(24, 12))
        canvas = tk.Canvas(body, bg="#f3f5f0", highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, command=canvas.yview);scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True);canvas.configure(yscrollcommand=scrollbar.set)
        form = ttk.Frame(canvas);self.form = form
        content = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(content, width=e.width))
        w.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))
        ttk.Label(form, text="服务商").pack(anchor="w", pady=(7, 4))
        self.provider = ttk.Combobox(form, state="readonly", values=[v["label"] for v in PRESETS.values()])
        self.provider.pack(fill="x");self.provider.set(PRESETS[self.provider_id]["label"])
        self.provider.bind("<<ComboboxSelected>>", self.change_provider)
        self.field(form, "text_model", "模型（可选择，也可直接输入）", combo=True)
        self.entries["text_model"].bind("<<ComboboxSelected>>", self.change_model)
        ttk.Label(form, text="API Key").pack(anchor="w", pady=(12, 4))
        key_row = ttk.Frame(form);key_row.pack(fill="x")
        self.entries["key"] = ttk.Entry(key_row, show="●");self.entries["key"].pack(side="left", fill="x", expand=True)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_row, text="显示", variable=self.show_key, command=lambda: self.entries["key"].configure(show="" if self.show_key.get() else "●")).pack(side="right", padx=(10, 0))
        self.key_note = tk.StringVar()
        ttk.Label(form, textvariable=self.key_note, foreground="#74816f", wraplength=610).pack(anchor="w", pady=(3, 0))
        self.field(form, "base_url", "服务地址 base_url（可修改）")
        self.address_var = tk.StringVar()
        self.entries["base_url"].configure(textvariable=self.address_var)
        self.address_var.trace_add("write", self.address_changed)
        self.note = tk.StringVar()
        ttk.Label(form, textvariable=self.note, wraplength=610, foreground="#687a70").pack(anchor="w", pady=(7, 7))
        self.persist = tk.BooleanVar(value=bool(self.service.store.setting("model_key_ciphertext")))
        ttk.Checkbutton(form, text="在本机加密保存 Key", variable=self.persist).pack(anchor="w")
        actions = ttk.Frame(form);actions.pack(fill="x", pady=(9, 4))
        self.discover_button = ttk.Button(actions, text="获取可用模型", command=self.discover);self.discover_button.pack(side="left")
        ttk.Label(actions, text="不发送论文内容", foreground="#74816f").pack(side="left", padx=10)
        self.advanced_button = ttk.Button(form, text="＋ 高级选项", command=self.toggle_advanced)
        self.advanced_button.pack(anchor="w", pady=(6, 4))
        self.advanced = ttk.Frame(form, padding=(0, 4, 0, 12));self.expanded = False
        self.field(self.advanced, "vision_model", "图像审阅模型（可修改，留空则关闭）", combo=True)
        self.field(self.advanced, "review_model", "独立复核文本模型（留空沿用上面的文本模型）", combo=True)
        self.field(self.advanced, "review_vision_model", "独立复核图像模型（留空沿用上面的图像模型）", combo=True)
        self.auto_supplements = tk.BooleanVar(value=profile.get('auto_supplements',True))
        ttk.Checkbutton(self.advanced,text='仅有正文时，尝试下载对应的公开补充材料',variable=self.auto_supplements).pack(anchor='w',pady=(8,2))
        self.enabled = tk.BooleanVar(value=profile.get("enable_model", True))
        self.local_http = tk.BooleanVar(value=profile.get("local_http", False))
        ttk.Checkbutton(self.advanced, text="启用模型辅助检查", variable=self.enabled).pack(anchor="w", pady=(8, 2))
        ttk.Checkbutton(self.advanced, text="允许本机模型地址（localhost HTTP）", variable=self.local_http).pack(anchor="w")
        ttk.Label(self.advanced,text='按材料与候选逐批完成，不设置模型调用次数或金额上限。',wraplength=610,foreground='#74816f').pack(anchor='w',pady=8)
        ttk.Separator(self.advanced).pack(fill="x", pady=8)
        self.api_note = tk.StringVar();self.update_api_note()
        ttk.Label(self.advanced, textvariable=self.api_note).pack(anchor="w")
        api_buttons = ttk.Frame(self.advanced);api_buttons.pack(fill="x", pady=8)
        ttk.Button(api_buttons, text="开启 / 关闭本机 API", command=self.toggle_api).pack(side="left")
        ttk.Button(api_buttons, text="复制工具令牌", command=self.copy_token).pack(side="left", padx=8)
        ttk.Label(self.advanced, text="远端检查会发送相应论文文本或图片。模型与图片能力、可用地域以服务商账号为准。", wraplength=610, foreground="#74816f").pack(anchor="w", pady=(4, 0))
        self.load(profile)
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = w.after(150, self.poll)

    def field(self, parent, key, label, combo=False):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(12, 4))
        entry = ttk.Combobox(parent, state="normal") if combo else ttk.Entry(parent)
        entry.pack(fill="x");self.entries[key] = entry

    def values(self):
        values = {key: self.entries[key].get().strip() for key in ("base_url", "text_model", "vision_model", "review_model", "review_vision_model")}
        values.update(provider_id=self.provider_id, enable_model=self.enabled.get(), local_http=self.local_http.get(), auto_supplements=self.auto_supplements.get())
        return values

    def load(self, profile, key="", persist=None):
        self.loading = True
        preset = PRESETS[self.provider_id]
        for field in ("base_url", "text_model", "vision_model", "review_model", "review_vision_model"):
            self.entries[field].set(profile.get(field, "")) if field.endswith("model") else self.replace(field, profile.get(field, ""))
        self.replace("key", key)
        self.entries["text_model"].configure(values=preset["models"])
        self.entries["vision_model"].configure(values=("",) + preset["vision_models"])
        self.entries['review_model'].configure(values=('',)+preset['models'])
        self.entries['review_vision_model'].configure(values=('',)+preset['vision_models'])
        self.auto_supplements.set(profile.get('auto_supplements',True))
        self.enabled.set(profile.get("enable_model", True));self.local_http.set(profile.get("local_http", False))
        if persist is not None:self.persist.set(persist)
        self.note.set(preset["note"]);self.show_key.set(False);self.entries["key"].configure(show="●")
        self.address_seen = self.entries["base_url"].get().strip().rstrip("/")
        self.model_seen = self.entries["text_model"].get()
        self.loading = False;self.update_key_note()

    def replace(self, key, value):
        self.entries[key].delete(0, "end");self.entries[key].insert(0, value)

    def change_provider(self, _=None):
        selected = next(k for k,v in PRESETS.items() if v["label"] == self.provider.get())
        if selected == self.provider_id:return
        self.drafts[self.provider_id] = (self.values(), self.entries["key"].get(), self.persist.get())
        self.provider_id = selected;self.request_id += 1;self.feedback.set("")
        self.discover_button.state(["!disabled"])
        profile,key,persist = self.drafts.get(selected, (preset_profile(selected), "", False))
        self.load(profile, key, persist)

    def change_model(self, _=None):
        model = self.entries["text_model"].get()
        if self.entries["vision_model"].get() == self.model_seen and model in PRESETS[self.provider_id]["vision_models"]:
            self.entries["vision_model"].set(model)
        self.model_seen = model

    def address_changed(self, *_):
        if self.loading:return
        address = self.entries["base_url"].get().strip().rstrip("/")
        if address == self.address_seen:return
        self.address_seen = address
        self.request_id += 1;self.discover_button.state(["!disabled"]);self.feedback.set("")
        self.replace("key", "");self.update_key_note()

    def update_key_note(self):
        same = self.entries["base_url"].get().strip().rstrip("/") == self.original.get("base_url", "").rstrip("/")
        self.key_note.set("此地址已有 Key，留空可继续使用。" if same and self.service.secrets.get_model_key() else "请输入此服务地址对应的 Key；不勾选保存则仅本次使用。")

    def discover(self):
        address = self.entries["base_url"].get().strip();entered = self.entries["key"].get().strip()
        try:
            address = endpoint_url(address, self.local_http.get())
            with self.service.config_lock:
                current = self.service.store.setting("model_profile", {})
                existing = self.service.secrets.get_model_key() if address == current.get("base_url", "").rstrip("/") else ""
            key = entered or existing
            if not key:raise ValueError("请先输入当前服务的 API Key。")
        except ValueError as exc:
            return self.feedback.set(str(exc))
        self.request_id += 1;rid = self.request_id
        profile = {"base_url": address, "local_http": self.local_http.get()}
        self.discover_button.state(["disabled"]);self.feedback.set("正在获取可用模型…")
        def request():
            try:self.responses.put((rid, ModelClient(profile, key).list_models(), ""))
            except ModelError as exc:self.responses.put((rid, [], str(exc)))
            except Exception:self.responses.put((rid, [], "connection_failed"))
        threading.Thread(target=request, daemon=True).start()

    def poll(self):
        try:
            while True:
                rid, choices, error = self.responses.get_nowait()
                if rid != self.request_id:continue
                self.discover_button.state(["!disabled"])
                if error:
                    self.feedback.set("未能获取列表，请核对地址和 Key；仍可手填模型 ID。")
                elif not choices:self.feedback.set("服务未返回模型列表，请选择预置或手填模型 ID。")
                else:
                    self.entries["text_model"].configure(values=tuple(dict.fromkeys(choices + list(PRESETS[self.provider_id]["models"]))))
                    self.entries["vision_model"].configure(values=("",) + tuple(choices))
                    self.feedback.set(f"已获取 {len(choices)} 个模型，图片能力请以服务商说明为准。")
        except queue.Empty:pass
        self.poll_id = self.window.after(150, self.poll)

    def toggle_advanced(self):
        self.expanded = not self.expanded
        if self.expanded:self.advanced.pack(fill="x")
        else:self.advanced.pack_forget()
        self.advanced_button.configure(text="－ 收起高级选项" if self.expanded else "＋ 高级选项")

    def update_api_note(self):
        self.api_note.set("本机 API：未开启" if not self.app.api else f"本机 API：127.0.0.1:{self.app.api.port}")

    def toggle_api(self):
        try:
            if self.app.api:self.app.api.stop();self.app.api = None
            else:self.app.api = Api(self.service)
            self.update_api_note()
        except Exception:self.feedback.set("本机 API 未能启动，请检查本机端口。")

    def copy_token(self):
        if self.app.api:
            self.window.clipboard_clear();self.window.clipboard_append(self.app.api.token);self.feedback.set("已复制本次工具令牌。")
        else:self.feedback.set("请先开启本机 API。")

    def save(self):
        try:
            profile = self.values()
            if profile["enable_model"] and (not profile["base_url"] or not profile["text_model"]):
                raise ValueError("请填写服务地址和模型名称，或在高级选项关闭模型辅助。")
            self.service.save_model_profile(profile, self.entries["key"].get().strip(), self.persist.get())
            self.app.status.set("模型设置已保存。")
            self.close()
        except ValueError as exc:self.feedback.set(str(exc))
        except Exception:self.feedback.set("未能保存，请检查服务地址及本机密钥保护是否可用。")

    def close(self):
        self.request_id += 1
        if self.poll_id is not None:self.window.after_cancel(self.poll_id)
        self.drafts.clear();self.replace("key", "")
        self.window.destroy()
