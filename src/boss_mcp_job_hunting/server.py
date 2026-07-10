#!/usr/bin/env python3
"""FastMCP server for Boss Zhipin job hunting.

The server intentionally uses a persistent Playwright browser profile instead of
hard-coding private Boss API endpoints. After the user logs in once, later
searches can reuse the same local browser profile.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad
from fastmcp import Context, FastMCP
from playwright.async_api import BrowserContext, Page, async_playwright


mcp = FastMCP("boss-mcp-job-hunting")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_DIR = PROJECT_ROOT / ".boss-browser-profile"
QR_DIR = PROJECT_ROOT / ".boss-qrcode"
BOSS_HOME_URL = "https://www.zhipin.com/"
BOSS_LOGIN_URL = "https://www.zhipin.com/web/user/"
BOSS_JOB_SEARCH_URL = "https://www.zhipin.com/web/geek/job"
BOSS_COOKIE_ENV = "BOSS_COOKIE"
DISPATCHER_I_STR = (
    "8048b8676fb7d3d8952276e6e98e0bde."
    "f2dc7a63c4b0fbfa4b51a07e2710cf83."
    "fef7e750fc3a1e6327e8a880915aee9c."
    "ae00f848beb1aa591d71d5a80dd3bd95"
)
DISPATCHER_E_B64 = "clRwXUJBK1VKK0k0IWFbbQ=="

CITY_CODE_MAP = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "南京": "101190100",
    "武汉": "101200100",
    "西安": "101110100",
    "苏州": "101190400",
    "天津": "101030100",
    "重庆": "101040100",
}


@dataclass
class JobPosting:
    title: str
    company: str | None = None
    salary: str | None = None
    location: str | None = None
    experience: str | None = None
    education: str | None = None
    tags: list[str] | None = None
    publish_text: str | None = None
    publish_date: str | None = None
    url: str | None = None
    matched_keywords: list[str] | None = None
    raw_text: str | None = None


@dataclass
class QrLoginState:
    qr_id: str | None = None
    qr_path: str | None = None
    created_at: float | None = None
    session: requests.Session | None = None


qr_login_state = QrLoginState()


def _profile_dir() -> Path:
    configured = os.getenv("BOSS_MCP_PROFILE_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_PROFILE_DIR


def _profile_display() -> str:
    return "<BOSS_MCP_PROFILE_DIR>" if os.getenv("BOSS_MCP_PROFILE_DIR") else "./.boss-browser-profile"


def _qr_display(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _city_code(city: str, city_code: str | None = None) -> str:
    if city_code:
        return city_code
    return CITY_CODE_MAP.get(city, city)


def _build_search_url(keyword: str, city: str, city_code: str | None = None) -> str:
    code = _city_code(city, city_code)
    return f"{BOSS_JOB_SEARCH_URL}?query={quote_plus(keyword)}&city={quote_plus(code)}"


def _safe_cookie_names(cookie_header: str) -> list[str]:
    names = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return sorted(set(names))


def _cookie_header_to_playwright_cookies(cookie_header: str) -> list[dict[str, str]]:
    cookies = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".zhipin.com",
                "path": "/",
            }
        )
    return cookies


def _requests_cookie_header(session: requests.Session) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in session.cookies)


def _make_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": BOSS_LOGIN_URL,
            "Origin": "https://www.zhipin.com",
        }
    )
    return session


def _dispatcher_fp() -> str:
    key_bytes = base64.b64decode(DISPATCHER_E_B64)
    plaintext_bytes = DISPATCHER_I_STR.encode("utf-8")
    iv_bytes = get_random_bytes(16)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
    ciphertext_bytes = cipher.encrypt(pad(plaintext_bytes, AES.block_size))
    return base64.b64encode(iv_bytes + ciphertext_bytes).decode("utf-8")


def _save_qr_image(qr_id: str, image_bytes: bytes, content_type: str = "") -> Path:
    QR_DIR.mkdir(parents=True, exist_ok=True)
    is_jpeg = image_bytes.startswith(b"\xff\xd8") or "jpeg" in content_type.lower() or "jpg" in content_type.lower()
    suffix = ".jpg" if is_jpeg else ".png"
    path = QR_DIR / f"boss_qr_{qr_id}{suffix}"
    path.write_bytes(image_bytes)
    return path


def _qr_session_path(qr_id: str) -> Path:
    return QR_DIR / f"{qr_id}.session.json"


def _save_qr_session(qr_id: str, session: requests.Session) -> None:
    QR_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "qr_id": qr_id,
        "created_at": time.time(),
        "cookies": requests.utils.dict_from_cookiejar(session.cookies),
    }
    _qr_session_path(qr_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_qr_session(qr_id: str) -> requests.Session:
    session = _make_requests_session()
    path = _qr_session_path(qr_id)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        session.cookies.update(requests.utils.cookiejar_from_dict(payload.get("cookies", {})))
    return session


def _start_qr_login_sync() -> dict[str, Any]:
    session = _make_requests_session()
    randkey_resp = session.post("https://www.zhipin.com/wapi/zppassport/captcha/randkey", timeout=15)
    randkey_resp.raise_for_status()
    qr_id = randkey_resp.json()["zpData"]["qrId"]

    image_resp = session.get(
        f"https://www.zhipin.com/wapi/zpweixin/qrcode/getqrcode?content={qr_id}",
        timeout=15,
    )
    image_resp.raise_for_status()
    qr_path = _save_qr_image(qr_id, image_resp.content, image_resp.headers.get("Content-Type", ""))

    qr_login_state.qr_id = qr_id
    qr_login_state.qr_path = str(qr_path)
    qr_login_state.created_at = time.time()
    qr_login_state.session = session
    _save_qr_session(qr_id, session)

    return {
        "qr_id": qr_id,
        "qr_path": str(qr_path),
        "qr_display": _qr_display(qr_path),
        "cookie_names": sorted(cookie.name for cookie in session.cookies),
    }


def _complete_qr_login_sync(qr_id: str, timeout_seconds: int) -> dict[str, Any]:
    session = qr_login_state.session if qr_login_state.qr_id == qr_id else None
    session = session or _load_qr_session(qr_id)
    deadline = time.time() + timeout_seconds
    scanned = False

    while time.time() < deadline:
        if not scanned:
            scan_resp = session.get(
                f"https://www.zhipin.com/wapi/zppassport/qrcode/scan?uuid={qr_id}",
                timeout=35,
            )
            if scan_resp.status_code == 200:
                payload = scan_resp.json()
                scanned = bool(payload.get("scaned"))
                if not scanned and payload.get("msg") != "timeout":
                    time.sleep(1)
            continue

        confirm_resp = session.get(
            f"https://www.zhipin.com/wapi/zppassport/qrcode/scanLogin?qrId={qr_id}&status=1",
            timeout=35,
        )
        if confirm_resp.status_code == 200:
            dispatcher_resp = session.get(
                "https://www.zhipin.com/wapi/zppassport/qrcode/dispatcher",
                params={"qrId": qr_id, "pk": "header-login", "fp": _dispatcher_fp()},
                allow_redirects=False,
                timeout=20,
            )
            dispatcher_resp.raise_for_status()
            cookie_header = _requests_cookie_header(session)
            return {
                "status": "confirmed",
                "qr_id": qr_id,
                "cookie_header": cookie_header,
                "cookie_names": _safe_cookie_names(cookie_header),
            }

        try:
            payload = confirm_resp.json()
        except Exception:
            payload = {}
        if payload.get("msg") != "timeout":
            time.sleep(1)

    return {
        "status": "timeout",
        "qr_id": qr_id,
        "scanned": scanned,
        "cookie_names": sorted(cookie.name for cookie in session.cookies),
    }


def _keyword_terms(keyword: str, extra_keywords: list[str] | None) -> list[str]:
    terms = [keyword.strip()]
    terms.extend(term.strip() for term in (extra_keywords or []) if term.strip())
    terms.extend(re.findall(r"[A-Za-z0-9+#.]+|[\u4e00-\u9fff]{2,}", keyword))

    seen: set[str] = set()
    unique_terms = []
    for term in terms:
        lowered = term.lower()
        if lowered and lowered not in seen:
            unique_terms.append(term)
            seen.add(lowered)
    return unique_terms


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term.lower() in lowered]


def _parse_publish_date(text: str, today: date | None = None) -> tuple[str | None, str | None]:
    """Parse common Boss publish-time fragments into an ISO date.

    Boss sometimes shows relative phrases such as "3天前", "刚刚", or "今日发布";
    older jobs may only expose "07月02日". If no publish text is visible, return
    ``(None, None)`` so callers can choose whether to keep or drop the item.
    """

    today = today or date.today()
    compact = re.sub(r"\s+", "", text)

    patterns = [
        r"((?:刚刚|今天|今日|昨天|昨日|前天)发布?)",
        r"((?:\d+)分钟前发布?)",
        r"((?:\d+)小时前发布?)",
        r"((?:\d+)天前发布?)",
        r"((?:\d{1,2})月(?:\d{1,2})日发布?)",
        r"(发布于(?:\d{1,2})月(?:\d{1,2})日)",
        r"((?:\d{4})-(?:\d{1,2})-(?:\d{1,2}))",
    ]

    publish_text = None
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            publish_text = match.group(1)
            break

    if not publish_text:
        return None, None

    if re.search(r"刚刚|今天|今日|分钟|小时", publish_text):
        parsed = today
    elif re.search(r"昨天|昨日", publish_text):
        parsed = today - timedelta(days=1)
    elif "前天" in publish_text:
        parsed = today - timedelta(days=2)
    elif day_match := re.search(r"(\d+)天前", publish_text):
        parsed = today - timedelta(days=int(day_match.group(1)))
    elif md_match := re.search(r"(\d{1,2})月(\d{1,2})日", publish_text):
        month = int(md_match.group(1))
        day = int(md_match.group(2))
        year = today.year
        parsed = date(year, month, day)
        if parsed > today:
            parsed = date(year - 1, month, day)
    elif ymd_match := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", publish_text):
        parsed = date(
            int(ymd_match.group(1)),
            int(ymd_match.group(2)),
            int(ymd_match.group(3)),
        )
    else:
        return publish_text, None

    return publish_text, parsed.isoformat()


def _is_recent(publish_date: str | None, days: int) -> bool:
    if not publish_date:
        return False
    parsed = datetime.strptime(publish_date, "%Y-%m-%d").date()
    return parsed >= date.today() - timedelta(days=days)


def _chrome_channel() -> str | None:
    """Prefer the user's real Google Chrome over the bundled test Chromium.

    Set BOSS_MCP_BROWSER_CHANNEL=chromium to force the bundled build.
    """

    channel = os.getenv("BOSS_MCP_BROWSER_CHANNEL", "chrome").strip()
    return channel or None


async def _launch_context(headless: bool) -> BrowserContext:
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(profile),
        "headless": headless,
        "viewport": {"width": 1440, "height": 1000},
        "locale": "zh-CN",
    }

    channel = _chrome_channel()
    playwright = await async_playwright().start()
    try:
        if channel and channel != "chromium":
            context = await playwright.chromium.launch_persistent_context(
                channel=channel, **launch_kwargs
            )
        else:
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        # Fall back to the bundled Chromium if the requested channel is missing.
        context = await playwright.chromium.launch_persistent_context(**launch_kwargs)

    context._boss_playwright = playwright  # type: ignore[attr-defined]
    return context


async def _close_context(context: BrowserContext) -> None:
    playwright = getattr(context, "_boss_playwright", None)
    await context.close()
    if playwright:
        await playwright.stop()


async def _goto(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass


def _looks_like_login_page(url: str) -> bool:
    return (
        _looks_blank(url)
        or "/web/user/" in url
        or "passport" in url
        or "login" in url
        or "security-check" in url
        or "verify.html" in url
    )


def _looks_blank(url: str) -> bool:
    return url == "about:blank" or not url


# Cookies Boss only issues to an authenticated geek (job-seeker) session. The
# anonymous profile carries tracking cookies (__zp_stoken__, __g, __l, ...) but
# none of these login tokens.
BOSS_LOGIN_COOKIE_MARKERS = {"geek_zp_token", "bst", "wt2", "t"}


def _has_login_cookie(cookie_names: list[str]) -> bool:
    return any(name in BOSS_LOGIN_COOKIE_MARKERS for name in cookie_names)


def _on_job_search_page(url: str) -> bool:
    return "/web/geek/job" in url and not _looks_like_login_page(url)


async def _apply_cookie_header(context: BrowserContext, cookie_header: str) -> int:
    cookies = _cookie_header_to_playwright_cookies(cookie_header)
    if cookies:
        await context.add_cookies(cookies)
    return len(cookies)


async def _apply_env_cookies(context: BrowserContext) -> int:
    cookie_header = os.getenv(BOSS_COOKIE_ENV, "").strip()
    if not cookie_header:
        return 0
    return await _apply_cookie_header(context, cookie_header)


async def _probe_login_status(context: BrowserContext) -> dict[str, Any]:
    """Positively confirm a logged-in Boss session.

    Boss may bounce the job-search URL to ``about:blank`` during a security
    check, so we retry the navigation a couple of times. Login is only reported
    when the page actually settles on the job-search URL AND the context holds a
    known login-token cookie. Landing on ``about:blank`` or the login page counts
    as *not* logged in rather than being inferred as success.
    """

    page = await context.new_page()
    try:
        current_url = ""
        for _ in range(3):
            await _goto(page, BOSS_JOB_SEARCH_URL)
            current_url = page.url
            if not _looks_blank(current_url):
                break
            await page.wait_for_timeout(1500)

        cookies = await context.cookies("https://www.zhipin.com")
        cookie_names = sorted(cookie["name"] for cookie in cookies)
        has_login_cookie = _has_login_cookie(cookie_names)
        on_search_page = _on_job_search_page(current_url)
        return {
            "likely_logged_in": on_search_page and has_login_cookie,
            "on_job_search_page": on_search_page,
            "has_login_cookie": has_login_cookie,
            "cookie_names": cookie_names,
            "current_url": current_url,
        }
    finally:
        await page.close()


async def _scroll_job_list(page: Page) -> None:
    for _ in range(4):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(900)


async def _extract_jobs_from_page(page: Page, terms: list[str]) -> list[JobPosting]:
    raw_jobs: list[dict[str, Any]] = await page.evaluate(
        """
        () => {
          const selectors = [
            '.job-card-wrapper',
            'li.job-card-wrapper',
            '.job-list-box li',
            '.job-card-body'
          ];
          const seen = new Set();
          const cards = [];
          for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
              if (!seen.has(node)) {
                seen.add(node);
                cards.push(node);
              }
            }
          }

          const pick = (root, selectors) => {
            for (const selector of selectors) {
              const node = root.querySelector(selector);
              const text = node?.innerText?.trim();
              if (text) return text;
            }
            return null;
          };

          return cards.map((card) => {
            const link = card.querySelector('a[href*="/job_detail/"]');
            const href = link ? new URL(link.getAttribute('href'), location.origin).href : null;
            const tags = Array.from(
              card.querySelectorAll('.tag-list li, .job-tag-list li, .job-card-footer li, .job-labels span')
            ).map((node) => node.innerText.trim()).filter(Boolean);

            return {
              title: pick(card, ['.job-name', '.job-title', '.name', 'a[href*="/job_detail/"]']),
              company: pick(card, ['.company-name', '.boss-name', '.brand-name']),
              salary: pick(card, ['.salary', '.red']),
              location: pick(card, ['.job-area', '.area-desc', '.location']),
              experience: pick(card, ['.job-info .tag-list li:nth-child(1)', '.job-limit p']),
              education: pick(card, ['.job-info .tag-list li:nth-child(2)']),
              tags,
              url: href,
              rawText: card.innerText.trim()
            };
          }).filter((item) => item.rawText && item.rawText.length > 10);
        }
        """
    )

    jobs: list[JobPosting] = []
    seen_keys: set[str] = set()
    for item in raw_jobs:
        raw_text = item.get("rawText") or ""
        publish_text, publish_date = _parse_publish_date(raw_text)
        title = item.get("title") or _first_line(raw_text) or ""
        matched = _matched_terms(raw_text, terms)
        key = item.get("url") or f"{title}|{item.get('company')}|{item.get('salary')}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        jobs.append(
            JobPosting(
                title=title,
                company=item.get("company"),
                salary=item.get("salary"),
                location=item.get("location"),
                experience=item.get("experience"),
                education=item.get("education"),
                tags=item.get("tags") or [],
                publish_text=publish_text,
                publish_date=publish_date,
                url=item.get("url"),
                matched_keywords=matched,
                raw_text=raw_text,
            )
        )
    return jobs


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return None


async def _click_next_page(page: Page) -> bool:
    candidates = [
        ".options-pages a:has-text('下一页')",
        ".page a:has-text('下一页')",
        "a:has-text('下一页')",
        "button:has-text('下一页')",
    ]
    for selector in candidates:
        locator = page.locator(selector).last
        try:
            if await locator.count() and await locator.is_visible() and await locator.is_enabled():
                await locator.click(timeout=5_000)
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def _connect_chrome_debug(debug_url: str) -> BrowserContext:
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(debug_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context._boss_playwright = playwright  # type: ignore[attr-defined]
        context._boss_browser = browser  # type: ignore[attr-defined]
        return context
    except Exception:
        await playwright.stop()
        raise


async def _close_chrome_debug_context(context: BrowserContext) -> None:
    playwright = getattr(context, "_boss_playwright", None)
    # Do not call browser.close() for a CDP connection: that closes the user's
    # real Chrome. Stopping Playwright detaches our client from the debugging
    # connection and leaves Chrome running.
    if playwright:
        await playwright.stop()


def _find_existing_boss_page(context: BrowserContext) -> Page | None:
    candidates = []
    for page in context.pages:
        url = page.url or ""
        if "zhipin.com" in url and not _looks_blank(url):
            candidates.append(page)
    for page in candidates:
        if "/web/geek/job" in (page.url or ""):
            return page
    return candidates[0] if candidates else None


async def _extract_jobs_from_chrome_debug(
    keyword: str,
    city: str,
    city_code: str | None,
    days: int,
    pages: int,
    extra_keywords: list[str] | None,
    require_publish_date: bool,
    debug_url: str,
    allow_navigation: bool,
) -> dict[str, Any]:
    terms = _keyword_terms(keyword, extra_keywords)
    search_url = _build_search_url(keyword, city, city_code)
    context = await _connect_chrome_debug(debug_url)
    try:
        page = _find_existing_boss_page(context)
        if page is None:
            return {
                "status": "manual_navigation_required",
                "message": "No existing Boss Zhipin page was found in the connected Chrome session. Open the source_url manually in that Chrome, wait for results to render, then call again.",
                "source_url": search_url,
            }

        if "/web/geek/job" not in page.url:
            if not allow_navigation:
                return {
                    "status": "manual_navigation_required",
                    "message": "A Boss page was found, but it is not a job-search results page. Open the source_url manually in that Chrome, wait for results to render, then call again.",
                    "current_url": page.url,
                    "source_url": search_url,
                }
            await _goto(page, search_url)

        if _looks_like_login_page(page.url):
            return {
                "status": "login_required",
                "message": "The connected Chrome session is not on an accessible Boss job-search page.",
                "current_url": page.url,
                "source_url": search_url,
            }

        collected: list[JobPosting] = []
        for page_number in range(1, pages + 1):
            await _scroll_job_list(page)
            collected.extend(await _extract_jobs_from_page(page, terms))
            if page_number < pages:
                has_next = await _click_next_page(page)
                if not has_next:
                    break

        filtered: list[JobPosting] = []
        for job in collected:
            text = job.raw_text or ""
            matches_keyword = bool(job.matched_keywords) or keyword.lower() in text.lower()
            has_recent_date = _is_recent(job.publish_date, days)
            if matches_keyword and (has_recent_date or (not require_publish_date and not job.publish_date)):
                filtered.append(job)

        return {
            "status": "success",
            "keyword": keyword,
            "city": city,
            "city_code": _city_code(city, city_code),
            "days": days,
            "source_url": search_url,
            "current_url": page.url,
            "total_collected": len(collected),
            "total_matched": len(filtered),
            "jobs": [asdict(job) for job in filtered],
        }
    finally:
        await _close_chrome_debug_context(context)


@mcp.tool()
async def open_boss_login(
    ctx: Context,
    headless: bool = False,
    wait_seconds: int = 180,
) -> str:
    """Open Boss Zhipin login page with the persistent browser profile.

    Keep the opened browser window until you finish scanning the QR code. The
    profile is stored locally and reused by search tools.
    """

    if wait_seconds < 0 or wait_seconds > 900:
        raise ValueError("wait_seconds must be between 0 and 900")

    await ctx.info("Opening Boss Zhipin login page...")
    context = await _launch_context(headless=headless)
    try:
        page = await context.new_page()
        await _goto(page, BOSS_LOGIN_URL)
        await ctx.info(f"Boss login page opened at {page.url}; keeping browser open for {wait_seconds}s")
        if wait_seconds:
            await page.wait_for_timeout(wait_seconds * 1000)
        cookies = await context.cookies("https://www.zhipin.com")
        cookie_names = sorted(cookie["name"] for cookie in cookies)
        return json.dumps(
            {
                "status": "opened",
                "message": "Boss login page was opened and held for manual QR scan.",
                "profile_dir": _profile_display(),
                "url": page.url,
                "cookie_names": cookie_names,
                "has_login_cookie": _has_login_cookie(cookie_names),
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        await _close_context(context)


@mcp.tool()
async def get_boss_login_status(ctx: Context, headless: bool = True) -> str:
    """Check whether the persistent Boss browser profile appears logged in."""

    await ctx.info("Checking Boss login status...")
    context = await _launch_context(headless=headless)
    try:
        applied_env_cookies = await _apply_env_cookies(context)
        status = await _probe_login_status(context)
        return json.dumps(
            {
                "likely_logged_in": status["likely_logged_in"],
                "cookie_names": status["cookie_names"],
                "applied_env_cookies": applied_env_cookies,
                "profile_dir": _profile_display(),
                "current_url": status["current_url"],
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        await _close_context(context)


@mcp.tool()
async def start_boss_qr_login(ctx: Context) -> str:
    """Generate a Boss Zhipin QR login image without opening a browser page.

    This is the preferred login start method when browser-based login is
    immediately redirected to about:blank. Scan the returned PNG with the Boss
    app, then call complete_boss_qr_login.
    """

    await ctx.info("Generating Boss QR login image...")
    try:
        result = await asyncio.to_thread(_start_qr_login_sync)
        return json.dumps(
            {
                "status": "qr_generated",
                "message": "Scan the QR image with the Boss app, then call complete_boss_qr_login.",
                "qr_id": result["qr_id"],
                "qr_image_path": result["qr_display"],
                "cookie_names": result["cookie_names"],
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        await ctx.error(f"Failed to generate Boss QR login image: {exc}")
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


@mcp.tool()
async def complete_boss_qr_login(
    ctx: Context,
    qr_id: str | None = None,
    timeout_seconds: int = 180,
    verify: bool = True,
) -> str:
    """Wait for Boss QR scan confirmation and import the resulting cookies."""

    if timeout_seconds < 30 or timeout_seconds > 600:
        raise ValueError("timeout_seconds must be between 30 and 600")

    qr_id = qr_id or qr_login_state.qr_id
    if not qr_id:
        return json.dumps(
            {
                "status": "missing_qr",
                "message": "Call start_boss_qr_login first, or pass qr_id explicitly.",
            },
            ensure_ascii=False,
            indent=2,
        )

    await ctx.info("Waiting for Boss QR scan confirmation...")
    result = await asyncio.to_thread(_complete_qr_login_sync, qr_id, timeout_seconds)
    if result["status"] != "confirmed":
        return json.dumps(result, ensure_ascii=False, indent=2)

    cookie_header = result.get("cookie_header", "")
    context = await _launch_context(headless=True)
    try:
        imported = await _apply_cookie_header(context, cookie_header)
        verification = await _probe_login_status(context) if verify else None
        return json.dumps(
            {
                "status": "success" if not verification or verification["likely_logged_in"] else "confirmed_but_not_verified",
                "message": "QR login confirmed and cookies imported.",
                "qr_id": qr_id,
                "imported_cookie_count": imported,
                "imported_cookie_names": result["cookie_names"],
                "profile_dir": _profile_display(),
                "verification": verification,
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        await _close_context(context)


@mcp.tool()
async def login_boss_interactive(
    ctx: Context,
    timeout_seconds: int = 300,
    qr_wait_seconds: int = 90,
    check_interval_seconds: int = 5,
) -> str:
    """Open a visible Boss login window and wait until the session can open job search.

    The tool first gives the user a quiet QR-login window. It never opens a
    replacement window after Boss redirects the page to about:blank, because that
    makes QR login unusable; instead it returns a clear status so the caller can
    fall back to cookie import.
    """

    if timeout_seconds < 30 or timeout_seconds > 900:
        raise ValueError("timeout_seconds must be between 30 and 900")
    if qr_wait_seconds < 10 or qr_wait_seconds > timeout_seconds:
        raise ValueError("qr_wait_seconds must be between 10 and timeout_seconds")
    if check_interval_seconds < 2 or check_interval_seconds > 30:
        raise ValueError("check_interval_seconds must be between 2 and 30")

    await ctx.info("Opening visible Boss login window...")
    context = await _launch_context(headless=False)
    try:
        applied_env_cookies = await _apply_env_cookies(context)
        page = await context.new_page()
        await _goto(page, BOSS_LOGIN_URL)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        quiet_login_deadline = loop.time() + qr_wait_seconds
        last_status: dict[str, Any] | None = None

        while loop.time() < deadline:
            if page.is_closed():
                return json.dumps(
                    {
                        "status": "browser_closed",
                        "message": "The Boss login window was closed before login completed.",
                        "applied_env_cookies": applied_env_cookies,
                        "profile_dir": _profile_display(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            if _looks_blank(page.url):
                cookies = await context.cookies("https://www.zhipin.com")
                cookie_names = sorted(cookie["name"] for cookie in cookies)
                return json.dumps(
                    {
                        "status": "blank_redirect",
                        "message": "Boss redirected the login window to about:blank before a login cookie was detected. The tool will not reopen windows automatically. Use import_boss_cookies with cookies copied from a normal browser session.",
                        "applied_env_cookies": applied_env_cookies,
                        "profile_dir": _profile_display(),
                        "cookie_names": cookie_names,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            cookies = await context.cookies("https://www.zhipin.com")
            cookie_names = sorted(cookie["name"] for cookie in cookies)
            has_login_cookie = _has_login_cookie(cookie_names)

            if loop.time() < quiet_login_deadline and not has_login_cookie:
                remaining = int(quiet_login_deadline - loop.time())
                await ctx.info(f"Waiting for Boss QR login before probing... {remaining}s left")
                await page.wait_for_timeout(check_interval_seconds * 1000)
                continue

            if has_login_cookie:
                await ctx.info("Login cookie detected; probing job search...")
            elif _looks_like_login_page(page.url):
                await ctx.info("QR wait elapsed; probing Boss login status...")
            else:
                await ctx.info("Boss page is no longer on the login URL; probing job search...")

            last_status = await _probe_login_status(context)
            if last_status["likely_logged_in"]:
                return json.dumps(
                    {
                        "status": "success",
                        "message": "Boss login is ready. Job search is accessible.",
                        "applied_env_cookies": applied_env_cookies,
                        "profile_dir": _profile_display(),
                        **last_status,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            await page.wait_for_timeout(check_interval_seconds * 1000)

        return json.dumps(
            {
                "status": "timeout",
                "message": "Timed out before Boss job search became accessible. If browser login keeps failing, use import_boss_cookies with cookies copied from a normal browser session.",
                "applied_env_cookies": applied_env_cookies,
                "profile_dir": _profile_display(),
                "qr_wait_seconds": qr_wait_seconds,
                "last_status": last_status,
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        await _close_context(context)


@mcp.tool()
async def import_boss_cookies(
    ctx: Context,
    cookie_header: str,
    verify: bool = True,
) -> str:
    """Import a Boss Zhipin Cookie header into the persistent browser profile.

    Copy the Cookie request header from a logged-in Boss Zhipin browser session
    and pass it here. The cookie values are not echoed back in the response.
    """

    cookie_header = cookie_header.strip()
    if not cookie_header:
        raise ValueError("cookie_header cannot be empty")

    await ctx.info("Importing Boss cookies into the persistent browser profile...")
    context = await _launch_context(headless=True)
    try:
        imported = await _apply_cookie_header(context, cookie_header)
        status = await _probe_login_status(context) if verify else None
        return json.dumps(
            {
                "status": "success" if not status or status["likely_logged_in"] else "imported_but_not_verified",
                "imported_cookie_count": imported,
                "imported_cookie_names": _safe_cookie_names(cookie_header),
                "profile_dir": _profile_display(),
                "verification": status,
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        await _close_context(context)


@mcp.tool()
async def search_boss_jobs(
    ctx: Context,
    keyword: str,
    city: str = "全国",
    city_code: str | None = None,
    days: int = 30,
    pages: int = 3,
    extra_keywords: list[str] | None = None,
    require_publish_date: bool = True,
    headless: bool = True,
) -> str:
    """Search Boss Zhipin jobs by keyword and keep postings from recent days.

    Args:
        keyword: Target role, for example "AI解决方案岗".
        city: City name. Common values include 全国、北京、上海、深圳、杭州.
        city_code: Optional Boss city code. If provided, it overrides city.
        days: Keep jobs whose visible publish date is within this many days.
        pages: Number of search result pages to inspect.
        extra_keywords: Extra terms that should be considered matching signals.
        require_publish_date: Drop cards without a visible publish date when true.
        headless: Run browser headless. Set false when login or verification is needed.
    """

    if not keyword.strip():
        raise ValueError("keyword cannot be empty")
    if days < 1:
        raise ValueError("days must be >= 1")
    if pages < 1 or pages > 10:
        raise ValueError("pages must be between 1 and 10")

    terms = _keyword_terms(keyword, extra_keywords)
    search_url = _build_search_url(keyword, city, city_code)
    await ctx.info(f"Searching Boss jobs: keyword={keyword}, city={city}, days={days}, pages={pages}")

    context = await _launch_context(headless=headless)
    try:
        applied_env_cookies = await _apply_env_cookies(context)
        page = await context.new_page()
        await _goto(page, search_url)

        if _looks_like_login_page(page.url):
            return json.dumps(
                {
                    "status": "login_required",
                    "message": "Boss Zhipin redirected to the login page. Run login_boss_interactive, import_boss_cookies, or set BOSS_COOKIE with a logged-in Cookie header.",
                    "applied_env_cookies": applied_env_cookies,
                    "current_url": page.url,
                    "source_url": search_url,
                },
                ensure_ascii=False,
                indent=2,
            )

        collected: list[JobPosting] = []
        for page_number in range(1, pages + 1):
            await ctx.info(f"Extracting page {page_number}...")
            await _scroll_job_list(page)
            collected.extend(await _extract_jobs_from_page(page, terms))

            if page_number < pages:
                has_next = await _click_next_page(page)
                if not has_next:
                    break

        filtered: list[JobPosting] = []
        for job in collected:
            text = job.raw_text or ""
            matches_keyword = bool(job.matched_keywords) or keyword.lower() in text.lower()
            has_recent_date = _is_recent(job.publish_date, days)
            if matches_keyword and (has_recent_date or (not require_publish_date and not job.publish_date)):
                filtered.append(job)

        response = {
            "status": "success",
            "keyword": keyword,
            "city": city,
            "city_code": _city_code(city, city_code),
            "days": days,
            "source_url": search_url,
            "total_collected": len(collected),
            "total_matched": len(filtered),
            "jobs": [asdict(job) for job in filtered],
            "notes": [
                "If no jobs are returned, run open_boss_login(headless=false) and finish login/verification.",
                "Boss may hide publish dates on some cards; set require_publish_date=false to keep undated matches.",
            ],
        }
        return json.dumps(response, ensure_ascii=False, indent=2)
    finally:
        await _close_context(context)


@mcp.tool()
async def search_boss_jobs_chrome_debug(
    ctx: Context,
    keyword: str,
    city: str = "全国",
    city_code: str | None = None,
    days: int = 30,
    pages: int = 3,
    extra_keywords: list[str] | None = None,
    require_publish_date: bool = True,
    debug_url: str = "http://127.0.0.1:9222",
    allow_navigation: bool = False,
) -> str:
    """Search Boss jobs by attaching to the user's real Chrome via CDP.

    Start Chrome with remote debugging enabled, log in normally, then call this
    tool. By default it only reads the existing Boss tab and will not navigate
    or open pages, because Boss may blank the page when automation navigates.
    """

    if not keyword.strip():
        raise ValueError("keyword cannot be empty")
    if days < 1:
        raise ValueError("days must be >= 1")
    if pages < 1 or pages > 10:
        raise ValueError("pages must be between 1 and 10")

    await ctx.info(f"Connecting to Chrome debug endpoint: {debug_url}")
    try:
        result = await _extract_jobs_from_chrome_debug(
            keyword=keyword,
            city=city,
            city_code=city_code,
            days=days,
            pages=pages,
            extra_keywords=extra_keywords,
            require_publish_date=require_publish_date,
            debug_url=debug_url,
            allow_navigation=allow_navigation,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            {
                "status": "chrome_debug_unavailable",
                "message": "Could not connect to Chrome DevTools. Close Chrome and start it with remote debugging enabled, then log in to Boss normally.",
                "debug_url": debug_url,
                "start_command": "open -na 'Google Chrome' --args --remote-debugging-port=9222 --user-data-dir=/tmp/boss-mcp-chrome-debug",
                "error": str(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


def main() -> None:
    asyncio.run(mcp.run_async())


if __name__ == "__main__":
    main()
