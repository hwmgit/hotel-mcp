# Hotel MCP

## AI 接入配置（保存即时生效）

前台登录 `/admin/` 后打开 **AI 接入配置**，选择本店前台或客房卡片，粘贴平台提供的完整 WSS MCP 接入点。可先点击“测试连通性”，再“保存并生效”；更换地址会关闭旧连接并启动新连接，“停用连接”立即断开。连接状态每5秒更新，输入内容不会被刷新覆盖。首次上线此功能需要更新程序一次，之后配置修改无需重启或重新部署。

测试仅验证 WebSocket 握手与心跳，不宣称设备、LLM或语音业务已通过；测试当前已保存连接复用现有连接心跳，不重复抢连。测试新地址为短连接，不保存配置或开放酒店业务工具。连接失败显示安全摘要，保存后网络暂不可用会自动重试。一个接入点只能绑定一个业务身份。

沿用角色权限：前台仅管理本店客房及前台身份，住客与销售 manager 不开放配置入口，也不能调用配置 API。页面与 API 不返回完整 URL，输入为密码字段；生产页面只接受可解析的公网 WSS 地址，禁止内网/回环目的地和重定向，保持 TLS 校验。旧私有配置的出站连接仍兼容。

新设置原子写入主配置旁的私有 `mcp-connections.json`；线上路径 `/data/bootstrap/mcp-connections.json`，随命名卷持久化，不需要数据库迁移。页面管理过的身份以此文件为准（含显式停用）；其余身份沿用主 `config.json` 的 `mcp_endpoint`。旧 CLI 修改主配置不能覆盖已由页面管理的身份，应继续在页面调整。备份时同时保留此文件和原私有配置，不提交或输出凭据。仅支持当前单进程/单副本运行约定。

页面接口均要求现有 Bearer 凭据，敏感配置不加入 MCP 工具：

- `GET /api/{principal_id}/connections`：本店身份、脱敏地址、配置版本及状态。
- `POST /api/{principal_id}/connections/{target_id}/test`：可传 `endpoint` 测试草稿，不传则测试保存值。
- `POST /api/{principal_id}/connections/{target_id}`：`endpoint`、`expected_version`、`request_key`；空地址为停用。版本和幂等检查防止重复切换及覆盖新配置。

166项测试通过（56.35秒），含真实 WebSocket 热连接/切换/停用、MCP业务调用、持久化恢复、权限/幂等/并发冲突、错误脱敏、内网及重定向拒绝、SQLite/MySQL和正式容器回归。浏览器隔离实测测试、保存即时连接及停用通过。


## 2026-10-20：受控部署 / MySQL 8

部署目标 `https://mcp-hotel.yingzeai.com/admin/`，GitLab `https://gitlab.leyingiot.com/yingze/mcp-hotel.git`，分支 `release_20261020`。使用根目录 `docker-compose.coolify.yml`，RDS 的五项 `DB_*` 变量由平台自动生成，无需提交连接密码。操作步骤、初始化规则、备份和验收见 [部署文档](docs/deployment.md)。已部署并通过公网 HTTPS 健康检查，当前运行代码提交 `63595b3`（含页面接入配置）。

本地默认 SQLite 保持兼容；部署入口显式选择 MySQL 8.0.16+，不读取或迁移本机运行数据。首次准备五家门店（中心、江畔、机场、湖畔、会展）、最近30天600条营业订单、10个入住和25张业务工单；重复部署不会重置处理结果或凭据。按客户最新要求，线上门店和页面采用正常业务名称，内部仍记录模拟来源；销售不与工单自动结算。早期章节的“演示”标签描述本地样例历史规则。

本轮验收：**156 passed，51.21秒**，包含受限数据库账号下的 MySQL 8 实测、五业务闭环、权限/并发/幂等/回滚、迁移中断恢复，以及正式 Docker 镜像连接临时 MySQL 的启动、网页资源、健康检查和重复迁移钩子。自动化测试使用临时数据库与容器；其后按用户确认创建独立线上 RDS 并验收五店600单、公网前台报障受理/完成和客房 MCP 读取。未触碰原8121实例或其硬件连接。


独立的酒店业务 MCP 服务。工单、身份权限、入住批次、状态机、SQLite、工具和业务事件归本项目。硬件平台只通过通用 MCP 调用、事件订阅和智能体通知协同；本项目不导入固件、设备管理、模型配置或平台源码。

## 当前实现

- P0：房间目录、入住开始/结束、客房入住绑定、分类工单、详情/筛选/分页、处理历史、写操作幂等、版本检查、事务事件、显式数据库迁移。
- 客房报障：分类、位置、紧急程度、关联本次入住的既有报障，提交、受理、进度记录、完成、拒绝、取消、无法解决结案。
- 续房：申请、前台人工核实报价、报价修订、住客确认、开始办理、成功/失败结果及退房联动，不连接 PMS。
- 多门店销售展示：授权门店列表/切换，按日期和业务分类查询演示销售额、订单数、门店对比和每日趋势。
- 门店实体解析：正式名称/别名匹配、已知语音误识别及近似名称候选、歧义澄清提示，确认后使用门店 ID 查询。
- 原一般请求保留：客房提交，前台受理和完成。
- 点餐：菜单、下单金额快照、前台确认、配送完成、拒绝/取消，线下结算。
- 健身房/会议室预约：固定时段、人数、前台确认时校验并占用资源，完成/取消释放占用。
- 可视化工作台：待办概览、五类业务处理、状态与处理历史、菜单与预约余量、多门店销售和名称澄清。

Python 3.12，单进程部署，支持 SQLite / MySQL 8。后台页面位于 `/admin/`，与 MCP 使用同一业务服务和权限；房间与凭据通过本地私有配置维护。

