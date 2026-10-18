# 当前状态

2026-10-18（Asia/Shanghai）。酒店工单已从硬件平台拆分为独立 Python MCP 服务，远端为 https://github.com/hwmgit/hotel-mcp.git。独立虚拟环境安装成功，4 项真实 HTTP 协议/业务测试通过；平台实际事件桥接器的跨项目 HTTP 联调另有 4 项通过，独立 Docker 镜像构建及 CLI 启动检查通过。构建、数据、凭据与平台分离，平台事件桥接只通过协议访问本服务。

迁移保留原仓库 LICENSE 历史。旧本机数据通过 SQLite backup API 制作一致性快照到 data/，原件保留；不是持续同步，切换后仅运行一个业务写入实例。未部署生产，未验收真实设备语音播报。服务运行方式及协议见 README.md。

功能提交：`05b9508`。推送尚未成功：本机 GitHub 凭据缺失，交互登录没有完成，非交互重试确认无法获取用户名。完成 Git 凭据管理器授权后，在本项目运行 `git push origin main`；不要把 Token 写进远端 URL 或文件。
