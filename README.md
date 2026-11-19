# 铁路票务日结发布台（Flask + SQLite + 本地文件）

每天把**完整**铁路票务流水 CSV（≤ 1 MB）发布给两个下游系统：按各自契约
（必需列的顺序 + integer/text 类型）预览转换结果，确认后把**原件 + 结算 CSV +
客流 CSV** 以同一日修订号一次发布。三个下载始终来自同一个不可变修订。

CSV 有两个入口，产物与一致性保证完全相同：

* **交件收件（共享目录）**：车站程序写入 `<交件号>.csv` + `<交件号>.ready`，
  操作员在「交件收件」页扫描、预览、确认发布；正式发布后平台在同一目录放出
  `<交件号>.ack.json` 回执，车站核对后清理源文件。
* **手动上传**：在「上传与预览」页直接上传某日的完整 CSV。

## 快速开始

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data INBOX_DIR=./inbox PORT=8000 python3 run.py
# 打开 http://127.0.0.1:8000 （「交件收件」页在 /inbox）
```

### Docker Compose

```bash
docker compose up --build
# 打开 http://127.0.0.1:8000
# 数据在 rail-data 卷；共享交件目录默认绑定到 ./inbox（车站程序往这里写文件）
```

生产部署把 `./inbox:/inbox` 改成车站可写的挂载点（例如
`/srv/rail-inbox:/inbox`）即可，容器内路径由 `INBOX_DIR` 指定。

## 交件协议（共享目录）

车站程序与平台只通过目录里的文件交互：

| 文件 | 写入方 | 含义 |
| --- | --- | --- |
| `<交件号>.csv` | 车站 | 某批次的完整日结流水（≤ 1 MB，UTF-8） |
| `<交件号>.ready` | 车站 | 写完 CSV 后才放置的标记，内容是批次日期 `YYYY-MM-DD` |
| `<交件号>.ack.json` | 平台 | **正式发布后**才放出的回执，车站核对后清理源文件 |

规则：

1. **扫描跳过无 `.ready` 的 CSV**：车站还在写，平台不看不收。
2. **收件即绑定**：交件号首次收件时绑定批次日期与原件内容（原件同时存入
   平台不可变存储）。重扫沿用原交件；目录里的日期或内容有变就在页面和扫描
   结果里**报冲突**，收件原件不变、可照常发布。
3. **预览失效可重建**：预览 2 小时过期；过期后用收件时的原件重新预览，
   即使车站已经改写或删除了源文件。
4. **同一交件最多发布一次**：确认在数据库写事务内把交件状态从 `received`
   翻走，并发扫描/确认、重复预览都不会重复发布。
5. **发布失败不出回执**：确认失败（不兼容、契约或日修订已变等）事务回滚，
   交件保持 `received`，共享目录不会出现 `.ack.json`。
6. **回执待补写**：发布已提交但回执写盘失败时，交件状态为 `ack_pending`
   （页面显示「待补写」）。下次扫描（或页面「补写回执」按钮）按**发布时的
   原修订号**补写——即使中间重启过、同日又发布了更新的修订。车站清理掉
   全部源文件后，扫描不会再重建回执。

回执内容（`<交件号>.ack.json`，原子写入）：

```json
{
  "day": "2026-11-19",
  "intake_id": "ST01-20261119-01",
  "original_sha256": "a6da…",
  "original_size": 198,
  "published_at": "2026-11-19T08:00:00Z",
  "revision": 1,
  "row_count": 2
}
```

车站核对 `revision` 与 `original_sha256` 后即可删除 `.csv` / `.ready` /
`.ack.json`。

## 功能与规则

| 需求 | 实现 |
| --- | --- |
| 两个下游各配列序与类型 | `/contracts` 页面或 `PUT /api/contracts/<name>`，按行序即下游列序，类型只允许 `integer`/`text` |
| 交件扫描、收件绑定、冲突上报 | `POST /api/inbox/scan`；无 `.ready` 跳过，日期/内容变化报冲突 |
| 交件预览与确认发布 | 交件页「预览」→ `POST /api/intakes/<id>/previews` → `POST /api/previews/<id>/confirm` |
| 上传某日完整 CSV，≤ 1 MB | `POST /api/previews`，超过 1 MB 返回 413，空文件整批拒绝 |
| 预览各自转换结果和不兼容行列 | 返回两个下游的转换预览（前 50 行）、缺列、逐行逐列的整数错误 |
| 缺列/类型错误/重复表头整批拒绝 | 缺列与类型错误使该批 `convertible=false`，确认时 422；重复表头、字段数不一致、非 UTF-8、空行直接 422 整批拒绝 |
| 原版继续可取 | 拒绝只发生在预览阶段，不写发布记录；旧修订文件不变 |
| 确认后发布原件及两份下游文件 | `POST /api/previews/<id>/confirm`，同日修订号 +1 |
| 三者按同一日修订号下载 | `/download/<day>/<rev>/{original,settlement,ridership}`，同一 `rev` 的三个 blob 由一条发布记录一起提交 |
| 预览后契约或该日版本变化拒绝旧确认 | 确认时在写锁内校验契约版本+指纹与该日 `current_rev`，任一变化返回 409 |
| 修改契约不改已发布文件 | 发布行冻结列契约与内容寻址文件；编辑契约只影响后续预览 |
| 同日并发确认只成功一份 | `BEGIN IMMEDIATE` 串行化确认；先到者发布 r(N+1)，后到者因基线版本不符得 409 |
| 同一交件并发确认/扫描只发布一次 | 交件状态在同一写事务内翻转，已发布交件再次确认得 409 `intake_already_published` |
| 写盘/登记失败/重启不混用新旧版 | blob 先落盘并 fsync，再单事务插入发布行+更新日指针；下载只按发布行解析，损坏/缺失则 503，绝不回退 |
| 已开始的下载必须完整结束 | 下载先解析出版本并打开不可变 blob 再开始流式响应，`Content-Length` 与 `ETag` 固定为该 blob 哈希 |
| 回执写失败可补写 | 回执在发布事务提交后原子写入；失败则交件置 `ack_pending`，扫描时按原修订补写 |

### 整批拒绝与不可发布

* **整批拒绝（HTTP 422 `batch_rejected`）**：空文件、超限、非 UTF-8、空行、
  重复表头、空列名、字段数与表头不一致、CSV 语法错误。不会产生预览。
* **不兼容（预览 201，`convertible=false`；确认 422 `incompatible`）**：
  源 CSV 缺少某下游必需列，或整数列出现非整数。预览中会具体列出缺失列、
  出问题的数据行/文件行号/列名/原值；整批不可发布，修正后重新上传。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/contracts` | 两个下游的契约、版本、指纹 |
