# Portable Windows single-file build: python -m PyInstaller PaperAudit.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_all,collect_data_files
root=Path(SPECPATH)
datas=collect_data_files('reportlab')
extra_data,binaries,hiddenimports=collect_all('pypdfium2_raw')
datas+=extra_data
a=Analysis([str(root/'app.py')],pathex=[str(root)],binaries=binaries,datas=datas,hiddenimports=hiddenimports,hookspath=[],hooksconfig={},runtime_hooks=[],excludes=['matplotlib','scipy','pandas','IPython','pytest'],noarchive=False,optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='PaperAudit',debug=False,bootloader_ignore_signals=False,strip=False,upx=True,console=False,disable_windowed_traceback=False)
