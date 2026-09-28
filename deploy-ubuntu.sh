#!/usr/bin/env bash
# Run from a trusted checkout on the target Ubuntu 24.04/26.04 server.
set +x
set -Eeuo pipefail
umask 077

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
usage() {
    printf 'Запуск: sudo bash deploy-ubuntu.sh demo.example.org [--external-routing|--local-routing]\n'
    printf 'Повторный запуск обновляет приложение и сохраняет пароль.\n'
}
if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
[[ $# -ge 1 && $# -le 2 ]] || { usage; exit 1; }
case ${2:-} in
    --external-routing) routing_mode=external ;;
    --local-routing) routing_mode=local ;;
    '') routing_mode='' ;;
    *) usage; die 'Неизвестный флаг.' ;;
esac
prepare_timeout=${PREPARE_TIMEOUT_SECONDS:-14400}
[[ $prepare_timeout =~ ^[1-9][0-9]{0,5}$ ]] || die 'PREPARE_TIMEOUT_SECONDS должен быть положительным целым числом до 999999.'
trap 'exit 130' INT
trap 'exit 143' TERM
domain=${1,,}
[[ ${#domain} -le 253 && $domain == *.* && ! $domain =~ ^[0-9.]+$ ]] || die 'Нужен домен, без https://, порта и пути.'
IFS=. read -ra labels <<< "$domain"
for label in "${labels[@]}"; do
    [[ ${#label} -le 63 && $label =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die 'Некорректный домен. Для кириллицы используй punycode.'
done
[[ $domain != *. ]] || die 'Укажи домен без завершающей точки.'
[[ $EUID == 0 ]] || die 'Запусти скрипт через sudo.'
[[ -r /etc/os-release ]] || die 'Нужна Ubuntu 24.04 или 26.04.'
# shellcheck disable=SC1091
. /etc/os-release
[[ $ID == ubuntu ]] || die 'Этот скрипт рассчитан на Ubuntu 24.04 или 26.04.'
case "$VERSION_ID" in
    24.04) docker_suite=noble ;;
    26.04) docker_suite=resolute ;;
    *) die "Неподдерживаемая версия Ubuntu: $VERSION_ID. Поддерживаются 24.04 и 26.04." ;;
esac

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd "$repo_dir"
[[ -f compose.production.yml && -f deploy/Caddyfile ]] || die 'Не найдены файлы развёртывания в репозитории.'
state_dir="$repo_dir/.local/deploy"
mkdir -p "$state_dir"
chown root:root "$state_dir"
chmod 700 "$state_dir"
exec 9>"$state_dir/deploy.lock"
flock -n 9 || die 'Другой запуск развёртывания ещё работает.'
if [[ -z $routing_mode ]]; then
    routing_mode=local
    if [[ -f $state_dir/routing-mode ]]; then routing_mode=$(cat "$state_dir/routing-mode"); fi
fi
case $routing_mode in
    external) compose_file="$repo_dir/compose.external.yml" ;;
    local) compose_file="$repo_dir/compose.production.yml" ;;
    *) die 'Некорректный сохранённый режим маршрутизации.' ;;
esac
[[ -f $compose_file ]] || die "Нет конфигурации $compose_file. Обнови весь репозиторий."
trap 'printf "Развёртывание не завершено (строка %s). Исправь причину и повтори запуск; пароль сохранится.\n" "$LINENO" >&2' ERR

printf 'Подготовка Ubuntu и Docker…\n'
if ! command -v curl >/dev/null || ! command -v openssl >/dev/null || [[ ! -f /etc/ssl/certs/ca-certificates.crt ]]; then
    apt-get update
    apt-get install -y ca-certificates curl openssl