当前产品目标是向客户展示各业务核心功能。五类业务均已实现核心闭环，多门店管理覆盖门店目录和查询切换，暂不提供门店在线增删改。

## 一台客房硬件 + 前台网页演示

唯一硬件绑定 `room` 客房身份，负责住客申请、续房报价确认和结果通知。工作人员以 `frontdesk` 身份登录网页，完成受理、报价、安排服务、预约确认和结案，无需第二台前台硬件。销售跨店对比另用 `manager` 登录；前台只能处理及查询本店。

先创建**全新目录**的完整演示实例，再显式启动它（以下命令供演示准备，本轮未部署常驻服务）：

```powershell
.venv/Scripts/python.exe -m hotel_mcp --init-demo .codex-tmp/hotel-demo
.venv/Scripts/python.exe -m hotel_mcp --config .codex-tmp/hotel-demo/config.json --database .codex-tmp/hotel-demo/hotel.sqlite3 --port 8121
```

浏览器打开 `http://127.0.0.1:8121/admin/`。从该独立实例的私有 `config.json` 读取对应入口编号和随机凭据，在页面输入；不要把配置或凭据放入文档、URL、截图或版本库。前台入口例为 `frontdesk-demo-central`，客房入口例为 `room-demo-central-101`，三店销售管理入口为 `sales-manager`。硬件通过平台已有通用 MCP 配置连接同一实例的 `/<客房入口编号>/mcp` 并订阅 `business://events`；跨主机地址须对平台可达，不能将平台的 localhost 当作业务服务地址。运行实例和硬件连接切换需另行安排。

生成器包含三店、六个有效入住、每店四道菜、一个健身房和一个会议室、每店五张待处理工单，以及84条模拟销售订单和门店别名。预约时段为创建日期次日起三天的09:00、14:00、18:00，每段一小时；健身房4个名额，会议室单组最多12人。预约日期始终相对创建当天，`--demo-end-date` 仅控制销售周期。过期演示样例请另建新目录；命令拒绝覆盖已有目录，不修改已有实例。

| 演示业务 | 客房硬件 / 客房身份 | 前台页面操作与可见结果 |
| --- | --- | --- |
| 报障 | 提交空调故障 | 工单详情：受理 → 追加进展 → 修复完成；也可拒绝、取消或无法解决结案 |
| 续房 | 申请日期，收到报价后明确同意 | 核实并报价 → 待住客确认 → 开始办理 → 记录成功/失败；成功才延长入住 |
| 点餐 | 查询菜单，确认菜品数量并下单 | 确认/受理 → 待配送 → 完成；页面展示菜品金额和线下结算 |
| 健身预约 | 选择日期时段及人数 | 确认时扣减名额 → 完成/取消释放；余量不足显示错误，保持待确认 |
| 会议预约 | 选择日期时段及人数 | 确认时独占整间会议室 → 完成/取消释放；同场同时段不能确认两组 |

工作概览和工单列表每15秒自动刷新，填写表单、提交或打开详情时暂停；详情可手动刷新。列表按类型/状态筛选、每页50条；概览统计最近加载的50张，详情显示最近50条处理记录。所有动作由工作人员明确点击并填写说明，页面只展示当前角色可执行的操作，不因收到通知自动流转。另提供客房网页入口，方便没有硬件连接时测试申请和确认，但不扩大前台权限。

页面使用本项目打包的 HTML/CSS/JavaScript，无额外 Node 构建或第三方字体。`GET /api/{principal_id}/session` 返回当前角色和允许工具；`POST /api/{principal_id}/tools/{tool}` 使用与 MCP 相同的 Bearer 凭据、schema、分发器、事务、版本检查和幂等记录。网页操作提交后同样发布 MCP 资源更新，已连接客房订阅方可读到处理结果。凭据只保存在页面内存，刷新/退出需重新登录；没有新增账号权限体系。`/admin/config` 只公开统计配置白名单，不返回任何业务凭据。

Umami 接入默认关闭。仅私有配置 `analytics` 的 `enabled=true`、`environment=production`，并在 HTTPS 下精确匹配 `production_domain`、提供 `website_id` 和 HTTPS `script_url` 时才加载；回环、本地、test、preview、staging 域名禁用。此次未提供正式站点参数，也未进行生产统计联调。

## 点餐与预约核心规则

| MCP 工具 | 权限 | 参数 |
| --- | --- | --- |
| `hotel_list_menu` | 客房/前台 | 无，返回本店在售菜品、整数分价格、`currency=CNY / settlement=offline` |
| `hotel_submit_meal_order` | 客房 | `items:[{item_id,quantity}], request_key, note?` |
| `hotel_list_booking_slots` | 客房/前台 | `kind=gym/meeting, date=YYYY-MM-DD`，返回未来固定时段及 `remaining` |
| `hotel_submit_booking` | 客房 | `slot_id, participants, request_key, note?` |
| `hotel_process_request` | 前台 | 复用 `accept / complete / reject / add_note`，必填当前版本、幂等键、处理说明 |
| `hotel_cancel_request` | 客房/前台 | 复用取消接口；客房仅能取消 `pending`，已确认服务由前台取消 |

新类型为 `meal / gym_booking / meeting_booking`，均复用 `pending → accepted → completed`，待确认可拒绝，待确认或已确认可由前台取消。网页将点餐 `accepted` 展示为“待配送”、预约展示为“已确认”；终态不可继续修改。点餐一次1～10种不同菜品，每种1～20份，下单时保存菜名、单价、数量及总价快照，之后菜单改价不改旧单。完成表示配送服务完成，不代表收款，没有线上支付或收款凭据管理。

