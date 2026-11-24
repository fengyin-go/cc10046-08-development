# 网络数据采集与RPA自动化平台

面向网页数据采集、自动化流程编排、解析规则管理、代理池调度与采集结果校验的数据采集工程平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
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

健康检查：`curl http://127.0.0.1:8010/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5174/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8010`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 爬虫配置 | `crawler_config` | 爬虫配置 | 配置标识、起始地址、页面编码 |
| 解析规则 | `parser_rule` | 解析规则 | 规则标识、目标页面、字段选择器 |
| 代理池 | `proxy_pool` | 代理节点 | 节点标识、代理地址、代理协议 |
| Cookie管理 | `cookie_mgr` | Cookie会话 | 会话标识、来源站点、Cookie内容 |
| 限速控制 | `rate_limiter` | 限速策略 | 策略标识、目标站点、请求频率 |
| 抽取模板 | `extraction_template` | 抽取模板 | 模板标识、适用站点、列表选择器 |
| 数据校验器 | `data_validator` | 校验规则 | 规则标识、校验字段、校验类型 |
| 采集调度 | `crawl_schedule` | 调度任务 | 任务标识、关联配置、调度周期 |
| 结果存储 | `result_store` | 存储批次 | 批次标识、来源配置、采集时间 |
| 去重引擎 | `dedup_engine` | 去重策略 | 策略标识、业务键列、排序字段 |
| 字段映射 | `field_mapper` | 映射方案 | 方案标识、来源字段、目标字段 |
| 异常重试 | `error_retry` | 重试策略 | 策略标识、错误类型、重试次数 |
| 批次追踪 | `batch_tracker` | 采集批次 | 批次标识、调度任务、开始时间 |
| 导出通道 | `export_channel` | 导出任务 | 任务标识、来源批次、导出格式 |
| 采集审计 | `crawl_audit` | 审计记录 | 记录标识、操作类型、操作对象 |
| 来源认证 | `source_auth` | 认证凭证 | 凭证标识、来源站点、认证方式 |
| HTML清洗 | `html_cleaner` | 清洗模板 | 模板标识、适用站点、标签过滤 |
| 增量同步 | `incremental_sync` | 同步水位 | 水位标识、来源配置、增量字段 |

## 跨批去重与有来源交付

去重引擎原来只对单次输入做无状态去重，看不出同一商品的两个报价分别出自哪次采集。
现在增加了一条跨批链路（`backend/app/services/dedup_registry.py` +
`backend/app/routers/dedup.py`），由它持久保存有来源的去重数据集和每次交付的原始行：

- **来源登记**：`POST /api/dedup/batches` 把原始行按批次登记，每行带
  `provenance_id`、`batch_id`、`row_key`；同一批次重发按内容幂等，不会多记一份来源。
- **跨批去重数据集**：`GET /api/dedup/dataset` 按业务键选一条最早入库且来源仍有效的报价，
  其它有效报价作为备选来源保留，两条来源都能回查。
- **领取页**：`GET /api/dedup/board` 给出每个商品是可领取、已交付、核对中还是无可领取；
  同一商品同时只允许一条有效交付（`POST /api/dedup/claim`、`POST /api/dedup/deliver`）。
- **撤回来源**：`POST /api/dedup/withdrawals` 可分批推进，`POST /api/dedup/withdrawals/{id}/resume`
  续跑；撤回未完成时该商品显示核对中，既不会继续交付旧报价，也不会让下一轮提前发出新报价。
  重放批次（`POST /api/dedup/batches/{id}/replay`）不会恢复已撤回的来源。
- **历史回执**：每次交付生成冻结快照（`GET /api/dedup/receipts`），保存当时发出的行和来源，
  后续撤回、重放都不改动它。
- **数据处理流水线**：`POST /api/chains/data-process` 带 `batch_id` + `rows` 时走来源登记；
  `POST /api/chains/delivery` 带 `rows` 或 `use_dedup_dataset` 时按去重数据集交付并返回回执。
- **持久化**：状态默认落盘到 `backend/.data/dedup_state.json`（可用 `DEDUP_STATE_PATH` 覆盖，
  `APP_ENV=test` 时不落盘），重启后回执、来源和撤回进度都还在。

去重引擎页面（`/dedup_engine`）下方新增「跨批去重与来源核对」区块，展示领取页状态、
进行中的撤回任务和历史回执。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
