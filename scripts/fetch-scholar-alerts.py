#!/usr/bin/env python3
"""
fetch-scholar-alerts.py
Pipeline: JSON → Markdown → PDF (pandoc+XeLaTeX, NO ReportLab)
"""

import argparse, email, imaplib, json, logging, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse, parse_qs

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    requests = None
    HAS_REQUESTS = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

TITLE_MATCH_THRESHOLD = 0.6
FIXED_TEMPLATE_VERSION = "fixed-bilingual-v4-math-paper"
FIXED_PDF_STYLE_VERSION = "math-paper-v2"
BAD_GLYPHS = ("■", "□", "�")
TRANSLATION_FAILURE_PREFIXES = ("[翻译失败]", "[翻译不可用]")

# ═══════════════════════════════════════════════════════════════
# 0. ENVIRONMENT CHECK — runs at startup, reports missing deps
# ═══════════════════════════════════════════════════════════════

_REQUIRED_PYPI = ["requests"]
_OPTIONAL_PYPI = ["pypdf", "PyPDF2"]
_REQUIRED_BINARIES = {
    "pandoc": "生成 PDF（--skip-pdf 可跳过）",
    "xelatex": "PDF 渲染引擎（pandoc 依赖）",
}
_OPTIONAL_BINARIES = {
    "fc-match": "自动检测系统字体",
    "kpsewhich": "查找 TeX 字体文件",
}
_REQUIRED_TEX_PACKAGES = [
    "xcolor.sty",
    "setspace.sty",
    "titlesec.sty",
    "enumitem.sty",
    "fancyhdr.sty",
    "tcolorbox.sty",
]
_MIN_FONTS_EN = ["TeX Gyre Termes", "Latin Modern Roman", "Times New Roman"]
_MIN_FONTS_CJK = ["Noto Serif CJK SC", "SimSun", "Source Han Serif SC"]


