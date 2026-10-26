// Client for the model server (team_app/faraday_server.py). Same-origin: the dev server proxies /api to it, the built site is served by it.

export type ReqKey = "speed" | "carbon" | "cost";
export interface CaseRef { templateId?: string | null; state?: string | null; area?: string | null }
export interface Inputs { weights: number[]; goLive: number | null; carbonMax: number | null; costPct: number | null; excluded: string[]; case?: CaseRef }
export interface StressEvent { id: string; operator: string; type: string; startUtc: string; hours: number; importPct: number; noticeMin: number; confidence: string }
export interface StressInfo { area: string; count: number; minHours: number; maxHours: number; totalHours: number; months: string[]; importPct: number; noticeMin: number; events: StressEvent[]; source: string }
export interface CaseInfo { key: string; custom: boolean; ready: boolean; state?: string | null; area?: string | null; templateApplied: boolean; build: { status: string; message: string; error?: string | null; seconds: number }; starts: number[]; goLive: number }
export interface Catalog { source: string; areas: { area: string; operators: string[]; events: number; minHours: number; maxHours: number; totalHours: number; importFraction: number }[]; states: { state: string; postal: string; areas: { area: string; coverage: string; hasEvents: boolean; primary: boolean }[] }[] }

export interface Flag { id: string; area: string; status: "not-modeled" | "partial" | "derived" | "definition" | "scope"; message: string }
export interface Preset { weights: number[]; goLive: number | null; carbonMax: number | null; costPct: number | null }
export interface Meta {
  campusPeakMw: number; tier0PeakMw: number; months: number; hours: number; resources: number;
  gridSchedule: { fromMonth: number; maxImportMw: number }[];
  choices: { goLive: number[]; carbonMax: number[]; costPct: number[]; resources: { id: string; label: string }[]; rampMonths: number };
  presets: Record<string, Preset>;
  defaults: { weights: number[] };
  assumptions: { name: string; detail: string; source: string }[];
  flags: Flag[];
  window: { minOpening: number; maxOpening: number };
  poolSizes: { base: number; noTier2: number };
  response: { relocationUsdPerMwh: number; interruptionUsdPerMwh: number; remoteMw: number };
  stress: StressInfo; case: CaseInfo; catalog: Catalog;
}

export interface Option {
  role: string; name: string; label: string; detail: string; caps: Record<string, number>; start: number; opening: number; fullDemand: number; costM: number; kgPerMwh: number; backupMw: number;
  meets: Record<ReqKey, boolean>; tag: string;
}
export interface Kpis {
  costM: number; maintenanceM: number; dollarsPerMwh: number; kgPerMwh: number; cleanSharePct: number; onsiteBackupMw: number; servedTwh: number;
  interruptedMwh: number; relocatedMwh: number; decisionCostPerRequestedMwh: number;
}
export interface StressLayer {
  eventHours: number[]; relocatedMwh: number[]; interruptedMwh: number[]; relocationM: number[]; interruptionM: number[]; eventYear: number;
  events: { id: string; hours: number; demandMw: number; gridCapMw: number; gridMw: number; gasMw: number; relocatedMwh: number; interruptedMwh: number }[];
}
export interface Tradeoff { opening: number; fullDemand: number; costM: number; kgPerMwh: number; cleanSharePct: number; detail: string }
export interface Tradeoffs { top: "speed" | "cost" | "carbon" | "balanced"; weights: number[]; recommended: Tradeoff; anchors: { cheapest: Tradeoff; fastest: Tradeoff; cleanest: Tradeoff } }
export interface Power {
  years: number[]; span: string[]; stress: StressLayer;
  series: Record<"gas_turbine" | "gas_engine" | "fuel_cell" | "wind" | "solar" | "nuclear" | "merchant" | "battery" | "relocated" | "interrupted" | "demand" | "gridCap", number[]>;
}
export interface PortfolioRow {
  id: string; label: string; location: string; selected: boolean; mw: number; maxMw: number; blockMw: number; availableMonth: number; fromMonth: number; landAcres: number;
  fixedChargeMPerYear: number | null; priceUsdPerMwh: number | null; energyGwh: number; sharePct: number | null; capacityFactorPct: number | null;
}
export interface CostBreakdown {
  rows: { label: string; valueM: number; sharePct: number; note: string }[]; totalM: number; maintenanceM: number; maintenanceFixedM: number; maintenanceVariableM: number;
}
export interface Sensitivity {
  scenarios: { key: string; label: string; text: string; wholesalePct: number; gasPct: number }[];
  rows: { label: string; role: string; detail: string; opening: number; fullDemand: number; kgPerMwh: number; tag: string; backup: { name: string; totalM: number; addedMw: number }[]; perScenario: Record<string, { feasible: boolean; totalM: number; localUsdPerMwh: number; merchantSharePct: number; gasMwhShare: number; merchantM: number; gasM: number; kgPerMwh: number }>; deltaPct: { bear: number; bull: number } }[];
  yearly: { years: number[]; series: Record<string, number[]> } | null; note: string;
}
export interface Stress {
  hour: string; demandMw: number; tier0Mw: number; gridMw: number; gridCapMw: number; gasMw: number; batteryMw: number; relocatedMw: number; interruptedMw: number;
}
export interface Reliability {
  onsiteBackupMw: number; tier0PeakMw: number; batteryMw: number; batteryMwh: number; batteryDeliverableMwh: number; rideThroughMinutes: number;
  maintenanceM: number; maintenanceFixedM: number; maintenanceVariableM: number; stressHoursPerYear: number; stressHoursNoInterruptionPct: number;
  capMetPct: number; tier0ServedAlways: boolean;
  backup: { criticalMw: number; firmN1Mw: number; shortfallMw: number; options: { name: string; addedMw: number; totalM: number; note: string }[] };
}
export interface Recommendation {
  name: string; detail: string; opening: number; fullDemand: number; score: number; capacities: Record<string, number>; capacityText: string; why: string; nextStep: string; kpis: Kpis;
}
export interface Result {
  status: "ok" | "no_match" | "needs_build";
  case?: CaseInfo; tradeoffs?: Tradeoffs;
  inputs: Inputs;
  notes: string[]; flags: Flag[];
  message?: string; relaxations?: string[]; tested?: number;
  exportId?: string;
  search?: { tested: number; passing: number; scored: number; cheapestM: number | null; costCapM: number | null };
  recommendation?: Recommendation; options?: Option[]; powerOverTime?: Power; stress?: Stress; reliability?: Reliability; portfolioTable?: PortfolioRow[]; costBreakdown?: CostBreakdown;
}
export interface Verify { passed: boolean; totalDecisionCostM?: number; kgPerMwh?: number; maxGridImportMw?: number; fullDemandMonth?: number; error?: string }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new Error("The model server is not reachable. Start it with: python team_app/faraday_server.py");
  }
  const text = await res.text();
  let body: any;
  try { body = JSON.parse(text); } catch { throw new Error(res.ok ? "The model server returned something that is not JSON." : "The model server is not reachable (is it running on port 8770?)."); }
  if (!res.ok) throw new Error(body?.error ?? `Model server error ${res.status}`);
  return body as T;
}