预约申请不占资源，也不承诺成功。前台确认事务中重新核验有效入住、场地启用状态、尚未开始、人数和剩余容量；健身按人数占用，会议室按一组占用整间。占用量由同一时段 `accepted` 工单累计，完成或取消即释放；已开始的时段不再允许新申请/确认。预约须早于预计退房日期，同一资源的固定时段禁止重叠，相邻时段允许。并发确认同一资源时通过 SQLite 写事务避免超卖，冲突返回 `RESOURCE_UNAVAILABLE`，不会改变工单或事件。

退房原子取消待确认点餐、待确认/已确认预约并释放资源，返回 `cancelled_service_count`；已受理点餐保留给前台核实处置。菜单、资源和时段目前由演示生成器/本地维护准备，没有在线增删改、餐饮库存、餐厅分工、预约计费、循环预约或真实销售对接。所有新业务完成均不自动写入独立的销售演示台账。

详情返回 `meal:{total_amount_minor,currency,settlement,items}` 或 `booking:{slot_id,participants,units,resource_name,starts_at,ends_at}`。新增错误包括 `ITEM_UNAVAILABLE / SLOT_EXPIRED / RESOURCE_UNAVAILABLE`；其他错误与 P0 保持一致。

## 多门店销售演示

销售数据使用独立的模拟已结算订单，所有结果都有 `is_demo:true / data_source:demo / display_notice`，智能体展示或播报时必须说明“演示数据”，不可描述为实际营业收入。一条模拟订单只属于一家店和一个业务分类；销售额为订单金额之和，订单数为订单条数，人民币整数分。当前没有真实销售导入、退款或净收入模型；续房报价、确认和办理结果均不会自动计入销售。

权限来自私有配置，工具参数不能扩大范围：

- `manager`：只提供门店列表、实体解析和销售查询，须配置非空 `allowed_hotels`（最多100个明确门店 ID），不能设置 `hotel / room / stay_id`，不开放住客工单或其事件。
- `frontdesk`：保留本酒店工单能力，同时只能查询本店销售。
- `room`：不开放销售工具，保持已有客房/入住隔离。

例如管理身份的业务字段为 `{"id":"sales-manager","role":"manager","allowed_hotels":["demo-central","demo-riverside","demo-airport"]}`；凭据仍使用每个入口独立的本地随机 token，不写入源码或文档。授权变更沿用私有配置和安排重启的方式。

| MCP 工具 | 参数 | 返回 |
| --- | --- | --- |
| `hotel_list_stores` | 无 | 授权门店 `hotel/name`、每店演示数据起止日期 |
| `hotel_resolve_entities` | 必填 `query`；可选 `entity_type=store` | 解析状态、候选门店、澄清问题、唯一确定时的 `resolved_id` |
| `hotel_query_sales` | 必填 `date_from/date_to`；可选 `hotel_ids/category` | `totals`、`by_store`、`by_category`、`daily_trend` |

`date_from/date_to` 为 `YYYY-MM-DD`，按上海时区的酒店营业日期统计，包含首尾，最长366天。`hotel_ids` 省略即查询全部授权且已配置门店，传一个 ID 即切换单店；指定列表必须非空、不重复且全部已授权，混入未授权门店整体拒绝。`category` 为 `accommodation`（住宿）、`dining`（餐饮）、`gym`（健身）、`meeting_room`（会议）。这些销售分类仅供演示，不表示对应工单业务已经开发完成。

各组指标为 `sales_amount_minor / order_count`；门店按销售额倒序排列，同额按 ID 排序；每日趋势按日期升序并补零。日期内没有数据时返回零值，门店、分类和日期维度仍保留；授权门店尚未配置时列表为空，显式指定该店则返回 `NOT_FOUND`。上述查询与解析工具均为只读，不需要 `request_key`，不生成通知事件或共享的“当前门店”状态。

### 生成隔离的演示实例

```powershell
.venv/Scripts/python.exe -m hotel_mcp --init-sales-demo .codex-tmp/sales-demo --demo-end-date 2026-10-18
```

目标目录必须全新；命令拒绝已有目录及同时传入 `--config/--database`。生成独立 `config.json`、`hotel.sqlite3`，包含三店、七天、84条模拟订单，以及一个三店管理入口、一个单店管理入口和各店前台入口。凭据随机生成到私有配置，不输出到终端。省略结束日期则使用北京时间当天。本命令不启动服务，也不对已有库补充样例。

需要单独演示时，使用新实例的明确路径和独立端口启动（本轮未启动常驻演示服务）：

```powershell
.venv/Scripts/python.exe -m hotel_mcp --config .codex-tmp/sales-demo/config.json --database .codex-tmp/sales-demo/hotel.sqlite3 --port 8121
```

在智能体中绑定管理入口后，可依次演示“有哪些门店”“查询10月12日至18日各店销售”“只看中心店”“比较各店餐饮销售”。对应查询示例：

```json
{"date_from":"2026-10-12","date_to":"2026-10-18"}
```

固定上述结束日期时，三店合计 **演示销售额24,528.00元、84单**；中心店为 **7,896.00元、28单**。前台入口只能查看所属店，单店管理入口不能查看其余两店。此实例只准备销售演示，不包含入住和客房凭据绑定。

### 名称解析与歧义澄清

语音平台只负责通用识别和文本传递；智能体从用户话语中提取门店名称片段，调用 `hotel_resolve_entities`。本期仅支持门店，不解析整句意图，也不纠正房号、日期、数量或金额。复用销售权限：管理身份按 `allowed_hotels`，前台仅本店，客房不可调用。先过滤权限再匹配，候选和澄清文案不会包含未授权门店。

