# PaperVault CLI

将经过 DOI / Crossref 核对的 PDF 归档到 Google Drive，按 DOI 或标题找回文件。本地只需保留元数据索引与按需缓存。所有导入默认保留原件；清理是独立命令。

详细操作见 [USAGE.md](USAGE.md)；当前代码校对、架构不变量和发布检查见 [PROJECT_REVIEW.md](PROJECT_REVIEW.md)。

当前为个人库 v0.1：期刊论文 PDF、Crossref 严格准入、单设备写入、其他设备只读。普通扫描 PDF 可人工核对 DOI 后归档；不包含 OCR、自动抓取出版社全文、多人同步写入或无 DOI 条目。

## 已实现

- 桌面 OAuth + PKCE；默认把刷新令牌保存到系统钥匙串。
- PDF 元数据、XMP、首页正文及链接提取；独立解析进程，45 秒超时。
- DOI 精确查询、标题回查 Crossref、保守自动匹配、人工复核队列。
- 保留 Crossref 原始响应和 CSL 接口原始响应，生成规范化 CSL-JSON。
- 原始文件 SHA-256 去重、同 DOI 多文件版本、显式主版本选择。
- 每个云端 PDF 同目录保存标准 `<SHA-256>.csl.json` 旁车；完整不可变 record 仍是权威元数据历史。
- 支持大规模本地索引与分片目录。
- Google Drive 分块续传、进程重启恢复、预分配 ID 避免重复创建。
- 远端文件大小/校验和验证；服务器无 SHA-256 时回读文件验证。
- 不可变的记录版本；从云端重建 SQLite，检测断链、分叉和 DOI 冲突。
- 下载先写临时文件，通过 SHA-256 验证后发布；不覆盖已有不同文件。
- CSL 导出、元数据刷新、任务恢复、缓存保护与显式清理原件。

## 安装

需要 Python 3.12+，推荐使用 uv：

```bash
uv sync --python 3.12
uv run pv --help

# 安装可从任意目录调用的命令；此安装会跟随本目录代码更新
uv tool install --editable .
pv --version
```

不使用 uv 时，可以在 Python 3.12+ 虚拟环境中 `pip install .`。

默认输出为可阅读的 JSON。`--json` 保证业务错误也以 JSON 输出；进度始终输出到 stderr。全局 `--home` 放在命令前，为不同文献库使用不同本地目录。

## 首次 Google 配置

在 Google Cloud 项目中启用 Drive API，配置 OAuth 应用并创建 **Desktop app** 客户端。若应用处于 Testing，务必将实际登录账号加入 **Google Auth Platform → 目标对象 → 测试用户**。

```bash
pv auth login --client /path/to/client_secret.json
pv init --name PaperVault
pv doctor
```

