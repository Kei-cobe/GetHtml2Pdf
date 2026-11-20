from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageChops
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Page, async_playwright

TIMEZONE = ZoneInfo("Asia/Shanghai")
NAVIGATION_TIMEOUT_MS = 60_000
MAX_SCROLL_STEPS = 100


@dataclass(frozen=True)
class Task:
    name: str
    url: str
    frame_selector: str | None = None
    remove_selectors: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: dict[str, Any], index: int = 0) -> "Task":
        url = validate_http_url(str(data.get("url", "")))
        name = str(data.get("name") or "").strip() or f"网页任务 {index + 1}"
        frame_selector = str(data.get("frame_selector") or "").strip() or None
        remove_selectors = tuple(str(item) for item in data.get("remove_selectors", []))
        return cls(name, url, frame_selector, remove_selectors)


@dataclass(frozen=True)
class CaptureResult:
    task: Task
    pdf_path: Path
    final_url: str


def validate_http_url(value: str) -> str:
    url = value.strip()
    if not url or any(char.isspace() for char in url):
        raise ValueError("网址为空或包含空白字符")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("仅支持有效的 http/https 网址")
        if parsed.username or parsed.password:
            raise ValueError("网址中不能包含账号或密码")
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"无效网址：{url}") from exc
    return url


def parse_manual_tasks(value: str, saved_tasks: list[Task] | None = None) -> list[Task]:
    entries = [item.strip() for item in re.split(r"[;，,\n]+", value) if item.strip()]
    tasks = []
    for index, entry in enumerate(entries):
        if "|" in entry:
            name, url = entry.split("|", 1)
            task = Task.from_mapping({"name": name.strip(), "url": url.strip()}, index)
        elif re.match(r"^https?://", entry, re.I):
            task = Task.from_mapping({"name": f"手动任务 {index + 1}", "url": entry}, index)
        else:
            name = entry.strip()
            match = next((t for t in (saved_tasks or []) if t.name.strip() == name), None)
            if not match:
                raise ValueError(f"未找到备注名为「{name}」的任务，请先在定时任务里添加")
            task = match
        tasks.append(task)
    return tasks


def load_tasks(path: Path, only_enabled: bool = True) -> list[Task]:
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    tasks = []
    for index, item in enumerate(config.get("tasks", [])):
        if not only_enabled or item.get("enabled", True):
            tasks.append(Task.from_mapping(item, index))
    return tasks


def safe_name(text: str, limit: int = 60) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "-", text).strip("-._")[:limit] or "webpage"


def safe_pdf_path(output_dir: Path, task: Task) -> Path:
    name = safe_name(task.name)
    timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(task.url.encode("utf-8")).hexdigest()[:8]
    return output_dir / f"{name}-{timestamp}-{digest}.pdf"


def origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def same_origin(first: str, second: str) -> bool:
    try:
        return origin(first) == origin(second)
    except ValueError:
        return False


async def wait_for_load(page: Page) -> None:
    try:
        await page.wait_for_load_state("load", timeout=30_000)
    except PlaywrightTimeoutError:
        logging.warning("页面 load 事件等待超时，将继续处理已加载内容")


async def navigate(page: Page, url: str) -> None:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    if response and response.status >= 400:
        raise RuntimeError(f"网页返回 HTTP {response.status}")
    await wait_for_load(page)


