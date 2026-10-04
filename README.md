# SniperJoe

Beobachtet alle Hyperliquid-Perps (Krypto und, falls als HIP-3-Markt gelistet, TradFi) im 1h-Chart und schickt eine Telegram-Nachricht, wenn eine Kerze die untere oder obere Linie beruehrt.

Linien: EMA(20) +/- 2.5 x ATR(14). Nur geschlossene Kerzen.
- Kerze beruehrt untere Linie: KAUF
- Kerze beruehrt obere Linie: VERKAUF
- Pro Markt wird ein Signal nur gemeldet, wenn die Kerze neu ist; Abwechslung KAUF/VERKAUF wird im Zustand mitgefuehrt.
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
