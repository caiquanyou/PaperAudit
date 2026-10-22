"""Meaningful native/storage/queue/report/API integration checks using synthetic PDFs."""
from __future__ import annotations

import base64
import json
import shutil
import sys
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop_app.storage import Store
from desktop_app.service import Service, Secrets
from desktop_app.reports import generate_report, validate_result, PDF_LOCK
from desktop_app.api import Api
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium

WORK = ROOT / ".build" / "integration-tests"


def make_pdf(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 18)
    c.drawString(55, 770, "SYNTHETIC FIXTURE - NOT RESEARCH DATA")
    c.setStrokeColorRGB(.2, .55, .45)
    c.setFillColorRGB(.9, .95, .91)
    c.rect(65, 485, 185, 140, stroke=1, fill=1)
    c.setFillColorRGB(.2, .45, .4)
    c.circle(120, 545, 22, fill=1)
    c.circle(200, 560, 15, fill=1)
    c.setFont("Helvetica", 12)
    c.drawString(68, 465, "Figure A / screenshot export fixture")
    c.showPage()
    c.setFont("Helvetica", 18)
    c.drawString(60, 750, "Second page / supplementary context")
    c.save()


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WORK.mkdir(parents=True, exist_ok=True)
        cls.source = WORK / "synthetic-paper.pdf"
        make_pdf(cls.source)
        cls.second = WORK / "synthetic-paper-2.pdf"
        shutil.copyfile(cls.source, cls.second)
        cls.supplement = WORK / "supplement.txt"
        cls.supplement.write_text("Synthetic supplement for paper one only.", encoding="utf-8")

    def store(self, name):
        import uuid
        return Store(WORK / (name + "-" + uuid.uuid4().hex[:8]))

    def result(self, artifacts):
        main = next(a for a in artifacts if a["role"] == "manuscript")
        return {"engine_id": "synthetic-test-fixture-not-a-detector", "summary": "此文件仅验证截图与报告导出格式，未执行真实论文查验。", "findings": [{"title": "截图导出测试示例", "kind": "image", "severity": "测试样例", "observation": "以下截图来自人工生成的测试PDF，不对应真实科研疑点。", "alternative_explanation": "此处为测试输入，不能用于任何科研判断。", "recommendation": "核对裁图、来源页码和PDF内嵌图片是否正常。", "anchors": [{"artifact_id": main["id"], "page": 1, "bbox": [.09, .24, .45, .44]}]}], "checks": [{"name": "报告导出格式验证", "status": "测试完成", "reason": "不表示完成论文查验"}, {"name": "RNA-seq专项", "status": "不适用", "reason": "该测试PDF不含RNA-seq研究"}], "limitations": ["这是导出测试样例，非论文核查结果。"]}

    def test_materials_persist_and_supplements_stay_with_paper(self):
        store = self.store("storage")
        a = store.import_paper(self.source, [self.supplement])
        b = store.import_paper(self.second)
        self.assertEqual(len(store.artifacts(a)), 2)
        self.assertEqual(len(store.artifacts(b)), 1)
        reopened = Store(store.root)
        self.assertEqual(len(reopened.list_papers()), 2)
        self.assertTrue(all(reopened.resolve(f["relative_path"]).is_file() for f in reopened.artifacts(a)))

    def test_queue_is_ordered_one_paper_per_call_and_idempotent(self):
        store = self.store("queue")
        papers = [store.import_paper(p) for p in (self.source, self.second)]
        called = []

        def analyzer(paper, artifacts, profile, getter):
            called.append(paper["id"])
            return self.result(artifacts)
        service = Service(store, analyzer)
        first = service.enqueue(papers[0])
        self.assertEqual(first, service.enqueue(papers[0]))
        service.enqueue(papers[1])
        self.assertTrue(service.run_once())
        self.assertEqual(called, papers[:1])
        self.assertTrue(service.run_once())
        self.assertEqual(called, papers)
        self.assertFalse(service.run_once())
        self.assertNotEqual(store.report_list(papers[0])[0]["relative_dir"], store.report_list(papers[1])[0]["relative_dir"])

    def test_no_engine_never_generates_fake_report(self):
        store = self.store("pending")
        paper = store.import_paper(self.source)
        service = Service(store)
        service.enqueue(paper)
        service.run_once()
        self.assertEqual(store.paper(paper)["status"], "waiting_engine")
        self.assertFalse(store.report_list(paper))

    def test_rendered_evidence_and_portable_markdown(self):
        store = self.store("reports")
        paper = store.import_paper(self.source)
        result = generate_report(store, paper, self.result(store.artifacts(paper)))
        self.assertFalse(result["warnings"])
        folder = result["directory"]
        markdown = (folder / "report.md").read_text(encoding="utf-8")
        self.assertIn("images/issue_001_01.png", markdown)
        self.assertIn("PDF第1页", markdown)
        self.assertTrue((folder / "images/issue_001_01.png").is_file())
        with PDF_LOCK:
            doc = pdfium.PdfDocument(str(folder / "report.pdf"))
            self.assertGreaterEqual(len(doc), 1)
            for i in range(len(doc)):
                page = doc[i]
                bmp = page.render(scale=1.2)
                bmp.to_pil().save(WORK / f"report-page-{i+1}.png")
                bmp.close()
                page.close()
            doc.close()
        output = ROOT / "output" / "pdf"
        output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(folder / "report.pdf", output / "截图报告_导出格式样例.pdf")
        shutil.copytree(folder, ROOT / "output" / "报告截图格式样例", dirs_exist_ok=True)

    def test_reject_foreign_anchor_and_invalid_bbox(self):
        store = self.store("validation")
        paper = store.import_paper(self.source)
        files = store.artifacts(paper)
        result = self.result(files)
        result["findings"][0]["anchors"][0]["artifact_id"] = "another-paper-file"
        with self.assertRaises(ValueError):
            validate_result(result, files)
        result = self.result(files)
        result["findings"][0]["anchors"][0]["bbox"] = [0, 0, 2, 1]
        with self.assertRaises(ValueError):
            validate_result(result, files)

    def test_windows_credentials_encrypted(self):
        store = self.store("secrets")
        secrets = Secrets(store)
        secrets.set_model_key("NOT-A-REAL-KEY-TEST-ONLY", True)
        self.assertNotIn("NOT-A-REAL", store.db.read_bytes().decode("utf-8", "ignore"))
        secrets.temporary_key = ""
        self.assertEqual(secrets.get_model_key(), "NOT-A-REAL-KEY-TEST-ONLY")

    def test_api_authentication_and_file_upload(self):
        store = self.store("api")
        service = Service(store)
        api = Api(service)
        url = f"http://127.0.0.1:{api.port}/api/v1"
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + "/papers")
            self.assertEqual(error.exception.code, 401)
            payload = {"files": [{"name": "fixture.pdf", "role": "manuscript", "content_base64": base64.b64encode(self.source.read_bytes()).decode()}], "enqueue": False}
            req = urllib.request.Request(url + "/paper-packages", data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + api.token, "Content-Type": "application/json"})
            response = json.loads(urllib.request.urlopen(req).read())
            self.assertFalse(response["engine_ready"])
            self.assertEqual(len(store.list_papers()), 1)
            req = urllib.request.Request(url + "/papers", headers={"Authorization": "Bearer " + api.token, "Origin": "https://untrusted.invalid"})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req)
        finally:
            api.stop()

    def test_native_has_only_two_main_tabs(self):
        from desktop_app.ui import App
        store = self.store("native")
        service = Service(store)
        app = App(service)
        app.root.withdraw()
        try:
            self.assertEqual(len(app.tabs.tabs()), 2)
            self.assertEqual([app.tabs.tab(i, "text") for i in range(2)], ["上传文献", "历史记录"])
            app.root.update_idletasks()
        finally:
            service.stop()
            app.root.destroy()

    def test_old_external_result_does_not_complete_new_job(self):
        store = self.store("late-result")
        paper = store.import_paper(self.source)
        service = Service(store)
        old = service.enqueue(paper)
        service.run_once()
        newer = service.enqueue(paper)
        result = self.result(store.artifacts(paper))
        result.update(job_id=old, input_fingerprint=store.job(old)["input_fingerprint"])
        service.accept_external_result(paper, result)
        self.assertEqual(store.job(old)["status"], "completed")
        self.assertEqual(store.job(newer)["status"], "queued")
        self.assertEqual(store.paper(paper)["status"], "queued")
        with self.assertRaises(ValueError):
            service.accept_external_result(paper, result)

    def test_model_and_key_are_bound_to_queued_job(self):
        store = self.store("config")
        paper = store.import_paper(self.source)
        calls = []
        def analyzer(paper, artifacts, profile, getter):
            calls.append((profile["base_url"], getter()))
            return self.result(artifacts)
        service = Service(store, analyzer)
        service.save_model_profile({"base_url": "https://a.invalid", "text_model": "a"}, "KEY-A", False)
        service.enqueue(paper)
        with self.assertRaises(ValueError):
            service.save_model_profile({"base_url": "https://b.invalid"}, "", False)
        self.assertEqual(store.setting("model_profile")["base_url"], "https://a.invalid")
        service.save_model_profile({"base_url": "https://b.invalid", "text_model": "b"}, "KEY-B", False)
        service.run_once()
        self.assertEqual(calls, [("https://a.invalid", "KEY-A")])

    def test_export_failure_recovers_without_repeating_analyzer(self):
        store = self.store("recovery")
        paper = store.import_paper(self.source)
        called = []
        def analyzer(paper, artifacts, profile, getter):
            called.append(paper["id"])
            return self.result(artifacts)
        service = Service(store, analyzer)
        job = service.enqueue(paper)
        with patch("desktop_app.service.generate_report", side_effect=OSError("disk error")):
            service.run_once()
        self.assertEqual(store.job(job)["status"], "export_failed")
        self.assertTrue(store.result_path(store.job(job)).is_file())
        self.assertEqual(service.enqueue(paper), job)
        service.run_once()
        self.assertEqual(called, [paper])
        self.assertEqual(store.job(job)["status"], "completed")

    def test_tiff_second_frame_is_used_for_evidence(self):
        from PIL import Image
        from desktop_app.reports import screenshot
        source = WORK / "two-frames.tiff"
        Image.new("RGB", (80, 60), "red").save(source, save_all=True, append_images=[Image.new("RGB", (80, 60), "blue")])
        target = WORK / "second-frame.png"
        screenshot(source, {"page": 2}, target)
        with Image.open(target) as output:
            self.assertEqual(output.getpixel((40, 30)), (0, 0, 255))
        with self.assertRaises(ValueError):
            screenshot(source, {"page": 3}, target)

    def test_attachments_never_launch_programs_or_scripts(self):
        from desktop_app.ui import attachment_view_kind
        for name in ["tool.exe", "script.ps1", "payload.lnk", "report.docm", "open.url", "run.bat", "sample.xlsm"]:
            self.assertEqual(attachment_view_kind(name), "save_only")
        self.assertEqual(attachment_view_kind("analysis.py"), "text")
        self.assertEqual(attachment_view_kind("figure.tif"), "image")

    def test_restart_does_not_bind_new_key_to_old_queued_profile(self):
        store = self.store("restart-config")
        paper = store.import_paper(self.source)
        old = Service(store)
        old.save_model_profile({"base_url": "https://a.invalid"}, "KEY-A", False)
        job = old.enqueue(paper)
        calls = []
        restarted = Service(Store(store.root), lambda *args: calls.append(args))
        restarted.save_model_profile({"base_url": "https://b.invalid"}, "KEY-B", False)
        self.assertEqual(restarted.enqueue(paper), job)
        restarted.run_once()
        self.assertEqual(calls, [])
        self.assertEqual(store.job(job)["status"], "waiting_configuration")

    def test_interrupted_external_export_recovers_saved_result(self):
        store = self.store("external-recovery")
        paper = store.import_paper(self.source)
        service = Service(store)
        job_id = service.enqueue(paper)
        service.run_once()
        result = self.result(store.artifacts(paper))
        result.update(job_id=job_id, input_fingerprint=store.job(job_id)["input_fingerprint"])
        with patch("desktop_app.service.generate_report", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                service.accept_external_result(paper, result)
        self.assertEqual(store.job(job_id)["status"], "exporting")
        reopened = Store(store.root)
        self.assertEqual(reopened.job(job_id)["status"], "interrupted")
        resumed = Service(reopened)
        self.assertEqual(resumed.enqueue(paper), job_id)
        resumed.run_once()
        self.assertEqual(reopened.job(job_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
