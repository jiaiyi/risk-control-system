# 电商风控系统（AI_Risk）

面向电商业务的实时风险控制系统 —— 规则驱动 · 决策可解释 · 流程可闭环 · 审计可追溯。

在**登录、营销领券、下单、支付、售后申请、物流投诉**六个关键节点实时完成「特征计算 → 规则匹配 → 评分决策」全链路，覆盖刷单、薅羊毛、恶意退款、黄牛囤货、撞库盗号等风险场景。

---

## 文档索引

| 目录 | 内容 |
|------|------|
| `specs/PRD.md` | 需求规格说明书 v1.3（54 条 FR / 43 特征 / 41 规则 / 40 条 AC） |
| `specs/SPEC.md` | 技术设计规格 v1.3（29 张表 / 52 个接口 / 8 条 ADR / 27 个任务） |
| `specs/需求覆盖核对表.md` | 对《大模型就业项目实战》2.3 全部 52 条需求的逐条核对 |
| `design/` | 原型生成脚本与 .pen 设计源文件 |
| `exports/` | 原型图导出（13 个页面） |

---

## 核心指标

| 项 | 值 |
|----|-----|
| 事件类型 | 6 类 |
| 风控特征 | 43 个（用户 14 / 订单 8 / 地址 4 / 登录 4 / 领券 6 / 设备 2 / IP 2 / 商品 2 / 手机号 1） |
| 预置规则 | 41 条 / 10 大风险场景 |
| 数据表 | 29 张（19 业务只读 + 10 风控读写） |
| 接口 | 45 个端点 / 10 个 Router |
| 决策阈值 | 30 / 60 / 80 → 通过 / 标记 / 人工审核·二次验证 / 拒绝 |

---

## 技术栈

| 层 | 技术 |
|----|------|
| Web | FastAPI 0.115 + Uvicorn |
| ORM | SQLAlchemy 2.0（Mapped 声明式） |
| 校验 | Pydantic 2.x + pydantic-settings |
| 存储 | MySQL 8.x（PyMySQL） |
| 缓存 | Redis 7.x（缓存旁路，可降级） |
| 模板 | Jinja2 + Bootstrap 5 + ECharts |
| Agent | LangGraph + LangChain + 通义千问（OpenAI 兼容模式） |
| 测试 | pytest + httpx |

---

## 目录结构

```
risk-control-system/
├── specs/                 # 规范文档（PRD / SPEC / 核对表）
├── design/                # 原型生成脚本与 .pen 设计源文件
├── exports/               # 原型图导出
├── app/
│   ├── main.py            # FastAPI 应用入口
│   ├── config.py          # 全局配置（所有可调参数）
│   ├── database.py        # 引擎 / Session / 依赖注入
│   ├── models.py          # ORM 模型（29 张表）        [T-02]
│   ├── schemas.py         # Pydantic 契约              [T-02]
│   ├── cache.py           # Redis 客户端与降级封装      [T-06]
│   ├── api/               # 10 个 Router               [T-17]
│   ├── engine/            # 特征 / 规则 / 决策引擎      [T-07~T-09]
│   ├── service/           # 事件管道 / 案件 / 名单 / 画像 / 图谱 / 审计 / 仿真
│   └── agent/             # ReAct 编排 + 8 个工具       [T-18]
├── templates/             # Jinja2 页面模板            [T-19~T-24]
├── static/                # CSS / JS
├── sql/                   # 建表与预置数据脚本          [T-02~T-05]
├── scripts/               # 数据生成与运维脚本          [T-04、T-05]
├── tests/                 # 单元与集成测试              [T-25]
├── requirements.txt
└── .env.example
```

---

## 快速开始

### 1. 环境要求

- Python 3.12
- MySQL 8.0+（库名 `ecs`）
- Redis（可选，关闭后自动降级直查数据库）

**本机已安装**（位于 `D:\idetools_main\`）：

| 服务 | 版本 | 端口 | 启动方式 |
|------|------|------|---------|
| MySQL | 8.0.40 | 3306 | 双击 `D:\idetools_main\mysql\start-mysql.bat` |
| Redis | 5.0.14.1 | 6379 | 双击 `D:\idetools_main\redis\start-redis.bat` |

一键启动两个服务：`D:\idetools_main\start-db-services.bat`

> 说明：Redis 使用 Windows 移植版（tporadowski），版本为 5.0 —— Redis 官方自 6.0 起不再提供 Windows 版本。
> 本项目用到的命令（SETEX / GET / DEL / ZSET / TTL）5.0 均支持。
>
> MySQL 连接账号：`root` / 空密码（仅本地开发环境）。

### 2. 安装依赖

> **⚠️ 本机 pip 不可用** —— 新建 venv 里的 pip 取不到索引（`from versions: none`，且无任何连接日志），
> 即使清空代理变量、换镜像源均无效（已实测）。**统一改用 uv**。

```bash
python -m venv .venv
export VIRTUAL_ENV="$(pwd)/.venv"                    # Windows Git Bash
uv pip install -r requirements.txt --python .venv/Scripts/python.exe
```

uv 的清华源与缓存已在用户级 `%APPDATA%\uv\uv.toml` 配置好（`cache-dir=D:\uv-cache`），无需额外参数。

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填写 DB_PASSWORD 与 LLM_API_KEY
```

### 4. 初始化数据（待 T-02~T-05 完成后可用）

```bash
python scripts/init_db.py
```

### 5. 启动服务

```bash
python -m app.main
# 或：uvicorn app.main:app --reload --port 8000
```

访问：
- 健康检查 `http://localhost:8000/health`
- 接口文档 `http://localhost:8000/docs`

---

## 关键设计约束（实现时不可违背）

来自 `specs/SPEC.md` 第 11.1 节的已确认决策：

1. **评分链路顺序固定**：`max(分值) → +BONUS×额外条数 → +灰名单加严 → 一票否决判定 → 截断 100`
2. **一票否决仅由 `rule.risk_level == '极高'` 触发**，不受灰名单加严影响
3. **订单维度与商品维度特征禁止缓存**（含本次订单实时金额）
4. **缓存只加速读，不改变判定结果**；缓存不可用时自动降级直查数据库，**不阻断风控主链路**
5. **名单判定路径不产生特征快照**（0 条 `risk_feature`）
6. **单次风控检查的全部落库操作在同一事务内提交**，任一步失败整体回滚
7. **处置回写只允许改写「未终结」状态的单据**（已发货订单、已完成售后不回写）
8. **规则删除为逻辑删除**；**审计表只追加，不提供更新/删除接口**
9. **规则条件求值使用白名单运算符**，禁止 `eval` / `exec` / 第三方沙箱库
10. **仿真回放零副作用**（不写事件、不写特征、不建案件、不动画像）

---

## 开发进度

| 任务 | 模块 | 状态 |
|------|------|------|
| T-01 | 项目骨架 | ✅ 完成 |
| T-02 | 数据层（ORM + 建表脚本） | ⬜ |
| T-03 | 增量迁移脚本 | ⬜ |
| T-04~T-05 | 数据初始化（业务数据 + 41 条规则） | ⬜ |
| T-06 | 缓存层 | ⬜ |
| T-07~T-09 | 引擎层（特征 / 规则 / 决策） | ⬜ |
| T-10~T-16 | 服务层（名单 / 事件 / 案件 / 画像 / 图谱 / 审计 / 仿真） | ⬜ |
| T-17 | 接口层（45 个端点） | ⬜ |
| T-18 | Agent 层 | ⬜ |
| T-19~T-24 | 前端（13 个页面） | ⬜ |
| T-25~T-26 | 测试与验收 | ⬜ |