async def find_content_frame_url(page: Page, selector: str | None) -> str | None:
    if selector:
        element = await page.wait_for_selector(selector, state="attached", timeout=30_000)
        if not element:
            raise RuntimeError(f"找不到主内容 iframe：{selector}")
        src = await element.get_attribute("src")
        if not src:
            raise RuntimeError("主内容 iframe 没有 src")
        frame_url = urljoin(page.url, src)
        if not same_origin(page.url, frame_url):
            raise RuntimeError("为避免误入第三方页面，仅支持同源主内容 iframe")
        return frame_url

    viewport_height = page.viewport_size["height"] if page.viewport_size else 800
    candidates: list[tuple[int, str]] = []
    for element in await page.query_selector_all("iframe[src]"):
        src = await element.get_attribute("src")
        if not src:
            continue
        frame_url = urljoin(page.url, src)
        if not same_origin(page.url, frame_url):
            continue
        box = await element.bounding_box()
        if not box or box["width"] < 300:
            continue
        content_height = int(box["height"])
        frame = await element.content_frame()
        if frame:
            try:
                content_height = max(
                    content_height,
                    await frame.evaluate("() => document.documentElement.scrollHeight"),
                )
            except Exception:
                pass
        if content_height >= max(1_200, int(viewport_height * 1.5)):
            candidates.append((int(content_height * box["width"]), frame_url))
    return max(candidates)[1] if candidates else None


async def auto_scroll(page: Page) -> None:
    previous_height = -1
    stable_rounds = 0
    for _ in range(MAX_SCROLL_STEPS):
        state = await page.evaluate(
            """() => {
                const root = document.scrollingElement || document.documentElement;
                const containers = Array.from(document.querySelectorAll('body *')).filter(el => {
                    const style = getComputedStyle(el);
                    return el.clientHeight > 50 && el.scrollHeight > el.clientHeight + 100
                        && /(auto|scroll)/.test(style.overflowY);
                });
                return {
                    height: root.scrollHeight,
                    atBottom: root.scrollTop + innerHeight >= root.scrollHeight - 20,
                    containersDone: containers.every(el => el.scrollTop + el.clientHeight >= el.scrollHeight - 20)
                };
            }"""
        )
        if state["atBottom"] and state["containersDone"] and state["height"] == previous_height:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        previous_height = state["height"]
        await page.evaluate(
            """() => {
                document.querySelectorAll('img').forEach(img => img.loading = 'eager');
                window.scrollBy(0, Math.max(innerHeight * 0.85, 500));
                document.querySelectorAll('body *').forEach(el => {
                    const style = getComputedStyle(el);
                    if (el.clientHeight > 50 && el.scrollHeight > el.clientHeight + 100
                            && /(auto|scroll)/.test(style.overflowY)) {
                        el.setAttribute('data-pdf-scroll-container', '');
                        el.scrollTop = Math.min(el.scrollTop + el.clientHeight * 0.85, el.scrollHeight);
                    }
                });
            }"""
        )
        await page.wait_for_timeout(450)
    else:
        logging.warning("已达到最大滚动步数，将保存当前已加载内容")


async def prepare_document(page: Page, task: Task) -> None:
    await auto_scroll(page)
    await page.evaluate(
        """(removeSelectors) => {
            for (const selector of removeSelectors) {
                document.querySelectorAll(selector).forEach(el => el.remove());
            }
            document.querySelectorAll('body *').forEach(el => {
                if (getComputedStyle(el).position === 'fixed'
                        && !el.hasAttribute('data-pdf-scroll-container')) {
                    el.style.setProperty('display', 'none', 'important');
                }
            });
            document.querySelectorAll('img').forEach(img => {
                img.loading = 'eager';
                const lazySrc = img.getAttribute('data-src') || img.getAttribute('data-original');
                if (!img.getAttribute('src') && lazySrc) img.src = lazySrc;
            });
            window.scrollTo(0, 0);
            document.querySelectorAll('[data-pdf-scroll-container]').forEach(el => el.scrollTop = 0);
        }""",
        list(task.remove_selectors),
    )
    await page.add_style_tag(
        content="""
            *, *::before, *::after {
                animation: none !important;
                transition: none !important;
                caret-color: transparent !important;
            }
            [data-pdf-scroll-container] {
                height: auto !important;
                max-height: none !important;
                overflow: visible !important;
            }
            html, body {
                height: auto !important;
                overflow: visible !important;
            }
        """
    )
    try:
        await page.wait_for_function(
            "() => Array.from(document.images).every(img => img.complete)", timeout=30_000
        )
    except PlaywrightTimeoutError:
        incomplete = await page.evaluate(
            "() => Array.from(document.images).filter(img => !img.complete).length"
        )
        logging.warning("仍有 %s 张图片未加载完成，将先生成 PDF", incomplete)
    try:
        await asyncio.wait_for(
            page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()"),
            timeout=15,
        )
    except asyncio.TimeoutError:
        logging.warning("字体等待超时，将继续生成 PDF")


