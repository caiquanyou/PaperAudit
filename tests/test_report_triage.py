import copy,tempfile,unittest,json
from pathlib import Path
from desktop_app.risk_assessment import assessment,category
from desktop_app.reports import _write_pdf

class ReportTriageTests(unittest.TestCase):
    def test_title_topic_precedes_incidental_context_and_rules_remain_explicit(self):
        finding={'title':'统计方法与多重比较未说明','observation':'需要核对附件重复数据是否为技术重复。'}
        self.assertEqual(category(finding),'statistics')
        finding['rule_id']='duplicate_rows'
        self.assertEqual(category(finding),'table_pattern')

    def sample(self,kind='image',rule='image_reuse'):
        f={'candidate_id':'C1','title':'两个实验区域对应','kind':kind,'rule_id':rule,'review_disposition':'keep','metrics':{'exact_pixels':True},'observation':'Synthetic source evidence.','anchors':[]}
        ev=[{'artifact_id':'a','page':1,'quote':'Experiment Alpha is an independent group.'},{'artifact_id':'b','page':2,'quote':'Experiment Beta is an independent group.'}]
        d={'candidate_id':'C1','action':'keep','decision_validated':True,'impact':'Synthetic identity contradiction changes the independent sample interpretation.','evidence':ev,'validated_risk_basis':{'priority':'high','reason':'The explicitly independent samples have the same measured source content.','conflict_evidence':ev,'context':{'claim':'distinct_experiments','left_label':'Alpha','right_label':'Beta','alternatives_checked':['shared_control','schematic','derived_or_source_copy']}}}
        result={'engine_id':'synthetic-test','summary':'Synthetic test only.','findings':[f],'checks':[{'name':'OCR','status':'unavailable','reason':'Not run.'}],'review_audit':{'decisions':[d],'batches':[{'candidate_ids':['C1'],'image_labels':[{'candidate_id':'C1'},{'candidate_id':'C1'}]}]}}
        return result,[[{'status':'captured'},{'status':'captured'}]]

    def test_high_priority_requires_pixels_geometry_and_context(self):
        r,e=self.sample();self.assertEqual(assessment(r,e)['counts']['high'],1)
        for field in ['context','conflict_evidence']:
            x=copy.deepcopy(r);x['review_audit']['decisions'][0]['validated_risk_basis'].pop(field)
            self.assertEqual(assessment(x,e)['counts']['high'],0)
        x=copy.deepcopy(r);x['review_audit']['batches']=[]
        self.assertEqual(assessment(x,e)['counts']['high'],0)
        self.assertEqual(assessment(r,[[{'status':'unavailable'}]])['counts']['high'],0)

    def test_unreviewed_or_failed_is_pending_even_if_severity_says_high(self):
        for disposition in ['needs_context','',None]:
            r,e=self.sample();r['findings'][0].update(review_disposition=disposition,severity='高风险')
            out=assessment(r,e);self.assertEqual(out['counts']['error'],1);self.assertEqual(out['counts']['high'],0)

    def test_digits_and_affine_patterns_are_never_high_by_themselves(self):
        for rule in ['terminal_digit','affine_columns','constant_increment','duplicate_values']:
            r,e=self.sample('table',rule);r['review_audit']['decisions'][0]['validated_risk_basis']['context']['claim']='independent_measurements'
            self.assertEqual(assessment(r,e)['counts']['review'],1)

    def test_duplicate_rows_require_independent_measurement_context(self):
        r,e=self.sample('table','duplicate_rows')
        self.assertEqual(assessment(r,e)['counts']['high'],0)
        r['review_audit']['decisions'][0]['validated_risk_basis']['context']['claim']='independent_measurements'
        self.assertEqual(assessment(r,e)['counts']['high'],1)

    def test_numeric_high_requires_actual_recalculation_discrepancy(self):
        r,e=self.sample('table','model_evidence_review');r['findings'][0]['title']='同一数值计算不一致'
        d=r['review_audit']['decisions'][0];d['validated_risk_basis']['context']['claim']='same_quantity_same_unit'
        self.assertEqual(assessment(r,e)['counts']['high'],0)
        d['validation_status']='calculation_discrepancy_supported';d['calculation_records']=[{'matches':False}]
        self.assertEqual(assessment(r,e)['counts']['high'],1)

    def test_classification_is_immutable_and_coverage_is_separate(self):
        r,e=self.sample();before=copy.deepcopy((r,e));out=assessment(r,e)
        self.assertEqual((r,e),before);self.assertEqual(len(out['coverage_gaps']),1);self.assertEqual(sum(out['counts'].values()),1)
        self.assertEqual(len(out['catalogue']),7)

    def test_embedded_fonts_and_clean_mixed_text(self):
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'report.pdf'
            result={'engine_id':'layout-test','summary':'METTL3 log2 0.01 科学记数法','findings':[],'checks':[{'name':'范围','status':'no_signal','reason':'Test.'}]}
            _write_pdf(path,{'title':'论文排版 METTL3 α β µ × 10⁻³'},result,[],[])
            reader=PdfReader(path);embedded=[]
            for page in reader.pages:
                for ref in page['/Resources'].get('/Font',{}).values():
                    font=ref.get_object();desc=font.get('/FontDescriptor')
                    if desc:embedded.append('/FontFile2' in desc.get_object())
            self.assertTrue(embedded);self.assertTrue(all(embedded))
            with pdfium.PdfDocument(path) as doc:
                text=''.join(page.get_textpage().get_text_range() for page in doc)
                self.assertIn('METTL3',text);self.assertIn('高风险',text);self.assertNotIn('M E T T L 3',text);self.assertIn('-3',text)

if __name__=='__main__':unittest.main()
