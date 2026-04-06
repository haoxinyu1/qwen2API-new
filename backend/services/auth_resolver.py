import asyncio
import logging
from backend.core.account_pool import AccountPool, Account
from backend.core.browser_engine import _new_browser
from backend.core.config import settings
from backend.core.account_pool import Account
import logging
import asyncio
import random
import string
import time
import json
import re
from typing import Optional
from camoufox.async_api import AsyncCamoufox

log = logging.getLogger(__name__)

BASE_URL = "https://chat.qwen.ai"

def _new_browser():
    return AsyncCamoufox(
        headless=True,
        enable_cache=True,
        block_images=True,
        i_know_what_im_doing=True,
        os=["windows"]
    )

async def get_fresh_token(email: str, password: str) -> str:
    """如果提供了此功能，用 playwright 重新登录获取 Token，这里提供一个 mock 或抛错以防未实现"""
    raise NotImplementedError("Auto-login not fully implemented yet in the separated architecture")

def _gen_password(length=14):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(random.choices(chars, k=length))
        if (any(c.isupper() for c in pwd) and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in "!@#$%^&*" for c in pwd)):
            return pwd

def _gen_username():
    first = random.choice(["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie",
                            "Drew", "Avery", "Quinn", "Blake", "Sage", "Reese", "Dakota", "Emery"])
    last = random.choice(["Smith", "Brown", "Wilson", "Lee", "Chen", "Wang", "Kim", "Park",
                           "Davis", "Miller", "Garcia", "Martinez", "Anderson", "Taylor", "Thomas"])
    return f"{first} {last}"

MAIL_BASE = "https://mail.chatgpt.org.uk"

