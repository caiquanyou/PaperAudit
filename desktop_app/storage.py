from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_root():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PaperAudit"


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(value).name).strip(" .")
    return (value or "file")[:160]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Store:
    def __init__(self, root=None):
        self.root = Path(root or default_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "papers.sqlite3"
        if self.db.is_file():
            with sqlite3.connect(self.db) as existing:
                columns={r[1] for r in existing.execute("PRAGMA table_info(artifacts)")}
                job_columns={r[1] for r in existing.execute('PRAGMA table_info(jobs)')}
                if columns and ('source_json' not in columns or 'analysis_input_fingerprint' not in job_columns):
                    backup=self.root/"backups"/("before-v0.6-"+uuid.uuid4().hex[:12]+".sqlite3")
                    backup.parent.mkdir(exist_ok=True)
                    with sqlite3.connect(backup) as target:
                        existing.backup(target)
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS papers(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
                    role TEXT NOT NULL, name TEXT NOT NULL, relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
                    status TEXT NOT NULL, created_at TEXT NOT NULL, message TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS reports(
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
                    created_at TEXT NOT NULL, relative_dir TEXT NOT NULL,
                    evidence_status TEXT NOT NULL, finding_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(paper_id)
                    WHERE status IN ('queued','running');
            """)
            for table, column, declaration in [("jobs", "input_fingerprint", "TEXT NOT NULL DEFAULT ''"), ("jobs", "analysis_input_fingerprint", "TEXT NOT NULL DEFAULT ''"), ("jobs", "model_profile", "TEXT NOT NULL DEFAULT '{}'"), ("reports", "job_id", "TEXT"), ("artifacts", "parent_id", "TEXT"), ("artifacts", "source_json", "TEXT NOT NULL DEFAULT '{}'" )]:
                if column not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            con.execute("UPDATE jobs SET status='interrupted',message='程序退出时任务出现异常，需重新检查' WHERE status IN ('running','exporting')")
            con.execute("UPDATE papers SET status='interrupted',message='上次任务中断，已保存结果可重新生成报告' WHERE status IN ('running','exporting')")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def resolve(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("文件路径超出材料库")
        return path

    def import_paper(self, manuscript, supplements=()):
        source = Path(manuscript).resolve()
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise ValueError("论文正文必须为PDF文件")
        with source.open("rb") as handle:
            if b"%PDF-" not in handle.read(1024):
                raise ValueError("正文不是有效的PDF文件")
        sources = [("manuscript", source)] + [("supplement", Path(p).resolve()) for p in supplements]
        if any(not p.is_file() for _, p in sources):
            raise ValueError("附件文件不存在")
        paper_id = uuid.uuid4().hex
        dest = self.resolve(f"papers/{paper_id}/files")
        dest.mkdir(parents=True)
        artifacts = []
        for role, path in sources:
            artifact_id = uuid.uuid4().hex
            name = safe_name(path.name)
            target = dest / (artifact_id[:10] + "_" + name)
            pending = target.with_suffix(target.suffix + ".part")
            shutil.copyfile(path, pending)
            os.replace(pending, target)
            artifacts.append((artifact_id, paper_id, role, name, str(target.relative_to(self.root)), sha256(target), target.stat().st_size))
        with self.connect() as con:
            con.execute("INSERT INTO papers VALUES (?,?,?,?,?)", (paper_id, source.stem, now(), "imported", "文件已保存，尚未查验"))
            con.executemany("INSERT INTO artifacts(id,paper_id,role,name,relative_path,sha256,size) VALUES (?,?,?,?,?,?,?)", artifacts)
        return paper_id

    def list_papers(self):
        with self.connect() as con:
            return [dict(row) for row in con.execute("""SELECT p.*,
                (SELECT COUNT(*) FROM artifacts a WHERE a.paper_id=p.id) AS file_count,
                (SELECT COUNT(*) FROM reports r WHERE r.paper_id=p.id) AS report_count
                FROM papers p ORDER BY p.created_at DESC,p.rowid DESC""")]

    def paper(self, paper_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row is None:
            raise KeyError("论文不存在")
        return dict(row)

    def artifacts(self, paper_id):
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM artifacts WHERE paper_id=? ORDER BY rowid", (paper_id,))]

    def report_list(self, paper_id):
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM reports WHERE paper_id=? ORDER BY created_at DESC,rowid DESC", (paper_id,))]

    def progress(self, job_id, stage, message):
        with self.connect() as con:
            job=con.execute("SELECT paper_id FROM jobs WHERE id=? AND status='running'",(job_id,)).fetchone()
            if job:
                text=stage+"："+message
                con.execute("UPDATE jobs SET message=? WHERE id=?",(text,job_id))
                con.execute("UPDATE papers SET message=? WHERE id=?",(text,job[0]))

    def fingerprint(self, paper_id):
        items = [(f["id"], f["role"], f["sha256"]) for f in self.artifacts(paper_id) if not f.get("parent_id")]
        return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()

    def register_child(self, parent, member, content):
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = hashlib.sha256((parent["id"] + member + digest).encode()).hexdigest()[:32]
        name = safe_name(member)
        path = self.resolve(f"papers/{parent['paper_id']}/children/{artifact_id}_{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(path.suffix + ".part")
        pending.write_bytes(content)
        os.replace(pending, path)
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO artifacts(id,paper_id,role,name,relative_path,sha256,size,parent_id) VALUES (?,?,?,?,?,?,?,?)", (artifact_id, parent["paper_id"], "archive_member", member, str(path.relative_to(self.root)), digest, len(content), parent["id"]))
            row = con.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return dict(row)

    def job(self, job_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        return dict(row)

    def register_download(self, parent, name, content, source):
        artifact=self.register_child(parent, 'download/'+name, content)
        with self.connect() as con:
            con.execute("UPDATE artifacts SET role='downloaded_supplement',source_json=? WHERE id=?",(json.dumps(source,ensure_ascii=False),artifact['id']))
        return next(a for a in self.artifacts(parent['paper_id']) if a['id']==artifact['id'])

    def analysis_path(self, job):
        return self.resolve(f"papers/{job['paper_id']}/analysis-inputs/{job['id']}.json")

    def freeze_analysis(self, job, artifacts, retrieval):
        path=self.analysis_path(job)
        if path.is_file():
            manifest=json.loads(path.read_text(encoding='utf-8'))
            self.verify_analysis(manifest)
            return manifest
        if self.fingerprint(job['paper_id'])!=job['input_fingerprint']:raise ValueError('排队材料已变化')
        files=[{k:a[k] for k in ['id','role','name','sha256','size','relative_path']} for a in artifacts]
        digest=hashlib.sha256(json.dumps([(a['id'],a['sha256'],a['role']) for a in files],sort_keys=True).encode()).hexdigest()
        manifest={'job_id':job['id'],'paper_id':job['paper_id'],'input_fingerprint':job['input_fingerprint'],'analysis_input_fingerprint':digest,'files':files,'retrieval':retrieval,'created_at':now()}
        self.verify_analysis(manifest,check_database=False)
        path.parent.mkdir(parents=True,exist_ok=True)
        # One worker owns the job; exclusive creation prevents accidental overwrite.
        with path.open('x',encoding='utf-8') as handle:json.dump(manifest,handle,ensure_ascii=False,indent=2)
        with self.connect() as con:
            con.execute('UPDATE jobs SET analysis_input_fingerprint=? WHERE id=?',(digest,job['id']))
        return manifest

    def verify_analysis(self, manifest, check_database=True):
        job=self.job(manifest['job_id'])
        if manifest['paper_id']!=job['paper_id'] or manifest['input_fingerprint']!=job['input_fingerprint'] or self.fingerprint(job['paper_id'])!=job['input_fingerprint']:raise ValueError('分析材料未绑定当前任务')
        files=manifest['files']
        if check_database:
            frozen=json.loads(self.analysis_path(job).read_text(encoding='utf-8'))
            if frozen['files']!=files or frozen.get('retrieval')!=manifest.get('retrieval'):raise ValueError('结果与冻结分析材料不一致')
        digest=hashlib.sha256(json.dumps([(a['id'],a['sha256'],a['role']) for a in files],sort_keys=True).encode()).hexdigest()
        if digest!=manifest['analysis_input_fingerprint'] or (check_database and digest!=job['analysis_input_fingerprint']):raise ValueError('分析材料指纹不一致')
        actual={a['id']:a for a in self.artifacts(job['paper_id'])}
        for f in files:
            row=actual.get(f['id'])
            if not row or row['sha256']!=f['sha256'] or row['relative_path']!=f['relative_path'] or sha256(self.resolve(row['relative_path']))!=f['sha256']:raise ValueError('分析材料已改变')
        if 'derived_fingerprint' in manifest or 'derived_members' in manifest:
            frozen_path=self.analysis_path(job).with_suffix('.derived.json')
            if not frozen_path.is_file():raise ValueError('派生快照尚未冻结')
            frozen=json.loads(frozen_path.read_text(encoding='utf-8'))
            if not manifest.get('derived_fingerprint') or frozen.get('derived_members')!=manifest.get('derived_members') or frozen.get('derived_fingerprint')!=manifest.get('derived_fingerprint'):raise ValueError('结果与冻结派生材料不一致')
            allowed={f['id'] for f in files}
            for f in manifest['derived_members']:
                row=actual.get(f['id'])
                if not row or row.get('parent_id') not in allowed or row['sha256']!=f['sha256'] or sha256(self.resolve(row['relative_path']))!=f['sha256']:raise ValueError('派生材料不属于本次分析快照')
                allowed.add(row['id'])
            expected=hashlib.sha256(json.dumps(manifest['derived_members'],sort_keys=True).encode()).hexdigest()
            if expected!=manifest.get('derived_fingerprint'):raise ValueError('派生材料指纹不一致')
        return True

    def freeze_derived(self, manifest, produced_ids=None):
        manifest=dict(manifest)
        path=self.analysis_path(self.job(manifest['job_id'])).with_suffix('.derived.json')
        if produced_ids is None and path.is_file():
            previous=json.loads(path.read_text(encoding='utf-8'));self.verify_analysis(previous);return previous
        allowed={f['id'] for f in manifest['files']};members=[]
        actual={a['id']:a for a in self.artifacts(manifest['paper_id'])}
        for aid in dict.fromkeys(produced_ids or []):
            a=actual[aid]
            if a.get('parent_id') in allowed and a['role']!='downloaded_supplement':
                members.append({k:a[k] for k in ['id','parent_id','sha256','role','name']});allowed.add(a['id'])
            else:raise ValueError('解析产物不属于本次材料')
        manifest['derived_members']=members
        manifest['derived_fingerprint']=hashlib.sha256(json.dumps(members,sort_keys=True).encode()).hexdigest()
        if path.is_file():
            previous=json.loads(path.read_text(encoding='utf-8'))
            if previous['derived_fingerprint']!=manifest['derived_fingerprint']:raise ValueError('派生快照不允许覆盖')
        else:
            with path.open('x',encoding='utf-8') as f:json.dump(manifest,f,ensure_ascii=False,indent=2)
        self.verify_analysis(manifest)
        return manifest

    def analysis_artifacts(self, manifest):
        allowed={a['id'] for a in manifest['files']}|{a['id'] for a in manifest.get('derived_members',[])}
        return [a for a in self.artifacts(manifest['paper_id']) if a['id'] in allowed]

    def jobs(self, paper_id):
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM jobs WHERE paper_id=? ORDER BY rowid DESC", (paper_id,))]

    def result_path(self, job):
        return self.resolve(f"papers/{job['paper_id']}/results/{job['id']}.json")

    def save_result(self, job, result):
        target = self.result_path(job)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(".part")
        pending.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(pending, target)

    def enqueue(self, paper_id, profile=None):
        self.paper(paper_id)
        job_id = uuid.uuid4().hex
        with self.connect() as con:
            existing = con.execute("SELECT id FROM jobs WHERE paper_id=? AND status IN ('queued','running')", (paper_id,)).fetchone()
            if existing:
                return existing[0], False
            latest = con.execute("SELECT * FROM jobs WHERE paper_id=? ORDER BY rowid DESC LIMIT 1", (paper_id,)).fetchone()
            if latest and latest["status"] in {"export_failed", "interrupted"} and self.result_path(dict(latest)).is_file():
                con.execute("UPDATE jobs SET status='queued',message='重新生成已保存结果的报告' WHERE id=?", (latest["id"],))
                con.execute("UPDATE papers SET status='queued',message='等待从已保存结果重建报告，不重跑引擎' WHERE id=?", (paper_id,))
                return latest["id"], False
            con.execute("INSERT INTO jobs(id,paper_id,status,created_at,message,input_fingerprint,model_profile) VALUES (?,?,?,?,?,?,?)", (job_id, paper_id, "queued", now(), "", self.fingerprint(paper_id), json.dumps(profile or {}, ensure_ascii=False)))
            con.execute("UPDATE papers SET status='queued',message='等待逐篇检查' WHERE id=?", (paper_id,))
        return job_id, True

    def claim(self):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if job is None:
                return None
            con.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
            con.execute("UPDATE papers SET status='running',message='正在检查这一篇论文' WHERE id=?", (job["paper_id"],))
            return dict(job)

    def finish_job(self, job, status, message=""):
        with self.connect() as con:
            con.execute("UPDATE jobs SET status=?,message=? WHERE id=?", (status, message, job["id"]))
            latest = con.execute("SELECT id FROM jobs WHERE paper_id=? ORDER BY rowid DESC LIMIT 1", (job["paper_id"],)).fetchone()
            if latest and latest[0] == job["id"]:
                con.execute("UPDATE papers SET status=?,message=? WHERE id=?", (status, message, job["paper_id"]))

    def add_report(self, paper_id, directory, count, evidence_status, job_id=None):
        report_id = uuid.uuid4().hex
        with self.connect() as con:
            con.execute("INSERT INTO reports(id,paper_id,created_at,relative_dir,evidence_status,finding_count,job_id) VALUES (?,?,?,?,?,?,?)", (report_id, paper_id, now(), str(Path(directory).relative_to(self.root)), evidence_status, count, job_id))
            latest = con.execute("SELECT id FROM jobs WHERE paper_id=? ORDER BY rowid DESC LIMIT 1", (paper_id,)).fetchone()
            if job_id is None or (latest and latest[0] == job_id):
                con.execute("UPDATE papers SET status='completed',message=? WHERE id=?", (f"已生成报告，列出 {count} 项核对事项或待确认候选", paper_id))
        return report_id

    def setting(self, key, default=None):
        with self.connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.set_settings({key: value})

    def set_settings(self, values):
        with self.connect() as con:
            for key, value in values.items():
                con.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))
