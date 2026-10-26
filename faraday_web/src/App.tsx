import { useEffect, useRef, useState } from "react";
import { buildCase, caseStatus, exportUrl, getMeta, getReport, getSensitivity, recommend, templateUrl, uploadFile, verifyExport } from "./api";
import type { CaseRef, CostBreakdown, Flag, Meta, Option, PortfolioRow, Power, Report, Result, Sensitivity, Stress, StressInfo, Tradeoffs, UploadResult, Verify, YearRow } from "./api";

const IMG = {
  hero: "/faraday-hero.png",
  wind: "/faraday-grid-map.jpg",
  solar: "/faraday-server-light.jpg",
  plant: "/faraday-transmission.jpg",
  battery: "/faraday-server-dark.jpg",
};

type Page = "overview" | "intelligence";

function Bolt() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13.4 1 4 13h6.7L9.6 23 20 9.2h-7.1L13.4 1Z" /></svg>;
}

function SectionHead({ label, number }: { label: string; number: string }) {
  return <div className="section-head"><span>{label}</span><span>{number}</span></div>;
}

function Sub({ children }: { children: React.ReactNode }) {
  return <p className="section-sub">{children}</p>;
}

function Count({ children }: { children: React.ReactNode }) {
  return <strong className="display count">{children}</strong>;
}

