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
├── render_engine/           # FFmpeg 渲染、AI 分析、媒体预览
├── templates/               # Jinja2 页面模板
├── static/                  # CSS 和示例资源
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

