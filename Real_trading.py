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
import numpy as np
from typing import Dict, Optional
import websockets
import sys
import codecs

# Настройка кодировки для вывода
sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)

def setup_logging():
    # Проверяем, не настроен ли уже логгер
    if logging.getLogger().handlers:
        return
        
    # Создаем форматтер для логов
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Настраиваем вывод в файл
    file_handler = logging.FileHandler('trading.log', encoding='utf-8')
    file_handler.setFormatter(formatter)
    
    # Настраиваем вывод в консоль
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Получаем корневой логгер и настраиваем его
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Очищаем существующие обработчики
    logger.handlers.clear()
    
    # Добавляем наши обработчики
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    # Устанавливаем кодировку консоли для Windows
    if os.name == 'nt':
        os.system('chcp 65001')

# Настройка отдельных уровней для разных компонентов
logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

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
TAKE_PROFIT_PERCENT = 2
TIMEOUT = time.time() + 60 * 60 * 12  # 12 часов работы скрипта
TARGET_PRICES = {}  # Целевые цены будут определяться динамически
previous_prices = {}  # Хранение предыдущих цен для проверки изменений
risk_manager = None  # Будет инициализирован в main()
price_analyzer = None  # Добавляем глобальную переменную

# Класс для управления рисками
class TradingConfig:
    MAX_POSITIONS = 3  # Максимальное количество одновременных позиций
    MAX_DAILY_TRADES = 10  # Максимальное количество сделок в день
    MIN_VOLUME_USD = 1000000  # Минимальный 24h объем торгов в USD
    MAX_SPREAD_PERCENT = 0.1  # Максимальный спред
    LEVERAGE = 3  # Уровень плеча

    # Динамические стопы
    MIN_STOP_LOSS = 0.5  # Минимальнй стоп-лосс в %
    MAX_STOP_LOSS = 5.0  # Максимальный стоп-лосс в %
    ATR_MULTIPLIER = 2.0  # Множитель ATR для стопов

    # Параметры объема позиции
    RISK_PER_TRADE = 0.01  # Риск на сделку (1% от баланса)

    # Добавляем новые параетры
    PARTIAL_CLOSE_LEVELS = [
        {"profit": 1.0, "close_percent": 0.3},
        {"profit": 1.5, "close_percent": 0.3},
        {"profit": 2.0, "close_percent": 0.4}
    ]
    
    @classmethod
    def calculate_position_size(cls, balance: float, entry_price: float, stop_loss_price: float) -> float:
        """Рассчитывает размер позиции на основе риска"""
        price_diff_percent = abs(entry_price - stop_loss_price) / entry_price * 100
        risk_amount = balance * cls.RISK_PER_TRADE
        position_size = (risk_amount / price_diff_percent) * cls.LEVERAGE
        return position_size

