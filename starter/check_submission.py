"""GRIDTECH v5.5.1: submission-only physical validation and common accounting.

Usage: python starter/check_submission.py portfolio.json dispatch.csv
Prints physical validity and common benchmark accounting JSON; not a ranking.
Readable validation must recompute inputs and costs internally. It is not
an optimizer, a dispatch engine, or a recommendation.
"""

"""Load/expand shared inputs. No portfolio search or dispatch engine."""
from pathlib import Path
import json
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
def read_json(name):return json.loads((ROOT/'data'/name).read_text())
hourly=pd.read_csv(ROOT/'data/reference_hourly.csv',keep_default_na=False)
options=pd.read_csv(ROOT/'data/resource_options.csv').fillna('')
case=read_json('case.json');response=read_json('response_parameters.json');trajectory=read_json('price_trajectory.json')
ramp=pd.read_csv(ROOT/'data/commissioning_ramp.csv');grid_schedule=pd.read_csv(ROOT/'data/grid_connection_schedule.csv')
def integer(v,name,low=0):
    if isinstance(v,bool) or not isinstance(v,(int,float,np.integer,np.floating)) or not np.isfinite(v) or v!=int(v) or v<low:raise ValueError(name+' must be a finite integer >= '+str(low))
    return int(v)
def unpack_portfolio(p):
    keys={'initial_service_month','grid_delay_months','capacities_mw'}
    if not isinstance(p,dict) or set(p)!=keys:raise ValueError('Portfolio requires exactly '+', '.join(sorted(keys)))
    start=integer(p['initial_service_month'],'initial_service_month',case['campus_ready_month'])
    if start+case['ramp_duration_months']>=case['horizon_months']:raise ValueError('Full demand must be reached before horizon end')
    delay=integer(p['grid_delay_months'],'grid_delay_months')
    caps=p['capacities_mw']
    if not isinstance(caps,dict) or set(caps)-set(options.option_id):raise ValueError('Unknown energy resource or invalid capacities_mw')
    land=0.
    for _,r in options.iterrows():
        v=caps.get(r.option_id,0)
        if isinstance(v,bool) or not isinstance(v,(int,float,np.integer,np.floating)) or not np.isfinite(v) or v<0 or v>r.max_mw+1e-7 or abs(v/r.block_mw-round(v/r.block_mw))>1e-7:raise ValueError(r.option_id+' capacity/module limit')
        land+=v*r.land_acres_per_mw
    if land>case['onsite_energy_land_acres']+1e-7:raise ValueError('Land limit')
    return caps,start,delay,land

