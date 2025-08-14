import asyncio
import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import time
from typing import List, Optional, Tuple, Dict, Set

from playwright.async_api import async_playwright, TimeoutError, Page, Browser, BrowserContext, ElementHandle


DEFAULT_URL = "https://hiring.amazon.ca/search/warehouse-jobs#/"
DEFAULT_STATE_FILE = "./amazon_state.json"

# Heuristic selector candidates (site is a JS SPA and changes markup; we try multiple)
JOB_CARD_SELECTORS = [
    # Try data-testid patterns first
    "[data-testid*='job']",
    "[data-test*='job']",
    # Common card-like containers
    "div[class*='job']:has(a)",
    "div[class*='tile']:has(a)",
    "li:has(a)",
    "article:has(a)",
    # Fallback: anything with an Apply inside
    "div:has(:text-matches('Apply', 'i'))",
]

APPLY_WITHIN_CARD_SELECTORS = [
    "a:has-text('Apply')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply')",
    "button:has-text('Apply Now')",
    "a[href*='apply']",
    "button[data-cta*='apply']",
]

DETAIL_APPLY_SELECTORS = [
    "a:has-text('Apply')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply')",
    "button:has-text('Apply Now')",
    "[data-testid*='apply']",
    "a[href*='apply']",
]


def cmd_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def notify(title: str, message: str) -> None:
    """
    Desktop notification if 'notify-send' exists (XFCE usually has it).
    Falls back to printing and terminal bell.
    """
    if cmd_exists("notify-send"):
        try:
            subprocess.run(["notify-send", title, message], check=False)
            return
        except Exception:
            pass
    # fallback
    sys.stdout.write("\a")  # terminal bell
    sys.stdout.flush()
    print(f"[NOTIFY] {title}: {message}")


def clean_text(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


async def capture_state(context: BrowserContext, path: str) -> None:
    await context.storage_state(path=path)


async def ensure_context(p, headless: bool, state_file: str, no_sandbox: bool) -> Tuple[Browser, BrowserContext]:
    launch_args = {}
    if no_sandbox:
        launch_args["args"] = ["--no-sandbox"]
    browser: Browser = await p.chromium.launch(headless=headless, **launch_args)

    if os.path.exists(state_file):
        context: BrowserContext = await browser.new_context(storage_state=state_file)
    else:
        context = await browser.new_context()
    return browser, context


async def wait_for_user_enter(prompt: str = "Press ENTER here after you finish logging in...") -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: input(prompt))


async def first_run_login_flow(url: str, state_file: str, headless: bool, no_sandbox: bool) -> None:
    async with async_playwright() as p:
        browser, context = await ensure_context(p, headless=False, state_file=state_file, no_sandbox=no_sandbox)
        page = await context.new_page()
        print("[INFO] Opening the Amazon jobs site for login...")
        await page.goto(url, wait_until="domcontentloaded")
        print("[ACTION] Log in if required in the opened browser window.")
        print("[ACTION] Once you can see job listings and are logged in, come back to the terminal.")
        await wait_for_user_enter()
        await capture_state(context, state_file)
        print(f"[SUCCESS] Saved login session to {state_file}. You can close the browser.")
        await context.close()
        await browser.close()


async def pick_working_job_selector(page: Page) -> Optional[str]:
    """Try selector candidates until one returns elements that look like job tiles."""
    for sel in JOB_CARD_SELECTORS:
        try:
            handles = await page.query_selector_all(sel)
            if len(handles) == 0:
                continue

            # sanity check: at least some have text and a link
            good = 0
            for h in handles[:8]:
                txt = clean_text(await h.text_content())
                link = await h.query_selector("a")
                if len(txt) >= 5 and link:
                    good += 1
            if good >= 1:
                print(f"[INFO] Using job card selector: {sel}")
                return sel
        except Exception:
            continue
    return None


async def extract_job_items(page: Page, job_card_selector: str) -> List[Dict]:
    items = []
    cards = await page.query_selector_all(job_card_selector)
    for c in cards:
        try:
            txt = clean_text(await c.text_content())
            # prefer a meaningful anchor inside the card
            link_el = await c.query_selector("a[href]")
            href = await link_el.get_attribute("href") if link_el else None
            # make an id to dedupe
            job_id = (txt[:180] + " | " + (href or "")).lower()
            items.append({"el": c, "text": txt, "href": href, "id": job_id})
        except Exception:
            continue
    return items


