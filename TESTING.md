# v0.1 验证记录

执行日期：2026-09-13。

## 自动化验证

- `uv run pytest -q`：46 个测试通过。
- `uv run ruff check src tests scripts`：通过。
- `uv run ruff format --check src tests scripts`：通过。
- 全局安装后的 `pv --version`：0.1.0；`pv auth status --json`：已授权，令牌存储为系统钥匙串。

测试包括真实 PDF 解析，以及使用可控 Drive / Crossref 替身进行故障注入：

- DOI 规范化、保留合法括号、排除参考文献 DOI。
- 显式 DOI 不绕过匹配；多 DOI 冲突、必需元数据缺失进入复核或拒绝。
- Crossref 实际 CSL 类型兼容转换，并保留原始响应。
- 导入、重复导入、多版本、默认版本选择、下载和索引重建。
- 云端记录上传失败或成功响应丢失后恢复；后续已有新版本时恢复历史成功提交。
- 同一哈希绑定不同 DOI 被阻止。
- 下载损坏不生成最终文件；现有用户文件不被覆盖。
- 本地原件变化、云端文件被删除、完整下载校验失败时，不清理原件。
- 记录断链、分叉、内容篡改时保留旧索引。
- 新设备只读；修改后的缓存不会被淘汰。
- 持久化续传偏移、上传响应丢失后的服务端探测、分页及不完整搜索处理。
- 远端缺 SHA-256 时回读；不把自己写入的属性哈希当作服务端独立校验。
- 禁止向非 Google API 主机发送授权请求；CLI 异常输出隐藏凭据信息。
- 精确 SHA-256 重复导入不会再次解析 PDF、查询 Crossref、扫描全库或上传 blob。
- 新 DOI 导入热路径不调用全库同步；DOI/哈希均走有界索引和目标记录查询。
- 同 DOI 不同字节默认跳过；`--keep-version` 保留版本；`--force` 先提交可恢复的新版本再回收旧 blob。
- remove/replace 的 tombstone 与回收站故障可恢复；重新导入已删除 DOI 会形成合法后继记录。
- objects/records 两位十六进制分片写入；旧版平铺 records 仍可显式全量重建。
- DOI、标题/参考文献文本解析以及歧义候选显式选择路径均有覆盖。

## 50 万条本地索引基准

`scripts/benchmark_index.py` 已对 500,000 条合成文献投影运行，结果保存在 `BENCHMARK.json`：

- 建立 SQLite/FTS 投影：63.83 秒。
- SQLite 文件：1,232,138,240 bytes。
- 进程峰值 RSS：57,049,088 bytes。
- DOI 查询：median 0.014 ms，p95 0.021 ms。
- SHA-256 查询：median 0.011 ms，p95 0.015 ms。
- 标题查询：median 1.119 ms，p95 1.511 ms。

这是合成本地 SQLite 基准，只验证索引路径不会随 50 万篇退化为 Python 全表扫描；不代表 Google Drive 或 Crossref 的网络吞吐量。

## 真实接口验证

- Google Desktop OAuth 浏览器授权完成，刷新令牌保存至系统钥匙串。
- 私有状态目录权限 700；客户端和配置文件权限 600；未生成明文 token.json。
- `pv doctor`：Crossref、Google Drive 和正式库目录检查通过。
- 正式库 `PaperVault` 的 objects、records、vault.json 已创建。
- 实际查询 DOI `10.1038/s41586-020-2649-2`，取得标题 “Array programming with NumPy”，保存原始元数据并规范化 CSL 类型。
- `scripts/smoke_drive.py`：5,244,358 字节合成 PDF 多分块上传、全量下载 SHA-256 核验、重复上传复用 fileId、JSON 上传读回和文件列举均通过。
- 测试数据使用独立临时目录，完成后已移入 Drive 回收站；没有作为论文导入正式库。
- 实际执行 `pv rebuild --from-drive` 成功；正式库当前 0 篇论文、0 条文献记录。

## 验证边界

尚未导入用户的真实论文集，也未测量其自动匹配覆盖率。自动匹配采用保守规则，人工复核仍是必要流程。

故障恢复用可控替身验证；真实 Google 接口验证正常传输和重复请求，未人为断开用户网络或撤销账号授权。真实本地原件清理未执行，相关行为用测试替身核验。程序默认保留原件。
