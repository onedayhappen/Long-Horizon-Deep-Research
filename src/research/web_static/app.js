"use strict";
const $ = (s, root = document) => root.querySelector(s);
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const names = {workspace:"研究工作台",research:"全部研究",evidence:"证据资料库",reports:"研究报告",settings:"模型与设置"};
const phaseNames = {scoping:"明确范围",planning:"研究规划",research:"收集证据",researching:"收集证据",writing:"撰写报告",drafting:"撰写报告",auditing:"审核报告",delivery:"报告交付",complete:"研究完成",terminal:"本轮已结束"};
let state = {view:"workspace", runs:[], settings:{}, selected:null, detail:null, tab:"overview", filter:"all", query:"", library:null};
let importedContract = null, toastTimer, loadingList = false, routeEpoch = 0;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:"no-store"} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请重试");
  return data;
}
function toast(text) { const node=$("#toast"); node.textContent=text; node.style.display="block"; clearTimeout(toastTimer); toastTimer=setTimeout(()=>node.style.display="none",4500); }
function date(value) { return value ? new Date(value).toLocaleDateString("zh-CN",{month:"2-digit",day:"2-digit"}) : "—"; }
function time(value) { return value ? new Date(value).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"}) : "—"; }
function status(run) {
  if (run.lifecycle_status === "complete") return {label:"已完成",cls:"complete"};
  if (run.pending?.length && run.running) return {label:"等待资料",cls:"waiting"};
  if (run.running || run.lifecycle_status === "starting") return {label:"研究中",cls:""};
  if (run.research_outcome === "failed" || run.error || run.lifecycle_status === "failed") return {label:"运行失败",cls:"failed"};
  return {label:"可恢复",cls:"waiting"};
}
function badge(run) { const s=status(run); return `<span class="badge ${s.cls}">${s.label}</span>`; }
function empty(title, text, button=false) { return `<div class="empty">${icon("search")}<h3>${esc(title)}</h3><p>${esc(text)}</p>${button?'<button class="button primary" data-action="new">新建研究</button>':""}</div>`; }
function heading(title, description, button=true) { return `<div class="page-heading"><div><h1>${title}</h1><p>${description}</p></div>${button?`<button class="button primary" data-action="new">${icon("plus")}新建研究</button>`:""}</div>`; }
function card(run) {
  const calls = Object.values(run.budget?.external_calls_by_pool || {}).reduce((a,b)=>a+b,0);
  return `<button class="research-card" data-run="${esc(run.run_id)}"><div class="card-top"><div class="card-title">${icon("file")}<h3>${esc(run.question)}</h3></div>${badge(run)}</div><p class="card-question">${esc(run.run_id)} · ${esc(phaseNames[run.phase] || run.phase)}</p><div class="card-meta"><span>${icon("book")}${run.counts?.evidence || 0} 条证据</span><span>${icon("spark")}${calls} 次调用</span><span>${icon("clock")}${date(run.created_at)}</span><span class="mode">${run.execution?.mode === "replay"?"离线回放 · 虚构样例":run.execution?.model_backend === "chat_json"?"AI 辅助研究":"会话辅助研究"}</span></div></button>`;
}
function dashboard() {
  const runs=state.runs, completed=runs.filter(r=>r.lifecycle_status==="complete").length;
  const evidence=runs.reduce((sum,r)=>sum+(r.counts?.evidence||0),0), active=runs.filter(r=>r.running).length;
  const stats=[["全部研究",runs.length,"search","每一个问题，都是新的探索"],["已完成报告",completed,"file","保留结论、依据与研究边界"],["研究进行中",active,"clock","持续跟进研究与待补资料"],["已收集证据",evidence,"book","可定位、可回查的原文片段"]];
  return `${heading("让每一次探索，都有据可循。","从一个好问题开始，构建你的研究与知识积累。",false)}
  <section class="intro"><div><span class="eyebrow">YOUR NEXT DISCOVERY</span><h2>下一个值得深入的问题是什么？</h2><p>定义范围、追踪证据，形成经得起回查的研究报告。</p><button class="button primary" data-action="new">${icon("plus")}开启新的研究</button></div><div class="flow-mini" aria-label="问题、证据、报告"><div class="flow-step"><span>${icon("search")}</span>提出问题</div><div class="flow-line"></div><div class="flow-step"><span>${icon("link")}</span>收集证据</div><div class="flow-line"></div><div class="flow-step"><span>${icon("file")}</span>形成报告</div></div></section>
  <section class="stats">${stats.map(s=>`<div class="stat"><div class="stat-top">${s[0]}<span class="stat-icon">${icon(s[2])}</span></div><div class="stat-number">${s[1]}</div><small>${s[3]}</small></div>`).join("")}</section>
  <div class="workspace-grid"><section><div class="section-head"><h3>最近的研究 <span class="section-count">${runs.length}</span></h3><button class="text-button" data-view="research">查看全部</button></div><div class="research-list">${runs.slice(0,5).map(card).join("") || empty("你的研究空间已就绪","从感兴趣的问题开始，或运行离线样例熟悉流程。",true)}</div></section>
  <aside><div class="side-panel"><h3>一份可靠报告的诞生</h3><div class="steps">${[["明确研究问题","定义范围与必答需求"],["收集与核验证据","保存来源，保留原文定位"],["迭代与审核","围绕缺口补证，检查需求覆盖"],["交付研究报告","结论与引用一同留存"]].map((s,i)=>`<div class="step"><span class="step-num">${i+1}</span><div><b>${s[0]}</b><p>${s[1]}</p></div></div>`).join("")}</div></div><div class="side-panel"><div class="small-note"><b>先体验一次完整流程</b>使用虚构资料的离线样例，无需模型密钥。样例不代表真实研究结论。</div><button class="text-button" data-action="demo">运行离线样例</button><div class="tagline">一切积累，始于一次追问。</div></div></aside></div>`;
}
function listView() {
  return `${heading("全部研究","管理问题、查看进度，继续尚未完成的探索。")}
    <div class="searchbar"><div class="search-field">${icon("search")}<input id="search-runs" type="search" placeholder="搜索研究问题…" aria-label="搜索研究" value="${esc(state.query)}"></div><div class="filter">${[["all","全部"],["active","进行中"],["complete","已完成"],["waiting","待继续"]].map(([v,t])=>`<button data-filter="${v}" class="${state.filter===v?"active":""}">${t}</button>`).join("")}</div></div><div id="filtered-list" class="research-list">${filteredCards()}</div>`;
}
function filteredCards() { return state.runs.filter(r=>(r.question+" "+r.run_id).toLowerCase().includes(state.query.toLowerCase())).filter(r=>state.filter==="all" || (state.filter==="complete"?r.lifecycle_status==="complete":state.filter==="active"?r.running:!r.running&&r.lifecycle_status!=="complete")).map(card).join("") || empty("没有符合条件的研究","试试其他关键词或筛选条件。"); }
function reportsView() { const runs=state.runs.filter(r=>r.artifacts?.includes("report.md")); return heading("研究报告","从结论回到原始证据，随时回顾、下载与复用。") + `<div class="compact-list">${runs.map(r=>`<article class="report-tile"><div><div class="meta-line">${badge(r)}<span class="muted">${date(r.created_at)}</span></div><h3>${esc(r.question)}</h3><p>${r.counts?.evidence||0} 条证据 · Markdown 报告${r.execution?.mode==="replay"?" · 离线虚构样例":""}</p></div><button class="button" data-run="${esc(r.run_id)}" data-initial-tab="report">阅读报告</button></article>`).join("") || empty("还没有研究报告","完成研究后，报告会自动出现在这里。",true)}</div>`; }
function safeUrl(value) { try { const url=new URL(value); return ["https:","http:"].includes(url.protocol)?url.href:null; } catch { return null; } }
function evidenceCard(e, run) {
  const snapshots=run.entities?.snapshot||[]; const snap=e.snapshot || snapshots.find(s=>s.id===e.snapshot_id) || {};
  const url=safeUrl(snap.url); const loc=e.locator||{};
  return `<article class="evidence-item"><div class="meta-line"><span class="badge">${esc(({primary_official:"官方一手资料",primary_study:"原始研究",secondary:"二手资料"})[e.source_class] || "证据片段")}</span><span class="muted">${esc(e.id||e.evidence_id||"")}</span></div><blockquote>${esc(e.excerpt||e.quote||"暂无原文片段")}</blockquote>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`:""}<small>原文位置 ${esc(loc.start??"—")}–${esc(loc.end??"—")} · ${esc(loc.kind||"待定位")}</small><small>${esc(run.question)}</small></article>`;
}
function libraryView() { return `${heading("证据资料库","保存下来的原文片段，让每个事实都可以回到来源。",false)}${state.library===null?'<div class="loading">正在整理已保存的证据…</div>':`<div class="evidence-grid">${state.library.map(({e,run})=>evidenceCard(e,run)).join("")}</div>${state.library.length?"":empty("暂无证据","研究中核验的证据会在这里汇总。")}`}`; }
function settingsView() { const s=state.settings; return `${heading("模型与设置","连接真实模型，使用现有研究引擎完成规划、分析和写作。",false)}<div class="settings-grid"><form id="settings-form" class="panel"><div class="section-head"><h2>模型服务</h2><span class="badge ${s.configured?"complete":"waiting"}">${s.configured?"已配置密钥":"待配置"}</span></div><p class="muted">支持具备 JSON Output 能力的模型。填写服务商控制台中的准确模型名称。</p><label for="provider">服务商</label><select name="provider" id="provider"><option value="deepseek" ${s.provider==="deepseek"?"selected":""}>DeepSeek</option><option value="siliconflow" ${s.provider==="siliconflow"?"selected":""}>硅基流动 SiliconFlow</option></select><label for="model">模型名称</label><input id="model" name="model" value="${esc(s.model)}" required maxlength="120"><label for="api-key">API Key</label><input type="password" id="api-key" name="api_key" autocomplete="off" placeholder="${s.configured?"已配置；留空保持当前密钥":"粘贴你的模型 API Key"}" maxlength="4096"><p class="muted">密钥仅保留在本次服务进程中，不写入磁盘；也可通过服务端环境变量配置。</p><div class="form-error" id="settings-error" role="alert"></div><div class="form-actions"><button class="button primary" type="submit">保存设置</button><button class="button" type="button" data-action="check">${icon("link")}测试连接</button><button class="text-button" type="button" data-action="clear-key">清除临时密钥</button></div><div class="notice">测试连接会发送一次真实模型请求并产生少量 API 费用。新配置用于新建研究；历史研究恢复时沿用原模型。</div></form><aside><div class="panel"><h3>研究如何运行</h3><div class="key-value"><span>模型后端</span><b>真实 API 调用</b></div><div class="key-value"><span>执行模式</span><b>Assisted</b></div><div class="key-value"><span>搜索与抓取</span><b>外部辅助通道</b></div><div class="key-value"><span>状态保存</span><b>本地 SQLite</b></div><p class="small-note">模型规划、提取、审核和写作来自同一配置模型的独立请求，不等同于独立模型交叉验证。</p></div><div class="panel"><h3>运行目录</h3><p class="small-note path-label">${esc(s.runs_root)}</p></div></aside></div>`; }
function inlineMarkdown(text) {
  return esc(text).replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,(all,label,url)=>`<a href="${url}" rel="noopener noreferrer" target="_blank">${label}</a>`)
    .replace(/\[([^\]]+)\]\(blobs\/([a-f0-9]{64})\)/g,(all,label,sha)=>`<a href="/api/runs/${encodeURIComponent(state.selected)}/snapshots/${sha}">${label}</a>`)
    .replace(/\[\^([A-Za-z0-9._-]+)\]/g,(all,id)=>`<sup><a href="#citation-${id}" class="citation-ref">[${id}]</a></sup>`)
    .replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/`([^`]+)`/g,"<code>$1</code>");
}
function markdown(text) {
  let fenced=false, output=[], table=false;
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) { if(table){output.push("</tbody></table>");table=false;} output.push(fenced?"</code></pre>":"<pre><code>"); fenced=!fenced; continue; }
    if(fenced){output.push(esc(line)+"\n");continue;}
    if(line.trim().startsWith("|")){if(/^\s*\|[\s:|\-]+\|\s*$/.test(line))continue;if(!table){output.push("<table><tbody>");table=true;}output.push("<tr>"+line.trim().replace(/^\||\|$/g,"").split("|").map(c=>`<td>${inlineMarkdown(c.trim())}</td>`).join("")+"</tr>");continue;}
    if(table){output.push("</tbody></table>");table=false;}
    const footnote=/^\[\^([A-Za-z0-9._-]+)\]:\s*(.*)$/.exec(line);
    if(footnote){output.push(`<p id="citation-${footnote[1]}" class="citation"><b>[${esc(footnote[1])}]</b> ${inlineMarkdown(footnote[2])}</p>`);continue;}
    if(line.startsWith("> ")){output.push(`<blockquote>${inlineMarkdown(line.slice(2))}</blockquote>`);continue;}
    const head=/^(#{1,3})\s+(.+)$/.exec(line); if(head){output.push(`<h${head[1].length}>${inlineMarkdown(head[2])}</h${head[1].length}>`);continue;}
    if(line.trim())output.push(`<p>${inlineMarkdown(line)}</p>`);
  }
  if(fenced)output.push("</code></pre>"); if(table)output.push("</tbody></table>"); return output.join("");
}
function overview(run) {
  const covered=(run.coverage||[]).filter(c=>c.disposition==="satisfied").map(c=>c.requirement_id);
  const reqs=run.contract?.requirements||[], calls=Object.values(run.budget?.external_calls_by_pool||{}).reduce((a,b)=>a+b,0);
  return `<div class="detail-columns"><div><section class="panel"><div class="section-head"><h3>需求覆盖</h3><span class="muted">${reqs.filter(r=>covered.includes(r.id)).length} / ${reqs.length} 已满足</span></div>${reqs.map(r=>`<div class="requirement"><span class="number-tag">${esc(r.id)}</span><div><h3>${esc(r.question)}</h3><p>${r.acceptance_checks.length} 项验收条件 · ${r.priority==="must"?"必须回答":"补充问题"}</p></div><span class="badge ${covered.includes(r.id)?"complete":"waiting"}">${covered.includes(r.id)?"已覆盖":"待补证"}</span></div>`).join("") || '<p class="overview-empty">正在读取研究合约…</p>'}</section><section class="panel"><h3>研究大纲</h3>${(run.entities?.outline||[]).map(o=>`<pre class="json-box">${esc(JSON.stringify(o,null,2))}</pre>`).join("") || '<p class="overview-empty">完成规划后，大纲将在此显示。</p>'}</section></div><aside><section class="panel"><h3>运行概况</h3><div class="key-value"><span>当前阶段</span><b>${esc(phaseNames[run.phase]||run.phase)}</b></div><div class="key-value"><span>研究轮次</span><b>${run.research_rounds||0}</b></div><div class="key-value"><span>外部调用</span><b>${calls}</b></div><div class="key-value"><span>大纲版本</span><b>${run.outline_version||"—"}</b></div><div class="key-value"><span>模型</span><b>${esc(run.execution?.model||"外部会话 / 回放")}</b></div><div class="key-value"><span>研究结果</span><b>${esc(run.research_outcome||"尚未完成")}</b></div></section><section class="panel"><h3>最近活动</h3>${(run.events||[]).slice(0,9).map(e=>`<div class="event"><time>${time(e.created_at)}</time><span>${esc(e.kind)}</span></div>`).join("") || '<p class="overview-empty">等待首条运行事件</p>'}</section></aside></div>`;
}
function tasks(run) {
  return (run.pending||[]).map(p=>`<section class="panel pending-card"><div class="section-head"><h3>${esc(({search:"提交搜索结果",fetch:"提交网页快照",role:"提交角色响应"})[p.kind]||p.kind)}</h3><span class="badge waiting">等待处理</span></div><p class="muted">${esc(p.payload?.text||p.payload?.url||p.logical_key)}</p><details><summary>查看完整请求与输入哈希</summary><pre class="json-box">${esc(JSON.stringify(p,null,2))}</pre></details><button class="button" data-action="request-download" data-request="${esc(p.request_id)}">${icon("download")}下载请求 JSON</button><form class="response-form" data-request="${esc(p.request_id)}"><label for="response-${esc(p.request_id)}">响应信封 JSON</label><textarea id="response-${esc(p.request_id)}" name="response" required placeholder='包含 input_hash、producer 与 result；网页快照可添加 snapshot_text。'></textarea><div class="form-actions"><button class="button primary" type="submit">提交响应</button></div></form><div class="notice">请提交实际来源数据。网页快照的 UTF-8 内容必须与 blob_hash 一致；也可继续使用项目的 research_assistant.py 脚本。</div></section>`).join("") || empty("当前没有待处理请求","研究运行时，需外部提交的资料会出现在这里。");
}
function detailView() {
  const run=state.detail; if(!run)return '<div class="loading">正在读取研究…</div>';
  const report=state.tab==="report", downloading=run.artifacts?.includes("report.md");
  let body="";
  if(state.tab==="overview")body=overview(run);
  if(state.tab==="evidence")body=`<div class="evidence-grid">${(run.evidence||[]).map(e=>evidenceCard(e,run)).join("")}</div>${run.evidence?.length?"":empty("尚未产生证据","收集并核验资料后，证据会在这里显示。")}`;
  if(report)body=run.report?`<article class="panel report">${markdown(run.report)}</article>`:empty("报告尚未生成","研究完成并通过检查后，可以阅读和下载报告。");
  if(state.tab==="requests")body=tasks(run);
  return `<button class="text-button back" data-view="research">返回全部研究</button><div class="page-heading detail-heading"><div><span class="eyebrow">RESEARCH ${esc(run.run_id)}</span><h1>${esc(run.question)}</h1><p>${date(run.created_at)} 创建 · 证据与状态自动保存</p></div>${badge(run)}</div>
  ${run.execution?.mode==="replay"?'<div class="notice">这是使用虚构资料运行的离线回放，用于验证流程，不代表真实研究结论。</div>':""}${run.error?`<div class="error-banner">${esc(run.error)}</div>`:""}
  <div class="detail-actions">${!run.running&&run.lifecycle_status!=="complete"&&run.lifecycle_status!=="starting"?`<button class="button primary" data-action="resume">${icon("play")}恢复研究</button>`:""}${downloading?`<a class="button" href="/api/runs/${encodeURIComponent(run.run_id)}/artifacts/report.md">${icon("download")}下载报告</a>`:""}${run.artifacts?.includes("evidence.json")?`<a class="button" href="/api/runs/${encodeURIComponent(run.run_id)}/artifacts/evidence.json">${icon("download")}导出证据</a>`:""}<button class="button" data-action="refresh">${icon("refresh")}刷新进度</button></div>
  <div class="tabs" role="tablist">${[["overview","研究概况"],["evidence",`证据资料 ${run.evidence?.length||0}`],["report","研究报告"],["requests",`待处理请求 ${run.pending?.length||0}`]].map(([v,t])=>`<button class="tab ${state.tab===v?"active":""}" data-tab="${v}" role="tab" aria-selected="${state.tab===v}">${t}</button>`).join("")}</div><div role="tabpanel">${body}</div>`;
}
function render() {
  $("#crumb").textContent=state.selected?"研究详情":names[state.view];
  document.querySelectorAll("nav .nav-item, .sidebar-bottom .nav-item").forEach(n=>n.classList.toggle("active",n.dataset.view===state.view));
  $("#nav-count").textContent=state.runs.length;
  $("#main").innerHTML=state.selected?detailView():({workspace:dashboard,research:listView,reports:reportsView,evidence:libraryView,settings:settingsView}[state.view]||dashboard)();
}
async function reloadRuns() { if(loadingList)return;loadingList=true;try{const data=await api("/api/runs");state.runs=data.runs;if(data.warnings?.length)toast(data.warnings.join("；"));}finally{loadingList=false;} }
async function refreshDetail() { const id=state.selected;if(!id)return;const detail=await api(`/api/runs/${encodeURIComponent(id)}`);if(state.selected===id)state.detail=detail; }
async function navigate(view, runId=null, tab="overview") {
  const epoch=++routeEpoch; state.view=view;state.selected=runId;state.tab=tab;state.detail=null;
  $("#sidebar").classList.remove("open");
  history.replaceState(null,"",runId?`#run/${encodeURIComponent(runId)}/${tab}`:`#${view}`); render();
  try {
    if(runId)await refreshDetail();
    else if(view==="evidence") {state.library=null;render();const runs=await Promise.all(state.runs.map(r=>api(`/api/runs/${encodeURIComponent(r.run_id)}`)));if(epoch===routeEpoch)state.library=runs.flatMap(run=>(run.evidence||[]).map(e=>({e,run})));}
    else if(view==="settings")state.settings=await api("/api/settings");
    if(epoch===routeEpoch)render();
  }catch(error){if(epoch===routeEpoch)$("#main").innerHTML=`<div class="error-banner">${esc(error.message)}</div><button class="button" data-view="workspace">返回工作台</button>`;}
}
async function openNew() { $("#create-error").textContent="";$("#new-dialog").showModal();$("#question").focus(); }
async function action(name,node) {
  if(name==="new")return openNew();
  if(name==="close-dialog")return $("#new-dialog").close();
  if(name==="demo"){node.disabled=true;const result=await api("/api/runs",{question:"离线回放演示研究",mode:"demo"});await navigate("research",result.run_id);return;}
  if(name==="refresh"){await refreshDetail();await reloadRuns();render();return;}
  if(name==="resume"){node.disabled=true;await api(`/api/runs/${encodeURIComponent(state.selected)}/resume`,{});toast("研究已恢复");await refreshDetail();render();return;}
  if(name==="check"){node.disabled=true;node.textContent="正在连接模型…";await saveModel();const result=await api("/api/settings/check",{});toast(`连接成功 · ${result.model}`);render();return;}
  if(name==="clear-key"){state.settings=await api("/api/settings",{provider:state.settings.provider,model:state.settings.model,clear_key:true});render();toast(state.settings.configured?"临时密钥已清除，当前使用服务端环境变量":"临时密钥已清除");return;}
  if(name==="request-download"){const p=state.detail.pending.find(p=>p.request_id===node.dataset.request);download(JSON.stringify(p,null,2),`request-${p.request_id.slice(0,8)}.json`);}
}
function download(text,name){const url=URL.createObjectURL(new Blob([text],{type:"application/json;charset=utf-8"}));const a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function saveModel(){const form=$("#settings-form");const data=new FormData(form);state.settings=await api("/api/settings",{provider:data.get("provider"),model:data.get("model"),api_key:data.get("api_key")});}
document.addEventListener("click", async event=>{
  const node=event.target.closest("button, a[data-view]"); if(!node)return;
  try {
    if(node.dataset.view)await navigate(node.dataset.view);
    else if(node.dataset.run)await navigate("research",node.dataset.run,node.dataset.initialTab||"overview");
    else if(node.dataset.tab){state.tab=node.dataset.tab;history.replaceState(null,"",`#run/${encodeURIComponent(state.selected)}/${state.tab}`);render();}
    else if(node.dataset.filter){state.filter=node.dataset.filter;render();}
    else if(node.dataset.action)await action(node.dataset.action,node);
  }catch(error){toast(error.message);node.disabled=false;if(node.dataset.action==="check")node.innerHTML=icon("link")+"测试连接";}
});
document.addEventListener("input",event=>{if(event.target.id==="search-runs"){state.query=event.target.value;$("#filtered-list").innerHTML=filteredCards();}});
document.addEventListener("change",async event=>{
  if(event.target.id==="provider")$("#model").value=event.target.value==="deepseek"?"deepseek-v4-pro":"deepseek-ai/DeepSeek-V3.2";
  if(event.target.id==="contract-file"){
    try{const file=event.target.files[0];if(!file){importedContract=null;return;}if(file.size>500000)throw new Error("合约文件过大");const value=JSON.parse(await file.text());if(typeof value.question!=="string")throw new Error("合约缺少 question");importedContract=value;$("#question").value=value.question;$("#contract-note").textContent=`已导入：${file.name}`;}
    catch(error){importedContract=null;$("#contract-note").textContent=error.message;}
  }
});
document.addEventListener("submit",async event=>{
  event.preventDefault();const form=event.target;const button=form.querySelector('button[type="submit"]');if(button)button.disabled=true;
  try{
    if(form.id==="new-form"){
      const fields=new FormData(form);const data={question:fields.get("question"),requirements:fields.get("requirements").split("\n").map(s=>s.trim()).filter(Boolean),scope:fields.get("scope"),max_calls:Number(fields.get("max_calls")),max_minutes:Number(fields.get("max_minutes")),contract:importedContract};
      const result=await api("/api/runs",data);$("#new-dialog").close();form.reset();importedContract=null;$("#contract-note").textContent="";await navigate("research",result.run_id);
    }else if(form.id==="settings-form"){await saveModel();render();toast("设置已保存，仅保留在本次服务进程中");}
    else if(form.classList.contains("response-form")){const response=JSON.parse(new FormData(form).get("response"));await api(`/api/runs/${encodeURIComponent(state.selected)}/responses/${encodeURIComponent(form.dataset.request)}`,response);toast("响应已提交");await refreshDetail();render();}
  }catch(error){if(form.id==="new-form")$("#create-error").textContent=error.message;else if(form.id==="settings-form")$("#settings-error").textContent=error.message;else toast(error.message);}
  finally{if(button)button.disabled=false;}
});
$("#menu").addEventListener("click",()=>$("#sidebar").classList.toggle("open"));
document.addEventListener("click",e=>{if(window.innerWidth<=700&&!e.target.closest("#sidebar,#menu"))$("#sidebar").classList.remove("open");});
async function init(){try{await Promise.all([reloadRuns(),api("/api/settings").then(s=>state.settings=s)]);const hash=location.hash.slice(1).split("/");if(hash[0]==="run"&&hash[1])await navigate("research",decodeURIComponent(hash[1]),["overview","evidence","report","requests"].includes(hash[2])?hash[2]:"overview");else await navigate(names[hash[0]]?hash[0]:"workspace");}catch(error){$("#main").innerHTML=`<div class="error-banner">无法连接研究服务：${esc(error.message)}</div>`;}}
// Polling never replaces a form while the user is composing a response or setting.
setInterval(async()=>{if(document.hidden||$("#new-dialog").open)return;try{if(state.selected){if(state.tab==="requests"&&Array.from(document.querySelectorAll("textarea")).some(t=>t.value||t===document.activeElement))return;await refreshDetail();if(state.tab!=="report")render();}else if(state.view==="workspace"){await reloadRuns();render();}}catch{/* Keep the last good view; explicit refresh reports errors. */}},5000);
init();
