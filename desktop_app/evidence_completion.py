"""Source-bound evidence enrichment and independent adjudication of unresolved leads."""
from __future__ import annotations
import copy, hashlib, json, re
from pathlib import Path
from .models_client import ModelError
from .storage import sha256
from .parsers import TABLE_TYPES, read_tables


def outcome(decision):
    if decision.get('action') in {'drop','merge'}:return 'excluded'
    if decision.get('action')=='keep' and decision.get('decision_validated'):return 'supported_concern'
    if decision.get('decision_validated'):return 'insufficient_evidence'
    return 'technical_error'


class EvidenceCompletion:
    def __init__(self,store,paper,profile,segments,folder,client_factory,progress,cancelled):
        self.store,self.paper,self.profile,self.segments,self.folder=store,paper,profile,segments,folder
        self.client_factory,self.progress,self.cancelled=client_factory,progress,cancelled
        manifest=profile.get('_analysis_manifest')
        records=store.analysis_artifacts(manifest) if manifest else store.artifacts(paper['id'])
        self.artifacts={a['id']:a for a in records};self.tables={}

    def context(self,candidate):
        """Include full implicated cells, headers, adjacent rows and source narratives.

        This deliberately replaces 80-character parser previews for the implicated
        worksheet, rather than treating those previews as the full source cells.
        """
        anchors=candidate.get('anchors',[]);selected={};notes=[];resolved=[];table_keys=set()
        for anchor in anchors:
            aid=anchor.get('artifact_id');artifact=self.artifacts.get(aid)
            if not artifact:notes.append('来源不属于本篇绑定材料。');continue
            path=self.store.resolve(artifact['relative_path'])
            if sha256(path)!=artifact['sha256']:raise ValueError('补证来源哈希已变化')
            if path.suffix.lower() not in TABLE_TYPES:resolved.append(copy.deepcopy(anchor));continue
            if aid not in self.tables:self.tables[aid]=read_tables(path)[0]
            table=next((t for t in self.tables[aid] if t['sheet']==anchor.get('sheet')),None)
            if not table:notes.append('找不到候选指定的工作表。');continue
            rows=table['rows'];table_keys.add((aid,table['sheet']))
            cells=anchor.get('cells');alleged=set()
            if isinstance(cells,list) and len(cells)==4:
                alleged.update(range(cells[0],min(cells[1],cells[0]+15)+1))
                if cells[1]-cells[0]>15:notes.append('候选跨度较大，补证先核对起始16行及引文明确标出的行，不能据此排除其余行。')
            alleged.update(int(v) for v in re.findall(r'\bR(\d+)\s*:',anchor.get('quote','')))
            if not alleged and isinstance(anchor.get('row_start'),int):alleged.add(anchor['row_start'])
            alleged={r for r in alleged if 1<=r<=len(rows)}
            if not alleged:notes.append('候选没有可核对的行号，不能推定单元格位置。')
            wanted=set(range(1,min(8,len(rows))+1))
            for row in alleged:wanted.update(range(max(1,row-4),min(len(rows),row+4)+1))
            token=hashlib.sha256((aid+table['sheet']).encode()).hexdigest()[:14]
            prioritized=list(dict.fromkeys(list(range(1,min(8,len(rows))+1))+sorted(alleged)+sorted(wanted)))
            table_chars=sum(len(s['text']) for s in selected.values())
            for row in prioritized:
                text=f'R{row}: '+' | '.join(str(v) for v in rows[row-1])
                # An overlong cell is disclosed; it is never silently ellipsized.
                if len(text)>32000:notes.append(f'{table["sheet"]} R{row}整行超过上下文长度，需原表核对。');continue
                if table_chars+len(text)>100000:notes.append(f'{table["sheet"]} R{row}未纳入本轮上下文，需原表核对。');continue
                sid=f'fullcell_{token}_{row}'
                selected[sid]={'segment_id':sid,'artifact_id':aid,'sheet':table['sheet'],'row_start':row,'text':text,'cell_text_complete':True}
                table_chars+=len(text)
            for row in sorted(alleged)[:4]:
                ncols=len(rows[row-1])
                c0,c1=(cells[2],cells[3]) if isinstance(cells,list) and len(cells)==4 else (1,ncols)
                for start in range(c0,min(c1,ncols)+1,8):
                    resolved.append({'artifact_id':aid,'sheet':table['sheet'],'cells':[row,row,start,min(start+7,c1,ncols)],'quote':f'R{row}: '+' | '.join(str(v) for v in rows[row-1])})
        narrative=[(sid,s) for sid,s in self.segments.items() if s.get('artifact_id') in self.artifacts and 'sheet' not in s and s.get('source_type')!='derived_provenance']
        words=set(re.findall(r'[A-Za-z][A-Za-z0-9_-]{3,}',candidate.get('title','')+' '+candidate.get('observation','')))
        def rank(pair):
            _,s=pair;anchored=any(s.get('artifact_id')==a.get('artifact_id') and (abs(s.get('page',-1000)-a['page'])<=1 if isinstance(a.get('page'),int) else abs(s.get('paragraph',-1000)-a['paragraph'])<=4 if isinstance(a.get('paragraph'),int) else False) for a in anchors)
            return (int(anchored),sum(w.casefold() in s['text'].casefold() for w in words))
        used=sum(len(s['text']) for s in selected.values());omitted=[]
        for sid,s in sorted(narrative,key=rank,reverse=True):
            if used+len(s['text'])>180000:omitted.append(sid);continue
            selected[sid]={'segment_id':sid,**s};used+=len(s['text'])
        for sid,s in self.segments.items():
            if s.get('artifact_id') not in self.artifacts:continue
            if 'sheet' not in s or (s.get('artifact_id'),s.get('sheet')) in table_keys:continue
            if s.get('row_start')!=1:continue
            if used+len(s['text'])>180000:omitted.append(sid);continue
            selected[sid]={'segment_id':sid,**s};used+=len(s['text'])
        notes.extend('本轮补证上下文未纳入原文片段：'+sid for sid in omitted)
        return selected,{'full_cell_rows':sum(s.get('cell_text_complete',False) for s in selected.values()),'source_segments':len(selected),'characters':used,'omitted_segments':omitted,'notes':notes,'resolved_anchors':resolved[:6]}

    def run(self,candidates,decisions,key,calls,audit):
        from .second_pass import SYSTEM,canonical,decide,digest
        lookup={c['candidate_id']:c for c in candidates};final=copy.deepcopy(decisions)
        audit['initial_decisions']=copy.deepcopy(decisions);audit['supplemental_reviews']=[]
        circuit=None
        for i,d in enumerate(final):
            if self.cancelled():raise InterruptedError()
            if d.get('action')!='needs_context':d['resolution']=outcome(d);continue
            candidate=lookup[d['candidate_id']]
            self.progress('evidence_completion',f'补齐来源并再次复核 {d["candidate_id"]}')
            record={'candidate_id':d['candidate_id'],'prior_decision':copy.deepcopy(d),'attempts':[]}
            audit['supplemental_reviews'].append(record)
            try:visible,coverage=self.context(candidate)
            except (ValueError,OSError,KeyError):
                d.update(decision_validated=False,reason='来源补证读取异常，不能据此判断论文。',resolution='technical_error');record['error']='source_read_error';continue
            record['coverage']=coverage
            if coverage['resolved_anchors']:d['resolved_anchors']=coverage['resolved_anchors']
            model=self.profile.get('review_model') or self.profile.get('text_model')
            visual=candidate.get('kind')=='image';images=[];image_ids=set();image_labels=[]
            if visual:
                model=self.profile.get('review_vision_model') or self.profile.get('vision_model')
                from .reports import screenshot
                from PIL import Image
                for pos,a in enumerate(candidate.get('anchors',[])[:2]):
                    artifact=self.artifacts.get(a.get('artifact_id'))
                    if not artifact:continue
                    try:
                        source=self.store.resolve(artifact['relative_path'])
                        if sha256(source)!=artifact['sha256']:raise ValueError('来源哈希已变化')
                        target=self.folder/f'completion-{d["candidate_id"]}-{pos}.png'
                        screenshot(source,a,target)
                        with Image.open(target) as im:
                            im=im.convert('RGB');im.thumbnail((1400,1400));im.save(target)
                        images.append(str(target));image_labels.append({'candidate_id':d['candidate_id'],'image_number':len(images),'anchor':a})
                    except (ValueError,OSError):continue
                if images and len(images)==min(2,len(candidate.get('anchors',[]))):image_ids.add(d['candidate_id'])
            configured=self.profile.get('enable_model',True) and self.profile.get('base_url') and key and model
            if not configured or circuit:
                d.update(decision_validated=False,reason=circuit or ('图像候选需要配置支持图片输入的视觉模型；文本模型不能代替像素核验。' if visual else '独立复核模型或Key没有配置，不能据此判断论文。'),resolution='technical_error');continue
            for repair in (False,True):
                payload=canonical({'candidates':[candidate],'source_segments':list(visible.values()),'images':image_labels,'evidence_completion':coverage,'previous_decision':d,'instructions':'单条重新判断，只使用keep/drop/needs_context，不使用merge。完整单元格优先于首轮截断预览。材料缺口不是论文错误，不能把缺少图片/方法片段当作实质疑点。已被上下文解释的候选应筛除；不确定则明确缺失的具体材料。'+('上次输出未通过校验，请按规定JSON字段修正；不得编造引文或强行保留/筛除。' if repair else '')})
                call={'id':digest({'candidate':d['candidate_id'],'stage':'evidence_completion','repair':repair,'run':self.profile.get('_job_id',str(self.folder))}),'stage':'review_repair' if repair else 'evidence_completion','model':model,'status':'started','candidate_ids':[d['candidate_id']],'image_count':len(images),'input_hash':hashlib.sha256((SYSTEM+payload+canonical([sha256(p) for p in images])+str(model)+str(self.profile.get('_analysis_input_fingerprint'))).encode()).hexdigest()}
                calls.append(call);record['attempts'].append(call['id'])
                try:
                    response,usage=self.client_factory(self.profile,key).chat(SYSTEM,payload,model,images)
                    call['usage']=usage
                    answer=decide([candidate],response,visible,image_ids,not coverage['omitted_segments'] and not coverage['notes'])[0]
                    call['status']='succeeded' if answer.get('decision_validated') else 'invalid_decision'
                    target=self.folder/f'completion-{d["candidate_id"]}-{"repair" if repair else "review"}.json'
                    target.write_text(json.dumps({'response':response,'usage':usage,'coverage':coverage},ensure_ascii=False,indent=2),encoding='utf-8')
                    d.update(answer,model=model,stage='evidence_completion')
                    if answer.get('decision_validated'):break
                except (ModelError,ValueError,TypeError) as exc:
                    code=str(exc) if isinstance(exc,ModelError) else 'invalid_model_configuration'
                    call.update(status='failed',error_code=code,usage=getattr(exc,'usage',{}))
                    d.update(action='needs_context',decision_validated=False,reason='模型服务检查异常：'+code+'；本项不作为论文问题结论。',evidence=[])
                    if code in {'http_401','http_402','http_403','http_429','invalid_model_configuration'}:circuit=d['reason']
                    if code not in {'invalid_model_json','invalid_review_schema','invalid_response','response_incomplete'}:break
            d['resolution']=outcome(d)
            if d['resolution']=='technical_error' and d.get('reason','').startswith('复核未给出'):
                d['reason']='已补齐相关来源并请求修正输出，但模型决定仍未通过引文或结构校验；本项记录为检查异常，不计为论文风险。'
            record['final_decision']=copy.deepcopy(d)
            if coverage['resolved_anchors']:d['resolved_anchors']=coverage['resolved_anchors']
            # Keep the original image input ledger so high-priority gates remain auditable.
            if image_labels:audit.setdefault('batches',[]).append({'candidate_ids':[d['candidate_id']],'stage':'evidence_completion','image_labels':image_labels})
            (self.folder/'completion-progress.json').write_text(json.dumps(audit['supplemental_reviews'],ensure_ascii=False,indent=2),encoding='utf-8')
        for d in final:d.setdefault('resolution',outcome(d))
        by_id={d['candidate_id']:d for d in final}
        for record in audit['supplemental_reviews']:record['final_decision']=copy.deepcopy(by_id[record['candidate_id']])
        return final
