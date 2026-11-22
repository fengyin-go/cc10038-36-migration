# 多源日志采集与解析中心

管理多来源日志接入、字段解析、批量去重和水位推进的数据采集平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/DeliveryView.vue       版本化日志交付工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/delivery_router.py  版本化日志交付接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/delivery.py 版本化清单、迁移、切换、回滚与核实
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8013/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5193/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8013`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源登记 | `source_registry` | 数据源 | 来源标识、接入协议、连接凭证 |
| 采集批次 | `ingest_batch` | 采集批次 | 批次编号、所属数据源、采集时间窗口 |
| 字段解析规则 | `field_parser` | 解析规则 | 规则名称、适用数据源、输入格式 |
| 原始日志记录 | `raw_records` | 日志记录 | 批次编号、原始行号、采集时间戳 |
| 标准化明细 | `parsed_entity` | 标准化记录 | 来源批次、原始记录ID、解析规则版本 |
| 去重引擎 | `dedup_engine` | 去重策略 | 策略名称、去重键字段、去重窗口 |
| 水位管理器 | `watermark_mgr` | 采集水位 | 数据源、最后成功批次、当前水位值 |
| 重试队列 | `retry_queue` | 重试任务 | 失败记录ID、失败原因、重试次数 |
| 格式适配层 | `format_adapter` | 格式适配 | 输入格式名、适配器类型、目标格式 |
| 质量校验规则 | `quality_check` | 校验规则 | 规则名称、校验类型、阈值参数 |
| 归档策略 | `data_archive` | 归档策略 | 策略名称、归档周期、保留时长 |
| 限流控制器 | `rate_limiter` | 限流规则 | 数据源、每秒上限、突发并发 |
| 解析操作日志 | `parse_journal` | 解析日志 | 操作类型、批次编号、执行时间 |
| RPA采集流程 | `rpa_workflow` | 采集流程 | 流程名称、目标系统、触发方式 |
| Schema版本 | `schema_version` | Schema版本 | 版本号、适用数据源、字段定义 |
| 采集告警策略 | `alert_policy` | 告警策略 | 策略名称、监控指标、告警阈值 |
| 数据导出 | `data_export` | 导出任务 | 导出范围、目标格式、过滤条件 |
| 回溯重放引擎 | `replay_engine` | 重放任务 | 历史批次范围、新解析规则版本、重放状态 |
| 聚合查询视图 | `aggregation_view` | 聚合视图 | 视图名称、聚合维度、指标定义 |
| 采集血缘 | `data_lineage` | 血缘关系 | 来源批次、解析规则版本、标准化版本 |

## 版本化日志交付清单

历史数据导出只按「视图名称 + 导出编号」落记录，文件路径之外没有可交换的来源结构。
`backend/app/services/delivery.py` 把这些存量交付迁移为版本化日志交付清单：

- **双写**：`build_export` 一次导出同时登记旧结构交付（`legacy-v1`）和版本化清单
  （`versioned-manifest-v2`）。清单声明来源文件摘要、分组依据、逐条记录的每个日志
  字段，以及生成文件摘要，写回 `data_lineage`，用于从清单反查真实文件。
- **存量迁移**：`record_legacy` 登记旧交付，`migrate` 按交付编号逐份迁移；支持
  `fail_after` 模拟部署中断，再次调用同一迁移编号按游标续做。已封存清单不会被覆盖。
- **来源核实**：来源只能通过显式内容、已登记文件 ID 或旧入口保存的文件引用（内容可读）
  确认；仅凭文件名不会建立来源绑定，清单保持 `待补来源` 且不可读。
- **读取切换**：`switch_read_mode(view, 1|2)` 切换视图读取版本。部署期同名视图可以
  同时存在两份交付：旧客户端读取冻结的上一份旧交付，新入口读取最新封存清单。
- **快照下载**：`download` / `snapshot` 在同一把锁内解析「交付编号 → 完整内容」，
  一次下载只对应一份完整清单；旧交付不会用新生成文件或重新分组的结果冒充。
- **暂停 / 回滚**：`pause_migration` 暂停单份交付的迁移（旧结构继续可读）；
  `rollback` 切回旧读取结构但保留清单与文件归属，`verify` 可核实原始文件、
  旧交付文件、新生成文件各属于哪次交付，`restore_file` 按原编号还原文件。

接口前缀 `/api/delivery`，前端页面 `/delivery`（导航「版本化日志交付」）。
交付文件落在 `backend/var/deliveries/`（可用 `DELIVERY_ROOT` 覆盖，已在 `.gitignore`）。
测试见 `backend/tests/test_delivery.py`。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
