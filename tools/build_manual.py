from pathlib import Path
import sys,re,shutil
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from desktop_app.pdf_report import fonts
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate,Paragraph,Image,Spacer,KeepTogether
from xml.sax.saxutils import escape
docs=ROOT/'docs';images=docs/'screenshots';images.mkdir(exist_ok=True)
regular,bold=fonts();base=ParagraphStyle('Body',fontName=regular,fontSize=10.5,leading=17,spaceAfter=8,wordWrap='CJK',textColor=colors.HexColor('#202A35'))
h1=ParagraphStyle('Title',parent=base,fontName=bold,fontSize=24,leading=32,spaceAfter=16,keepWithNext=True)
h2=ParagraphStyle('Heading',parent=base,fontName=bold,fontSize=15,leading=23,spaceBefore=14,spaceAfter=9,keepWithNext=True)
small=ParagraphStyle('Caption',parent=base,fontSize=8.5,leading=13,textColor=colors.HexColor('#526170'))
story=[]
for line in (docs/'使用说明.md').read_text(encoding='utf-8').splitlines():
    if not line.strip():continue
    m=re.fullmatch(r'!\[(.*?)\]\((.*?)\)',line)
    if m:
        im=Image(str(docs/m[2]));scale=min(168*mm/im.imageWidth,100*mm/im.imageHeight);im.drawWidth=im.imageWidth*scale;im.drawHeight=im.imageHeight*scale
        story.append(KeepTogether([Spacer(1,2*mm),im,Spacer(1,2*mm),Paragraph(escape(m[1]),small)]));continue
    text=line.lstrip('#').strip();style=h1 if line.startswith('# ') else h2 if line.startswith('## ') else base
    story.append(Paragraph(escape(text),style))
def footer(c,doc):
    c.saveState();c.setFont(regular,8);c.setFillColor(colors.HexColor('#526170'));c.drawString(21*mm,13*mm,'PaperAudit 0.6.2 | 使用说明');c.drawRightString(189*mm,13*mm,str(doc.page));c.restoreState()
target=docs/'使用说明.pdf'
SimpleDocTemplate(str(target),pagesize=(210*mm,297*mm),leftMargin=21*mm,rightMargin=21*mm,topMargin=20*mm,bottomMargin=21*mm,title='论文自查使用说明 0.6.2',author='PaperAudit').build(story,onFirstPage=footer,onLaterPages=footer)
print(target)
