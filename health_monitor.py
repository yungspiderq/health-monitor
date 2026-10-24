"""
HealthMonitor — диагностика и сторож (плагин для FunPay Cardinal v0.1.17+)

❤️ Бот на VPS работает 24/7, и самый обидный сценарий — он «тихо умер»:
протух golden key, отвалился прокси, завис поток запросов, кончилось место
на диске. Плагин показывает состояние бота одной командой и сам бьёт тревогу,
когда что-то идёт не так.

Возможности:
 • Аптайм, аккаунт и ЖИВАЯ проверка сессии FunPay.
 • «Пульс» Runner: когда было последнее событие, отдельно сообщения и заказы.
 • Лоты в профиле, авто-лоты, включенные модули Кардинала, время следующего
   поднятия лотов, число плагинов.
 • ОЗУ процесса, CPU, ОЗУ системы, свободное место на диске, число потоков.
 • Сторож: критические уведомления (не глушатся настройками чата) при отвале
   сессии, молчании Runner, нехватке памяти или места на диске.
 • Повторные тревоги одного типа подавляются на время cooldown.
 • Опциональный heartbeat «бот жив» раз в N часов.

Команды Telegram:
 • /health — полный отчет (с живым запросом к FunPay).
 • /health fast — то же самое без запроса к FunPay.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Настройки: storage/plugins/health_monitor.cfg (перечитываются на лету).
Автор: yungspider
"""

from __future__ import annotations

import configparser
import datetime
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

import FunPayAPI
from telebot.types import Message
from tg_bot import utils as tg_utils
from locales.localizer import Localizer

try:
    import psutil
except ImportError:  # psutil идет в requirements.txt, но на всякий случай не падаем
    psutil = None

logger = logging.getLogger("FPC.HealthMonitor")
localizer = Localizer()
_ = localizer.translate

# ═══════════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ═══════════════════════════════════
NAME = "HealthMonitor (Diagnostics & Watchdog)"
VERSION = "1.1.0"
DESCRIPTION = ("Диагностика и сторож бота: аптайм, живость сессии FunPay, давность событий Runner, лоты, " \
               "включенные модули, ОЗУ/CPU/диск. Критические уведомления при отвале сессии, зависании Runner и " \
               "нехватке ресурсов. Настройки: storage/plugins/health_monitor.cfg. Команда: /health")
CREDITS = "yungspider"
UUID = "2d68fc4e-a3e0-48d5-ba7c-845688310766"
SETTINGS_PAGE = False

# ═══════════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════════
CONFIG_FILE = "storage/plugins/health_monitor.cfg"

DEFAULT_CONFIG_TEXT = """[HealthMonitor]
; Включен ли сторож (1 / 0). Команда /health работает независимо от этого параметра.
enabled = 1
; Интервал проверки сессии FunPay (минуты).
sessionCheckInterval = 10
; Сколько подряд неудачных проверок сессии считать проблемой.
sessionFailuresToAlert = 3
; Через сколько минут без единого события Runner бить тревогу (0 = не проверять).
; На «тихих» аккаунтах событий может не быть часами - ставьте значение с запасом.
noEventsAlert = 360
; Макс. потребление ОЗУ процессом бота в МБ (0 = не проверять).
maxMemoryMb = 800
; Мин. свободное место на диске в МБ (0 = не проверять).
minDiskFreeMb = 500
; Пауза между повторными одинаковыми тревогами (минуты).
alertCooldown = 60
; Интервал heartbeat-сообщения «бот жив» в часах (0 = выключено).
heartbeatInterval = 0
"""

_lock = threading.RLock()
_cfg: dict = {}
_cfg_mtime: float = -1.0
_cardinal: Cardinal | None = None
_thread_started = False

_state = {
    "started": time.time(),
    "last_event": time.time(),
    "last_message_event": 0.0,
    "last_order_event": 0.0,
    "events_count": 0,
    "session_ok": None,
    "session_failures": 0,
    "last_session_check": 0.0,
    "last_alerts": {},
    "last_heartbeat": 0.0,
}

MESSAGE_EVENTS = (
    FunPayAPI.events.EventTypes.INITIAL_CHAT,
    FunPayAPI.events.EventTypes.CHATS_LIST_CHANGED,
    FunPayAPI.events.EventTypes.LAST_CHAT_MESSAGE_CHANGED,
    FunPayAPI.events.EventTypes.NEW_MESSAGE,
)
ORDER_EVENTS = (
    FunPayAPI.events.EventTypes.INITIAL_ORDER,
    FunPayAPI.events.EventTypes.ORDERS_LIST_CHANGED,
    FunPayAPI.events.EventTypes.NEW_ORDER,
    FunPayAPI.events.EventTypes.ORDER_STATUS_CHANGED,
)


