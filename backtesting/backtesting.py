from data_loader import DataLoader
import os
from dotenv import load_dotenv
import logging
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
from datetime import datetime
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
import numpy as np

# Настройка логирования с более подробным форматом
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()


class BacktestingException(Exception):
    """Пользовательский класс исключений для ошибок бэктестинга."""
    pass


class Backtesting:
    """
    Фреймворк для бэктестинга торговых стратегий на данных криптовалют.

    Атрибуты:
        api_key (str): API ключ для источника данных
        api_secret (str): API секрет для источника данных
        data_loader (DataLoader): Экземпляр класса DataLoader для загрузки данных
    """

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None):
        """
        Инициализация фреймворка для бэктестинга.

        Аргументы:
            api_key: Опциональный API ключ. Если не указан, будет загружен из переменных окружения
            api_secret: Опциональный API секрет. Если не указан, будет загружен из переменных окружения
        """
        self.api_key = api_key or os.getenv("API_KEY")
        self.api_secret = api_secret or os.getenv("API_SECRET")

        if not self.api_key or not self.api_secret:
            raise BacktestingException(
                "API учетные данные не найдены в переменных окружения или аргументах конструктора")

        self.data_loader = DataLoader(self.api_key, self.api_secret)

    def get_data(self, symbol: str, interval: str, start_date: str, end_date: str,
                 from_csv: Optional[str] = None) -> pd.DataFrame:
        """
        Загрузка исторических рыночных данных из CSV или API.

        Аргументы:
            symbol: Символ торговой пары (например, 'BTCUSDT')
            interval: Временной интервал для свечей
            start_date: Дата начала исторических данных
            end_date: Дата окончания исторических данных
            from_csv: Опциональный путь к CSV файлу с историческими данными

        Возвращает:
            pd.DataFrame: Обработанные рыночные данные с индексом datetime

        Вызывает:
            BacktestingException: Если загрузка данных не удалась или данные недействительны
        """
        try:
            logger.info(f"Загрузка данных для {symbol} с {start_date} по {end_date}")

            if from_csv:
                data = self.data_loader.load_from_csv(from_csv)
            else:
                data = self.data_loader.fetch_historical_data(symbol, interval, start_date, end_date)

            if data.empty:
                raise BacktestingException("Не получено данных от источника")

            # Преобразование и проверка данных
            data['timestamp'] = pd.to_datetime(data['timestamp'])
            data.set_index('timestamp', inplace=True)
            data = data.apply(pd.to_numeric, errors='coerce')

            # Проверка на отсутствующие или недействительные значения
            if data.isnull().any().any():
                logger.warning("Найдены пропущенные значения в данных, удаляем затронутые строки")
                data = data.dropna()

            # Проверка наличия OHLCV данных
            required_columns = ['open', 'high', 'low', 'close', 'volume']
            if not all(col in data.columns for col in required_columns):
                raise BacktestingException(f"Отсутствуют необходимые столбцы. Ожидаются: {required_columns}")

            return data

        except Exception as e:
            raise BacktestingException(f"Ошибка загрузки данных: {str(e)}")

    def analyze_and_plot(self, data: pd.DataFrame, strategy: 'Strategy',
                         save_plot: bool = False, filename: str = None) -> None:
        """
        Применение торговой стратегии и создание визуализации.

        Аргументы:
            data: DataFrame с рыночными данными
            strategy: Экземпляр торговой стратегии
            save_plot: Сохранять ли график в файл
            filename: Опциональное имя файла для сохранения графика

        Вызывает:
            BacktestingException: Если анализ или построение графика не удались
        """
        try:
            if data.empty:
                raise BacktestingException("Предоставлен пустой набор данных")

            logger.info(f"Применение стратегии {strategy.__class__.__name__}")
            strategy.apply(data)

            # Создание графика с индикаторами стратегии
            addplots = strategy.get_addplots()

            # Настройка стиля графика
            style = mpf.make_mpf_style(
                base_mpf_style='charles',
                gridstyle='',
                y_on_right=False,
                marketcolors=mpf.make_marketcolors(
                    up='green',
                    down='red',
                    edge='inherit',
                    volume='in'
                )
            )

            # Создание фигуры и построение графика
            fig, axlist = mpf.plot(
                data[['open', 'high', 'low', 'close', 'volume']],
                type='candle',
                style=style,
                addplot=addplots,
                title=f"\nАнализ {strategy.__class__.__name__}",
                ylabel='Цена',
                volume=True,
                warn_too_much_data=10000,
                figsize=(16, 10),
                returnfig=True
            )

            # Настройка внешнего вида графика
            plt.xticks(rotation=45)
            plt.tight_layout()

            if save_plot:
                filename = filename or f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                plt.savefig(filename, dpi=300, bbox_inches='tight')
                logger.info(f"График сохранен как {filename}")

            plt.show()

        except Exception as e:
            raise BacktestingException(f"Ошибка в анализе и построении графика: {str(e)}")


