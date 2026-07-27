# dockerbundle

Собирает несколько docker-compose сервисов в **один запечённый образ** под управлением
supervisord, плюс тонкую прослойку `docker-compose.yml` для прода. Деплой сводится к
`docker pull` одного образа из ghcr.

Утилита распространяется как самодостаточный бинарь (`.exe` для Windows, ELF для Linux):
ни Python, ни зависимостей на целевой машине не нужно. Docker нужен только для сборки
самого образа — сама генерация работает и без него, по compose-файлам.

---

## Быстрый старт

```bash
cd /path/to/project
dockerbundle init --source ../packages     # создаст bundle.yml
dockerbundle scan                          # что нашлось и какой рецепт подобрался
dockerbundle wizard                        # интерактивно выбрать сервисы и решить конфликты
dockerbundle generate                      # bundle.yml -> dist/
docker build -t ghcr.io/you/project-bundle:v1 dist/
```

`dist/` самодостаточен: `docker build dist/` работает из любого каталога.

## Команды

| Команда | Назначение |
|---|---|
| `init` | Создать `bundle.yml` в текущем проекте |
| `scan` | Показать найденные сервисы и подобранные рецепты |
| `wizard` | TUI-редактор `bundle.yml` (сервисы, маунты, порты, реплики, конфликты) |
| `generate` | Отрендерить `bundle.yml` в `dist/`. Это вызывает CI |
| `doctor` | Проверить окружение |
| `ps` / `restart` / `logs` | Управлять отдельным сервисом внутри запущенного бандла |
| `set-language` | Запомнить язык интерфейса (`en`, `ru`) |

Интерфейс переводится на русский и английский: язык определяется по локали,
переопределяется через `--lang` или `DOCKERBUNDLE_LANG`.

## Источники сервисов

`bundle.yml` перечисляет, где искать кандидатов. Поддерживаются четыре вида:

```yaml
sources:
  - {type: catalog,   path: ../packages}          # каталог папок, в каждой compose-файл
  - {type: compose,   path: docker-compose.yml}   # один файл с N сервисами
  - {type: container, name: shop_php_1}           # живой контейнер (docker ps)
  - {type: image,     ref: redis:7-alpine}        # голый образ
```

Метаданные берутся из `docker image inspect` (entrypoint, cmd, порты, env), а при
недоступности образа или демона — из исходного compose-файла. Для сервисов с `build:`
базовый образ определяется по `FROM` их Dockerfile с подстановкой build-args.

## Что попадает в образ

Всё выбранное — кроме того, что запечь физически нельзя. Такие сервисы помечаются в
рецепте `bakeable: false` и попадают в сгенерированный compose соседним сервисом:

- traefik и всё, что монтирует `/var/run/docker.sock` — прокси не может быть одним из
  процессов, которыми он управляет;
- `privileged`, `network_mode: host`;
- сервисы-заглушки вроде `sleep infinity` ради создания сети.

Бинд-маунты классифицируются на **config** / **code** / **state**. Конфиги и код
запекаются через `COPY`, состояние (`/var/lib/mysql`, `/data`, кэш моделей) остаётся
томом. Непонятные маунты по умолчанию остаются томами: запечь то, что мы не поняли,
хуже, чем лишний том. Любое решение переопределяется в визарде или в `bundle.yml`.

## Рецепты

Поддержка нового рантайма — это YAML-файл, а не правка кода. Встроенные лежат в
`recipes/builtin/`; проект может положить свои в `recipes/` рядом с `bundle.yml`,
одноимённый файл полностью заменяет встроенный.

```yaml
name: redis
match:
  image: ["redis", "redis:*"]
  files: ["redis.conf"]
install:
  debian: [redis-server]
port:
  default: 6379
  configure: {type: cli_flag, flag: "--port"}
supervisor:
  - name: "{slug}"
    command: redis-server --port {port}
    priority: 15
    scalable: false
```

Если ни один рецепт не подошёл, генерируется автоматический: файловая система исходного
образа импортируется в бандл, а запускается его собственный `ENTRYPOINT`/`CMD`. Импорт
идёт слиянием — существующие файлы базы сохраняются, недостающие пользователи
дописываются, — иначе чужой `/etc/passwd` сломал бы все остальные сервисы.

## Порты и реплики

Внутри одного контейнера все сервисы делят network namespace, поэтому два nginx не могут
слушать `:80`. Первый претендент сохраняет порт, остальные переезжают в диапазон
`port_range` и их конфиг переписывается механизмом, объявленным в рецепте. Итоговая
карта портов пишется в `dist/bundle.lock.yml`.

Реплики задаются через `.env` и работают только для процессов, **не занимающих порт**
(очереди, воркеры) — им supervisord поднимает `numprocs` копий. Для port-bound процессов
это невозможно (EADDRINUSE), утилита предупреждает об этом при генерации; масштабировать
такие сервисы нужно на уровне compose.

## Порядок старта

`depends_on` превращается в supervisord `priority` топологической сортировкой. Но
порядок — не готовность, поэтому запуск разбит на три фазы:

1. поднимаются сервисы данных (БД, кэши, брокеры);
2. после проверок готовности выполняются **собственные `entrypoint.sh` сервисов**;
3. освобождаются прикладные процессы.

Исходный `entrypoint.sh` копируется как есть и выполняется целиком, ровно как задумал
автор пакета — dockerbundle его не читает и не переписывает. Маркер `.done` делает
повторные старты мгновенными: `composer install` и загрузка моделей происходят один раз.

Из-за этого **первый старт на проде может быть долгим и требовать сети**.

## Переменные окружения

