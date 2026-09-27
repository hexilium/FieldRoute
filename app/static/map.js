/* Leaflet and the OSM street layer are served locally. */
class RouteMap {
  constructor({onEngineer, onJob}) {
    this.onEngineer = onEngineer;
    this.onJob = onJob;
    this.selectedEngineer = null;
    this.plan = null;
    this.jobMarkers = new Map();
    this.colors = new Map();
    this.palette = ['#2563eb', '#a21caf', '#0f766e', '#c2410c', '#4f46e5', '#be185d', '#4d7c0f'];
    this.map = L.map('map', {scrollWheelZoom: false, minZoom: 1, maxZoom: 19});
    L.control.scale({imperial: false}).addTo(this.map);
    this.layers = L.layerGroup().addTo(this.map);
    this.select = document.getElementById('engineerSelect');
    this.select.onchange = () => this.selectEngineer(this.select.value || null);
    document.getElementById('fitMapBtn').onclick = () => this.fit();
    this.tileToggle = document.getElementById('basemapToggle');
    this.basemap = new LocalMapLayer(this.map, document.getElementById('mapStatus'));
    this.tileToggle.onchange = () => this.toggleTiles();
    this.toggleTiles();
  }

  color(id) {
    if (!this.colors.has(id)) this.colors.set(id, this.palette[this.colors.size % this.palette.length]);
    return this.colors.get(id);
  }

  toggleTiles() {
    this.basemap.setEnabled(this.tileToggle.checked);
  }

  setPlan(plan) {
    this.plan = plan;
    this.jobs = new Map((plan.map_data?.jobs || []).map(job => [job.job_id, job]));
    this.engineers = plan.map_data?.engineers || [];
    if (!this.engineers.some(e => e.engineer_id === this.selectedEngineer)) this.selectedEngineer = null;
    const options = [textNode('option', 'Все инженеры')];
    options[0].value = '';
    for (const engineer of this.engineers) {
      const route = plan.routes.find(r => r.engineer_id === engineer.engineer_id);
      const count = route?.stops.length || 0;
      const option = textNode('option', `${engineer.name} · ${count} заявок${engineer.available ? '' : ' · недоступен'}`);
      option.value = engineer.engineer_id;
      options.push(option);
    }
    this.select.replaceChildren(...options);
    this.select.value = this.selectedEngineer || '';
    this.draw();
    this.fit();
  }

  selectEngineer(id) {
    this.selectedEngineer = id;
    this.select.value = id || '';
    this.draw();
    this.fit();
    this.onEngineer(id);
  }

  popup(title, lines) {
    const box = textNode('div', '', 'map-popup');
    box.append(textNode('b', title));
    for (const line of lines.filter(Boolean)) box.append(textNode('div', line));
    return box;
  }

  marker(location, {text, color, kind, title, popup, click}) {
    const badge = textNode('span', text, `map-marker ${kind}`);
    badge.style.setProperty('--marker-color', color);
    const marker = L.marker([location.lat, location.lon], {
      icon: L.divIcon({html: badge, className: 'map-marker-wrap', iconSize: [30, 30], iconAnchor: [15, 15]}),
      title, alt: title, keyboard: true,
      zIndexOffset: kind.includes('unassigned') ? 200 : kind.includes('origin') ? -100 : 0,
    }).bindPopup(popup).addTo(this.layers);
    if (click) marker.on('click', click);
    return marker;
  }

