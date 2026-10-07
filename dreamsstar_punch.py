"""
Dreams Star admin portal - punch accepted Sixt rides as bookings (Selenium)

Reads the rides that sixt_ride_bot.py ACCEPTED (from rides_log.csv) and creates
each one as a booking in the admin portal  http://booking.dreamsstarlimo.com/administrator_dashboard
(Add Booking form: ADD_URL; if that page has no form, the "Add Booking" link on the dashboard is used)

Form mapping (from the live Add Booking form):
  booking_reference  <- Sixt ride id
  trip_date          <- ride date/time, sent as "YYYY-MM-DD HH:MM"
  supplier_id        <- 12  "SIXT Ride - MyDriver"   (currency auto-fills USD)
  service_type       <- 1 Arrival/Departure if airport in route, else 2 Transfer
  vehicle_type       <- mapped from the Sixt vehicle class (see VEHICLE_MAP)
  pickup/drop city   <- guessed from address keywords (default Dubai)
  booking_type       <- 1 Credit, booking_amount = payout (credit_amount auto-fills)
  customer_collection / supplier_amount <- 0 (configurable)
  remarks            <- Sixt comments + distance

SETUP
  pip install -r requirements.txt

LOGIN (one time)
  python dreamsstar_punch.py --login      -> log in in the window, press Enter

RUN
  python dreamsstar_punch.py              -> DRY RUN: fills the form, does NOT submit
  python dreamsstar_punch.py --live       -> actually clicks "Add Booking"
  python dreamsstar_punch.py --live --watch   -> keep checking rides_log.csv every 60s
  python dreamsstar_punch.py --include-would-accept  -> dry-run test with the bot's dry-run rows

A ride is never punched twice: punched ids are kept in punched_rides.json, and
before submitting the script also checks the portal's booking lists for the ride id.
"""

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.common.exceptions import (NoAlertPresentException, TimeoutException,
                                        WebDriverException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

try:
    from dateutil import parser as dtparser
except ImportError:
    dtparser = None

# ---------------- CONFIG ----------------
ADMIN_BASE = "http://booking.dreamsstarlimo.com"
DASHBOARD_URL = f"{ADMIN_BASE}/administrator_dashboard"   # admin portal home / login landing
ADD_URL = f"{ADMIN_BASE}/admin/add_booking"                # Add Booking form (auto-found if this moves)
CHECK_URLS = [f"{ADMIN_BASE}/admin/manage_bookings", f"{ADMIN_BASE}/admin/all_bookings"]

RIDES_LOG = "rides_log.csv"          # written by sixt_ride_bot.py
PUNCHED_FILE = "punched_rides.json"
BOOKINGS_LOG = "bookings_log.csv"    # Sixt ride id -> admin Booking Code (JO-...) for every punch
PROFILE_DIR = os.path.abspath("./dreamsstar_chrome_profile")
DRY_RUN = True
WATCH_SECONDS = 60

SUPPLIER_ID = "12"                   # 12 = SIXT Ride - MyDriver | 156 = ... ( NEW PREMIUM )
BOOKING_TYPE = "1"                   # 1 Credit, 2 Cash, 3 Both
CUSTOMER_COLLECTION = "0"
SUPPLIER_AMOUNT = "0"
DEFAULT_PASSENGERS = "1"
DATE_DAYFIRST = True                 # how to read ambiguous dates like 05/10/2026

# Sixt vehicle text (lowercase keyword) -> admin vehicle_type value. First match wins.
VEHICLE_MAP = [
    ("first", "4"),                  # First Class
    ("suv", "10"),                   # SUV / Business SUV
    ("van", "2"),                    # Business Van / Premium Van
    ("green", "5"),                  # Green / Business Green
    ("xl", "7"),                     # Ride XL
    ("economy", "6"), ("ride", "6"), # Ride / Economy
    ("mpv", "3"),
    ("business", "1"),               # Business Class (must come before "bus")
    ("bus", "9"),
]
DEFAULT_VEHICLE = "1"

# address keyword -> admin city value
CITY_MAP = [
    ("abu dhabi", "2"), ("auh", "2"), ("sharjah", "3"), ("shj", "3"),
    ("ras al kh", "4"), ("rak", "4"), ("al ain", "5"), ("ajman", "6"),
    ("fujairah", "7"), ("umm al", "8"), ("dubai", "1"), ("dxb", "1"), ("dwc", "1"),
]
DEFAULT_CITY = "1"
AIRPORT_WORDS = ("airport", "dxb", "dwc", "auh", "shj", "terminal")
# ----------------------------------------


def make_driver(headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # The admin portal only works over plain http. Stop Chrome from silently switching
    # http:// to https:// (that lands on a different site: certificate error + 404).
    opts.add_argument("--disable-features=HttpsUpgrades,HttpsFirstBalancedModeAutoEnable,"
                      "HttpsFirstModeV2ForTypicallySecureUsers,HttpsFirstBalancedMode")
    opts.add_experimental_option("prefs", {"https_only_mode_enabled": False})
    return webdriver.Chrome(options=opts)


# ---------- data helpers ----------
def load_punched() -> set:
    if os.path.exists(PUNCHED_FILE):
        with open(PUNCHED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_punched(ids: set):
    with open(PUNCHED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=1)


def log_booking(b: dict, booking_code: str, status: str):
    new = not os.path.exists(BOOKINGS_LOG)
    with open(BOOKINGS_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["punched_at", "ride_id", "booking_code", "trip_date", "amount",
                        "pickup", "drop_off", "status"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), b["booking_reference"], booking_code,
                    b["trip_date"], b["booking_amount"], b["pickup_address"], b["drop_off_address"], status])


def read_accepted(decisions=("accepted",)) -> list[dict]:
    if not os.path.exists(RIDES_LOG):
        return []
    with open(RIDES_LOG, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("decision") in decisions]
    uniq = {}
    for r in rows:
        uniq[r["ride_id"].strip()] = r          # keep latest row per ride id
    return list(uniq.values())


def to_trip_date(text: str, checked_at: str = "") -> str | None:
    """Return 'YYYY-MM-DD HH:MM' or None.
    Sixt shows near rides as 'Today, 13:00' / 'Tomorrow, 00:23'; those are resolved
    against the time the bot saw the ride (checked_at), not the time we punch it."""
    text = clean(text)
    if not text:
        return None
    rel = re.match(r"(today|tomorrow|yesterday)\W*(\d{1,2}):(\d{2})\s*([ap]m)?", text, flags=re.I)
    if rel:
        try:
            base = datetime.fromisoformat(checked_at) if checked_at else datetime.now()
        except ValueError:
            base = datetime.now()
        hour, minute = int(rel.group(2)), int(rel.group(3))
        ampm = (rel.group(4) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        day = base + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[rel.group(1).lower()])
        return day.replace(hour=hour, minute=minute).strftime("%Y-%m-%d %H:%M")
    if dtparser:
        try:
            return dtparser.parse(text, dayfirst=DATE_DAYFIRST, fuzzy=True).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError):
            pass
    for fmt in ("%d/%m/%Y %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d-%b-%Y %H:%M",
                "%b %d, %Y %I:%M %p", "%d/%m/%Y %I:%M %p", "%a, %d %b %Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return None


def clean(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()


def has_word(word: str, text: str) -> bool:
    """Whole-word match, so 'rak' does not hit 'Barakat' and 'auh' needs to stand alone."""
    return re.search(rf"\b{re.escape(word)}", text, flags=re.I) is not None


def map_vehicle(text: str) -> str:
    t = text.lower()
    return next((v for k, v in VEHICLE_MAP if k in t), DEFAULT_VEHICLE)


def map_city(text: str) -> str:
    return next((v for k, v in CITY_MAP if has_word(k, text)), DEFAULT_CITY)


# Sixt route text = "<pickup ... - United Arab Emirates> <drop-off ... - United Arab Emirates> 22.0 Km ...".
# Each address ends with the country; the drop-off starts after the first country name that is
# NOT followed by a comma (a comma means the pickup repeats itself: "Hotel,Addr - UAE, Hotel, Addr - UAE").
ADDRESS_END = re.compile(r"(United Arab Emirates|UAE)\s+(?![,\s])", flags=re.I)


def split_route(ride: dict) -> tuple[str, str]:
    """Prefer separate pickup/dropoff columns; otherwise split the route text."""
    if ride.get("pickup") or ride.get("dropoff"):
        return clean(ride.get("pickup")), clean(ride.get("dropoff"))
    route = re.sub(r"\s*\b[\d.,]+\s*Km\b.*", "", clean(ride.get("route")), flags=re.I).strip()
    m = ADDRESS_END.search(route)
    if m:
        return route[:m.end(1)].strip(" ,"), route[m.end():].strip(" ,")
    parts = re.split(r"\s*(?:→|->|➔)\s*", route, maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (route, "")


def build_booking(ride: dict) -> dict:
    pickup, drop = split_route(ride)
    route_text = f"{pickup} {drop}".lower()
    payout = float(ride["payout"]) if ride.get("payout") not in (None, "", "None") else 0.0
    remarks = " | ".join(x for x in [
        f"Sixt ride {ride['ride_id']}",
        clean(ride.get("vehicle")),
        clean(ride.get("distance")),
        clean(ride.get("comments")),
    ] if x)
    return {
        "booking_reference": ride["ride_id"].strip(),
        "trip_date": to_trip_date(ride.get("datetime", ""), ride.get("checked_at", "")),
        "supplier_id": SUPPLIER_ID,
        "service_type": "1" if any(has_word(w, route_text) for w in AIRPORT_WORDS) else "2",
        "vehicle_type": map_vehicle(ride.get("vehicle", "")),
        "pickup_city": map_city(pickup),
        "pickup_address": pickup,
        "drop_off_city": map_city(drop),
        "drop_off_address": drop,
        "booking_type": BOOKING_TYPE,
        "booking_amount": f"{payout:.2f}",
        "customer_collection": CUSTOMER_COLLECTION,
        "supplier_amount": SUPPLIER_AMOUNT,
        "passengers": DEFAULT_PASSENGERS,
        "remarks": remarks,
    }


# ---------- portal actions ----------
def has_booking_form(driver) -> bool:
    return bool(driver.find_elements(By.CSS_SELECTOR, "#frm [name='booking_reference']"))


def find_add_url(driver) -> str:
    """Open the Add Booking form. Tries ADD_URL, then the Add Booking link on the dashboard."""
    global ADD_URL
    driver.get(ADD_URL)
    if has_booking_form(driver):
        return ADD_URL
    driver.get(DASHBOARD_URL)
    links = driver.find_elements(By.XPATH,
        "//a[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add booking')"
        " or contains(@href,'add_booking') or contains(@href,'add-booking') or contains(@href,'addbooking')]")
    for href in [l.get_attribute("href") for l in links if l.get_attribute("href")]:
        driver.get(href)
        if has_booking_form(driver):
            print(f"   Add Booking form found at {href}")
            ADD_URL = href
            return href
    if "login" in driver.current_url.lower() or driver.find_elements(By.CSS_SELECTOR, "input[type=password]"):
        raise RuntimeError("Not logged in to the admin portal - run with --login first.")
    raise RuntimeError(f"Could not find the Add Booking form (tried {ADD_URL} and the dashboard links). "
                       "Set ADD_URL in dreamsstar_punch.py to the Add Booking page address.")


def already_in_portal(driver, ref: str) -> bool:
    """Search the booking lists for the reference (runs inside the logged-in page)."""
    js = """
    const [urls, ref, done] = [arguments[0], arguments[1], arguments[2]];
    Promise.all(urls.map(u => fetch(u).then(r => r.text()).catch(() => '')))
      .then(pages => done(pages.some(p => p.includes(ref))));
    """
    try:
        return bool(driver.execute_async_script(js, CHECK_URLS, ref))
    except WebDriverException:
        return False


def read_booking_code(driver) -> str:
    """The portal generates the Booking Code (e.g. JO-1791395706) when the form opens."""
    return driver.execute_script("""
        const f = document.querySelector('#frm') || document;
        for (const n of ['booking_code', 'bookingcode', 'booking_no', 'code']) {
            const e = f.querySelector('[name="'+n+'"], #'+n);
            if (e && e.value) return e.value.trim();
        }
        for (const l of f.querySelectorAll('label')) {      // fall back to the "Booking Code" label
            if (!/booking\\s*code/i.test(l.textContent)) continue;
            const e = (l.htmlFor && document.getElementById(l.htmlFor))
                   || l.parentElement.querySelector('input, textarea');
            if (e && e.value) return e.value.trim();
        }
        return '';
    """) or ""


def fill_form(driver, b: dict) -> str:
    """Fill the Add Booking form and return the Booking Code the portal generated."""
    find_add_url(driver)

    # selects + readonly/date/select2 fields are set through jQuery so the page's
    # own change handlers run (supplier -> currency, booking type -> credit amount)
    driver.execute_script("""
        const b = arguments[0], $ = window.jQuery, f = $('#frm');
        const set = (name, val) => f.find('[name="'+name+'"]').val(val).trigger('change').trigger('keyup');
        set('booking_reference', b.booking_reference);
        $('#delivery_date').val(b.trip_date).trigger('change');
        $('#supplier_id').val(b.supplier_id).trigger('change');
        set('service_type', b.service_type);
        set('vehicle_type', b.vehicle_type);
        set('pickup_city', b.pickup_city);
        set('pickup_address', b.pickup_address);
        set('drop_off_city', b.drop_off_city);
        set('drop_off_address', b.drop_off_address);
        $('#booking_type').val(b.booking_type).trigger('change');
        $('#booking_amount').val(b.booking_amount).trigger('keyup').trigger('blur');
        set('customer_collection', b.customer_collection);
        set('supplier_amount', b.supplier_amount);
        set('passengers', b.passengers);
        set('remarks', b.remarks);
    """, b)

    # supplier change fills currency via ajax - wait for it
    try:
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return jQuery('#currency_val').val() || jQuery('#currency').val()"))
    except TimeoutException:
        print("   ! currency did not auto-fill after choosing the supplier")
    return read_booking_code(driver)


def submit_form(driver) -> tuple[bool, str]:
    before = driver.current_url
    driver.find_element(By.ID, "submit").click()
    time.sleep(2)
    try:                                        # the page uses alert() for validation errors
        alert = driver.switch_to.alert
        msg = alert.text
        alert.accept()
        return False, f"alert: {msg}"
    except NoAlertPresentException:
        pass
    invalid = driver.execute_script(
        "return [...document.querySelectorAll('#frm :invalid, .parsley-error')].map(e=>e.name||e.id)")
    if invalid:
        return False, f"invalid fields: {invalid}"
    return True, driver.current_url if driver.current_url != before else "submitted"


def punch(driver, ride: dict, live: bool) -> str:
    b = build_booking(ride)
    tag = f"{b['booking_reference']} | {b['trip_date']} | ${b['booking_amount']}"
    if not b["trip_date"]:
        return f"[NEEDS FIX]  {tag} - could not read date '{ride.get('datetime')}'"
    if not b["pickup_address"] or not b["drop_off_address"]:
        return f"[NEEDS FIX]  {tag} - missing pickup/drop-off address"

    code = fill_form(driver, b)
    if already_in_portal(driver, b["booking_reference"]):
        return f"[EXISTS]     {tag}"   # the code on the (unused) form would be a new one - don't show it
    tag += f" | {code or 'no booking code'}"
    if not live:
        driver.save_screenshot(f"dryrun_{re.sub(r'[^A-Za-z0-9_-]', '_', b['booking_reference'])}.png")
        return f"[WOULD ADD]  {tag} | {b['pickup_address'][:40]} -> {b['drop_off_address'][:40]}"

    ok, info = submit_form(driver)
    if not ok:
        return f"[FAILED]     {tag} - {info}"
    if already_in_portal(driver, b["booking_reference"]):
        log_booking(b, code, "added")
        return f"[ADDED]      {tag}"
    # submitted without errors but the list page didn't show it (pagination / ajax table).
    # Still treat it as punched so the next run doesn't create a duplicate booking.
    log_booking(b, code, "added_unverified")
    return f"[ADDED?]     {tag} - submitted, but not found in booking list; please verify"


def run_once(driver, punched: set, live: bool, decisions=("accepted",)):
    todo = [r for r in read_accepted(decisions) if r["ride_id"].strip() not in punched]
    if not todo:
        print("No new accepted rides.")
        return
    for ride in todo:
        try:
            result = punch(driver, ride, live)
        except Exception as e:                      # keep going with the next ride
            result = f"[ERROR]      {ride['ride_id']} - {e}"
        print(result)
        if live and result.startswith(("[ADDED]", "[ADDED?]", "[EXISTS]")):
            punched.add(ride["ride_id"].strip())
            save_punched(punched)
        if "Not logged in" in result:
            raise RuntimeError(result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="open visible browser to log in once")
    ap.add_argument("--live", action="store_true", help="actually submit (overrides DRY_RUN)")
    ap.add_argument("--watch", action="store_true", help="keep checking rides_log.csv")
    ap.add_argument("--show", action="store_true", help="run with a visible browser")
    ap.add_argument("--include-would-accept", action="store_true",
                    help="also use the bot's dry-run 'would_accept' rows (for testing; dry run only)")
    args = ap.parse_args()

    if args.login:
        d = make_driver(headless=False)
        d.get(DASHBOARD_URL)
        input("Log in to the admin portal in the browser window, then press Enter here...")
        d.quit()
        print("Session saved. Now run without --login.")
        return

    live = args.live or not DRY_RUN
    if live and args.include_would_accept:
        ap.error("--include-would-accept is for dry runs only (those rides were never accepted)")
    decisions = ("accepted", "would_accept") if args.include_would_accept else ("accepted",)
    print(f"Mode: {'LIVE' if live else 'DRY RUN (form filled, not submitted)'}")
    driver = make_driver(headless=not args.show)
    punched = load_punched()
    try:
        while True:
            print(f"\n--- check {datetime.now():%H:%M:%S} ---")
            run_once(driver, punched, live, decisions)
            if not args.watch:
                break
            time.sleep(WATCH_SECONDS)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