class RiskManager:
    def __init__(self, client: Client):
        self.client = client
        self.open_positions: Dict[str, dict] = {}
        self.total_trades = 0
        self.profitable_trades = 0
        self.session_start_balance = float(client.futures_account()['totalWalletBalance'])
        self.price_alerts = {}  # Для отслеживания ценвых уровней
        self.trailing_stops = {}  # Для трейлинг-стопов
        
    def check_entry_conditions(self, symbol: str, price: float) -> bool:
        """Проверяет условия входа в позицию"""
        try:
            # Проверка количества открытых позиций
            if len(self.open_positions) >= TradingConfig.MAX_POSITIONS:
                return False
                
            # Проверка дневного объема
            ticker_24h = self.client.futures_ticker(symbol=symbol)
            if float(ticker_24h['volume']) * float(ticker_24h['lastPrice']) < TradingConfig.MIN_VOLUME_USD:
                return False
                
            # Проверка спреда
            book = self.client.futures_order_book(symbol=symbol)
            spread = (float(book['asks'][0][0]) - float(book['bids'][0][0])) / float(book['bids'][0][0]) * 100
            if spread > TradingConfig.MAX_SPREAD_PERCENT:
                return False
                
            return True
            
        except Exception as e:
            logging.error(f"Ошибка при проверке условий входа: {e}")
            return False
            
    def calculate_dynamic_stops(self, symbol: str, df: pd.DataFrame) -> tuple[float, float]:
        """Рассчитывает динамические стоп-лосс и тейк-профит"""
        try:
            # Расчет ATR
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            atr = true_range.rolling(14).mean().iloc[-1]
            
            current_price = float(df['close'].iloc[-1])
            
            # Динамический стоп-лосс на основе ATR
            stop_loss_distance = atr * TradingConfig.ATR_MULTIPLIER
            stop_loss_percent = (stop_loss_distance / current_price) * 100
            
            # Ограничение стоп-лосса
            stop_loss_percent = max(min(stop_loss_percent, TradingConfig.MAX_STOP_LOSS), 
                                  TradingConfig.MIN_STOP_LOSS)
            
            take_profit_percent = stop_loss_percent * 1.5  # Соотношение риск/прибыль 1:1.5
            
            return stop_loss_percent, take_profit_percent
            
        except Exception as e:
            logging.error(f"Ошибка при расчете динамических стопов: {e}")
            return STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT

    def update_trade_stats(self, pnl: float):
        """Обнвляет статистику торговли"""
        self.total_trades += 1
        if pnl > 0:
            self.profitable_trades += 1

    def add_price_alert(self, symbol: str, price: float, direction: str, message: str):
        """Добавляет ценовой алерт"""
        if symbol not in self.price_alerts:
            self.price_alerts[symbol] = []
        self.price_alerts[symbol].append({
            'price': price,
            'direction': direction,  # 'above' или 'below'
            'message': message,
            'triggered': False
        })
        
    def update_trailing_stop(self, symbol: str, current_price: float):
        """Обновляет трейлинг-стоп для позиции"""
        if symbol in self.open_positions:
            position = self.open_positions[symbol]
            if symbol not in self.trailing_stops:
                # Инициализация трейлинг-стопа
                initial_stop = position['stop_loss']
                self.trailing_stops[symbol] = {
                    'highest_price': current_price,
                    'stop_price': initial_stop,
                    'distance_percent': abs(current_price - initial_stop) / current_price * 100
                }
            else:
                trailing = self.trailing_stops[symbol]
                if position['side'] == 'long':
                    if current_price > trailing['highest_price']:
                        # Обновляем максимальную цену и трейлинг-стоп
                        trailing['highest_price'] = current_price
                        new_stop = current_price * (1 - trailing['distance_percent']/100)
                        if new_stop > trailing['stop_price']:
                            trailing['stop_price'] = new_stop
                            self.update_stop_loss_order(symbol, new_stop)
                            logging.info(f"Трейлнг-стоп обновлен для {symbol}: {new_stop:.4f}")

    def check_price_alerts(self, symbol: str, current_price: float):
        """Проверяет и обрабатывает ценовые алерты"""
        if symbol in self.price_alerts:
            for alert in self.price_alerts[symbol]:
                if not alert['triggered']:
                    if (alert['direction'] == 'above' and current_price > alert['price']) or \
                       (alert['direction'] == 'below' and current_price < alert['price']):
                        logging.info(f"Сработал алерт для {symbol}: {alert['message']}")
                        print(f"\n⚠️ АЛЕРТ {symbol}: {alert['message']}")
                        winsound.Beep(2000, 1000)  # Звуковой сигнал
                        alert['triggered'] = True

    def update_stop_loss_order(self, symbol: str, new_stop_price: float):
        """Обновляет ордер стоп-лосс"""
        try:
            position = self.open_positions[symbol]
            # Отменяем старый стоп-лосс
            open_orders = self.client.futures_get_open_orders(symbol=symbol)
            for order in open_orders:
                if order['type'] == 'STOP_MARKET':
                    self.client.futures_cancel_order(
                        symbol=symbol,
                        orderId=order['orderId']
                    )
            
            # Создаем новый стоп-лосс
            stop_order_side = "SELL" if position['side'] == 'long' else "BUY"
            self.client.futures_create_order(
                symbol=symbol,
                side=stop_order_side,
                type="STOP_MARKET",
                stopPrice=new_stop_price,
                quantity=position['quantity']
            )
            
            # Обновляем информацию о позиции
            self.open_positions[symbol]['stop_loss'] = new_stop_price
            
        except Exception as e:
            logging.error(f"Ошибка при обновлении стоп-лосса для {symbol}: {e}")

    def check_margin_usage(self):
        try:
            account = self.client.futures_account()
            margin_ratio = float(account['totalMarginBalance']) / float(account['totalWalletBalance'])
            return margin_ratio <= 0.8  # Максимум 80% использования маржи
        except Exception as e:
            logging.error(f"Ошибка при проверке маржи: {e}")
            return False

    def set_leverage(self, symbol: str):
        try:
            self.client.futures_change_leverage(
                symbol=symbol,
                leverage=TradingConfig.LEVERAGE
            )
            logging.info(f"Установлено плечо {TradingConfig.LEVERAGE}x для {symbol}")
        except Exception as e:
            logging.error(f"Ошибка при установке плеча для {symbol}: {e}")

    def calculate_liquidation_price(self, position):
        try:
            entry_price = position['entry_price']
            side = position['side']
            leverage = TradingConfig.LEVERAGE
            
            if side == 'long':
                liq_price = entry_price * (1 - (1 / leverage))
            else:
                liq_price = entry_price * (1 + (1 / leverage))
                
            return liq_price
        except Exception as e:
            logging.error(f"Ошибка при расчете ликвидационной цены: {e}")
            return None

    def validate_position_size(self, position_size: float, balance: float) -> float:
        max_size = balance * 0.1  # Максимум 10% от баланса
        return min(position_size, max_size)

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
        logging.error(f"Ошибка при получеии tick size для {symbol}: {e}")
    return None