程序仅申请 `drive.file`，可管理该应用创建或获准访问的文件。它不能凭任意文件夹 ID 读取里面所有历史文件。首版面向 My Drive，不提供 Shared Drive 管理。[Google 权限范围](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

本机路径：

```text
~/.papervault/
  client.json          # Desktop OAuth 配置，权限 600
  config.json          # 库 ID、文件夹 ID、设备 ID；权限 600
  index.sqlite3        # 可重建的文献索引 + 本机导入任务日志
  crossref/            # 30 天元数据缓存
  staging/             # 待复核或失败任务的文件快照
  uploads/             # 私有续传会话状态
  pending-records/     # 尚未确认成功的元数据修改提交
  pending-ops/         # 待恢复的删除或替换操作
  cache/               # 按需阅读缓存
```

父目录权限为 700。刷新令牌默认在系统钥匙串中，不在上述文件中。无系统钥匙串的环境可显式使用 `pv auth login --token-store file`；该选项把令牌保存在本机权限 600 的 `token.json`。配置、令牌、上传会话地址不得加入版本控制或公开日志。

Google 的 External + Testing 应用涉及 Drive 权限时，刷新令牌通常 7 天过期；失效后运行 `pv auth login`。长期使用应根据 Google 控制台要求调整发布配置。Production 也不代表授权永不过期。[Google 令牌说明](https://developers.google.com/identity/protocols/oauth2#expiration)

## 导入文献

先预览：

```bash
pv import ~/Papers --recursive --dry-run
```

dry-run 可以查询 Crossref 并使用本机元数据缓存，但不上传、不修改原件、不留下导入任务。正式导入：

```bash
pv import ~/Papers --recursive
pv import ./paper.pdf --doi '10.1038/s41586-020-2649-2'
pv import ./annotated.pdf --doi '10.1038/s41586-020-2649-2' --role annotated
pv import ./annotated.pdf --doi '10.1038/s41586-020-2649-2' --keep-version
pv import ./replacement.pdf --doi '10.1038/s41586-020-2649-2' --force
```

`--doi` 仅能用于单个文件，并不会绕过 PDF 匹配。自动接受要求长标题在首页精确出现、作者支持，以及年份或标题元数据支持。短标题、多个候选、扫描件、识别不清的 PDF 会进入待复核队列。标题匹配阈值是保守规则，尚未在用户真实文献集上校准，不能将自动匹配视为身份真实性保证。

Crossref 严格准入要求：有效 DOI、期刊论文类型、标题、作者与出版年份。非 Crossref DOI、图书、会议论文暂不收录。CSL 转换会把 Crossref 实际返回的 `journal-article` 映射为标准 `article-journal`，清除非 CSL 字段，并独立保留完整服务端响应。

每次导入返回 `batch_id` 和各文件 `job_id`。待复核及失败任务保留原始字节快照，所以在处理这些任务之前，本地会暂时占用额外空间。成功后自动清理快照，原文件仍保留。

正式导入先计算文件哈希，再查询本地索引。若完全相同的文件已经归档，默认直接复用，不再次解析 PDF、查询 Crossref 或上传。文件是否改名、移动到别的子目录不影响 SHA-256 去重。因此可以反复执行 `pv import <目录> --recursive`：已归档原样文件会自动跳过，只有新增或发生字节变化的 PDF 才进入后续识别流程。若 DOI 已存在但文件内容不同，默认跳过；需要保留另一版本时使用 `--keep-version`，需要替换时使用 `--force`。

```bash
pv review list
pv review show <job_id>
pv review resolve <job_id> --doi '10.xxxx/example' --reason '已逐项核对首页标题、作者和年份'
pv jobs list
pv jobs retry --failed
pv jobs retry <job_id>
```

复核理由必须真实填写；人工确认可覆盖自动匹配不足，不能跳过 Crossref 存在性、必需字段和文件哈希冲突检查。格式损坏的 PDF 无法通过复核直接上传，需要修复后重新导入。

## 云端结构

```text
PaperVault/
  vault.json
  objects/
    00/
      00<其余62位SHA-256>.pdf
      00<其余62位SHA-256>.csl.json
    ...
    ff/
  records/
    00/
      <work_id>.<commit_id>.json
    ...
    ff/
```

DOI 是文献外部标识；`work_id` 是内部文献 ID；SHA-256 标识具体字节；Drive fileId 是云端定位标识。文件名相同不等于 Drive 对象相同，程序使用 fileId。

`objects` 按 SHA-256 前两位分片；每个 PDF 旁边保存同名 `<SHA-256>.csl.json`。旁车文件是标准 CSL-JSON，可脱离 PaperVault 被其他工具直接读取；`records` 中不可变 JSON 仍是权威档案历史。`records` 按 DOI 的 SHA-256 key 前两位分片。程序依靠 Drive fileId 与 `appProperties` 定位对象，而不是依赖文件名。这样不会把 50 万篇文献全部堆进单一 Drive 文件夹。

每个记录 JSON 保存全量 CSL、原始元数据、匹配依据、全部 PDF 关联及主文件选择。更新生成新记录，旧记录保留。不可直接手工修改 records 内容或覆盖 objects 文件；异常会阻止重建或后续清理。

同 DOI + 同字节默认直接复用。同 DOI + 不同字节默认跳过；显式 `--keep-version` 才保留不同版本，`--force` 或 `replace` 才替换文件。同一字节关联不同 DOI 会停止并报告冲突，不自动合并。

## 检索、下载、阅读和导出

```bash
pv search '标题关键词'
pv search 'Array programming with NumPy' --remote
pv resolve 'Array programming with NumPy'
pv show '10.1038/s41586-020-2649-2'
pv files '10.1038/s41586-020-2649-2'
pv get '10.1038/s41586-020-2649-2' --output ~/Downloads
pv get 'Array programming with NumPy' --resolve --output ~/Downloads
pv get '一段参考文献文本' --resolve --select-doi '10.xxxx/example' --output ~/Downloads
pv get '10.1038/s41586-020-2649-2' --sha256 '<完整哈希>' --output ~/Downloads
pv open '10.1038/s41586-020-2649-2'
pv primary '10.1038/s41586-020-2649-2' --sha256 '<完整哈希>'
pv export --format csl-json --output ./library.json
```

标题和作者搜索使用本地 SQLite FTS 索引，不触发云端全库同步。DOI 查询未命中时只查询该 DOI 对应的云端记录链。需要从标题或参考文献文本解析 DOI 时使用 `pv resolve` 或 `get --resolve`；有歧义时显式 `--select-doi`。`--remote` 查询 Crossref，并标明本地索引是否已归档，它不会自动从出版社下载 PDF。

云端 PDF 可以安全剔除或替换：

```bash
pv remove '10.xxxx/example'
pv remove '10.xxxx/example' --sha256 '<hash>' --apply
pv replace '10.xxxx/example' ./new.pdf
pv replace '10.xxxx/example' ./new.pdf --apply
pv replace '10.xxxx/example' ./new.pdf --sha256 '<old-hash>' --apply
pv recover
```

`remove` 和 `replace` 默认只预览。应用变更时，程序先提交新的可恢复记录，再把不再被当前记录引用的旧 PDF blob 与对应 CSL sidecar 一起移入 Drive 回收站；若回收站动作失败，可用 `pv recover` 继续，不会先破坏旧记录。

下载输出为 `<SHA-256>.pdf`。下载中断时删除不完整临时文件，重试重新下载；**当前仅上传支持跨进程断点续传，下载尚不支持断点续传**。本地已有相同哈希的输出文件时复用，不重复下载。

`open` 生成受管缓存并调用系统阅读器。由于无法可靠判断阅读器是否仍在使用文件，打开的缓存默认 pin；不会在后台自动删除。确认阅读器关闭后可运行：

```bash
pv config --cache-limit-mb 2048
pv cache prune --readers-closed
```

按最近使用时间将未修改、解除保护的缓存淘汰到指定容量以内。带批注等哈希已变化的缓存始终保留，可把其路径重新 `pv import ... --role annotated`。指定 `--output` 下载到其他目录的文件不属于自动缓存。缓存容量是软上限，受保护或修改后的文件会使其超限。

## 本地原件清理

```bash
pv release-local --batch <batch_id> --dry-run
pv release-local --batch <batch_id> --apply
```

清理前请关闭会编辑原件的应用。命令会同步云端记录、逐文件实际下载核验 SHA-256，再检查本地原件的身份、大小、修改时间和哈希。只有全部通过，`--apply` 才移入系统回收站；从不永久删除或清空回收站。

原件变化、符号链接、云端缺失、权限不足、档案冲突或下载不完整都会跳过。只有清空系统回收站才真正释放对应磁盘空间。

只有一份 Google Drive 文件意味着单副本存储。需要容灾时应另外保留独立备份；本工具不保证云端误删或账号不可用情况下能找回文件。

## 恢复和维护

```bash
pv rebuild --from-drive
pv verify --all
pv metadata refresh '10.1038/s41586-020-2649-2'
pv recover
```

`rebuild` 只读取云端记录，不下载全部 PDF；`verify --all` 实际下载核验所有 PDF，并校验其 CSL sidecar 与当前 record 元数据一致，但不保留验证副本。记录校验失败时保留原本索引，报告错误。不可变记录的远端 SHA-256 没变时复用本地记录缓存，避免重复下载元数据。

`rebuild` 是显式灾难恢复/迁移操作，会遍历 records 分片；正常导入、精确 DOI 查询、哈希去重、下载、替换和删除不依赖全库扫描。

`jobs retry` 恢复导入，`recover` 恢复元数据刷新和 primary 修改的待确认提交。上传会话过期会重新开始，使用同一个预生成 Drive ID；记录已提交但响应丢失时识别已成功提交，避免重建一份。

换电脑先用同一 OAuth 应用和 Google 账号授权，再连接：

```bash
pv auth login --client /path/to/the-same-desktop-client.json
pv vaults
pv connect <vault.json的fileId>
pv rebuild --from-drive
```

新电脑默认只读。首版没有自动写入设备接管功能；若迁移唯一写入设备，先完全停止原设备，安全迁移其 `config.json`（保留 device_id），再重新授权并重建索引。不能复制这套写入配置后同时在两台机器导入；本机锁不是分布式锁。

更换 OAuth 项目后，原 `drive.file` 授权及应用私有属性不能假定继续有效，需要重新授予文件访问或进行迁移。仅有云端记录可恢复文献和文件关联，不含本机源文件路径与导入批次；本机任务日志丢失后，需重新导入本地原件建立清理批次。

## 配置、退出码和排错

```bash
pv config --mailto your-email@example.com
pv config --max-pdf-mb 200
pv doctor --json
```

支持环境变量 `PAPERVAULT_HOME` 和 requests 标准代理变量 `HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY`；回环授权服务需保证浏览器能访问 `127.0.0.1`。

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 待复核、不符合准入条件或参数对应对象不明确 |
| 3 | 可重试网络或 API 故障 |
| 4 | 配置、授权、文件权限或未分类运行错误 |
| 5 | 完整性、档案或文件关联冲突 |
| 130 | 用户中断 |

- 登录页 `403 access_denied` 且提示正在测试：把登录账号加入当前项目测试用户，重新 `pv auth login`。
- 登录页等待超时：重新 `pv auth login`；无需重新下载客户端配置。
- OAuth 被撤销或 Testing 令牌到期：重新登录；无需重建云端文件。
- API 403：检查 Drive API 是否启用、scope、目标文件权限及容量。
- 网络、429、临时 5xx：程序有限重试；批次结束后用 `pv jobs retry --failed`。
- `pv` 不在 PATH：运行 `~/.local/bin/pv` 或 `uv run pv`。

## 开发与验证

```bash
uv sync --python 3.12
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

`scripts/benchmark_index.py` 可生成 50 万条合成本地索引基准。当前 `BENCHMARK.json` 记录 500,000 条：DOI 查询中位数约 0.014 ms、SHA-256 查询约 0.011 ms、标题查询约 1.119 ms。该结果只衡量本地 SQLite 投影，不代表 Google Drive 或 Crossref 网络吞吐量。

真实 Drive 传输测试（会创建独立临时测试文件夹，完成后移入云盘回收站）：

```bash
uv run python scripts/smoke_drive.py
```

完整设计背景见 [DESIGN.md](DESIGN.md)，实现范围与具体命令以本 README 为准。
