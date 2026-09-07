# Сборка бандла в CI

Как собрать образ стенда на раннере, **не кладя бинарник утилиты в репозиторий проекта**.

Утилита раздаётся релизами этого репозитория. Проект ставит нужную версию скриптом
`install.sh` прямо в шаге сборки и запускает `dockerbundle generate`; дальше готовый
`dist/` собирается в образ.

---

## Подключение

1. Поставь в проект тонкий `ci-cd.yml` — тот же, что раздаёт `git_toolkit`
   ([`.github/workflow-templates/ci-cd.yml`](https://github.com/cat-of-summer/Git_toolkit/blob/main/.github/workflow-templates/ci-cd.yml)).
   Ничего специфичного для dockerbundle в нём нет; если такой файл уже стоит — не трогай его.
2. Задай переменные репозитория (Settings → Secrets and variables → Actions → Variables).
3. Убери из проекта вендоренный бинарник и держи `dist/` в `.gitignore`.

## Переменные

| Переменная | Значение |
|---|---|
| `ACTION_TRIGGER` | `release` |
| `RUNS_ON` | `ubuntu-latest` |
| `BUILD_COMMAND` | см. ниже |
| `PUBLISH_METHOD` | `docker` |
| `DOCKERFILE_PATH` | `dist/Dockerfile` |
| `BUILD_CONTEXT` | `dist` |

```
BUILD_COMMAND = curl -fsSL https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/vX.Y.Z/install.sh | sh -s -- vX.Y.Z --dir "$RUNNER_TEMP/bin" && "$RUNNER_TEMP/bin/dockerbundle" generate --yes
```

Бинарь вызывается по пути, а не по имени: `GITHUB_PATH`, куда установщик дописывает
каталог, действует только на **следующие** шаги, а установка и генерация здесь — один шаг.
В отдельном шаге (например, в `CI_COMMAND`) команда `dockerbundle` уже доступна по имени.

`vX.Y.Z` в обоих местах — один и тот же тег: первый выбирает версию скрипта установки,
второй версию бинарника. **Тег обязателен**: без него скрипт падает с подсказкой. Это
намеренно — стенд, который молча пересобирается другим генератором, и есть та проблема,
ради которой всё затевалось. Обновление утилиты должно быть видно как правка переменной.

`TOOLCHAIN` не нужен: бинарник самодостаточен, Python на раннере не требуется.

## Как это работает

Джоба `ci` из `git_toolkit` выполняет `BUILD_COMMAND` и загружает **всё рабочее дерево**
артефактом. Джоба `docker-publish` распаковывает его обратно **в корень** рабочего
каталога, после чего `dist/Dockerfile` оказывается на месте и собирается с контекстом
`dist`. Поэтому коммитить `dist/` не нужно — он живёт ровно один прогон.

## Свой workflow вместо git_toolkit

Если проект не использует `git_toolkit`, ту же установку даёт composite action:

```yaml
      - uses: cat-of-summer/DockerBundle---python/.github/actions/setup-dockerbundle@vX.Y.Z
        with:
          version: vX.Y.Z
      - run: dockerbundle generate --yes
      - uses: docker/build-push-action@v7
        with:
          context: dist
          file: dist/Dockerfile
          push: true
          tags: ghcr.io/acme/stand:latest
```

## Локально

Тот же скрипт ставит утилиту на машину разработчика:

```bash
curl -fsSL https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/vX.Y.Z/install.sh | sh -s -- vX.Y.Z
```

Windows:

```powershell
irm https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/vX.Y.Z/install.ps1 | iex
```

Каталог установки — `--dir`, иначе `$RUNNER_TEMP` под Actions, иначе `~/.local/bin`.
Проверка контрольной суммы включена по умолчанию и отключается `--no-verify`.
