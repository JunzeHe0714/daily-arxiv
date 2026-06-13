# Daily arXiv

每天北京时间 9 点自动从 arXiv 抓取 CS/LLM 相关新论文和更新论文，按你的兴趣偏好排序，发送到 `junzehe0714@gmail.com`。

第一版不需要 OpenAI API key，使用 arXiv 元数据、关键词、作者、摘要相似度和你每天的邮件回复来做个性化排序。

## 你每天怎么反馈

直接回复收到的 `Daily arXiv - YYYY-MM-DD` 邮件即可，例如：

```text
多发第三篇类似的，少发第一篇；多发 landscape 和 inference optimization，少发普通 benchmark。
```

下一次运行会读取你的回复，并调整排序权重。邮件里的论文会编号为 `#1` 到 `#15`，所以你可以说 `多发 #3`、`少发 #1`、`更像第 4 篇`。

## GitHub 设置

1. 在 GitHub 创建一个新仓库，例如 `daily-arxiv`。
2. 上传本项目里的所有文件。
3. 在仓库里进入 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`。
4. 添加这些 Secrets：

| Secret | 值 |
| --- | --- |
| `GMAIL_USER` | `junzehe0714@gmail.com` |
| `GMAIL_APP_PASSWORD` | Gmail App Password，不是 Gmail 登录密码 |
| `EMAIL_TO` | `junzehe0714@gmail.com` |

5. 进入仓库的 `Actions` 页面，启用 workflow。
6. 可以手动点 `Daily arXiv` -> `Run workflow` 测试一次。

## Gmail App Password

你需要开启 Google 账号的两步验证，然后创建 App Password：

1. 打开 Google Account。
2. 进入 `Security`。
3. 开启 `2-Step Verification`。
4. 搜索或进入 `App passwords`。
5. 创建一个用于 Mail 的 16 位专用密码。
6. 把它填入 GitHub Secret `GMAIL_APP_PASSWORD`。

如果 App Password 失效，更新 GitHub Secret 即可。

为了读取你每天回复邮件里的反馈，请确认 Gmail 设置里启用了 IMAP：

1. 打开 Gmail 网页版。
2. 进入 `Settings` -> `See all settings` -> `Forwarding and POP/IMAP`。
3. 在 `IMAP access` 里选择 `Enable IMAP`。
4. 保存设置。

## 自动运行时间

`.github/workflows/daily-arxiv.yml` 使用 UTC `01:00`，对应北京时间每天 `09:00`。

GitHub Actions 的定时任务有时会延迟几分钟，这是 GitHub 平台正常现象。

## 个性化配置

主要配置在 [config/profile.json](config/profile.json)。

你通常不需要手动改它。日常偏好通过回复邮件维护。

可以手动修改的常见项：

- `interest_terms`: 长期关注关键词。
- `must_watch_authors`: 重点作者。
- `less_like_terms`: 降权主题。
- `categories`: 默认抓取 arXiv 分类。

## 数据文件

自动生成或更新：

- `data/sent_history.json`: 已发送论文，避免重复。
- `data/feedback_memory.json`: 从你的邮件回复积累的偏好。
- `data/last_email_items.json`: 上一封邮件的编号到论文映射，用于解析“多发第三篇”。
- `outputs/latest_email.html`: 最近一次生成的邮件 HTML 预览。

GitHub Actions 会在每次运行后把这些文件提交回仓库。

## 无 API key 版本的限制

这一版不会真正“阅读全文”。它基于标题、作者、arXiv 摘要、分类和你的反馈做摘要与排序。

邮件里的英文内容主要来自论文原摘要的压缩提取；中文内容是规则化摘要和解释。等你以后愿意接 OpenAI API key，可以把同一套管线升级成更强的双语精读版。
