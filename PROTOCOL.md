# HTTP-протокол GOI

Ядро использует адаптированный `source/http.c`, имя функции — `vtfs_http_call`.
Сохранены функции шаблона fill_request, receive_all, parse_http_response и encode.
Вызов принимает конфигурацию mount, готовую строку query и необязательное тело POST;
дополнительно возвращает число полученных байтов для проверки полноты ответа.
IPv4, HTTP/1.1, отдельное соединение на запрос. Авторизация:
`Authorization: Bearer <server-token>`. Сервис также принимает query-параметр token.

Ответ `/api/<method>` начинается с little-endian int64: 0 либо отрицательный Linux
errno. Дальше идут данные. HTTP Content-Length обязателен; обрезанные ответы,
слишком большие размеры и Transfer-Encoding клиент отклоняет. Таймаут ядра — 10 с.

Metadata = `<QQQIIQ` (40 байт): id, размер, mtime в наносекундах, mode, flags,
номер изменения файла. В flags bit 0 означает read-only, bit 1 — корень ветки.
Номер изменения нужен
для обновления чистого кеша, например после goi resolve; блокировки версий нет.

| Метод | Параметры / body | Данные ответа после errno |
|---|---|---|
| hello | — | metadata корня |
| stat | id | metadata |
| lookup | parent, name | metadata |
| list | id, offset | u32 count; count × (metadata, u16 длина имени, UTF-8 имя), до 100 имён |
| read | id, offset, count | байты, не более 32768 за запрос |
| write | id, mode; POST: весь файл | metadata после сохранения в SQLite |
| create | parent, name, mode | metadata |
| unlink/rmdir | parent, name | пусто |
| rename | old_parent, old_name, new_parent, new_name, flags | metadata |
| setattr | id, mode?, size? | metadata |

Имена кодируются percent-encoding, до 255 UTF-8 байт, без slash/NUL и `.`/`..`.
Размер файла и тела write — до 64 МиБ. Сервис сначала читает **всё** тело запроса,
потом обновляет строку файла и делает SQLite commit. Частичный запрос не меняет
файл. Повтор write заменяет файл теми же байтами; отдельных сеансов загрузки нет.

`POST /control` принимает JSON с полем operation. Ответ:
`{"ok":true,"result":...}` или `{"ok":false,"errno":N,"error":"…"}`.
Операции: auth_start, auth_poll, activate(path), status(branch), diff(branch),
commit(branch,paths,message), resolve(branch,path,choice?), refresh(branch),
restore(branch,paths), recover(branch,forget?).

Fsync вызывает только write. Публикация на GitHub — отдельная команда commit.
Намерение обновить ref журналируется в SQLite до сетевого вызова. Recover
подтверждает уже опубликованный SHA или повторяет его обновление, сохраняя
последующие правки. Forget удаляет намерение без отката локальных файлов.
Restore отменяет выбранные правки до baseline. Refresh возвращает head, conflicts
и preserved_directories: поддеревья локальных изменений каталогов. Ветка обновляется
без force; GitHub отклоняет обновление, если история успела разойтись.

Локальные ioctl: 0x4701 синхронизирует файлы mount; 0x4702 на каталоге ветки
сохраняет изменения и освобождает закрытые чистые файлы других веток из page cache.
Идентификатор ветки наследуется inode от родительского каталога. Открытые файлы
удерживают кеш до закрытия; mmap тоже сохраняет ссылку на открытый файл.

### Кеш сервера

`cache` в `/control` возвращает `cached_bytes`, `removed_bytes`, `limit_bytes`.
Параметр `clear: true` очищает воспроизводимые чистые blobs всего сервиса.
Операция не меняет metadata/revision и не требует синхронизации RAM клиента,
поскольку не затрагивает рабочие данные. Сервер применяет LRU-лимит после
запросов и при запуске; новые и изменённые файлы, конфликты и незавершённые
публикации исключены. Лимит по умолчанию 256 МиБ, настройка `--cache-mib`.

### Клиентский журнал, авторизация и исключения

Mount-параметры `cache_mib` (1..4096, default 256) и `journal_port`
(локальный loopback-порт, 0 отключает журнал). CLI включает журнал по умолчанию.
Локальный журнал использует тот же бинарный STATUS и session token:
POST `/api/record?id=N&revision=R&mode=M` с полным файлом и
POST `/api/ack?id=N` с пустым телом. Ответы без metadata, после SQLite commit.
`record` сохраняет первую ревизию и последнюю копию; `ack` удаляет запись.

Основной `/api/write` поддерживает `expected_revision`: другая ревизия
разрешена только при совпадающих байтах и правах (идемпотентное восстановление).
Иначе ESTALE; журнал сохраняется. Локальные control-команды: info, replay,
export (один ID, base64), discard (явный ID). CLI запрещает replay/discard
на смонтированной ФС. Демон привязан к адресу и session token сервиса.

Основной control: accounts возвращает client_id и login/authenticated;
logout удаляет токен по login, сохраняя данные. status принимает ignored,
commit принимает force для публикации исключённых новых файлов.