# ═════════════════════════════════════════ КОНФИГ ═════════════════════════════════════════
def load_config() -> dict:
    """Читает настройки плагина (создает файл при отсутствии, перечитывает при изменении)."""
    global _cfg, _cfg_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0.0
        except OSError:
            mtime = 0.0
        if _cfg and mtime == _cfg_mtime:
            return _cfg

        if not os.path.exists(CONFIG_FILE):
            try:
                os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    f.write(DEFAULT_CONFIG_TEXT)
                mtime = os.path.getmtime(CONFIG_FILE)
                logger.info(f"[HealthMonitor] Создан файл настроек $YELLOW{CONFIG_FILE}$RESET")
            except Exception:
                logger.error("[HealthMonitor] Не удалось создать файл настроек.")
                logger.debug("TRACEBACK", exc_info=True)

        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(CONFIG_FILE, encoding="utf-8")
        except Exception:
            logger.error("[HealthMonitor] Не удалось прочитать конфиг, использую значения по умолчанию.")
            logger.debug("TRACEBACK", exc_info=True)
            parser = configparser.ConfigParser(interpolation=None)
        if "HealthMonitor" not in parser:
            parser.read_string(DEFAULT_CONFIG_TEXT)
        section = parser["HealthMonitor"]

        def get_bool(name: str, default: bool) -> bool:
            try:
                return section.getboolean(name, fallback=default)
            except Exception:
                return default

        def get_int(name: str, default: int) -> int:
            try:
                return int(section.get(name, str(default)))
            except (ValueError, TypeError):
                return default

        _cfg = {
            "enabled": get_bool("enabled", True),
            "session_interval": max(1, get_int("sessionCheckInterval", 10)) * 60,
            "session_failures": max(1, get_int("sessionFailuresToAlert", 3)),
            "no_events_alert": max(0, get_int("noEventsAlert", 360)) * 60,
            "max_memory_mb": max(0, get_int("maxMemoryMb", 800)),
            "min_disk_free_mb": max(0, get_int("minDiskFreeMb", 500)),
            "alert_cooldown": max(1, get_int("alertCooldown", 60)) * 60,
            "heartbeat_interval": max(0, get_int("heartbeatInterval", 0)) * 3600,
        }
        _cfg_mtime = mtime
        return _cfg


# ════════════════════════════════════ ВСПОМОГАТЕЛЬНОЕ ════════════════════════════════════
def human_time(seconds: float) -> str:
    """Форматирует кол-во секунд в человекочитаемый вид («2 дн. 3 ч. 15 мин.»)."""
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes or days or hours:
        parts.append(f"{minutes} мин.")
    if not parts:
        parts.append(f"{secs} сек.")
    return " ".join(parts)


def ago(timestamp: float) -> str:
    """Возвращает текст «N назад» для метки времени (0 = «событий не было»)."""
    if not timestamp:
        return "не было"
    return f"{human_time(time.time() - timestamp)} назад"


def check_mark(ok: bool | None) -> str:
    """Возвращает эмодзи-статус."""
    if ok is None:
        return "❔"
    return "✅" if ok else "❌"


def collect_system_info() -> list[str]:
    """Собирает информацию о процессах/памяти/диске (если доступен psutil)."""
    lines = []
    if psutil is None:
        return ["💻 <i>psutil не установлен - системная информация недоступна.</i>"]
    try:
        process = psutil.Process(os.getpid())
        rss_mb = process.memory_info().rss / 1024 / 1024
        cpu = process.cpu_percent(interval=None)
        virtual = psutil.virtual_memory()
        lines.append(f"🧠 ОЗУ процесса: <b>{rss_mb:.0f} МБ</b>  |  CPU: <b>{cpu:.0f}%</b>  |  "
                     f"ОЗУ системы: <b>{virtual.percent:.0f}%</b>")
        disk = psutil.disk_usage(os.path.abspath(os.path.dirname(CONFIG_FILE)) or ".")
        lines.append(f"💾 Диск: свободно <b>{disk.free / 1024 / 1024 / 1024:.2f} ГБ</b> "
                     f"из {disk.total / 1024 / 1024 / 1024:.1f} ГБ")
        lines.append(f"🧵 Потоков: <b>{threading.active_count()}</b>")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        lines.append("💻 <i>Не удалось собрать системную информацию.</i>")
    return lines


