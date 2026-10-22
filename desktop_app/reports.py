from __future__ import annotations

import json
import math
import re
import threading
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import pypdfium2 as pdfium
from PIL import Image as PILImage, ImageDraw, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, KeepTogether

from .storage import now, sha256
from .risk_assessment import assessment

PDF_LOCK = threading.RLock()
CHECK_LABELS={"signal":"有待复核线索","no_signal":"本项范围内未触发线索","not_applicable":"本轮不适用","needs_mapping":"需确认变量/适用前提","insufficient_data":"材料或上下文不足","unavailable":"尚未启用或未接入","failed":"执行失败","cancelled":"已取消"}


def plain(value):
    return str(value or "").replace("\x00", "")[:12000]


def md_text(value):
    return plain(value).replace("<", "&lt;").replace(">", "&gt;").replace("[", "\\[").replace("]", "\\]")


def source_location(item):
    if "sheet" in item["anchor"]:
        return "工作表 " + str(item["anchor"]["sheet"]) + " / " + str(item["anchor"].get("cells", "仅引文片段，未定位单元格"))
    if "page" not in item["anchor"]:
        return ""
    page = item["anchor"]["page"]
    return f"PDF第{page}页" if Path(item["source_name"]).suffix.lower() == ".pdf" else f"图像第{page}帧"


def validate_result(result, artifacts):
    if not isinstance(result, dict) or not isinstance(result.get("engine_id"), str):
        raise ValueError("查验结果必须说明引擎来源")
    if not isinstance(result.get("findings"), list) or not isinstance(result.get("checks"), list) or not result["checks"]:
        raise ValueError("查验结果需包含问题列表及实际检查范围，不能用空结果冒充通过")
    if len(result["findings"]) > 300:
        raise ValueError("单篇问题数超出当前导出上限")
    if not isinstance(result.get("limitations", []), list) or any(not isinstance(v, str) for v in result.get("limitations", [])):
        raise ValueError("报告限制格式错误")
    for check in result["checks"]:
        if not isinstance(check, dict) or not isinstance(check.get("status"), str):
            raise ValueError("必须逐项说明实际检查状态")
    allowed = {item["id"] for item in artifacts}
    for finding in result["findings"]:
        if not isinstance(finding, dict) or not finding.get("title"):
            raise ValueError("问题缺少标题")
        for field in ["title", "severity", "observation", "alternative_explanation", "recommendation"]:
            if field in finding and not isinstance(finding[field], str):
                raise ValueError("问题描述必须为文字")
        anchors = finding.get("anchors", [])
        if not isinstance(anchors, list) or len(anchors) > 6:
            raise ValueError("问题证据定位格式错误或数量超限")
        if finding.get("kind") == "image" and not anchors:
            raise ValueError("图像疑点必须给出来源位置，才能自动截图")
        for anchor in anchors:
            if not isinstance(anchor, dict):
                raise ValueError("证据定位格式错误")
            if anchor.get("artifact_id") not in allowed:
                raise ValueError("证据引用了不属于该论文的文件")
            if "page" in anchor and (isinstance(anchor["page"], bool) or not isinstance(anchor["page"], int) or anchor["page"] < 1):
                raise ValueError("PDF页码必须从1开始")
            if "cells" in anchor:
                cells=anchor["cells"]
                if not isinstance(anchor.get("sheet"),str) or not isinstance(cells,list) or len(cells)!=4 or any(type(v) is not int or v<1 for v in cells) or cells[0]>cells[1] or cells[2]>cells[3]:
                    raise ValueError("表格证据定位不合法")
            box = anchor.get("bbox")
            if box is not None:
                if not isinstance(box, list) or len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in box):
                    raise ValueError("截图区域必须为0到1之间的规范化坐标")
                if not (box[0] < box[2] and box[1] < box[3]):
                    raise ValueError("截图区域宽高必须为正数")
    return result


