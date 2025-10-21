import os, asyncio, re, shutil, math, time
from pathlib import Path
from datetime import datetime
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
import random
import json
import assemblyai as aai

load_dotenv()

WEBAPP_URL   = os.getenv("WEBAPP_URL", "")
WEBAPP_TOKEN = os.getenv("WEBAPP_TOKEN", "")

NETELLER_EMAIL = os.getenv("NETELLER_EMAIL", "")
NETELLER_PASS  = os.getenv("NETELLER_PASS", "")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://member.neteller.com/wallet/ng/dashboard")

# Where Playwright will actually run (must be writable)
USER_DATA_DIR = os.getenv("USER_DATA_DIR", "/app/user-data")
# Where the host Chrome profile is mounted (read-only)
HOST_PROFILE_DIR = os.getenv("HOST_PROFILE_DIR", "/app/host-profile")

BALANCE_SELECTOR_MAIN = os.getenv("BALANCE_SELECTOR_MAIN", ".ps-digits-1.balance-amount")
BALANCE_SELECTOR_DEC  = os.getenv("BALANCE_SELECTOR_DEC", ".ps-digits-2")
CURRENCY_SELECTOR     = os.getenv("CURRENCY_SELECTOR", ".balance-currency")

SYNC_MARKER = Path(USER_DATA_DIR) / ".host_profile_synced"

clips = []

VPS = os.getenv("VPS")

def _copy_host_profile_once():
    """Copy the mounted host Chrome profile into USER_DATA_DIR (first run only)."""
    src = Path(HOST_PROFILE_DIR)
    dst = Path(USER_DATA_DIR)

    if not src.exists():
        print("[INFO] No host profile mounted; using existing/warm Playwright profile.")
        return

    if SYNC_MARKER.exists():
        # Already synced before; keep using existing USER_DATA_DIR
        return

    if any(dst.iterdir()):
        # USER_DATA_DIR not empty: don't overwrite; but mark to avoid repeated checks
        SYNC_MARKER.touch()
        print("[INFO] USER_DATA_DIR already has data; skipping initial sync.")
        return

    print(f"[INFO] Syncing host profile from {src} -> {dst} (one-time)")
    def _ignore(dirpath, names):
        # skip bulky caches
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
    # Get current window/viewport size
    size = await page.evaluate("""() => ({
        width: Math.max(document.documentElement.clientWidth, window.innerWidth || 0),
        height: Math.max(document.documentElement.clientHeight, window.innerHeight || 0)
    })""")
    width, height = int(size['width']), int(size['height'])

    if width <= 2 * margin_px or height <= 2 * margin_px:
        raise RuntimeError(f"Viewport too small for margin ({width}x{height}, margin={margin_px})")

    for _ in range(clicks):
        # Pick random coordinates inside page (safe margin)
        x = random.randint(margin_px, width - margin_px)
        y = random.randint(margin_px, height - margin_px)

        # Move and click
        await page.mouse.move(x, y, steps=random.randint(2, 6))
        await page.mouse.click(x, y, delay=random.randint(min_delay_click_ms, max_delay_click_ms))

        # Random short pause
        await page.wait_for_timeout(random.randint(min_pause_ms, max_pause_ms))

def _normalize_amount(txt: str) -> str:
    if not txt: return ""
    # remove spaces, thousand separators, keep digits and dot/comma then standardize
    t = txt.strip().replace("\u00A0", " ")
    t = t.replace(",", "")  # assumes dot decimal; change if your locale uses comma decimal
    # keep digits and dot only
    m = re.findall(r"[0-9.]+", t)
    return m[0] if m else txt.strip()

