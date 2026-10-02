# E8：PDF 图像与视觉证据

本功能在 `src/research` 现有 Controller、RoleBackend、预算池、APSW Store、
依赖图与跨 run 复用链路中实现，默认关闭。它不是独立的第二套研究数据库。

## 启用

```powershell
python -m pip install --only-binary=apsw -e ".[research,research-vision,test]"
python -m src research validate --contract your-contract.json --config examples/research/visual/research.toml
python -m src research run --contract your-contract.json --config examples/research/visual/research.toml --runs-root research-runs --run-id visual-001
```

先修改示例里的模型服务地址、model 和 `vision-profile.json`，并设置配置指定的 API
key 环境变量。**必须选择实际支持图片输入、且通过本项目 chat-completions 协议测试
的服务**。示例中的名称是占位符，不宣称任何现有文本模型天然支持视觉。

当前已有的在线研究入口仍是 `assisted + chat_json`：角色直接调用模型，搜索与 PDF
获取由现有 assisted mailbox 提供。没有新增自主 live 搜索控制器，也没有用固定答案
替代真实模型。仅 `external` 文本角色或没有 vision profile 的配置不能启用 E8。

`[research.visual] enabled = true` 打开能力；同一份 ResearchConfig 定义全部限值：

| 配置 | 默认值 |
|---|---|
| visual.max_figures_per_action | 3 |
| visual.max_images_per_figure | 2（可显式设至 6，以容纳跨页/多子图） |
| visual.dpi / detail_dpi | 150 / 300；每幅最多一张细节图 |
| visual.max_image_pixels | 6,000,000 |
| visual.max_image_bytes | 8 MiB |
| extraction.max_parse_seconds | 15 秒 |
| extraction.max_pdf_pages / max_text_chars | 200 / 300,000 |
| extraction.max_memory_bytes | 512 MiB |
| extraction.max_input_bytes / max_total_render_bytes | 8,000,000 / 48 MiB |

Provider profile 中的单图、像素、张数和整包限制也会执行。模型、revision、profile、
视觉配置和提示模板哈希写入不可变 `visual_manifest`，恢复不能悄悄更换。
首次实际读图先发送随机六位数图片验证图片输入；测试输入、输出与调用计量保留。
这个测试检验传输和基本识别，不是科学图表理解质量评测。

## 数据与调用链

1. 保存原 PDF 与 SHA-256。启用时由 PyMuPDF 子进程生成文字块、页几何与 Figure/Fig./图
   候选地图。候选默认 ambiguous；嵌入位图数量不当作论文图数量。无文本的扫描件留下
   `needs_ocr` 缺口。
2. `researcher.inspect_figures` 对每个候选给出阅读、无关或待查处置。正文提及的图不能
   仅因图注无关键词被跳过。每个 SearchAction 最多三幅，选择记录持久化。
3. 模型可提出未旋转 PDF point 坐标的区域；没有区域时回退整页预览。Controller
   检查页码、bbox、像素预算和图片体积。多图/跨页归属由独立审核核查，不由矩形合法性
   代替。`region_from_pixels` 可把视觉模型的像素区域按保存的实际矩阵映射回 PDF。
4. FigureArtifact 保存原 PDF hash、页码、CropBox、rotation、区域、图注/正文块引用、
   PyMuPDF 版本与渲染配置、PNG hash 和像素变换。矢量、文字、位图一起渲染；关闭批注，
   RGB、不透明。渲染缓存含原 PDF、区域、解析器版本及配置；旧审核的 PNG 不被覆盖。
5. `researcher.read_figure` 接收真实 PNG image content 和图注、引用正文所在页，提出
   候选 Claim 与结构化 VisualObservation。描述保存在 `observation`；`excerpt` 为空，
   不把解释冒充逐字原文。标签转录、单位、条件、子图和限制单独保存。
6. `auditor.visual` 与 `auditor.visual_counter` 分别接收相同 PNG 和原始上下文，不读取
   另一审核的 verdict。检查裁图完整性、配对、轴/刻度、图例、条件、误差条、精度和图文
   一致性；两路通过且无待查项才接入现有 `evidence_links`。相同模型两次判断不等于
   独立测量。图文矛盾建立 ConflictCase，保持视觉缺口。
7. 覆盖审核不能把 unresolved visual gap 认定为 satisfied。契约允许的 bounded_unknown
   仍须通过原有未知门禁。Writer 使用审核后的 Claim/Evidence；报告引用保留 PDF hash、
   页、图号、条件和本地 PNG 预览，`evidence.json` 附带 FigureArtifact。

首版仅支持明确印刷的 `reported` 数字，必须绑定图像和标签位置。拒绝
`digitized_estimate`、额外精度和未标值曲线的精确估读；表格截图的数值计算等待 E4。
超出图示范围的因果/显著性/总体效果不由图像本身证明。

## 预算、恢复与复用

- 视觉调用通过原有 `Controller.role`、attempt、ContextPacket 和 BudgetManager 计费。
  阅读/格式修复之前检查两路审核剩余额度，尊重 SearchAction 配额，不借用写作与报告
  审计保护池。格式修复最多一次，同样计费。记录 provider 原始 usage；缺失计量保留
  unknown，不用文本 tokenizer 虚算图像费用。
- 原角色输入/结果缓存与图像 hash 一起恢复；完成的角色不会重新计费，未知/失败调用
  不退款。图像内容从校验过的 blob 重建，给模型的不是本地路径字符串。
- 数据库迁移 `003_visual.sql` 增加渲染缓存和动作选图记录。实体、blob、事件、lease、
  attempt、支持关系和依赖仍使用现有表；旧文本 run 自动迁移且默认不启用视觉。
- 复用白名单包含 FigureArtifact、DocumentMap、PNG/PDF 与图注依赖。新 run 导入的是
  候选材料，必须通过原有时间/范围/来源复核，并让本地两路 Auditor 重新看到原图。
  缺失图像会失败；未启用视觉的目标 run 保持 `requires_refresh`，不接受旧描述。
- 原 PDF 与图像证据共用观测根，不增加独立来源数。图区依赖进入授权祖先的失效传播，
  失效后撤销受影响的本地 Claim、覆盖和报告资格；历史图像与报告仍可追溯。
- 解析子进程有超时、RSS、输出体积监控；取消/超时后终止并回收。Unix 额外设置地址
  空间上限，Windows RSS 监控有 20ms 采样间隔。渲染前估算像素，超限返回 unreadable。

## 当前边界

HTML 已发现 figure/img/figcaption 及正文锚点，但外图下载/安全 SVG 转换尚未接入；
发现这些图片时记录 `requires_validated_image_fetch` 缺口，不越过 FetchProvider 发请求。
Docling、OCR、自动曲线数字化、E4 表格计算和完整 E2 引用深读仍未启用。上下文包含
图区及图注/提及所在页；方法条件在别页且缺失时，审核必须留缺口。

验收命令：`python -m pytest tests/research -q`。新增测试使用合成 PDF 和协议夹具，
覆盖矢量渲染、rotation/CropBox、像素换算、跨页图注、真实图片请求结构、审核隔离、
预算/恢复、数字类别、报告、完整视觉复用和祖先失效传播。没有真实服务密钥测试或
人工保留集质量/成本对比，不宣称视觉模型优于文本模型。