const post = <T,>(path: string, body: unknown) => call<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const getMeta = (c?: CaseRef) => (c && (c.templateId || c.state) ? post<Meta>("/api/meta", { case: c }) : call<Meta>("/api/meta"));
export const buildCase = (c: CaseRef) => post<CaseInfo>("/api/case/build", { case: c });
export const caseStatus = (c: CaseRef) => post<CaseInfo>("/api/case/status", { case: c });
export const recommend = (payload: Inputs) =>
  call<Result>("/api/recommend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const verifyExport = (id: string) => call<Verify>(`/api/verify/${id}`);
export const exportUrl = (id: string, file: "portfolio.json" | "dispatch.csv") => `/api/export/${id}/${file}`;

// ---- Year-by-year report (team_app/report.py)
export interface YearRow {
  year: number | "horizon"; requestedGwh: number | null; servedGwh: number | null; relocatedMwh: number | null; interruptedMwh: number | null;
  capacityM: number | null; contractM: number | null; gasM: number | null; merchantM: number | null; localM: number | null; relocationM: number | null; interruptionM: number | null; totalM: number | null;
  localUsdPerMwh: number | null; decisionUsdPerMwh: number | null; kgPerMwh: number | null; co2T: number | null; cleanSharePct: number | null; merchantSharePct: number | null;
}
export interface Report {
  start: number; fullMonth: number; years: number[]; rows: YearRow[];
  mix: Record<string, (number | null)[]>; costM: Record<string, (number | null)[]>; unit: Record<string, (number | null)[]>; unitRows: string[];
  names: Record<string, string>; colors: Record<string, string>; co2Target: number; sources: { mix: string[]; cost: string[] };
  images: { key: string; title: string; url: string }[]; zipUrl: string;
}
export const getReport = (id: string) => call<Report>(`/api/report/${id}`);
export const getSensitivity = (id: string) => call<Sensitivity>(`/api/sensitivity/${id}`);

// ---- One upload button: the developer input template (xlsx) or a stress-event / state file (csv, xlsx)
export const templateUrl = "/api/template";
export interface UploadResult {
  kind: "template" | "stress"; ok: boolean; errors?: string[]; warnings?: string[]; problems?: string[]; changed?: { parameter: string; packet: string; yours: string; status: string; note: string }[];
  ignored?: { parameter: string; value: string }[]; isBenchmark?: boolean; templateId?: string | null; goLive?: number | null; read?: { events: number; states: number }; catalog?: Catalog;
}
export const uploadFile = async (file: File): Promise<UploadResult> => call<UploadResult>("/api/upload", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-File-Name": file.name }, body: await file.arrayBuffer() });
