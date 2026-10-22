import copy,json,tempfile,unittest
from pathlib import Path
from reportlab.pdfgen import canvas
from desktop_app.storage import Store
from desktop_app.evidence_completion import EvidenceCompletion
from desktop_app.second_pass import decide,IndependentReview
from desktop_app.models_client import ModelError
from desktop_app.risk_assessment import assessment
from desktop_app.reports import generate_report

class EvidenceCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        pdf=self.root/'paper.pdf';c=canvas.Canvas(str(pdf));c.drawString(30,700,'Methods explicitly state a shared control for both groups.');c.save()
        self.store=Store(self.root/'library');self.pid=self.store.import_paper(pdf);self.paper=self.store.paper(self.pid)
        self.artifact=self.store.artifacts(self.pid)[0]
        self.segments={'s1':{'artifact_id':self.artifact['id'],'page':1,'text':'Methods explicitly state a shared control for both groups.'}}
        self.candidate={'candidate_id':'C0001','title':'Shared control candidate','kind':'text','observation':'Synthetic test only.','anchors':[{'artifact_id':self.artifact['id'],'page':1}]}
        self.prior={'candidate_id':'C0001','action':'needs_context','reason':'Initial decision needs source context.','decision_validated':False,'evidence':[]}
        self.folder=self.root/'review';self.folder.mkdir()
        self.profile={'enable_model':True,'base_url':'https://example.invalid','text_model':'test','max_model_calls':1}
    def tearDown(self):self.tmp.cleanup()
    def answer(self,action='drop',code='normal_reuse'):
        return {'candidate_id':'C0001','action':action,'reason_code':code,'reason':'The original methods explicitly state the shared control for both groups.','evidence':[{'segment_id':'s1','quote':self.segments['s1']['text']}]}
    def run_completion(self,client,prior=None):
        audit={'decisions':[copy.deepcopy(prior or self.prior)]};calls=[]
        result=EvidenceCompletion(self.store,self.paper,self.profile,self.segments,self.folder,client,lambda *_:None,lambda:False).run([self.candidate],audit['decisions'],'TEST',calls,audit)
        return result,calls,audit
    def test_repaired_output_gets_a_new_client_and_source_bound_decision(self):
        outer=self;created=[]
        class Client:
            def __init__(self,*_):created.append(1)
            def chat(self,system,payload,*_):
                p=json.loads(payload);assert p['source_segments'];assert len(p['candidates'])==1
                return ({'decisions':[]} if len(created)==1 else {'decisions':[outer.answer()]}),{}
        result,calls,audit=self.run_completion(Client)
        self.assertEqual(len(created),2);self.assertEqual(result[0]['resolution'],'excluded')
        self.assertEqual([c['stage'] for c in calls],['evidence_completion','review_repair'])
        self.assertNotEqual(calls[0]['input_hash'],calls[1]['input_hash'])
        self.assertEqual(audit['initial_decisions'][0],self.prior)
    def test_valid_evidence_insufficiency_is_a_conclusion_and_does_not_loop(self):
        outer=self
        class Client:
            def __init__(self,*_):pass
            def chat(self,*_):return {'decisions':[outer.answer('needs_context','insufficient_context')]},{}
        result,calls,_=self.run_completion(Client)
        self.assertEqual(len(calls),1);self.assertEqual(result[0]['resolution'],'insufficient_evidence')
        self.assertTrue(result[0]['decision_validated'])
    def test_transport_failure_is_technical_error_not_paper_risk(self):
        class Client:
            def __init__(self,*_):pass
            def chat(self,*_):raise ModelError('network_or_timeout_billing_uncertain')
        result,calls,_=self.run_completion(Client)
        self.assertEqual(len(calls),1);self.assertEqual(result[0]['resolution'],'technical_error')
        f={**self.candidate,'review_disposition':'needs_context','severity':'高风险'}
        triage=assessment({'findings':[f],'review_audit':{'decisions':result}},[[]])
        self.assertEqual(triage['counts']['error'],1);self.assertEqual(triage['counts']['high'],0)
    def test_coverage_only_is_not_rejected_for_saying_cannot_determine(self):
        d=self.answer('drop','coverage_only');d['reason']='没有提供原始图片，无法判断像素，属于检查范围缺口，不是论文错误。'
        self.assertEqual(decide([self.candidate],{'decisions':[d]},self.segments,set(),False)[0]['action'],'drop')
        d['reason_code']='normal_reuse'
        self.assertEqual(decide([self.candidate],{'decisions':[d]},self.segments,set(),False)[0]['action'],'needs_context')
    def test_only_explicit_scope_notices_can_be_archived_without_paper_quotes(self):
        d=self.answer('drop','coverage_only');d['evidence']=[]
        f={**self.candidate,'title':'无图像与正文，无法审查图像复用或编辑线索'}
        out=decide([f],{'decisions':[d]},self.segments,set(),False)[0]
        self.assertEqual(out['action'],'drop');self.assertEqual(out['validation_status'],'scope_notice_excluded')
        for changes in [{'title':'发现局部复制，但缺少原图无法核对'},{'kind':'image'},{'observation':'发现两个实验存在重复区域，需要原图核对。'}]:
            self.assertEqual(decide([{**f,**changes}],{'decisions':[d]},self.segments,set(),False)[0]['action'],'needs_context')
    def test_full_cells_and_headers_replace_truncated_preview(self):
        text='GeneLongList_'+'ABCDEFGH'*35
        source=self.root/'table.csv';source.write_text('geneID,Count\n'+text+',35\nOtherGene,1\n',encoding='utf-8')
        self.pid=self.store.import_paper(self.root/'paper.pdf',[source]);self.paper=self.store.paper(self.pid)
        a=next(a for a in self.store.artifacts(self.pid) if a['name']=='table.csv')
        f={**self.candidate,'anchors':[{'artifact_id':a['id'],'sheet':'CSV','quote':'R2: '+text[:80]}]}
        segment={**self.segments,'old':{'artifact_id':a['id'],'sheet':'CSV','row_start':1,'text':'R1: geneID | Count\nR2: '+text[:80]}}
        runner=EvidenceCompletion(self.store,self.paper,self.profile,segment,self.folder,None,lambda *_:None,lambda:False)
        visible,meta=runner.context(f)
        joined='\n'.join(v['text'] for v in visible.values())
        self.assertIn(text,joined);self.assertIn('geneID | Count',joined);self.assertNotIn('old',visible)
        self.assertEqual(meta['resolved_anchors'][0]['cells'],[2,2,1,2])
    def test_tampered_source_cannot_be_used_for_completion(self):
        self.store.resolve(self.artifact['relative_path']).write_bytes(b'tampered')
        class Never:
            def __init__(self,*_):raise AssertionError('No request for changed material')
        result,calls,_=self.run_completion(Never)
        self.assertEqual(calls,[]);self.assertEqual(result[0]['resolution'],'technical_error')
    def test_full_pipeline_preserves_first_review_and_finishes_supplemental_review(self):
        outer=self;received=[]
        class Client:
            def __init__(self,*_):pass
            def chat(self,system,payload,*_):
                received.append(json.loads(payload))
                return {'decisions':[outer.answer('needs_context','insufficient_context') if len(received)==1 else outer.answer()]},{}
        calls=[]
        findings,audit=IndependentReview(self.store,Client).run(self.paper,self.profile,'TEST',self.segments,[self.candidate],calls)
        self.assertEqual(findings,[]);self.assertEqual(audit['counts']['drop'],1)
        self.assertEqual(audit['initial_decisions'][0]['action'],'needs_context')
        self.assertEqual(audit['supplemental_reviews'][0]['final_decision']['action'],'drop')
    def test_report_uses_specific_conclusions_and_no_unfinished_label(self):
        d=self.answer('needs_context','insufficient_context');d['decision_validated']=True
        f={**self.candidate,'review_disposition':'needs_context'}
        result={'engine_id':'synthetic-completion','findings':[f],'checks':[{'name':'Synthetic test','status':'no_signal','reason':'Test scope only.'}],'review_audit':{'decisions':[d],'candidates':[self.candidate]}}
        report=generate_report(self.store,self.pid,result)
        text=(report['directory']/'report.md').read_text(encoding='utf-8')
        self.assertNotIn('未完成',text);self.assertIn('证据不足',text);self.assertIn('检查异常',text)

if __name__=='__main__':unittest.main()
