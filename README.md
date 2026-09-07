# dockerbundle

Берёт несколько `docker-compose.yml` и **запекает их сервисы в один образ** под
supervisord. Деплой после этого — `docker pull` одного образа плюс тонкий
`docker-compose.yml` на 20 строк.

Вся настройка — **один файл `docker-bundle.yml`**. Он не повторяет содержимое
compose-файлов: он говорит только то, чего в них нет и быть не может — что из чего
собирать, что оставить снаружи и как разрешить противоречия между пакетами, которые
писались независимо друг от друга.

---

## Содержание

- [Зачем](#зачем)
- [Как это работает](#как-это-работает)
- [Полный пример: nginx + php + vue + mysql + traefik](#полный-пример-nginx--php--vue--mysql--traefik)
  - [Исходные пакеты](#исходные-пакеты)
  - [Что здесь конфликтует](#что-здесь-конфликтует)
  - [`docker-bundle.yml` целиком](#docker-bundleyml-целиком)
  - [Что показывает `scan`](#что-показывает-scan)
  - [Что появляется в `dist/`](#что-появляется-в-dist)
- [Разбор сгенерированного](#разбор-сгенерированного)
  - [Dockerfile](#dockerfile)
  - [supervisord.conf](#supervisordconf)
  - [entrypoint.sh: три фазы старта](#entrypointsh-три-фазы-старта)
  - [.env.example](#envexample)
  - [docker-compose.yml](#docker-composeyml)
  - [docker-bundle.lock.yml](#docker-bundlelockyml)
- [Рецепты](#рецепты)
- [Как разрешаются конфликты](#как-разрешаются-конфликты)
- [Флаги сборки](#флаги-сборки)
- [Ограничения](#ограничения)
- [Установка](#установка)

---

## Зачем

Типичный стенд — это несколько независимых compose-проектов:

```
было                                    стало
────                                    ─────
packages/laravel-nginx/   → 2 контейнера
packages/vue-nginx-vite/  → 2 контейнера        ghcr.io/acme/shop-bundle:latest
packages/mysql/           → 1 контейнер   ══>   один образ, 5 процессов внутри
packages/traefik/         → 1 контейнер         + traefik соседним контейнером

деплой: git pull ×4, docker compose  ══>  деплой: docker pull && docker compose up -d
        up -d ×4, следить за сетью,
        версиями и порядком старта
```

Сложить это руками мешает не сборка образа, а то, что пакеты писались порознь и
**спорят друг с другом**: два nginx хотят `:80`, четыре пакета определяют
`EXTERNAL_ACCESS` с разными значениями, `DB_HOST=mysql` перестаёт резолвиться, когда
mysql становится соседним процессом, а не контейнером.

`docker-bundle.yml` — это файл, где эти споры разрешены.

## Как это работает

Пять шагов, ни один из которых не требует Docker (демон нужен только чтобы потом собрать
образ):

```
1. discover   sources: → читает compose-файлы, раскрывает include:,
              подставляет ${...} из .env, достаёт образы, порты,
              маунты, переменные, depends_on
                    │
                    ▼
2. match      каждому сервису подбирается рецепт: «это nginx»,
              «это php-fpm», «это mysql». Рецепт знает, как этот
              рантайм ставить, чем его конфигурировать и чем запускать
                    │
                    ▼
3. plan       разрешение конфликтов: порты, переменные, метки, слаги;
              решение, что запечь, а что оставить томом или снаружи
                    │
                    ▼
4. render     dist/: Dockerfile, supervisord.conf, entrypoint.sh,
              healthcheck.sh, docker-compose.yml, .env.example
              + context/ со всеми файлами, на которые ссылается COPY
                    │
                    ▼
5. docker build dist/
```

Ключевая мысль: **dockerbundle не переписывает ваши файлы**. Конфиги, код и собственные
`entrypoint.sh` пакетов копируются как есть и выполняются как задумал их автор. Меняется
только то, что физически не может остаться прежним внутри одного контейнера — номер
занятого порта и имя переменной, за которую спорят два сервиса.

---

## Полный пример: nginx + php + vue + mysql + traefik

Всё ниже — не выдумка: это каталог `tests/fixtures/packages/` из этого репозитория, и
все листинги ниже получены реальным прогоном `dockerbundle generate`.

### Исходные пакеты

```
shop/
├── docker-bundle.yml          ← единственный файл, который вы пишете
└── packages/
    ├── 2. traefik/            docker-compose.yml, .env.example
    ├── 3. mysql/              docker-compose.yml, .env.example, mysql.cnf
    ├── laravel-nginx/         docker-compose.yml, .env.example, Dockerfile,
    │                          entrypoint.sh, nginx.conf, php.ini, opcache.ini,
    │                          crontab, supervisord.conf, data/
    └── vue-nginx-vite/        docker-compose.yml, .env.example, Dockerfile,
                               entrypoint.sh, nginx.conf, data/
```

`laravel-nginx/docker-compose.yml` — два сервиса, `laravel` (php-fpm) и `nginx`:

```yaml
services:
  laravel:
    build: {context: ., dockerfile: Dockerfile, args: {PHP_VERSION: ${PHP_VERSION}}}
    working_dir: /var/www/html
    volumes:
      - ./data:/var/www/html
      - ./php.ini:/usr/local/etc/php/conf.d/custom.ini
      - ./crontab:/etc/crontab
    environment:
      - DB_HOST=${DB_HOST}

  nginx:
    image: nginx:alpine
    ports: ["${EXTERNAL_ACCESS}"]
    volumes:
      - ./data:/var/www/html
      - ./nginx.conf:/tmp/nginx.conf.template
    entrypoint: ["sh", "-c", "envsubst < /tmp/nginx.conf.template > /etc/nginx/conf.d/default.conf && exec \"$$@\"", "--"]
    depends_on: [laravel]
```

`vue-nginx-vite/` устроен так же: `vue` (node) + `nginx` с тем же
`/tmp/nginx.conf.template`. `mysql/` и `traefik/` — по одному сервису.

Каталог-источник (`type: catalog`) — это папка, где каждая подпапка отдельный пакет.
Порядковый префикс в имени отбрасывается: `3. mysql` даёт слаг `mysql`. Пакет с
несколькими сервисами даёт слаги вида `laravel_nginx_nginx` — имя пакета плюс имя
сервиса, иначе два `nginx` были бы неразличимы.

### Что здесь конфликтует

| Что | В чём спор | Кто решает |
|---|---|---|
| **порт 80** | `laravel-nginx/nginx` и `vue-nginx-vite/nginx` оба слушают `:80`, а внутри одного контейнера общий network namespace | сборщик автоматически: первый оставляет `80`, второй переезжает в `port_range` |
| **`EXTERNAL_ACCESS`** | объявлен всеми четырьмя пакетами с разными значениями (`8081:80`, `8082:80`, `3306:3306`, `8080:8080`) | **вы**, в `env:` — иначе генерация останавливается |
| **`TRAEFIK_DOMAIN`** | в двух пакетах, значения совпадают сегодня, но это разные сайты | вы, если хотите развести их насовсем |
| **`VITE_API_BASE_URL`** | `http://localhost` больше не верен: api теперь тот же контейнер | вы, литералом |
| **`DB_HOST=mysql`** | имя контейнера `mysql` внутри бандла не резолвится | вы, литералом |
| **traefik** | монтирует `/var/run/docker.sock` и владеет `:80/:443` — прокси не может быть одним из процессов, которыми он управляет | рецепт: `bakeable: false` |
| **`/tmp/nginx.conf.template`** | оба nginx читают один и тот же жёстко зашитый путь | рецепт nginx: подкладывает нужный шаблон перед каждым entrypoint |
| **`./data:/var/lib/mysql`** | это состояние, его нельзя запечь в образ | классификация маунтов: остаётся томом |

Первое и последние три сборщик закрывает сам. Остальное — ваши решения, и они
записываются в `docker-bundle.yml`.

### `docker-bundle.yml` целиком

```yaml
version: 2
name: shop
output: dist
image: ghcr.io/acme/shop-bundle:latest

variants: [cpu]
base:
  cpu: debian:bookworm-slim

network: {name: network, external: true}

# Куда переезжают сервисы, проигравшие спор за порт.
port_range: [20000, 20999]

# Ключи, общие по смыслу: расхождение в них не считается конфликтом.
globals: [NETWORK, INSTANCE, TZ, TRAEFIK_ENABLE, TRAEFIK_ENTRYPOINT, ACME_EMAIL]

# ── Флаги сборки ──────────────────────────────────────────────────────────────
# На ноутбуке база внутри образа, в проде она живёт отдельно:
#   dockerbundle generate --disable mysql
features:
  mysql: true

# ── Откуда брать сервисы ──────────────────────────────────────────────────────
sources:
  - {id: packages, type: catalog, path: packages}

# ── Рецепты ───────────────────────────────────────────────────────────────────
# Достраиваем встроенный nginx, а не подменяем его: нужен всего один лишний пакет.
# entrypoint.nginx.sh считает set_real_ip_from через `ip route`, а на debian-базе
# команды `ip` нет — в исходном образе nginx:alpine её давал busybox.
recipes:
  nginx:
    extends: nginx
    +install: {debian: [iproute2]}

# ── Что с каким сервисом делать ───────────────────────────────────────────────
services:
  # Прокси остаётся отдельным контейнером и переживает пересборку стенда.
  traefik: {mode: sidecar}
  # А база — только пока флаг включён.
  mysql: {when: mysql}

# ── Разрешение конфликтов переменных ──────────────────────────────────────────
env:
  # Имена во внешнем .env заданы явно: их правят руками, и EXTERNAL_ACCESS_SITE
  # понятнее, чем автоматическое LARAVEL_NGINX_NGINX_EXTERNAL_ACCESS.
  # Каждый процесс при этом по-прежнему читает EXTERNAL_ACCESS.
  EXTERNAL_ACCESS:
    per_service:
      laravel_nginx_nginx:   EXTERNAL_ACCESS_SITE
      vue_nginx_vite_nginx:  EXTERNAL_ACCESS_ADMIN
      laravel_nginx_laravel: EXTERNAL_ACCESS_PHP
      vue_nginx_vite_vue:    EXTERNAL_ACCESS_VITE
      mysql:                 EXTERNAL_ACCESS_DB
      traefik:               EXTERNAL_ACCESS_PROXY

  # Сайт и админка — разные домены, даже если сейчас оба localhost.
  TRAEFIK_DOMAIN:
    per_service:
      laravel_nginx_laravel: DOMAIN_SITE
      vue_nginx_vite_vue:    DOMAIN_ADMIN

  # api теперь тот же контейнер, полный URL не нужен.
  VITE_API_BASE_URL: value:/api

  # Имя контейнера `mysql` внутри бандла не резолвится — база стала соседним
  # процессом. Строка нужна только когда база действительно внутри.
  DB_HOST: {rule: "value:127.0.0.1", when: mysql}

# ── Тома для состояния, у которого нет своего маунта ──────────────────────────
# Загрузки лежат внутри запекаемого дерева кода, и без этой строки первый же
# docker pull их потерял бы.
volumes:
  uploads: /var/www/laravel_nginx_laravel/storage/app/public

labels:
  traefik.enable: "${TRAEFIK_ENABLE:-true}"
```

Обратите внимание, чего здесь **нет**: ни списка образов, ни портов, ни маунтов, ни
команд запуска. Всё это уже написано в compose-файлах, и повторять его — значит завести
вторую копию, которая разъедется с первой.

### Что показывает `scan`

```
$ dockerbundle scan

                           Discovered 6 service(s)
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ Service               ┃ Image           ┃ Recipe        ┃ Placement        ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ traefik               │ traefik:latest  │ traefik       │ separate service │
│ mysql                 │ mysql:8.0       │ mysql         │ in image         │
│ laravel_nginx_laravel │ php:fpm-alpine  │ laravel       │ in image         │
│ laravel_nginx_nginx   │ nginx:alpine    │ nginx         │ in image         │
│ vue_nginx_vite_vue    │ node:lts-alpine │ node-frontend │ in image         │
│ vue_nginx_vite_nginx  │ nginx:alpine    │ nginx         │ in image         │
└───────────────────────┴─────────────────┴───────────────┴──────────────────┘
Features: mysql=on
```

`laravel` подобран рецептом `laravel`, а не `php-fpm`, потому что рецепт `laravel` имеет
приоритет выше и совпал по имени сервиса. Образ у него — `php:fpm-alpine`: сервис
собирается из своего Dockerfile, и базовый образ вытащен из его `FROM` с подстановкой
build-аргументов.

### Что появляется в `dist/`

```
$ dockerbundle generate

! vue_nginx_vite_nginx: port 80 taken by laravel_nginx_nginx; moved to 20000
! traefik: kept outside the image — Traefik reads the Docker socket to discover
  containers and owns :80/:443 on the host. A reverse proxy that routes to the
  bundle cannot be one of the processes inside it.
Generated /tmp/shop/dist
```

```
dist/
├── Dockerfile                 сборка образа
├── supervisord.conf           кто и в каком порядке запускается
├── entrypoint.sh              трёхфазный старт
├── healthcheck.sh             HEALTHCHECK образа
├── docker-compose.yml         прослойка для прода: бандл + traefik
├── .env.example               слитые переменные всех пакетов
├── .dockerignore
├── docker-bundle.lock.yml     что получилось: порты, программы, переименования
└── context/                   всё, на что ссылается COPY
    ├── _bundle/
    ├── laravel_nginx_laravel/
    ├── laravel_nginx_nginx/
    ├── mysql/
    ├── vue_nginx_vite_nginx/
    └── vue_nginx_vite_vue/
```

`dist/` самодостаточен: `docker build dist/` работает из любого каталога и не тянет в
контекст сборки остальной репозиторий. Его коммитят — тогда каждое изменение стенда
видно в дифе.

---

## Разбор сгенерированного

### Dockerfile

Один слой пакетов на весь бандл — объединение того, что запросили все рецепты:

```dockerfile
ARG BASE_IMAGE=debian:bookworm-slim
FROM ${BASE_IMAGE} AS bundle

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl procps tini supervisor netcat-openbsd \
        default-mysql-server default-mysql-client \      ← рецепт mysql
        php-fpm php-cli php-mysql php-mbstring ... \     ← рецепт php-fpm
        cron \                                           ← рецепт cron
        nginx-light gettext-base openssl \               ← рецепт nginx
        iproute2 \                                       ← ваш +install
        nodejs npm \                                     ← рецепт node-frontend
    && rm -rf /var/lib/apt/lists/*
RUN curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer
RUN rm -f /etc/php/*/fpm/pool.d/www.conf
RUN rm -f /etc/nginx/sites-enabled/default
```

Дальше — по блоку на сервис. Каждый блок ставит конфиги туда, где их ждёт **общий**
рантайм, а не туда, где они лежали в отдельном контейнере:

```dockerfile
# ---- mysql (mysql) --------------------------------------
COPY context/mysql/mysql.cnf /etc/mysql/conf.d/mysql.cnf

# ---- laravel_nginx_laravel (laravel) --------------------
COPY context/laravel_nginx_laravel/php.ini    /etc/php/conf.d/laravel_nginx_laravel-custom.ini
COPY context/laravel_nginx_laravel/opcache.ini /etc/php/conf.d/laravel_nginx_laravel-opcache.ini
COPY context/laravel_nginx_laravel/crontab    /etc/cron.d/laravel_nginx_laravel
COPY context/laravel_nginx_laravel/data       /var/www/laravel_nginx_laravel
COPY context/laravel_nginx_laravel/entrypoint.sh /usr/local/bin/entrypoint-laravel_nginx_laravel.sh
RUN sed -i -e 's#/var/www/html#/var/www/laravel_nginx_laravel#g' ... /etc/cron.d/laravel_nginx_laravel

# ---- vue_nginx_vite_nginx (nginx) -----------------------
COPY context/vue_nginx_vite_nginx/nginx.conf /etc/nginx/templates/vue_nginx_vite_nginx.conf.template
RUN sed -i -E 's/^([[:space:]]*)listen[[:space:]]+80([^0-9]|$)/\1listen 20000\2/' \
    /etc/nginx/templates/vue_nginx_vite_nginx.conf.template || true
```

Здесь видно всю механику склейки:

- **`php.ini` уехал** из `/usr/local/etc/php/conf.d/custom.ini` (путь официального образа
  `php:fpm`) в `/etc/php/conf.d/laravel_nginx_laravel-custom.ini` — путь дебиановского
  php-fpm. Плюс в имя добавлен слаг, чтобы второй PHP-сервис не затёр первый.
- **Код уехал** из общего `/var/www/html` в `/var/www/laravel_nginx_laravel`. Двум
  сервисам нельзя жить в одном каталоге.
- **`crontab` стал `/etc/cron.d/`-файлом**: у системного cron другой формат — в строке
  появляется колонка пользователя, а пути внутри переписаны под новое место кода.
- **`listen 80` стал `listen 20000`** у второго nginx — тем механизмом, который объявил
  его рецепт (`port.configure.type: nginx_conf`). У первого строка тоже прогоняется через
  `sed`, но остаётся `80`.

Заканчивается всё общей обвязкой:

```dockerfile
COPY context/_bundle/supervisord.conf /etc/supervisord.conf
COPY context/_bundle/entrypoint.sh    /usr/local/bin/bundle-entrypoint.sh
COPY context/_bundle/healthcheck.sh   /usr/local/bin/bundle-healthcheck.sh

EXPOSE 80 3306 5173 9000 20000

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD /usr/local/bin/bundle-healthcheck.sh

# tini reaps the zombies that cron, composer and npm leave behind.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bundle-entrypoint.sh"]
```

### supervisord.conf

Пять `[program:...]`, упорядоченных по `priority`. Порядок выведен из базовых значений в
рецептах, уточнённых топологической сортировкой `depends_on`:

```ini
[program:mysql]
command=mysqld --user=root
priority=10
autostart=true                                       ← фаза 1, поднимается сразу
stopsignal=TERM
stopwaitsecs=60                                      ← базе дают закрыться
environment=EXTERNAL_ACCESS="%(ENV_EXTERNAL_ACCESS_DB)s"

[program:php-fpm]
command=php-fpm -F -y /etc/php/fpm/php-fpm.conf
priority=30
autostart=false                                      ← фаза 3, после инициализации

[program:vue_nginx_vite_vue]
command=sh -c 'cd /var/www/vue_nginx_vite_vue && npm run dev -- --host 0.0.0.0 --port 5173'
priority=45
autostart=false
environment=EXTERNAL_ACCESS="%(ENV_EXTERNAL_ACCESS_VITE)s",TRAEFIK_DOMAIN="%(ENV_DOMAIN_ADMIN)s"

[program:queue-laravel_nginx_laravel]
command=php /var/www/laravel_nginx_laravel/artisan queue:work --sleep=3 --tries=3
priority=60
numprocs=%(ENV_LARAVEL_REPLICAS)s                    ← масштабируется: порт не занимает
process_name=%(program_name)s_%(process_num)02d

[program:nginx]
command=nginx -g "daemon off;"
priority=61
stopsignal=QUIT
```

Три вещи стоит заметить:

1. **`nginx` один на весь бандл**, хотя nginx-сервисов два. Рецепт объявляет
   `shared: nginx`: пакеты поставляют не целые конфиги, а фрагменты `server { }`, что и
   есть содержимое `conf.d/`. Второй мастер только дрался бы за pid-файл. То же у
   php-fpm: один мастер, по пулу на сервис.
2. **`environment=...%(ENV_EXTERNAL_ACCESS_DB)s`** — вот как работает переименование.
   Во внешнем `.env` ключ называется `EXTERNAL_ACCESS_DB`, а процесс `mysql` получает его
   под именем `EXTERNAL_ACCESS`, как и ожидает.
3. **`numprocs` только у очереди.** Реплики возможны лишь для процессов, не занимающих
   порт; попытка отмасштабировать nginx дала бы `EADDRINUSE`, и сборщик об этом
   предупреждает.

### entrypoint.sh: три фазы старта

Порядок запуска — это ещё не готовность. Собственный `entrypoint.sh` laravel хочет
мигрировать базу, а база — процесс в этом же контейнере. Поэтому старт разбит на три
фазы:

```sh
# ---- phase 1: data services -------------------------------------------------
/usr/bin/supervisord -c /etc/supervisord.conf &
SUPERVISORD_PID=$!
trap 'kill -TERM "$SUPERVISORD_PID"' TERM INT
# ждём, пока supervisord ответит по своему сокету
for _ in $(seq 1 30); do supervisorctl -s "$SOCK" pid >/dev/null 2>&1 && break; sleep 1; done
wait_for tcp 3306 90 "mysql" || true          ← readiness-проба из рецепта mysql

# ---- phase 2: service initialisation ----------------------------------------
# -- laravel_nginx_laravel
(
    EXTERNAL_ACCESS="${EXTERNAL_ACCESS_PHP}"; export EXTERNAL_ACCESS
    TRAEFIK_DOMAIN="${DOMAIN_SITE}"; export TRAEFIK_DOMAIN
    cd /var/www/laravel_nginx_laravel 2>/dev/null || true
    run_init laravel_nginx_laravel /usr/local/bin/entrypoint-laravel_nginx_laravel.sh
)
# -- laravel_nginx_nginx
cp -f /etc/nginx/templates/laravel_nginx_nginx.conf.template /tmp/nginx.conf.template
(
    EXTERNAL_ACCESS="${EXTERNAL_ACCESS_SITE}"; export EXTERNAL_ACCESS
    run_init laravel_nginx_nginx /usr/local/bin/entrypoint-laravel_nginx_nginx.sh
)
if [ -f /etc/nginx/conf.d/default.conf ]; then
    mv -f /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/laravel_nginx_nginx.conf
fi
# ... то же для vue_nginx_vite_* ...

# ---- phase 3: application processes -----------------------------------------
supervisorctl -s "$SOCK" start php-fpm vue_nginx_vite_vue queue-... nginx
wait "$SUPERVISORD_PID"
```

Что здесь происходит:

- **Фаза 1** поднимает только сервисы данных (`priority < 30`) и ждёт их readiness-пробы.
- **Фаза 2** выполняет собственные `entrypoint.sh` пакетов — **целиком и без правок**,
  ровно как задумал автор пакета. Вокруг каждого выставляются переменные под теми
  именами, которые скрипт ожидает (`EXTERNAL_ACCESS`, `TRAEFIK_DOMAIN`), и рабочий
  каталог — тот, куда реально уехал код.
- Здесь же виден трюк с **`/tmp/nginx.conf.template`**: оба пакета читают этот жёстко
  зашитый путь. Рецепт nginx подкладывает туда шаблон *нужного* сервиса непосредственно
  перед его entrypoint, а результат (`conf.d/default.conf` — тоже одно имя на всех) сразу
  после переименовывает в `conf.d/<слаг>.conf`. Так два nginx уживаются, и ни один их
  скрипт не тронут.
- **Фаза 3** отпускает прикладные процессы.
- Маркер `.done` в `/var/lib/bundle/init` делает повторные старты мгновенными:
  `composer install` и `npm ci` происходят один раз. Из-за этого **первый старт на проде
  может быть долгим и требовать сети**.

### .env.example

Все `.env.example` пакетов слиты в один файл, разбитый на секции:

```ini
# ---- global --------------------------------------------------------------------
NETWORK=network
TRAEFIK_ENABLE=true

# ---- shared --------------------------------------------------------------------
APP_URL=http://${TRAEFIK_DOMAIN}
DB_DATABASE=db
DB_PASSWORD=password
TRAEFIK_DOMAIN=localhost

# ---- laravel_nginx_laravel -----------------------------------------------------
DOMAIN_SITE=localhost
EXTERNAL_ACCESS_PHP=127.0.0.1:8081:80

# ---- laravel_nginx_nginx -------------------------------------------------------
EXTERNAL_ACCESS_SITE=127.0.0.1:8081:80

# ---- manual --------------------------------------------------------------------
DB_HOST=127.0.0.1
VITE_API_BASE_URL=/api

# ---- mysql ---------------------------------------------------------------------
EXTERNAL_ACCESS_DB=127.0.0.1:3306:3306
MYSQL_USER=mysql

# ---- vue_nginx_vite_nginx ------------------------------------------------------
EXTERNAL_ACCESS_ADMIN=127.0.0.1:8082:80

# ---- vue_nginx_vite_vue --------------------------------------------------------
DOMAIN_ADMIN=localhost
EXTERNAL_ACCESS_VITE=127.0.0.1:8082:80
```

- `shared` — ключи, которые все пакеты определяют **одинаково**; они просто перенесены.
- `global` — объявленные вами в `globals:`; расхождение в них не считается конфликтом.
- `manual` — ваши литералы из `value:`.
- Секции по слагам — то, что развело `per_service`.

Комментарии из исходных `.env.example` сохранены и едут вместе со своими ключами.

### docker-compose.yml

Прослойка для прода: один сервис-бандл плюс всё, что запечь не удалось.

```yaml
services:
  shop:
    image: ${BUNDLE_IMAGE:-ghcr.io/acme/shop-bundle:latest}
    container_name: shop${INSTANCE:-}
    env_file: .env
    ports:
      - "${EXTERNAL_ACCESS_DB}"        ← переменная, а не её текущее значение
      - "127.0.0.1:9000:9000"
      - "${EXTERNAL_ACCESS_SITE}"
      - "${VITE_ACCESS}"
      - "127.0.0.1:20000:20000"        ← литерал: порт переехал, переменная
    volumes:                              описывала бы отображение, которого нет
      - mysql_data:/var/lib/mysql
      - uploads:/var/www/laravel_nginx_laravel/storage/app/public
    healthcheck:
      test: ["CMD", "/usr/local/bin/bundle-healthcheck.sh"]
    labels:
      - "traefik.enable=${TRAEFIK_ENABLE:-true}"

  # Not baked: kept outside the image by its recipe
  traefik:
    image: traefik:latest
    ports: ["80:80", "443:443"]
    volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro", "./data:/letsencrypt"]

volumes:
  mysql_data:
  uploads:
```

Публикация портов переносится **записью, а не значением**: иначе хост-порт застыл бы в
образе и `.env` развёртывания его бы уже не сдвинул — а это единственное, ради чего этот
файл существует. Как только порт переехал из-за конфликта, запись становится литералом.

Метки сервисов тоже переезжают на контейнер бандла, с подстановкой назначенного порта в
`traefik.http.services.*.loadbalancer.server.port`.

### docker-bundle.lock.yml

Что получилось — в одном месте, чтобы не читать Dockerfile:

```yaml
generator: {version: 0.2.0, format: 2}
bundle: {name: shop, variant: cpu, image: ghcr.io/acme/shop-bundle:latest}
features:
  mysql: true                          ← с какими флагами собрано
ports:
  vue_nginx_vite_nginx:
  - {original: 80, assigned: 20000, moved: true}
programs:
- {name: mysql, priority: 10, autostart: true,  scalable: false, critical: true}
- {name: nginx, priority: 61, autostart: false, scalable: false, critical: true}
baked:    [laravel_nginx_laravel, laravel_nginx_nginx, mysql, vue_nginx_vite_nginx, vue_nginx_vite_vue]
sidecars: [traefik]
external: []
volumes:
  mysql_data: /var/lib/mysql
  uploads: /var/www/laravel_nginx_laravel/storage/app/public
env_renames:
  laravel_nginx_laravel: {EXTERNAL_ACCESS: EXTERNAL_ACCESS_PHP, TRAEFIK_DOMAIN: DOMAIN_SITE}
context_digest: sha256:0f446c327b...
```

---

## Рецепты

Рецепт отвечает на один вопрос: **«как этот рантайм живёт внутри общего образа»**.
Compose-файл говорит, какой образ запустить; внутри бандла образа больше нет — есть общая
база, в которую надо поставить пакеты, положить конфиги в правильные места и запустить
процесс.

Встроенных рецептов два десятка (`recipes/builtin/`), и они покрывают обычный набор:
nginx, php-fpm, laravel, node, mysql/mariadb/postgres, redis, kafka, qdrant, ollama,
traefik, cron. Список **не ограничивает** сборщик: свой рецепт пишется прямо в
`docker-bundle.yml`, а если рецепта нет вообще — сработает автоматический.

### Анатомия

Полный рецепт на примере встроенного nginx:

```yaml
recipes:
  nginx:
    priority: 60                       # кто выигрывает при равном счёте подбора

    match:                             # как рецепт узнаёт свой сервис
      image: ["nginx", "nginx:*", "*/nginx", "*/nginx:*"]
      files: ["nginx.conf"]
      command: ["nginx"]

    families: [debian, alpine]         # на каких базах умеет работать

    install:                           # пакеты в общий слой образа
      debian: [nginx-light, gettext-base, openssl]
      alpine: [nginx, gettext, openssl]

    run:                               # RUN один раз на весь образ
      debian: ["rm -f /etc/nginx/sites-enabled/default"]

    shared: nginx                      # один мастер на все nginx-сервисы

    copy:                              # что запечь и куда
      - src: nginx.conf                # путь относительно каталога сервиса
        dest: /etc/nginx/templates/{slug}.conf.template
        kind: config
        optional: true

    port:
      default: 80
      configure:                       # чем переписать порт при конфликте
        type: nginx_conf
        file: /etc/nginx/templates/{slug}.conf.template

    post_copy:                         # RUN сразу после COPY этого сервиса
      - "sed -i -E 's/^([[:space:]]*)listen[[:space:]]+80([^0-9]|$)/\\1listen {port}\\2/' /etc/nginx/templates/{slug}.conf.template || true"

    pre_init:                          # в entrypoint, перед скриптом сервиса
      - "cp -f /etc/nginx/templates/{slug}.conf.template /tmp/nginx.conf.template 2>/dev/null || true"

    post_init:                         # в entrypoint, сразу после него
      - "if [ -f /etc/nginx/conf.d/default.conf ]; then mv -f /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/{slug}.conf; fi"

    supervisor:                        # что запускать
      - name: nginx
        command: nginx -g "daemon off;"
        priority: 40
        stopsignal: QUIT
        stopwaitsecs: 15
        scalable: false                # порт занимает — вторая копия упадёт
        critical: true                 # healthcheck требует RUNNING

    readiness:                          # чем проверять готовность
      - {type: tcp, target: "{port}", timeout: 30}

    mount_kinds:                        # как трактовать маунты сервиса
      "/etc/nginx/*": config
      "/tmp/nginx.conf.template": skip  # подкладывается pre_init'ом
      "/var/www/*": code
```

В строках доступны подстановки `{slug}`, `{port}`, `{name}`, `{package}`, `{prefix}`,
`{image}`. Всё остальное — включая `${VAR:-default}` и `%(program_name)s` — остаётся как
написано.

| Секция | Когда выполняется |
|---|---|
| `install`, `run` | один раз на весь образ, в общем слое |
| `copy`, `post_copy` | на сборке, по блоку на сервис |
| `pre_init`, `post_init` | при **каждом** старте контейнера, вокруг `entrypoint.sh` сервиса |
| `supervisor`, `readiness` | при старте, через supervisord |

### Как рецепт находит свой сервис

Каждый рецепт получает очки; побеждает набравший больше, при равенстве — больший
`priority`.

| Совпало | Очки |
|---|---|
| `image` | +100, а **не**совпадение отменяет рецепт целиком |
| `service` (имя сервиса в compose) | +20 |
| `package` (имя пакета) | +15 |
| `files` (файл рядом с сервисом) | +10 |
| `path` (путь до каталога сервиса) | +10 |
| `env` (переменная, объявленная сервисом) | +5 |
| `command` | +5 |

Вето по образу — важная часть: `php-fpm` никогда не должен захватить Postgres. Зато
*отсутствие* образа вето не даёт, так что сервис, у которого образ не разрешился, всё
ещё подбирается по имени или файлам.

Подбор можно не гадать, а назначить: `services.<слаг>.recipe: nginx`.

### `extends:` — достроить, а не переписать

Рецепты складываются в три слоя: встроенные → `recipe_paths:` → `recipes:` из вашего
файла. Одноимённый рецепт сверху **заменяет** нижний целиком; с `extends:` —
**дополняет**. Правило одно: обычный ключ заменяет, `+ключ` дописывает.

```yaml
recipes:
  nginx:
    extends: nginx
    families: [debian]                    # заменяет
    +install: {debian: [iproute2]}        # дописывает к списку встроенного
    copy:                                 # заменяет весь copy целиком
      - {src: services/nginx/configs, dest: /etc/nginx/configs, optional: false}
    +post_copy:                           # дописывает в конец
      - "sed -i 's|proxy_pass http://playwright[^:]*:|proxy_pass http://127.0.0.1:|g' /etc/nginx/configs/nginx.playwright.conf"
    pre_init: []                          # пустой список снимает шаги родителя
    post_init: []
    +mount_kinds: {"/var/www/html": skip}
```

| Форма | Поведение |
|---|---|
| ключ не указан | наследуется |
| `install: {...}` | заменяет родительский целиком |
| `+install: {debian: [...]}` | дописывает по семействам |
| `+copy`, `+post_copy`, `+pre_init`, `+post_init`, `+supervisor`, `+readiness`, `+run` | дописывает в конец |
| `+match`, `+mount_kinds` | сливает по ключам |
| `pre_init: []` | явно очищает |

Порядок рецептов внутри слоя роли не играет. Цикл `extends:` и ссылка на несуществующее
имя — ошибки.

Рецепт можно вынести в отдельный файл, если он длинный или общий для нескольких стендов:

```yaml
recipe_paths: [../shared-recipes, ./ci/vnu.yml]
```

### Взять кусок чужого образа

Чтобы не тащить целую файловую систему ради одного каталога:

```yaml
recipes:
  vnu:
    match: {image: ["ghcr.io/validator/validator:*"]}
    copy:
      - src: /vnu-runtime-image
        dest: /opt/vnu-runtime-image
        from_image: "{image}"          # {image} — образ самого сервиса
        optional: false
    supervisor:
      - name: vnu
        command: /opt/vnu-runtime-image/bin/java -m vnu/nu.validator.servlet.Main {port}
```

Такой образ объявляется отдельной стадией и **не** вливается в базу: из него берётся
только названное.

### Если рецепта нет

Генерация не упирается в тупик. Файловая система исходного образа импортируется в бандл,
а запускается его собственный `ENTRYPOINT`/`CMD`. Импорт идёт слиянием — файлы базы
сохраняются, недостающие пользователи дописываются, — иначе чужой `/etc/passwd` сломал бы
все остальные сервисы.

Платить за это приходится дважды: образ получается толстым, и порт такого сервиса
**сдвинуть нельзя** — неизвестно, где он настраивается. Конфликт портов с ним
разрешается вручную через `services.<слаг>.ports`. Если из образа нужен один каталог,
дешевле написать рецепт с `copy.from_image`.

---

## Как разрешаются конфликты

| Конфликт | Решение по умолчанию | Как переопределить |
|---|---|---|
| два сервиса на одном порту | первый оставляет порт, второй переезжает в `port_range`, конфиг переписывается механизмом из рецепта | `services.<слаг>.ports: {80: 8080}` |
| порт у сервиса, чей рецепт не умеет его двигать | **ошибка** | пришпилить порт вручную или убрать один из сервисов |
| одна переменная, разные значения | **ошибка** | `env:` — `prefix`, `keep:<слаг>`, `value:<литерал>`, `per_service` |
| два разных сервиса на один слаг | **ошибка** | `sources[].prefix` |
| две метки с одним ключом | берётся первая, выдаётся предупреждение | `labels:` в конфигурации |
| `shm_size` | так же | — |
| маунт непонятного назначения | остаётся томом | `services.<слаг>.mounts: {/path: copy}` |
| именованный том | всегда том | — |
| сокет | пропускается | — |
| состояние без своего маунта | теряется | `volumes:` в конфигурации |

Маунты классифицируются на **config** / **code** / **state**. Конфиги и код запекаются
через `COPY`, состояние (`/var/lib/mysql`, `/data`, кэш моделей) остаётся томом.
Непонятное по умолчанию остаётся томом: запечь то, чего мы не поняли, хуже, чем лишний
том.

> **Известная шероховатость.** Если два сервиса монтируют разные каталоги в **один и тот
> же** путь и рецепт не переносит их в отдельные места (как это делает php-fpm через
> `from_mount`), оба `COPY` пишут по одному адресу и второй затирает первый. В примере
> выше это два `COPY ... /var/www/html` от двух nginx. Обходится через
> `services.<слаг>.mounts: {/var/www/html: skip}` для одного из них.

### Переименование переменных

Все четыре формы делают одно и то же: разводят ключ по сервисам так, чтобы приложение
продолжало читать привычное имя.

```yaml
env:
  # 1. автоматический префикс: LARAVEL_EXTERNAL_ACCESS, NGINX_EXTERNAL_ACCESS
  EXTERNAL_ACCESS: prefix

  # 2. взять значение одного сервиса для всех
  DB_HOST: keep:mysql

  # 3. задать литерал
  VITE_API_BASE_URL: value:/api

  # 4. имена вручную
  TRAEFIK_DOMAIN:
    per_service:
      site:  DOMAIN_SITE
      admin: DOMAIN_ADMIN
```

Сервис, не перечисленный в `per_service`, оставляет ключ под исходным именем. Если после
этого оставшиеся всё ещё расходятся — ключ по-прежнему конфликт. Два сервиса на одно
внешнее имя — ошибка: одна строка `.env` не удержит два значения.

Отдельно — `local:`, для адресов соседей по бандлу:

```yaml
env:
  VNU_URL: value:http://{local:vnu}    # -> http://127.0.0.1:8888
  DB_ADDR: local:mysql                 # -> 127.0.0.1:3306
```

`{local:<слаг>}` подставляет **назначенный** порт, а не написанный в исходном compose.
Литерал, вбитый руками, разъедется в тот момент, когда распределитель портов сдвинет
сервис, и никто об этом не узнает. Обратите внимание: подставляется `хост:порт`, поэтому
для переменной, которая ждёт только хост (`DB_HOST`), нужен `value:127.0.0.1`.

---

## Флаги сборки

Один стенд редко имеет одну форму. `features:` держит обе в файле и записывает, какая
собрана:

```yaml
features:
  mysql: true
  gpu: false

sources:
  - {type: compose, path: ../mysql/docker-compose.yml, when: mysql}

services:
  mysql: {when: mysql}
  gpu_worker: {when: [gpu, "!mysql"]}

volumes:
  models: {path: /root/.ollama, when: gpu}

env:
  DB_HOST: {rule: value:127.0.0.1, when: mysql}
```

`when:` принимает имя, отрицание `!имя` или список (все сразу) и работает на `sources:`,
`services:`, `volumes:`, `labels:`, `env:`, а внутри рецептов — на `copy:`, `supervisor:`,
`readiness:` и шагах `post_copy` / `pre_init` / `post_init` / `run`
(`{cmd: ..., when: ...}`).

Имя, не объявленное в `features:`, — **ошибка**, а не молчаливое «выключено»: опечатка
иначе выкинула бы сервис из образа, ни слова об этом не сказав.

```bash
dockerbundle generate --disable mysql --enable gpu
```

Итоговые значения пишутся в `docker-bundle.lock.yml`.

### `mode:` — что делать с сервисом

```yaml
services:
  app_nginx: {}                  # bake — по умолчанию
  traefik:   {mode: sidecar}     # соседним сервисом в сгенерированном compose
  mysql:     {mode: external}    # снаружи, но ключи .env остаются
  network:   {mode: off}         # выкинуть вместе с ключами
```

`external` — про базу, которая уже крутится отдельно: в образ она не попадает, а
`DB_HOST`, `DB_PORT` и пароль по-прежнему настраиваются из `.env` развёртывания. `off` —
про то, чего в этом стенде нет вовсе.

---

## Ограничения

- **Один рантайм на образ.** Общий рантайм ставится один раз, из пакетов базового
  образа: бандл получает дебиановский PHP независимо от того, какой тег назвал исходный
  compose. Сервис под `php:7.4-fpm-alpine` и сервис под `php:8.3-fpm-alpine` в одном
  образе оба поедут на версии из базы. Сборщик об этом предупреждает.
- **Одно семейство базы.** `postgresql-dev`, `oniguruma-dev`, `mariadb-dev` из
  alpine-пакетов на debian-базе не существуют — пакеты перечисляются по семействам.
- **Имена контейнеров как хосты не работают.** `http://hf-audio:8000`, `DB_HOST=postgres`,
  `proxy_pass http://playwright${INSTANCE}:` внутри бандла не резолвятся: сервисы делят
  network namespace. Для переменных это закрывает `local:`; для зашитого в конфиги —
  `post_copy` с `sed`.
- **Только `linux/amd64`.**
- **rootfs-fallback даёт толстый образ** и не умеет менять порт сервиса.
- **Логи не префиксуются** в общем stdout: обёртка `cmd | sed 's/^/[svc] /'` сделала бы
  `sed` процессом, который отслеживает supervisord, портя коды выхода и `autorestart`.
  Per-service вывод — через `dockerbundle logs <контейнер> <программа>`.

---

## Установка

Утилита — самодостаточный бинарь: ни Python, ни зависимостей на целевой машине не нужно.

```bash
curl -fsSL https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/vX.Y.Z/install.sh | sh -s -- vX.Y.Z
```

Windows:

```powershell
irm https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/vX.Y.Z/install.ps1 | iex
```

Тег обязателен. Скрипт без него падает с подсказкой — это намеренно: стенд, который молча
пересобирается другим генератором, ровно та беда, ради которой утилита существует.
Обновление должно быть видно как правка одной строки, а не случиться само.

Скачанное сверяется с опубликованной SHA-256 (`--no-verify` отключает). Каталог установки
— `--dir`, иначе `$RUNNER_TEMP` под GitHub Actions, иначе `~/.local/bin`.

### В CI

В целевом проекте бинарник **не хранится**. Достаточно переменных репозитория:

| Переменная | Значение |
|---|---|
| `ACTION_TRIGGER` | `release` |
| `RUNS_ON` | `ubuntu-latest` |
| `BUILD_COMMAND` | `curl -fsSL .../install.sh \| sh -s -- vX.Y.Z --dir "$RUNNER_TEMP/bin" && "$RUNNER_TEMP/bin/dockerbundle" generate --yes` |
| `PUBLISH_METHOD` | `docker` |
| `DOCKERFILE_PATH` | `dist/Dockerfile` |
| `BUILD_CONTEXT` | `dist` |

`dist/` при этом остаётся в `.gitignore`: он генерируется на каждом прогоне. Подробности,
готовый шаблон workflow и composite action для собственных workflow —
[`.github/workflow-templates/README.md`](.github/workflow-templates/README.md).

## Команды

| Команда | Назначение |
|---|---|
| `init` | Создать `docker-bundle.yml` |
| `scan` | Что нашлось, какой рецепт подобрался, что с ним будет |
| `generate` | `docker-bundle.yml` → `dist/` |
| `wizard` | TUI-редактор конфигурации |
| `doctor` | Проверить окружение |
| `ps` / `restart` / `logs` | Управлять отдельным сервисом внутри запущенного бандла |

```bash
dockerbundle ps      shop-bundle
dockerbundle restart shop-bundle nginx
dockerbundle logs    shop-bundle php-fpm
```

Путь к конфигурации — `--config` / `-c`. Подробности по внутреннему устройству,
разработке и CI — в [`docs/README.md`](docs/README.md).

## Лицензия

MIT.
