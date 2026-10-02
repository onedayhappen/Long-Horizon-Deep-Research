# Deep Research

[English](README.md)

一个以证据为中心的研究与报告工作流。给定研究问题、范围和验收要求，系统记录检索与资料快照，整理可定位的主张，检查需求覆盖，并生成带引用的报告。运行状态保存在本地，可查看、导出和恢复。

> 当前为开发中的实现。离线 `replay` 可复现测试流程；`assisted` 需要外部会话逐步提交模型、检索和抓取结果。自动联网的 `live` 模式尚未开放。

## 能做什么

- **明确研究边界**：用 JSON 合约定义问题、版本范围、必答需求、证据要求和报告结构；用 TOML 配置运行方式与预算。
- **追踪证据**：保存来源快照、原文定位、主张、审核结果及引用关系。报告中的事实可回查到本地保存的资料。
- **迭代研究与大纲**：从一级问题骨架开始，按资料逐步细化子章节；章节缺口驱动定向检索，经审核关闭后才能完成。支持同级排序、研究摘要、上下文上限及写作前全纲检查，并保留版本历史。详见[大纲设计](docs/research/outline.md)。
- **审核报告**：对章节和完整报告执行检查；需要补证时退回研究阶段，局部问题只修订相关章节。
- **保存与恢复**：以 SQLite 保存运行状态、预算和事件，支持中断后继续，并导出报告、证据和覆盖结果。
- **显式复用旧研究**：用 `--reuse-from` 导入同一 runs-root 的来源资料；按新契约选证、复核来源和双向审核。支持可恢复导入、来源谱系失效同步和历史报告回查。

## 环境与安装

- Python 3.11 或更新版本。
- 在项目根目录安装：

```bash
python -m pip install -e ".[research,test]"
```

当前命令入口为 `lh-harness research`，也可以通过 `python -m src research` 调用。下文使用后一种写法，便于在源码目录直接运行。

## 快速体验：离线回放

### 网页研究工作台

在项目根目录启动：

```powershell
python -m src research web --runs-root research-runs --port 8765
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。工作台直接读取指定运行目录，支持新建研究、查看进度与待处理请求、浏览证据和报告、下载产物，以及恢复研究。前端随 Python 包提供，无需 Node.js 或前端构建。

在「模型与设置」中选择 DeepSeek 或硅基流动，填写模型名和 API Key；也可在启动前设置 `DEEPSEEK_API_KEY` 或 `SILICONFLOW_API_KEY`。网页填写的密钥仅保留在服务进程内存中，不写入运行配置。测试连接会产生一次真实 API 调用。停止网页服务会停止它启动的研究进程，状态保留在 SQLite 中，之后可恢复；如租约尚未到期，需稍后再恢复。

网页沿用现有 `assisted` 引擎：模型负责规划、分析、审核和写作，搜索与网页抓取仍需通过辅助通道提交。「待处理请求」可下载请求并提交包含 `input_hash`、`producer`、`result` 的响应信封；网页快照可附加 `snapshot_text`，其 UTF-8 字节的 SHA-256 必须匹配 `blob_hash`。也可继续使用 `scripts/research_assistant.py`。

服务仅监听本机，不是公网托管服务。首页的「运行离线样例」无需密钥，用于验证完整流程，资料是虚构样例。

### 命令行离线回放

仓库提供虚构资料的测试样例。它用于验证完整流程，**不代表真实联网研究结论**。

```bash
python -m src research validate \
  --contract tests/fixtures/research/success/contract.json \
  --config tests/fixtures/research/success/research.toml

python -m src research run \
  --contract tests/fixtures/research/success/contract.json \
  --config tests/fixtures/research/success/research.toml \
  --runs-root .lh-harness/demo-runs \
  --run-id example-001

python -m src research status \
  --run-dir .lh-harness/demo-runs/example-001 --json

python -m src research export \
  --run-dir .lh-harness/demo-runs/example-001
```

Windows PowerShell 中可将每条命令写成单行；上述反斜杠续行适用于 Bash。`run-id` 只能包含字母、数字、下划线和连字符，且不能与已有运行目录重复。

## 使用自己的研究问题

1. 参考 [`examples/research/assisted/python_threads_contract.json`](examples/research/assisted/python_threads_contract.json) 编写研究合约，明确问题、范围、需求和验收条件。
2. 参考 [`examples/research/assisted/research.toml`](examples/research/assisted/research.toml) 配置 `assisted` 模式及预算。
3. 先用 `research validate` 检查输入，再用 `research run` 创建运行目录。
4. 程序发出 `ASSISTANT_REQUEST` 后，读取请求并提交与请求 ID、输入哈希匹配的 JSON 响应；按阶段提供实际检索结果、原文资料或角色输出。

```powershell
python -m src research validate --contract examples/research/assisted/python_threads_contract.json --config examples/research/assisted/research.toml
python -m src research run --contract examples/research/assisted/python_threads_contract.json --config examples/research/assisted/research.toml --runs-root research-runs --run-id my-study