# Функция округления цены по шагу
def round_price(price, tick_size):
    return round(price / tick_size) * tick_size

# Функция отправки уведомлений в Telegram (удалено)
def send_telegram_message(message):
    pass

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

# Обновляем функцию открытия позиции
def open_position(symbol: str, side: str, risk_manager: RiskManager, price: float):
    try:
        # Проверка маржи
        if not risk_manager.check_margin_usage():
            logging.warning(f"Слишком высокое использование маржи")
            return
            
        # Установка плеча
        risk_manager.set_leverage(symbol)
        
        # Проверка условий входа
        if not risk_manager.check_entry_conditions(symbol, price):
            logging.info(f"Условия входа не выполнены для {symbol}")
            return
            
        # Получаем баланс
        account = client.futures_account()
        balance = float(account['totalWalletBalance'])
        
        # Получаем данные для анализа
        df = get_futures_klines(symbol, limit=100)
        stop_loss_percent, take_profit_percent = risk_manager.calculate_dynamic_stops(symbol, df)
        
        # Используем текущую цену из параметра
        current_price = price
        stop_loss_price = current_price * (1 - stop_loss_percent/100)
        take_profit_price = current_price * (1 + take_profit_percent/100)
        
        # Рассчитываем размер позиции
        quantity = TradingConfig.calculate_position_size(balance, current_price, stop_loss_price)
        
        # Проверк максимального размера
        quantity = risk_manager.validate_position_size(quantity, balance)
        
        # Открываем позицию
        order_side = "BUY" if side == 'long' else "SELL"
        response = client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="MARKET",
            quantity=quantity
        )
        
        logging.info(f"Открыта позиция {side} для {symbol}: {response}")
        winsound.Beep(1000, 500)  # Звуковой сигнал при открытии позиции

        # Установка стоп-лосса и тейк-профита
        tick_size = get_tick_size(symbol)
        stop_loss_price = round_price(stop_loss_price, tick_size)
        take_profit_price = round_price(take_profit_price, tick_size)
        stop_order_side = "SELL" if side == 'long' else "BUY"

        # Размещаем стоп-лосс
        client.futures_create_order(
            symbol=symbol,
            side=stop_order_side,
            type="STOP_MARKET",
            stopPrice=stop_loss_price,
            quantity=quantity
        )

        # Размещаем тейк-профит
        client.futures_create_order(
            symbol=symbol,
            side=stop_order_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=take_profit_price,
            quantity=quantity
        )

        logging.info(f"Установлены ордера для {symbol}: SL={stop_loss_price}, TP={take_profit_price}")
        
        # Добавляем позицию в отслеживаемые
        risk_manager.open_positions[symbol] = {
            'side': side,
            'quantity': quantity,
            'entry_price': current_price,
            'stop_loss': stop_loss_price,
            'take_profit': take_profit_price
        }
        
    except Exception as e:
        logging.error(f"Ошибка при открытии позиции: {e}")

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

