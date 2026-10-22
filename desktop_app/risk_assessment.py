"""Evidence-based report triage. Priorities describe leads, never misconduct verdicts."""
from __future__ import annotations
import copy,re

CATALOGUE = [
    ('image_reuse','图片复用与局部复制','同图复制、裁剪、旋转、镜像或跨图相似区域；核对实验分组、图注、共用对照与原图。'),
    ('image_editing','图片拼接与编辑线索','接缝、背景、擦除等需原始图像或可靠版本对照；当前不以噪声或压缩差异单独作伪造判断。'),
    ('table_pattern','附件表格异常与数据生成线索','重复数据行或数据块、固定增量、确定线性关系、尾数集中；先排除量化、排序、单位换算及派生数据。'),
    ('numeric_consistency','数值与计算矛盾','正文、图表、附件间的数值冲突，可重算的公式或统计量不一致。'),
    ('statistics','统计设计与结果解释','样本量、独立重复、检验方法、分类阈值、多重比较及结论支撑。'),
    ('methods','方法与材料对应','实验条件、试剂、图号、样本标识及结果与方法对应。'),
    ('publication','发表与研究声明','伦理、数据可用性、作者及发表状态线索；外部状态以实际查询范围为准。'),
]
LABELS=dict((k,label) for k,label,_ in CATALOGUE)
PATTERNS={'duplicate_rows','duplicate_table_window','duplicate_values','constant_increment','horizontal_increment','affine_columns','terminal_digit'}
WEAK={'terminal_digit','duplicate_values','constant_increment','horizontal_increment','affine_columns'}
GROUPS={'high':'高风险线索','review':'需要复核的事项','insufficient':'证据不足','error':'检查异常'}

def category(f):
    rule=f.get('rule_id','');text=' '.join(str(f.get(k,'')) for k in ['title','observation','dimension'])
    if rule in PATTERNS:return 'table_pattern'
    if rule=='p_value_range':return 'numeric_consistency'
    if re.search('图像编辑|图片拼接|擦除|剪接|image.edit|splic',str(f.get('title','')),re.I) and rule!='image_reuse':return 'image_editing'
    if f.get('kind')=='image' or rule=='image_reuse':return 'image_reuse'
    for scope in [str(f.get('title','')),text]:
        if re.search('图像编辑|图片拼接|擦除|剪接|image.edit|splic',scope,re.I):return 'image_editing'
        if re.search('尾数|增量|仿射|重复.*(?:数值|数据|行|块)|数字.*(?:伪造|生成)|fabricat',scope,re.I):return 'table_pattern'
        if re.search('计算|公式|折算|符号|比值|log2|数值.*(?:矛盾|不一致)',scope,re.I):return 'numeric_consistency'
        if re.search('样本量|统计|重复测量|阈值|p.?value|NoChange|独立重复',scope,re.I):return 'statistics'
        if re.search('作者|撤稿|伦理|声明|发表|利益冲突|数据可用',scope,re.I):return 'publication'
    return 'methods'

def _locations(items):
    return {(v.get('artifact_id'),v.get('page'),v.get('paragraph'),v.get('sheet'),v.get('row_start'),v.get('quote')) for v in items if isinstance(v,dict)}

