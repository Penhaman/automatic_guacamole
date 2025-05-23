import ccxt
import pandas as pd
import ta
import logging
import requests
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import asyncio
import os

# ================== CONFIGURAÇÕES ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
EXCHANGE = ccxt.binance()
VALID_TIMEFRAMES = ['15m']
SYMBOL_LIMIT = 200

logging.basicConfig(level=logging.INFO)
scheduler = BackgroundScheduler()
application = None  # Será definido no main()

# ========== Indicadores ==========

def fetch_data(symbol, timeframe='15m', limit=100):
    try:
        ohlcv = EXCHANGE.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"[{symbol}] Erro: {e}")
        return None

def apply_indicators(df):
    df['rsi'] = ta.momentum.RSIIndicator(close=df['close'], window=7).rsi()
    macd = ta.trend.MACD(close=df['close'])
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    df['obv'] = ta.volume.OnBalanceVolumeIndicator(close=df['close'], volume=df['volume']).on_balance_volume()
    bb = ta.volatility.BollingerBands(close=df['close'])
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_lower'] = bb.bollinger_lband()
    return df

def avaliar_sinal(df):
    sinais = {"long": 0, "short": 0}
    close = df['close'].iloc[-1]

    rsi = df['rsi'].iloc[-1]
    if rsi < 30:
        sinais["long"] += 1
    elif rsi > 70:
        sinais["short"] += 1

    macd = df['macd'].iloc[-1]
    macd_signal = df['macd_signal'].iloc[-1]
    prev_macd = df['macd'].iloc[-2]
    prev_signal = df['macd_signal'].iloc[-2]

    if prev_macd < prev_signal and macd > macd_signal:
        sinais["long"] += 1
    elif prev_macd > prev_signal and macd < macd_signal:
        sinais["short"] += 1

    obv_diff = df['obv'].iloc[-1] - df['obv'].iloc[-2]
    if obv_diff > 0:
        sinais["long"] += 1
    elif obv_diff < 0:
        sinais["short"] += 1

    bb_upper = df['bb_upper'].iloc[-1]
    bb_lower = df['bb_lower'].iloc[-1]

    if close <= bb_lower:
        sinais["long"] += 1
    elif close >= bb_upper:
        sinais["short"] += 1

    return sinais, rsi, macd, obv_diff, close, bb_upper, bb_lower

# ========== CoinGecko ==========
def obter_top_symbols(limit=200):
    url = "https://api.coingecko.com/api/v3/exchanges/binance/tickers"
    try:
        res = requests.get(url)
        data = res.json()
        pares = sorted(set(item['base'] + '/' + item['target'] for item in data['tickers']))
        return pares[:limit]
    except Exception as e:
        print("Erro CoinGecko:", e)
        return []

# ========== Bot Telegram ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Envie: /siga BTCUSDT para análise manual.\nO bot envia sinais automáticos ao grupo a cada 30 minutos.")

async def siga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❗ Uso: /siga BTCUSDT")
        return

    raw_symbol = context.args[0].upper()
    if '/' not in raw_symbol:
        for base in ['USDT', 'BUSD', 'ETH', 'BTC']:
            if raw_symbol.endswith(base):
                raw_symbol = raw_symbol.replace(base, f"/{base}")
                break
    symbol = raw_symbol

    markets = EXCHANGE.load_markets()
    if symbol not in markets:
        await update.message.reply_text("❌ Par inválido")
        return

    df = fetch_data(symbol, '15m')
    if df is None:
        await update.message.reply_text("❌ Erro ao obter dados do par")
        return

    df = apply_indicators(df)
    sinais, rsi, macd, obv_diff, close, bb_upper, bb_lower = avaliar_sinal(df)

    msg = f"""📊 Análise de {symbol} (15m):
🔸 RSI(7): {rsi:.2f}
🔸 MACD: {macd:.5f}
🔸 OBV Δ: {'🔺' if obv_diff > 0 else '🔻'}
🔸 Preço: {close:.4f}
🔸 BBands: [{bb_lower:.4f} - {bb_upper:.4f}]
✅ Long: {sinais['long']} / 4
✅ Short: {sinais['short']} / 4
"""

    if sinais["long"] >= 3:
        msg += "\n📢 *SINAL DE COMPRA (LONG)*"
    elif sinais["short"] >= 3:
        msg += "\n📢 *SINAL DE VENDA (SHORT)*"
    else:
        msg += "\n⚠️ Nenhum sinal forte."

    await update.message.reply_text(msg, parse_mode="Markdown")

# ========== Auto Scan ==========
async def auto_analise():
    print("🔄 Analisando top 200 pares automaticamente...")
    symbols = obter_top_symbols(SYMBOL_LIMIT)
    markets = EXCHANGE.load_markets()
    for symbol in symbols:
        if symbol not in markets:
            continue
        df = fetch_data(symbol, '15m')
        if df is None or len(df) < 20:
            continue
        try:
            df = apply_indicators(df)
            sinais, rsi, macd, obv_diff, close, bb_upper, bb_lower = avaliar_sinal(df)

            if sinais["long"] >= 3 or sinais["short"] >= 3:
                mensagem = f"""📊 *SINAL DETECTADO* — {symbol} (15m)
🔸 RSI(7): {rsi:.2f}
🔸 MACD: {macd:.5f}
🔸 OBV Δ: {'🔺' if obv_diff > 0 else '🔻'}
🔸 Preço: {close:.4f}
🔸 BBands: [{bb_lower:.4f} - {bb_upper:.4f}]
✅ Long: {sinais['long']} / 4
✅ Short: {sinais['short']} / 4
"""

                if sinais["long"] >= 3:
                    mensagem += "\n📢 *SINAL DE COMPRA (LONG)*"
                elif sinais["short"] >= 3:
                    mensagem += "\n📢 *SINAL DE VENDA (SHORT)*"

                await application.bot.send_message(chat_id=GROUP_CHAT_ID, text=mensagem, parse_mode="Markdown")

        except Exception as e:
            print(f"Erro ao analisar {symbol}: {e}")
            continue

# ========== Bot Commands ==========
async def configurar_comandos(app):
    comandos = [
        BotCommand("start", "Inicia o bot"),
        BotCommand("siga", "Analisa um par individual"),
        BotCommand("update", "Executa análise dos top 200 agora")
    ]
    await app.bot.set_my_commands(comandos)
async def update_sinais(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Executando análise manual dos top 200 pares...")
    await auto_analise()
    await update.message.reply_text("✅ Análise concluída e sinais enviados (se encontrados).")


# ========== Main ==========
def main():
    global application
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("siga", siga))
    application.add_handler(CommandHandler("update", update_sinais))


    loop = asyncio.get_event_loop()
    loop.run_until_complete(configurar_comandos(application))

    scheduler.add_job(lambda: asyncio.run(auto_analise()), 'interval', minutes=30)
    scheduler.start()

    print("🤖 Bot rodando com autoanálise a cada 30min...")
    application.run_polling()

if __name__ == '__main__':
    main()
