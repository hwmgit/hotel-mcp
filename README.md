# Hotel MCP

独立的酒店工单业务服务。工单、客房/前台权限、状态机、幂等和事件归本项目；AI 硬件平台仅通过 MCP 调用工具和订阅业务事件。本项目不导入硬件平台代码，不需要相邻仓库，使用 Python 3.12。

## 启动与测试

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[test]"
.venv/Scripts/python.exe -m hotel_mcp --init
.venv/Scripts/python.exe -m hotel_mcp
.venv/Scripts/python.exe -m pytest -q
```

默认 `127.0.0.1:8120`，健康检查 `/health`。`--init` 创建 `data/config.json` 的随机独立凭据，不覆盖已有配置。业务数据库 `data/hotel.sqlite3`。已迁入旧数据时跳过 `--init`。配置、数据库、日志均不提交。

容器独立构建：`docker compose up -d --build`。首次须先用上述命令生成配置。Compose 只暴露本机 8120；跨机器使用 TLS 代理，按部署拓扑配置网络，不依赖硬件平台的 Docker 网络。

## MCP 接口与业务边界

| 凭据 | 地址 | 授权工具 |
| --- | --- | --- |
| room-101 | `/room-101/mcp` | `hotel_submit_request`、`hotel_list_requests` |
| frontdesk | `/frontdesk/mcp` | `hotel_list_requests`、`hotel_accept_request`、`hotel_complete_request` |

传输为 Streamable HTTP，认证 `Authorization: Bearer <该 principal 的 token>`。每个 principal 使用独立 ID、至少 32 字符的独立随机 token。酒店、角色、房间从服务端配置确定，模型不能传参越权；MCP session 也按凭据隔离。

状态路径：客房提交 → 前台受理 → 前台完成。提交必须提供稳定 `request_key`，重试复用原值；同键不同内容拒绝，同状态同备注重试不产生重复事件。请求和事件在同一 SQLite 事务提交。只有工作人员明确确认后才受理/完成，通知不能自动改变工单状态。

客房提示词建议：确认住客要求后提交请求；同次请求及重试复用 request_key，失败不声称前台已收到；可查询工单进度。
前台提示词建议：查询待办，确认具体工单和工作人员意见后受理或完成，不因收到通知自动操作。

## 事件契约

服务声明可订阅资源 `business://events`，更新发 `notifications/resources/updated`。客户端调用 `resources/read`，URI `business://events?after=<seq>`。返回 JSON `{ "events": [{"seq":1,"event_type":"...","speech_text":"..."}], "has_more":false, "next_cursor":1 }`。每页最多 50 条；seq 在数据库内递增，当前凭据只读授权范围事件，空洞正常。

平台事件订阅器持久化自己的游标，通知仅用于唤醒，断线后按游标补读。平台如何把文本通知到智能体/设备由平台实现；本服务不持有 manager 密钥、模型密钥、设备 MAC、MQTT 或硬件 SDK。业务凭据不同即可连接不同业务实例。

硬件平台接入时分别为客房和前台智能体配置上述服务地址及凭据，再由平台的通用事件订阅器绑定设备与智能体配置。地址由平台服务访问，不是由 ESP32 访问；容器访问宿主机需使用可达地址。

事件投递接受不等于设备已播放。业务状态以工单查询为准；本参考实现没有播放完成回执，不保证通知恰好播放一次。数据库重建并重置序号时，消费者必须重置对应游标。单进程部署；多副本需升级共享存储与消费者租约。

## 迁移与验证

2026-10-18 从 WhiteCat Interactive 的酒店服务拆出，来源提交 `6a2aafc74edae378f8e0c965f6fc33b0389b0451` 的 `services/hotel_mcp/{server,store,__main__}.py`，修改包路径、默认数据位置和独立构建入口。平台事件桥接器留在平台，不在本项目部署。Git 历史保留本仓库原始 Apache-2.0 LICENSE 提交。

测试通过真实 HTTP MCP 会话覆盖：请求—受理—完成、资源订阅、断线补读、幂等、跨房间/酒店/角色/会话拒绝。测试客户端只是协议验收夹具，不是运行依赖。没有使用真实设备播报作为验收证据。
