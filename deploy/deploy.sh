#!/usr/bin/env bash
#
# Обновление бэкенда ПТО на сервере. Запускается по SSH из GitHub Actions
# (.github/workflows/deploy-backend.yml), но работает и руками.
#
#   bash deploy/deploy.sh
#   PTO_PROFILE=mock bash deploy/deploy.sh
#   PTO_DEPLOY_WAIT=1800 bash deploy/deploy.sh    # дождаться конца прогона
#
# Вся специфика проекта живёт здесь, а не в workflow: у бэкенда есть три
# особенности, из-за которых наивный «down && up» вредит.
#
#   1. Прогон документа идёт ЧАСАМИ. Перезапуск он переживает — очередь и
#      посчитанные листы лежат на диске, сервис досчитает остаток, — но лист,
#      который считался в момент перезапуска, теряется вместе с потраченными
#      на него токенами.
#   2. Клиент интерфейса терпит около 60 секунд недоступности бэкенда
#      (20 неудачных опросов раз в 3 секунды). Дольше — и открытые документы
#      покажут ошибку. Листы при этом не пропадут, но инженер увидит красное.
#      Поэтому здесь `up -d --build`, а НЕ `down` + `up`: образ собирается до
#      остановки, контейнер пересоздаётся за секунды.
#   3. Профиль обязателен. Без --profile compose ничего не поднимет и выйдет
#      с нулём: деплой «успешен», а сервиса нет.
#
# И главное: НИКОГДА не добавлять -v к `docker compose down`. Это стирает тома
# с очередью и результатами прогонов, то есть часы работы и потраченные деньги.

set -euo pipefail

BACKEND_DIR="${PTO_BACKEND_DIR:-/opt/pto/backend}"
PROFILE="${PTO_PROFILE:-real}"
REF="${PTO_DEPLOY_REF:-origin/main}"
HEALTH_URL="${PTO_HEALTH_URL:-http://127.0.0.1:8000/health}"
# Сколько ждать окончания идущего прогона перед перезапуском. 0 — не ждать.
WAIT_FOR_IDLE="${PTO_DEPLOY_WAIT:-0}"
# Сколько ждать, пока сервис поднимется после перезапуска.
HEALTH_TIMEOUT="${PTO_HEALTH_TIMEOUT:-180}"

say() { printf '\n=== %s\n' "$*"; }

health() { curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null || true; }

# Разбор /health без jq и без python: их может не быть на сервере, а ответ
# наш собственный и формы известной. Числовое поле: "processing":2
health_number() {
    # `|| true` обязателен: при set -o pipefail grep без совпадений роняет всю
    # цепочку, и скрипт умирал бы ровно в том случае, ради которого написан, —
    # когда сервис не отвечает.
    health | grep -o "\"$1\":[0-9]*" | head -1 | cut -d: -f2 || true
}

# Строковое поле: "mode":"mock". Ключ mode встречается дважды (сверху и внутри
# profile) с одним значением, поэтому head -1 достаточно.
health_string() {
    health | grep -o "\"$1\":\"[a-zA-Z0-9_-]*\"" | head -1 | cut -d: -f2 | tr -d '"' || true
}

# Без -S и со скрытым stderr: пока контейнер поднимается, curl честно ругается
# «Empty reply from server», и эти строки в логе деплоя выглядят как поломка.
service_up() { curl -fs --max-time 10 -o /dev/null "$HEALTH_URL" 2>/dev/null; }

# --- 1. Идёт ли прогон прямо сейчас -----------------------------------------
say "Проверяю, не считается ли документ"
busy="$(health_number processing)"
if [ -z "$busy" ]; then
    echo "сервис не отвечает на $HEALTH_URL — считаю, что он не поднят"
elif [ "$busy" != "0" ]; then
    echo "ВНИМАНИЕ: сейчас считается документов: $busy"
    if [ "$WAIT_FOR_IDLE" -gt 0 ]; then
        echo "жду до $WAIT_FOR_IDLE с, пока прогон закончится"
        deadline=$(( $(date +%s) + WAIT_FOR_IDLE ))
        while [ "$(date +%s)" -lt "$deadline" ]; do
            sleep 15
            busy="$(health_number processing)"
            if [ "${busy:-0}" = "0" ]; then break; fi
        done
    fi
    if [ "${busy:-0}" != "0" ]; then
        echo "перезапускаю на ходу: лист, который считается сейчас, будет"
        echo "пересчитан, остальные посчитанные листы сохранятся"
    fi