| 返回状态 | 含义 | 智能体下一步 |
| --- | --- | --- |
| `resolved` | 唯一精确 ID、规范化正式名称或明确别名 | 使用 `resolved_id` 作为 `hotel_query_sales.hotel_ids` 中的门店 ID |
| `needs_clarification` | 多店匹配，或已知误识别/近似名称候选 | 展示/播报 `clarification_question`，等待用户确认或选择；不可默认选第一项 |
| `not_found` | 授权范围内无匹配 | 请求完整门店名称，不得省略 `hotel_ids` 改查全部门店 |

返回另含 `query / normalized_query / entity_type / requires_confirmation / candidates / has_more / next_step`。候选包含 `entity_id / name / match_type`，最多5条；截断时仍保持歧义，并提示补充名称。未确定时 `resolved_id=null`，即使只有一个近似候选也不自动选中。同名候选的澄清问题带序号和门店 ID；智能体应保留本次候选列表，将用户明确选择映射回 ID。后续销售接口仍重新校验该 ID 的权限。

名称规范化处理全半角、大小写、空格和标点；优先精确 ID，再比较正式名称与别名。精确匹配按门店去重，同店多个名称不会造成重复候选；不同店名称/别名冲突则要求选择。无精确匹配时使用文字相似度提供候选，已知误识别词不参与二次模糊扩展。`match_type` 为 `exact_id / exact_name / alias / asr / similar`，不是语音识别置信度。本版没有通用拼音/方言纠错、外部识别服务或自动学习别名。

本地私有配置可提供完整的 `store_aliases` 清单，例如：

```json
{"store_aliases":[
  {"hotel":"demo-central","alias":"中心门店","kind":"alias"},
  {"hotel":"demo-riverside","alias":"江盼店","kind":"asr"},
  {"hotel":"demo-central","alias":"市区店","kind":"alias"},
  {"hotel":"demo-riverside","alias":"市区店","kind":"alias"}
]}
```

`kind=alias`（默认）表示明确别名；`kind=asr` 表示已知识别误写，必须确认。同店规范化别名不能重复，跨店允许共享别名；门店须已存在。服务加载配置时，显式清单会在单一事务中替换全部别名，非法条目或写入失败则保留旧清单；`[]` 清空，省略字段保留数据库中的别名。配置变更须另行安排重启，不提供在线别名写入 MCP 工具。解析工具本身只读，不保存识别文本、不生成事件或幂等记录。

新生成的销售演示实例已包含上述场景及三店常用名称。可以这样展示：

1. “查中心门店的销售”：提取 `中心门店` → `resolved / demo-central` → 查询该店，固定演示周期返回7,896.00元、28单。
2. 识别文字为“江盼店”（已登记误写）或“江盘店”（近似名称）：返回“您指的是江畔店（演示）吗？” → 用户确认 → 使用 `demo-riverside` 查询，返回8,176.00元、28单。
3. “查市区店的销售”：三店管理身份收到中心店、江畔店两个候选 → 用户选择 → 仅查询所选店。
4. “查火星度假村”：返回 `not_found`，请用户补充名称，不执行全店查询。

澄清交互由智能体执行，业务接口提供结构化候选和提示，不会自行播放、自动调用销售工具，也不能证明人类已经口头确认。自动化验收使用模拟用户确认后的 MCP 调用序列，未进行真实 ASR/LLM/设备语音联调。