def screenshot(source, anchor, target, table_cache=None):
    source = Path(source)
    if source.suffix.lower() == ".pdf":
        page_number = anchor.get("page")
        if page_number is None:
            raise ValueError("PDF证据缺少页码，无法自动定位截图")
        with PDF_LOCK:
            doc = pdfium.PdfDocument(str(source))
            try:
                if page_number > len(doc):
                    raise ValueError("证据页码超出PDF范围")
                page = doc[page_number - 1]
                w, h = page.get_size()
                scale = min(2.0, math.sqrt(12_000_000 / max(w * h, 1)))
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().convert("RGB").copy()
                finally:
                    bitmap.close()
                    page.close()
            finally:
                doc.close()
    elif source.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        with PILImage.open(source) as original:
            frame = anchor.get("page", 1)
            if not 1 <= frame <= getattr(original, "n_frames", 1):
                raise ValueError("图像帧号超出范围")
            original.seek(frame - 1)
            if original.width * original.height > 80_000_000:
                raise ValueError("附件图片过大，需提供适合核验的图像")
            image = ImageOps.exif_transpose(original).convert("RGB").copy()
    elif source.suffix.lower() in {".xlsx", ".xls", ".xlsm", ".csv", ".tsv"}:
        if "cells" not in anchor:
            raise ValueError("表格引文尚未定位到可验证单元格，未生成表格截图")
        from .parsers import table_evidence_image
        image = table_evidence_image(source,anchor,table_cache)
    else:
        raise ValueError("该附件格式暂不支持自动截图")
    box = anchor.get("bbox")
    if box:
        width, height = image.size
        px = (int(box[0] * width), int(box[1] * height), math.ceil(box[2] * width), math.ceil(box[3] * height))
        margin = max(8, int(min(width, height) * .025))
        context = (max(0, px[0] - margin), max(0, px[1] - margin), min(width, px[2] + margin), min(height, px[3] + margin))
        image = image.crop(context)
        image.save(target.with_name(target.stem + "_original.png"))
        ImageDraw.Draw(image).rectangle((px[0] - context[0], px[1] - context[1], px[2] - context[0] - 1, px[3] - context[1] - 1), outline="#b85f32", width=max(2, int(image.width / 250)))
    image.thumbnail((2200, 2200))
    image.save(target)
    return {"width": image.width, "height": image.height, "page": anchor.get("page"), "bbox": box, "source_sha256": sha256(source), "screenshot_sha256": sha256(target), "coordinate_space": "worksheet_cells" if "cells" in anchor else "normalized_top_left_display", "scope": "cell_view_not_workbook_screenshot" if "cells" in anchor else "region_with_context" if box else "whole_page_or_image"}