A4_WIDTH_MM = 210
A4_WIDTH_PX = round(A4_WIDTH_MM / 25.4 * 96)  # 与 A4 纸宽 1:1 对应的 CSS 像素
MAX_PDF_HEIGHT_IN = 200.0  # Chromium 单页高度上限
MIN_PDF_SCALE = 0.1
MAX_PREVIEW_PX = 60000  # 单张预览长图的物理像素高度上限（更高则整体等比缩小）
PREVIEW_SEGMENT_CSS = 20000  # 单次截图的 CSS 像素高度（2 倍 DPI 下 40000 物理像素，避开编码上限）
PREVIEW_DEVICE_SCALE = 2  # 预览图按 2 倍 DPI 截图，手机高清屏不模糊


def page_height_js() -> str:
    return """() => {
        const se = document.scrollingElement || document.documentElement;
        const body = document.body;
        const h = Math.max(se ? se.scrollHeight : 0, body ? body.scrollHeight : 0);
        return { height: Math.max(1, Math.ceil(h)) };
    }"""


def trim_image_whitespace(path: Path, padding: int = 12, threshold: int = 18) -> bool:
    """裁掉截图四周的空白边并补回少量边距，返回是否发生了裁剪。

    网页内容未占满视口时，截图右侧/底部会有大片白边（内容居中或 body 被撑高），
    直接展示会导致手机上图片右侧空白、底部拖着长白条。
    """
    im = Image.open(path).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im, bg).convert("L").point(
        lambda p: 255 if p > threshold else 0
    )
    bbox = diff.getbbox()
    if not bbox:
        return False  # 全白图，保留原样
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(im.width, bbox[2] + padding)
    bottom = min(im.height, bbox[3] + padding)
    if right - left >= im.width and bottom - top >= im.height:
        return False  # 没有可裁的白边
    logging.info(
        "裁剪预览图白边：%dx%d -> %dx%d",
        im.width, im.height, right - left, bottom - top,
    )
    im.crop((left, top, right, bottom)).save(path, "JPEG", quality=82)
    return True


async def save_preview_image(page: Any, target_dir: Path, prefix: int) -> str | None:
    """把整页截成一张不分段的长条 JPG（与长条 PDF 一致），返回文件名。"""
    metrics = await page.evaluate(page_height_js())
    total = int(metrics.get("height") or 0)
    if total <= 0:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix:02d}.jpg"
    target = target_dir / filename
    await page.evaluate("() => window.scrollTo(0, 0)")
    viewport_width = A4_WIDTH_PX
    try:
        client = await page.context.new_cdp_session(page)
        layout = await client.send("Page.getLayoutMetrics")
        size = layout.get("cssContentSize") or layout.get("contentSize") or {}
        page_w, page_h = int(size.get("width") or 0), int(size.get("height") or 0)
        if page_w > 0:
            viewport_width = min(2400, page_w)
        if page_h > 0:
            total = min(total, page_h)  # 裁剪区域不能超出 CDP 认定的页面内容，否则报 outside
    except Exception:
        pass

    # 超高页面一次性截图会触发 Chromium "Unable to encode screenshot"，
    # 因此按固定高度分块截取（每块仍在 2 倍 DPI 下），再纵向拼成一张完整长图。
    seg_css = PREVIEW_SEGMENT_CSS
    parts: list[Path] = []
    for index, top in enumerate(range(0, total, seg_css)):
        height = min(seg_css, total - top)
        part = target_dir / f"{prefix:02d}-part{index:02d}.jpg"
        await page.screenshot(
            path=str(part),
            clip={"x": 0, "y": top, "width": viewport_width, "height": height},
            type="jpeg",
            quality=82,
            timeout=120_000,
        )
        parts.append(part)

    if len(parts) == 1:
        parts[0].replace(target)
    else:
        logging.info("内容 %d px，分 %d 块截图后拼接为单张长图", total, len(parts))
        images = [Image.open(part) for part in parts]
        width = max(image.width for image in images)
        canvas = Image.new("RGB", (width, sum(image.height for image in images)), "white")
        offset = 0
        for image in images:
            canvas.paste(image, (0, offset))
            offset += image.height
            image.close()
        if canvas.height > MAX_PREVIEW_PX:
            ratio = MAX_PREVIEW_PX / canvas.height
            canvas = canvas.resize((max(1, round(canvas.width * ratio)), MAX_PREVIEW_PX), Image.LANCZOS)
        canvas.save(target, "JPEG", quality=82)
        for part in parts:
            part.unlink(missing_ok=True)

    trim_image_whitespace(target)
    return filename


