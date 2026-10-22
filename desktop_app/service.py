from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes
from pathlib import Path

from .reports import generate_report, validate_result


def incomplete_result(result,warnings=()):
    return bool(result.get("partial") or warnings or any(c.get("status") in {"failed","unavailable","insufficient_data","needs_mapping","cancelled"} or c.get("coverage") in {"partial","failed","unsupported"} for c in result.get("checks",[])))


class Secrets:
    def __init__(self, store):
        self.store = store
        self.temporary_key = ""

    @staticmethod
    def _protect(data, decrypt=False):
        if os.name != "nt":
            raise RuntimeError("保存密钥需要Windows用户保护机制")

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(data)
        incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        if decrypt:
            fn = crypt.CryptUnprotectData
            fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        else:
            fn = crypt.CryptProtectData
            fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            free(outgoing.pbData)

    def set_model_key(self, key, persist):
        self.temporary_key = key
        encrypted = base64.b64encode(self._protect(key.encode())).decode() if persist and key else None
        self.store.set_setting("model_key_ciphertext", encrypted)

    def get_model_key(self):
        if self.temporary_key:
            return self.temporary_key
        encrypted = self.store.setting("model_key_ciphertext")
        return self._protect(base64.b64decode(encrypted), True).decode() if encrypted else ""


