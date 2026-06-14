from __future__ import annotations

import datetime as dt
import email
import html
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "profile.json"
DATA_DIR = ROOT / "data"
OUTPUTS_DIR = ROOT / "outputs"
SENT_HISTORY_PATH = DATA_DIR / "sent_history.json"
FEEDBACK_MEMORY_PATH = DATA_DIR / "feedback_memory.json"
LAST_ITEMS_PATH = DATA_DIR / "last_email_items.json"
DIGEST_RUNS_PATH = DATA_DIR / "digest_runs.json"
LATEST_HTML_PATH = OUTPUTS_DIR / "latest_email.html"

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


@dataclass
class Paper:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    primary_category: str
    abs_url: str
    pdf_url: str
    published: str
    updated: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def text_blob(self) -> str:
        return f"{self.title} {self.abstract} {' '.join(self.authors)} {' '.join(self.categories)}".lower()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def arxiv_id_from_url(url_or_id: str) -> str:
    match = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", url_or_id)
    return match.group(1) if match else url_or_id.strip()


def parse_arxiv_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_arxiv_query(search_query: str, max_results: int) -> list[Paper]:
    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "lastUpdatedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "daily-arxiv-digest/1.0"})
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                payload = response.read()
            break
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(5 + attempt * 5)
    else:
        raise last_exc or RuntimeError("arXiv request failed")

    root = ET.fromstring(payload)
    papers: list[Paper] = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = entry.findtext(f"{ATOM}id", default="")
        arxiv_id = arxiv_id_from_url(raw_id)
        title = normalize_space(entry.findtext(f"{ATOM}title", default=""))
        abstract = normalize_space(entry.findtext(f"{ATOM}summary", default=""))
        authors = [
            normalize_space(author.findtext(f"{ATOM}name", default=""))
            for author in entry.findall(f"{ATOM}author")
        ]
        categories = [
            node.attrib.get("term", "")
            for node in entry.findall(f"{ATOM}category")
            if node.attrib.get("term")
        ]
        primary_node = entry.find(f"{ARXIV_NS}primary_category")
        primary_category = primary_node.attrib.get("term", categories[0] if categories else "") if primary_node is not None else (categories[0] if categories else "")
        links = entry.findall(f"{ATOM}link")
        abs_url = next((node.attrib.get("href", "") for node in links if node.attrib.get("rel") == "alternate"), f"https://arxiv.org/abs/{arxiv_id}")
        pdf_url = next((node.attrib.get("href", "") for node in links if node.attrib.get("title") == "pdf"), f"https://arxiv.org/pdf/{arxiv_id}")
        published = entry.findtext(f"{ATOM}published", default="")
        updated = entry.findtext(f"{ATOM}updated", default="")
        if title and abstract and arxiv_id:
            papers.append(
                Paper(
                    arxiv_id=arxiv_id,
                    title=title,
                    authors=authors,
                    abstract=abstract,
                    categories=categories,
                    primary_category=primary_category,
                    abs_url=abs_url,
                    pdf_url=pdf_url,
                    published=published,
                    updated=updated,
                )
            )
    return papers


