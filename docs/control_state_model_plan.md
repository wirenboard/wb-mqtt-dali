# Plan: типизированная ошибка контрола + единая структура состояния

Branch: `feature/control-state-model`

## Контекст

Две связанные внутренние чистки представления состояния контрола. Поведение на шине не
меняется: формат `/meta/error` остаётся строкой из флагов, топики/RPC/схема — как есть.

Сейчас:
- `/meta/error` таскается свободной строкой: `Device.set_control_error(error: str)`,
  `DevicePublisher.set_control_error(error: str)`, `ControlState.error: Optional[str]`,
  `ControlPollResult.error: Optional[str]`, `GroupStateUpdate.payload` (несёт `"r"`), плюс
  ~22 литерала `"r"`/`"w"` по коду. Пусто (`""`) значит «нет ошибки» (`set_control_error("")`
  → хранит `None`, стирает топик; `set_control_value` так же снимает стоящую ошибку).
  Приложение эмитит только `r` (ошибка чтения) и `w` (ошибка записи); `p` (запоздалый опрос)
  WB-конвенции здесь не используется. Родственный `GatewayMetaErrorPayload`
  (`wbdali.py`) — это *входящий* gateway-error (одиночное значение `""`/`"r"`), отдельная
  сущность, не трогаем.
- `ControlState` (`wbmqtt.py`) = `{meta, value, error}` c `__post_init__`, который
  `deepcopy`-ит meta (чтобы рантайм-`set_control_title`/`read_only` не мутировали чужую
  meta); конструируется в 2 местах — только внутри `Device` как его кэш живого состояния.
  `ControlInfo` (`device_publisher.py`) = `{id, meta, value}`, конструируется в 51 месте,
  читается в 34. То есть `ControlInfo == ControlState + id` — один и тот же набор полей.

## Сценарии

### 1. Ошибка контрола — типизированный флаг вместо строки

`/meta/error`-ошибка моделируется `enum.Flag` `ControlError` (в `wbmqtt.py`) с членами
`NONE` (пустой флаг, «нет ошибки»), `READ` (`"r"`, чтение не удалось) и `WRITE` (`"w"`,
ошибка записи) — только эти два ненулевых флага WB-конвенции проект и использует (`p` —
запоздалый опрос — здесь не детектится и не эмитится, поэтому в тип не включаем). Строкой ошибку больше не задать. Каноническое
преобразование флаг → строка для шины (`to_mqtt()`/`__str__`: конкатенация в порядке r, w;
`""` для пустого). Парсер строка→флаг не вводим: per-control `/meta/error` приложение только
эмитит, не читает. «Нет ошибки» = пустой флаг → на шину `""` (топик стирается), как сейчас.

Тип пронизывает всю цепочку ошибки: `ControlPollResult.error`, поле ошибки на единой
структуре состояния (сценарий 2), сигнатуры `set_control_error`. Все ~22 литерала `"r"`/
`"w"` заменяются членами флага. Флаг конвертируется в строку на границе MQTT-publish
(`to_mqtt()`). `GroupStateUpdate` для ERROR-обновлений группы флаг не несёт: вид (kind)
ERROR самоописателен (групповое состояние всплывает только при ошибке чтения), payload
остаётся пустым, а контроллер сам эмитит `ControlError.READ`. Клиринг (`set_control_value`
снимает стоящую ошибку) выражается пустым флагом.

Итог на шине идентичен сегодняшнему (`r`/`w`/их комбинация/пусто).

### 2. `ControlInfo` = `id` + единый объект состояния `ControlState`

В `ControlInfo` тройка `{meta, value, error}` заменяется одним объектом состояния:
`ControlInfo` = `{id, state: ControlState}`, где `ControlState = {meta, value, error}` —
переиспользуемый объект живого состояния (тот же, что кэширует `Device`; поле `error` —
флаг из сценария 1). `ControlState` сохраняет `deepcopy(meta)` в `__post_init__`, так что
рантайм-`set_control_title`/`read_only` по-прежнему не трогают meta, переданную моделью.
`Device` работает с `ControlState` как сейчас; модель строит
`ControlInfo(id, ControlState(meta, value))`.

Доступ к полям идёт через `.state` (`control_info.state.value` и т.д.) — это затрагивает
места чтения (`~34`) и конструирования (`~51`) `ControlInfo`. Опционально можно добавить
делегирующие свойства (`ControlInfo.value` → `self.state.value`), чтобы сократить правки
чтения, но это прячет композицию; по умолчанию план исходит из явного `.state`-доступа.

## API

Публичных RPC/топиков не добавляется. Меняется внутренний публичный контракт слоя контролов:

| Символ | Было | Стало | Эффект |
| --- | --- | --- | --- |
| `ControlError(Flag)` | — | новый тип в `wbmqtt.py`: `NONE`/`READ`/`WRITE`, `READ`/`WRITE` → `r`/`w`; `to_mqtt()`/`__str__` (без `from_mqtt`) | единственное представление ошибки контрола |
| `Device.set_control_error` / `DevicePublisher.set_control_error` | `error: str` | `error: ControlError` | принимает флаг; на шину — `to_mqtt()` |
| `ControlInfo` | `{id, meta, value}` | `{id, state: ControlState}` | `id` + единый объект состояния (доступ через `.state`) |
| `ControlState` | `{meta, value, error: str}` | `{meta, value, error: ControlError}` (дефолт — `ControlError.NONE`) | переиспользуемый объект состояния, встраивается в `ControlInfo` |
| `ControlPollResult.error` | `Optional[str]` | `ControlError` (пустой = нет) | опрос отдаёт флаг |

Формат `/meta/error` на шине не меняется.

## Tests

- `test_control_error_to_mqtt` — `READ|WRITE` → `"rw"`; пустой → `""`; порядок флагов стабилен
  (r, w).
- `test_set_control_error_publishes_flag_string` — `set_control_error(ControlError.READ)` шлёт
  `"r"` в `/meta/error`; пустой флаг стирает топик (как `""` сейчас).
- `test_set_control_value_clears_standing_error` — существующее поведение (снятие ошибки при
  установке значения) сохраняется с флагом.
- `test_poll_read_failure_yields_read_flag` — упавший опрос отдаёт `ControlPollResult` с
  `ControlError.READ`, публикуется `"r"` (существующие поллинг-тесты переведены на флаг).
- `test_group_error_update_has_empty_payload` — групповой ERROR-апдейт не несёт payload
  (вид ERROR самоописателен; контроллер эмитит `ControlError.READ`), значение публикуется
  как прежде.
- `test_control_state_deepcopies_meta` — рантайм-`set_control_title`/`read_only` не мутируют
  meta, переданную моделью (`deepcopy` в `ControlState.__post_init__` сохранён при встраивании
  в `ControlInfo`).
- `test_control_info_default_error_is_empty` — у нового `ControlInfo` `state.error` — пустой
  флаг, контрол публикуется без `/meta/error`.

## Out of scope

- Формат `/meta/error` на шине, топики, RPC, схема — не меняются.
- `GatewayMetaErrorPayload` (входящий gateway-error) — не трогаем.
- Флаг `p` (запоздалый опрос) WB-конвенции — в этом проекте не нужен, в `ControlError` не
  включаем.
- Реконсиляция с `feature/init-control-state`: та ветка независимо добавляет
  `ControlInfo.error: str` и ~десяток строковых ошибок; при слиянии обеих поле/использования
  сводятся к `ControlError`. Это ожидаемая точка слияния, не часть этой ветки.