class Service:
    def __init__(self, store, analyzer=None):
        self.store = store
        self.secrets = Secrets(store)
        self.analyzer = analyzer
        self.stop_event = threading.Event()
        self.work_event = threading.Event()
        self.execution_lock = threading.Lock()
        self.config_lock = threading.RLock()
        self.key_bindings = {}
        self.thread = None

    @property
    def engine_ready(self):
        return self.analyzer is not None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._work, name="paper-sequential-worker", daemon=True)
            self.thread.start()
            self.work_event.set()

    def enqueue(self, paper_id):
        with self.config_lock:
            profile = self.store.setting("model_profile", {})
            key = self.secrets.get_model_key()
            job_id, created = self.store.enqueue(paper_id, profile)
            if created:
                self.key_bindings[job_id] = key
        self.work_event.set()
        return job_id

    def save_model_profile(self, profile, entered_key, persist):
        from .models_client import endpoint_url
        profile = dict(profile)
        if any(k in profile and not isinstance(profile[k],str) for k in ['base_url','text_model','vision_model','review_model','review_vision_model']):raise ValueError('模型名称和地址需要文字')
        if profile.get("base_url"):
            profile["base_url"] = endpoint_url(profile["base_url"],profile.get("local_http",False))
        if any(k in profile and not isinstance(profile[k],bool) for k in ["enable_model","local_http","auto_supplements"]):
            raise ValueError("模型开关需要布尔值")
        profile.pop('max_model_calls',None)  # Old profiles no longer impose a quota.
        with self.config_lock:
            old = self.store.setting("model_profile", {})
            existing = self.secrets.get_model_key()
            if old.get("base_url", "").rstrip("/") != profile.get("base_url", "") and existing and not entered_key:
                raise ValueError("切换服务地址时请重新输入对应Key，不能复用旧服务密钥")
            key = entered_key or existing
            ciphertext = base64.b64encode(self.secrets._protect(key.encode())).decode() if persist and key else None
            profile = dict(profile, version=uuid.uuid4().hex)
            self.store.set_settings({"model_profile": profile, "model_key_ciphertext": ciphertext})
            self.secrets.temporary_key = key

    def _work(self):
        while not self.stop_event.is_set():
            self.work_event.wait(.5)
            self.work_event.clear()
            while not self.stop_event.is_set() and self.run_once():
                pass

    def run_once(self):
        with self.execution_lock:
            job = self.store.claim()
            if job is None:
                return False
            saved = self.store.result_path(job)
            if self.analyzer is None and not saved.is_file():
                self.store.finish_job(job, "waiting_engine", "核心查验组件待接入；已保存文件，未执行论文查验")
                self.key_bindings.pop(job["id"], None)
                return True
            try:
                paper_id = job["paper_id"]
                if self.store.fingerprint(paper_id) != job["input_fingerprint"]:
                    raise ValueError("输入材料版本不一致")
                if saved.is_file():
                    result = json.loads(saved.read_text(encoding="utf-8"))
                else:
                    if job["id"] not in self.key_bindings:
                        self.store.finish_job(job, "waiting_configuration", "原任务配置已失效，需明确重新排队；未自动套用新模型Key")
                        return True
                    frozen_key = self.key_bindings[job["id"]]
                    profile = json.loads(job["model_profile"])
                    profile.update(_job_id=job["id"],_input_fingerprint=job["input_fingerprint"])
                    path=self.store.analysis_path(job)
                    if path.is_file():
                        manifest=json.loads(path.read_text(encoding='utf-8'));self.store.verify_analysis(manifest)
                    else:
                        roots=[a for a in self.store.artifacts(paper_id) if not a.get('parent_id')]
                        retrieval={'status':'disabled','files':[],'notes':['已关闭公开补充材料检索']}
                        downloaded=[]
                        if profile.get('auto_supplements',True):
                            self.store.progress(job['id'],'supplement_search','正在寻找对应的公开补充材料')
                            try:
                                from .supplements import SupplementFetcher
                                downloaded,retrieval=SupplementFetcher(self.store,cancelled=self.stop_event.is_set).fetch(roots)
                            except InterruptedError:raise
                            except Exception:retrieval={'status':'unavailable','files':[],'notes':['公开附件检索出现异常，继续分析已导入材料']}
                        manifest=self.store.freeze_analysis(job,roots+downloaded,retrieval)
                    profile.update(_analysis_manifest=manifest,_analysis_input_fingerprint=manifest['analysis_input_fingerprint'])
                    result = self.analyzer(self.store.paper(paper_id), self.store.analysis_artifacts(manifest), profile, lambda: frozen_key)
                    result['analysis_manifest']=self.store.freeze_derived(manifest)
                    self.store.verify_analysis(result['analysis_manifest'])
                    validate_result(result, self.store.analysis_artifacts(result['analysis_manifest']))
                    self.store.save_result(job, result)
                if result.get('analysis_manifest'):self.store.verify_analysis(result['analysis_manifest'])
                report = generate_report(self.store, paper_id, result, job["id"])
                message = "已生成Markdown与PDF报告" + ("；部分截图未能生成，请查看报告限制" if report["warnings"] else "")
                self.store.finish_job(job, "partial" if incomplete_result(result,report["warnings"]) else "completed", message + ("；存在未覆盖项目，详见检查范围" if incomplete_result(result,report["warnings"]) else ""))
            except InterruptedError:
                self.store.finish_job(job, "interrupted", "任务已停止；已有局部检查及模型响应文件保留，未自动重试")
            except Exception:
                # Provider errors may embed credentials or document excerpts. Never put them in history.
                self.store.finish_job(job, "export_failed" if saved.is_file() else "failed", "结果已保存，可重新生成报告而不重跑引擎" if saved.is_file() else "该篇检查出现异常。请核对引擎连接、输入和额度后重试；未输出通过结论")
            finally:
                self.key_bindings.pop(job["id"], None)
            return True

    def accept_external_result(self, paper_id, result):
        # A normalized adapter result can be delivered through the authenticated local API.
        with self.execution_lock:
            job = self.store.job(result.get("job_id", ""))
            if job["paper_id"] != paper_id or result.get("input_fingerprint") != job["input_fingerprint"] or self.store.fingerprint(paper_id) != job["input_fingerprint"]:
                raise ValueError("外部结果与具体任务或材料版本不一致")
            if job["status"] in {"running", "exporting", "completed", "partial"}:
                raise ValueError("该任务正在执行或已完成，不能覆盖")
            if job.get('analysis_input_fingerprint'):
                manifest=result.get('analysis_manifest')
                if not manifest:raise ValueError('结果缺少实际分析材料快照')
                self.store.verify_analysis(manifest)
                if manifest['job_id']!=job['id']:raise ValueError('分析快照不属于此任务')
                bound=self.store.analysis_artifacts(manifest)
            else:bound=[a for a in self.store.artifacts(paper_id) if not a.get('parent_id')]
            validate_result(result, bound)
            self.store.save_result(job, result)
            self.store.finish_job(job, "exporting", "分析结果已保存，正在生成报告")
            try:
                report = generate_report(self.store, paper_id, result, job["id"])
            except Exception:
                self.store.finish_job(job, "export_failed", "引擎结果已保存，等待重新生成报告")
                raise
            partial=incomplete_result(result,report["warnings"])
            self.store.finish_job(job, "partial" if partial else "completed", "已为该任务生成阶段报告，存在未覆盖或缺失证据" if partial else "已为该任务生成报告")
            return report

    def stop(self):
        self.stop_event.set()
        self.work_event.set()


STATUS_LABELS = {
    "imported": "已导入", "queued": "排队中", "running": "检查中", "completed": "已生成报告",
    "waiting_engine": "等待查验组件", "failed": "出现异常", "interrupted": "上次中断",
    "export_failed": "报告待重建", "waiting_configuration": "等待确认配置",
    "exporting": "正在生成报告",
    "partial": "已生成阶段报告",
}