class Strategy(ABC):
    """Абстрактный базовый класс для торговых стратегий."""

    @abstractmethod
    def apply(self, data: pd.DataFrame) -> None:
        """Применить стратегию к данным."""
        pass

    @abstractmethod
    def get_addplots(self) -> List:
        """Получить список графиков для добавления к основному графику."""
        pass


class MovingAverageStrategy(Strategy):
    """Реализация стратегии пересечения скользящих средних."""

    def __init__(self, fast_period: int = 10, slow_period: int = 20):
        """
        Инициализация стратегии MA.

        Аргументы:
            fast_period: Период быстрой скользящей средней
            slow_period: Период медленной скользящей средней
        """
        if fast_period >= slow_period:
            raise ValueError("Быстрый период должен быть меньше медленного периода")

        self.fast_period = fast_period
        self.slow_period = slow_period
        self.addplots = []
        self.signals = pd.DataFrame()

    def apply(self, data: pd.DataFrame) -> None:
        """
        Применить стратегию MA к данным.

        Аргументы:
            data: DataFrame с рыночными данными
        """
        # Расчет скользящих средних
        data['fast_ma'] = data['close'].rolling(window=self.fast_period).mean()
        data['slow_ma'] = data['close'].rolling(window=self.slow_period).mean()

        # Генерация торговых сигналов
        data['signal'] = 0
        data.loc[data['fast_ma'] > data['slow_ma'], 'signal'] = 1  # Сигнал на покупку
        data.loc[data['fast_ma'] < data['slow_ma'], 'signal'] = -1  # Сигнал на продажу

        # Расчет показателей эффективности стратегии
        data['returns'] = data['close'].pct_change()
        data['strategy_returns'] = data['signal'].shift(1) * data['returns']

        self.signals = data

    def get_addplots(self) -> List:
        """Получить графики для скользящих средних и сигналов."""
        self.addplots = [
            mpf.make_addplot(self.signals['fast_ma'], color='blue', width=1,
                             label=f'Быстрая СС ({self.fast_period})'),
            mpf.make_addplot(self.signals['slow_ma'], color='red', width=1,
                             label=f'Медленная СС ({self.slow_period})')
        ]
        return self.addplots

    def get_performance_metrics(self) -> Dict[str, float]:
        """Расчет метрик эффективности стратегии."""
        if self.signals.empty:
            return {}

        metrics = {
            'общая_доходность': (1 + self.signals['strategy_returns']).prod() - 1,
            'годовая_доходность': (1 + self.signals['strategy_returns']).prod() ** (252 / len(self.signals)) - 1,
            'коэффициент_шарпа': np.sqrt(252) * self.signals['strategy_returns'].mean() /
                                 self.signals['strategy_returns'].std(),
            'максимальная_просадка': (self.signals['strategy_returns'].cumsum().cummax() -
                                      self.signals['strategy_returns'].cumsum()).max()
        }
        return metrics


