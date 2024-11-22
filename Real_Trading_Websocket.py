import time
import requests
import pandas as pd
import logging
import logging.handlers
from binance import Client, ThreadedWebsocketManager
from tenacity import retry, stop_after_attempt, wait_exponential
from cred import KEY, SECRET
import winsound  # Модуль для воспроизведения звуков
import asyncio
import platform

# Настройка логирования с ротацией файлов и кодировкой UTF-8
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
handler = logging.handlers.RotatingFileHandler('trading.log', maxBytes=1_000_000, backupCount=5, encoding='utf-8')
logging.getLogger().addHandler(handler)

# Установка SelectorEventLoop для Windows
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Инициализация клиента Binance
client = Client(KEY, SECRET)

# Инициализация WebSocket менеджера
twm = None

# Переменные для отслеживания состояния WebSocket и попыток переподключения
MAX_RECONNECT_ATTEMPTS = 5
reconnect_attempts = 0


def initialize_websocket_manager():
    global twm
    twm = ThreadedWebsocketManager(api_key=KEY, api_secret=SECRET)
    twm.start()

initialize_websocket_manager()

# Telegram параметры для отправки уведомлений
TELEGRAM_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
TELEGRAM_CHAT_ID = 'YOUR_CHAT_ID'

# Основные параметры стратегии
MAX_POSITION = 0.03
STOP_LOSS_PERCENT = 2
TAKE_PROFIT_PERCENT = 2
TIMEOUT = time.time() + 60 * 60 * 12  # 12 часов работы скрипта
TARGET_PRICE = 2500  # Пример целевой цены для покупки

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
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
        if response.status_code != 200:
            logging.error(f"Ошибка при отправке сообщения в Telegram: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения в Telegram: {e}")

# Функция открытия позиции
def open_position(symbol, side, quantity):
    order_side = "BUY" if side == 'long' else "SELL"
    try:
        response = client.futures_create_order(
            symbol=symbol, side=order_side, type="MARKET", quantity=quantity
        )
        logging.info(f"Открыта позиция {side} для {symbol}: {response}")
        send_telegram_message(f"Открыта позиция {side} для {symbol}")
        play_sound('entry')  # Звуковой сигнал при открытии позиции

        # Установка стоп-лосса и тейк-профита
        tick_size = get_tick_size(symbol)
        current_price = client.get_symbol_ticker(symbol=symbol)['price']
        current_price = float(current_price)
        stop_loss = round_price(current_price * (1 - STOP_LOSS_PERCENT / 100), tick_size)
        take_profit = round_price(current_price * (1 + TAKE_PROFIT_PERCENT / 100), tick_size)
        stop_order_side = "SELL" if side == 'long' else "BUY"

        client.futures_create_order(symbol=symbol, side=stop_order_side, type="STOP_MARKET", stopPrice=stop_loss, quantity=quantity)
        client.futures_create_order(symbol=symbol, side=stop_order_side, type="TAKE_PROFIT_MARKET", stopPrice=take_profit, quantity=quantity)

        logging.info(f"Стоп-лосс: {stop_loss}, Тейк-профит: {take_profit} для {symbol}")
    except Exception as e:
        logging.error(f"Ошибка при открытии позиции {side} для {symbol}: {e}")

# Функция для обработки данных с WebSocket
def handle_socket_message(msg):
    try:
        if msg['e'] == 'aggTrade':
            symbol = msg['s']
            price = float(msg['p'])
            logging.info(f"Получена новая цена для {symbol}: {price}")

            # Проверка условия для открытия позиции
            if price <= TARGET_PRICE:
                logging.info(f"Цена для {symbol} достигла целевого уровня {TARGET_PRICE}. Открытие позиции.")
                open_position(symbol, 'long', MAX_POSITION)
    except KeyError as e:
        logging.error(f"Ошибка при обработке сообщения WebSocket: {e}")
    except Exception as e:
        logging.error(f"Неожиданная ошибка при обработке сообщения WebSocket: {e}")