async def handle_cookie_banner(page):
    """Automatically reject cookies if OneTrust banner appears."""
    try:
        # Wait briefly for the cookie banner to load
        if await page.locator('#onetrust-reject-all-handler').count():
            await page.click('#onetrust-reject-all-handler')
            print("[INFO] Cookie banner detected → clicked Reject All")
            await page.wait_for_timeout(1500)  # small pause to let it disappear
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

    # # ✅ Detect CAPTCHA right after clicking submit
    # if await detect_captcha(page):
    #     input()
    #     count = 0
    #     old_extracted_list = []
    #     while True:
    #         img_path = await capture_full_page(page)
    #         if count == 0:
    #             await capture_recaptcha_tile_positions(page, pad=2)
    #         try:
    #             extracted_list = send_image_to_gemini(img_path, "Identify all tiles in the grid that contain the specify object ASAP, ignore the uncertain tile or blank tile or low opacity tile and return only the indices in a list from left to right, top to bottom, starting at 0. Response with empty list [] if the object is not present in ANY of the tiles")
    #             #extracted_list = send_image_to_gemini(img_path, "if you have to verify, which picture will you choose? reply with only a list of indices from left to right, up to down start from 0, and return empty list if there're none")
    #         except Exception as e:
    #             print("[WARN] Gemini send failed:", e)
    #         s = set(extracted_list)
    #         if extracted_list != [] or s == old_extracted_list:
    #             # Shuffle the order randomly
    #             random.shuffle(extracted_list)
    #             for idx in extracted_list:
    #                 # Pick a random point inside the clip
    #                 click_x = clips[idx]['x'] + random.uniform(0, clips[idx]['width'])
    #                 click_y = clips[idx]['y'] + random.uniform(0, clips[idx]['height'])

    #                 # Click at that position
    #                 await page.mouse.click(int(click_x), int(click_y))
    #                 await asyncio.sleep(random.uniform(1, 1.5))
    #             await asyncio.sleep(random.uniform(0.5, 1.5))
    #         else:
    #             break
    #         count +=1
    #         if count >= 10:
    #             raise Exception("Loop exceeded maximum count of 10")
    #         old_extracted_list = set(extracted_list)

    #     frame = page.frame_locator('iframe[title^="recaptcha challenge"]')
    #     # Locate the button by its ID
    #     button = frame.locator('#recaptcha-verify-button')
    #     # Wait until it appears and is visible
    #     await button.wait_for(state='visible')
    #     # Get the bounding box
    #     box = await button.bounding_box()
    #     if box:
    #         rect = {
    #             'x': int(box['x']),
    #             'y': int(box['y']),
    #             'width': int(box['width']),
    #             'height': int(box['height'])
    #         }
    #         print(rect)
    #         # Pick a random point inside the clip
    #         click_x = rect['x'] + random.uniform(0, rect['width'])
    #         click_y = rect['y'] + random.uniform(0, rect['height'])

    #         # Click at that position
    #         await page.mouse.click(click_x, click_y)
    #     else:
    #         print("Button not found or not visible.")
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



