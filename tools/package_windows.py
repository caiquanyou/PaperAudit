"""Package an already-built executable with public documentation, never a user library."""
from pathlib import Path
import argparse,hashlib,json,shutil,zipfile,re

VERSION='0.6.2'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    dest=args.destination.parent/f'v{VERSION}';dest.mkdir(parents=True,exist_ok=True)
    shutil.copy2(args.exe,dest/'PaperAudit.exe')
    sources=[(root/'desktop_app/API.md','API说明.md'),(root/'docs/使用说明.md','使用说明.md'),(root/'docs/报告分级与自动补证.md','报告分级与自动补证.md'),(root/'THIRD_PARTY_NOTICES.md','THIRD_PARTY_NOTICES.md')]
    for source,name in sources:shutil.copy2(source,dest/name)
    for name in ['使用说明.pdf','screenshots','licenses']:
        source=root/'docs'/name if name!='licenses' else root/name
        if source.is_dir():shutil.copytree(source,dest/name,dirs_exist_ok=True)
        elif source.is_file():shutil.copy2(source,dest/name)
    (dest/'开始使用.txt').write_text('论文自查 0.6.2\n\n完整解压后双击PaperAudit.exe，无需安装Python或浏览器。\n在模型设置中选择服务商、填写自己的Key，可修改模型和base_url；随后导入PDF及附件，按篇检查。\n本版加入自动来源补证和再次独立复核，报告分别列出高风险、需复核、证据不足及检查异常。\n通用步骤与截图见使用说明.pdf；规则见报告分级与自动补证.md。\n不设置模型金额或调用次数配额。材料库位于%LOCALAPPDATA%\\PaperAudit，更新前退出旧版并备份材料库。\n',encoding='utf-8-sig')
    executable=dest/'PaperAudit.exe'
    manifest={'version':VERSION,'sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),'size_bytes':executable.stat().st_size,'user_data_bundled':False,'model_call_or_money_quota':None,'automatic_evidence_completion':True,'embedded_pdf_fonts':True}
    (dest/'build-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    args.destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.destination,'w',zipfile.ZIP_DEFLATED) as z:
        for p in dest.rglob('*'):
            if p.is_file():z.write(p,p.relative_to(dest).as_posix())
    with zipfile.ZipFile(args.destination) as z:
        assert z.testzip() is None
        assert not any(n.endswith(('.sqlite3','.db','.ttc','.ttf')) for n in z.namelist())
        for name in z.namelist():
            if name.endswith(('.md','.txt','.json')):assert not re.search(rb'sk-[A-Za-z0-9]{15,}',z.read(name))
    print(json.dumps({'archive':str(args.destination),**manifest},ensure_ascii=False))
if __name__=='__main__':main()
