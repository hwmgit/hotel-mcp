# Hotel MCP 项目规则

业务边界：本项目拥有酒店工单、身份权限、状态机、SQLite 和 MCP 工具/事件。不要引入固件、设备管理、模型配置或硬件平台源码依赖。

先读 README.md 和 docs/current-state.md。保留既有改动。data/、.env、数据库、运行日志不入库，不记录 token。测试使用临时数据库与回环随机端口，不修改运行数据。变更后运行 .venv/Scripts/python.exe -m pytest -q；接口必须保持角色隔离、事务与 request_key 幂等。新事件遵循 business://events 资源契约。提交前核对 git diff，更新当前状态。
