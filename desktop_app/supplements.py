"""Bounded retrieval of DOI-bound public supporting files. No model credentials."""
from __future__ import annotations

import io
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

import pypdfium2 as pdfium
from .reports import PDF_LOCK
from .storage import now, sha256

SUPPORTED={'.pdf','.docx','.xlsx','.xls','.csv','.tsv','.png','.jpg','.jpeg','.tif','.tiff','.webp','.txt','.zip'}
EPMC='https://www.ebi.ac.uk/europepmc/webservices/rest/'
DOI=re.compile(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+',re.I)


class RetrievalError(Exception): pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*_): return None


class PinnedHTTPS(urllib.request.HTTPSHandler):
    """Connect to the already validated IP, retaining TLS hostname verification."""
    def __init__(self,address):super().__init__();self.address=address
    def https_open(self,req):
        address=self.address
        class Connection(http.client.HTTPSConnection):
            def connect(self):
                raw=socket.create_connection((address,443),timeout=self.timeout)
                try:self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
                except BaseException:raw.close();raise
        return self.do_open(Connection,req,context=ssl.create_default_context())


class PublicHttp:
    def __init__(self,cancelled=lambda:False,seconds=100):
        self.cancelled=cancelled;self.deadline=time.monotonic()+seconds
        self.total=0;self.requests=0

    def get(self,url,limit=4*1024*1024,allowed_hosts=None):
        for _ in range(5):
            if self.cancelled(): raise InterruptedError()
            if self.requests>=16 or time.monotonic()>=self.deadline: raise RetrievalError('检索时间或请求次数达到上限')
            parts=urllib.parse.urlsplit(url)
            host=parts.hostname or ''
            if parts.scheme!='https' or parts.username or parts.password or parts.port not in (None,443): raise RetrievalError('仅接受公开HTTPS地址')
            if allowed_hosts is not None and host not in allowed_hosts: raise RetrievalError('附件跳转离开已确认来源')
            try:
                addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses): raise RetrievalError('地址不是公开网络地址')
                self.requests+=1
                req=urllib.request.Request(url,headers={'User-Agent':'PaperAudit/0.6 (+public supplementary material retrieval)','Accept-Encoding':'identity'})
                with urllib.request.build_opener(urllib.request.ProxyHandler({}),PinnedHTTPS(addresses[0][4][0]),NoRedirect()).open(req,timeout=max(1,min(15,self.deadline-time.monotonic()))) as response:
                    size=response.headers.get('Content-Length')
                    if size and int(size)>limit: raise RetrievalError('下载文件超过限额')
                    chunks=[];count=0
                    while True:
                        if self.cancelled():raise InterruptedError()
                        if time.monotonic()>=self.deadline:raise RetrievalError('下载时间达到上限')
                        block=response.read(min(65536,limit-count+1))
                        if not block:break
                        chunks.append(block);count+=len(block);self.total+=len(block)
                        if count>limit or self.total>100*1024*1024:raise RetrievalError('下载体积达到上限')
                    return b''.join(chunks),url,response.headers.get('Content-Type','')
            except urllib.error.HTTPError as exc:
                if exc.code in (301,302,303,307,308):
                    url=urllib.parse.urljoin(url,exc.headers.get('Location',''));continue
                raise RetrievalError('公开来源返回HTTP '+str(exc.code)) from None
            except (OSError,ValueError,urllib.error.URLError):raise RetrievalError('公开来源暂不可用') from None
        raise RetrievalError('来源跳转次数过多')


def front_text(path):
    with PDF_LOCK:
        with pdfium.PdfDocument(path) as doc:
            return identity_front(doc[0].get_textpage().get_text_range()) if len(doc) else ''


def identity_front(text):
    return re.split(r'(?im)^\s*(?:references|bibliography|参考文献)\s*$',text,maxsplit=1)[0]


def primary_doi(text):
    # Ignore the references at the end; only front matter is supplied here.
    found=[]
    for match in DOI.finditer(identity_front(text)[:12000]):
        value=match.group().rstrip('.,').lower()
        while value.endswith(')') and value.count(')')>value.count('('):value=value[:-1]
        found.append(value)
    if not found:return None
    counts={v:found.count(v) for v in dict.fromkeys(found)}
    best=max(counts,key=counts.get)
    return best if len(counts)==1 or counts[best]>=2 else None


