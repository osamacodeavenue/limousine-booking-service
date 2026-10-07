"""
Sixt Driver Company Portal - ride scraper + auto-accept bot (Selenium, headless)

Rule: payout > MIN_PAYOUT  -> click "Accept"
      otherwise            -> skip (the portal has no Reject button; the ride is simply left alone)

SETUP
  pip install selenium
  (Selenium 4.6+ downloads the matching chromedriver automatically.)

LOGIN (one time)
  The script reuses a dedicated Chrome profile so it stays logged in.
  1. Run once with a visible browser:   python sixt_ride_bot.py --login
  2. Log in to the portal in the window that opens, then press Enter in the terminal.
  3. After that, run normally (headless):  python sixt_ride_bot.py

SAFETY
  DRY_RUN = True by default: it only prints what it WOULD accept.
  Set DRY_RUN = False (or pass --live) when you're happy with the output.

ADMIN PORTAL
  --punch  also creates every accepted ride as a booking in the Dreams Star admin
           portal right after the scan (uses dreamsstar_punch.py and its login profile).
"""

import argparse
import csv
import os
import re
import time
from datetime import datetime

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------- CONFIG ----------------
BASE_URL = "https://dcp.orange.sixt.com/availableRides"
MIN_PAYOUT = 27.00            # accept only if payout is ABOVE this (USD)
POLL_SECONDS = 30             # how often to re-check for new rides
MAX_PAGES = 10                # safety cap on pagination
DRY_RUN = True                # True = don't click, just log
PROFILE_DIR = os.path.abspath("./sixt_chrome_profile")
LOG_FILE = "rides_log.csv"
# ----------------------------------------


def make_driver(headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=opts)


def parse_payout(text: str) -> float | None:
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def scrape_page(driver) -> list[dict]:
    """Return rides on the current page. Table columns:
    0 Date/Time & Status | 1 Ride Info | 2 Pickup & Drop | 3 Payout | 4 Comments | 5 Action"""
    rides = []
    for row in driver.find_elements(By.CSS_SELECTOR, "table tbody tr"):
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 6:
            continue
        info_lines = [l for l in cells[1].text.splitlines() if l.strip()]
        route_lines = [l for l in cells[2].text.splitlines() if l.strip()]
        distance = next((l for l in route_lines if "Km" in l), "")

        rides.append({
            "datetime": clean(cells[0].text.splitlines()[0]) if cells[0].text else "",
            "ride_id": info_lines[0] if info_lines else "",
            "vehicle": info_lines[1] if len(info_lines) > 1 else "",
            "route": clean(cells[2].text),
            "distance": distance,
            "payout_text": clean(cells[3].text),
            "payout": parse_payout(cells[3].text),
            "comments": clean(cells[4].text),
            "row": row,
        })
    return rides


def confirm_if_prompted(driver):
    """If a confirmation dialog appears after clicking Accept, confirm it.
    Adjust the XPath if your confirm button uses different text."""
    try:
        btn = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((
            By.XPATH,
            "//div[contains(@class,'modal') or @role='dialog']"
            "//button[normalize-space()='Accept' or normalize-space()='Confirm'"
            " or normalize-space()='Yes' or normalize-space()='OK']",
        )))
        btn.click()
    except TimeoutException:
        pass  # no dialog


def still_available(driver, page_url: str, ride_id: str) -> bool:
    """Reload the list and check whether the ride is still offered."""
    driver.get(page_url)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
    except TimeoutException:
        return False  # table empty -> ride is gone
    return any(r["ride_id"] == ride_id for r in scrape_page(driver))


def accept_ride(driver, ride, page_url: str) -> bool:
    try:
        btn = ride["row"].find_element(By.XPATH, ".//button[normalize-space()='Accept']")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        btn.click()
        confirm_if_prompted(driver)
        time.sleep(2)
    except WebDriverException as e:
        print(f"   ! could not accept {ride['ride_id']}: {e.msg if hasattr(e, 'msg') else e}")
        return False
    # only count it as accepted once it has left the available-rides list,
    # otherwise a failed click would be punched into the admin portal
    if still_available(driver, page_url, ride["ride_id"]):
        print(f"   ! clicked Accept but {ride['ride_id']} is still listed - will retry")
        return False
    return True