## 安装、启动与验证

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[test]"
.venv/Scripts/python.exe -m pytest -q
```

默认服务地址 `127.0.0.1:8120`，健康检查 `/health`；配置默认 `data/config.json`，数据库默认 `data/hotel.sqlite3`。配置、数据库和日志均不提交、不记录 token。

首次创建独立实例时：

```powershell
.venv/Scripts/python.exe -m hotel_mcp --init
.venv/Scripts/python.exe -m hotel_mcp
```

`--init` 仅生成私有配置和随机独立凭据，不覆盖已有文件，不自动创建入住。新数据库首次启动初始化 schema v6；旧数据库（包括 P0 v2、续房 v3、销售 v4 和实体解析 v5）必须显式迁移，不能直接套用上述首次启动流程。

新配置包含 `rooms` 列表，例如 `{"hotel":"demo-hotel","room":"101","enabled":true}`，及 `principals` 列表。房间条目按酒店/房号更新；停用须显式设置 `enabled:false`，从配置中移除条目不等于停用数据库中的房间。

容器独立构建入口仍为 `docker compose up -d --build`。首次须生成配置并完成必要迁移。Compose 只暴露本机 8120，跨机器使用 TLS 代理；不依赖硬件平台 Docker 网络。此次 P0 验收未重新构建镜像或部署运行实例。

## 入住与客房凭据生命周期

1. 配置本酒店房间目录及前台凭据，启动服务。
2. 工作人员确认入住后，前台调用 `hotel_start_stay`，获得 `stay.id`。同一酒店、房间最多一个有效入住。
3. 由本地管理员将客房配置绑定该入住并轮换 token；命令只修改明确指定的私有配置，不输出 token：

```powershell
.venv/Scripts/python.exe -m hotel_mcp --config .codex-tmp/demo/config.json --database .codex-tmp/demo/hotel.sqlite3 --bind-stay <stay-id> --principal-id room-101
```

以上路径为独立实例示例，数据库和配置必须已存在。绑定命令不迁移数据库、不启动或停止服务；配置变更需安排服务重启生效，并通过平台既有配置机制更新连接凭据。

4. 客房 principal 在服务端固定绑定 `hotel / room / stay_id`，不能从工具参数切换身份。未绑定、入住已结束、房间停用时返回 `STAY_INACTIVE`。
5. 工作人员确认退房后，调用 `hotel_end_stay`。旧客房会话立即失去业务读写和资源订阅资格，新会话也不能绕过；未完一般工单、报障和已受理点餐保留，未办理续房、待确认点餐及未履行预约自动取消。若续房正在办理，拒绝退房并要求先核实结果。返回 `open_request_count / cancelled_extension_count / cancelled_service_count`。
6. 下一批住客新建入住并绑定新凭据。前台可以继续处理旧入住工单，但后续通知不会投递给新住客。

预计退房日为 `YYYY-MM-DD`，作为酒店当地日历日期记录；它不是自动退房时间，不自动撤销入住。系统生成的时间戳统一为 UTC，格式为带 `Z` 的 ISO 时间。

本地管理员必须使用绑定命令轮换凭据，不要手动复用旧 token 为新入住授权。服务端配置属于信任边界；本期没有在线凭据热更新或凭据管理 MCP 工具。共享前台凭据只能审计到 principal，不能证明具体工作人员身份。

## 客房报障规则

故障分类 `category`：`air_conditioning`（空调）、`plumbing_electrical`（水电）、`network`（网络）、`door_lock`（门锁）、`facilities`（设施）、`other`（其他）。

`description` 为 1～300 字，`location` 可选且最多 80 字，`urgency` 为 `normal`（默认）或 `urgent`。紧急程度记录住客声明，不自动派单或自动改变处理状态。`related_request_id` 可选，只能关联本次入住有权访问的报障工单；新建关联工单不重新打开旧工单。

| 原状态 | 操作 | 权限 | 新状态 |
| --- | --- | --- | --- |
| 无 | 提交 | 客房 | `pending` |
| `pending` | `accept`，填写处理安排 | 前台 | `accepted` |
| `pending` | `reject`，填写原因 | 前台 | `rejected` |
| `pending` | 取消，填写原因 | 客房/前台 | `cancelled` |
| `accepted` | `add_note`，追加处理记录 | 前台 | `accepted` |
| `accepted` | `complete`，填写修复结果 | 前台 | `completed` |
| `accepted` | `close_unresolved`，填写原因和后续建议 | 前台 | `closed_unresolved` |
| `accepted` | 确认停止处理并取消 | 前台 | `cancelled` |

`completed` 表示工作人员确认故障解决；无法解决不得冒用完成状态。终态不可再变更，客房不能直接取消已受理工单。新接口处理说明/取消原因为 1～500 字（首尾空白去除），所有处理说明对该入住客房可见，不应填写内部敏感信息。

本轮由前台协调维修，不提供工程人员账号或自动派单。服务验证权限、状态及说明必填；是否确实获得人工确认、是否已修复依赖工作人员和智能体流程，不以一个模型传入的姓名或布尔值作为证明。

## 续房业务规则

`request_type=stay_extension`，仅支持当前入住在同一房间续住。房态、报价和实际手续由前台人工核实；系统不连接 PMS、不自动预留房间、不处理支付/退款，也不支持换房或提前退房。

住客提交目标退房日，必须晚于当前预计退房日，晚数由服务端计算。同一入住最多一张 `pending / quoted / confirmed / processing` 续房工单，结案后可以再次申请。申请日期不可在报价中偷偷修改；需要改日期时先按规则取消并重新申请。

| 原状态 | 操作 | 新状态 |
| --- | --- | --- |
| 无 | 客房申请 | `pending` |
| `pending` | 前台人工核实后报价 | `quoted` |
| `quoted / confirmed` | 前台修订报价，填写变更原因 | `quoted`，清空当前确认 |
| `quoted` | 客房明确同意当前有效报价 | `confirmed` |
| `confirmed` | 前台再次核实并开始办理 | `processing` |
| `processing` | 前台记录实际办理成功 | `completed`，原子更新入住退房日期和版本 |
| `processing` | 前台记录实际办理失败 | `failed`，入住不变 |
| `pending / quoted` | 前台拒绝 | `rejected` |
| `pending / quoted` | 客房或前台取消 | `cancelled` |
| `confirmed` | 前台确认尚未办理并取消 | `cancelled` |

`processing` 不允许改价、直接取消或退房；结果不确定时通过 `hotel_process_request(action=add_note)` 追加说明，保持办理中，不能猜测成功/失败。终态不能重开。

报价规则：

- 金额为人民币整数分，`currency` 固定 `CNY`，`total_amount_minor` 范围 0～9007199254740991；不接受浮点数、负数或布尔值。
- `pricing_note` 为 1～500 字，说明费用和服务条件；零元报价应写明免费原因，由工作人员负责内容真实性。
- 有效期默认 30 分钟，可设置 1～1440 分钟，由服务端生成 UTC 到期时间。到期时刻及之后不得确认。
- 每次报价都有独立 `quote_id` 和递增修订号；修订不覆盖旧报价。住客确认须指向当前报价，改价后必须重新确认，旧确认仍保留在处理历史。
- 过期状态在查询/确认时实时判断；工单仍为 `quoted`，可重新报价或取消，不生成自动到期通知。
- 住客在有效期内确认的报价不会自动失效；办理前仍需前台核实房态，系统检查入住日期/版本快照。
- 住客确认仅表示同意办理；只有 `hotel_finish_stay_extension(outcome=success)` 才延长现有入住。`stay_id`、客房凭据不变。成功表示前台确认已办理，不表示系统核验了 PMS 或收款。

人工手续与 SQLite 无法组成同一事务。前台实际办理后记录接口失败，应复用原 `request_key` 重试记录，不能重复执行线下手续。发生 `STAY_CHANGED` 时先核实入住信息，不自动覆盖。新写接口幂等重放返回原结果快照，最新状态仍应查询详情。

退房时，在同一事务中取消未办理的续房并记录原因，通知前台；不会把旧入住通知发给新住客。办理中返回 `STAY_HAS_PROCESSING_EXTENSION`。普通工单和报障继续保留。

### 续房 MCP 工具

所有操作都遵守现有酒店/房间/入住范围；报价和办理仅前台可用，申请和确认仅客房可用。以下除查询外均需 `request_key`，除首次提交外均需当前工单 `expected_version`。

| 工具 | 权限 | 业务参数（通用写参数见上） |
| --- | --- | --- |
| `hotel_submit_stay_extension` | 客房 | `target_checkout_date, note?` |
| `hotel_quote_stay_extension` | 前台 | `request_id, total_amount_minor, pricing_note, valid_for_minutes?, change_reason?` |
| `hotel_confirm_stay_extension` | 客房 | `request_id, quote_id` |
| `hotel_begin_stay_extension` | 前台 | `request_id, quote_id, note` |
| `hotel_finish_stay_extension` | 前台 | `request_id, quote_id, outcome, note, result_reference?`；`outcome=success/failed` |
| `hotel_reject_stay_extension` | 前台 | `request_id, reason` |
| `hotel_list_stay_extension_quotes` | 客房/前台 | `request_id, cursor?, limit?`，报价修订号倒序分页 |

申请 `note` 最多 300 字；办理说明/拒绝原因 1～500 字，人工凭证编号 `result_reference` 最多 160 字。复用 `hotel_get_request / hotel_query_requests / hotel_list_request_actions / hotel_cancel_request`。通用 `hotel_process_request` 对续房只允许追加说明，不能绕过报价、确认和办理；旧受理/完成接口仍只支持一般工单。

返回保持 `{"request":{...},"replayed":false}`，办理结果另返回 `stay`。续房详情放在 `request.extension` 中，包含原/目标退房日期、申请说明、当前/已确认报价 ID、确认/办理时间和结果。`current_quote` 包含日期快照、晚数、总价、费用说明、有效期和修订号；`quote_status` 为 `none / valid / expired / confirmed / inactive`，`available_actions` 按 room/frontdesk 提供当前动作提示，写入时仍重新校验。历史报价的前台 principal ID 不向客房返回。

例如前台报价：

```json
{"request_id":"<续房工单编号>","total_amount_minor":76000,"pricing_note":"两晚合计760元，含早餐，由前台办理","expected_version":1,"request_key":"quote-turn-001"}
```

住客智能体说明日期、晚数、总价和费用条件并取得明确同意后，提交 `request_id / quote_id / expected_version / request_key` 确认，不能用通知接收或播放成功代替住客确认。

新增错误码：`ACTIVE_EXTENSION_EXISTS / QUOTE_EXPIRED / QUOTE_SUPERSEDED / STAY_CHANGED / STAY_HAS_PROCESSING_EXTENSION`，继续使用既有 JSON 错误结构。入住结束、越权、版本冲突和幂等冲突仍使用 P0 错误码。

## MCP 工具

传输仍为 Streamable HTTP，入口 `/<principal-id>/mcp`，认证为 `Authorization: Bearer <该 principal 的 token>`。endpoint ID 与至少 32 字符随机 token 必须独立；MCP session 按凭据隔离。身份范围来自服务端配置。

下表 `?` 表示可选参数；所有新写操作的重试必须复用原 `request_key` 和原参数。

| 工具 | 权限 | 参数 |
| --- | --- | --- |
| `hotel_get_current_stay` | 客房 | 无 |
| `hotel_start_stay` | 前台 | `room, expected_checkout_date, request_key` |
| `hotel_end_stay` | 前台 | `stay_id, expected_version, reason, request_key` |
| `hotel_submit_repair` | 客房 | `request_key, category, description, location?, urgency?, related_request_id?` |
| `hotel_get_request` | 客房/前台 | `request_id` |
| `hotel_query_requests` | 客房/前台 | `request_type?, statuses?, created_from?, created_to?, cursor?, limit?`；前台可额外筛选 `room?, stay_id?` |
| `hotel_list_request_actions` | 客房/前台 | `request_id, cursor?, limit?` |
| `hotel_process_request` | 前台 | `request_id, action, note, expected_version, request_key` |
| `hotel_cancel_request` | 客房/前台 | `request_id, reason, expected_version, request_key` |

`hotel_process_request.action` 为 `accept / add_note / complete / reject / close_unresolved`。`general / repair` 沿用原规则，`stay_extension` 仅允许 `add_note`，点餐和两类预约允许除 `close_unresolved` 以外的操作。新增类型必须显式扩展 schema 和业务规则，不能自行传入未实现类型。

提交示例：

```json
{"request_key":"repair-turn-001","category":"air_conditioning","description":"空调开启后不制冷","location":"卧室"}
```

受理示例（工单号取提交返回值）：

```json
{"request_id":"<工单编号>","action":"accept","note":"已安排维修人员，十五分钟内联系客房","expected_version":1,"request_key":"accept-turn-001"}
```

- 提交/处理/取消返回 `{"request":{...},"replayed":false}`；入住写接口返回 `{"stay":{...},"replayed":false}`，退房另含 `open_request_count`。
- 工单详情包含原有字段以及 `stay_id, request_type, version`，报障另含 `repair:{category,location,urgency,related_request_id}`。
- 处理历史返回 `actions, next_cursor`，每条包含动作、前后状态、说明、时间、版本和操作者角色；客房不获取前台 principal ID。v3 增加 `data` 结构化动作信息，续房确认/办理会记录具体报价，旧记录为 `{}`。
- 版本从 1 开始，每次有效修改加 1。旧版本返回 `VERSION_CONFLICT`；重新查询后由工作人员判断，不自动覆盖。
- 新写接口幂等重放返回首次成功的结果快照和 `replayed:true`，最新业务状态以详情查询为准。住客提交键在同一入住范围唯一；操作结果还按 principal 和入住范围隔离，下一批住客不会重放旧结果。
- 查询默认 20 条、最多 50 条，按内部创建序号倒序；历史也倒序。游标绑定身份、查询种类、筛选条件并签名，跨重启有效。分页排除首次查询后新增记录；状态筛选反映读取时状态，不提供跨页数据库快照。
- 时间筛选为包含边界的带时区 ISO 时间，服务端转为 UTC；`statuses` 为非空状态数组。

业务工具失败返回 `isError:true`，文本内容为 JSON，例如：

```json
{"code":"VERSION_CONFLICT","message":"记录已被其他操作更新，请重新查询","retryable":false}
```

错误码包含 `VALIDATION_ERROR / FORBIDDEN / STAY_INACTIVE / NOT_FOUND / INVALID_TRANSITION / VERSION_CONFLICT / IDEMPOTENCY_CONFLICT / TEMPORARY_UNAVAILABLE`。资源或工具目录的访问失败通过 MCP 协议错误返回。越权工单号按 `NOT_FOUND` 处理，错误/缺失 Bearer 凭据返回 HTTP 401。

### 旧接口兼容

- `hotel_submit_request(request_key,description)` 继续提交 `general` 工单；返回旧字段，重复提交返回当前工单状态。
- `hotel_list_requests(after?)` 保留旧返回形状及按工单 ID 分页的语义。
- `hotel_accept_request(request_id,note?)`、`hotel_complete_request(request_id,note?)` 仅处理 `general`，保留同状态同备注幂等；备注上限 160 字。
- 报障使用新处理接口，续房使用专属流程，不能通过旧工具绕过版本、幂等键和业务条件。
- 所有客房接口统一增加入住隔离。缺少入住绑定的旧配置不再具有房间全部历史的读取权限，这是一项有意的访问规则收紧。

## 数据与迁移

schema v3 在 P0 的 `rooms / stays / requests / repair_details / request_actions / operation_receipts / events / schema_migrations / service_metadata` 上新增 `stay_extension_details / stay_extension_quotes`，并扩展工单类型/状态约束及操作历史数据。报价只追加，不提供修改/删除接口。数据库对同入住未结案续房增加唯一约束，当前/已确认报价通过复合外键限定属于同一工单。`service_metadata` 保存分页游标签名密钥，不存业务 token。

schema v4 增加 `stores(hotel,name)` 和 `demo_sales_orders(id,hotel,business_date,category,amount_minor,currency)` 及查询索引；订单通过外键归属门店。迁移不生成演示门店/订单，不自动把旧工单或续房费用转换成销售。真实数据接入仍需后续单独实现。

schema v5 增加 `store_aliases(hotel,alias,normalized_alias,kind)`，按门店和规范化别名唯一，通过外键绑定 `stores`。升级只增加空表，不推断或自动登记别名；新演示目录才随生成器写入演示别名。v4原门店、订单和工单数据保持不变。

当前 schema v6 扩展 `requests` 的业务类型及状态约束，并增加六张表：`menu_items` 菜品目录、`meal_orders` 点餐金额/结算信息、`meal_order_items` 菜品快照、`booking_resources` 场地容量、`booking_slots` 固定时段、`booking_details` 预约人数/占用单位与场地时间快照。详情表通过外键绑定工单，时段通过酒店/资源复合外键隔离，并使用触发器拒绝同资源时段重叠。迁移不灌入菜品、场地、时段或演示单，不更改已有工单内容。

请求、状态变化、操作记录、幂等结果和业务事件在同一 SQLite 事务中提交，开启外键校验。写事务使用 `BEGIN IMMEDIATE`，先查重放再检查版本；失败整体回滚。处理历史没有修改/删除 MCP 接口。

旧库不会在服务启动时自动升级。先对目标库做 SQLite 一致性备份，并停止该库的业务写入，再执行明确指定路径的迁移。例如，仅对隔离副本：

```powershell
.venv/Scripts/python.exe -m hotel_mcp --database .codex-tmp/migration-copy/hotel.sqlite3 --migrate
```

迁移维护命令要求显式 `--database` 且文件已存在，不会隐式使用默认运行库。

- 保留旧工单编号、内容、状态、备注与时间含义，统一时间戳格式。
- 保留事件内容、已有 seq 和自增高水位，不重置消费者游标。
- 旧工单归为 `general`、`stay_id=NULL`，仅本酒店前台可见；旧客房事件不绑定给新入住。
- 不推断旧历史的住客归属，不伪造旧操作历史。本版本不提供历史工单自动认领工具。
- 迁移与版本记录原子提交，重复执行安全；失败保持原结构和数据。
- 空库初始化到 v6；v2/v3/v4/v5 显式升级到 v6；旧无版本库在同一事务内按 v2→v3→v4→v5→v6 路径升级。v3→v4→v5 仅加表；v6 为扩展约束重建工单父表并恢复原索引，保留子表外键、工单/事件自增高水位、幂等记录和原游标签名密钥，旧分页游标继续有效。提交前检查外键一致性，失败回滚。
- 新版产生写入后，不能直接恢复旧备份，否则会丢失新业务；实际运行库升级需单独安排维护和回退方案。

## 事件契约与硬件平台边界

继续声明可订阅资源 `business://events`，更新发送 `notifications/resources/updated`。客户端读取 `business://events?after=<seq>`，返回：

