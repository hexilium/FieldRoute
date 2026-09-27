#!/bin/sh
set -eu
cd /data
signature="$(osrm-extract --version) $(sha256sum moscow.osm.pbf /config/shortest.lua /config/prepare.sh | sha256sum | cut -d ' ' -f 1)"
if [ -f ready ] && [ "$(cat ready)" = "$signature" ]; then
    echo 'Дорожные графы Москвы уже подготовлены.'
    exit 0
fi
rm -f ready
for profile in car foot bicycle; do
    if [ -f "$profile/ready" ] && [ "$(cat "$profile/ready")" = "$signature" ]; then
        continue
    fi
    echo "Подготовка профиля $profile…"
    mkdir -p "$profile"
    rm -f "$profile"/moscow.osrm* "$profile/ready"
    ln -f moscow.osm.pbf "$profile/moscow.osm.pbf"
    OSRM_PROFILE="$profile" osrm-extract --threads 2 -p /config/shortest.lua "$profile/moscow.osm.pbf"
    osrm-partition --threads 2 "$profile/moscow.osrm"
    osrm-customize --threads 2 "$profile/moscow.osrm"
    printf '%s\n' "$signature" > "$profile/ready"
done
printf '%s\n' "$signature" > ready.tmp
mv ready.tmp ready
echo 'Все дорожные графы Москвы готовы.'
