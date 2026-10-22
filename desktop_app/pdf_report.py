"""Embedded Unicode fonts and a report layout focused on evidence and triage."""
from __future__ import annotations
import os,re,unicodedata
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image,KeepTogether,PageBreak
from .storage import now

def fonts():
    regular,bold='PaperAuditText','PaperAuditBold'
    if regular not in pdfmetrics.getRegisteredFontNames():
        folder=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
        options=[(folder/'msyh.ttc',folder/'msyhbd.ttc'),(folder/'simhei.ttf',folder/'simhei.ttf')]
        for body,heading in options:
            if body.is_file() and heading.is_file():
                pdfmetrics.registerFont(TTFont(regular,str(body),subfontIndex=0))
                pdfmetrics.registerFont(TTFont(bold,str(heading),subfontIndex=0))
                break
        else:raise RuntimeError('未找到可嵌入的中文字体，请确认Windows字体中的微软雅黑或黑体可用。')
        pdfmetrics.registerFontFamily(regular,normal=regular,bold=bold,italic=regular,boldItalic=bold)
    return regular,bold

def display(text):
    text=unicodedata.normalize('NFC',str(text or ''))
    return ''.join(c for c in text if c in '\n\t' or (unicodedata.category(c) not in {'Cc','Cs'} and c not in '\ufffe\uffff')).replace('\u00a0',' ')

def location(item):
    a=item['anchor'];name=item['source_name']
    if a.get('sheet'):
        cells=a.get('cells');where=' / 行列待定位' if not cells else f' / R{cells[0]}:R{cells[1]}，C{cells[2]}:C{cells[3]}'
        return name+' / '+a['sheet']+where
    if a.get('page'):return name+f" / 第{a['page']}页或帧"
    if a.get('paragraph'):return name+f" / 段落{a['paragraph']}"
    return name