def text_matches(text: str, include_keywords: List[str], exclude_keywords: List[str]) -> bool:
    lower = text.lower()
    if include_keywords and not any(k.strip().lower() in lower for k in include_keywords):
        return False
    if exclude_keywords and any(x.strip().lower() in lower for x in exclude_keywords):
        return False
    return True


async def try_click_apply_within_card(card: ElementHandle) -> bool:
    # Try various apply selectors inside the card
    for sel in APPLY_WITHIN_CARD_SELECTORS:
        try:
            target = await card.query_selector(sel)
            if target:
                await target.click()
                return True
        except Exception:
            continue
    # Try role-based match
    try:
        btn = await card.get_by_role("button", name=re.compile("apply", re.I))
        await btn.click()
        return True
    except Exception:
        pass
    try:
        link = await card.get_by_role("link", name=re.compile("apply", re.I))
        await link.click()
        return True
    except Exception:
        pass
    return False


async def try_click_apply_on_detail(page: Page) -> bool:
    # Wait a moment for detail to load then try to click apply
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except TimeoutError:
        pass

    # Try several selectors
    for sel in DETAIL_APPLY_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                return True
        except Exception:
            continue

    # Role-based
    try:
        btn = page.get_by_role("button", name=re.compile("apply", re.I))
        await btn.first.click()
        return True
    except Exception:
        pass

    try:
        link = page.get_by_role("link", name=re.compile("apply", re.I))
        await link.first.click()
        return True
    except Exception:
        pass

    # As a last resort click any visible element with "Apply"
    try:
        el = await page.query_selector(":text-matches('Apply', 'i')")
        if el:
            await el.click()
            return True
    except Exception:
        pass
    return False


async def soft_hard_refresh(page: Page, url: str) -> None:
    # Rarely do a soft refresh to avoid stale DOM (not frequent to avoid rate-limit)
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(200)
        await page.evaluate("location.reload()")
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception:
            pass


