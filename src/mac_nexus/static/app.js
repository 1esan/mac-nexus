/* ==========================================================================
   Mac Nexus — Sci-Fi Orbital / JARVIS Cyber-HUD Controller (100% Offline)
   ========================================================================== */

(() => {
  'use strict';

  const CATEGORY_LABELS = {
    all: 'Все',
    pkg_cache: 'Пакетные кэши',
    app_cache: 'Кэши программ',
    downloads_inbox: 'Downloads Inbox',
    dev_projects: 'Проекты ~/Projects',
    ghost_dmgs: 'Образы DMG',
  };

  const RISK_META = {
    low: { label: 'Низкий риск', cls: 'tag-pill--emerald' },
    safe: { label: 'Безопасно', cls: 'tag-pill--emerald' },
    medium: { label: 'Умеренный', cls: 'tag-pill--amber' },
    high: { label: 'Внимание', cls: 'tag-pill--crimson' },
  };

  const ACTION_LABELS = {
    trash: 'В Корзину',
    prune: 'Очистка кэша',
    clean_cache: 'Очистка кэша',
    detach_dmg: 'Отмонтировать DMG',
    detach: 'Отмонтировать DMG',
    organize: 'Organize',
    rm_artifact: 'Удаление артефакта',
  };

  const PRESSURE_LABELS = {
    normal: 'Норма',
    warn: 'Повышено',
    warning: 'Повышено',
    critical: 'Критично',
  };

  const state = {
    dryRun: true,
    olderDays: 30,
    activeTab: 'radar',
    activeCategory: 'all',
    dupTarget: 'downloads',

    isScanning: false,
    isTransferring: false,
    isDupScanning: false,
    activeJobId: null,
    activeJobKind: null,

    telemetry: null,
    scanData: null,
    dupData: null,

    selectedItemIds: new Set(),
    selectedMediaIds: new Set(),
    selectedDupFileIds: new Set(),

    odometerTargetBytes: 0,
    odometerDisplayBytes: 0,
    odometerAnimId: null,

    terminalExpanded: true,
    terminalAutoScroll: true,
    terminalCount: 0,

    eventSource: null,
    modalConfirmFn: null,
    modalCopyText: '',
  };

  /* ------------------------------------------------------------------------
     XSS & Formatting Helpers
     ------------------------------------------------------------------------ */
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function safeNum(val, fallback = 0) {
    const n = Number(val);
    return Number.isFinite(n) ? n : fallback;
  }

  function formatGiB(bytes) {
    const b = Math.max(0, safeNum(bytes, 0));
    if (b === 0) return '0 Б';
    const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'];
    let idx = 0;
    let val = b;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx += 1;
    }
    const decimals = idx === 0 ? 0 : val >= 100 ? 1 : 2;
    return `${val.toFixed(decimals)} ${units[idx]}`;
  }

  function formatGB(bytes) {
    const b = Math.max(0, safeNum(bytes, 0));
    if (b === 0) return '0 Б';
    const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    let idx = 0;
    let val = b;
    while (val >= 1000 && idx < units.length - 1) {
      val /= 1000;
      idx += 1;
    }
    const decimals = idx === 0 ? 0 : val >= 100 ? 1 : 2;
    return `${val.toFixed(decimals)} ${units[idx]}`;
  }

  function formatDateTime(input) {
    if (!input) return 'нет данных';
    let dateObj;
    if (typeof input === 'number') {
      const ms = input < 1e12 ? input * 1000 : input;
      dateObj = new Date(ms);
    } else if (/^\d+(\.\d+)?$/.test(String(input).trim())) {
      const num = Number(input);
      dateObj = new Date(num < 1e12 ? num * 1000 : num);
    } else {
      dateObj = new Date(input);
    }
    if (Number.isNaN(dateObj.getTime())) return String(input);
    return dateObj.toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  function formatTimeShort(input) {
    if (!input) {
      return new Date().toLocaleTimeString('ru-RU', { hour12: false });
    }
    let dateObj;
    if (typeof input === 'number') {
      dateObj = new Date(input < 1e12 ? input * 1000 : input);
    } else {
      dateObj = new Date(input);
    }
    if (Number.isNaN(dateObj.getTime())) {
      return new Date().toLocaleTimeString('ru-RU', { hour12: false });
    }
    return dateObj.toLocaleTimeString('ru-RU', { hour12: false });
  }

  function $(id) {
    return document.getElementById(id);
  }

  /* ------------------------------------------------------------------------
     API Client (CSRF Header on Every POST)
     ------------------------------------------------------------------------ */
  async function apiGet(url) {
    const res = await fetch(url, {
      method: 'GET',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { error: text || `HTTP ${res.status}` };
    }
    if (!res.ok) {
      throw new Error(data.error || data.detail || `Ошибка HTTP ${res.status}`);
    }
    return data;
  }

  async function apiPost(url, payload) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Mac-Nexus-Client': '1',
      },
      body: JSON.stringify(payload || {}),
    });
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { error: text || `HTTP ${res.status}` };
    }
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || data.detail || `Ошибка HTTP ${res.status}`);
    }
    return data;
  }

  /* ------------------------------------------------------------------------
     Toast Notifications
     ------------------------------------------------------------------------ */
  function showToast(message, level = 'info') {
    const container = $('toast-container');
    if (!container) return;
    const el = document.createElement('div');
    el.className = `toast toast--${level}`;
    el.textContent = String(message);
    container.appendChild(el);
    window.setTimeout(() => {
      el.remove();
    }, 4400);
  }

  /* ------------------------------------------------------------------------
     Animated Background Canvas (Orbital Grid & Particles)
     ------------------------------------------------------------------------ */
  function initOrbitalCanvas() {
    const canvas = $('orbital-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let width = 0;
    let height = 0;
    let dpr = 1;
    let currentSpeed = 1;
    let scanBlend = 0;
    let transferBlend = 0;
    let timeAcc = 0;
    let lastTs = performance.now();

    const particles = [];
    const PARTICLE_COUNT = 64;

    function resetCanvasSize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = window.innerWidth;
      height = window.innerHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function seedParticles() {
      particles.length = 0;
      for (let i = 0; i < PARTICLE_COUNT; i += 1) {
        particles.push({
          angle: Math.random() * Math.PI * 2,
          radiusFactor: 0.14 + Math.random() * 0.68,
          angularVel: (0.04 + Math.random() * 0.12) * (Math.random() > 0.5 ? 1 : -1),
          size: 1 + Math.random() * 2.1,
          alpha: 0.25 + Math.random() * 0.55,
          phase: Math.random() * Math.PI * 2,
        });
      }
    }

    function lerp(a, b, t) {
      return a + (b - a) * t;
    }

    function drawFrame(now) {
      const dt = Math.min(0.05, Math.max(0.001, (now - lastTs) / 1000));
      lastTs = now;

      if (!document.hidden) {
        const targetSpeed = state.isTransferring ? 3.5 : state.isScanning ? 2.6 : 1.0;
        currentSpeed = lerp(currentSpeed, targetSpeed, 0.05);
        scanBlend = lerp(scanBlend, state.isScanning ? 1 : 0, 0.06);
        transferBlend = lerp(transferBlend, state.isTransferring ? 1 : 0, 0.06);

        timeAcc += dt * currentSpeed;

        ctx.clearRect(0, 0, width, height);

        const cx = width * 0.52;
        const cy = height * 0.36;
        const maxR = Math.hypot(width, height) * 0.58;

        // Primary hue blend: cyan (0, 240, 255) -> violet (139, 92, 246) on transfer
        const r = Math.round(lerp(0, 139, transferBlend));
        const g = Math.round(lerp(240, 92, transferBlend));
        const b = Math.round(lerp(255, 246, transferBlend));

        // Concentric Orbital Rings
        const ringCount = 6;
        for (let i = 1; i <= ringCount; i += 1) {
          const ringR = (maxR / ringCount) * i;
          ctx.beginPath();
          ctx.arc(cx, cy, ringR, 0, Math.PI * 2);
          ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${0.045 + scanBlend * 0.04 + transferBlend * 0.04})`;
          ctx.lineWidth = i % 2 === 0 ? 1.1 : 0.7;
          if (i % 2 === 1) {
            ctx.setLineDash([6, 12]);
            ctx.lineDashOffset = -timeAcc * (14 + i * 3);
          } else {
            ctx.setLineDash([]);
          }
          ctx.stroke();
        }
        ctx.setLineDash([]);

        // Rotating Orbital Sweep Arcs
        for (let arcIdx = 0; arcIdx < 3; arcIdx += 1) {
          const arcR = maxR * (0.24 + arcIdx * 0.22);
          const start = timeAcc * (0.28 - arcIdx * 0.07) + arcIdx * 2.1;
          const span = 0.55 + scanBlend * 0.45 + transferBlend * 0.5;
          ctx.beginPath();
          ctx.arc(cx, cy, arcR, start, start + span);
          ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${0.14 + scanBlend * 0.16 + transferBlend * 0.2})`;
          ctx.lineWidth = 2;
          ctx.stroke();
        }

        // Subtle Radial Sector Lines
        ctx.strokeStyle = `rgba(0, 240, 255, ${0.03 + scanBlend * 0.03})`;
        ctx.lineWidth = 1;
        for (let s = 0; s < 12; s += 1) {
          const ang = (s / 12) * Math.PI * 2 + timeAcc * 0.03;
          ctx.beginPath();
          ctx.moveTo(cx + Math.cos(ang) * 48, cy + Math.sin(ang) * 48);
          ctx.lineTo(cx + Math.cos(ang) * maxR, cy + Math.sin(ang) * maxR);
          ctx.stroke();
        }

        // Orbiting Particles & Soft Filaments
        const coords = [];
        for (let i = 0; i < particles.length; i += 1) {
          const p = particles[i];
          p.angle += p.angularVel * dt * currentSpeed;
          const orbitR = maxR * p.radiusFactor;
          const px = cx + Math.cos(p.angle) * orbitR;
          const py = cy + Math.sin(p.angle) * orbitR * 0.78;
          coords.push({ x: px, y: py, p });

          const glowAlpha = p.alpha * (0.65 + 0.35 * Math.sin(timeAcc * 1.8 + p.phase));
          ctx.beginPath();
          ctx.arc(px, py, p.size, 0, Math.PI * 2);
          ctx.fillStyle = i % 4 === 0
            ? `rgba(139, 92, 246, ${glowAlpha})`
            : `rgba(${r}, ${g}, ${b}, ${glowAlpha})`;
          ctx.fill();
        }

        // Connect nearby particles
        const linkDist = 115;
        ctx.lineWidth = 0.7;
        for (let i = 0; i < coords.length; i += 1) {
          for (let j = i + 1; j < coords.length; j += 1) {
            const dx = coords[i].x - coords[j].x;
            const dy = coords[i].y - coords[j].y;
            const dist = Math.hypot(dx, dy);
            if (dist < linkDist) {
              const alpha = (1 - dist / linkDist) * (0.09 + transferBlend * 0.09);
              ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
              ctx.beginPath();
              ctx.moveTo(coords[i].x, coords[i].y);
              ctx.lineTo(coords[j].x, coords[j].y);
              ctx.stroke();
            }
          }
        }
      }

      if (!reduceMotion) {
        window.requestAnimationFrame(drawFrame);
      }
    }

    resetCanvasSize();
    seedParticles();
    window.addEventListener('resize', resetCanvasSize);
    if (reduceMotion) {
      drawFrame(performance.now());
    } else {
      window.requestAnimationFrame(drawFrame);
    }
  }

  /* ------------------------------------------------------------------------
     SVG Orbital Reactor & Odometer Rendering
     ------------------------------------------------------------------------ */
  function setArcProgress(circleEl, radius, percent) {
    if (!circleEl) return;
    const clamped = Math.max(0, Math.min(100, safeNum(percent, 0)));
    const circumference = 2 * Math.PI * radius;
    const offset = circumference * (1 - clamped / 100);
    circleEl.style.strokeDasharray = `${circumference.toFixed(2)}`;
    circleEl.style.strokeDashoffset = `${offset.toFixed(2)}`;
  }

  function setRingTone(circleEl, tone) {
    if (!circleEl) return;
    circleEl.classList.remove(
      'ring-progress--cyan',
      'ring-progress--violet',
      'ring-progress--emerald',
      'ring-progress--amber',
      'ring-progress--crimson'
    );
    circleEl.classList.add(`ring-progress--${tone}`);
  }

  function animateOdometerTo(targetBytes, deltaBytes = 0) {
    const valEl = $('odometer-value');
    const cardEl = valEl ? valEl.closest('.tele-chip--odometer') : null;
    const deltaEl = $('odometer-delta');

    const target = Math.max(0, safeNum(targetBytes, 0));
    state.odometerTargetBytes = target;

    if (deltaBytes > 0 && deltaEl) {
      deltaEl.textContent = `+${formatGiB(deltaBytes)}`;
      deltaEl.hidden = false;
      if (cardEl) {
        cardEl.classList.remove('is-pulsing');
        void cardEl.offsetWidth;
        cardEl.classList.add('is-pulsing');
      }
      window.setTimeout(() => {
        deltaEl.hidden = true;
      }, 4200);
    }

    if (state.odometerAnimId) {
      window.cancelAnimationFrame(state.odometerAnimId);
    }

    const startVal = state.odometerDisplayBytes;
    const diff = target - startVal;
    if (Math.abs(diff) < 1) {
      state.odometerDisplayBytes = target;
      if (valEl) valEl.textContent = formatGiB(target);
      return;
    }

    const duration = 850;
    const startTime = performance.now();

    function step(now) {
      const progress = Math.min(1, (now - startTime) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      state.odometerDisplayBytes = startVal + diff * eased;
      if (valEl) {
        valEl.textContent = formatGiB(state.odometerDisplayBytes);
      }
      if (progress < 1) {
        state.odometerAnimId = window.requestAnimationFrame(step);
      } else {
        state.odometerDisplayBytes = target;
        if (valEl) valEl.textContent = formatGiB(target);
        state.odometerAnimId = null;
      }
    }

    state.odometerAnimId = window.requestAnimationFrame(step);
  }

  function renderTelemetry(data) {
    state.telemetry = data || {};
    const planes = data?.planes || {};
    const nvme = planes.nvme || {};
    const ssd = planes.ssd || {};
    const sb = planes.secondbrain || {};
    const sys = data?.system || {};
    const hist = data?.history || {};

    // Reactor 1: Internal NVMe
    const nvmePct = safeNum(nvme.used_percent, 0);
    const nvmeMounted = nvme.mounted !== false;
    $('nvme-mount').textContent = nvme.mount || '/System/Volumes/Data';
    $('nvme-percent').textContent = nvmeMounted ? `${nvmePct.toFixed(1)}%` : 'OFF';
    $('nvme-used-total').textContent = `${formatGB(nvme.used_bytes)} / ${formatGB(nvme.total_bytes)}`;
    $('nvme-free').textContent = `${formatGB(nvme.free_bytes)} (${formatGiB(nvme.free_bytes)})`;
    $('nvme-purgeable').textContent = formatGiB(nvme.purgeable_bytes);
    $('nvme-gib-detail').textContent = `${formatGiB(nvme.used_bytes)} / ${formatGiB(nvme.total_bytes)}`;

    const nvmeArcMain = $('nvme-arc-main');
    const nvmeBadge = $('nvme-status-badge');
    const nvmeCard = $('reactor-nvme');
    nvmeCard.classList.remove('reactor-card--warning', 'reactor-card--critical');
    if (nvmePct >= 88) {
      setRingTone(nvmeArcMain, 'crimson');
      nvmeBadge.className = 'badge-chip badge-chip--crimson';
      nvmeBadge.textContent = 'ВЫСОКАЯ НАГРУЗКА';
      nvmeCard.classList.add('reactor-card--critical');
    } else if (nvmePct >= 75) {
      setRingTone(nvmeArcMain, 'amber');
      nvmeBadge.className = 'badge-chip badge-chip--amber';
      nvmeBadge.textContent = 'ВНИМАНИЕ';
      nvmeCard.classList.add('reactor-card--warning');
    } else {
      setRingTone(nvmeArcMain, 'cyan');
      nvmeBadge.className = 'badge-chip badge-chip--cyan';
      nvmeBadge.textContent = 'НОРМА';
    }
    setArcProgress(nvmeArcMain, 68, nvmeMounted ? nvmePct : 0);
    const purgePct = safeNum(nvme.total_bytes) > 0
      ? Math.min(100, (safeNum(nvme.purgeable_bytes) / safeNum(nvme.total_bytes)) * 100 * 4)
      : 0;
    setArcProgress($('nvme-arc-inner'), 53, nvmeMounted ? Math.max(8, purgePct) : 0);

    // Reactor 2: External APFS SSD
    const ssdMounted = Boolean(ssd.mounted);
    const ssdPct = safeNum(ssd.used_percent, 0);
    const shelves = Array.isArray(ssd.shelves) ? ssd.shelves : [];
    const existingShelves = shelves.filter((s) => s && s.exists).length;

    $('ssd-mount').textContent = ssd.mount || '/Volumes/secret for mine';
    $('ssd-percent').textContent = ssdMounted ? `${ssdPct.toFixed(1)}%` : 'OFF';
    $('ssd-center-label').textContent = ssdMounted ? 'ЗАНЯТО' : 'ОТКЛЮЧЁН';
    $('ssd-used-total').textContent = ssdMounted
      ? `${formatGB(ssd.used_bytes)} / ${formatGB(ssd.total_bytes)}`
      : 'Том не подключён';
    $('ssd-free').textContent = ssdMounted
      ? `${formatGB(ssd.free_bytes)} (${formatGiB(ssd.free_bytes)})`
      : '--';
    $('ssd-fs-type').textContent = ssdMounted ? (ssd.fs_type || 'APFS') : 'Не смонтирован';
    $('ssd-shelves-summary').textContent = ssdMounted
      ? `${existingShelves} / ${shelves.length || 6} активны`
      : 'Ожидает подключения';

    const ssdBadge = $('ssd-status-badge');
    const ssdArcMain = $('ssd-arc-main');
    if (ssdMounted) {
      ssdBadge.className = 'badge-chip badge-chip--violet';
      ssdBadge.textContent = (ssd.fs_type || 'APFS').toUpperCase();
      setRingTone(ssdArcMain, 'violet');
      setArcProgress(ssdArcMain, 68, ssdPct);
      const shelfRatio = shelves.length > 0 ? (existingShelves / shelves.length) * 100 : 100;
      setArcProgress($('ssd-arc-inner'), 53, shelfRatio);
    } else {
      ssdBadge.className = 'badge-chip badge-chip--amber';
      ssdBadge.textContent = 'НЕ ПОДКЛЮЧЁН';
      setRingTone(ssdArcMain, 'amber');
      setArcProgress(ssdArcMain, 68, 0);
      setArcProgress($('ssd-arc-inner'), 53, 0);
    }

    $('ssd-unmounted-alert').hidden = ssdMounted;

    // Reactor 3: SecondBrain iCloud
    const sbMounted = Boolean(sb.mounted);
    const sbRisk = Boolean(sb.has_node_modules_risk);
    const sbNotes = safeNum(sb.md_notes_count, 0);
    $('sb-path').textContent = sb.path || 'SecondBrain iCloud';
    $('sb-notes-big').textContent = sbMounted ? String(sbNotes) : 'OFF';
    $('sb-size').textContent = `${formatGiB(sb.size_bytes)} (${formatGB(sb.size_bytes)})`;
    $('sb-notes-count').textContent = `${sbNotes} шт.`;
    $('sb-mounted').textContent = sbMounted ? 'Подключён' : 'Не найден';

    const sbRiskEl = $('sb-risk');
    const sbBadge = $('sb-status-badge');
    const sbArcMain = $('sb-arc-main');
    if (!sbMounted) {
      sbRiskEl.textContent = 'Нет доступа';
      sbRiskEl.className = 'metric-val metric-val--amber';
      sbBadge.className = 'badge-chip badge-chip--amber';
      sbBadge.textContent = 'ОФФЛАЙН';
      setRingTone(sbArcMain, 'amber');
      setArcProgress(sbArcMain, 68, 0);
      setArcProgress($('sb-arc-inner'), 53, 0);
    } else if (sbRisk) {
      sbRiskEl.textContent = 'Обнаружен node_modules!';
      sbRiskEl.className = 'metric-val metric-val--crimson';
      sbBadge.className = 'badge-chip badge-chip--amber';
      sbBadge.textContent = 'ТРЕБУЕТ ВНИМАНИЯ';
      setRingTone(sbArcMain, 'amber');
      setArcProgress(sbArcMain, 68, 72);
      setArcProgress($('sb-arc-inner'), 53, 60);
    } else {
      sbRiskEl.textContent = '0 (Чистый контур)';
      sbRiskEl.className = 'metric-val metric-val--emerald';
      sbBadge.className = 'badge-chip badge-chip--emerald';
      sbBadge.textContent = 'ЧИСТО';
      setRingTone(sbArcMain, 'emerald');
      setArcProgress(sbArcMain, 68, 100);
      setArcProgress($('sb-arc-inner'), 53, Math.min(100, Math.max(20, sbNotes / 5)));
    }

    // Telemetry Chips
    $('chip-ram-value').textContent = `${formatGiB(sys.ram_used_bytes)} / ${formatGiB(sys.ram_total_bytes)}`;
    const cpuLoad = safeNum(sys.cpu_load_1m, 0).toFixed(2);
    const cpuCores = safeNum(sys.cpu_cores, 0);
    $('chip-ram-sub').textContent = `CPU 1m: ${cpuLoad} · ${cpuCores || '--'} ядер`;

    $('chip-swap-value').textContent = formatGiB(sys.swap_used_bytes);
    const pressureKey = String(sys.ram_pressure || 'normal').toLowerCase();
    $('chip-swap-sub').textContent = `Давление RAM: ${PRESSURE_LABELS[pressureKey] || sys.ram_pressure || 'Норма'}`;

    const ghostDmgs = safeNum(sys.ghost_dmgs_count, 0);
    const dmgEl = $('chip-dmg-value');
    dmgEl.textContent = `${ghostDmgs} шт.`;
    dmgEl.className = ghostDmgs > 0 ? 'tele-chip-value text-amber' : 'tele-chip-value text-emerald';
    $('chip-dmg-sub').textContent = ghostDmgs > 0 ? 'Требуется отмонтирование' : 'Призрачных образов нет';

    const orcaCount = safeNum(sys.orca_live_worktrees, 0);
    $('chip-orca-value').textContent = `${orcaCount} акт.`;
    $('chip-orca-sub').textContent = orcaCount > 0 ? 'Worktrees защищены LIVE LOCK' : 'Активные worktrees не найдены';

    // Odometer
    const totalReclaimed = safeNum(hist.total_reclaimed_bytes, 0);
    if (totalReclaimed !== state.odometerTargetBytes) {
      animateOdometerTo(totalReclaimed, Math.max(0, totalReclaimed - state.odometerTargetBytes));
    } else if (state.odometerDisplayBytes === 0) {
      $('odometer-value').textContent = formatGiB(totalReclaimed);
    }
    $('odometer-sub').textContent = `Последний запуск: ${formatDateTime(hist.last_run_at)}`;

    // Tab 4 Shelves & Plane Details
    renderPlanesControlTab(data);
    updateActionButtonsState();
  }

  /* ------------------------------------------------------------------------
     Scan Data & Tab 1 (РАДАР ОЧИСТКИ) + Tab 2 (ТЕЛЕПОРТ НА SSD)
     ------------------------------------------------------------------------ */
  function setScanningVisuals(active) {
    state.isScanning = active;
    const widget = $('radar-sweep-widget');
    const stateText = $('radar-state-text');
    const banner = $('radar-scan-banner');
    if (widget) widget.classList.toggle('is-scanning', active);
    if (stateText) stateText.textContent = active ? 'СКАН...' : 'ГОТОВ';
    if (banner) banner.hidden = !active;
  }

  function renderCategoryCounts(items) {
    const counts = { all: items.length };
    for (const it of items) {
      const cat = it.category || 'other';
      counts[cat] = (counts[cat] || 0) + 1;
    }
    const buttons = document.querySelectorAll('#radar-category-chips .filter-chip');
    for (const btn of buttons) {
      const cat = btn.dataset.cat;
      const baseLabel = CATEGORY_LABELS[cat] || cat;
      const count = counts[cat] || 0;
      btn.textContent = `${baseLabel} (${count})`;
      btn.classList.toggle('is-active', cat === state.activeCategory);
    }
  }

  function renderRadarItems() {
    const listEl = $('radar-items-list');
    if (!listEl) return;

    const scan = state.scanData;
    const allItems = Array.isArray(scan?.items) ? scan.items : [];
    const summary = scan?.summary || {};

    $('badge-tab-radar').textContent = String(allItems.length);
    $('sum-ready-bytes').textContent = formatGiB(summary.reclaimable_ready_bytes);
    $('sum-locked-bytes').textContent = formatGiB(summary.reclaimable_locked_bytes);
    $('sum-offload-bytes').textContent = formatGiB(summary.offloadable_media_bytes);
    $('sum-organize-count').textContent = String(safeNum(summary.organize_candidates_count, 0));

    renderCategoryCounts(allItems);

    const filtered = state.activeCategory === 'all'
      ? allItems
      : allItems.filter((it) => it.category === state.activeCategory);

    if (filtered.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state">
          <strong>Кандидаты в этой категории не найдены</strong>
          <span>Попробуйте выбрать фильтр «Все» или изменить порог возраста проектов (14д / 30д / 60д).</span>
        </div>`;
      updateRadarDockStats();
      return;
    }

    const html = filtered.map((item) => {
      const id = String(item.id || '');
      const locked = Boolean(item.locked);
      const selected = !locked && state.selectedItemIds.has(id);
      const catLabel = CATEGORY_LABELS[item.category] || item.category || 'Кандидат';
      const riskObj = RISK_META[String(item.risk || 'low').toLowerCase()] || RISK_META.low;
      const actionLabel = ACTION_LABELS[item.action_kind] || item.action_kind || 'Очистка';
      const ageDays = item.age_days !== null && item.age_days !== undefined ? `${safeNum(item.age_days)} дн.` : null;
      const matchedCount = safeNum(item.matched_count, 0);

      return `
        <article class="item-card ${locked ? 'is-locked' : ''} ${selected ? 'is-selected' : ''}" data-item-id="${escapeHtml(id)}">
          <input
            type="checkbox"
            class="item-checkbox js-radar-check"
            data-id="${escapeHtml(id)}"
            ${selected ? 'checked' : ''}
            ${locked ? 'disabled aria-disabled="true"' : ''}
            aria-label="${escapeHtml(item.title || id)}"
          />
          <div class="item-main">
            <div class="item-top-row">
              <span class="item-title">${escapeHtml(item.title || id)}</span>
              ${locked ? '<span class="live-lock-badge"><span class="live-lock-dot"></span>LIVE LOCK</span>' : ''}
              <span class="tag-pill tag-pill--cyan">${escapeHtml(catLabel)}</span>
              <span class="tag-pill ${escapeHtml(riskObj.cls)}">${escapeHtml(riskObj.label)}</span>
              <span class="tag-pill">${escapeHtml(actionLabel)}</span>
              ${ageDays ? `<span class="tag-pill">${escapeHtml(ageDays)}</span>` : ''}
              ${matchedCount > 1 ? `<span class="tag-pill">${matchedCount} объектов</span>` : ''}
            </div>
            ${item.subtitle ? `<div class="item-subtitle">${escapeHtml(item.subtitle)}</div>` : ''}
            ${item.path ? `<div class="item-path" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</div>` : ''}
            ${locked ? `<div class="lock-reason-box">Защита процесса: ${escapeHtml(item.lock_reason || 'Приложение или Orca worktree активно в данный момент')}</div>` : ''}
          </div>
          <div class="item-right">
            <span class="item-size">${escapeHtml(formatGiB(item.size_bytes))}</span>
            <span class="item-size-sub">${escapeHtml(formatGB(item.size_bytes))}</span>
          </div>
        </article>
      `;
    }).join('');

    listEl.innerHTML = html;
    updateRadarDockStats();
  }

  function updateRadarDockStats() {
    const items = Array.isArray(state.scanData?.items) ? state.scanData.items : [];
    let selectedBytes = 0;
    let selectedCount = 0;
    for (const item of items) {
      if (!item.locked && state.selectedItemIds.has(String(item.id))) {
        selectedCount += 1;
        selectedBytes += safeNum(item.size_bytes, 0);
      }
    }
    $('radar-selected-count').textContent = String(selectedCount);
    $('radar-selected-bytes').textContent = formatGiB(selectedBytes);
    updateActionButtonsState();
  }

  function renderTeleportCandidates() {
    const listEl = $('teleport-list');
    if (!listEl) return;

    const candidates = Array.isArray(state.scanData?.offload_candidates)
      ? state.scanData.offload_candidates
      : [];

    $('badge-tab-teleport').textContent = String(candidates.length);

    if (candidates.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state">
          <strong>Тяжёлые медиа-кандидаты на Desktop/Downloads не обнаружены</strong>
          <span>Сюда попадают крупные .mov / .mp4 / .mkv и архивы, безопасные для SHA-256 переноса на External APFS SSD.</span>
        </div>`;
      updateTeleportDockStats();
      return;
    }

    const html = candidates.map((cand) => {
      const id = String(cand.id || '');
      const selected = state.selectedMediaIds.has(id);
      const label = cand.display_label || cand.name || id;
      const ext = cand.ext ? String(cand.ext).toUpperCase() : 'MEDIA';
      const ageDays = cand.age_days !== null && cand.age_days !== undefined ? `${safeNum(cand.age_days)} дн.` : '--';

      return `
        <article class="item-card ${selected ? 'is-selected' : ''}" data-media-id="${escapeHtml(id)}">
          <input
            type="checkbox"
            class="item-checkbox js-media-check"
            data-id="${escapeHtml(id)}"
            ${selected ? 'checked' : ''}
            aria-label="${escapeHtml(label)}"
          />
          <div class="item-main">
            <div class="item-top-row">
              <span class="item-title">${escapeHtml(label)}</span>
              <span class="tag-pill tag-pill--violet">${escapeHtml(ext)}</span>
              <span class="tag-pill">${escapeHtml(ageDays)}</span>
              ${cand.mtime ? `<span class="tag-pill">${escapeHtml(formatDateTime(cand.mtime))}</span>` : ''}
            </div>
            <div class="item-path" title="${escapeHtml(cand.rel_path || cand.name || '')}">
              Источник: ${escapeHtml(cand.rel_path || cand.name || '')}
            </div>
            <div class="dest-preview-line">
              &rarr; ${escapeHtml(cand.dest_preview || '/Volumes/secret for mine/04_Media_Archive/Screen_Recordings/')}
            </div>
          </div>
          <div class="item-right">
            <span class="item-size">${escapeHtml(formatGiB(cand.size_bytes))}</span>
            <span class="item-size-sub">${escapeHtml(formatGB(cand.size_bytes))}</span>
          </div>
        </article>
      `;
    }).join('');

    listEl.innerHTML = html;
    updateTeleportDockStats();
  }

  function updateTeleportDockStats() {
    const candidates = Array.isArray(state.scanData?.offload_candidates)
      ? state.scanData.offload_candidates
      : [];
    let selectedBytes = 0;
    let selectedCount = 0;
    for (const c of candidates) {
      if (state.selectedMediaIds.has(String(c.id))) {
        selectedCount += 1;
        selectedBytes += safeNum(c.size_bytes, 0);
      }
    }
    $('teleport-selected-count').textContent = String(selectedCount);
    $('teleport-selected-bytes').textContent = formatGiB(selectedBytes);
    updateActionButtonsState();
  }

  /* ------------------------------------------------------------------------
     Tab 3: ДУБЛИКАТЫ
     ------------------------------------------------------------------------ */
  function renderDuplicates() {
    const listEl = $('duplicates-list');
    if (!listEl) return;

    const groups = Array.isArray(state.dupData?.groups) ? state.dupData.groups : [];
    const totalWasted = safeNum(state.dupData?.total_wasted_bytes, 0);

    $('badge-tab-duplicates').textContent = String(groups.length);
    $('dup-groups-count').textContent = String(groups.length);
    $('dup-wasted-bytes').textContent = formatGiB(totalWasted);

    if (!state.dupData) {
      listEl.innerHTML = `
        <div class="empty-state">
          <strong>Поиск дубликатов ещё не запускался</strong>
          <span>Выберите контур (Downloads, Desktop или SSD Media) и нажмите «Сканировать дубликаты».</span>
        </div>`;
      updateDupDockStats();
      return;
    }

    if (groups.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state">
          <strong>Дубликатов не найдено</strong>
          <span>В выбранной области нет идентичных копий файлов размером от 1 МиБ.</span>
        </div>`;
      updateDupDockStats();
      return;
    }

    const html = groups.map((group, gIdx) => {
      const files = Array.isArray(group.files) ? group.files : [];
      const rowsHtml = files.map((file, idx) => {
        const isOrigin = idx === 0;
        const fid = String(file.id || '');
        const checked = !isOrigin && state.selectedDupFileIds.has(fid);
        return `
          <div class="dup-file-row ${isOrigin ? 'dup-file-row--origin' : ''}">
            <input
              type="checkbox"
              class="item-checkbox js-dup-check"
              data-id="${escapeHtml(fid)}"
              ${checked ? 'checked' : ''}
              ${isOrigin ? 'disabled aria-disabled="true"' : ''}
              aria-label="${escapeHtml(file.name || fid)}"
            />
            ${isOrigin
              ? '<span class="origin-badge">ОРИГИНАЛ / СОХРАНИТЬ</span>'
              : '<span class="copy-badge">КОПИЯ</span>'}
            <div class="dup-file-info">
              <div class="dup-file-name">${escapeHtml(file.name || 'Файл')}</div>
              <div class="dup-file-path" title="${escapeHtml(file.path || '')}">${escapeHtml(file.path || '')}</div>
            </div>
            <div class="dup-file-mtime">${escapeHtml(formatDateTime(file.mtime))}</div>
          </div>
        `;
      }).join('');

      return `
        <article class="dup-group-card">
          <div class="dup-group-head">
            <span class="dup-group-id">ГРУППА #${gIdx + 1} · ${escapeHtml(group.hash_or_id || '')}</span>
            <div class="dup-group-stats">
              <span class="tag-pill">Размер файла: ${escapeHtml(formatGiB(group.size_bytes))}</span>
              <span class="tag-pill tag-pill--amber">Избыточно: ${escapeHtml(formatGiB(group.wasted_bytes))}</span>
            </div>
          </div>
          <div class="dup-files-list">${rowsHtml}</div>
        </article>
      `;
    }).join('');

    listEl.innerHTML = html;
    updateDupDockStats();
  }

  function updateDupDockStats() {
    const groups = Array.isArray(state.dupData?.groups) ? state.dupData.groups : [];
    let count = 0;
    let bytes = 0;
    for (const g of groups) {
      const files = Array.isArray(g.files) ? g.files : [];
      const unitSize = safeNum(g.size_bytes, 0);
      for (let i = 1; i < files.length; i += 1) {
        if (state.selectedDupFileIds.has(String(files[i].id))) {
          count += 1;
          bytes += unitSize;
        }
      }
    }
    $('dup-selected-count').textContent = String(count);
    $('dup-selected-bytes').textContent = formatGiB(bytes);
    updateActionButtonsState();
  }

  /* ------------------------------------------------------------------------
     Tab 4: ПУЛЬТ КОНТУРОВ
     ------------------------------------------------------------------------ */
  function renderPlanesControlTab(data) {
    const planes = data?.planes || {};
    const nvme = planes.nvme || {};
    const ssd = planes.ssd || {};
    const sb = planes.secondbrain || {};
    const shelves = Array.isArray(ssd.shelves) ? ssd.shelves : [];

    const shelvesGrid = $('shelves-grid');
    const mountPill = $('shelves-mount-pill');
    if (mountPill) {
      mountPill.textContent = ssd.mounted ? `ПОЛОК: ${shelves.length}` : 'SSD ОТКЛЮЧЁН';
      mountPill.className = ssd.mounted ? 'badge-chip badge-chip--violet' : 'badge-chip badge-chip--amber';
    }

    if (shelvesGrid) {
      if (!ssd.mounted) {
        shelvesGrid.innerHTML = `
          <div class="empty-state">
            <strong>Внешний SSD не смонтирован</strong>
            <span>Подключите <code>/Volumes/secret for mine</code>, чтобы увидеть статус полок 01..99.</span>
          </div>`;
      } else if (shelves.length === 0) {
        shelvesGrid.innerHTML = `
          <div class="empty-state">
            <strong>Список полок пуст</strong>
            <span>Бэкенд не вернул каталоги 01..99 для этого тома.</span>
          </div>`;
      } else {
        shelvesGrid.innerHTML = shelves.map((shelf) => {
          const name = String(shelf.name || '');
          const exists = Boolean(shelf.exists);
          const prefixMatch = name.match(/^(\d{2})/);
          const numCode = prefixMatch ? prefixMatch[1] : '--';
          return `
            <div class="shelf-card ${exists ? 'shelf-card--exists' : 'shelf-card--missing'}">
              <div class="shelf-top">
                <span class="shelf-num">${escapeHtml(numCode)}</span>
                <span class="badge-chip ${exists ? 'badge-chip--emerald' : 'badge-chip--amber'}">
                  ${exists ? 'АКТИВНА' : 'ОТСУТСТВУЕТ'}
                </span>
              </div>
              <div class="shelf-name">${escapeHtml(name)}</div>
            </div>
          `;
        }).join('');
      }
    }

    const detailGrid = $('planes-detail-grid');
    if (detailGrid) {
      detailGrid.innerHTML = `
        <div class="plane-detail-card">
          <div class="plane-detail-title">Контур 1 · ${escapeHtml(nvme.name || 'Internal NVMe')}</div>
          <div class="plane-detail-path">${escapeHtml(nvme.mount || '/System/Volumes/Data')}</div>
          <div class="metric-row">
            <span class="metric-key">Свободно</span>
            <span class="metric-val metric-val--emerald">${escapeHtml(formatGiB(nvme.free_bytes))}</span>
          </div>
          <div class="metric-row">
            <span class="metric-key">Purgeable</span>
            <span class="metric-val metric-val--cyan">${escapeHtml(formatGiB(nvme.purgeable_bytes))}</span>
          </div>
        </div>
        <div class="plane-detail-card">
          <div class="plane-detail-title">Контур 2 · ${escapeHtml(ssd.name || 'External APFS SSD')}</div>
          <div class="plane-detail-path">${escapeHtml(ssd.mount || '/Volumes/secret for mine')}</div>
          <div class="metric-row">
            <span class="metric-key">Формат</span>
            <span class="metric-val metric-val--violet">${escapeHtml(ssd.fs_type || (ssd.mounted ? 'APFS' : 'Не подключён'))}</span>
          </div>
          <div class="metric-row">
            <span class="metric-key">Свободно</span>
            <span class="metric-val metric-val--emerald">${ssd.mounted ? escapeHtml(formatGiB(ssd.free_bytes)) : '--'}</span>
          </div>
        </div>
        <div class="plane-detail-card">
          <div class="plane-detail-title">Контур 3 · ${escapeHtml(sb.name || 'SecondBrain iCloud')}</div>
          <div class="plane-detail-path">${escapeHtml(sb.path || 'SecondBrain')}</div>
          <div class="metric-row">
            <span class="metric-key">Заметок .md</span>
            <span class="metric-val metric-val--cyan">${safeNum(sb.md_notes_count, 0)}</span>
          </div>
          <div class="metric-row">
            <span class="metric-key">node_modules риск</span>
            <span class="metric-val ${sb.has_node_modules_risk ? 'metric-val--crimson' : 'metric-val--emerald'}">
              ${sb.has_node_modules_risk ? 'ОБНАРУЖЕН' : 'НЕТ'}
            </span>
          </div>
        </div>
      `;
    }
  }

  /* ------------------------------------------------------------------------
     DRY-RUN Toggle & Button State Sync
     ------------------------------------------------------------------------ */
  function syncDryRunUI() {
    const toggleBtn = $('dry-run-toggle');
    const stateLabel = $('dry-run-state-label');
    const radarHint = $('radar-mode-hint');
    const teleportHint = $('teleport-mode-hint');

    if (toggleBtn) {
      toggleBtn.setAttribute('aria-checked', state.dryRun ? 'true' : 'false');
      toggleBtn.classList.toggle('dry-run-pill--safe', state.dryRun);
      toggleBtn.classList.toggle('dry-run-pill--live', !state.dryRun);
    }
    if (stateLabel) {
      stateLabel.textContent = state.dryRun ? 'ВКЛ · БЕЗОПАСНО' : 'ВЫКЛ · БОЕВОЙ РЕЖИМ';
    }

    const hintText = state.dryRun ? 'DRY-RUN СИМУЛЯЦИЯ' : 'БОЕВОЕ ВЫПОЛНЕНИЕ';
    if (radarHint) {
      radarHint.textContent = hintText;
      radarHint.className = `dock-mode-tag ${state.dryRun ? 'dock-mode-tag--safe' : 'dock-mode-tag--live'}`;
    }
    if (teleportHint) {
      teleportHint.textContent = hintText;
      teleportHint.className = `dock-mode-tag ${state.dryRun ? 'dock-mode-tag--safe' : 'dock-mode-tag--live'}`;
    }

    const cleanLabel = $('btn-execute-clean-label');
    if (cleanLabel) {
      cleanLabel.textContent = state.dryRun ? 'ОЧИСТИТЬ ВЫБРАННОЕ (DRY-RUN)' : 'ОЧИСТИТЬ ВЫБРАННОЕ';
    }
  }

  function updateActionButtonsState() {
    const cleanBtn = $('btn-execute-clean');
    const offloadBtn = $('btn-execute-offload');
    const trashDupBtn = $('btn-trash-duplicates');
    const scanDupBtn = $('btn-scan-duplicates');

    if (cleanBtn) {
      cleanBtn.disabled = state.selectedItemIds.size === 0 || state.isTransferring;
    }
    if (offloadBtn) {
      const ssdMounted = Boolean(state.telemetry?.planes?.ssd?.mounted);
      const blockedNoSsd = !ssdMounted && !state.dryRun;
      offloadBtn.disabled = state.selectedMediaIds.size === 0 || state.isTransferring || blockedNoSsd;
    }
    if (trashDupBtn) {
      trashDupBtn.disabled = state.selectedDupFileIds.size === 0 || state.isTransferring || state.isDupScanning;
    }
    if (scanDupBtn) {
      scanDupBtn.disabled = state.isDupScanning;
    }

    const beamCard = $('teleport-beam-panel');
    if (beamCard) {
      beamCard.classList.toggle('is-transferring', state.isTransferring && state.activeJobKind === 'offload');
    }
  }

  /* ------------------------------------------------------------------------
     Bottom Collapsible Live SSE Cyber-Terminal
     ------------------------------------------------------------------------ */
  function resolveLogTag(rawType, message) {
    const msg = String(message || '');
    const match = msg.match(/^\[([A-Z0-9_-]+)\]\s*(.*)$/);
    if (match) {
      const tag = `[${match[1]}]`;
      const body = match[2];
      return { tag, body, cls: classifyTagStyle(tag, rawType) };
    }
    const t = String(rawType || 'log').toLowerCase();
    if (t === 'reclaimed') return { tag: '[DONE]', body: msg, cls: 'log-tag--done' };
    if (t === 'done') return { tag: '[DONE]', body: msg, cls: 'log-tag--done' };
    if (t === 'progress') return { tag: '[SHA-256]', body: msg, cls: 'log-tag--sha' };
    if (t === 'error') return { tag: '[ERROR]', body: msg, cls: 'log-tag--err' };
    return { tag: '[INFO]', body: msg, cls: 'log-tag--info' };
  }

  function classifyTagStyle(tag, rawType) {
    const upper = tag.toUpperCase();
    if (upper.includes('SCAN')) return 'log-tag--scan';
    if (upper.includes('LOCK')) return 'log-tag--lock';
    if (upper.includes('SHA') || upper.includes('TELEPORT')) return 'log-tag--sha';
    if (upper.includes('DRY')) return 'log-tag--dry';
    if (upper.includes('DONE') || rawType === 'done' || rawType === 'reclaimed') return 'log-tag--done';
    if (upper.includes('ERR') || upper.includes('FAIL') || upper.includes('WARN')) return 'log-tag--err';
    return 'log-tag--info';
  }

  function appendTerminalLog(ev) {
    const logEl = $('terminal-log');
    const previewEl = $('terminal-preview-line');
    const counterEl = $('terminal-count');
    if (!logEl) return;

    const { tag, body, cls } = resolveLogTag(ev.type, ev.message);
    const tsStr = formatTimeShort(ev.ts);

    const row = document.createElement('div');
    row.className = 'log-line';

    const tsSpan = document.createElement('span');
    tsSpan.className = 'log-ts';
    tsSpan.textContent = `[${tsStr}]`;

    const tagSpan = document.createElement('span');
    tagSpan.className = `log-tag ${cls}`;
    tagSpan.textContent = tag;

    const msgSpan = document.createElement('span');
    msgSpan.className = 'log-msg';
    const extraBytes = safeNum(ev.bytes, 0) > 0 && ev.type === 'reclaimed'
      ? ` (+${formatGiB(ev.bytes)})`
      : '';
    msgSpan.textContent = `${body}${extraBytes}`;

    row.appendChild(tsSpan);
    row.appendChild(tagSpan);
    row.appendChild(msgSpan);
    logEl.appendChild(row);

    // Keep terminal bounded to 350 lines
    while (logEl.children.length > 350) {
      logEl.removeChild(logEl.firstChild);
    }

    state.terminalCount += 1;
    if (counterEl) counterEl.textContent = `${state.terminalCount} строк`;
    if (previewEl) previewEl.textContent = `${tag} ${body}`;

    if (state.terminalAutoScroll) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function setTerminalExpanded(expanded) {
    state.terminalExpanded = expanded;
    const term = $('cyber-terminal');
    const btn = $('btn-toggle-terminal');
    if (term) {
      term.classList.toggle('is-expanded', expanded);
      term.classList.toggle('is-collapsed', !expanded);
    }
    if (btn) {
      btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }
  }

  function initSSE() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }

    const ssePill = $('pill-sse-status');
    const badge = $('terminal-live-badge');

    function setSseUi(statusClass, text) {
      if (ssePill) {
        ssePill.className = `status-pill ${statusClass}`;
        const spans = ssePill.querySelectorAll('span');
        if (spans[1]) spans[1].textContent = text;
      }
      if (badge) {
        badge.textContent = statusClass === 'status-pill--online' ? 'LIVE STREAM' : 'RECONNECT';
      }
    }

    const es = new EventSource('/api/events');
    state.eventSource = es;

    es.onopen = () => {
      setSseUi('status-pill--online', 'SSE: LIVE');
      appendTerminalLog({ type: 'log', message: '[SCAN] Канал телеметрии /api/events подключён' });
    };

    es.onmessage = (event) => {
      if (!event || !event.data) return;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        appendTerminalLog({ type: 'log', message: event.data });
        return;
      }

      if (payload.message) {
        appendTerminalLog(payload);
      }

      if (state.activeJobKind === 'offload' && payload.message) {
        const beamStatus = $('teleport-beam-status');
        if (beamStatus) beamStatus.textContent = payload.message;
      }

      if (payload.type === 'reclaimed') {
        const delta = safeNum(payload.bytes, 0);
        if (delta > 0) {
          animateOdometerTo(state.odometerTargetBytes + delta, delta);
        }
      }

      if (payload.type === 'done') {
        state.isTransferring = false;
        state.activeJobId = null;
        state.activeJobKind = null;
        const beamStatus = $('teleport-beam-status');
        if (beamStatus) {
          beamStatus.textContent = payload.message || 'ОПЕРАЦИЯ ЗАВЕРШЕНА';
        }
        updateActionButtonsState();
        refreshTelemetry();
        runScan({ silent: true });
      }
    };

    es.onerror = () => {
      setSseUi('status-pill--pending', 'SSE: ПЕРЕПОДКЛЮЧЕНИЕ');
    };
  }

  /* ------------------------------------------------------------------------
     Holo Modal (Safe Markdown Digest Viewer & Live Execution Confirm)
     ------------------------------------------------------------------------ */
  function openHoloModal({ eyebrow, title, contentNode, copyText = '', confirmMode = false, confirmLabel = 'Подтвердить', onConfirm = null }) {
    const modal = $('holo-modal');
    if (!modal) return;

    $('holo-modal-eyebrow').textContent = eyebrow || 'HOLO READER';
    $('holo-modal-title').textContent = title || 'Mac Nexus';

    const contentEl = $('holo-modal-content');
    contentEl.innerHTML = '';
    contentEl.appendChild(contentNode);

    const copyBtn = $('holo-modal-copy');
    state.modalCopyText = copyText || '';
    copyBtn.hidden = !copyText;

    const footerEl = $('holo-modal-footer');
    const confirmBtn = $('holo-modal-confirm');
    state.modalConfirmFn = onConfirm;
    footerEl.hidden = !confirmMode;
    if (confirmBtn) confirmBtn.textContent = confirmLabel;

    modal.hidden = false;
  }

  function closeHoloModal() {
    const modal = $('holo-modal');
    if (modal) modal.hidden = true;
    state.modalConfirmFn = null;
    state.modalCopyText = '';
  }

  function buildSafeMarkdownDom(markdownText) {
    const frag = document.createDocumentFragment();
    const lines = String(markdownText || '').replace(/\r\n/g, '\n').split('\n');
    let inCode = false;
    let codeLines = [];

    function flushCode() {
      if (codeLines.length === 0) return;
      const pre = document.createElement('pre');
      pre.className = 'digest-pre';
      pre.textContent = codeLines.join('\n');
      frag.appendChild(pre);
      codeLines = [];
    }

    for (const line of lines) {
      if (line.trim().startsWith('```')) {
        if (inCode) {
          flushCode();
          inCode = false;
        } else {
          inCode = true;
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }
      if (line.startsWith('### ')) {
        const h3 = document.createElement('h3');
        h3.className = 'digest-h3';
        h3.textContent = line.slice(4);
        frag.appendChild(h3);
      } else if (line.startsWith('## ')) {
        const h2 = document.createElement('h2');
        h2.className = 'digest-h2';
        h2.textContent = line.slice(3);
        frag.appendChild(h2);
      } else if (line.startsWith('# ')) {
        const h1 = document.createElement('h1');
        h1.className = 'digest-h1';
        h1.textContent = line.slice(2);
        frag.appendChild(h1);
      } else if (/^\s*[-*]\s+/.test(line)) {
        const li = document.createElement('div');
        li.className = 'digest-li';
        li.textContent = `• ${line.replace(/^\s*[-*]\s+/, '')}`;
        frag.appendChild(li);
      } else if (line.trim() === '') {
        const spacer = document.createElement('div');
        spacer.style.height = '6px';
        frag.appendChild(spacer);
      } else {
        const p = document.createElement('p');
        p.className = 'digest-p';
        p.textContent = line;
        frag.appendChild(p);
      }
    }
    flushCode();
    return frag;
  }

  function requestConfirmIfNeeded(opts, actionFn) {
    if (state.dryRun && !opts.alwaysConfirm) {
      actionFn();
      return;
    }
    const box = document.createElement('div');
    box.className = 'confirm-box';

    const warn = document.createElement('div');
    warn.className = 'confirm-warning';
    warn.textContent = opts.warningText;

    const detail = document.createElement('p');
    detail.className = 'digest-p';
    detail.textContent = opts.detailText;

    box.appendChild(warn);
    box.appendChild(detail);

    openHoloModal({
      eyebrow: 'ПОДТВЕРЖДЕНИЕ ОПЕРАЦИИ',
      title: opts.title,
      contentNode: box,
      confirmMode: true,
      confirmLabel: opts.confirmLabel,
      onConfirm: () => {
        closeHoloModal();
        actionFn();
      },
    });
  }

  /* ------------------------------------------------------------------------
     Core Async Workflows
     ------------------------------------------------------------------------ */
  async function checkHealth() {
    const pill = $('pill-api-status');
    try {
      const data = await apiGet('/api/health');
      if (pill) {
        pill.className = 'status-pill status-pill--online';
        const spans = pill.querySelectorAll('span');
        if (spans[1]) spans[1].textContent = `API: ONLINE (PID ${safeNum(data.pid, 0)})`;
      }
    } catch (err) {
      if (pill) {
        pill.className = 'status-pill status-pill--error';
        const spans = pill.querySelectorAll('span');
        if (spans[1]) spans[1].textContent = 'API: OFFLINE';
      }
      appendTerminalLog({ type: 'error', message: `[ERROR] /api/health недоступен: ${err.message}` });
    }
  }

  async function refreshTelemetry() {
    try {
      const data = await apiGet('/api/telemetry');
      renderTelemetry(data);
    } catch (err) {
      appendTerminalLog({ type: 'error', message: `[ERROR] Сбой телеметрии: ${err.message}` });
    }
  }

  async function runScan({ silent = false } = {}) {
    if (state.isScanning) return;
    setScanningVisuals(true);
    if (!silent) {
      appendTerminalLog({ type: 'log', message: `[SCAN] Запуск сканирования контуров (older_days=${state.olderDays})...` });
    }

    try {
      const scan = await apiGet(`/api/scan?older_days=${encodeURIComponent(state.olderDays)}`);
      state.scanData = scan;

      // Prune stale or newly locked selections
      const items = Array.isArray(scan?.items) ? scan.items : [];
      const unlockedIds = new Set(items.filter((i) => !i.locked).map((i) => String(i.id)));
      for (const id of Array.from(state.selectedItemIds)) {
        if (!unlockedIds.has(id)) state.selectedItemIds.delete(id);
      }

      const media = Array.isArray(scan?.offload_candidates) ? scan.offload_candidates : [];
      const mediaIds = new Set(media.map((m) => String(m.id)));
      for (const id of Array.from(state.selectedMediaIds)) {
        if (!mediaIds.has(id)) state.selectedMediaIds.delete(id);
      }

      renderRadarItems();
      renderTeleportCandidates();

      if (!silent) {
        appendTerminalLog({
          type: 'log',
          message: `[SCAN] Найдено кандидатов: ${items.length}, медиа для SSD: ${media.length}`,
        });
      }
    } catch (err) {
      showToast(`Ошибка сканирования: ${err.message}`, 'error');
      appendTerminalLog({ type: 'error', message: `[ERROR] Скан завершился ошибкой: ${err.message}` });
    } finally {
      setScanningVisuals(false);
    }
  }

  async function executeSelectedCleanup() {
    const itemIds = Array.from(state.selectedItemIds);
    if (itemIds.length === 0 || state.isTransferring) return;

    state.isTransferring = true;
    state.activeJobKind = 'execute';
    setTerminalExpanded(true);
    updateActionButtonsState();

    appendTerminalLog({
      type: 'log',
      message: `${state.dryRun ? '[DRY-RUN]' : '[LOCK-GUARD]'} Отправка ${itemIds.length} задач на выполнение...`,
    });

    try {
      const res = await apiPost('/api/execute', {
        item_ids: itemIds,
        dry_run: state.dryRun,
      });
      state.activeJobId = res.job_id || null;
      showToast(
        state.dryRun
          ? `Симуляция запущена (job: ${res.job_id || 'ok'})`
          : `Очистка запущена (job: ${res.job_id || 'ok'})`,
        'info'
      );
      if (!state.dryRun) {
        state.selectedItemIds.clear();
        renderRadarItems();
      }
      // Fallback unlock if SSE misses done event
      window.setTimeout(() => {
        if (state.isTransferring && state.activeJobId === (res.job_id || null)) {
          state.isTransferring = false;
          updateActionButtonsState();
          refreshTelemetry();
          runScan({ silent: true });
        }
      }, 20000);
    } catch (err) {
      state.isTransferring = false;
      state.activeJobKind = null;
      updateActionButtonsState();
      showToast(`Ошибка выполнения: ${err.message}`, 'error');
      appendTerminalLog({ type: 'error', message: `[ERROR] /api/execute: ${err.message}` });
    }
  }

  async function executeSelectedOffload() {
    const mediaIds = Array.from(state.selectedMediaIds);
    if (mediaIds.length === 0 || state.isTransferring) return;

    state.isTransferring = true;
    state.activeJobKind = 'offload';
    setTerminalExpanded(true);
    updateActionButtonsState();

    const beamStatus = $('teleport-beam-status');
    if (beamStatus) {
      beamStatus.textContent = state.dryRun
        ? 'DRY-RUN СИМУЛЯЦИЯ SHA-256 ТЕЛЕПОРТА...'
        : 'ПЕРЕДАЧА НА SSD И ПРОВЕРКА SHA-256...';
    }

    appendTerminalLog({
      type: 'log',
      message: `[SHA-256] Старт телепорта ${mediaIds.length} файлов (${state.dryRun ? 'DRY-RUN' : 'LIVE'})...`,
    });

    try {
      const res = await apiPost('/api/offload', {
        media_ids: mediaIds,
        dry_run: state.dryRun,
      });
      state.activeJobId = res.job_id || null;
      showToast(`Телепорт запущен (job: ${res.job_id || 'ok'})`, 'info');
      if (!state.dryRun) {
        state.selectedMediaIds.clear();
        renderTeleportCandidates();
      }
      window.setTimeout(() => {
        if (state.isTransferring && state.activeJobId === (res.job_id || null)) {
          state.isTransferring = false;
          updateActionButtonsState();
          refreshTelemetry();
          runScan({ silent: true });
        }
      }, 30000);
    } catch (err) {
      state.isTransferring = false;
      state.activeJobKind = null;
      if (beamStatus) beamStatus.textContent = `ОШИБКА: ${err.message}`;
      updateActionButtonsState();
      showToast(`Ошибка телепорта: ${err.message}`, 'error');
      appendTerminalLog({ type: 'error', message: `[ERROR] /api/offload: ${err.message}` });
    }
  }

  async function scanDuplicates() {
    if (state.isDupScanning) return;
    state.isDupScanning = true;
    updateActionButtonsState();

    const listEl = $('duplicates-list');
    if (listEl) {
      listEl.innerHTML = `
        <div class="empty-state">
          <strong>Идёт поиск дубликатов (${escapeHtml(state.dupTarget)})...</strong>
          <span>Сравнение размеров и криптографических хэшей через czkawka_cli.</span>
        </div>`;
    }

    appendTerminalLog({ type: 'log', message: `[SCAN] Поиск дубликатов в контуре: ${state.dupTarget}...` });

    try {
      const data = await apiPost('/api/duplicates', { target: state.dupTarget });
      state.dupData = data;
      state.selectedDupFileIds.clear();

      // Auto-select all copies (index >= 1), keeping index 0 protected as ОРИГИНАЛ / СОХРАНИТЬ
      const groups = Array.isArray(data?.groups) ? data.groups : [];
      for (const group of groups) {
        const files = Array.isArray(group.files) ? group.files : [];
        for (let i = 1; i < files.length; i += 1) {
          if (files[i] && files[i].id) {
            state.selectedDupFileIds.add(String(files[i].id));
          }
        }
      }

      renderDuplicates();
      appendTerminalLog({
        type: 'log',
        message: `[SCAN] Найдено групп дубликатов: ${groups.length}, избыточно: ${formatGiB(data.total_wasted_bytes)}`,
      });
    } catch (err) {
      showToast(`Ошибка поиска дубликатов: ${err.message}`, 'error');
      appendTerminalLog({ type: 'error', message: `[ERROR] /api/duplicates: ${err.message}` });
      renderDuplicates();
    } finally {
      state.isDupScanning = false;
      updateActionButtonsState();
    }
  }

  async function trashSelectedDuplicates() {
    const fileIds = Array.from(state.selectedDupFileIds);
    if (fileIds.length === 0 || state.isTransferring) return;

    state.isTransferring = true;
    updateActionButtonsState();

    try {
      const res = await apiPost('/api/trash-duplicates', { file_ids: fileIds });
      const count = safeNum(res.trashed_count, 0);
      const reclaimed = safeNum(res.reclaimed_bytes, 0);

      showToast(`Перемещено в Корзину: ${count} файлов (${formatGiB(reclaimed)})`, 'success');
      appendTerminalLog({
        type: 'reclaimed',
        message: `[DONE] Дубликаты перемещены в ~/.Trash (${count} шт.)`,
        bytes: reclaimed,
      });

      if (reclaimed > 0) {
        animateOdometerTo(state.odometerTargetBytes + reclaimed, reclaimed);
      }

      state.selectedDupFileIds.clear();
      await refreshTelemetry();
      await scanDuplicates();
    } catch (err) {
      showToast(`Ошибка удаления дубликатов: ${err.message}`, 'error');
      appendTerminalLog({ type: 'error', message: `[ERROR] /api/trash-duplicates: ${err.message}` });
    } finally {
      state.isTransferring = false;
      updateActionButtonsState();
    }
  }

  async function openTool(toolId) {
    try {
      const res = await apiPost('/api/open-tool', { tool: toolId });
      if (toolId === 'digest') {
        const content = res.content || 'Файл digest_latest.md пуст или ещё не сформирован.';
        openHoloModal({
          eyebrow: 'ВОСКРЕСНЫЙ ДАЙДЖЕСТ // ~/.local/state/mac-hygiene/digest_latest.md',
          title: 'Воскресный дайджест системы',
          contentNode: buildSafeMarkdownDom(content),
          copyText: content,
        });
        appendTerminalLog({ type: 'log', message: '[DONE] Открыт Воскресный дайджест системы' });
      } else {
        showToast(`Запущено: ${toolId}`, 'success');
        appendTerminalLog({ type: 'log', message: `[DONE] Открыта утилита / контур: ${toolId}` });
      }
    } catch (err) {
      showToast(`Не удалось открыть ${toolId}: ${err.message}`, 'error');
      appendTerminalLog({ type: 'error', message: `[ERROR] /api/open-tool (${toolId}): ${err.message}` });
    }
  }

  /* ------------------------------------------------------------------------
     Tab Navigation & Event Bindings
     ------------------------------------------------------------------------ */
  function switchTab(tabId) {
    state.activeTab = tabId;
    const buttons = document.querySelectorAll('.tab-btn');
    for (const btn of buttons) {
      const active = btn.dataset.tab === tabId;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    }
    const panels = document.querySelectorAll('.tab-panel');
    for (const panel of panels) {
      const match = panel.id === `panel-${tabId}`;
      panel.classList.toggle('is-active', match);
      panel.hidden = !match;
    }
  }

  function bindEvents() {
    // Tabs
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    // DRY-RUN toggle
    $('dry-run-toggle').addEventListener('click', () => {
      state.dryRun = !state.dryRun;
      syncDryRunUI();
      updateActionButtonsState();
      appendTerminalLog({
        type: 'log',
        message: state.dryRun
          ? '[DRY-RUN] Включён безопасный режим симуляции'
          : '[LOCK-GUARD] Внимание: активирован БОЕВОЙ РЕЖИМ выполнения',
      });
    });

    // Refresh all
    $('btn-refresh-all').addEventListener('click', () => {
      checkHealth();
      refreshTelemetry();
      runScan();
    });

    // Category filter chips
    $('radar-category-chips').addEventListener('click', (e) => {
      const btn = e.target.closest('.filter-chip');
      if (!btn || !btn.dataset.cat) return;
      state.activeCategory = btn.dataset.cat;
      renderRadarItems();
    });

    // Age selector
    document.querySelectorAll('.age-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const days = safeNum(btn.dataset.days, 30);
        if (days === state.olderDays) return;
        state.olderDays = days;
        document.querySelectorAll('.age-btn').forEach((b) => {
          b.classList.toggle('is-active', safeNum(b.dataset.days) === days);
        });
        runScan();
      });
    });

    // Tab 1 buttons
    $('btn-select-all-ready').addEventListener('click', () => {
      const items = Array.isArray(state.scanData?.items) ? state.scanData.items : [];
      const filtered = state.activeCategory === 'all'
        ? items
        : items.filter((it) => it.category === state.activeCategory);
      for (const item of filtered) {
        if (!item.locked && item.id) {
          state.selectedItemIds.add(String(item.id));
        }
      }
      renderRadarItems();
    });

    $('btn-clear-radar-selection').addEventListener('click', () => {
      state.selectedItemIds.clear();
      renderRadarItems();
    });

    $('btn-rescan-radar').addEventListener('click', () => runScan());

    $('radar-items-list').addEventListener('change', (e) => {
      const cb = e.target.closest('.js-radar-check');
      if (!cb || cb.disabled) return;
      const id = String(cb.dataset.id || '');
      if (cb.checked) state.selectedItemIds.add(id);
      else state.selectedItemIds.delete(id);
      const card = cb.closest('.item-card');
      if (card) card.classList.toggle('is-selected', cb.checked);
      updateRadarDockStats();
    });

    $('btn-execute-clean').addEventListener('click', () => {
      const count = state.selectedItemIds.size;
      const sizeText = $('radar-selected-bytes').textContent;
      requestConfirmIfNeeded(
        {
          title: 'Запустить боевую очистку выбранных элементов?',
          warningText: 'Внимание: режим DRY-RUN выключен. Очистка выполнит реальные действия на диске (заблокированные LIVE LOCK цели защищены на бэкенде).',
          detailText: `Выбрано целей: ${count} · Расчётный объём: ${sizeText}. Загрузки перемещаются строго в Корзину (~/.Trash).`,
          confirmLabel: 'ОЧИСТИТЬ ВЫБРАННОЕ',
        },
        executeSelectedCleanup
      );
    });

    // Tab 2: Teleport
    $('btn-select-all-media').addEventListener('click', () => {
      const media = Array.isArray(state.scanData?.offload_candidates)
        ? state.scanData.offload_candidates
        : [];
      for (const m of media) {
        if (m.id) state.selectedMediaIds.add(String(m.id));
      }
      renderTeleportCandidates();
    });

    $('btn-clear-media-selection').addEventListener('click', () => {
      state.selectedMediaIds.clear();
      renderTeleportCandidates();
    });

    $('teleport-list').addEventListener('change', (e) => {
      const cb = e.target.closest('.js-media-check');
      if (!cb) return;
      const id = String(cb.dataset.id || '');
      if (cb.checked) state.selectedMediaIds.add(id);
      else state.selectedMediaIds.delete(id);
      const card = cb.closest('.item-card');
      if (card) card.classList.toggle('is-selected', cb.checked);
      updateTeleportDockStats();
    });

    $('btn-execute-offload').addEventListener('click', () => {
      const count = state.selectedMediaIds.size;
      const sizeText = $('teleport-selected-bytes').textContent;
      requestConfirmIfNeeded(
        {
          title: 'Подтвердить SHA-256 телепорт медиа на Внешний SSD?',
          warningText: 'Внимание: файлы будут скопированы на /Volumes/secret for mine, проверены по размеру и SHA-256, затем оригиналы на NVMe будут удалены.',
          detailText: `Выбрано файлов: ${count} · Объём: ${sizeText}.`,
          confirmLabel: 'ТЕЛЕПОРТИРОВАТЬ НА SSD',
        },
        executeSelectedOffload
      );
    });

    // Tab 3: Duplicates
    $('dup-target-group').addEventListener('click', (e) => {
      const btn = e.target.closest('.filter-chip');
      if (!btn || !btn.dataset.target) return;
      state.dupTarget = btn.dataset.target;
      document.querySelectorAll('#dup-target-group .filter-chip').forEach((b) => {
        b.classList.toggle('is-active', b.dataset.target === state.dupTarget);
      });
    });

    $('btn-scan-duplicates').addEventListener('click', scanDuplicates);

    $('duplicates-list').addEventListener('change', (e) => {
      const cb = e.target.closest('.js-dup-check');
      if (!cb || cb.disabled) return;
      const id = String(cb.dataset.id || '');
      if (cb.checked) state.selectedDupFileIds.add(id);
      else state.selectedDupFileIds.delete(id);
      updateDupDockStats();
    });

    $('btn-trash-duplicates').addEventListener('click', () => {
      const count = state.selectedDupFileIds.size;
      const sizeText = $('dup-selected-bytes').textContent;
      requestConfirmIfNeeded(
        {
          alwaysConfirm: true,
          title: 'Переместить выбранные копии в Корзину?',
          warningText: 'Первые файлы («ОРИГИНАЛ / СОХРАНИТЬ») останутся нетронутыми. Выбранные копии будут перемещены в ~/.Trash.',
          detailText: `К перемещению в Корзину: ${count} копий · Освободится: ${sizeText}.`,
          confirmLabel: 'УБРАТЬ В КОРЗИНУ',
        },
        trashSelectedDuplicates
      );
    });

    // Tab 4: Launch Tools
    document.querySelectorAll('.tool-btn[data-tool]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tool = btn.dataset.tool;
        if (tool) openTool(tool);
      });
    });

    // Terminal controls
    $('btn-toggle-terminal').addEventListener('click', () => {
      setTerminalExpanded(!state.terminalExpanded);
    });

    $('btn-terminal-autoscroll').addEventListener('click', (e) => {
      state.terminalAutoScroll = !state.terminalAutoScroll;
      e.currentTarget.classList.toggle('is-toggled', state.terminalAutoScroll);
      e.currentTarget.setAttribute('aria-pressed', state.terminalAutoScroll ? 'true' : 'false');
    });

    $('btn-terminal-clear').addEventListener('click', () => {
      const logEl = $('terminal-log');
      if (logEl) logEl.innerHTML = '';
      state.terminalCount = 0;
      $('terminal-count').textContent = '0 строк';
      $('terminal-preview-line').textContent = 'Журнал очищен';
    });

    // Modal controls
    $('holo-modal-close').addEventListener('click', closeHoloModal);
    $('holo-modal-backdrop').addEventListener('click', closeHoloModal);
    $('holo-modal-cancel').addEventListener('click', closeHoloModal);
    $('holo-modal-confirm').addEventListener('click', () => {
      if (typeof state.modalConfirmFn === 'function') {
        const fn = state.modalConfirmFn;
        state.modalConfirmFn = null;
        fn();
      }
    });
    $('holo-modal-copy').addEventListener('click', async () => {
      if (!state.modalCopyText) return;
      try {
        await navigator.clipboard.writeText(state.modalCopyText);
        showToast('Текст дайджеста скопирован в буфер обмена', 'success');
      } catch {
        showToast('Не удалось скопировать в буфер обмена', 'warn');
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('holo-modal').hidden) {
        closeHoloModal();
      }
    });
  }

  /* ------------------------------------------------------------------------
     Bootstrap
     ------------------------------------------------------------------------ */
  function bootstrap() {
    initOrbitalCanvas();
    syncDryRunUI();
    bindEvents();
    renderDuplicates();
    initSSE();

    checkHealth();
    refreshTelemetry();
    runScan();

    // Gentle telemetry heartbeat every 15s while visible & idle
    window.setInterval(() => {
      if (!document.hidden && !state.isScanning && !state.isTransferring) {
        refreshTelemetry();
      }
    }, 15000);
  }

  document.addEventListener('DOMContentLoaded', bootstrap);
})();
