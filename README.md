# Scholar Alert Translator

把 Google Scholar Alert 邮件整理成中英对照学术快讯的 skill。它会从邮箱 IMAP 拉取 Scholar Alert 邮件，解析论文信息，补全缺失字段，翻译标题和摘要，并生成 Markdown 与 PDF。

这个仓库的核心入口是 `SKILL.md`；`README.md` 只是给 GitHub 读者看的安装和配置说明。

## 功能

- 从 IMAP 邮箱读取 Google Scholar Alert 邮件
- 提取论文标题、链接、作者、期刊、年份和摘要片段
- 通过 Crossref、OpenAlex、Semantic Scholar 等外部来源补全缺失信息
- 使用 OpenAI 兼容接口或 Ark 接口翻译中文标题和中文摘要
- 生成固定模板的中英对照 Markdown
- 使用 Pandoc + XeLaTeX 生成带论文卡片样式的 PDF
- 支持 webhook 或文件复制通知

## 目录

```text
scholar-alert-translator/
├── SKILL.md
├── README.md
├── install.sh
├── run.sh
├── .env.example
└── scripts/
    ├── fetch-scholar-alerts.py
    ├── pdf_fixed_style.tex
    └── wrap_papers.lua
```

## 使用前需要补充什么

其他人要使用这个 skill，至少需要准备：

1. 邮箱 IMAP 信息
   - `EMAIL_ADDRESS`
   - `EMAIL_APP_PASSWORD`
   - `IMAP_SERVER`
   - 邮箱里需要能收到 Google Scholar Alert 邮件
   - 对 163、QQ、Gmail 等邮箱，通常要先开启 IMAP，并使用“授权码/应用专用密码”，不要直接填网页登录密码

2. 翻译 API 信息
   - `TRANSLATE_PROVIDER`
   - `OPENAI_API_KEY`
   - `OPENAI_BASE_URL`
   - `OPENAI_MODEL`
   - 如果使用 Ark/火山方舟兼容接口，也可以补 `ARK_API_KEY`、`TRANSLATE_BASE_URL`、`TRANSLATE_MODEL`

3. 本机依赖
   - Python 3
   - Python 包：`requests`、`pypdf`
   - `pandoc`
   - `xelatex`
   - LaTeX 宏包：`tcolorbox`、`fancyhdr`、`titlesec` 等
   - 中文字体：推荐 Noto Serif CJK SC、Source Han Serif SC 或 SimSun

4. 可选通知配置
   - `NOTIFY_WEBHOOK`
   - `NOTIFY_COPY_TO`

## 配置

复制示例配置：

```bash
cp .env.example .env
```

然后编辑 `.env`，填入自己的邮箱授权码和翻译 API Key。

不要把 `.env` 上传到 GitHub。仓库已经用 `.gitignore` 排除了 `.env`、`.cache/`、`output/` 和生成文件。

## 安装依赖

Linux 环境可先尝试：

```bash
bash install.sh
```

如果自动安装失败，按脚本提示手动安装 Python 包、Pandoc、XeLaTeX、LaTeX 宏包和中文字体。

## 常用命令

完整流程：

```bash
python3 scripts/fetch-scholar-alerts.py --since-days 7
```

只抓取和补全，保存 JSON 后停止，不翻译、不生成 Markdown/PDF：

```bash
python3 scripts/fetch-scholar-alerts.py --skip-translate --json-output /tmp/papers_raw.json
```

从已有 JSON 生成 Markdown + PDF：

```bash
python3 scripts/fetch-scholar-alerts.py --json-input /tmp/papers_translated.json
```

离线冒烟测试，不访问邮箱和翻译 API：

```bash
python3 scripts/fetch-scholar-alerts.py --test --skip-pdf --output-dir /tmp/scholar-alert-test
```

每日 cron 示例：

```cron
0 9 * * * cd /path/to/scholar-alert-translator && bash run.sh
```

## 输出文件

默认输出在 `OUTPUT_DIR` 指定的目录，未设置时脚本默认使用 `/tmp`，`run.sh` 默认使用仓库内的 `output/`。

- `papers_translated.json`
- `scholar_alert_output.md`
- `scholar_alert_output.pdf`

## 安全提醒

- 不要提交 `.env`
- 不要提交 `.cache/`
- 不要提交生成的 JSON、Markdown、PDF
- 邮箱授权码和 API Key 泄露后应立即撤销并重新生成
- 如果公开仓库，建议先检查历史提交，确认没有包含密钥或私人邮件内容

## 作为 skill 使用

把整个目录放到小龙虾/OpenClaw 可识别的 skills 目录后，触发词见 `SKILL.md` 的 frontmatter。实际执行规则以 `SKILL.md` 为准。