def write_pdf(path,paper,result,evidence,warnings,triage,check_labels):
    regular,bold=fonts();ink=colors.HexColor('#202A35');muted=colors.HexColor('#526170');navy=colors.HexColor('#23465C')
    base=ParagraphStyle('PaperAuditBody',fontName=regular,fontSize=10.5,leading=17,spaceAfter=7,wordWrap='CJK',splitLongWords=True,textColor=ink)
    heading=ParagraphStyle('PaperAuditHeading',parent=base,fontName=bold,fontSize=15,leading=23,spaceBefore=15,spaceAfter=10,keepWithNext=True)
    sub=ParagraphStyle('PaperAuditSubheading',parent=base,fontName=bold,fontSize=11.5,leading=18,spaceBefore=11,spaceAfter=6,keepWithNext=True)
    title=ParagraphStyle('PaperAuditTitle',parent=heading,fontSize=25,leading=34,spaceBefore=0,spaceAfter=14)
    small=ParagraphStyle('PaperAuditSmall',parent=base,fontSize=8.5,leading=13,spaceAfter=5,textColor=muted)
    cell=ParagraphStyle('PaperAuditCell',parent=base,fontSize=9,leading=14,spaceAfter=0)
    th=ParagraphStyle('PaperAuditTableHeader',parent=cell,fontName=bold,textColor=colors.white)
    story=[]
    def p(text,style=base):
        text=escape(display(text))
        text=re.sub('[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+',lambda m:'<super>'+m.group().translate(str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺','0123456789-+'))+'</super>',text)
        text=re.sub('[₀₁₂₃₄₅₆₇₈₉₋₊]+',lambda m:'<sub>'+m.group().translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉₋₊','0123456789-+'))+'</sub>',text)
        return Paragraph(text.replace('\n','<br/>'),style)
    def table(headers,rows,widths):
        data=[[p(v,th) for v in headers]]+[[p(v,cell) for v in row] for row in rows]
        t=Table(data,colWidths=[w*mm for w in widths],repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),navy),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,0),(-1,0),.6,navy),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F3F6F8')]),('LINEBELOW',(0,1),(-1,-1),.3,colors.HexColor('#D9E1E7'))]))
        return t
    counts=triage['counts'];kept=counts['high']+counts['review']
    story += [p('论文查验报告',title),p(paper['title'],sub),p('生成时间：'+now()+'  |  报告排版：0.6.2',small),p('报告阅读顺序',heading),p('先核对高风险线索与需要复核的事项，再查看证据不足、检查异常及材料覆盖说明。'),table(['高风险线索','需要复核','证据不足','检查异常'],[[str(counts[k])+'项' for k in ['high','review','insufficient','error']]],[42,42,42,42]),Spacer(1,4*mm),p(triage['notice'],small)]
    if not counts['high']:story += [p('本轮无达到高风险证据门槛的项目。不能据此排除未覆盖范围内的风险。',small)]
    if result.get('partial'):story += [p('本报告按实际可核验材料出具；证据边界和服务异常另列，不能解释为整篇通过。',small)]
    audit=result.get('review_audit',{});ac=audit.get('counts',{})
    if audit:story += [p(f"首轮候选{len(audit.get('candidates',[]))}项；复核筛除{ac.get('drop',0)}项、合并{ac.get('merge',0)}项。本报告列出{len(triage['items'])}项，其中{kept}项已有有效保留决策。",small)]
    story += [p('风险类别与本轮项目',heading),table(['风险类别','高风险','需复核','证据不足','检查异常'],[[v['name']]+[str(v[k]) for k in ['high','review','insufficient','error']] for v in triage['catalogue']],[80,22,22,22,22])]
    story += [p('以上是本轮列出项目的分布，不是各类检查全部完成的证明。具体执行范围见报告末尾及 checks.json。',small)]
    story += [PageBreak()]
    for group,label in [('high','高风险项目清单'),('review','需要复核的项目清单'),('insufficient','证据不足：具体疑点与所需材料'),('error','检查异常：工具或模型服务记录')]:
        entries=[v for v in triage['items'] if v['group']==group]
        if not entries:continue
        story += [p(label,heading)]
        story += [table(['编号 / 类别','项目','处理依据'],[[v['candidate_id']+'\n'+v['category_label'],v['title'],v['basis']] for v in entries],[39,62,67])]
    if not triage['items']:story += [p('本轮未列出风险项目。具体覆盖范围与限制见后文。')]
    if triage['items']:story += [PageBreak(),p('逐项证据与复核要求',heading)]
    for n,row in enumerate(triage['items'],1):
        f=result['findings'][row['index']];images=evidence[row['index']]
        badge=ParagraphStyle('Badge'+str(n),parent=small,textColor=colors.HexColor({'high':'#9F2F24','review':'#805311','insufficient':'#526170','error':'#7B4864'}[row['group']]))
        story += [KeepTogether([p(f"{n:02d}  {f['title']}",sub),p(row['candidate_id']+'  |  '+row['category_label']+'  |  '+row['group_label'],badge),p('分级依据：'+row['basis'])])]
        for label,text in [('原始观察',f.get('observation','')),('合理解释',f.get('alternative_explanation','')),('下一步复核',f.get('recommendation',''))]:
            if text:story += [p(label,sub),p(text)]
        for start in range(0,len(images),2):
            pairs=images[start:start+2];cells=[]
            for item in pairs:
                content=[p(location(item),small)]
                if item['status']=='captured':
                    im=Image(str(path.parent/item['image']));scale=min(76*mm/im.imageWidth,62*mm/im.imageHeight)
                    im.drawWidth,im.drawHeight=im.imageWidth*scale,im.imageHeight*scale
                    content += [im,Spacer(1,2*mm),p('原始单元格视图' if 'cells' in item['anchor'] else '来源裁图，边框仅用于定位' if item['anchor'].get('bbox') else '整页或整图定位，请打开原件放大核对',small)]
                else:content += [p('证据图未生成：'+item['reason'],small)]
                cells.append(content)
            t=Table([cells],colWidths=[84*mm]*len(cells),hAlign='LEFT')
            t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BOX',(0,0),(-1,-1),.4,colors.HexColor('#D9E1E7')),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
            story += [t,Spacer(1,4*mm)]
    story += [p('风险类别判定参考',heading)]
    for v in triage['catalogue']:story += [p(v['name'],sub),p(v['focus'])]
    story += [p('材料缺口与未开展检查',heading)]
    gaps=defaultdict(list)
    for c in triage['coverage_gaps']:
        name=c.get('name',c.get('id','检查'));status=c.get('status','')
        text=c.get('reason','')
        if text not in gaps[(name,status)]:gaps[(name,status)].append(text)
    if not gaps:story += [p('本轮未单列范围缺口；仍须结合原文和实际材料理解检查结论。')]
    for (name,status),reasons in gaps.items():
        story += [p(name+' / '+check_labels.get(status,status),sub)]
        for reason in reasons[:2]:story += [p(reason)]
        if len(reasons)>2:story += [p(f'另有{len(reasons)-2}条同类范围记录，完整内容见 checks.json。',small)]
    retrieval=result.get('analysis_manifest',{}).get('retrieval',{})
    if retrieval:
        story += [p('本次补充材料',heading),p('获取状态：'+retrieval.get('status','未说明')+'；取得'+str(len(retrieval.get('files',[])))+'份。')]
        for a in retrieval.get('files',[]):story += [p(a['name'],sub),p(a['source_url'],small)]
        for note in retrieval.get('notes',[]):story += [p(note,small)]
    story += [p('已执行检查与审计文件',heading)]
    done=defaultdict(int)
    for c in result.get('checks',[]):
        if c.get('status') in {'signal','no_signal','not_applicable'}:done[(c.get('name',c.get('id','检查')),c['status'])]+=1
    for (name,status),count in done.items():story += [p(name+'：'+check_labels.get(status,status)+f'（{count}条记录）。',small)]
    calls=defaultdict(int)
    for call in result.get('model_calls',[]):calls[(call.get('stage','first_pass'),call.get('model'),call.get('status'))]+=1
    for (stage,model,status),count in calls.items():story += [p(f'模型调用：{stage} / {model} / {status} / {count}批。',small)]
    story += [p('checks.json 保存完整检查范围；risk-assessment.json 保存分级依据；review-decisions.md 与 review-audit.json 保存复核、筛除与本地校验；evidence.json 保存截图来源和逐次调用记录。',small),p('限制与证据图失败',heading)]
    for text in result.get('limitations',[])+warnings:story += [p(text,small)]
    if not result.get('limitations') and not warnings:story += [p('本轮未另列限制；仍以实际检查范围及原始材料为准。',small)]
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont(regular,8);canvas.setFillColor(muted)
        canvas.drawString(21*mm,13*mm,'论文自查  |  证据核验报告');canvas.drawRightString(189*mm,13*mm,f'{doc.page}')
        canvas.restoreState()
    SimpleDocTemplate(str(path),pagesize=(210*mm,297*mm),leftMargin=21*mm,rightMargin=21*mm,topMargin=20*mm,bottomMargin=21*mm,title='论文查验报告',author='PaperAudit').build(story,onFirstPage=footer,onLaterPages=footer)
