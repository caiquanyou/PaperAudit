# 原生桌面基础版的本机接口

应用版本0.6.2（接口契约延续0.5）。在模型设置的“高级选项”中开启后仅监听127.0.0.1，复制本次运行的工具令牌用于Authorization Bearer。退出或重新开启会更换令牌。模型API Key不是本接口令牌，普通桌面使用无需开启API。

当前实现的接口如下，完整设计草案中的其他端点仍待后续实施。

| 接口 | 用途 |
| --- | --- |
| GET /api/v1/capabilities | 查询实际能力与engine_ready |
| GET /api/v1/papers | 获取历史文献 |
| POST /api/v1/paper-packages | 导入单篇正文与附件包，可排队 |
| GET /api/v1/papers/{id}/files | 获取本篇文件标识，不回显本地路径 |
| GET /api/v1/files/{id}/content | 下载对应材料供接入引擎分析 |
| POST /api/v1/papers/{id}/check | 将该篇加入串行队列 |
| POST /api/v1/papers/{id}/result | 接收已授权引擎的规范化结果并生成报告 |
| POST /api/v1/model-profile | 设置模型连接和Key，不返回Key |
| GET /api/v1/papers/{id}/results | 获取最近已保存的原始分析结果 |
| GET /api/v1/papers/{id}/jobs | 获取任务标识、状态与input_fingerprint |
| GET /api/v1/papers/{id}/reports | 查询本篇报告 |
| GET /api/v1/reports/{id}/markdown | 下载Markdown |
| GET /api/v1/reports/{id}/pdf | 下载PDF |
| GET /api/v1/reports/{id}/bundle | 下载含截图的完整报告资料包 |

JSON导入包格式如下，单次请求体限制24MB；大文件使用桌面导入。批量调用方可逐篇提交多个包，后端仍串行检查，不能把多篇正文混进同一个包。

```json
{
  "files": [
    {"name": "paper.pdf", "role": "manuscript", "content_base64": "BASE64_FILE_CONTENT"},
    {"name": "supplement.csv", "role": "supplement", "content_base64": "BASE64_FILE_CONTENT"}
  ],
  "enqueue": true
}
```

接入引擎的结果需要说明来源、实际检查范围及问题列表。图像疑点至少提供一个已导入文件的定位。bbox使用显示方向页面或图片的左上角原点，数值归一化至0到1；PDF页码从1起。两个面板可分别提供两条定位，报告自动并排输出。

```json
{
  "job_id": "JOB_ID_RETURNED_BY_CHECK",
  "input_fingerprint": "FINGERPRINT_RETURNED_BY_CHECK",
  "engine_id": "ACTUAL_ENGINE_AND_VERSION",
  "summary": "本轮分析摘要",
  "findings": [{
    "title": "需要核验的问题",
    "kind": "image",
    "severity": "需核验",
    "observation": "具体观察与依据",
    "alternative_explanation": "需要排除的合理解释",
    "recommendation": "后续核验建议",
    "anchors": [{"artifact_id": "UPLOADED_FILE_ID", "page": 2, "bbox": [0.1, 0.2, 0.6, 0.5]}]
  }],
  "checks": [{"name": "图像候选检查", "status": "已执行", "reason": "实际覆盖范围"}],
  "limitations": ["材料缺口或方法限制"]
}
```

截图不能由模型凭空生成。程序核对文件归属及哈希，从实际文件渲染裁图；没有局部框时只展示整页或整图并明示范围，截图失败时记录限制。未知附件、越界坐标和跨论文引用不会被当作有效图片证据。证据图片及其来源记录保存在每篇独立报告目录。

第一轮真实本地引擎已接入；capabilities说明当前支持能力与未实现扩展。未启用模型也可本地检查，存在材料或能力边界时任务状态partial，报告仍可下载。外部结果来源须单列，不冒充本机执行。

排队与导入排队响应返回job_id及input_fingerprint。回传结果必须精确绑定这两个字段，不能只按论文ID提交。旧任务结果不能完成新的排队任务；已完成任务拒绝覆盖。TIFF的page表示从1开始的帧号。

设置模型连接示例（需工具Bearer令牌，api_key只写入不回显）：

```json
{"profile":{"base_url":"https://YOUR_PROVIDER/v1","text_model":"YOUR_MODEL","vision_model":"","enable_model":true,"local_http":false,"review_model":"","review_vision_model":"","auto_supplements":true},"api_key":"YOUR_OWN_KEY","persist":false}
```

GET /papers/{id}/results 返回job_id与结果；模型调用、材料解析覆盖和规则状态在原始JSON中可读。外部结果带partial或检查含failed/unavailable/needs_mapping/insufficient_data时不能标完整完成。已完成/partial任务禁止覆写，需要重新排队创建新任务。


## 0.6 独立复核与补充材料

首轮候选经新的独立请求上下文逐条复核后生成报告。profile可配置review_model和review_vision_model，留空分别沿用text_model和vision_model。auto_supplements默认true，仅有正文时尝试取得DOI和标题匹配的公开补充材料。取不到则继续检查已有材料。

不设应用层模型金额或调用次数上限，旧max_model_calls字段不再参与控制。每次调用仍受服务商账号权限、上下文长度和接口约束。对有具体疑点的证据不足或无效决定自动补齐来源并再次独立复核；结构化输出仍无效时请求修正。无法恢复的网络错误不盲目重放，明确记录为检查异常。

jobs新增analysis_input_fingerprint。结果包含analysis_manifest、review_audit、parser_coverage和分阶段model_calls。analysis_manifest绑定实际取得的附件和解析产物；回传结果不得引用本任务快照外的材料。

报告包新增review-decisions.md、review-audit.json、first-pass-candidates.json、materials.json。MD与PDF区分复核保留事项和待人工核对候选，筛除详情保留在账本。

## 0.6.1 报告分级与字体

capabilities新增risk_triage与embedded_pdf_fonts。报告包新增risk-assessment.json与checks.json，分别保存高风险、需复核、证据不足或检查异常的逐项清单和完整检查范围；PDF使用嵌入的中文字体。材料缺口单列，不计作已支持的风险项目。

外部传入的severity或模型自报high不直接决定报告优先级。没有有效独立复核与证据的候选列为证据不足或检查异常；高风险须满足来源冲突、实验身份、正常解释排查及相应本地验证门槛。详细规则见随包的报告阅读与分级说明.md。


## 0.6.2 自动来源补证

独立复核后，对needs_context逐项补齐对应工作表的完整单元格、表头、相邻行及相关正文/图注，替代首轮80字符单元格预览。来源只限本任务绑定材料，原文件哈希变化则拒绝补证。

每个疑点单独进入新的复核上下文，结构或引文校验不合格时请求修正。能排除的筛除，支持的保留；仍缺作者原始记录的明确列为证据不足，模型配置、网络、输出校验等服务问题列为检查异常。检查异常不计作论文风险。

review_audit新增initial_decisions、supplemental_reviews和resolution_counts，保留初次决定、补证范围、调用标识及最终决定。risk-assessment.json改为risk-triage-2，counts和目录使用high、review、insufficient、error四类；不再输出旧pending标签。后台任务状态partial继续表示实际范围存在边界，桌面和报告按具体原因展示。
