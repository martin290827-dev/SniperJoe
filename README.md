# SniperJoe

Beobachtet alle Hyperliquid-Perps (Krypto und, falls als HIP-3-Markt gelistet, TradFi) im Zeitrahmen 4h und 1D (einstellbar, auch 1h moeglich) und schickt eine Telegram-Nachricht, wenn eine Kerze die untere oder obere Linie beruehrt.

Linien: EMA(20) +/- 2.5 x ATR(14). Nur geschlossene Kerzen.
- Kerze beruehrt untere Linie: KAUF
- Kerze beruehrt obere Linie: VERKAUF
- Pro Markt und Richtung gilt eine Sperre von 6 Stunden (kein Abwechseln mehr).
- Modus "Ausbruch" (umgekehrt) ueber Variable MODE=Ausbruch.

Hinweis: Regel ist nicht als profitabel belegt. Es ist ein Hinweisgeber, kein Handelssystem.

## Einrichtung
1. In Telegram dem Bot t.me/SniperJoe99_bot eine Nachricht senden (/start).
2. GitHub: Settings > Secrets and variables > Actions > New repository secret
   - `TELEGRAM_TOKEN` = Token von @BotFather (nie in Dateien oder Chat posten)
   - `TELEGRAM_CHAT_ID` = optional. Fehlt es, sucht das Skript die Chat-ID selbst (Log zeigt sie).
3. Actions > "SniperJoe Watch" > Run workflow (einmal manuell). Danach laeuft es stuendlich (Minute 3).
4. Erster Lauf sendet nur "gestartet" und meldet keine alten Signale.

## Einstellungen (Env im Workflow)
EMA_LEN, ATR_LEN, EXT, MODE (Gegenbewegung | Ausbruch), MIN_VOLUME_USD (Standard 2 Mio, gegen Signalflut), INCLUDE_HIP3, BACKFILL_MAX, MAX_MARKETS.

## Lokal testen
`DRY_RUN=1 MAX_MARKETS=5 python watcher.py` (gibt statt Telegram auf der Konsole aus)

## Grenzen
- GitHub-Cron kann um Minuten verzoegert sein. Signale sind Stunden-Signale, kein Echtzeit.
- Zustand in `state.json` (wird vom Workflow committet).

## Filter (ab v2)
- Nur KAUF: R >= 2, R = (10-Tage-Hoch - Schluss) / Tages-ATR. VERKAUF hat keinen R-Filter (R wird nur angezeigt).
- Nur KAUF: Kanalbreite vor der Signalkerze >= 5 %, Rueckgang >= 2 ATR in 4 Kerzen, Volumen >= 2x Schnitt der letzten 20 Kerzen.
- Telegram: KAUF und VERKAUF fett mit gruenem bzw. rotem Punkt (Telegram kennt keine Textfarbe).
- Alle Werte sind Startwerte, kein belegtes Optimum. Einstellbar im Workflow (Abschnitt env).

## Zeitrahmen
Standard: 4h und 1D (Variable TIMEFRAMES im Workflow, z. B. "1h,4h,1d"). Telegram markiert den Zeitrahmen farbig: 4H blau, 1D lila, 1H weiss.
Sperre gegen Wiederholungen: 6 Kerzen des jeweiligen Zeitrahmens pro Markt und Richtung.