def log(ride, decision):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["checked_at", "ride_id", "datetime", "vehicle", "payout",
                        "distance", "route", "comments", "decision"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), ride["ride_id"],
                    ride["datetime"], ride["vehicle"], ride["payout"], ride["distance"],
                    ride["route"], ride["comments"], decision])


def is_logged_out(driver) -> bool:
    return "availableRides" not in driver.current_url


def run_once(driver, seen: set, dry_run: bool) -> int:
    """Scan all pages once. Returns the number of rides accepted."""
    accepted = 0
    for page in range(1, MAX_PAGES + 1):
        page_url = f"{BASE_URL}?page={page}"
        driver.get(page_url)
        if is_logged_out(driver):
            raise RuntimeError("Not logged in - run with --login first.")
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        except TimeoutException:
            break  # no rides on this page

        rides = scrape_page(driver)
        if not rides:
            break

        accepted_any = False
        for ride in rides:
            if ride["ride_id"] in seen:
                continue
            seen.add(ride["ride_id"])
            p = ride["payout"]
            tag = f"{ride['ride_id']} | {ride['datetime']} | {ride['vehicle']} | ${p} | {ride['distance']}"

            if p is not None and p > MIN_PAYOUT:
                if dry_run:
                    print(f"[WOULD ACCEPT] {tag}")
                    log(ride, "would_accept")
                elif accept_ride(driver, ride, page_url):
                    print(f"[ACCEPTED]     {tag}")
                    log(ride, "accepted")
                    accepted_any = True
                    break  # table changes after accepting -> reload page
                else:
                    seen.discard(ride["ride_id"])  # retry next cycle
            else:
                print(f"[SKIPPED]      {tag}")
                log(ride, "skipped")

        if accepted_any:
            return accepted + 1 + run_once(driver, seen, dry_run)  # rescan from page 1

        # stop if there's no "Next Page" link
        if not driver.find_elements(By.XPATH, "//*[contains(normalize-space(),'Next Page')]"):
            break
    return accepted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="open visible browser to log in once")
    ap.add_argument("--live", action="store_true", help="actually click Accept (overrides DRY_RUN)")
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--punch", action="store_true",
                    help="after each scan, add accepted rides to the Dreams Star admin portal")
    ap.add_argument("--show", action="store_true", help="run with a visible browser")
    args = ap.parse_args()

    if args.login:
        d = make_driver(headless=False)
        d.get(BASE_URL)
        input("Log in in the browser window, then press Enter here...")
        d.quit()
        print("Session saved. Now run without --login.")
        return

    dry_run = DRY_RUN and not args.live
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'} | accept if payout > ${MIN_PAYOUT}"
          f"{' | punch into admin portal' if args.punch else ''}")
    driver = make_driver(headless=not args.show)
    admin = punched = None
    if args.punch:
        import dreamsstar_punch  # imported here so the bot runs without it
        admin = dreamsstar_punch.make_driver(headless=not args.show)
        punched = dreamsstar_punch.load_punched()
    seen: set[str] = set()
    try:
        while True:
            print(f"\n--- scan {datetime.now():%H:%M:%S} ---")
            accepted = run_once(driver, seen, dry_run)
            if admin and accepted:
                print("--- punching accepted rides into admin portal ---")
                try:
                    dreamsstar_punch.run_once(admin, punched, live=not dry_run)
                except Exception as e:  # keep accepting rides even if the admin side fails;
                    print(f"   ! admin portal: {e}")  # accepted rides stay in rides_log.csv
            if args.once:
                break
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        driver.quit()
        if admin:
            admin.quit()


if __name__ == "__main__":
    main()