async def run_watcher(
    url: str,
    include_keywords: List[str],
    exclude_keywords: List[str],
    min_interval: int,
    max_interval: int,
    state_file: str,
    dry_run: bool,
    keep_running: bool,
    headless: bool,
    no_sandbox: bool,
    hard_refresh_every: int,
    notify_on_match: bool,
) -> None:
    assert min_interval >= 1 and max_interval >= min_interval
    print("[START] Amazon Job Watcher starting...")
    print(f"        URL: {url}")
    print(f"        Include keywords: {include_keywords or ['<none>']}")
    print(f"        Exclude keywords: {exclude_keywords or ['<none>']}")
    print(f"        Interval (sec): {min_interval}–{max_interval}")
    print(f"        Storage state: {state_file} ({'exists' if os.path.exists(state_file) else 'not found'})")
    if dry_run:
        print("        Mode: DRY RUN (won't click Apply)")

    last_hard_refresh = time.time()
    seen_ids: Set[str] = set()
    backoff = 5

    async with async_playwright() as p:
        browser, context = await ensure_context(p, headless=headless, state_file=state_file, no_sandbox=no_sandbox)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        # pick a selector that works on the current markup
        job_card_selector = await pick_working_job_selector(page)
        if not job_card_selector:
            print("[WARN] Could not auto-detect job card selector. Will still attempt a generic scan.")
            job_card_selector = "a"  # worst-case fallback

        print("[INFO] Watching page without aggressive reloads to avoid rate limits...")

        while True:
            try:
                # occasional soft/hard refresh to keep the view fresh
                if hard_refresh_every > 0 and (time.time() - last_hard_refresh) > hard_refresh_every:
                    print("[INFO] Performing a rare soft refresh to keep DOM fresh...")
                    await soft_hard_refresh(page, url)
                    last_hard_refresh = time.time()
                    # re-pick selector after refresh
                    job_card_selector = await pick_working_job_selector(page) or job_card_selector

                # scan DOM
                items = await extract_job_items(page, job_card_selector)

                # If fallback "a" returns too many, cap to something sane
                if job_card_selector == "a" and len(items) > 500:
                    items = items[:500]

                new_items = [it for it in items if it["id"] not in seen_ids]
                for it in new_items:
                    seen_ids.add(it["id"])

                # Check new/changed items for matches first
                candidates = new_items or items  # prefer new; if none, scan all
                matched_any = False

                for it in candidates:
                    if not it["text"]:
                        continue
                    if text_matches(it["text"], include_keywords, exclude_keywords):
                        matched_any = True
                        short = (it["text"][:160] + "...")
                        print(f"[MATCH] {short}")
                        if notify_on_match:
                            notify("Amazon Job Found", short)

                        if dry_run:
                            print("[DRY] Would click Apply here. Continuing...")
                            if not keep_running:
                                await context.close()
                                await browser.close()
                                return
                            else:
                                continue

                        # Try clicking Apply within the card
                        clicked = await try_click_apply_within_card(it["el"])
                        if not clicked:
                            # As fallback, open the link inside the card (job details)
                            try:
                                if it["href"]:
                                    # open details in same tab
                                    await page.goto(it["href"], wait_until="domcontentloaded")
                                else:
                                    # try clicking the card itself
                                    await it["el"].click()
                            except Exception:
                                pass

                            # then try to click apply in details
                            clicked = await try_click_apply_on_detail(page)

                        if clicked:
                            print("[ACTION] Clicked Apply.")
                            if notify_on_match:
                                notify("Amazon Apply Clicked", "Attempted to open/apply to matched job.")
                            if not keep_running:
                                print("[DONE] Exiting after first successful click.")
                                await context.close()
                                await browser.close()
                                return
                            else:
                                # After clicking, you may be on a new page; navigate back to keep watching
                                try:
                                    await page.go_back()
                                except Exception:
                                    # if back fails, reopen the main listing URL
                                    try:
                                        await page.goto(url, wait_until="domcontentloaded")
                                    except Exception:
                                        pass
                                # refresh selector, DOM may have changed
                                job_card_selector = await pick_working_job_selector(page) or job_card_selector
                        else:
                            print("[INFO] Could not find an Apply button yet. Will keep watching.")

                # Randomized delay to look human (prevents rate-limiting)
                wait_s = random.randint(min_interval, max_interval)
                print(f"[IDLE] No new match. Checking again in ~{wait_s}s...")
                await asyncio.sleep(wait_s)
                backoff = 5  # reset backoff on success

            except Exception as e:
                print(f"[ERROR] {e}")
                # cooldown/backoff if CloudFront or transient errors
                print(f"[COOLDOWN] Waiting {backoff}s before retry...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)  # cap backoff at 2 minutes

        # end while


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Amazon Auto Refresh + Apply (DOM Watcher, anti-block)"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Jobs listing URL")
    parser.add_argument("--keywords", default="warehouse,picker,associate", help="Comma-separated include keywords")
    parser.add_argument("--exclude", default="", help="Comma-separated exclude keywords")
    parser.add_argument("--min-interval", type=int, default=6, help="Minimum seconds between scans")
    parser.add_argument("--max-interval", type=int, default=14, help="Maximum seconds between scans")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Playwright storage_state JSON path")
    parser.add_argument("--login", action="store_true", help="Open browser to login and save state, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except clicking Apply")
    parser.add_argument("--keep-running", action="store_true", help="Keep watching after first Apply click")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (not recommended for login)")
    parser.add_argument("--no-sandbox", action="store_true", help="Chromium --no-sandbox (use if running as root)")
    parser.add_argument("--notify", action="store_true", help="Desktop notify on match (uses notify-send if available)")
    parser.add_argument("--hard-refresh-every", type=int, default=600, help="Occasional soft refresh every N seconds (0=never)")

    args = parser.parse_args()

    if args.max_interval < args.min_interval:
        parser.error("--max-interval must be >= --min-interval")

    return args


async def main_async():
    args = parse_args()

    include_keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    exclude_keywords = [k.strip() for k in args.exclude.split(",") if k.strip()]

    if args.login:
        await first_run_login_flow(args.url, args.state_file, args.headless, args.no_sandbox)
        return

    await run_watcher(
        url=args.url,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
        state_file=args.state_file,
        dry_run=args.dry_run,
        keep_running=args.keep_running,
        headless=args.headless,
        no_sandbox=args.no_sandbox,
        hard_refresh_every=args.hard_refresh_every,
        notify_on_match=args.notify,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[EXIT] Stopped by user.")

