from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="论文自查原生Windows基础版")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--self-test", action="store_true", help="验证打包依赖，不启动窗口或模型调用")
    args = parser.parse_args()
    from desktop_app.storage import Store, default_root
    from desktop_app.service import Service
    if args.self_test:
        import pypdfium2
        import PIL
        import reportlab
        import tkinter
        if args.data_dir is None:
            raise SystemExit("自检需要显式指定独立测试数据目录")
        store = Store(args.data_dir)
        from desktop_app.audit_engine import AuditEngine
        service = Service(store)
        service.analyzer = AuditEngine(store,cancelled=service.stop_event.is_set)
        assert service.engine_ready
        from desktop_app.pdf_report import fonts
        assert len(fonts())==2
        from desktop_app.ui import App
        app = App(service)
        app.root.withdraw()
        assert len(app.tabs.tabs()) == 2
        app.settings()
        settings = app.settings_dialog
        settings.window.withdraw()
        assert len(settings.provider['values']) == 5
        assert not settings.expanded
        assert str(settings.entries['base_url']['state']) == 'normal'
        assert settings.auto_supplements.get()
        assert 'review_model' in settings.entries and 'review_vision_model' in settings.entries
        settings.toggle_advanced()
        assert settings.expanded
        settings.close()
        app.root.update_idletasks()
        service.stop()
        app.root.destroy()
        doc = pypdfium2.PdfDocument.new()
        page = doc.new_page(200, 200)
        bitmap = page.render(scale=1)
        assert bitmap.to_pil().size == (200, 200)
        bitmap.close()
        page.close()
        doc.close()
        import cv2,numpy,openpyxl,xlrd
        assert hasattr(cv2,"SIFT_create")
        (store.root / "self-test.json").write_text('{"ok":true,"native":true,"engine_ready":true,"tabs":2,"pdf_renderer":true,"cv_sift":true,"table_parser":true,"provider_presets":4,"custom_base_url":true,"settings_native":true}', encoding="utf-8")
        return
    handle = None
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        identity = hashlib.sha256(str(args.data_dir or default_root()).encode()).hexdigest()[:20]
        handle = kernel.CreateMutexW(None, False, "Local\\PaperAudit_" + identity)
        if ctypes.get_last_error() == 183:
            ctypes.windll.user32.MessageBoxW(None, "此材料库已有论文自查程序运行，请使用已打开的窗口。", "论文自查", 0)
            return
    try:
        from desktop_app.ui import App
        from desktop_app.audit_engine import AuditEngine
        store=Store(args.data_dir)
        service=Service(store)
        service.analyzer=AuditEngine(store,cancelled=service.stop_event.is_set)
        App(service).run()
    except Exception:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, "程序未能启动。请检查数据目录权限及安装包是否完整。", "论文自查", 0x10)
        else:
            raise
    finally:
        if handle:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