def collect_session_info(c: Cardinal, live_check: bool = False) -> tuple[str, bool | None]:
    """
    Собирает информацию о сессии FunPay.

    :param c: объект кардинала.
    :param live_check: выполнять ли живой запрос к FunPay для проверки сессии.

    :return: (строка статуса, валидна ли сессия или None если не проверяли).
    """
    account = c.account
    session_ok = None
    if live_check:
        try:
            result = account.get()
            session_ok = bool(result)
        except Exception:
            session_ok = False
            logger.debug("TRACEBACK", exc_info=True)
    else:
        try:
            session_ok = bool(account.is_initiated())
        except Exception:
            session_ok = None

    username = tg_utils.escape(str(getattr(account, "username", "?") or "?"))
    account_id = getattr(account, "id", "?")
    active_sales = getattr(account, "active_sales", "?")
    text = (f"🔑 Аккаунт: <a href=\"https://funpay.com/users/{account_id}/\">{username}</a> "
            f"(ID <code>{account_id}</code>)\n"
            f"🔐 Сессия FunPay: {check_mark(session_ok)}"
            f"{' валидна' if session_ok else ' НЕ отвечает' if session_ok is False else ' не проверялась'}\n"
            f"📦 Незавершенных продаж: <b>{active_sales}</b>")
    return text, session_ok


def build_health_text(c: Cardinal, live_check: bool = True) -> str:
    """Собирает полный отчет о состоянии бота."""
    cfg = load_config()
    start_time = int(getattr(c, "start_time", _state["started"]) or _state["started"])
    lines = [
        "❤️ <b>Состояние FunPay Cardinal</b>",
        "",
        f"⏱ Аптайм: <b>{human_time(time.time() - start_time)}</b> "
        f"(старт: {datetime.datetime.fromtimestamp(start_time).strftime('%d.%m.%Y %H:%M:%S')})",
    ]

    try:
        session_text, session_ok = collect_session_info(c, live_check=live_check)
        lines.append(session_text)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        lines.append("🔑 <i>Не удалось получить информацию об аккаунте.</i>")

    lines.append(f"📡 Событий с запуска плагина: <b>{_state['events_count']}</b>")
    lines.append(f"   • последнее: {ago(_state['last_event'])}")
    lines.append(f"   • сообщения: {ago(_state['last_message_event'])}  |  "
                 f"заказы: {ago(_state['last_order_event'])}")

    try:
        profile = c.profile
        lots = profile.get_lots() if profile else []
        auto_lots = [i for i in lots if getattr(i, "auto", False)]
        lines.append(f"🛍 Лотов в профиле: <b>{len(lots)}</b> (авто-лоты: {len(auto_lots)})")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        lines.append("🛍 <i>Профиль не загружен.</i>")

    try:
        lines.append(f"⚙️ Поднятие лотов: {check_mark(c.autoraise_enabled)}  |  "
                     f"Автоответчик: {check_mark(c.autoresponse_enabled)}  |  "
                     f"Автовыдача: {check_mark(c.autodelivery_enabled)}")
        lines.append(f"⚙️ Автовосстановление: {check_mark(c.autorestore_enabled)}  |  "
                     f"Автодеактивация: {check_mark(c.autodisable_enabled)}  |  "
                     f"Telegram: {check_mark(c.telegram is not None)}")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)

    try:
        raise_time = getattr(c, "raise_time", None)
        if c.autoraise_enabled and raise_time:
            next_raise = min(int(i) for i in raise_time.values() if i)
            lines.append(f"⤴️ След. поднятие лотов: через <b>{human_time(next_raise - time.time())}</b>")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)

    try:
        plugins = getattr(c, "plugins", {})
        enabled = len([i for i in plugins.values() if getattr(i, "enabled", False)])
        lines.append(f"🧩 Плагины: <b>{enabled}/{len(plugins)}</b> включено")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)

    lines.extend(collect_system_info())
    if not cfg["enabled"]:
        lines.append("")
        lines.append("⚠️ <i>Сторож плагина выключен в конфиге (health_monitor.cfg).</i>")
    return "\n".join(lines)