class PriceAnalyzer:
    def __init__(self, timeframes=['5m', '15m', '1h']):
        self.timeframes = timeframes
        self.price_history = {}
        self.indicators = {}
        self.volatility = {}
        
    def update_price_data(self, symbol: str, price: float, timestamp: int):
        if symbol not in self.price_history:
            self.price_history[symbol] = []
        
        self.price_history[symbol].append({
            'price': price,
            'timestamp': timestamp
        })
        
        # Осавляем только последние 1000 точек
        if len(self.price_history[symbol]) > 1000:
            self.price_history[symbol] = self.price_history[symbol][-1000:]
            
        # Обновляем волатильность
        self._update_volatility(symbol)
        
    def _update_volatility(self, symbol: str):
        """Рассчитывает текущую волатильност"""
        if len(self.price_history[symbol]) < 20:
            return
            
        prices = [p['price'] for p in self.price_history[symbol][-20:]]
        returns = np.diff(np.log(prices))
        volatility = np.std(returns) * np.sqrt(20) * 100  # Годовая волатильность в процентах
        self.volatility[symbol] = volatility
        
        # Если волатильность высокая, создаем алерт
        if volatility > 100:  # Порог в 100% годовых
            risk_manager.add_price_alert(
                symbol=symbol,
                price=prices[-1],
                direction='both',
                message=f"Высокая волатильность: {volatility:.1f}%"
            )

