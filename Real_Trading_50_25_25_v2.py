import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance.streams import ThreadedWebsocketManager
import os
import asyncio
import platform

# Настройка цикла событий для Windows
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Константы для минимального ордера
MIN_ORDER_QUANTITIES = {
    "BTCUSDT": 0.001,
    "ETHUSDT": 0.01,
}

# Подключение к Binance API
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_SECRET_KEY")

# Если переменные окружения не заданы, пытаемся загрузить их из cred.py
if not api_key or not api_secret:
    try:
        from cred import API_KEY, API_SECRET

        api_key, api_secret = API_KEY, API_SECRET
    except ImportError:
        api_key, api_secret = None, None  # Устанавливаем значения по умолчанию в случае ошибки импорта
        logging.error("API ключи не найдены. Убедитесь, что они заданы корректно.")
        exit(1)

client = Client(api_key, api_secret)


def check_minimum_order(symbol, quantity):
    min_quantity = MIN_ORDER_QUANTITIES.get(symbol, 0.0)
    return quantity >= min_quantity


def get_account_balance():
    try:
        account_info = client.get_account()
        balances = {item['asset']: float(item['free']) for item in account_info['balances'] if float(item['free']) > 0}
        logging.info(f"Доступные активы и их баланс: {balances}")
        return balances
    except BinanceAPIException as e:
        logging.error(f"Ошибка при получении баланса: {e}")
        return {}


def execute_trade(symbol, side, quantity):
    base_asset = symbol[:-4]
    balance = get_account_balance().get(base_asset, 0)
    logging.info(f"Доступный баланс для {base_asset}: {balance}")

    logging.info(f"Попытка выполнить ордер {side.upper()} для {symbol} на количество {quantity}")

    if not check_minimum_order(symbol, quantity):
        logging.error(
            f"Ошибка: размер ордера для {symbol} ниже минимального ({MIN_ORDER_QUANTITIES.get(symbol, 'неизвестно')})")
        return

    if quantity > balance:
        logging.error(
            f"Недостаточный баланс для {side.upper()} ордера {symbol}: требуется {quantity}, доступно {balance}")
        return

    try:
        if side == "buy":
            order = client.order_market_buy(symbol=symbol, quantity=quantity)
        else:
            order = client.order_market_sell(symbol=symbol, quantity=quantity)
        logging.info(f"Ордер выполнен: {order}")
    except BinanceAPIException as e:
        logging.error(f"Ошибка выполнения ордера {side.upper()} для {symbol}: {e}")


def process_message(msg):
    logging.info(f"Получены данные сообщения: {msg}")
    symbol = msg['s']
    close_price = float(msg['p'])
    volume = float(msg['q'])
    logging.info(f"Обработанные данные: символ={symbol}, цена закрытия={close_price}, объем={volume}")

    average_price = (close_price + volume) / 2
    if close_price < average_price:
        signal = "buy"
    else:
        signal = "sell"

    logging.info(f"Сгенерирован сигнал: {signal}")
    execute_trade(symbol, signal, volume)


def start_trading():
    symbol = input("Введите символ актива для торговли (например, 'btcusdt'): ").upper()
    side = input("Введите тип торговли (buy/sell): ").lower()

    get_account_balance()
    logging.info(f"Подключение к WebSocket для символа {symbol}...")

    twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
    twm.start()
    logging.info(f"Торговля началась для символа {symbol} на стороне {side}")

    def message_handler(msg):
        if msg['e'] == 'aggTrade':
            process_message(msg)

    twm.start_symbol_ticker_socket(callback=message_handler, symbol=symbol)

    try:
        while True:
            pass
    except KeyboardInterrupt:
        logging.info("Завершение работы...")
        twm.stop()


if __name__ == "__main__":
    start_trading()
