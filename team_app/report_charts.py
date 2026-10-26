"""Report charts: the seven visuals of the second model (model/GridTech.ipynb) and its input-cost dashboard (model/Resource_Costs.ipynb), ported to
read from OUR report data (report.build), so they follow whichever case is in force. Chart design and wording follow the notebooks; the dashboard's
"key takeaways" are computed from the data (the notebook typed them in, and its stress-price claim was understated).

    render_all(R, outdir, co2_target=100, rules=None) -> [{'key', 'title', 'file'}, ...]
"""
import sys
import threading
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Patch  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import report as RP  # noqa: E402

E, cs, gm = RP.E, RP.cs, RP.gm
HPY = 8760
_LOCK = threading.Lock()                       # matplotlib's pyplot state is not thread safe
CAP_LABEL = {'utility_solar': 'Solar contract', 'wind': 'Wind contract', 'nuclear': 'Nuclear contract', 'gas_turbine': 'Gas turbine', 'gas_engine': 'Gas engine',
             'fuel_cell': 'Fuel cell', 'battery_4h': 'Battery'}
PROFILE = {'utility_solar': 'solar_proxy_cf', 'wind': 'wind_proxy_cf', 'nuclear': 'firm_clean_available_fraction'}
TITLES = {
    '1_scorecard': 'Recommended energy portfolio at a glance', '2_energy_mix': 'Where the electricity comes from, year by year', '3_annual_cost': 'What it costs each year, and who is paid',
    '4_carbon': 'How clean the electricity is', '5_asset_use': 'How hard each resource works', '6_cost_per_mwh_by_source': 'What each source really costs per MWh it delivers',
    '7_stress_week': 'How the campus stays powered when the grid is under stress', '8_input_costs': 'Resource input costs at a glance'}


def caption(fig, text):
    fig.text(0.5, 0.005, text, ha='center', va='bottom', fontsize=11, style='italic', color='#333', wrap=True)


def render_all(R, outdir, co2_target=100.0, rules=None):
    with _LOCK:
        return _render_all(R, outdir, co2_target, rules)