def _check_environment():
    """Diagnose the environment. Returns list of fatal errors and list of warnings."""
    errors = []
    warnings = []

    # Python packages
    for pkg in _REQUIRED_PYPI:
        try:
            __import__(pkg)
        except ImportError:
            errors.append(f"Python 包缺失: pip3 install {pkg}")
    for pkg in _OPTIONAL_PYPI:
        try:
            __import__(pkg)
        except ImportError:
            warnings.append(f"建议安装: pip3 install {pkg}（PDF 链接检测）")

    # System binaries
    for cmd, desc in _REQUIRED_BINARIES.items():
        if not shutil.which(cmd):
            errors.append(f"系统命令缺失: {cmd} — {desc}")
    for cmd, desc in _OPTIONAL_BINARIES.items():
        if not shutil.which(cmd):
            warnings.append(f"建议安装: {cmd} — {desc}")

    # LaTeX packages (only if kpsewhich is available)
    if shutil.which("kpsewhich"):
        for sty in _REQUIRED_TEX_PACKAGES:
            result = subprocess.run(["kpsewhich", sty], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            if not result.stdout.strip():
                errors.append(f"LaTeX 宏包缺失: {sty}（texlive-latex-extra）")

    # Fonts (only if fc-match is available)
    if shutil.which("fc-match"):
        en_ok = any(
            subprocess.run(["fc-match", "-s", f], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5).returncode == 0
            for f in _MIN_FONTS_EN
        )
        if not en_ok:
            warnings.append("未检测到英文衬线字体（TeX Gyre Termes / Times New Roman 等）")
        cjk_ok = any(
            subprocess.run(["fc-match", "-s", f], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5).returncode == 0
            for f in _MIN_FONTS_CJK
        )
        if not cjk_ok:
            warnings.append("未检测到中文衬线字体（Noto Serif CJK SC / SimSun 等）")

    # Print diagnostic summary
    if errors or warnings:
        logger.info("=" * 50)
        logger.info("环境诊断")
        for e in errors:
            logger.error(f"  [MISSING] {e}")
        for w in warnings:
            logger.warning(f"  [WARN]    {w}")
        guide = []
        if any("pandoc" in e for e in errors):
            guide.append("Ubuntu: sudo apt install pandoc texlive-xetex texlive-latex-extra texlive-fonts-extra fonts-noto-cjk")
            guide.append("CentOS: sudo dnf install pandoc texlive-xetex texlive-latex-extra texlive-fonts-extra google-noto-serif-cjk-fonts")
            guide.append("macOS:  brew install pandoc basictex && sudo tlmgr install tcolorbox titlesec")
        if any("requests" in e for e in errors):
            guide.append("pip3 install requests pypdf")
        if guide:
            logger.info("  快速修复：")
            for g in guide:
                logger.info(f"    {g}")
        logger.info("=" * 50)

    return errors, warnings


def _first_env(*names, default=""):
    """Return the first non-empty environment variable from names."""
    for name in names:
        value = os.environ.get(name, "")
        if value and value.strip():
            return value.strip()
    return default




def _retry(fn, max_retries=3, base_delay=1.0, backoff=2.0, exceptions=(Exception,)):
    """Retry with exponential backoff. Returns result or raises last exception."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except exceptions as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (backoff ** attempt)
                logger.warning(f"Retry {attempt+1}/{max_retries} after {delay:.1f}s: {e}")
                time.sleep(delay)
    raise last_exc

def resolve_paper_link(href):
    """Resolve a Google Scholar title link to the real paper URL when possible.

    Scholar Alert title links are often Google wrappers such as
    https://scholar.google.com/scholar_url?url=https%3A%2F%2F...
    The Markdown/PDF title should point to the literature page directly, not to
    a separate plain URL line. If there is no wrapped URL, keep the original
    http(s) link as a safe fallback.
    """
    if not href:
        return ""
    href = href.strip().replace("&amp;", "&")
    href = unquote(href)
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://scholar.google.com" + href

    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        for key in ("url", "q"):
            values = qs.get(key) or []
            for value in values:
                value = unquote(value.strip())
                if value.startswith(("http://", "https://")):
                    return value
    except Exception:
        pass

    if href.startswith(("http://", "https://")):
        return href
    return ""


def _is_candidate_title_href(href):
    """Heuristic for Google Scholar Alert result-title links."""
    if not href:
        return False
    h = href.replace("&amp;", "&")
    if "scholar.google" not in h and "google.com/url" not in h:
        return False
    low = h.lower()
    # Exclude management/navigation links from Scholar Alert emails.
    if any(x in low for x in ("alerts?", "citations?", "settings", "unsubscribe", "help")):
        return False
    return ("scholar_url" in low) or ("/scholar?" in low) or ("url=" in low)

# ═══════════════════════════════════════════════════════════════
# 1. EMAIL FETCH
# ═══════════════════════════════════════════════════════════════

def _send_imap_id(mail):
    """Send IMAP ID command required by 163/126/yeah.net servers."""
    try:
        tag = mail._new_tag().decode()
        imap_id = tag + ' ID ("name" "scholar-alert" "version" "1.0" "vendor" "scholar-bot")' + chr(13) + chr(10)
        mail.send(imap_id.encode())
        mail.readline()
    except Exception:
        pass


def _imap_connect(imap_server, email_addr, app_password):
    """Connect to IMAP server with 163/QQ ID workaround. Returns mail object."""
    imaplib._MAXLINE = 10_000_000
    mail = imaplib.IMAP4_SSL(imap_server, timeout=30)
    if "163.com" in imap_server or "126.com" in imap_server or "yeah.net" in imap_server:
        _send_imap_id(mail)
    mail.login(email_addr, app_password)
    if "163.com" in imap_server or "126.com" in imap_server or "yeah.net" in imap_server:
        _send_imap_id(mail)
    return mail


def _fetch_from_folder(mail, label, since_date, max_emails=10, unseen_only=True):
    """Fetch raw emails from one IMAP folder. Returns list of raw bytes.

    With unseen_only=True (default), only unread emails are fetched and they
    get marked as read. This is ideal for cron — each day picks up new alerts
    without re-processing old ones.
    """
    readonly = not unseen_only  # readonly when we want to keep emails unread
    status, count = mail.select('"' + label + '"', readonly=readonly)
    if status != "OK":
        logger.warning(f"IMAP SELECT {label} failed: {count}")
        return []

    # Check folder size before scanning — warn if mailbox is huge
    try:
        _, folder_info = mail.status('"' + label + '"', "(MESSAGES UNSEEN)")
        if folder_info[0]:
            info_str = folder_info[0].decode(errors="replace")
            msg_total = re.search(r"MESSAGES\s+(\d+)", info_str)
            msg_unseen = re.search(r"UNSEEN\s+(\d+)", info_str)
            total = int(msg_total.group(1)) if msg_total else 0
            unseen = int(msg_unseen.group(1)) if msg_unseen else 0
            if unseen_only and unseen > 0:
                logger.info(f"  [{label}] {unseen} 封未读待处理")
            if unseen == 0 and unseen_only:
                logger.info(f"  [{label}] 没有未读邮件，跳过")
                return []
            if total > 2000 or unseen > 500:
                logger.warning(f"  [{label}] 文件夹巨大: {total} 封总计, {unseen} 封未读")
                logger.warning(f"  [{label}] 建议缩小范围: --since-days 3 或 --max-emails 100")
    except Exception:
        pass

    unseen_prefix = "UNSEEN " if unseen_only else ""
    search_strategies = [
        '(' + unseen_prefix + 'FROM "scholaralerts-noreply@google.com" SINCE ' + since_date + ')',
        '(' + unseen_prefix + 'FROM "scopus@notification.elsevier.com" SINCE ' + since_date + ')',
        '(' + unseen_prefix + 'FROM "service@siam.org" SINCE ' + since_date + ')',
        '(' + unseen_prefix + 'FROM "scholaralerts" SINCE ' + since_date + ')',
        '(' + unseen_prefix + 'SUBJECT "scholar" SINCE ' + since_date + ')',
        '(' + unseen_prefix + 'SINCE ' + since_date + ')',
    ]
    msg_ids = [b""]
    for strategy in search_strategies:
        try:
            _, msg_ids = mail.search(None, strategy)
            if msg_ids[0]:
                count = len(msg_ids[0].split())
                logger.info(f"  [{label}] {strategy[:60]}... → {count} 封邮件")
                break
        except imaplib.IMAP4.error as e:
            logger.debug(f"  [{label}] search failed for {strategy}: {e}")
            continue
    if not msg_ids[0]:
        logger.info(f"  [{label}] no emails found")
        return []

    all_ids = msg_ids[0].split()
    if len(all_ids) > max_emails:
        logger.warning(f"  [{label}] 匹配 {len(all_ids)} 封，超过上限 {max_emails}，只取前 {max_emails} 封")
        logger.warning(f"  [{label}] 用 --max-emails 调大上限，或 --since-days 缩小范围")
        all_ids = all_ids[:max_emails]

    raw_emails = []
    for mid in all_ids:
        _, data = mail.fetch(mid, "(RFC822)")
        for part in data:
            if isinstance(part, tuple):
                raw_emails.append(part[1])
    logger.info(f"  [{label}] fetched {len(raw_emails)} emails")
    return raw_emails


def fetch_scholar_alerts(email_addr, app_password, since_days=7, label="INBOX", imap_server=None, max_emails=10, unseen_only=True):
    imap_server = imap_server or os.environ.get("IMAP_SERVER", "imap.163.com")
    mail = _imap_connect(imap_server, email_addr, app_password)
    since_date = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")

    # Support multiple folders: comma-separated in label
    labels = [l.strip() for l in label.split(",") if l.strip()]
    all_raw = []
    seen_ids = set()
    remaining = max_emails
    for lbl in labels:
        if remaining <= 0:
            break
        logger.info(f"Scanning folder: {lbl} (remaining quota: {remaining})")
        raw_list = _fetch_from_folder(mail, lbl, since_date, max_emails=remaining, unseen_only=unseen_only)
        for raw in raw_list:
            if remaining <= 0:
                break
            # Deduplicate by Message-ID header
            try:
                msg = email.message_from_bytes(raw)
                mid = msg.get("Message-ID", "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
            except Exception:
                pass
            all_raw.append(raw)
            remaining -= 1
    mail.logout()
    logger.info(f"Fetched {len(all_raw)} unique emails from {len(labels)} folder(s) (quota was {max_emails})")
    return all_raw

# ═══════════════════════════════════════════════════════════════
# 2. HTML PARSING — robust metadata extraction
# ═══════════════════════════════════════════════════════════════

class ScholarAlertParser(HTMLParser):
    """Parse GS Alert HTML. Extracts 'AUTHORS - JOURNAL, YEAR' from text
    near title links, NOT from #006621 green color styles."""

    def __init__(self):
        super().__init__()
        self.papers = []
        self._current = None
        self._in_title_link = False
        self._in_snippet = False
        self._snippet_depth = 0
        self._text_after_title = ""
        self._collecting_metadata = False
        self._alert_name = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href", "")
        if tag == "a" and _is_candidate_title_href(href):
            if self._current and self._current.get("title"):
                self._finalize_paper()
            self._current = {
                "title": "", "link": resolve_paper_link(href), "snippet": "",
                "raw_metadata": "", "authors": "", "authors_source": "",
                "journal": "", "journal_source": "", "year": "", "year_source": "",
                "link_source": "Google Scholar Alert title link",
                "abstract_source": "", "scholar_snippet": "",
            }
            self._in_title_link = True
            self._text_after_title = ""
            self._collecting_metadata = True
            return
        if self._current and self._collecting_metadata and tag == "div":
            if self._text_after_title and self._text_after_title.strip():
                self._collecting_metadata = False
                self._in_snippet = True
                self._snippet_depth = 1
                return
        if self._in_snippet and tag == "div":
            self._snippet_depth += 1

    def handle_endtag(self, tag):
        if self._in_snippet and tag == "div":
            self._snippet_depth -= 1
            if self._snippet_depth <= 0:
                self._in_snippet = False
        if tag == "a" and self._in_title_link:
            self._in_title_link = False

    def handle_data(self, data):
        text = data.strip()
        if self._in_title_link and text and self._current is not None:
            self._current["title"] += " " + text
            return
        if self._current and self._collecting_metadata and data.strip():
            self._text_after_title += " " + data
        if self._in_snippet and text and self._current is not None:
            self._current["snippet"] += " " + text

    def _finalize_paper(self):
        if not self._current:
            return
        title = self._current["title"].strip()
        # Clean trailing ": Author Name" or ": A Study" patterns from GS titles
        title = re.sub(r':\s+[A-Z][A-Za-z\.\s\-]+(?:et al\.?)?\s*$', '', title).strip()
        self._current["title"] = title
        self._current["snippet"] = self._current["snippet"].strip()
        raw = self._text_after_title.strip()
        if raw:
            self._current["raw_metadata"] = raw
            parsed = parse_google_scholar_metadata(raw)
            if parsed.get("authors"):
                self._current["authors"] = parsed["authors"]
                self._current["authors_source"] = "Google Scholar Alert metadata"
            if parsed.get("journal"):
                self._current["journal"] = parsed["journal"]
                self._current["journal_source"] = "Google Scholar Alert metadata"
            if parsed.get("year"):
                self._current["year"] = parsed["year"]
                self._current["year_source"] = "Google Scholar Alert metadata"
        self.papers.append(self._current)
        self._current = None


def parse_google_scholar_metadata(raw_text):
    """Parse 'W Lv - AIMS Mathematics, 2026' → {authors, journal, year}."""
    result = {"authors": "", "journal": "", "year": ""}
    if not raw_text:
        return result
    # Normalize non-breaking spaces (\xa0 from HTML &nbsp;) to regular spaces
    raw_text = raw_text.replace('\xa0', ' ')
    year_match = re.search(r'\b(20[2-3]\d)\b', raw_text)
    if year_match:
        result["year"] = year_match.group(1)
    segments = [s.strip() for s in raw_text.split(" - ")]
    if len(segments) >= 2:
        if _looks_like_authors(segments[0]):
            result["authors"] = segments[0]
        for seg in segments[1:]:
            if result["year"] and result["year"] in seg:
                jp = seg.split(",")[0].strip() if "," in seg else seg.replace(result["year"], "").strip().rstrip(",").strip()
                if jp and not _looks_like_authors(jp):
                    result["journal"] = jp
                    break
            elif seg and not _looks_like_authors(seg) and not result["journal"]:
                result["journal"] = seg.split(",")[0].strip() if "," in seg else seg
    if not result["authors"] and not result["journal"]:
        parts = [p.strip() for p in raw_text.split(",")]
        if len(parts) >= 2 and _looks_like_authors(parts[0]):
            result["authors"] = parts[0]
            result["journal"] = parts[1].strip()
    return result


def _looks_like_authors(text):
    text = text.strip()
    if not text: return False
    if "et al" in text: return True
    # Filter out non-name single chars: ellipsis (…), ampersand (&), punctuation, digits
    tokens = [t for t in re.split(r'[,\s]+', text) if t]
    if not tokens: return False
    # Only count ASCII single letters as author initials (not …, &, etc.)
    initial_count = sum(1 for t in tokens if len(t) == 1 and t.isascii() and t.isalpha())
    if initial_count >= 1: return True
    jw = {"journal","proceedings","review","letters","nature","science","mathematics",
          "biology","physics","chemistry","engineering","medicine","applied",
          "computational","international","annals","transactions",
          "chaos","nonlinearity","vaccine","ecology","epidemiology","dynamics",
          "siam","bulletin","fractal","heliyon","entropy","symmetry","axioms",
          "analysis","computation","annalen","physik","reports","research",
          "cell","lancet","pnas","frontiers","discrete","continuous",
          "stochastic","numerical","mathematical","differential","biological",
          "biomathematics","nonlinear","communications",
          # Additional journal keywords to reduce false positives
          "acta","methods","scripta","ecosystem","forests","dimensions",
          "human","health","sustainability","cauchy","jurnal","murni",
          "aplikasi","physica","ices","marine","fisheries","american",
          "north","south","east","asian","conference","preprint","arxiv",
          "springer","elsevier","wiley","taylor","francis","academic",
          "quarterly","monthly","annual","advances","studies","survey",
          "modeling","modelling","equations","systems","networks","theory",
          "aerosol","biomedical","bioinformatics","genomics","proteomics",
          "forestry","wildlife","aquatic","veterinary","parasitology",
          "epidemics","infections","infectious","tropical","clinical"}
    if any(w in text.lower() for w in jw): return False
    # Require at least one name-like pattern: "A Smith" (single initial + surname)
    # But NOT patterns like "Acta Mathematica" (multi-char word + multi-char word)
    # A name pattern requires a short token (1-2 chars) followed by a capitalized word
    if re.search(r'\b[A-Z]{1,2}\b\s+[A-Z][a-z]', text):
        return True
    return False


def parse_emails(raw_emails):
    all_papers, seen = [], set()

    def _dedup_key(title):
        """Normalize title for dedup: lowercase, strip punctuation, collapse whitespace."""
        return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', title.lower().strip()))

    for raw in raw_emails:
        msg = email.message_from_bytes(raw)
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    html_body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                html_body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if not html_body:
            continue
        parser = ScholarAlertParser()
        parser.feed(html_body)
        if parser._current and parser._current.get("title"):
            parser._finalize_paper()
        for p in parser.papers:
            tc = p["title"].strip()
            key = _dedup_key(tc)
            if tc and key and key not in seen:
                seen.add(key)
                p["title"] = tc
                p.setdefault("title_en", tc)
                p["link"] = resolve_paper_link(p.get("link", ""))
                p.setdefault("link_source", "Google Scholar Alert title link")
                snippet = p.get("snippet", "").strip()
                p["scholar_snippet"] = p.get("scholar_snippet") or snippet
                if not p.get("abstract_en") and snippet:
                    p["abstract_en"] = snippet
                    p["abstract_source"] = "Google Scholar Alert snippet"
                elif p.get("abstract_en") and not p.get("abstract_source"):
                    p["abstract_source"] = "Google Scholar Alert snippet"
                if not p.get("source"):
                    p["source"] = msg.get("Subject", "Google Scholar Alert")
                all_papers.append(p)
    logger.info(f"Parsed {len(all_papers)} unique papers")
    return all_papers

# ═══════════════════════════════════════════════════════════════
# 3. ABSTRACT ENRICHMENT — with title matching
# ═══════════════════════════════════════════════════════════════

def _title_similarity(t1, t2):
    if not t1 or not t2: return 0.0
    def norm(t): return set(re.sub(r'[^\w\s]','',t.lower().strip()).split())
    w1, w2 = norm(t1), norm(t2)
    if not w1 or not w2: return 0.0
    return len(w1 & w2) / len(w1 | w2)

DOI_PATTERN = re.compile(r'(?:doi\.org/|DOI[:\s]*|doi[:\s]*)(10\.\d{4,}/[^\s<>"\'\[\]]+)', re.IGNORECASE)

# --- API cache (simple JSON file, keyed by DOI or normalized title) ---

_api_cache = {}
_cache_dirty = False
_cache_path = None


def _init_cache():
    global _api_cache, _cache_path
    if _cache_path is not None:
        return
    cache_dir = os.environ.get("CACHE_DIR", "")
    if not cache_dir:
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    _cache_path = os.path.join(cache_dir, "api_cache.json")
    if os.path.isfile(_cache_path):
        try:
            with open(_cache_path, encoding="utf-8") as f:
                _api_cache = json.load(f)
        except Exception:
            _api_cache = {}
    now = time.time()
    _api_cache = {k: v for k, v in _api_cache.items() if now - v.get("ts", 0) < 30 * 86400}


def _save_cache():
    global _cache_dirty
    if not _cache_dirty or not _cache_path:
        return
    try:
        with open(_cache_path, "w", encoding="utf-8") as f:
            json.dump(_api_cache, f, ensure_ascii=False, indent=2)
        _cache_dirty = False
    except Exception as e:
        logger.warning(f"Failed to save API cache: {e}")


def _cached(key, fetcher, ttl=7 * 86400):
    """Get from cache or call fetcher and cache. Returns dict or None."""
    _init_cache()
    now = time.time()
    entry = _api_cache.get(key)
    if entry and (now - entry.get("ts", 0)) < ttl:
        logger.debug(f"  Cache hit: {key[:60]}")
        return entry.get("data")
    data = fetcher()
    if data:
        global _cache_dirty
        _api_cache[key] = {"ts": now, "data": data}
        _cache_dirty = True
    return data


def _extract_doi(paper):
    """Extract DOI from paper link, raw_metadata, snippet, or scholar_snippet."""
    candidates = [
        paper.get("link", ""),
        paper.get("raw_metadata", ""),
        paper.get("snippet", ""),
        paper.get("scholar_snippet", ""),
    ]
    for text in candidates:
        if not text:
            continue
        m = DOI_PATTERN.search(str(text))
        if m:
            return m.group(1).rstrip(".")
    return None


def _fetch_crossref_by_doi(doi):
    """Look up paper metadata via Crossref DOI. Returns dict or None."""
    if not HAS_REQUESTS:
        return None

    def _do():
        try:
            r = requests.get(
                f"https://api.crossref.org/works/{doi}",
                timeout=15,
                headers={"User-Agent": "ScholarAlertBot/1.0 (mailto:research@example.com)"},
            )
            if r.status_code != 200:
                return None
            item = r.json().get("message", {})
            if not item:
                return None
            result = {}
            ab = item.get("abstract")
            if ab:
                result["abstract_en"] = re.sub(r"<[^>]+>", "", ab)
            authors = item.get("author", [])
            if authors:
                result["authors"] = ", ".join(
                    f"{a.get('given', '')} {a.get('family', '')}".strip()
                    for a in authors[:10]
                )
            container = item.get("container-title", [])
            if container:
                result["journal"] = container[0]
            pub_date = item.get("published-print") or item.get("published-online") or {}
            date_parts = (pub_date.get("date-parts") or [[None]])[0]
            if date_parts and date_parts[0]:
                result["year"] = str(date_parts[0])
            return result if result else None
        except Exception as e:
            logger.warning(f"  Crossref DOI lookup failed: {e}")
        return None

    return _cached(f"cr_doi:{doi}", _do)


def _fetch_openalex_by_doi(doi):
    """Look up paper metadata via OpenAlex DOI. Returns dict or None."""
    if not HAS_REQUESTS:
        return None

    def _do():
        try:
            r = requests.get(f"https://api.openalex.org/works/doi:{doi}", timeout=20)
            if r.status_code != 200:
                return None
            item = r.json()
            result = {}
            aii = item.get("abstract_inverted_index")
            if aii:
                wp = []
                for word, positions in aii.items():
                    for pos in positions:
                        wp.append((pos, word))
                wp.sort()
                result["abstract_en"] = " ".join(w for _, w in wp)
            authorship = item.get("authorships", [])
            if authorship:
                result["authors"] = ", ".join(
                    a.get("author", {}).get("display_name", "") for a in authorship[:10]
                )
            loc = item.get("primary_location", {}) or {}
            src = loc.get("source", {}) or {}
            if src.get("display_name"):
                result["journal"] = src["display_name"]
            pub_date = item.get("publication_date", "")
            if pub_date:
                result["year"] = pub_date[:4]
            return result if result else None
        except Exception as e:
            logger.warning(f"  OpenAlex DOI lookup failed: {e}")
        return None

    return _cached(f"oa_doi:{doi}", _do)


def _apply_external_metadata(paper, meta, source):
    """Apply externally-fetched metadata to paper, NEVER overwriting GS data."""
    for field in ("abstract_en", "authors", "journal", "year"):
        if meta.get(field) and not paper.get(field):
            paper[field] = meta[field]
            paper[f"{field}_source" if field != "abstract_en" else "abstract_source"] = source


def _fetch_semantic_scholar(title):
    if not HAS_REQUESTS:
        return None
    key = f"s2_title:{title.lower().strip()[:120]}"

    def _do():
        try:
            r = requests.get("https://api.semanticscholar.org/graph/v1/paper/search",
                             params={"query": title, "limit": 3, "fields": "title,abstract,year,venue"}, timeout=20)
            if r.status_code != 200: return None
            for item in r.json().get("data", []):
                rt = item.get("title", "")
                if _title_similarity(title, rt) >= TITLE_MATCH_THRESHOLD and item.get("abstract"):
                    logger.info(f"  SemanticScholar match (sim={_title_similarity(title,rt):.2f}): {rt[:60]}")
                    return item["abstract"]
            logger.info(f"  SemanticScholar: no match for '{title[:50]}'")
        except Exception as e:
            logger.warning(f"  SemanticScholar failed: {e}")
        return None

    return _cached(key, _do, ttl=14 * 86400)


def _fetch_crossref(title):
    if not HAS_REQUESTS:
        return None
    key = f"cr_title:{title.lower().strip()[:120]}"

    def _do():
        try:
            r = requests.get("https://api.crossref.org/works",
                             params={"query.title": title, "rows": 3}, timeout=15,
                             headers={"User-Agent": "ScholarAlertBot/1.0 (mailto:research@example.com)"})
            if r.status_code != 200: return None
            for item in r.json().get("message",{}).get("items",[]):
                titles = item.get("title", [])
                if titles:
                    rt = titles[0]
                    if _title_similarity(title, rt) >= TITLE_MATCH_THRESHOLD:
                        ab = item.get("abstract")
                        if ab:
                            ab = re.sub(r'<[^>]+>', '', ab)
                            logger.info(f"  Crossref match (sim={_title_similarity(title,rt):.2f}): {rt[:60]}")
                            return ab
            logger.info(f"  Crossref: no match for '{title[:50]}'")
        except Exception as e:
            logger.warning(f"  Crossref failed: {e}")
        return None

    return _cached(key, _do, ttl=14 * 86400)

def _fetch_openalex(title):
    if not HAS_REQUESTS:
        return None
    key = f"oa_title:{title.lower().strip()[:120]}"

    def _do():
        try:
            r = requests.get("https://api.openalex.org/works",
                             params={"search": title, "per_page": 3}, timeout=20)
            if r.status_code != 200: return None
            for item in r.json().get("results", []):
                rt = item.get("display_name", "")
                if _title_similarity(title, rt) >= TITLE_MATCH_THRESHOLD:
                    aii = item.get("abstract_inverted_index")
                    if aii:
                        wp = []
                        for word, positions in aii.items():
                            for pos in positions: wp.append((pos, word))
                        wp.sort()
                        ab = " ".join(w for _, w in wp)
                        logger.info(f"  OpenAlex match (sim={_title_similarity(title,rt):.2f}): {rt[:60]}")
                        return ab
            logger.info(f"  OpenAlex: no match for '{title[:50]}'")
        except Exception as e:
            logger.warning(f"  OpenAlex failed: {e}")
        return None

    return _cached(key, _do, ttl=14 * 86400)


def _enrich_one_paper(i, paper, total, use_semantic, use_crossref, use_openalex):
    """Enrich a single paper's metadata (thread-safe). DOI-first, then title search."""
    title = paper.get("title_en", paper.get("title", ""))
    if not title:
        return

    needs_abstract = not paper.get("abstract_en") or len(paper.get("abstract_en", "")) < 100
    needs_meta = not paper.get("authors") or not paper.get("journal") or not paper.get("year")

    if not needs_abstract and not needs_meta:
        logger.info(f"[{i+1}] Already complete, skip")
        return

    logger.info(f"[{i+1}/{total}] Enriching: {title[:60]}...")

    # --- DOI path: exact match, no title similarity needed ---
    doi = _extract_doi(paper)
    if doi:
        logger.info(f"  DOI found: {doi}")
        if use_crossref:
            meta = _fetch_crossref_by_doi(doi)
            if meta:
                _apply_external_metadata(paper, meta, "Crossref (DOI)")
                logger.info(f"  OK [{i+1}] Crossref DOI enriched")
        if use_openalex:
            meta = _fetch_openalex_by_doi(doi)
            if meta:
                _apply_external_metadata(paper, meta, "OpenAlex (DOI)")
                logger.info(f"  OK [{i+1}] OpenAlex DOI enriched")

    # --- Title path: fallback for papers without DOI or with no DOI results ---
    still_need_abstract = not paper.get("abstract_en") or len(paper.get("abstract_en", "")) < 100
    if still_need_abstract:
        abstract = None
        if use_semantic and not abstract:
            abstract = _fetch_crossref(title)
        if use_openalex and not abstract:
            abstract = _fetch_openalex(title)
        if use_semantic and not abstract:
            abstract = _fetch_semantic_scholar(title)
        if abstract:
            paper["abstract_en"] = abstract
            paper["abstract_source"] = "External (title match confirmed)"
            logger.info(f"  OK [{i+1}] Abstract enriched ({len(abstract)} chars) via title search")
        else:
            logger.info(f"  - [{i+1}] No matching abstract found")
            if paper.get("snippet") and not paper.get("abstract_en"):
                paper["abstract_en"] = paper["snippet"]

    # Fill remaining gaps with placeholder
    if not paper.get("authors"):
        paper["authors"] = "\u672a\u6838\u5b9e"
        paper["authors_source"] = "external lookup (no match)"
    if not paper.get("journal"):
        paper["journal"] = "\u672a\u6838\u5b9e"
        paper["journal_source"] = "external lookup (no match)"
    if not paper.get("year"):
        paper["year"] = "\u672a\u6838\u5b9e"
        paper["year_source"] = "external lookup (no match)"


def enrich_abstracts(papers, use_semantic=True, use_crossref=True, use_openalex=True):
    """Enrich abstracts in parallel. GS metadata authors/journal/year are NEVER overwritten."""
    papers_needing = [(i, p) for i, p in enumerate(papers)
                      if p.get("title_en", p.get("title", "")) and
                      (not p.get("abstract_en") or len(p.get("abstract_en", "")) < 100)]
    if not papers_needing:
        logger.info("All papers have sufficient abstracts, skipping enrichment")
        return papers
    max_workers = min(4, len(papers_needing))
    total = len(papers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, paper in papers_needing:
            futures.append(executor.submit(
                _enrich_one_paper, i, paper, total, use_semantic, use_crossref, use_openalex
            ))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.warning(f"Enrichment error (non-fatal): {e}")
    return papers

class TranslationError(RuntimeError):
    pass




def _get_translation_api():
    """Resolve translation API config from env."""
    if not HAS_REQUESTS:
        raise TranslationError("requests is not available")
    provider = os.environ.get("TRANSLATE_PROVIDER", "openai").lower().strip()
    if provider == "ark":
        api_key = _first_env("ARK_API_KEY", "TRANSLATE_API_KEY", "LLM_API_KEY")
        api_base = _first_env("TRANSLATE_BASE_URL", "LLM_API_BASE", default="https://ark.cn-beijing.volces.com/api/v3")
        model = _first_env("TRANSLATE_MODEL", "OPENAI_MODEL", "LLM_MODEL")
    else:
        api_key = _first_env("OPENAI_API_KEY", "LLM_API_KEY", "TRANSLATE_API_KEY")
        api_base = _first_env("OPENAI_BASE_URL", "LLM_API_BASE", "TRANSLATE_BASE_URL", default="https://api.openai.com/v1")
        model = _first_env("OPENAI_MODEL", "LLM_MODEL", "TRANSLATE_MODEL", default="gpt-4o-mini")
    if not api_key:
        raise TranslationError("No API key. Set OPENAI_API_KEY in .env")
    if not model:
        raise TranslationError("No model. Set OPENAI_MODEL in .env")
    return api_key, api_base.rstrip("/"), model


TITLE_PROMPT = (
    "You are an academic translator. Translate the paper title below into concise, "
    "natural Simplified Chinese suitable for a literature review or table of contents. "
    "Keep mathematical notation, Latin species names, and model names unchanged. "
    "Output ONLY the Chinese title, no explanations or labels."
)

ABSTRACT_PROMPT = (
    "You are a professional academic translator. Translate the text below into natural, "
    "precise Simplified Chinese. Keep mathematical notation, Latin species names, model "
    "names, and LaTeX symbols unchanged when appropriate. Output ONLY the translation; "
    "do not add explanations or extra labels."
)


def translate_batch(texts, text_type="abstract"):
    """Translate a batch of texts in one API call. Returns list of translations."""
    if not texts:
        return []
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return []

    api_key, api_base, model = _get_translation_api()
    prompt = TITLE_PROMPT if text_type == "title" else ABSTRACT_PROMPT

    payload_lines = []
    for i, t in enumerate(texts, 1):
        truncated = t[:8000] if text_type == "abstract" else t[:1000]
        payload_lines.append(f"[{i}] {truncated}")
    payload = "\n\n".join(payload_lines)

    user_msg = (
        f"Translate each of the following {text_type}s into Simplified Chinese.\n"
        f"Return the translations as a numbered list in the SAME order, like:\n"
        f"1. translation1\n"
        f"2. translation2\n"
        f"...\n"
        f"Do NOT include the original text, numbers from the input, or labels "
        f"-- only the translations, one per line with a number prefix.\n\n{payload}"
    )

    def _do():
        resp = requests.post(
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.2,
            },
            timeout=(10, 120),
        )
        if resp.status_code == 429:
            raise TranslationError("Rate limited (429)")
        if resp.status_code >= 500:
            raise TranslationError(f"Server error ({resp.status_code})")
        if resp.status_code in (400, 401, 403):
            raise RuntimeError(f"API error {resp.status_code}: {resp.text[:300]}")
        if resp.status_code != 200:
            raise TranslationError(f"API error {resp.status_code}")

        try:
            data = resp.json()
            raw = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as e:
            raise TranslationError(f"Response parse failed: {e}")

        # Try JSON parse first (backward compatibility)
        results = []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                results = [str(it).strip() for it in parsed]
            elif isinstance(parsed, dict):
                for k in ("translations", "results", "items"):
                    if k in parsed and isinstance(parsed[k], list):
                        results = [str(it).strip() for it in parsed[k]]
                        break
                else:
                    results = [str(parsed.get(str(i), parsed.get(str(i+1), ""))).strip()
                              for i in range(len(texts))]
            else:
                raise json.JSONDecodeError("Not list/dict", raw, 0)
        except (json.JSONDecodeError, UnboundLocalError):
            pass

        # If JSON parse failed or yielded wrong count, try line-based parsing
        if not results or len(results) != len(texts):
            logger.info("  Using line-based parsing for numbered list output")
            results = []
            # Match lines like: "1. translation", "1、translation", "1: translation", "[1] translation"
            pattern = re.compile(r'^(?:\[?(\d+)\]?[\.\s、：:]+\s*|\d+\.\s+)')
            current_lines = []
            current_idx = None

            for line in raw.split('\n'):
                line = line.strip()
                if not line:
                    continue
                m = pattern.match(line)
                if m:
                    # Save previous block
                    if current_lines and current_idx is not None:
                        while len(results) < current_idx - 1:
                            results.append("")
                        results.append(' '.join(current_lines).strip())
                    current_lines = [pattern.sub('', line)]
                    try:
                        current_idx = int(m.group(1))
                    except (ValueError, IndexError):
                        current_idx = len(results) + 1
                else:
                    current_lines.append(line)

            # Save last block
            if current_lines and current_idx is not None:
                while len(results) < current_idx - 1:
                    results.append("")
                results.append(' '.join(current_lines).strip())

        while len(results) < len(texts):
            results.append("")
        return results[:len(texts)]

    try:
        return _retry(_do, max_retries=2, base_delay=3.0,
                       exceptions=(TranslationError, requests.exceptions.ConnectionError,
                                   requests.exceptions.Timeout))
    except TranslationError:
        raise
    except Exception as e:
        raise TranslationError(f"Batch translation failed: {e}") from e


def _needs_translation(value):
    value = (value or "").strip()
    if not value:
        return True
    if value.startswith(TRANSLATION_FAILURE_PREFIXES):
        return True
    return not re.search(r'[一-鿿]', value)


def translate_papers(papers):
    """Batch-translate all titles then all abstracts in 2 API calls.

    10 papers: 2 API calls instead of 20. Failures skip individually.
    """
    if not papers:
        return papers

    logger.info(f"Translating {len(papers)} papers (batch mode)")

    # -- Batch 1: Titles --
    idx_title = []
    txt_title = []
    for i, p in enumerate(papers):
        t = p.get("title_en", p.get("title", ""))
        if t and _needs_translation(p.get("title_cn")):
            idx_title.append(i)
            txt_title.append(t)

    if txt_title:
        logger.info(f"  Batch-translating {len(txt_title)} titles...")
        try:
            results = translate_batch(txt_title, text_type="title")
            for idx, r in zip(idx_title, results):
                if r and re.search(r'[一-鿿]', r):
                    papers[idx]["title_cn"] = r
                else:
                    papers[idx]["title_cn"] = papers[idx].get("title_en", "")
        except TranslationError as e:
            logger.error(f"  Title batch failed: {e}")
            for idx in idx_title:
                papers[idx]["title_cn"] = papers[idx].get("title_en", "")

    # -- Batch 2: Abstracts --
    idx_abs = []
    txt_abs = []
    for i, p in enumerate(papers):
        a = p.get("abstract_en", "")
        if a and _needs_translation(p.get("abstract_cn")):
            idx_abs.append(i)
            txt_abs.append(a)

    if txt_abs:
        logger.info(f"  Batch-translating {len(txt_abs)} abstracts...")
        try:
            results = translate_batch(txt_abs, text_type="abstract")
            for idx, r in zip(idx_abs, results):
                if r and re.search(r'[一-鿿]', r):
                    src = (papers[idx].get("abstract_source") or "").lower()
                    if "scholar alert snippet" in src and "[Google Scholar" not in r:
                        r = "[根据 Google Scholar Alert 摘要片段翻译]\n" + r
                    papers[idx]["abstract_cn"] = r
                else:
                    papers[idx]["abstract_cn"] = (
                        "[翻译失败] " + (papers[idx].get("abstract_en", "")[:200] or "")
                    )
        except TranslationError as e:
            logger.error(f"  Abstract batch failed: {e}")
            for idx in idx_abs:
                papers[idx]["abstract_cn"] = (
                    "[翻译失败] " + (papers[idx].get("abstract_en", "")[:200] or "")
                )

    ok_t = sum(1 for p in papers if p.get("title_cn") and re.search(r'[一-鿿]', p["title_cn"]))
    ok_a = sum(1 for p in papers if p.get("abstract_cn") and re.search(r'[一-鿿]', p["abstract_cn"]))
    logger.info(f"  Done: {ok_t}/{len(papers)} titles, {ok_a}/{len(papers)} abstracts")

    return papers

# ═══════════════════════════════════════════════════════════════
# 5. VALIDATION
# ═══════════════════════════════════════════════════════════════

def _has_bad_glyphs(text):
    return any(ch in (text or "") for ch in BAD_GLYPHS)


def validate_papers(papers):
    """Validate papers before generating Markdown/PDF.

    Critical errors stop the pipeline, because the user wants a complete fixed
    bilingual template rather than a half-translated PDF. Non-critical metadata
    gaps are logged as warnings.
    """
    errors = []
    warnings = []
    for i, p in enumerate(papers):
        idx = i + 1
        title_en = (p.get("title_en") or p.get("title") or "").strip()
        title_cn = (p.get("title_cn") or "").strip()
        abstract_en = (p.get("abstract_en") or "").strip()
        abstract_cn = (p.get("abstract_cn") or "").strip()
        link = (p.get("link") or "").strip()

        if not title_en or re.search(r'No\s+Title', title_en, re.IGNORECASE):
            errors.append(f"[{idx}] Missing or invalid English title")
        if not link.startswith(("http://", "https://")):
            errors.append(f"[{idx}] Missing valid title hyperlink: {title_en[:60]}")
        if not title_cn or not re.search(r'[\u4e00-\u9fff]', title_cn):
            errors.append(f"[{idx}] Missing Chinese title: {title_en[:60]}")
        if not abstract_en or len(abstract_en) < 30:
            errors.append(f"[{idx}] Short/missing English abstract: {title_en[:60]}")
        if not abstract_cn or not re.search(r'[\u4e00-\u9fff]', abstract_cn):
            errors.append(f"[{idx}] Missing Chinese abstract: {title_en[:60]}")
        if abstract_cn.startswith(TRANSLATION_FAILURE_PREFIXES) or title_cn.startswith(TRANSLATION_FAILURE_PREFIXES):
            errors.append(f"[{idx}] Translation failed: {title_en[:60]}")
        for field in ("title_en", "title_cn", "abstract_en", "abstract_cn"):
            if _has_bad_glyphs(str(p.get(field, ""))):
                errors.append(f"[{idx}] Broken glyphs in {field}: {title_en[:60]}")

        # Metadata should be present in the fixed template; lack of verification
        # is not fatal because LLM must not invent it.
        raw = p.get("raw_metadata", "")
        auth = (p.get("authors") or "").strip()
        journal = (p.get("journal") or "").strip()
        if raw and auth in ("", "未核实"):
            warnings.append(f"[{idx}] raw_metadata has data but authors not extracted: {raw[:80]}")
        if not auth:
            warnings.append(f"[{idx}] Missing authors; template will show 未核实")
        if not journal:
            warnings.append(f"[{idx}] Missing journal; template will show 未核实")

    for e in warnings:
        logger.warning(f"VALIDATION WARNING: {e}")
    for e in errors:
        logger.error(f"VALIDATION ERROR: {e}")
    if errors:
        raise ValueError(f"Validation failed with {len(errors)} critical issue(s). Refusing to generate incomplete Markdown/PDF.")
    logger.info("Validation passed — required bilingual fields, title links, and abstracts are complete")
    return warnings


# ═══════════════════════════════════════════════════════════════
# 5.5  LATEX MATH NORMALIZATION
# ═══════════════════════════════════════════════════════════════

# LaTeX command → plain-text fallback
_LATEX_PLAIN_MAP = {
    # Relations
    r"\geq": ">=", r"\ge": ">=",
    r"\leq": "<=", r"\le": "<=",
    r"\neq": "≠", r"\ne": "≠",
    r"\approx": "≈",
    r"\sim": "~",
    r"\equiv": "≡",
    r"\propto": "∝",
    r"\simeq": "≈",
    r"\cong": "≅",
    r"\doteq": "≐",
    # Arrows
    r"\to": "→", r"\rightarrow": "→", r"\Rightarrow": "⇒",
    r"\leftarrow": "←", r"\Leftarrow": "⇐",
    r"\leftrightarrow": "↔", r"\Leftrightarrow": "⇔",
    r"\mapsto": "↦",
    # Operators
    r"\times": "×",
    r"\cdot": "·",
    r"\pm": "±", r"\mp": "∓",
    r"\div": "÷",
    r"\ast": "*",
    r"\star": "★",
    r"\circ": "°",
    r"\bullet": "•",
    r"\oplus": "⊕", r"\ominus": "⊖", r"\otimes": "⊗",
    r"\odot": "⊙", r"\oslash": "⊘",
    # Set / logic
    r"\forall": "∀", r"\exists": "∃",
    r"\in": "∈", r"\notin": "∉", r"\ni": "∋",
    r"\subset": "⊂", r"\subseteq": "⊆",
    r"\supset": "⊃", r"\supseteq": "⊇",
    r"\cup": "∪", r"\cap": "∩",
    r"\emptyset": "∅", r"\varnothing": "∅",
    r"\setminus": "\\",
    r"\land": "∧", r"\lor": "∨", r"\lnot": "¬",
    # Analysis
    r"\infty": "∞",
    r"\partial": "∂", r"\nabla": "∇",
    r"\int": "∫", r"\iint": "∫∫", r"\iiint": "∫∫∫",
    r"\oint": "∮",
    r"\sum": "Σ", r"\prod": "Π",
    r"\lim": "lim", r"\max": "max", r"\min": "min",
    r"\sup": "sup", r"\inf": "inf",
    r"\log": "log", r"\ln": "ln", r"\lg": "lg",
    r"\sin": "sin", r"\cos": "cos", r"\tan": "tan",
    r"\exp": "exp",
    # Greek (already present, extended)
    r"\theta": "θ", r"\eta": "η", r"\alpha": "α",
    r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε",
    r"\lambda": "λ", r"\mu": "μ",
    r"\sigma": "σ", r"\omega": "ω", r"\rho": "ρ",
    r"\nu": "ν", r"\phi": "φ", r"\varphi": "φ", r"\pi": "π",
    r"\tau": "τ", r"\chi": "χ", r"\psi": "ψ",
    r"\xi": "ξ", r"\zeta": "ζ", r"\kappa": "κ",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ",
    r"\Lambda": "Λ", r"\Xi": "Ξ", r"\Pi": "Π",
    r"\Sigma": "Σ", r"\Phi": "Φ", r"\Psi": "Ψ", r"\Omega": "Ω",
    # Misc symbols
    r"\ell": "l",
    r"\hbar": "h",
    r"\Im": "Im", r"\Re": "Re",
    r"\aleph": "ℵ",
    r"\angle": "∠", r"\triangle": "△",
    r"\square": "□", r"\Box": "□",
    r"\nabla": "∇",
    r"\parallel": "∥", r"\perp": "⊥",
    r"\dots": "...", r"\ldots": "...", r"\cdots": "...",
    r"\vdots": "⋮", r"\ddots": "⋱",
    # Styling commands — strip, keep content
    r"\mathcal{": "", r"\mathbb{": "", r"\mathrm{": "",
    r"\mathbf{": "", r"\mathit{": "", r"\mathsf{": "",
    r"\mathtt{": "", r"\text{": "", r"\textbf{": "",
    r"\textit{": "", r"\textsf{": "", r"\texttt{": "",
    r"\emph{": "",
    # Accents — normalize to plain prefix
    r"\hat{": "_hat{", r"\bar{": "_bar{", r"\tilde{": "_tilde{",
    r"\dot{": "_dot{", r"\ddot{": "_ddot{",
    r"\vec{": "_vec{", r"\widehat{": "_widehat{",
    r"\widetilde{": "_widetilde{",
    # Sizing (remove — they don't carry meaning in plain text)
    r"\left": "", r"\right": "",
    r"\bigl": "", r"\bigr": "", r"\Bigl": "", r"\Bigr": "",
    r"\big": "", r"\Big": "", r"\bigg": "", r"\Bigg": "",
}

# Patterns for structural LaTeX that need regex rewriting
_LATEX_STRUCTURAL = [
    (re.compile(r"\\frac\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"), r"(\1)/(\2)"),
    (re.compile(r"\\sqrt\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"), r"√(\1)"),
    (re.compile(r"\\sqrt\[([^\]]+)\]\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"), r"\1√(\2)"),
]

# HTML entity → character
_HTML_ENTITY_MAP = {
    "&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"',
    "&nbsp;": " ", "&apos;": "'",
}


def _unescape_html(text):
    """Replace common HTML entities."""
    for entity, char in _HTML_ENTITY_MAP.items():
        text = text.replace(entity, char)
    # Also handle numeric entities like &#60;
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    return text


def _is_balanced_dollar(text):
    """Check if $ delimiters are balanced (even count of unescaped $)."""
    # Count $ not preceded by \
    count = 0
    i = 0
    while i < len(text):
        if text[i] == '$' and (i == 0 or text[i - 1] != '\\'):
            count += 1
        i += 1
    return count % 2 == 0


def _has_broken_latex_commands(text):
    """Detect obvious broken LaTeX: dangling backslash commands, unclosed braces."""
    # Dangling backslash at end of string
    if text.rstrip().endswith('\\'):
        return True
    # Backslash followed by non-alpha and not a valid special (\, \$ etc.)
    for m in re.finditer(r'\\([^a-zA-Z{}\\$|_^])', text):
        return True
    # Unclosed braces: count { vs }
    open_b = text.count('{')
    close_b = text.count('}')
    if abs(open_b - close_b) > 1:
        return True
    return False


def _has_latex_commands(text):
    """Detect raw LaTeX commands that would break when left outside math mode."""
    return bool(re.search(r'\\[a-zA-Z]+', text or ""))


def _degrade_to_plain(text):
    """Convert a broken LaTeX fragment to readable plain text."""
    # Step 0: Rewrite structural commands (\frac, \sqrt) with regex
    for pattern, replacement in _LATEX_STRUCTURAL:
        text = pattern.sub(replacement, text)
    # Step 1: Apply LaTeX → plain replacements (longest keys first)
    for latex, plain in sorted(_LATEX_PLAIN_MAP.items(), key=lambda x: -len(x[0])):
        text = text.replace(latex, plain)
    # Step 2: Remove remaining backslash commands (\word → word)
    text = re.sub(r'\\([a-zA-Z]+)', r'\1', text)
    # Step 3: Remove stray backslashes
    text = text.replace('\\', '')
    # Step 4: Clean up leftover braces
    text = text.replace('{', '').replace('}', '')
    # Step 5: Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def normalize_math_text(text):
    """Normalize math/LaTeX in text so pandoc+xelatex won't choke.
    
    Rules:
    - Valid $...$ and $$...$$ formulas are preserved
    - HTML entities are unescaped
    - Incomplete/broken formulas are degraded to plain text
    """
    if not text or not text.strip():
        return text

    # Step 1: Unescape HTML entities
    text = _unescape_html(text)

    # Step 2: Extract well-formed $...$ and $$...$$ spans, replace with placeholders
    placeholders = []
    counter = [0]

    def _store_match(m):
        key = f"\x00MATH{counter[0]}\x00"
        counter[0] += 1
        placeholders.append((key, m.group(0)))
        return key

    # Match $$...$$ first (greedy, non-nested)
    text = re.sub(r'\$\$(.+?)\$\$', _store_match, text, flags=re.DOTALL)
    # Then $...$ (not $$)
    text = re.sub(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', _store_match, text)

    # Step 3: Degrade raw LaTeX commands left outside math spans. Pandoc passes
    # many raw TeX commands through to XeLaTeX; commands like \alpha outside
    # $...$ are a common source of "Missing $ inserted".
    if _has_latex_commands(text):
        text = _degrade_to_plain(text)

    # Step 4: Check remaining text for broken math indicators
    # A lone $ that wasn't matched = unpaired delimiter
    if '$' in text:
        # Degradation mode: put placeholders back without $, convert all
        for key, val in placeholders:
            # Still check if the stored formula itself is well-formed
            inner = val.strip('$').strip()
            if _has_broken_latex_commands(inner):
                degraded = _degrade_to_plain(inner)
                text = text.replace(key, degraded)
            else:
                # Keep the formula but wrap in $ for pandoc
                text = text.replace(key, val)
        # Now handle the remaining broken $ signs
        # Remove stray $ characters (they're unpaired / broken)
        text = text.replace('$', '')
        text = _degrade_to_plain(text) if _has_broken_latex_commands(text) else text
        return re.sub(r'\s+', ' ', text).strip()

    # Step 5: Check placeholders for broken LaTeX
    final_text = text
    for key, val in placeholders:
        inner = val.strip('$').strip()
        if _has_broken_latex_commands(inner) or not _is_balanced_dollar(val):
            degraded = _degrade_to_plain(inner)
            final_text = final_text.replace(key, degraded)
        else:
            final_text = final_text.replace(key, val)

    # Step 6: Any remaining broken/raw LaTeX outside formulas?
    check = re.sub(r'\$\$(.+?)\$\$', '', final_text, flags=re.DOTALL)
    check = re.sub(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', '', check)
    if _has_broken_latex_commands(check) or _has_latex_commands(check):
        final_text = _degrade_to_plain(final_text)

    # Step 7: Clean up any leftover placeholder markers (shouldn't happen)
    final_text = re.sub(r'\x00MATH\d+\x00', '', final_text)

    return re.sub(r'\s+', ' ', final_text).strip()


# ═══════════════════════════════════════════════════════════════
# 6. MARKDOWN GENERATION
# ═══════════════════════════════════════════════════════════════

def _md_link_text(text):
    """Escape only characters that can break Markdown link text."""
    text = normalize_math_text(str(text or "")).strip()
    return text.replace("[", r"\[").replace("]", r"\]")


def _field(value, default="未核实"):
    value = str(value or "").strip()
    return value if value else default


def _reference_url(url):
    """Make a URL safe for Markdown reference-link definitions."""
    url = str(url or "").strip().replace("\n", "").replace("\r", "")
    url = url.replace("<", "%3C").replace(">", "%3E")
    return url


def generate_markdown(papers, output_path):
    """Generate fixed-template bilingual Markdown from papers list.

    Fixed paper format:
    1. clickable English title (reference-style Markdown link)
    2. Chinese title
    3. author / journal / year / source metadata
    4. English Abstract
    5. 中文摘要

    No standalone raw URL is displayed in the body. The reference definitions at
    the bottom make the title itself clickable in Markdown and in pandoc PDFs.
    """
    lines = []
    link_defs = []
    lines.append("# 谷歌学术快讯（中英对照）\n")
    now_str = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"生成时间：{now_str}  ")
    lines.append(f"模板版本：{FIXED_TEMPLATE_VERSION}  ")
    lines.append(f"PDF样式版本：{FIXED_PDF_STYLE_VERSION}  ")
    lines.append(f"共 {len(papers)} 篇论文")
    lines.append("\n---\n")

    for i, p in enumerate(papers, 1):
        ref = f"paper-{i}"
        title_en = _md_link_text(p.get("title_en") or p.get("title") or "No Title")
        title_cn = normalize_math_text(_field(p.get("title_cn"), "未翻译"))
        authors = _field(p.get("authors"))
        journal = _field(p.get("journal"))
        year = _field(p.get("year"))
        source = _field(p.get("source"), "Google Scholar Alert")
        abstract_source = _field(p.get("abstract_source"), "未标注")
        link_source = _field(p.get("link_source"), "Google Scholar Alert title link")
        link = _reference_url(p.get("link", ""))
        abstract_en = normalize_math_text(p.get("abstract_en", ""))
        abstract_cn = normalize_math_text(p.get("abstract_cn", ""))

        if "Google Scholar Alert snippet" in abstract_source:
            if not abstract_en.startswith("[Google Scholar Alert snippet]"):
                abstract_en = "[Google Scholar Alert snippet]\n\n" + abstract_en
            if not abstract_cn.startswith("[根据 Google Scholar Alert 摘要片段翻译]"):
                abstract_cn = "[根据 Google Scholar Alert 摘要片段翻译]\n\n" + abstract_cn

        lines.append(f"## [{i}. {title_en}][{ref}]\n")
        lines.append(f"**中文标题：** {title_cn}\n")
        lines.append(f"**作者：** {authors}  ")
        lines.append(f"**期刊：** {journal}  ")
        lines.append(f"**年份：** {year}  ")
        lines.append(f"**来源：** {source}  ")
        lines.append(f"**摘要来源：** {abstract_source}  ")
        lines.append(f"**链接来源：** {link_source}\n")
        lines.append("### English Abstract\n")
        lines.append(f"{abstract_en}\n")
        lines.append("### 中文摘要\n")
        lines.append(f"{abstract_cn}\n")
        lines.append("---\n")

        if link:
            link_defs.append(f"[{ref}]: <{link}>")

    if link_defs:
        lines.append("\n" + "\n".join(link_defs) + "\n")

    md_text = "\n".join(lines)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md_text)
    logger.info(f"Markdown written to {output_path} ({len(md_text)} chars, template={FIXED_TEMPLATE_VERSION})")
    return output_path


# ═══════════════════════════════════════════════════════════════
# 7. PDF CONVERSION (pandoc + XeLaTeX)
# ═══════════════════════════════════════════════════════════════

def _norm_font_name(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _font_match_name(font_name):
    """Return an installed family name suitable for fontspec, or None.

    `fc-match` may normalize family names, for example TeX Gyre Termes can be
    exposed as TeXGyreTermes. Passing the matched family back to XeLaTeX avoids
    false positives where fontconfig found a font but fontspec cannot load the
    prettified name.
    """
    if not shutil.which("fc-match"):
        return None
    try:
        r = subprocess.run(
            ["fc-match", "-f", "%{family}\n", font_name],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        requested = _norm_font_name(font_name)
        raw_families = [x.strip() for x in (r.stdout or "").strip().split(",") if x.strip()]
        matched_families = [_norm_font_name(x) for x in raw_families]
        if any(requested in fam or fam in requested for fam in matched_families if fam):
            return raw_families[0]
        return None
    except Exception:
        return None


def _font_available(font_name):
    return _font_match_name(font_name) is not None


def _select_font(env_name, candidates, fallback):
    forced = os.getenv(env_name, "").strip()
    if forced:
        matched = _font_match_name(forced)
        if matched:
            return matched
        logger.warning(f"{env_name}={forced!r} is not available; falling back to fixed candidate list")
    for font in candidates:
        matched = _font_match_name(font)
        if matched:
            return matched
    # Keep a sane default; XeLaTeX/pandoc will report the actual font error.
    return fallback


def _tex_font_file(font_name):
    """Return a TeX-distributed OpenType math font filename, or None.

    XeLaTeX often loads TeX math fonts reliably by filename, e.g.
    `STIXTwoMath-Regular.otf`, even when the human family name is not visible to
    fontconfig/fontspec.
    """
    candidates = {
        "Latin Modern Math": ["latinmodern-math.otf"],
        "STIX Two Math": ["STIXTwoMath-Regular.otf", "STIXTwoMath.otf"],
        "TeX Gyre Termes Math": ["texgyretermes-math.otf"],
        "TeX Gyre Pagella Math": ["texgyrepagella-math.otf"],
    }
    for filename in candidates.get(font_name, []):
        try:
            r = subprocess.run(["kpsewhich", filename], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            if r.returncode == 0 and (r.stdout or "").strip():
                return filename
        except Exception:
            pass
    return None


def _tex_font_available(font_name):
    return _tex_font_file(font_name) is not None


def _select_math_font():
    forced = os.getenv("PDF_MATH_FONT", "").strip()
    if forced:
        matched = _font_match_name(forced)
        if matched:
            return matched
        tex_file = _tex_font_file(forced)
        if tex_file:
            return tex_file
        # Also allow users to set a direct .otf filename known to TeX.
        if forced.lower().endswith(".otf"):
            try:
                r = subprocess.run(["kpsewhich", forced], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                if r.returncode == 0 and (r.stdout or "").strip():
                    return forced
            except Exception:
                pass
        logger.warning(f"PDF_MATH_FONT={forced!r} is not available; falling back to fixed candidate list")
    for font in [
        "STIX Two Math",
        "Latin Modern Math",
        "TeX Gyre Termes Math",
        "TeX Gyre Pagella Math",
    ]:
        matched = _font_match_name(font)
        if matched:
            return matched
        tex_file = _tex_font_file(font)
        if tex_file:
            return tex_file
    return ""


def _select_pdf_fonts():
    """Choose a stable mathematics-paper font profile for bilingual PDFs.

    Default profile is serif, not Microsoft YaHei, because ordinary mathematics
    papers and theses usually use Times/Termes-like Latin text, Song/serif CJK
    text, and a dedicated math font. Users can still override with
    PDF_MAIN_FONT / PDF_CJK_FONT / PDF_MONO_FONT / PDF_MATH_FONT in .env.
    """
    main_candidates = [
        "TeX Gyre Termes",
        "Tinos",
        "STIX Two Text",
        "Times New Roman",
        "Noto Serif",
        "DejaVu Serif",
    ]
    cjk_candidates = [
        "Noto Serif CJK SC",
        "Source Han Serif SC",
        "SimSun",
        "AR PL UMing CN",
        "AR PL UMing SC",
        "Noto Sans CJK SC",
        "Microsoft YaHei",
    ]
    mono_candidates = [
        "Noto Sans Mono CJK SC",
        "Noto Sans Mono",
        "Source Code Pro",
        "DejaVu Sans Mono",
        "Consolas",
    ]
    main_font = _select_font("PDF_MAIN_FONT", main_candidates, "TeX Gyre Termes")
    cjk_font = _select_font("PDF_CJK_FONT", cjk_candidates, "Noto Serif CJK SC")
    mono_font = _select_font("PDF_MONO_FONT", mono_candidates, "DejaVu Sans Mono")
    math_font = _select_math_font()
    return main_font, cjk_font, mono_font, math_font


def _pdf_style_header_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_fixed_style.tex")


def validate_markdown(md_path, expected_count=None):
    """Validate fixed bilingual Markdown before PDF generation."""
    if not os.path.isfile(md_path):
        raise FileNotFoundError(f"Markdown not found: {md_path}")
    text = open(md_path, encoding="utf-8").read()
    errors = []
    if "模板版本：" + FIXED_TEMPLATE_VERSION not in text:
        errors.append("Markdown is not using the fixed bilingual template")
    if re.search(r'No\s+Title', text, re.IGNORECASE):
        errors.append("Markdown contains No Title")
    for token in BAD_GLYPHS:
        if token in text:
            errors.append(f"Markdown contains broken glyph: {token}")
    if "### English Abstract" not in text:
        errors.append("Markdown missing English Abstract heading")
    if "### 中文摘要" not in text:
        errors.append("Markdown missing 中文摘要 heading")
    if not re.search(r'## \[[0-9]+\. .+?\]\[paper-[0-9]+\]', text):
        errors.append("Markdown title is not a clickable reference-style link")
    if expected_count is not None:
        title_count = len(re.findall(r'^## \[[0-9]+\. ', text, flags=re.MULTILINE))
        if title_count != expected_count:
            errors.append(f"Markdown has {title_count} paper titles, expected {expected_count}")
        link_def_count = len(re.findall(r'^\[paper-[0-9]+\]: <https?://', text, flags=re.MULTILINE))
        if link_def_count != expected_count:
            errors.append(f"Markdown has {link_def_count} title link definitions, expected {expected_count}")
    blocks = re.split(r'\n---\n', text)
    for idx, block in enumerate(blocks, 1):
        if "### 中文摘要" in block:
            after = block.split("### 中文摘要", 1)[1]
            if not re.search(r'[\u4e00-\u9fff]', after):
                errors.append(f"Block {idx}: 中文摘要 heading has no Chinese body")
        if "### English Abstract" in block:
            after = block.split("### English Abstract", 1)[1].split("### 中文摘要", 1)[0]
            if len(after.strip()) < 30:
                errors.append(f"Block {idx}: English Abstract body is too short")
    if errors:
        raise ValueError("Markdown validation failed:\n" + "\n".join(errors))
    logger.info("Markdown validation passed")
    return True


def validate_pdf_links(pdf_path, expected_links=None):
    """Validate that PDF has clickable link annotations.

    Returns (links_count, error_msg). If pypdf/PyPDF2 is unavailable, returns
    (-1, message) so the caller can warn without falsely claiming failure.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return -1, "pypdf/PyPDF2 not installed, cannot automatically validate PDF link annotations"

    if not os.path.isfile(pdf_path):
        return 0, f"PDF not found: {pdf_path}"

    r = PdfReader(pdf_path)
    links = 0
    for p in r.pages:
        annots = p.get("/Annots")
        if not annots:
            continue
        annots = annots.get_object() if hasattr(annots, "get_object") else annots
        for a in annots:
            obj = a.get_object()
            if obj.get("/Subtype") == "/Link":
                links += 1
    if links == 0:
        return 0, "PDF has no clickable hyperlink annotations"
    if expected_links is not None and links < expected_links:
        return links, f"PDF has only {links} clickable link(s), expected at least {expected_links}"
    return links, None


def _write_pdf_safe_markdown(md_path):
    """Write a conservative Markdown copy for a second PDF attempt."""
    safe_path = os.path.splitext(md_path)[0] + ".pdf-safe.md"
    with open(md_path, encoding="utf-8") as f:
        lines = f.readlines()

    safe_lines = []
    for line in lines:
        if line.startswith("[paper-"):
            safe_lines.append(line)
            continue
        safe_lines.append(normalize_math_text(line.rstrip("\n")) + "\n")

    with open(safe_path, "w", encoding="utf-8") as f:
        f.writelines(safe_lines)
    logger.warning(f"Wrote PDF-safe Markdown fallback: {safe_path}")
    return safe_path


def convert_markdown_to_pdf(md_path, pdf_path):
    """Convert Markdown to PDF using one fixed visual style.

    This is where PDF rendering is locked down: white background, A4 paper,
    fixed margins, fixed font family, fixed line spacing, fixed paragraph spacing,
    fixed heading spacing, and fixed hyperlink colors. Do not add ad-hoc pandoc
    flags elsewhere; change the constants/header here so every run looks the same.
    """
    if not os.path.isfile(md_path):
        logger.error(f"Markdown not found: {md_path}")
        return None
    if not shutil.which("pandoc"):
        logger.error("pandoc not installed — cannot generate PDF")
        return None

    main_font, cjk_font, mono_font, math_font = _select_pdf_fonts()
    style_header = _pdf_style_header_path()
    if not os.path.isfile(style_header):
        logger.error(f"Fixed PDF style file not found: {style_header}")
        return None
    os.makedirs(os.path.dirname(pdf_path) or ".", exist_ok=True)

    lua_filter = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrap_papers.lua")
    if not os.path.isfile(lua_filter):
        logger.warning(f"Lua filter not found: {lua_filter}, PDF will not have paper cards")

    cmd = [
        "pandoc", md_path,
        "-o", pdf_path,
        "--standalone",
        "--pdf-engine=xelatex",
        "--from", "markdown+tex_math_dollars+link_attributes+raw_attribute",
        "--include-in-header", style_header,
        "-V", "papersize=a4",
        "-V", "geometry:margin=2.5cm",
        "-V", "fontsize=11pt",
        "-V", "linestretch=1.12",
        "-V", f"mainfont={main_font}",
        "-V", f"sansfont={main_font}",
        "-V", f"monofont={mono_font}",
        "-V", f"CJKmainfont={cjk_font}",
        "-V", f"CJKsansfont={cjk_font}",
    ]
    if os.path.isfile(lua_filter):
        # Insert after --standalone, before --pdf-engine
        cmd.insert(cmd.index("--standalone") + 1, "--lua-filter=" + lua_filter)
    if math_font:
        cmd.extend(["-V", f"mathfont={math_font}"])

    def _run_pandoc(input_md):
        run_cmd = list(cmd)
        run_cmd[1] = input_md
        logger.info(f"Running fixed PDF style {FIXED_PDF_STYLE_VERSION}: {' '.join(run_cmd)}")
        return subprocess.run(run_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)

    result = _run_pandoc(md_path)
    if result.returncode != 0:
        logger.error(f"pandoc failed (rc={result.returncode}): {result.stderr[:1500]}")
        if "Missing $ inserted" in result.stderr or "LaTeX Error" in result.stderr:
            safe_md_path = _write_pdf_safe_markdown(md_path)
            logger.warning("Retrying PDF generation with conservative Markdown sanitization")
            result = _run_pandoc(safe_md_path)
        if result.returncode != 0:
            logger.error(f"pandoc retry failed (rc={result.returncode}): {result.stderr[:1500]}")
            return None
    if not os.path.isfile(pdf_path):
        logger.error(f"PDF not created: {pdf_path}")
        return None
    size_kb = os.path.getsize(pdf_path) / 1024
    logger.info(
        f"PDF written to {pdf_path} ({size_kb:.1f} KB, "
        f"style={FIXED_PDF_STYLE_VERSION}, mainfont={main_font}, CJKmainfont={cjk_font}, mathfont={math_font or 'default'})"
    )
    return pdf_path


def _sample_raw_email():
    """Build one minimal Scholar Alert-like HTML email for offline smoke tests."""
    html = """\
<html>
  <body>
    <a href="https://scholar.google.com/scholar_url?url=https%3A%2F%2Fexample.com%2Fpapers%2Fheat-equation">
      Stability estimates for a nonlinear heat equation
    </a>
    <div>A Author, B Researcher - Journal of Applied Mathematics, 2026</div>
    <div>
      This paper studies stability estimates for a nonlinear heat equation and
      proves a priori bounds under mild regularity assumptions for the source
      term and boundary data.
    </div>
  </body>
</html>
"""
    raw = (
        "From: scholaralerts-noreply@google.com\r\n"
        "Subject: Google Scholar Alert\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "\r\n" + html
    )
    return raw.encode("utf-8")


def _run_test_mode(json_path, md_path, pdf_path, skip_pdf):
    """Run an offline parser/Markdown/PDF smoke test without email or API calls."""
    logger.info("=" * 50)
    logger.info("TEST MODE: parsing built-in Scholar Alert sample")
    papers = parse_emails([_sample_raw_email()])
    if not papers:
        raise SystemExit("Test mode failed: sample email produced no papers")

    for p in papers:
        p["title_cn"] = "非线性热方程稳定性估计"
        p["abstract_cn"] = (
            "本文研究非线性热方程的稳定性估计，并在源项和边界数据满足温和"
            "正则性假设时证明了先验界。"
        )

    logger.info("=" * 50)
    logger.info("TEST MODE: saving JSON and validating fixed bilingual output")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    logger.info(f"JSON saved: {json_path}")

    validate_papers(papers)
    generate_markdown(papers, md_path)
    validate_markdown(md_path, expected_count=len(papers))
    logger.info(f"Markdown OK ({os.path.getsize(md_path)} bytes)")

    if not skip_pdf:
        result = convert_markdown_to_pdf(md_path, pdf_path)
        if result:
            links_count, err = validate_pdf_links(pdf_path, expected_links=len(papers))
            if err:
                logger.warning(f"PDF validation warning: {err}")
            else:
                logger.info(f"PDF OK ({os.path.getsize(pdf_path)} bytes, {links_count} clickable links)")

    logger.info("=" * 50)
    logger.info("TEST MODE DONE. Output files:")
    logger.info(f"  JSON:      {json_path}")
    logger.info(f"  Markdown:  {md_path}")
    if not skip_pdf:
        logger.info(f"  PDF:       {pdf_path}")


# ═══════════════════════════════════════════════════════════════
# 8. MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    # Load .env before building argument defaults so env-backed defaults work.
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Fetch & translate Google Scholar Alerts → JSON → Markdown → PDF")
    parser.add_argument("--email", default=os.getenv("GMAIL_ADDRESS") or os.getenv("EMAIL_ADDRESS"), help="Email address")
    parser.add_argument("--app-password", default=os.getenv("GMAIL_APP_PASSWORD") or os.getenv("EMAIL_APP_PASSWORD"), help="Email app password / auth code")
    parser.add_argument("--imap-server", default=os.getenv("IMAP_SERVER", "imap.163.com"), help="IMAP server (default: imap.163.com)")
    parser.add_argument("--label", default=os.getenv("IMAP_LABEL", "INBOX"), help="IMAP folder/label to search (default: INBOX)")
    parser.add_argument("--since-days", type=int, default=int(os.getenv("SINCE_DAYS", "7")), help="Days to look back")
    parser.add_argument("--max-emails", type=int, default=int(os.getenv("MAX_EMAILS", "10")), help="Max emails to fetch per folder (default: 10)")
    parser.add_argument("--keep-unread", action="store_true", help="Don't mark emails as read (for testing)")
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "/tmp"), help="Output directory")
    parser.add_argument("--json-input", default=None, help="Read papers from an existing JSON file instead of fetching email")
    parser.add_argument("--json-output", default=None, help="Custom JSON output path (default: <output-dir>/papers_translated.json)")
    parser.add_argument("--markdown-output", default=None, help="Custom Markdown output path (default: <output-dir>/scholar_alert_output.md)")
    parser.add_argument("--pdf-output", default=None, help="Custom PDF output path (default: <output-dir>/scholar_alert_output.pdf)")
    parser.add_argument("--skip-pdf", action="store_true", help="Skip PDF generation (JSON+MD only)")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip abstract enrichment from external sources")
    parser.add_argument("--skip-translate", action="store_true", help="Skip translation; when fetching email, save JSON and stop before Markdown/PDF")
    parser.add_argument("--cache-dir", default=None, help="API cache directory (default: .cache/ in skill dir)")
    parser.add_argument("--skip-check", action="store_true", help="Skip environment diagnostic")
    parser.add_argument("--test", action="store_true", help="Offline smoke test: parse built-in sample HTML without email/API calls")
    args = parser.parse_args()

    if not args.skip_check:
        _check_environment()

    if args.cache_dir:
        os.environ["CACHE_DIR"] = args.cache_dir

    out_dir = args.output_dir
    os.makedirs(out_dir, exist_ok=True)
    json_path = args.json_output or os.path.join(out_dir, "papers_translated.json")
    md_path = args.markdown_output or os.path.join(out_dir, "scholar_alert_output.md")
    pdf_path = args.pdf_output or os.path.join(out_dir, "scholar_alert_output.pdf")

    if args.test:
        _run_test_mode(json_path, md_path, pdf_path, args.skip_pdf)
        return

    if not HAS_REQUESTS:
        print("Error: requests library required. pip install requests")
        sys.exit(1)

    if not args.json_input and (not args.email or not args.app_password):
        parser.error("Need --email and --app-password (or set GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env)")

    if args.json_input:
        logger.info("=" * 50)
        logger.info(f"Step 1: Loading papers from JSON: {args.json_input}")
        with open(args.json_input, "r", encoding="utf-8") as f:
            papers = json.load(f)
        if not isinstance(papers, list):
            raise ValueError("--json-input must contain a JSON array of paper objects")
    else:
        # 1. Fetch emails
        logger.info("=" * 50)
        logger.info("Step 1: Fetching Scholar Alert emails...")
        raw_emails = fetch_scholar_alerts(args.email, args.app_password, since_days=args.since_days, label=args.label, imap_server=args.imap_server, max_emails=args.max_emails, unseen_only=not args.keep_unread)

        # 2. Parse papers
        logger.info("=" * 50)
        logger.info("Step 2: Parsing papers from emails...")
        papers = parse_emails(raw_emails)
    if not papers:
        logger.warning("No papers found. Exiting.")
        return

    # 3. Enrich abstracts (optional)
    if not args.skip_enrich:
        logger.info("=" * 50)
        logger.info("Step 3: Enriching abstracts from external sources...")
        enrich_abstracts(papers)

    # 3.5 Pre-validation: filter out papers with no valid title before translation
    papers = [p for p in papers if p.get("title_en", "").strip()]
    logger.info(f"After pre-validation: {len(papers)} papers with valid titles")
    if not papers:
        logger.warning("No papers with valid titles. Exiting.")
        return

    # 4. Translate (optional)
    if not args.skip_translate:
        logger.info("=" * 50)
        logger.info("Step 4: Translating titles & abstracts...")
        try:
            translate_papers(papers)
        except TranslationError as e:
            logger.error(f"Translation failed; refusing to generate incomplete bilingual output: {e}")
            raise SystemExit(1)

    # 5. Save JSON
    logger.info("=" * 50)
    logger.info("Step 5: Saving JSON...")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    logger.info(f"JSON saved: {json_path}")

    if args.skip_translate and not args.json_input:
        logger.info("Translation skipped for fetched/enriched papers; stopping after JSON output.")
        _save_cache()
        return

    # 6. Validate
    logger.info("=" * 50)
    logger.info("Step 6: Validating papers...")
    try:
        validate_papers(papers)
    except Exception as e:
        logger.error(str(e))
        raise SystemExit(1)

    # 7. Generate Markdown
    logger.info("=" * 50)
    logger.info("Step 7: Generating Markdown...")
    generate_markdown(papers, md_path)

    # 8. Validate Markdown
    try:
        validate_markdown(md_path, expected_count=len(papers))
    except Exception as e:
        logger.error(str(e))
        raise SystemExit(1)
    md_size = os.path.getsize(md_path)
    logger.info(f"Markdown OK ({md_size} bytes)")

    # 9. Convert to PDF
    if not args.skip_pdf:
        logger.info("=" * 50)
        logger.info("Step 9: Converting Markdown to PDF...")
        result = convert_markdown_to_pdf(md_path, pdf_path)
        if not result:
            raise SystemExit(1)
        # 10. Validate PDF
        if os.path.getsize(pdf_path) < 1000:
            logger.error(f"PDF suspiciously small ({os.path.getsize(pdf_path)} bytes)")
            raise SystemExit(1)
        # 11. Validate PDF links
        links_count, err = validate_pdf_links(pdf_path, expected_links=len(papers))
        if err and links_count == -1:
            logger.warning(err)
        elif err:
            logger.error(f"PDF validation failed: {err}")
            raise SystemExit(1)
        else:
            logger.info(f"PDF OK ({os.path.getsize(pdf_path)} bytes, {links_count} clickable links)")

    # Print final output paths
    logger.info("=" * 50)
    logger.info("DONE. Output files:")
    logger.info(f"  JSON:      {json_path}")
    logger.info(f"  Markdown:  {md_path}")
    if not args.skip_pdf:
        logger.info(f"  PDF:       {pdf_path}")

    # Notify (webhook + optional file copy)
    _notify(papers, md_path, pdf_path if not args.skip_pdf else None)

    _save_cache()


def _notify(papers, md_path, pdf_path):
    """Send summary to chat webhook; optionally copy PDF for bot delivery."""
    webhook = os.environ.get("NOTIFY_WEBHOOK", "").strip()
    copy_to = os.environ.get("NOTIFY_COPY_TO", "").strip()

    if not webhook and not copy_to:
        return

    logger.info("Sending notification...")

    # Build summary
    titles = [p.get("title_en", p.get("title", ""))[:80] for p in papers[:20]]
    summary = {
        "msgtype": "text",
        "text": {
            "content": (
                f"📄 谷歌学术快讯 ({datetime.now().strftime('%Y-%m-%d')})\n"
                f"共 {len(papers)} 篇论文\n\n" +
                "\n".join(f"• {t}" for t in titles) +
                (f"\n\n... 还有 {len(papers)-20} 篇" if len(papers) > 20 else "")
            )
        }
    }

    if webhook:
        try:
            if HAS_REQUESTS:
                requests.post(webhook, json=summary, timeout=10)
                logger.info("  Webhook sent")
        except Exception as e:
            logger.warning(f"  Webhook failed: {e}")

    if copy_to and pdf_path and os.path.isfile(pdf_path):
        try:
            os.makedirs(copy_to, exist_ok=True)
            dest = os.path.join(copy_to, os.path.basename(pdf_path))
            shutil.copy2(pdf_path, dest)
            logger.info(f"  PDF copied to: {dest}")
        except Exception as e:
            logger.warning(f"  PDF copy failed: {e}")


def _load_dotenv():
    """Minimal .env loader (no dependency on python-dotenv)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if not os.path.isfile(env_path):
        env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


if __name__ == "__main__":
    main()