async def print_long_pdf(page: Any, pdf_path: Path) -> None:
    """把整页内容输出为一张不分页的长条 PDF。"""
    metrics = await page.evaluate(page_height_js())
    content_px = int(metrics.get("height") or 0)
    height_in = content_px / 96.0
    scale = 1.0
    if height_in > MAX_PDF_HEIGHT_IN:
        # 留一点余量，避免浮点误差多出一张空白页
        scale = (MAX_PDF_HEIGHT_IN - 0.5) / height_in
        height_in = MAX_PDF_HEIGHT_IN
    if scale < MIN_PDF_SCALE:
        logging.warning("页面过长（%s px），改为 A4 分页输出", content_px)
        await print_paginated_pdf(page, pdf_path)
        return
    logging.info("输出长条 PDF：高 %.1f 英寸，缩放 %.2f", height_in, scale)
    await page.pdf(
        path=str(pdf_path),
        width=f"{A4_WIDTH_MM}mm",
        height=f"{height_in:.2f}in",
        scale=scale,
        print_background=True,
        margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
    )


async def print_paginated_pdf(page: Any, pdf_path: Path) -> None:
    """超长页面的兜底方案：按 A4 分页。"""
    await page.pdf(
        path=str(pdf_path),
        format="A4",
        print_background=True,
        display_header_footer=True,
        header_template="<div></div>",
        footer_template="""
            <div style="box-sizing:border-box;width:100%;padding:0 10mm;font-size:8px;color:#666;display:flex;justify-content:space-between;">
                <span class="date"></span>
                <span><span class="pageNumber"></span> / <span class="totalPages"></span></span>
            </div>
        """,
        margin={"top": "10mm", "right": "8mm", "bottom": "14mm", "left": "8mm"},
    )


async def capture_pdf(
    browser: Any,
    task: Task,
    output_dir: Path,
    preview_dir: Path | None = None,
    preview_index: int = 0,
) -> CaptureResult:
    context = await browser.new_context(
        viewport={"width": A4_WIDTH_PX, "height": 1200},
        device_scale_factor=PREVIEW_DEVICE_SCALE,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        ignore_https_errors=True,
    )
    try:
        page = await context.new_page()
        page.set_default_timeout(30_000)
        await page.emulate_media(media="screen")
        logging.info("开始抓取：%s", task.name)
        await navigate(page, task.url)
        frame_url = await find_content_frame_url(page, task.frame_selector)
        if frame_url:
            logging.info("已定位同源主内容 iframe，改为完整打印其内容")
            await navigate(page, frame_url)
        await prepare_document(page, task)
        pdf_path = safe_pdf_path(output_dir, task)
        await print_long_pdf(page, pdf_path)
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RuntimeError("PDF 文件未生成")
        logging.info("PDF 已生成：%s（%.1f MB）", pdf_path.name, pdf_path.stat().st_size / 1_048_576)
        if preview_dir is not None:
            try:
                filename = await save_preview_image(page, preview_dir, preview_index)
                logging.info("网页预览长图已生成：%s", filename or "无")
            except Exception as exc:
                logging.warning(
                    "网页预览图生成失败：%s | %s: %s",
                    humanize_failure(exc), type(exc).__name__, repr(exc)[:400],
                )
        return CaptureResult(task, pdf_path, page.url)
    finally:
        await context.close()


