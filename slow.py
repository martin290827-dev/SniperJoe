"""SniperJoe Langsam: Hinweis per Telegram, wenn eine Kerze (1W / 1M) die obere oder untere
Linie des Perp-Guard-Indikators beruehrt. Zwei Bandarten: ATR und Perzentil.
Nur Kerzen, die abgeschlossen sind. Kein Handelssignal, nur ein Hinweis."""
import csv, datetime, json, math, os, time, urllib.parse, urllib.request
import watcher as W

STATE_FILE = os.environ.get("SLOW_STATE_FILE", "slow_state.json")
LIST_FILE = os.environ.get("SLOW_LIST", "slow-watchlist.csv")
TD_KEY = os.environ.get("TWELVEDATA_KEY", "")
TD_PAUSE = float(os.environ.get("TD_PAUSE", "8"))
TFS = [x.strip() for x in os.environ.get("SLOW_TIMEFRAMES", "1w,1m").split(",") if x.strip()]
EMA_LEN, ATR_LEN, EXT = 20, 14, 2.5
PCT_N = {"1w": int(os.environ.get("PCT_N_1W", "100")), "1m": int(os.environ.get("PCT_N_1M", "48"))}
PCT_LO, PCT_HI = 5.0, 95.0
LABEL = {"1w": "1W", "1m": "1M"}
BADGE = {"1w": "\U0001F7E7", "1m": "\U0001F7E5"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
LAST_ERR = [""]


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(4)
    raise last


def td_bars(sym, tf):
    iv = {"1w": "1week", "1m": "1month"}[tf]
    size = {"1w": 400, "1m": 150}[tf]
    url = ("https://api.twelvedata.com/time_series?symbol=%s&interval=%s&outputsize=%d&order=ASC&apikey=%s"
           % (urllib.parse.quote(sym), iv, size, TD_KEY))
    for i in range(3):
        j = get_json(url)
        if j.get("status") == "error" or "values" not in j:
            if j.get("code") == 429:
                time.sleep(35)
                continue
            raise RuntimeError("%s %s" % (j.get("code"), j.get("message")))
        out = []
        for v in j["values"]:
            d = datetime.datetime.strptime(v["datetime"][:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
            out.append({"d": d, "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])})
        return out
    raise RuntimeError("429 Twelve Data")


def yahoo_bars(sym, tf):
    iv, rng = {"1w": ("1wk", "10y"), "1m": ("1mo", "max")}[tf]
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s&includePrePost=false" % (urllib.parse.quote(sym), iv, rng)
    j = get_json(url, 2)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for k, t in enumerate(res.get("timestamp") or []):
        h, l, c = q["high"][k], q["low"][k], q["close"][k]
        if None in (h, l, c):
            continue
        d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
        out.append({"d": d, "h": h, "l": l, "c": c})
    return out


def normalize(bars, tf):
    for b in bars:
        d = b["d"]
        if tf == "1m":
            b["d"] = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            d = d.replace(hour=0, minute=0, second=0, microsecond=0)
            b["d"] = d - datetime.timedelta(days=d.weekday())   # Montag der Woche
    seen = {}
    for b in bars:
        seen[b["d"]] = b       # doppelte Perioden: letzte gewinnt
    return [seen[k] for k in sorted(seen)]


def closed(bar, tf, typ, now):
    d = bar["d"]
    if tf == "1w":
        # Aktien: nach Freitag-Boersenschluss (ca. 22 Uhr UTC), Krypto: erst nach Sonntag
        end = d + (datetime.timedelta(days=4, hours=22) if typ == "Aktie" else datetime.timedelta(days=7))
    else:
        end = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)   # Beginn des Folgemonats
    return now >= end


def fetch(row, tf):
    errs = []
    if TD_KEY:
        time.sleep(TD_PAUSE)
        try:
            return normalize(td_bars(row["td"], tf), tf)
        except Exception as e:
            errs.append("TD %r" % (e,))
    try:
        return normalize(yahoo_bars(row["yahoo"], tf), tf)
    except Exception as e:
        errs.append("Yahoo %r" % (e,))
    LAST_ERR[0] = "; ".join(errs)
    raise RuntimeError(LAST_ERR[0])


def percentile(vals, p):
    s = sorted(vals)
    r = p / 100.0 * (len(s) - 1)
    lo = int(math.floor(r))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (r - lo)


def bands(bars, tf):
    """Linien der LETZTEN Kerze. Gibt {'ATR': (unten, oben)|None, 'PCT': (unten, oben)|None} zurueck."""
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    e = W.ema(c, EMA_LEN)
    a = W.atr_wilder(h, l, c, ATR_LEN)
    i = len(c) - 1
    res = {"ATR": None, "PCT": None}
    if len(c) >= EMA_LEN + ATR_LEN:
        res["ATR"] = (e[i] - EXT * a[i], e[i] + EXT * a[i])
    n = PCT_N[tf]
    if len(c) >= n + EMA_LEN + 1:
        lo = [math.log(l[j] / e[j]) for j in range(i - n, i)]
        hi = [math.log(h[j] / e[j]) for j in range(i - n, i)]
        res["PCT"] = (e[i] * math.exp(percentile(lo, PCT_LO)), e[i] * math.exp(percentile(hi, PCT_HI)))
    return res


def fp(x):
    if x >= 1000:
        return "{:,.0f}".format(x).replace(",", ".")
    if x >= 10:
        return "{:,.2f}".format(x).replace(",", "X").replace(".", ",").replace("X", ".")
    return "%.3f" % x


def band_text(kind, tf):
    if kind == "ATR":
        return "ATR: EMA20 ± %.1f × ATR14" % EXT, "Bandart ATR, Faktor %.1f" % EXT
    n = PCT_N[tf]
    return ("Perzentil: %d%% / %d%% der letzten %d Kerzen" % (PCT_LO, PCT_HI, n),
            "Bandart Perzentil (Historie), Rückblick %d, Perzentil %d / %d" % (n, PCT_LO, PCT_HI))


def when_text(bar, tf):
    d = bar["d"]
    return ("Woche ab %s" % d.strftime("%d.%m.%Y")) if tf == "1w" else d.strftime("Monat %m/%Y")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"sent": {}, "started": False}


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    state = load_state()
    first = not state.get("started")
    with open(LIST_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    msgs, errors, status, ok = [], [], [], 0
    for tf in TFS:
        for row in rows:
            try:
                bars = fetch(row, tf)
            except Exception as e:
                errors.append("%s %s" % (row["name"], LABEL[tf]))
                print("Fehler %s %s: %r" % (row["name"], tf, e), flush=True)
                continue
            ok += 1
            bars = [b for b in bars if closed(b, tf, row["typ"], now)]
            if len(bars) < EMA_LEN + ATR_LEN:
                status.append("%s %s: zu wenig Historie (%d Kerzen)" % (row["name"], LABEL[tf], len(bars)))
                continue
            bar = bars[-1]
            bd = bands(bars, tf)
            if bd["PCT"] is None:
                status.append("%s %s: Perzentil erst ab %d Kerzen (jetzt %d), nur ATR" % (row["name"], LABEL[tf], PCT_N[tf] + EMA_LEN + 1, len(bars)))
            for side in ("unten", "oben"):
                hits = []
                for kind in ("ATR", "PCT"):
                    b = bd[kind]
                    if not b:
                        continue
                    if side == "unten" and bar["l"] <= b[0]:
                        hits.append((kind, b[0]))
                    if side == "oben" and bar["h"] >= b[1]:
                        hits.append((kind, b[1]))
                key = "%s|%s|%s" % (row["name"], tf, side)
                stamp = bar["d"].strftime("%Y-%m-%d")
                if not hits or state["sent"].get(key) == stamp:
                    continue
                state["sent"][key] = stamp
                head = "%s <b>%s</b> · <b>%s</b>: %s Linie berührt" % (BADGE[tf], LABEL[tf], row["name"], "UNTERE" if side == "unten" else "OBERE")
                lines = [head, "%s (%s) · Schluss %s" % (when_text(bar, tf), "Tief %s" % fp(bar["l"]) if side == "unten" else "Hoch %s" % fp(bar["h"]), fp(bar["c"]))]
                for kind, val in hits:
                    desc, setting = band_text(kind, tf)
                    lines.append("• %s → Linie bei %s\n   Im Chart: Perp Guard v1.25, Profil Langsam, %s" % (desc, fp(val), setting))
                lines.append("Hinweis zur Aufmerksamkeit, kein Kauf- oder Verkaufsbefehl.")
                msgs.append("\n".join(lines))
    if ok == 0:
        raise SystemExit("Keine Daten von der Quelle: %s" % LAST_ERR[0])
    if first:
        W.telegram("SniperJoe Langsam gestartet. Ich melde, wenn eine abgeschlossene 1W- oder 1M-Kerze die obere oder untere Linie berührt. "
                   "Werte: %s. Bandarten: ATR und Perzentil." % ", ".join(r["name"] for r in rows))
        state["started"] = True
    if status:
        print("\n".join(status))
    if msgs:
        W.telegram("\n\n".join(msgs), html=True)
    if errors:
        today = now.strftime("%Y-%m-%d")
        if state.get("fail_alert") != today:
            state["fail_alert"] = today
            W.telegram("SniperJoe Langsam: keine Daten für %s. Nächster Versuch automatisch." % ", ".join(errors))
    if not W.DRY_RUN:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
    print("Fertig: %d Meldung(en), %d Fehler." % (len(msgs), len(errors)))


if __name__ == "__main__":
    main()
