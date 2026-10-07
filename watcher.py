#!/usr/bin/env python3
"""SniperJoe: Waechter fuer Hyperliquid-Perps.

Regel (gleich wie Perp Guard v1.9, 1h-Kerzen):
  Linien: EMA(20) +/- 2.5 * ATR(14)   (ATR nach Wilder, wie TradingView ta.atr)
  Modus "Gegenbewegung": Tief <= untere Linie -> KAUF, Hoch >= obere Linie -> VERKAUF
  Modus "Ausbruch": umgekehrt
  Filter (alle per Umgebungsvariable einstellbar):
    - nur KAUF: R >= MIN_R (Chance bis 10-Tage-Hoch, Stop = 1 Tages-ATR). VERKAUF ohne R-Filter.
    - KAUF: Kanalbreite (obere/untere Linie, vor der Signalkerze) >= MIN_BAND_PCT %
    - KAUF: starker Rueckgang (>= MIN_DROP_ATR ATR in 4 Kerzen) mit starkem Volumen (>= MIN_VOL_RATIO x Schnitt der letzten 20 Kerzen)
  Kein Abwechseln mehr. Pro Markt und Richtung gilt eine Sperre von COOLDOWN_H Stunden.
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
INTERVAL = "1h"
INTERVAL_MS = 3600_000
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
COOLDOWN_H = float(os.environ.get("COOLDOWN_H", "6"))
STOP_DAY_MULT = float(os.environ.get("STOP_DAY_MULT", "1.0"))
RANGE_DAYS = int(os.environ.get("RANGE_DAYS", "10"))

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
    step = INTERVAL_MS if interval == "1h" else 86400_000
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
    """Liste von Signal-Dicts (Rohsignale) fuer Kerzen mit t > last_t_done."""
    h = [k["h"] for k in candles]
    l = [k["l"] for k in candles]
    c = [k["c"] for k in candles]
    v = [k.get("v", 0.0) for k in candles]
    e = ema(c, EMA_LEN)
    a = atr_wilder(h, l, c, ATR_LEN)
    sig = []
    warm = max(EMA_LEN, ATR_LEN) * 2
    for i, k in enumerate(candles):
        if i < warm or k["t"] <= last_t_done:
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
        sig.append({
            "t": k["t"], "side": side, "line": dn if touch_dn else up, "close": k["c"], "atr": a[i],
            "band_pct": band_prev,
            "vol_ratio": vol_ratio, "drop_atr": drop_atr, "rise_atr": rise_atr,
            "touch_low": touch_dn,
        })
    return sig


def fmt(x):
    if x == 0:
        return "0"
    if abs(x) >= 100:
        return "%.2f" % x
    if abs(x) >= 1:
        return "%.4f" % x
    return "%.6g" % x


def day_info(coin, now_ms):
    """Tages-ATR und Hoch/Tief der letzten RANGE_DAYS Tage (inkl. heutigem Tag). None, wenn nicht verfuegbar."""
    try:
        d = get_candles(coin, "1d", 40, now_ms)
        done = [k for k in d if k["T"] <= now_ms]
        if len(done) < ATR_LEN + 2:
            return None
        a = atr_wilder([k["h"] for k in done], [k["l"] for k in done], [k["c"] for k in done], ATR_LEN)
        rng = d[-RANGE_DAYS:]
        return {"atr": a[-1], "hi": max(k["h"] for k in rng), "lo": min(k["l"] for k in rng)}
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


def main():
    now_ms = int(time.time() * 1000)
    state = load_state()
    first_run = not state.get("started")
    closed_t = (now_ms // INTERVAL_MS) * INTERVAL_MS - INTERVAL_MS  # Start der zuletzt geschlossenen Kerze
    if state.get("hour_done") == closed_t and not os.environ.get("FORCE"):
        print("Kerze %d bereits verarbeitet, nichts zu tun." % closed_t)
        return
    gap_h = (closed_t - state["hour_done"]) / INTERVAL_MS if state.get("hour_done") else 0
    markets = list_markets()
    todo = sorted([m for m in markets if m[1] >= MIN_VOLUME], key=lambda x: -x[1])
    limit = int(os.environ.get("MAX_MARKETS", "0"))
    if limit:
        todo = todo[:limit]
    print("Maerkte gesamt %d, geprueft (Volumen >= %.0f USD): %d" % (len(markets), MIN_VOLUME, len(todo)))

    messages = []
    filtered = {"sperre": 0, "kanal": 0, "rueckgang_volumen": 0, "keine_tagesdaten": 0, "r": 0}
    errors = 0
    for coin, vol in todo:
        try:
            cs = get_candles(coin, INTERVAL, CANDLES, now_ms)
        except Exception as e:
            errors += 1
            print("Fehler %s: %s" % (coin, e))
            time.sleep(SLEEP)
            continue
        cs = [k for k in cs if k["T"] <= now_ms]  # nur geschlossene Kerzen
        if len(cs) < max(EMA_LEN, ATR_LEN) * 2 + 2:
            time.sleep(SLEEP)
            continue
        newest = cs[-1]["t"]
        last_done = state["last_t"].get(coin)
        if last_done is None:
            last_done = newest - 1  # erster Kontakt: nur ab jetzt
            state["last_t"][coin] = newest
            time.sleep(SLEEP)
            continue
        raw = evaluate(cs, last_done)
        new = [x for x in raw if x["t"] >= newest - BACKFILL_MAX * INTERVAL_MS]
        for x in new:
            side, t = x["side"], x["t"]
            key = coin + "|" + side
            last_sent = state.setdefault("last_sent", {}).get(key)
            if last_sent is not None and t - last_sent < COOLDOWN_H * INTERVAL_MS:
                filtered["sperre"] += 1
                continue
            if side == "KAUF":
                if x["band_pct"] < MIN_BAND_PCT:
                    filtered["kanal"] += 1
                    continue
                if x["drop_atr"] < MIN_DROP_ATR or x["vol_ratio"] < MIN_VOL_RATIO:
                    filtered["rueckgang_volumen"] += 1
                    continue
            di = day_info(coin, now_ms)
            have_day = bool(di) and di["atr"] > 0
            if side == "KAUF" and not have_day:
                filtered["keine_tagesdaten"] += 1
                continue
            r = stop = dist = None
            if have_day:
                dist = di["atr"] * STOP_DAY_MULT
                r = ((di["hi"] - x["close"]) if side == "KAUF" else (x["close"] - di["lo"])) / dist
                stop = x["close"] - dist if side == "KAUF" else x["close"] + dist
            if side == "KAUF" and r < MIN_R:   # R-Filter gilt nur fuer KAUF
                filtered["r"] += 1
                continue
            state["last_sent"][key] = t
            arrow = "\U0001F7E2 <b>KAUF</b>" if side == "KAUF" else "\U0001F534 <b>VERKAUF</b>"
            lines = [
                "%s  %s" % (arrow, coin),
                "Kerze %s (Wien)" % vienna(t),
                "Linie %s | Schluss %s | 24h-Vol %.1f Mio USD" % (fmt(x["line"]), fmt(x["close"]), vol / 1e6),
            ]
            if have_day:
                lines.append("Chance bis %d-Tage-%s: <b>%.1f R</b>" % (RANGE_DAYS, "Hoch" if side == "KAUF" else "Tief", r))
                lines.append("Stop-Vorschlag (%.1f Tages-ATR): %s (%.1f %%)" % (STOP_DAY_MULT, fmt(stop), dist / x["close"] * 100))
            if side == "KAUF":
                lines.append("Rueckgang %.1f ATR in 4 Kerzen | Volumen %.1fx Schnitt | Kanalbreite %.1f %%" % (x["drop_atr"], x["vol_ratio"], x["band_pct"]))
            messages.append("\n".join(lines))
        state["last_t"][coin] = newest
        time.sleep(SLEEP)

    if first_run:
        state["started"] = True
        telegram("SniperJoe gestartet. Ueberwache %d Hyperliquid-Maerkte (1h, Modus %s, Linien EMA%d +/- %.1f ATR%d). Ab jetzt kommen Signale." % (len(todo), MODE, EMA_LEN, EXT, ATR_LEN))
    if messages:
        header = "SniperJoe: %d Signal(e)\n\n" % len(messages)
        telegram(header + "\n\n".join(messages) + "\n\nKein Handelssignal. Plan und Risiko pruefen.", html=True)
    print("Signale: %d, Fehler: %d, gefiltert: %s" % (len(messages), errors, filtered))
    if gap_h > 3 and not first_run:
        telegram("SniperJoe: Der Waechter war etwa %d Stunden nicht aktiv. Es werden nur die letzten %d Kerzen nachgeholt." % (gap_h, BACKFILL_MAX))
    if errors == 0:
        state["hour_done"] = closed_t
    if errors > len(todo) * 0.5 and len(todo) > 0:
        telegram("SniperJoe: Warnung, mehr als die Haelfte der Abfragen ist fehlgeschlagen (%d von %d)." % (errors, len(todo)))
    save_state(state)


if __name__ == "__main__":
    main()
