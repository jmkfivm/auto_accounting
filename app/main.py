import subprocess, sys, os, asyncio, re, tempfile, shutil, random, math, time
import hashlib
from pathlib import Path
from datetime import datetime
import requests
import json
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
import assemblyai as aai

load_dotenv()

WEBAPP_URL   = os.getenv("WEBAPP_URL", "")
WEBAPP_TOKEN = os.getenv("WEBAPP_TOKEN", "")

NETELLER_EMAIL = os.getenv("NETELLER_EMAIL", "")
NETELLER_PASS  = os.getenv("NETELLER_PASS", "")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://member.neteller.com/wallet/ng/dashboard")

USER_DATA_DIR = os.getenv("USER_DATA_DIR", "/app/user-data")

BALANCE_SELECTOR_MAIN = os.getenv("BALANCE_SELECTOR_MAIN", ".ps-digits-1.balance-amount")
BALANCE_SELECTOR_DEC  = os.getenv("BALANCE_SELECTOR_DEC", ".ps-digits-2")
CURRENCY_SELECTOR     = os.getenv("CURRENCY_SELECTOR", ".balance-currency")

SYNC_MARKER = Path(USER_DATA_DIR) / ".host_profile_synced"

VPS = os.getenv("VPS")

RAW_URL = os.getenv("RAW_URL")
LOCAL_FILE = "main.py"

# Adjust if you rename/move things
GITHUB_REPO  = "jmkfivm/auto_accounting"   # <owner>/<repo>
GIT_BRANCH   = "main"
REMOTE_PATH  = "app/main.py"            # path to this very file in the repo
SHA_CACHE    = ".last_remote_sha"       # stored next to your working dir