def prepare_inputs(portfolio):
    caps,start,delay,land=unpack_portfolio(portfolio)
    n=case['horizon_months']*case['hours_per_model_month'];mh=np.arange(n);ref=mh%case['hours_per_year'];month=mh//case['hours_per_model_month']
    rel=month-start
    fraction=np.interp(rel,ramp.months_since_initial_service,ramp.load_fraction,left=0,right=1)
    get=lambda col:hourly[col].to_numpy()[ref]
    # Real-terms trajectory (data/price_trajectory.json): multiplier=(1+rate)**(model_hour/8760), 1.0 at hour 0.
    rates=trajectory['annual_real_rates'];years=mh/case['hours_per_year']
    m_wholesale=(1+rates['wholesale_energy'])**years;m_gas=(1+rates['henry_hub'])**years;m_co2=(1+rates['grid_co2'])**years
    grid_price=get('wholesale_price_usd_per_mwh')*m_wholesale+get('grid_adder_usd_per_mwh')
    gas_price=get('henry_hub_usd_per_mmbtu')*m_gas+get('delivered_gas_adder_usd_per_mmbtu')
    grid_co2=get('grid_co2_t_per_mwh')*m_co2
    load=get('load_mw')*fraction
    t1=get('tier1_relocatable_mw')*fraction;t2=get('tier2_interruptible_mw')*fraction;t0=load-t1-t2
    ordinary=np.zeros(n)
    for _,g in grid_schedule.iterrows():ordinary=np.where(month>=g.from_month+(delay if g.max_import_mw>0 else 0),g.max_import_mw,ordinary)
    active=get('stress_active').astype(bool);impfrac=get('stress_import_fraction').astype(float)
    limit=ordinary*impfrac
    remote=response['remote_capacity_mw']
    eligible=active&(get('notice_minutes')>=response['relocation_notice_minutes'])
    reloc=np.where(eligible,np.minimum(t1,remote/response['remote_energy_multiplier']),0.)
    curt=np.where(active,t2*response['tier2_max_fraction'],0.)
    frame=pd.DataFrame({'model_hour':mh,'development_month':month,'model_year':mh//8760+1,'reference_hour':ref,'demand_fraction':fraction,'load_mw':load,'tier0_protected_mw':t0,'tier1_relocatable_mw':t1,'tier2_interruptible_mw':t2,'ordinary_grid_limit_mw':ordinary,'effective_grid_limit_mw':limit,'stress_active':active.astype(int),'stress_import_fraction':impfrac,'notice_minutes':get('notice_minutes'),'stress_event_id':np.where(active,'Y'+pd.Series(mh//8760+1).astype(str)+'_'+get('stress_event_id'),'none')})
    for col in ['wind_proxy_cf','solar_proxy_cf','firm_clean_available_fraction']:
        frame[col]=get(col)
    frame['grid_co2_t_per_mwh']=grid_co2
    frame['merchant_price_usd_per_mwh']=grid_price
    frame['delivered_gas_price_usd_per_mmbtu']=gas_price
    return dict(frame=frame,n=n,caps=caps,start=start,delay=delay,land=land,ref=ref,month=month,year=mh//8760,load=load,tier0=t0,tier1=t1,tier2=t2,active=active,grid_limit=limit,relocation_limit=reloc,interruption_limit=curt,
      gas_price=gas_price,grid_price=grid_price,grid_co2=grid_co2,remote_co2=response['remote_co2_t_per_mwh']*m_co2,
      relocation_price=(response['remote_energy_multiplier']*response['remote_electricity_usd_per_mwh']+response['relocation_service_usd_per_local_mwh']),interruption_price=response['interruption_consequence_usd_per_mwh'])
def available_mask(row,ctx):return ctx['month']>=max(ctx['start'],int(row.available_month))
def profile(row,ctx):
    a=hourly[row.profile_column].to_numpy(float)[ctx['ref']] if row.profile_column else np.ones(ctx['n'])
    return a*available_mask(row,ctx)
def default_portfolio():return {'initial_service_month':case['default_initial_service_month'],'grid_delay_months':0,'capacities_mw':{}}
def validate_inputs():
    if len(hourly)!=8760:raise ValueError('Reference year must have8760hours')
    ts=pd.to_datetime(hourly.timestamp_utc,utc=True)
    if not ts.is_unique or not (ts.diff().dropna()==pd.Timedelta(hours=1)).all():raise ValueError('Reference timestamps')
    if not np.isfinite(hourly.select_dtypes('number').to_numpy()).all():raise ValueError('Nonfinite input')
    if not np.allclose(hourly[['tier0_protected_mw','tier1_relocatable_mw','tier2_interruptible_mw']].sum(axis=1),hourly.load_mw,atol=1e-7,rtol=0):raise ValueError('Tier sum')
    if (hourly[['tier0_protected_mw','tier1_relocatable_mw','tier2_interruptible_mw']]<0).any().any():raise ValueError('Negative tier')
    if not hourly.wind_proxy_cf.between(0,1).all() or not hourly.solar_proxy_cf.between(0,1).all():raise ValueError('Renewable profile')
    if not options.option_id.is_unique or len(options)!=7:raise ValueError('Catalog')
    if ramp.months_since_initial_service.tolist()!=list(range(11)) or not np.allclose(ramp.load_fraction,[min(1,1/3+k/15) for k in range(11)],atol=1e-12,rtol=0):raise ValueError('Ramp')
    if not np.allclose(grid_schedule[['from_month','max_import_mw']],pd.DataFrame(case['grid_access'])[['from_month','max_import_mw']]):raise ValueError('Grid schedule and case disagree')
    if not np.isfinite(options.select_dtypes('number').to_numpy()).all():raise ValueError('Catalog numeric values')
    if (options.life_years<=case['horizon_months']/12).any():raise ValueError('Life must exceed horizon in core accounting')
    ev=pd.read_csv(ROOT/'data/stress_events.csv');expected=np.zeros(8760,int)
    for _,e in ev.iterrows():
        a,b,notice=map(pd.Timestamp,[e.start_utc,e.end_utc,e.notice_utc]);m=(ts>=a)&(ts<b)
        if m.sum()!=e.duration_hours or (a-notice).total_seconds()<1800 or expected[m].any():raise ValueError('Event windows/notice')
        expected[m]=1
        if not np.allclose(hourly.loc[m,'stress_import_fraction'],e.import_fraction):raise ValueError('Event fraction')
    if not np.array_equal(expected,hourly.stress_active):raise ValueError('Event markers')
    return {'input_checks_passed':True,'reference_hours':8760,'timeline_hours':case['horizon_months']*730,'resources':7,'reference_stress_hours':int(expected.sum())}


# --- Released-data integrity check (do not edit) ---
import hashlib
RELEASED_SHA256={
    'case.json':'a06a599dfd8fcfaad64979ba83d58f275788169e3137ce02951531073ee26e19',
    'commissioning_ramp.csv':'200c9c53aea2d37639eb44b07697c9ac605380a07ba617dab4d59b262e5d3296',
    'price_trajectory.json':'c4f20bbf8db90b2d52642bc0c3a809ff27b23431ff4fbf98823b0a7e1df6eb9d',
    'grid_connection_schedule.csv':'4c3cd07ca9c204b91cfcf16f6b5edef45529b82f7b4655c5a7c3b7655232d63b',
    'reference_hourly.csv':'a1b2588da52059363524aa2020ce506e702741db69efb0a5cc67477b58993e93',
    'resource_options.csv':'374f77d144befbebd94576a60f3f2dac417008ec30d2049ecddc883dc3dbb64d',
    'response_parameters.json':'5e994e35b3a04ae3561a57fd960abfc0ae5a42a7b90704268481c27fd558615d',
    'stress_events.csv':'03c21e6404e14a0bb7bd91660f36066b2707f94e8a8cb62e04381e6a693ea3e2',
}
def verify_released_data():
    changed=[n for n,h in RELEASED_SHA256.items() if hashlib.sha256((ROOT/'data'/n).read_bytes()).hexdigest()!=h]
    if changed:raise ValueError('These data files differ from the released packet: '+', '.join(changed)+'. Restore the original files (re-extract the zip) and rerun.')

"""Check chronological submitted schedules. Does not generate a solution."""
import json,sys
from pathlib import Path
import numpy as np,pandas as pd
TOL=1e-5

def required_columns():
    c=['model_hour','grid_mw','tier1_relocated_mw','tier2_curtailed_mw']
    for _,r in options.iterrows():c+=([r.option_id+'_charge_mw',r.option_id+'_discharge_mw',r.option_id+'_soc_mwh'] if r.kind=='storage' else [r.option_id+'_mw'])
    return c

def check(portfolio,d,context=None):
    ctx=prepare_inputs(portfolio) if context is None else context;n=ctx['n'];p=ctx['caps'];tol=TOL
    if not set(required_columns())<=set(d.columns):raise ValueError('Missing dispatch columns')
    if len(d)!=n or not np.array_equal(d.model_hour.to_numpy(),np.arange(n)):raise ValueError('model_hour must cover entire horizon in order')
    vals=d[required_columns()[1:]].to_numpy(float)
    if not np.isfinite(vals).all() or vals.min()<-tol:raise ValueError('Negative or nonfinite dispatch')
    reloc=d.tier1_relocated_mw.to_numpy(float);curt=d.tier2_curtailed_mw.to_numpy(float)
    if np.max(reloc-ctx['relocation_limit'])>tol:raise ValueError('Tier1 eligibility/notice/remote capacity')
    if np.max(curt-ctx['interruption_limit'])>tol:raise ValueError('Tier2 eligibility/quantity')
    local=ctx['load']-reloc-curt
    if np.max(ctx['tier0']-local)>tol:raise ValueError('Protected demand')
    balance=d.grid_mw.to_numpy(float).copy();imports=balance.copy();clean=np.zeros(n);charge=np.zeros(n);storage_net=np.zeros(n)
    for _,r in options.iterrows():
        k=r.option_id;mw=p.get(k,0);mask=available_mask(r,ctx)
        if r.kind=='storage':
            ch=d[k+'_charge_mw'].to_numpy(float);di=d[k+'_discharge_mw'].to_numpy(float);soc=d[k+'_soc_mwh'].to_numpy(float);eta=np.sqrt(r.round_trip_efficiency)
            if np.max(ch-mw*mask)>tol or np.max(di-mw*mask)>tol:raise ValueError('Battery power or availability')
            if np.max(soc-mw*r.duration_hours*mask)>tol or abs(soc[-1])>tol:raise ValueError('Battery energy or final boundary')
            if np.max(np.abs(soc-np.r_[0.,soc[:-1]]-eta*ch+di/eta))>tol:raise ValueError('Battery continuity/energy conservation')
            if np.minimum(ch,di).max()>tol:raise ValueError('Simultaneous battery charge/discharge')
            for y in range(case['horizon_months']//12):
                m=ctx['year']==y;allowed=mw*r.duration_hours*r.max_cycles_per_year*float(mask[m].sum())/8760
                if di[m].sum()/eta>allowed+tol:raise ValueError('Battery annual cycle limit')
            balance+=di-ch;charge+=ch;storage_net+=di-ch
        else:
            g=d[k+'_mw'].to_numpy(float)
            if np.max(g-mw*profile(r,ctx))>tol:raise ValueError(k+' output/availability date')
            balance+=g
            if r.location=='offsite':imports+=g
            if r.clean_credit:clean+=g
    if np.max(charge-clean)>tol:raise ValueError('Battery charging must use contemporaneous clean portfolio energy')
    if np.max(imports-ctx['grid_limit'])>tol:raise ValueError('Grid connection/stress import limit')
    if np.max(np.abs(balance-local))>tol:raise ValueError('Energy balance/unreported unmet demand')
    clean+=storage_net
    if clean.min()<-tol or np.max(clean-local)>tol:raise ValueError('Clean energy double counting')
    return {'physical_checks_passed':True,'initial_service_month':int(ctx['start']),'full_demand_month':int(ctx['start']+case['ramp_duration_months']),'grid_delay_months':int(ctx['delay']),'timeline_hours':n,'land_acres':float(ctx['land']),'max_balance_error_mw':float(np.max(np.abs(balance-local))),'max_grid_import_mw':float(imports.max()),'clean_energy_share':float(clean.sum()/local.sum()),'tier0_unserved_mwh':0.}

"""Recompute common chronological accounting; no selection or dispatch engine."""
import json,sys
from pathlib import Path
import numpy as np,pandas as pd

def score(portfolio,d):
    verify_released_data();validate_inputs()
    ctx=prepare_inputs(portfolio);physical=check(portfolio,d,ctx);n=ctx['n'];ledger=[];cost=np.zeros(n);co2=np.zeros(n);clean=np.zeros(n)
    for _,r in options.iterrows():
        k=r.option_id;mw=ctx['caps'].get(k,0);mask=available_mask(r,ctx);crf=case['discount_rate_real']/(1-(1+case['discount_rate_real'])**(-r.life_years))
        fixed_annual=mw*1000*(r.capex_usd_per_kw*crf+r.fom_usd_per_kw_year);fixed=mask*fixed_annual/8760
        gen=np.zeros(n) if r.kind=='storage' else d[k+'_mw'].to_numpy(float)
        billed=mw*profile(r,ctx) if r.payment_basis=='available_generation' else gen
        payment=billed*r.payment_usd_per_mwh;fuel=gen*ctx['gas_price']*r.heat_rate_mmbtu_per_mwh_hhv;resource_cost=fixed+payment+fuel
        cost+=resource_cost;co2+=gen*r.co2_t_per_mwh
        if r.clean_credit and r.kind!='storage':clean+=gen
        if r.kind=='storage':clean+=d[k+'_discharge_mw'].to_numpy()-d[k+'_charge_mw'].to_numpy()
        ledger.append({'option_id':k,'selected_mw':float(mw),'commitment_start_month':int(max(ctx['start'],r.available_month)),'charged_hours':int(mask.sum()) if mw else 0,'capacity_charges_usd':float(fixed.sum()),'energy_payment_or_variable_om_usd':float(payment.sum()),'fuel_cost_usd':float(fuel.sum()),'total_cost_usd':float(resource_cost.sum())})
    grid=d.grid_mw.to_numpy(float);cost+=grid*ctx['grid_price'];co2+=grid*ctx['grid_co2']
    reloc=d.tier1_relocated_mw.to_numpy(float);interrupted=d.tier2_curtailed_mw.to_numpy(float);remote=reloc*response['remote_energy_multiplier'];local=ctx['load']-reloc-interrupted
    rcost=reloc*ctx['relocation_price'];icost=interrupted*ctx['interruption_price'];rco2=remote*ctx['remote_co2']
    def aggregate(m):
        requested=float(ctx['load'][m].sum());served=float(local[m].sum());rem=float(remote[m].sum());intr=float(interrupted[m].sum());lc=float(cost[m].sum());rr=float(rcost[m].sum());ii=float(icost[m].sum());ce=float(co2[m].sum());re=float(rco2[m].sum());cm=float(clean[m].sum())
        div=lambda a,b:a/b if b>0 else None
        return {'requested_mwh':requested,'local_served_mwh':served,'relocated_local_equivalent_mwh':float(reloc[m].sum()),'remote_electricity_mwh':rem,'interrupted_mwh':intr,'service_completion_share':div(requested-intr,requested),'local_energy_cost_usd':lc,'relocation_cost_usd':rr,'interruption_consequence_usd':ii,'total_decision_cost_usd':lc+rr+ii,'local_energy_cost_usd_per_mwh':div(lc,served),'decision_cost_usd_per_requested_mwh':div(lc+rr+ii,requested),'local_operational_co2_t':ce,'remote_operational_co2_t':re,'combined_operational_co2_t':ce+re,'combined_electricity_co2_kg_per_mwh':div(1000*(ce+re),served+rem),'service_co2_kg_per_served_equivalent_mwh':div(1000*(ce+re),requested-intr),'clean_energy_mwh':cm,'clean_energy_share':div(cm,served),'merchant_mwh':float(grid[m].sum())}
    total=aggregate(np.ones(n,bool));annual=[{'model_year':y+1,**aggregate(ctx['year']==y)} for y in range(case['horizon_months']//12)]
    result={**physical,**total,'capacity_cost_ledger':ledger,'annual_results':annual,'accounting_basis':'Undiscounted sum of annualized-capacity screening charges and energy/response costs; not project NPV. Fixed horizon month0..120. Constant 2023 USD; repeated historical year with merchant wholesale, Henry Hub gas and grid/remote CO2 on the real-terms path in data/price_trajectory.json; screening scenario, not a forecast.', 'comparison_note':'Show timing and requested/interrupted MWh with costs. Later service delivers less electricity within the fixed horizon; cheaper total alone does not establish a better outcome. Primary100kg threshold uses combined_electricity_co2_kg_per_mwh.'}
    return result

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Verify an existing JSON portfolio and hourly CSV against shared rules; calculate common screening-accounting totals (not ranking).')
    parser.add_argument('portfolio',type=Path)
    parser.add_argument('dispatch',type=Path)
    a=parser.parse_args()
    try:
        result=score(json.loads(a.portfolio.read_text()),pd.read_csv(a.dispatch))
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        raise SystemExit('SUBMISSION CHECK FAILED: '+str(exc))
    print(json.dumps(result,indent=2,allow_nan=False))
