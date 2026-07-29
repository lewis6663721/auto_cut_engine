# 🎬 AutoCut Engine 1.0：企业级智能剪辑平台

> 将人类顶级剪辑经验代码化、资产化、自动化。

## 项目文档

- [部署指南](DEPLOY.md)
- [系统架构](docs/ARCHITECTURE.md)
- [权限与账号体系](docs/AUTHORIZATION.md)
- [项目结构](docs/PROJECT_STRUCTURE.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [变更记录](CHANGELOG.md)

## 项目概述

AutoCut Engine 是一个企业级智能视频剪辑平台，采用前后端不分离的 SSR 架构（FastAPI + Jinja2 + TailwindCSS 暗黑毛玻璃风）。通过维度化配置系统，将抽象的审美标准转化为可勾选、可调参的机器指令，由 FFmpeg 渲染引擎异步执行。

### 核心能力

| 能力 | 说明 |
|---|---|
| 🎞️ 三大模板体系 | 通用 / 个性化 / 多场景，覆盖标准化到复杂叙事 |
| 🤖 AI 智能匹配 | 上传视频 → Qwen-VL 分析 → 自动推荐模板+配置+场景分割 |
| 🎨 创意坊 | 专家经验采集平台——描述剪辑方法+上传视频，AI 自动分析结构化存储 |
| 🧩 Remotion 模板工厂 | 上传参考图 / 口头描述 → 生成代码化视频模板草稿、转场和特效资产 |
| 🧠 AI 工具包 | 多厂商模型统一接入，支持 AI 对话、视频转字幕、文生图、文生视频、参考生视频 |
| 📋 剪辑详情 | 渲染完成后自动生成可解释的剪辑操作表格（时间点/类型/原因/来源） |
| ⚙️ 维度注册表 | DB 持久化的维度定义系统，管理后台可编辑参数 schema |
| 🔐 角色权限 | 普通用户 / 管理员隔离，管理后台全局管控 |

---

## 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| **后端框架** | FastAPI + Jinja2 | SSR，前后端不分离 |
| **UI 样式** | TailwindCSS | 暗黑主题 + 毛玻璃效果 |
| **异步任务** | Celery + Redis | 渲染队列 + 进度缓存 |
| **数据库** | MySQL + Tortoise-ORM + Aerich | 数据持久化 + 版本迁移 |
| **渲染引擎** | FFmpeg | 视频处理核心（滤镜图/场景检测/拼接/降噪） |
| **代码化视频模板** | Remotion 草稿生成 | React 视频模板源码、参数 Schema、转场/特效组件规划 |
| **音频处理** | noisereduce | 谱减法自适应降噪 |
| **AI 分析** | 多厂商 OpenAI 兼容接口 / Qwen 3.7 / Sedance | 视频理解 + 自然语言→结构化配置 |
| **ASR 转写** | Qwen3-ASR / GPT 转写接口 + faster-whisper 兜底 | 视频转字幕 / 本地降级转写 |

---

## 快速开始

### 环境要求

- Python 3.12+
- FFmpeg 6.0+（含 ffprobe）
- Redis 6+
- MySQL 8+

### 安装与启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 创建数据库
mysql -u root -p -e "CREATE DATABASE auto_cut_engine CHARACTER SET utf8mb4;"

# 3. 配置环境变量（或修改 config.py）
export MYSQL_URL="mysql://root:YOUR_PASSWORD@localhost:3306/auto_cut_engine"
export DASHSCOPE_API_KEY="sk-xxx"
export OPENAI_API_KEY="sk-xxx"
export SEDANCE_API_KEY="sk-xxx"

# 4. 数据库迁移
aerich migrate
aerich upgrade

# 5. 初始化种子数据（模板/资产/维度定义/管理员账号）
python seed_data.py

# 6. 启动 Redis
redis-server --daemonize yes

# 7. 启动 Web 服务
uvicorn main:app --host 0.0.0.0 --port 8000

# 8. 启动 Celery Worker（另一个终端）
celery -A tasks worker -Q celery,render --loglevel=info
```

### 访问

- **地址**：http://localhost:8000
- **管理员**：`admin` / `admin123`
- **普通用户**：`demo` / `demo123`

---

## 核心功能详解

### 1. 三大模板体系

- **通用模板**：针对高频标准化视频类型，预设经过验证的最佳实践。用户上传素材即可获得基准成品。
- **个性化模板**：在通用模板基础上，允许对视觉风格、节奏把控、声音设计等维度进行微调。
- **多场景模板**：面向长视频/复杂叙事。系统自动识别素材中的不同段落（开场/高潮/结尾），匹配不同处理规则。

### 2. 维度化配置系统

所有剪辑效果抽象为 **7 个维度**，分属 3 个组：

| 组 | 维度 Key | 说明 | 默认 |
|---|---|---|---|
| **视觉风格** (visual_style) | `cinematic_lut` | 电影感调色 LUT | ✅ 开 |
| | `particle_vfx` | 粒子特效叠加 | ❌ 关 |
| | `motion_blur` | 动态模糊增强（插帧） | ❌ 关 |
| **节奏把控** (pacing_control) | `auto_speedup` | 静默片段自动加速 | ❌ 关 |
| | `smooth_transition` | 渐进缩放转场 | ✅ 开 |
| **声音设计** (sound_design) | `deep_filter_voice` | 人声降噪（noisereduce） | ✅ 开 |
| | `sfx_auto_layer` | 自动垫底环境音效 | ❌ 关 |

#### 核心原则

- **无效果 = 原样输出**：不选任何效果时，使用 `-c copy` 流拷贝，输出视频与输入字节级一致。
- **参数可配置**：每个维度不仅有开关，还有参数滑块/下拉/文本框（如 LUT 文件、速度倍率、透明度等），由维度注册表的 params_schema 驱动。
- **维度注册表为唯一数据源**：`render_engine/dimension_registry.py` 三层架构（种子→缓存→代理），FFmpeg 命令构建器和 AI 分析器均动态读取，禁止各模块硬编码。
- **DB 持久化**：维度定义存储在 `DimensionGroup` + `DimensionDef` 表中，管理后台可编辑，运行时缓存 + 回调刷新。

### 3. AI 智能匹配

```
用户上传视频
  → FFmpeg 抽 6 帧关键帧（512px, base64 JPEG）
  → Qwen-VL-Max 分析（视频理解 + 内容分类 + 情绪/节奏判断）
  → 结构化 JSON 输出（推荐模板 + 维度配置 + 关键时刻 + 场景分割）
  → 配置规范化（白名单过滤 + 别名映射 + 参数范围限制）
  → 用户可交互调整配置面板
  → 提交渲染（携带 ai_context 完整上下文）
```

- **降级机制**：AI 分析失败时回退到安全默认配置，不阻塞渲染；ASR 会进一步回退到本地 faster-whisper / whisper / 占位字幕。
- **页面**：`/ai-match` — 上传 → 分析进度条 → 结果展示（配置面板 + 关键时刻时间线 + 场景分割方案 + 模板可手动覆盖）。

### 4. 创意坊（专家经验采集）

```
专家用户 → 填写描述 + 上传原片/成品 → 提交
  → Celery 异步 AI 分析（双管并行，~8s）：
    1. Qwen-VL 抽 4 帧 → 分析成品视频风格（视觉风格/内容类型/情绪/节奏/视觉技巧/色彩特征）
    2. Qwen-VL → 从自然语言描述提取结构化剪辑参数（每个维度 enabled/params/note原文引用）
  → status: draft → published → 创意坊列表可浏览
```

**设计理念**：不懂剪辑的用户无法定义好模板。平台让懂剪辑的人贡献经验——描述方法 + 上传视频，AI 自动分析风格 + 提取结构化参数，存入数据库作为专家经验库。未来可通过经验聚类自动生成新模板。

### 5. 剪辑详情表

渲染完成后自动生成可解释的剪辑操作记录，在任务详情页（`/task/{tid}`）以表格展示：

| 列 | 说明 |
|---|---|
| # | 序号 |
| 原视频时间点 | start → end（mm:ss.xx 格式） |
| 剪辑类型 | 调色 / 粒子特效 / 插帧 / 加速 / 转场 / 降噪 / 音效 |
| 影响 | 🎬视频 / 🔊音频 / 🎬🔊两者 |
| 剪辑详情 | 具体参数值（LUT 文件名、速度倍率、透明度等） |
| 剪辑原因 | 模板预设 / AI 推荐 / 用户配置 + 上下文说明 |
| 来源 | AI / 用户 / 模板 标签 |

- 无效果时生成"原样输出"记录。
- 多场景模式生成场景分段 + 段内效果记录。
- AI 模式额外生成关键时刻记录。
- 旧任务（无剪辑详情）显示兼容提示。

### 6. 任务管理

- **异步渲染**：Celery + Redis 队列，用户提交后无需等待。
- **实时进度**：进度写入 Redis（1h TTL），任务详情页 JS 每 2s 轮询更新进度条，完成/失败自动刷新。
- **任务详情页**：任务信息卡片 + 成品预览 + AI 分析摘要 + 剪辑详情表格 + 安全配置摘要。
- **工具任务详情页**：字幕烧录 / 拼接 / 提取音频 / 转字幕等任务直接显示进度百分比和当前步骤，工具任务不展示无意义的剪辑详情表。任务详情提供“请求参数预览”，用于回溯本次调用的 Provider、模型、上传素材和配置；API Key、Token 等敏感字段会隐藏，Base64、二进制和超长文本只展示摘要和长度，避免页面卡顿。
- **字幕烧录审核**：支持导入 `.srt` / `.ass` / `.ssa` 字幕文件，上传后先进入“审核字幕稿”，用户可直接修改字幕内容和时间轴并预览样式；最终烧录以审核稿为准，上传文件只作为导入来源。提交前必须勾选审核确认，避免未检查字幕就白烧录。
- **字幕烧录兜底**：优先使用 FFmpeg `subtitles` 滤镜；若本机 FFmpeg 缺少 libass / subtitles / drawtext 等文字滤镜，会自动切换为 Python + PIL + PyAV 逐帧烧录，再用 FFmpeg 封装原音频，保证本地轻量 FFmpeg 也能生成字幕视频。烧录前会按“最新字幕优先”策略裁剪重叠字幕时间段，避免 ASR 生成的重叠片段在最终视频里多条叠字；任务结果会保留原始字幕与最终烧录 SRT 便于核对。
- **权限隔离**：普通用户只看自己的任务，管理员全局视图。

### 7. AI 工具包与用户中心

- **用户中心**：集中管理各大模型厂商 API Key、Base URL、默认模型和能力范围。
- **功能页模型优先**：用户中心的默认模型只用于打开工具页时预填；实际调用以具体功能页的模型下拉为准，用户无需为了单次任务反复保存默认配置。
- **Provider 心跳检测**：支持对聊天 / ASR / 图片能力做可用性检查，结果会写回配置状态。
- **统一降级策略**：优先调用用户配置的 Provider，失败后自动进入本地兜底，确保业务流程不中断。
- **Qwen / 百炼默认配置**：默认 Base URL 为 `https://dashscope.aliyuncs.com/compatible-mode/v1`；也支持替换为百炼控制台里的工作空间专属兼容域名。默认模型为 `qwen3.7-plus`、`fun-asr-flash-2026-06-15`、`wan2.7-image`、`happyhorse-1.1-t2v`；聊天候选包含 `qwen-vl-plus`，图片候选包含 `qwen-image-2.0-pro-2026-04-22`。
- **AI 对话助手**：放在 AI 工具包独立子页面，并为登录用户提供全站右下角悬浮快捷对话窗。对话走 OpenAI 兼容 `chat/completions` 协议；默认可选 Qwen / 百炼 `qwen3.7-plus`，完整页也可选择 `qwen-vl-plus` 并随消息上传图片或填写图片 URL，后端按 `image_url` 内容格式提交。
- **Fun-ASR 视频转字幕**：视频转字幕优先使用百炼 `fun-asr-flash-2026-06-15` 原生多模态接口，读取模型返回的句级 / 词级时间戳生成 SRT；页面支持填写上下文和热词来提升行业词、人名、游戏词识别。`qwen3-asr-flash` 仍可作为备选，走 OpenAI 兼容 `chat/completions` + `input_audio`；长音频超出内联音频建议大小或远程失败时自动切换本地 ASR。任务详情和任务中心会显示 `Fun-ASR 大模型`、`Qwen3-ASR 大模型` 或 `本地兜底字幕` 标签，方便确认实际执行路径。
- **字幕语义拆分**：ASR SRT 生成采用纯硬代码规则引擎，不依赖 LLM。优先使用词级时间戳，按标点天然边界、`sentence_id`、`punct_id` 保护语义段，再用 `jieba` 词性和规则修复否定词、介宾、动宾、数量词、动补、“的”字结构、复合词等边界；每条严格单行，字数会根据视频宽度、字号和安全区动态估算，极端长句会递归硬拆保证不溢屏。只有句级时间戳时会按文本比例生成词级时间，作为降级路径。
- **模型下拉**：用户中心已为聊天 / ASR / 图片 / 视频生成提供候选模型下拉，支持常用模型直选和自定义手填。视频候选包含 HappyHorse 文生视频 `happyhorse-1.1-t2v`、参考生视频 `happyhorse-1.1-r2v`、视频编辑 `happyhorse-1.0-video-edit`，以及 Wan `wan2.7-t2v` 等模型。
- **文生图工具**：Provider 下拉只显示具备图片生成能力且有图片模型的厂商；阿里系图片模型按百炼文档提交 `parameters`，支持反向提示词、智能改写、水印、随机种子、尺寸和一次生成多张，任务完成后在任务详情页直接预览和下载第一张图片，完整图片列表写入任务结果 JSON。
- **百炼图片接口**：Qwen / Wan 图片模型会自动走百炼原生 `/api/v1/services/aigc/multimodal-generation/generation` 接口，并把 `compatible-mode/v1` Base URL 转换为原生 `/api/v1`，避免误打 OpenAI 兼容图片接口导致 404。
- **文生视频工具**：Provider 下拉只显示具备视频生成能力且拥有 `t2v` 文生视频模型的厂商；阿里系视频生成按百炼 `input` + `parameters` 结构提交，支持反向提示词、智能改写、水印、随机种子、画幅、清晰度和时长。
- **参考生视频工具**：按 HappyHorse R2V 文档实现，Provider 只显示 DashScope / 百炼 maas 原生域名，并仅展示 `happyhorse-1.1-r2v` / `happyhorse-1.0-r2v`。支持用 `+ / -` 逐张添加或删除 1-9 张 JPG / PNG / WEBP 参考图，提示词可用 `[Image 1]`、`[Image 2]` 指代当前 `media` 顺序；请求走百炼异步 `/api/v1/services/aigc/video-generation/video-synthesis`，参数仅提交 `resolution`、`ratio`、`duration`、`watermark`、`seed`，并轮询 `/api/v1/tasks/{task_id}` 下载结果。
- **声音复刻口播工具**：阿里百炼 Qwen-TTS 链路，必须使用 DashScope / 百炼原生 Base URL（如 `https://dashscope.aliyuncs.com/compatible-mode/v1`）和 DashScope API Key，不支持 AIFox / OpenAI 兼容网关。系统会先调用 `/services/audio/tts/customization` 创建复刻音色，再用生成的 `voice/voice_id` 调用语音合成接口输出口播音频；复刻目标模型和合成模型默认保持一致，页面强制确认声音授权。
- **工具包入口**：剪辑工具包和 AI 工具包使用横向滑动图标 Dock 展示小工具；工具包首页只做入口，具体操作进入各自子页面，剪辑工具包不混放 AI 工具。
- **本地 ASR**：优先使用 `faster-whisper`，其次 `whisper` CLI，最后回退占位字幕。

### 8. Remotion 模板工厂

- **入口位置**：登录后在顶部“创作工作台”下进入 `/remotion-templates`。
- **模板生成**：用户可上传草图、参考图、别人模板截图，或直接输入口头描述；系统生成结构化蓝图、参数 Schema、Remotion 源码草稿、HTML 预览草图和浏览器动态预览。
- **效果预览**：模板详情页提供“动态预览”入口，打开 `/remotion-templates/{id}/preview` 可看到转场、震屏、光扫、字幕弹出、粒子等效果的快速模拟；该预览不执行生成代码、不渲染 MP4，只用于低成本判断模板方向。
- **发布到剪辑台**：模板详情页可将模板中的特效 / 转场发布到剪辑台素材面板。剪辑台会保留 Remotion 模板 ID 和原始资产 key；导出时检测到 Remotion 资产会优先走 Remotion 真渲染，失败时自动回落 FFmpeg 近似映射。
- **素材入轨时长**：视频、音频和音效入轨时优先读取 FFprobe 探测到的真实媒体时长，并写入片段 `source_duration`；只有探测失败时才回退到类型默认值。新建工程一次导入多个视频时按真实时长顺序无缝排列，不受空工程默认 30 秒时间轴影响。
- **真渲染 runtime**：`remotion_runtime/` 是独立 Node 子工程，使用 Remotion CLI 渲染代码化转场 / 特效；本地首次使用需执行 `cd remotion_runtime && npm install`，Docker 镜像会在构建时自动 `npm ci`。
- **剪映特效形态参考**：专业剪辑软件的特效通常不是单个 HTML，而是“资源包 + 元数据 + 原生渲染算子 / shader / 预览缩略图”的组合。我们用 Remotion 源码和 JSON 蓝图承担“可编辑逻辑”，用浏览器 HTML 做低成本预览，用 Remotion runtime 做最终视频渲染。
- **用户自定义特效导入**：`/effect-assets/upload` 支持导入透明视频 / 普通视频 / 图片序列 zip / Lottie JSON / LUT `.cube` / 音效 / Remotion zip。视频、图片和音效类会进入剪辑台素材型特效库，可点击加入，也可从特效 / 转场库拖到时间线指定位置；素材型特效进入叠加轨，音效进入音效轨。Remotion zip 会解析 `manifest.json` 并作为代码级特效 / 转场模板入库；Lottie 和 LUT 先完成资产入库，渲染适配后续接入。
- **转场交互**：内置转场和 Remotion 代码化转场支持从转场库拖到视频轨道的片段 / 剪辑点，系统会把转场应用到落点附近的右侧视觉片段；自定义素材型转场仍按透明视频 / 普通视频素材拖入叠加轨，便于做覆盖式动效。
- **特效 / 转场删除**：轨道上的素材型特效和转场按普通片段处理，选中后可用删除按钮或 Delete / Backspace 删除；应用到当前视频片段的滤镜、Remotion 特效和 Remotion 转场可在右侧参数面板一键清除，并同步移除导出参数。
- **Remotion zip 结构**：建议包含 `manifest.json`、`src/Root.tsx`、`public/assets/`、`schema.json`、`preview.png` 或 `preview.mp4`。`manifest.json` 至少包含 `name`、`type`(`effect`/`transition`)、`entry`、`key` 和 `params_schema`。
- **安全边界**：第一阶段不直接执行 AI / 用户生成的任意 Remotion 代码；源码会先保存为可审核草稿，并做基础安全检查，禁止 `fs`、`child_process`、`process.env`、`fetch` 等危险能力。
- **转场 / 特效库**：内置首批代码化资产种子，包括闪白切、漫画分镜推入、故障闪切、速度线推进、冲击震屏、字幕弹出、能量光扫、粒子爆发。后续可把这些资产接入剪辑台的转场库和特效库。
- **Remotion 规范**：生成源码遵循 Remotion 基础约束，用 `Composition`、`useCurrentFrame()`、`interpolate()` 驱动时间轴动画，不依赖 CSS transition。

### 9. 管理后台

| 路由 | 功能 |
|---|---|
| `/admin` | 首页：模板列表（缩略图/分类/维度数/编辑/停用）+ 维度概览 |
| `/admin/template/new` | 新建模板（元数据表单 + 维度配置面板） |
| `/admin/template/{tid}/edit` | 编辑模板（同上） |
| `/admin/dimensions` | 维度定义管理（JSON schema 编辑器 / 停用启用 / 从种子重置） |

---

## 路由总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页：模板卡片展示 |
| GET | `/login` | 登录页 |
| POST | `/login` | 登录处理 |
| GET | `/register` | 注册页 |
| POST | `/register` | 注册处理 |
| GET | `/logout` | 退出 |
| GET | `/account` | 用户中心（AI Provider 配置） |
| POST | `/account/ai-providers` | 保存 Provider 配置 |
| POST | `/account/ai-providers/{id}/delete` | 删除 Provider 配置 |
| GET | `/remotion-templates` | Remotion 模板工厂列表 |
| GET | `/remotion-templates/new` | 新建 Remotion 模板草稿 |
| POST | `/remotion-templates/create` | 根据描述 / 参考图生成 Remotion 模板草稿 |
| GET | `/effect-assets/upload` | 导入用户自定义特效 / 转场资产 |
| POST | `/effect-assets/upload` | 上传素材型特效或 Remotion zip 模板包 |
| GET | `/remotion-templates/{id}` | Remotion 模板详情、蓝图、Schema 和源码 |
| GET | `/remotion-templates/{id}/preview` | Remotion 模板浏览器动态预览 |
| POST | `/remotion-templates/{id}/publish-editor` | 将 Remotion 模板特效 / 转场发布到剪辑台库 |
| POST | `/remotion-templates/{id}/delete` | 删除 Remotion 模板 |
| GET | `/api/remotion/library` | 获取内置转场 / 特效库 |
| GET | `/template/{tid}` | 模板详情页（Demo 对比 + 上传 + 配置面板） |
| POST | `/template/{tid}/render` | 提交渲染（普通模式） |
| GET | `/ai-match` | AI 智能匹配页 |
| POST | `/api/ai-match` | AI 分析接口 |
| POST | `/template/{tid}/render-ai` | 提交渲染（AI 模式，携带 ai_context） |
| GET | `/ai-tools` | AI 工具包 |
| GET | `/ai-tools/chat` | AI 对话助手 |
| POST | `/api/ai-tools/chat` | 调用大模型对话 |
| GET | `/ai-tools/video-transcription` | 视频转字幕工具页 |
| POST | `/ai-tools/video-transcription` | 视频转字幕 |
| GET | `/ai-tools/text-image` | 文生图工具页 |
| POST | `/ai-tools/text-image` | 提交文生图任务 |
| GET | `/ai-tools/text-video` | 文生视频工具页 |
| POST | `/ai-tools/text-video` | 提交文生视频任务 |
| GET | `/ai-tools/reference-video` | 参考生视频工具页 |
| POST | `/ai-tools/reference-video` | 提交参考生视频任务 |
| GET | `/ai-tools/voice-clone-tts` | 声音复刻口播工具页 |
| POST | `/ai-tools/voice-clone-tts` | 提交声音复刻口播任务 |
| POST | `/api/ai-providers/heartbeat` | Provider 心跳检测 |
| GET | `/tasks` | 任务列表（管理员全局 / 普通用户仅自己） |
| GET | `/task/{tid}` | 任务详情（剪辑详情表 + 实时进度） |
| GET | `/api/task/{tid}/progress` | 进度查询 API（Redis 实时值） |
| GET | `/creative` | 创意坊列表页 |
| GET | `/creative/new` | 创意坊提交页 |
| POST | `/creative/create` | 创建创意作品（触发异步 AI 分析） |
| GET | `/creative/{cid}` | 创意坊详情页 |
| GET | `/admin` | 管理后台首页 |
| GET | `/admin/template/new` | 新建模板 |
| GET | `/admin/template/{tid}/edit` | 编辑模板 |
| POST | `/admin/template/save` | 保存模板 |
| POST | `/admin/template/{tid}/toggle` | 启用/停用模板 |
| GET | `/admin/dimensions` | 维度定义管理 |
| POST | `/admin/dimensions/{did}` | 更新维度定义 |
| POST | `/admin/dimensions/reseed` | 从种子重置维度 |

---
## 目录结构

```text
auto_cut_engine/
├── main.py                          # FastAPI 应用入口 + 全部路由
├── config.py                        # 配置（DB/Redis/Celery/Tortoise）
├── models.py                        # Tortoise ORM 模型（User/Template/Asset/
│                                    #   RenderTask/EditDetail/CreativeWork/
│                                    #   DimensionGroup/DimensionDef）
├── database.py                      # Tortoise 初始化（幂等）
├── auth.py                          # 认证（bcrypt + cookie session）
├── tasks.py                         # Celery 任务（render_video / analyze_creative）
├── seed_data.py                     # 种子数据（模板/资产/维度/用户）
├── render_engine/
│   ├── ffmpeg_builder.py            # FFmpeg 命令构建器（维度→滤镜映射）
│   ├── pipeline.py                  # 渲染管线（单场景/多场景/音频/拼接）
│   ├── audio.py                     # 音频处理（noisereduce 降噪）
│   ├── scene_detector.py            # 场景检测（FFmpeg select=gt(scene,0.3)）
│   ├── ai_analyzer.py               # AI 智能匹配（Qwen-VL 分析+配置生成）
│   ├── creative_analyzer.py         # 创意坊 AI 分析（视频风格+描述解析）
│   ├── edit_reporter.py             # 剪辑详情生成器（渲染后自动记录）
│   └── dimension_registry.py        # 维度注册表（种子→缓存→代理三层）
├── static/
│   ├── demos/                       # Demo 视频（原片+成品）
│   ├── luts/                        # LUT 滤镜文件（.cube）
│   └── sfx/                         # 音效资产
├── templates/
│   ├── base.html                    # 基础布局（导航栏+页脚+Tailwind）
│   ├── index.html                   # 首页（模板卡片）
│   ├── template_detail.html         # 模板详情（Demo对比+配置面板+参数滑块）
│   ├── ai_match.html                # AI 智能匹配（上传→分析→配置→渲染）
│   ├── tasks.html                   # 任务列表（进度条+详情按钮）
│   ├── task_detail.html             # 任务详情（剪辑详情表+进度轮询）
│   ├── creative_list.html           # 创意坊列表（作品卡片网格）
│   ├── creative_new.html            # 创意坊提交（表单+文件上传）
│   ├── creative_detail.html         # 创意坊详情（视频+AI分析+结构化参数）
│   ├── admin.html                   # 管理后台首页（模板列表+维度概览）
│   ├── admin_template_edit.html     # 模板编辑器
│   └── admin_dimensions.html        # 维度定义管理
├── migrations/                      # Aerich 迁移文件
├── tests/
│   ├── conftest.py                  # 全局 fixture（DB 连接/测试用户）
│   ├── factories.py                 # 数据工厂
│   ├── test_infrastructure.py       # 基建测试（DB/Celery/FFmpeg）
│   ├── test_templates.py            # 模板 API 测试
│   ├── test_render_logic.py         # FFmpeg 命令构建单元测试
│   ├── test_e2e_flow.py             # 端到端流程测试
│   └── test_scene_audio_pipeline.py # 多场景+音频管线测试
└── scripts/
    └── generate_assets.py           # 测试资产生成脚本
```

---

## 数据模型

### User
`id` / `username` / `password_hash` / `is_admin` / `created_at`

### Template
`id` / `name` / `category`(general/personalized/multi_scene) / `description` / `demo_original_url` / `demo_result_url` / `thumbnail_url` / `config_schema`(JSON) / `sort_order` / `is_active` / `created_at`

### Asset
`id` / `name` / `file_path` / `asset_type`(video/image/audio/sfx) / `tags`(JSON) / `created_at`

### RenderTask
`id` / `user_id`(FK) / `template_id`(FK) / `source_asset_id`(FK) / `applied_config`(JSON) / `ai_context`(JSON) / `status`(pending/processing/success/failed) / `progress` / `result_url` / `error_log` / `created_at` / `completed_at`

### AiProviderCredential
`id` / `user_id`(FK) / `provider_key` / `label` / `base_url` / `api_key_secret` / `default_chat_model` / `default_asr_model` / `default_image_model` / `default_video_model` / `capabilities`(JSON) / `is_enabled` / `last_status` / `last_message` / `last_checked_at` / `created_at`

### RemotionTemplate
`id` / `user_id`(FK) / `title` / `category` / `description` / `source_prompt` / `source_type` / `reference_files`(JSON) / `blueprint`(JSON) / `props_schema`(JSON) / `remotion_code` / `preview_html` / `preview_url` / `effect_keys`(JSON) / `transition_keys`(JSON) / `status` / `version` / `created_at`

### EditDetail
`id` / `task_id`(FK, CASCADE) / `start_time` / `end_time` / `edit_type` / `edit_detail` / `edit_reason` / `affects`(video/audio/both) / `source`(template/ai/user) / `dimension_key` / `sort_order` / `created_at`

### CreativeWork
`id` / `user_id`(FK) / `title` / `description` / `original_video_path` / `result_video_path` / `result_video_url` / `video_style` / `content_category` / `mood` / `pacing` / `duration` / `ai_analysis`(JSON) / `parsed_config`(JSON) / `tags`(JSON) / `status`(draft/published/archived) / `view_count` / `like_count` / `created_at`

### DimensionGroup
`id` / `key` / `label` / `icon` / `sort_order` / `is_active`

### DimensionDef
`id` / `key` / `label` / `group_key`(FK) / `icon` / `description` / `params_schema`(JSON) / `sort_order` / `is_active`

---

## 渲染管线

### 单场景流程

```
源视频 → [场景检测?] → FFmpeg 滤镜图构建 → 音频预处理（可选） → 渲染 → 剪辑详情生成
```

1. **配置解析**：`applied_config` JSON → `_is_enabled()` / `_get_params()` 统一处理两种格式
2. **滤镜构建**：`ffmpeg_builder.build_filter_chain()` — 按维度生成滤镜串
   - 有视频滤镜 + 无额外输入 → `-vf`
   - 有额外输入（粒子叠加等）→ `-filter_complex` + `-map`
   - 无任何效果 → `-c copy`（流拷贝）
3. **音频处理**（可选）：FFmpeg 提取 PCM → noisereduce 自适应降噪 → FFmpeg 滤波（highpass=60, treble 提升）→ 混音
4. **渲染**：FFmpeg 执行，进度写入 Redis
5. **剪辑详情**：`edit_reporter.generate_edit_details()` 非致命调用

### 多场景流程

```
源视频 → 场景检测（FFmpeg select=gt(scene,0.3)）
  → 无切换时回退 20%/60%/20%（opening/climax/ending）
  → 分段渲染（opening: LUT调色 / climax: 粒子VFX / ending: 渐进缩放）
  → 各段统一编码 H.264/AAC
  → concat demuxer 拼接
  → 音频处理 + SFX 混音
```

### FFmpeg 技术要点

- **zoompan 弃用**：`d` 参数导致时长膨胀，改用 `scale→crop→scale` 时间表达式
- **minterpolate**：`mi_mode=blend`（帧混合，快 40 倍）替代 `mi_mode=mci`（运动补偿，极慢）
- **scale2ref**：两个输出（scaled + ref），ref 接 `nullsink` 消掉
- **filter_complex**：同一 pad 不能消费两次 → `split` 分流
- **源尺寸探测**：`ffprobe` 探测源宽高，输出分辨率 = 源分辨率，不硬编码
- **音频降噪**：noisereduce 需要单声道 16kHz WAV，FFmpeg 先提取 `pcm_s16le`

---

## 测试

```bash
# 运行全部测试（SQLite 内存/文件型，不依赖 MySQL）
pytest tests/ -v

# 当前状态：35/35 通过，0 warnings
```

| 测试文件 | 覆盖范围 |
|---|---|
| `test_infrastructure.py` | DB 连接 / Celery 连通性 / FFmpeg 可用性 |
| `test_templates.py` | 模板加载 / Schema 验证 / 详情页渲染 / 上传创建任务 |
| `test_render_logic.py` | FFmpeg 命令构建（单场景/多场景/各维度滤镜） |
| `test_e2e_flow.py` | 端到端：上传 → Celery 渲染 → DB 状态变更 |
| `test_scene_audio_pipeline.py` | 场景检测 / 音频降噪 / 粒子叠加 / concat 拼接 |

---

## UI 设计规范

- **背景**：Slate-950 / Neutral-900（极深灰，避免纯黑）
- **卡片**：带透明度深色背景 + `backdrop-blur` 毛玻璃 + 细微边框
- **主色调**：赛博蓝（Cyan-to-Blue 渐变），仅用于高亮元素
- **文字**：标题 Slate-100，正文 Slate-400
- **Demo 对比**：Before/After 并排播放器

---

## 后续规划

- [ ] 生产环境部署（gunicorn + nginx + supervisor）
- [ ] 效果可解释性 UI（效果说明 + 可配置开关的详细解释）
- [ ] 多场景模板演示内容增强
- [ ] 创意坊经验聚类成模板（相似风格+参数自动归簇 → 生成新模板）
- [ ] WebSocket 替代轮询（实时进度推送）