  draw() {
    this.layers.clearLayers();
    this.jobMarkers.clear();
    this.visiblePoints = [];
    const plan = this.plan;
    if (!plan) return;
    const visible = id => !this.selectedEngineer || id === this.selectedEngineer;
    let stopCount = 0;
    for (const engineer of this.engineers.filter(e => visible(e.engineer_id))) {
      const color = this.color(engineer.engineer_id);
      const route = plan.routes.find(r => r.engineer_id === engineer.engineer_id);
      const points = [engineer.location, ...(route?.stops || []).map(stop => stop.location)];
      this.visiblePoints.push(...points);
      const roadGeometry = plan.map_data?.distance_model === 'osm_fixed_speed'
        && route?.geometry?.length > 1;
      const linePoints = roadGeometry ? route.geometry : points;
      if (roadGeometry) this.visiblePoints.push(...linePoints);
      if (linePoints.length > 1) L.polyline(linePoints.map(p => [p.lat, p.lon]), {
        color, weight: 4, opacity: .8, dashArray: roadGeometry ? null : '8 6',
      }).addTo(this.layers).on('click', () => this.selectEngineer(engineer.engineer_id));
      const kind = {start: 'Старт смены', current: 'Текущая позиция', in_progress: 'Выполняет работу'}[engineer.location_kind];
      this.marker(engineer.location, {
        text: 'С', color, kind: 'origin', title: `${kind}: ${engineer.name}`,
        popup: this.popup(engineer.name, [kind, engineer.location.label,
          engineer.available ? null : 'Инженер недоступен',
          `${route?.stops.length || 0} заявок · ${(route?.total_distance_km || 0).toFixed(1)} км`]),
      });
      for (const [index, stop] of (route?.stops || []).entries()) {
        stopCount++;
        const job = this.jobs.get(stop.job_id);
        const urgent = job?.priority >= 80;
        const marker = this.marker(stop.location, {
          text: String(index + 1), color,
          kind: `${urgent ? 'urgent' : ''} ${stop.frozen ? 'frozen' : ''}`,
          title: `Визит ${index + 1}: ${stop.title} · ${engineer.name}`,
          popup: this.popup(stop.title, [
            `${engineer.name} · визит №${index + 1}`,
            `${new Date(stop.service_start).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'})}–${new Date(stop.departure).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'})} · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`,
            stop.location.label,
            urgent ? `Срочная · приоритет ${job.priority}` : null,
            stop.frozen ? 'Исполнитель закреплён' : null,
            ...(stop.explanation || []),
          ]),
          click: () => this.onJob(stop.job_id),
        });
        this.jobMarkers.set(stop.job_id, marker);
      }
    }
    for (const unassigned of plan.unassigned) {
      const job = this.jobs.get(unassigned.job_id);
      if (!job) continue;
      this.visiblePoints.push(job.location);
      const marker = this.marker(job.location, {
        text: '!', color: '#b91c1c', kind: 'unassigned',
        title: `Не назначена: ${job.title}`,
        popup: this.popup(job.title, ['Не назначена', job.location.label,
          job.priority >= 80 ? `Срочная · приоритет ${job.priority}` : null, unassigned.explanation]),
        click: () => this.onJob(job.job_id),
      });
      this.jobMarkers.set(job.job_id, marker);
    }
    const missing = plan.unassigned.filter(job => !this.jobs.has(job.job_id)).length;
    document.getElementById('mapSummary').textContent =
      `${this.selectedEngineer ? 'Выбранный инженер' : 'Все инженеры'} · визитов: ${stopCount} · не назначено: ${plan.unassigned.length}`;
    document.getElementById('mapEmpty').hidden = this.visiblePoints.length > 0;
    document.getElementById('mapEmpty').textContent = missing
      ? `Для ${missing} неназначенных заявок нет координат в этом плане.` : 'В плане нет точек для отображения.';
    document.getElementById('distanceModel').textContent = plan.map_data?.distance_model === 'osm_fixed_speed'
      ? 'Путь по дорогам и дорожкам OpenStreetMap. Время — расстояние / фиксированная скорость. Общественный транспорт оценён по автомобильным дорогам; пробки, остановки и расписания не учитываются.'
      : plan.map_data?.distance_model === 'osrm_road'
        ? 'Пробег и время рассчитаны автомобильным сервисом OSRM.' : 'Пробег — расстояние по сфере × 1,25. Время зависит от транспорта и заданной скорости инженера; это оценка без пробок и расписаний.';
    document.getElementById('legend').textContent = plan.map_data?.distance_model === 'osm_fixed_speed'
      ? 'Цветные линии показывают рассчитанные пути по дорогам и дорожкам. Цвет соответствует инженеру; номера обозначают порядок визитов.'
      : 'Пунктир соединяет визиты по порядку. Цвет соответствует инженеру; линии не повторяют дороги.';
  }

  fit() {
    this.map.invalidateSize();
    if (this.visiblePoints?.length) {
      this.map.fitBounds(this.visiblePoints.map(p => [p.lat, p.lon]), {padding: [38, 38], maxZoom: 15, animate: false});
    } else {
      this.map.setView([0, 0], 2, {animate: false});
    }
  }

  focusJob(id) {
    if (!this.jobMarkers.has(id)) this.selectEngineer(null);
    const marker = this.jobMarkers.get(id);
    if (marker) {
      this.map.setView(marker.getLatLng(), Math.max(this.map.getZoom(), 15), {animate: false});
      marker.openPopup();
    }
  }
}
