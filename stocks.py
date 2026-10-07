#!/usr/bin/env python3
"""SniperJoe Aktien: gleiche Regel wie der Hyperliquid-Waechter, Daten von Yahoo Finance.
Zeitrahmen: 4H (US-Handelszeit in zwei Bloecken 09:30-13:30 und 13:30-16:00 New York) und 1D.
Signale gehen an denselben Telegram-Bot."""
import csv, datetime, json, os, time, urllib.request, urllib.error, urllib.parse
from zoneinfo import ZoneInfo

import watcher as W

NY = ZoneInfo("America/New_York")
CSV_FILE = os.environ.get("WATCHLIST", "stocks/watchlist-main-fund.csv")
STATE_FILE = os.environ.get("STOCK_STATE_FILE", "stocks_state.json")
SLEEP = float(os.environ.get("STOCK_SLEEP", "0.4"))
RETRY_HOURS = float(os.environ.get("RETRY_HOURS", "3"))   # solange wird nach einer Grenze auf Yahoo-Daten gewartet
TFS = [x.strip().lower() for x in os.environ.get("STOCK_TIMEFRAMES", "4h,1d").split(",") if x.strip().lower() in ("4h", "1d")]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def load_watchlist():
    rows = []
    with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = (r.get("Stock") or "").strip().upper()
            if t:
                rows.append({"t": t, "range": r.get("Range", "-"), "mbx": r.get("Monthly BX", "-"),
                             "wbx": r.get("Weekly BX", "-"), "pos": (r.get("In Fund", "-") or "-")})
    return rows


def yahoo(sym, interval, rng):
    sym = urllib.parse.quote(sym.replace(".", "-"))
    last = None
    for host in ("query1", "query2"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s&includePrePost=false" % (host, sym, interval, rng)
        for i in range(2):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                    j = json.loads(r.read().decode())
                res = j["chart"]["result"][0]
                ts = res.get("timestamp") or []
                q = res["indicators"]["quote"][0]
                out = []
                for k, t in enumerate(ts):
                    o, h, l, c, v = (q[x][k] for x in ("open", "high", "low", "close", "volume"))
                    if None in (o, h, l, c):
                        continue
                    out.append({"t": t * 1000, "o": o, "h": h, "l": l, "c": c, "v": float(v or 0)})
                return out
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:
                    time.sleep(5)
                elif e.code == 404:
                    raise
            except Exception as e:
                last = e
                time.sleep(2)
    raise last


def ny_dt(t_ms):
    return datetime.datetime.fromtimestamp(t_ms / 1000, NY)


def ny_ms(d, hh, mm):
    return int(datetime.datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp() * 1000)


def build_4h(h1):
    """1h-Kerzen zu zwei Sitzungsbloecken pro Tag: A = 09:30-13:30, B = 13:30-16:00 (New York)."""
    groups = {}
    for k in h1:
        d = ny_dt(k["t"])
        blk = "A" if (d.hour, d.minute) < (13, 30) else "B"
        groups.setdefault((d.date(), blk), []).append(k)
    out = []
    for (d, blk), ks in sorted(groups.items()):
        ks.sort(key=lambda x: x["t"])
        start = ny_ms(d, 9, 30) if blk == "A" else ny_ms(d, 13, 30)
        end = ny_ms(d, 13, 30) if blk == "A" else ny_ms(d, 16, 0)
        out.append({"t": start, "T": end, "o": ks[0]["o"], "h": max(x["h"] for x in ks), "l": min(x["l"] for x in ks),
                    "c": ks[-1]["c"], "v": sum(x["v"] for x in ks)})
    return out


def build_1d(d1):
    out = []
    for k in d1:
        d = ny_dt(k["t"]).date()
        out.append(dict(k, t=ny_ms(d, 9, 30), T=ny_ms(d, 16, 0)))
    return out


def latest_boundary(tf, now_ms):
    """Letzte vergangene Blockgrenze (Beginn der dazugehoerigen Kerze, Ende) an einem Werktag."""
    d = ny_dt(now_ms).date()
    for back in range(0, 6):
        day = d - datetime.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        cands = []
        if tf == "4h":
            cands = [(ny_ms(day, 13, 30), ny_ms(day, 9, 30)), (ny_ms(day, 16, 0), ny_ms(day, 13, 30))]
        else:
            cands = [(ny_ms(day, 16, 0), ny_ms(day, 9, 30))]
        for end, start in sorted(cands, reverse=True):
            if end <= now_ms:
                return end, start
    return None, None


def day_context(d1c, now_ms):
    done = [k for k in d1c if k["T"] <= now_ms]
    if len(done) < W.ATR_LEN + 2:
        return None
    a = W.atr_wilder([k["h"] for k in done], [k["l"] for k in done], [k["c"] for k in done], W.ATR_LEN)
    rng = d1c[-W.RANGE_DAYS:]
    return {"atr": a[-1], "hi": max(k["h"] for k in rng), "lo": min(k["l"] for k in rng)}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"tf": {}, "started": False}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)