```json
{"events":[{"seq":1,"event_id":"hotel-1","event_type":"hotel.repair.submitted","speech_text":"客房有新报障"}],"has_more":false,"next_cursor":1}
```

每页最多 50 条，seq 在数据库内递增；按酒店、角色、房间和客房入住范围过滤，序号空洞正常。新事件另含 `schema_version:2, request_type, request_id, request_version, status, display_text`。

报障事件为 `hotel.repair.submitted / accepted / updated / completed / rejected / cancelled / closed_unresolved`。一般工单保留 `hotel.request.<status>`。

点餐和预约事件前缀分别为 `hotel.meal / hotel.gym_booking / hotel.meeting_booking`，阶段为 `submitted / accepted / updated / completed / rejected / cancelled`。继续使用事件 `schema_version:2`、稳定工单 ID/版本/类型/状态和 `speech_text / display_text`，无需平台识别特定业务状态机。前台网页与 MCP 操作生成相同事件。

续房事件为 `hotel.stay_extension.submitted / quoted / confirmed / processing / completed / failed / rejected / cancelled / updated`。保持事件 `schema_version:2`，按需增加 `quote_id, quote_revision, target_checkout_date, total_amount_minor, currency`；数据库版本和事件版本互不等同。客房申请/确认通知前台，报价/办理结果通知对应有效入住。事件通知不会自动执行确认、办理或改变状态。