# ═════════════════════════════════════════ СТОРОЖ ═════════════════════════════════════════
def send_alert(c: Cardinal, key: str, text: str) -> bool:
    """
    Отправляет критическое уведомление в Telegram, соблюдая паузу между повторами.

    :param c: объект кардинала.
    :param key: идентификатор типа тревоги (для cooldown).
    :param text: текст уведомления.

    :return: True, если уведомление отправлено.
    """
    if not c.telegram:
        return False
    cfg = load_config()
    with _lock:
        last = _state["last_alerts"].get(key, 0.0)
        if time.time() - last < cfg["alert_cooldown"]:
            return False
        _state["last_alerts"][key] = time.time()
    try:
        c.telegram.send_notification(text, notification_type=tg_utils.NotificationTypes.critical)
        logger.warning(f"[HealthMonitor] Отправлена тревога: $YELLOW{key}$RESET")
        return True
    except Exception:
        logger.error("[HealthMonitor] Не удалось отправить тревогу в Telegram.")
        logger.debug("TRACEBACK", exc_info=True)
        return False


def check_session(c: Cardinal) -> None:
    """Проверяет живость сессии FunPay живым запросом."""
    cfg = load_config()
    try:
        account_ok = bool(c.account.get())
    except Exception:
        account_ok = False
        logger.debug("TRACEBACK", exc_info=True)

    with _lock:
        _state["last_session_check"] = time.time()
        _state["session_ok"] = account_ok
        if account_ok:
            _state["session_failures"] = 0
        else:
            _state["session_failures"] += 1
        failures = _state["session_failures"]

    if not account_ok and failures >= cfg["session_failures"]:
        send_alert(c, "session",
                   f"🚨 <b>Сессия FunPay не отвечает!</b>\n"
                   f"Подряд неудачных проверок: <b>{failures}</b>.\n"
                   f"Возможные причины: протух golden key, блокировка аккаунта, проблемы с сетью или прокси.\n"
                   f"Проверьте бота: /health")
    elif account_ok and failures == 0 and _state.get("was_broken"):
        send_alert(c, "session_restored", "✅ <b>Сессия FunPay снова отвечает.</b>")
    with _lock:
        _state["was_broken"] = not account_ok


def check_events(c: Cardinal) -> None:
    """Проверяет, давно ли были события от Runner."""
    cfg = load_config()
    if not cfg["no_events_alert"]:
        return
    silence = time.time() - _state["last_event"]
    if silence >= cfg["no_events_alert"]:
        send_alert(c, "no_events",
                   f"⚠️ <b>Runner молчит уже {human_time(silence)}.</b>\n"
                   f"От FunPay давно не было ни одного события. Возможные причины: завис поток запросов, "
                   f"проблемы с сетью/прокси или очень «тихий» аккаунт.\n"
                   f"Подробности: /health")


def check_resources(c: Cardinal) -> None:
    """Проверяет потребление ОЗУ и свободное место на диске."""
    cfg = load_config()
    if psutil is None or (not cfg["max_memory_mb"] and not cfg["min_disk_free_mb"]):
        return
    try:
        if cfg["max_memory_mb"]:
            rss_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            if rss_mb > cfg["max_memory_mb"]:
                send_alert(c, "memory",
                           f"⚠️ <b>Высокое потребление ОЗУ: {rss_mb:.0f} МБ</b> "
                           f"(порог: {cfg['max_memory_mb']} МБ).\n"
                           f"Возможна утечка памяти - рассмотрите перезапуск бота (/restart).")
        if cfg["min_disk_free_mb"]:
            free_mb = psutil.disk_usage(os.path.abspath(".")).free / 1024 / 1024
            if free_mb < cfg["min_disk_free_mb"]:
                send_alert(c, "disk",
                           f"⚠️ <b>Заканчивается место на диске: {free_mb:.0f} МБ свободно</b> "
                           f"(порог: {cfg['min_disk_free_mb']} МБ).\n"
                           f"Логи и данные плагина могут перестать записываться.")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


def send_heartbeat(c: Cardinal) -> None:
    """Отправляет периодическое сообщение «бот жив» со сводкой."""
    cfg = load_config()
    if not cfg["heartbeat_interval"] or not c.telegram:
        return
    with _lock:
        if time.time() - _state["last_heartbeat"] < cfg["heartbeat_interval"]:
            return
        _state["last_heartbeat"] = time.time()
    try:
        text = build_health_text(c, live_check=False)
        c.telegram.send_notification(f"💓 <b>Heartbeat</b>\n\n{text}",
                                     notification_type=tg_utils.NotificationTypes.other)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


def watchdog_loop(c: Cardinal, run_id: int) -> None:
    """Фоновый поток сторожа."""
    logger.info("[HealthMonitor] Сторож запущен.")
    while True:
        if c.run_id != run_id:
            logger.info("[HealthMonitor] Сторож остановлен.")
            return
        try:
            cfg = load_config()
            if cfg["enabled"]:
                if time.time() - _state["last_session_check"] >= cfg["session_interval"]:
                    check_session(c)
                check_events(c)
                check_resources(c)
                send_heartbeat(c)
        except Exception:
            logger.error("[HealthMonitor] Ошибка в потоке сторожа.")
            logger.debug("TRACEBACK", exc_info=True)
        for _ in range(30):
            if c.run_id != run_id:
                return
            time.sleep(1)


