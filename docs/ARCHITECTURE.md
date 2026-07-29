# 系统架构

AutoCut Engine 采用 FastAPI SSR 架构，页面由 Jinja2 渲染，复杂剪辑交互在浏览器端执行，渲染任务由 Celery Worker 异步处理。

## 核心模块

| 模块 | 位置 | 职责 |
|---|---|---|
| Web 应用 | `main.py` | 路由、页面渲染、接口编排、权限入口 |
| 认证 | `auth.py` | Cookie Session、登录、管理员校验 |
| 数据模型 | `models.py` | 用户、模板、素材、工程、轨道、片段、任务 |
| 渲染任务 | `tasks.py` | Celery 任务入口 |
| 渲染引擎 | `render_engine/` | FFmpeg 命令、时间线渲染、AI 分析、预览生成 |
| AI Provider | `render_engine/ai_providers.py` | 用户中心配置、心跳检测、统一降级、Qwen3-ASR / 本地 ASR 兜底 |
| 页面模板 | `templates/` | 首页、剪辑台、任务中心、创意坊、管理后台 |
| 静态资源 | `static/` | CSS、示例视频、前端运行资源 |

## 请求链路

1. 用户登录后进入模板、剪辑台、AI 匹配、AI 工具包、用户中心、任务中心或创意坊。
2. 页面表单或编辑器 API 写入 MySQL。
3. AI 工具类请求会先解析 Provider 配置，再决定走远程模型还是本地兜底。
4. 渲染类操作创建 `RenderTask`，交给 Celery Worker。
5. Worker 调用 FFmpeg 渲染，阶段进度写入任务记录。
6. 前端轮询进度 API，完成后展示成品和剪辑详情。

## 部署形态

生产环境使用 Docker Compose：

- `web`：FastAPI + Uvicorn
- `worker`：Celery Worker
- `mysql`：MySQL 8
- `redis`：Celery Broker / Result Backend

Nginx 在宿主机上做 HTTPS 终止和反向代理，默认代理到 `127.0.0.1:18080`。
