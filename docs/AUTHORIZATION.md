# 权限与账号体系

## 页面访问规则

未登录用户只能访问：

- `/`
- `/home`
- `/entertainment`
- `/entertainment/name-score`
- `/entertainment/baby-names`
- `/api/entertainment/provider/heartbeat`
- `/api/entertainment/name-score`
- `/api/entertainment/baby-names`
- `/login`
- `/register`
- `/healthz`
- `/static/*`

娱乐广场游客接口必须由用户填写自己的 API Key，后端会记录脱敏后的调用日志。其它业务页面会跳转到登录页，并携带 `next` 参数。登录成功后自动回到原页面。

## API 访问规则

业务 API 默认要求登录。未登录请求返回 `401 Login required`，避免前端 fetch 被重定向到 HTML 登录页后产生解析错误。

## 登录有效期

默认登录会话有效期为 6 小时，由 `ACCESS_TOKEN_EXPIRE_MINUTES=360` 控制。

## 角色

| 角色 | 权限 |
|---|---|
| 普通用户 | 只能查看和操作自己的任务、工程、素材 |
| 管理员 | 可查看全部任务和工程，可管理模板、维度、专家经验模板化和娱乐广场游客调用日志 |

## 管理员专属日志

- `/admin/entertainment-logs` 仅管理员可访问。
- 日志包含游客调用的工具、Provider、模型、耗时、状态、脱敏请求参数、上游模型请求和响应。
- API Key、Token、密码、Base64、二进制和超长文本会隐藏、摘要化或截断，避免泄露敏感信息和拖慢页面。

## 默认账号

开发环境种子数据会创建：

- 管理员：`admin` / `admin123`
- 普通用户：`demo` / `demo123`

生产环境上线后应立即修改默认密码，并更换 `.env.production` 中的 `SECRET_KEY`。