def title_matches(title,text):
    header=re.split(r'(?im)^\s*(?:abstract|introduction|background|摘要|引言)\s*[:：]?\s*$',identity_front(text),maxsplit=1)[0][:2200]
    def tokens(value):return re.findall(r'[a-z0-9]+',unicodedata.normalize('NFKC',value).lower())
    wanted=tokens(title);actual=tokens(header)
    # A title must occur in order in the article header, not as a page-wide bag of words.
    return len(wanted)>=5 and ' '.join(wanted) in ' '.join(actual)


def checked_payload(name,raw):
    ext=Path(name).suffix.lower()
    if ext not in SUPPORTED or not raw or len(raw)>32*1024*1024:raise RetrievalError('附件格式或体积不在下载范围')
    if raw[:300].lstrip().lower().startswith((b'<!doctype html',b'<html')):raise RetrievalError('下载结果是网页而非附件')
    if ext=='.pdf' and b'%PDF-' not in raw[:1024]:raise RetrievalError('附件不是有效PDF')
    if ext in {'.docx','.xlsx','.zip'}:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if ext=='.docx' and 'word/document.xml' not in z.namelist():raise RetrievalError('Word附件结构无效')
                if ext=='.xlsx' and 'xl/workbook.xml' not in z.namelist():raise RetrievalError('Excel附件结构无效')
        except zipfile.BadZipFile:raise RetrievalError('附件压缩结构无效') from None
    return raw


class SupplementLinks(HTMLParser):
    def __init__(self):super().__init__();self.links=[];self.current=None;self.dois=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta' and a.get('name','').lower() in {'citation_doi','dc.identifier','dc.identifier.doi'}:self.dois.append(a.get('content','').lower())
        if tag=='a':self.current=[a.get('href',''),'']
    def handle_data(self,data):
        if self.current:self.current[1]+=data
    def handle_endtag(self,tag):
        if tag=='a' and self.current:self.links.append(self.current);self.current=None


