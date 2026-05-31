---
name: scholar-alert-translator
description: 从邮箱拉取 Google Scholar Alert 邮件，提取论文信息，补全字段，翻译成中文，生成中英对照 Markdown 和 PDF。触发词：「翻译学术快讯」「scholar alert」「翻译邮件里的论文」「谷歌学术快讯」。
---

# Scholar Alert Translator

## 流程

```
IMAP 邮箱 → papers_raw.json → papers_translated.json → Markdown → PDF
```

脚本：`scripts/fetch-scholar-alerts.py`，一条命令完成全流程。

## 快速使用

```bash
# 完整流程（拉邮件→补全→翻译→Markdown→PDF）
python3 scripts/fetch-scholar-alerts.py --since-days 7

# 分步：只拉取+补全，不翻译；保存 JSON 后停止，不生成 Markdown/PDF
python3 scripts/fetch-scholar-alerts.py --skip-translate --json-output /tmp/papers_raw.json

# 从已有 JSON 生成 Markdown + PDF
python3 scripts/fetch-scholar-alerts.py --json-input /tmp/papers_translated.json

# 离线冒烟测试：解析内置样例并生成 JSON + Markdown，不访问邮箱/API
python3 scripts/fetch-scholar-alerts.py --test --skip-pdf --output-dir /tmp/scholar-alert-test
```

邮箱凭据和 API Key 在 `.env` 中配置，支持 `--email`/`--app-password`/`--imap-server` 覆盖。

## 输出文件

| 文件 | 默认路径 | 可覆盖参数 |
|------|---------|-----------|
| JSON | `<output-dir>/papers_translated.json` | `--json-output` |
| Markdown | `<output-dir>/scholar_alert_output.md` | `--markdown-output` |
| PDF | `<output-dir>/scholar_alert_output.pdf` | `--pdf-output` |

## 核心规则

### 信息来源优先级

1. **Google Scholar Alert 邮件**是主来源，外部只补充缺失字段，不覆盖
2. 外部来源优先级：DOI 官方页 > 期刊官网 > Crossref > OpenAlex > Semantic Scholar
3. LLM **只用于翻译** `title_cn` 和 `abstract_cn`，不得编造 authors/journal/year/abstract_en

### 论文 Markdown 模板（固定，不可更改）

```md
## [1. English Title][paper-1]

**中文标题：** 中文标题内容

**作者：** 作者  
**期刊：** 期刊  
**年份：** 年份  
**来源：** 来源  
**摘要来源：** 摘要来源  
**链接来源：** Google Scholar Alert title link

### English Abstract

英文摘要正文。

### 中文摘要

中文摘要正文。

---

[paper-1]: <https://example.com/paper>
```

### PDF 样式（固定 `math-paper-v2`）

- A4，2.5cm 边距，11pt，1.12 行距
- **配色**：海军蓝 Accent (`#1a5276`) 标题 + 深灰 (`#2c3e50`) 正文
- **论文卡片**：每篇论文独立灰底圆角卡片（`tcolorbox`），左侧浅蓝竖线点缀
- **页眉**：左 "Google Scholar Alert" + 右日期
- **页脚**：居中页码
- 英文衬线字体（TeX Gyre Termes / Times New Roman 系列）
- 中文衬线字体（Noto Serif CJK SC / SimSun 系列）
- 数学字体：Latin Modern Math / STIX Two Math
- 超链接蓝色可点击（主题色）
- 数学公式字体：STIX Two Math / Latin Modern Math
- 标题超链接保持可点击但隐藏颜色
- 字体可通过 `.env` 的 `PDF_MAIN_FONT`/`PDF_CJK_FONT`/`PDF_MATH_FONT` 覆盖
- 必须使用 `scripts/pdf_fixed_style.tex`，不可临时改 Pandoc 参数

### 成功标准

每篇论文必须有：英文标题（可点击超链接）、中文标题、来源、英文摘要、中文摘要。作者、期刊、年份必须出现在模板字段中；无法核实时写作 `未核实`，不得编造。不得出现：No Title、乱码（■□�）、裸 URL、缺失中文摘要的半成品。脚本内置验证（JSON → Markdown → PDF 三级检查），任一失败则停止。

## 文件交付规则

PDF 生成后必须主动交付给用户：
1. ChatGPT sandbox → 复制到 `/mnt/data/`，给下载链接
2. Feishu/Lark bot → 复制到 workspace 目录，通过 openclaw 发送
3. 无文件上传能力 → 说明限制，提供本地路径

## .env 配置项

```env
# 邮箱
EMAIL_ADDRESS=xxx@163.com
EMAIL_APP_PASSWORD=xxx
IMAP_SERVER=imap.163.com

# 翻译 API（TRANSLATE_PROVIDER=openai 或 ark）
TRANSLATE_PROVIDER=openai
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
OPENAI_MODEL=Qwen/Qwen2.5-7B-Instruct

# 可选：字体覆盖
# PDF_MAIN_FONT=Microsoft YaHei
# PDF_CJK_FONT=Microsoft YaHei
```
