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
venv\Scripts\python.exe tools\test_synth_lane.py    # полоса синтеза: приоритеты
venv\Scripts\python.exe tools\test_settings.py       # нормализация настроек
venv\Scripts\python.exe tools\test_api.py            # маршруты и связность модулей
venv\Scripts\python.exe tools\test_imports.py        # циклы импортов и неопределённые имена
venv\Scripts\python.exe tools\test_queue.py           # очередь озвучки (без сервера)
venv\Scripts\python.exe tools\test_synthesis.py       # модуль синтеза (без моделей)
venv\Scripts\python.exe tools\bench_queue.py         # замер цикла очереди (нужен сервер)
venv\Scripts\python.exe tools\ab_compare.py           # слепой A/B наборов качества (оффлайн)
venv\Scripts\python.exe tools\profile_pipeline.py      # baseline конвейера через HTTP (нужен сервер)
venv\Scripts\python.exe tools\profile_synthesis.py     # профиль XTTS по фазам (нужна видеокарта)
B:\projects\TwitchTTS\cosyvoice\env\Scripts\python.exe tools\profile_cosyvoice.py   # профиль CosyVoice (в окружении CosyVoice)
venv\Scripts\python.exe tools\test_emotes.py         # эмоции и разбор текста
venv\Scripts\python.exe tools\test_langs.py          # языки
venv\Scripts\python.exe tools\audit_emotes.py        # сквозная проверка эмоций (нужен сервер)
venv\Scripts\python.exe tools\audit_layout.py        # вёрстка страниц (нужен сервер)
venv\Scripts\python.exe tools\audit_functional.py    # сценарии в браузере (нужен сервер)
venv\Scripts\python.exe tools\audit_mute_volume.py   # мьют и громкость (нужен сервер)
venv\Scripts\python.exe tools\test_chat_live.py      # чат и команды через HTTP (нужен сервер)
```

Аудиты с пометкой «нужен сервер» бьют по `http://127.0.0.1:8765` — сначала
поднимите сервер. `test_chat_live.py` бьёт по `http://127.0.0.1:8769` и требует
сервер с заглушкой и отладочным входом чата:

```
set TTS_PORT=8769 & set TTS_MOCK=1 & set TTS_DEBUG_CHAT=1
venv\Scripts\python.exe app\main.py --no-browser
```

Каждый набор печатает `Провалено: N`; ненулевое число — причина не закрывать задачу.

## Золотые слепки

Слепок фиксирует поведение, чтобы рефакторинг не прошёл тихо. Перед шагом
слепок должен быть снят **со старого кода**, после шага — проверен новым.

```
# снять эталон (сервер поднят на 8769 с заглушкой)
venv\Scripts\python.exe tools\golden_chat.py --out tools\golden_chat.json
venv\Scripts\python.exe tools\golden_api.py --base http://127.0.0.1:8769 --out tools\golden_api.json
venv\Scripts\python.exe tools\golden_settings.py --base http://127.0.0.1:8769 --out tools\golden_settings.json
venv\Scripts\python.exe tools\golden_queue.py --base http://127.0.0.1:8769 --out tools\golden_queue.json

# проверить новый код
venv\Scripts\python.exe tools\golden_api.py --base http://127.0.0.1:8769 --check tools\golden_api.json
venv\Scripts\python.exe tools\golden_settings.py --base http://127.0.0.1:8769 --check tools\golden_settings.json
venv\Scripts\python.exe tools\golden_queue.py --base http://127.0.0.1:8769 --check tools\golden_queue.json
```

Если поведение меняют намеренно — слепок переснимают **отдельным коммитом**
с объяснением, что изменилось. Молчаливый пересчёт слепка означает спрятанную
регрессию.

`golden_queue.py` сначала прогревает движок: в режиме заглушки XTTS настоящий,
а CosyVoice — нет, поэтому первый синтез поднимает его воркер и занимает
десятки секунд. `golden_api.py` ждёт пустой очереди и возвращает настройки
как были — иначе слепок затирал бы громкость и скорость пользователя.

Слепки не зависят от личных настроек: `golden_settings.json` хранит только
отличия от исходного состояния, `golden_api.json` — только состав полей
настроек. Поэтому смена громкости, скорости или добавление токена не ломают
сверку. Аудиты тоже возвращают настройки как были.

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

Слепое сравнение нескольких наборов — `tools/ab_compare.py`:

```
venv\Scripts\python.exe tools\ab_compare.py --sets before-G after-G control-G --out data\quality\AB-2026-11-15
```

Скрипт считает объективные метрики (длительность, RMS, f0, спектральный
центроид, sha256), собирает слепой комплект: файлы перемешаны и переименованы
в A/B/C, ключ пишется отдельно в `blind-key.json`. Комплект для прослушивания
лежит в `data/quality/<дата>/blind-listening.zip` — ключ открывают после
прослушивания. Итог последнего прогона: `docs/quality-ab-2026-11-15.md`.

Важно: одинаковый sha256 у двух наборов означает побайтово идентичный звук —
это сильнее любой оценки. Расхождение оценивают, сравнивая с разбросом двух
прогонов одного кода: он и есть шум движка.

## Замеры производительности

```
venv\Scripts\python.exe tools\quality_baseline.py --out data\quality\run   # время генерации в лог
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv
```

Полезно снимать: время первой фразы (прогрев модели), время обычной фразы,
пиковую VRAM, утилизацию GPU, время ожидания в очереди. Сравнивать только
в одинаковых условиях.

Полный baseline конвейера (синтез по движкам, оверхед HTTP, отдача звука,
загрузка GPU) снимается инструментом `tools/profile_pipeline.py`:

```
venv\Scripts\python.exe tools\profile_pipeline.py --base http://127.0.0.1:8765 --repeats 2 --out data\bench\perf.json
```

Он ходит только по публичному HTTP-интерфейсу, поэтому код приложения не нужно
трогать. Раскладку XTTS по фазам (токенизация, генерация кодов GPT, вокодер)
даёт `tools/profile_synthesis.py`: он поднимает тот же движок в отдельном
процессе и снимает профиль Python — сервер для этого останавливать не нужно,
но видеокарта должна быть свободна.


Прослушивание голосов на странице «Голоса» замеряется отдельно:

```
venv\Scripts\python.exe tools\measure_preview.py --count 8 --out data\bench\preview.json
venv\Scripts\python.exe tools\check_preview_vs_chat.py --base http://127.0.0.1:8765 --count 8
```

Первый скрипт меряет, сколько стоит предпрослушивание и как ведёт себя очередь;
второй - не глушит ли предпрослушивание озвучку чата (реплика должна прозвучать
за секунды, а не после всех голосов).

Итог замеров: `docs/performance-baseline-2026-11-15.md`.

Цикл очереди — от начала озвучки одной реплики до начала следующей — и есть
главная метрика задержки:

```
venv\Scripts\python.exe tools\bench_queue.py --count 4 --out data\bench\run.json
```

Сервер должен быть запущен (по умолчанию `http://127.0.0.1:8765`, другой
адрес — переменной `TTS_BASE`). В JSON — длительность каждого цикла и
среднее. Сравнивать только на прогретом сервере: первый цикл включает
разогрев модели. После шага H (предгенерация) цикл сократился с 8.42 с
до 4.64 с.

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
