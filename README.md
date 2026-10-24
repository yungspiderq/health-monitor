# ❤️ HealthMonitor — диагностика и сторож бота

Плагин для [FunPay Cardinal](https://github.com/sidor0912/FunPayCardinal) **v0.1.17+**

**Автор:** yungspider

Бот на VPS работает 24/7, и самый обидный сценарий — он «тихо умер»: протух golden key,
отвалился прокси, завис поток запросов, кончилось место на диске. Плагин показывает
состояние бота одной командой и сам бьёт тревогу, когда что-то идёт не так.

## Возможности

- ❤️ **`/health`** — всё на одном экране: аптайм, аккаунт, **живая проверка сессии**,
  «пульс» Runner, лоты, модули Кардинала, плагины, ОЗУ/CPU/диск/потоки.
- ⚡ **`/health fast`** — тот же отчет без дополнительного запроса к FunPay.
- 🔐 **Сторож сессии**: N неудачных проверок подряд (`sessionFailuresToAlert`) — критическое
  уведомление: протух golden key, бан, сеть или прокси.
- 📡 **Молчание Runner**: ни одного события дольше `noEventsAlert` минут — тревога.
- 🧠 **Ресурсы**: ОЗУ процесса выше `maxMemoryMb` или место на диске меньше `minDiskFreeMb`.
- 🚨 Тревоги идут типом **critical** — их нельзя заглушить настройками уведомлений чата,
  а повторы одного типа подавляются на `alertCooldown` минут.
- 💓 Опциональный **heartbeat** «бот жив» раз в N часов — спокойный сон при работе 24/7.

## Установка

```bash
cd /home/fpc/FunPayCardinal
wget https://raw.githubusercontent.com/yungspiderq/FPC-HealthMonitor/main/health_monitor.py -O plugins/health_monitor.py
sudo systemctl restart FunPayCardinal@fpc
```

или отправьте файл боту в Telegram → `🔌 Плагины` → `➕ Добавить плагин`.

## Использование

Команда **`/health`** (или **`/health fast`** — без живого запроса к FunPay).

### Пример ответа

```text
❤️ Состояние FunPay Cardinal

⏱ Аптайм: 2 дн. 3 ч. 15 мин. (старт: 22.10.2026 10:00:12)
🔑 Аккаунт: seller_test (ID 111)
🔐 Сессия FunPay: ✅ валидна
📦 Незавершенных продаж: 3
📡 Событий с запуска плагина: 1284
   • последнее: 2 мин. назад
   • сообщения: 2 мин. назад  |  заказы: 41 мин. назад
🛍 Лотов в профиле: 24 (авто-лоты: 20)
⚙️ Поднятие лотов: ✅  |  Автоответчик: ✅  |  Автовыдача: ✅
⚙️ Автовосстановление: ❌  |  Автодеактивация: ✅  |  Telegram: ✅
⤴️ След. поднятие лотов: через 12 мин.
🧩 Плагины: 5/5 включено
🧠 ОЗУ процесса: 78 МБ  |  CPU: 3%  |  ОЗУ системы: 19%
💾 Диск: свободно 9.27 ГБ из 40.0 ГБ
🧵 Потоков: 12
```

## Настройки

Файл `storage/plugins/health_monitor.cfg` создаётся при первом запуске и **перечитывается на лету** — перезапуск бота не нужен.

| Параметр | По умолчанию | Что делает |
|---|---|---|
| `enabled` | `1` | включен ли сторож (`/health` работает всегда) |
| `sessionCheckInterval` | `10` | интервал проверки сессии, минут |
| `sessionFailuresToAlert` | `3` | сколько неудач подряд считать проблемой |
| `noEventsAlert` | `360` | минут без событий Runner до тревоги (`0` = выкл.) |
| `maxMemoryMb` | `800` | лимит ОЗУ процесса, МБ (`0` = выкл.) |
| `minDiskFreeMb` | `500` | минимум свободного места, МБ (`0` = выкл.) |
| `alertCooldown` | `60` | пауза между повторными тревогами, минут |
| `heartbeatInterval` | `0` | сводка «бот жив» раз в N часов (`0` = выкл.) |

## Важно знать

- На «тихих» аккаунтах событий Runner может не быть часами — это нормально, поэтому
  `noEventsAlert` по умолчанию 360 минут. Поставьте `0`, если проверка не нужна.
- Проверка сессии — это один живой запрос `account.get()` раз в `sessionCheckInterval`
  минут: нагрузка на FunPay минимальная.
- Системная информация берётся через `psutil` (он уже есть в `requirements.txt` Кардинала);
  если его нет, отчет просто не будет содержать блока про ОЗУ и диск.
- Плагин read-only: ничего не пишет покупателям и не меняет настройки Кардинала.
- Настройки: `storage/plugins/health_monitor.cfg`.

## Состав репозитория

- `health_monitor.py` — плагин (класть в `plugins/`).
- `tests_plugin_health_monitor.py` — функциональные тесты на моках (52 проверки: метаданные и загрузка
  через `Cardinal.load_plugin()`, все хэндлеры событий, команды Telegram, фоновые потоки и выход
  по смене `run_id`, алерты, cooldown'ы, дедупликация событий, удаление плагина).
- `tests_integration_health_monitor.py` — интеграционные тесты (22 проверки): плагин грузится
  **настоящим** `Cardinal` и проходит боевой путь `load_plugins()` → `add_handlers()` →
  `run_handlers()` → `toggle_plugin()`, плюс утверждение «в логах бота ноль ошибок».
  Запуск из корня FunPayCardinal:
  ```bash
  cp tests_plugin_health_monitor.py tests_integration_health_monitor.py /path/to/FunPayCardinal/
  cd /path/to/FunPayCardinal && pip install -r requirements.txt
  python3 tests_plugin_health_monitor.py && python3 tests_integration_health_monitor.py
  ```
  Тесты сами найдут Кардинал и в другом месте: `FPC_PATH=/opt/FunPayCardinal python3 tests_plugin_health_monitor.py`.
  Сетевые объекты подменены заглушками — реальных запросов к FunPay и Telegram тесты не делают.
- `LICENSE` — MIT.

### Другие плагины автора

* [FPC-BalanceGuard](https://github.com/yungspiderq/FPC-BalanceGuard) — контроль баланса, [FPC-SalesAnalytics](https://github.com/yungspiderq/FPC-SalesAnalytics) — статистика продаж, [FPC-ChatGuard](https://github.com/yungspiderq/FPC-ChatGuard) — тревожные ситуации в чатах.
