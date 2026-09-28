# FieldRoute

Планирование и пересчёт маршрутов выездных инженеров. В репозитории есть приложение, встроенные учебные данные и пример запроса.

## Быстрый запуск

Нужны Python 3.11+ и доступ к PyPI для установки зависимостей.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints.txt -e .
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Приложение: [http://127.0.0.1:8000](http://127.0.0.1:8000). API: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). В браузере загружается встроенный сценарий. Быстрый запуск использует приближённые расстояния Haversine и не требует дорожных данных.

Тот же режим в Docker:

```bash
docker compose up --build
```

## Проверка через API

```bash
curl http://127.0.0.1:8000/api/v1/health
curl -X POST -H 'Content-Type: application/json' --data-binary @examples/priority/specialist-chain.request.json http://127.0.0.1:8000/api/v1/plan
```

В ответе `routes` содержит маршруты, `unassigned` — неназначенные работы, `metrics` — показатели, `diagnostics` — этапы поиска. Пример показывает освобождение специалиста для заявки более высокого класса.

## Дорожные маршруты и поиск адресов

Нужны Docker с Compose 2.20+, интернет при первом запуске и около 3 ГБ свободного места для выгрузки Москвы и подготовленных графов. Остановите быстрый режим, если он занимает порт 8000.

```bash
docker compose -f compose.roads.yaml up --build
```

Данные сохраняются в `.local/roads/`; следующие запуски используют их повторно. В этом режиме расчёт использует локальные автомобильный, пеший и велосипедный графы OSRM, а поиск адресов — локальный индекс. Источник дорожных данных: © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).

[Алгоритм и сравнение с FIFO](ALGORITHM.md).

## Развёртывание с HTTPS и паролем

Ubuntu 24.04 и 26.04, из корня checkout на сервере:

```bash
sudo bash deploy-ubuntu.sh fieldroute.mooo.com --external-routing
```

Внешний режим не загружает OSM и не строит графы на VPS. Для локального режима
используй `--local-routing`. Выбранный режим и пароль сохраняются.
Перед запуском домен должен указывать на сервер, TCP 80/443 должны быть открыты.
[Подробности, ограничения публичных сервисов и управление](DEPLOYMENT.md).