# Обновляем функцию handle_socket_message
def handle_socket_message(msg):
    try:
        if msg['e'] == 'aggTrade':
            symbol = msg['s']
            price = float(msg['p'])
            timestamp = msg['E']
            
            if symbol not in previous_prices or previous_prices[symbol] != price:
                logging.info(f"Получена новая цена для {symbol}: {price}")
                previous_prices[symbol] = price
                
                # Обновляем данные анализатора
                price_analyzer.update_price_data(symbol, price, timestamp)
                
                # Обновляем трейлинг-стопы и проверяем алерты
                if risk_manager:
                    risk_manager.update_trailing_stop(symbol, price)
                    risk_manager.check_price_alerts(symbol, price)
                    
                    # Проверяем условия для частичного закрытия
                    if symbol in risk_manager.open_positions:
                        position = risk_manager.open_positions[symbol]
                        entry_price = position['entry_price']
                        pnl_percent = ((price - entry_price) / entry_price * 100 * 
                                     (1 if position['side'] == 'long' else -1))
                        
                        for level in TradingConfig.PARTIAL_CLOSE_LEVELS:
                            if pnl_percent >= level['profit']:
                                close_amount = position['quantity'] * level['close_percent']
                                risk_manager.partial_close_position(symbol, close_amount)
                                break
            
            if symbol not in TARGET_PRICES or TARGET_PRICES[symbol] is None:
                TARGET_PRICES[symbol] = determine_target_price(symbol)
            
            target_price = TARGET_PRICES.get(symbol)
            if target_price and price <= target_price:
                logging.info(f"Цена для {symbol} достигла целевого уровня {target_price}")
                
                # Проверяем волатильность перед входом
                if symbol in price_analyzer.volatility:
                    volatility = price_analyzer.volatility[symbol]
                    if volatility > 100:  # лишком высокая волатильность
                        logging.warning(f"Вход отменен из-за высоой волатильности ({volatility:.1f}%)")
                        return
                
                open_position(symbol, 'long', risk_manager, price)
                
            # Проверка ликвидационной цены для открытых позиций
            if symbol in risk_manager.open_positions:
                position = risk_manager.open_positions[symbol]
                liq_price = risk_manager.calculate_liquidation_price(position)
                
                # Если цена приближается к ликвидационной (например, на 20%)
                if abs(price - liq_price) / price < 0.2:
                    logging.warning(f"Цена близка к ликвидационной для {symbol}")
                    # Можно добавить логику закрытия позиции
                
    except Exception as e:
        logging.error(f"Ошибка при обработке сообщения WebSocket: {e}")

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
        logging.error(f"Ошибка при переодключении WebSocket для {symbol}: {e}")

# В начале файла после импортов добавим проверку подключения к API
def check_connection():
    try:
        client.ping()
        logging.info("Успешное подключение к Binance API")
        return True
    except Exception as e:
        logging.error(f"Ошибка подключения к Binance API: {e}")
        return False

def get_available_symbols():
    """Получает список доступных фьючерсных пар"""
    try:
        exchange_info = client.futures_exchange_info()
        symbols = [info['symbol'] for info in exchange_info['symbols'] if info['status'] == 'TRADING']
        return symbols
    except Exception as e:
        logging.error(f"Ошибка при получении списка символов: {e}")
        return []

def show_popular_pairs():
    """Показывает популярные торговые пары"""
    popular_pairs = [
        'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT', 'DOGEUSDT',
        'XRPUSDT', 'DOTUSDT', 'LINKUSDT', 'SOLUSDT', 'MATICUSDT'
    ]
    print("\nПопулярные торговые пары:")
    print(", ".join(popular_pairs))

def show_trading_status():
    """Показывает текущий статус торговли"""
    try:
        account = client.futures_account()
        balance = float(account['totalWalletBalance'])
        pnl = float(account['totalUnrealizedProfit'])
        available_balance = float(account['availableBalance'])
        margin_ratio = float(account.get('totalMarginBalance', 0)) / balance * 100 if balance > 0 else 0
        
        print("\nСтатус торговли:")
        print(f"Общий баланс: {balance:.2f} USDT")
        print(f"Доступный баланс: {available_balance:.2f} USDT")
        print(f"Нереализованная прибыль/убыток: {pnl:.2f} USDT ({(pnl/balance*100 if balance > 0 else 0):.2f}%)")
        print(f"Использование маржи: {margin_ratio:.1f}%")
        print(f"Открытые позиции: {len(risk_manager.open_positions)}/{TradingConfig.MAX_POSITIONS}")
        
        if risk_manager.open_positions:
            print("\nАктивные позиции:")
            for symbol, position in risk_manager.open_positions.items():
                current_price = float(client.futures_symbol_ticker(symbol=symbol)['price'])
                entry_price = position['entry_price']
                pnl_percent = ((current_price - entry_price) / entry_price * 100 * 
                             (1 if position['side'] == 'long' else -1))
                
                # Добавляем информацию о стопах и расстоянии до них
                sl_distance = abs(current_price - position['stop_loss']) / current_price * 100
                tp_distance = abs(position['take_profit'] - current_price) / current_price * 100
                
                print(f"{symbol}: {position['side'].upper()}")
                print(f"  Вход: {entry_price:.4f}, Текущая: {current_price:.4f}")
                print(f"  P&L: {pnl_percent:.2f}%")
                print(f"  SL: {position['stop_loss']:.4f} ({sl_distance:.1f}% от цены)")
                print(f"  TP: {position['take_profit']:.4f} ({tp_distance:.1f}% до цели)")
                
                # Добавляем предупреждения
                if sl_distance < 0.5:  # Если цена близко к стоп-лоссу
                    print("  ⚠️ Внимание: Цена близка к стоп-лоссу!")
                if pnl_percent > 1.0:  # Если есть прибыль больше 1%
                    print("  💰 Рекомендуется частичное закрытие позиции")
        
        # Добавляем статистику торговли
        print("\nСтатистика торговли за сессию:")
        print(f"Всего сделок: {risk_manager.total_trades}")
        print(f"Прибыльных: {risk_manager.profitable_trades}")
        print(f"Убыточных: {risk_manager.total_trades - risk_manager.profitable_trades}")
        win_rate = (risk_manager.profitable_trades / risk_manager.total_trades * 100 
                   if risk_manager.total_trades > 0 else 0)
        print(f"Винрейт: {win_rate:.1f}%")
        
    except Exception as e:
        logging.error(f"Ошибка при получении статуса торговли: {e}")
        print("Не удалось олучить статус торговли")

