# Deep Research

[English](README.md)

一个以证据为中心的研究与报告工作流。给定研究问题、范围和验收要求，系统记录检索与资料快照，整理可定位的主张，检查需求覆盖，并生成带引用的报告。运行状态保存在本地，可查看、导出和恢复。

> 当前为开发中的实现。离线 `replay` 可复现测试流程；`assisted` 需要外部会话逐步提交模型、检索和抓取结果。自动联网的 `live` 模式尚未开放。

## 能做什么

- **明确研究边界**：用 JSON 合约定义问题、版本范围、必答需求、证据要求和报告结构；用 TOML 配置运行方式与预算。
- **追踪证据**：保存来源快照、原文定位、主张、审核结果及引用关系。报告中的事实可回查到本地保存的资料。
- **迭代研究与大纲**：从一级问题骨架开始，根据检索资料中的实质子主题逐步添加二、三级标题；父章节概述，子章节按绑定证据展开论述与限制，并保留版本历史。详见[大纲设计](docs/research/outline.md)。
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