# ════════════════════════════════════ ХЭНДЛЕРЫ СОБЫТИЙ ════════════════════════════════════
def on_event(c: Cardinal, *args) -> None:
    """Универсальный хэндлер: отмечает время последнего события Runner."""
    event = args[0] if args else None
    event_type = getattr(event, "type", None)
    now = time.time()
    with _lock:
        _state["last_event"] = now
        _state["events_count"] += 1
        if event_type in MESSAGE_EVENTS:
            _state["last_message_event"] = now
        elif event_type in ORDER_EVENTS:
            _state["last_order_event"] = now


# ════════════════════════════════════════ TELEGRAM ════════════════════════════════════════
def is_authorized(c: Cardinal, m: Message) -> bool:
    """Проверяет, есть ли отправитель в списке авторизованных пользователей ПУ."""
    if not c.telegram or m.from_user is None:
        return False
    return m.from_user.id in c.telegram.authorized_users


def send_health(m: Message) -> None:
    """Обрабатывает команду /health (живой запрос к FunPay выполняется в отдельном потоке)."""
    c = _cardinal
    if c is None or not c.telegram:
        return
    if not is_authorized(c, m):
        logger.warning(f"[HealthMonitor] Команда /health от неавторизованного пользователя "
                       f"$YELLOW{m.from_user.id}$RESET проигнорирована.")
        return

    args = (m.text or "").split()[1:]
    live = "fast" not in [i.lower() for i in args]

    def worker():
        try:
            c.telegram.bot.send_message(m.chat.id, build_health_text(c, live_check=live))
        except Exception:
            logger.error("[HealthMonitor] Ошибка при формировании отчета /health.")
            logger.debug("TRACEBACK", exc_info=True)
            try:
                c.telegram.bot.send_message(m.chat.id, "❌ Не удалось собрать отчет. Подробности в логах бота.")
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)

    threading.Thread(target=worker, daemon=True, name="HealthMonitorRequest").start()


# ═════════════════════════════════════ ИНИЦИАЛИЗАЦИЯ ═════════════════════════════════════
def pre_init(c: Cardinal) -> None:
    """Регистрирует команду /health и переводы."""
    global _cardinal
    _cardinal = c
    load_config()
    with _lock:
        _state["started"] = time.time()
        _state["last_event"] = time.time()

    for language, text in (("ru", "Состояние бота: сессия, события, ресурсы"),
                           ("en", "Bot health: session, events, resources"),
                           ("uk", "Стан бота: сесія, події, ресурси")):
        try:
            localizer.add_translation(UUID, "cmd_health", text, language)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

    if not c.telegram:
        logger.info("[HealthMonitor] Telegram отключен, команда /health не зарегистрирована.")
        return
    c.telegram.msg_handler(send_health, commands=["health"])
    c.add_telegram_commands(UUID, [("health", f"{UUID}_cmd_health", True)])
    logger.info("[HealthMonitor] Команда $YELLOW/health$RESET зарегистрирована.")


def post_start(c: Cardinal) -> None:
    """Запускает поток сторожа."""
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    with _lock:
        _state["last_session_check"] = time.time()  # не проверять сессию сразу после старта
    threading.Thread(target=watchdog_loop, args=(c, c.run_id), daemon=True, name="HealthMonitorWatchdog").start()


def on_delete(c: Cardinal, call) -> None:
    """Удаляет файлы плагина при его удалении через ПУ Telegram."""
    try:
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
    try:
        c.telegram.bot.answer_callback_query(call.id, "Файлы плагина Health Monitor удалены.")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


BIND_TO_PRE_INIT = [pre_init]
BIND_TO_POST_START = [post_start]

# Считаем все события Runner, чтобы понимать, жив ли бот.
BIND_TO_INIT_MESSAGE = [on_event]
BIND_TO_MESSAGES_LIST_CHANGED = [on_event]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [on_event]
BIND_TO_NEW_MESSAGE = [on_event]
BIND_TO_INIT_ORDER = [on_event]
BIND_TO_ORDERS_LIST_CHANGED = [on_event]
BIND_TO_NEW_ORDER = [on_event]
BIND_TO_ORDER_STATUS_CHANGED = [on_event]
BIND_TO_DELETE = on_delete