function App() {
  const [page, setPage] = useState<Page>("overview");
  return (
    <div className="app-shell">
      <header className="nav">
        <button className="brand" onClick={() => setPage("overview")} aria-label="Faraday home"><Bolt /><span>FARADAY</span></button>
        <nav aria-label="Main navigation">
          <button className={page === "overview" ? "active" : ""} onClick={() => setPage("overview")}>Overview</button>
          <button className={page === "intelligence" ? "active" : ""} onClick={() => setPage("intelligence")}>Power Intelligence</button>
        </nav>
      </header>
      <main>{page === "overview" ? <Overview openModel={() => setPage("intelligence")} /> : <Intelligence />}</main>
      <footer><div className="brand"><Bolt /><span>FARADAY</span></div><span>Power decisions for the grid-constrained era.</span><span>© 2025</span></footer>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Overview: problem, solution, methodology, data

const DATA_ROWS: [string, string, string, string][] = [
  ["Hourly wholesale power prices", "Public", "ERCOT LZ_SOUTH 2023, escalated +5%/yr real", "Grid purchases"],
  ["Wind and solar output profiles", "Public", "ERCOT 2023 generation and capacity", "Contract energy"],
  ["Grid-stress windows (7 events)", "Public", "ERCOT 2023 conservation notices (the 60% cap is assumed)", "Stress response"],
  ["Natural gas price", "Public", "EIA Henry Hub 2023, +3%/yr real, plus a $1 adder", "Gas cost"],
  ["Grid CO₂ factor", "Public", "EPA eGRID 2023, falling 3%/yr", "Carbon"],
  ["Turbine and battery cost, engine heat rate, battery efficiency", "Public", "EIA AEO2025, EPA CHP catalog, NREL ATB", "Equipment charges, fuel"],
  ["Campus demand, protected / relocatable / interruptible split, ramp", "Private (case)", "Customer packet: a fictional 75 MW campus", "Load"],
  ["Grid import schedule (0, 25, 50, 75 MW)", "Private (case)", "Utility schedule in the packet (fictional)", "Import cap"],
  ["Contract offers: solar $40, wind $35, nuclear $95 per MWh; sizes and dates", "Private (case)", "Commercial offers in the packet (fictional)", "Contract payments"],
  ["Engine and fuel-cell costs, equipment footprints, 8 usable acres", "Private (case)", "Packet assumptions (fictional)", "Equipment charges, land limit"],
  ["Remote-site terms, interruption consequence", "Private (case)", "Packet assumptions: $123.50/MWh to relocate, $1,000/MWh to interrupt", "Stress response"],
  ["Go-live month, carbon and cost limits, unavailable resources, priority weights", "User input", "Set in the tool, per customer", "Filter and rank"],
  ["Price scenario (bear / base / bull), grid delay", "User input", "Chosen by the user", "Sensitivity"],
  ["Land, peak demand, ramp, grid schedule, remote site, nuclear terms", "User input", "Developer input template (download and upload on the Power Intelligence page): values from the developer's own project plan", "Replace the case values"],
];

function Overview({ openModel }: { openModel: () => void }) {
  const solution = [["Understand the customer", "Turn business priorities into limits and weights the model can use."], ["Build the portfolios", "Combine grid, onsite and contracted energy into thousands of candidates."], ["Simulate every hour", "Test cost, carbon, reliability and maintenance across 87,600 hours."], ["Recommend and explain", "One recommendation, the alternatives, the year-by-year numbers and the trade-offs."]];
  const kinds = ["Public", "Private (case)", "User input"];
  return <>
    <section className="hero">
      <div className="hero-photo" style={{ backgroundImage: `url(${IMG.hero})` }} />
      <div className="hero-shade" />
      <div className="hero-content reveal">
        <p className="eyebrow">Faraday · power intelligence for data centers</p>
        <h1 className="display">FARADAY TURNS A POWER PROBLEM <em>INTO A</em> DECISION.</h1>
        <p className="lede">Helping data center developers with their energy portfolio decisions.</p>
        <div className="actions hero-actions"><button className="primary" onClick={openModel}>Open Power Intelligence <span>↗</span></button><button className="outline" onClick={() => document.querySelector("#problem")?.scrollIntoView({ behavior: "smooth" })}>How it works</button></div>
      </div>
    </section>
    <div className="ticker">
      <div className="ticker-track">
        {[0, 1].map(group => (
          <div className="ticker-group" key={group} aria-hidden={group === 1}>
            {["EIA", "ERCOT", "EPA eGRID", "NREL ATB", "EPA CHP CATALOG"].map(source => <span key={source}>{source}</span>)}
          </div>
        ))}
      </div>
    </div>

    <section className="section" id="problem">
      <SectionHead label="Problem statement" number="01" />
      <div className="split intro-grid">
        <div><h2 className="display">WHICH PORTFOLIO <em>MEETS THE DEMAND?</em></h2>
          <p className="lede dark">How can data center developers decide which energy portfolio will meet their demand requirements?</p>
          <p className="lede dark">The developer has to choose a mix of grid power, onsite generation, a battery and clean energy sources, while considering price, reliability and accounting for potential stress events.</p>
          <p className="lede dark">Developers face many constraints and market events. To decide well they need sensitivity analysis on prices, the response to stress events when the grid connection is cut, carbon information, and the cost and timing of every option side by side.</p>
</div>
        <SampleMix />
      </div>
    </section>

    <section className="section" id="solution">
      <SectionHead label="Our solution" number="02" />
      <Sub>The solution is an optimization product. It lets developers optimize three different priorities, and weigh them against each other: speed to power, cost and environmental impact (carbon emissions).</Sub>
      <div className="cards three priorities">
        {[["Speed to power", "How soon the campus can switch on and reach full demand.", IMG.hero], ["Cost", "The ten-year cost of equipment, contracts, fuel and grid power.", IMG.plant], ["Environmental", "Carbon emissions per MWh and the share of clean energy.", IMG.wind]].map(([x, y, im], i) => <article className="card process" key={x}><img src={im} alt="" /><span className="mono">0{i + 1}</span><h3>{x}</h3><p>{y}</p></article>)}
      </div>
      <div className="card extra-card"><span className="tag">SHARPER, MORE COMPREHENSIVE DECISIONS</span><p>To make the decision sharper and more comprehensive we have added <b>stress events</b>, the seven short periods a year when the grid connection is cut and every portfolio must keep protected load on, and <b>sensitivity analysis</b>, which re-runs each portfolio under bear, base and bull price scenarios.</p></div>
    </section>

    <section className="section" id="data">
      <SectionHead label="Data used" number="03" />
      <Sub>Three kinds of data feed the model. Public data is real and cited. Private (case) data is the commercial and campus information of this exercise, fictional but fixed. User input is what the person making the decision can change.</Sub>
      <div className="cards four data-kinds">
        {[["Public", "Real, published data prepared in the packet: prices, weather-driven output, emissions and technology costs."], ["Private (case)", "The campus and its commercial terms: load, utility schedule, contract offers, site and remote-site terms. Fictional in this exercise."], ["User input", "Limits, priority weights, price scenarios, and the developer's own project data through the input template."]].map(([k, v]) => <article className="card" key={k}><span className={`tag kind-${k.split(" ")[0].toLowerCase()}`}>{k.toUpperCase()}</span><p>{v}</p></article>)}
      </div>
      <div className="card table-card"><div className="table-scroll"><table className="year-table data-table"><thead><tr><th>Data</th><th>Type</th><th>Source</th><th>Where it enters</th></tr></thead>
        <tbody>{DATA_ROWS.map(([a, b, c, d]) => <tr key={a}><td>{a}</td><td><span className={`tag kind-${b.split(" ")[0].toLowerCase()}`}>{kinds.includes(b) ? b.toUpperCase() : b}</span></td><td>{c}</td><td>{d}</td></tr>)}</tbody></table></div></div>
      <p className="footnote">The campus is a fictional benchmark case. Public sources are cited; every assumption is listed on the Power Intelligence page.</p>
    </section>

    <section className="section"><div className="dark-card"><span className="tag">TRY THE TOOL</span><h2 className="display">READY TO PLAN<br/>THE CAMPUS?</h2><button className="light" onClick={openModel}>Open Power Intelligence <span>↗</span></button></div></section>
  </>;
}

const SAMPLE_YEARS = ["Y2", "Y3", "Y4", "Y5", "Y6", "Y7"];
const SAMPLE_MIX: [string, string, number[]][] = [
  ["wind", "Wind contract", [8, 20, 34, 38, 38, 38]],
  ["solar", "Solar contract", [0, 6, 14, 20, 20, 20]],
  ["gas_turbine", "Onsite gas", [14, 22, 18, 12, 10, 10]],
  ["merchant", "Grid power", [0, 4, 9, 5, 7, 7]],
];

function SampleMix() {
  const scale = 75;
  return <div className="card chart-card"><div className="chart-title"><span>Sample energy sources</span><span>MW, average per year</span></div>
    <div className="legend sample-legend">{SAMPLE_MIX.map(([k, n]) => <span key={k} className="lc" style={{ ["--c" as string]: RES_COLOR[k] }}>{n}</span>)}<span className="dash">Grid import cap</span></div>
    <div className="bars year-bars sample-bars"><div className="ylabels">{[1, 2 / 3, 1 / 3, 0].map(f => <span key={f}>{(scale * f).toFixed(0)}</span>)}</div>
      {SAMPLE_YEARS.map((y, j) => <div className="bar-col" key={y}><div className="stack">{SAMPLE_MIX.map(([k, n, v]) => <i key={k} style={{ height: `${v[j] / scale * 100}%`, background: RES_COLOR[k] }} title={`${n}: ${v[j]} MW`} />)}
        <b className="cap-mark" style={{ bottom: `${[0, 25, 50, 75, 75, 75][j] / scale * 100}%` }} /></div><small className="ytick">{y}</small></div>)}</div>
    <p className="axis-label">Illustrative sample, not model output · the engine builds this chart for the portfolio it recommends</p></div>;
}

// ------------------------------------------------------------------------------------------------ Power Intelligence
const BLURBS = ["Best overall balance", "Earliest possible service", "Lowest 10-year cost", "Lowest operational carbon"];
const PRESET_ORDER = ["Balanced", "Speed-first", "Cost-first", "Carbon-first"];
const SOURCE_CLASS: Record<string, string> = { "PACKET · PUBLIC DATA": "s0", "PACKET · CASE ASSUMPTION": "s1", "TEAM ASSUMPTION": "s2", "NOT MODELED": "s3" };
const FLAG_LABEL: Record<Flag["status"], string> = { "not-modeled": "NOT MODELED", partial: "PARTIAL", derived: "DERIVED", definition: "DEFINITION", scope: "SCOPE" };
const FLAG_CLASS: Record<Flag["status"], string> = { "not-modeled": "s3", partial: "s3", derived: "s2", definition: "s1", scope: "s0" };
const STRESS_COLOR: Record<string, string> = { Grid: "#2b2b2b", Gas: "#f7b48a", Battery: "#4fb3a9", Relocated: "#85c1e9", Interrupted: "#e5484d" };
const RES_COLOR: Record<string, string> = { nuclear: "#6a3d9a", wind: "#1f78b4", solar: "#f1c40f", battery: "#2ecc71", fuel_cell: "#f5b041", gas_engine: "#d35400", gas_turbine: "#873600", merchant: "#95a5a6" };
const RES_NAME: Record<string, string> = { nuclear: "Nuclear PPA", wind: "Wind contract", solar: "Solar contract", battery: "Battery discharge", fuel_cell: "Fuel cell", gas_engine: "Gas engine", gas_turbine: "Gas turbine", merchant: "Grid power (bought)" };
const RES_SELECTED_KEY: Record<string, string> = { nuclear: "nuclear", wind: "wind", solar: "utility_solar", battery: "battery_4h", fuel_cell: "fuel_cell", gas_engine: "gas_engine", gas_turbine: "gas_turbine", merchant: "" };
const SCEN_COLOR: Record<string, string> = { bear: "#27ae60", base: "#8a8a8a", bull: "#e67e22" };

const fmt = (v: number | null | undefined, d = 0) => (v == null ? "–" : v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));

const openFlags = () => { const d = document.getElementById("flags") as HTMLDetailsElement | null; if (d) d.open = true; };

function FlagPill({ label }: { label: string }) {
  return <a className="flag-pill" href="#flags" onClick={openFlags} title="An additional assumption or something the model does not fully produce. Opens the list under the chart.">{label}</a>;
}

function linkWeights(w: number[], i: number, v: number): number[] {
  const rest = 100 - v;
  const others = [0, 1, 2].filter(j => j !== i);
  const tot = w[others[0]] + w[others[1]];
  const out = [...w];
  out[i] = v;
  if (tot === 0) { out[others[0]] = Math.round(rest / 2); out[others[1]] = rest - out[others[0]]; }
  else { out[others[0]] = Math.round(rest * w[others[0]] / tot); out[others[1]] = rest - out[others[0]]; }
  return out;
}

function FlagList({ flags }: { flags: Flag[] }) {
  return <div className="assumption-list flags-list">{flags.map((f, i) => <div key={f.id}><span>{String(i + 1).padStart(2, "0")}</span><p><b>{f.area}</b><small>{f.message}</small></p><em className={`source ${FLAG_CLASS[f.status]}`}>{FLAG_LABEL[f.status]}</em></div>)}</div>;
}

function Fold({ title, hint, id, flags }: { title: string; hint: string; id?: string; flags: Flag[] }) {
  return <details className="fold" id={id}><summary><b>{title}</b><span>{hint}</span></summary><FlagList flags={flags} /></details>;
}

function AssumpFold({ items }: { items: Meta["assumptions"] }) {
  return <details className="fold" id="assumptions"><summary><b>Assumptions</b><span>the packet data and conventions behind the charts, with where each comes from</span></summary>
    <div className="assumption-list">{items.map((a, i) => <div key={a.name}><span>{String(i + 1).padStart(2, "0")}</span><p><b>{a.name}</b><small>{a.detail}</small></p><em className={`source ${SOURCE_CLASS[a.source] ?? "s3"}`}>{a.source}</em></div>)}</div></details>;
}

const openAssumptions = () => { const d = document.getElementById("assumptions") as HTMLDetailsElement | null; if (d) d.open = true; };

function Intelligence() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [persona, setPersona] = useState("Balanced");
  const [weights, setWeights] = useState<number[]>([34, 33, 33]);
  const [goLive, setGoLive] = useState<number | null>(null);
  const [carbonMax, setCarbonMax] = useState<number | null>(null);
  const [costPct, setCostPct] = useState<number | null>(null);
  const [excluded, setExcluded] = useState<string[]>([]);
  const [raw, setResult] = useState<Result | null>(null);
  const [lastOk, setLastOk] = useState<Result | null>(null);
  const result = raw?.status === "ok" ? raw : raw?.status === "needs_build" ? lastOk : null;   // while a case is being built the previous results stay on the page
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verify, setVerify] = useState<Verify | "running" | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [sens, setSens] = useState<Sensitivity | null>(null);
  const [view, setView] = useState<"power" | "stress" | "price">("power");
  const [backupOn, setBackupOn] = useState<string[]>([]);
  const [uploads, setUploads] = useState<(UploadResult & { name: string })[] | "reading">([]);
  const [caseRef, setCaseRef] = useState<CaseRef>({});
  const [tick, setTick] = useState(0);
  const firstMeta = useRef(true);
  const seq = useRef(0);

  const applyPreset = (m: Meta, name: string) => {
    const p = m.presets[name];
    if (!p) return;
    setPersona(name); setWeights(p.weights); setGoLive(p.goLive); setCarbonMax(p.carbonMax); setCostPct(p.costPct);
  };

  useEffect(() => {
    getMeta(caseRef).then(m => {
      setMeta(m); setMetaError(null);
      if (firstMeta.current) { firstMeta.current = false; applyPreset(m, "Balanced"); }
      setGoLive(g => (g != null && !m.choices.goLive.includes(g) ? null : g));
    }).catch(e => setMetaError(e.message));
  }, [caseRef]);

  useEffect(() => {
    if (!meta) return;
    const id = ++seq.current;
    setBusy(true);
    const t = setTimeout(() => {
      recommend({ weights, goLive, carbonMax, costPct, excluded, case: caseRef })
        .then(r => { if (id === seq.current) { setResult(r); if (r.status === "ok") setLastOk(r); setError(null); } })
        .catch(e => { if (id === seq.current) setError(e.message); })
        .finally(() => { if (id === seq.current) setBusy(false); });
    }, 200);
    return () => clearTimeout(t);
  }, [meta, weights, goLive, carbonMax, costPct, excluded, caseRef, tick]);

  const building = raw?.status === "needs_build" && raw.case?.build.status === "building";
  useEffect(() => {
    if (!building) return;
    const t = setInterval(() => { caseStatus(caseRef).then(c => { setResult(r => (r ? { ...r, case: c } : r)); if (c.ready) setTick(x => x + 1); }).catch(() => {}); }, 3000);
    return () => clearInterval(t);
  }, [building, caseRef]);

  useEffect(() => {
    const id = result?.exportId;
    if (!id) return;
    let live = true;
    setVerify("running"); setSens(null);
    getReport(id).then(r => { if (live) setReport(r); }).catch(() => { if (live) setReport(null); });
    getSensitivity(id).then(r => { if (live) setSens(r); }).catch(() => { if (live) setSens(null); });
    verifyExport(id).then(v => { if (live) setVerify(v); }).catch(e => { if (live) setVerify({ passed: false, error: e.message }); });
    return () => { live = false; };
  }, [result?.exportId]);

  const touch = () => setPersona("Custom");
  const toggleBackup = (name: string) => setBackupOn(b => b.includes(name) ? b.filter(x => x !== name) : [...b, name]);
  const toggleResource = (id: string) => { setExcluded(e => e.includes(id) ? e.filter(x => x !== id) : [...e, id]); touch(); };
  const onUpload = async (files: FileList | null) => {
    if (!files || !files.length) return;
    setUploads("reading");
    const done: (UploadResult & { name: string })[] = [];
    let next = { ...caseRef };
    for (const f of Array.from(files)) {
      try {
        const r = await uploadFile(f);
        done.push({ ...r, name: f.name });
        if (r.kind === "template" && r.ok && r.templateId) next = { ...next, templateId: r.templateId };
      } catch (e) { done.push({ kind: "stress", ok: false, problems: [(e as Error).message], name: f.name }); }
    }
    setUploads(done); setCaseRef(next);
  };
  const startBuild = () => { buildCase(caseRef).then(c => setResult(r => (r ? { ...r, case: c } : r))).catch(e => setError(e.message)); };
  const rec = result?.status === "ok" ? result.recommendation! : null;
  const peak = meta?.campusPeakMw ?? 75;

  if (metaError) {
    return <section className="section model-hero"><span className="tag orange">MODEL SERVER NOT CONNECTED</span>
      <h1 className="display">THE MODEL IS <em>NOT RUNNING.</em></h1>
      <p className="lede dark">{metaError}</p>
      <p className="footnote">This page shows no numbers of its own: every figure on it comes from the model. Start the server and reload.</p></section>;
  }
  if (!meta) return <section className="section model-hero"><span className="tag">LOADING THE MODEL</span><h1 className="display">STARTING <em>UP…</em></h1></section>;

  const backupFlags = meta.flags.filter(f => ["onsite-backup", "sens-backup", "battery-ride-through", "reliability-fixed"].includes(f.id));
  const recSens = sens?.rows.find(r => r.role === "Recommended");
  const backupPool = (recSens?.backup ?? []).filter(b => b.totalM > 0.05);
  const addFor = (row?: { backup: { name: string; totalM: number }[] }) => (row ? row.backup.filter(b => backupOn.includes(b.name)).reduce((a, b) => a + b.totalM, 0) : 0);

  return <div className="model-page">
    <div className={`progress ${busy ? "on" : ""}`} />
    <div className="quick"><span>JUMP TO</span>{[["01 Template", "inputs"], ["02 Requirements", "requirements"], ["03 Portfolio", "portfolio"], ["04 Reliability", "reliability"], ["05 Options", "options"], ["06 Year-by-year break-down", "yearly"]].map(([x, h]) => <a key={h} href={`#${h}`}>{x}</a>)}<a className="active" href="#final">07 Final recommendation</a></div>
    <section className="model-hero section">
      <span className="tag orange">FARADAY ENGINE</span>
      <h1 className="display">MAKE POWER DECISIONS BASED ON <em>YOUR INPUTS.</em></h1>
      <p className="lede dark">Set your goals, your requirements and your priorities, or upload your own project information. The engine searches thousands of portfolios, simulates every hour of the ten-year horizon and recommends one, with the alternatives, the year-by-year numbers and a checked export.</p>
      <div className="actions upload-actions">
        <label className="primary upload-btn">Upload your information<input type="file" multiple accept=".xlsx,.csv" onChange={e => { onUpload(e.target.files); e.target.value = ""; }} /></label>
        <a className="outline" href={templateUrl} download>Download the input template</a>
      </div>
      <p className="upload-note"><b>Optional.</b> Upload the filled developer input template (project data: go-live month, peak demand and ramp, grid schedule, land, remote site, nuclear terms) and/or a stress-event file such as <i>stress_events_all_interconnections_2023</i> with <i>states_by_interconnection</i>. The engine reads the state, finds its interconnection and re-runs every portfolio against that interconnection's stress events. Leave it out to use the packet's Texas benchmark.</p>
      <UploadCards u={uploads} close={() => setUploads([])} />
      {error && <p className="notice">{error}</p>}
    </section>

    <section className="section" id="inputs">
      <SectionHead label="Choose a template depending on the goal" number="01" />
      <Sub>Choose the template that matches the customer's goal (balanced, speed, cost or carbon), or calibrate the requirements and priorities yourself in the next section. A template fills in the limits and the priority weights; changing anything afterwards makes it a custom customer.</Sub>
      <div className="customer-grid">
        <div className="preset-grid">{PRESET_ORDER.filter(x => meta.presets[x]).map((x, i) => <button className={`preset ${persona === x ? "selected" : ""}`} onClick={() => applyPreset(meta, x)} key={x}><span>0{i + 1}</span><b>{x}</b><small>{BLURBS[i]}</small></button>)}</div>
        <div className="card customer-card"><span className="tag orange">TEMPLATE CUSTOMER</span><h2>{persona} customer</h2><p>A developer seeking a credible path from first energized building to full campus load.</p><dl><div><dt>Cares about</dt><dd>{persona === "Carbon-first" ? "Emissions, clean share" : persona === "Speed-first" ? "Speed to full demand" : persona === "Cost-first" ? "Lowest 10-year cost" : "Schedule certainty, economics"}</dd></div><div><dt>Sets</dt><dd>{meta.campusPeakMw.toFixed(0)} MW peak, {meta.tier0PeakMw.toFixed(0)} MW protected load</dd></div><div><dt>Expect</dt><dd>Auditable assumptions and one clear answer</dd></div></dl></div>
      </div>
    </section>

    <section className="section" id="requirements">
      <SectionHead label="Requirements & priorities" number="02" />
      <Sub>Requirements are hard limits: a portfolio that misses one is removed, and “Any” means no limit. Priorities are the weights that rank the portfolios that remain. Every recommendation is reliable by construction: the protected Tier 0 load is served in every hour and the dispatch passes the official checker.</Sub>
      <div className="cards three requirement-cards">
        <article className="card"><label>Speed to power</label><small className="hint">When the campus should go live: the latest month of first power</small>
          <select aria-label="Go-live month" value={goLive ?? ""} onChange={e => { setGoLive(e.target.value === "" ? null : +e.target.value); touch(); }}><option value="">Any month</option>{meta.choices.goLive.map(m => <option key={m} value={m}>Live by month {m} (full demand by month {m + 10})</option>)}</select></article>
        <article className="card"><label>Carbon limit</label><small className="hint">Highest average CO₂ per MWh over the ten years</small>
          <select aria-label="Carbon limit" value={carbonMax ?? ""} onChange={e => { setCarbonMax(e.target.value === "" ? null : +e.target.value); touch(); }}><option value="">No limit</option>{meta.choices.carbonMax.map(v => <option key={v} value={v}>At most {v} kg CO₂/MWh</option>)}</select></article>
        <article className="card"><label>Cost limit</label><small className="hint">How far above the cheapest option the cost may be</small>
          <select aria-label="Cost limit" value={costPct ?? ""} onChange={e => { setCostPct(e.target.value === "" ? null : +e.target.value); touch(); }}><option value="">No limit</option>{meta.choices.costPct.map(v => <option key={v} value={v}>Within {v}% of the cheapest</option>)}</select></article>
      </div>
      <LocationPick meta={meta} caseRef={caseRef} setCaseRef={setCaseRef} />
      {raw?.status === "needs_build" && raw.case && <BuildCard c={raw.case} start={startBuild} back={() => setCaseRef({})} hasPrev={!!lastOk} />}
      <div className="card avail-card"><div><label>What if a resource is not available?</label><small className="hint">Switch a resource off to see the best portfolio without it, for example wind blocked by regulation or no nuclear contract. Every portfolio that uses it is removed and the rest are re-ranked.</small></div>
        <div className="chips">{meta.choices.resources.map(r => <button key={r.id} className={`chip ${excluded.includes(r.id) ? "off" : "on"}`} onClick={() => toggleResource(r.id)} aria-pressed={!excluded.includes(r.id)}><i style={{ background: RES_COLOR[r.id === "utility_solar" ? "solar" : r.id === "battery_4h" ? "battery" : r.id] }} />{r.label}<em>{excluded.includes(r.id) ? "not available" : "available"}</em></button>)}</div></div>
      <div className="card sliders">{["Speed to power", "Cost", "Carbon"].map((x, i) => <label key={x}><span>{x}<b className="display">{weights[i]}%</b></span><input type="range" min="0" max="100" value={weights[i]} onChange={e => { setWeights(linkWeights(weights, i, +e.target.value)); touch(); }} /></label>)}</div>
      <div className="explain-row one"><p><b>Priority weights</b> say how much each goal counts when ranking the portfolios that pass your requirements. They always add up to 100%: raising one lowers the others, and 0% means that goal is ignored. <button className="link" onClick={() => { setWeights([34, 33, 33]); touch(); }}>Reset to an equal split</button></p></div>
    </section>

    <div className={busy ? "busy" : ""}>
        {raw?.status === "no_match" && <section className="section"><div className="dark-card"><span className="tag orange">NO PORTFOLIO MEETS EVERY LIMIT</span><h2 className="display">RELAX ONE OF THE <em>LIMITS.</em></h2><p>{(raw.relaxations ?? []).join(" ")}</p></div></section>}
    {rec && result && <div className={`results ${raw?.status === "needs_build" ? "stale" : ""}`} key={result.exportId}>
    <section className="section" id="portfolio">
      <SectionHead label="Portfolio recommendation" number="03" />
      <Sub>The recommended portfolio, what each resource contributes and what it costs. All seven resources the model can choose are listed, including the ones this portfolio does not use.</Sub>
      <div className="rec-grid">
        <div className="card rec-summary"><span className="tag">RECOMMENDED</span><h3>{rec.name}</h3><p className="detail">{rec.detail}</p>
          <dl className="kv"><div><dt>First power</dt><dd>Month {rec.opening}</dd></div><div><dt>Full demand</dt><dd>Month {rec.fullDemand}</dd></div><div><dt>10-year cost</dt><dd>${fmt(rec.kpis.costM, 1)}M</dd></div><div><dt>Cost per MWh</dt><dd>${fmt(rec.kpis.dollarsPerMwh)} local energy</dd></div>
            <div><dt>Carbon</dt><dd>{fmt(rec.kpis.kgPerMwh)} kg CO₂/MWh</dd></div><div><dt>Clean share</dt><dd>{fmt(rec.kpis.cleanSharePct)}%</dd></div><div><dt>Score</dt><dd>{rec.score.toFixed(0)}/100</dd></div></dl></div>
        <div className="card table-card flush"><h3 className="chart-h">Capacity by resource <small>all seven options, selected or not</small></h3><CapacityTable rows={result.portfolioTable!} /></div>
      </div>
      {result.tradeoffs && <TradeoffCard t={result.tradeoffs} />}
      <div className="card table-card"><h3 className="chart-h">Cost breakdown <small>10 years · constant 2023 USD · each part is paid differently</small></h3><CostTable c={result.costBreakdown!} /></div>
      <div className="card power-chart">
        <div className="chart-top"><h3 className="chart-h">{view === "power" ? "Power sources" : view === "stress" ? "Stress events" : "Price sensitivity"} <small>{view === "power" ? "average MW by resource, per model year" : view === "stress" ? "what the portfolio does when the grid connection is cut" : "variable costs only · each scenario re-runs the dispatch"}</small></h3>
          <div className="segment view-tabs" role="tablist"><button role="tab" aria-selected={view === "power"} className={view === "power" ? "on" : ""} onClick={() => setView("power")}>1 · Power sources</button><button role="tab" aria-selected={view === "stress"} className={view === "stress" ? "on" : ""} onClick={() => setView("stress")}>2 · Stress events</button><button role="tab" aria-selected={view === "price"} className={view === "price" ? "on" : ""} onClick={() => setView("price")}>3 · Price sensitivity</button></div></div>
        {view === "power"
          ? <PowerChart p={result.powerOverTime!} peak={peak} caps={rec.capacities} />
          : view === "stress" ? <StressView p={result.powerOverTime!} st={meta.stress} resp={meta.response} />
          : <PriceView s={sens} span={result.powerOverTime!} backupPool={backupPool} backupOn={backupOn} toggle={toggleBackup} add={addFor(recSens)} />}
        <AssumpFold items={meta.assumptions} />
        <Fold id="flags" title="Additional assumptions" hint="what this page adds on top of the packet's data, and what the model does not fully produce" flags={meta.flags} />
      </div>
    </section>
    <section className="section" id="reliability">
      <SectionHead label="Reliability & maintenance" number="04" />
      <Sub>Tier 0 is the protected load: it must stay served on site, always. The cards show how much onsite generation stands behind it, how long the battery could carry it alone, and the maintenance cost already inside the total.</Sub>
      <Reliability r={result.reliability!} backupOn={backupOn} toggle={toggleBackup} flags={backupFlags} resp={meta.response} />
    </section>
    <section className="section" id="options">
      <SectionHead label="Grid stress & options tested" number="05" />
      <Sub>{meta.stress.count === 7 ? "Seven" : meta.stress.count} times a year ({meta.stress.minHours === meta.stress.maxHours ? meta.stress.minHours.toFixed(0) : `${meta.stress.minHours.toFixed(1).replace(".0", "")}–${meta.stress.maxHours.toFixed(0)}`} hours each, in {meta.stress.months.join(" and ")}) the grid connection is cut to {meta.stress.importPct.toFixed(0)}% of its limit ({meta.stress.area}). Tier 1 work can move to a remote site, Tier 2 work can be paused, Tier 0 can never be touched. The options below were tested against the same hours, and against the three price scenarios.</Sub>
      <StressSummary st={meta.stress} />
      <div className="options-grid"><StressBar s={result.stress!} covered={result.reliability!.tier0ServedAlways} st={meta.stress} campus={peak} /><OptionsCard options={result.options!} sens={sens} backupOn={backupOn} /></div>
    </section>
    <section className="section" id="yearly">
      <SectionHead label="Year-by-year break-down" number="06" />
      <Sub>The same recommendation, one model year at a time: where the energy comes from, who is paid, carbon and clean share. Same accounting as the official checker, so these rows add up to the headline numbers.</Sub>
      {report ? <YearByYear r={report} /> : <p className="footnote">Building the year-by-year report…</p>}
    </section></div>}
    </div>
    <div className={busy ? "busy" : ""}>
    {rec && result && <section className="section" id="final">
      <SectionHead label="Final recommendation" number="07" />
      <Sub>The portfolio to build, with its headline numbers and the notes for this run, and one export for the submission.</Sub>
      <div className="dark-card recommendation" key={rec.name + rec.opening + rec.score}><span className="tag orange">FINAL RECOMMENDATION</span>
        <h2 className="display one-line">BUILD THE {rec.name.toUpperCase()}: FIRST POWER M{rec.opening}, FULL {peak.toFixed(0)} MW M{rec.fullDemand}</h2>
        <div className="kpis">{[[`$${rec.kpis.costM.toFixed(0)}M`, "10-yr cost"], [`$${rec.kpis.maintenanceM.toFixed(0)}M`, "maintenance"], [`$${rec.kpis.dollarsPerMwh.toFixed(0)}`, "$/MWh"], [`${rec.kpis.kgPerMwh.toFixed(0)}`, "kg CO₂/MWh"], [`${rec.kpis.cleanSharePct.toFixed(0)}%`, "clean share"], [`${rec.kpis.onsiteBackupMw.toFixed(0)} MW`, "onsite backup"], [`${rec.kpis.servedTwh.toFixed(1)} TWh`, "energy served"], [`${rec.kpis.interruptedMwh.toFixed(0)}`, "MWh interrupted"]].map(([a, b]) => <div key={b}><Count>{a}</Count><span>{b}</span></div>)}</div>
        <div className="final-grid"><div className="score-block"><Count>{rec.score.toFixed(0)}/100</Count><span>weighted score of the one recommended, on your priorities</span></div><div><span className="tag">SELECTED CAPACITY</span><h3>{rec.capacityText}</h3><p>{rec.detail}</p></div><div><span className="tag">WHY THIS PORTFOLIO</span><h3>{rec.why}</h3></div></div>
        <div className="next"><span>NEXT STEP</span><b>{rec.nextStep}</b></div>
        {result.notes.length > 0 && <div className="run-notes"><span className="tag">NOTES FOR THIS RUN</span>{result.notes.map(n => <p key={n}>{n}</p>)}</div>}</div>
      <div className="card export-card"><span className="tag">EXPORT</span>
        <div className="export-row"><a className="export-link" href={exportUrl(result.exportId!, "portfolio.json")}>portfolio.json ↓</a><a className="export-link" href={exportUrl(result.exportId!, "dispatch.csv")}>dispatch.csv ↓</a>{report && <a className="export-link" href={report.zipUrl}>Report (zip) ↓</a>}</div>
        <p className="footnote">{verify === "running" || verify == null ? "Running the official checker…" : verify.passed ? `Passed the official checker · total decision cost $${verify.totalDecisionCostM?.toFixed(1)}M · ${verify.kgPerMwh?.toFixed(0)} kg CO₂/MWh · full demand month ${verify.fullDemandMonth}.` : `The checker rejected this export: ${verify.error}`}</p></div>
    </section>}
    </div>
  </div>;
}

