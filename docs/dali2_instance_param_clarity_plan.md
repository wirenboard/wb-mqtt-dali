# Plan: Подробные описания конфиг-параметров датчика присутствия (DALI2, тип 3)

Branch: feature/dali2_instance_param_clarity

## Контекст

У датчика присутствия/движения (тип инстанса 3, IEC 62386‑303) названия таймеров не
отражают логику конечного автомата (Figure 2/3): «занято» у датчика движения —
залатченное состояние, удерживаемое hold timer’ом после последнего движения;
`report_timer` — интервал повторов «still occupied/vacant», а не «таймер отчёта»;
`deadtime_timer` — минимальный зазор между событиями на шине, а **не** удержание
(прямо оговорено в стандарте).

Цель — добавить подробные двуязычные описания (`description`) этим параметрам, чтобы
было ясно, на что влияет каждый. Заголовки и ключи параметров **не меняем**.

## Что меняется

### Двуязычные описания: доработка механизма

Сейчас `NumberSettingsParam.get_schema` выводит `description` одной строкой и **не
переводит** её. Решение: тип поля `description` меняется со `str` на существующую
структуру en/ru `TranslatedTitle` (из `wbmqtt.py`, та же форма, что у заголовков).
В отличие от короткого `title`, текст описания длинный, поэтому **сам текст не
используется как ключ перевода** (иначе он дублировался бы в схеме). Вместо этого
`get_schema` генерирует короткий уникальный ключ на основе имени свойства —
`f"{property_name}_description"` — кладёт его в `description`, и регистрирует **оба**
варианта (`en` в `translations.en`, `ru` в `translations.ru`) под этим ключом через
`add_translations`. Существующее строковое описание в `dali2_type32_parameters.py`
мигрирует на `TranslatedTitle(en=…)` — текст уезжает в `translations.en` под ключом.

`DimmingCurveParam` (`dali_parameters.py`) рефакторится на тот же механизм: вместо
ключа-заглушки `"dimming_curve_desc"` с ручной подстановкой en/ru-текстов через
`add_translations` для обеих локалей, `description` задаётся настоящим
`TranslatedTitle(en=…, ru=…)` (по ветке read-only). База сама генерирует ключ и кладёт
en/ru в `translations`; ручные блоки переводов в `get_schema` удаляются, остаётся
только enum и его переводы (`standard`/`linear`).

### Описания для типа 3

Тексты описывают только смысл и влияние параметра; числовые границы, шаг и значения
по умолчанию в описания не включаем (они видны в самом виджете).

- **`hold_timer`** —
  EN: «Movement sensors only. How long the area stays "Occupied" after the last
  detected movement. Every new movement restarts the countdown. When it expires, the
  state turns to "Vacant". Not used by presence sensors (they conclude occupancy
  directly).»
  RU: «Только для датчиков движения. Как долго область остаётся в состоянии «Занято»
  после последнего обнаруженного движения. Любое новое движение перезапускает отсчёт
  заново. По истечении — переход в «Вакантно». У датчиков присутствия не действует
  (там занятость определяется напрямую).»
- **`report_timer`** —
  EN: «How often the sensor re-reports its current state ("still occupied" / "still
  vacant") even when nothing changed. Keep-alive confirmations; they do not affect the
  occupied/vacant transitions or the hold time.»
  RU: «Период повторной отправки текущего состояния («всё ещё занято» / «всё ещё
  вакантно»), даже если оно не менялось. Служебные подтверждения активности датчика;
  на переходы «занято/вакантно» и на удержание не влияют.»
- **`deadtime_timer`** —
  EN: «Limits how often events are sent on the bus, so the sensor doesn't flood it when
  triggers come rapidly. After each sent event the sensor pauses and won't send new
  ones until that pause elapses (it restarts after every send). State is still tracked
  as usual — only the sending rate is limited. This is not the "Occupied" hold timer,
  and it does not delay state detection itself.»
  RU: «Ограничивает частоту отправки событий на шину, чтобы датчик не «заваливал» её
  сообщениями при частых срабатываниях. После каждого отправленного события датчик
  выдерживает паузу и не шлёт новые, пока она не истечёт (пауза отсчитывается заново
  после каждой отправки). Состояние при этом отслеживается как обычно — ограничивается
  только темп отправки. Это не таймер удержания «Занято» и не задержка распознавания
  самого состояния.»

## Tests

Проверки через публичный `get_schema(...)`:

- `test_description_translated_to_both_locales` — у типа 3 `hold_timer`/`report_timer`/
  `deadtime_timer` в `description` стоит короткий ключ `{property_name}_description`, а
  оба варианта текста (en и ru) зарегистрированы под этим ключом в `translations`.
- `test_description_without_ru_adds_only_en_translation` — параметр с описанием только
  на en (type32) даёт валидную схему: en-перевод под ключом есть, ru-перевода нет.

## Documentation

Пользовательских доков/AsyncAPI правок не требуется: меняется только поле
`description` в генерируемой JSON-схеме параметров.

## Out of scope

- **Переименование заголовков** (`SettingsParamName`) — не трогаем.
- Числовые границы, шаг и дефолты в текстах описаний.
- Описания для типов 2/4/6 (в т.ч. неоднозначность `hysteresis` свет/общий датчик и
  множитель `report_timer` ×5 у типа 6) — при необходимости отдельной задачей.
- MQTT-топики, контролы, публикуемые события, различение presence/movement.
- Изменение ключей `property_name`, диапазонов, множителей, логики чтения/записи.