fi
if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $docker_suite
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    chmod 644 /etc/apt/sources.list.d/docker.sources
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
docker compose version >/dev/null 2>&1 || die 'Нужен Docker Compose plugin >= 2.24.4. Установи его из репозитория Docker.'
compose_version=$(docker compose version --short)
compose_version=${compose_version#v}
dpkg --compare-versions "$compose_version" ge 2.24.4 || die 'Обнови Docker Compose до версии >= 2.24.4.'
systemctl enable --now docker
docker info >/dev/null

if command -v ufw >/dev/null && LC_ALL=C ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp
    ufw allow 443/tcp
fi
printf 'DNS домена должен указывать на этот сервер. В панели хостинга открой TCP 80 и 443.\n'

# Do not source files containing credentials or expose passwords in process arguments.
password_file="$state_dir/password"
if [[ ! -f $password_file ]]; then
    openssl rand -hex 24 > "$password_file.tmp"
    mv "$password_file.tmp" "$password_file"
fi
chmod 600 "$password_file"
chown root:root "$password_file"
password=$(cat "$password_file")
[[ $password =~ ^[0-9a-f]{48}$ ]] || die "Некорректный файл пароля: $password_file"
docker pull caddy:2-alpine
password_hash=$(printf '%s\n' "$password" | docker run --rm -i --network none caddy:2-alpine caddy hash-password)
[[ $password_hash == '$2'* && $password_hash != *$'\n'* ]] || die 'Caddy не вернул bcrypt-хеш.'
env_file="$state_dir/production.env"
printf "FIELDROUTE_DOMAIN='%s'\nFIELDROUTE_PASSWORD_HASH='%s'\n" "$domain" "$password_hash" > "$env_file.tmp"
mv "$env_file.tmp" "$env_file"
printf 'URL: https://%s\nЛогин: jury\nПароль: %s\n' "$domain" "$password" > "$state_dir/credentials.txt"
chmod 600 "$env_file" "$state_dir/credentials.txt"
chown root:root "$env_file" "$state_dir/credentials.txt"
# Shell environment takes precedence over --env-file; discard inherited values.
unset FIELDROUTE_DOMAIN FIELDROUTE_PASSWORD_HASH
compose=(docker compose --project-name fieldroute-production --env-file "$env_file" -f "$compose_file")
"${compose[@]}" config --quiet
if [[ $routing_mode == external ]]; then
    printf 'Внешние маршруты: останавливаем локальную подготовку и дорожные сервисы…\n'
    docker compose --project-name fieldroute-production --env-file "$env_file" \
        -f "$repo_dir/compose.production.yml" stop map-download map-prepare geodata-prepare roads-car roads-foot roads-bicycle
fi
printf '%s\n' "$routing_mode" > "$state_dir/routing-mode"
"${compose[@]}" run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
show_diagnostics() {
    printf '\nСостояние контейнеров и последние сообщения:\n' >&2
    "${compose[@]}" ps -a >&2 || true
    "${compose[@]}" logs --no-color --tail 40 >&2 || true
    local container_id
    while IFS= read -r container_id; do
        [[ -n $container_id ]] || continue
        docker inspect --format '{{.Name}} status={{.State.Status}} exit={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} error={{.State.Error}}' "$container_id" >&2 || true
    done < <("${compose[@]}" ps -aq)
}

prepare_stage() {
    local service=$1 result container_id state
    printf '\nПодготовка: %s (лимит %s секунд). Ниже — логи контейнера.\n' "$service" "$prepare_timeout"
    # Attach to the existing service container; do not start a concurrent one-off
    # downloader that could write the same .partial file.
    if timeout --foreground --kill-after=30s "$prepare_timeout" \
        "${compose[@]}" up --no-deps --exit-code-from "$service" "$service"; then
        # Compose can return zero on interruption. Require actual job completion.
        container_id=$("${compose[@]}" ps -aq "$service")
        state=$(docker inspect --format '{{.State.Status}}:{{.State.ExitCode}}' "$container_id")
        if [[ $state == exited:0 ]]; then return 0; fi
        printf 'Этап %s не завершился успешно: %s\n' "$service" "$state" >&2
    else
        result=$?
        printf 'Ошибка этапа %s, код %s.\n' "$service" "$result" >&2
        if [[ $result == 124 || $result == 137 ]]; then
            printf 'Истёк лимит этапа. Проверь прогресс, сеть и ресурсы перед увеличением PREPARE_TIMEOUT_SECONDS.\n' >&2
        fi
    fi
    show_diagnostics
    die "Подготовка $service не завершена. Пароль и готовые данные сохранены."
}

if [[ $routing_mode == external ]]; then
    printf 'Сборка приложения без загрузки OSM и подготовки графов…\n'
    "${compose[@]}" build api
    runtime_groups=('api' 'caddy')
else
    printf 'Сборка приложения и инструмента подготовки адресов…\n'
    "${compose[@]}" build api geodata-prepare
    prepare_stage map-download
    prepare_stage map-prepare
    prepare_stage geodata-prepare
    runtime_groups=('roads-car roads-foot roads-bicycle' 'api' 'caddy')
fi

# Each dependency was explicitly completed above. Start runtime layers in order
# without rerunning the preparation jobs or sharing their deadline.
for runtime_services in "${runtime_groups[@]}"; do
    read -ra services <<< "$runtime_services"
    printf '\nЗапуск: %s\n' "$runtime_services"
    if ! "${compose[@]}" up -d --no-deps --wait --wait-timeout 300 "${services[@]}"; then
        show_diagnostics
        die "Не удалось запустить $runtime_services."
    fi
done

printf 'Проверка HTTPS и защиты API (ожидание сертификата до нескольких минут)…\n'
ready=false
for ((attempt=1; attempt<=30; attempt++)); do
    # Verify the public certificate while connecting directly to this server.
    # No -k: an invalid certificate must fail deployment verification.
    code=$(curl --noproxy '*' --resolve "$domain:443:127.0.0.1" --connect-timeout 3 --max-time 5 \
        -s -o /dev/null -w '%{http_code}' "https://$domain/api/v1/health") || code=000
    if [[ $code == 401 ]]; then ready=true; break; fi
    if [[ $code == 200 ]]; then die 'API доступен без пароля! Проверь конфигурацию Caddy.'; fi
    sleep 5
done
if [[ $ready != true ]]; then
    printf 'Контейнеры запущены, но HTTPS с защитой пока не подтверждён. Проверь A/AAAA DNS, TCP 80/443 и логи Caddy.\n' >&2
    printf 'Данные входа сохранены: %s/credentials.txt\n' "$state_dir" >&2
    exit 1
fi
authenticated_code=$(printf 'user = "jury:%s"\n' "$password" | curl --config - --noproxy '*' \
    --resolve "$domain:443:127.0.0.1" --connect-timeout 5 --max-time 15 \
    -sS -o /dev/null -w '%{http_code}' "https://$domain/api/v1/health")
[[ $authenticated_code == 200 ]] || die "Проверка с паролем вернула HTTP $authenticated_code вместо 200."
printf '\nГотово. HTTPS проверен: без пароля — 401, с паролем — 200.\n'
cat "$state_dir/credentials.txt"
printf '\nДанные входа сохранены в %s/credentials.txt (доступ только root).\n' "$state_dir"
printf 'Проверь открытие сайта с другого устройства: локальная проверка не проверяет внешний firewall.\n'
