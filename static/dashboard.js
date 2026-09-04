(() => {
  const $ = id => document.getElementById(id);
  const colors = ['#007c83', '#d94b45', '#e49b32', '#6088a0', '#7c5b9e'];
  const metricUnits = {
    flow_ml_s: 'ml/s', fill_volume_ml: 'ml', fill_error_ml: 'ml', valve_open_time_s: 's',
    tank_level_ml: 'ml', tank_level_pct: '%', cap_torque_nm: 'Nm',
    cycle_time_s: 's', actuator_response_time_s: 's', vacuum_pressure_bar: 'bar',
    color_confidence_pct: '%', sorting_time_s: 's', classification_confidence_pct: '%'
  };
  const maintenanceAdvice = {
    valve_flow_loss: ['Vană de dozare', 'Verifică debitul, depunerile și etanșarea vanei'],
    valve_stiction: ['Vană de dozare', 'Verifică timpul de deschidere și cursa vanei'],
    vacuum_leak: ['Cap pneumatic', 'Verifică vacuumul, prinderea și poziționarea capacului'],
    sensor_drift: ['Senzor', 'Calibrează senzorul și verifică alinierea'],
    cylinder_slowdown: ['Cilindru', 'Verifică presiunea și lubrifierea'],
    sensor_misread: ['Senzor material/culoare', 'Curăță și calibrează senzorii de sortare']
  };

  let hoverPoints = [];
  let lastRows = [];
  let lastRange = [];
  let lastSummary = null;
  let activeQuickHours = 24;
  let requestNumber = 0;
  let activeController = null;

  const pretty = value => String(value ?? '').replaceAll('_', ' ');
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  })[character]);
  const format = value => typeof value === 'number'
    ? new Intl.NumberFormat('ro-RO', { maximumFractionDigits: 2 }).format(value)
    : value;
  const dateInput = date => new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 19);

  function setQuickButton(hours) {
    document.querySelectorAll('[data-hours]').forEach(button => {
      button.classList.toggle('active', hours !== null && Number(button.dataset.hours) === hours);
    });
  }

  function metricValue(row, metric) {
    return metric === 'degradation'
      ? Number(row.payload?.health?.degradation_score)
      : Number(row.payload?.measurements?.[metric]);
  }

  function updateMetricOptions(rows) {
    const selected = $('trend-metric').value;
    const metrics = new Set(['degradation']);
    rows.forEach(row => {
      Object.entries(row.payload?.measurements || {}).forEach(([name, value]) => {
        if (typeof value === 'number') metrics.add(name);
      });
    });
    $('trend-metric').innerHTML = [...metrics].map(name =>
      `<option value="${escapeHtml(name)}">${name === 'degradation' ? 'Degradare' : escapeHtml(pretty(name))}</option>`
    ).join('');
    $('trend-metric').value = metrics.has(selected) ? selected : 'degradation';
  }

  function drawTrend(rows, start, end) {
    const canvas = $('chart');
    const box = canvas.parentElement;
    const ratio = window.devicePixelRatio || 1;
    const width = box.clientWidth;
    const height = box.clientHeight;
    const pad = { left: 62, right: 18, top: 18, bottom: 46 };

    canvas.width = Math.max(1, width * ratio);
    canvas.height = Math.max(1, height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    hoverPoints = [];

    const metric = $('trend-metric').value;
    const grouped = {};
    rows.forEach(row => {
      const value = metricValue(row, metric);
      const time = new Date(row.timestamp).getTime();
      if (Number.isFinite(value) && Number.isFinite(time)) {
        (grouped[row.station] ??= []).push({ value, time });
      }
    });
    Object.values(grouped).forEach(points => points.sort((a, b) => a.time - b.time));
    const allPoints = Object.values(grouped).flat();

    if (!allPoints.length) {
      $('chart-empty').hidden = false;
      $('chart-empty').innerHTML = `Nu există valori pentru ${escapeHtml(pretty(metric))} în intervalul selectat.`;
      $('chart-tooltip').hidden = true;
      $('legend').innerHTML = '';
      return;
    }
    $('chart-empty').hidden = true;

    const startMs = start.getTime();
    const endMs = end.getTime();
    const span = Math.max(endMs - startMs, 1);
    const rawMin = Math.min(...allPoints.map(point => point.value));
    const rawMax = Math.max(...allPoints.map(point => point.value));
    const valuePadding = rawMax === rawMin
      ? Math.max(Math.abs(rawMax) * .1, metric === 'degradation' ? .05 : 1)
      : (rawMax - rawMin) * .1;
    const low = metric === 'degradation' ? Math.max(0, rawMin - valuePadding) : rawMin - valuePadding;
    const highCandidate = metric === 'degradation' ? Math.min(1, rawMax + valuePadding) : rawMax + valuePadding;
    const high = highCandidate > low ? highCandidate : low + (metric === 'degradation' ? .1 : 1);
    const xAt = time => pad.left + (time - startMs) / span * (width - pad.left - pad.right);
    const yAt = value => height - pad.bottom - (value - low) / (high - low) * (height - pad.top - pad.bottom);
    const unit = metric === 'degradation' ? '%' : (metricUnits[metric] || '');

    ctx.font = '11px DM Sans';
    ctx.strokeStyle = '#dfe5e1';
    ctx.fillStyle = '#65727d';
    ctx.textAlign = 'right';
    for (let tick = 0; tick < 5; tick++) {
      const value = low + (high - low) * tick / 4;
      const y = yAt(value);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      const label = metric === 'degradation' ? (value * 100).toFixed(0) : value.toFixed(2);
      ctx.fillText(`${label}${unit}`, pad.left - 8, y + 4);
    }

    ctx.textAlign = 'center';
    for (let index = 0; index < 6; index++) {
      const time = startMs + span * index / 5;
      const x = xAt(time);
      const options = span >= 86400000
        ? { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }
        : { hour: '2-digit', minute: '2-digit' };
      ctx.fillText(new Date(time).toLocaleString('ro-RO', options), x, height - 16);
    }

    const seriesInfo = Object.fromEntries(Object.entries(grouped).map(([name, points]) => {
      const deltas = points.slice(1).map((point, index) => point.time - points[index].time).sort((a, b) => a - b);
      const medianDelta = deltas.length ? deltas[Math.floor(deltas.length / 2)] : Infinity;
      const gapThreshold = Math.max(60000, medianDelta * 5);
      const gaps = deltas.filter(delta => delta > gapThreshold).length;
      return [name, { gapThreshold, gaps }];
    }));

    $('legend').innerHTML = Object.entries(grouped).map(([name, points], index) => {
      const gaps = seriesInfo[name].gaps;
      return `<span class="legend-item" style="--legend:${colors[index % colors.length]}">${escapeHtml(name)}${gaps ? ` · ${gaps} întreruperi` : ''}</span>`;
    }).join('');

    Object.entries(grouped).forEach(([name, points], index) => {
      const color = colors[index % colors.length];
      const gapThreshold = seriesInfo[name].gapThreshold;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.beginPath();
      points.forEach((point, pointIndex) => {
        const x = xAt(point.time);
        const y = yAt(point.value);
        const previous = points[pointIndex - 1];
        if (!previous || point.time - previous.time > gapThreshold) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
        hoverPoints.push({ x, y, name, value: point.value, time: new Date(point.time), unit });
      });
      ctx.stroke();

      if (points.length <= 80) {
        ctx.fillStyle = color;
        points.forEach(point => {
          ctx.beginPath();
          ctx.arc(xAt(point.time), yAt(point.value), 2.2, 0, Math.PI * 2);
          ctx.fill();
        });
      }
    });
  }

  $('chart').addEventListener('mousemove', event => {
    if (!hoverPoints.length) return;
    const rect = $('chart').getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const point = hoverPoints.reduce((best, candidate) =>
      Math.hypot(candidate.x - x, candidate.y - y) < Math.hypot(best.x - x, best.y - y)
        ? candidate : best
    );
    const tooltip = $('chart-tooltip');
    if (Math.hypot(point.x - x, point.y - y) > 16) {
      tooltip.hidden = true;
      return;
    }
    const displayed = point.unit === '%' ? (point.value * 100).toFixed(1) : point.value.toFixed(2);
    tooltip.innerHTML = `<strong>${escapeHtml(point.name)}</strong><br>${point.time.toLocaleString('ro-RO')}<br>Valoare: <strong>${displayed}${escapeHtml(point.unit)}</strong>`;
    tooltip.style.left = `${Math.min(point.x, rect.width - 180)}px`;
    tooltip.style.top = `${point.y}px`;
    tooltip.hidden = false;
  });
  $('chart').addEventListener('mouseleave', () => { $('chart-tooltip').hidden = true; });

  function classForScore(score, state = '') {
    return score >= .82 || state === 'FAILURE' || state === 'FAULT'
      ? 'critical'
      : score >= .45 || state === 'DEGRADED' ? 'high' : '';
  }

  function currentScore(data) {
    const latest = data.latest || {};
    return Number(data.health?.current_degradation ?? latest.health?.degradation_score ?? 0);
  }

  function metricDisplay(name, value) {
    if (typeof value === 'boolean') return value ? 'DA' : 'NU';
    if (typeof value === 'number') return `${format(value)}${metricUnits[name] ? ` ${metricUnits[name]}` : ''}`;
    return pretty(value || '--');
  }

  function renderStations(stations) {
    const entries = Object.entries(stations);
    const sort = $('station-sort').value;
    const moduleOrder = { Distributing: 1, Separating: 2, Bottling: 3, Pick_and_Place: 4, Sorting: 5 };
    const descriptions = {
      Distributing: 'Preluare recipient cu axă motorizată și cap vacuum; transfer către prima bandă.',
      Separating: 'Detecție prezență și culoare; continuare pe flux sau deviere pe banda secundară.',
      Bottling: 'Poziționare cu opritor, dozare temporizată și compensare după nivelul rezervorului.',
      Pick_and_Place: 'Poziționare, preluare capac și înfiletare cu un cap pneumatic pe trei axe.',
      Sorting: 'Detecție material și culoare; rutare finală cu două opritoare către trei ieșiri.'
    };
    entries.sort(([nameA, dataA], [nameB, dataB]) => {
      if (sort === 'name') return nameA.localeCompare(nameB);
      const valueA = sort === 'degradation' ? dataA.health?.average_degradation || 0 : currentScore(dataA);
      const valueB = sort === 'degradation' ? dataB.health?.average_degradation || 0 : currentScore(dataB);
      return valueB - valueA;
    });

    $('stations').innerHTML = entries.length ? entries.map(([name, data]) => {
      const latest = data.latest || {};
      const current = currentScore(data);
      const average = Number(data.health?.average_degradation || 0);
      const peak = Number(data.health?.peak_degradation || 0);
      const change = Number(data.health?.change_in_range || 0);
      const state = latest.health?.state || latest.state || 'UNKNOWN';
      const process = latest.operational?.operational_state || latest.process?.state || '--';
      const kind = classForScore(current, state);
      const operational = data.operational || {};
      const measurements = Object.entries(latest.measurements || {}).slice(0, 8);
      const components = Object.entries(latest.health?.components || {});
      const activeFaults = Object.entries(latest.health?.active_faults || {});
      const activeInputs = Object.entries(latest.inputs || {}).filter(([, value]) => value === true);
      const activeOutputs = Object.entries(latest.outputs || {}).filter(([, value]) => value === true);
      const inputCount = Object.keys(latest.inputs || {}).length;
      const outputCount = Object.keys(latest.outputs || {}).length;
      const activeSignals = [...activeInputs.map(([signal]) => `I: ${pretty(signal)}`), ...activeOutputs.map(([signal]) => `O: ${pretty(signal)}`)].slice(0, 6);
      const trendClass = change > .01 ? 'trend-up' : change < -.01 ? 'trend-down' : 'trend-flat';
      const trendText = `${change > 0 ? '+' : ''}${(change * 100).toFixed(1)} pp`;
      const lastSeen = data.last_seen ? new Date(data.last_seen).toLocaleString('ro-RO') : '--';

      return `<article class="station module-card ${kind}">
        <div class="module-header">
          <div class="station-identity"><span class="module-number">Modul ${moduleOrder[name] || '--'}</span><h3>${escapeHtml(pretty(name))}</h3><p>${escapeHtml(descriptions[name] || '')}</p></div>
          <div class="status-stack"><span class="badge ${kind === 'critical' ? 'danger' : kind ? 'warn' : ''}">${escapeHtml(state)}</span><span class="process-pill">${escapeHtml(pretty(process))}</span></div>
        </div>
        <div class="health-overview">
          <div class="health-primary"><small>Degradare curentă</small><strong>${(current * 100).toFixed(1)}%</strong><div class="meter"><i style="width:${Math.min(current * 100, 100)}%"></i></div><span>Sănătate estimată ${(Math.max(0, 1 - current) * 100).toFixed(1)}%</span></div>
          <div class="health-stat"><small>Tendință interval</small><b class="${trendClass}">${trendText}</b></div>
          <div class="health-stat"><small>Medie interval</small><b>${(average * 100).toFixed(1)}%</b></div>
          <div class="health-stat historical"><small>Vârf interval · informativ</small><b>${(peak * 100).toFixed(1)}%</b></div>
        </div>
        <div class="module-grid">
          <section class="module-section"><h4>Performanță în interval</h4><div class="detail-list"><span>Cicluri<b>${format(operational.cycles_in_range ?? 0)}</b></span><span>Ritm mediu<b>${format(operational.average_cycle_rate_per_min ?? 0)}/min</b></span><span>Disponibilitate<b>${format(operational.average_availability_pct ?? 0)}%</b></span><span>Probe analizate<b>${format(data.samples ?? 0)}</b></span></div></section>
          <section class="module-section"><h4>Măsurători curente</h4><div class="detail-list">${measurements.length ? measurements.map(([metric, value]) => `<span>${escapeHtml(pretty(metric))}<b>${escapeHtml(metricDisplay(metric, value))}</b></span>`).join('') : '<p class="empty-compact">Încă nu există o măsurătoare completă.</p>'}</div></section>
          <section class="module-section"><h4>Semnale PLC / I/O</h4><div class="io-counts"><span>Intrări active <b>${activeInputs.length}/${inputCount}</b></span><span>Ieșiri active <b>${activeOutputs.length}/${outputCount}</b></span></div><div class="signal-list">${activeSignals.length ? activeSignals.map(signal => `<span>${escapeHtml(signal)}</span>`).join('') : '<span>Niciun semnal activ</span>'}</div></section>
          <section class="module-section"><h4>Sănătate componente</h4><div class="component-list">${components.length ? components.map(([component, health]) => `<div><span>${escapeHtml(pretty(component))}<b>${(Number(health) * 100).toFixed(0)}%</b></span><i><em style="width:${Math.max(0, Math.min(100, Number(health) * 100))}%"></em></i></div>`).join('') : '<p class="empty-compact">Fără date pe componente.</p>'}</div></section>
        </div>
        <div class="module-footer"><span>Ultima probă: ${escapeHtml(lastSeen)}</span>${activeFaults.length ? `<strong class="fault-banner">Fault activ: ${activeFaults.map(([fault, severity]) => `${escapeHtml(pretty(fault))} (${(Number(severity) * 100).toFixed(0)}%)`).join(', ')}</strong>` : '<strong class="healthy-banner">Fără faulturi active</strong>'}</div>
      </article>`;
    }).join('') : '<div class="empty">Nu există date de modul în intervalul selectat.</div>';
  }

  function renderMaintenance(stations) {
    const items = Object.entries(stations).map(([name, data]) => {
      const latest = data.latest || {};
      const score = currentScore(data);
      const state = latest.health?.state || latest.state || 'UNKNOWN';
      const faults = Object.keys(latest.health?.active_faults || {});
      const fault = faults[0];
      const requiresStop = ['FAULT', 'FAILURE'].includes(state) || score >= .9;
      if (score < .45 && !fault && !requiresStop) return null;
      const advice = maintenanceAdvice[fault] || ['Echipament', 'Inspectează modulul și compară măsurătorile cu valorile de bază'];
      return {
        name, score, fault, state, component: advice[0],
        action: requiresStop ? `Izolează în siguranță modulul ${pretty(name)} și deschide un ordin de lucru` : advice[1],
        due: requiresStop ? 'Acum' : score >= .65 || fault ? 'La următoarea oprire' : 'În 7 zile'
      };
    }).filter(Boolean).sort((a, b) => b.score - a.score);

    $('maintenance').innerHTML = items.length ? `<p class="maintenance-note">Planul folosește degradarea curentă și faulturile active. Vârfurile istorice nu declanșează opriri.</p>${items.map(item => {
      const kind = classForScore(item.score, item.state);
      return `<div class="task ${kind === 'critical' ? 'danger' : kind ? 'warn' : ''}"><i></i><div>
        <strong>${escapeHtml(item.action)}</strong>
        <span>${escapeHtml(item.name)} · ${escapeHtml(item.component)} · degradare curentă ${(item.score * 100).toFixed(1)}%${item.fault ? ` · ${escapeHtml(pretty(item.fault))}` : ''}</span>
      </div><span class="due">${item.due}</span></div>`;
    }).join('')}` : '<div class="maintenance-ok"><strong>Nicio intervenție necesară acum</strong><span>Modulele nu au faulturi active și degradarea curentă este sub pragul de mentenanță.</span></div>';
    $('actions').textContent = items.length;
  }

  function renderEvents(events) {
    $('events').innerHTML = events.length
      ? `<table><thead><tr><th>Data și ora</th><th>Modul</th><th>Eveniment</th><th>Severitate</th></tr></thead><tbody>${events.slice(0, 12).map(event =>
          `<tr><td>${new Date(event.timestamp).toLocaleString('ro-RO')}</td><td>${escapeHtml(event.station)}</td><td>${escapeHtml(pretty(event.code || 'UNKNOWN'))}</td><td>${escapeHtml(event.severity || '--')}</td></tr>`
        ).join('')}</tbody></table>`
      : '<div class="empty">Nu există evenimente în intervalul selectat.</div>';
  }

  function freshnessLabel(seconds) {
    if (seconds == null) return '--';
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
    return `${(seconds / 3600).toFixed(1)} h`;
  }

  function updateLineSummary(line) {
    const production = line.production || {};
    const state = line.state || 'FĂRĂ DATE';
    $('line-state').textContent = state;
    $('line-mode').textContent = pretty(line.operational_state || '--');
    $('line-state').className = state === 'ONLINE' ? 'ok' : state === 'DEGRADED' ? 'warn' : 'danger';
    $('good').textContent = format(production.good ?? 0);
    $('diverted').textContent = `Bandă secundară: ${format(production.diverted ?? 0)}`;
    $('quality').textContent = production.quality_rate == null ? '--' : `${(production.quality_rate * 100).toFixed(1)}%`;
    $('oee').textContent = line.oee_pct == null ? '--' : `${line.oee_pct.toFixed(1)}%`;
    $('oee').className = line.oee_pct >= 85 ? 'ok' : line.oee_pct >= 60 ? 'warn' : 'danger';
    $('oee-components').textContent = `A ${format(line.availability_pct ?? 0)}% · P ${format(line.performance_pct ?? 0)}% · Q ${format(line.quality_pct ?? 0)}%`;
    $('throughput').textContent = `${format(line.throughput_per_min ?? 0)}/min`;
    $('bottleneck').textContent = line.bottleneck || '--';
    $('wip').textContent = line.wip?.total ?? '--';
    $('alarms').textContent = line.active_alarms ?? '--';
    $('freshness').textContent = freshnessLabel(line.freshness_seconds);
  }

  async function fetchJson(url, signal) {
    const response = await fetch(url, { signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  async function populateStations(signal) {
    const live = await fetchJson('/api/live', signal);
    const selected = $('station').value;
    $('station').innerHTML = '<option value="">Toate modulele</option>' +
      Object.keys(live).sort().map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
    $('station').value = selected;
    return live;
  }

  async function load() {
    const start = new Date($('from').value);
    const end = new Date($('to').value);
    if (!Number.isFinite(start.valueOf()) || !Number.isFinite(end.valueOf()) || start >= end) {
      $('connection').textContent = 'Selecție invalidă';
      $('line-state').textContent = 'INTERVAL INVALID';
      $('line-state').className = 'danger';
      $('updated').textContent = 'Interval invalid: „De la” trebuie să fie înainte de „Până la”.';
      return;
    }

    const thisRequest = ++requestNumber;
    if (activeController) activeController.abort();
    activeController = new AbortController();
    const button = $('apply');
    button.disabled = true;
    button.textContent = 'Se încarcă...';

    const params = new URLSearchParams({
      from: start.toISOString(),
      to: end.toISOString()
    });
    if ($('station').value) params.set('station', $('station').value);

    try {
      const [history, summary] = await Promise.all([
        fetchJson('/api/history?' + new URLSearchParams([...params, ['resolution', '300']]), activeController.signal),
        fetchJson('/api/summary?' + params, activeController.signal)
      ]);
      if (thisRequest !== requestNumber) return;

      lastRows = history.rows;
      lastRange = [start, end];
      lastSummary = summary;
      updateMetricOptions(lastRows);
      drawTrend(lastRows, start, end);
      renderStations(summary.stations || {});
      renderMaintenance(summary.stations || {});
      renderEvents(summary.events || []);
      updateLineSummary(summary.line || {});
      $('connection').textContent = summary.count ? 'Sistem online' : 'Fără date în interval';
      $('updated').textContent = `Interval: ${start.toLocaleString('ro-RO')} – ${end.toLocaleString('ro-RO')} · ${format(history.count)} probe · ${format(history.sampled_count)} puncte afișate`;
    } catch (error) {
      if (error.name === 'AbortError') return;
      $('connection').textContent = 'Conexiune indisponibilă';
      $('line-state').textContent = 'OFFLINE';
      $('line-state').className = 'danger';
      $('chart-empty').hidden = false;
      $('updated').textContent = `Nu s-au putut încărca datele: ${error.message}`;
    } finally {
      if (thisRequest === requestNumber) {
        button.disabled = false;
        button.textContent = 'Actualizează';
      }
    }
  }

  async function applyQuickRange(hours) {
    activeQuickHours = hours;
    setQuickButton(hours);
    try {
      const live = await populateStations();
      const timestamps = Object.values(live)
        .map(item => new Date(item.timestamp).getTime())
        .filter(Number.isFinite);
      const latest = timestamps.length ? Math.max(...timestamps) : Date.now();
      const end = new Date(latest + 1000);
      $('to').value = dateInput(end);
      $('from').value = dateInput(new Date(end.getTime() - hours * 3600000));
      await load();
    } catch (error) {
      if (error.name !== 'AbortError') {
        $('updated').textContent = `Nu s-a putut determina ultima telemetrie: ${error.message}`;
      }
    }
  }

  document.querySelectorAll('[data-hours]').forEach(button => {
    button.onclick = () => applyQuickRange(Number(button.dataset.hours));
  });
  $('apply').onclick = () => {
    activeQuickHours = null;
    setQuickButton(null);
    load();
  };
  $('station').onchange = load;
  $('station-sort').onchange = () => lastSummary && renderStations(lastSummary.stations || {});
  $('trend-metric').onchange = () => lastRange.length && drawTrend(lastRows, lastRange[0], lastRange[1]);
  ['from', 'to'].forEach(id => $(id).addEventListener('input', () => {
    activeQuickHours = null;
    setQuickButton(null);
  }));
  window.addEventListener('resize', () => {
    if (lastRange.length) drawTrend(lastRows, lastRange[0], lastRange[1]);
  });

  applyQuickRange(24);
  setInterval(() => {
    if (activeQuickHours !== null) applyQuickRange(activeQuickHours);
    else load();
  }, 30000);
})();

