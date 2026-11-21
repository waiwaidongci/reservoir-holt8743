import {feedback} from '../components/common/fields.js';
import {plot} from '../components/common/chart.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {PUMP_STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const NUMERIC = {step: 'any'};

function numericInput(name, current, {min = 0, max, required = true} = {}) {
  return el('input', {name, type: 'number', required, min, max, step: NUMERIC.step,
    value: current ?? '', autocomplete: 'off'});
}

function dayRows(source) {
  const forecasts = source ? Object.fromEntries(
    source.result.tables.find(table => table.title === '逐日预报').rows
      .map(row => [row.date, row.selected])) : {};
  const dates = Object.keys(forecasts);
  const warnDefault = source?.params?.alert_level_m ?? 2.05;
  const rows = dates.length ? dates.map(day => ({
    date: day, forecast_m: forecasts[day], pump_cap_m: 0.3, price: 0.6,
    floor_m: 1.0, warning_m: warnDefault,
  })) : [{date: '', forecast_m: '', pump_cap_m: 0.3, price: 0.6, floor_m: 1.0, warning_m: warnDefault}];
  return {source, rows};
}

function buildForm(spec, source, refresh) {
  const message = feedback();
  const {rows: initial} = dayRows(source);
  const state = {rows: initial};
  const grid = el('div', {class: 'pump-grid'});
  const energyField = el('label', {class: 'field'}, el('span', {}, '每米抽水耗电量（kWh/m）'),
    numericInput('energy_kwh_per_m', spec.pump.default_energy_kwh_per_m,
      {max: spec.pump.max_energy_kwh_per_m}));

  function paint() {
    grid.replaceChildren(...state.rows.map((row, index) => el('div', {class: 'pump-row'},
      el('label', {class: 'field'}, el('span', {}, '日期'),
        el('input', {name: 'date', type: 'date', required: true, value: row.date,
          onChange: event => { row.date = event.target.value; }})),
      el('label', {class: 'field'}, el('span', {}, '液位预报（m）'),
        numericInput('forecast_m', row.forecast_m, {max: 20})),
      el('label', {class: 'field'}, el('span', {}, '抽水上限（m）'),
        numericInput('pump_cap_m', row.pump_cap_m, {max: 20})),
      el('label', {class: 'field'}, el('span', {}, '电价（元/kWh）'),
        numericInput('price', row.price, {max: spec.pump.max_price})),
      el('label', {class: 'field'}, el('span', {}, '保供底线（m）'),
        numericInput('floor_m', row.floor_m, {max: 20})),
      el('label', {class: 'field'}, el('span', {}, '高位预警线（m）'),
        numericInput('warning_m', row.warning_m, {max: 20})),
      el('div', {class: 'field'}, el('span', {}, '操作'),
        button('删除', {class: 'quiet', onClick: () => { state.rows.splice(index, 1); paint(); }})))));
  }
  paint();

  const addRow = button('增加一天', {class: 'quiet', type: 'button',
    onClick: () => { state.rows.push({date: '', forecast_m: '', pump_cap_m: 0.3, price: 0.6, floor_m: 1.0, warning_m: 2.05}); paint(); }});
  const submit = button('发起抽水排班计算', {type: 'submit', class: 'primary', 'data-testid': 'start-pump'});
  const form = el('form', {class: 'fields', onSubmit: handler(submit, message, async event => {
    event.preventDefault();
    const data = new FormData(event.target);
    const payload = {
      name: data.get('name') || `抽水排班 ${new Date().toLocaleDateString('zh-CN')}`,
      source_run_id: source?.id ?? null,
      energy_kwh_per_m: Number(data.get('energy_kwh_per_m')),
      days: state.rows.map(row => ({
        date: row.date, forecast_m: Number(row.forecast_m), pump_cap_m: Number(row.pump_cap_m),
        price: Number(row.price), floor_m: Number(row.floor_m), warning_m: Number(row.warning_m),
      })),
    };
    const plan = await workspace.startPumpPlan(payload);
    await refresh();
    if (plan.status === 'queued' || plan.status === 'running') {
      location.hash = `#pump/${plan.id}`;
    }
  })},
    el('label', {class: 'field'}, el('span', {}, '方案名称'),
      el('input', {name: 'name', type: 'text', maxlength: 120, placeholder: '抽水排班'})),
    source && p(`预报来源：${source.name}（逐日预报已快照，重算期间照常使用）`, 'subtle'),
    energyField, grid, el('div', {class: 'actions'}, addRow, submit), message);
  return form;
}

