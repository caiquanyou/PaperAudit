# PaperAudit · 论文自查

Windows原生桌面工具。输入论文PDF及其补充材料，按篇检查图片和表格线索，经过独立模型复核与自动来源补证后，输出含来源图的Markdown、PDF和审计资料包。

**当前版本：0.6.2。** 在 [Releases](https://github.com/caiquanyou/PaperAudit/releases) 下载Windows发行包，完整解压后双击PaperAudit.exe，无需安装Python、Node或浏览器。

## 使用

1. 在模型设置中选择DeepSeek、Kimi、豆包、Qwen或自定义服务，填写自己的API Key。模型名称和base_url均可修改。
2. 添加一篇或多篇PDF，选中每篇分别添加其附件。程序按篇依次检查。
3. 在历史记录查看进度、原文件和报告，导出完整资料包以保留截图。

说明与截图见 [使用说明](docs/使用说明.md)，报告规则见 [报告分级与自动补证](docs/报告分级与自动补证.md)，接口见 [本机API](desktop_app/API.md)。发行包包含PDF使用说明。

## 检查流程

正文及附件解析 → 本地图片/表格规则和模型初筛 → 新上下文独立复核 → 对争议项目补齐表头、完整单元格、相邻行及相关正文/图注 → 再次复核与输出校验 → 报告。

报告分为高风险线索、需要复核、证据不足和检查异常。材料或工具能力缺口不会直接计作论文问题。尾数集中、重复小数或图片相似不能单独证明伪造；报告给出合理解释和所需核验材料。

七类目录覆盖图片复用、图片编辑、附件表格与数据生成线索、数值计算、统计设计、方法材料对应和发表声明。图片复用使用精确显示匹配、pHash、ORB/RANSAC和页面SIFT后备，表格检查包含重复行/块、增量、派生关系和尾数等候选规则。模型的正面判断需要可核对的原文证据。原始候选、筛除理由、补证范围和最终决定分别保留。

不设置模型金额或总调用次数配额。相关材料片段会发送到用户选择的模型服务地址；Key可仅本次运行使用，或经Windows用户保护机制加密保存。自动附件获取不携带模型Key，仅尝试可确认归属的公开材料。

## 能力边界

当前实现是辅助核验，报告不作学术不端认定。图片编辑、统计设计、方法和发表类尚无足以自动抬升高风险的专用验证器。扫描OCR、跨论文全库查重、完整统计重算和外部RNA-seq复现尚未接入；RNA-seq只是条件专项。

解析、上下文长度、图像索引和公开附件下载仍有资源边界，实际覆盖保存在报告中。科学判断、服务异常和材料不足分开记录。模型补证不能补造作者未公开的实验记录。

## 开发与打包

开发环境为Windows x64、Python 3.12。安装依赖后运行：

```powershell
python -m pip install -r requirements-dev.txt
python app.py
python -m unittest discover -s tests -q
python -m PyInstaller PaperAudit.spec --noconfirm
python tools/build_manual.py
python tools/package_windows.py --exe dist/PaperAudit.exe --destination release/PaperAudit-0.6.2-Windows-x64.zip
```

PDF使用本机微软雅黑或黑体生成嵌入子集，需要系统中存在对应字体；仓库和发行包不分发完整系统字体。材料库默认位于当前用户的%LOCALAPPDATA%/PaperAudit，更新前退出程序并备份该目录。

仓库仅包含应用、测试、构建脚本与公开说明，不含用户论文、补充材料、历史数据库或模型Key。第三方版权与许可见 [组件记录](THIRD_PARTY_NOTICES.md) 和licenses目录。
