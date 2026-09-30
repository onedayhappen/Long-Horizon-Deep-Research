# 显式复用旧研究

实现依据：LongHorizon-Harness `demo.md` 0.15 第 17 节。运行时不依赖该仓库或该文件。

## 命令

```powershell
python -m pip install --only-binary=apsw -e ".[research,test]"
python -m src research run --contract contracts/b.json --config research.toml --runs-root research-runs --run-id B --reuse-from research-runs/A
python -m src research resume --run-dir research-runs/B
python -m src research reuse-sync --run-dir research-runs/B
```

一次显式指定一个来源，来源与目标必须属于同一 runs-root、同一 OS 用户。普通 `run` 不读取其他 run。来源可以未完成，但必须有有效数据库、创建身份和可验证材料。没有创建身份的旧数据库仍可恢复自己的研究；作为复用来源时明确拒绝，不自动补造身份。

`--reuse-selection selection.json` 的严格格式：

```json
{
  "schema_version": 1,
  "source_question_ids": ["R1"],
  "source_evidence_version_ids": []
}
```

两种选择取并集，至少一项非空。省略 selection 时，以有本地审核链接的证据为种子。显式需求选择可以导入尚未完成审核的候选和失败/空结果调查记录。旧调查不会满足新 run 的反证搜索义务。

## 导入与审核

1. 获取来源 lease，验证来源身份、受控 schema、文件权限和路径。活动来源返回 `run_busy`。
2. 按依赖白名单冻结精确版本、payload、原文哈希、调查记录和相关祖先信息。限制为 1,000 条证据、512 MiB 去重 blob、10,000 节点、30,000 边、16 MiB manifest、32 个祖先；超限整体拒绝。
3. 目标持久化 `planned` 清单，释放来源 lease，流式复制 blob。`resume` 按保存的清单继续，不重新读取来源 heads；已复制且校验成功的 blob 不重复复制。
4. 单一事务提交本地实体、映射、候选、谱系和回执。旧审核存成 `historical_audit`，不创建有效支持链接；预算、Coverage、Draft、Stop 均不继承。
5. 每个未满足需求每批最多呈现 20 个主候选，按直接绑定和 FTS5/BM25 排序；规划角色只选择实际需要的绑定，未选中项保留 `not_scheduled`。来源复核按精确文档共享，语义审核按用途独立进行。
6. 本地正向和反向审核通过后，创建新契约的支持主张与链接；原始身份和直接导入来源均可追溯。Writer 只接收可用的本地事实。

当前采取保守处理：所有使用的复用链接均双向审核。已知重大冲突的双方资料会一起导入并呈现给审核者；现有主流程尚无完整冲突裁决门禁，因此这些绑定保持未通过，须继续补证，不能仅凭旧裁决或一句 `no_objection` 放行。安全章节模板只保留占位标题、段落数量和结构来源，旧事实句、数字、引用不复制。

## 时间政策与来源状态

Contract 支持可选 `reuse_policy`；默认展开为 `historical_as_of / strict_as_of / unclassified / 0`。可显式设置：

```json
{
  "reuse_policy": {
    "schema_version": 1,
    "default_rule": {
      "temporal_mode": "historical_as_of",
      "knowledge_mode": "strict_as_of",
      "material_class": "stable",
      "max_validation_age_seconds": 2592000
    },
    "requirement_rules": []
  }
}
```

`0` 表示每个新 worker generation 都要复核一次来源。非零期限到点即失效；同 run 原文和政策未变化时可继续使用已有语义审核。缺失必要日期保持待刷新，未来知识不通过严格截止。`current_at_as_of` 必须同时配置 freshness 检查；freshness 使用明确的 published_at/event_at，不使用抓取或导入时间替代。

`assisted` 会发出 `ASSISTANT_REQUEST source_validation ...`。外部工具/操作者需要检查请求中的文档身份与更正状态，提交 `SourceValidationResult`，包含 validation_key、实际 checked_at、status、method、checked_channels、checks_performed、result_refs 与 observed_raw_hash。普通模型角色不能替代这项工具观察；成功抓取本身也不等于检查过撤稿。JSON Schema 位于 `src/research/schemas/v1/SourceValidationResult.json`。

Replay 通过 fixture manifest 的 `source_validations[url]` 提供固定观察；运行时填充请求 validation_key，并验证文档 hash、检查项和时间。缺少 fixture 明确失败，不编造来源状态。

如果资料日期、版本或来源状态不符合要求，该用途保持 `requires_refresh/rejected`。规划器会收到具体缺口，可用正常搜索/抓取流程收集替代证据；实现不会自动放宽政策。当前普通新抓取路径保留原有审核机制，并新增 freshness 的确定性日期检查；完整统一的新资料来源复核调度仍属于原研究控制器的后续扩展。

## 失效传播与历史交付

提交有证据依据的本地修正：

```json
{
  "schema_version": 1,
  "subject_version_ids": ["精确版本 UUID"],
  "reason_code": "withdrawn",
  "affected_requirement_ids": [],
  "evidence_refs": ["发布方撤稿记录的引用"]
}
```

```powershell
python -m src research invalidate --run-dir research-runs/A --decision correction.json
python -m src research reuse-sync --run-dir research-runs/C
```

每条通知最多 128 个对象、1 MiB，连续 sequence 与哈希链绑定 run_instance_id。C 会直接检查相关 A、B，不要求 B 正在运行；处理回执、用途失效和游标在一个事务中提交。每轮固定水位，最多 5 秒，每页至多 256 条/2 MiB，未消费完保持 pending。通知不会自动授予新支持。

来源移动时，B 的本地原文和历史报告仍可读取；当前使用会因谱系不可达而阻断。用户显式指定同 root 新位置：

```powershell
python -m src research reuse-sync --run-dir research-runs/B --source-run-dir research-runs/moved-A
```

只有 UUID、已知版本哈希及通知游标连续性匹配才更新位置。`status` 保持只读，`current_validity=not_checked` 表示它没有执行当前同步。`export` 可以恢复原交付快照，不把历史完成重新解释为当前有效。

## 存储与验证

研究数据库统一使用 APSW 3.53.4.0 的私有 SQLite，保留本项目现有 `<run-dir>/state.sqlite`、`blobs/` 布局。schema 1 在写入恢复时事务迁移到 schema 2；迁移不补造旧 run 的身份。FTS5 索引与候选同事务提交。每次角色调用保存独立且不可变的 context packet；检查点由状态事务生成，预算仍以最新账本为准。

`tests/research/test_reuse.py` 覆盖可恢复导入、本地重审、精确回放、真实 CLI 分派、按需候选、文档复核共享、时效、冲突闭包、来源锁、损坏、权限边界、三代通知、通知完整性、来源移动、历史导出和 APSW 备份。`reuse_metrics.py` 按单位分别计算成本变化，分母为零返回 null，允许负节省，质量未同时通过时不报告节省结论。

这些离线测试验证协议与预算账本，不代表真实研究质量评测，也没有将第 17 节全部 RU 场景宣称为已验收。

2026-09-30 本机验证：Windows / Python 3.13，`python -m pytest tests/research -q` 为 **94 passed**（其中 26 项新增复用协议测试）；wheel 构建与新增迁移/Schema 打包检查通过。Windows/Linux、Python 3.11/3.12 的 GitHub Actions 矩阵已配置，需 push 后执行。全仓测试还存在原 supervisor/webapi 的 Windows 不兼容失败，例如 `os.killpg` 与安全目录文件描述符操作不可用；本次没有修改这些模块。
