(() => {
  const CONFIG_KEY = 'ace.entertainment.provider.v1';
  const SECRET_KEY = 'ace.entertainment.apiKey.session';
  const PROVIDER_COLLAPSED_KEY = 'ace.entertainment.provider.collapsed';

  const readConfig = () => {
    try {
      return JSON.parse(localStorage.getItem(CONFIG_KEY) || '{}') || {};
    } catch {
      return {};
    }
  };

  const writeConfig = (payload) => {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(payload));
  };

  const selectedModel = (panel) => {
    const custom = panel.querySelector('[data-guest-custom-model]')?.value.trim();
    return custom || panel.querySelector('[data-guest-model]')?.value || '';
  };

  const panelPayload = (panel) => ({
    provider: panel.querySelector('[data-guest-provider]')?.value || 'qwen',
    base_url: panel.querySelector('[data-guest-base-url]')?.value.trim() || '',
    model: selectedModel(panel),
    api_key: panel.querySelector('[data-guest-api-key]')?.value.trim() || '',
  });

  const syncPanelStorage = (panel) => {
    const payload = panelPayload(panel);
    writeConfig({
      provider: payload.provider,
      base_url: payload.base_url,
      model: payload.model,
    });
    if (payload.api_key) sessionStorage.setItem(SECRET_KEY, payload.api_key);
  };

  const fillModels = (panel, preferredModel = '') => {
    const provider = panel.querySelector('[data-guest-provider]');
    const model = panel.querySelector('[data-guest-model]');
    const baseUrl = panel.querySelector('[data-guest-base-url]');
    if (!provider || !model) return;
    const option = provider.selectedOptions?.[0];
    const defaultBaseUrl = option?.dataset.baseUrl || '';
    const defaultModel = option?.dataset.model || '';
    let options = [];
    try {
      options = JSON.parse(option?.dataset.modelOptions || '[]') || [];
    } catch {
      options = [];
    }
    const rows = Array.from(new Set([preferredModel, defaultModel, ...options].filter(Boolean)));
    model.textContent = '';
    rows.forEach((item) => {
      const el = document.createElement('option');
      el.value = item;
      el.textContent = item;
      model.appendChild(el);
    });
    if (preferredModel && rows.includes(preferredModel)) model.value = preferredModel;
    if (baseUrl && !baseUrl.value) baseUrl.value = defaultBaseUrl;
  };

  const initProviderPanel = (panel) => {
    const provider = panel.querySelector('[data-guest-provider]');
    const model = panel.querySelector('[data-guest-model]');
    const baseUrl = panel.querySelector('[data-guest-base-url]');
    const apiKey = panel.querySelector('[data-guest-api-key]');
    const customModel = panel.querySelector('[data-guest-custom-model]');
    const status = panel.querySelector('[data-guest-provider-status]');
    const testButton = panel.querySelector('[data-guest-provider-test]');
    const toggleButton = panel.querySelector('[data-guest-provider-toggle]');
    const stored = readConfig();
    const syncCollapse = () => {
      const collapsed = panel.classList.contains('is-collapsed');
      if (toggleButton) toggleButton.textContent = collapsed ? '展开配置' : '收起配置';
      panel.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    };
    if (localStorage.getItem(PROVIDER_COLLAPSED_KEY) === '1') panel.classList.add('is-collapsed');
    syncCollapse();
    toggleButton?.addEventListener('click', () => {
      panel.classList.toggle('is-collapsed');
      localStorage.setItem(PROVIDER_COLLAPSED_KEY, panel.classList.contains('is-collapsed') ? '1' : '0');
      syncCollapse();
    });
    if (stored.provider && provider?.querySelector(`option[value="${CSS.escape(stored.provider)}"]`)) {
      provider.value = stored.provider;
    }
    if (baseUrl) baseUrl.value = stored.base_url || provider?.selectedOptions?.[0]?.dataset.baseUrl || '';
    fillModels(panel, stored.model || provider?.selectedOptions?.[0]?.dataset.model || '');
    if (stored.model && model && !Array.from(model.options).some((option) => option.value === stored.model) && customModel) {
      customModel.value = stored.model;
    }
    if (apiKey) apiKey.value = sessionStorage.getItem(SECRET_KEY) || '';

    provider?.addEventListener('change', () => {
      if (baseUrl) baseUrl.value = provider.selectedOptions?.[0]?.dataset.baseUrl || '';
      if (customModel) customModel.value = '';
      fillModels(panel, provider.selectedOptions?.[0]?.dataset.model || '');
      syncPanelStorage(panel);
      if (status) status.textContent = '已切换供应商，建议重新测试连通性。';
    });
    [model, baseUrl, apiKey, customModel].forEach((item) => {
      item?.addEventListener('input', () => syncPanelStorage(panel));
      item?.addEventListener('change', () => syncPanelStorage(panel));
    });
    testButton?.addEventListener('click', async () => {
      syncPanelStorage(panel);
      const payload = panelPayload(panel);
      if (status) {
        status.textContent = '正在测试连通性...';
        status.dataset.state = 'pending';
      }
      testButton.disabled = true;
      try {
        const response = await fetch('/api/entertainment/provider/heartbeat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.detail || data.message || '连通性测试失败');
        if (status) {
          status.textContent = `✅ ${data.message || '模型接口可用'}`;
          status.dataset.state = 'ok';
        }
      } catch (error) {
        if (status) {
          status.textContent = `❌ ${error.message || error}`;
          status.dataset.state = 'error';
        }
      } finally {
        testButton.disabled = false;
      }
    });
  };

  const renderList = (target, rows) => {
    if (!target) return;
    target.textContent = '';
    (rows || []).forEach((item) => {
      const li = document.createElement('li');
      li.textContent = item;
      target.appendChild(li);
    });
  };

  const textList = (value) => {
    if (Array.isArray(value)) return value.map((item) => String(item ?? '').trim()).filter(Boolean);
    if (value === null || value === undefined) return [];
    const text = String(value).trim();
    return text ? [text] : [];
  };

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[char]));

  const renderAdvancedAnalysis = (result) => {
    const box = document.getElementById('scoreAdvancedBox');
    const list = document.getElementById('scoreAdvanced');
    if (!box || !list) return;
    const advanced = result.advanced || null;
    if (!advanced) {
      box.hidden = true;
      list.textContent = '';
      return;
    }
    const rows = [
      advanced.bazi_overview && `八字概览：${advanced.bazi_overview}`,
      advanced.wuxing_analysis && `五行分析：${advanced.wuxing_analysis}`,
      advanced.yongshen_note && `喜用神参考：${advanced.yongshen_note}`,
      advanced.name_element_support && `姓名补益：${advanced.name_element_support}`,
      advanced.balance_note && `平衡提醒：${advanced.balance_note}`,
      advanced.bazi_fit_level && `适配等级：${advanced.bazi_fit_level}`,
      advanced.classical_reference && `典籍参考：${advanced.classical_reference}`,
    ].filter(Boolean);
    box.hidden = rows.length === 0;
    renderList(list, rows);
  };

  const dimensionLabel = (key) => ({
    phonology: '音韵',
    glyph: '字形',
    meaning: '字义/寓意',
    recognition: '辨识度',
    usability: '正式可用性',
    gender_fit: '性别适配',
    culture: '文化联想',
    bazi: '五行参考',
  }[key] || key);

  const formatScoreNumber = (value) => {
    const number = Number(value || 0);
    return Number.isInteger(number) ? String(number) : number.toFixed(1).replace(/\.0$/, '');
  };

  const pad2 = (value) => String(value).padStart(2, '0');

  const fillSelect = (select, rows, formatter) => {
    if (!select) return;
    const current = select.value;
    select.textContent = '';
    rows.forEach((value) => {
      const option = document.createElement('option');
      option.value = String(value);
      option.textContent = formatter ? formatter(value) : String(value);
      select.appendChild(option);
    });
    if (rows.map(String).includes(current)) select.value = current;
  };

  const initBirthPicker = () => {
    const hidden = document.getElementById('scoreBirth');
    const year = document.getElementById('scoreBirthYear');
    const month = document.getElementById('scoreBirthMonth');
    const day = document.getElementById('scoreBirthDay');
    const hour = document.getElementById('scoreBirthHour');
    if (!hidden || !year || !month || !day || !hour) return;
    const now = new Date();
    const years = [];
    for (let value = now.getFullYear(); value >= now.getFullYear() - 120; value -= 1) years.push(value);
    fillSelect(year, years, (value) => `${value}年`);
    fillSelect(month, Array.from({length: 12}, (_, index) => index + 1), (value) => `${value}月`);
    fillSelect(hour, Array.from({length: 24}, (_, index) => index), (value) => `${pad2(value)}时`);
    year.value = String(now.getFullYear());
    month.value = String(now.getMonth() + 1);
    hour.value = String(now.getHours());

    const sync = () => {
      const daysInMonth = new Date(Number(year.value), Number(month.value), 0).getDate();
      const selectedDay = Math.min(Number(day.value || now.getDate()), daysInMonth);
      fillSelect(day, Array.from({length: daysInMonth}, (_, index) => index + 1), (value) => `${value}日`);
      day.value = String(selectedDay);
      hidden.value = `${year.value}-${pad2(month.value)}-${pad2(day.value)}T${pad2(hour.value)}:00`;
    };

    day.value = String(now.getDate());
    [year, month, day, hour].forEach((item) => item.addEventListener('change', sync));
    sync();
  };

  const renderScoreResult = (payload) => {
    const result = payload.result || {};
    const report = result.rule_report || {};
    const scoreDetail = report.score_detail || {};
    const breakdown = scoreDetail.weighted_breakdown || {};
    const resultEl = document.getElementById('nameScoreResult');
    resultEl.hidden = false;
    document.getElementById('scoreValue').textContent = result.score ?? '--';
    document.getElementById('scoreTitle').textContent = `${report.name || ''} · ${report.mode === 'advanced' ? '进阶版' : '基础版'}`;
    document.getElementById('scoreVerdict').textContent = result.verdict || '';
    document.getElementById('scoreModelTag').textContent = `${payload.provider || ''} / ${payload.model || ''}`;
    document.getElementById('scoreSummary').textContent = result.summary || '';
    const detailEl = document.getElementById('scoreDetail');
    if (detailEl) {
      const penalty = Number(scoreDetail.quality_penalty || 0);
      const cap = scoreDetail.score_cap;
      const adjustment = Number(result.score_adjustment || 0);
      const capText = cap === null || cap === undefined ? '无封顶' : `${cap} 分封顶${scoreDetail.cap_applied ? '（已触发）' : '（未触发）'}`;
      const aiText = `AI 微调 ${adjustment > 0 ? '+' : ''}${formatScoreNumber(adjustment)}：${result.adjustment_reason || '无明确调分证据'}`;
      detailEl.innerHTML = `
        <strong>计算明细</strong>
        <span>维度贡献合计 ${formatScoreNumber(scoreDetail.weighted_score)} 分 · 质量门槛扣 ${formatScoreNumber(penalty)} 分 · ${capText} · 规则分 ${formatScoreNumber(result.base_score ?? scoreDetail.final_score)} → 最终分 ${formatScoreNumber(result.score)} · ${aiText}</span>
      `;
    }
    const dimensions = document.getElementById('scoreDimensions');
    dimensions.textContent = '';
    Object.entries(report.dimensions || {}).forEach(([key, item]) => {
      const weighted = breakdown[key] || {};
      const points = weighted.points ?? ((item.score || 0) * ((report.weights || {})[key] || 0));
      const maxPoints = weighted.max_points ?? (((report.weights || {})[key] || 0) * 100);
      const rawScore = weighted.raw_score ?? item.score ?? 0;
      const row = document.createElement('div');
      row.className = 'score-dimension-row';
      row.innerHTML = `
        <div>
          <strong>${dimensionLabel(key)}</strong>
          <em>原始 ${formatScoreNumber(rawScore)}/100 · 权重满分 ${formatScoreNumber(maxPoints)}</em>
          <span>${item.reason || ''}</span>
        </div>
        <b>${formatScoreNumber(points)}/${formatScoreNumber(maxPoints)}</b>
        <i style="--score-width:${Math.max(0, Math.min(100, rawScore || 0))}%"></i>
      `;
      dimensions.appendChild(row);
    });
    renderList(document.getElementById('scoreHighlights'), result.highlights);
    renderList(document.getElementById('scoreCautions'), result.cautions);
    renderList(document.getElementById('scoreSources'), result.source_notes);
    renderAdvancedAnalysis(result);
    renderList(document.getElementById('scoreSuggestions'), result.suggestions);
    resultEl.scrollIntoView({behavior: 'smooth', block: 'start'});
  };

  const renderBabyNamesResult = (payload) => {
    const result = payload.result || {};
    const resultEl = document.getElementById('babyNameResult');
    const list = document.getElementById('babyNameList');
    const advancedBox = document.getElementById('babyAdvancedSummary');
    if (!resultEl || !list) return;
    resultEl.hidden = false;
    document.getElementById('babyResultTitle').textContent = `${result.surname || ''}姓 · ${result.mode === 'advanced' ? '进阶版' : '基础版'}`;
    document.getElementById('babyResultSummary').textContent = result.summary || '已生成候选名字';
    document.getElementById('babyModelTag').textContent = `${payload.provider || ''} / ${payload.model || ''}`;
    const advanced = result.advanced || null;
    if (advancedBox) {
      if (advanced) {
        const preferredElements = textList(advanced.preferred_elements).join('、');
        advancedBox.hidden = false;
        advancedBox.innerHTML = `
          <strong>命理策略</strong>
          <span>${escapeHtml(advanced.bazi_overview || '')}</span>
          <span>喜用神参考：${escapeHtml(preferredElements || advanced.yongshen_note || '以模型报告为准')}</span>
          <span>${escapeHtml(advanced.naming_strategy || '')}</span>
          <span>${escapeHtml(advanced.disclaimer || '仅作传统文化参考，不作命运预测。')}</span>
        `;
      } else {
        advancedBox.hidden = true;
        advancedBox.textContent = '';
      }
    }
    list.textContent = '';
    const names = Array.isArray(result.names) ? result.names : [];
    if (names.length === 0) {
      list.innerHTML = '<p class="empty-inline">暂未生成可展示的候选名，请调整姓氏、来源或模型配置后重试。</p>';
      resultEl.scrollIntoView({behavior: 'smooth', block: 'start'});
      return;
    }
    names.forEach((item, index) => {
      const card = document.createElement('details');
      card.className = 'baby-name-card';
      if (index === 0) card.open = true;
      const risks = textList(item.risks).map((risk) => `<li>${escapeHtml(risk)}</li>`).join('');
      card.innerHTML = `
        <summary>
          <span>${escapeHtml(String(item.rank || index + 1).padStart(2, '0'))}</span>
          <strong>${escapeHtml(item.full_name || '')}</strong>
          <em>${escapeHtml(item.source || '')}</em>
        </summary>
        <div class="baby-name-detail">
          <p>${escapeHtml(item.reason || '')}</p>
          <dl>
            <div><dt>音韵</dt><dd>${escapeHtml(item.phonology || '')}</dd></div>
            <div><dt>字形</dt><dd>${escapeHtml(item.glyph || '')}</dd></div>
            <div><dt>字义寓意</dt><dd>${escapeHtml(item.meaning || '')}</dd></div>
            <div><dt>辨识度</dt><dd>${escapeHtml(item.recognition || '')}</dd></div>
            <div><dt>正式可用性</dt><dd>${escapeHtml(item.formal_usability || '')}</dd></div>
            <div><dt>文化联想</dt><dd>${escapeHtml(item.cultural_imagery || '')}</dd></div>
            ${item.wuxing_note ? `<div><dt>五行参考</dt><dd>${escapeHtml(item.wuxing_note)}</dd></div>` : ''}
          </dl>
          ${risks ? `<section><h4>潜在风险</h4><ul>${risks}</ul></section>` : ''}
        </div>
      `;
      list.appendChild(card);
    });
    resultEl.scrollIntoView({behavior: 'smooth', block: 'start'});
  };

  const initNameScore = () => {
    const form = document.getElementById('nameScoreForm');
    if (!form) return;
    const modeInput = document.getElementById('scoreMode');
    const birthField = document.getElementById('birthField');
    const submit = document.getElementById('nameScoreSubmit');
    const providerPanel = document.querySelector('[data-guest-provider-panel]');
    document.querySelectorAll('[data-mode-button]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-mode-button]').forEach((item) => item.classList.remove('is-active'));
        button.classList.add('is-active');
        modeInput.value = button.dataset.modeButton || 'basic';
        birthField.classList.toggle('is-hidden', modeInput.value !== 'advanced');
      });
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!providerPanel) return;
      syncPanelStorage(providerPanel);
      const payload = {
        ...panelPayload(providerPanel),
        mode: modeInput.value || 'basic',
        name: document.getElementById('scoreName').value.trim(),
        gender: document.getElementById('scoreGender').value,
        birth_datetime: document.getElementById('scoreBirth').value,
        preference: document.getElementById('scorePreference').value.trim(),
      };
      submit.disabled = true;
      submit.textContent = '打分中...';
      try {
        const response = await fetch('/api/entertainment/name-score', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '名字打分失败');
        renderScoreResult(data);
      } catch (error) {
        const status = providerPanel.querySelector('[data-guest-provider-status]');
        if (status) {
          status.textContent = `❌ ${error.message || error}`;
          status.dataset.state = 'error';
        }
      } finally {
        submit.disabled = false;
        submit.textContent = '开始打分';
      }
    });
  };

  const initBabyNames = () => {
    const form = document.getElementById('babyNameForm');
    if (!form) return;
    const modeInput = document.getElementById('babyMode');
    const birthField = document.getElementById('birthField');
    const submit = document.getElementById('babyNameSubmit');
    const providerPanel = document.querySelector('[data-guest-provider-panel]');
    document.querySelectorAll('[data-baby-mode-button]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-baby-mode-button]').forEach((item) => item.classList.remove('is-active'));
        button.classList.add('is-active');
        modeInput.value = button.dataset.babyModeButton || 'basic';
        birthField.classList.toggle('is-hidden', modeInput.value !== 'advanced');
      });
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!providerPanel) return;
      syncPanelStorage(providerPanel);
      const payload = {
        ...panelPayload(providerPanel),
        mode: modeInput.value || 'basic',
        surname: document.getElementById('babySurname').value.trim(),
        name_length: Number(document.getElementById('babyNameLength')?.value || 3),
        gender: document.getElementById('babyGender').value,
        birth_datetime: document.getElementById('scoreBirth')?.value || '',
        source_preference: document.getElementById('babySource').value.trim(),
        style_preference: document.getElementById('babyPreference').value.trim(),
      };
      submit.disabled = true;
      submit.textContent = '生成中...';
      try {
        const response = await fetch('/api/entertainment/baby-names', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '宝宝起名失败');
        renderBabyNamesResult(data);
      } catch (error) {
        const status = providerPanel.querySelector('[data-guest-provider-status]');
        if (status) {
          status.textContent = `❌ ${error.message || error}`;
          status.dataset.state = 'error';
        }
      } finally {
        submit.disabled = false;
        submit.textContent = '生成名字';
      }
    });
  };

  document.querySelectorAll('[data-guest-provider-panel]').forEach(initProviderPanel);
  initBirthPicker();
  initNameScore();
  initBabyNames();
})();