`.env.example` всех пакетов сливаются в один. Ключ, определённый один раз или везде
одинаково, просто переносится. Ключ с **разными** значениями — это конфликт, и генерация
останавливается, пока решение не записано в `bundle.yml`:

```yaml
env_conflicts:
  EXTERNAL_ACCESS: prefix        # у каждого сервиса свой LARAVEL_EXTERNAL_ACCESS
  DB_HOST: keep:mysql            # взять значение из mysql
  VITE_API_BASE_URL: value:/api  # задать вручную
```

При `prefix` приложение продолжает видеть привычное имя: supervisord отдаёт процессу
`DB_HOST` со значением переименованного ключа.

## Эксплуатация

В образе `tini` как PID 1 (собирает зомби от cron и composer), у каждой программы свои
`stopsignal`/`stopwaitsecs` из рецепта, так что `docker stop` не рвёт БД. `HEALTHCHECK`
проверяет, что все критичные программы в `RUNNING` и отвечают readiness-пробы.

Отдельный сервис можно перезапустить без перезапуска контейнера:

```bash
dockerbundle ps      shop-bundle
dockerbundle restart shop-bundle nginx
dockerbundle logs    shop-bundle php-fpm
```

## Разработка

Нужен Python 3.10+. Зависимости объявлены только в `pyproject.toml`.

```bash
pip install -e ".[dev]"

pytest                        # весь набор
pytest -m "not docker"        # без тестов, реально собирающих образ
ruff check .
```

Сборка бинаря: `bash build/build.sh` (POSIX) или `build\build.ps1` (Windows). Скрипт сам
ставит зависимости, прогоняет тесты и кладёт артефакт в `dist/`. `SKIP_TESTS=true`
пропускает тесты.

Golden-тесты сравнивают весь сгенерированный `dist/` побайтово. После намеренного
изменения шаблона или рецепта эталоны обновляются так:

```bash
pytest tests/test_golden.py --update-golden
```

Тесты с меткой `docker` собирают настоящий образ и требуют доступный демон; без него
они пропускаются автоматически.

## CI

`.github/workflows/ci-cd.yml` — тонкий вызов переиспользуемого workflow из
`cat-of-summer/Git_toolkit`. Правки YAML не нужны, вся настройка — переменные
репозитория (Settings → Secrets and variables → Actions → Variables):

| Переменная | Значение |
|---|---|
| `ACTION_TRIGGER` | `RELEASE` |
| `TOOLCHAIN` | `python@3.12` |
| `RUNS_ON` | `ubuntu-latest,windows-latest` |
| `BUILD_COMMAND` | `bash build/build.sh` |
| `RELEASE_FILES` | `dist/dockerbundle-*` |

`ACTION_TRIGGER` обязателен: по умолчанию он равен `WORKFLOW_DISPATCH`, а при этом
значении пуш тега не запускает ни сборку, ни релиз. `PUSH` дополнительно гоняет CI на
каждый пуш в любую ветку.

`BUILD_COMMAND` тоже обязателен, и не только ради бинаря: job `release-publish` ищет
файлы `RELEASE_FILES` в рабочем дереве после job `ci`. Если не задать ни
`BUILD_COMMAND`, ни `CI_COMMAND`, job `ci` не запустится, `dist/` не появится и релиз
упадёт. Отдельный `CI_COMMAND` не нужен — `build/build.sh` прогоняет тесты сам.

Не задавайте `MULTIPLE_PACKAGES` (иначе тег обязан быть в форме `{branch}/vX.Y.Z`),
`PUBLISH_METHOD` (утилита не публикует docker-образ) и переменные `DEPLOY_*` (иначе
запустится деплой). Секреты не нужны, `GITHUB_TOKEN` выдаётся автоматически.

Релиз:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Тег строго `vX`, `vX.Y` или `vX.Y.Z` без префикса ветки. К релизу прикрепятся
`dockerbundle-linux-x64` и `dockerbundle-windows-x64.exe`.

В **целевом проекте**, где утилита собирает бандл, тот же workflow можно настроить на
сборку и пуш образа в ghcr:

| Переменная | Значение |
|---|---|
| `BUILD_COMMAND` | `dockerbundle generate --yes` |
| `PUBLISH_METHOD` | `docker` |
| `DOCKERFILE_PATH` | `dist/Dockerfile` |
| `BUILD_CONTEXT` | `dist` |

Job `docker-publish` сам логинится в ghcr и ставит теги `:версия`, `:latest`, `:branch`.

## Ограничения

- **glibc.** PyInstaller-бинарь совместим с glibc только «вперёд»: собранный на новой
  системе не запустится на старой. Артефакт из CI наследует glibc раннера
  (`ubuntu-latest`), что для более старых серверов может оказаться слишком ново. Если
  целевой сервер старше, соберите бинарь на нём же или в контейнере с подходящей базой:
  `docker run --rm -v "$PWD:/w" -w /w python:3.12-slim-bookworm sh -c 'apt-get update && apt-get install -y binutils && bash build/build.sh'`
- **Один рантайм на образ.** Два PHP-сервиса с разными версиями PHP в один образ не
  поместятся: версия в образе одна. То же для Node и Python.
- **multi-arch не поддерживается** — только `linux/amd64`. Части базовых образов
  (например TGI) под arm64 просто не существует.
- **rootfs-fallback даёт толстый образ** и не умеет менять порт сервиса: конфликт портов
  с таким сервисом разруливается вручную через `services.<slug>.ports` в `bundle.yml`.
- **Логи не префиксуются** в общем stdout. Обёртка `cmd | sed 's/^/[svc] /'` сделала бы
  `sed` процессом, который отслеживает supervisord, портя коды выхода и `autorestart`.
  Per-service вывод доступен через `dockerbundle logs`.

## Лицензия

MIT.