def github_run_url() -> str:
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    return f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else ""


def smtp_send(message: EmailMessage, username: str, auth_code: str) -> None:
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=60) as smtp:
        smtp.login(username, auth_code)
        smtp.send_message(message)


def smtp_send_with_retry(
    message: EmailMessage, username: str, auth_code: str, attempts: int = 3
) -> None:
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            smtp_send(message, username, auth_code)
            return
        except smtplib.SMTPRecipientsRefused as exc:
            details = []
            for address, error in exc.recipients.items():
                reason = error[1]
                if isinstance(reason, bytes):
                    reason = reason.decode("utf-8", "replace")
                details.append(f"{address}: {reason}")
            raise RuntimeError("收件地址被拒绝（" + "; ".join(details) + "）") from None
        except (smtplib.SMTPException, OSError) as exc:
            last_error = exc
            if index + 1 < attempts:
                time.sleep(5 * (index + 1))
    raise RuntimeError(f"邮件发送失败：{type(last_error).__name__}: {last_error}") from None


def build_email(result: CaptureResult, sender: str, recipients: list[str], attach_pdf: bool) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Subject"] = f"[网页 PDF] {result.task.name}"
    content = f"任务：{result.task.name}\n来源：{result.task.url}\n"
    run_url = github_run_url()
    if attach_pdf:
        content += "PDF 已作为附件发送。\n"
    else:
        content += f"PDF 附件发送失败，请从运行结果页下载：{run_url}\n"
    message.set_content(content)
    if attach_pdf:
        with result.pdf_path.open("rb") as file:
            message.add_attachment(
                file.read(), maintype="application", subtype="pdf", filename=result.pdf_path.name
            )
    return message


EMAIL_PATTERN = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}$")


def parse_recipients(raw: str) -> list[str]:
    """支持英文/中文逗号、分号、顿号、空格与空白分隔，并过滤非法地址。"""
    parts = re.split(r"[,;，；、\s]+", raw)
    valid: list[str] = []
    invalid: list[str] = []
    for part in parts:
        candidate = part.strip().strip("<>").strip()
        if not candidate:
            continue
        (valid if EMAIL_PATTERN.match(candidate) else invalid).append(candidate)
    if invalid:
        logging.warning("已忽略无效收件地址：%s", "、".join(invalid))
    return valid


def send_qq_email(result: CaptureResult) -> None:
    username = os.environ["QQ_SMTP_USERNAME"]
    auth_code = os.environ["QQ_SMTP_AUTH_CODE"]
    recipients = parse_recipients(os.environ["QQ_EMAIL_TO"])
    if not recipients:
        logging.info("QQ_EMAIL_TO 未填写有效邮箱地址（当前为占位），跳过邮件推送")
        return
    try:
        smtp_send_with_retry(build_email(result, username, recipients, True), username, auth_code)
        logging.info("QQ 邮件 PDF 附件已发送")
    except RuntimeError as exc:
        run_url = github_run_url()
        if not run_url:
            raise RuntimeError(f"QQ 邮件附件发送失败：{exc}") from None
        logging.warning("QQ 邮件附件发送失败（%s），改发 Artifact 下载通知", exc)
        smtp_send_with_retry(build_email(result, username, recipients, False), username, auth_code)
        logging.info("QQ 邮件下载通知已发送")


def wecom_request(url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = requests.post(url, timeout=60, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"企业微信网络请求失败：{type(exc).__name__}") from None
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"企业微信返回 HTTP {response.status_code}") from None
    if response.status_code != 200 or payload.get("errcode") != 0:
        detail = str(payload.get("errmsg") or f"HTTP {response.status_code}")[:200]
        raise RuntimeError(f"企业微信接口错误：{detail}")
    return payload