class SupplementFetcher:
    def __init__(self,store,http=None,cancelled=lambda:False):
        self.store=store;self.http=http or PublicHttp(cancelled);self.cancelled=cancelled

    def fetch(self,artifacts):
        report={'status':'not_found','attempted_at':now(),'files':[],'notes':[],'provider':'Europe PMC / Crossref publisher landing page'}
        main=next(a for a in artifacts if a['role']=='manuscript')
        if any(a['role']=='supplement' and not a.get('parent_id') for a in artifacts):
            report.update(status='skipped',notes=['已手动关联附件，不再自动检索']);return [],report
        text=front_text(self.store.resolve(main['relative_path']));doi=primary_doi(text)
        if not doi:report['notes'].append('未能从论文首页可靠识别DOI，继续分析已导入正文');return [],report
        report['doi']=doi;self.parent=main;self.report=report;self.downloaded=[]
        try:self.europe_pmc(doi,text)
        except (RetrievalError,ValueError,ET.ParseError,zipfile.BadZipFile,KeyError,TypeError) as exc:report['notes'].append(str(exc)[:180])
        if not self.downloaded:
            try:self.publisher(doi,text)
            except (RetrievalError,ValueError,KeyError,TypeError) as exc:report['notes'].append(str(exc)[:180])
        obtained={PurePosixPath(a['name']).name for a in self.downloaded}
        report['missing_files']=sorted(set(report.get('expected_files',[]))-obtained)
        if self.downloaded:report['status']='partial_downloaded' if report['missing_files'] else 'downloaded'
        else:report['notes'].append('未取得可确认归属的公开附件，已继续正文检查')
        return self.downloaded,report

    def add(self,name,raw,url,identity):
        if len(self.downloaded)>=8:raise RetrievalError('本篇自动下载最多8份附件')
        checked_payload(name,raw)
        if sum(a['size'] for a in self.downloaded)+len(raw)>80*1024*1024:raise RetrievalError('本篇自动附件总量达到80MB上限')
        origin={'doi':self.report['doi'],'source_url':url,'identity_url':identity,'retrieved_at':now(),'verified_by':'matching DOI and title; supplementary link on matching article'}
        artifact=self.store.register_download(self.parent,name,raw,origin)
        if artifact['id'] not in {a['id'] for a in self.downloaded}:
            self.downloaded.append(artifact);self.report['files'].append({'artifact_id':artifact['id'],'name':artifact['name'],'sha256':artifact['sha256'],'size':artifact['size'],**origin})

    def europe_pmc(self,doi,text):
        url=EPMC+'search?'+urllib.parse.urlencode({'query':'DOI:'+doi,'format':'json','resultType':'core','pageSize':5})
        raw,_,_=self.http.get(url)
        records=json.loads(raw).get('resultList',{}).get('result',[])
        matched=[r for r in records if str(r.get('doi','')).lower()==doi and title_matches(r.get('title',''),text) and r.get('pmcid')]
        if not matched:raise RetrievalError('Europe PMC未找到DOI与标题均匹配的开放附件记录')
        record=matched[0];pmcid=record['pmcid']
        if not re.fullmatch(r'PMC\d+',pmcid) or record.get('isOpenAccess')!='Y' or record.get('hasSuppl')!='Y':raise RetrievalError('匹配记录没有可获取的开放补充文件')
        xml,_,_=self.http.get(EPMC+pmcid+'/fullTextXML',limit=6*1024*1024)
        root=ET.fromstring(xml)
        article_meta=root.find('./front/article-meta')
        ids=[(e.text or '').strip().lower() for e in article_meta.findall('article-id') if e.get('pub-id-type')=='doi'] if article_meta is not None else []
        if doi not in ids:raise RetrievalError('全文记录的DOI不匹配')
        self.report['article_title']=record.get('title');self.report['pmcid']=pmcid
        expected=set()
        for supp in root.iter('supplementary-material'):
            for el in supp.iter():
                href=el.get('{http://www.w3.org/1999/xlink}href')
                if href:expected.add(PurePosixPath(urllib.parse.unquote(urllib.parse.urlsplit(href).path)).name)
        if not expected:raise RetrievalError('文章记录未列出可识别的补充附件名')
        self.report['expected_files']=sorted(expected)
        bundle_url=EPMC+pmcid+'/supplementaryFiles'
        raw,final,_=self.http.get(bundle_url,limit=48*1024*1024,allowed_hosts={'www.ebi.ac.uk','ebi.ac.uk'})
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            members=z.infolist()
            if len(members)>200 or sum(m.file_size for m in members)>80*1024*1024:raise RetrievalError('补充包超过解包限额')
            for m in members:
                name=PurePosixPath(m.filename.replace('\\','/'))
                if name.is_absolute() or '..' in name.parts or ':' in str(name):raise RetrievalError('补充包路径不合法')
                if (m.external_attr>>16)&0o170000==0o120000:raise RetrievalError('补充包不能含符号链接')
                if name.name not in expected or name.suffix.lower() not in SUPPORTED or m.is_dir():continue
                if m.file_size>32*1024*1024 or m.file_size/max(1,m.compress_size)>250:raise RetrievalError('补充包成员超过限额')
                self.add(name.name,z.read(m),final+'#'+urllib.parse.quote(str(name)),EPMC+pmcid+'/fullTextXML')

    def publisher(self,doi,text):
        raw,_,_=self.http.get('https://api.crossref.org/works/'+urllib.parse.quote(doi,safe=''))
        meta=json.loads(raw).get('message',{})
        if str(meta.get('DOI','')).lower()!=doi or not title_matches(' '.join(meta.get('title',[])),text):raise RetrievalError('出版元数据与正文不匹配')
        url=meta.get('resource',{}).get('primary',{}).get('URL') or meta.get('URL','')
        page,landing,_=self.http.get(url)
        html=SupplementLinks();html.feed(page.decode('utf-8',errors='replace'))
        if not any(v.removeprefix('https://doi.org/').removeprefix('http://doi.org/').strip().rstrip('.')==doi for v in html.dois):raise RetrievalError('出版网页未明确标识该DOI，未抓取不确定附件')
        host=urllib.parse.urlsplit(landing).hostname
        allowed={host,'api.crossref.org'}
        for domain in ['wiley.com','springer.com','springernature.com','nature.com','biomedcentral.com','plos.org','frontiersin.org','sciencedirect.com','elsevier.com']:
            if host and (host==domain or host.endswith('.'+domain)):
                allowed.update({'onlinelibrary.wiley.com','media.springernature.com','static-content.springer-cdn.com','ars.els-cdn.com','journals.plos.org','www.frontiersin.org','public-pages-files-2025.frontiersin.org'})
        for href,label in html.links:
            if len(self.downloaded)>=8:break
            link=urllib.parse.urljoin(landing,href);path=urllib.parse.urlsplit(link).path
            if Path(path).suffix.lower() not in SUPPORTED or not re.search(r'supp|support|additional|mmc|MOESM|esm',href+' '+label,re.I):continue
            self.report.setdefault('expected_files',[]).append(PurePosixPath(urllib.parse.unquote(path)).name)
            try:
                data,final,_=self.http.get(link,limit=32*1024*1024,allowed_hosts=allowed)
                self.add(PurePosixPath(urllib.parse.unquote(path)).name,data,final,landing)
            except RetrievalError as exc:self.report['notes'].append(str(exc))
