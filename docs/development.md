# Разработка TwitchTTS

Как запустить, проверить и безопасно менять проект.

## Запуск

```
start.bat                          # обычный запуск с открытием браузера
venv\Scripts\python.exe app\main.py --no-browser   # без браузера
```

Порт задаётся переменной `TTS_PORT` (по умолчанию 8765), хост — `TTS_HOST`.
Полезные переменные:

| Переменная | Смысл |
|---|---|
| `TTS_MOCK=1` | заглушка вместо XTTS: гудок вместо голоса, тесты не ждут модель |
| `TTS_DEBUG_CHAT=1` | добавляет маршрут `POST /api/debug/chat` — подставить сообщение как из чата |
| `TTS_PORT` / `TTS_HOST` | адрес сервера |

## Проверки

Все наборы лежат в `tools/` и запускаются по одному:

```
venv\Scripts\python.exe tools\test_styles.py        # стили речи
venv\Scripts\python.exe tools\test_style_presets.py # подбор стилей под голос
venv\Scripts\python.exe tools\test_features.py       # курс, перевод, события
venv\Scripts\python.exe tools\test_features2.py      # команды чата, префиксы, голоса
venv\Scripts\python.exe tools\test_features3.py      # Twitch API: награды, подписчики, таймаут
venv\Scripts\python.exe tools\test_chat_rules.py     # таблица правил чата
venv\Scripts\python.exe tools\test_settings.py       # нормализация настроек
venv\Scripts\python.exe tools\test_api.py            # маршруты и связность модулей
venv\Scripts\python.exe tools\test_emotes.py         # эмоции и разбор текста
venv\Scripts\python.exe tools\test_langs.py          # языки
venv\Scripts\python.exe tools\audit_emotes.py        # сквозная проверка эмоций (нужен сервер)
venv\Scripts\python.exe tools\audit_layout.py        # вёрстка страниц (нужен сервер)
venv\Scripts\python.exe tools\audit_functional.py    # сценарии в браузере (нужен сервер)
venv\Scripts\python.exe tools\audit_mute_volume.py   # мьют и громкость (нужен сервер)
```

Аудиты с пометкой «нужен сервер» бьют по `http://127.0.0.1:8765` — сначала
поднимите сервер. Каждый набор печатает `Провалено: N`; ненулевое число —
причина не закрывать задачу.

## Золотые слепки

Слепок фиксирует поведение, чтобы рефакторинг не прошёл тихо. Перед шагом
слепок должен быть снят **со старого кода**, после шага — проверен новым.

```
# снять эталон (сервер поднят на 8769 с заглушкой)
venv\Scripts\python.exe tools\golden_chat.py --out tools\golden_chat.json
venv\Scripts\python.exe tools\golden_api.py --base http://127.0.0.1:8769 --out tools\golden_api.json
venv\Scripts\python.exe tools\golden_settings.py --base http://127.0.0.1:8769 --out tools\golden_settings.json

# проверить новый код
venv\Scripts\python.exe tools\golden_api.py --base http://127.0.0.1:8769 --check tools\golden_api.json
venv\Scripts\python.exe tools\golden_settings.py --base http://127.0.0.1:8769 --check tools\golden_settings.json
```

Если поведение меняют намеренно — слепок переснимают **отдельным коммитом**
с объяснением, что изменилось. Молчаливый пересчёт слепка означает спрятанную
регрессию.

## Контроль качества голоса

Сэмплы XTTS стохастичны, поэтому качество проверяют на слух. Набор текстов
зафиксирован в `tools/quality_baseline.py`:

```
venv\Scripts\python.exe tools\quality_baseline.py --out data\quality\baseline-2026-11-15
venv\Scripts\python.exe tools\quality_baseline.py --out data\quality\after-change
```

В каждой папке лежат wav-файлы и `manifest.json` (текст, голос, длительность,
параметры настроек). Сравнение «до» и «после» — слепое: файлы переименовываются,
слушатель не знает, где какой. Автоматические метрики (длительность, энергия,
тон) — только вспомогательные.

Правило: если после оптимизации голос звучит иначе — оптимизация откатывается.
Качество важнее скорости.

## Замеры производительности

```
venv\Scripts\python.exe tools\quality_baseline.py --out data\quality\run   # время генерации в лог
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv
```

Полезно снимать: время первой фразы (прогрев модели), время обычной фразы,
пиковую VRAM, утилизацию GPU, время ожидания в очереди. Сравнивать только
в одинаковых условиях.

## Правила изменения кода

1. Один шаг — один коммит. Поведение не меняется.
2. Сначала тест или слепок, потом правка.
3. Состояние передаётся явно: модуль не читает чужое глобальное состояние.
4. Модуль делает одно: правило чата решает, HTTP переводит запрос в действие,
   синтез озвучивает, очередь ведёт порядок.
5. Не трогаем алгоритмы движков и параметры генерации без доказательства,
   что качество не изменилось.

## Отладка

* Логи движков: `data/rvc_log.txt`, `data/cosyvoice_log.txt`,
  `data/rvc_train_log.txt`.
* Сообщение из чата без Twitch: поднять сервер с `TTS_DEBUG_CHAT=1` и слать
  `POST /api/debug/chat` с `{"user": "test", "text": "..."}`.
* Заглушка вместо модели: `TTS_MOCK=1` — синтез мгновенный, удобно для тестов
  очереди и маршрутов.

## Релиз

```
venv\Scripts\python.exe tools\make_update_zip.py
```

Сборка обновления не трогает `data/`, `voices/` и `sounds/` — пользовательские
данные остаются на месте.
