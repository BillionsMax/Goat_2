import time
import requests
import pandas as pd
import logging
import logging.handlers
from concurrent_log_handler import ConcurrentRotatingFileHandler
from binance import Client, ThreadedWebsocketManager
from tenacity import retry, stop_after_attempt, wait_exponential
from cred import KEY, SECRET
import winsound  # Модуль для воспроизведения звуков
import asyncio
import platform
import os

# Уникальный идентификатор стратегии
STRATEGY_NAME = "strategy1"  # Укажите уникальное имя для каждой стратегии

# Настройка пути для папки логов
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Имя файла логов, уникальное для каждой стратегии
log_file = os.path.join(log_dir, f'trading_strategy_{STRATEGY_NAME}.log')

# Настройка логирования с ротацией файлов и кодировкой UTF-8
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
handler = ConcurrentRotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding='utf-8')
logging.getLogger().addHandler(handler)

# Установка SelectorEventLoop для Windows
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Инициализация клиента Binance
client = Client(KEY, SECRET)

# Инициализация WebSocket менеджера
twm = ThreadedWebsocketManager(api_key=KEY, api_secret=SECRET)

# Основные параметры стратегии
MAX_POSITION = 0.03
STOP_LOSS_PERCENT = 2
TAKE_PROFIT_PERCENT = 6  # Соотношение 1 к 3
TIMEOUT = time.time() + 60 * 60 * 12  # 12 часов работы скрипта
TARGET_PRICES = {}  # Целевые цены будут определяться динамически
previous_prices = {}  # Хранение предыдущих цен для проверки изменений

# Класс для управления рисками
class TradingConfig:
    MAX_POSITIONS = 3  # Максимальное количество одновременных позиций
    MAX_DAILY_TRADES = 10  # Максимальное количество сделок в день
    MIN_VOLUME_USD = 1000000  # Минимальный 24h объем торгов в USD
    MAX_SPREAD_PERCENT = 0.1  # Максимальный спред
    LEVERAGE = 3  # Уровень плеча

    # Динамические стопы
    MIN_STOP_LOSS = 0.5  # Минимальный стоп-лосс в %
    MAX_STOP_LOSS = 5.0  # Максимальный стоп-лосс в %
    ATR_MULTIPLIER = 2.0  # Множитель ATR для стопов

    # Параметры объема позиции
    RISK_PER_TRADE = 0.01  # Риск на сделку (1% от баланса)

# Функция для получения минимального шага цены (tick size)
@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
def get_tick_size(symbol):
    try:
        exchange_info = client.futures_exchange_info()
        for s in exchange_info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        return float(f['tickSize'])
    except Exception as e:
        logging.error(f"Ошибка при получении tick size для {symbol}: {e}")
    return None

# Функция округления цены по шагу
def round_price(price, tick_size):
    return round(price / tick_size) * tick_size

# Функция отправки уведомлений в Telegram
def send_telegram_message(message):
    pass  # Добавьте реализацию отправки сообщения в Telegram при необходимости

# Функция для определения целевой цены на основе технического анализа
def determine_target_price(symbol):
    try:
        df = get_futures_klines(symbol, limit=100)
        if df.empty:
            return None
        sma_20 = df['close'].rolling(window=20).mean().iloc[-1]
        atr = df['high'].subtract(df['low']).rolling(window=14).mean().iloc[-1]
        target_price = sma_20 - atr  # Пример: целевая цена устанавливается на уровне SMA20 минус ATR
        return target_price
    except Exception as e:
        logging.error(f"Ошибка при определении целевой цены для {symbol}: {e}")
        return None

