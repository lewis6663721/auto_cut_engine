# 项目结构

```text
auto_cut_engine/
├── main.py                  # FastAPI 入口、生命周期、静态挂载和主路由注册
├── auth.py                  # 登录、Session、权限校验
├── config.py                # 环境变量和路径配置
├── database.py              # Tortoise ORM 初始化
├── models.py                # ORM 模型，含娱乐广场游客调用日志 EntertainmentLog
├── tasks.py                 # Celery 任务入口
├── seed_data.py             # 种子用户、模板、音效、维度
├── app/                     # 逐步拆分的新应用层
│   ├── core/web.py          # Jinja2 模板、公共 view_context、render helper
│   ├── core/jobs.py         # 渲染 / 工具任务入队封装
│   ├── routes/auth.py       # 登录、注册、退出路由
│   ├── routes/account.py    # 用户中心、AI Provider 保存 / 删除 / 心跳检测路由
│   ├── routes/ai_tools.py   # AI 工具包页面 / API：对话、转字幕、文生图/视频、参考生视频、声音复刻
│   ├── routes/admin.py      # 管理后台：模板管理、维度重置
│   ├── routes/creative.py   # 创意坊：经验提交、详情、生成模板
│   ├── routes/editor.py     # 剪辑台路由聚合入口
│   ├── routes/editor_pages.py   # 剪辑台首页 / 工程页 / 项目设置
│   ├── routes/editor_assets.py  # 剪辑台素材上传 / 删除
│   ├── routes/editor_tracks.py  # 轨道创建 / 排序 / 视图 / 清理
│   ├── routes/editor_clips.py   # 片段创建 / 编辑 / 拆分 / 冻结帧 / 撤销重做
│   ├── routes/editor_export.py  # 剪辑台导出
│   ├── routes/entertainment.py # 娱乐广场页面 / API / 管理员日志路由
│   ├── routes/pages.py      # 首页、模板中心、模板详情、AI 智能匹配
│   ├── routes/remotion.py   # Remotion 模板工厂、特效资产上传、发布到剪辑台
│   ├── routes/tasks.py      # 任务中心、任务详情、任务进度 API
│   ├── routes/toolkit.py    # 剪辑工具包页面 / API：字幕烧录、视频拼接、提取音频
│   ├── services/editor_assets.py # 剪辑台素材、Remotion / 自定义特效资产 payload
│   ├── services/editor_timeline.py # 剪辑台时间线公共逻辑、校验、序列化
│   ├── services/render_tasks.py # 普通模板渲染任务创建
│   ├── services/templates.py # 模板维度表单解析
│   ├── services/tool_tasks.py # 工具任务创建、工具上传素材入库
│   └── utils/               # 公共工具函数：数值 clamp、媒体上传/URL、请求参数脱敏和摘要
├── render_engine/           # FFmpeg 渲染、AI 分析、Provider 管理、媒体预览
│   ├── ai_providers.py      # 多厂商 key 配置、心跳检测、Qwen3-ASR / 本地 ASR 兜底
│   ├── entertainment.py     # 娱乐广场规则引擎：名字打分、游客 Provider 预设、提示词
│   ├── llm_client.py        # OpenAI 兼容聊天客户端：URL 拼接、JSON 模式、Provider 差异参数
│   ├── remotion_factory.py  # Remotion 模板蓝图、源码草稿、转场/特效库生成
│   ├── remotion_timeline_renderer.py # 剪辑台 Remotion 真渲染桥接器
│   └── toolkit.py           # 剪辑工具包 / AI 工具包任务实现
├── remotion_runtime/        # Remotion CLI 子工程，用于真渲染代码化转场 / 特效
├── templates/               # Jinja2 页面模板
│   ├── account.html         # 用户中心：AI Provider 管理
│   ├── ai_tools.html        # AI 工具包入口：转字幕、文生图/视频、参考生视频、声音复刻口播
│   ├── entertainment.html   # 娱乐广场入口，游客模型配置
│   ├── entertainment_name_score.html # 名字打分工具
│   ├── entertainment_baby_names.html # 宝宝起名工具，生成 10 个候选名
│   ├── admin_entertainment_logs.html # 管理员查看娱乐广场游客调用日志
│   └── remotion_template_preview.html # Remotion 模板浏览器动态预览
├── static/                  # CSS、娱乐广场 JS 和示例资源
├── media/effects/           # 用户上传的特效包、透明动效、Lottie、LUT、Remotion zip
├── tests/                   # pytest 自动化测试
├── docs/                    # 项目文档
│   ├── DEVELOPMENT_GUIDELINES.md # 开发规范：文档同步、提示词契约、输入控件等
├── deploy/                  # 部署相关配置示例
├── scripts/                 # 部署脚本和维护脚本
├── AGENTS.md                # 开发 Agent 入口规范，要求先读开发规范
├── CLAUDE.md                # Claude / Codex 等 Agent 行为规范
├── Dockerfile
├── docker-compose.yml
├── DEPLOY.md
├── CONTRIBUTING.md
├── SECURITY.md
└── CHANGELOG.md
```

## 开发约定

- 页面优先放在 `templates/`，通用样式放在 `static/css/app.css`。
- 新增页面 / API 路由优先放进 `app/routes/`，由 `main.py` 注册 router；旧路由逐步迁移，不做一次性大搬家。
- `main.py` 只作为应用入口存在：生命周期、静态资源挂载、router 注册、`/healthz` 和 favicon。新增业务路由不要写回 `main.py`。
- 跨模块复用的纯函数放进 `app/utils/`，例如数值归一化、请求参数脱敏、大字段摘要。
- Web 公共上下文和模板渲染 helper 放在 `app/core/web.py`。
- 渲染 / 工具任务入队统一走 `app/core/jobs.py`，路由层不要直接判断 Celery 或 BackgroundTasks。
- 剪辑工具包 / AI 工具包提交异步任务时，统一复用 `app/services/tool_tasks.py` 创建 `RenderTask` 和保存上传素材。
- 剪辑台素材库、Remotion 发布资产、自定义特效资产的 payload 生成统一放在 `app/services/editor_assets.py`，避免页面路由重复拼装素材结构。
- 剪辑台时间线的公共校验、序列化、撤销栈、冻帧、字幕解析等逻辑统一放在 `app/services/editor_timeline.py`，路由层只保留请求处理。
- 模板维度配置表单解析统一放在 `app/services/templates.py`，普通模板渲染和后台模板编辑共用同一套解析逻辑。
- 普通模板渲染任务创建统一放在 `app/services/render_tasks.py`。
- 剪辑算法和 FFmpeg 逻辑放在 `render_engine/`，避免塞进路由文件。
- 所有需要登录的业务页面和 API 必须调用 `require_user` 或 `require_admin`。
- 涉及工程、任务、素材的接口必须校验资源归属。
- 新增重要功能需要补充 `tests/` 下的行为测试。
- 后续项目更新必须同步更新项目文档；涉及页面、路由、模型、配置、依赖、部署或任务流程时，至少更新 README 或对应 `docs/` 文档。
- 每次开发前先阅读根目录 `AGENTS.md` 和 `docs/DEVELOPMENT_GUIDELINES.md`。
- 提示词属于生产契约，必须定义角色、输入变量、硬约束、输出 Schema 和解析兜底；后端必须校验模型返回值，不能直接信任大模型 JSON。
- OpenAI 兼容聊天 / 分析调用统一走 `render_engine/llm_client.py`；新增 DeepSeek、OpenAI、Qwen 等同类 Provider 时，只扩展 Provider 预设、能力过滤和公共客户端差异参数。
