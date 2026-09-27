/* Address suggestions and map selections share one cancellable lookup. */
class UrgentLocationPicker {
  constructor() {
    this.address = eventNode('urgentAddress');
    this.lat = eventNode('urgentLat');
    this.lon = eventNode('urgentLon');
    this.status = eventNode('urgentLocationStatus');
    this.results = eventNode('urgentAddressResults');
    this.version = 0;
    this.boundAddress = null;
    this.suggestions = [];
    this.activeSuggestion = -1;
    eventNode('urgentFindAddressBtn').onclick = () => { this.address.focus(); this.search(); };
    eventNode('urgentLocateBtn').onclick = () => this.reverse();
    eventNode('urgentMapToggleBtn').onclick = () => {
      const panel = eventNode('urgentMapPanel');
      panel.hidden = !panel.hidden;
      eventNode('urgentMapToggleBtn').setAttribute('aria-expanded', String(!panel.hidden));
      eventNode('urgentMapToggleBtn').textContent = panel.hidden ? 'Выбрать на карте' : 'Скрыть карту';
      if (panel.hidden) this.pauseMap(); else this.showMapIfOpen({scroll: true});
    };
    this.address.addEventListener('keydown', event => {
      if (event.isComposing) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (this.resultsQuery === this.address.value.trim() && this.suggestions.length) {
          this.openResults();
          const index = this.activeSuggestion < 0
            ? (event.key === 'ArrowDown' ? 0 : this.suggestions.length - 1)
            : this.activeSuggestion + (event.key === 'ArrowDown' ? 1 : -1);
          this.activateSuggestion(index);
        } else this.search({activate: true});
      } else if (event.key === 'Enter') {
        event.preventDefault();
        if (!this.results.hidden && this.activeSuggestion >= 0) this.select(this.suggestions[this.activeSuggestion]);
        else this.search();
      } else if (event.key === 'Escape' && (!this.results.hidden || this.searchTimer || this.pendingLookup === 'search')) {
        event.preventDefault(); event.stopPropagation(); this.dismissSuggestions();
      } else if (event.key === 'Tab') this.dismissSuggestions();
    });
    this.address.addEventListener('input', event => {
      this.cancel();
      this.clearResults();
      // Once a resolved address changes, its coordinates are no longer valid.
      // A point without a resolved address may still be labelled manually.
      if (this.boundAddress !== null) {
        this.lat.value = ''; this.lon.value = '';
        this.boundAddress = null;
        this.removeMarker();
      }
      this.status.textContent = this.coordinates()
        ? 'Адрес введён вручную для выбранных координат. Проверьте точку на карте.'
        : 'Начните вводить адрес и выберите подходящий вариант из списка.';
      this.syncButtons();
      if (!event.isComposing) this.scheduleSearch();
    });
    this.address.addEventListener('compositionend', () => this.scheduleSearch());
    this.address.addEventListener('focus', () => {
      if (this.boundAddress === this.address.value) return;
      if (this.resultsQuery === this.address.value.trim() && this.suggestions.length) this.openResults();
      else this.scheduleSearch();
    });
    this.address.addEventListener('blur', () => this.dismissSuggestions());
    this.results.addEventListener('pointerdown', event => event.preventDefault());
    document.addEventListener('pointerdown', event => {
      if (!event.target.closest('.location-combobox') && event.target.id !== 'urgentFindAddressBtn') this.dismissSuggestions();
    });
    for (const input of [this.lat, this.lon]) input.addEventListener('input', () => {
      this.cancel();
      this.clearResults();
      if (this.boundAddress !== null) this.address.value = '';
      this.boundAddress = null;
      this.updateMarker();
      this.status.textContent = 'Координаты изменены вручную. Нажмите «Определить адрес по координатам» или введите адрес.';
      this.syncButtons();
    });
    eventNode('eventDetails').addEventListener('close', () => { this.stopLookup(); this.pauseMap(); });
    eventNode('eventType').addEventListener('change', () => {
      this.stopLookup();
      if (eventNode('urgentFields').hidden) this.pauseMap(); else this.showMapIfOpen();
    });
    this.syncButtons();
  }

  coordinates() {
    if (!this.lat.value.trim() || !this.lon.value.trim()) return null;
    const lat = Number(this.lat.value), lon = Number(this.lon.value);
    return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180
      ? {lat, lon} : null;
  }

  enabled() {
    return !eventNode('urgentFields').disabled && !eventNode('shell').classList.contains('loading');
  }

  syncButtons() {
    const disabled = !this.enabled();
    eventNode('urgentFindAddressBtn').disabled = disabled || this.busy || this.address.value.trim().length < 3;
    eventNode('urgentLocateBtn').disabled = disabled || this.busy || !this.coordinates();
    eventNode('urgentMapToggleBtn').disabled = disabled;
    if (this.marker?.dragging) {
      if (disabled) this.marker.dragging.disable(); else this.marker.dragging.enable();
    }
  }

  openResults() {
    if (!this.suggestions.length) return;
    this.results.hidden = false;
    this.address.setAttribute('aria-expanded', 'true');
  }

  hideResults() {
    this.results.hidden = true;
    this.address.setAttribute('aria-expanded', 'false');
    this.address.removeAttribute('aria-activedescendant');
    this.activeSuggestion = -1;
    this.results.querySelectorAll('[role=option]').forEach(node => node.setAttribute('aria-selected', 'false'));
  }

  clearResults() {
    this.hideResults(); this.results.replaceChildren(); this.suggestions = []; this.resultsQuery = '';
  }

  dismissSuggestions() {
    if (this.searchTimer || this.pendingLookup === 'search') {
      this.cancel();
      this.status.textContent = 'Подсказки закрыты. Продолжите ввод, чтобы увидеть варианты.';
    }
    this.hideResults();
  }

  activateSuggestion(index) {
    this.activeSuggestion = (index + this.suggestions.length) % this.suggestions.length;
    const options = [...this.results.querySelectorAll('[role=option]')];
    options.forEach((node, position) => node.setAttribute('aria-selected', String(position === this.activeSuggestion)));
    const option = options[this.activeSuggestion];
    this.address.setAttribute('aria-activedescendant', option.id);
    // Keep scrolling inside the dropdown; do not move the enclosing dialog.
    if (option.offsetTop < this.results.scrollTop) this.results.scrollTop = option.offsetTop;
    else if (option.offsetTop + option.offsetHeight > this.results.scrollTop + this.results.clientHeight)
      this.results.scrollTop = option.offsetTop + option.offsetHeight - this.results.clientHeight;
  }

  scheduleSearch() {
    clearTimeout(this.searchTimer); this.searchTimer = null;
    if (!this.enabled() || this.address.value.trim().length < 3 || this.boundAddress === this.address.value) return;
    this.searchTimer = setTimeout(() => {
      this.searchTimer = null;
      if (document.activeElement === this.address && eventNode('eventDetails').open) this.search();
    }, 300);
  }

  cancel() {
    clearTimeout(this.searchTimer); this.searchTimer = null;
    this.version++;
    this.controller?.abort();
    this.controller = null;
    this.busy = false;
    this.pendingLookup = null;
    this.syncButtons();
  }

  stopLookup() {
    if (this.busy) this.status.textContent = 'Поиск остановлен. Повторите поиск адреса или укажите его вручную.';
    this.cancel();
    this.hideResults();
  }

  reset() {
    this.cancel(); this.clearResults();
    this.address.value = ''; this.lat.value = ''; this.lon.value = '';
    this.boundAddress = null;
    this.removeMarker();
    this.mapNeedsView = true;
    eventNode('urgentMapPanel').hidden = true;
    eventNode('urgentMapToggleBtn').setAttribute('aria-expanded', 'false');
    eventNode('urgentMapToggleBtn').textContent = 'Выбрать на карте';
    this.pauseMap();
    this.status.textContent = 'Начните вводить адрес — варианты появятся под полем. Или выберите точку на карте.';
    this.syncButtons();
  }

  async lookup(path, params, pending, receive) {
    this.cancel();
    const version = this.version;
    const controller = new AbortController();
    this.controller = controller;
    this.busy = true;
    this.pendingLookup = path;
    this.status.textContent = pending;
    this.syncButtons();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/v1/geocoding/${path}?${new URLSearchParams(params)}`, {signal: controller.signal});
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Не удалось определить адрес. Повторите поиск или укажите данные вручную.');
      if (version === this.version) receive(body);
    } catch (error) {
      if (version === this.version) this.status.textContent = error.name === 'AbortError'
        ? 'Сервис поиска не ответил вовремя. Повторите поиск или укажите адрес и координаты вручную.'
        : `Поиск недоступен. ${error.message} Координаты можно указать вручную или выбрать на карте.`;
    } finally {
      clearTimeout(timer);
      if (version === this.version) { this.busy = false; this.controller = null; this.pendingLookup = null; this.syncButtons(); }
    }
  }

  search({activate = false} = {}) {
    if (!this.enabled()) return;
    const q = this.address.value.trim();
    if (q.length < 3) { this.status.textContent = 'Введите город, улицу и дом (не менее трёх символов).'; return; }
    this.clearResults();
    const center = (this.map && !eventNode('urgentMapPanel').hidden ? this.map.getCenter() : routeMap.map.getCenter()).wrap();
    this.lookup('search', {q, lat: center.lat, lon: center.lng}, 'Ищем адрес…', body => {
      if (!body.results?.length) {
        this.status.textContent = 'Адрес не найден. Уточните город, улицу и дом или выберите точку на карте.';
        return;
      }
      this.suggestions = body.results;
      this.resultsQuery = q;
      for (const [index, place] of body.results.entries()) {
        const item = document.createElement('li');
        item.setAttribute('role', 'presentation');
        const button = textNode('button', place.label);
        button.type = 'button';
        button.id = `urgentAddressOption-${index}`;
        button.tabIndex = -1;
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', 'false');
        const distance = center.distanceTo(L.latLng(place.lat, place.lon));
        const detail = textNode('span',
          `${(distance / 1000).toLocaleString('ru-RU', {maximumFractionDigits: 1})} км от центра карты · ${Number(place.lat).toFixed(5)}, ${Number(place.lon).toFixed(5)}`,
          'muted tiny');
        detail.style.display = 'block';
        button.append(detail);
        button.onclick = () => this.select(place);
        item.append(button); this.results.append(item);
      }
      this.openResults();
      if (activate) this.activateSuggestion(0);
      this.status.textContent = 'Выберите подходящий адрес из списка — координаты заполнятся автоматически.';
    });
  }

  select(place) {
    if (!this.enabled()) return;
    this.cancel(); this.clearResults();
    this.address.value = place.label;
    this.boundAddress = place.label;
    this.lat.value = place.lat; this.lon.value = place.lon;
    this.updateMarker(true);
    this.status.textContent = 'Адрес выбран, координаты заполнены. Положение можно уточнить на карте.';
    this.syncButtons();
  }

  reverse() {
    if (!this.enabled()) return;
    const point = this.coordinates();
    if (!point) { this.status.textContent = 'Укажите допустимые широту и долготу.'; return; }
    this.clearResults();
    this.address.value = ''; this.boundAddress = null;
    this.lookup('reverse', point, 'Определяем ближайший адрес…', body => {
      if (body.result) {
        this.address.value = body.result.label;
        this.boundAddress = body.result.label;
        // Reverse geocoding names a nearby object; never move the selected point.
        this.status.textContent = 'Найден ближайший адрес. Проверьте его; выбранные координаты сохранены.';
      } else this.status.textContent = 'Для этой точки адрес не найден. Координаты сохранены — введите адрес вручную.';
    });
  }

  pickPoint(point) {
    if (!this.enabled()) { this.updateMarker(); return; }
    point = L.latLng(point).wrap();
    this.lat.value = point.lat.toFixed(6); this.lon.value = point.lng.toFixed(6);
    this.updateMarker();
    this.reverse();
  }

  removeMarker() { this.marker?.remove(); this.marker = null; }

  updateMarker(focus = false) {
    if (!this.map) return;
    const point = this.coordinates();
    if (!point) { this.removeMarker(); return; }
    const position = [point.lat, point.lon];
    if (this.marker) this.marker.setLatLng(position);
    else {
      this.marker = L.marker(position, {draggable: true, title: 'Место срочной заявки', alt: 'Место срочной заявки'}).addTo(this.map);
      this.marker.on('dragend', () => this.pickPoint(this.marker.getLatLng()));
    }
    if (focus) this.map.setView(position, Math.max(this.map.getZoom(), 16), {animate: false});
  }

  mapVisible() {
    return !eventNode('urgentMapPanel').hidden && eventNode('eventDetails').open && !eventNode('urgentFields').hidden;
  }

  pauseMap() {
    cancelAnimationFrame(this.mapFrame);
    this.basemap?.setEnabled(false);
  }

  showMapIfOpen({scroll = false} = {}) {
    if (!this.mapVisible()) return;
    cancelAnimationFrame(this.mapFrame);
    // Measure only after the panel is visible and the dialog has laid out.
    this.mapFrame = requestAnimationFrame(() => {
      if (!this.mapVisible()) return;
      const container = eventNode('urgentLocationMap');
      const point = this.coordinates();
      if (!this.map) {
        this.map = L.map(container, {scrollWheelZoom: false, minZoom: 1, maxZoom: 19});
        this.map.setView(point ? [point.lat, point.lon] : routeMap.map.getCenter(),
          point ? 16 : Math.max(routeMap.map.getZoom(), 16), {animate: false});
        this.map.on('click', event => this.pickPoint(event.latlng));
        this.basemap = new LocalMapLayer(this.map, eventNode('urgentMapStatus'));
        this.mapResizeObserver = new ResizeObserver(() => {
          if (this.mapVisible() && container.clientWidth && container.clientHeight)
            this.map.invalidateSize({pan: false, animate: false});
        });
        this.mapResizeObserver.observe(container);
      } else if (this.mapNeedsView) {
        this.map.setView(point ? [point.lat, point.lon] : routeMap.map.getCenter(),
          point ? 16 : Math.max(routeMap.map.getZoom(), 16), {animate: false});
      }
      this.mapNeedsView = false;
      this.map.invalidateSize({pan: false, animate: false});
      if (!this.basemap.enabled) this.basemap.setEnabled(true); else this.basemap.refresh();
      this.updateMarker();
      if (scroll) container.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    });
  }
}

window.urgentLocationPicker = new UrgentLocationPicker();