def show_menu():
    """Показывает главное меню"""
    print("\nДоступные команды:")
    print("list     - показать все доступные пары")
    print("popular  - показать популярные пары")
    print("status   - показать текущий статус торговли")
    print("help     - показать эту справку")
    print("exit     - выйти из программы")

# Обновляем main() для поддержки новых команд
def main():
    global risk_manager, price_analyzer
    setup_logging()
    logging.info("Успешное подключение к Binance API")
    
    # Инициализация риск-менеджера и анализатора цен
    risk_manager = RiskManager(client)
    price_analyzer = PriceAnalyzer()
    
    print("\nДобро пожаловать в торговый бот!")
    
    # Запускаем WebSocket менеджер
    twm.start()
    
    while True:
        try:
            print("\nПопулярные торговые пары:")
            print("BTCUSDT, ETHUSDT, BNBUSDT, ADAUSDT, DOGEUSDT, XRPUSDT, DOTUSDT, LINKUSDT, SOLUSDT, MATICUSDT")
            
            print("\nДоступные команды:")
            print("list     - показать все доступные пары")
            print("popular  - показать популярные пары")
            print("status   - показать текущий статус торговли")
            print("help     - показать эту справку")
            print("exit     - выйти из программы")
            
            user_input = input("\nВведите торговые пары через запятую или команду: ").strip().upper()
            
            if user_input == 'EXIT':
                print("Завершение работы бота...")
                break
            elif user_input == 'HELP':
                continue
            elif user_input == 'LIST':
                symbols = get_available_symbols()
                print("\nДоступные пары:")
                print(", ".join(symbols))
            elif user_input == 'POPULAR':
                show_popular_pairs()
            elif user_input == 'STATUS':
                show_trading_status()
            else:
                # Обработка введенных пар
                pairs = [pair.strip() for pair in user_input.split(',')]
                for symbol in pairs:
                    if symbol in get_available_symbols():
                        print(f"\nПодключение к {symbol}...")
                        start_symbol_ticker_socket(symbol)
                        # Определяем целевую цену
                        TARGET_PRICES[symbol] = determine_target_price(symbol)
                        if TARGET_PRICES[symbol]:
                            print(f"Установлена целевая цена для {symbol}: {TARGET_PRICES[symbol]}")
                    else:
                        print(f"Неверный символ: {symbol}")
                
        except KeyboardInterrupt:
            print("\nПрограмма остановлена пользователем")
            break
        except Exception as e:
            logging.error(f"Произошла ошибка: {e}")
            print("Произошла ошибка. Проверьте логи для деталей.")
    
    # Закрываем WebSocket соединения при выходе
    twm.stop()