function planDetail(plan, refresh) {
  const message = feedback();
  if (plan.status === 'queued' || plan.status === 'running') {
    const status = el('p', {role: 'status'}, '后台正在重算抽水排班，当前方案保持不变，完成后自动切换。');
    const original = location.hash;
    poll(() => workspace.getPumpPlan(plan.id),
      item => ['complete', 'failed', 'superseded'].includes(item.status),
      () => location.hash === original)
      .then(item => { if (item && status.isConnected && location.hash === original) refresh(); })
      .catch(error => { if (status.isConnected) status.textContent = error.message; });
    return panel(`排班代次 G${plan.generation}`, pill(PUMP_STATUS[plan.status], plan.status), status,
      button('刷新', {class: 'quiet', onClick: refresh}));
  }
  if (!plan.result) {
    return panel(`排班代次 G${plan.generation}`, pill(PUMP_STATUS[plan.status], plan.status),
      p(plan.error || '该排班没有可用结果。', 'feedback'));
  }
  const result = plan.result;
  const download = button('下载排班 CSV', {class: 'quiet',
    onClick: event => handler(download, message, () =>
      workspace.download(`/pump-plans/${plan.id}/export`, `pump-schedule-${plan.id}.csv`))(event)});
  const activate = session.canReview && !plan.is_active && plan.status === 'complete'
    ? button('切换为当前方案', {class: 'primary', onClick: handler(button, message, async () => {
        await workspace.activatePumpPlan(plan.id); await refresh();
      })}) : null;
  return el('section', {class: 'analysis', 'data-testid': 'pump-complete'},
    el('div', {class: 'result-head'},
      p(`代次 G${plan.generation} · 规则 ${plan.rule_revision}`, 'eyeline'),
      h(2, result.headline), p(result.note, 'intro-copy'),
      el('div', {class: 'actions'}, pill(PUMP_STATUS[plan.status], plan.status),
        plan.is_active && pill('当前方案', 'complete'), download, activate, message)),
    result.infeasible && el('div', {class: 'decision', role: 'alert'},
      h(3, `最早守不住日期：${result.infeasible.date}`), p(result.infeasible.reason)) ,
    metrics(result.metrics),
    result.charts.map(plot),
    (result.sections || []).map(item => panel(item.title, p(item.text))),
    result.tables.map(item => panel(item.title, table(item.columns, item.rows, {caption: item.title}))));
}

export async function pumpPage(route, refresh) {
  const {spec, runs} = workspace;
  const plans = await workspace.pumpPlans();
  const active = await workspace.activePumpPlan();
  const selectedId = route.id || active?.id || plans[0]?.id;
  const selected = selectedId ? await workspace.getPumpPlan(selectedId) : null;

  // 列表视图不含 result/params，需拉取最新完成运行的详情作为预报预填来源。
  const latestCompleted = runs.find(item => item.status === 'complete');
  let source = null;
  if (latestCompleted) {
    const detail = await workspace.getRun(latestCompleted.id);
    if (detail.result?.tables?.some(table => table.title === '逐日预报')) {
      source = detail;
    }
  }

  const form = session.canWrite
    ? panel('抽水排班计算', p('按日填写抽水上限、电价、保供底线和预警线，提交后由后台求全期最低电费。', 'subtle'),
        buildForm(spec, source, refresh))
    : panel('抽水排班计算', p('只读角色可查看当前方案与历史代次。', 'empty'));

  const activePanel = active
    ? panel('当前方案（交班页）',
        link(active.name, `#pump/${active.id}`, {class: 'quiet'}),
        pill(PUMP_STATUS[active.status], active.status),
        p(active.result?.headline || active.error, 'intro-copy'),
        p(`代次 G${active.generation} · 规则 ${active.rule_revision} · ${date(active.finished_at)}`, 'subtle'))
    : panel('当前方案（交班页）', empty('还没有生效的抽水排班方案。', '#pump', '发起首次计算'));

  const generationList = panel('排班代次', table([
    {label: '代次', render: item => `G${item.generation}`},
    {label: '方案', render: item => link(item.name, `#pump/${item.id}`)},
    {label: '状态', render: item => pill(PUMP_STATUS[item.status], item.status)},
    {label: '当前', render: item => (item.is_active ? '是' : '—')},
    {label: '规则版本', key: 'rule_revision'},
    {label: '完成时间', render: item => date(item.finished_at)},
  ], plans, {caption: '排班代次', empty: '首次发起后在此显示，旧方案在重算期间保持可用。'}));

  const detail = selected ? planDetail(selected, refresh)
    : panel(spec.pump.page, empty('选择一个排班代次查看逐日明细。', '#pump', '刷新'));

  return el('div', {}, el('div', {class: 'two-col'},
    el('div', {class: 'wide-panel'}, detail),
    el('aside', {class: 'sidebar-panel'}, activePanel, generationList,
      panel('排班口径', el('ul', {class: 'method-list'}, spec.pump.methodology.map(text => el('li', {}, text)))))),
    form);
}
