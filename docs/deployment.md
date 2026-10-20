# 酒店服务受控部署（MySQL 8）

目标域名：`mcp-hotel.yingzeai.com`。仓库：`https://gitlab.leyingiot.com/yingze/mcp-hotel.git`，分支：`release_20261020`。平台：`https://clify.yingzeai.com/controlled-deployment`，项目选择“ MCP项目 ”，环境 `production`。

## 平台操作

1. 确保目标分支包含本次完整代码，根目录有 `Dockerfile`、`docker-compose.coolify.yml`、`requirements.txt` 和 `hotel_mcp/web/`。
2. 在受控部署页填写仓库和分支，检查仓库；核对域名、应用 `hotel-mcp`、类型 `rds`、健康检查 `/health`、容器服务 `hotel:8120`、持久卷 `/data`。
3. 创建或复用目标应用。平台自动分配 RDS 库及账号，并注入 `DB_HOST / DB_PORT / DB_DATABASE / DB_USERNAME / DB_PASSWORD`；密码为敏感变量，不写进仓库或文档。库须为本服务独立的 MySQL 8.0.16+ 数据库，不接共享业务库。
4. 在应用中手动点击部署。容器启动执行 `python -m hotel_mcp.deployment serve`；平台迁移钩子执行 `python -m hotel_mcp.deployment prepare`。两者共用可重复准备流程，首次启动已准备好数据才提供健康检查，避免健康检查等待与迁移钩子互相阻塞。
5. 等待平台报告完成，检查 HTTPS `/health` 和 `/admin/`；用本应用私有配置中的前台、客房和销售管理身份验证页面和 MCP 权限。

未配置单独的前端/API域名：静态页面、业务 API 和 MCP 均为同源。Compose 只 expose 8120，公网 TLS/路由由平台代理负责，不绑定宿主机端口。单副本 1 CPU / 512M，前端制品声明为 `local-dist`、路径 `/hotel_mcp/web`（已随代码提供的 HTML/CSS/JS）；RDS 类型的预检不接受 `none`。无 Node 构建、宿主机目录或硬件平台源码挂载。现有 `compose.yaml` 保留本地 SQLite 用法。

## 数据与私有配置

首次初始化创建 schema v6、五家门店、最近30天600条营业订单（每店120条，覆盖住宿、餐饮、健身、会议），每店两间入住客房、四道菜、健身/会议资源和次日起三天固定时段。每店有报障、续房、点餐、健身、会议各一张待办，共25张。

页面采用正常业务名称。台账仍为模拟数据，内部 `data_source:demo / is_demo:true` 和初始化标记保留，便于识别来源；没有接真实收入源。续房人工核实、不接 PMS，点餐线下结算，预约在前台确认时占用资源。工单完成不自动生成销售收入。

`hotel-private` 命名卷保存 `/data/bootstrap/config.json`（随机访问凭据和入住绑定）及只用于初始化的 `hotel.sqlite3` 种子快照；运行业务全部读写 RDS，种子文件不是运行库。初始化不会读本机 `data/`，也不会复制8121实例的凭据、数据或硬件接入点。

重复部署或执行迁移钩子保留已有业务、别名、凭据和游标；不会重建测试单、更新日期或重置已经处理的工单。配置中的 token、入住绑定和 MCP 地址可以按维护流程调整，不会导致重灌数据。模拟台账使用稳定的 `demo-*` 内部编号，面向客户显示的是正常门店名称。

数据库已有初始化标记但私有卷丢失时，程序拒绝重新生成身份，须恢复该实例的卷备份。已存在冲突门店编号、种子快照不匹配或数据库不属于本服务时拒绝覆盖。备份须同时覆盖 RDS 和命名卷；不要仅重建卷或删除初始化标记来重置数据。

后台访问身份示例：

- 总部销售管理：`sales-manager`，有五家门店查询权。
- 中心店前台：`frontdesk-demo-central`，只能处理本店。
- 中心店客房：`room-demo-central-101` / `room-demo-central-102`，只访问绑定入住。
- 其他店内部编号为 `demo-riverside / demo-airport / demo-lake / demo-convention`，使用相同身份命名规则。

访问凭据仅从授权的私有卷管理渠道获取，不在部署日志、聊天、截图或公开接口中输出。页面刷新后需重登。云端 MCP 接入点仍按 README 的隐藏输入命令配置，对应文件为 `/data/bootstrap/config.json`；不得复制原运行实例的接入点造成双实例抢连。

