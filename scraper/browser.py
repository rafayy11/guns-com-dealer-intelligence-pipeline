"""
SeleniumBase UC browser session with age-gate and Cloudflare CAPTCHA handling.

Uses seleniumbase.undetected.Chrome directly (bypasses SB() startup hang in WSL2).
Provides a thin BrowserWrapper with the same interface our scraper modules expect.
"""

import os
import random
import stat
import time
from pathlib import Path

from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

_browser = None

CAPTCHA_TITLE_KEYWORDS = [
    "just a moment",
    "checking your browser",
    "cloudflare",
    "attention required",
    "ddos protection",
    "access denied",
    "one more step",
]

DELAY_MIN = float(os.getenv("DELAY_MIN", "1.5"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "3.5"))

# Paths to Chrome for Testing + uc_driver installed via `seleniumbase install chrome`
_SB_DRIVERS = Path.home() / ".local/lib" / f"python{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}" / "site-packages/seleniumbase/drivers"
_CHROME_BIN  = _SB_DRIVERS / "chrome-linux64/chrome"
_UC_DRIVER   = _SB_DRIVERS / "uc_driver"


class BrowserWrapper:
    """Thin wrapper around the raw UC WebDriver providing SB-compatible methods."""

    def __init__(self, driver):
        self._driver = driver

    # ── Navigation ──────────────────────────────────────────────────────────
    def open(self, url: str) -> None:
        self._driver.get(url)

    def get_title(self) -> str:
        return self._driver.title or ""

    def get_current_url(self) -> str:
        return self._driver.current_url or ""

    def get_page_source(self) -> str:
        return self._driver.page_source or ""

    def execute_script(self, script: str, *args):
        return self._driver.execute_script(script, *args)

    # ── Element lookup ───────────────────────────────────────────────────────
    def find_element(self, selector: str):
        return self._driver.find_element(By.CSS_SELECTOR, selector)

    def find_elements(self, selector: str):
        return self._driver.find_elements(By.CSS_SELECTOR, selector)

    def is_element_visible(self, selector: str) -> bool:
        try:
            el = self._driver.find_element(By.CSS_SELECTOR, selector)
            return el.is_displayed()
        except Exception:
            return False

    def wait_for_element(self, selector: str, timeout: int = 10):
        return WebDriverWait(self._driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

    def click(self, selector: str) -> None:
        el = self._driver.find_element(By.CSS_SELECTOR, selector)
        el.click()

    def cdp_cmd(self, cmd: str, params: dict = None):
        """Execute a Chrome DevTools Protocol command on the underlying driver."""
        return self._driver.execute_cdp_cmd(cmd, params or {})

    def quit(self) -> None:
        try:
            self._driver.quit()
        except Exception:
            pass


def _ensure_chrome_executable() -> None:
    if _CHROME_BIN.parent.exists():
        for f in _CHROME_BIN.parent.iterdir():
            if f.is_file():
                f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _is_captcha_page(wrapper: BrowserWrapper) -> bool:
    try:
        title = wrapper.get_title().lower()
        url   = wrapper.get_current_url().lower()
        # Only flag Cloudflare-specific URL patterns, not product URLs that
        # happen to contain words like "challenge" in the path.
        cf_url = "/cdn-cgi/" in url or "cloudflare.com" in url
        return any(k in title for k in CAPTCHA_TITLE_KEYWORDS) or cf_url
    except Exception:
        return False


def _handle_captcha(wrapper: BrowserWrapper) -> None:
    while _is_captcha_page(wrapper):
        print("\n" + "=" * 60)
        print("  CAPTCHA / CLOUDFLARE CHALLENGE DETECTED")
        print("  Solve it in the Chrome window that opened,")
        print("  then press ENTER here to continue...")
        print("=" * 60)
        try:
            input("  >>> Press ENTER after solving: ")
        except EOFError:
            print("  [non-interactive] Waiting 10s for challenge to auto-resolve...")
            time.sleep(10)
        time.sleep(1.5)
    print("  Challenge passed. Continuing...\n")


def _handle_age_gate(wrapper: BrowserWrapper) -> None:
    try:
        for selector in [
            "button[data-testid='age-gate-yes']",
            "button[data-testid='age-gate-submit']",
        ]:
            if wrapper.is_element_visible(selector):
                wrapper.click(selector)
                return
        # Fallback: any button/link containing YES text
        for tag in ["button", "a"]:
            for el in wrapper.find_elements(tag):
                try:
                    if el.text.strip().upper() in ("YES", "YES, I'M 18+", "I'M 18+", "ENTER"):
                        el.click()
                        return
                except Exception:
                    continue
    except Exception:
        pass


def get_browser(headless: bool = False) -> BrowserWrapper:
    global _browser
    if _browser is not None:
        return _browser

    import seleniumbase.undetected as uc

    headless_flag = headless or os.getenv("HEADLESS", "false").lower() == "true"

    _ensure_chrome_executable()

    options = uc.ChromeOptions()

    # Persistent profile — keeps VPN extension installed between runs.
    # Located at ./chrome_profile/ inside the project directory.
    profile_dir = Path(__file__).parent.parent / "chrome_profile"
    profile_dir.mkdir(exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")

    # Use locally installed Chrome for Testing
    if _CHROME_BIN.exists():
        options.binary_location = str(_CHROME_BIN)

    # Required for WSL2: no sandboxing + disable crashpad
    for arg in [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
    ]:
        options.add_argument(arg)

    if headless_flag:
        options.add_argument("--headless=new")

    driver_path = str(_UC_DRIVER) if _UC_DRIVER.exists() else None

    driver = uc.Chrome(
        options=options,
        driver_executable_path=driver_path,
        use_subprocess=True,
    )

    _browser = BrowserWrapper(driver)

    # Check if this is a first-time run (profile is fresh, VPN not yet installed)
    _maybe_prompt_vpn_setup(_browser)

    # Prime the session: load guns.com, handle age gate.
    # If the VPN extension needs to reconnect, wait for the user.
    for attempt in range(1, 4):
        try:
            _browser.open("https://www.guns.com")
            break
        except Exception as e:
            if "ERR_PROXY_CONNECTION_FAILED" in str(e) or "proxy" in str(e).lower():
                print("\n" + "=" * 60)
                print("  VPN PROXY NOT CONNECTED")
                print("  The Chrome window just opened.")
                print("  Click the VPN extension icon, turn it ON,")
                print("  set location to United States, then press ENTER here.")
                print("=" * 60)
                try:
                    input("  >>> Press ENTER when VPN is ON: ")
                except EOFError:
                    time.sleep(5)
            else:
                raise
    time.sleep(2)
    _handle_captcha(_browser)
    _handle_age_gate(_browser)
    time.sleep(1)

    return _browser


def _maybe_prompt_vpn_setup(wrapper: BrowserWrapper) -> None:
    """
    On first run (fresh profile), prompt the user to install the VPN extension.
    A marker file is written after setup so this prompt never appears again.
    """
    marker = Path(__file__).parent.parent / "chrome_profile" / ".vpn_setup_done"
    if marker.exists():
        return  # VPN already set up in a previous run

    print("\n" + "=" * 60)
    print("  FIRST-TIME SETUP — VPN REQUIRED")
    print("=" * 60)
    print("  guns.com requires a US IP address.")
    print("  A Chrome window has opened.")
    print()
    print("  Please do the following:")
    print("  1. Install a free VPN extension (e.g. Windscribe, ProtonVPN,")
    print("     or browsec — search Chrome Web Store for 'free VPN')")
    print("  2. Set the location to United States")
    print("  3. Turn the VPN ON")
    print("  4. Come back here and press ENTER")
    print()
    print("  This is a ONE-TIME setup. The VPN will stay installed")
    print("  in the saved Chrome profile for all future runs.")
    print("=" * 60)
    try:
        input("  >>> Press ENTER when VPN is ON and set to US: ")
    except EOFError:
        time.sleep(5)

    # Write the marker so we never ask again
    marker.write_text("vpn setup complete\n")
    print("  VPN setup saved. Future runs will skip this step.\n")


def safe_get(wrapper: BrowserWrapper, url: str, retries: int = 3) -> bool:
    """Navigate to url, handle CAPTCHA, retry on network errors."""
    for attempt in range(1, retries + 1):
        try:
            wrapper.open(url)
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            _handle_age_gate(wrapper)
            _handle_captcha(wrapper)
            return True
        except Exception as exc:
            if attempt == retries:
                print(f"  [WARN] Failed to load {url} after {retries} attempts: {exc}")
                return False
            wait = 2 ** attempt
            print(f"  [RETRY {attempt}/{retries}] {exc} — waiting {wait}s")
            time.sleep(wait)
    return False


def random_delay() -> None:
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def close_browser() -> None:
    global _browser
    if _browser is not None:
        _browser.quit()
        _browser = None