class BollingerBandsStrategy(Strategy):
    """Реализация стратегии полос Боллинджера."""

    def __init__(self, period: int = 20, num_std: float = 2.0):
        """
        Инициализация стратегии полос Боллинджера.

        Аргументы:
            period: Период для скользящей средней
            num_std: Количество стандартных отклонений для полос
        """
        self.period = period
        self.num_std = num_std
        self.addplots = []
        self.signals = pd.DataFrame()

    def apply(self, data: pd.DataFrame) -> None:
        """
        Применить стратегию полос Боллинджера к данным.

        Аргументы:
            data: DataFrame с рыночными данными
        """
        # Расчет полос Боллинджера
        data['ma'] = data['close'].rolling(window=self.period).mean()
        data['std'] = data['close'].rolling(window=self.period).std()
        data['upper_band'] = data['ma'] + (data['std'] * self.num_std)
        data['lower_band'] = data['ma'] - (data['std'] * self.num_std)

        # Генерация торговых сигналов
        data['signal'] = 0
        data.loc[data['close'] > data['upper_band'], 'signal'] = -1  # Сигнал на продажу
        data.loc[data['close'] < data['lower_band'], 'signal'] = 1  # Сигнал на покупку

        # Расчет показателей эффективности стратегии
        data['returns'] = data['close'].pct_change()
        data['strategy_returns'] = data['signal'].shift(1) * data['returns']

        self.signals = data

    def get_addplots(self) -> List:
        """Получить графики для полос Боллинджера."""
        self.addplots = [
            mpf.make_addplot(self.signals['ma'], color='blue', width=1,
                             label=f'СС ({self.period})'),
            mpf.make_addplot(self.signals['upper_band'], color='green', width=1,
                             label='Верхняя полоса'),
            mpf.make_addplot(self.signals['lower_band'], color='red', width=1,
                             label='Нижняя полоса')
        ]
        return self.addplots

    def get_performance_metrics(self) -> Dict[str, float]:
        """Расчет метрик эффективности стратегии."""
        if self.signals.empty:
            return {}

        metrics = {
            'общая_доходность': (1 + self.signals['strategy_returns']).prod() - 1,
            'годовая_доходность': (1 + self.signals['strategy_returns']).prod() ** (252 / len(self.signals)) - 1,
            'коэффициент_шарпа': np.sqrt(252) * self.signals['strategy_returns'].mean() /
                                 self.signals['strategy_returns'].std(),
            'максимальная_просадка': (self.signals['strategy_returns'].cumsum().cummax() -
                                      self.signals['strategy_returns'].cumsum()).max()
        }
        return metrics


if __name__ == "__main__":
    try:
        backtesting = Backtesting()
        symbol = "BNBUSDT"
        interval = "1d"
        start_date = "2024-10-15"
        end_date = "2024-11-07"

        # Загрузка и анализ данных
        data = backtesting.get_data(symbol, interval, start_date, end_date)

        # Инициализация стратегий
        ma_strategy = MovingAverageStrategy(fast_period=10, slow_period=20)
        bb_strategy = BollingerBandsStrategy(period=20, num_std=2.0)

        # Список стратегий для тестирования
        strategies = [ma_strategy, bb_strategy]

        for strategy in strategies:
            # Анализ и построение графика
            backtesting.analyze_and_plot(data.copy(), strategy, save_plot=True)

            # Получение и вывод метрик производительности
            metrics = strategy.get_performance_metrics()
            print(f"\nМетрики производительности для {strategy.__class__.__name__}:")
            for metric, value in metrics.items():
                if metric in ['коэффициент_шарпа']:
                    print(f"{metric}: {value:.2f}")
                else:
                    print(f"{metric}: {value:.2%}")

    except BacktestingException as be:
        logger.error(f"Backtesting error: {be}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
