#!/usr/bin/env python3
"""SniperJoe: Waechter fuer Hyperliquid-Perps.

Regel (gleich wie Perp Guard v1.9, je Zeitrahmen):
  Linien: EMA(20) +/- 2.5 * ATR(14)   (ATR nach Wilder, wie TradingView ta.atr)
  Modus "Gegenbewegung": Tief <= untere Linie -> KAUF, Hoch >= obere Linie -> VERKAUF
  Modus "Ausbruch": umgekehrt
  Filter (alle per Umgebungsvariable einstellbar):
    - nur KAUF: R >= MIN_R (Chance bis 10-Tage-Hoch, Stop = 1 Tages-ATR). VERKAUF ohne R-Filter.
    - KAUF: Kanalbreite (obere/untere Linie, vor der Signalkerze) >= MIN_BAND_PCT %
    - KAUF: starker Rueckgang (>= MIN_DROP_ATR ATR in 4 Kerzen) mit starkem Volumen (>= MIN_VOL_RATIO x Schnitt der letzten 20 Kerzen)
  Zeitrahmen: TIMEFRAMES (Standard 4h,1d). Kein Abwechseln. Pro Markt, Zeitrahmen und Richtung gilt eine Sperre von COOLDOWN_BARS Kerzen.
Nur geschlossene Kerzen. Kein Handelssignal, kein Beleg fuer einen Vorteil.
Nachricht per Telegram. Zustand in state.json (wird vom Workflow ins Repo committet).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.hyperliquid.xyz/info"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

EMA_LEN = int(os.environ.get("EMA_LEN", "20"))
ATR_LEN = int(os.environ.get("ATR_LEN", "14"))
EXT = float(os.environ.get("EXT", "2.5"))
MODE = os.environ.get("MODE", "Gegenbewegung")  # oder "Ausbruch"
TF_MS = {"1h": 3600_000, "4h": 4 * 3600_000, "1d": 86400_000, "1w": 7 * 86400_000}
TF_LABEL = {"1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}
# Farbmarke je Zeitrahmen (Telegram kennt keine Textfarben, deshalb farbige Quadrate; bewusst nicht gruen/rot)
TF_BADGE = {"1h": "\u2B1C", "4h": "\U0001F7E6", "1d": "\U0001F7EA", "1w": "\U0001F7E7"}
TF_BACKFILL = {"1h": 6, "4h": 2, "1d": 1, "1w": 1}   # wie viele verpasste Kerzen nachgeholt werden
TIMEFRAMES = [x.strip().lower() for x in os.environ.get("TIMEFRAMES", "4h,1d").split(",") if x.strip().lower() in TF_MS]
CANDLES = int(os.environ.get("CANDLES", "120"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USD", "2000000"))  # 24h-Volumen in USD
INCLUDE_HIP3 = os.environ.get("INCLUDE_HIP3", "1") == "1"
SLEEP = float(os.environ.get("SLEEP", "1.4"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
BACKFILL_MAX = int(os.environ.get("BACKFILL_MAX", "6"))  # max. verpasste Kerzen, die nachgeholt werden
MIN_R = float(os.environ.get("MIN_R", "2.0"))
MIN_BAND_PCT = float(os.environ.get("MIN_BAND_PCT", "5.0"))
MIN_DROP_ATR = float(os.environ.get("MIN_DROP_ATR", "2.0"))
MIN_VOL_RATIO = float(os.environ.get("MIN_VOL_RATIO", "2.0"))
COOLDOWN_BARS = float(os.environ.get("COOLDOWN_BARS", "6"))  # Sperre pro Markt und Richtung, in Kerzen des jeweiligen Zeitrahmens
STOP_DAY_MULT = float(os.environ.get("STOP_DAY_MULT", "1.0"))
RANGE_DAYS = int(os.environ.get("RANGE_DAYS", "10"))
CONFIRM = os.environ.get("CONFIRM", "1") == "1"            # KAUF erst, wenn die naechste Kerze kein neues Tief macht
WICK_ON = os.environ.get("WICK_ON", "1") == "1"            # KAUF nur, wenn der Schluss deutlich ueber dem Tief liegt
WICK_MIN = float(os.environ.get("WICK_MIN", "0.5"))        # Lage des Schlusses in der Kerzenspanne (0 = Tief, 1 = Hoch)
TREND_FILTER = os.environ.get("TREND_FILTER", "0") == "1"  # kein KAUF im Abwaertstrend (sonst nur Info)
MKT_FILTER = os.environ.get("MKT_FILTER", "0") == "1"      # kein KAUF, wenn der Markt mitfaellt (sonst nur Info)
MKT_DROP_ATR = float(os.environ.get("MKT_DROP_ATR", "2.0"))

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def post(url, payload, retries=4):
    data = json.dumps(payload).encode()
    for i in range(retries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < retries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise
        except urllib.error.URLError:
            if i < retries - 1:
                time.sleep(3)
                continue
            raise


def info(payload):
    return post(API, payload)


def ema(values, n):
    k = 2.0 / (n + 1)
    out = []
    e = None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def atr_wilder(h, l, c, n):
    tr = []
    for i in range(len(c)):
        if i == 0:
            tr.append(h[i] - l[i])
        else:
            tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out = []
    a = None
    for i, t in enumerate(tr):
        if i < n:
            a = t if a is None else (a * i + t) / (i + 1)  # Anlauf: einfacher Mittelwert
        else:
            a = (a * (n - 1) + t) / n
        out.append(a)
    return out


def get_candles(coin, interval, n_candles, now_ms):
    step = TF_MS[interval]
    start = now_ms - (n_candles + 2) * step
    res = info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": int(start), "endTime": int(now_ms)}})
    out = []
    for k in res:
        out.append({"t": int(k["t"]), "T": int(k["T"]), "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k.get("v") or 0.0)})
    out.sort(key=lambda x: x["t"])
    return out


def list_markets():
    """Liste (coin, 24h-Volumen USD) fuer Hauptdex und, falls moeglich, HIP-3-Dexes (TradFi u. a.)."""
    markets = []
    try:
        meta, ctxs = info({"type": "metaAndAssetCtxs"})
        for u, c in zip(meta["universe"], ctxs):
            if u.get("isDelisted"):
                continue
            markets.append((u["name"], float(c.get("dayNtlVlm") or 0.0)))
    except Exception as e:
        print("Hauptdex-Liste fehlgeschlagen:", e)
        raise
    if INCLUDE_HIP3:
        try:
            dexs = info({"type": "perpDexs"})
            for d in dexs:
                if not d:
                    continue
                name = d.get("name")
                if not name:
                    continue
                try:
                    meta, ctxs = info({"type": "metaAndAssetCtxs", "dex": name})
                    for u, c in zip(meta["universe"], ctxs):
                        if u.get("isDelisted"):
                            continue
                        markets.append((u["name"], float(c.get("dayNtlVlm") or 0.0)))
                except Exception as e:
                    print("HIP-3-Dex %s uebersprungen: %s" % (name, e))
        except Exception as e:
            print("HIP-3-Liste uebersprungen:", e)
    seen = set()
    uniq = []
    for coin, vol in markets:
        if coin in seen:
            continue
        seen.add(coin)
        uniq.append((coin, vol))
    return uniq


def evaluate(candles, last_t_done):
    """Liste von Signal-Dicts. KAUF wird (bei CONFIRM) erst mit der naechsten Kerze gemeldet, wenn diese kein neues Tief macht.
    "t" ist der Zeitpunkt der Meldung (Bestaetigungskerze), "t_sig" die Signalkerze, "close" der Kaufkurs, "close_sig" der Schluss der Signalkerze."""
    h = [k["h"] for k in candles]
    l = [k["l"] for k in candles]
    c = [k["c"] for k in candles]
    v = [k.get("v", 0.0) for k in candles]
    e = ema(c, EMA_LEN)
    a = atr_wilder(h, l, c, ATR_LEN)
    n = len(candles)
    sig = []
    warm = max(EMA_LEN, ATR_LEN) * 2
    for i, k in enumerate(candles):
        if i < warm:
            continue
        up = e[i] + EXT * a[i]
        dn = e[i] - EXT * a[i]
        touch_up = k["h"] >= up
        touch_dn = k["l"] <= dn
        if touch_up and touch_dn:
            continue  # beide Linien in einer Kerze: uneindeutig, ignorieren
        if MODE == "Gegenbewegung":
            side = "KAUF" if touch_dn else ("VERKAUF" if touch_up else None)
        else:
            side = "KAUF" if touch_up else ("VERKAUF" if touch_dn else None)
        if not side:
            continue
        te, entry = k["t"], k["c"]
        if side == "KAUF" and CONFIRM:
            if i + 1 >= n:
                continue                       # noch nicht bestaetigt
            nxt = candles[i + 1]
            if nxt["l"] < k["l"]:
                continue                       # neues Tief: Signal verworfen
            te, entry = nxt["t"], nxt["c"]
        if te <= last_t_done:
            continue
        # Kanalbreite VOR der Signalkerze (sonst weitet die Signalkerze selbst den Kanal und der Filter waere immer erfuellt)
        dn_prev = e[i - 1] - EXT * a[i - 1]
        up_prev = e[i - 1] + EXT * a[i - 1]
        band_prev = (up_prev - dn_prev) / dn_prev * 100.0 if dn_prev > 0 else 0.0
        prev = v[max(0, i - 20):i]
        avg_v = sum(prev) / len(prev) if prev else 0.0
        vol_ratio = v[i] / avg_v if avg_v > 0 else 0.0
        hi4 = max(h[max(0, i - 3):i + 1])
        lo4 = min(l[max(0, i - 3):i + 1])
        drop_atr = (hi4 - k["l"]) / a[i] if a[i] else 0.0   # Rueckgang bis zum Tief der Signalkerze
        rise_atr = (k["h"] - lo4) / a[i] if a[i] else 0.0
        rng = k["h"] - k["l"]
        sig.append({
            "t": te, "t_sig": k["t"], "side": side, "line": dn if touch_dn else up, "close": entry, "close_sig": k["c"], "atr": a[i],
            "band_pct": band_prev, "wick": (k["c"] - k["l"]) / rng if rng > 0 else 0.0,
            "vol_ratio": vol_ratio, "drop_atr": drop_atr, "rise_atr": rise_atr,
            "touch_low": touch_dn,
        })
    return sig


def mkt_fall(mcs, t):
    """Faellt der Markt (Indexkerzen mcs) an der Kerze t stark? Rueckgang vom 4-Kerzen-Hoch bis zum Tief in ATR."""
    if not mcs:
        return False
    idx = next((j for j, k in enumerate(mcs) if k["t"] == t), None)
    if idx is None or idx < ATR_LEN + 1:
        return False
    a = atr_wilder([k["h"] for k in mcs], [k["l"] for k in mcs], [k["c"] for k in mcs], ATR_LEN)
    hi4 = max(k["h"] for k in mcs[max(0, idx - 3):idx + 1])
    return a[idx] > 0 and (hi4 - mcs[idx]["l"]) / a[idx] >= MKT_DROP_ATR


def trend_state(di, price):
    """(Zustand, Text) aus Tages-EMA50/EMA200 (letzte geschlossene Tageskerze) und aktuellem Preis."""
    if not di or di.get("e200") is None or di.get("e50") is None:
        return 0, "Trend 1D unbekannt (zu wenig Daten)"
    if price > di["e200"] and di["e50"] > di["e200"]:
        return 1, "Ruecksetzer im Aufwaertstrend (1D)"
    if price < di["e200"] and di["e50"] < di["e200"]:
        return -1, "gegen den Trend (Abwaertstrend 1D)"
    return 0, "Trend 1D neutral"


def make_signal(x, di, mfall, mname, name, badge, when_txt, vol_txt, extra_line=None):
    """Prueft die Filter und baut den Text. Rueckgabe (Text, None) oder (None, Ablehngrund)."""
    side = x["side"]
    arrow = "\U0001F7E2 <b>KAUF</b>" if side == "KAUF" else "\U0001F534 <b>VERKAUF</b>"
    head = "%s  %s   %s" % (arrow, name, badge)
    if side == "VERKAUF":   # Verkauf: nur der Hinweis, keine Zusatzangaben
        lines = [head, "Kerze %s (Beginn)" % when_txt, "Linie %s | Schluss %s%s" % (fmt(x["line"]), fmt(x["close"]), vol_txt)]
        if extra_line:
            lines.append(extra_line)
        return "\n".join(lines), None
    if x["band_pct"] < MIN_BAND_PCT:
        return None, "kanal"
    if x["drop_atr"] < MIN_DROP_ATR or x["vol_ratio"] < MIN_VOL_RATIO:
        return None, "rueckgang_volumen"
    if WICK_ON and x["wick"] < WICK_MIN:
        return None, "kerzenform"
    if not di or di["atr"] <= 0:
        return None, "keine_tagesdaten"
    dist = di["atr"] * STOP_DAY_MULT
    if (di["hi"] - x["close_sig"]) / dist < MIN_R:
        return None, "r"
    st, st_txt = trend_state(di, x["close"])
    if TREND_FILTER and st == -1:
        return None, "trend"
    if MKT_FILTER and mfall:
        return None, "markt"
    r = (di["hi"] - x["close"]) / dist
    stop = x["close"] - dist
    lines = [
        head,
        "Kerze %s (Beginn%s)" % (when_txt, ", bestaetigt" if CONFIRM else ""),
        "<b>Kaufkurs %s</b> | Stop %s (%.1f %%) | Ziel %s (<b>%.1f R</b>)" % (fmt(x["close"]), fmt(stop), dist / x["close"] * 100, fmt(di["hi"]), r),
        "Linie %s | Signalkerze Schluss %s%s" % (fmt(x["line"]), fmt(x["close_sig"]), vol_txt),
        st_txt,
    ]
    if mfall:
        lines.append("Markt faellt mit (%s)" % mname)
    lines.append("Rueckgang %.1f ATR in 4 Kerzen | Volumen %.1fx Schnitt | Kanalbreite %.1f %% | Schluss %d %% der Kerze" % (x["drop_atr"], x["vol_ratio"], x["band_pct"], round(x["wick"] * 100)))
    if extra_line:
        lines.append(extra_line)
    return "\n".join(lines), None


def fmt(x):
    if x == 0:
        return "0"
    if abs(x) >= 100:
        return "%.2f" % x
    if abs(x) >= 1:
        return "%.4f" % x
    return "%.6g" % x


def day_info(coin, now_ms):
    """Tages-ATR, Hoch/Tief der letzten RANGE_DAYS Tage (inkl. heutigem Tag) und Tages-EMA50/EMA200. None, wenn nicht verfuegbar."""
    try:
        d = get_candles(coin, "1d", 260, now_ms)
        done = [k for k in d if k["T"] <= now_ms]
        if len(done) < ATR_LEN + 2:
            return None
        a = atr_wilder([k["h"] for k in done], [k["l"] for k in done], [k["c"] for k in done], ATR_LEN)
        closes = [k["c"] for k in done]
        e50 = ema(closes, 50)[-1] if len(closes) >= 50 else None
        e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
        rng = d[-RANGE_DAYS:]
        return {"atr": a[-1], "hi": max(k["h"] for k in rng), "lo": min(k["l"] for k in rng), "e50": e50, "e200": e200}
    except Exception:
        return None


def vienna(t_ms):
    try:
        from zoneinfo import ZoneInfo
        import datetime
        return datetime.datetime.fromtimestamp(t_ms / 1000, ZoneInfo("Europe/Vienna")).strftime("%d.%m. %H:%M")
    except Exception:
        return time.strftime("%d.%m. %H:%M", time.gmtime(t_ms / 1000 + 3600 * 2))


def telegram(text, html=False):
    if DRY_RUN or not TG_TOKEN:
        print("[Telegram aus] " + text)
        return
    global TG_CHAT
    if not TG_CHAT:
        TG_CHAT = discover_chat()
    if not TG_CHAT:
        print("Keine Chat-ID. Schreibe dem Bot einmal eine Nachricht (z. B. /start) und starte den Lauf erneut.")
        return
    # in Bloecken senden, damit HTML-Tags nie mitten im Tag getrennt werden
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > 3500:
            chunks.append(cur)
            cur = block
        else:
            cur = cur + "\n\n" + block if cur else block
    if cur:
        chunks.append(cur)
    url = "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN
    for part in chunks:
        payload = {"chat_id": TG_CHAT, "text": part, "disable_web_page_preview": True}
        if html:
            payload["parse_mode"] = "HTML"
        post(url, payload)


def discover_chat():
    try:
        with urllib.request.urlopen("https://api.telegram.org/bot%s/getUpdates" % TG_TOKEN, timeout=30) as r:
            res = json.loads(r.read().decode())
        for u in reversed(res.get("result", [])):
            m = u.get("message") or u.get("channel_post")
            if m and m.get("chat"):
                cid = str(m["chat"]["id"])
                print("Chat-ID gefunden:", cid, "(als Secret TELEGRAM_CHAT_ID eintragen)")
                return cid
    except Exception as e:
        print("getUpdates fehlgeschlagen:", e)
    return ""


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_t": {}, "last_sig": {}, "started": False}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)


_day_cache = {}


def day_info_cached(coin, now_ms):
    if coin not in _day_cache:
        _day_cache[coin] = day_info(coin, now_ms)
    return _day_cache[coin]


def process_timeframe(tf, state, todo, now_ms, first_run):
    """Prueft alle Maerkte fuer einen Zeitrahmen. Gibt (Nachrichten, Fehlerzahl, gefiltert, neu_aktiviert) zurueck."""
    ms = TF_MS[tf]
    blk = state.setdefault("tf", {}).get(tf)
    is_new = blk is None
    if is_new:
        blk = {"last_t": {}, "last_sent": {}, "done": None}
        if tf == "1h" and state.get("last_t"):   # Altbestand aus frueheren Versionen uebernehmen
            blk["last_t"] = dict(state["last_t"])
            blk["last_sent"] = {k: v for k, v in state.get("last_sent", {}).items()}
            blk["done"] = state.get("hour_done")
            is_new = False
        state["tf"][tf] = blk
    messages, errors = [], 0
    filtered = {"sperre": 0, "kanal": 0, "rueckgang_volumen": 0, "keine_tagesdaten": 0, "r": 0}
    badge = "%s <b>%s</b>" % (TF_BADGE[tf], TF_LABEL[tf])
    try:
        mkt_cs = [k for k in get_candles("BTC", tf, CANDLES, now_ms) if k["T"] <= now_ms]
    except Exception as e:
        mkt_cs = []
        print("BTC-Marktdaten nicht verfuegbar:", e)
    for coin, vol in todo:
        try:
            cs = get_candles(coin, tf, CANDLES, now_ms)
        except Exception as e:
            errors += 1
            print("Fehler %s %s: %s" % (tf, coin, e))
            time.sleep(SLEEP)
            continue
        cs = [k for k in cs if k["T"] <= now_ms]  # nur geschlossene Kerzen
        if len(cs) < max(EMA_LEN, ATR_LEN) * 2 + 2:
            time.sleep(SLEEP)
            continue
        newest = cs[-1]["t"]
        last_done = blk["last_t"].get(coin)
        if last_done is None:
            blk["last_t"][coin] = newest   # erster Kontakt: nur ab jetzt, nichts melden
            time.sleep(SLEEP)
            continue
        raw = evaluate(cs, last_done)
        new = [x for x in raw if x["t"] >= newest - TF_BACKFILL[tf] * ms]
        for x in new:
            side, t = x["side"], x["t"]
            last_sent = blk["last_sent"].get(coin + "|" + side)
            if last_sent is not None and t - last_sent < COOLDOWN_BARS * ms:
                filtered["sperre"] += 1
                continue
            di = day_info_cached(coin, now_ms) if side == "KAUF" else None
            mf = mkt_fall(mkt_cs, x["t_sig"]) if side == "KAUF" else False
            text, why = make_signal(x, di, mf, "BTC", coin, badge, vienna(t) + " Wien", " | 24h-Vol %.1f Mio USD" % (vol / 1e6))
            if text is None:
                filtered[why] = filtered.get(why, 0) + 1
                continue
            blk["last_sent"][coin + "|" + side] = t
            messages.append(text)
        blk["last_t"][coin] = newest
        time.sleep(SLEEP)
    return messages, errors, filtered, is_new


def main():
    now_ms = int(time.time() * 1000)
    state = load_state()
    first_run = not state.get("started")
    force = bool(os.environ.get("FORCE"))
    due = []
    for tf in TIMEFRAMES:
        closed_t = (now_ms // TF_MS[tf]) * TF_MS[tf] - TF_MS[tf]   # Beginn der zuletzt geschlossenen Kerze
        done = (state.get("tf", {}).get(tf) or {}).get("done")
        if tf == "1h" and "tf" not in state:
            done = state.get("hour_done")
        if done == closed_t and not force:
            continue
        due.append((tf, closed_t, done))
    if not due:
        print("Alle Zeitrahmen (%s) sind aktuell, nichts zu tun." % ",".join(TIMEFRAMES))
        return
    markets = list_markets()
    todo = sorted([m for m in markets if m[1] >= MIN_VOLUME], key=lambda x: -x[1])
    limit = int(os.environ.get("MAX_MARKETS", "0"))
    if limit:
        todo = todo[:limit]
    print("Maerkte gesamt %d, geprueft (Volumen >= %.0f USD): %d | Zeitrahmen faellig: %s" % (len(markets), MIN_VOLUME, len(todo), ",".join(t for t, _, _ in due)))

    all_msgs, activated = [], []
    for tf, closed_t, done in due:
        messages, errors, filtered, is_new = process_timeframe(tf, state, todo, now_ms, first_run)
        print("[%s] Signale: %d, Fehler: %d, gefiltert: %s" % (tf, len(messages), errors, filtered))
        all_msgs += messages
        if is_new:
            activated.append(tf)
        gap = (closed_t - done) / TF_MS[tf] if done else 0
        if gap > TF_BACKFILL[tf] + 2 and not first_run:
            telegram("SniperJoe: Der Waechter war im Zeitrahmen %s etwa %d Kerzen lang nicht aktiv. Es werden nur die letzten %d nachgeholt." % (TF_LABEL[tf], gap, TF_BACKFILL[tf]))
        if errors == 0:
            state["tf"][tf]["done"] = closed_t
        if errors > len(todo) * 0.5 and len(todo) > 0:
            telegram("SniperJoe: Warnung, mehr als die Haelfte der Abfragen ist fehlgeschlagen (%s: %d von %d)." % (TF_LABEL[tf], errors, len(todo)))

    state["started"] = True
    if first_run:
        telegram("SniperJoe gestartet. Ueberwache %d Hyperliquid-Maerkte (Zeitrahmen %s, Modus %s, Linien EMA%d +/- %.1f ATR%d). Ab jetzt kommen Signale." % (len(todo), ", ".join(TF_LABEL[t] for t in TIMEFRAMES), MODE, EMA_LEN, EXT, ATR_LEN))
    elif activated:
        telegram("SniperJoe: Neuer Zeitrahmen aktiv: %s. Ab jetzt kommen auch dort Signale. Aktive Zeitrahmen: %s." % (", ".join(TF_LABEL[t] for t in activated), ", ".join(TF_LABEL[t] for t in TIMEFRAMES)))
    if all_msgs:
        header = "SniperJoe: %d Signal(e)\n\n" % len(all_msgs)
        telegram(header + "\n\n".join(all_msgs) + "\n\nKein Handelssignal. Plan und Risiko pruefen.", html=True)
    save_state(state)


if __name__ == "__main__":
    main()
