"""Conservative, located table and text operations migrated from the supplied project."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from itertools import combinations

import numpy as np

from .parsers import numeric

NATURAL = re.compile(r"time|dose|day|position|coordinate|index|primer|window|length|编号|序号|时间|浓度|剂量|坐标", re.I)
DERIVED = re.compile(r"normal|ratio|fold|percent|average|mean|log|标准|均值|比值|归一|百分", re.I)


def finding(title, rule, observation, anchors, alternative, level="L1"):
    return {"title": title, "rule_id": rule, "kind": "table" if any("sheet" in a for a in anchors) else "text", "severity": "建议复核", "evidence_level": level, "observation": observation, "alternative_explanation": alternative, "recommendation": "结合原始记录、变量含义与论文图表核对；该线索不构成学术不端认定。", "anchors": anchors, "methods": [rule]}


def table_anchor(table, r0, r1, c0, c1):
    return {"artifact_id": table["artifact_id"], "sheet": table["sheet"], "cells": [max(1,r0), max(1,r1), max(1,c0), max(1,c1)]}


def table_checks(tables):
    findings, checks = [], []
    fingerprints = {}
    for table in tables:
        rows = table["rows"]
        if not rows:
            continue
        sheet = table["sheet"]
        base = {"artifact_id": table["artifact_id"], "sheet": sheet}
        signature = hashlib.sha256(repr(rows).encode()).hexdigest()
        if signature in fingerprints and table.get("source_sha256") and fingerprints[signature].get("source_sha256")==table["source_sha256"] and fingerprints[signature]["sheet"]==sheet:
            # Archive/recovery copies are duplicate material, not independent evidence.
            checks.append({"name": "同内容表去重", "status": "not_applicable", "reason": "与已解析表内容完全相同，未重复计数", **base})
            continue
        if signature in fingerprints:
            previous=fingerprints[signature]
            cells=[1,min(8,len(rows)),1,min(6,len(rows[0]))]
            findings.append(finding("两个表格的解析内容一致","duplicate_table_window",f"本轮读取的{len(rows)}行内容一致；仅说明解析窗口相同，需核对是否声明为同一资料或独立实验。",[{"artifact_id":previous["artifact_id"],"sheet":previous["sheet"],"cells":cells},table_anchor(table,*cells)],"可能为同一数据的不同版本、来源副本或合法重复引用。","L2"))
        fingerprints[signature] = {**base,"source_sha256":table.get("source_sha256")}
        headers = [str(v) for v in rows[0]]
        for cell in table.get("date_cells",[])[:20]:
            ci=cell["column"]-1
            if ci<len(headers) and re.search(r"gene|symbol|基因",headers[ci],re.I):
                findings.append(finding("基因标识列中存在日期类型单元格","gene_date_conversion",f"{sheet} R{cell['row']}C{cell['column']} 被保存为日期 {cell['value']}。",[table_anchor(table,cell['row'],cell['row'],cell['column'],cell['column'])],"可能为真实日期或Excel自动转换；需对照仓储中的原始基因标识。"))
        for ci,header in enumerate(headers):
            if not re.fullmatch(r"(?:sample|subject|patient)(?:[ _-]?id)?|ID|样本(?:编号)?|受试者(?:编号)?",header.strip(),re.I):
                continue
            seen_ids={}
            for ri,row in enumerate(rows[1:],2):
                if ci>=len(row) or str(row[ci]).strip()=="":continue
                ident=str(row[ci]).strip();payload=tuple(str(v) for v in row)
                if ident in seen_ids and seen_ids[ident][1]!=payload:
                    first=seen_ids[ident][0]
                    findings.append(finding("同一标识对应不同记录，需确认复合键","identifier_mapping",f"{sheet} 标识 {ident} 同时出现在第{first}和第{ri}行，其他内容不同。",[table_anchor(table,first,first,1,min(6,len(row))),table_anchor(table,ri,ri,1,min(6,len(row)))],"可能为纵向随访或重复测量；只有核对独立样本定义后才能判断合并是否有误。"))
                    break
                seen_ids.setdefault(ident,(ri,payload))
        seen = {}
        duplicates = []
        error_cells = []
        for ri,row in enumerate(rows, 1):
            normalized = tuple(str(v).strip() for v in row)
            if len([v for v in normalized if v]) >= 3 and sum(numeric(v) is not None for v in row) >= 2:
                if normalized in seen and len(duplicates) < 4:
                    duplicates.append((seen[normalized],ri))
                seen.setdefault(normalized,ri)
            for ci,value in enumerate(row,1):
                if str(value) in {"#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#NUM!", "#N/A"}:
                    error_cells.append((ri,ci,str(value)))
        for first,second in duplicates:
            ncols = min(len(rows[first-1]),7)
            findings.append(finding("存在完全相同的数据行", "duplicate_rows", f"{sheet} 第{first}行与第{second}行内容相同。", [table_anchor(table,first,first,1,ncols),table_anchor(table,second,second,1,ncols)], "可能为重复测量、模板记录或同一资料多次引用；应核对样本及复合标识。", "L2"))
        for ri,ci,value in error_cells[:5]:
            findings.append(finding("表格含显式错误单元格", "formula_error", f"{sheet} R{ri}C{ci} 为 {value}。", [table_anchor(table,ri,ri,ci,ci)], "错误可能位于未用于结果的辅助区域，需要核对引用链。", "L2"))
        if table.get("hidden") or table.get("hidden_ranges"):
            findings.append(finding("存在隐藏的表格区域", "hidden_regions", f"工作表隐藏={table.get('hidden',False)}；隐藏行列：{table.get('hidden_ranges',[])[:12]}。", [table_anchor(table,1,min(8,len(rows)),1,min(5,len(rows[0])))], "隐藏区域常用于中间计算或排版；单独存在并不表示问题。"))
        missing = [v["cell"] for v in table.get("formulas",[]) if not v["cached"]]
        if missing:
            checks.append({"name": "公式缓存", "status": "insufficient_data", "reason": "以下公式无缓存值，未执行公式或当作零：" + ", ".join(missing[:12]), **base})
        horizontal=0
        if not NATURAL.search(" ".join(headers)+" "+sheet):
            for ri,row in enumerate(rows[1:],2):
                entries=[(ci,numeric(v)) for ci,v in enumerate(row) if numeric(v) is not None]
                if len(entries)<6:continue
                diffs=[entries[i][1]-entries[i-1][1] for i in range(1,len(entries))]
                consecutive=all(entries[i][0]==entries[i-1][0]+1 for i in range(1,len(entries)))
                if consecutive and abs(diffs[0])>1e-9 and all(abs(d-diffs[0])<max(1e-9,abs(diffs[0])*1e-7) for d in diffs):
                    c0,c1=entries[0][0]+1,entries[-1][0]+1
                    findings.append(finding("同一行横向数值呈固定增量","horizontal_increment",f"{sheet} 第{ri}行 C{c0}:C{c1} 步长约为{diffs[0]:.8g}。",[table_anchor(table,ri,ri,c0,min(c1,c0+6))],"可能为时间/剂量设计、标准曲线或公式派生，需核对各列实验含义。"))
                    horizontal+=1
                    if horizontal>=4:break
        eligible = 0
        columns = []
        for ci in range(min(30,max(len(r) for r in rows))):
            header = headers[ci] if ci < len(headers) else f"C{ci+1}"
            cells = [(ri+1,row[ci]) for ri,row in enumerate(rows[1:],1) if ci < len(row) and numeric(row[ci]) is not None]
            if len(cells) < 5:
                continue
            values = [numeric(v) for _,v in cells]
            if NATURAL.search(header + " " + sheet):
                checks.append({"name": "连续参数序列", "status": "not_applicable", "reason": f"第{ci+1}列表头/工作表提示预设参数，未作测量异常判断", **base})
                continue
            if not header.strip() or numeric(header) is not None:
                checks.append({"name": "数值变量前提", "status": "needs_mapping", "reason": f"第{ci+1}列无明确变量表头", **base})
                continue
            eligible += 1
            # Runs use consecutive original row indices; missing cells break a run.
            run_start, step = 0, None
            for j in range(1,len(cells)+1):
                diff = values[j]-values[j-1] if j<len(cells) and cells[j][0]==cells[j-1][0]+1 else None
                same = diff is not None and step is not None and abs(diff-step) <= max(1e-10,abs(step)*1e-7)
                if j == 1:
                    step = diff
                    continue
                if same:
                    continue
                if step is not None and abs(step)>1e-10 and j-run_start>=6:
                    r0,r1 = cells[run_start][0], cells[j-1][0]
                    findings.append(finding("测量列存在连续固定增量", "constant_increment", f"{sheet} 第{ci+1}列 R{r0}:R{r1} 步长约为 {step:.8g}。", [table_anchor(table,r0,min(r1,r0+12),ci+1,ci+1)], "可能为标准曲线、公式、预设序列或取整结果，需先确认该列是否独立实验测量。"))
                run_start = max(0,j-1)
                step = diff
            decimals = [(r,str(v).strip()) for r,v in cells if "." in str(v) and "e" not in str(v).lower()]
            if len(decimals)>=30:
                digits = Counter(v[-1] for _,v in decimals if v[-1].isdigit())
                digit,count = digits.most_common(1)[0]
                if count/len(decimals)>=.5:
                    sample = [r for r,v in decimals if v.endswith(digit)][:3]
                    findings.append(finding("测量列显示尾数集中", "terminal_digit", f"{sheet} 第{ci+1}列所解析小数值中，显示末位{digit}占{count}/{len(decimals)}；保留字符串及简单小数格式。", [table_anchor(table,min(sample),min(max(sample),min(sample)+12),ci+1,ci+1)], "可由仪器精度、四舍五入、软件导出或同一变量量化产生；未假设尾数必然均匀。"))
            if len(values)>=30:
                repeated = Counter(v for v in values if not v.is_integer() and abs(v)>1e-8)
                if repeated and repeated.most_common(1)[0][1]>=max(6,len(values)*.4):
                    value,count = repeated.most_common(1)[0]
                    refs = [r for (r,_),v in zip(cells,values) if v==value][:3]
                    findings.append(finding("测量列存在集中重复数值", "duplicate_values", f"第{ci+1}列数值 {value:g} 重复{count}次。", [table_anchor(table,min(refs),min(max(refs),min(refs)+12),ci+1,ci+1)], "同精度量化、检测下限与重复测量可导致集中重复。"))
            if len(values)>=20 and len(set(values))>=12 and not DERIVED.search(header):
                columns.append((ci,header,{r:numeric(v) for r,v in cells}))
        for (ai,ah,av),(bi,bh,bv) in combinations(columns,2):
            common = sorted(av.keys() & bv.keys())
            if len(common)<20:
                continue
            a,b = np.array([av[r] for r in common]),np.array([bv[r] for r in common])
            if a.std()<1e-10 or b.std()<1e-10:
                continue
            slope,intercept = np.polyfit(a,b,1)
            residual = float(np.sqrt(np.mean((b-slope*a-intercept)**2))/b.std())
            if residual<1e-7:
                findings.append(finding("两列存在近乎确定的线性关系", "affine_columns", f"{sheet} {ah}与{bh}，共同{len(common)}行，y≈{slope:.8g}x+{intercept:.8g}，标准化残差={residual:.3g}。", [table_anchor(table,common[0],min(common[0]+10,common[-1]),ai+1,ai+1),table_anchor(table,common[0],min(common[0]+10,common[-1]),bi+1,bi+1)], "单位换算、归一化、标准曲线、技术重复或派生公式可产生该关系，须核对独立重复定义。"))
        checks.append({"name": "表格重复/增量/尾数/仿射与结构", "status": "signal" if any(f["anchors"][0].get("sheet")==sheet and f["anchors"][0]["artifact_id"]==table["artifact_id"] for f in findings) else "no_signal", "reason": f"解析{len(rows)}行，满足初步变量前提的列{eligible}；格式及语义仍需复核", **base})
    checks.append({"name": "Benford", "status": "needs_mapping", "reason": "默认不对混合工作簿或未经生成过程验证的变量应用本福特规则"})
    return findings,checks


def text_checks(segments):
    findings,checks = [],[]
    for segment in segments:
        text = segment["text"]
        pattern=r"\bp\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:(?:[eE]\s*([+−-]?\d+))|(?:\s*[×x·*]\s*10\s*(?:\^|\*\*)?\s*([⁻⁺⁰¹²³⁴⁵⁶⁷⁸⁹−+\-\d]+)))?"
        for match in re.finditer(pattern,text,re.I):
            exponent=match.group(2) or match.group(3)
            tail=text[match.end():].lstrip()
            if exponent is None and re.match(r"[eE×x·*]",tail):
                checks.append({"name":"p值指数表达解析","status":"insufficient_data","reason":"发现不完整或未支持的科学记数法，未把尾部截断后判断越界"})
                continue
            translated=exponent.translate(str.maketrans("⁻⁺⁰¹²³⁴⁵⁶⁷⁸⁹−","-+0123456789-")) if exponent else "0"
            if not re.fullmatch(r"[+-]?\d+",translated) or abs(int(translated))>300:
                continue
            value = float(match.group(1))*10**int(translated)
            if value<0 or value>1:
                anchor = {k:v for k,v in segment.items() if k != "text"}
                anchor["quote"] = text[max(0,match.start()-45):match.end()+45]
                findings.append(finding("提取到范围外的p值表达", "p_value_range", f"原文片段：{anchor['quote']}。提取的p={value}不在0到1内。", [anchor], "可能是PDF排版、字符识别、百分数或符号提取错误，需核对原页。", "L2"))
    joined = "\n".join(s["text"] for s in segments if s.get("page"))
    # Background/reference mentions cannot trigger expensive specialist workflows.
    relevant = bool(re.search(r"(?:we|our|本研究|我们).{0,100}(?:RNA.seq|转录组测序)|(?:RNA.seq|转录组测序).{0,100}(?:performed|generated|samples|本研究|测序)",joined,re.I))
    checks += [{"name": "RNA-seq专项复现", "status": "unavailable" if relevant else "not_applicable", "reason": "正文出现研究实施线索；未配置外部复现执行器，未重跑测序" if relevant else "未识别到本研究实际执行RNA-seq的明确线索，未启动专项；适用性仅为自动初筛"}, {"name": "正文数字范围初筛", "status": "signal" if findings else "no_signal", "reason": "仅核对可抽取文本中的p值范围，未重算统计检验"}]
    return findings,checks
