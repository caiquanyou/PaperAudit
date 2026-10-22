# 第三方组件记录

应用自身代码位于desktop_app及app.py。下列运行组件或构建工具的版权及许可文本保存在licenses文件夹。版本来自本次实际构建环境。

Python 3.12.14；Windows自带系统库按系统环境使用。

- pypdfium2 5.14.0
- Pillow 12.3.0
- reportlab 4.4.9
- charset-normalizer 3.5.2
- PyInstaller 6.22.3
- altgraph 0.17.5
- pefile 2024.8.26
- pywin32-ctypes 0.2.3
- numpy 2.3.5
- opencv-python-headless 4.11.0.86
- openpyxl 3.1.5
- xlrd 2.0.1
- et-xmlfile 2.0.0
- lxml 6.1.1

Tk/Tcl随Python运行时分发。PDFium及其依赖的具体文本保存在pypdfium2许可目录。用户提供的筛选项目算法经适配后加入本工具；源码来源、哈希、复用边界和未支持能力见随包整合说明。篇内ORB/RANSAC的上游来源gengscan及MIT文本保存在licenses/gengscan-LICENSE.txt。本包不含原项目论文、原图、历史数据库、Key或旧网页服务。
