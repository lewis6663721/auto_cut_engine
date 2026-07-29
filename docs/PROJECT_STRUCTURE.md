# 项目结构

```text
auto_cut_engine/
├── main.py                  # FastAPI 入口、页面路由、业务 API
├── auth.py                  # 登录、Session、权限校验
├── config.py                # 环境变量和路径配置
├── database.py              # Tortoise ORM 初始化
├── models.py                # ORM 模型
├── tasks.py                 # Celery 任务入口
├── seed_data.py             # 种子用户、模板、音效、维度
├── render_engine/           # FFmpeg 渲染、AI 分析、Provider 管理、媒体预览
│   ├── ai_providers.py      # 多厂商 key 配置、心跳检测、Qwen3-ASR / 本地 ASR 兜底
│   ├── remotion_factory.py  # Remotion 模板蓝图、源码草稿、转场/特效库生成
│   ├── remotion_timeline_renderer.py # 剪辑台 Remotion 真渲染桥接器
│   └── toolkit.py           # 剪辑工具包 / AI 工具包任务实现
├── remotion_runtime/        # Remotion CLI 子工程，用于真渲染代码化转场 / 特效
├── templates/               # Jinja2 页面模板
│   ├── account.html         # 用户中心：AI Provider 管理
│   ├── ai_tools.html        # AI 工具包入口：转字幕、文生图/视频、参考生视频、声音复刻口播
│   └── remotion_template_preview.html # Remotion 模板浏览器动态预览
├── static/                  # CSS 和示例资源
├── media/effects/           # 用户上传的特效包、透明动效、Lottie、LUT、Remotion zip
├── tests/                   # pytest 自动化测试
├── docs/                    # 项目文档
├── deploy/                  # 部署相关配置示例
├── scripts/                 # 部署脚本和维护脚本
├── Dockerfile
├── docker-compose.yml
├── DEPLOY.md
├── CONTRIBUTING.md
├── SECURITY.md
└── CHANGELOG.md
```

## 开发约定

- 页面优先放在 `templates/`，通用样式放在 `static/css/app.css`。
- 剪辑算法和 FFmpeg 逻辑放在 `render_engine/`，避免塞进路由文件。
- 所有需要登录的业务页面和 API 必须调用 `require_user` 或 `require_admin`。
- 涉及工程、任务、素材的接口必须校验资源归属。
- 新增重要功能需要补充 `tests/` 下的行为测试。
- 后续项目更新必须同步更新项目文档；涉及页面、路由、模型、配置、依赖、部署或任务流程时，至少更新 README 或对应 `docs/` 文档。
