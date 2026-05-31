#!/usr/bin/env bash
# scholar-alert-translator 一键安装脚本
# 用法: bash install.sh
# 支持: Ubuntu 18.04+ / Debian 10+ / CentOS 7+

set -e

RED='\033[31m'
GREEN='\033[32m'
YELLOW='\033[33m'
BOLD='\033[1m'
NC='\033[0m'

say()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[..]${NC} $1"; }
die()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo ""
echo "================================================"
echo "  Scholar Alert Translator — 环境安装"
echo "================================================"
echo ""

# ── 检测包管理器 ──
if command -v apt &>/dev/null; then
    PKG="apt"
    INSTALL="sudo apt install -y"
    say "检测到 apt (Ubuntu/Debian)"
elif command -v dnf &>/dev/null; then
    PKG="dnf"
    INSTALL="sudo dnf install -y"
    say "检测到 dnf (CentOS/RHEL/Fedora)"
elif command -v yum &>/dev/null; then
    PKG="yum"
    INSTALL="sudo yum install -y"
    say "检测到 yum (CentOS/RHEL)"
else
    die "未检测到 apt/dnf/yum，请手动安装依赖"
fi

# ── Python3 ──
if command -v python3 &>/dev/null; then
    say "python3: $(python3 --version)"
else
    warn "安装 python3..."
    $INSTALL python3
fi

# ── pip3 ──
if command -v pip3 &>/dev/null; then
    say "pip3: $(pip3 --version 2>&1 | head -1)"
else
    warn "安装 pip3..."
    $INSTALL python3-pip
fi

# ── Python 包 ──
warn "安装 Python 依赖..."
pip3 install requests pypdf 2>&1 | tail -1
say "requests + pypdf 安装完成"

# ── pandoc ──
if command -v pandoc &>/dev/null; then
    say "pandoc: $(pandoc --version | head -1)"
else
    warn "安装 pandoc..."
    $INSTALL pandoc
    say "pandoc 安装完成"
fi

# ── XeLaTeX + 基础包 ──
if command -v xelatex &>/dev/null; then
    say "xelatex 已就绪"
else
    warn "安装 texlive-xetex（可能较慢，约200MB）..."
    if [ "$PKG" = "apt" ]; then
        sudo apt install -y texlive-xetex texlive-latex-extra texlive-fonts-extra
    else
        $INSTALL texlive-xetex texlive-latex-extra texlive-fonts-extra
    fi
    say "texlive 安装完成"
fi

# ── LaTeX 宏包（补齐 tcolorbox 等） ──
if [ "$PKG" = "apt" ]; then
    warn "补齐 LaTeX 宏包..."
    sudo apt install -y texlive-latex-extra 2>/dev/null || true
fi

# ── 中文字体 ──
if fc-list ":lang=zh" 2>/dev/null | grep -qi "Noto\|SimSun\|Song\|Serif"; then
    say "中文字体已就绪"
else
    warn "安装中文字体..."
    if [ "$PKG" = "apt" ]; then
        sudo apt install -y fonts-noto-cjk 2>/dev/null || \
        sudo apt install -y fonts-wqy-zenhei 2>/dev/null || \
        warn "自动安装字体失败，请手动: sudo apt install fonts-noto-cjk"
    else
        $INSTALL google-noto-serif-cjk-fonts 2>/dev/null || \
        $INSTALL google-noto-cjk-fonts 2>/dev/null || \
        warn "自动安装字体失败，请手动安装中文字体"
    fi
    say "字体安装完成"
fi

# ── 验证 ──
echo ""
echo "================================================"
echo "  验证环境"
echo "================================================"

ok=0
fail=0

check() {
    if eval "$1" &>/dev/null; then
        say "$2"
        ((ok++))
    else
        warn "$2 — 缺失"
        ((fail++))
    fi
}

check "python3 -c 'import requests'"   "Python: requests"
check "python3 -c 'import pypdf'"      "Python: pypdf"
check "pandoc --version"               "pandoc"
check "xelatex --version"              "xelatex"
check "kpsewhich tcolorbox.sty"        "LaTeX: tcolorbox (卡片样式)"
check "kpsewhich fancyhdr.sty"         "LaTeX: fancyhdr (页眉)"
check "kpsewhich titlesec.sty"         "LaTeX: titlesec (标题格式)"
check "fc-list ':lang=zh'"             "中文字体"

echo ""
echo "================================================"
if [ $fail -eq 0 ]; then
    echo -e "  ${GREEN}${BOLD}全部就绪！${NC} 可以运行了："
    echo ""
    echo "    cd scholar-alert-translator"
    echo "    python3 scripts/fetch-scholar-alerts.py --since-days 7"
    echo ""
else
    echo -e "  ${YELLOW}有 ${fail} 项缺失${NC}，请根据上方提示手动安装"
    echo "  之后重新运行: bash install.sh"
fi
echo "================================================"