class _EmailSession:
    def __init__(self):
        from curl_cffi import requests as cffi_requests
        self._session = cffi_requests.Session(impersonate="chrome")
        self._session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._current_token = ""
        self._token_expires_at = 0
        self._initialized = False

    def _init_session(self) -> bool:
        try:
            resp = self._session.get(f"{MAIL_BASE}/", timeout=15)
            if resp.status_code != 200:
                return False
            match = re.search(r'window\.__BROWSER_AUTH\s*=\s*(\{[^}]+\})', resp.text)
            if match:
                auth_data = json.loads(match.group(1))
                self._current_token = auth_data.get("token", "")
                self._token_expires_at = auth_data.get("expires_at", 0)
                self._initialized = True
                return True
            return False
        except Exception as e:
            log.warning(f"[MailSession] init error: {e}")
            return False

    def _ensure_token(self) -> bool:
        if not self._initialized or not self._current_token or time.time() > self._token_expires_at - 120:
            return self._init_session()
        return True

    def get_email(self) -> str:
        if not self._ensure_token():
            raise Exception("mail.chatgpt.org.uk: session init failed")
        resp = self._session.get(
            f"{MAIL_BASE}/api/generate-email",
            headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                     "x-inbox-token": self._current_token},
            timeout=15,
        )
        if resp.status_code == 401:
            self._initialized = False
            self._init_session()
            resp = self._session.get(
                f"{MAIL_BASE}/api/generate-email",
                headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                         "x-inbox-token": self._current_token},
                timeout=15,
            )
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"mail.chatgpt.org.uk: generate-email failed: {data}")
        email = str(data.get("data", {}).get("email", "")).strip()
        new_tok = data.get("auth", {}).get("token", "")
        if new_tok:
            self._current_token = new_tok
            self._token_expires_at = data.get("auth", {}).get("expires_at", 0)
        return email

    def poll_verify_link(self, email: str, timeout_sec: int = 300) -> str:
        keywords = ("qwen", "verify", "activate", "confirm", "aliyun", "alibaba", "qwenlm")
        log.info(f"[MailSession] Polling inbox for {email} (timeout {timeout_sec}s)...")
        deadline = time.time() + timeout_sec
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                resp = self._session.get(
                    f"{MAIL_BASE}/api/emails",
                    params={"email": email},
                    headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                             "x-inbox-token": self._current_token},
                    timeout=15,
                )
                if resp.status_code == 401:
                    self._initialized = False
                    self._init_session()
                    time.sleep(3)
                    continue
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("auth", {}).get("token"):
                        self._current_token = data["auth"]["token"]
                        self._token_expires_at = data["auth"].get("expires_at", 0)
                    emails_list = data.get("data", {}).get("emails", [])
                    log.info(f"[MailSession] 第{attempt}次轮询，收件箱邮件数: {len(emails_list)}")
                    for msg in emails_list:
                        subject = str(msg.get("subject", ""))
                        parts = []
                        for field in ("html_content", "content", "body", "html", "text", "raw"):
                            v = msg.get(field)
                            if v: parts.append(str(v))
                        for field in ("payload", "data", "message"):
                            v = msg.get(field)
                            if isinstance(v, dict): parts.extend(str(x) for x in v.values() if x)
                            elif isinstance(v, str) and v: parts.append(v)
                        combined = " ".join(parts)
                        combined = (combined.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                                    .replace("\\u003c", "<").replace("\\u003e", ">")
                                    .replace("\\u0026", "&").replace("\\/", "/"))
                        all_links = re.findall(r'https?://[^\s"\'<>\\,\)]+', combined)
                        for link in all_links:
                            link = link.rstrip(".,;)")
                            if any(kw in link.lower() for kw in keywords):
                                log.info(f"[MailSession] 找到验证链接: {link[:120]}...")
                                return link
                        if any(kw in subject.lower() for kw in keywords) and all_links:
                            return all_links[0]
                else:
                    log.warning(f"[MailSession] 邮件API HTTP {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                log.warning(f"[MailSession] 轮询异常: {e}")
            time.sleep(5)
        log.error("[MailSession] 超时：未收到验证邮件")
        return ""

class _AsyncMailClient:
    def __init__(self):
        self._sess: Optional[_EmailSession] = None
        self._email = ""

    async def __aenter__(self):
        self._sess = await asyncio.to_thread(_EmailSession)
        return self

    async def __aexit__(self, *args):
        pass

    async def generate_email(self) -> str:
        self._email = await asyncio.to_thread(self._sess.get_email)
        return self._email

    async def get_verify_link(self, timeout_sec: int = 300) -> str:
        return await asyncio.to_thread(self._sess.poll_verify_link, self._email, timeout_sec)

async def register_qwen_account() -> Optional[Account]:
    log.info("[Register] ── 开始注册流程 ──")
    async with _AsyncMailClient() as mail_client:
        log.info("[Register] [1/7] 生成临时邮箱...")
        email = await mail_client.generate_email()
        password = _gen_password()
        username = _gen_username()
        log.info(f"[Register] [1/7] 邮箱: {email}  用户名: {username}")

        try:
            async with _new_browser() as browser:
                page = await browser.new_page()
                log.info(f"[Register] [2/7] 打开注册页面: {BASE_URL}/auth?action=signup")
                try:
                    await page.goto(f"{BASE_URL}/auth?action=signup", wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    log.warning(f"[Register] [2/7] 页面加载异常: {e}")

                log.info("[Register] [3/7] 填写注册表单...")
                name_input = None
                for sel in ['input[placeholder*="Full Name"]', 'input[placeholder*="Name"]']:
                    try:
                        name_input = await page.wait_for_selector(sel, timeout=15000)
                        if name_input: break
                    except Exception:
                        pass
                if not name_input:
                    inputs = await page.query_selector_all('input')
                    name_input = inputs[0] if len(inputs) >= 4 else None
                if not name_input:
                    log.error("[Register] [3/7] 找不到姓名输入框，注册中止")
                    return None

                await name_input.click(); await name_input.fill(username)
                log.info(f"[Register] [3/7]  ✓ 姓名: {username}")
                email_input = await page.query_selector('input[placeholder*="Email"]')
                if not email_input:
                    inputs = await page.query_selector_all('input')
                    email_input = inputs[1] if len(inputs) >= 2 else None
                if email_input: await email_input.click(); await email_input.fill(email)
                log.info(f"[Register] [3/7]  ✓ 邮箱: {email}")

                pwd_input = await page.query_selector('input[placeholder*="Password"]:not([placeholder*="Again"])')
                if not pwd_input:
                    inputs = await page.query_selector_all('input')
                    pwd_input = inputs[2] if len(inputs) >= 3 else None
                if pwd_input: await pwd_input.click(); await pwd_input.fill(password)

                confirm_input = await page.query_selector('input[placeholder*="Again"]')
                if not confirm_input:
                    inputs = await page.query_selector_all('input')
                    confirm_input = inputs[3] if len(inputs) >= 4 else None
                if confirm_input: await confirm_input.click(); await confirm_input.fill(password)
                log.info("[Register] [3/7]  ✓ 密码已填写")

                checkbox = await page.query_selector('input[type="checkbox"]')
                if checkbox and not await checkbox.is_checked(): await checkbox.click()
                else:
                    agree = await page.query_selector('text=I agree')
                    if agree: await agree.click()
                log.info("[Register] [3/7]  ✓ 同意条款")

                log.info("[Register] [4/7] 提交注册表单...")
                await asyncio.sleep(1)
                submit = await page.query_selector('button:has-text("Create Account")') or await page.query_selector('button[type="submit"]')
                if submit: await submit.click()
                log.info("[Register] [4/7] 已点击提交，等待页面跳转（6s）...")
                await asyncio.sleep(6)

                url_after = page.url
                log.info(f"[Register] [4/7] 提交后URL: {url_after}")

                # Check if already logged in (redirected to main page)
                token = None
                if BASE_URL in url_after and "auth" not in url_after:
                    log.info("[Register] [5/7] 已跳转主页，尝试直接获取token...")
                    await asyncio.sleep(3)
                    token = await page.evaluate("localStorage.getItem('token')")
                    if token:
                        log.info("[Register] [5/7] ✓ 注册后直接获取到token，跳过邮件验证")

                # If no token yet, try explicit login with email+password (faster than email poll)
                if not token:
                    log.info("[Register] [5/7] 尝试用账号密码直接登录...")
                    try:
                        await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                        li_email = await page.query_selector('input[placeholder*="Email"]')
                        if li_email: await li_email.fill(email)
                        li_pwd = await page.query_selector('input[type="password"]')
                        if li_pwd: await li_pwd.fill(password)
                        li_btn = await page.query_selector('button:has-text("Log in")') or await page.query_selector('button[type="submit"]')
                        if li_btn: await li_btn.click()
                        await asyncio.sleep(8)
                        token = await page.evaluate("localStorage.getItem('token')")
                        if token:
                            log.info("[Register] [5/7] ✓ 直接登录成功，获取到token")
                    except Exception as e:
                        log.warning(f"[Register] [5/7] 直接登录失败: {e}")

                # If still no token, poll email for verification link
                if not token:
                    log.info("[Register] [6/7] 等待验证邮件（最多5分钟）...")
                    verify_link = await mail_client.get_verify_link(timeout_sec=300)

                    if not verify_link:
                        log.error("[Register] [6/7] 未收到验证邮件，注册失败")
                        return None

                    log.info(f"[Register] [6/7] ✓ 收到验证链接，访问中...")
                    try:
                        await page.goto(verify_link, wait_until="domcontentloaded", timeout=30000)
                    except Exception: pass
                    await asyncio.sleep(6)
                    token = await page.evaluate("localStorage.getItem('token')")
                    log.info(f"[Register] [6/7] 验证后URL: {page.url}")

                    # Login after verification
                    if not token:
                        log.info("[Register] [6/7] 验证链接后尝试登录...")
                        try:
                            await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                            await asyncio.sleep(3)
                            li_email = await page.query_selector('input[placeholder*="Email"]')
                            if li_email: await li_email.fill(email)
                            li_pwd = await page.query_selector('input[type="password"]')
                            if li_pwd: await li_pwd.fill(password)
                            li_btn = await page.query_selector('button:has-text("Log in")') or await page.query_selector('button[type="submit"]')
                            if li_btn: await li_btn.click()
                            await asyncio.sleep(8)
                            token = await page.evaluate("localStorage.getItem('token')")
                            if token:
                                log.info("[Register] [6/7] ✓ 验证后登录成功")
                        except Exception: pass

                if not token:
                    log.error("[Register] 所有方法均无法获取token，注册失败")
                    return None

                log.info("[Register] [7/7] 提取 cookies...")
                all_cookies = await page.context.cookies()
                cookie_str = "; ".join(f"{c.get('name','')}={c.get('value','')}" for c in all_cookies if "qwen" in c.get("domain", ""))
                log.info(f"[Register] ✓ 注册完成: {email}")
                return Account(email=email, password=password, token=token, cookies=cookie_str, username=username)
        except Exception as e:
            log.error(f"[Register] 浏览器引擎崩溃或被拦截: {str(e)}")
            return None

async def activate_account(acc: Account) -> bool:
    """Open mail.chatgpt.org.uk/{email} in browser, find the Qwen activation link,
    click it, then login to get a fresh token. Returns True on success."""
    log.info(f"[Activate] 开始激活 {acc.email}，打开邮箱页面...")
    keywords = ("qwen", "verify", "activate", "confirm", "aliyun", "alibaba", "qwenlm", "active mail")
    mail_url = f"{MAIL_BASE}/{acc.email}"
    try:
        async with _new_browser() as browser:
            page = await browser.new_page()

            # Step 1: Open the inbox page
            log.info(f"[Activate] 打开收件箱: {mail_url}")
            try:
                # 改用 domcontentloaded 极速加载，放弃等待所有网络请求（networkidle 太慢）
                await page.goto(mail_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            
            # 缩短强制等待时间
            await asyncio.sleep(2)

            # Step 2: Wait for email list to appear, then click the first email
            log.info(f"[Activate] 等待收件箱加载...")
            clicked_email = False

            # Primary: confirmed GPTMail selector
            for sel in ['#emailList li:first-child', '#emailList li', '[class*="EmailItem"]',
                        '[class*="email-item"]', '[class*="MailItem"]', '[class*="mail-item"]',
                        'table tbody tr:first-child', '[role="row"]:first-child']:
                try:
                    # 缩短选择器超时
                    await page.wait_for_selector(sel, timeout=3000)
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        await asyncio.sleep(1)
                        clicked_email = True
                        log.debug(f"[Activate] 点击邮件项: {sel}")
                        break
                except Exception:
                    pass

            if not clicked_email:
                # Fallback: look for any clickable element containing Qwen keywords
                for sel in ['li', 'tr', 'div[class]', '[class*="row"]', '[class*="item"]']:
                    try:
                        els = await page.query_selector_all(sel)
                        for el in (els or [])[:10]:
                            try:
                                text = await el.inner_text()
                                if any(kw in text.lower() for kw in keywords):
                                    await el.click()
                                    await asyncio.sleep(1)
                                    clicked_email = True
                                    log.debug(f"[Activate] 按关键词点击邮件项: {sel}")
                                    break
                            except Exception:
                                pass
                        if clicked_email:
                            break
                    except Exception:
                        pass

            # 新增增强型兜底策略
            if not clicked_email:
                try:
                    log.info("[Activate] 尝试通过增强型 JavaScript 选择器点击第一封邮件")
                    await page.evaluate('''() => {
                        const emails = document.querySelectorAll('li, tr, .mail-item, .email-item');
                        if(emails.length > 0) { emails[0].click(); }
                    }''')
                    await asyncio.sleep(1)
                except Exception as e:
                    log.debug(f"[Activate] 增强型点击失败: {e}")

            # Step 3: Extract activation link — email body is inside #emailFrame iframe
            js_find_link = """() => {
                const kws = ['qwen', 'verify', 'activate', 'confirm', 'aliyun', 'alibaba', 'qwenlm', 'active mail'];
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const a of links) {
                    const href = a.href || '';
                    const text = (a.textContent || '').toLowerCase();
                    if (kws.some(k => href.toLowerCase().includes(k))) return href;
                    if (kws.some(k => text.includes(k)) && href.startsWith('http')) return href;
                }
                const html = document.body ? document.body.innerHTML : '';
                const matches = html.match(/https?:\\/\\/[^"'\\s<>\\\\]+/g) || [];
                for (const m of matches) {
                    if (kws.some(k => m.toLowerCase().includes(k))) return m;
                }
                return null;
            }"""

            verify_link = None

            # Primary: read from #emailFrame iframe (GPTMail renders body inside iframe)
            try:
                iframe_el = await page.query_selector('#emailFrame')
                if iframe_el:
                    await asyncio.sleep(3)  # wait for iframe content to load
                    frame = await iframe_el.content_frame()
                    if frame:
                        verify_link = await frame.evaluate(js_find_link)
                        if verify_link:
                            log.debug(f"[Activate] 从 #emailFrame iframe 提取到链接")
            except Exception as e:
                log.debug(f"[Activate] iframe 读取失败: {e}")

            # Fallback: search main page
            if not verify_link:
                verify_link = await page.evaluate(js_find_link)

            # 新增增强型提取策略（不修改上方原有代码）：
            # 有时 GPTMail 会用 shadow DOM 渲染内容，直接读取 html 文本是最硬核的兜底
            if not verify_link:
                try:
                    log.info("[Activate] 尝试通过全量 DOM 强制提取激活链接")
                    page_html = await page.content()
                    matches = re.findall(r'https?://[^\s"\'<>\\,\)]+', page_html)
                    for m in matches:
                        if any(kw in m.lower() for kw in keywords):
                            verify_link = m
                            break
                except Exception as e:
                    log.debug(f"[Activate] 全量 DOM 提取失败: {e}")

            if not verify_link:
                log.warning(f"[Activate] {acc.email} 邮箱页面未找到激活链接，URL={page.url}")
                title = await page.title()
                log.debug(f"[Activate] 页面标题: {title!r}")
                content = await page.evaluate("document.body ? document.body.innerText.slice(0,400) : ''")
                log.debug(f"[Activate] 页面内容片段: {content!r}")
                return False

            log.info(f"[Activate] 找到激活链接: {verify_link[:120]}")

            # Step 4: Visit the activation link
            try:
                await page.goto(verify_link, wait_until="networkidle", timeout=30000)
            except Exception:
                try:
                    await page.goto(verify_link, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            await asyncio.sleep(5)
            token = await page.evaluate("localStorage.getItem('token')")
            log.info(f"[Activate] 访问激活链接后 URL={page.url}, token={'有' if token else '无'}")

            # Step 5: If no token yet, try logging in
            if not token and acc.password:
                try:
                    await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    li_email = await page.query_selector('input[placeholder*="Email"]')
                    if li_email:
                        await li_email.fill(acc.email)
                    li_pwd = await page.query_selector('input[type="password"]')
                    if li_pwd:
                        await li_pwd.fill(acc.password)
                    li_btn = (await page.query_selector('button:has-text("Log in")') or
                              await page.query_selector('button[type="submit"]'))
                    if li_btn:
                        await li_btn.click()
                    await asyncio.sleep(8)
                    token = await page.evaluate("localStorage.getItem('token')")
                except Exception as e:
                    log.warning(f"[Activate] 激活后登录异常: {e}")

            if token:
                acc.token = token
                acc.valid = True
                log.info(f"[Activate] {acc.email} 激活成功，token已更新")
                return True
            log.warning(f"[Activate] {acc.email} 激活后未能获取token")
            return False
    except Exception as e:
        log.error(f"[Activate] {acc.email} 激活异常: {e}")
        return False

class AuthResolver:
    """自动登录并提取 Token，在检测到 401 时自动自愈凭证"""
    def __init__(self, pool: AccountPool):
        self.pool = pool

    async def refresh_token(self, acc: Account) -> bool:
        if not acc.email or not acc.password:
            log.warning(f"[Auth] 账号 {acc.email} 缺少密码，无法自愈。")
            return False
            
        log.info(f"[Auth] 正在启动独立浏览器为 {acc.email} 自动刷新 Token...")
        try:
            async with _new_browser() as browser:
                page = await browser.new_page()
                await page.goto("https://chat.qwen.ai/auth", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                
                # 填写邮箱密码
                li_email = await page.query_selector('input[placeholder*="Email"]')
                if li_email: await li_email.fill(acc.email)
                li_pwd = await page.query_selector('input[type="password"]')
                if li_pwd: await li_pwd.fill(acc.password)
                
                # 提交
                li_btn = (await page.query_selector('button:has-text("Log in")') or
                          await page.query_selector('button[type="submit"]'))
                if li_btn: await li_btn.click()
                
                await asyncio.sleep(8)
                
                # 提取 LocalStorage Token
                new_token = await page.evaluate("localStorage.getItem('token')")
                if new_token and new_token != acc.token:
                    acc.token = new_token
                    acc.valid = True
                    await self.pool.save()
                    log.info(f"[Auth] 自愈成功，{acc.email} 获得全新 Token。")
                    return True
                elif new_token == acc.token:
                    acc.valid = True
                    log.info(f"[Auth] {acc.email} 重新校验成功。")
                    return True
                else:
                    log.error(f"[Auth] {acc.email} 登录失败或遭遇滑块验证拦截。")
                    return False
        except Exception as e:
            log.error(f"[Register] 浏览器引擎崩溃或被拦截: {str(e)}")
            return None