# Подключение к WebSocket для получения агрегационных данных по символу
def start_symbol_ticker_socket(symbol):
    global reconnect_attempts
    try:
        reconnect_attempts = 0
        twm.start_aggtrade_socket(callback=handle_socket_message, symbol=symbol.lower())
    except Exception as e:
        logging.error(f"Ошибка при подключении WebSocket для {symbol}: {e}")
        send_telegram_message(f"Ошибка при подключении WebSocket для {symbol}: {e}")
        # Переподключение WebSocket после разрыва
        reconnect_websocket(symbol)

# Функция переподключения WebSocket
def reconnect_websocket(symbol):
    global twm, reconnect_attempts
    try:
        reconnect_attempts += 1
        if reconnect_attempts > MAX_RECONNECT_ATTEMPTS:
            logging.error(f"Превышено максимальное количество попыток переподключения для {symbol}")
            send_telegram_message(f"Превышено максимальное количество попыток переподключения для {symbol}")
            return
        if twm:
            twm.stop()
        initialize_websocket_manager()
        start_symbol_ticker_socket(symbol)
    except Exception as e:
        logging.error(f"Ошибка при переподключении WebSocket для {symbol}: {e}")
        send_telegram_message(f"Ошибка при переподключении WebSocket для {symbol}: {e}")

# Основной цикл стратегии
def main():
    input_symbols = input("Введите торговые пары через запятую (например, ETHUSDT,BTCUSDT): ")
    symbols = [symbol.strip() for symbol in input_symbols.split(',')]

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
            send_telegram_message(f"Ошибка в основном цикле: {e}")

# Создайте директорию sounds и добавьте обработку ошибок
def play_sound(sound_type):
    try:
        sound_files = {
            'entry': 'sounds/entry_sound.wav',
            'exit': 'sounds/exit_sound.wav'
        }
        if sound_type in sound_files:
            winsound.PlaySound(sound_files[sound_type], winsound.SND_FILENAME)
    except Exception as e:
        logging.warning(f"Не удалось воспроизвести звук: {e}")

# Замените проверку OCO-ордера на правильный метод API
def check_oco_order_status(order_list_id):
    try:
        # Используйте правильный метод API для проверки OCO
        orders = client.get_open_orders()
        oco_orders = [order for order in orders if order.get('orderListId') == order_list_id]
        return oco_orders
    except Exception as e:
        logging.error(f"Ошибка при проверке статуса ордеров: {e}")
        return None

def place_oco_order(symbol, quantity, entry_price):
    try:
        # Расчет цен для стоп-лосса и тейк-профита
        stop_price = entry_price * (1 - STOP_LOSS_PERCENT / 100)
        take_profit = entry_price * (1 + TAKE_PROFIT_PERCENT / 100)
        
        # Округление цен
        stop_price = round_price(stop_price, get_tick_size(symbol))
        take_profit = round_price(take_profit, get_tick_size(symbol))
        
        # Размещение OCO ордера
        oco_order = client.create_oco_order(
            symbol=symbol,
            side='SELL',
            quantity=quantity,
            price=take_profit,
            stopPrice=stop_price,
            stopLimitPrice=stop_price,
            stopLimitTimeInForce='GTC'
        )
        
        logging.info(f"OCO-ордер для {symbol}: {oco_order}")
        return oco_order
    except Exception as e:
        logging.error(f"Ошибка при размещении OCO-ордера: {e}")
        return None

def monitor_position(symbol, order_list_id):
    try:
        while True:
            # Проверка открытых ордеров
            open_orders = client.get_open_orders(symbol=symbol)
            
            # Проверка баланса
            balance = float(client.get_asset_balance(asset=symbol.replace('USDT', ''))['free'])
            
            if balance == 0 or not open_orders:
                logging.info(f"Позиция по {symbol} закрыта")
                break
                
            time.sleep(1)
    except Exception as e:
        logging.error(f"Ошибка при мониторинге позиции: {e}")

if __name__ == "__main__":
    main()
