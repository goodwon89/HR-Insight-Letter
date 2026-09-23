"""
인재경영실 Letter — Notion 기반 뉴스레터 시스템
================================================
노션 페이지 작성 → GitHub Actions 수동 실행 → 이메일 발송 + GitHub Pages 누적 아카이빙

필수 GitHub Secrets:
  - NOTION_TOKEN       : Notion Integration 토큰
  - GMAIL_USER         : 발신 Gmail 주소
  - GMAIL_APP_PASS     : Gmail 앱 비밀번호 (16자리)
  - EMAIL_RECIPIENTS   : 수신자 이메일 (쉼표 구분)

워크플로우 실행 시 입력:
  - notion_page_id     : 발행할 노션 페이지 URL 또는 ID
  - send_email         : true / false (기본: true)
"""

import os, re, json, base64, smtplib, uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests

# ──────────────────────────────────────────────────────────────
# 로고 로드 (logo.png 또는 logo.jpg가 같은 폴더에 있으면 base64 임베드)
# ──────────────────────────────────────────────────────────────
def _load_logo_base64() -> tuple:
    """(data_uri, mime_type) 반환. 파일 없으면 (None, None)"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for fname, mime in [("logo.png","image/png"), ("logo.jpg","image/jpeg"),
                        ("logo.jpeg","image/jpeg"), ("logo.svg","image/svg+xml")]:
        path = os.path.join(base_dir, fname)
        if os.path.exists(path):
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return f"data:{mime};base64,{b64}", mime
    return None, None

LOGO_DATA_URI, LOGO_MIME = _load_logo_base64()

# ──────────────────────────────────────────────────────────────
# 상수 · 설정
# ──────────────────────────────────────────────────────────────
KST             = timezone(timedelta(hours=9))
ARCHIVE_FILE    = "letters_archive.json"
INDEX_FILE      = "index.html"
NEWSLETTER_NAME = "SSI Weekly HR Insight"
ORG_NAME        = "상상인그룹 인재경영실"
TEAL            = "#00BFB6"   # 포인트 컬러 (R0 G191 B182)
DARK_NAVY       = "#273646"   # 본문 텍스트 컬러 (R39 G54 B70) — 변수명은 유지, 값만 신규 팔레트로 교체
LINK_TEXT       = "#00958D"   # 링크 텍스트 실색상(대비 확보용 진한 톤)
LINK_TINT       = "#B2F0EB"   # 사용자 지정 링크컬러(R178 G240 B235) — 밑줄/하이라이트 전용
                               # (본문 배경에 텍스트로 그대로 쓰면 명도 대비가 약 1.3:1로 WCAG 기준 미달)
NOTION_API_VER  = "2022-06-28"

# ▼ GitHub Pages 로고 URL (Gmail 호환용)
# Gmail은 data: URI 이미지를 보안 차단함 → 외부 URL 필수
# 예시: "https://yourusername.github.io/your-repo/logo.png"
# GitHub Secret 'LOGO_URL' 또는 아래에 직접 입력
LOGO_URL = os.environ.get("LOGO_URL", "").strip()


# ──────────────────────────────────────────────────────────────
# Notion 페이지 ID 정규화
# ──────────────────────────────────────────────────────────────

def normalize_page_id(raw: str) -> str:
    """URL 또는 ID 문자열에서 순수 32자리 hex ID 추출"""
    raw = raw.strip()
    # URL 형식이면 마지막 path segment 추출
    if raw.startswith("http"):
        raw = raw.rstrip("/").split("/")[-1].split("?")[0]
    # 제목-ID 형식 (page-title-abc123...) → 마지막 32자 추출
    raw = re.sub(r"[^a-fA-F0-9]", "", raw)
    if len(raw) >= 32:
        return raw[-32:]
    return raw


# ──────────────────────────────────────────────────────────────
# Notion API
# ──────────────────────────────────────────────────────────────

def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VER,
        "Content-Type": "application/json",
    }

def fetch_page_meta(page_id: str, token: str) -> dict:
    url = f"https://api.notion.com/v1/pages/{page_id}"
    r = requests.get(url, headers=notion_headers(token), timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_blocks(block_id: str, token: str) -> list:
    """페이지네이션을 지원하는 블록 전체 로드"""
    blocks, cursor = [], None
    while True:
        url = f"https://api.notion.com/v1/blocks/{block_id}/children"
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(url, headers=notion_headers(token), params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks

def get_page_title(page_data: dict) -> str:
    """페이지 properties에서 title 추출"""
    props = page_data.get("properties", {})
    for key in props:
        prop = props[key]
        if prop.get("type") == "title":
            return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
    return NEWSLETTER_NAME


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


# ──────────────────────────────────────────────────────────────
# Notion 블록 → HTML 변환
# ──────────────────────────────────────────────────────────────

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def rt_to_html(rich_texts: list) -> str:
    """Notion rich_text 배열을 인라인 HTML로 변환"""
    out = ""
    for rt in rich_texts:
        t = esc(redact_secrets(rt.get("plain_text", "")))
        a = rt.get("annotations", {})
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
    """수집된 텍스트 조각으로 발췌문 생성 (의미 있는 1~2문장)"""
    result = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(result) >= max_len:
            break
        result = (result + " … " + p) if result else p
    if len(result) > max_len:
        result = result[:max_len].rsplit(" ", 1)[0] + "…"
    return result


def blocks_to_html(blocks: list):
    """
    Notion 블록 목록 → (이메일용 HTML 문자열, 발췌 문자열)
    이메일 클라이언트 호환을 위해 table·인라인 스타일 사용
    """
    html = ""
    excerpt_parts: list = []   # 발췌 후보 텍스트 조각 (최대 2개)
    i = 0

    while i < len(blocks):
        b  = blocks[i]
        bt = b.get("type", "")

        # ── paragraph ──
        if bt == "paragraph":
            rts   = b["paragraph"].get("rich_text", [])
            text  = rt_to_html(rts)
            plain = redact_secrets("".join(r.get("plain_text", "") for r in rts)).strip()
            # 발췌: 20자 이상 의미있는 단락만, 최대 2개 수집
            if plain and len(plain) >= 20 and len(excerpt_parts) < 2:
                excerpt_parts.append(plain)
            if text.strip():
                html += (f'<p style="margin:0 0 16px;line-height:1.8;'
                         f'color:{DARK_NAVY};font-size:15px;">{text}</p>')
            else:
                html += '<div style="height:8px;"></div>'

        # ── headings ──
        elif bt == "heading_1":
            t = rt_to_html(b["heading_1"].get("rich_text", []))
            html += (f'<h1 style="font-size:21px;font-weight:800;color:{DARK_NAVY};'
                     f'margin:32px 0 14px;padding-bottom:10px;'
                     f'border-bottom:2px solid {TEAL};">{t}</h1>')
        elif bt == "heading_2":
            # 다이제스트 구조에서는 heading_2 = 개별 기사 제목.
            # 바로 위에 heading_3(카테고리 라벨)을 붙여 쓰면 카테고리+제목 조합으로 보인다.
            t = rt_to_html(b["heading_2"].get("rich_text", []))
            html += (f'<h2 style="font-size:21px;font-weight:800;color:{DARK_NAVY};'
                     f'margin:8px 0 10px;line-height:1.4;letter-spacing:-.2px;">{t}</h2>')
        elif bt == "heading_3":
            # 다이제스트 구조에서 두 가지 용도로 재사용:
            #  1) 기사 heading_2 바로 앞에 써서 "카테고리 라벨" 역할 (예: 채용·인력)
            #  2) 기사 안에서 "관련기사"/"OOO 뉴스 더보기" 소제목 역할
            t = rt_to_html(b["heading_3"].get("rich_text", []))
            html += (f'<h3 style="font-size:13px;font-weight:800;color:{TEAL};'
                     f'margin:28px 0 2px;letter-spacing:.02em;line-height:1.5;">{t}</h3>')

        # ── bulleted list (연속 항목 묶음) ──
        elif bt == "bulleted_list_item":
            items = ""
            first_plain = ""
            while i < len(blocks) and blocks[i].get("type") == "bulleted_list_item":
                rts_li = blocks[i]["bulleted_list_item"].get("rich_text", [])
                t = rt_to_html(rts_li)
                items += f'<li style="margin-bottom:8px;line-height:1.7;color:{DARK_NAVY};">{t}</li>'
                if not first_plain:
                    first_plain = redact_secrets("".join(r.get("plain_text","") for r in rts_li)).strip()
                i += 1
            html += f'<ul style="margin:0 0 16px;padding-left:22px;">{items}</ul>'
            if first_plain and len(first_plain) >= 20 and len(excerpt_parts) < 2:
                excerpt_parts.append(first_plain)
            continue

        # ── numbered list (연속 항목 묶음) ──
        elif bt == "numbered_list_item":
            items = ""
            first_plain = ""
            while i < len(blocks) and blocks[i].get("type") == "numbered_list_item":
                rts_li = blocks[i]["numbered_list_item"].get("rich_text", [])
                t = rt_to_html(rts_li)
                items += f'<li style="margin-bottom:8px;line-height:1.7;color:{DARK_NAVY};">{t}</li>'
                if not first_plain:
                    first_plain = redact_secrets("".join(r.get("plain_text","") for r in rts_li)).strip()
                i += 1
            html += f'<ol style="margin:0 0 16px;padding-left:22px;">{items}</ol>'
            if first_plain and len(first_plain) >= 20 and len(excerpt_parts) < 2:
                excerpt_parts.append(first_plain)
            continue

        # ── divider ──
        # 다이제스트 구조에서는 기사와 기사 사이 구분선으로도 쓰인다.
        elif bt == "divider":
            html += f'<hr style="border:none;border-top:1px solid #DCEEEC;margin:32px 0;">'

        # ── callout (table로 이메일 호환) ──
        # 다이제스트 구조에서 "이번 주 HR 요약" 박스, 기사 마무리 인사이트 박스로 활용.
        elif bt == "callout":
            icon_d = b["callout"].get("icon") or {}
            icon   = icon_d.get("emoji", "💡") if icon_d.get("type") == "emoji" else "💡"
            t      = rt_to_html(b["callout"].get("rich_text", []))
            html  += (
                f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;">'
                f'<tr><td style="background:#F4FDFC;border-left:3px solid {TEAL};'
                f'border-radius:0 8px 8px 0;padding:14px 18px;line-height:1.75;'
                f'color:{DARK_NAVY};font-size:14.5px;">{icon}&nbsp;&nbsp;{t}</td></tr></table>'
            )

        # ── quote ──
        elif bt == "quote":
            t = rt_to_html(b["quote"].get("rich_text", []))
            html += (
                f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;">'
                f'<tr><td style="background:#f9fafb;border-left:4px solid {TEAL};'
                f'padding:14px 20px;color:#5c6b78;font-style:italic;'
                f'line-height:1.75;font-size:15px;border-radius:0 8px 8px 0;">{t}</td></tr></table>'
            )

        # ── image ──
        elif bt == "image":
            img = b["image"]
            src = ((img.get("file") or {}).get("url")
                   or (img.get("external") or {}).get("url", ""))
            cap = "".join(r.get("plain_text", "") for r in img.get("caption", []))
            if src:
                html += (
                    f'<div style="text-align:center;margin:4px 0 16px;">'
                    f'<img src="{src}" alt="{esc(cap)}" width="100%"'
                    f' style="max-width:580px;border-radius:10px;'
                    f'border:1px solid {TEAL};">'
                    + (f'<p style="font-size:11px;color:#94a3ab;margin:6px 0 0;text-align:right;">{esc(cap)}</p>' if cap else "")
                    + '</div>'
                )

        # ── toggle (제목만 표시) ──
        elif bt == "toggle":
            t = rt_to_html(b["toggle"].get("rich_text", []))
            html += (
                f'<div style="background:#f9fafb;border:1px solid #e5e7eb;'
                f'border-radius:8px;padding:14px 18px;margin:0 0 14px;">'
                f'<strong style="color:{DARK_NAVY};">▶ {t}</strong></div>'
            )

        # ── code ──
        elif bt == "code":
            code_text = "".join(r.get("plain_text", "") for r in b["code"].get("rich_text", []))
            html += (
                f'<pre style="background:{DARK_NAVY};color:#e5e7eb;border-radius:10px;'
                f'padding:18px 22px;margin:0 0 16px;overflow-x:auto;'
                f'font-size:13px;line-height:1.6;font-family:\'Courier New\',monospace;">'
                f'{esc(code_text)}</pre>'
            )

        i += 1

    excerpt = _build_excerpt(excerpt_parts, max_len=200)
    return html, excerpt


# ──────────────────────────────────────────────────────────────
# 이메일 HTML 빌더
# ──────────────────────────────────────────────────────────────

def build_email_html(title: str, content_html: str, date_str: str,
                     letter_no: int, archive_url: str) -> str:

    # 로고 셀 우선순위:
    #   1) LOGO_URL (GitHub Pages 외부 URL) → Gmail/Outlook 등 모든 클라이언트 호환
    #   2) LOGO_DATA_URI (base64)           → Python SMTP 직접 발송 전용 (Gmail API 차단)
    #   3) 텍스트 폴백
    # width:1%;white-space:nowrap → 로고 셀이 내용 최소폭만 차지, 남은 공간은 좌측 텍스트로
    _logo_td_style = (
        "width:1%;white-space:nowrap;"
        "vertical-align:middle;text-align:right;padding-left:24px;"
    )
    if LOGO_URL:
        logo_cell = (
            f'<td style="{_logo_td_style}">'
            f'<img src="{LOGO_URL}" alt="상상인그룹" height="40"'
            f' style="display:block;height:40px;max-width:160px;border:0;">'
            f'</td>'
        )
    elif LOGO_DATA_URI:
        # ⚠️ Gmail은 data: URI를 차단함 → LOGO_URL 설정 권장
        logo_cell = (
            f'<td style="{_logo_td_style}">'
            f'<img src="{LOGO_DATA_URI}" alt="상상인그룹" height="40"'
            f' style="display:block;height:40px;max-width:160px;border:0;">'
            f'</td>'
        )
    else:
        logo_cell = (
            f'<td style="{_logo_td_style}">'
            f'<span style="font-size:15px;font-weight:800;color:{DARK_NAVY};">상상인그룹</span>'
            f'</td>'
        )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{NEWSLETTER_NAME} — {esc(title)}</title>
  <link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#EEF5F4;
  font-family:'Pretendard','Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic',
  'Apple Color Emoji',sans-serif;">

<div style="max-width:660px;margin:0 auto;padding:32px 16px 48px;">

  <!-- ① 헤더 (흰 배경, 캡슐형 상단 — 좌: 브랜드+날짜+부제 / 우: 로고) -->
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#ffffff;border:1.5px solid {TEAL};border-bottom:none;
         border-radius:16px 16px 0 0;">
    <tr>
      <td style="padding:24px 28px 20px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <!-- 좌: 날짜 · 브랜드명 · 부제(=노션 페이지 제목) -->
            <td style="vertical-align:middle;">
              <div style="font-size:12px;color:#8a97a2;margin-bottom:10px;">
                {date_str} · {ORG_NAME}
              </div>
              <div style="font-size:25px;font-weight:800;color:{DARK_NAVY};
                letter-spacing:-.5px;line-height:1.3;">
                🧭 {NEWSLETTER_NAME}
              </div>
              <div style="font-size:13.5px;color:#5c6b78;font-weight:500;margin-top:6px;">
                {esc(title)}
              </div>
            </td>
            <!-- 우: 로고 -->
            {logo_cell}
          </tr>
        </table>
      </td>
    </tr>
  </table>

  <!-- ② 본문 (카테고리별 기사 블록 — Notion 원고 그대로 렌더링) -->
  <div style="background:#ffffff;padding:6px 28px 30px;">
    {content_html}
  </div>

  <!-- ③ 푸터 (캡슐형 하단 — 아카이브 CTA) -->
  <div style="background:#F4FDFC;border:1.5px solid {TEAL};border-top:none;
    padding:22px 28px;text-align:center;">
    <a href="{archive_url}" target="_blank" rel="noopener"
      style="display:inline-block;background:{TEAL};color:#ffffff;text-decoration:none;
      font-size:14px;font-weight:800;border-radius:10px;padding:12px 26px;margin-bottom:12px;">
      📂 지난 레터 아카이브 보기
    </a>
    <div style="font-size:12.5px;color:#5c6b78;">{ORG_NAME}</div>
  </div>

  <!-- ④ 하단 바 (틸 색 마감 스트립) -->
  <div style="background:{TEAL};border-radius:0 0 16px 16px;padding:14px 28px;text-align:center;">
    <p style="margin:0;font-size:11.5px;color:#ffffff;line-height:1.6;">
      이 메일은 {NEWSLETTER_NAME} 구독자에게 발송됩니다.
    </p>
  </div>

</div>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────
# [삭제됨] build_index_html() — index.html은 정적 파일로 관리
# GitHub Actions는 letters_archive.json만 업데이트하고
# index.html은 덮어쓰지 않음 (디자인 변경 시 직접 업로드)
# ──────────────────────────────────────────────────────────────

def _DELETED_build_index_html() -> str:  # ← 사용 안 함 (삭제 표시)
    return """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>인재경영실 Letter — 아카이브</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Apple SD Gothic Neo', 'Noto Sans KR', 'Malgun Gothic', sans-serif;
      background: #f0f2f5; color: #1e2235; min-height: 100vh;
    }

    /* ─── 헤더 ─── */
    .site-header { background: #1e2235; }
    .header-inner {
      max-width: 900px; margin: 0 auto;
      padding: 28px 24px 24px;
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 12px;
    }
    .logo-area { display: flex; align-items: center; gap: 12px; }
    .logo-mark { width: 34px; height: 34px; position: relative; flex-shrink: 0; }
    .logo-mark .c1 {
      position: absolute; top: 0; left: 0;
      width: 22px; height: 22px; background: #00A7A7; border-radius: 50%;
    }
    .logo-mark .c2 {
      position: absolute; bottom: 0; right: 0;
      width: 15px; height: 15px; background: #00A7A7;
      border-radius: 50%; opacity: 0.65;
    }
    .logo-text .org  { font-size: 11px; color: #94a3b8; font-weight: 500; }
    .logo-text .name { font-size: 18px; font-weight: 800; color: #fff; letter-spacing: -0.3px; }
    .header-badge {
      background: #00A7A7; color: #fff; font-size: 11px;
      font-weight: 700; padding: 4px 10px; border-radius: 20px; letter-spacing: 0.3px;
    }

    /* ─── 검색 바 ─── */
    .search-bar { background: #fff; border-bottom: 1px solid #e2e8f0; }
    .search-inner {
      max-width: 900px; margin: 0 auto; padding: 14px 24px;
      display: flex; gap: 12px; align-items: center;
    }
    .search-inner input {
      flex: 1; border: 1.5px solid #e2e8f0; border-radius: 9px;
      padding: 9px 16px; font-size: 14px; outline: none;
      font-family: inherit; transition: border-color .2s; color: #374151;
    }
    .search-inner input:focus { border-color: #00A7A7; }
    .count-badge { font-size: 13px; color: #64748b; white-space: nowrap; }
    .count-badge strong { color: #00A7A7; }

    /* ─── 메인 ─── */
    .main { max-width: 900px; margin: 0 auto; padding: 32px 24px; }

    /* ─── 카드 그리드 ─── */
    .letters-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
      gap: 22px;
    }

    /* ─── 카드 ─── */
    .card {
      background: #fff; border-radius: 14px;
      border: 1px solid #e2e8f0; overflow: hidden;
      transition: transform .2s ease, box-shadow .2s ease;
    }
    .card:hover { transform: translateY(-3px); box-shadow: 0 12px 32px rgba(0,0,0,0.09); }

    .card-header {
      background: #1e2235; padding: 16px 22px;
      display: flex; justify-content: space-between; align-items: center;
    }
    .card-no {
      font-size: 11px; color: #00A7A7; font-weight: 700;
      letter-spacing: 0.6px; text-transform: uppercase;
    }
    .card-date { font-size: 12px; color: #64748b; }

    .card-body { padding: 18px 22px 14px; }
    .card-title {
      font-size: 16px; font-weight: 700; color: #1e2235;
      line-height: 1.45; margin-bottom: 10px;
    }
    .card-excerpt {
      font-size: 13px; color: #64748b; line-height: 1.65;
      display: -webkit-box; -webkit-line-clamp: 3;
      -webkit-box-orient: vertical; overflow: hidden;
    }

    .card-footer { padding: 12px 22px 18px; display: flex; gap: 10px; }
    .btn-read {
      background: #00A7A7; color: #fff; border: none;
      border-radius: 8px; padding: 8px 18px; font-size: 13px;
      font-weight: 600; cursor: pointer; font-family: inherit;
      transition: background .15s;
    }
    .btn-read:hover { background: #008f8f; }
    .btn-collapse {
      background: #f1f5f9; color: #64748b; border: none;
      border-radius: 8px; padding: 8px 14px; font-size: 13px;
      cursor: pointer; font-family: inherit; display: none;
      transition: background .15s;
    }
    .btn-collapse:hover { background: #e2e8f0; }

    /* 전문 영역 */
    .card-full {
      display: none; border-top: 3px solid #00A7A7;
      padding: 28px 28px 24px; background: #fff;
    }
    .card-full h1 {
      font-size: 20px; font-weight: 800; color: #1e2235;
      margin: 28px 0 12px; padding-bottom: 8px;
      border-bottom: 2px solid #00A7A7;
    }
    .card-full h2 { font-size: 17px; font-weight: 700; color: #1e2235; margin: 24px 0 10px; }
    .card-full h3 {
      font-size: 13px; font-weight: 700; color: #00A7A7;
      margin: 18px 0 8px; text-transform: uppercase; letter-spacing: .6px;
    }
    .card-full p  { margin: 0 0 16px; line-height: 1.8; color: #374151; font-size: 15px; }
    .card-full ul { margin: 0 0 16px; padding-left: 22px; }
    .card-full ol { margin: 0 0 16px; padding-left: 22px; }
    .card-full li { margin-bottom: 8px; line-height: 1.7; color: #374151; }
    .card-full hr { border: none; border-top: 2px solid #e5e7eb; margin: 28px 0; }
    .card-full blockquote {
      margin: 0 0 16px; padding: 14px 20px;
      border-left: 4px solid #00A7A7; background: #f9fafb;
      color: #6b7280; font-style: italic; border-radius: 0 8px 8px 0; line-height: 1.75;
    }
    .card-full table { width: 100%; margin-bottom: 16px; }
    .card-full pre {
      background: #1e2235; color: #e5e7eb; border-radius: 10px;
      padding: 18px 22px; margin: 0 0 16px; overflow-x: auto;
      font-size: 13px; line-height: 1.6; font-family: 'Courier New', monospace;
    }
    .card-full img { max-width: 100%; border-radius: 10px; }
    .card-full code {
      background: #e6f7f7; color: #00A7A7; padding: 2px 6px;
      border-radius: 3px; font-size: 88%; font-family: monospace;
    }

    /* 빈 상태 */
    .empty-state { text-align: center; padding: 80px 24px; color: #94a3b8; }
    .empty-state .emoji { font-size: 48px; margin-bottom: 16px; }

    /* 사이트 푸터 */
    .site-footer { text-align: center; padding: 32px 24px; color: #94a3b8; font-size: 12px; }
    .site-footer a { color: #00A7A7; text-decoration: none; }

    @media (max-width: 640px) {
      .letters-grid { grid-template-columns: 1fr; }
      .header-inner { flex-direction: column; align-items: flex-start; }
      .card-full { padding: 20px 18px; }
    }
  </style>
</head>
<body>

<!-- 헤더 -->
<header class="site-header">
  <div class="header-inner">
    <div class="logo-area">
      <div class="logo-mark"><div class="c1"></div><div class="c2"></div></div>
      <div class="logo-text">
        <div class="org">상상인그룹</div>
        <div class="name">인재경영실 Letter</div>
      </div>
    </div>
    <span class="header-badge">ARCHIVE</span>
  </div>
</header>

<!-- 검색 바 -->
<div class="search-bar">
  <div class="search-inner">
    <input type="text" id="searchInput" placeholder="레터 제목 또는 내용으로 검색…" autocomplete="off">
    <div class="count-badge">총 <strong id="letterCount">-</strong>편</div>
  </div>
</div>

<!-- 메인 -->
<main class="main">
  <div class="letters-grid" id="grid"></div>
  <div class="empty-state" id="emptyState" style="display:none;">
    <div class="emoji">📭</div>
    <p>아직 발행된 레터가 없거나 검색 결과가 없습니다.</p>
  </div>
</main>

<!-- 푸터 -->
<footer class="site-footer">
  <p>© 상상인그룹 인재경영실 &nbsp;·&nbsp;
    <a href="https://www.sangsangin.com" target="_blank" rel="noopener">sangsangin.com</a>
  </p>
</footer>

<script>
let allLetters = [];

async function loadData() {
  try {
    const r = await fetch("letters_archive.json?t=" + Date.now());
    if (!r.ok) throw new Error("fetch failed");
    allLetters = await r.json();
  } catch(e) {
    allLetters = [];
  }
  renderGrid(allLetters);
}

function formatDate(dateStr) {
  const d = new Date(dateStr + "T00:00:00+09:00");
  return d.toLocaleDateString("ko-KR", { year: "numeric", month: "long", day: "numeric" });
}

function renderGrid(letters) {
  const grid   = document.getElementById("grid");
  const empty  = document.getElementById("emptyState");
  document.getElementById("letterCount").textContent = letters.length;

  if (!letters.length) {
    grid.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  grid.innerHTML = letters.map((l, idx) => `
    <article class="card">
      <div class="card-header">
        <div class="card-no">No.${l.number || (letters.length - idx)}</div>
        <div class="card-date">${formatDate(l.date)}</div>
      </div>
      <div class="card-body">
        <div class="card-title">${escHtml(l.title)}</div>
        <div class="card-excerpt">${escHtml(l.excerpt || "")}</div>
      </div>
      <div class="card-footer">
        <button class="btn-read" onclick="expandCard(this, ${idx})">전문 읽기 ↓</button>
        <button class="btn-collapse" onclick="collapseCard(this, ${idx})">접기 ↑</button>
      </div>
      <div class="card-full" id="full-${idx}"></div>
    </article>
  `).join("");
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function expandCard(btn, idx) {
  const full = document.getElementById("full-" + idx);
  // 콘텐츠 지연 주입 (XSS 안전: 서버 생성 HTML만 사용)
  if (!full.dataset.loaded) {
    full.innerHTML = allLetters[idx].html_content || "<p>본문을 불러올 수 없습니다.</p>";
    full.dataset.loaded = "1";
  }
  full.style.display = "block";
  btn.style.display  = "none";
  btn.nextElementSibling.style.display = "inline-flex";
  full.scrollIntoView({ behavior: "smooth", block: "start" });
}

function collapseCard(btn, idx) {
  const full = document.getElementById("full-" + idx);
  full.style.display = "none";
  btn.style.display  = "none";
  btn.previousElementSibling.style.display = "inline-flex";
}

document.getElementById("searchInput").addEventListener("input", function() {
  const q = this.value.trim().toLowerCase();
  if (!q) { renderGrid(allLetters); return; }
  const filtered = allLetters.filter(l =>
    l.title.toLowerCase().includes(q) ||
    (l.excerpt || "").toLowerCase().includes(q) ||
    (l.html_content || "").toLowerCase().includes(q)
  );
  renderGrid(filtered);
});

loadData();
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────
# 이메일 발송
# ──────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, recipients: list) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASS"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"상상인그룹 인재경영실 <{gmail_user}>"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, recipients, msg.as_string())
    print(f"  ✅ 이메일 발송 완료: {len(recipients)}명")


# ──────────────────────────────────────────────────────────────
# GitHub API — 파일 읽기/쓰기
# ──────────────────────────────────────────────────────────────

def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

def load_archive(owner: str, repo: str, token: str) -> list:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{ARCHIVE_FILE}"
    r = requests.get(url, headers=gh_headers(token), timeout=10)
    if r.status_code == 200:
        raw = base64.b64decode(r.json()["content"]).decode("utf-8")
        return json.loads(raw)
    return []

def push_file(content_str: str, path: str, message: str,
              owner: str, repo: str, token: str) -> None:
    url     = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = gh_headers(token)
    r       = requests.get(url, headers=headers, timeout=10)
    sha     = r.json().get("sha") if r.status_code == 200 else None

    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode(),
    }
    if sha:
        payload["sha"] = sha

    resp   = requests.put(url, headers=headers, json=payload, timeout=20)
    status = "완료" if resp.status_code in (200, 201) else f"실패 ({resp.status_code})"
    print(f"  GitHub [{path}] 업데이트 {status}")


# ──────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────

def main():
    now      = datetime.now(KST)
    date_str = now.strftime("%Y년 %m월 %d일")
    date_key = now.strftime("%Y-%m-%d")

    # ── 환경 변수 ──
    notion_token   = os.environ.get("NOTION_TOKEN", "").strip()
    raw_page_id    = os.environ.get("NOTION_PAGE_ID", "").strip()
    gh_owner       = os.environ.get("GITHUB_OWNER", "").strip()
    gh_repo        = os.environ.get("GITHUB_REPO", "").strip()
    gh_token       = os.environ.get("GITHUB_TOKEN", "").strip()
    recipients_str = os.environ.get("EMAIL_RECIPIENTS", "")
    recipients     = [r.strip() for r in recipients_str.split(",") if r.strip()]
    do_send_email  = os.environ.get("SEND_EMAIL", "true").strip().lower() != "false"
    archive_url    = f"https://{gh_owner}.github.io/{gh_repo}/"

    if not notion_token:
        print("❌ NOTION_TOKEN이 설정되지 않았습니다. GitHub Secrets를 확인하세요.")
        return
    if not raw_page_id:
        print("❌ NOTION_PAGE_ID가 비어있습니다. 워크플로우 입력값을 확인하세요.")
        return

    page_id = normalize_page_id(raw_page_id)
    print(f"=== {NEWSLETTER_NAME} 발행 시작 ===")
    print(f"날짜: {date_str}  |  노션 페이지 ID: {page_id}")

    # 1. 아카이브 로드
    print("\n[1] 기존 아카이브 로드 중...")
    archive   = load_archive(gh_owner, gh_repo, gh_token) if gh_token else []
    letter_no = len(archive) + 1
    print(f"  기존 레터 {len(archive)}편  →  이번 호: No.{letter_no}")

    # 2. 노션 페이지 읽기
    print("\n[2] 노션 페이지 읽는 중...")
    try:
        page_data    = fetch_page_meta(page_id, notion_token)
        title        = get_page_title(page_data)
        blocks       = fetch_blocks(page_id, notion_token)
        content_html, excerpt = blocks_to_html(blocks)
        print(f"  제목: {title}  |  블록 {len(blocks)}개")
    except Exception as e:
        print(f"❌ 노션 읽기 실패: {e}")
        raise

    # 3. 이메일 발송
    if do_send_email:
        print("\n[3] 이메일 발송 중...")
        if recipients:
            subject   = f"[인재경영실 Insight Letter] {title}"
            html_body = build_email_html(title, content_html, date_str,
                                         letter_no, archive_url)
            try:
                send_email(subject, html_body, recipients)
            except Exception as e:
                print(f"  ⚠️  이메일 발송 실패: {e}")
        else:
            print("  ⚠️  EMAIL_RECIPIENTS 미설정 — 이메일 발송 건너뜀")
    else:
        print("\n[3] 이메일 발송 건너뜀 (SEND_EMAIL=false)")

    # 4. 아카이브 업데이트 (항상 누적)
    print("\n[4] 아카이브 업데이트 중...")
    new_entry = {
        "id":             str(uuid.uuid4()),
        "number":         letter_no,
        "date":           date_key,
        "title":          title,
        "excerpt":        excerpt,
        "notion_page_id": raw_page_id,
        "html_content":   content_html,
    }
    archive.insert(0, new_entry)  # 최신순 prepend

    push_file(
        json.dumps(archive, ensure_ascii=False, indent=2),
        ARCHIVE_FILE,
        f"letter: No.{letter_no} — {title}",
        gh_owner, gh_repo, gh_token,
    )

    # 5. GitHub Pages index.html은 정적 파일로 유지 — 덮어쓰지 않음
    # (디자인 변경이 필요할 때만 index.html을 저장소에 직접 업로드)

    print(f"\n✅ 완료! No.{letter_no} '{title}' 발행 완료")
    print(f"   아카이브 URL: {archive_url}")


if __name__ == "__main__":
    main()