客房提交/取消通知前台，前台处理通知该工单对应的有效入住客房；没有相关新事件的 endpoint 不发送资源更新，结束入住时清除旧客房订阅。退房导致续房、点餐或预约取消时生成面向前台的取消事件；不会投递给新住客。

平台持久化自己的消费游标，通知仅用于唤醒，断线后按游标补读；服务通知失败不回滚已提交业务。事件接收不代表设备已播放，播放成功也不代表业务完成。本服务不保证通知恰好播放一次，不提供播放完成回执。

本服务不持有硬件平台 manager 密钥、模型密钥、设备 MAC、MQTT 或硬件 SDK。酒店销售的 `manager` 角色仅为本项目的业务查询身份，与硬件平台管理身份无关。地址由平台服务访问，不是 ESP32 直接访问；容器访问宿主机需使用可达地址。未来 PMS、餐饮或支付适配仍归酒店业务侧。

## 验收证据与限制

当前全量测试 **139项通过（28.50秒）**。本轮新增21项覆盖菜品快照、点餐闭环、预约占用与释放、并发确认冲突、跨店/角色隔离、退房联动、幂等和事务回滚、v5→v6迁移数据保留/失败回滚、完整演示目录拒绝覆盖，以及真实 HTTP MCP 客房申请 → HTTP 前台页面接口确认/完成 → 客房订阅通知。浏览器实际操作已验证前台点餐确认/完成和处理记录、续房报价、健身预约确认后名额4→2。此前118项 P0/报障/续房/销售/门店解析回归全部保留。