# Функция открытия позиции с использованием лимитного ордера
def open_position(symbol, side, quantity):
    order_side = "BUY" if side == 'long' else "SELL"
    try:
        current_price = float(client.get_symbol_ticker(symbol=symbol)['price'])
        tick_size = get_tick_size(symbol)
        limit_price = round_price(current_price * (0.99 if side == 'long' else 1.01), tick_size)  # Лимитная цена на 1% лучше текущей

        response = client.futures_create_order(
            symbol=symbol, side=order_side, type="LIMIT", quantity=quantity, price=limit_price, timeInForce="GTC"
        )
        logging.info(f"Открыта лимитная позиция {side} для {symbol} по цене {limit_price}: {response}")
        winsound.Beep(1000, 500)  # Звуковой сигнал при открытии позиции

        # Установка тейк-профита и стоп-лосса с динамическим управлением
        take_profit_level_1 = round_price(current_price * (1 + TAKE_PROFIT_PERCENT / 100), tick_size)
        stop_loss_level_1 = round_price(current_price * (1 - STOP_LOSS_PERCENT / 100), tick_size)
        stop_order_side = "SELL" if side == 'long' else "BUY"

        # Закрытие 50% позиции при достижении тейк-профита 1 к 3
        client.futures_create_order(symbol=symbol, side=stop_order_side, type="TAKE_PROFIT_MARKET",
                                    stopPrice=take_profit_level_1, quantity=quantity * 0.5)
        logging.info(f"Установлен тейк-профит на 50% позиции: {take_profit_level_1} для {symbol}")

        # Перемещение стоп-лосса на 1% выше после частичного закрытия
        updated_stop_loss = round_price(current_price * (1 + 1 / 100), tick_size)
        client.futures_create_order(symbol=symbol, side=stop_order_side, type="STOP_MARKET", stopPrice=updated_stop_loss,
                                    quantity=quantity * 0.5)
        logging.info(f"Обновлен стоп-лосс: {updated_stop_loss} для оставшейся позиции {symbol}")

        # Закрытие оставшейся позиции по частям (25% дважды) при дальнейшем движении цены
        take_profit_level_2 = round_price(current_price * (1 + (TAKE_PROFIT_PERCENT * 2) / 100), tick_size)
        client.futures_create_order(symbol=symbol, side=stop_order_side, type="TAKE_PROFIT_MARKET",
                                    stopPrice=take_profit_level_2, quantity=quantity * 0.25)
        logging.info(f"Установлен тейк-профит на 25% позиции: {take_profit_level_2} для {symbol}")

        take_profit_level_3 = round_price(current_price * (1 + (TAKE_PROFIT_PERCENT * 3) / 100), tick_size)
        client.futures_create_order(symbol=symbol, side=stop_order_side, type="TAKE_PROFIT_MARKET",
                                    stopPrice=take_profit_level_3, quantity=quantity * 0.25)
        logging.info(f"Установлен тейк-профит на оставшиеся 25% позиции: {take_profit_level_3} для {symbol}")

    except Exception as e:
        logging.error(f"Ошибка при открытии позиции {side} для {symbol}: {e}")

# Функция для получения данных по свечам (OHLC)
def get_futures_klines(symbol, limit=100):
    try:
        url = f'https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&limit={limit}&interval=5m'
        response = requests.get(url)
        data = pd.DataFrame(response.json())
        data.columns = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'ignore1', 'ignore2', 'ignore3', 'ignore4', 'ignore5']
        return data[['open_time', 'open', 'high', 'low', 'close', 'volume']].astype(float)
    except Exception as e:
        logging.error(f"Ошибка при получении свечей для {symbol}: {e}")
        return pd.DataFrame()

# Функция для обработки данных с WebSocket
def handle_socket_message(msg):
    try:
        if msg['e'] == 'aggTrade':
            symbol = msg['s']
            price = float(msg['p'])

            if symbol not in previous_prices or previous_prices[symbol] != price:
                logging.info(f"Получена новая цена для {symbol}: {price}")
                previous_prices[symbol] = price

            # Проверка условия для открытия позиции
            if symbol not in TARGET_PRICES or TARGET_PRICES[symbol] is None:
                TARGET_PRICES[symbol] = determine_target_price(symbol)

            target_price = TARGET_PRICES.get(symbol)
            if target_price and price <= target_price:
                logging.info(f"Цена для {symbol} достигла целевого уровня {target_price}. Открытие позиции.")
                open_position(symbol, 'long', MAX_POSITION)
    except KeyError as e:
        logging.error(f"Ошибка при обработке сообщения WebSocket: {e}")
    except Exception as e:
        logging.error(f"Неожиданная ошибка при обработке сообщения WebSocket: {e}")

# Подключение к WebSocket для получения агрегационных данных по символу
def start_symbol_ticker_socket(symbol):
    try:
        twm.start_aggtrade_socket(callback=handle_socket_message, symbol=symbol.lower())
    except Exception as e:
        logging.error(f"Ошибка при подключении WebSocket для {symbol}: {e}")
        reconnect_websocket(symbol)

# Функция переподключения WebSocket
def reconnect_websocket(symbol):
    global twm
    try:
        if twm:
            twm.stop()
        twm = ThreadedWebsocketManager(api_key=KEY, api_secret=SECRET)
        twm.start()
        start_symbol_ticker_socket(symbol)
    except Exception as e:
        logging.error(f"Ошибка при переподключении WebSocket для {symbol}: {e}")

# Основной цикл стратегии
def main():
    input_symbols = input("Введите торговые пары через запятую (например, ETHUSDT,BTCUSDT): ")
    symbols = [symbol.strip() for symbol in input_symbols.split(',')]

    twm.start()  # Запуск WebSocket менеджера один раз

    for symbol in symbols:
        start_symbol_ticker_socket(symbol)

    while time.time() < TIMEOUT:
        try:
            time.sleep(10)  # Период ожидания в основном цикле
        except KeyboardInterrupt:
            logging.info("Скрипт остановлен пользователем.")
            break
        except Exception as e:
            logging.error(f"Ошибка в основном цикле: {e}")

if __name__ == "__main__":
    main()
