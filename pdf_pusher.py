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


def safe_pdf_path(output_dir: Path, task: Task) -> Path:
    name = re.sub(r"[\\/:*?\"<>|\s]+", "-", task.name).strip("-._")[:60] or "webpage"
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
        element = await page.wait_for_selector(selector, state="attached", timeout=15_000)
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


async def capture_pdf(browser: Any, task: Task, output_dir: Path) -> CaptureResult:
    context = await browser.new_context(
        viewport={"width": 1440, "height": 1000},
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
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RuntimeError("PDF 文件未生成")
        logging.info("PDF 已生成：%s（%.1f MB）", pdf_path.name, pdf_path.stat().st_size / 1_048_576)
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


def _upload_catbox(pdf_path: Path) -> str | None:
    with pdf_path.open("rb") as file:
        response = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (pdf_path.name, file, "application/pdf")},
            timeout=120,
        )
    url = response.text.strip()
    return url if url.startswith("http") else None


def _upload_0x0(pdf_path: Path) -> str | None:
    with pdf_path.open("rb") as file:
        response = requests.post(
            "https://0x0.st",
            files={"file": (pdf_path.name, file, "application/pdf")},
            timeout=120,
        )
    url = response.text.strip()
    return url if url.startswith("http") else None


def upload_public(pdf_path: Path) -> str | None:
    """把 PDF 传到可公开访问的临时托管，返回直链；全部失败返回 None。"""
    if os.getenv("PUBLIC_UPLOAD", "1").lower() not in {"1", "true", "yes", "on"}:
        return None
    for uploader in (_upload_catbox, _upload_0x0):
        try:
            url = uploader(pdf_path)
        except requests.RequestException as exc:
            logging.warning("公开上传失败（%s）：%s", uploader.__name__, type(exc).__name__)
            continue
        if url:
            logging.info("PDF 已上传至公开地址：%s", url)
            return url
    return None


def send_pushplus(result: CaptureResult, public_url: str | None) -> None:
    token = os.environ["PUSHPLUS_TOKEN"]
    size_mb = result.pdf_path.stat().st_size / 1_048_576
    link = public_url or github_run_url()
    link_md = f"[点此在微信中打开 / 下载 PDF]({link})" if link else "PDF 已随邮件和企业微信发送"
    content = (
        f"**网页 PDF 已生成**\n\n"
        f"- 任务：{result.task.name}\n"
        f"- 大小：{size_mb:.1f} MB\n"
        f"- 来源：{result.task.url}\n\n"
        f"{link_md}\n\n"
        f"完整版 PDF 同时已发送至 QQ 邮箱和企业微信群。"
    )
    payload = {
        "token": token,
        "title": f"[网页 PDF] {result.task.name}",
        "content": content,
        "template": "markdown",
    }
    response = requests.post("https://www.pushplus.plus/send", json=payload, timeout=60)
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"PushPlus 返回 HTTP {response.status_code}") from None
    if response.status_code != 200 or data.get("code") != 200:
        detail = str(data.get("msg") or f"HTTP {response.status_code}")[:200]
        raise RuntimeError(f"PushPlus 接口错误：{detail}")
    logging.info("微信（PushPlus）推送已发送")


def deliver_pdf(result: CaptureResult) -> list[str]:
    errors = []
    email_variables = ("QQ_SMTP_USERNAME", "QQ_SMTP_AUTH_CODE", "QQ_EMAIL_TO")
    configured_email = [name for name in email_variables if os.getenv(name)]
    if configured_email:
        if len(configured_email) != len(email_variables):
            errors.append("QQ 邮件 Secrets 配置不完整")
        else:
            try:
                send_qq_email(result)
            except RuntimeError as exc:
                errors.append(str(exc))
    if os.getenv("WECOM_WEBHOOK_KEY"):
        try:
            send_wecom_pdf(result)
        except RuntimeError as exc:
            errors.append(str(exc))
    if os.getenv("PUSHPLUS_TOKEN"):
        try:
            public_url = upload_public(result.pdf_path)
            send_pushplus(result, public_url)
        except RuntimeError as exc:
            errors.append(str(exc))
    if (
        not configured_email
        and not os.getenv("WECOM_WEBHOOK_KEY")
        and not os.getenv("PUSHPLUS_TOKEN")
    ):
        logging.info("未配置推送 Secrets，仅保留 PDF 文件")
    return errors


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

    failures = []
    pdf_count = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            for task in tasks:
                try:
                    result = await capture_pdf(browser, task, args.output_dir)
                    pdf_count += 1
                    if not args.no_push:
                        failures.extend(f"{task.name}：{error}" for error in await asyncio.to_thread(deliver_pdf, result))
                except Exception as exc:
                    logging.exception("任务失败：%s", task.name)
                    failures.append(f"{task.name}：{type(exc).__name__}: {exc}")
        finally:
            await browser.close()

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