if __name__ == "__main__":
    main()

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,  # Изменено с DEBUG на INFO
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Для HTTP заросов можно установить более высокий уровень
logging.getLogger('urllib3').setLevel(logging.WARNING)

class BinanceWebsocketClient:
    def __init__(self, ws_url="wss://stream.binance.com:9443/ws"):
        self.ws = None
        self.ping_interval = 20  # секунды
        self.ping_timeout = 10   # секунды
        self.ws_url = ws_url
        
    async def connect(self):
        try:
            self.ws = await websockets.connect(self.ws_url)
            asyncio.create_task(self._keepalive())
        except Exception as e:
            logger.error(f"Connection error: {e}")
            raise
            
    async def _keepalive(self):
        while True:
            try:
                await asyncio.sleep(self.ping_interval)
                pong_waiter = await self.ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=self.ping_timeout)
            except asyncio.TimeoutError:
                await self.reconnect()
                break
            except Exception as e:
                logger.error(f"Keepalive error: {e}")
                await self.reconnect()
                break
                
    async def reconnect(self):
        try:
            await self.ws.close()
            await self.connect()
        except Exception as e:
            logger.error(f"Reconnection error: {e}")
            raise

class BinanceTrader:
    def __init__(self):
        self.last_margin_check = 0
        self.margin_check_interval = 60  # увеличиваем до 60 секунд
        self.price_alert_sent = False
        self.last_margin_warning = 0
        self.margin_warning_interval = 300  # предупреждение раз в 5 минут
        self.last_price_log = {}  # словарь для хранения последних цен по символам
        self.price_log_threshold = 0.1  # порог изменения цены для логирования (0.1%)
        
    async def check_price_target(self, symbol: str, price: float):
        if symbol not in TARGET_PRICES:
            return
            
        target_price = TARGET_PRICES[symbol]
        
        # Проверяем, действительно ли цена достигла цели
        if price >= target_price:
            if not self.price_alert_sent:
                logger.info(f"🎯 Цена для {symbol} достигла целевого уровня {target_price:.4f}")
                self.price_alert_sent = True
                # Здесь ваша торговая логика
        else:
            # Сбрасываем флаг только если цена упала ниже цели на 0.1%
            if price < target_price * 0.999:
                self.price_alert_sent = False
            
    async def check_margin(self):
        current_time = time.time()
        
        # Проверяем интервал для маржи
        if current_time - self.last_margin_check < self.margin_check_interval:
            return
            
        try:
            account_info = await self.client.get_account()
            margin_ratio = self.calculate_margin_ratio(account_info)
            
            if margin_ratio > self.max_margin_ratio:
                # Проверяем интервал для предупреждений
                if current_time - self.last_margin_warning >= self.margin_warning_interval:
                    logger.warning(f"⚠️ Высокое использование маржи: {margin_ratio:.2f}%")
                    self.last_margin_warning = current_time
                    
            self.last_margin_check = current_time
            
        except Exception as e:
            logger.error(f"Ошибка при проверке маржи: {e}")
            
    async def process_price_update(self, symbol: str, price: float):
        # Логируем цену только при значительном изменении
        if symbol not in self.last_price_log:
            self.last_price_log[symbol] = price
            logger.info(f"📊 Получена начальная цена для {symbol}: {price:.4f}")
        else:
            price_change = abs(price - self.last_price_log[symbol]) / self.last_price_log[symbol] * 100
            if price_change >= self.price_log_threshold:
                logger.info(f"📊 Новая цена для {symbol}: {price:.4f} ({price_change:+.2f}%)")
                self.last_price_log[symbol] = price
            
        await self.check_price_target(symbol, price)
