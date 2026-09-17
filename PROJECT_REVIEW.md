# PaperVault 项目校对与审查记录

本文档用于记录 `paper-valut` 当前版本的代码校对结果、关键设计不变量、已验证能力、已知边界，以及后续修改时需要重新核验的项目。它既是开发审查记录，也是发布前检查清单。

## 1. 当前定位

PaperVault 是一个 DOI-first 的个人文献云盘 CLI：

- 本地读取 PDF，计算 SHA-256；
- 从 PDF 元数据、XMP、首页文本和链接中提取 DOI / 标题线索；
- 通过 Crossref 查询并核验元数据；
- 保存规范化 CSL-JSON；
- 将经过核验的 PDF 存入 Google Drive；
- 本地用 SQLite 建立可重建的快速索引；
- 用户可通过 DOI、标题或参考文献文本定位记录并按需下载 PDF。

当前版本目标是个人单写入设备使用，允许其他设备以只读方式连接同一文献库。

## 2. 关键身份模型校对

当前实现明确区分以下四种身份，不应混用：

| 标识 | 用途 | 是否稳定 |
| --- | --- | --- |
| DOI | 标识学术作品 | 作品层面稳定，但并非所有文献都有 DOI |
| `work_id` | PaperVault 内部文献 ID | 库内稳定 |
| SHA-256 | 标识某个具体 PDF 字节版本 | 文件内容不变时稳定 |
| Google Drive `fileId` | 定位实际云端对象 | Drive 对象稳定标识 |

正确关系是：

```text
DOI -> work_id -> 一个或多个 PDF 版本 -> SHA-256 -> Drive fileId
```

同一 DOI 可以存在出版社版、作者稿、批注版等多个不同 SHA-256；相同 SHA-256 不允许静默绑定到不同 DOI。

## 3. 导入流程校对

当前导入顺序符合“先廉价去重，再做昂贵识别和网络请求”的原则：

1. 校验输入为普通文件，拒绝符号链接。
2. 记录文件指纹并计算 SHA-256。
3. 先查本地 `blobs` 索引。
4. 完全相同的 SHA-256 已归档时，默认跳过 PDF 解析、Crossref 查询和重新上传。
5. 未命中时复制到受控 staging 快照。
6. 在独立进程中解析 PDF，限制运行时间和页面数量。
7. DOI 精确查询优先；必要时根据标题走 Crossref bibliographic search。
8. 使用标题、作者、年份等首页证据判断 PDF 与 DOI 是否匹配。
9. 自动证据不足时进入人工复核，而不是自动上传。
10. 上传 PDF 后校验远端大小与 SHA-256。
11. 再提交不可变文献记录版本。
12. 成功后清理 staging 快照，默认保留用户原始 PDF。

此顺序能够避免“参考文献中的真实 DOI 被误认成本篇 DOI”以及“上传成功但记录没有持久化”的常见风险。

## 4. 重复、版本与替换策略校对

### 相同文件

相同 SHA-256 再次导入时：

- 默认复用已有记录；
- 不再次解析 PDF；
- 不再次请求 Crossref；
- 不再次上传 PDF；
- 会重新确认远端对象仍然存在且校验一致。

### 同 DOI、不同文件

默认不会自动改变现有文献：

- `--keep-version`：保留为同一 DOI 下的另一个文件版本；
- `--force`：作为替换流程处理；
- 无上述参数：标记为 `skipped_existing_doi`。

### 安全替换

替换遵循：

```text
上传新 PDF
-> 校验新 PDF
-> 提交新记录版本
-> 确认新记录可读
-> 将旧 blob 移入 Google Drive 回收站
```

因此记录提交失败时，旧文件不会先被删除。旧文件清理失败时，会保留 pending operation，可通过 `pv recover` 恢复完成。

## 5. 云端目录规模校对

为避免几十万文件平铺在一个 Google Drive 文件夹中，当前使用 256 路前缀分片：

```text
PaperVault/
  vault.json
  objects/
    00/
    01/
    ...
    ff/
  records/
    00/
    01/
    ...
    ff/
```

- `objects/<sha256前2位>/` 保存 PDF；
- `records/<doi_key前2位>/` 保存不可变 JSON 记录版本；
- 程序使用 Drive `fileId` 和 `appProperties` 做定位，不依赖文件名唯一性。

显式 `pv rebuild --from-drive` 会递归遍历全部 record 分片；日常 DOI 查询不会为找一篇文章扫描整个库。

## 6. 本地索引规模校对

SQLite 当前包含：

- DOI -> work 映射；
- SHA-256 -> 文件映射；
- commit 历史；
- 导入 jobs；
- cache 状态；
- FTS5 trigram / unicode61 文本检索索引。

项目已有 500,000 条合成记录基准。当前 `BENCHMARK.json` 记录的结果为：

