from __future__ import annotations

import hmac
import json
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Api:
    """Opt-in loopback API. Native UI calls services directly, not a web page."""
    def __init__(self, service, port=0):
        self.service = service
        self.token = secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(30)

            def log_message(self, *_):
                pass

            def reply(self, code, body):
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                valid_host = self.headers.get("Host", "").split(":")[0] in {"127.0.0.1", "localhost"}
                token = self.headers.get("Authorization", "")
                if not valid_host or self.headers.get("Origin") or not hmac.compare_digest(token, "Bearer " + owner.token):
                    self.reply(401, {"error": "需要有效本机工具令牌，不允许网页跨来源调用"})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                parts = self.path.split("?")[0].strip("/").split("/")
                if parts[:2] != ["api", "v1"]:
                    return self.reply(404, {"error": "接口不存在"})
                if parts[2:] == ["capabilities"]:
                    return self.reply(200, {"version": "0.6.2", "native_windows": True, "engine_ready": owner.service.engine_ready, "concurrent_papers": 1, "report_formats": ["markdown", "pdf"], "risk_triage":True,"embedded_pdf_fonts":True,"local_checks": ["pdf_and_supplements", "table_rules", "exact_phash_orb_sift","docx_embedded_images"], "independent_review":True,"automatic_evidence_completion":True,"public_supplement_retrieval":True,"model_protocols": ["chat_completions"], "unavailable": ["ocr", "cross_paper_corpus", "external_reproduction"], "implemented": ["GET /papers", "POST /paper-packages", "POST /model-profile", "POST /papers/{id}/check", "POST /papers/{id}/result", "GET /papers/{id}/files", "GET /papers/{id}/jobs", "GET /papers/{id}/reports"]})
                if parts[2:] == ["papers"]:
                    return self.reply(200, {"items": owner.service.store.list_papers()})
                if len(parts) == 5 and parts[2] == "reports" and parts[4] in {"markdown", "pdf", "bundle"}:
                    import io
                    import zipfile
                    with owner.service.store.connect() as con:
                        row = con.execute("SELECT * FROM reports WHERE id=?", (parts[3],)).fetchone()
                    if not row:
                        return self.reply(404, {"error": "报告不存在"})
                    directory = owner.service.store.resolve(row["relative_dir"])
                    if parts[4] == "bundle":
                        stream = io.BytesIO()
                        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                            for file in directory.rglob("*"):
                                if file.is_file():
                                    archive.write(file, file.relative_to(directory).as_posix())
                        raw, mime = stream.getvalue(), "application/zip"
                    else:
                        target = directory / ("report.md" if parts[4] == "markdown" else "report.pdf")
                        raw = target.read_bytes()
                        mime = "text/markdown; charset=utf-8" if parts[4] == "markdown" else "application/pdf"
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if len(parts) == 5 and parts[2] == "files" and parts[4] == "content":
                    with owner.service.store.connect() as con:
                        row = con.execute("SELECT * FROM artifacts WHERE id=?", (parts[3],)).fetchone()
                    if not row:
                        return self.reply(404, {"error": "文件不存在"})
                    path = owner.service.store.resolve(row["relative_path"])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(path.stat().st_size))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with path.open("rb") as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b""):
                            self.wfile.write(block)
                    return
                if len(parts) == 5 and parts[2] == "papers":
                    try:
                        owner.service.store.paper(parts[3])
                        if parts[4] == "files":
                            items = [{k: v for k, v in f.items() if k != "relative_path"} for f in owner.service.store.artifacts(parts[3])]
                            return self.reply(200, {"items": items})
                        if parts[4] == "reports":
                            items = [{k: v for k, v in r.items() if k != "relative_dir"} for r in owner.service.store.report_list(parts[3])]
                            return self.reply(200, {"items": items})
                        if parts[4] == "jobs":
                            items = [{k: v for k, v in j.items() if k != "model_profile"} for j in owner.service.store.jobs(parts[3])]
                            return self.reply(200, {"items": items})
                        if parts[4] == "results":
                            for job in owner.service.store.jobs(parts[3]):
                                path=owner.service.store.result_path(job)
                                if path.is_file():
                                    return self.reply(200,{"job_id":job["id"],"result":json.loads(path.read_text(encoding="utf-8"))})
                            return self.reply(404,{"error":"该篇还没有已保存的分析结果"})
                    except KeyError:
                        return self.reply(404, {"error": "论文不存在"})
                return self.reply(404, {"error": "接口不存在"})

            def do_POST(self):
                if not self.authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 24 * 1024 * 1024:
                        return self.reply(413, {"error": "本接口接受24MB以内JSON；大文件使用桌面导入"})
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        return self.reply(415, {"error": "需要application/json"})
                    data = json.loads(self.rfile.read(length))
                    parts = self.path.strip("/").split("/")
                    if parts == ["api","v1","model-profile"]:
                        profile=data["profile"]
                        if not isinstance(profile,dict): raise ValueError()
                        allowed={k:profile[k] for k in ["base_url","text_model","vision_model","review_model","review_vision_model","auto_supplements","enable_model","local_http"] if k in profile}
                        owner.service.save_model_profile(allowed,data.get("api_key",""),bool(data.get("persist",False)))
                        return self.reply(200,{"saved":True,"key_returned":False})
                    if parts == ["api", "v1", "paper-packages"]:
                        return self.import_package(data)
                    if len(parts) == 5 and parts[:3] == ["api", "v1", "papers"]:
                        paper_id = parts[3]
                        owner.service.store.paper(paper_id)
                        if parts[4] == "check":
                            job = owner.service.enqueue(paper_id)
                            return self.reply(202, {"job_id": job, "paper_id": paper_id, "input_fingerprint": owner.service.store.job(job)["input_fingerprint"], "engine_ready": owner.service.engine_ready})
                        if parts[4] == "result":
                            report = owner.service.accept_external_result(paper_id, data)
                            return self.reply(201, {"report_id": report["report_id"], "warnings": report["warnings"]})
                    return self.reply(404, {"error": "接口不存在"})
                except KeyError:
                    return self.reply(404, {"error": "关联对象不存在"})
                except (ValueError, TypeError):
                    return self.reply(422, {"error": "输入格式或证据定位不合法"})
                except Exception:
                    return self.reply(500, {"error": "请求出现异常；未输出通过结论"})

            def import_package(self, data):
                import base64
                files = data.get("files", [])
                if not isinstance(files, list) or not 1 <= len(files) <= 40:
                    raise ValueError("文件数量不合法")
                if sum(f.get("role") == "manuscript" for f in files) != 1:
                    raise ValueError("每个包必须恰好包含一份正文")
                folder = owner.service.store.resolve("incoming/" + uuid.uuid4().hex)
                folder.mkdir(parents=True)
                sources = []
                for n, f in enumerate(files):
                    # No local path or arbitrary download URL is accepted by the public API.
                    from .storage import safe_name
                    name = safe_name(f["name"])
                    path = folder / (str(n) + "_" + name)
                    content = base64.b64decode(f["content_base64"], validate=True)
                    if not content:
                        raise ValueError("文件为空")
                    path.write_bytes(content)
                    sources.append((f["role"], path))
                main = next(p for role, p in sources if role == "manuscript")
                paper_id = owner.service.store.import_paper(main, [p for role, p in sources if role != "manuscript"])
                job_id = owner.service.enqueue(paper_id) if data.get("enqueue", True) else None
                return self.reply(201, {"paper_id": paper_id, "job_id": job_id, "input_fingerprint": owner.service.store.job(job_id)["input_fingerprint"] if job_id else None, "engine_ready": owner.service.engine_ready})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, name="optional-local-api", daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
