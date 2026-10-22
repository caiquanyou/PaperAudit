"""Evidence filtering, source-bound retrieval and failure retention. Synthetic inputs only."""
import copy,io,json,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from reportlab.pdfgen import canvas
from desktop_app.second_pass import decide,global_context,IndependentReview,SYSTEM
from desktop_app.supplements import SupplementFetcher,PublicHttp,RetrievalError,checked_payload,primary_doi
from desktop_app.storage import Store,sha256
from desktop_app.parsers import Parser
from desktop_app.audit_engine import AuditEngine,DIMENSIONS
from desktop_app.service import Service
from desktop_app.reports import generate_report

class TwoStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='paper-audit-v06-');self.root=Path(self.tmp.name)
        self.pdf=self.root/'paper.pdf';c=canvas.Canvas(str(self.pdf));c.drawString(40,750,'Synthetic experimental paper title testing supplementary material association')
        c.drawString(40,710,'DOI: 10.1234/test.5678');c.showPage();c.drawString(40,740,'Methods specify separate groups and a shared schematic diagram.');c.save()
        self.store=Store(self.root/'library');self.pid=self.store.import_paper(self.pdf);self.a=self.store.artifacts(self.pid)[0]
        self.seg={'s1':{'artifact_id':self.a['id'],'page':2,'text':'Methods specify separate groups and a shared schematic diagram.'}}
        self.f={'title':'Repeated schematic','kind':'text','observation':'First round candidate','anchors':[{'artifact_id':self.a['id'],'page':2}],'candidate_id':'C0001'}
    def tearDown(self):self.tmp.cleanup()
    def decision(self,action='drop',code='normal_reuse',cid='C0001'):
        return {'candidate_id':cid,'action':action,'reason_code':code,'reason':'The Methods explicitly describe separate groups and shared schematic reuse.','impact':'Synthetic substantive contradiction affecting the specified experiment.','evidence':[{'segment_id':'s1','quote':self.seg['s1']['text']}]}
    def test_cross_page_context_can_resolve_false_positive(self):
        d=decide([self.f],{'decisions':[self.decision()]},self.seg,set(),True)[0]
        self.assertEqual(d['action'],'drop');self.assertTrue(d['decision_validated'])
    def test_true_concern_is_preserved_with_source(self):
        d=decide([self.f],{'decisions':[self.decision('keep','supported_concern')]},self.seg,set(),True)[0]
        self.assertEqual(d['action'],'keep')
    def test_missing_ids_duplicates_and_invented_quotes_are_pending(self):
        bad=self.decision();bad['evidence'][0]['quote']='A made up statement absent from the source'
        for ds in [[],[self.decision(cid='unknown')],[self.decision(),self.decision()],[bad]]:
            with self.subTest(ds=ds):self.assertEqual(decide([self.f],{'decisions':ds},self.seg,set(),True)[0]['action'],'needs_context')
    def test_images_cannot_be_dropped_without_actual_pixels(self):
        f={**self.f,'kind':'image'}
        self.assertEqual(decide([f],{'decisions':[self.decision()]},self.seg,set(),True)[0]['action'],'needs_context')
        self.assertEqual(decide([f],{'decisions':[self.decision()]},self.seg,{'C0001'},True)[0]['action'],'drop')
    def test_merge_cycles_and_missing_targets_are_not_deleted(self):
        f2={**self.f,'candidate_id':'C0002'};a=self.decision('merge','duplicate');b=self.decision('merge','duplicate','C0002')
        a['merge_into']='C0002';b['merge_into']='C0001'
        self.assertTrue(all(d['action']=='needs_context' for d in decide([self.f,f2],{'decisions':[a,b]},self.seg,set(),True)))
        a['merge_into']='unknown';self.assertEqual(decide([self.f],{'decisions':[a]},self.seg,set(),True)[0]['action'],'needs_context')
    def test_valid_merge_requires_kept_target_and_evidence(self):
        f2={**self.f,'candidate_id':'C0002'};a=self.decision('keep','supported_concern');b=self.decision('merge','duplicate','C0002');b['merge_into']='C0001'
        ds=decide([self.f,f2],{'decisions':[a,b]},self.seg,set(),True)
        self.assertEqual([d['action'] for d in ds],['keep','merge'])
    def test_absence_claim_cannot_survive_truncated_context(self):
        f={**self.f,'title':'未报告实验方法'}
        d=decide([f],{'decisions':[self.decision('keep','supported_concern')]},self.seg,set(),False)[0]
        self.assertEqual(d['action'],'needs_context')
        context,coverage=global_context({'a':{'text':'a'*1000},'z':{'text':'z'*1000}},300)
        self.assertEqual({s['segment_id'] for s in context},{'a','z'});self.assertFalse(coverage['complete'])
    def test_fresh_client_and_audit_keep_excluded_candidate(self):
        received=[];outer=self
        class Client:
            def __init__(self,*_):received.append('new-client')
            def chat(self,system,payload,model,images=()):
                received.append((system,json.loads(payload),model));return {'decisions':[outer.decision()]},{'total_tokens':10}
        calls=[];out,audit=IndependentReview(self.store,Client).run(self.store.paper(self.pid),{'base_url':'https://example.invalid','text_model':'review-test','enable_model':True,'max_model_calls':2},'TEST-ONLY',self.seg,[self.f],calls)
        self.assertEqual(out,[]);self.assertEqual(audit['counts']['drop'],1);self.assertEqual(len(audit['candidates']),1)
        self.assertEqual(received[0],'new-client');self.assertEqual(received[1][0],SYSTEM);self.assertNotIn('messages',received[1][1]);self.assertEqual(calls[0]['stage'],'independent_review')
    def test_failed_review_keeps_candidates_and_ignores_old_quota(self):
        from desktop_app.models_client import ModelError
        class Bad:
            def __init__(self,*_):pass
            def chat(self,*_):raise ModelError('network_or_timeout_billing_uncertain')
        for calls in [[],[{'status':'succeeded'}]]:
            out,audit=IndependentReview(self.store,Bad).run(self.store.paper(self.pid),{'base_url':'https://example.invalid','text_model':'review-test','max_model_calls':1},'TEST-ONLY',self.seg,[self.f],calls)
            self.assertEqual(len(out),1);self.assertEqual(out[0]['review_disposition'],'needs_context');self.assertFalse(audit['complete']);self.assertEqual(calls[-1]['status'],'failed')
    def test_every_batch_runs_past_legacy_one_call_quota(self):
        outer=self
        class Client:
            def __init__(self,*_):pass
            def chat(self,system,payload,*_):
                ids=[f['candidate_id'] for f in json.loads(payload)['candidates']]
                return {'decisions':[outer.decision(cid=cid) for cid in ids]},{}
        candidates=[copy.deepcopy(self.f) for _ in range(14)]
        calls=[{'status':'succeeded'}]
        out,audit=IndependentReview(self.store,Client).run(self.store.paper(self.pid),{'base_url':'https://example.invalid','text_model':'test','max_model_calls':1},'TEST',self.seg,candidates,calls)
        self.assertEqual(len(calls),4)
        self.assertEqual(len(audit['decisions']),14)
        self.assertEqual(audit['counts']['drop'],14)
        self.assertEqual(out,[])

    def test_malformed_first_pass_fields_become_partial_instead_of_crashing(self):
        engine=AuditEngine(self.store)
        candidates=[];notes=[]
        engine.accept_model_findings({'findings':[{'title':'bad','evidence':[{'segment_id':[],'quote':'Methods specify separate groups'}]}]},self.seg,candidates,notes)
        self.assertEqual(candidates,[])
        class Bad:
            def __init__(self,*_):pass
            def chat(self,*_):return {'findings':[],'dimensions':{k:[] for k in DIMENSIONS}},{}
        calls=[];checks=[]
        engine.client_factory=Bad
        engine.model_review(self.store.paper(self.pid),{'base_url':'https://example.invalid','text_model':'test'},'TEST',self.seg,[],checks,[],calls,type('Parsed',(),{'images':[]})())
        self.assertEqual(calls[0]['status'],'failed')
        self.assertEqual(calls[0]['error_code'],'invalid_result_schema')

    def test_first_pass_continues_after_middle_failure_without_quota(self):
        received=[]
        class Client:
            def __init__(self,*_):pass
            def chat(self,system,payload,*_):
                received.append(payload)
                return {'findings':[],'dimensions':{k:[] if len(received)==2 else 'no_signal' for k in DIMENSIONS}},{}
        segments={str(i):{'artifact_id':self.a['id'],'page':i+1,'text':str(i)*7000} for i in range(3)}
        calls=[];checks=[];notes=[]
        AuditEngine(self.store,client_factory=Client).model_review(self.store.paper(self.pid),{'base_url':'https://example.invalid','text_model':'test','max_model_calls':1},'TEST',segments,[],checks,notes,calls,type('Parsed',(),{'images':[]})())
        self.assertEqual(len(received),3)
        self.assertEqual([c['status'] for c in calls],['succeeded','failed','succeeded'])
        self.assertTrue(any('2/3' in c['reason'] and c['status']=='failed' for c in checks))

    def test_display_limit_never_skips_reviewed_candidates(self):
        outer=self
        class Client:
            def __init__(self,*_):pass
            def chat(self,system,payload,*_):
                ids=[f['candidate_id'] for f in json.loads(payload)['candidates']]
                return {'decisions':[outer.decision('keep','supported_concern',cid) for cid in ids]},{}
        candidates=[copy.deepcopy(self.f) for _ in range(245)]
        calls=[]
        out,audit=IndependentReview(self.store,Client).run(self.store.paper(self.pid),{'base_url':'https://example.invalid','text_model':'test','max_model_calls':1},'TEST',self.seg,candidates,calls)
        self.assertEqual(len(calls),41)
        self.assertEqual(len(audit['decisions']),245)
        self.assertEqual(audit['counts']['keep'],245)
        self.assertEqual(len(out),240)
        self.assertEqual(audit['display_omitted'],5)

    def test_table_evidence_cache_reads_workbook_once_per_report(self):
        from desktop_app.parsers import read_tables,table_evidence_image
        source=self.root/'sample.csv';source.write_text('sample,value\nA,1\nB,2',encoding='utf-8')
        cache={}
        with patch('desktop_app.parsers.read_tables',wraps=read_tables) as reader:
            table_evidence_image(source,{'sheet':'CSV','cells':[2,2,1,2]},cache)
            table_evidence_image(source,{'sheet':'CSV','cells':[3,3,1,2]},cache)
            self.assertEqual(reader.call_count,1)
        self.assertEqual(len(cache),1)

    def test_retrieval_binding_retains_queued_fingerprint(self):
        jobid,_=self.store.enqueue(self.pid);job=self.store.job(jobid);fp=job['input_fingerprint']
        child=self.store.register_download(self.a,'supp.csv',b'group,value\nA,1\n',{'doi':'10.1234/test.5678','source_url':'https://example.org/supp.csv'})
        manifest=self.store.freeze_analysis(job,[self.a,child],{'status':'downloaded','files':[{'name':'supp.csv'}]})
        self.assertEqual(self.store.fingerprint(self.pid),fp);self.assertEqual(self.store.job(jobid)['input_fingerprint'],fp)
        self.assertNotEqual(manifest['analysis_input_fingerprint'],fp)
        parsed=Parser(self.store).parse(self.store.analysis_artifacts(manifest));self.assertTrue(any(t['artifact_id']==child['id'] for t in parsed.tables))
        self.store.resolve(child['relative_path']).write_bytes(b'tampered')
        with self.assertRaises(ValueError):self.store.verify_analysis(manifest)
    def test_old_analysis_cannot_reference_later_download(self):
        jid,_=self.store.enqueue(self.pid);manifest=self.store.freeze_analysis(self.store.job(jid),[self.a],{})
        later=self.store.register_download(self.a,'later.csv',b'x,y\nA,2',{})
        self.assertNotIn(later['id'],{a['id'] for a in self.store.analysis_artifacts(manifest)})
        result={'engine_id':'test','checks':[{'status':'no_signal'}],'findings':[{'title':'future','kind':'text','anchors':[{'artifact_id':later['id']}]}],'analysis_manifest':manifest}
        with self.assertRaises(ValueError):generate_report(self.store,self.pid,result,jid)
    def fake_http(self,wrong=False,unsafe=False):
        data=b'sample,value\nA,1\n';stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('../supp.csv' if unsafe else 'supp.csv',data)
        rootdoi='10.5555/wrong' if wrong else '10.1234/test.5678'
        class Http:
            def get(self,url,**_):
                if 'search?' in url:raw=json.dumps({'resultList':{'result':[{'doi':'10.1234/test.5678','title':'Synthetic experimental paper title testing supplementary material association','pmcid':'PMC1234','isOpenAccess':'Y','hasSuppl':'Y'}]}}).encode()
                elif 'fullTextXML' in url:raw=f'<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta><article-id pub-id-type="doi">{rootdoi}</article-id></article-meta></front><back><ref-list><article-id pub-id-type="doi">10.1234/test.5678</article-id></ref-list></back><supplementary-material><media xlink:href="supp.csv"/></supplementary-material></article>'.encode()
                elif 'supplementaryFiles' in url:raw=stream.getvalue()
                else:raise RetrievalError('no publisher fallback in synthetic test')
                return raw,url,'application/octet-stream'
        return Http()
    def test_exact_doi_download_and_wrong_front_identity(self):
        files,record=SupplementFetcher(self.store,self.fake_http()).fetch([self.a]);self.assertEqual(len(files),1);self.assertEqual(record['status'],'downloaded')
        files,record=SupplementFetcher(self.store,self.fake_http(wrong=True)).fetch([self.a]);self.assertEqual(files,[]);self.assertIn('DOI不匹配',' '.join(record['notes']))
    def test_archive_traversal_and_html_attachment_rejected(self):
        files,record=SupplementFetcher(self.store,self.fake_http(unsafe=True)).fetch([self.a]);self.assertEqual(files,[])
        with self.assertRaises(RetrievalError):checked_payload('supp.docx',b'<!doctype html>login')
    def test_private_network_refused_and_doi_ambiguity(self):
        for url in ['https://127.0.0.1/file.pdf','http://example.org/file.pdf','https://user:pass@example.org/s.pdf']:
            with self.assertRaises(RetrievalError):PublicHttp().get(url)
        self.assertIsNone(primary_doi('DOI:10.1234/a DOI:10.5678/b'))
        self.assertEqual(primary_doi('(10.1234/abc(test))'),'10.1234/abc(test)')
    def test_docx_embedded_picture_preserves_parent_source(self):
        png=io.BytesIO();Image.new('RGB',(120,120),'green').save(png,format='PNG')
        docx=self.root/'support.docx'
        with zipfile.ZipFile(docx,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="w" xmlns:a="a" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:p><w:t>Figure S1 shared control caption</w:t><a:blip r:embed="rId1"/></w:p></w:document>')
            z.writestr('word/_rels/document.xml.rels','<Relationships><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>')
            z.writestr('word/media/image1.png',png.getvalue())
        pid=self.store.import_paper(self.pdf,[docx]);art=self.store.artifacts(pid);parsed=Parser(self.store).parse(art)
        self.assertEqual(len([r for r in parsed.images if r['kind']=='raster']),1)
        child=next(a for a in self.store.artifacts(pid) if a.get('parent_id'))
        self.assertEqual(child['parent_id'],art[1]['id']);self.assertTrue(any('Figure S1' in s['text'] for s in parsed.segments))

    def test_non_string_model_schema_fields_never_crash_or_exclude(self):
        for field,value in [('action',[]),('reason_code',[]),('merge_into',[]),('reason',{}),('candidate_id',[])]:
            d=self.decision();d[field]=value
            if field=='merge_into':d['action']='merge'
            ds=decide([self.f],{'decisions':[d]},self.seg,set(),True)
            self.assertEqual(ds[0]['action'],'needs_context')
        d=self.decision();d['evidence'][0]['segment_id']=[]
        self.assertEqual(decide([self.f],{'decisions':[d]},self.seg,set(),True)[0]['action'],'needs_context')

    def test_derived_fingerprint_omission_and_later_member_forgery_are_rejected(self):
        jid,_=self.store.enqueue(self.pid);roots=self.store.freeze_analysis(self.store.job(jid),[self.a],{})
        frozen=self.store.freeze_derived(roots,[])
        later=self.store.register_child(self.a,'new.txt',b'new later material')
        forged=copy.deepcopy(frozen);forged['derived_members']=[{k:later[k] for k in ['id','parent_id','sha256','role','name']}]
        import hashlib
        forged['derived_fingerprint']=hashlib.sha256(json.dumps(forged['derived_members'],sort_keys=True).encode()).hexdigest()
        with self.assertRaises(ValueError):self.store.verify_analysis(forged)
        del forged['derived_fingerprint']
        with self.assertRaises(ValueError):self.store.verify_analysis(forged)
        self.assertEqual(self.store.freeze_derived(roots)['derived_members'],[])

    def test_merge_output_keeps_both_anchors_without_mutating_snapshot(self):
        outer=self
        class Client:
            def __init__(self,*_):pass
            def chat(self,*_):
                a=outer.decision('keep','supported_concern');b=outer.decision('merge','duplicate','C0002');b['merge_into']='C0001'
                return {'decisions':[a,b]},{}
        second={**self.f,'title':'second candidate','anchors':[{'artifact_id':self.a['id'],'page':1}]}
        out,audit=IndependentReview(self.store,Client).run(self.store.paper(self.pid),{'base_url':'https://example.invalid','text_model':'test'},'TEST',self.seg,[self.f,second],[])
        self.assertEqual(len(out),1);self.assertEqual(len(out[0]['anchors']),2);self.assertEqual(len(audit['candidates'][0]['anchors']),1)
        from desktop_app.second_pass import digest
        self.assertEqual(digest(audit['candidates']),audit['snapshot_hash'])

    def test_generated_provenance_and_uncertain_exclusions_are_not_evidence(self):
        meta={'s1':{**self.seg['s1'],'source_type':'derived_provenance'}}
        self.assertEqual(decide([self.f],{'decisions':[self.decision()]},meta,set(),True)[0]['action'],'needs_context')
        d=self.decision();d['reason']='缺少原始材料，无法判断是否正常，但可能正常。'
        self.assertEqual(decide([self.f],{'decisions':[d]},self.seg,set(),True)[0]['action'],'needs_context')

    def test_empty_normalized_quote_and_unseen_image_merge_rejected(self):
        d=self.decision();d['evidence'][0]['quote']=' '*10
        self.assertEqual(decide([self.f],{'decisions':[d]},self.seg,set(),True)[0]['action'],'needs_context')
        left={**self.f,'kind':'image'};right={**left,'candidate_id':'C0002'}
        a=self.decision('keep','supported_concern');b=self.decision('merge','duplicate','C0002');b['merge_into']='C0001'
        ds=decide([left,right],{'decisions':[a,b]},self.seg,{'C0001'},True)
        self.assertEqual(ds[1]['action'],'needs_context')

    def test_reference_doi_and_reference_title_are_not_article_identity(self):
        from desktop_app.supplements import title_matches
        text='A different short paper title\nAuthors\nAbstract\nSome text.\nReferences\nSynthetic experimental paper title testing supplementary material association. DOI 10.1234/test.5678'
        self.assertIsNone(primary_doi(text))
        self.assertFalse(title_matches('Synthetic experimental paper title testing supplementary material association',text))

if __name__=='__main__':unittest.main()