else
    echo "очередь пуста, перезапуск безопасен"
fi

# --- 2. Забрать код ----------------------------------------------------------
say "Обновляю код в $BACKEND_DIR до $REF"
cd "$BACKEND_DIR"
# origin обычно git@github.com:…, а Deploy key в репо часто ещё не добавлен.
# Actions передаёт короткий GITHUB_TOKEN на один прогон (PTO_GITHUB_TOKEN) —
# на диск его не пишем, remote не меняем.
if [ -n "${PTO_GITHUB_TOKEN:-}" ]; then
  https_url="$(git remote get-url origin | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$##').git"
  branch="${REF#origin/}"
  basic="$(printf 'x-access-token:%s' "$PTO_GITHUB_TOKEN" | base64 | tr -d '\n')"
  git -c http.https://github.com/.extraheader="AUTHORIZATION: basic ${basic}" \
      fetch --prune "$https_url" "+refs/heads/${branch}:refs/remotes/origin/${branch}"
  unset basic
  git reset --hard "origin/${branch}"
else
  git fetch --prune origin
  git reset --hard "$REF"
fi
git log --oneline -1
# .env не отслеживается git — reset его не трогает, настройки сервера целы.
[ -f .env ] || { echo "НЕТ .env на сервере: без него не будет ни HF_TOKEN, ни PTO_API_TOKEN"; exit 1; }

# --- 3. Пересобрать и поднять ------------------------------------------------
# Конвертер DWG сервер больше не компилирует — берёт готовый образ из GHCR
# (собирает его Actions, см. Dockerfile.dwgtools). Пакет приватный, как и
# репозиторий, поэтому нужен вход тем же коротким токеном, что и для git.
# Без токена (ручной запуск на сервере) вход пропускаем: если образ уже
# скачан, сборка пройдёт и так.
ghcr_logged=""
if [ -n "${PTO_GITHUB_TOKEN:-}" ]; then
  say "Вхожу в GHCR за образом конвертера"
  if printf '%s' "$PTO_GITHUB_TOKEN" \
       | docker login ghcr.io -u x-access-token --password-stdin >/dev/null; then
    ghcr_logged="да"
  else
    echo "вход не удался — если образа нет локально, сборка упадёт"
  fi
fi

say "Собираю образ и пересоздаю контейнер (профиль: $PROFILE)"
build_status=0
docker compose --profile "$PROFILE" up -d --build || build_status=$?

# Токен живёт один прогон, но оставлять его в ~/.docker/config.json незачем.
if [ -n "$ghcr_logged" ]; then
  docker logout ghcr.io >/dev/null 2>&1 || true
fi

if [ "$build_status" -ne 0 ]; then
  echo "Сборка образа не удалась (код $build_status)."
  echo "Если жалуется на ghcr.io/…/pto-dwgtools — сначала должен пройти"
  echo "job «dwgtools» в Actions: он собирает конвертер и кладёт его в GHCR."
  exit "$build_status"
fi

# --- 4. Дождаться готовности -------------------------------------------------
say "Жду /health"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
ok=""
while [ "$(date +%s)" -lt "$deadline" ]; do
    if service_up; then ok="да"; break; fi
    sleep 3
done
if [ -z "$ok" ]; then
    echo "сервис не ответил за ${HEALTH_TIMEOUT} с — смотрите логи:"
    docker compose --profile "$PROFILE" logs --tail 60
    exit 1
fi

mode="$(health_string mode)"
echo "сервис поднят, режим: $mode"
if [ "$mode" != "$PROFILE" ]; then
    echo "ВНИМАНИЕ: профиль $PROFILE, а сервис сообщает режим $mode."
    echo "Проверьте PTO_PIPELINE_MODE в .env — он перекрыт значением из compose."
fi

# --- 5. Прибраться -----------------------------------------------------------
# Каждая сборка оставляет прежний образ висеть без тега. За месяц ежедневных
# деплоев это десятки гигабайт. Тома не трогаются — только образы.
say "Убираю образы без тегов"
docker image prune -f >/dev/null || echo "не удалось, не страшно"

say "Деплой завершён"