def _render_all(R, outdir, co2_target, rules):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    d, t, res, caps, av, yearly = R['d'], R['t'], R['res'], R['caps'], R['av'], R['yearly']
    num = E.NUM
    start, full_month, land = R['start'], R['full_month'], R['land']
    rules = rules or dict(ok=True, text='Checked by the model rules')
    NAME, COL = RP.NAME, RP.COLOR
    YRS = [y for y in range(1, 11) if yearly.loc[y, 'requested_mwh'] > 0]
    XL = [f'Year {y}' for y in YRS]
    SRC = [k for k in RP.SRC_ORDER if d[k].sum() > 1e-6]
    H = yearly.loc['horizon']
    yr = t.model_year.values
    plt.rcParams.update({'axes.spines.top': False, 'axes.spines.right': False, 'font.size': 11})
    made = []

    def save(fig, key):
        fn = f'{key}.png'
        fig.savefig(out / fn, dpi=140, bbox_inches='tight')
        plt.close(fig)
        made.append(dict(key=key, title=TITLES[key], file=fn))

    # ---------------------------------------------------------------- 1. scorecard
    tiles = [
        ('Total cost, 10-year window', f'${H.total_decision_cost / 1e6:,.0f}M', 'everything: equipment, contracts, fuel, grid', '#2c3e50'),
        ('Average cost', f'${H.decision_usd_per_mwh:,.0f}', 'per MWh of demand', '#2c3e50'),
        ('Carbon intensity', f'{H.combined_kg_co2_per_mwh:,.0f} kg', f'CO2 per MWh  (reference {co2_target:g})', '#27ae60' if H.combined_kg_co2_per_mwh <= co2_target else '#c0392b'),
        ('Clean energy share', f'{H.clean_share:.0%}', 'from wind, solar, nuclear', '#27ae60' if H.clean_share >= 0.5 else '#e67e22'),
        ('Opening timeline', f'Month {start}', f'first power;  full campus month {full_month}', '#2c3e50'),
        ('Reliance on grid purchases', f'{H.merchant_share:.0%}', "of the campus's electricity", '#e67e22' if H.merchant_share > 0.25 else '#27ae60'),
        ('Demand served', f'{(1 - H.unserved_mwh / H.requested_mwh):.1%}', 'protected load always on site', '#27ae60' if H.unserved_mwh < 1 else '#c0392b'),
        ('Grid-stress response', f'{(H.relocated_mwh + H.interrupted_mwh):,.0f} MWh', f'{H.relocated_mwh:,.0f} moved + {H.interrupted_mwh:,.0f} paused', '#2c3e50'),
    ]
    fig = plt.figure(figsize=(16, 9))
    for i, (head, big, small, colr) in enumerate(tiles):
        ax = fig.add_axes([0.02 + (i % 4) * 0.245, 0.68 - (i // 4) * 0.27, 0.225, 0.23]); ax.axis('off')
        ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle='round,pad=0.02,rounding_size=0.06', fc='#f4f6f7', ec=colr, lw=2.5, transform=ax.transAxes))
        ax.text(0.5, 0.80, head, ha='center', fontsize=12, color='#555', transform=ax.transAxes)
        ax.text(0.5, 0.45, big, ha='center', fontsize=28, fontweight='bold', color=colr, transform=ax.transAxes)
        ax.text(0.5, 0.15, small, ha='center', fontsize=10, color='#555', transform=ax.transAxes)
    ax = fig.add_axes([0.08, 0.06, 0.62, 0.24])
    items = [(CAP_LABEL[k], v, COL[k]) for k, v in caps.items() if v]
    if items:
        ax.barh([i[0] for i in items], [i[1] for i in items], color=[i[2] for i in items])
        for j, (_, v, _) in enumerate(items):
            ax.text(v + 0.8, j, f'{v:g} MW', va='center', fontweight='bold')
        ax.invert_yaxis()
    ax.set_title('What we recommend building / contracting', fontweight='bold', loc='left'); ax.set_xlabel('MW')
    ax2 = fig.add_axes([0.76, 0.06, 0.22, 0.24]); ax2.axis('off')
    ax2.text(0, 0.85, 'Rules check', fontsize=13, fontweight='bold')
    ax2.text(0, 0.55, 'PASSED' if rules['ok'] else 'CHECK FAILED', fontsize=24, fontweight='bold', color='#27ae60' if rules['ok'] else '#c0392b')
    ax2.text(0, 0.12, f"{rules['text']}\nonsite land used {land:.1f} of {R['land_limit']:g} acres", fontsize=10, color='#555')
    fig.suptitle('Recommended energy portfolio - at a glance', fontsize=20, fontweight='bold', y=0.97)
    save(fig, '1_scorecard')

    # ---------------------------------------------------------------- 2. energy mix
    mix = R['mix'].loc[YRS]
    fig, ax = plt.subplots(figsize=(15, 7))
    bot = np.zeros(len(YRS))
    for k in SRC:
        ax.bar(XL, mix[k], bottom=bot, color=COL[k], label=NAME[k], edgecolor='white', lw=0.5)
        bot += mix[k].values
    for i, y in enumerate(YRS):
        ax.text(i, bot[i] + 8, f"{yearly.loc[y, 'clean_share']:.0%} clean", ha='center', fontweight='bold', color='#1e8449')
    ax.set_ylabel('Electricity supplied (GWh per year)'); ax.set_ylim(0, bot.max() * 1.12)
    ax.legend(loc='upper left', fontsize=10, ncol=2, frameon=False)
    ax.set_title("Where does the campus's electricity come from each year?", fontsize=16, fontweight='bold', loc='left')
    first_nuc = next((y for y in YRS if d.nuclear[yr == y].sum() > 0), None)
    caption(fig, 'The first year shown may be short because the campus opens part-way through it. Bars grow as the campus fills up.'
            + (f'  From year {first_nuc}, the nuclear contract replaces most grid purchases and gas.' if first_nuc and caps.get('nuclear') else ''))
    plt.tight_layout(rect=[0, 0.05, 1, 1]); save(fig, '2_energy_mix')

    # ---------------------------------------------------------------- 3. annual cost
    C = R['cost'].loc[YRS]
    order = [k for k in ['nuclear', 'wind', 'utility_solar', 'battery_4h', 'fuel_cell', 'gas_engine', 'gas_turbine', 'grid', 'response'] if k in C and C[k].sum() > 1e-6]
    fig, ax = plt.subplots(figsize=(15, 7))
    bot = np.zeros(len(YRS))
    for k in order:
        ax.bar(XL, C[k], bottom=bot, color=COL[k], label=NAME[k], edgecolor='white', lw=0.5)
        bot += C[k].values
    for i, v in enumerate(bot):
        ax.text(i, v + 0.6, f'${v:.1f}M', ha='center', fontweight='bold')
    ax.set_ylabel('Cost (\\$ million per year)'); ax.set_ylim(0, bot.max() * 1.25)
    ax2 = ax.twinx()
    ax2.plot(XL, yearly.loc[YRS, 'decision_usd_per_mwh'], 'o-', color='black', lw=2.5, label='Cost per MWh (right axis)')
    ax2.set_ylabel('\\$ per MWh'); ax2.set_ylim(0, yearly.loc[YRS, 'decision_usd_per_mwh'].max() * 1.6); ax2.spines['right'].set_visible(True)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc='upper left', fontsize=10, ncol=3, frameon=False)
    ax.set_title('What does it cost each year, and who are we paying?', fontsize=16, fontweight='bold', loc='left')
    caption(fig, 'Contracts and onsite equipment are paid every year whether used or not; grid power and gas fuel are paid only when used.')
    plt.tight_layout(rect=[0, 0.05, 1, 1]); save(fig, '3_annual_cost')

    # ---------------------------------------------------------------- 4. carbon
    co2 = yearly.loc[YRS, 'combined_kg_co2_per_mwh'].values
    fig, ax = plt.subplots(figsize=(15, 6.5))
    ax.bar(XL, co2, color=['#27ae60' if v <= co2_target else '#e67e22' for v in co2])
    for i, v in enumerate(co2):
        ax.text(i, v + 4, f'{v:.0f}', ha='center', fontweight='bold')
    ax.axhline(co2_target, color='#c0392b', ls='--', lw=2)
    ax.text(len(YRS) - 0.5, co2_target + 4, f'reference {co2_target:g} kg/MWh', color='#c0392b', ha='right', fontweight='bold')
    ax.set_ylabel('kg of CO2 per MWh used'); ax.set_ylim(0, max(co2.max(), co2_target) * 1.25)
    ax2 = ax.twinx()
    ax2.plot(XL, yearly.loc[YRS, 'clean_share'].values * 100, 'o-', color='#1e8449', lw=2.5)
    ax2.set_ylabel('Clean energy share (%)', color='#1e8449'); ax2.set_ylim(0, 110); ax2.spines['right'].set_visible(True)
    ax.legend(handles=[Patch(color='#27ae60', label=f'At or below {co2_target:g} kg'), Patch(color='#e67e22', label=f'Above {co2_target:g} kg'),
                       plt.Line2D([], [], color='#1e8449', marker='o', lw=2.5, label='Clean share (right axis)')], loc='upper right', frameon=False, fontsize=10)
    ax.set_title(f'How clean is the electricity?  10-year average {H.combined_kg_co2_per_mwh:.0f} kg CO2/MWh', fontsize=16, fontweight='bold', loc='left')
    caption(fig, 'Lower bars are cleaner. Emissions come from grid purchases, onsite gas and relocated work; wind, solar and nuclear count as zero.')
    plt.tight_layout(rect=[0, 0.05, 1, 1]); save(fig, '4_carbon')

    # ---------------------------------------------------------------- 5. asset use
    rows = []
    for r in ['utility_solar', 'wind', 'nuclear', 'gas_turbine', 'gas_engine', 'fuel_cell', 'battery_4h']:
        if not caps.get(r):
            continue
        col = 'dis' if r == 'battery_4h' else r
        hrs = av[r].sum()
        rows.append((CAP_LABEL[r], caps[r], d[col].max(), d[col].sum() / (caps[r] * hrs) if hrs else 0, COL[r]))
    if rows:
        fig, (ax, axu) = plt.subplots(1, 2, figsize=(16, 6.5), gridspec_kw={'width_ratios': [1.3, 1]})
        yy = np.arange(len(rows))
        ax.barh(yy + 0.2, [r[1] for r in rows], 0.4, color='#d5d8dc', label='Size contracted / built')
        ax.barh(yy - 0.2, [r[2] for r in rows], 0.4, color=[r[4] for r in rows], label='Highest output in any hour')
        for i, r in enumerate(rows):
            ax.text(r[1] + 0.8, i + 0.2, f'{r[1]:.0f} MW', va='center', fontsize=9, color='#555')
            ax.text(r[2] + 0.8, i - 0.2, f'{r[2]:.0f} MW', va='center', fontsize=9, fontweight='bold')
        ax.set_yticks(yy); ax.set_yticklabels([r[0] for r in rows]); ax.invert_yaxis(); ax.set_xlabel('MW'); ax.legend(frameon=False, loc='lower right')
        ax.set_title('Size vs. highest output', fontweight='bold', loc='left')
        axu.barh(yy, [r[3] * 100 for r in rows], color=[r[4] for r in rows])
        for i, r in enumerate(rows):
            axu.text(r[3] * 100 + 1, i, f'{r[3]:.0%}', va='center', fontweight='bold')
        axu.set_yticks(yy); axu.set_yticklabels([]); axu.invert_yaxis(); axu.set_xlim(0, 115)
        axu.set_xlabel('Average utilisation (% of the time at full output)'); axu.set_title('How much of the time it is effectively running', fontweight='bold', loc='left')
        fig.suptitle('How hard is each resource working?', fontsize=16, fontweight='bold', x=0.01, ha='left')
        caption(fig, 'Wind and solar are limited by weather and the shared grid connection; the battery is held mainly for grid-stress hours.')
        plt.tight_layout(rect=[0, 0.05, 1, 0.95]); save(fig, '5_asset_use')

    # ---------------------------------------------------------------- 6. unit cost table
    unit = R['unit'].rename(columns={'Merchant grid': 'Grid power', 'Solar': 'Solar contract', 'Wind': 'Wind contract', 'Nuclear': 'Nuclear contract',
                                     'Portfolio avg (local $/MWh served)': 'Overall average'})
    unit = unit.loc[YRS + ['horizon']]
    unit.index = [f'Year {y}' for y in YRS] + ['10-yr average']
    vals = unit.values.astype(float)
    with np.errstate(divide='ignore', invalid='ignore'):
        lv = np.log10(vals)
    lo, hi = np.nanmin(lv), np.nanmax(lv)
    cmap = plt.get_cmap('RdYlGn_r')
    colours = [[(1, 1, 1, 1) if np.isnan(v) else cmap(0.12 + 0.76 * ((v - lo) / (hi - lo) if hi > lo else 0.5)) for v in row] for row in lv]
    txt = [['-' if np.isnan(v) else (f'${v:,.0f}' if v >= 100 else f'${v:,.1f}') for v in row] for row in vals]
    fig, ax = plt.subplots(figsize=(1.7 * (unit.shape[1] + 1), 0.55 * (unit.shape[0] + 3))); ax.axis('off')
    tb = ax.table(cellText=txt, rowLabels=list(unit.index), colLabels=list(unit.columns), cellColours=colours, cellLoc='center', loc='center')
    tb.auto_set_font_size(False); tb.set_fontsize(11); tb.scale(1, 1.7)
    for (r, c), cell in tb.get_celld().items():
        cell.set_edgecolor('white')
        if r == 0:
            cell.set_facecolor('#1f4e79'); cell.set_text_props(color='white', fontweight='bold')
        if c == -1:
            cell.set_facecolor('#d6e4f0'); cell.set_text_props(fontweight='bold')
        if r == unit.shape[0] or c == unit.shape[1] - 1:
            cell.set_text_props(fontweight='bold')
    ax.set_title('What each source really costs per MWh it delivers  (green = cheap, red = expensive)', fontsize=14, fontweight='bold')
    caption(fig, "Includes each source's fixed charges. A source that is paid for but rarely used (e.g. the battery) looks expensive per MWh.")
    save(fig, '6_cost_per_mwh_by_source')

    # ---------------------------------------------------------------- 7. stress week
    first_event_ref = int(np.argmax(t.stress_active.values[:HPY]))
    full_yrs = [y for y in YRS if t.demand_fraction[(t.model_year == y)].min() >= 1]
    if full_yrs:
        pick = [full_yrs[0], full_yrs[-1]] if len(full_yrs) > 1 else full_yrs
        if first_nuc and first_nuc in full_yrs and first_nuc != pick[0]:
            pick = [pick[0], first_nuc]
        fig, axes = plt.subplots(len(pick), 1, figsize=(16, 5.2 * len(pick)), sharey=True, squeeze=False)
        top = max(100.0, float(np.ceil(t.load_mw.max() / 25) * 25) + 25)
        for ax, y in zip(axes[:, 0], pick):
            s0 = (y - 1) * HPY + first_event_ref - 24
            sl = slice(s0, s0 + 168); hrs = np.arange(168)
            ax.stackplot(hrs, *[d[k].values[sl] for k in SRC], colors=[COL[k] for k in SRC], labels=[NAME[k] for k in SRC], step='post')
            ax.fill_between(hrs, 0, -d.ch.values[sl], step='post', color='#145a32', label='Battery charging')
            ax.step(hrs, t.load_mw.values[sl], where='post', color='black', lw=1.8, label='Campus demand')
            ax.step(hrs, t.effective_grid_limit_mw.values[sl], where='post', color='#c0392b', ls='--', lw=1.8, label='Grid connection limit')
            st = t.stress_active.values[sl].astype(bool)
            ax.fill_between(hrs, -15, top, where=st, step='post', color='#e74c3c', alpha=0.12)
            first = np.argmax(st)
            ax.annotate('Grid-stress event:\nlimit cut to 60%', (first + 2, top * 0.92), fontsize=10, color='#c0392b', fontweight='bold')
            ax.set_xticks(range(0, 169, 24)); ax.set_xticklabels([f'Day {i + 1}' for i in range(7)] + [''])
            ax.set_xlim(0, 167); ax.set_ylim(-15, top); ax.set_ylabel('MW'); ax.grid(alpha=0.25)
            ax.set_title(f'Year {y}: a week with grid-stress events', fontweight='bold', loc='left', fontsize=13)
        h, l = axes[0, 0].get_legend_handles_labels()
        fig.legend(h, l, loc='lower center', ncol=6, frameon=False, fontsize=10, bbox_to_anchor=(0.5, 0.03))
        fig.suptitle('How the campus stays powered when the grid is under stress', fontsize=16, fontweight='bold', x=0.01, ha='left')
        caption(fig, 'Red shading = grid asks the campus to cut imports to 60%. Onsite gas and the battery step up, and a little flexible work moves or pauses.')
        plt.tight_layout(rect=[0, 0.1, 1, 0.96]); save(fig, '7_stress_week')

    # ---------------------------------------------------------------- 8. input-cost dashboard (Resource_Costs.ipynb)
    g = lambda o, c: num(res.loc[o, c])
    rate = float(gm.CASE['discount_rate_real'])
    crf = lambda L_: rate / (1 - (1 + rate) ** -L_)
    ym = t.groupby('model_year')[['merchant_price_usd_per_mwh', 'delivered_gas_price_usd_per_mmbtu']].mean()
    yrs = ym.index.values
    gas = ym.delivered_gas_price_usd_per_mmbtu.values
    COLR = {'Merchant grid': '#7f8c8d', 'Gas turbine': '#a04000', 'Gas engine': '#d35400', 'Fuel cell': '#e67e22', 'Solar PPA': '#f1c40f', 'Wind PPA': '#1f78b4', 'Nuclear PPA': '#6a3d9a', 'Battery 4h': '#2ecc71'}
    GASM = {'Gas turbine': 'gas_turbine', 'Gas engine': 'gas_engine', 'Fuel cell': 'fuel_cell'}
    PPA = {'Solar PPA': 'utility_solar', 'Wind PPA': 'wind', 'Nuclear PPA': 'nuclear'}
    energy = {'Merchant grid': ym.merchant_price_usd_per_mwh.values}
    for lab, o in GASM.items():
        energy[lab] = g(o, 'payment_usd_per_mwh') + g(o, 'heat_rate_mmbtu_per_mwh_hhv') * gas
    for lab, o in PPA.items():
        energy[lab] = np.full(len(yrs), g(o, 'payment_usd_per_mwh'))
    fixed_kw = {lab: g(o, 'capex_usd_per_kw') * crf(g(o, 'life_years')) + g(o, 'fom_usd_per_kw_year') for lab, o in {**GASM, 'Battery 4h': 'battery_4h'}.items()}
    traj = cs.trajectory['annual_real_rates']
    fig = plt.figure(figsize=(18, 11.5))
    gs = fig.add_gridspec(2, 3, height_ratios=[1, 1], width_ratios=[1.35, 1, 1], hspace=0.38, wspace=0.28)
    ax = fig.add_subplot(gs[0, 0])
    NUDGE = {'Gas engine': 5, 'Gas turbine': -5, 'Nuclear PPA': 5, 'Merchant grid': -5, 'Wind PPA': 4, 'Fuel cell': -5}
    for lab, v in energy.items():
        ax.plot(yrs, v, '--' if 'PPA' in lab else '-', marker='o', ms=4, lw=2.2, color=COLR[lab])
        ax.annotate(f'{lab}  {v[-1]:.0f}', (yrs[-1], v[-1]), xytext=(6, NUDGE.get(lab, 0)), textcoords='offset points', va='center', fontsize=9, color=COLR[lab], fontweight='bold')
    ax.set_xticks(yrs); ax.set_xticklabels([f'Y{i}' for i in yrs]); ax.set_xlim(0.7, 12.6); ax.set_ylim(0, max(105, float(max(v.max() for v in energy.values())) * 1.1))
    ax.set_ylabel('\\$/MWh'); ax.set_title('1. Energy cost per MWh, by model year\ngas = fuel + VOM; PPA = contract; grid = avg price', fontsize=11, fontweight='bold', loc='left'); ax.grid(alpha=0.3)
    ax = fig.add_subplot(gs[0, 1])
    labs = list(fixed_kw)
    ax.bar(labs, fixed_kw.values(), color=[COLR[l] for l in labs])
    for i, v in enumerate(fixed_kw.values()):
        ax.text(i, v + 10, f'\\${v:.0f}', ha='center', fontweight='bold')
    ax.set_ylabel('\\$ per kW per year'); ax.set_ylim(0, max(fixed_kw.values()) * 1.18)
    ax.set_title('2. Fixed charge for owned onsite assets\npaid every hour once committed, used or not', fontsize=11, fontweight='bold', loc='left'); ax.tick_params(axis='x', labelsize=9); ax.grid(axis='y', alpha=0.3)
    ax = fig.add_subplot(gs[0, 2])
    Yr, cf = 4, 0.5
    items = ['Wind PPA', 'Solar PPA', 'Merchant grid', 'Gas turbine', 'Gas engine', 'Nuclear PPA', 'Fuel cell']
    var = [energy[i][Yr - 1] for i in items]
    fix = [fixed_kw.get(i, 0) * 1000 / (8760 * cf) for i in items]
    ordr = np.argsort(np.array(var) + np.array(fix))
    items = [items[i] for i in ordr]; var = [var[i] for i in ordr]; fix = [fix[i] for i in ordr]
    ax.barh(items, var, color=[COLR[i] for i in items])
    ax.barh(items, fix, left=var, color=[COLR[i] for i in items], alpha=0.45, hatch='//')
    for i, (a, b) in enumerate(zip(var, fix)):
        ax.text(a + b + 2, i, f'\\${a + b:.0f}', va='center', fontweight='bold', fontsize=9)
    ax.set_xlim(0, max(np.array(var) + np.array(fix)) * 1.2); ax.set_xlabel('\\$/MWh')
    ax.set_title(f'3. All-in cost if run 50% of the time (year {Yr})\nsolid = energy, hatched = fixed charge', fontsize=11, fontweight='bold', loc='left')
    ax.legend(handles=[Patch(color='#888', label='Energy cost'), Patch(facecolor='#888', alpha=0.45, hatch='//', label='Fixed charge')], fontsize=8, loc='lower right'); ax.grid(axis='x', alpha=0.3)
    ax = fig.add_subplot(gs[1, 0:2])
    avail = [('Fuel cell', 'fuel_cell'), ('Battery 4h', 'battery_4h'), ('Gas engine', 'gas_engine'), ('Gas turbine', 'gas_turbine'), ('Wind PPA', 'wind'), ('Solar PPA', 'utility_solar'), ('Nuclear PPA', 'nuclear')]
    for i, (lab, o) in enumerate(avail):
        m0 = g(o, 'available_month')
        ax.barh(i, 120 - m0, left=m0, color=COLR[lab], height=0.6)
        loc = 'onsite' if res.loc[o, 'location'] == 'onsite' else 'via grid'
        mx = 75 if o == 'gas_turbine' else g(o, 'max_mw')
        ax.text(m0 + 1, i, f"from month {m0:g}  ({loc}, {g(o, 'block_mw'):g} MW blocks, max {mx:g} MW)", va='center', fontsize=9, color='white', fontweight='bold')
    ax.set_yticks(range(len(avail))); ax.set_yticklabels([a[0] for a in avail])
    steps = [(int(m) + (R['delay'] if x > 0 else 0), float(x)) for m, x in zip(cs.grid_schedule.from_month, cs.grid_schedule.max_import_mw) if x > 0]
    for k, (m, mw) in enumerate(steps):
        ax.axvline(m, color='#c0392b', ls='--', lw=1)
        ax.text(m + 0.5, len(avail) - 0.75 + 0.33 * k, f'grid {mw:g} MW from m{m}', color='#c0392b', fontsize=9, fontweight='bold')
    ax.axvline(start, color='k', lw=1.5); ax.text(start + 0.5, -0.75, f'opening (m{start})', fontsize=9, fontweight='bold')
    ax.set_xlim(0, 120); ax.set_ylim(-1.1, len(avail) + 0.4); ax.invert_yaxis(); ax.set_xlabel('Development month')
    ax.set_title('4. When each resource can be used (grid import cap shown in red; every grid resource shares it)', fontsize=11, fontweight='bold', loc='left'); ax.grid(axis='x', alpha=0.3)
    ax = fig.add_subplot(gs[1, 2]); ax.axis('off')
    st_price = t.loc[t.stress_active == 1, 'merchant_price_usd_per_mwh']
    ann_t = fixed_kw['Gas turbine'] / (8.76 * cf) + energy['Gas turbine'][Yr - 1]
    fast = [lab for lab, o in {**GASM, 'Battery 4h': 'battery_4h'}.items() if g(o, 'available_month') <= 14]
    notes = [
        ('Cheapest energy', f"Wind \\${g('wind', 'payment_usd_per_mwh'):.0f} and solar \\${g('utility_solar', 'payment_usd_per_mwh'):.0f} contracts, but\nthey arrive through the shared grid cap."),
        ('Grid is cheap on average', f"\\${energy['Merchant grid'][0]:.0f} rising to \\${energy['Merchant grid'][-1]:.0f}/MWh (+{traj['wholesale_energy'] * 100:.0f}%/yr); in\nstress hours it averages \\${st_price.mean():,.0f}, peaks \\${st_price.max():,.0f}."),
        ('Gas: efficiency vs fixed cost', f"Fuel cell has the cheapest fuel (~\\${energy['Fuel cell'][Yr - 1]:.0f}) but the\nhighest fixed charge (\\${fixed_kw['Fuel cell']:.0f}/kW-yr)."),
        ('Best firm onsite option', f"Gas turbine ~\\${ann_t:.0f}/MWh all-in at 50% use;\n{g('gas_turbine', 'block_mw'):g} MW blocks, from month {g('gas_turbine', 'available_month'):g}, highest CO2."),
        ('Speed-to-power option', f"Onsite before month 15: {', '.join(fast)}.\nOffsite contracts wait for the grid connection."),
        ('Clean firm, but late & dear', f"Nuclear \\${g('nuclear', 'payment_usd_per_mwh'):.0f}/MWh, only from month {g('nuclear', 'available_month'):g}."),
    ]
    yy = 0.98
    ax.text(0, yy, 'Key takeaways', fontsize=13, fontweight='bold', transform=ax.transAxes, va='top'); yy -= 0.09
    for head, body in notes:
        ax.text(0, yy, '- ' + head, fontsize=10.5, fontweight='bold', transform=ax.transAxes, va='top')
        ax.text(0.04, yy - 0.045, body, fontsize=9.5, transform=ax.transAxes, va='top', color='#333')
        yy -= 0.155
    fig.suptitle(f"Resource input costs at a glance  (constant 2023 USD; gas {traj['henry_hub'] * 100:+.0f}%/yr, wholesale {traj['wholesale_energy'] * 100:+.0f}%/yr)", fontsize=15, fontweight='bold', y=0.985)
    save(fig, '8_input_costs')
    return made