def send_wecom_text(key: str, result: CaptureResult) -> None:
    run_url = github_run_url()
    target = f"[查看运行结果并下载 Artifact]({run_url})" if run_url else "请在输出目录取 PDF"
    content = f"**网页 PDF 已生成**\n任务：{result.task.name}\n文件直发失败，{target}"
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    wecom_request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send", params={"key": key}, json=payload)


def send_wecom_pdf(result: CaptureResult) -> None:
    key = os.environ["WECOM_WEBHOOK_KEY"]
    upload_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
    try:
        with result.pdf_path.open("rb") as file:
            payload = wecom_request(
                upload_url,
                params={"key": key, "type": "file"},
                files={"media": (result.pdf_path.name, file, "application/pdf")},
            )
        media_id = payload.get("media_id")
        if not media_id:
            raise RuntimeError("企业微信未返回 media_id")
        wecom_request(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
            params={"key": key},
            json={"msgtype": "file", "file": {"media_id": media_id}},
        )
        logging.info("企业微信 PDF 文件已发送")
    except RuntimeError as exc:
        logging.warning("企业微信文件直发失败（%s），改发下载通知", exc)
        send_wecom_text(key, result)
        logging.info("企业微信下载通知已发送")


DEFAULT_CHANNELS = {"email": True, "wecom": True, "preview": True}
CHANNEL_NAMES = {"email": "QQ 邮箱", "wecom": "企业微信", "preview": "网页图片预览"}