| PUT | `/api/contracts/<settlement\|ridership>` | body `{"columns":[{"name":"x","type":"integer"}]}` |
| POST | `/api/previews` | multipart：`day=YYYY-MM-DD`、`file=@x.csv` |
| GET | `/api/previews/<id>` | 重新获取预览快照 |
| DELETE | `/api/previews/<id>` | 放弃预览 |
| POST | `/api/previews/<id>/confirm` | 确认发布；201 返回发布版本（交件预览含回执结果） |
| GET | `/api/intakes` | 交件列表（状态、绑定、冲突、回执、目录文件在否） |
| POST | `/api/inbox/scan` | 扫描共享目录：收件、冲突检测、回执补写 |
| POST | `/api/intakes/<交件号>/previews` | 用收件原件创建预览（失效预览的重建入口） |
| POST | `/api/intakes/<交件号>/ack` | 手动补写回执（扫描也会自动补写） |
| GET | `/api/days`、`/api/days/<day>` | 发布记录（含每修订的列契约与哈希） |
| GET | `/download/<day>/<rev\|latest>/<kind>` | `kind` 为 `original`/`settlement`/`ridership` |
| GET | `/api/health` | 健康检查 |

下载响应头：`ETag` 与 `X-Content-SHA256` 为该文件 SHA-256，`X-Revision`
形如 `2026-11-01-r3`。

## 一致性设计