function UploadCards({ u, close }: { u: (UploadResult & { name: string })[] | "reading"; close: () => void }) {
  if (u === "reading") return <div className="card upload-card"><b>Reading your files…</b></div>;
  if (!u.length) return null;
  return <div className="card upload-card"><div className="upload-head"><span className="tag">YOUR FILES</span><button className="link" onClick={close}>Close</button></div>
    {u.map(r => <div className="upload-item" key={r.name}><p><b>{r.name}</b> · {r.kind === "template" ? "developer input template" : "stress events / states"} · <span className={r.ok ? "okc" : "err"}>{r.ok ? "read" : "problem"}</span></p>
      {[...(r.errors ?? []), ...(r.kind === "stress" && !r.ok ? r.problems ?? [] : [])].map(e => <p className="err" key={e}>{e}</p>)}
      {r.kind === "template" && r.ok && <p>{r.isBenchmark ? "Every value matches the packet's benchmark case." : `${r.changed?.length ?? 0} value${r.changed?.length === 1 ? "" : "s"} differ from the benchmark and are used by the engine (a portfolio search for the case runs once, below).`}</p>}
      {r.kind === "template" && (r.changed?.length ?? 0) > 0 && <div className="table-scroll"><table className="year-table"><thead><tr><th>Input</th><th>Packet</th><th>Yours</th><th>Note</th></tr></thead><tbody>{r.changed!.map(c => <tr key={c.parameter}><td>{c.parameter}</td><td>{c.packet}</td><td><b>{c.yours}</b></td><td className="note">{c.note}</td></tr>)}</tbody></table></div>}
      {r.kind === "stress" && r.ok && <p>Read {r.read?.events ? `${r.read.events} stress events` : ""}{r.read?.events && r.read?.states ? " and " : ""}{r.read?.states ? `${r.read.states} state rows` : ""}. Pick the campus state below.</p>}
      {(r.warnings ?? []).map(w => <p className="footnote" key={w}>{w}</p>)}
      {(r.ignored?.length ?? 0) > 0 && <p className="footnote">Not applied (the model needs the packet's tier formula): {r.ignored!.map(i => i.parameter).join(", ")}.</p>}</div>)}
  </div>;
}

function LocationPick({ meta, caseRef, setCaseRef }: { meta: Meta; caseRef: CaseRef; setCaseRef: (c: CaseRef) => void }) {
  const states = meta.catalog.states;
  const cur = states.find(x => x.state === (caseRef.state ?? "Texas"));
  const areaSel = caseRef.area ?? cur?.areas[0].area;
  return <div className="card loc-card"><div className="loc-top"><div><label className="loc-title">Where is the campus?</label>
    <p className="hint">The campus state sets the interconnection, and the interconnection sets which grid-stress events every portfolio has to ride through (shown under Grid stress &amp; options tested). Texas is the packet's benchmark. Upload a stress-event file at the top of the page to add more.</p></div>
    <div className="loc-pick"><label>State<select value={caseRef.state ?? "Texas"} onChange={e => setCaseRef({ ...caseRef, state: e.target.value === "Texas" && !caseRef.state ? null : e.target.value, area: null })}>{states.map(x => <option key={x.state} value={x.state} disabled={!x.areas.some(a => a.hasEvents)}>{x.state}{x.areas.some(a => a.hasEvents) ? "" : " (no events in the file)"}</option>)}</select></label>
      {cur && cur.areas.filter(a => a.hasEvents).length > 1 && <label>Interconnection<select value={areaSel} onChange={e => setCaseRef({ ...caseRef, state: caseRef.state ?? "Texas", area: e.target.value })}>{cur.areas.filter(a => a.hasEvents).map(a => <option key={a.area} value={a.area}>{a.area} · {a.coverage}</option>)}</select></label>}</div></div></div>;
}

function StressSummary({ st }: { st: StressInfo }) {
  return <div className="card loc-card stress-summary"><span className="tag">STRESS EVENTS FOR YOUR STATE</span>
    <p className="loc-line"><b>{st.area}</b> · {st.count} events a year, {st.minHours === st.maxHours ? `${st.minHours.toFixed(0)}` : `${st.minHours.toFixed(1).replace(".0", "")}–${st.maxHours.toFixed(0)}`} hours each, {st.months.join(", ")} · grid connection cut to {st.importPct.toFixed(0)}% of its limit · {st.source === "bundled" ? "bundled 2023 notices" : st.source}</p>
    <details className="fold"><summary><b>Show the {st.count} stress events</b><span>date, operator, length and how far the import limit drops</span></summary>
      <div className="table-scroll"><table className="year-table"><thead><tr><th>Event (UTC start)</th><th>Operator</th><th>Type</th><th>Hours</th><th>Import limit</th><th>Notice</th><th>Timing</th></tr></thead>
        <tbody>{st.events.map(e => <tr key={e.id + e.startUtc}><td>{e.startUtc}</td><td>{e.operator}</td><td className="note">{e.type}</td><td>{e.hours.toFixed(1).replace(".0", "")}</td><td>{e.importPct.toFixed(0)}%</td><td>{e.noticeMin.toFixed(0)} min</td><td>{e.confidence}</td></tr>)}</tbody></table></div></details></div>;
}

function BuildCard({ c, start, back, hasPrev }: { c: NonNullable<Result["case"]>; start: () => void; back: () => void; hasPrev: boolean }) {
  const b = c.build;
  return <div className="dark-card build-card"><span className="tag orange">{b.status === "building" ? "SEARCHING PORTFOLIOS FOR YOUR CASE" : "YOUR CASE NEEDS ITS OWN PORTFOLIO SEARCH"}</span>
    <h2 className="display one-line">{b.status === "building" ? "THE ENGINE IS WORKING ON IT" : "ONE SEARCH, THEN IT IS INSTANT"}</h2>
    <p>The packet's precomputed portfolios were scored against the Texas benchmark. Your inputs change the hours every portfolio is tested on, so the engine searches and simulates portfolios for each go-live month ({c.starts.map(m => `M${m}`).join(", ")}) against your case. It takes several minutes, runs offline, and is remembered afterwards.{hasPrev ? " The results below are the previous case's, shown dimmed until this one is ready." : ""}</p>
    <div className="build-actions">{b.status === "building" ? <p className="build-msg"><i className="spin" />{b.message || "Working…"} · {Math.max(0, Math.round(b.seconds))} s</p> : <button className="light" onClick={start}>Search portfolios for my case ↗</button>}
      <button className="light ghost" onClick={back}>Back to the Texas benchmark</button></div>
    {b.error && <p className="err">{b.error}</p>}</div>;
}

function TradeoffCard({ t }: { t: Tradeoffs }) {
  const r = t.recommended, a = t.anchors;
  const pct = (x: number, base: number) => (base ? (x / base - 1) * 100 : 0);
  const dCost = r.costM - a.cheapest.costM, dCostP = pct(r.costM, a.cheapest.costM);
  const dKg = r.kgPerMwh - a.cleanest.kgPerMwh, dKgP = pct(r.kgPerMwh, a.cleanest.kgPerMwh);
  const dMo = r.fullDemand - a.fastest.fullDemand;
  const costTxt = dCost < 0.5 ? "matches the cheapest portfolio" : `$${dCost.toFixed(0)}M (+${dCostP.toFixed(0)}%) more than the cheapest`;
  const kgTxt = dKg < 0.5 ? "matches the cleanest portfolio" : `${dKg.toFixed(0)} kg CO₂/MWh (+${dKgP.toFixed(0)}%) more carbon than the cleanest`;
  const moTxt = dMo <= 0 ? "as fast as the fastest" : `${dMo} month${dMo === 1 ? "" : "s"} later to full demand than the fastest`;
  const lead = { cost: "Cost comes first.", speed: "Speed to power comes first.", carbon: "Carbon comes first.", balanced: "Your priorities are balanced." }[t.top];
  const gives = t.top === "cost" ? `It is ${costTxt}, but it emits ${kgTxt.replace(" more carbon than the cleanest", " more than the cleanest")} and is ${moTxt}.`
    : t.top === "speed" ? `Full demand arrives ${moTxt.replace(" later to full demand than the fastest", " after the fastest").replace("as fast as the fastest", "with the fastest")}; the price is ${costTxt} and ${kgTxt}.`
    : t.top === "carbon" ? `It emits ${dKg < 0.5 ? "as little as the cleanest portfolio" : `${dKg.toFixed(0)} kg CO₂/MWh more than the cleanest (${r.kgPerMwh.toFixed(0)} vs ${a.cleanest.kgPerMwh.toFixed(0)})`}; the price is ${costTxt} and it is ${moTxt}.`
    : `Against the best of each goal it is ${costTxt}, carries ${kgTxt}, and is ${moTxt}.`;
  const rows: [string, Tradeoff2, boolean][] = [["Recommended", r, true], ["Cheapest portfolio", a.cheapest, false], ["Fastest portfolio", a.fastest, false], ["Cleanest portfolio", a.cleanest, false]];
  const d = (v: number, unit: string, dig = 0) => (Math.abs(v) < 0.5 * Math.pow(10, -dig) ? "best" : `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(dig)}${unit}`);
  return <div className="card tradeoff-card"><h3 className="chart-h">Trade-offs of your priorities <small>what the recommendation gives up against the best portfolio on each goal</small></h3>
    <p className="tradeoff-text"><b>{lead}</b> {gives}</p>
    <div className="table-scroll"><table className="year-table"><thead><tr><th>Portfolio</th><th>Full demand</th><th>10-yr cost</th><th>vs cheapest</th><th>kg CO₂/MWh</th><th>vs cleanest</th><th>Clean share</th><th>Mix</th></tr></thead>
      <tbody>{rows.map(([n, x, me]) => <tr key={n} className={me ? "total" : ""}><td>{n}</td><td>M{x.fullDemand} <small className="delta">{d(x.fullDemand - a.fastest.fullDemand, " mo")}</small></td><td>${x.costM.toFixed(0)}M</td><td className={x.costM - a.cheapest.costM > 0.5 ? "bad" : "good"}>{d(x.costM - a.cheapest.costM, "M")}</td><td>{x.kgPerMwh.toFixed(0)}</td><td className={x.kgPerMwh - a.cleanest.kgPerMwh > 0.5 ? "bad" : "good"}>{d(x.kgPerMwh - a.cleanest.kgPerMwh, " kg")}</td><td>{x.cleanSharePct.toFixed(0)}%</td><td className="note">{x.detail}</td></tr>)}</tbody></table></div>
    <p className="footnote">Best and worst are taken over every portfolio tested, ignoring your limits, so the gaps show what the weights and limits you chose cost on the other two goals. Change a weight above and these numbers move.</p></div>;
}
type Tradeoff2 = Tradeoffs["recommended"];

const evLabel = (id: string) => { const m = /(\d{4})(\d{2})(\d{2})/.exec(id); return m ? `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+m[2] - 1]} ${+m[3]}` : id; };

function StressView({ p, st, resp }: { p: Power; st: StressInfo; resp: Meta["response"] }) {
  const sx = p.stress;
  const tot = p.years.map((_, i) => sx.relocationM[i] + sx.interruptionM[i]);
  const scale = Math.max(...tot, 0.01) * 1.2;
  const evHours = sx.eventHours.reduce((a, b) => a + b, 0);
  const money = (v: number) => (scale < 1 ? `$${(v * 1000).toFixed(0)}K` : `$${v.toFixed(scale < 10 ? 2 : 1)}M`);
  return <>
    <div className="legend"><span className="lc" style={{ ["--c" as string]: STRESS_COLOR.Relocated }}>Tier 1 relocated (${resp.relocationUsdPerMwh.toFixed(2)}/MWh)</span><span className="lc" style={{ ["--c" as string]: STRESS_COLOR.Interrupted }}>Tier 2 interrupted (${resp.interruptionUsdPerMwh.toLocaleString()}/MWh)</span></div>
    <div className="bars year-bars"><div className="ylabels">{[1, 2 / 3, 1 / 3, 0].map(f => <span key={f}>{money(scale * f)}</span>)}</div>
      {p.years.map((y, j) => <div className="bar-col" key={y}><div className="stack">
        {sx.relocationM[j] > 0.0005 && <i style={{ height: `${sx.relocationM[j] / scale * 100}%`, background: STRESS_COLOR.Relocated }} title={`Relocated ${sx.relocatedMwh[j].toFixed(0)} MWh · ${money(sx.relocationM[j])}`} />}
        {sx.interruptionM[j] > 0.0005 && <i style={{ height: `${sx.interruptionM[j] / scale * 100}%`, background: STRESS_COLOR.Interrupted }} title={`Interrupted ${sx.interruptedMwh[j].toFixed(0)} MWh · ${money(sx.interruptionM[j])}`} />}
        <b className="bar-top" style={{ bottom: `calc(${tot[j] / scale * 100}% + 4px)` }}>{tot[j] > 0.0005 ? money(tot[j]) : "$0"}</b></div><YearTick y={y} span={`${sx.eventHours[j]} event h`} /></div>)}</div>
    <p className="axis-label">Cost of responding to stress events per model year · {st.count} {st.area} events a year · the grid connection is cut to {st.importPct.toFixed(0)}% of its limit</p>
    <div className="table-scroll"><table className="year-table"><thead><tr><th>Event in model year {sx.eventYear}</th><th>Hours</th><th>Campus demand</th><th>Grid cap</th><th>Grid + contracts</th><th>Onsite gas</th><th>Relocated MWh</th><th>Interrupted MWh</th></tr></thead>
      <tbody>{sx.events.map(e => <tr key={e.id}><td>{evLabel(e.id)}</td><td>{e.hours}</td><td>{e.demandMw.toFixed(0)} MW</td><td>{e.gridCapMw.toFixed(0)} MW</td><td>{e.gridMw.toFixed(0)} MW</td><td>{e.gasMw.toFixed(0)} MW</td><td>{fmt(e.relocatedMwh)}</td><td>{fmt(e.interruptedMwh)}</td></tr>)}</tbody></table></div>
    <p className="footnote">{evHours === 0 ? "No stress event overlaps an operating hour." : `Over the ten years ${evHours} operating hours fall inside a stress event. `}{tot.every(v => v < 0.0005) ? "This portfolio rides through every event without moving or pausing any work, so the response costs nothing." : "Relocation and interruption are the only levers in an event; Tier 0 is always served."}</p></>;
}

function CapacityTable({ rows }: { rows: PortfolioRow[] }) {
  return <div className="table-scroll"><table className="year-table cap-table"><thead><tr><th>Resource</th><th>Where</th><th>Selected</th><th>Offer max</th><th>Available from</th><th>Energy, 10 yr</th><th>Share of local energy</th><th>Capacity factor</th><th>Price / fixed charge</th></tr></thead>
    <tbody>{rows.map(r => <tr key={r.id} className={r.selected ? "" : "off"}><td><span className="swatch" style={{ background: RES_COLOR[r.id === "utility_solar" ? "solar" : r.id === "battery_4h" ? "battery" : r.id] }} />{r.label}</td><td>{r.location}</td><td><b>{r.selected ? `${fmt(r.mw)} MW` : "not selected"}</b></td><td>{fmt(r.maxMw)} MW</td><td>M{r.selected ? r.fromMonth : r.availableMonth}</td>
      <td>{r.selected ? `${fmt(r.energyGwh)} GWh` : "–"}</td><td>{r.sharePct == null ? "–" : `${fmt(r.sharePct, 1)}%`}</td><td>{r.capacityFactorPct == null ? "–" : `${fmt(r.capacityFactorPct)}%`}</td>
      <td>{r.priceUsdPerMwh != null ? `$${fmt(r.priceUsdPerMwh)}/MWh` : r.fixedChargeMPerYear != null ? `$${fmt(r.fixedChargeMPerYear, 2)}M/yr` : "–"}</td></tr>)}</tbody></table></div>;
}

function CostTable({ c }: { c: CostBreakdown }) {
  return <div className="table-scroll"><table className="year-table cost-table"><thead><tr><th>Cost item</th><th>10-year $M</th><th>Share</th><th>How it is paid</th></tr></thead>
    <tbody>{c.rows.map(r => <tr key={r.label}><td>{r.label}</td><td>{fmt(r.valueM, 1)}</td><td>{fmt(r.sharePct, 1)}%</td><td className="note">{r.note}</td></tr>)}
      <tr className="total"><td>Total decision cost</td><td>{fmt(c.totalM, 1)}</td><td>100%</td><td className="note">Local energy cost + relocation + interruption</td></tr>
      <tr className="memo"><td>of which maintenance (fixed + variable O&amp;M)</td><td>{fmt(c.maintenanceM, 1)}</td><td>{fmt(c.maintenanceM / c.totalM * 100, 1)}%</td><td className="note">Fixed ${fmt(c.maintenanceFixedM, 1)}M + variable ${fmt(c.maintenanceVariableM, 1)}M, already inside the lines above</td></tr></tbody></table></div>;
}

function YearTick({ y, span }: { y: number; span?: string }) {
  return <small className="ytick">Y{y}{span && <em>{span}</em>}</small>;
}

function PowerChart({ p, peak, caps }: { p: Power; peak: number; caps: Record<string, number> }) {
  const order = ["nuclear", "wind", "solar", "battery", "fuel_cell", "gas_engine", "gas_turbine", "merchant"] as const;
  const selected = (k: string) => k === "merchant" ? p.series.merchant.some(v => v > 0.05) : (caps[RES_SELECTED_KEY[k]] ?? 0) > 0;
  const scale = Math.max(peak, ...p.series.demand);
  const shed = Math.max(...p.series.relocated, ...p.series.interrupted);
  return <>
    <div className="legend">{order.map(k => <span key={k} className={`lc ${selected(k) ? "" : "unused"}`} style={{ ["--c" as string]: RES_COLOR[k] }}>{RES_NAME[k]}{selected(k) ? "" : " · not selected"}</span>)}<span className="dash">Grid import cap</span></div>
    <div className="bars year-bars"><div className="ylabels">{[1, 2 / 3, 1 / 3, 0].map(f => <span key={f}>{(scale * f).toFixed(0)}</span>)}</div>
      {p.years.map((y, j) => <div className="bar-col" key={y}><div className="stack">{order.map(k => p.series[k][j] > 0.05 ? <i key={k} style={{ height: `${p.series[k][j] / scale * 100}%`, background: RES_COLOR[k] }} title={`${RES_NAME[k]}: ${p.series[k][j].toFixed(1)} MW`} /> : null)}<b className="cap-mark" style={{ bottom: `${p.series.gridCap[j] / scale * 100}%` }} /></div><YearTick y={y} span={p.span[j]} /></div>)}</div>
    <p className="axis-label">MW supplied, average over each model year · dashed mark = grid import cap · resources not selected stay in the legend</p>
    <p className="footnote">The grid cap steps from 0 to 25, 50 and 75 MW; until it reaches the campus load the gap is filled by onsite gas, which is why gas is largest early on. The first year averages in the months before the campus opens.{shed > 0.05 ? ` Relocated or interrupted work is demand removed in stress events, not supply, so it is not stacked (up to ${Math.max(...p.series.relocated).toFixed(1)} MW relocated and ${Math.max(...p.series.interrupted).toFixed(1)} MW interrupted on average in a year).` : ""}</p></>;
}

function PriceView({ s, span, backupPool, backupOn, toggle, add }: { s: Sensitivity | null; span: Power; backupPool: { name: string; totalM: number }[]; backupOn: string[]; toggle: (n: string) => void; add: number }) {
  const y = s?.yearly;
  if (!s || !y) return <p className="footnote">Running the three price scenarios…</p>;
  const per = add / y.years.length;
  const totals = (k: string, i: number) => y.series[k][i] + per;
  const maxV = Math.max(...["bear", "base", "bull"].flatMap(k => y.years.map((_, i) => totals(k, i)))) * 1.15;
  const spanOf = (yr: number) => span.span[span.years.indexOf(yr)];
  const me = s.rows.find(r => r.role === "Recommended");
  return <>
    <div className="scen-defs">{s.scenarios.map(x => <div key={x.key}><i style={{ background: SCEN_COLOR[x.key] }} /><b>{x.label}</b><span>{x.text}</span></div>)}</div>
    <div className="backup-toggles"><span className="mini-h">Add reliability and backup generation to the price <small>click a box to add its cost to every scenario</small></span>
      <div className="chips">{backupPool.length === 0 && <small>No backup is needed for this portfolio.</small>}{backupPool.map(b => <button key={b.name} className={`chip box ${backupOn.includes(b.name) ? "on" : "off"}`} onClick={() => toggle(b.name)} aria-pressed={backupOn.includes(b.name)}><u>{backupOn.includes(b.name) ? "✓" : ""}</u>{b.name}<em>+${fmt(b.totalM, 1)}M</em></button>)}</div></div>
    <div className="bars year-bars grouped"><div className="ylabels">{[1, 2 / 3, 1 / 3, 0].map(f => <span key={f}>{(maxV * f).toFixed(0)}</span>)}</div>
      {y.years.map((yr, i) => <div className="bar-col" key={yr}><div className="group">{["bear", "base", "bull"].map(k => <div className="gb" key={k} title={`${k}: $${totals(k, i).toFixed(1)}M${per > 0 ? ` (incl. $${per.toFixed(1)}M backup)` : ""}`}><i className="base" style={{ height: `${y.series[k][i] / maxV * 100}%`, background: SCEN_COLOR[k] }} />{per > 0 && <i className="add" style={{ height: `${per / maxV * 100}%` }} />}</div>)}</div><YearTick y={yr} span={spanOf(yr)} /></div>)}</div>
    <p className="axis-label">Total cost per model year, $M · {s.scenarios.map(x => x.label).join(" / ")}{per > 0 ? " · hatched = added backup" : ""}</p>
    <p className="footnote">{me && `The recommended portfolio moves ${me.deltaPct.bear.toFixed(1)}% in the bear case and +${me.deltaPct.bull.toFixed(1)}% in the bull case. `}{s.note}{add > 0 ? ` Added backup: $${add.toFixed(1)}M over the years with demand (our extension, diesel costs team-assumed).` : ""}</p></>;
}

function Reliability({ r, backupOn, toggle, flags, resp }: { r: NonNullable<Result["reliability"]>; backupOn: string[]; toggle: (n: string) => void; flags: Flag[]; resp: Meta["response"] }) {
  const scale = Math.max(r.onsiteBackupMw, r.tier0PeakMw) * 1.15;
  const cards: [string, string, string | null][] = [
    [`${r.onsiteBackupMw.toFixed(0)} MW`, "onsite backup · generators + battery", "partial"],
    [`${r.batteryMwh.toFixed(0)} MWh`, r.batteryMw > 0 ? `battery ride-through · ≈${r.rideThroughMinutes.toFixed(0)} min` : "no battery selected · 0 min ride-through", "partial"],
    [`$${r.maintenanceM.toFixed(1)}M`, "maintenance · 10 years (fixed + variable O&M)", null],
    [`${r.stressHoursNoInterruptionPct.toFixed(0)}%`, "grid-stress hours with no interruption", "partial"],
  ];
  return <>
    <div className="cards four stat-cards">{cards.map(([a, b, flag]) => <article className="card" key={b}><Count>{a}</Count><span>{b}</span>{flag && <FlagPill label={flag} />}</article>)}</div>
    <div className="card tier-card"><div className="tier-head"><b>Onsite generation against the protected (Tier 0) load</b><span>MW</span></div>
      <div className="tier-bar"><i style={{ width: `${Math.min(100, r.onsiteBackupMw / scale * 100)}%` }}><em>{r.onsiteBackupMw.toFixed(0)} MW installed</em></i><u className="tier-mark" style={{ left: `${r.tier0PeakMw / scale * 100}%` }}><span>Tier 0 peak {r.tier0PeakMw.toFixed(0)} MW</span></u></div>
      <div className="tier-scale"><span>0</span><span>{scale.toFixed(0)}</span></div>
      <div className="tier-legend"><div><i style={{ background: "#2b2b2b" }} /><b>Tier 0 · protected</b><span>Must stay served on site in every hour. Never moved or paused.</span></div>
        <div><i style={{ background: "#85c1e9" }} /><b>Tier 1 · relocatable</b><span>Can move to a remote site during an event, up to {resp.remoteMw.toFixed(1)} MW, at ${resp.relocationUsdPerMwh.toFixed(2)} per MWh moved.</span></div>
        <div><i style={{ background: "#e5484d" }} /><b>Tier 2 · interruptible</b><span>Can be paused during an event, at ${resp.interruptionUsdPerMwh.toLocaleString()} per MWh not served.</span></div></div></div>
    <div className="card table-card"><h3 className="chart-h">Backup beyond the portfolio <small>extension · outside the packet's cost · tick a row to add its price to the sensitivity chart and the options table</small></h3>
      <p className="footnote top">With the largest unit out, firm onsite capacity is {r.backup.firmN1Mw.toFixed(0)} MW against {r.backup.criticalMw.toFixed(1)} MW of protected load{r.backup.shortfallMw > 0.05 ? `: a ${r.backup.shortfallMw.toFixed(1)} MW gap.` : ": covered."}</p>
      <div className="table-scroll"><table className="year-table backup-table"><thead><tr><th>Add to price</th><th>Backup option</th><th>Added MW</th><th>Total to month 120, $M</th><th>Note</th></tr></thead><tbody>{r.backup.options.map(o => <tr key={o.name} className={`pick ${backupOn.includes(o.name) ? "picked" : ""}`} onClick={() => o.totalM > 0.05 && toggle(o.name)}><td>{o.totalM > 0.05 ? <span className={`tick ${backupOn.includes(o.name) ? "on" : ""}`}>{backupOn.includes(o.name) ? "✓" : ""}</span> : <small>no cost</small>}</td><td>{o.name}</td><td>{fmt(o.addedMw, 1)}</td><td>{fmt(o.totalM, 1)}</td><td className="note">{o.note}</td></tr>)}</tbody></table></div>
      <Fold title="Assumptions" hint="what the backup numbers rest on" flags={flags} /></div>
  </>;
}

function StressBar({ s, covered, st, campus }: { s: Stress; covered: boolean; st: StressInfo; campus: number }) {
  const segs: [string, number, string][] = ([["Grid", s.gridMw, `Imports through the connection, capped at ${st.importPct.toFixed(0)}% in an event`], ["Gas", s.gasMw, "Onsite generation"], ["Battery", s.batteryMw, "Discharging"], ["Relocated", s.relocatedMw, "Tier 1 work moved to the remote site"], ["Interrupted", s.interruptedMw, "Tier 2 work paused"]] as [string, number, string][]).filter(([, v]) => v > 0.05);
  const total = segs.reduce((a, [, v]) => a + v, 0) || 1;
  return <div className="card stress"><span className="tag">BUSIEST STRESS HOUR · {s.hour}</span><h3>{s.demandMw.toFixed(0)} MW demand{covered ? ", protected load fully served" : ", protected load NOT fully served"}</h3>
    <div className="stress-track">{segs.map(([name, v]) => <i key={name} style={{ width: `${v / total * 100}%`, background: STRESS_COLOR[name] }} />)}</div>
    <div className="stress-legend">{segs.map(([name, v, d]) => <div key={name}><i style={{ background: STRESS_COLOR[name] }} /><b>{name} {v.toFixed(0)} MW</b><span>{d}</span></div>)}</div>
    <p className="footnote">The busiest stress hour after full demand: the grid is capped at {s.gridCapMw.toFixed(0)} MW ({st.importPct.toFixed(0)}% of its limit), and {s.tier0Mw.toFixed(0)} MW of the {campus.toFixed(0)} MW campus demand is protected Tier 0. Events: {st.count} a year from the 2023 {st.area} notices; the import cap during an event is a case assumption. Every portfolio is tested against these hours, not only annual averages. <FlagPill label="case assumption" /></p></div>;
}

function OptionsCard({ options, sens, backupOn }: { options: Option[]; sens: Sensitivity | null; backupOn: string[] }) {
  const lo = 12, hi = 40, span = hi - lo;
  const row = (o: Option) => sens?.rows.find(r => r.label === o.label);
  const add = (o: Option) => { const r = row(o); return r ? r.backup.filter(b => backupOn.includes(b.name)).reduce((a, b) => a + b.totalM, 0) : 0; };
  const sc = (o: Option, k: "bear" | "base" | "bull") => { const r = row(o); return r ? fmt(r.perScenario[k].totalM + add(o), 0) : "…"; };
  const anyAdd = backupOn.length > 0;
  return <div className="card timeline"><h3 className="chart-h">Options tested against the same hours <small>recommended, then the fastest, cheapest and cleanest overall · cost under three price scenarios{anyAdd ? " · backup added" : ""}</small></h3>
    <div className="table-scroll"><table className="year-table opt-table"><thead><tr><th>Option</th><th>First power → full</th><th>10-yr cost</th><th>Bear $M</th><th>Bull $M</th><th>kg CO₂/MWh</th><th>Onsite MW</th><th>Against your limits</th></tr></thead>
      <tbody>{options.map(o => <tr key={o.label} className={o.role === "Recommended" ? "total" : ""}><td>{o.label}<small className="detail">{o.detail}</small></td><td>M{o.opening} → M{o.fullDemand}</td><td>${fmt(o.costM + add(o), 0)}M</td><td>{sc(o, "bear")}</td><td>{sc(o, "bull")}</td><td>{fmt(o.kgPerMwh)}</td><td>{fmt(o.backupMw)}</td><td>{o.tag}</td></tr>)}</tbody></table></div>
    <div className="months">{[12, 20, 28, 36].map(m => <span key={m}>M{m}</span>)}<span>M{hi}</span></div>
    {options.map(o => <div className="timeline-row" key={o.label}><div className={`timeline-pill ${o.role === "Recommended" ? "winner" : ""}`} style={{ marginLeft: `${(o.opening - lo) / span * 100}%`, width: `${Math.max(40, (o.fullDemand - o.opening) / span * 100)}%` }}>{o.label}<span>{o.tag}</span></div></div>)}
    <p className="footnote">Pills run from first power to full demand. “Misses” names the limit the option fails. Bear and bull re-run the dispatch with flat and faster-rising wholesale power and gas prices (see the price sensitivity above).</p></div>;
}

function StackedBars({ r, data, keys, scale, unit, labels }: { r: Report; data: Record<string, (number | null)[]>; keys: string[]; scale: number; unit: string; labels?: (i: number) => string }) {
  return <div className="bars year-bars"><div className="ylabels">{[1, 2 / 3, 1 / 3, 0].map(f => <span key={f}>{(scale * f).toFixed(0)}</span>)}</div>
    {r.years.map((y, i) => <div className="bar-col" key={y}><div className="stack">{keys.map(k => (data[k]?.[i] ?? 0) > 0.001 ? <i key={k} style={{ height: `${(data[k][i] ?? 0) / scale * 100}%`, background: r.colors[k] }} title={`${r.names[k]}: ${fmt(data[k][i], 1)} ${unit}`} /> : null)}
      {labels && <b className="bar-top" style={{ bottom: `calc(${keys.reduce((a, k) => a + (data[k]?.[i] ?? 0), 0) / scale * 100}% + 4px)` }}>{labels(i)}</b>}</div><small className="ytick">Y{y}</small></div>)}</div>;
}

function YearByYear({ r }: { r: Report }) {
  const rows = r.rows.filter(x => x.year !== "horizon");
  const hor = r.rows.find(x => x.year === "horizon") as YearRow;
  const sum = (d: Record<string, (number | null)[]>, keys: string[], i: number) => keys.reduce((a, k) => a + (d[k]?.[i] ?? 0), 0);
  const mixScale = Math.max(...r.years.map((_, i) => sum(r.mix, r.sources.mix, i))) * 1.12;
  const costScale = Math.max(...r.years.map((_, i) => sum(r.costM, r.sources.cost, i))) * 1.3;
  const kg = rows.map(x => x.kgPerMwh ?? 0);
  const kgScale = Math.max(...kg, r.co2Target) * 1.25;
  const usd = rows.map(x => x.decisionUsdPerMwh ?? 0);
  const usdScale = Math.max(...usd) * 1.6;
  const unitCols = Object.keys(r.unit);
  const allUnit = unitCols.flatMap(c => r.unit[c]).filter((v): v is number => v != null && v > 0);
  const lo = Math.log10(Math.min(...allUnit)), hi = Math.log10(Math.max(...allUnit));
  const heat = (v: number | null) => v == null ? "transparent" : `hsl(${120 - 120 * ((Math.log10(v) - lo) / Math.max(1e-9, hi - lo))}, 55%, 82%)`;
  return <div className="merged-card">
    <div className="card power-chart"><h3 className="chart-h">Where the electricity comes from <small>GWh a year</small></h3>
      <div className="legend">{r.sources.mix.map(k => <span key={k} style={{ ["--c" as string]: r.colors[k] }} className="lc">{r.names[k]}</span>)}</div>
      <StackedBars r={r} data={r.mix as Record<string, (number | null)[]>} keys={r.sources.mix} scale={mixScale} unit="GWh" labels={i => `${fmt(rows[i].cleanSharePct)}% clean`} />
      <p className="axis-label">Model years from development start; a year with no demand is not shown <FlagPill label="scope" /></p></div>
    <div className="year-pair">
      <div className="card power-chart"><h3 className="chart-h">What it costs, and who is paid <small>$M a year · dots = $/MWh</small></h3>
        <div className="legend">{r.sources.cost.map(k => <span key={k} style={{ ["--c" as string]: r.colors[k] }} className="lc">{r.names[k]}</span>)}</div>
        <div className="bars year-bars"><div className="ylabels">{[1, 2 / 3, 1 / 3, 0].map(f => <span key={f}>{(costScale * f).toFixed(0)}</span>)}</div>
          {r.years.map((y, i) => <div className="bar-col" key={y}><div className="stack">{r.sources.cost.map(k => (r.costM[k]?.[i] ?? 0) > 0.001 ? <i key={k} style={{ height: `${(r.costM[k][i] ?? 0) / costScale * 100}%`, background: r.colors[k] }} title={`${r.names[k]}: $${fmt(r.costM[k][i], 1)}M`} /> : null)}
            <b className="bar-top" style={{ bottom: `calc(${sum(r.costM, r.sources.cost, i) / costScale * 100}% + 4px)` }}>${fmt(sum(r.costM, r.sources.cost, i), 1)}M</b>
            <u className="dot" style={{ bottom: `${usd[i] / usdScale * 100}%` }} title={`$${fmt(usd[i], 0)} per MWh`} /></div><small className="ytick">Y{y}</small></div>)}</div>
        <p className="axis-label">Contracts and onsite equipment are paid every year; grid power and gas fuel only when used</p></div>
      <div className="card power-chart"><h3 className="chart-h">How clean it is <small>kg CO₂ per MWh</small></h3>
        <div className="legend"><span className="lc" style={{ ["--c" as string]: "#27ae60" }}>At or below {r.co2Target.toFixed(0)}</span><span className="lc" style={{ ["--c" as string]: "#e67e22" }}>Above</span><span className="dash">Carbon reference line</span></div>
        <div className="bars year-bars"><div className="ylabels">{[1, 2 / 3, 1 / 3, 0].map(f => <span key={f}>{(kgScale * f).toFixed(0)}</span>)}</div>
          {r.years.map((y, i) => <div className="bar-col" key={y}><div className="stack"><i style={{ height: `${kg[i] / kgScale * 100}%`, background: kg[i] <= r.co2Target ? "#27ae60" : "#e67e22" }} /><b className="bar-top" style={{ bottom: `calc(${kg[i] / kgScale * 100}% + 4px)` }}>{kg[i].toFixed(0)}</b>
            <b className="cap-mark" style={{ bottom: `${r.co2Target / kgScale * 100}%` }} /></div><small className="ytick">Y{y}</small></div>)}</div>
        <p className="axis-label">Average {fmt(hor.kgPerMwh)} kg/MWh over 10 years · clean share {fmt(hor.cleanSharePct)}%</p></div>
    </div>
    <div className="card table-card"><h3 className="chart-h">Results per model year <small>constant 2023 USD · same accounting as the official checker</small></h3>
      <div className="table-scroll"><table className="year-table"><thead><tr><th>Year</th><th>Requested GWh</th><th>Local energy $M</th><th>Relocation $M</th><th>Interruption $M</th><th>Total $M</th><th>Local $/MWh</th><th>kg CO₂/MWh</th><th>Clean</th><th>Merchant</th></tr></thead>
        <tbody>{[...rows, hor].map(x => <tr key={String(x.year)} className={x.year === "horizon" ? "total" : ""}><td>{x.year === "horizon" ? "10-year" : `Year ${x.year}`}</td><td>{fmt(x.requestedGwh)}</td><td>{fmt(x.localM, 1)}</td><td>{fmt(x.relocationM, 2)}</td><td>{fmt(x.interruptionM, 2)}</td><td><b>{fmt(x.totalM, 1)}</b></td><td>{fmt(x.localUsdPerMwh, 1)}</td><td>{fmt(x.kgPerMwh)}</td><td>{fmt(x.cleanSharePct)}%</td><td>{fmt(x.merchantSharePct)}%</td></tr>)}</tbody></table></div></div>
    <div className="card table-card"><h3 className="chart-h">What each source really costs per MWh it delivers <small>$/MWh · green = cheap, red = expensive</small></h3>
      <div className="table-scroll"><table className="year-table"><thead><tr><th>Year</th>{unitCols.map(c => <th key={c}>{c.replace("Portfolio avg (local $/MWh served)", "Overall average")}</th>)}</tr></thead>
        <tbody>{r.unitRows.map((u, i) => <tr key={u} className={u === "horizon" ? "total" : ""}><td>{u === "horizon" ? "10-year" : `Year ${u}`}</td>{unitCols.map(c => <td key={c} style={{ background: heat(r.unit[c][i]) }}>{r.unit[c][i] == null ? "–" : `$${fmt(r.unit[c][i], r.unit[c][i]! >= 100 ? 0 : 1)}`}</td>)}</tr>)}</tbody></table></div>
      <p className="footnote">Includes each source's fixed charges: a source that is paid for but rarely used looks expensive per MWh.</p></div>
    <details className="card fold report-fold"><summary><b>Download report (zip)</b><span>the images, yearly_results.csv and a plain-language results_summary.md</span></summary>
      <div className="gallery-head"><a className="export-link" href={r.zipUrl}>Download report (zip) ↓</a></div>
      <div className="gallery">{r.images.map(im => <figure key={im.key}><a href={im.url} target="_blank" rel="noreferrer"><img src={im.url} alt={im.title} loading="lazy" /></a><figcaption>{im.title}</figcaption></figure>)}</div></details>
  </div>;
}


export default App;