def _gh_headers():
    h = {}
    tok = os.getenv("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    h["Accept"] = "application/vnd.github+json"
    return h

def _fetch_remote_meta():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{REMOTE_PATH}?ref={GIT_BRANCH}"
    r = requests.get(url, headers=_gh_headers(), timeout=10)
    r.raise_for_status()
    data = r.json()
    # data["sha"] is the blob SHA (good stable version token), data["download_url"] is raw file
    return data["sha"], data["download_url"]

def _atomic_replace(target_path: str, new_bytes: bytes):
    d = os.path.dirname(target_path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".update_", suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(new_bytes)
    # keep a quick backup just in case
    backup = target_path + ".bak"
    if os.path.exists(target_path):
        shutil.copy2(target_path, backup)
    os.replace(tmp, target_path)

def maybe_self_update():
    try:
        remote_sha, raw_url = _fetch_remote_meta()
    except Exception as e:
        print(f"[update] skip (GitHub query failed): {e}")
        return

    # If we’ve already seen this exact SHA, skip
    if os.path.exists(SHA_CACHE):
        try:
            if open(SHA_CACHE, "r", encoding="utf-8").read().strip() == remote_sha:
                print("[update] already latest")
                return
        except Exception:
            pass

    # Download the raw file
    r = requests.get(raw_url, headers=_gh_headers(), timeout=15)
    if r.status_code != 200:
        print(f"[update] download failed: {r.status_code}")
        return
    new_bytes = r.content

    # Compare with current file contents to avoid needless rewrite
    try:
        with open(__file__, "rb") as f:
            cur_bytes = f.read()
        if hashlib.sha256(cur_bytes).hexdigest() == hashlib.sha256(new_bytes).hexdigest():
            # Content same but SHA cache stale — refresh it
            with open(SHA_CACHE, "w", encoding="utf-8") as f:
                f.write(remote_sha)
            print("[update] content unchanged; cache updated")
            return
    except Exception:
        pass

    print("[update] applying update...")
    _atomic_replace(__file__, new_bytes)
    with open(SHA_CACHE, "w", encoding="utf-8") as f:
        f.write(remote_sha)

    print("[update] restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def chrome_user_data_root():
    # Works on native Windows Python
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA not set. Are you running on native Windows?")
    root = Path(local) / "Google" / "Chrome" / "User Data"
    if not root.exists():
        raise FileNotFoundError(f"Chrome user data root not found: {root}")
    return root
def get_last_used_profile(root: Path):
    local_state = root / "Local State"
    if local_state.exists():
        try:
            data = json.loads(local_state.read_text(encoding="utf-8"))
            last = data.get("profile", {}).get("last_used")
            if last:
                p = root / last
                if p.exists():
                    return p
        except Exception:
            pass
    return None

def _copy_host_profile_once():
    """Copy the mounted host Chrome profile into USER_DATA_DIR (first run only)."""
    root = chrome_user_data_root()
    src = Path(get_last_used_profile(root))
    dst = Path(USER_DATA_DIR)

    if not src.exists():
        print("[INFO] No host profile mounted; using existing/warm Playwright profile.")
        return

    if SYNC_MARKER.exists():
        return

    if any(dst.iterdir()):
        SYNC_MARKER.touch()
        print("[INFO] USER_DATA_DIR already has data; skipping initial sync.")
        return

    print(f"[INFO] Syncing host profile from {src} -> {dst} (one-time)")
    def _ignore(dirpath, names):
        ignore_names = {"Cache", "Code Cache", "GPUCache", "GrShaderCache", "Media Cache"}
        return [n for n in names if n in ignore_names]
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)
    SYNC_MARKER.touch()

async def random_clicks_any_resolution(page,
                                       clicks=3,
                                       margin_px=20,
                                       min_delay_click_ms=50,
                                       max_delay_click_ms=150,
                                       min_pause_ms=300,
                                       max_pause_ms=1000):
    """
    Perform random clicks anywhere within the current visible screen area.
    Works with any resolution, using system (no fixed viewport).
    """
    size = await page.evaluate("""() => ({
        width: Math.max(document.documentElement.clientWidth, window.innerWidth || 0),
        height: Math.max(document.documentElement.clientHeight, window.innerHeight || 0)
    })""")
    width, height = int(size['width']), int(size['height'])

    if width <= 2 * margin_px or height <= 2 * margin_px:
        raise RuntimeError(f"Viewport too small for margin ({width}x{height}, margin={margin_px})")

    for _ in range(clicks):
        x = random.randint(margin_px, width - margin_px)
        y = random.randint(margin_px, height - margin_px)

        await page.mouse.move(x, y, steps=random.randint(2, 6))
        await page.mouse.click(x, y, delay=random.randint(min_delay_click_ms, max_delay_click_ms))

        await page.wait_for_timeout(random.randint(min_pause_ms, max_pause_ms))

def _normalize_amount(txt: str) -> str:
    if not txt: return ""
    t = txt.strip().replace("\u00A0", " ")
    t = t.replace(",", "")  
    m = re.findall(r"[0-9.]+", t)
    return m[0] if m else txt.strip()

async def handle_cookie_banner(page):
    """Automatically reject cookies if OneTrust banner appears."""
    try:
        if await page.locator('#onetrust-reject-all-handler').count():
            await page.click('#onetrust-reject-all-handler')
            print("[INFO] Cookie banner detected → clicked Reject All")
            await page.wait_for_timeout(1500)  
        elif await page.locator('#onetrust-accept-btn-handler').count():
            print("[INFO] Cookie banner detected → fallback to Accept All (Reject not found)")
            await page.click('#onetrust-accept-btn-handler')
            await page.wait_for_timeout(1500)
    except Exception as e:
        print(f"[WARN] Cookie banner handling failed: {e}")
async def detect_captcha(page) -> bool:
    try:
        # reCAPTCHA iframes
        if await page.locator('iframe[src*="recaptcha"]').count():
            return True
        if await page.locator('div[class*="recaptcha"], #rc-anchor-container').count():
            return True
        # hCaptcha
        if await page.locator('iframe[src*="hcaptcha.com"]').count():
            return True
        if await page.locator('[data-hcaptcha-response], .h-captcha').count():
            return True
        # generic “I am not a robot” text
        if await page.locator('text=/not\\s+robot/i').count():
            return True
    except Exception:
        pass
    return False

async def attempt_login_once(page, email, password):
    """Fill creds, wait for enabled submit, and submit once.
       If submit can’t be clicked, click random safe spots 2–3× then retry."""
    EMAIL_SEL = 'input[id^="user_authentication_email"]'
    PASS_SEL  = 'input[type="password"]'
    SUBMIT_ANY   = '#login_button button[type="submit"], form button[type="submit"]'
    SUBMIT_READY = '#login_button button[type="submit"]:not([disabled]), form button[type="submit"]:not([disabled])'
    RES_SEL = 'input[id^="audio-response"]'

    if not await page.locator(EMAIL_SEL).count():
        return False

    await page.click(EMAIL_SEL)
    for ch in email:
        await page.keyboard.type(ch, delay=random.randint(50, 150))

    await page.click(PASS_SEL)
    await page.keyboard.type(password, delay=random.randint(60, 120))

    # Wait for submit to enable (if it never does, we’ll still try fallback)
    try:
        await page.locator(SUBMIT_READY).first.wait_for(timeout=5000)
    except Exception:
        print("[WARN] Submit button stayed disabled; continuing anyway")

    # --- Click helper with random-page retries ---
    async def try_click_with_random_retries(target_sel):
        for attempt in range(3):
            try:
                await page.click(target_sel)
                print(f"[INFO] Clicked submit on attempt {attempt+1}")
                return True
            except Exception as e:
                print(f"[WARN] Submit click attempt {attempt+1} failed: {e}")
                # Click at random coordinates inside viewport to “wake up” the UI
                box = await page.viewport_size()
                if box:
                    x = random.randint(int(box['width'] * 0.3), int(box['width'] * 0.7))
                    y = random.randint(int(box['height'] * 0.3), int(box['height'] * 0.7))
                    try:
                        await page.mouse.click(x, y)
                        await page.wait_for_timeout(500)
                        print(f"[INFO] Clicked random point ({x},{y}) to refresh focus")
                    except Exception:
                        pass
        # final fallback
        return False

    # Try enabled button first, else any submit
    clicked = await try_click_with_random_retries(SUBMIT_READY)
    if not clicked and await page.locator(SUBMIT_ANY).count():
        clicked = await try_click_with_random_retries(SUBMIT_ANY)

    # If still nothing, press Enter in password field
    if not clicked:
        print("[WARN] All submit clicks failed → pressing Enter")
        try:
            await page.press(PASS_SEL, "Enter")
        except Exception as e:
            print(f"[WARN] Enter key failed: {e}")

    # Wait for possible navigation
    await page.wait_for_timeout(1500)

    # ✅ Detect CAPTCHA right after clicking submit
    if await detect_captcha(page):
        frame = page.frame_locator('iframe[title^="recaptcha challenge"]')
        # Locate the button by its ID
        button = frame.locator('#recaptcha-audio-button')
        # Wait until it appears and is visible
        await button.wait_for(state='visible')
        # Get the bounding box
        box = await button.bounding_box()
        if box:
            rect = {
                'x': int(box['x']),
                'y': int(box['y']),
                'width': int(box['width']),
                'height': int(box['height'])
            }
            print(rect)
            # Pick a random point inside the clip
            click_x = rect['x'] + random.uniform(0, rect['width'])
            click_y = rect['y'] + random.uniform(0, rect['height'])

            # Click at that position
            await page.mouse.click(click_x, click_y)
        else:
            print("Button not found or not visible.")
        aai.settings.api_key = os.getenv("AAI_KEY")
        audio_file = await frame.locator("#audio-source").evaluate("el => el.src")
        config = aai.TranscriptionConfig(speech_model=aai.SpeechModel.universal)
        transcript = aai.Transcriber(config=config).transcribe(audio_file)
        if transcript.status == "error":
            raise RuntimeError(f"Transcription failed: {transcript.error}")
        print(transcript.text)
        res = frame.locator(RES_SEL)
        await res.first.click()
        await res.type(transcript.text, delay=100)
        # Locate the button by its ID
        button = frame.locator('#recaptcha-verify-button')
        # Wait until it appears and is visible
        await button.wait_for(state='visible')
        # Get the bounding box
        box = await button.bounding_box()
        if box:
            rect = {
                'x': int(box['x']),
                'y': int(box['y']),
                'width': int(box['width']),
                'height': int(box['height'])
            }
            print(rect)
            # Pick a random point inside the clip
            click_x = rect['x'] + random.uniform(0, rect['width'])
            click_y = rect['y'] + random.uniform(0, rect['height'])

            # Click at that position
            await page.mouse.click(click_x, click_y)
        else:
            print("Button not found or not visible.")
    # Wait for network to settle
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1500)
    if not await page.locator(BALANCE_SELECTOR_MAIN).count():
        input()
    return True

async def ensure_logged_in(page, email, password, balance_selector_main):
    """Try up to 5 times: handle cookie, try login, wait for balance; else reload."""
    MAX_TRIES = 5
    for i in range(1, MAX_TRIES + 1):
        print(f"[INFO] Login/balance attempt {i}/{MAX_TRIES}")

        # If the balance is already visible, we’re done
        if await page.locator(balance_selector_main).count():
            print("[INFO] Already logged in (balance visible)")
            return True

        # If a login form exists, attempt to log in once
        if await page.locator('input[id^="user_authentication_email"]').count():
            await attempt_login_once(page, email, password)

        # After potential login, check for balance
        try:
            await page.locator(balance_selector_main).first.wait_for(timeout=8000)
            print("[INFO] Balance located → login succeeded")
            return True
        except Exception:
            print("[WARN] Balance not visible yet → reloading page")
            try:
                await page.reload(wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(1500)
                # Handle cookie popup again if it reappears
                await handle_cookie_banner(page)
            except Exception as e:
                print(f"[WARN] Reload failed: {e}")

    return False

async def _fetch_balance():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--start-maximized"
                "--no-sandbox",                # required in most Docker runs
                "--disable-dev-shm-usage",     # use /tmp if /dev/shm is small
                "--disable-gpu",               # safer on VMs
                "--disable-software-rasterizer"
            ]
        )
        page = await browser.new_page()
        stealth_js = r"""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [{name:'Chrome PDF Plugin'}] });
        Object.defineProperty(navigator, 'mimeTypes', { get: () => [{type:'application/pdf'}] });
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.__proto__.query = function(parameters) {
        if (parameters && parameters.name === 'notifications') {
            return Promise.resolve({ state: Notification.permission });
        }
        return origQuery.apply(this, [parameters]);
        };

        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris';
        return getParameter.call(this, parameter);
        };
        """
        await page.add_init_script(stealth_js)
        await page.goto(DASHBOARD_URL, wait_until="networkidle")

        # 1) Cookie banner
        await handle_cookie_banner(page)

        # Simulate 3 random exploratory clicks
        await random_clicks_any_resolution(page, clicks=3)

        # 2) Ensure logged in (with retry/refresh logic)
        ok = await ensure_logged_in(page, NETELLER_EMAIL, NETELLER_PASS, BALANCE_SELECTOR_MAIN)
        if not ok:
            await browser.close()
            raise RuntimeError("Login failed after 5 attempts; check credentials/2FA or selectors.")

        # 3) Extract the split balance
        try:
            main_el = page.locator(BALANCE_SELECTOR_MAIN).first
            await main_el.wait_for(timeout=30000)
            main_text = (await main_el.text_content() or "").strip()

            dec_text = ""
            dec_el = page.locator(BALANCE_SELECTOR_DEC).first
            if await dec_el.count():
                dec_text = (await dec_el.text_content() or "").strip()

            balance_text = _normalize_amount(main_text + dec_text)
        except PwTimeout:
            await browser.close()
            raise RuntimeError("Balance element not found; update BALANCE_SELECTOR_MAIN/_DEC")

        # 4) Currency (best-effort)
        currency_text = "USD"
        try:
            cur_el = page.locator(CURRENCY_SELECTOR).first
            if await cur_el.count():
                currency_text = (await cur_el.text_content() or "").strip() or "USD"
        except Exception:
            pass

        await browser.close()
        return balance_text, currency_text

def _push_to_sheet(balance, currency):
    if not WEBAPP_URL or not WEBAPP_TOKEN:
        raise RuntimeError("WEBAPP_URL/WEBAPP_TOKEN not configured")
    payload = {"neteller": str(balance)+str(currency), "vps": VPS}
    r = requests.post(WEBAPP_URL, json=payload, timeout=25)
    r.raise_for_status()
    print(f"[OK] {datetime.now().isoformat(timespec='seconds')} -> {balance} {currency}")

if __name__ == "__main__":
    maybe_self_update()
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    _copy_host_profile_once()
    bal, cur = asyncio.run(_fetch_balance())
    _push_to_sheet(bal, cur)