def load_channels(config_path: Path) -> dict:
    """读取 channels.json 中的推送开关，缺省全部开启。"""
    path = Path(config_path).with_name("channels.json")
    channels = dict(DEFAULT_CHANNELS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logging.info("未找到 channels.json，默认开启全部推送方式")
        return channels
    for key in channels:
        if key in data:
            channels[key] = bool(data[key])
    disabled = [CHANNEL_NAMES[k] for k, enabled in channels.items() if not enabled]
    if disabled:
        logging.info("已按设置关闭推送方式：%s", "、".join(disabled))
    return channels


def deliver_pdf(result: CaptureResult, channels: dict | None = None) -> list[str]:
    channels = DEFAULT_CHANNELS if channels is None else channels
    errors = []
    email_variables = ("QQ_SMTP_USERNAME", "QQ_SMTP_AUTH_CODE", "QQ_EMAIL_TO")
    configured_email = [name for name in email_variables if os.getenv(name)]
    if configured_email and channels.get("email", True):
        if len(configured_email) != len(email_variables):
            errors.append("QQ 邮件 Secrets 配置不完整")
        else:
            try:
                send_qq_email(result)
            except RuntimeError as exc:
                errors.append(str(exc))
    if os.getenv("WECOM_WEBHOOK_KEY") and channels.get("wecom", True):
        try:
            send_wecom_pdf(result)
        except RuntimeError as exc:
            errors.append(str(exc))
    if not any(channels.get(name, True) for name in channels):
        logging.info("推送方式已在控制台全部关闭，仅保留 PDF 文件")
    elif not configured_email and not os.getenv("WECOM_WEBHOOK_KEY"):
        logging.info("未配置推送 Secrets，仅保留 PDF 文件")
    return errors


def humanize_failure(exc: BaseException) -> str:
    """把底层异常翻译成一句中文简短说明（用于控制台任务列表）。"""
    raw = f"{type(exc).__name__}: {exc}".lower()
    rules = [
        ("waiting for selector", "未找到网页内容区域"),
        ("err_name_not_resolved", "网址域名无法解析"),
        ("err_connection_timed_out", "连接网站超时"),
        ("err_connection_refused", "网站拒绝连接"),
        ("err_connection_reset", "连接被网站重置"),
        ("err_connection_closed", "连接被网站关闭"),
        ("err_internet_disconnected", "网络连接中断"),
        ("err_cert", "网站证书异常"),
        ("err_ssl", "网站证书异常"),
        ("err_aborted", "网页加载被中断"),
        ("err_http_response_code_failure", "网页返回错误状态"),
        ("err_empty_response", "网页返回空内容"),
        ("err_too_many_redirects", "网页重定向次数过多"),
        ("timeout", "网页加载超时"),
        ("target page, context or browser has been closed", "浏览器页面异常关闭"),
        ("browser has been closed", "浏览器异常关闭"),
        ("executable doesn't exist", "浏览器组件缺失"),
        ("net::err", "网页访问失败"),
        ("pdf 文件未生成", "PDF 生成失败"),
        ("pdf 内容为空", "页面内容为空"),
        ("bad address syntax", "收件邮箱格式错误"),
        ("smtp", "邮箱发送失败"),
        ("wecom", "企业微信推送失败"),
    ]
    for needle, message in rules:
        if needle in raw:
            return message
    return "抓取时出现异常"


async def capture_with_retry(
    browser: Any,
    task: Task,
    output_dir: Path,
    preview_dir: Path | None = None,
    preview_index: int = 0,
) -> CaptureResult:
    """单个任务失败后重试一次，降低偶发超时的影响。"""
    try:
        return await capture_pdf(browser, task, output_dir, preview_dir, preview_index)
    except Exception as exc:
        logging.warning(
            "任务「%s」首次失败（%s），10 秒后重试一次", task.name, humanize_failure(exc)
        )
        await asyncio.sleep(10)
        return await capture_pdf(browser, task, output_dir, preview_dir, preview_index)


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="完整保存网页为 PDF，并通过 QQ 邮件和企业微信推送")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("tasks.json"))
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).with_name("output"))
    parser.add_argument("--manual-tasks", default=os.getenv("MANUAL_TASKS", ""))
    parser.add_argument("--no-push", action="store_true", help="只生成 PDF，不发送消息")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.manual_tasks.strip():
        all_saved = load_tasks(args.config, only_enabled=False)
        tasks = parse_manual_tasks(args.manual_tasks, all_saved)
    else:
        tasks = load_tasks(args.config)
    if not tasks:
        raise ValueError("没有可执行的任务")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    channels = load_channels(args.config)

    failures = []
    pdf_count = 0
    preview_root = None
    preview_manifest = []
    if channels.get("preview", True):
        run_id = os.getenv("GITHUB_RUN_ID") or datetime.now(TIMEZONE).strftime("local-%Y%m%d-%H%M%S")
        preview_root = Path(__file__).with_name("previews") / f"run-{run_id}"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            for index, task in enumerate(tasks, start=1):
                try:
                    result = await capture_with_retry(
                        browser,
                        task,
                        args.output_dir,
                        preview_root,
                        index,
                    )
                    pdf_count += 1
                    if preview_root is not None:
                        image_name = f"{index:02d}.jpg"
                        if (preview_root / image_name).is_file():
                            preview_manifest.append({"index": index, "name": task.name, "image": image_name})
                    if not args.no_push:
                        failures.extend(f"{task.name}：{error}" for error in await asyncio.to_thread(deliver_pdf, result, channels))
                except Exception as exc:
                    logging.exception("任务失败：%s", task.name)
                    failures.append(f"{task.name}：{humanize_failure(exc)}")
        finally:
            await browser.close()

    if preview_root is not None and preview_manifest:
        (preview_root / "manifest.json").write_text(
            json.dumps(preview_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logging.info("网页预览图清单已写入：%d 个任务", len(preview_manifest))

    if failures:
        for item in failures:
            print("::error::" + item.replace("\n", " ")[:400], flush=True)
    if failures and pdf_count == 0:
        logging.error("全部任务均失败：\n%s", "\n".join(failures))
        return 1
    if failures:
        logging.warning(
            "已完成 %d 个 PDF，以下项目未成功（不影响本次结果）：\n%s",
            pdf_count,
            "\n".join(failures),
        )
        return 0
    logging.info("全部任务执行完成，共 %d 个 PDF", pdf_count)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(async_main()))
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        logging.error("启动失败：%s", exc)
        sys.exit(2)