## MySQL 维护与限制

使用原有主入口时，显式传 `--database mysql` 才使用 RDS 环境变量；省略仍为原 SQLite 默认。维护绑定入住也支持此选择。部署专用入口固定使用 MySQL，不接受隐式 SQLite 降级。

MySQL 使用 InnoDB、utf8mb4、外键、唯一约束和 CHECK。写操作先锁定 `hotel_write_lock` 单行，保持原 SQLite 写事务顺序，保护幂等、预约容量和事件序号提交顺序；失败时工单、历史、回执和事件一起回滚。当前仍按单副本部署，未承诺多副本主动连接器或跨进程订阅唤醒。

RDS 普通业务账号即可初始化，不需要 SUPER、不修改全局 binlog 配置。固定时段目录写入必须使用 `Services.insert_slot` 并持有写事务锁，以拒绝时间重叠；MySQL 无需高权限触发器。数据库约束额外拒绝同资源同起点；不支持直接 SQL 绕过目录校验。SQLite 保留原有时段触发器。

MySQL DDL 会隐式提交，不能承诺迁移整体事务回滚：本次新库初始化通过数据库归属标记、迁移锁、重复安全的建表和最后写版本记录支持中断后重试。不提供将现有 SQLite 运行库自动搬迁到 MySQL 的功能，也不改动当前运行数据。

平台默认五项 DB 变量即可连接。若实际 RDS 要求 CA 校验，适配层支持 `DB_SSL_CA` 文件路径及证书/主机名校验，需运维按平台允许的私有配置方式提供 CA，不能关闭证书校验绕过连接错误。

## 隔离验收

安装项目依赖和测试依赖，启动 Docker 后运行：

```powershell
docker build -t hotel-mcp:mysql-demo-20261020 .
$env:HOTEL_TEST_MYSQL = '1'
$env:HOTEL_TEST_IMAGE = 'hotel-mcp:mysql-demo-20261020'
.venv/Scripts/python.exe -m pytest -q
```

测试自动创建带随机密码的临时 MySQL 8 容器、仅限测试库的账号、回环随机端口以及临时应用容器和卷；结束清理自己创建的资源，不连接任何外部 RDS。未设置开关时跳过 MySQL 集成测试，仍运行 SQLite 回归与静态契约检查。

2026-10-20：156项通过（51.21秒），包含正式 Linux 镜像启动、网页资源、健康检查、迁移钩子重复执行、五店600单、五业务闭环、权限隔离、并发确认、幂等、故障回滚和中断迁移恢复。两项告警为现有 WebSocket 依赖弃用提示。平台线上验收结果另见 `current-state.md`。

## 本次上线结果

2026-10-20，应用 `zaqdbg5k9cw15js6de9szkc7` 部署 `b10e6i8fqm4ractarroumfbo` 已完成，运行代码 `71f07eb`。平台 healthy，公网 `/health`、管理页和脚本均200。RDS 核实五店600单、营业汇总188,100元、10个入住、25张工单、20道菜和90个时段。中心店101报障样例已完成前台受理/完成验收（version3），客房经公网 MCP 读到completed；未登录401。

本次构建仍使用默认 PyPI，服务器首次下载约357秒；未配置项目级阿里云 pip 镜像，也未核对服务器级 Docker mirror。更改镜像源应作为后续独立优化，不影响当前已上线实例。

## 页面接入配置热更新

更新到支持“AI 接入配置”的程序后，前台可直接在页面测试、保存、修改和停用本店身份的 MCP 接入点，不再通过部署应用配置连接。首次升级程序仍需一次部署；后续设置立即生效。新增私有文件 `/data/bootstrap/mcp-connections.json` 必须随原命名卷备份；不要删除卷来升级代码。此功能不修改数据库结构、销售台账或工单数据，部署准备仍是幂等的。

列表及测试结果不包含完整接入凭据。程序通过主配置路径定位持久化文件；自定义 Python 启动器须向 `create_app` 提供 `config_path`，否则保持原行为并隐藏可编辑入口。标准 CLI 和部署入口已提供该参数。单进程/单副本是当前运行约定。

2026-10-20 页面接入配置已发布：代码 `63595b3`，部署 `l101j6027sc8vbap0decw1r0`，容器 healthy。公网前台配置接口200，客房/销售403，未登录401，网页与健康检查200。未填写真实硬件接入点。
