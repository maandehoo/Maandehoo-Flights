#!/usr/bin/env python3
"""Flight price tracker — runs on GitHub Actions (or any computer).

Reads docs/watches.json, prices every departure date in each watch's range on
Google Flights (via the open-source `fli` library), optionally cross-checks
Aviasales (Travelpayouts Data API), stores results for the phone app and sends
alerts (ntfy push / Telegram / e-mail) when a price hits the target or drops.

All credentials come from environment variables (GitHub Secrets):
  NTFY_TOPIC            - ntfy topic name (push to phone)       [optional]
  TELEGRAM_BOT_TOKEN    - Telegram bot token                    [optional]
  TELEGRAM_CHAT_ID      - your Telegram chat id                 [optional]
  SMTP_USER, SMTP_PASS  - Gmail address + Gmail *app password*  [optional]
  EMAIL_TO              - where to send e-mail (default SMTP_USER)
  TRAVELPAYOUTS_TOKEN   - Aviasales data (second source)        [optional]
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHES = ROOT / "docs" / "watches.json"
DATA = ROOT / "docs" / "data"
LATEST = DATA / "latest.json"
HISTORY = DATA / "history.json"
STATE = DATA / "state.json"
MAX_HISTORY = 400          # points kept per watch
CHUNK_DAYS = 90            # fli prices at most 93 dates per search

# IATA codes that the library labels with an outdated/wrong carrier name
AIRLINE_FIX = {"W4": "Wizz Air", "W6": "Wizz Air", "W9": "Wizz Air UK", "5W": "Wizz Air Abu Dhabi",
               "LY": "El Al", "IZ": "Arkia", "6H": "Israir", "BZ": "Bluebird Airways", "FR": "Ryanair",
               "U2": "easyJet", "A3": "Aegean", "OA": "Olympic Air"}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_HE = ["ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "שבת", "א׳"]


# ----------------------------------------------------------------- helpers
def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def fmt_date(x: str) -> str:
    dt = d(x)
    return f"{dt.day}.{dt.month} ({DAY_HE[dt.weekday()]})"


def google_link(w: dict, dep: str, ret: str | None, currency: str | None = None) -> str:
    q = f"Flights to {w['destination']} from {w['origin']} on {dep}"
    if ret:
        q += f" through {ret}"
    q += " one adult economy"
    if w.get("max_stops") == 0:
        q += " nonstop"
    return "https://www.google.com/travel/flights?hl=he&curr=" + (currency or CURRENCY) + "&q=" + urllib.parse.quote(q)


# ------------------------------------------------------------ Google Flights
def search_google(w: dict, currency: str | None = None) -> list[dict]:
    from fli.core.builders import build_date_search_segments
    from fli.core.parsers import parse_max_stops, resolve_airport
    from fli.models import DateSearchFilters, PassengerInfo, SeatType, TripType
    from fli.search import SearchDates

    origin = resolve_airport(w["origin"])
    dest = resolve_airport(w["destination"])
    stops = parse_max_stops(str(w.get("max_stops", "ANY")) if w.get("max_stops") is not None else "ANY")
    trip_days = w.get("trip_days")  # None / 0 -> one way
    round_trip = bool(trip_days)

    start = max(d(w["from"]), date.today() + timedelta(days=1))
    end = d(w["to"])
    out: list[dict] = []
    client = SearchDates()
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=CHUNK_DAYS - 1))
        segs, trip_type = build_date_search_segments(
            origin=origin, destination=dest, start_date=cur.isoformat(),
            trip_duration=trip_days or None, is_round_trip=round_trip,
        )
        filters = DateSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(adults=int(w.get("adults", 1))),
            flight_segments=segs,
            stops=stops,
            seat_type=SeatType.ECONOMY,
            from_date=cur.isoformat(),
            to_date=chunk_end.isoformat(),
            duration=trip_days if trip_type == TripType.ROUND_TRIP else None,
        )
        res = client.search(filters, currency=currency or CURRENCY) or []
        for r in res:
            dep = r.date[0].strftime("%Y-%m-%d")
            ret = r.date[1].strftime("%Y-%m-%d") if len(r.date) > 1 else None
            out.append({"dep": dep, "ret": ret, "price": round(r.price)})
        cur = chunk_end + timedelta(days=1)
        time.sleep(float(w.get("_pause", 2)))  # be polite

    wd = [WEEKDAYS.index(x) for x in w.get("weekdays") or [] if x in WEEKDAYS]
    if wd:
        out = [x for x in out if d(x["dep"]).weekday() in wd]
    out.sort(key=lambda x: x["dep"])
    return out


def flight_details(w: dict, dep: str, ret: str | None) -> dict | None:
    """Airline + times of the cheapest itinerary on the best date (1-2 extra page loads)."""
    try:
        from fli.core.builders import build_flight_segments
        from fli.core.parsers import parse_max_stops, resolve_airport
        from fli.models import FlightSearchFilters, PassengerInfo, SeatType, SortBy
        from fli.search import SearchFlights

        segs, trip_type = build_flight_segments(
            origin=resolve_airport(w["origin"]), destination=resolve_airport(w["destination"]),
            departure_date=dep, return_date=ret,
        )
        filters = FlightSearchFilters(
            trip_type=trip_type, passenger_info=PassengerInfo(adults=int(w.get("adults", 1))),
            flight_segments=segs, stops=parse_max_stops(str(w.get("max_stops", "ANY"))),
            seat_type=SeatType.ECONOMY, sort_by=SortBy.CHEAPEST,
        )
        res = SearchFlights().search(filters, currency=CURRENCY, top_n=1) or []
        if not res:
            print("   details: empty page")
            return None
        first = res[0]
        outb = first[0] if isinstance(first, tuple) else first
        back = first[1] if isinstance(first, tuple) and len(first) > 1 else None

        def leg_info(f):
            legs = f.legs
            code = getattr(legs[0].airline, "name", "").lstrip("_")
            name = legs[0].airline.value if hasattr(legs[0].airline, "value") else str(legs[0].airline)
            return {
                "airline": AIRLINE_FIX.get(code, name),
                "code": code,
                "flight": f"{code}{legs[0].flight_number}",
                "depart": legs[0].departure_datetime.strftime("%H:%M"),
                "arrive": legs[-1].arrival_datetime.strftime("%H:%M"),
                "stops": f.stops,
            }

        return {"out": leg_info(outb), "back": leg_info(back) if back else None}
    except Exception as e:  # details are nice-to-have only
        print(f"   details failed: {e}")
        return None


# ------------------------------------------------------ Aviasales (optional)
def search_aviasales(w: dict) -> dict | None:
    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if not token:
        return None
    start, end = max(d(w["from"]), date.today()), d(w["to"])
    months = sorted({(start + timedelta(days=i)).strftime("%Y-%m") for i in range((end - start).days + 1)})
    best = None
    for m in months:
        params = {
            "origin": w["origin"], "destination": w["destination"], "departure_at": m,
            "currency": CURRENCY.lower(), "sorting": "price", "limit": 1000,
            "direct": "true" if w.get("max_stops") == 0 else "false",
            "one_way": "false" if w.get("trip_days") else "true", "token": token,
        }
        url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.loads(r.read()).get("data") or []
        except Exception as e:
            print(f"   aviasales failed: {e}")
            continue
        for t in data:
            dep = t.get("departure_at", "")[:10]
            ret = (t.get("return_at") or "")[:10] or None
            if not dep or not (start <= d(dep) <= end):
                continue
            if w.get("trip_days") and ret and abs((d(ret) - d(dep)).days - w["trip_days"]) > 1:
                continue
            if best is None or t["price"] < best["price"]:
                best = {"price": round(t["price"]), "dep": dep, "ret": ret,
                        "airline": t.get("airline"), "transfers": t.get("transfers"),
                        "link": "https://www.aviasales.com" + t.get("link", "")}
    return best


# ---------------------------------------------------------------- alerting
def notify(title: str, body: str, link: str) -> list[str]:
    sent = []
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        try:
            payload = json.dumps({"topic": topic, "title": title, "message": body, "click": link,
                                  "tags": ["airplane"], "priority": 4,
                                  "actions": [{"action": "view", "label": "לטיסה", "url": link}]}).encode()
            req = urllib.request.Request(os.getenv("NTFY_SERVER", "https://ntfy.sh"), data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            sent.append("ntfy")
        except Exception as e:
            print(f"   ntfy failed: {e}")

    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            text = f"✈️ <b>{title}</b>\n{body}\n<a href=\"{link}\">פתח ב-Google Flights</a>"
            data = urllib.parse.urlencode({"chat_id": tg_chat, "text": text, "parse_mode": "HTML",
                                           "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{tg_token}/sendMessage", data=data, timeout=20).read()
            sent.append("telegram")
        except Exception as e:
            print(f"   telegram failed: {e}")

    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if user and pw:
        try:
            html = (f'<div dir="rtl" style="font-family:Arial,sans-serif;font-size:16px">'
                    f'<h2>{title}</h2><p>{body.replace(chr(10), "<br>")}</p>'
                    f'<p><a href="{link}">פתח ב-Google Flights</a></p></div>')
            msg = MIMEText(html, "html", "utf-8")
            msg["Subject"], msg["From"], msg["To"] = f"✈️ {title}", user, os.getenv("EMAIL_TO") or user
            with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
            sent.append("email")
        except Exception as e:
            print(f"   email failed: {e}")
    return sent



# --------------------------------------------------- explore: "anywhere"
DEFAULT_DESTS = [
    # Greece / Cyprus
    "ATH", "SKG", "RHO", "HER", "CHQ", "CFU", "JTR", "JMK", "LCA", "PFO",
    # Italy
    "FCO", "MXP", "BGY", "VCE", "NAP", "BLQ", "BRI", "CTA", "PMO", "PSA", "FLR", "TRN",
    # Spain / Portugal
    "BCN", "MAD", "PMI", "AGP", "VLC", "LIS", "OPO",
    # Western Europe
    "CDG", "ORY", "NCE", "MRS", "LYS", "AMS", "BRU", "LTN", "STN", "LHR", "MAN", "DUB",
    "BER", "MUC", "FRA", "HAM", "DUS", "CGN", "NUE", "VIE", "ZRH", "GVA", "BSL",
    # Central / Eastern Europe / Balkans
    "PRG", "BUD", "WAW", "KRK", "GDN", "WRO", "OTP", "CLJ", "SOF", "VAR", "BOJ", "BEG",
    "TGD", "TIV", "SPU", "DBV", "ZAG", "LJU", "SKP", "TIA", "VNO", "RIX", "TLL", "RMO",
    # Scandinavia
    "CPH", "ARN", "OSL", "HEL",
    # Caucasus / Turkey / Middle East / Africa
    "TBS", "BUS", "KUT", "EVN", "GYD", "IST", "SAW", "AYT", "DXB", "AUH", "AMM", "CAI",
    "SSH", "HRG", "RAK", "ADD", "NBO",
    # Long haul
    "BKK", "HKT", "DEL", "BOM", "GOI", "MLE", "JFK", "EWR", "MIA", "LAX", "YYZ",
]


def run_explore(cfg: dict, now: str, state: dict) -> None:
    ex = cfg.get("explore") or {}
    if not ex.get("active", False):
        return
    cur = ex.get("currency", CURRENCY)
    max_price = float(ex["max_price"])
    trip_days = int(ex.get("trip_days", 7))
    start = date.today() + timedelta(days=int(ex.get("days_from_now", 3)))
    end = start + timedelta(days=int(ex.get("window_days", 90)) - 1)
    if ex.get("to"):
        end = min(end, d(ex["to"])) if not ex.get("from") else d(ex["to"])
    if ex.get("from"):
        start = max(start, d(ex["from"]))
    dests = [x.upper() for x in (ex.get("destinations") or DEFAULT_DESTS) if x.upper() != ex.get("origin", "TLV")]
    exclude = {x.upper() for x in ex.get("exclude") or []}
    dests = [x for x in dict.fromkeys(dests) if x not in exclude]
    print(f"- explore: {ex.get('origin', 'TLV')} -> {len(dests)} destinations, {trip_days}d trips, "
          f"{start}..{end}, under {max_price:g} {cur}")

    results, deals, failed = [], [], []
    for dst in dests:
        w = {"origin": ex.get("origin", "TLV"), "destination": dst, "from": start.isoformat(),
             "to": end.isoformat(), "trip_days": trip_days, "weekdays": ex.get("weekdays") or [],
             "max_stops": ex.get("max_stops", "ANY"), "_pause": 0.5}
        try:
            dates = search_google(w, currency=cur)
        except Exception as e:  # unknown airport code, blocked page, ...
            failed.append(dst)
            print(f"   {dst}: {type(e).__name__}: {str(e)[:80]}")
            continue
        if not dates:
            continue
        best = min(dates, key=lambda x: x["price"])
        under = [x for x in dates if x["price"] <= max_price]
        row = {"dest": dst, "price": best["price"], "dep": best["dep"], "ret": best["ret"],
               "under": len(under), "link": google_link(w, best["dep"], best["ret"], cur)}
        results.append(row)
        if under:
            deals.append(row)
    results.sort(key=lambda r: r["price"])
    deals.sort(key=lambda r: r["price"])
    print(f"   {len(results)} destinations priced, {len(deals)} under {max_price:g} {cur}, {len(failed)} failed")

    save(DATA / "explore.json", {
        "checked": now, "currency": cur, "max_price": max_price, "trip_days": trip_days,
        "from": start.isoformat(), "to": end.isoformat(), "origin": ex.get("origin", "TLV"),
        "scanned": len(dests), "failed": failed, "deals": deals, "all": results,
    })

    # alert only on destinations that are new under the threshold, or got cheaper by 5%+
    st = state.setdefault("_explore", {})
    fresh = []
    for r in deals:
        prev = st.get(r["dest"])
        if prev is None or r["price"] <= prev * 0.95:
            fresh.append(r)
        st[r["dest"]] = min(r["price"], prev or 10**9)
    for dst in list(st):                     # re-arm destinations that went back above threshold
        if dst not in {r["dest"] for r in deals}:
            st.pop(dst)
    if fresh:
        sym = {"USD": "$", "ILS": "₪", "EUR": "€"}.get(cur, cur + " ")
        lines = [f"{r['dest']}: {sym}{r['price']} · {fmt_date(r['dep'])}" +
                 (f"–{fmt_date(r['ret'])}" if r["ret"] else "") for r in fresh[:12]]
        more = f"\n+ עוד {len(fresh) - 12}" if len(fresh) > 12 else ""
        title = f"{len(fresh)} יעדים מתחת ל־{sym}{max_price:g} ({trip_days} ימים)"
        sent = notify(title, "\n".join(lines) + more, fresh[0]["link"])
        print(f"   EXPLORE ALERT ({len(fresh)} deals) via {sent or 'nothing'}")

# -------------------------------------------------------------------- main
def main() -> int:
    global CURRENCY
    cfg = load(WATCHES, {})
    CURRENCY = cfg.get("currency", "ILS")
    drop_pct = float(cfg.get("drop_alert_pct", 10))
    only = sys.argv[1] if len(sys.argv) > 1 else None

    latest = load(LATEST, {"watches": {}})
    history = load(HISTORY, {})
    state = load(STATE, {})
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    errors = 0

    for w in cfg.get("watches", []):
        if not w.get("active", True) or (only and w["id"] != only):
            continue
        if d(w["to"]) < date.today():
            print(f"- {w['id']}: date range is in the past, skipped")
            continue
        print(f"- {w['id']}: {w['origin']}->{w['destination']} {w['from']}..{w['to']}")
        entry = {"checked": now, "status": "ok"}
        try:
            dates = []
            for attempt in range(3):          # Google occasionally serves an empty page
                dates = search_google(w)
                if dates:
                    break
                print(f"   empty result, retry {attempt + 1}/2")
                time.sleep(5 * (attempt + 1))
            if not dates:
                entry["status"] = "empty"
        except Exception as e:
            traceback.print_exc()
            dates, entry["status"], entry["error"] = [], "error", f"{type(e).__name__}: {e}"[:300]
            errors += 1

        avia = search_aviasales(w)
        best = min(dates, key=lambda x: x["price"]) if dates else None
        if best:
            best = dict(best, source="google", link=google_link(w, best["dep"], best["ret"]))
            for attempt in range(3):
                best["details"] = flight_details(w, best["dep"], best["ret"])
                if best["details"]:
                    break
                time.sleep(3 * (attempt + 1))
        elif avia:
            best = dict(avia, source="aviasales")
        entry.update({"dates": dates, "best": best, "aviasales": avia})
        latest["watches"][w["id"]] = entry

        if best:
            print(f"   best {best['price']} {CURRENCY} on {best['dep']} (of {len(dates)} dates)")
            hist = history.setdefault(w["id"], [])
            prev = hist[-1]["price"] if hist else None
            hist.append({"t": now, "price": best["price"], "dep": best["dep"], "ret": best.get("ret")})
            history[w["id"]] = hist[-MAX_HISTORY:]

            # ---- alert rules
            st = state.setdefault(w["id"], {})
            reasons = []
            target = w.get("target_price")
            if target and best["price"] <= target and best["price"] < st.get("last_alert_price", 10**9):
                reasons.append(f"ירד מתחת ליעד שלך ({target} {CURRENCY})")
            if prev and best["price"] <= prev * (1 - drop_pct / 100):
                reasons.append(f"ירידה של {round((1 - best['price'] / prev) * 100)}% מהבדיקה הקודמת ({prev})")
            if target and best["price"] > target * 1.05:
                st.pop("last_alert_price", None)   # re-arm once price rises again
            if reasons:
                ret_txt = f" – חזרה {fmt_date(best['ret'])}" if best.get("ret") else ""
                det = best.get("details") or {}
                air = f"\n{det['out']['airline']} {det['out']['depart']}→{det['out']['arrive']}" if det.get("out") else ""
                title = f"{w['name']}: {best['price']} {CURRENCY}"
                body = (f"{w['origin']}→{w['destination']} יציאה {fmt_date(best['dep'])}{ret_txt}{air}\n"
                        + " · ".join(reasons))
                sent = notify(title, body, best.get("link", ""))
                print(f"   ALERT sent via {sent or 'nothing (no channel configured)'}")
                st["last_alert_price"] = best["price"]
                st["last_alert"] = now
        else:
            print("   no prices found")

    if not only or only == "explore":
        try:
            run_explore(cfg, now, state)
        except Exception:
            traceback.print_exc()

    latest["updated"] = now
    latest["currency"] = CURRENCY
    save(LATEST, latest)
    save(HISTORY, history)
    save(STATE, state)
    return 1 if errors and errors == len([w for w in cfg.get("watches", []) if w.get("active", True)]) else 0


CURRENCY = "ILS"
if __name__ == "__main__":
    if "--test-alert" in sys.argv:
        print(notify("בדיקת התראה ✈️", "אם קיבלת את זה — ההתראות עובדות.", "https://www.google.com/travel/flights"))
        sys.exit(0)
    sys.exit(main())