def assessment(result,evidence):
    audit=result.get('review_audit',{})
    decisions={d.get('candidate_id'):d for d in audit.get('decisions',[]) if isinstance(d,dict) and isinstance(d.get('candidate_id'),str)}
    rows=[]
    for i,f in enumerate(result['findings']):
        cid=f.get('candidate_id');d=decisions.get(cid,{}) if isinstance(cid,str) else {}
        reviewed=f.get('review_disposition')=='keep' and d.get('action')=='keep' and d.get('decision_validated') is True and bool(d.get('evidence')) and isinstance(d.get('impact'),str) and len(d['impact'].strip())>=8
        kind=category(f)
        group='review' if reviewed else 'insufficient' if d.get('action')=='needs_context' and d.get('decision_validated') is True else 'error'
        basis=d.get('validated_risk_basis',{});basis=basis if isinstance(basis,dict) else {}
        conflict=basis.get('conflict_evidence',[]);conflict=[v for v in conflict if isinstance(v,dict)] if isinstance(conflict,list) else []
        supported_conflict=len(_locations(conflict))>=2 and isinstance(basis.get('reason'),str) and len(basis['reason'].strip())>=12
        context=basis.get('context',{});context=context if isinstance(context,dict) else {}
        checked=set(context.get('alternatives_checked',[])) if isinstance(context.get('alternatives_checked'),list) and all(isinstance(v,str) for v in context['alternatives_checked']) else set()
        left,right=context.get('left_label',''),context.get('right_label','')
        labels_bound=isinstance(left,str) and isinstance(right,str) and len(left.strip())>=2 and len(right.strip())>=2 and left!=right and len(conflict)>=2 and left in conflict[0].get('quote','') and right in conflict[1].get('quote','')
        explicit_scope=context.get('claim') in {'distinct_experiments','independent_measurements','same_quantity_same_unit'} and labels_bound and {'shared_control','schematic','derived_or_source_copy'}.issubset(checked)
        captured=evidence[i] if i<len(evidence) else []
        has_pixels=len(captured)>=2 and all(v.get('status')=='captured' for v in captured[:2])
        batches=[b for b in audit.get('batches',[]) if f.get('candidate_id') in b.get('candidate_ids',[])]
        visual=any(sum(v.get('candidate_id')==f.get('candidate_id') for v in b.get('image_labels',[]))>=2 for b in batches)
        metrics=f.get('metrics',{});metrics=metrics if isinstance(metrics,dict) else {}
        geometric=metrics.get('exact_pixels') is True or (isinstance(metrics.get('inliers'),(int,float)) and metrics['inliers']>=20 and isinstance(metrics.get('pixel_correlation'),(int,float)) and metrics['pixel_correlation']>=.9)
        verified_calc=d.get('validation_status')=='calculation_discrepancy_supported' and any(v.get('matches') is False for v in d.get('calculation_records',[]))
        if reviewed and supported_conflict and explicit_scope and basis.get('priority')=='high':
            if kind=='image_reuse' and context.get('claim')=='distinct_experiments' and has_pixels and visual and geometric:group='high'
            elif kind=='table_pattern' and context.get('claim')=='independent_measurements' and f.get('rule_id') in {'duplicate_rows','duplicate_table_window'}:group='high'
            elif kind=='numeric_consistency' and context.get('claim')=='same_quantity_same_unit' and verified_calc:group='high'
        if group=='high':reason='独立复核保留并满足优先核验门槛：'+basis['reason']+' 仍需原始记录确认。'
        elif group=='review':reason='独立复核已保留该事项；当前证据尚不满足高风险门槛。'+str(d.get('reason',''))
        elif group=='insufficient':reason='现有证据不足以确认或排除该疑点。'+str(d.get('reason') or f.get('review_reason') or '请提供对应原始记录或明确的分析定义。')
        else:reason='本项检查异常，不作为论文问题结论。'+str(d.get('reason') or f.get('review_reason') or '没有可验证的独立复核决定，请核对模型连接与服务输出。')
        if group!='high' and f.get('rule_id') in WEAK:reason+=' 尾数、重复值或数列关系本身不能证明数据虚构。'
        rows.append({'index':i,'candidate_id':f.get('candidate_id',f'F{i+1:04}'),'category':kind,'category_label':LABELS[kind],'group':group,'group_label':GROUPS[group],'title':f['title'],'basis':reason,'next_step':f.get('recommendation','请结合来源原件核对。')})
    rows.sort(key=lambda r:({'high':0,'review':1,'insufficient':2,'error':3}[r['group']],r['index']))
    counts={k:sum(v['group']==k for v in rows) for k in GROUPS}
    catalogue=[{'id':k,'name':label,'focus':focus,**{group:sum(v['category']==k and v['group']==group for v in rows) for group in GROUPS}} for k,label,focus in CATALOGUE]
    gaps=[copy.deepcopy(c) for c in result.get('checks',[]) if c.get('status') in {'insufficient_data','needs_mapping','unavailable','failed'}]
    return {'version':'risk-triage-2','counts':counts,'catalogue':catalogue,'items':rows,'coverage_gaps':gaps,'notice':'高风险表示优先核验的证据线索，不是伪造或学术不端认定；零项不表示已排除该类风险。证据不足、检查异常及材料缺口均不计为已支持的问题。'}
