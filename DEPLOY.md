# Запуск бота на сервере (Ubuntu)

Инструкция для чистого VPS с Ubuntu 22.04 или 24.04. Бот будет работать круглосуточно: сам запустится после перезагрузки сервера и перезапустится, если упадёт.

Команды ниже выполняются в терминале сервера, к которому вы подключились по SSH:

```bash
ssh root@АДРЕС_СЕРВЕРА
```

Если вы заходите не под `root`, а под обычным пользователем с правами администратора, — всё так же, команды с `sudo` попросят ваш пароль.

## ⚠️ Важно: один бот — один запуск

**Нельзя одновременно запускать бота на своём компьютере и на сервере с одним и тем же токеном.** Telegram отдаёт сообщения только одному получателю, поэтому две копии будут мешать друг другу: в логах появится ошибка `TelegramConflictError: terminated by other getUpdates request`, а часть сообщений будет уходить «не туда».

Кроме того, у каждой копии своя база `barbershop.db`, и записи, сделанные через одну копию, другая не увидит.

Перед запуском на сервере остановите бота на компьютере (`Ctrl+C` в терминале, где он запущен). Если нужно что-то проверить локально — сначала остановите бота на сервере (`sudo systemctl stop barbershop-bot`), а после проверки запустите обратно (`sudo systemctl start barbershop-bot`). Ещё удобнее завести для локальных экспериментов отдельного тестового бота у @BotFather со своим токеном.

## 1. Установка Python и Git

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version
```

Нужен Python 3.10 или новее — в Ubuntu 22.04 и 24.04 он уже такой.

## 2. Отдельный пользователь для бота

Запускать бота от `root` небезопасно, поэтому создадим для него пользователя `barbershop`:

```bash
sudo adduser --disabled-password --gecos "" barbershop
```

Дальше работаем от его имени:

```bash
sudo -iu barbershop
```

Приглашение в терминале сменится на `barbershop@...`. Вернуться обратно — командой `exit`.

## 3. Клонирование репозитория

```bash
cd ~
git clone https://github.com/seniawer/barbershop-bot.git
cd barbershop-bot
```

Если репозиторий приватный, GitHub попросит логин и вместо пароля — [персональный токен доступа](https://github.com/settings/tokens) (Personal access token), либо можно настроить SSH-ключ.

## 4. Виртуальное окружение и зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 5. Файл .env с настройками

Файла `.env` нет в репозитории (там токен, его нельзя выкладывать), поэтому создаём его на сервере вручную:

```bash
cp .env.example .env
nano .env
```

Заполните значения:

```
BOT_TOKEN=токен_от_BotFather
ADMIN_ID=ваш_telegram_id
DB_PATH=
```

- `BOT_TOKEN` — обязательно.
- `ADMIN_ID` — необязательно; узнать свой ID можно командой `/id` в боте.
- `DB_PATH` — можно оставить пустым, тогда база будет в файле `barbershop.db` рядом с `bot.py`.

Список мастеров — в файле `masters.json` рядом с `bot.py` (см. README). После его изменения перезапустите службу.
При первом запуске новой версии бот сам привяжет старые записи и закрытое время к первому мастеру из списка;
перед обновлением на всякий случай сделайте копию файла базы.

Сохранить в nano: `Ctrl+O`, затем `Enter`; выйти: `Ctrl+X`.

Закройте файл от посторонних глаз:

```bash
chmod 600 .env
```

### Проверка вручную

Перед настройкой автозапуска убедитесь, что бот стартует (на компьютере бот должен быть остановлен!):

```bash
python bot.py
```

Напишите боту `/start` в Telegram. Если отвечает — остановите его `Ctrl+C` и выйдите из пользователя `barbershop`:

```bash
deactivate
exit
```

## 6. Запуск через systemd

systemd — системный менеджер служб в Ubuntu. Он будет запускать бота при включении сервера и перезапускать при падении. Файл службы уже лежит в репозитории: `deploy/barbershop-bot.service`.

Если вы использовали другого пользователя или другую папку — сначала поправьте в этом файле строки `User`, `Group`, `WorkingDirectory` и `ExecStart`.

```bash
sudo cp /home/barbershop/barbershop-bot/deploy/barbershop-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barbershop-bot
```

`enable` включает автозапуск после перезагрузки, `--now` сразу запускает бота.

Проверить, что бот работает:

```bash
sudo systemctl status barbershop-bot
```

В строке `Active` должно быть `active (running)`. Выйти из просмотра — клавиша `q`.

Полезные команды:

| Команда | Что делает |
|---------|-----------|
| `sudo systemctl status barbershop-bot` | Состояние бота |
| `sudo systemctl restart barbershop-bot` | Перезапустить (например, после изменения `.env`) |
| `sudo systemctl stop barbershop-bot` | Остановить |
| `sudo systemctl start barbershop-bot` | Запустить |
| `sudo systemctl disable barbershop-bot` | Отключить автозапуск |

## 7. Просмотр логов

Всё, что бот пишет в консоль, сохраняет системный журнал:

```bash
sudo journalctl -u barbershop-bot -f
```

`-f` — показывать новые строки в реальном времени (выход — `Ctrl+C`). Другие варианты:

```bash
sudo journalctl -u barbershop-bot -n 100        # последние 100 строк
sudo journalctl -u barbershop-bot --since today # всё за сегодня
```

## 8. Обновление бота после git push

Когда вы изменили код на компьютере и сделали `git push`, на сервере выполните:

```bash
sudo -iu barbershop
cd ~/barbershop-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
deactivate
exit
sudo systemctl restart barbershop-bot
```

`pip install` нужен, только если менялся `requirements.txt`, но лишним он не будет.

Если изменился сам файл `deploy/barbershop-bot.service`, скопируйте его заново и перезагрузите настройки systemd:

```bash
sudo cp /home/barbershop/barbershop-bot/deploy/barbershop-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart barbershop-bot
```

Файлы `.env` и `barbershop.db` при `git pull` не затрагиваются — они есть только на сервере.

## Резервная копия базы

Все записи клиентов хранятся в одном файле `barbershop.db`. Скачать его к себе на компьютер можно командой (выполняется на компьютере, а не на сервере):

```bash
scp root@АДРЕС_СЕРВЕРА:/home/barbershop/barbershop-bot/barbershop.db .
```

## Если что-то не работает

- **`Не найден BOT_TOKEN`** — нет файла `.env` в папке бота или в нём пустой `BOT_TOKEN`.
- **`TelegramConflictError`** — бот запущен где-то ещё (чаще всего на вашем компьютере). Остановите вторую копию.
- **`status=203/EXEC`** в `systemctl status` — неверный путь в `ExecStart` или не создано виртуальное окружение `.venv`.
- **`Permission denied`** — файлы бота принадлежат другому пользователю. Исправить: `sudo chown -R barbershop:barbershop /home/barbershop/barbershop-bot`.
