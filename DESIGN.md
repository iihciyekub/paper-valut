# 文献云归档 CLI 设计草案

日期：2026-09-13。状态：v0.1 CLI 已实现并完成 Google OAuth 与真实 Drive 传输验证；本文保留设计背景，实际命令、首版限制及使用方法以 README.md 为准。

## 1. 产品目标与首版边界

把本地 PDF 识别为一条有可靠 DOI 和 Crossref 元数据的文献，上传原始文件到 Google Drive；此后通过 DOI 或标题检索，按需下载。允许清理已经核验归档的本地原件，日常只保留元数据索引和有限缓存。

首版假设：个人 Google 账号、My Drive、一个文献库、一台指定写入电脑；其他电脑可重建索引、检索、下载。只收录通过 Crossref 验证并确认与 PDF 相符的文献。暂不包含多人并发写入、自动从出版社获取全文、无 DOI 条目、全文向量搜索或文献管理 GUI。

这里的 CSL 指 CSL-JSON 引文数据；不是控制参考文献排版的 .csl 样式文件。Crossref 的普通 REST JSON 与 CSL-JSON 是不同结构，分别保存。Crossref 提供元数据，不保存用户上传的 PDF。[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)、[CSL 内容协商](https://www.crossref.org/documentation/retrieve-metadata/content-negotiation/)、[全文说明](https://www.crossref.org/documentation/retrieve-metadata/text-and-data-mining/)。

## 2. 身份设计

| 对象 | 身份 | 用途 |
|---|---|---|
| 文献记录 | 内部 work_id（UUID） | 保持记录稳定，容许以后纠正错误 DOI |
| 文献外部标识 | 规范化 DOI，库内唯一约束 | 用户检索、匹配 Crossref |
| 文件对象 | 原始字节的完整 SHA-256 | 精确去重、上传下载完整性校验 |
| 云端对象 | Google Drive fileId | 定位、下载；不可由 DOI 或 SHA-256 推导 |
| 记录版本 | commit_id（UUID） | 保存元数据和关联变更历史 |

关系：DOI → work_id → 一个或多个文件关联 → SHA-256 → Drive fileId。

DOI 不与 SHA-256 拼成唯一文献 ID。相同 DOI 的出版社版、作者稿、批注版、补充材料可能字节不同；同一文件改名，哈希不变；添加批注或重新保存，哈希可能改变。SHA-256 判断字节相同，不能判断学术内容相同。

规则：

- 同 DOI、同 SHA-256：复用已核验对象，记录这次导入，不重复上传。
- 同 DOI、不同 SHA-256：批量导入默认跳过，避免静默增加版本；显式 `--keep-version` 才保留为另一版本，显式 `--force`/`replace` 才替换。新文件仍须重新核验匹配。
- 同 SHA-256、不同 DOI：默认进入冲突复核，不能自动把一份合集或误匹配文件绑定给多个条目。
- 不同 DOI、相似标题：不自动合并，可能是勘误、预印本或后续版本。
- 变更 canonical DOI 必须是显式纠错操作；保存旧值和理由，不能把已证实错误的 DOI 保留为可命中的别名。
- 每条文献可选一个 primary 文件；没有明确选择且存在多个候选时，下载命令要求指定版本，禁止静默选择最后上传者。

DOI 规范化：剥离已识别的 doi: 或 DOI URL 包装，谨慎处理编码与排版换行，规范化大小写并保留原始文本。不能粗暴删除所有尾部括号或标点；提取产生的歧义必须经注册记录验证。标题用于搜索，不充当唯一键。

## 3. 从 PDF 到准入结果

1. 检查文件类型和可读性，识别伪装成 PDF 的 HTML、损坏文件及加密文件；设置解析时间、大小、页数上限。
2. 建立临时原始字节快照，流式计算 SHA-256；提取和上传使用同一快照，避免用户同时修改文件造成错配。
3. 从 PDF 元数据、XMP、首页及前几页正文、链接注释寻找 DOI、标题、作者和年份；优先文章标题区域及出版信息。参考文献列表中的 DOI 只能是低可信候选。
4. 有 DOI 候选则逐个精确查 Crossref，并与 PDF 的标题、作者和出版信息交叉核验。用户用 --doi 指定的值也要核验，不能绕过。
5. 没有可靠 DOI 时，用标题加作者、年份调用 query.bibliographic，取有限候选，例如前 5 条，再精确获取候选 DOI 元数据。[官方查询参数](https://github.com/CrossRef/rest-api-doc)。
6. 选定 DOI 后获取 CSL-JSON，检查 DOI 一致性、结构和准入字段；同时保存 Crossref 原始响应、请求时间、来源 URL 和匹配依据。
7. 产生 accepted、needs_review、rejected 或 retryable_error。只有 accepted 才允许进入归档上传。

无可用文本的扫描 PDF 进入 needs_review；首版允许用户补 DOI 和核对，OCR 作为可选扩展。纯文本提取不能等同于 OCR。[pypdf 文本提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。

建议首版期刊论文的必需字段：DOI、title、type、author、可用的出版年份；其他类型以后设独立规则。页码、卷期、期刊名缺失先记录质量提示，避免误拒在线优先发表文章。记录多个日期来源，不强制所有年份相同。

自动接受须同时满足：

- Crossref 中存在有效 DOI，原始响应与 CSL-JSON 的 DOI 一致，必需字段通过验证；
- 有足够的 PDF 证据支持该文献身份；
- 提取 DOI 与候选元数据没有实质矛盾；
- 标题回查时，标题高度一致、作者或年份有支持，且没有难以区分的竞争候选。

标题模糊度分数只参与筛选，Crossref 搜索 score 不是正确概率。标题阈值和候选差距应在真实样本标注后校准，不宣称某个阈值天然达到 99% 准确率。短标题、标题缺损、多个 DOI、勘误、合集、明显作者冲突一律复核。复核界面展示提取证据和候选，记录用户选择与理由；人工确认不能跳过 Crossref 存在性或必要字段检查。

Crossref 的 404 与超时、限流要区分：后两者可重试，不能误判为文献不合格。非 Crossref 注册的 DOI 在严格模式下标记 unsupported_registry；以后可增加显式 DataCite 模式，不静默切换来源。[DOI 内容协商覆盖的注册机构](https://www.crossref.org/documentation/retrieve-metadata/content-negotiation/)。

## 4. 元数据的权威与历史

Crossref 数据作为默认书目信息依据，但“注册记录存在”不意味着它一定属于眼前 PDF。匹配证据负责确认对应关系。

保存四组信息：

- extraction：从 PDF 获取的原始候选、页码、位置类别及片段；只存必要短片段。
- crossref_raw：完整原始响应、抓取时间、来源地址。
- csl：Crossref 转换得到的 CSL-JSON 和本工具 schema 验证结果。
- decision：接受方式、候选比较、规则版本、人工处理理由。

后续 metadata refresh 写入新记录版本，不覆盖历史，不改变已有文件哈希。用户笔记、标签与 provider 数据分开保存；不把本地修改伪装为 Crossref 原值。用于引文导出的 CSL id 可映射为稳定 work_id，但需保留 provider 原始快照。

## 5. 云端布局与可恢复索引

建议由 CLI 创建专用普通 Drive 文件夹，并记录所有文件夹 ID：

```text
PaperVault/
  vault.json
  objects/
    <SHA-256前2位>/
      <完整 SHA-256>.pdf
      <完整 SHA-256>.csl.json
  records/
    <doi_key前2位>/
      <work_id>.<commit_id>.json
  snapshots/                 # 可选的元数据加速快照
```

路径用于人工查看；程序操作使用 fileId。Drive 同一个文件夹可存在同名文件，所以不能按文件名“唯一查找”，也不能依靠改名实现事务。[Drive File 资源](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)。

vault.json 包含 schema_version、vault_id、objects/records 文件夹 ID 和指定 writer_id。面向约 50 万篇规模，objects 按 SHA-256 前两位、records 按规范化 DOI 的 SHA-256 key 前两位分为最多 256 个子目录，避免把全部对象堆进单一 Drive 文件夹。日常精确操作依赖本地索引、Drive fileId 与 appProperties，不依赖全库目录扫描。

records 中每个 JSON 是不可变的完整记录版本，包含：schema_version、vault_id、work_id、canonical_doi、commit_id、parent_commit_id、writer_id、CSL、Crossref 原始元数据、匹配结果、全部文件关联及 primary 选择。文件关联含 SHA-256、Drive fileId、字节数、媒体类型、版本角色、校验方式和时间。原始文件名可保留，完整本地目录只存本地。

每个受管 PDF 同目录保存一个 `<SHA-256>.csl.json` 旁车文件，内容是可独立使用的标准 CSL-JSON，并使用 DOI 作为稳定 `id`。旁车用于人工读取和与 Zotero/WOSAIDE 等工具互操作；`records` 中的不可变记录仍是权威档案。记录通过 `csl_file_id` 关联旁车；元数据刷新先写新旁车和新记录，确认提交后才把旧旁车移入 Drive 回收站，避免覆盖造成历史状态不一致。

每次提交写新 JSON，旧记录保留。恢复时验证 schema、库归属、父链和对象引用；用唯一有效的链头生成当前记录，不能只按 modifiedTime 或文件名选最新。有分叉或重复 DOI 的记录进入冲突状态，不允许静默丢弃任何一方。

Drive appProperties 只用于加速定位：

```json
{
  "vault_id": "<UUID>",
  "kind": "record",
  "doi_key": "<SHA-256 of normalized DOI>",
  "work_id": "<UUID>",
  "commit_id": "<UUID>"
}
```

PDF 的属性存 kind=blob、vault_id、sha256、schema；CSL 旁车使用 kind=csl、vault_id、sha256、schema。DOI 放在记录 JSON 和 CSL 内容内，doi_key 是 DOI 字符串的哈希，不是文件哈希；查出候选后仍核对完整 DOI。

不把完整 CSL 塞进 appProperties：单属性 key 与 value 合计上限是 124 UTF-8 字节，而且私有属性有应用访问边界。JSON 才是可迁移的记录主体。[属性限制](https://developers.google.com/workspace/drive/api/guides/properties)。

按 DOI 回退云端查询的表达式示意：

```text
'<records_folder_id>' in parents
and trashed = false
and appProperties has { key='vault_id' and value='<vault_id>' }
and appProperties has { key='doi_key' and value='<doi_key>' }
```

处理全部分页、转义查询值，取回匹配版本并验证。搜索延迟或属性缺失时可列举 records 文件夹并读取 JSON；没有权限时列举也无法绕过。[Drive 属性搜索](https://developers.google.com/workspace/drive/api/guides/search-files)。

本地 SQLite 是可重建的查询投影及任务日志，不作为唯一的文献档案。建议表：works、blobs、work_files、record_commits、import_jobs、review_items、cache_entries。对 canonical_doi 和 sha256 设相应唯一约束。

不让多个客户端直接同步和写同一个 SQLite 文件。首版用本地锁防止本机重复写，并限制指定 writer_id；它不是跨机器分布式锁。同一写入配置被复制或用户绕过限制仍可能分叉，恢复和提交检查必须检测冲突。以后若需要真正并发写入，再引入协调服务或完整合并协议。

换电脑：用同一 Google 账号与原 OAuth 应用重新授权，选择 vault，下载记录，重建 SQLite；无需下载所有 PDF。若更换 OAuth 项目，drive.file 权限和私有属性可能不可直接沿用，需要重新授予文件访问或显式迁移，不能承诺任意新客户端都能自动恢复。

## 6. 上传、幂等与提交顺序

状态流：

```text
discovered → extracted → matched → accepted
  → uploading → blob_verified → record_committed → archived

matched → needs_review / rejected
任意网络步骤 → retryable_error → 从最近持久化步骤恢复
archived → downloaded_verified → local_released（仅显式清理）
```

1. accepted 后先持久化任务 UUID、源快照 SHA-256、拟用对象 ID、记录提交 ID。
2. 检查已有已核验对象；命中也要确认可访问、未进回收站且内容一致。
3. 新对象用 files.generateIds 获取 Drive ID，保存后启动 resumable upload，媒体类型 application/pdf，不转换成 Google Docs。
4. 网络超时先检查已知 fileId 和上传会话状态，再恢复上传；复用预生成 ID。收到 409 时检查既有文件，不把冲突直接当作成功。Drive 官方支持用预生成 ID 避免不确定重试制造重复文件。[上传和预生成 ID](https://developers.google.com/workspace/drive/api/guides/manage-uploads)。
5. 上传后获取 size、sha256Checksum 等字段并比对。sha256Checksum 如果不可用，则下载原始内容流式计算 SHA-256，不能以缺字段作为通过。自己写入 appProperties 的 sha256 不是服务器独立校验。[校验和字段](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)。
6. 创建不可变记录 JSON，读取确认内容和关联正确。只有对象验证、记录提交和读回全部完成，才在本地事务中标记 archived。
7. 若 PDF 上传成功而记录失败，只留下可重用的孤立对象，原件不清理；若记录成功但本地数据库提交失败，则从云端记录恢复成功结果。

Drive 上多个文件的写入不按数据库事务设计。依靠明确的发布顺序、可重试 ID、任务日志和不可变记录实现应用层恢复。中断上传会话失效时重启；进程重启后先核对远端，再决策。

配置有限重试次数，使用指数退避与随机抖动，尊重 Retry-After 等服务器提示。默认低并发、元数据缓存、避免重复 Crossref 请求；失败不会触发无限循环或自动删文件。

## 7. 下载、阅读与清理

DOI 查询优先本地索引；未命中或记录过期时查询云端。标题查询优先搜索本库标题、作者和 DOI，必要时显式请求 Crossref。Crossref 找到的文章不一定已归档；分别显示“有元数据”和“有可下载 PDF”。

下载：选定文献和文件版本 → fileId → files.get?alt=media → 临时 .part 文件 → SHA-256 校验 → 原子重命名为最终文件。支持断点恢复时必须验证已下载前缀和目标对象未变更。[Drive 下载原始文件](https://developers.google.com/workspace/drive/api/guides/manage-downloads)。

open 命令下载到受管缓存后调用系统阅读器。缓存可配置上限，例如 2 GB；这是产品默认值而非容量承诺。清理只针对本工具管理的、未被用户修改的副本，正在使用或哈希发生变化的文件不得自动淘汰。被修改的批注版提示重新归档。缓存只能做到尽量回收，不能承诺阅读器或操作系统完全不产生临时文件。

对导入原件的清理使用独立 release-local 命令，默认输出 dry-run 清单；执行时必须满足：

- 该条目已 archived，云端记录可读取，对象可下载且没有冲突；
- 实际下载核验过 SHA-256（流式计算后丢弃验证字节也可，无须再长期存一份）；
- 清理前确认原路径仍是同一文件且 SHA-256 未改变；符号链接、身份或内容变化直接跳过；
- 逐文件明确记录结果，只移动到系统回收站，首版不永久删除。

移入回收站保留恢复余地，但要清空回收站才真正释放相应磁盘空间。用户明确指定的下载输出目录不是自动缓存；清理不能擅自删除其中文件。

不长期存本机 PDF 可以实现，但只存一份 Drive 文件仍是单副本。若要求容灾，可另选独立云端或离线备份；同一 Drive 里复制一份不解决账号失效或整库误删问题。首版不承诺零丢失。

## 8. CLI 接口草案

以下保留最初的接口草案；当前已实现命令请查看 `pv --help` 和 README.md：

```bash
pv auth login --client ./client_secret.json
pv init --name PaperVault
pv doctor

pv import ~/Papers --recursive --dry-run
pv import ~/Papers --recursive
pv import ./article.pdf --doi '10.xxxx/example'
pv review list
pv review resolve <job-id> --doi '10.xxxx/example' --reason '已核对首页标题及作者'
pv jobs retry --failed

pv search '标题关键词'
pv search '标题关键词' --remote
pv show '10.xxxx/example'
pv files '10.xxxx/example'
pv get '10.xxxx/example' --output ~/Downloads
pv get '10.xxxx/example' --sha256 '<hash>' --output ~/Downloads
pv open '10.xxxx/example'
pv export --format csl-json --output ./library.json

pv verify --all
pv rebuild --from-drive
pv metadata refresh '10.xxxx/example'
pv release-local --batch <batch-id> --dry-run
pv release-local --batch <batch-id> --apply
pv cache prune
```

import 默认保留原件，批量任务不弹出阻塞式逐篇问答，疑难条目进入复核队列。dry-run 可进行只读网络查询和本地诊断，但不上传、不清理原件。

为脚本提供 --json 和稳定退出码：0=全部成功；2=部分待复核或不合格；3=可重试网络故障；4=配置或授权问题；5=完整性或记录冲突。stdout 仅输出结果，诊断和进度走 stderr。批次总结列出接受、重复、待复核、拒绝、失败数量和可重试任务 ID。

## 9. Google API 配置

1. 在 Google Cloud 建项目并启用 Google Drive API。
2. 配置 Google Auth Platform / OAuth consent，声明应用信息和使用账号；测试时加入自己的账号。
3. 创建 Desktop app 类型 OAuth client，下载客户端配置 JSON。
4. CLI 使用浏览器登录、随机回环端口接收授权码，使用 state 和 PKCE，申请离线访问以取得刷新令牌；不采用已停用的复制粘贴授权码流程。[桌面 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)。
5. 首版申请 https://www.googleapis.com/auth/drive.file，由应用自己创建文献库。该权限适合管理应用创建或获准访问的文件，并不等于给某个既有文件夹 ID 就能访问其中全部历史文件。[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)。
6. OAuth 刷新令牌存系统凭据库；配置只存引用。令牌、上传会话 URL 不进 Git、不写普通日志。退出登录删除本地令牌；撤销云端授权是独立动作。
7. 外部应用保持 Testing 且申请 Drive scope 时，刷新令牌通常 7 天过期；长期使用应按控制台要求完成适合的发布配置，Production 也不保证令牌永不失效。[令牌过期规则](https://developers.google.com/identity/protocols/oauth2#expiration)。

普通个人盘首版使用用户 OAuth，不需要为此搭建后端或采用 service account。API key 不能代替私有 Drive 文件的用户授权。Crossref 公共查询不需密钥，配置联系邮箱和明确 User-Agent。[Crossref 公共访问](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)。

doctor 检查：配置、账号授权、到 Crossref/Google 的连通性、目标库归属和写入权限、可用容量、schema 兼容性；不输出敏感令牌。支持显式代理配置，遇到网络不可达输出可诊断错误。

Google Drive API 与 Google Cloud Storage 是不同服务。本设计使用用户的 Drive 容量；开启 API 不会提供无限存储。API 标准使用当前无额外费用，但官方文档已说明后续超额计费安排，容量和配额以实际账号及项目控制台为准，不写死“永久免费”。[Google Drive API 配额和费用](https://developers.google.com/workspace/drive/api/guides/limits)。

## 10. 实现结构与交付顺序

建议 Python 首版：标准库 argparse、sqlite3、hashlib、json 管理命令、索引与哈希；pypdf 做基础抽取；使用 Google 官方 Python 客户端与 OAuth 库对接 Drive。依赖锁定，OCR 作为额外安装项，不设常驻服务器。[Google Python 示例](https://developers.google.com/workspace/drive/api/quickstart/python)。

```text
src/papervault/
  cli.py
  config.py
  auth.py
  extract.py
  doi.py
  crossref.py
  matching.py
  models.py
  index.py
  drive_store.py
  import_pipeline.py
  records.py
  download.py
  cleanup.py
  recovery.py
```

存储层只暴露对象上传、读取、列举和记录提交等接口，为以后换存储服务保留边界；不为首版搭建通用插件框架。

交付顺序：

1. 用带人工真值的小样本检验提取与 Crossref 匹配，完成 dry-run 和复核结果，记录自动接受率、误接受数与覆盖率。
2. 实现显式 DOI 导入、云端对象与记录保存、重复导入、DOI 下载核验，跑通闭环。
3. 接入自动 DOI/标题识别、批量队列、失败恢复、元数据导出与云端重建。
4. 通过故障和恢复验收后才交付 release-local；其后再扩展 OCR、其他注册机构与多端写入。

验收必须覆盖：

- 本文 DOI 与参考文献 DOI 同时出现、短标题重名、首页标题断行、Crossref 元数据缺失、扫描及损坏 PDF；疑难样本不应自动放行。
- 重复导入相同字节在 PDF 解析和 Crossref 前命中哈希索引，不重复创建对象；同 DOI 的不同字节默认跳过，只有显式 `--keep-version` 才完整保留为另一版本。
- 网络在上传中断、上传成功响应丢失、记录提交前后、本地提交前后中断，各步骤均可恢复且不会清理原件。
- 服务端校验和缺失、下载内容损坏、文件被修改，均不得标记清理就绪。
- 删除测试环境本地索引后，只靠账号授权和 Drive 记录重建并下载得到原 SHA-256。
- 云端文件被移入回收站或失去权限、授权过期、配额错误、多记录分叉，给出明确状态且不假报成功。
- 原件在导入后变化、缓存被批注、目标文件已存在时，不覆盖或删除用户修改。

首版完成的判断：可以安全归档、可靠找回，并在本地状态全部丢失后恢复文献库；只有上传成功还不足以完成这个产品。