```
data/
  app.db              SQLite（WAL，synchronous=FULL）
  blobs/objects/<aa>/<sha256>   只写一次的不可变文件
  blobs/tmp/                    写入后 fsync 再原子 rename
inbox/                共享交件目录（INBOX_DIR）
  <交件号>.csv / .ready         车站写入
  <交件号>.ack.json             平台正式发布后原子放出
```

1. **收件**：扫描只在 `.ready` 存在时读取 CSV；原件字节按 SHA-256 落入
   blob 存储，交件号、批次日期、原件哈希一次绑定（`INSERT OR IGNORE`，
   并发扫描只有一个赢家）。
2. **预览**：解析与转换全部在内存；输出按 SHA-256 落到 blob 存储；预览行
   保存契约版本/指纹、基线修订号与结果哈希，2 小时过期。交件预览记录
   `intake_id`，原件永远取收件时的 blob。
3. **确认**（单个 `BEGIN IMMEDIATE` 事务）：
   * 重新校验未过期、未使用、基线 `current_rev` 仍相同、两个契约版本与
     指纹未变；交件预览还要确认交件仍是 `received` 且绑定与预览一致；
   * 用已存储的原件按**冻结的契约快照**重新转换，SHA-256 必须与预览一致；
   * 需要时补写 blob（写盘失败 → 503，事务回滚，旧修订不变）；
   * 一条 `publications` 行同时写入三个 blob 哈希 + 冻结契约，
     `days.current_rev` 同事务前移；交件同事务置 `ack_pending` 并记录
     正式修订号。`COMMIT` 后新修订才对下载可见。
4. **回执**：提交后把 `<交件号>.ack.json` 原子写入共享目录（临时文件 +
   rename + fsync），成功才把交件置 `published`。写失败保持
   `ack_pending`：页面显示「待补写」，之后每次扫描按发布时记录的
   原修订号补写，重启不丢。已发布的交件若回执丢失/损坏而源文件仍在，
   扫描也会重写；源文件已清理则不再触碰目录。
5. **下载**：只按 `(day, rev)` 解析发布行，先确认三个 blob 都在（否则 503，
   不静默回退到其它修订），再打开目标 blob 并以固定 `Content-Length`
   流式返回。已经在传输的下载持有已解析的不可变文件描述符，新发布不会
   影响其完整性。
6. **重启**：启动时清理 `tmp/`、自检所有发布记录的 blob 是否存在并在日志
   和首页报告，不删除、不改写任何已发布修订；待补写的交件状态存在
   数据库里，重启后第一次扫描即补写回执。

## 测试

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. python3 -m pytest tests -q
```

测试覆盖：列增删/调序、缺列、类型错误、重复表头、字段数不一致、编码错误、
1 MB 上限、三文件同修订下载、契约变更使旧预览失效且不改历史文件、同日并发
确认只成功一份（线程竞争）、预览重复确认、写盘失败/事务中断后旧修订仍完整、
缺失 blob 时 503 不回退、下载字节与 `Content-Length`/`ETag` 一致；
交件侧：无 `.ready` 跳过、收件绑定、日期/内容冲突、同一交件并发扫描+确认
只发布一次、发布失败不出回执、回执写失败后跨重启按原修订补写、车站清理后
不重建回执、预览过期后用收件原件重新预览。

## 说明与边界

* 单进程 gunicorn（1 worker、8 线程）+ SQLite 是默认部署；如需多副本，
  SQLite 数据库、blob 目录与共享交件目录必须位于同一共享卷，且只能有一个
  写者。
* 内容寻址会在不同修订间复用相同哈希的 blob（例如未变化的原件），这是有意
  设计：文件不可变，复用不会导致混用。
* 交件号取自文件名（去掉 `.csv`），允许字母数字与 `._-` 等安全字符，最长
  128；不以点开头、不含路径分隔符。无效名字在扫描结果里单列，不会收件。
* 目前不提供已发布版本的删除或改签；如需清理历史 blob，应离线按
  `publications` 表中的哈希做引用计数。
* 表头按 UTF-8（容忍 BOM）解析，列名区分大小写、保留内部空格但拒绝首尾空白。
