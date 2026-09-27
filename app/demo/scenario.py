from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.models import Engineer, Job, Location, PlanRequest, TimeWindow


def demo_request() -> PlanRequest:
    tz = timezone(timedelta(hours=3))
    day = datetime.now(tz).replace(hour=8, minute=0, second=0, microsecond=0)
    if day < datetime.now(tz) - timedelta(hours=10):
        day += timedelta(days=1)

    engineers = [
        Engineer(
            id="eng-anna",
            name="Анна К.",
            start_location=Location(lat=55.7518, lon=37.6176, label="Центр"),
            shift=TimeWindow(start=day, end=day + timedelta(hours=9)),
            skills={"fiber", "router"},
            equipment={"otdr", "ladder"},
            transport_modes={"car"},
            max_jobs=5,
        ),
        Engineer(
            id="eng-max",
            name="Максим Р.",
            start_location=Location(lat=55.7810, lon=37.6325, label="Проспект Мира"),
            shift=TimeWindow(start=day, end=day + timedelta(hours=9)),
            skills={"router", "voice"},
            equipment={"tester"},
            transport_modes={"car", "metro"},
            max_jobs=5,
        ),
        Engineer(
            id="eng-ilya",
            name="Илья С.",
            start_location=Location(lat=55.7216, lon=37.6084, label="Шаболовка"),
            shift=TimeWindow(start=day, end=day + timedelta(hours=9)),
            skills={"fiber", "voice"},
            equipment={"otdr", "tester"},
            transport_modes={"car"},
            max_jobs=5,
        ),
    ]

    locations = [
        (55.7603, 37.6187, "Трубная"),
        (55.7448, 37.6051, "Пречистенка"),
        (55.7894, 37.6791, "Сокольники"),
        (55.7312, 37.6360, "Павелецкая"),
        (55.7789, 37.5870, "Белорусская"),
        (55.7088, 37.6574, "Автозаводская"),
        (55.7688, 37.6470, "Бауманская"),
        (55.7386, 37.5483, "Деловой центр"),
    ]
    specs = [
        ("Нет связи у корпоративного клиента", {"fiber"}, {"otdr"}, 95),
        ("Замена маршрутизатора", {"router"}, set(), 70),
        ("Диагностика SIP", {"voice"}, {"tester"}, 80),
        ("Авария оптической линии", {"fiber"}, {"otdr"}, 100),
        ("Настройка резервного канала", {"router"}, set(), 65),
        ("Проверка телефонии", {"voice"}, {"tester"}, 55),
        ("Монтаж CPE", {"router"}, set(), 60),
        ("Измерение линии", {"fiber"}, {"otdr"}, 75),
    ]

    jobs: list[Job] = []
    for idx, ((lat, lon, label), (title, skills, equipment, priority)) in enumerate(
        zip(locations, specs), start=1
    ):
        window_start = day + timedelta(minutes=45 + idx * 20)
        jobs.append(
            Job(
                id=f"job-{idx:02d}",
                title=title,
                location=Location(lat=lat, lon=lon, label=label),
                service_minutes=35 if priority < 90 else 50,
                time_windows=[TimeWindow(start=window_start, end=window_start + timedelta(hours=4))],
                required_skills=skills,
                required_equipment=equipment,
                required_transport="car" if idx in {1, 4, 8} else None,
                priority=priority,
                sla_deadline=window_start + timedelta(hours=2, minutes=30),
            )
        )

    return PlanRequest(planning_time=day, engineers=engineers, jobs=jobs)