页面通过隔离临时演示服务检查，另跑通报障受理/完成、会议预约确认、续房住客确认/前台办理成功，以及多店看板和歧义选择后的单店销售查询。Python 包配置包含静态资源，JavaScript 语法检查通过，浏览器无控制台错误；临时预览已退出并清理数据库。本轮未进行真实硬件 ASR/LLM/TTS 联调、生产迁移、生产统计或容器重建；本次测试不证明客房设备实际播放完成。完整演示实例准备及页面操作见上文。

P0/报障/续房验收全部使用 pytest 临时数据库和回环随机端口，包括真实 HTTP MCP 会话、订阅/重连、状态出口、入住隔离、幂等、报价修订及过期边界、办理成功才延长入住、失败保持入住、退房联动、事务故障注入、基础并发冲突、v2/v3 迁移回滚和本地绑定命令。不访问现有 data/ 数据、不启动/停止现有服务。

当前仍为单进程；未提供多副本消费者租约、部门账号体系或真实工作人员身份认证。没有使用本次真实设备播放或生产迁移作为验收证据。

历史来源：2026-10-18 从 WhiteCat Interactive 的 `6a2aafc74edae378f8e0c965f6fc33b0389b0451` 提交拆出 `services/hotel_mcp/{server,store,__main__}.py`。平台事件桥接器留在平台，本项目保留原仓库 Apache-2.0 LICENSE 历史。此前的设备联调记录见 `docs/current-state.md`，不代表本次新增功能已部署。

## 配置智控台 MCP 接入点（主动 WebSocket）

HTTP MCP 继续可用。酒店服务也可主动连接云端，无需开放本机8121端口。在智控台分别复制客房、前台智能体的 MCP 接入点，在酒店项目运行：

```powershell
.venv/Scripts/python.exe -m hotel_mcp --config <实例配置文件> --principal-id <客房或前台入口编号> --set-mcp-endpoint
```

命令隐藏输入完整 `wss://…/mcp/?token=…` 地址，原子保存到该身份的 `mcp_endpoint` 字段；留空停用，重启该实例生效。地址含凭据，不放命令行、截图、日志、Git或聊天。生产只接受WSS，本机测试可用回环WS。重复地址不能绑定多个身份。HTTP入口、业务权限与数据库均保持兼容。

配置也可直接在本机私有 config.json 的对应 principals 项增加 `mcp_endpoint`；不要把客房和前台连到同一个智能体。当前演示101客房不能默认为真实801房；房号以酒店身份配置为准。启动命令仍为 `--config … --database … --port …`，一次启动同时运行HTTP服务和主动连接器。

`/health` 的 `mcp_connections` 仅返回身份编号及 connecting/connected/retrying/stopped，不返回地址、token；connected表示传输连接建立，业务可用性须通过工具测试确认。断线指数退避重连（最多30秒加抖动），不重放未确认写操作；调用方用原 request_key 恢复。前台网页与MCP使用同一事件源。云端须部署支持资源通知的MCP接入点，并为目标设备启用通用订阅器，才能主动播报。