# async def locate_captcha_images(page, save_tiles=True, max_tiles=50):
    """
    Find captcha-related image tiles on the page and (optionally) save clipped screenshots.
    Returns list of dicts: {source, x, y, width, height, filename}
    """
    results = []

    # Helper: selectors to try on the main page (non-iframe)
    selectors = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "div.rc-imageselect-tile img",        # google recaptcha older
        "div.rc-image-tile-wrapper img",
        "img[alt*='captcha']",
        "img[src*='captcha']",
        ".h-captcha img",
        "canvas",                              # sometimes tiles are rendered to canvas
        ".captcha img",
        ".cf-turnstile img"
    ]

    # 1) Search inside iframes first (common for reCAPTCHA/hCaptcha)
    iframe_selectors = ["iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", "iframe"]
    seen_frames = set()
    for sel in iframe_selectors:
        # find iframe elements on the main page
        for iframe_el in await page.query_selector_all(sel):
            try:
                frame = await iframe_el.content_frame()
            except Exception:
                frame = None
            if not frame:
                continue
            frame_id = frame.url or str(iframe_el)  # best-effort id
            if frame_id in seen_frames:
                continue
            seen_frames.add(frame_id)

            # try to collect img/canvas elements inside this frame
            # Use an evaluate to return bounding rects relative to viewport (window)
            # We evaluate in the frame context so getBoundingClientRect is relative to that frame's viewport;
            # but Playwright maps those coordinates to the main page viewport for screenshots/clipping.
            tiles = await frame.eval_on_selector_all(
                "img, canvas",
                """
                els => els.map(el => {
                    const r = el.getBoundingClientRect();
                    const visible = (r.width > 2 && r.height > 2);
                    return {
                        tag: el.tagName.toLowerCase(),
                        src: el.tagName.toLowerCase() === 'img' ? (el.currentSrc || el.src) : null,
                        x: Math.round(r.x), y: Math.round(r.y),
                        width: Math.round(r.width), height: Math.round(r.height),
                        visible
                    };
                })
                """
            )

            # Save each visible tile
            for i, t in enumerate(tiles):
                if not t["visible"]:
                    continue
                if len(results) >= max_tiles:
                    break
                # Compose filename
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                filename = None
                if save_tiles:
                    filename = os.path.join(TILE_DIR, f"tile-{ts}-frame-{len(results)}.png")
                    # Use page.screenshot with clip — coordinates are viewport-based
                    # Add a small padding, but keep within viewport limits
                    clip = {
                        "x": max(0, t["x"] - 2),
                        "y": max(0, t["y"] - 2),
                        "width": max(1, t["width"] + 4),
                        "height": max(1, t["height"] + 4)
                    }
                    # Crop must be integers
                    clip = {k: int(math.floor(v)) for k, v in clip.items()}
                    try:
                        await page.screenshot(path=filename, clip=clip)
                    except Exception as e:
                        # if clip fails (sometimes due to off-screen), try scrollIntoView + retry
                        try:
                            await frame.evaluate("(el)=>el.scrollIntoView({block:'center',inline:'center'})", await frame.query_selector_all("img,canvas")[i])
                            await page.wait_for_timeout(300)
                            await page.screenshot(path=filename, clip=clip)
                        except Exception:
                            filename = None

                results.append({
                    "source": "iframe:"+frame.url,
                    "tag": t["tag"],
                    "src": t.get("src"),
                    "x": t["x"], "y": t["y"],
                    "width": t["width"], "height": t["height"],
                    "filename": filename
                })

    # 2) Search on the main page (outside iframes) using selectors above
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
        except Exception:
            els = []
        for el in els:
            if len(results) >= max_tiles:
                break
            # get bounding rect via evaluate on the element
            rect = await el.evaluate(
                "(e)=>{ const r=e.getBoundingClientRect(); return {x:Math.round(r.x), y:Math.round(r.y), width:Math.round(r.width), height:Math.round(r.height), tag:e.tagName.toLowerCase(), src: e.tagName.toLowerCase()==='img' ? (e.currentSrc||e.src) : null}; }"
            )
            if rect["width"] < 2 or rect["height"] < 2:
                continue
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = None
            if save_tiles:
                os.makedirs(TILE_DIR, exist_ok=True)
                filename = os.path.join(TILE_DIR, f"tile-{ts}-main-{len(results)}.png")
                clip = {
                    "x": max(0, rect["x"] - 2),
                    "y": max(0, rect["y"] - 2),
                    "width": max(1, rect["width"] + 4),
                    "height": max(1, rect["height"] + 4)
                }
                clip = {k: int(math.floor(v)) for k, v in clip.items()}
                try:
                    await page.screenshot(path=filename, clip=clip)
                except Exception:
                    # try scroll + retry
                    try:
                        await el.scroll_into_view_if_needed()
                        await page.wait_for_timeout(300)
                        await page.screenshot(path=filename, clip=clip)
                    except Exception:
                        filename = None

            results.append({
                "source": "page",
                "tag": rect["tag"],
                "src": rect.get("src"),
                "x": rect["x"], "y": rect["y"],
                "width": rect["width"], "height": rect["height"],
                "filename": filename
            })

    return results
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
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    _copy_host_profile_once()
    bal, cur = asyncio.run(_fetch_balance())
    _push_to_sheet(bal, cur)