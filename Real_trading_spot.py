import time
import pandas as pd
import logging
from binance import Client
from cred import KEY, SECRET
import threading
import pygame
import os

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Инициализация клиента Binance для спотовой торговли
client = Client(KEY, SECRET)

# Инициализация микшера pygame
pygame.mixer.init()

# Получение минимального шага цены (tick size) и количества (lot size) для символа
def get_symbol_info(symbol):
    try:
        exchange_info = client.get_exchange_info()
        for s in exchange_info['symbols']:
            if s['symbol'] == symbol:
                filters = {f['filterType']: f for f in s['filters']}
                tick_size = float(filters['PRICE_FILTER']['tickSize'])
                lot_size = float(filters['LOT_SIZE']['stepSize'])
                return tick_size, lot_size
        logging.error(f"Не удалось найти информацию для символа {symbol}")
        return None, None
    except Exception as e:
        logging.error(f"Ошибка при получении информации для символа {symbol}: {e}")
        return None, None

# Округление цены в соответствии с шагом цены
def round_price_to_tick_size(price, tick_size):
    return round(price / tick_size) * tick_size

# Округление количества в соответствии с шагом количества
def round_quantity_to_lot_size(quantity, lot_size):
    return round(quantity / lot_size) * lot_size

# Получение текущей цены символа
def get_symbol_price(symbol):
    try:
        prices = client.get_symbol_ticker(symbol=symbol)
        return float(prices['price'])
    except Exception as e:
        logging.error(f"Ошибка при получении цены символа {symbol}: {e}")
        return None

# Функция для воспроизведения звука
def play_sound(sound_file):
    try:
        if os.path.exists(sound_file):
            pygame.mixer.music.load(sound_file)
            pygame.mixer.music.play()
        else:
            logging.error(f"Звуковой файл {sound_file} не найден.")
    except Exception as e:
        logging.error(f"Ошибка при воспроизведении звука {sound_file}: {e}")

# Открытие позиции с установкой стоп-лосса и тейк-профита
def open_position_with_sl_tp(symbol, side, quantity, stop_loss_percent=2, take_profit_percent=2):
    try:
        # Получение текущей цены и информации о символе
        current_price = get_symbol_price(symbol)
        tick_size, lot_size = get_symbol_info(symbol)
        if not current_price or not tick_size or not lot_size:
            logging.error(f"Не удалось получить текущую цену или информацию для {symbol}.")
            return

        # Округление количества в соответствии с шагом количества
        quantity = round_quantity_to_lot_size(quantity, lot_size)
        if quantity <= 0:
            logging.error("Количество должно быть больше нуля после округления.")
            return

        # Установка уровней стоп-лосса и тейк-профита с учетом процентов
        if side.lower() == 'buy':
            # Вычисление цен для тейк-профита и стоп-лосса
            take_profit_price = round_price_to_tick_size(current_price * (1 + take_profit_percent / 100), tick_size)
            stop_loss_trigger_price = round_price_to_tick_size(current_price * (1 - stop_loss_percent / 100), tick_size)
            stop_limit_price = round_price_to_tick_size(stop_loss_trigger_price * (1 - 0.001), tick_size)  # На 0.1% ниже триггерной цены

            # Проверка доступного баланса
            account_balance = client.get_asset_balance(asset='USDT')
            available_balance = float(account_balance['free'])
            order_cost = current_price * quantity
            if order_cost > available_balance:
                logging.error("Недостаточно средств для покупки.")
                return

            # Оформление рыночного ордера на покупку
            order_params = {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quantity": f"{quantity:.8f}",
            }
            response = client.create_order(**order_params)
            logging.info(f"Куплен {symbol}: {response}")

            # Воспроизведение звука входа в позицию
            threading.Thread(target=play_sound, args=(r'E:\GOAT_2\sounds\entry_sound.wav',), daemon=True).start()

            # Проверка выполнения ордера
            order_id = response['orderId']
            if not wait_for_order_fill(symbol, order_id):
                logging.warning("Ордер на покупку не был исполнен в течение времени ожидания.")
                return

            # Установка OCO-ордера для стоп-лосса и тейк-профита
            oco_order_params = {
                "symbol": symbol,
                "side": "SELL",
                "quantity": f"{quantity:.8f}",
                "price": f"{take_profit_price:.8f}",           # Цена тейк-профита
                "stopPrice": f"{stop_loss_trigger_price:.8f}",  # Триггерная цена стоп-лосса
                "stopLimitPrice": f"{stop_limit_price:.8f}",    # Цена стоп-лимитного ордера
                "stopLimitTimeInForce": "GTC"
            }
            oco_response = client.create_oco_order(**oco_order_params)
            logging.info(f"OCO-ордер для {symbol}: {oco_response}")

            # Ожидание исполнения OCO-ордера
            wait_for_oco_order_fill(symbol, oco_response['orderListId'])

        else:
            logging.error("В спотовой торговле нельзя открывать короткие позиции без использования маржинальной торговли.")
            return

    except Exception as e:
        logging.error(f"Ошибка при открытии позиции для {symbol}: {e}")

# Функция для ожидания выполнения ордера
def wait_for_order_fill(symbol, order_id, timeout=30):
    """Ожидает, пока ордер будет исполнен, в течение указанного времени (секунд)."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            order_status = client.get_order(symbol=symbol, orderId=order_id)
            if order_status['status'] == 'FILLED':
                logging.info(f"Ордер исполнен: {order_status}")
                return True
            time.sleep(1)
        except Exception as e:
            logging.error(f"Ошибка при проверке статуса ордера {order_id}: {e}")
            return False
    logging.warning(f"Ожидание исполнения ордера {order_id} завершилось неудачно.")
    return False

# Функция для ожидания выполнения OCO-ордера
def wait_for_oco_order_fill(symbol, order_list_id, check_interval=5):
    """Ожидает, пока один из ордеров OCO будет исполнен."""
    try:
        while True:
            oco_order_status = client.get_oco_order(orderListId=order_list_id)
            list_status = oco_order_status['listStatusType']
            if list_status == 'ALL_DONE':
                logging.info(f"OCO-ордер исполнен или отменен: {oco_order_status}")

                # Воспроизведение звука выхода из позиции
                threading.Thread(target=play_sound, args=(r'E:\GOAT_2\sounds\exit_sound.wav',), daemon=True).start()
                break
            time.sleep(check_interval)
    except Exception as e:
        logging.error(f"Ошибка при проверке статуса OCO-ордера {order_list_id}: {e}")

# Пример использования функции
if __name__ == "__main__":
    # Пример вызова функции с параметрами для стоп-лосса и тейк-профита
    open_position_with_sl_tp(symbol='BNBUSDT', side='buy', quantity=0.05, stop_loss_percent=2, take_profit_percent=2)