def main():
    now_ms = int(time.time() * 1000)
    force = bool(os.environ.get("FORCE"))
    state = load_state()
    first_run = not state.get("started")
    due = []
    for tf in TFS:
        end, start = latest_boundary(tf, now_ms)
        if end is None:
            continue
        blk = state["tf"].get(tf)
        if blk and blk.get("done") == end and not force:
            continue
        due.append((tf, end, start))
    if not due:
        print("Aktien: nichts faellig.")
        return
    wl = load_watchlist()
    print("Aktien: %d Ticker, faellig: %s" % (len(wl), ",".join(t for t, _, _ in due)))

    # Daten holen (pro Ticker einmal 1h und 1d)
    data, errors = {}, 0
    for r in wl:
        try:
            h1 = yahoo(r["t"], "60m", "60d") if "4h" in [t for t, _, _ in due] else []
            time.sleep(SLEEP)
            d1 = yahoo(r["t"], "1d", "1y")
            data[r["t"]] = {"4h": build_4h(h1), "1d": build_1d(d1)}
        except Exception as e:
            errors += 1
            print("Fehler %s: %s" % (r["t"], e))
        time.sleep(SLEEP)
    if errors > len(wl) * 0.5:
        print("Mehr als die Haelfte der Abfragen fehlgeschlagen.")
        if first_run or os.environ.get("ALERT_FAIL"):
            W.telegram("SniperJoe Aktien: Datenquelle (Yahoo Finance) liefert keine Daten, %d von %d Abfragen fehlgeschlagen." % (errors, len(wl)))
        save_state(state)
        raise SystemExit(1)

    msgs, activated = [], []
    for tf, end, start in due:
        blk = state["tf"].get(tf)
        is_new = blk is None
        if is_new:
            blk = state["tf"][tf] = {"last_t": {}, "last_sent": {}, "done": None}
        ms = W.TF_MS[tf]
        badge = "%s <b>%s</b>" % (W.TF_BADGE[tf], W.TF_LABEL[tf])
        stale = 0
        filt = {"sperre": 0, "kanal": 0, "rueckgang_volumen": 0, "keine_tagesdaten": 0, "r": 0}
        for r in wl:
            d = data.get(r["t"])
            if not d:
                continue
            cs = [k for k in d[tf] if k["T"] <= now_ms]
            if len(cs) < max(W.EMA_LEN, W.ATR_LEN) * 2 + 2:
                continue
            if cs[-1]["t"] < start:
                stale += 1
                continue
            newest = cs[-1]["t"]
            last_done = blk["last_t"].get(r["t"])
            if last_done is None:
                blk["last_t"][r["t"]] = newest
                continue
            raw = W.evaluate(cs, last_done)
            back = 2 if tf == "4h" else 1
            idx_newest = len(cs) - 1
            for x in raw:
                i = next(j for j, k in enumerate(cs) if k["t"] == x["t"])
                if idx_newest - i > back:
                    continue
                side = x["side"]
                ls = blk["last_sent"].get(r["t"] + "|" + side)
                if ls is not None:
                    bars_since = sum(1 for k in cs if ls < k["t"] <= x["t"])
                    if bars_since < W.COOLDOWN_BARS:
                        filt["sperre"] += 1
                        continue
                if side == "KAUF":
                    if x["band_pct"] < W.MIN_BAND_PCT:
                        filt["kanal"] += 1
                        continue
                    if x["drop_atr"] < W.MIN_DROP_ATR or x["vol_ratio"] < W.MIN_VOL_RATIO:
                        filt["rueckgang_volumen"] += 1
                        continue
                di = day_context(d["1d"], now_ms)
                have_day = bool(di) and di["atr"] > 0
                if side == "KAUF" and not have_day:
                    filt["keine_tagesdaten"] += 1
                    continue
                rr = stop = dist = None
                if have_day:
                    dist = di["atr"] * W.STOP_DAY_MULT
                    rr = ((di["hi"] - x["close"]) if side == "KAUF" else (x["close"] - di["lo"])) / dist
                    stop = x["close"] - dist if side == "KAUF" else x["close"] + dist
                if side == "KAUF" and rr < W.MIN_R:
                    filt["r"] += 1
                    continue
                blk["last_sent"][r["t"] + "|" + side] = x["t"]
                arrow = "\U0001F7E2 <b>KAUF</b>" if side == "KAUF" else "\U0001F534 <b>VERKAUF</b>"
                when = ny_dt(x["t"]).strftime("%d.%m. %H:%M") + " New York"
                lines = [
                    "%s  %s   %s   \U0001F4C8 Aktie" % (arrow, r["t"], badge),
                    "Kerze %s (Beginn)" % when,
                    "Linie %s | Schluss %s" % (W.fmt(x["line"]), W.fmt(x["close"])),
                ]
                if have_day:
                    lines.append("Chance bis %d-Tage-%s: <b>%.1f R</b>" % (W.RANGE_DAYS, "Hoch" if side == "KAUF" else "Tief", rr))
                    lines.append("Stop-Vorschlag (%.1f Tages-ATR): %s (%.1f %%)" % (W.STOP_DAY_MULT, W.fmt(stop), dist / x["close"] * 100))
                if side == "KAUF":
                    lines.append("Rueckgang %.1f ATR in 4 Kerzen | Volumen %.1fx Schnitt | Kanalbreite %.1f %%" % (x["drop_atr"], x["vol_ratio"], x["band_pct"]))
                lines.append("Liste: %s | Monat %s | Woche %s | %s" % (r["range"], r["mbx"], r["wbx"], r["pos"]))
                msgs.append("\n".join(lines))
            blk["last_t"][r["t"]] = newest
        print("[%s] stale=%d gefiltert=%s" % (tf, stale, filt))
        waited_h = (now_ms - end) / 3600000.0
        if stale > len(wl) * 0.5 and waited_h < RETRY_HOURS:
            print("[%s] Neue Kerzen noch nicht bei Yahoo, spaeter erneut." % tf)
            continue
        blk["done"] = end
        if is_new:
            activated.append(tf)
    state["started"] = True
    if first_run:
        W.telegram("SniperJoe Aktien gestartet. Ueberwache %d Aktien aus der Liste (Zeitrahmen %s, Regel wie bei Hyperliquid). Ab jetzt kommen Signale kurz nach 13:30 und 16:00 New York." % (len(wl), ", ".join(W.TF_LABEL[t] for t in TFS)))
    if msgs:
        W.telegram("SniperJoe Aktien: %d Signal(e)\n\n" % len(msgs) + "\n\n".join(msgs) + "\n\nKein Handelssignal. Plan und Risiko pruefen.", html=True)
    save_state(state)


if __name__ == "__main__":
    main()
