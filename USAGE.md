# PaperVault 使用手册

本文档按“第一次配置 -> 导入 -> 查找 -> 下载 -> 替换/删除 -> 恢复”的实际使用顺序说明 PaperVault CLI。

## 1. 环境要求

- Python 3.12+
- 推荐安装 `uv`
- Google 账号
- 已创建 Google Cloud 项目并启用 Google Drive API
- OAuth Client 类型必须为 **Desktop app**

进入项目目录：

```bash
cd paper-valut
```

安装依赖：

```bash
uv sync --python 3.12
```

开发目录中运行：

```bash
uv run pv --help
```

安装为全局命令：

```bash
uv tool install --editable .
pv --version
```

## 2. Google OAuth 首次配置

下载 Desktop OAuth 客户端 JSON，例如：

```text
client_secret_xxx.apps.googleusercontent.com.json
```

登录：

```bash
pv auth login --client ~/Downloads/client_secret_xxx.apps.googleusercontent.com.json
```

默认会打开浏览器。完成 Google 授权后检查：

```bash
pv auth status --json
```

如果 Google OAuth 应用仍处于 Testing 状态，需要在 Google Auth Platform 的测试用户中加入实际登录账号，否则会出现 `403 access_denied`。

刷新令牌默认保存到系统钥匙串，不会保存在项目目录。

## 3. 创建文献库

首次创建：

```bash
pv init --name PaperVault
```

检查：

```bash
pv doctor --json
```

正常情况下应看到：

- Crossref：OK
- Google Drive：OK
- vault：OK

云端会形成：

```text
PaperVault/
  vault.json
  objects/
    00/
      <SHA-256>.pdf
      <SHA-256>.csl.json
    ...
    ff/
  records/
    00/ ... ff/
```

分片文件夹按需创建，不要求一开始就出现 256 个目录。

每个云端 PDF 都有一个同目录、同 SHA-256 文件名前缀的标准 CSL-JSON 旁车。旁车用于人工查看和与 Zotero、WOSAIDE 等工具互操作；`records` 下的不可变 record JSON 仍是 PaperVault 恢复、历史追踪和一致性判断的权威来源。

## 4. 基础配置

推荐设置 Crossref 联系邮箱：

```bash
pv config --mailto your-email@example.com
```

设置单个 PDF 最大允许大小：

```bash
pv config --max-pdf-mb 200
```

设置受管阅读缓存上限：

```bash
pv config --cache-limit-mb 2048
```

## 5. 导入单篇 PDF

最简单的导入：

```bash
pv import ./paper.pdf
```

如果你明确知道 DOI：

```bash
pv import ./paper.pdf --doi '10.1038/s41586-020-2649-2'
```

注意：`--doi` 只是告诉程序优先核验哪个 DOI，并不会绕过 PDF 内容匹配。

先预览、不上传：

```bash
pv import ./paper.pdf --dry-run
```

## 6. 批量导入目录

当前目录：

```bash
pv import ~/Papers
```

递归扫描：

```bash
pv import ~/Papers --recursive
```

这个命令可以重复运行。PaperVault 会先对每个 PDF 计算 SHA-256 并查询本地索引：完全相同且已经归档的文件会直接复用，不重新解析 PDF、不查 Crossref、也不重复上传 PDF/CSL。文件改名或移动目录不会影响去重。只有新增文件或字节发生变化的 PDF 才继续进入识别流程。

大量文件时使用 JSON Lines，避免把几万条结果一次性堆在最终 JSON 中：

```bash
pv import ~/Papers --recursive --jsonl
```

导入过程中每个文件都会有 job 状态，失败不会让已经完成的文件失效。

## 7. 重复文件如何处理

### 完全相同的 PDF

PaperVault 先计算 SHA-256。

如果文件已经归档：

- 不重新解析 PDF；
- 不重新查询 Crossref；
- 不重新上传；
- 不重复创建 CSL sidecar；
- 验证现有 Drive 对象后直接返回已有归档信息。

因此可以安全地反复扫描同一个下载目录。

### 同 DOI，但文件内容不同

默认：

```text
skipped_existing_doi
```

这防止新下载的未知版本静默覆盖已有文件。

保留为另一个版本：

```bash
pv import ./annotated.pdf \
  --doi '10.xxxx/example' \
  --role annotated \
  --keep-version
```

可用角色包括：

```text
unspecified
publisher
accepted-manuscript
annotated
supplement
```

## 8. 强制替换文献文件

直接在导入阶段替换主文件：

