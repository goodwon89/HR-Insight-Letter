"""
인재경영실 Insight Letter — GitHub Pages 아카이브 업데이터
===================================================
역할: 이메일 발송(Cowork에서 처리) 후 GitHub Pages 아카이브만 업데이트

필요한 GitHub Secrets:
  - NOTION_TOKEN    : Notion Integration 토큰
  - GITHUB_TOKEN    : 자동 제공 (별도 등록 불필요)

워크플로우 입력:
  - notion_page_id  : 발행한 노션 페이지 URL 또는 ID
"""

import os, re, json, base64, uuid
from datetime import datetime, timedelta, timezone
import requests

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────
KST            = timezone(timedelta(hours=9))
ARCHIVE_FILE   = "letters_archive.json"
INDEX_FILE     = "index.html"
TEAL           = "#00BFB6"   # 포인트 컬러 (R0 G191 B182) — hr_letter.py와 통일
DARK_NAVY      = "#273646"   # 본문 텍스트 컬러 (R39 G54 B70) — 변수명은 유지, 값만 신규 팔레트로 교체
LINK_TEXT      = "#00958D"   # 링크 텍스트 실색상(대비 확보용 진한 톤)
LINK_TINT      = "#B2F0EB"   # 사용자 지정 링크컬러(R178 G240 B235) — 밑줄/하이라이트 전용
NOTION_API_VER = "2022-06-28"


