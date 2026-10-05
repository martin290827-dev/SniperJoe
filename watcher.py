#!/usr/bin/env python3
"""SniperJoe: Waechter fuer Hyperliquid-Perps.

Regel (gleich wie Perp Guard v1.9, 1h-Kerzen):
  Linien: EMA(20) +/- 2.5 * ATR(14)   (ATR nach Wilder, wie TradingView ta.atr)
  Modus "Gegenbewegung": Tief <= untere Linie -> KAUF, Hoch >= obere Linie -> VERKAUF
  Modus "Ausbruch": umgekehrt
  Signale wechseln sich je Markt ab (nach KAUF kommt erst wieder VERKAUF und umgekehrt).
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
        out.append({"t": int(k["t"]), "T": int(k["T"]), "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"])})
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
    """Gibt Liste (kerzen_t, 'KAUF'/'VERKAUF', linie, schluss) fuer Kerzen mit t > last_t_done zurueck (Rohsignale)."""
    h = [k["h"] for k in candles]
    l = [k["l"] for k in candles]
    c = [k["c"] for k in candles]
    e = ema(c, EMA_LEN)
    a = atr_wilder(h, l, c, ATR_LEN)
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
        if side:
            line = dn if touch_dn else up
            sig.append((k["t"], side, line, k["c"], a[i]))
    return sig


def fmt(x):
    if x == 0:
        return "0"
    if abs(x) >= 100:
        return "%.2f" % x
    if abs(x) >= 1:
        return "%.4f" % x
    return "%.6g" % x


def day_atr(coin, now_ms):
    try:
        d = get_candles(coin, "1d", 40, now_ms)
        d = [k for k in d if k["T"] <= now_ms]
        if len(d) < ATR_LEN + 2:
            return None
        a = atr_wilder([k["h"] for k in d], [k["l"] for k in d], [k["c"] for k in d], ATR_LEN)
        return a[-1]
    except Exception:
        return None


def telegram(text):
    if DRY_RUN or not TG_TOKEN:
        print("[Telegram aus] " + text)
        return
    global TG_CHAT
    if not TG_CHAT:
        TG_CHAT = discover_chat()
    if not TG_CHAT:
        print("Keine Chat-ID. Schreibe dem Bot einmal eine Nachricht (z. B. /start) und starte den Lauf erneut.")
        return
    for i in range(0, len(text), 3800):
        part = text[i:i + 3800]
        url = "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN
        post(url, {"chat_id": TG_CHAT, "text": part, "disable_web_page_preview": True})


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
            # Zustand der Abwechslung aus der Historie ableiten (letztes Rohsignal), ohne zu melden
            hist = evaluate(cs, 0)
            if hist:
                state["last_sig"][coin] = hist[-1][1]
            time.sleep(SLEEP)
            continue
        raw = evaluate(cs, last_done)
        new = [s for s in raw if s[0] > last_done and s[0] >= newest - BACKFILL_MAX * INTERVAL_MS]
        for t, side, line, close, a in new:
            if state["last_sig"].get(coin) == side:
                continue
            state["last_sig"][coin] = side
            da = day_atr(coin, now_ms)
            if da:
                stop = close - da if side == "KAUF" else close + da
                stop_txt = "Stop-Vorschlag (1 Tages-ATR): %s (%.1f %%)" % (fmt(stop), da / close * 100)
            else:
                stop_txt = "Stop-Vorschlag: n/a"
            ts = time.strftime("%d.%m. %H:%M", time.gmtime(t / 1000 + 3600 * 2))  # ungefaehr Wien (Sommerzeit)
            arrow = "🟢 KAUF" if side == "KAUF" else "🔴 VERKAUF"
            messages.append("%s  %s\nKerze %s (Wien)\nLinie %s | Schluss %s | 24h-Vol %.1f Mio USD\n%s" % (arrow, coin, ts, fmt(line), fmt(close), vol / 1e6, stop_txt))
        state["last_t"][coin] = newest
        time.sleep(SLEEP)

    if first_run:
        state["started"] = True
        telegram("SniperJoe gestartet. Ueberwache %d Hyperliquid-Maerkte (1h, Modus %s, Linien EMA%d +/- %.1f ATR%d). Ab jetzt kommen Signale." % (len(todo), MODE, EMA_LEN, EXT, ATR_LEN))
    if messages:
        header = "SniperJoe: %d Signal(e)\n\n" % len(messages)
        telegram(header + "\n\n".join(messages) + "\n\nKein Handelssignal. Plan und Risiko pruefen.")
    print("Signale: %d, Fehler: %d" % (len(messages), errors))
    if gap_h > 3 and not first_run:
        telegram("SniperJoe: Der Waechter war etwa %d Stunden nicht aktiv. Es werden nur die letzten %d Kerzen nachgeholt." % (gap_h, BACKFILL_MAX))
    if errors == 0:
        state["hour_done"] = closed_t
    if errors > len(todo) * 0.5 and len(todo) > 0:
        telegram("SniperJoe: Warnung, mehr als die Haelfte der Abfragen ist fehlgeschlagen (%d von %d)." % (errors, len(todo)))
    save_state(state)


if __name__ == "__main__":
    main()
