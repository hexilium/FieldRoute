/* The shared street layer reads only the application's local OSM index. */
class LocalMapLayer {
  constructor(map, status) {
    this.map = map;
    this.status = status;
    this.enabled = false;
    this.version = 0;
    const pane = map.createPane('localBasemap');
    pane.style.zIndex = '220';
    pane.style.pointerEvents = 'none';
    const labelsPane = map.createPane('localMapLabels');
    labelsPane.style.zIndex = '260';
    labelsPane.style.pointerEvents = 'none';
    this.renderer = L.canvas({pane: 'localBasemap', padding: .2});
    this.layer = L.geoJSON([], {
      pane: 'localBasemap', renderer: this.renderer, interactive: false,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      style: feature => this.style(feature.properties?.kind),
      pointToLayer: (feature, point) => L.circleMarker(point, {
        pane: 'localBasemap', renderer: this.renderer, interactive: false, radius: 2,
      }),
    });
    this.labels = L.layerGroup();
    map.on('moveend', () => this.refresh());
    map.whenReady(() => { this.ready = true; this.refresh(); });
  }

  style(kind) {
    if (kind === 'water') return {color: '#aacddb', weight: 1, fillColor: '#c8e2ec', fillOpacity: 1};
    if (kind === 'building') return {color: '#cec9bc', weight: .7, fillColor: '#e3dfd4', fillOpacity: 1};
    return {color: '#ffffff', weight: this.map.getZoom() >= 15 ? 4 : 2, opacity: 1};
  }

  setEnabled(enabled) {
    this.enabled = enabled;
    this.version++;
    this.controller?.abort();
    this.controller = null;
    if (enabled) {
      this.layer.addTo(this.map);
      this.labels.addTo(this.map);
      this.metadata = null;
      this.refresh();
    } else {
      this.layer.remove(); this.labels.remove();
      this.status.textContent = 'Локальная карта выключена. Точки и маршруты остаются доступны.';
    }
  }

  async json(url, signal) {
    const response = await fetch(url, {signal});
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string'
      ? body.detail : 'Не удалось прочитать локальные данные карты.');
    return body;
  }

  clear() { this.layer.clearLayers(); this.labels.clearLayers(); }

  async refresh() {
    if (!this.enabled || !this.ready) return;
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const version = ++this.version;
    const timer = setTimeout(() => controller.abort(), 15000);
    this.status.textContent = 'Загружается локальная карта…';
    try {
      let metadata = this.metadata;
      if (!metadata || Date.now() - this.metadataAt > 30000) {
        metadata = await this.json('/api/v1/map/status', controller.signal);
        if (version !== this.version) return;
        if (metadata.available) { this.metadata = metadata; this.metadataAt = Date.now(); }
      }
      if (!metadata.available || !metadata.bounds) {
        this.clear();
        this.status.textContent = 'Локальная карта ещё не подготовлена. Точки и маршруты доступны; загрузите данные региона и обновите карту.';
        return;
      }
      const bounds = this.map.getBounds();
      const coverage = L.latLngBounds(metadata.bounds);
      if (!coverage.intersects(bounds)) {
        this.clear();
        this.status.textContent = 'Эта область вне загруженной локальной карты. Доступны точки и маршруты; выберите область загруженного региона.';
        return;
      }
      const params = new URLSearchParams({
        west: Math.max(-180, bounds.getWest()), south: Math.max(-90, bounds.getSouth()),
        east: Math.min(180, bounds.getEast()), north: Math.min(90, bounds.getNorth()),
        zoom: this.map.getZoom(),
      });
      const body = await this.json(`/api/v1/map/features?${params}`, controller.signal);
      if (version !== this.version) return;
      if (!Array.isArray(body.features)) throw new Error('Некорректные локальные данные карты.');
      const order = {water: 0, building: 1, road: 2};
      const features = [...body.features].sort((a, b) =>
        (order[a.properties?.kind] ?? 3) - (order[b.properties?.kind] ?? 3));
      this.clear();
      this.layer.addData({type: 'FeatureCollection', features});
      this.addLabels(features);
      this.status.textContent = body.truncated
        ? 'Локальная карта · без интернета. Показана часть объектов — приблизьте карту для подробностей.'
        : !coverage.contains(bounds)
          ? 'Локальная карта · без интернета. Часть видимой области находится за пределами загруженного региона.'
          : !features.length
            ? 'Локальная карта · без интернета. На этом масштабе нет объектов; приблизьте карту.'
            : 'Локальная карта OpenStreetMap · без интернета.';
    } catch (error) {
      if (version !== this.version) return;
      this.clear();
      this.status.textContent = error.name === 'AbortError'
        ? 'Локальная карта не ответила вовремя. Точки и маршруты остаются доступны.'
        : `Локальная карта недоступна. ${error.message} Точки и маршруты остаются доступны.`;
    } finally {
      clearTimeout(timer);
      if (version === this.version) this.controller = null;
    }
  }

  addLabels(features) {
    if (this.map.getZoom() < 14) return;
    const names = new Set();
    for (const feature of features) {
      const name = feature.properties?.name;
      if (feature.properties?.kind !== 'road' || typeof name !== 'string' || !name.trim()
        || names.has(name) || names.size >= 60 || feature.geometry?.type !== 'LineString') continue;
      const coordinates = feature.geometry.coordinates;
      const point = coordinates[Math.floor(coordinates.length / 2)];
      if (!point || !this.map.getBounds().contains([point[1], point[0]])) continue;
      names.add(name);
      const label = document.createElement('span');
      label.textContent = name;
      L.marker([point[1], point[0]], {
        pane: 'localMapLabels', interactive: false, keyboard: false,
        icon: L.divIcon({html: label, className: 'local-map-label', iconSize: null}),
      }).addTo(this.labels);
    }
  }
}