| 指标 | 当前记录 |
| --- | ---: |
| 记录数 | 500,000 |
| 建索引时间 | 63.83 s |
| SQLite 文件 | 约 1.23 GB |
| 峰值 RSS | 约 57 MB |
| DOI 查询 median | 0.014 ms |
| SHA-256 查询 median | 0.011 ms |
| 标题查询 median | 1.119 ms |

该结果只代表合成本地索引性能，不代表 50 万真实 PDF 的总体导入时间。真实批量导入主要受磁盘读取、PDF 文本提取、Crossref 速率和 Google Drive 上传带宽影响。

## 7. 恢复机制校对

当前已设计并测试以下恢复路径：

- Google Drive 分块续传中断后恢复；
- 上传成功但客户端丢失响应时探测远端状态；
- record 上传失败后重试；
- record 实际成功但本地未收到成功响应时避免重复 commit；
- remove / replace 完成记录提交但旧文件尚未移入回收站时通过 `pv recover` 收尾；
- 本地 SQLite 删除后通过 `pv rebuild --from-drive` 重建；
- 云端记录发生断链、分叉、冲突时拒绝静默覆盖本地索引。

## 8. 删除与本地清理校对

`pv remove` 与 `pv release-local` 是两种完全不同的操作：

- `pv remove`：修改 PaperVault 云端文献关系，并把受管 PDF 移入 Google Drive 回收站；
- `pv release-local`：确认云端副本完整后，把用户本地原始 PDF 移入操作系统回收站。

`release-local` 在真正清理本地文件前会：

1. 确认对应导入任务已成功归档；
2. 检查源文件的 inode / size / mtime / SHA-256 未变化；
3. 同步目标 DOI 记录；
4. 从 Google Drive 完整下载并重新计算 SHA-256；
5. 网络操作后再次检查本地文件没有发生变化；
6. 最后才调用系统回收站。

不会永久删除，也不会自动清空系统或 Google Drive 回收站。

## 9. 安全校对

当前项目应保持以下规则：

- OAuth client secret 不提交到 Git；
- refresh token 默认存系统钥匙串；
- 明文 token 文件仅在用户明确选择 file token store 时使用；
- `.papervault/` 本地状态目录不提交；
- Drive 授权请求仅发送给允许的 Google API 主机；
- CLI 顶层异常不得打印 Bearer token、续传 URL 或完整凭据对象；
- 下载先写 `.part` 临时文件，校验通过后再原子发布；
- 不覆盖用户已有的不同内容文件；
- cache 中被用户修改过的 PDF 不自动淘汰。

## 10. 当前已知边界

这些是当前明确的产品边界，不应误写成 bug：

1. 首版只严格接收 Crossref 可核验的期刊论文。
2. OCR 尚未内置；纯扫描 PDF 通常需要人工提供 DOI 并复核。
3. 不负责从出版社自动获取全文。
4. 不支持多设备同时写入；只允许一个 writer device。
5. 下载暂未实现跨进程断点续传；上传支持。
6. `drive.file` 只能管理应用创建或用户授予应用访问的文件，不等价于任意扫描整块 Google Drive。
7. Google Drive 不是独立备份策略；账号丢失或用户清空回收站后仍可能永久丢失文件。
8. Crossref 标题搜索只能提供候选，不能把第一条搜索结果直接视为正确 DOI。

## 11. 每次发布前必须重新检查

- [ ] `uv run pytest -q`
- [ ] `uv run ruff check src tests scripts`
- [ ] `uv run ruff format --check src tests scripts`
- [ ] `pv --version`
- [ ] `pv doctor --json`
- [ ] 至少一次隔离的 Drive smoke test
- [ ] `.gitignore` 未遗漏 OAuth / token / `.papervault` / `.env`
- [ ] `git status` 中没有 `.DS_Store`、虚拟环境、测试缓存或凭据
- [ ] README、USAGE、DESIGN 与实际 CLI 参数一致
- [ ] replace/remove 的测试仍覆盖“record 成功/失败”和“Trash 成功/失败”顺序
- [ ] rebuild 仍可从云端 records 恢复本地索引
- [ ] 不把真实用户 PDF 或真实 token 放入测试 fixture

## 12. 当前校对结论

当前架构已经形成完整闭环：

```text
PDF -> SHA-256 去重 -> DOI/标题识别 -> Crossref 核验 -> CSL
    -> Google Drive 分片归档 -> 本地可重建 SQLite 索引
    -> DOI/标题/文本检索 -> 按需下载并校验
```

核心设计适合继续作为大规模个人文献库 CLI 演进。后续最值得单独规划的方向是：OCR、批量并发/限流调度、增量远端同步、多 writer 协议，以及额外备份后端；这些功能不应在没有新的事务和冲突模型之前直接塞入当前写入流程。