```bash
pv import ./replacement.pdf \
  --doi '10.xxxx/example' \
  --force
```

或者先指定旧文件版本：

```bash
pv replace '10.xxxx/example' ./replacement.pdf \
  --sha256 '<旧文件完整SHA-256>'
```

上面的命令默认只预览。真正执行：

```bash
pv replace '10.xxxx/example' ./replacement.pdf \
  --sha256 '<旧文件完整SHA-256>' \
  --apply
```

替换顺序是“新 PDF + CSL sidecar 上传并校验 -> 新 record 提交 -> 旧 PDF + 旧 CSL sidecar 移入 Drive 回收站”，不会先删旧文件。

## 9. 人工复核

自动匹配证据不足的文献进入 review 队列：

```bash
pv review list
```

查看详情：

```bash
pv review show <job_id>
```

你已经人工核对首页标题、作者、年份和 DOI 后：

```bash
pv review resolve <job_id> \
  --doi '10.xxxx/example' \
  --reason '已核对首页标题、作者、年份与 DOI'
```

人工复核可以覆盖自动证据不足，但不能绕过以下检查：

- DOI 必须是有效 Crossref 条目；
- 必需元数据必须存在；
- PDF 必须可解析；
- 文件哈希不能与另一 DOI 冲突。

## 10. 查看导入任务

```bash
pv jobs list
```

重试单个任务：

```bash
pv jobs retry <job_id>
```

重试所有可恢复失败：

```bash
pv jobs retry --failed
```

## 11. 搜索本地文献库

DOI：

```bash
pv search '10.1038/s41586-020-2649-2'
```

标题关键词：

```bash
pv search 'Array programming NumPy'
```

作者 + 标题词：

```bash
pv search 'Harris NumPy'
```

本地搜索使用 SQLite FTS 索引，不会逐条 Python 扫描 50 万条记录。

## 12. 用 Crossref 查 DOI

输入完整标题或参考文献文本：

```bash
pv resolve 'Array programming with NumPy'
```

直接查看 Crossref 搜索结果：

```bash
pv search 'Array programming with NumPy' --remote
```

Crossref 搜索结果表示“候选文献”，不代表你的 Google Drive 中已经有 PDF。

## 13. 下载 PDF

按 DOI：

```bash
pv get '10.1038/s41586-020-2649-2' --output ~/Downloads
```

下载指定文件版本：

```bash
pv get '10.xxxx/example' \
  --sha256 '<完整SHA-256>' \
  --output ~/Downloads
```

输出文件默认命名为：

```text
<SHA-256>.pdf
```

下载时先写临时文件，重新校验 SHA-256 后才生成最终文件；如果目标路径已有不同内容，不会覆盖。

## 14. 标题 / 文本直接解析后下载

当你只有完整标题：

```bash
pv get 'Array programming with NumPy' \
  --resolve \
  --output ~/Downloads
```

如果 Crossref 有多个候选，先查看：

```bash
pv resolve '你的参考文献文本'
```

然后明确指定：

```bash
pv get '你的参考文献文本' \
  --resolve \
  --select-doi '10.xxxx/correct-doi' \
  --output ~/Downloads
```

解析到 DOI 不代表一定能下载；只有该 DOI 已经在你的 PaperVault 中归档了 PDF 才能获取文件。

## 15. 直接打开文献

```bash
pv open '10.xxxx/example'
```

它会把 PDF 下载到受管 cache，然后调用系统默认 PDF 阅读器。

缓存默认 pin，避免你还在阅读时被自动清理。

确认阅读器关闭后：

```bash
pv cache prune --readers-closed
```

如果你对缓存 PDF 做了批注导致哈希变化，PaperVault 会保留该文件而不是删除它。之后可以重新导入为 `annotated` 版本。

## 16. 查看一篇文献及其文件版本

完整记录：

```bash
pv show '10.xxxx/example'
```

只列文件：

```bash
pv files '10.xxxx/example'
```

切换默认文件：

```bash
pv primary '10.xxxx/example' --sha256 '<完整SHA-256>'
```

## 17. 从 PaperVault 中移除文件

预览整篇文献的移除：

```bash
pv remove '10.xxxx/example'
```

真正执行：

```bash
pv remove '10.xxxx/example' --apply
```

只移除一个文件版本：

```bash
pv remove '10.xxxx/example' \
  --sha256 '<完整SHA-256>' \
  --apply
```

程序会先提交新的 record/tombstone，再把 PDF 及对应 CSL sidecar 移入 Google Drive 回收站。