# 在另一个终端查看待处理请求
python scripts/research_assistant.py research-runs/my-study --full

# 将本阶段响应保存为 response.json 后提交
python scripts/research_assistant.py research-runs/my-study --response response.json
```

`assisted` 需要外部会话持续处理请求；单独执行 `run` 不会自动获得模型或搜索结果。抓取资料的原始字节与响应哈希也要按请求要求保存。

## 长报告写作

大纲规划可单独配置（默认值如下）。层级上限是保护边界，通常按证据需要展开二、三级，不要求凑满层级。

```toml
[research.planning]
max_depth = 4
context_characters = 48000
summary_characters = 2400
max_sources_per_action = 5
closing_reserve_calls = 4
```

规划器读取完整大纲、已审核主张索引和有额度限制的研究摘要；省略的摘要保留 ID，可按需调取。字符预算不是模型 token 上限，需按模型上下文调整；必要信息超限时以 `incomplete_context` 停止，不截掉缺口或强行完成。同一 run 默认复用相同 URL 的抓取结果；新研究目标重新提取，`refresh_sources=true` 强制刷新抓取。研究调用池为收尾检查保留额度，写作前另检查剩余写作调用数。

Writer 参考 [WebWeaver 的逐节检索与写作流程](https://arxiv.org/html/2509.13312v2#S3.SS3)：按冻结大纲逐节读取已审核证据及其附近的本地原文，保留有限前文用于衔接，每节使用新的上下文。原文邻近内容和前文不能直接充当新增事实的依据。

可在 TOML 中配置（以下为默认值）：

```toml
[research.writing]
max_output_tokens = 8192
target_section_characters = 1800
previous_context_characters = 2400
source_context_characters = 12000
max_expansion_rounds = 1
```

写作 token 上限独立于 `research.model.max_output_tokens`，需适配所用模型的输出限制。章节目标统计正文字符，会按合约总长度与输出额度缩小；它是软目标，证据稀少或问题简单时可以短写。至少有 3 条主张却不足目标一半的章节，在预算允许时最多补写一次；补写保留后续未完成章节的首次写作调用。更多章节或修订需要相应增加 `research.budget` 的写作池（`pool_percentages` 第二项）。

报告审计同时检查论述深度、重复和遗漏：已有证据未展开时退回章节修订；必要解释缺证据时退回研究。最终仍检查事实引用与合约总长度。离线样例使用预先编写的虚构短文，验证执行流程，不代表真实模型的长文质量。

## 查看与恢复

新建研究并复用一个来源 run：

```powershell
python -m src research run --contract contracts/new-question.json --config research.toml --runs-root research-runs --run-id research-b --reuse-from research-runs/research-a
python -m src research reuse-sync --run-dir research-runs/research-b
```

可加 `--reuse-selection selection.json`，按来源需求 ID 或精确证据版本选择材料。来源须记录创建身份；旧版本创建、缺少身份元数据的 run 会返回 `reuse_source_unverified`。详细的选择格式、时间政策、来源复核、失效通知及限制见 [旧研究复用](docs/research/reuse.md)。

研究存储使用固定版本的 APSW 私有 SQLite 引擎；安装依赖时建议使用 `python -m pip install --only-binary=apsw -e ".[research,test]"`。

```bash
python -m src research status --run-dir research-runs/my-study --json
python -m src research resume --run-dir research-runs/my-study
python -m src research export --run-dir research-runs/my-study
```

典型运行目录包含：

| 文件或目录 | 内容 |
| --- | --- |
| `report.md`、`report.json` | 报告及结构化章节 |
| `evidence.json`、`coverage.json` | 证据、引用定位和需求覆盖 |
| `outline.json` | 当前大纲及历史版本 |
| `stop.json`、`execution.json` | 停止原因、执行方式及用量记录 |
| `state.sqlite` | 可恢复的状态与事件账本 |
| `blobs/`、`bridge/` | 资料快照及会话协助请求和响应 |

运行目录可能包含资料原文、模型输出和其他敏感内容，默认不会提交到 Git。

## 当前进度

合约校验、离线回放、状态持久化、动态大纲、报告修订与导出已有实现和测试。`assisted` 模式曾使用公开文档完成真实资料研究，但由同一会话承担多个角色，不构成独立模型复核。

自动联网控制器、完整冲突处理、全部停止结果及人工复核仍在开发中。`research review` 和预算扩展目前尚未实现。

## 开发检查

```bash
python -m pytest tests/research -q
```

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