def generate_report(store, paper_id, result, job_id=None):
    paper = store.paper(paper_id)
    if result.get('analysis_manifest'):
        store.verify_analysis(result['analysis_manifest'])
        artifacts=store.analysis_artifacts(result['analysis_manifest'])
    else:artifacts = store.artifacts(paper_id)
    validate_result(result, artifacts)
    files = {item["id"]: item for item in artifacts}
    directory = store.resolve(f"papers/{paper_id}/reports/{uuid.uuid4().hex}")
    images = directory / "images"
    images.mkdir(parents=True)
    evidence = []
    table_cache = {}
    warnings = []
    for index, finding in enumerate(result["findings"], 1):
        group = []
        for number, anchor in enumerate(finding.get("anchors", []), 1):
            source = files[anchor["artifact_id"]]
            target = images / f"issue_{index:03d}_{number:02d}.png"
            record = {"artifact_id": source["id"], "source_name": source["name"], "anchor": anchor}
            try:
                if sha256(store.resolve(source["relative_path"])) != source["sha256"]:
                    raise ValueError("材料文件已改变，不能对旧版本结果截图")
                record.update(screenshot(store.resolve(source["relative_path"]), anchor, target,table_cache))
                record["image"] = str(target.relative_to(directory)).replace("\\", "/")
                record["status"] = "captured"
            except Exception as exc:
                record.update(status="unavailable", reason=plain(exc))
                warnings.append(f"问题{index}证据{number}：{plain(exc)}")
            group.append(record)
        evidence.append(group)
    triage=assessment(result,evidence)
    (directory/'risk-assessment.json').write_text(json.dumps(triage,ensure_ascii=False,indent=2),encoding='utf-8')
    (directory/'checks.json').write_text(json.dumps(result['checks'],ensure_ascii=False,indent=2),encoding='utf-8')
    counts=triage['counts']
    lines = ["# 论文查验报告", "", f"论文：{md_text(paper['title'])}", f"生成时间：{now()}", f"查验来源：{md_text(result['engine_id'])}", "", "> 报告供核对论文和原始记录使用，不构成学术不端认定。", "", "## 结果摘要", "", md_text(result.get("summary", "")), "", f"高风险线索 {counts['high']} 项；需要复核 {counts['review']} 项；证据不足 {counts['insufficient']} 项；检查异常 {counts['error']} 项。",triage['notice'], "当前范围：以实际检查表及材料覆盖说明为准", "",'## 风险类别与本轮项目','','| 风险类别 | 高风险 | 需复核 | 证据不足 | 检查异常 |','| --- | --- | --- | --- | --- |']
    for row in triage['catalogue']:lines += [f"| {row['name']} | {row['high']} | {row['review']} | {row['insufficient']} | {row['error']} |"]
    for group,label in [('high','高风险项目清单'),('review','需要复核的项目清单'),('insufficient','证据不足：具体疑点与所需材料'),('error','检查异常：工具或模型服务记录')]:
        rows=[v for v in triage['items'] if v['group']==group]
        lines+=['','## '+label,'']
        if not rows:lines+=['本轮无列出项目。']
        for row in rows:lines+=['- '+row['candidate_id']+' / '+row['category_label']+' / '+md_text(row['title'])+'：'+md_text(row['basis'])]
    material=result.get('analysis_manifest',{})
    retrieval=material.get('retrieval',{})
    if retrieval:
        lines+=['## 补充材料获取','',md_text('获取状态：'+retrieval.get('status','未说明')+'；本次取得'+str(len(retrieval.get('files',[])))+'份。'),'']
        for note in retrieval.get('notes',[]):lines+=['- '+md_text(note)]
        for a in retrieval.get('files',[]):lines+=['- '+md_text(a['name'])+'　来源：'+md_text(a['source_url'])]
        lines+=['']
    section=None
    for index,row in enumerate(triage['items'],1):
        finding=result['findings'][row['index']];group=evidence[row['index']]
        group_title=row['group_label']
        if group_title!=section:lines+=['## '+group_title,''];section=group_title
        lines += [f"## {index}. {md_text(finding['title'])}", "", f"风险类别：{row['category_label']}；状态：{row['group_label']}；编号：{row['candidate_id']}","",'分级依据：'+md_text(row['basis']), "", "观察与依据：" + md_text(finding.get("observation", "")), "", "可能解释：" + md_text(finding.get("alternative_explanation", "待补充原始材料核验")), "", "建议：" + md_text(finding.get("recommendation", "结合原文和原始材料复核")), ""]
        for item in group:
            page = "，" + source_location(item) if source_location(item) else ""
            lines += [f"来源：{md_text(item['source_name'])}{page}", ""]
            if item["status"] == "captured":
                lines += [f"![问题{index}的来源证据]({item['image']})", "", "表格单元格视图：从原始单元格生成，非工作簿屏幕截图。" if "cells" in item["anchor"] else "截图为原始材料的定位裁图；边框仅用于指示疑点区域。" if item["anchor"].get("bbox") else "仅定位到整页或整幅图，尚无局部疑点框。", ""]
            else:
                lines += ["截图未能生成：" + md_text(item["reason"]), ""]
    lines += ['## 材料缺口与未开展检查','']
    for check in triage['coverage_gaps']:lines+=['- '+md_text(check.get('name','检查'))+'：'+md_text(check.get('reason',''))]
    lines += ["## 实际检查范围", ""]
    for check in result["checks"]:
        lines += ["- " + md_text(check.get("name", check.get("id", "检查"))) + "：" + md_text(CHECK_LABELS.get(check.get("status"),check.get("status", "未说明"))) + "。" + md_text(check.get("reason", ""))]
    lines += ["", "## 限制与待补证", ""]
    for item in result.get("limitations", []) + warnings:
        lines += ["- " + md_text(item)]
    if not result.get("limitations") and not warnings:
        lines += ["本轮未另列限制；仍以实际检查范围和材料为准。"]
    lines += ["", "## 模型调用记录", "", md_text(json.dumps(result.get("model_calls",[]),ensure_ascii=False))]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if result.get('review_audit'):
        audit=result['review_audit']
        (directory/'review-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
        (directory/'first-pass-candidates.json').write_text(json.dumps(audit['candidates'],ensure_ascii=False,indent=2),encoding='utf-8')
        ledger=['# 复核与筛除记录','','首轮候选保留在 first-pass-candidates.json。下列记录区分保留、筛除、合并和待人工核对。','']
        lookup={f['candidate_id']:f for f in audit['candidates']}
        labels={'keep':'保留','drop':'筛除','merge':'合并','needs_context':'待人工核对'}
        for d in audit['decisions']:
            ledger+=['## '+d['candidate_id']+' '+md_text(lookup[d['candidate_id']]['title']),'',labels[d['action']]+'：'+md_text(d['reason']),'']
            if d.get('merge_into'):ledger+=['合并至：'+md_text(d['merge_into']),'']
            if d.get('original_model_decision'):
                prior=d['original_model_decision']
                ledger+=['模型原判断：'+labels.get(prior.get('action'),str(prior.get('action')))+'；'+md_text(prior.get('reason','')),'','本地校验：'+md_text(d.get('validation_reason',d['reason'])),'']
            for calc in d.get('calculation_records',[]):
                display=dict(calc)
                if isinstance(display.get('support_rows'),list):display['support_rows_sample']=display.pop('support_rows')[:12];display['details']='全部支持行保存在review-audit.json'
                ledger+=['重算记录：'+md_text(json.dumps(display,ensure_ascii=False)),'']
            for ev in d.get('evidence',[]):ledger+=['依据：'+md_text(ev.get('quote',''))+'（原文片段 '+md_text(ev.get('segment_id',''))+'）','']
        (directory/'review-decisions.md').write_text('\n'.join(ledger),encoding='utf-8')
        lines+=['','## 首轮与筛除记录','','首轮候选、逐条复核理由及依据见同一资料包中的 review-decisions.md、review-audit.json 和 first-pass-candidates.json。']
        (directory/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if material:(directory/'materials.json').write_text(json.dumps(material,ensure_ascii=False,indent=2),encoding='utf-8')
    _write_pdf(directory / "report.pdf", paper, result, evidence, warnings,triage)
    files=["report.md", "report.pdf", "images/", "evidence.json",'risk-assessment.json','checks.json']
    files += [name for name in ["review-audit.json", "first-pass-candidates.json", "review-decisions.md", "materials.json"] if (directory/name).exists()]
    manifest = {"paper_id": paper_id, "job_id": job_id, "created_at": now(), "engine_id": result["engine_id"], "partial": result.get("partial",False), "checks": result["checks"], "model_calls": result.get("model_calls",[]), "evidence": evidence, "warnings": warnings, "files": files, "finding_count": len(result["findings"]),'risk_assessment':triage,'pdf_font':'embedded Microsoft YaHei or SimHei'}
    (directory / "evidence.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report_id = store.add_report(paper_id, directory, len(result["findings"]), "partial" if warnings else "complete", job_id)
    return {"report_id": report_id, "directory": directory, "warnings": warnings}


def _write_pdf(path, paper, result, evidence, warnings, triage=None):
    from .pdf_report import write_pdf
    with PDF_LOCK:
        write_pdf(path,paper,result,evidence,warnings,triage or assessment(result,evidence),CHECK_LABELS)