## 18. 安全释放本地原始 PDF

导入成功后，PaperVault 默认不会删本机原文件。

先获取导入返回的 `batch_id`，预览：

```bash
pv release-local --batch <batch_id> --dry-run
```

真正执行：

```bash
pv release-local --batch <batch_id> --apply
```

执行前程序会从 Google Drive 实际重新下载并验证 PDF，再检查本地文件未发生变化，然后移入系统回收站。

## 19. 元数据刷新

```bash
pv metadata refresh '10.xxxx/example'
```

刷新会创建新的不可变 record 版本，不会直接覆盖旧 JSON 历史。

刷新还会为当前 PDF 版本生成新的 CSL sidecar；新 record 成功提交后，旧 sidecar 才会移入 Drive 回收站。若中途出现网络或 API 故障，可使用 `pv recover` 恢复一致状态。

## 20. 导出 CSL-JSON

```bash
pv export --format csl-json --output ./library.json
```

目标文件已经存在时默认拒绝覆盖。

## 21. 校验所有云端 PDF

```bash
pv verify --all
```

该命令会完整下载每个 PDF 并核验 SHA-256，同时检查对应 CSL sidecar 是否存在、归属正确且内容与当前 record 中的 CSL 一致；不保留验证副本。对于几十万篇大型库，这是非常重的维护操作，不应频繁执行。

## 22. 本地索引丢失后的重建

```bash
pv rebuild --from-drive
```

重建只读取 `records` JSON，不会把全部 PDF 下载回来。

如果记录链出现：

- 缺失 parent；
- 分叉；
- 重复 DOI；
- SHA-256 冲突；

程序会停止，而不是静默选择一个版本覆盖现有索引。

## 23. 中断操作恢复

```bash
pv recover
```

主要用于：

- 元数据更新的 commit 已准备但响应中断；
- primary 修改未完成确认；
- remove / replace 已提交新记录，但旧 Drive 文件尚未完成移入回收站。

## 24. 换电脑连接已有文献库

新电脑先用同一个 Google OAuth 应用登录：

```bash
pv auth login --client /path/to/client_secret.json
```

列出可见库：

```bash
pv vaults
```

连接：

```bash
pv connect <vault.json的Google Drive fileId>
```

然后重建：

```bash
pv rebuild --from-drive
```

新设备默认是只读设备。当前版本不允许多台电脑同时写入。

## 25. 使用独立本地状态目录

全局参数：

```bash
pv --home ~/my-vault-state doctor
```

也可以：

```bash
export PAPERVAULT_HOME=~/my-vault-state
pv doctor
```

不同 PaperVault 库应使用不同本地状态目录。

## 26. 常见错误

### Google 登录出现 403 access_denied

应用仍在 Testing，登录账号不在测试用户列表中。将账号加入 Google Auth Platform 的测试用户后重新：

```bash
pv auth login
```

### `pv` 命令找不到

```bash
uv tool install --editable .
```

或直接：

```bash
uv run pv --help
```

### Crossref 查询失败

先执行：

```bash
pv doctor --json
```

网络、429 和部分 5xx 会按可重试故障处理，随后可以：

```bash
pv jobs retry --failed
```

### PDF 已有 DOI，但仍进入 review

这是正常的安全策略。仅“找到一个合法 DOI”不足以证明它属于当前 PDF，因为 DOI 也可能来自参考文献列表。

### 标题能在 Crossref 查到，但 `pv get` 提示本库没有对应文献

Crossref 只提供元数据。PaperVault 只能下载你已经自行归档到 Google Drive 的 PDF。

## 27. 日常推荐工作流

### 新论文下载后

```bash
pv import ~/Downloads/paper.pdf
```

### 批量整理 Downloads/Papers

```bash
pv import ~/Downloads/Papers --recursive --jsonl
pv review list
pv jobs retry --failed
```

### 确认归档后释放本地空间

```bash
pv release-local --batch <batch_id> --dry-run
pv release-local --batch <batch_id> --apply
```

### 以后需要阅读

```bash
pv open '10.xxxx/example'
```

或者：

```bash
pv get '论文完整标题' --resolve --output ~/Downloads
```

## 28. 开发者验证

```bash
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

真实 Drive smoke test：

```bash
uv run python scripts/smoke_drive.py
```

该脚本应只使用隔离临时目录，验证完成后将测试目录移入 Google Drive 回收站，不得把合成测试 PDF 写入正式论文记录。

