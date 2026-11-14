# 三系统重构说明（已完成分割）

目标：把原有一体化工程重构为三个可独立启动、独立开发、独立演进的系统。

## 当前重构结果

已建立三套系统级包（`systems/*`）：

1. `systems/roll`
2. `systems/youpin`
3. `systems/compare`

并将三个启动入口改为系统包路径：

- `python main_roll.py` -> `systems.roll.app:app`（默认端口 `8101`）
- `python main_youpin.py` -> `systems.youpin.app:app`（默认端口 `8102`）
- `python main_compare.py` -> `systems.compare.app:app`（默认端口来自 `APP_PORT`）

同时每个系统都提供了独立启动模块：

- `python -m systems.roll.main`
- `python -m systems.youpin.main`
- `python -m systems.compare.main`

根目录 `main_roll.py / main_youpin.py / main_compare.py` 现为薄包装，仅调用各系统 `run()`。

每个系统也已补充本地工程说明与依赖清单：

- `systems/roll/README.md` + `systems/roll/requirements.txt`
- `systems/youpin/README.md` + `systems/youpin/requirements.txt`
- `systems/compare/README.md` + `systems/compare/requirements.txt`

其中：

- `main_compare.py` 启动时会设置 `COMPARE_READ_ONLY=1`。
- `systems/compare/app.py` 已是独立实现（仅依赖 `shared` 读库与查询能力，不依赖一体化采集编排）。
- `app/roll_app.py`、`app/youpin_app.py` 保留为兼容转发层，避免旧引用路径立即失效。
- 采集器已完成物理拆分：
  - `systems/roll/services/collector.py`
  - `systems/youpin/services/collector.py`
  - 原 `services/roll_collector.py`、`services/youpin_collector.py` 仅保留兼容导入。
- 数据源已完成系统内命名空间收口：
  - `systems/roll/data_sources/csgoroll_source.py`
  - `systems/youpin/data_sources/youpin_source.py`
  - 旧 `data_sources/csgoroll_source.py`、`data_sources/youpin_source.py` 仅保留兼容导入。
- Youpin 专属会话与浏览器上下文服务已下沉：
  - `systems/youpin/services/session.py`
  - `systems/youpin/services/browser_context.py`
  - 原 `services/youpin_session.py`、`services/youpin_browser_context.py` 仅保留兼容导入。

## 目录边界（开发约束）

后续开发请按以下边界进行：

### Shared 公共层

- 主目录：`shared/`
- 职责：跨系统可复用能力
- 当前已迁入真实实现：
  - `shared/http_source.py`
  - `shared/utils.py`
  - `shared/market_watchlist.py`
  - `shared/name_mapper.py`
  - `shared/special_items.py`
  - `shared/special_priority_rules.py`
  - `shared/repository.py`
- 约束：仅保留确实跨系统复用的模块，避免回流业务编排逻辑

### 1) Roll 采集系统

- 主目录：`systems/roll/`
- 职责：Roll 数据抓取、写入 `roll_trades / product_catalog`
- 不允许：包含 Youpin 采集逻辑、包含比价 UI 逻辑

### 2) Youpin 采集系统

- 主目录：`systems/youpin/`
- 职责：Youpin 价格抓取、写入 `youpin_prices`
- 不允许：包含 Roll 采集逻辑、包含比价 UI 逻辑

### 3) Compare 展示系统（已独立）

- 主目录：`systems/compare/`
- 职责：前端页面、查询 API、统计展示
- 不允许：直接驱动 Roll/Youpin 采集循环（相关接口返回 `410`）
- 前端资源：优先使用 `systems/compare/web/`

## 启动顺序建议

1. 启动 Roll 系统并调用 `POST /loop/start`
2. 启动 Youpin 系统并调用 `POST /loop/start`
3. 启动 Compare 系统查看展示结果

## 完成标准

当前已满足以下分割标准：

1. 三个系统拥有各自的启动入口与系统目录。
2. `systems/*` 与 `shared/*` 不再直接依赖旧 `services/*`、`data_sources/*`。
3. 旧 `services/*`、`data_sources/*`、`main*.py`、`app/*` 主要作为兼容层保留。
4. Compare 已不再承担采集调度职责。
5. 三个系统均有各自的 README、requirements、测试入口。

## 当前建议

后续新增功能时：

- Roll 相关代码只放入 `systems/roll` 或 `shared`
- Youpin 相关代码只放入 `systems/youpin` 或 `shared`
- Compare 相关代码只放入 `systems/compare` 或 `shared`
- 不再向旧 `services/`、`data_sources/` 写入新的主实现
- 旧 `services/`、`data_sources/` 中已经迁出的文件只允许保留“门牌号转发层（shim）”，不允许再回填业务实现
- 以后如果需要改业务逻辑，应直接修改 `systems/*` 或 `shared/*` 中的真实实现
