# limousine-booking-service

Two Selenium bots that work together:

1. **`sixt_ride_bot.py`** watches the Sixt Driver Company Portal
   (`https://dcp.orange.sixt.com/availableRides`). It accepts every ride whose payout is **above $27**
   and writes each decision to `rides_log.csv`.
2. **`dreamsstar_punch.py`** reads the **accepted** rides from `rides_log.csv` and creates each one
   as a booking in the Dreams Star admin portal (`http://booking.dreamsstarlimo.com/administrator_dashboard`).

```text
Sixt available rides --(payout > $27, click Accept)--> rides_log.csv --(Add Booking)--> Dreams Star admin
```

## 1. Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

You also need Google Chrome installed. Selenium 4.6+ downloads the matching chromedriver for you.

## 2. Log in once to each portal

Each bot keeps its own Chrome profile, so you only have to log in once.

```bash
python sixt_ride_bot.py --login        # log in to Sixt in the window, then press Enter
python dreamsstar_punch.py --login     # log in to the admin portal, then press Enter
```

The sessions are saved in `sixt_chrome_profile/` and `dreamsstar_chrome_profile/`.
If a session expires, the bot stops with "Not logged in". Run `--login` again when that happens.

## 3. Test in dry-run mode first

Both scripts start in dry-run mode (`DRY_RUN = True`). In dry-run mode nothing is clicked or submitted.

```bash
python sixt_ride_bot.py --once                       # prints [WOULD ACCEPT] / [SKIPPED]
python dreamsstar_punch.py --include-would-accept    # fills the admin form for those rides,
                                                     # saves dryrun_<id>.png, does NOT submit
```

Open the `dryrun_*.png` screenshots to check how the admin form was filled.

## 4. Go live

To accept rides and punch them into the admin portal with one command:

```bash
python sixt_ride_bot.py --live --once --punch     # one live scan
python sixt_ride_bot.py --live --punch            # keep running, scan every 30 s
```

You can also run the two parts separately:

```bash
python sixt_ride_bot.py --live                    # accept rides only
python dreamsstar_punch.py --live --watch         # punch accepted rides every 60 s
```

Add `--show` to either script to watch the browser instead of running headless.

## 5. Configuration

`sixt_ride_bot.py`:

```python
MIN_PAYOUT = 27.00     # accept only if payout is ABOVE this (USD); $27.00 exactly is skipped
POLL_SECONDS = 30
MAX_PAGES = 10
DRY_RUN = True
```

`dreamsstar_punch.py`: `DASHBOARD_URL` (admin portal home, used for login), `ADD_URL` (Add Booking
form; if that page has no form, the bot follows the "Add Booking" link on the dashboard), `SUPPLIER_ID` (12 = SIXT Ride - MyDriver), `BOOKING_TYPE`, `VEHICLE_MAP`,
`CITY_MAP`, `DRY_RUN`, `WATCH_SECONDS`.

## 6. How the safety checks work

- A ride is logged as `accepted` only after it has disappeared from the available-rides list.
  If the Accept click did not work, the bot retries the ride on the next scan.
- Dates such as `Tomorrow, 00:23` are worked out from the time the bot saw the ride.
- A ride is never punched twice. Punched ride ids are stored in `punched_rides.json`.
  Before it submits, the bot also searches the admin booking lists for the ride id.

## Files created at runtime (git-ignored)

| File | Purpose |
| --- | --- |
| `rides_log.csv` | every ride the bot saw and its decision |
| `punched_rides.json` | ride ids already added to the admin portal |
| `dryrun_*.png` | screenshots of the filled form in dry-run mode |
| `*_chrome_profile/` | saved login sessions |

## Commands summary

| Command | Purpose |
| --- | --- |
| `python sixt_ride_bot.py --login` | Log in to Sixt |
| `python dreamsstar_punch.py --login` | Log in to the admin portal |
| `python sixt_ride_bot.py --once` | Dry-run test of one scan |
| `python dreamsstar_punch.py --include-would-accept` | Dry-run test of the admin form |
| `python sixt_ride_bot.py --live --once --punch` | One live scan with accept and punch |
| `python sixt_ride_bot.py --live --punch` | Keep accepting and punching |