# ──────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────
def normalize_page_id(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("http"):
        raw = raw.rstrip("/").split("/")[-1].split("?")[0]
    raw = re.sub(r"[^a-fA-F0-9]", "", raw)
    return raw[-32:] if len(raw) >= 32 else raw

# ──────────────────────────────────────────────────────────────
# 민감정보 자동 마스킹
# ──────────────────────────────────────────────────────────────
_SECRET_PATTERNS = [
    # AWS Access Key ID (영구: AKIA, 임시: ASIA)
    (re.compile(r'\b(AKIA|ASIA)[0-9A-Z]{16}\b'),          "[AWS_KEY_REDACTED]"),
    # AWS Secret Access Key (40자 base64)
    (re.compile(r'(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])'), "[AWS_SECRET_REDACTED]"),
    # GitHub Personal Access Token
    (re.compile(r'\b(ghp|ghs|gho|ghu|ghr)_[A-Za-z0-9]{36,}\b'), "[GITHUB_TOKEN_REDACTED]"),
    # Generic API Key 패턴 (api_key=, apikey=, token= 뒤 값)
    (re.compile(r'(?i)(api[_-]?key|apikey|access[_-]?token|secret[_-]?key)\s*[=:]\s*["\']?([A-Za-z0-9_\-]{20,})["\']?'),
     r'\1=[REDACTED]'),
    # Slack Token
    (re.compile(r'\bxox[baprs]-[0-9A-Za-z\-]{10,}\b'), "[SLACK_TOKEN_REDACTED]"),
    # Private Key 블록
    (re.compile(r'-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----', re.DOTALL),
     "[PRIVATE_KEY_REDACTED]"),
]

def redact_secrets(text: str) -> str:
    """텍스트에서 민감정보 패턴을 탐지해 마스킹 후 반환. 변경 시 경고 출력."""
    for pattern, replacement in _SECRET_PATTERNS:
        new_text = pattern.sub(replacement, text)
        if new_text != text:
            print(f"  ⚠️  민감정보 마스킹 적용: {pattern.pattern[:40]}...")
            text = new_text
    return text


def esc(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ──────────────────────────────────────────────────────────────
# Notion API
# ──────────────────────────────────────────────────────────────
def notion_headers(token):
    return {"Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VER,
            "Content-Type": "application/json"}

def fetch_page_meta(page_id, token):
    r = requests.get(f"https://api.notion.com/v1/pages/{page_id}",
                     headers=notion_headers(token), timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_blocks(block_id, token):
    blocks, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor: params["start_cursor"] = cursor
        r = requests.get(f"https://api.notion.com/v1/blocks/{block_id}/children",
                         headers=notion_headers(token), params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"): break
        cursor = data.get("next_cursor")
    return blocks

def get_page_title(page_data):
    for prop in page_data.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(rt.get("plain_text","") for rt in prop.get("title",[]))
    return "인재경영실 Insight Letter"


# ──────────────────────────────────────────────────────────────
# Notion 블록 → HTML
# ──────────────────────────────────────────────────────────────
def rt_to_html(rich_texts):
    out = ""
    for rt in rich_texts:
        t = esc(redact_secrets(rt.get("plain_text","")))
        a = rt.get("annotations",{})
        h = rt.get("href")
        if a.get("bold"):          t = f"<strong>{t}</strong>"
        if a.get("italic"):        t = f"<em>{t}</em>"
        if a.get("underline"):     t = f"<u>{t}</u>"
        if a.get("strikethrough"): t = f"<s>{t}</s>"
        if a.get("code"):
            t = (f'<code style="background:#e6f7f7;color:{TEAL};padding:2px 6px;'
                 f'border-radius:3px;font-size:88%;font-family:monospace;">{t}</code>')
        if h:
            t = (f'<a href="{h}" target="_blank" rel="noopener"'
                 f' style="color:{LINK_TEXT};text-decoration:underline;'
                 f'text-decoration-color:{LINK_TINT};text-decoration-thickness:2px;">{t}</a>')
        out += t
    return out

def _build_excerpt(parts: list, max_len: int = 200) -> str:
    """
    수집된 텍스트 조각들로 발췌문 생성.
    - 의미 있는 문장 1~2개를 이어 붙임
    - 제목성(짧고 마침표 없는) 텍스트는 단독으로 표시하지 않음
    """
    result = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 이미 충분히 길면 중단
        if len(result) >= max_len:
            break
        if result:
            result += " … " + p
        else:
            result = p
        if len(result) >= max_len:
            break
    # 최대 길이 초과 시 단어 경계에서 자르기
    if len(result) > max_len:
        result = result[:max_len].rsplit(" ", 1)[0] + "…"
    return result


def blocks_to_html(blocks):
    html = ""
    excerpt_parts = []   # 발췌 후보 텍스트 조각
    EXCERPT_TARGET = 200  # 목표 발췌 길이(자)
    i = 0
    while i < len(blocks):
        b, bt = blocks[i], blocks[i].get("type","")

        if bt == "paragraph":
            rts  = b["paragraph"].get("rich_text",[])
            text = rt_to_html(rts)
            plain = redact_secrets("".join(r.get("plain_text","") for r in rts)).strip()
            # 발췌 수집: 20자 이상 의미있는 단락만, 최대 2개 문단
            if plain and len(plain) >= 20 and len(excerpt_parts) < 2:
                excerpt_parts.append(plain)
            html += (f'<p style="margin:0 0 16px;line-height:1.8;color:{DARK_NAVY};font-size:15px;">{text}</p>'
                     if text.strip() else '<div style="height:8px;"></div>')

        elif bt == "heading_1":
            t = rt_to_html(b["heading_1"].get("rich_text",[]))
            html += (f'<h1 style="font-size:21px;font-weight:800;color:{DARK_NAVY};'
                     f'margin:32px 0 14px;padding-bottom:10px;border-bottom:2px solid {TEAL};">{t}</h1>')
        elif bt == "heading_2":
            # 다이제스트 구조에서는 heading_2 = 개별 기사 제목 (hr_letter.py와 스타일 통일)
            t = rt_to_html(b["heading_2"].get("rich_text",[]))
            html += (f'<h2 style="font-size:21px;font-weight:800;color:{DARK_NAVY};'
                     f'margin:8px 0 10px;line-height:1.4;letter-spacing:-.2px;">{t}</h2>')
        elif bt == "heading_3":
            # 다이제스트 구조에서 "카테고리 라벨" 또는 "관련기사" 소제목 역할 (hr_letter.py와 스타일 통일)
            t = rt_to_html(b["heading_3"].get("rich_text",[]))
            html += (f'<h3 style="font-size:13px;font-weight:800;color:{TEAL};'
                     f'margin:28px 0 2px;letter-spacing:.02em;line-height:1.5;">{t}</h3>')

        elif bt == "bulleted_list_item":
            items = ""
            first_plain = ""
            while i < len(blocks) and blocks[i].get("type") == "bulleted_list_item":
                rts_li = blocks[i]["bulleted_list_item"].get("rich_text",[])
                items += f'<li style="margin-bottom:8px;line-height:1.7;color:{DARK_NAVY};">{rt_to_html(rts_li)}</li>'
                if not first_plain:
                    first_plain = redact_secrets("".join(r.get("plain_text","") for r in rts_li)).strip()
                i += 1
            html += f'<ul style="margin:0 0 16px;padding-left:22px;">{items}</ul>'
            # 발췌: 단락이 부족할 때 첫 번째 리스트 항목으로 보충
            if first_plain and len(first_plain) >= 20 and len(excerpt_parts) < 2:
                excerpt_parts.append(first_plain)
            continue

        elif bt == "numbered_list_item":
            items = ""
            first_plain = ""
            while i < len(blocks) and blocks[i].get("type") == "numbered_list_item":
                rts_li = blocks[i]["numbered_list_item"].get("rich_text",[])
                items += f'<li style="margin-bottom:8px;line-height:1.7;color:{DARK_NAVY};">{rt_to_html(rts_li)}</li>'
                if not first_plain:
                    first_plain = redact_secrets("".join(r.get("plain_text","") for r in rts_li)).strip()
                i += 1
            html += f'<ol style="margin:0 0 16px;padding-left:22px;">{items}</ol>'
            if first_plain and len(first_plain) >= 20 and len(excerpt_parts) < 2:
                excerpt_parts.append(first_plain)
            continue

        elif bt == "divider":
            html += f'<hr style="border:none;border-top:1px solid #DCEEEC;margin:32px 0;">'

        elif bt == "callout":
            icon_d = b["callout"].get("icon") or {}
            icon   = icon_d.get("emoji","💡") if icon_d.get("type")=="emoji" else "💡"
            t      = rt_to_html(b["callout"].get("rich_text",[]))
            html  += (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;">'
                      f'<tr><td style="background:#F4FDFC;border-left:3px solid {TEAL};'
                      f'border-radius:0 8px 8px 0;padding:14px 18px;line-height:1.75;color:{DARK_NAVY};font-size:14.5px;">'
                      f'{icon}&nbsp;&nbsp;{t}</td></tr></table>')

        elif bt == "quote":
            t = rt_to_html(b["quote"].get("rich_text",[]))
            html += (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;">'
                     f'<tr><td style="background:#f9fafb;border-left:4px solid {TEAL};'
                     f'padding:14px 20px;color:#5c6b78;font-style:italic;line-height:1.75;'
                     f'font-size:15px;border-radius:0 8px 8px 0;">{t}</td></tr></table>')

        elif bt == "image":
            img = b["image"]
            src = ((img.get("file") or {}).get("url") or (img.get("external") or {}).get("url",""))
            cap = "".join(r.get("plain_text","") for r in img.get("caption",[]))
            if src:
                html += (f'<div style="text-align:center;margin:4px 0 16px;">'
                         f'<img src="{src}" alt="{esc(cap)}" width="100%"'
                         f' style="max-width:580px;border-radius:10px;border:1px solid {TEAL};">'
                         + (f'<p style="font-size:11px;color:#94a3ab;margin:6px 0 0;text-align:right;">{esc(cap)}</p>' if cap else "")
                         + '</div>')
        i += 1
    excerpt = _build_excerpt(excerpt_parts, max_len=200)
    return html, excerpt


# ──────────────────────────────────────────────────────────────
# GitHub API
# ──────────────────────────────────────────────────────────────
def gh_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

def load_archive(owner, repo, token):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{ARCHIVE_FILE}"
    r = requests.get(url, headers=gh_headers(token), timeout=10)
    if r.status_code == 200:
        return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    return []

def push_file(content_str, path, message, owner, repo, token):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    r   = requests.get(url, headers=gh_headers(token), timeout=10)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": message,
                "content": base64.b64encode(content_str.encode("utf-8")).decode()}
    if sha: payload["sha"] = sha
    resp = requests.put(url, headers=gh_headers(token), json=payload, timeout=20)
    status = "완료" if resp.status_code in (200,201) else f"실패({resp.status_code})"
    print(f"  GitHub [{path}] {status}")


# ──────────────────────────────────────────────────────────────
# GitHub Pages HTML (아카이브 전용, 누적)
# ──────────────────────────────────────────────────────────────
def _read_logo_b64():
    """logo.png 가 스크립트와 같은 폴더에 있으면 base64 data URI 반환, 없으면 빈 문자열"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for fname, mime in [("logo.png","image/png"),("logo.jpg","image/jpeg"),("logo.svg","image/svg+xml")]:
        p = os.path.join(base_dir, fname)
        if os.path.exists(p):
            with open(p,"rb") as f:
                return f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"
    return ""


def build_index_html(logo_data_uri: str = "") -> str:
    logo_tag = (f'<img src="{logo_data_uri}" alt="상상인그룹" class="logo-img">'
                if logo_data_uri else
                '<span class="logo-text-fallback">상상인그룹</span>')
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>인재경영실 Insight Letter — 아카이브</title>
  <link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css" rel="stylesheet">
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Pretendard','Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic',sans-serif;
         background:#f4f5f7;color:#1e2235;min-height:100vh}}

    /* ── 헤더 (흰 배경, 모닝브리핑 스타일) ── */
    .site-header{{background:#fff;border-bottom:1px solid #e2e8f0}}
    .header-top{{max-width:960px;margin:0 auto;padding:28px 28px 20px;
                display:flex;align-items:flex-start;justify-content:space-between;gap:20px}}
    .header-title-area h1{{font-size:26px;font-weight:800;color:#1e2235;
                           letter-spacing:-.5px;line-height:1.25;margin-bottom:6px}}
    .header-title-area .subtitle{{font-size:13px;color:#94a3b8;font-weight:400}}
    .logo-img{{height:46px;width:auto;max-width:200px;display:block;flex-shrink:0}}
    .logo-text-fallback{{font-size:16px;font-weight:800;color:#1e2235}}

    /* 헤더 하단 버튼 행 */
    .header-actions{{max-width:960px;margin:0 auto;padding:14px 28px 20px;
                    display:flex;align-items:center;gap:10px;flex-wrap:wrap;
                    border-top:1px solid #e2e8f0}}
    .btn-intro{{display:inline-flex;align-items:center;gap:6px;
               background:#fff;color:#374151;font-size:13px;font-weight:500;
               border:1px solid #d1d5db;border-radius:8px;padding:8px 16px;
               text-decoration:none;transition:background .15s,border-color .15s;
               font-family:inherit}}
    .btn-intro:hover{{background:#f9fafb;border-color:#00A7A7;color:#00A7A7}}

    /* 검색 바 */
    .search-bar{{background:#fff;border-bottom:1px solid #e2e8f0}}
    .search-inner{{max-width:960px;margin:0 auto;padding:12px 28px;
                  display:flex;gap:12px;align-items:center}}
    .search-inner input{{flex:1;border:1.5px solid #e2e8f0;border-radius:9px;
                        padding:9px 16px;font-size:14px;outline:none;font-family:inherit;
                        transition:border-color .2s;color:#374151;background:#f8fafc}}
    .search-inner input:focus{{border-color:#00A7A7;background:#fff}}
    .count-badge{{font-size:13px;color:#94a3b8;white-space:nowrap}}
    .count-badge strong{{color:#00A7A7;font-weight:700}}

    /* 메인 그리드 */
    .main{{max-width:720px;margin:0 auto;padding:28px 28px}}
    .letters-grid{{display:grid;grid-template-columns:1fr;gap:20px}}

    /* 카드 */
    .card{{background:#fff;border-radius:14px;border:1px solid #e2e8f0;overflow:hidden;
          transition:transform .2s ease,box-shadow .2s ease}}
    .card:hover{{transform:translateY(-3px);box-shadow:0 10px 28px rgba(0,0,0,.08)}}
    .card-header{{background:#1e2235;padding:13px 20px;display:flex;align-items:center}}
    .card-date-header{{font-size:13px;color:#ffffff;font-weight:500;letter-spacing:.2px}}
    .card-body{{padding:18px 20px 12px}}
    .card-title{{font-size:15px;font-weight:700;color:#1e2235;line-height:1.5;margin-bottom:8px}}
    .card-excerpt{{font-size:13px;color:#64748b;line-height:1.65;
                  display:-webkit-box;-webkit-line-clamp:3;
                  -webkit-box-orient:vertical;overflow:hidden}}
    .card-footer{{padding:10px 20px 16px;display:flex;gap:8px}}
    .btn-read{{background:#00A7A7;color:#fff;border:none;border-radius:7px;
              padding:7px 16px;font-size:13px;font-weight:600;cursor:pointer;
              font-family:inherit;transition:background .15s}}
    .btn-read:hover{{background:#008f8f}}
    .btn-collapse{{background:#f1f5f9;color:#64748b;border:none;border-radius:7px;
                  padding:7px 13px;font-size:13px;cursor:pointer;font-family:inherit;
                  display:none;transition:background .15s}}
    .btn-collapse:hover{{background:#e2e8f0}}

    /* 전문 */
    .card-full{{display:none;border-top:3px solid #00A7A7;padding:26px 26px 22px;background:#fff}}
    .card-full h1{{font-size:20px;font-weight:800;color:#1e2235;margin:28px 0 12px;
                  padding-bottom:8px;border-bottom:2px solid #00A7A7}}
    .card-full h2{{font-size:17px;font-weight:700;color:#1e2235;margin:22px 0 10px}}
    .card-full h3{{font-size:13px;font-weight:700;color:#00A7A7;margin:16px 0 8px;
                  text-transform:uppercase;letter-spacing:.6px}}
    .card-full p{{margin:0 0 14px;line-height:1.8;color:#374151;font-size:15px}}
    .card-full ul,.card-full ol{{margin:0 0 14px;padding-left:22px}}
    .card-full li{{margin-bottom:7px;line-height:1.7;color:#374151}}
    .card-full hr{{border:none;border-top:2px solid #e5e7eb;margin:24px 0}}
    .card-full blockquote,.card-full table td{{background:#f9fafb;border-left:4px solid #00A7A7;
      padding:12px 18px;color:#6b7280;font-style:italic;border-radius:0 8px 8px 0;line-height:1.75}}
    .card-full table{{width:100%;margin-bottom:14px;border-collapse:collapse}}
    .card-full pre{{background:#1e2235;color:#e5e7eb;border-radius:10px;padding:16px 20px;
                   margin:0 0 14px;overflow-x:auto;font-size:13px;line-height:1.6;
                   font-family:'Courier New',monospace}}
    .card-full img{{max-width:100%;border-radius:10px}}
    .card-full code{{background:#e6f7f7;color:#00A7A7;padding:2px 5px;border-radius:3px;
                    font-size:88%;font-family:monospace}}

    .empty-state{{text-align:center;padding:72px 24px;color:#94a3b8}}
    .empty-state .emoji{{font-size:44px;margin-bottom:14px}}
    .site-footer{{text-align:center;padding:28px 24px;color:#94a3b8;font-size:12px;
                 border-top:1px solid #e2e8f0;margin-top:12px;background:#fff}}
    .site-footer a{{color:#00A7A7;text-decoration:none}}

    @media(max-width:680px){{
      .letters-grid{{grid-template-columns:1fr}}
      .header-top{{flex-direction:column;padding:20px 18px 16px}}
      .header-actions{{padding:12px 18px 16px}}
      .search-inner,.main{{padding-left:18px;padding-right:18px}}
      .card-full{{padding:18px 16px}}
    }}
  </style>
</head>
<body>

<!-- ── 헤더 ── -->
<header class="site-header">
  <div class="header-top">
    <!-- 좌: 타이틀 + 부제 -->
    <div class="header-title-area">
      <h1>인재경영실 Insight Letter</h1>
      <p class="subtitle">상상인그룹 인재경영실 · 인사이트 레터</p>
    </div>
    <!-- 우: 로고 -->
    {logo_tag}
  </div>

  <!-- 버튼 행 -->
  <div class="header-actions">
    <a href="https://ssihr.oopy.io/" target="_blank" rel="noopener" class="btn-intro">
      📄 인재경영실 소개
    </a>
  </div>
</header>

<!-- ── 검색 바 ── -->
<div class="search-bar">
  <div class="search-inner">
    <input type="text" id="searchInput" placeholder="키워드 검색…" autocomplete="off">
    <div class="count-badge">총 <strong id="letterCount">-</strong>편</div>
  </div>
</div>

<!-- ── 메인 ── -->
<main class="main">
  <div class="letters-grid" id="grid"></div>
  <div class="empty-state" id="emptyState" style="display:none">
    <div class="emoji">📭</div>
    <p>아직 발행된 레터가 없거나 검색 결과가 없습니다.</p>
  </div>
</main>

<!-- ── 푸터 ── -->
<footer class="site-footer">
  <p>© 상상인그룹 인재경영실 &nbsp;·&nbsp;
     <a href="https://ssihr.oopy.io/" target="_blank" rel="noopener">ssihr.oopy.io</a></p>
</footer>

<script>
let allLetters=[];
async function loadData(){{
  try{{const r=await fetch("letters_archive.json?t="+Date.now());
      if(!r.ok)throw 0;allLetters=await r.json();}}
  catch{{allLetters=[];}}
  renderGrid(allLetters);
}}
function fmtDate(s){{
  return new Date(s+"T00:00:00+09:00")
    .toLocaleDateString("ko-KR",{{year:"numeric",month:"long",day:"numeric"}});
}}
function escHtml(s){{
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}
function renderGrid(letters){{
  const grid=document.getElementById("grid"),empty=document.getElementById("emptyState");
  document.getElementById("letterCount").textContent=letters.length;
  if(!letters.length){{grid.innerHTML="";empty.style.display="block";return;}}
  empty.style.display="none";
  grid.innerHTML=letters.map((l,idx)=>`
    <article class="card">
      <div class="card-header">
        <div class="card-date-header">${{fmtDate(l.date)}}</div>
      </div>
      <div class="card-body">
        <div class="card-title">${{escHtml(l.title)}}</div>
        <div class="card-excerpt">${{escHtml(l.excerpt||"")}}</div>
      </div>
      <div class="card-footer">
        <button class="btn-read" onclick="expand(this,${{idx}})">전문 읽기 ↓</button>
        <button class="btn-collapse" onclick="collapse(this,${{idx}})">접기 ↑</button>
      </div>
      <div class="card-full" id="full-${{idx}}"></div>
    </article>`).join("");
}}
function expand(btn,idx){{
  const full=document.getElementById("full-"+idx);
  if(!full.dataset.loaded){{full.innerHTML=allLetters[idx].html_content||"<p>본문 없음</p>";full.dataset.loaded=1;}}
  full.style.display="block";btn.style.display="none";
  btn.nextElementSibling.style.display="inline-flex";
  full.scrollIntoView({{behavior:"smooth",block:"start"}});
}}
function collapse(btn,idx){{
  document.getElementById("full-"+idx).style.display="none";
  btn.style.display="none";btn.previousElementSibling.style.display="inline-flex";
}}
document.getElementById("searchInput").addEventListener("input",function(){{
  const q=this.value.trim().toLowerCase();
  if(!q){{renderGrid(allLetters);return;}}
  renderGrid(allLetters.filter(l=>
    l.title.toLowerCase().includes(q)||
    (l.excerpt||"").toLowerCase().includes(q)||
    (l.html_content||"").toLowerCase().includes(q)));
}});
loadData();
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────
def main():
    now      = datetime.now(KST)
    date_str = now.strftime("%Y년 %m월 %d일")
    date_key = now.strftime("%Y-%m-%d")

    notion_token = os.environ.get("NOTION_TOKEN","").strip()
    raw_page_id  = os.environ.get("NOTION_PAGE_ID","").strip()
    gh_owner     = os.environ.get("GITHUB_OWNER","").strip()
    gh_repo      = os.environ.get("GITHUB_REPO","").strip()
    gh_token     = os.environ.get("GITHUB_TOKEN","").strip()

    if not notion_token: print("❌ NOTION_TOKEN 미설정"); return
    if not raw_page_id:  print("❌ NOTION_PAGE_ID 미입력"); return

    page_id = normalize_page_id(raw_page_id)
    print(f"=== 인재경영실 Insight Letter 아카이브 업데이트 ===")
    print(f"날짜: {date_str}  |  페이지: {page_id}")

    # 1. 아카이브 로드
    archive   = load_archive(gh_owner, gh_repo, gh_token) if gh_token else []
    letter_no = len(archive) + 1
    print(f"기존 {len(archive)}편 → 이번 호: No.{letter_no}")

    # 2. Notion 읽기
    print("노션 읽는 중...")
    page_data         = fetch_page_meta(page_id, notion_token)
    title             = get_page_title(page_data)
    blocks            = fetch_blocks(page_id, notion_token)
    content_html, exc = blocks_to_html(blocks)
    print(f"  제목: {title}  |  블록 {len(blocks)}개")

    # 3. 아카이브 업데이트 (letters_archive.json 만 업데이트)
    # ⚠️  index.html은 덮어쓰지 않음 — 디자인 변경 시 index.html을 저장소에 직접 업로드할 것
    archive.insert(0, {
        "id":             str(uuid.uuid4()),
        "number":         letter_no,
        "date":           date_key,
        "title":          title,
        "excerpt":        exc,
        "notion_page_id": raw_page_id,
        "html_content":   content_html,
    })
    push_file(json.dumps(archive, ensure_ascii=False, indent=2),
              ARCHIVE_FILE, f"letter: No.{letter_no} — {title}",
              gh_owner, gh_repo, gh_token)

    print(f"\n✅ No.{letter_no} '{title}' 아카이브 완료")
    print("📌 index.html은 유지됩니다. 디자인 변경 시 저장소에 직접 업로드하세요.")


if __name__ == "__main__":
    main()