def collect_candidates(config: dict[str, Any]) -> list[Paper]:
    categories = config["categories"]
    max_results = int(config.get("max_candidates_to_fetch", 220))
    per_category = max(20, min(60, max_results // max(len(categories), 1)))
    keyword_query = " OR ".join(
        [
            'all:"large language model"',
            'all:"foundation model"',
            'all:"LLM"',
            'all:"language agent"',
            'all:"inference optimization"',
            'all:"post-training"',
        ]
    )

    queries = [(f"cat:{cat}", per_category) for cat in categories]
    queries.append((f"({keyword_query})", min(100, max_results)))
    seen: dict[str, Paper] = {}
    for query, limit in queries:
        try:
            for paper in fetch_arxiv_query(query, max_results=limit):
                seen[paper.arxiv_id] = paper
            time.sleep(3)
        except Exception as exc:
            print(f"Warning: failed to fetch arXiv query {query!r}: {exc}", file=sys.stderr)
    return list(seen.values())


def term_hits(text: str, terms: list[str]) -> list[str]:
    hits = []
    lower = text.lower()
    for term in terms:
        needle = term.lower().strip()
        if needle and needle in lower:
            hits.append(term)
    return hits


def tokenize(value: str) -> set[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "using",
        "into", "via", "based", "model", "models", "language", "large", "paper", "study",
        "method", "approach", "results", "show", "can", "our", "their", "they", "we",
    }
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", value.lower()))
    return {token for token in tokens if token not in stop}


def update_feedback_from_mail(config: dict[str, Any]) -> dict[str, Any]:
    memory = load_json(
        FEEDBACK_MEMORY_PATH,
        {
            "more_terms": {},
            "less_terms": {},
            "more_authors": {},
            "less_authors": {},
            "raw_feedback": [],
            "processed_message_ids": [],
        },
    )
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        print("Gmail credentials missing; skipping feedback import.", file=sys.stderr)
        return memory

    last_items = load_json(LAST_ITEMS_PATH, {"items": []})
    processed = set(memory.get("processed_message_ids", []))
    since = (dt.datetime.utcnow() - dt.timedelta(days=14)).strftime("%d-%b-%Y")

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(user, password)
            imap.select("INBOX")
            status, data = imap.search(None, f'(SINCE "{since}" SUBJECT "{config["email_subject_prefix"]}")')
            if status != "OK":
                return memory
            ids = data[0].split()
            for msg_id in ids[-20:]:
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue
                raw = msg_data[0][1]
                message = email.message_from_bytes(raw)
                message_id = message.get("Message-ID") or msg_id.decode("utf-8", errors="ignore")
                if message_id in processed:
                    continue
                body = extract_message_text(message)
                if not body:
                    continue
                apply_feedback_text(memory, body, last_items)
                memory.setdefault("raw_feedback", []).append(
                    {
                        "message_id": message_id,
                        "date": message.get("Date", ""),
                        "text": body[:2000],
                    }
                )
                processed.add(message_id)
            memory["processed_message_ids"] = sorted(processed)
    except Exception as exc:
        print(f"Warning: failed to import Gmail feedback: {exc}", file=sys.stderr)
    save_json(FEEDBACK_MEMORY_PATH, memory)
    return memory


def extract_message_text(message: email.message.Message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get("Content-Disposition", "")
            if "attachment" in disposition.lower():
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))

    text = "\n".join(parts)
    clean_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or stripped.lower().startswith("on ") or "wrote:" in stripped.lower():
            break
        if stripped:
            clean_lines.append(stripped)
    return "\n".join(clean_lines).strip()


CN_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def referenced_indices(text: str, positive: bool) -> list[int]:
    words = "多发|更像|喜欢|类似|more|like" if positive else "少发|减少|不喜欢|别发|less|dislike"
    pattern = rf"({words})[^。\n,，;；#第]*(?:#\s*)?(\d+|[一二三四五六七八九十])"
    found = []
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        raw = match.group(2)
        found.append(int(raw) if raw.isdigit() else CN_NUMBERS.get(raw, 0))
    pattern2 = rf"({words})[^。\n,，;；]*第\s*(\d+|[一二三四五六七八九十])\s*篇"
    for match in re.finditer(pattern2, text, flags=re.IGNORECASE):
        raw = match.group(2)
        found.append(int(raw) if raw.isdigit() else CN_NUMBERS.get(raw, 0))
    return [idx for idx in found if idx > 0]


def extract_feedback_terms(text: str, positive: bool) -> list[str]:
    cues = ["多发", "更关注", "喜欢", "more", "like"] if positive else ["少发", "减少", "不喜欢", "别发", "less", "dislike"]
    all_cues = ["多发", "更关注", "喜欢", "more", "like", "少发", "减少", "不喜欢", "别发", "less", "dislike"]
    terms: list[str] = []
    for cue in cues:
        for match in re.finditer(rf"{cue}\s*([^。\n;；]+)", text, flags=re.IGNORECASE):
            fragment = match.group(1)
            fragment = re.sub(r"(第\s*)?[一二三四五六七八九十\d]+\s*篇|#\s*\d+|类似的?|这种|那种", " ", fragment)
            for piece in re.split(r"[,，、/]|和|以及|\band\b", fragment, flags=re.IGNORECASE):
                cleaned = normalize_space(piece)
                if any(other.lower() in cleaned.lower() for other in all_cues):
                    continue
                if 2 <= len(cleaned) <= 60:
                    terms.append(cleaned)
    return terms


def boost_terms_from_item(memory: dict[str, Any], item: dict[str, Any], positive: bool) -> None:
    bucket = "more_terms" if positive else "less_terms"
    author_bucket = "more_authors" if positive else "less_authors"
    features = item.get("features", [])
    for term in features[:12]:
        memory.setdefault(bucket, {})[term] = memory.setdefault(bucket, {}).get(term, 0) + 1.0
    for author in item.get("authors", [])[:4]:
        memory.setdefault(author_bucket, {})[author] = memory.setdefault(author_bucket, {}).get(author, 0) + 0.6


def apply_feedback_text(memory: dict[str, Any], text: str, last_items: dict[str, Any]) -> None:
    items_by_index = {int(item["index"]): item for item in last_items.get("items", []) if "index" in item}
    for idx in referenced_indices(text, positive=True):
        if idx in items_by_index:
            boost_terms_from_item(memory, items_by_index[idx], positive=True)
    for idx in referenced_indices(text, positive=False):
        if idx in items_by_index:
            boost_terms_from_item(memory, items_by_index[idx], positive=False)

    for term in extract_feedback_terms(text, positive=True):
        memory.setdefault("more_terms", {})[term] = memory.setdefault("more_terms", {}).get(term, 0) + 1.2
    for term in extract_feedback_terms(text, positive=False):
        memory.setdefault("less_terms", {})[term] = memory.setdefault("less_terms", {}).get(term, 0) + 1.2


def score_papers(papers: list[Paper], config: dict[str, Any], memory: dict[str, Any]) -> list[Paper]:
    seed_tokens = set()
    seed_terms = config.get("interest_terms", []) + config.get("strong_like_terms", [])
    seed_tokens.update(tokenize(" ".join(seed_terms)))

    for paper in papers:
        score = 0.0
        reasons: list[str] = []
        blob = paper.text_blob

        interest_hits = term_hits(blob, config.get("interest_terms", []))
        score += len(interest_hits) * 4.0
        if interest_hits:
            reasons.append("matches " + ", ".join(interest_hits[:4]))

        strong_hits = term_hits(blob, config.get("strong_like_terms", []))
        score += len(strong_hits) * 7.0
        if strong_hits:
            reasons.append("strong interest: " + ", ".join(strong_hits[:3]))

        less_hits = term_hits(blob, config.get("less_like_terms", []))
        score -= len(less_hits) * 5.0

        for author in paper.authors:
            if any(author.lower() == target.lower() for target in config.get("must_watch_authors", [])):
                score += 25.0
                reasons.append(f"must-watch author: {author}")

        more_terms = memory.get("more_terms", {})
        less_terms = memory.get("less_terms", {})
        for term, weight in more_terms.items():
            if term.lower() in blob:
                score += min(float(weight), 8.0) * 3.0
                reasons.append(f"your recent feedback likes: {term}")
        for term, weight in less_terms.items():
            if term.lower() in blob:
                score -= min(float(weight), 8.0) * 3.5

        for author, weight in memory.get("more_authors", {}).items():
            if any(author.lower() == p_author.lower() for p_author in paper.authors):
                score += min(float(weight), 5.0) * 3.0
                reasons.append(f"author boosted by feedback: {author}")
        for author, weight in memory.get("less_authors", {}).items():
            if any(author.lower() == p_author.lower() for p_author in paper.authors):
                score -= min(float(weight), 5.0) * 3.0

        overlap = len(tokenize(paper.text_blob) & seed_tokens)
        score += min(overlap, 18) * 1.3
        if overlap >= 4:
            reasons.append("semantically close to your LLM/optimization profile")

        if paper.primary_category in {"cs.CL", "cs.LG", "cs.AI"}:
            score += 4.0
        if "survey" in blob or "landscape" in blob or "taxonomy" in blob:
            score += 8.0
            reasons.append("landscape/survey-style paper")

        paper.score = round(score, 3)
        paper.reasons = dedupe(reasons)[:5]
    return sorted(papers, key=lambda item: item.score, reverse=True)


def dedupe(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def filter_recent_and_unsent(papers: list[Paper], config: dict[str, Any], history: dict[str, Any], now: dt.datetime) -> list[Paper]:
    lookback = dt.timedelta(days=int(config.get("lookback_days", 3)))
    sent_keys = set(history.get("sent_keys", []))
    recent = []
    for paper in papers:
        try:
            updated = parse_arxiv_time(paper.updated)
        except ValueError:
            continue
        sent_key = f"{paper.arxiv_id}:{paper.updated}"
        if now.astimezone(dt.timezone.utc) - updated <= lookback and sent_key not in sent_keys:
            recent.append(paper)
    return recent


def first_sentences(text: str, max_sentences: int = 3, max_chars: int = 650) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", normalize_space(text))
    selected = []
    total = 0
    for sentence in sentences:
        if not sentence:
            continue
        if total + len(sentence) > max_chars and selected:
            break
        selected.append(sentence)
        total += len(sentence)
        if len(selected) >= max_sentences:
            break
    return " ".join(selected)[:max_chars].strip()


def infer_method(paper: Paper) -> str:
    blob = paper.text_blob
    patterns = [
        ("reinforcement learning / post-training", ["reinforcement learning", "rlhf", "rlaif", "rlvr", "preference optimization"]),
        ("retrieval-augmented generation", ["retrieval", "rag", "retrieval-augmented"]),
        ("agent or tool-use evaluation", ["agent", "tool use", "planning"]),
        ("inference/training efficiency optimization", ["inference", "latency", "throughput", "efficient", "optimization", "serving"]),
        ("survey/taxonomy/meta-analysis", ["survey", "taxonomy", "landscape", "comprehensive review"]),
        ("multimodal modeling", ["multimodal", "vision-language", "image", "video"]),
        ("benchmark/evaluation", ["benchmark", "evaluation", "eval"]),
    ]
    for label, needles in patterns:
        if any(needle in blob for needle in needles):
            return label
    return "method inferred from abstract-level evidence"


def infer_limitations(paper: Paper) -> str:
    blob = paper.text_blob
    if "benchmark" in blob or "evaluation" in blob:
        return "可能依赖特定任务集，泛化到真实研究/生产场景仍需进一步验证。"
    if "survey" in blob or "landscape" in blob:
        return "综述类工作通常更强在结构化认知，具体结论需要回到原论文和实证细节核对。"
    if "efficient" in blob or "inference" in blob or "optimization" in blob:
        return "效率收益可能受模型规模、硬件、batch 设置和实现细节影响，需关注实验配置。"
    return "当前只基于 arXiv 摘要判断，细节、消融和真实效果需要阅读全文确认。"


def why_relevant(paper: Paper) -> str:
    if paper.reasons:
        return "；".join(paper.reasons[:3])
    return "与 LLM / optimization / agent / evaluation 相关度较高。"


def extract_features(paper: Paper) -> list[str]:
    candidates = list(paper.categories)
    candidates.extend(term for term in tokenize(paper.title + " " + paper.abstract) if len(term) > 3)
    preferred = [
        "landscape", "survey", "taxonomy", "optimization", "inference", "training", "efficiency",
        "reasoning", "agent", "alignment", "post-training", "retrieval", "multimodal",
        "benchmark", "evaluation", "scaling", "synthetic", "data", "moe",
    ]
    ordered = [term for term in preferred if term in " ".join(candidates).lower()]
    ordered.extend(candidates)
    return dedupe([normalize_space(term) for term in ordered if normalize_space(term)])[:30]


def render_email(selected: list[Paper], config: dict[str, Any], now_local: dt.datetime) -> tuple[str, str]:
    deep_n = int(config.get("top_deep_dive", 5))
    deep = selected[:deep_n]
    candidates = selected[deep_n : deep_n + int(config.get("top_candidates", 10))]
    date_label = now_local.strftime("%Y-%m-%d")
    title = f'{config["email_subject_prefix"]} - {date_label}'

    html_parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;line-height:1.55;color:#202124;max-width:860px;margin:24px auto;padding:0 16px;}h1{font-size:24px;}h2{font-size:20px;margin-top:28px;}h3{font-size:17px;margin-bottom:6px}.paper{border-top:1px solid #ddd;padding-top:16px;margin-top:16px}.meta{color:#5f6368;font-size:13px}.pill{display:inline-block;border:1px solid #dadce0;border-radius:12px;padding:1px 8px;margin:2px;color:#3c4043;font-size:12px}.score{color:#0b57d0}.feedback{background:#f8fafd;border:1px solid #dfe6f5;padding:12px;border-radius:8px;margin-top:28px}</style>",
        "</head><body>",
        f"<h1>{html.escape(title)}</h1>",
        "<p>Top 5 精读 + 10 篇一句话候选。你可以直接回复这封邮件来调整明天的偏好。</p>",
    ]
    plain_parts = [title, "", "Top 5 精读 + 10 篇一句话候选。", ""]

    html_parts.append("<h2>Top 5 精读</h2>")
    plain_parts.append("Top 5 精读")
    for idx, paper in enumerate(deep, start=1):
        append_paper_html(html_parts, idx, paper, deep=True)
        append_paper_plain(plain_parts, idx, paper, deep=True)

    html_parts.append("<h2>10 篇一句话候选</h2>")
    plain_parts.extend(["", "10 篇一句话候选"])
    for offset, paper in enumerate(candidates, start=deep_n + 1):
        append_paper_html(html_parts, offset, paper, deep=False)
        append_paper_plain(plain_parts, offset, paper, deep=False)

    feedback_text = "直接回复：多发 #3 类似的，少发 #1；多发 landscape / inference optimization / RLVR。"
    html_parts.append(f"<div class='feedback'><strong>反馈方式</strong><br>{html.escape(feedback_text)}</div>")
    html_parts.append("</body></html>")
    plain_parts.extend(["", "反馈方式", feedback_text])
    return "\n".join(html_parts), "\n".join(plain_parts)


def append_paper_html(parts: list[str], idx: int, paper: Paper, deep: bool) -> None:
    authors = ", ".join(paper.authors[:8]) + (" et al." if len(paper.authors) > 8 else "")
    parts.append("<div class='paper'>")
    parts.append(f"<h3>#{idx} <a href='{html.escape(paper.abs_url)}'>{html.escape(paper.title)}</a></h3>")
    parts.append(f"<div class='meta'>{html.escape(authors)} | {html.escape(paper.primary_category)} | score <span class='score'>{paper.score:.1f}</span> | updated {html.escape(paper.updated[:10])}</div>")
    for cat in paper.categories[:6]:
        parts.append(f"<span class='pill'>{html.escape(cat)}</span>")
    if deep:
        fields = [
            ("TL;DR", first_sentences(paper.abstract, 1, 260)),
            ("中文摘要", chinese_summary(paper)),
            ("English Summary", first_sentences(paper.abstract, 3, 680)),
            ("方法 / Method", infer_method(paper)),
            ("贡献 / Contribution", contribution_text(paper)),
            ("局限 / Limitation", infer_limitations(paper)),
            ("为什么与你相关 / Why relevant", why_relevant(paper)),
        ]
        for label, value in fields:
            parts.append(f"<p><strong>{html.escape(label)}:</strong> {html.escape(value)}</p>")
    else:
        parts.append(f"<p>{html.escape(one_line_candidate(paper))}</p>")
    parts.append("</div>")


def append_paper_plain(parts: list[str], idx: int, paper: Paper, deep: bool) -> None:
    authors = ", ".join(paper.authors[:8]) + (" et al." if len(paper.authors) > 8 else "")
    parts.extend(
        [
            "",
            f"#{idx} {paper.title}",
            f"Authors: {authors}",
            f"Link: {paper.abs_url}",
            f"Category: {paper.primary_category}; Score: {paper.score:.1f}; Updated: {paper.updated[:10]}",
        ]
    )
    if deep:
        parts.extend(
            [
                f"TL;DR: {first_sentences(paper.abstract, 1, 260)}",
                f"中文摘要: {chinese_summary(paper)}",
                f"English Summary: {first_sentences(paper.abstract, 3, 680)}",
                f"方法 / Method: {infer_method(paper)}",
                f"贡献 / Contribution: {contribution_text(paper)}",
                f"局限 / Limitation: {infer_limitations(paper)}",
                f"为什么与你相关 / Why relevant: {why_relevant(paper)}",
            ]
        )
    else:
        parts.append(one_line_candidate(paper))


def chinese_summary(paper: Paper) -> str:
    method = infer_method(paper)
    tldr = first_sentences(paper.abstract, 2, 360)
    return f"这篇论文主要围绕 {paper.primary_category} 中的 {method} 展开。根据摘要，核心问题是：{tldr} 适合先作为候选阅读，再结合实验设置判断是否值得精读。"


def contribution_text(paper: Paper) -> str:
    blob = paper.text_blob
    if "survey" in blob or "taxonomy" in blob or "landscape" in blob:
        return "提供领域结构、问题分类或趋势梳理，帮助更新 LLM landscape 认知地图。"
    if "benchmark" in blob or "evaluation" in blob:
        return "提供新的评测视角或任务设置，可用于判断模型能力和研究空白。"
    if "efficient" in blob or "optimization" in blob or "inference" in blob:
        return "尝试改善训练、推理或服务效率，对 LLM optimization 方向有参考价值。"
    return "提出或分析一种与 LLM/AI 系统相关的方法，具体贡献需结合正文实验确认。"


def one_line_candidate(paper: Paper) -> str:
    return f"{first_sentences(paper.abstract, 1, 220)} Relevant because: {why_relevant(paper)}"


def send_email(subject: str, html_body: str, plain_body: str) -> bool:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = os.environ.get("EMAIL_TO") or user
    if not user or not password or not to_addr:
        print("Gmail credentials or EMAIL_TO missing; writing preview only.", file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return True


def update_history_and_mapping(selected: list[Paper], history: dict[str, Any], now_local: dt.datetime) -> None:
    sent_ids = set(history.get("sent_ids", []))
    sent_keys = set(history.get("sent_keys", []))
    sent_log = history.get("sent_log", [])
    for paper in selected:
        sent_ids.add(paper.arxiv_id)
        sent_keys.add(f"{paper.arxiv_id}:{paper.updated}")
        sent_log.append(
            {
                "arxiv_id": paper.arxiv_id,
                "sent_key": f"{paper.arxiv_id}:{paper.updated}",
                "title": paper.title,
                "sent_at": now_local.isoformat(),
                "updated": paper.updated,
                "score": paper.score,
            }
        )
    history["sent_ids"] = sorted(sent_ids)
    history["sent_keys"] = sorted(sent_keys)
    history["sent_log"] = sent_log[-1000:]
    save_json(SENT_HISTORY_PATH, history)

    mapping = {
        "date": now_local.strftime("%Y-%m-%d"),
        "items": [
            {
                "index": idx,
                "arxiv_id": paper.arxiv_id,
                "title": paper.title,
                "authors": paper.authors,
                "categories": paper.categories,
                "features": extract_features(paper),
            }
            for idx, paper in enumerate(selected, start=1)
        ],
    }
    save_json(LAST_ITEMS_PATH, mapping)


def scheduled_digest_already_sent(now_local: dt.datetime) -> bool:
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return False
    runs = load_json(DIGEST_RUNS_PATH, {"sent_dates": []})
    return now_local.strftime("%Y-%m-%d") in set(runs.get("sent_dates", []))


def mark_scheduled_digest_sent(now_local: dt.datetime) -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    runs = load_json(DIGEST_RUNS_PATH, {"sent_dates": [], "sent_log": []})
    date_label = now_local.strftime("%Y-%m-%d")
    sent_dates = set(runs.get("sent_dates", []))
    sent_dates.add(date_label)
    runs["sent_dates"] = sorted(sent_dates)
    log = runs.get("sent_log", [])
    log.append({"date": date_label, "sent_at": now_local.isoformat()})
    runs["sent_log"] = log[-365:]
    save_json(DIGEST_RUNS_PATH, runs)


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    tz = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    now_local = dt.datetime.now(tz)
    now_utc = now_local.astimezone(dt.timezone.utc)

    DATA_DIR.mkdir(exist_ok=True)
    OUTPUTS_DIR.mkdir(exist_ok=True)

    if scheduled_digest_already_sent(now_local):
        print(f"Scheduled digest already sent for {now_local.strftime('%Y-%m-%d')}; skipping fallback run.")
        return 0

    memory = update_feedback_from_mail(config)
    history = load_json(SENT_HISTORY_PATH, {"sent_ids": [], "sent_log": []})
    candidates = collect_candidates(config)
    candidates = filter_recent_and_unsent(candidates, config, history, now_utc)
    ranked = score_papers(candidates, config, memory)
    needed = int(config.get("top_deep_dive", 5)) + int(config.get("top_candidates", 10))
    selected = ranked[:needed]

    if not selected:
        print("No new papers selected today.")
        return 0

    html_body, plain_body = render_email(selected, config, now_local)
    LATEST_HTML_PATH.write_text(html_body, encoding="utf-8")
    subject = f'{config["email_subject_prefix"]} - {now_local.strftime("%Y-%m-%d")}'
    sent = send_email(subject, html_body, plain_body)
    if sent:
        update_history_and_mapping(selected, history, now_local)
        mark_scheduled_digest_sent(now_local)
        print(f"Selected and sent {len(selected)} papers.")
    else:
        print(f"Selected {len(selected)} papers and wrote preview, but did not update sent history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
