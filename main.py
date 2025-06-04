import ccxt
import os
import pandas as pd
import ta
import time
import schedule
import traceback

from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv('API_KEY')
secret = os.getenv('SECRET')


def check_trade_signal(exchange, symbol):
    ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=10)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()

    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=10).average_true_range()
    rsi = ta.momentum.RSIIndicator(df['close'], window=10).rsi()

    df['atr'] = atr
    df['rsi'] = rsi

    latest_close = df['close'].iloc[-1]
    latest_atr = df['atr'].iloc[-1]
    normalized_atr = latest_atr / latest_close
    ema_9 = df['EMA_9'].iloc[-1]
    ema_21 = df['EMA_21'].iloc[-1]
    rsi_now = df['rsi'].iloc[-1]
    rsi_prev = df['rsi'].iloc[-2]

    trend = "uptrend" if ema_9 > ema_21 else "downtrend" if ema_9 < ema_21 else "sideways"

    should_trade, side = False, None
    if normalized_atr > 0.015:
        if trend == "downtrend" and rsi_now <= 30:
            should_trade, side = True, 'buy'
        elif trend == "uptrend" and rsi_now >= 70:
            should_trade, side = True, 'sell'

    return should_trade, side, {
        'atr': latest_atr,
        'atr_norm': normalized_atr,
        'ema_9': ema_9,
        'ema_21': ema_21,
        'rsi_now': rsi_now,
        'rsi_prev': rsi_prev,
        'trend': trend
    }

# Trading parameters
usdt_value = 1.5
leverage = 10

# MAIN LOOP
def main():
    try:
        exchange = ccxt.phemex({
        'apiKey': api_key,
        'secret': secret,
        'options': {'defaultType': 'swap'},
        })

        MAX_NO_SELL_TRADE = 15
        MAX_NO_BUY_TRADE = 2

        # Preload once
        # Step 1: Fetch all markets and filter for swap symbols
        markets = exchange.load_markets()
        all_symbols = [s for s in markets if s.endswith(':USDT') and markets[s]['type'] == 'swap']

        # Step 2: Fetch all positions (returns full position objects)
        positions = exchange.fetch_positions(symbols=all_symbols)

        # Step 3: Filter positions that are open (contracts > 0)
        open_positions = [pos for pos in positions if pos.get('contracts') and abs(float(pos['contracts'])) > 0]

        # Step 4: Get symbols that are NOT opened
        opened_symbols = {pos['symbol'] for pos in open_positions}
        symbols_not_opened = [s for s in all_symbols if s not in opened_symbols]

        for symbol in symbols_not_opened:
            try:
                signal, side, details = check_trade_signal(exchange, symbol)
                print(f"{symbol} → Signal: {signal}, Side: {side}, Trend: {details['trend']}")

                if signal:
                    # Step 5: Count short positions from full position objects
                    short_positions = [
                        pos for pos in open_positions
                        if (pos.get('side') == 'short') or
                        ('size' in pos and float(pos['size']) < 0) or
                        ('info' in pos and pos['info'].get('side', '').lower() == 'sell')
                    ]

                    # Step 5b: Count long positions from full position objects
                    long_positions = [
                        pos for pos in open_positions
                        if (pos.get('side') == 'long') or
                        ('size' in pos and float(pos['size']) > 0) or
                        ('info' in pos and pos['info'].get('side', '').lower() == 'buy')
                    ]
                    # Final: Count shorts
                    short_count = len(short_positions)
                    long_count = len(long_positions)
                    print(f"Short positions open: {short_count}")
                    print(f"Long positions open: {long_count}")
                    if short_count >= MAX_NO_SELL_TRADE and side == 'sell':
                        print(f"❌ Skip {symbol}: sell limit reached")
                        continue
                    if long_count >= MAX_NO_BUY_TRADE and side == 'buy':
                        print(f"❌ Skip {symbol}: buy limit reached")
                        continue

                    ticker = exchange.fetch_ticker(symbol)
                    market_price = ticker['last']
                    base_amount = usdt_value / market_price
                    exchange.set_leverage(leverage, symbol)
                    print(f"🔔 ORDER → {symbol} | {side.upper()} | Price: {market_price:.4f} | Qty: {base_amount:.5f}")

                    try:
                        order = exchange.create_order(
                            symbol=symbol,
                            type='market',
                            side=side,
                            amount=base_amount,
                            params={'reduceOnly': False}
                        )
                        print(f"Order Result: {order}")
                    except ccxt.BaseError as e:
                        if 'TE_ERR_INCONSISTENT_POS_MODE' in str(e):
                            order = exchange.create_order(
                                symbol=symbol,
                                type='market',
                                side=side,
                                amount=base_amount,
                                params={
                                    'reduceOnly': False,
                                    'posSide': 'Short' if side == "sell" else 'Long'
                                }
                            )
                            print(f"Order Result: {order}")
            except Exception as e:
                print(f"⚠️ {symbol} → Error: {e}")
    except Exception as e:
        print("Error inside job:")
        traceback.print_exc()

schedule.every(6).seconds.do(main)

# ✅ Outer loop handles everything
while True:
    try:
        schedule.run_pending()
        time.sleep(1)
    except Exception as e:
        print("Scheduler crashed:")
        traceback.print_exc()
        print("Retrying in 10 seconds...")
        time.sleep(8)
