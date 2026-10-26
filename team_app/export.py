import json, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E

COLS = ['model_hour','grid_mw','tier1_relocated_mw','tier2_curtailed_mw','utility_solar_mw','wind_mw','battery_4h_charge_mw','battery_4h_discharge_mw','battery_4h_soc_mwh','gas_turbine_mw','nuclear_mw','gas_engine_mw','fuel_cell_mw']

def export(res, folder):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    caps = {k: int(v) if float(v).is_integer() else v for k, v in res['caps'].items()}
    (folder/'portfolio.json').write_text(json.dumps({'initial_service_month': res['start'], 'grid_delay_months': res['delay'], 'capacities_mw': caps}, indent=1))
    d = pd.DataFrame(res['_hourly']); d.insert(0, 'model_hour', range(len(d)))
    d[COLS].to_csv(folder/'dispatch.csv', index=False)
