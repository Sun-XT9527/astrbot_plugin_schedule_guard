"use strict";
const $ = id => document.getElementById(id);
const statuses = {busy:"忙碌",free:"空闲",unknown:"无法确定"};
const deliveries = {skipped_busy:"已触发 · 因忙碌未发送",allowed_unconfirmed:"已放行 · 发送未确认",sent:"确认已发送",sent_partial:"已发送 · 部分失败",not_sent:"已放行 · 未发送",simulation:"模拟检查 · 未发送"};
let bridge;
let simulationCount=0;
function text(tag, value, className) { const node=document.createElement(tag);node.textContent=value;if(className)node.className=className;return node; }
function notice(value, error=false) { $("notice").textContent=value;$("notice").className=error?"error":""; }
function unpack(value) { if(value?.status==="error") throw Error(value.message);return value?.status==="ok"?value.data:value; }
async function api(path, body) {
  if(window.parent===window) {
    // Standalone view: use the existing first-party HttpOnly login cookie.
    // Never inspect browser storage or copy dashboard credentials into the view.
    const response=await fetch("/api/plug/astrbot_plugin_schedule_guard/page/"+path,{method:body===undefined?"GET":"POST",credentials:"same-origin",headers:body===undefined?{}:{"Content-Type":"application/json"},body:body===undefined?undefined:JSON.stringify(body)});
    if(!response.ok)throw Error(response.status===401?"请先在同一浏览器登录 AstrBot，再刷新此页":"管理接口连接失败（"+response.status+"）");
    return unpack(await response.json());
  }
  return unpack(body===undefined?await bridge.apiGet("page/"+path,{}):await bridge.apiPost("page/"+path,body));
}
function date(value) { return value?new Date(value).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}):"尚未安排"; }
function renderRecord(record) {
  const item=document.createElement("details"), summary=document.createElement("summary");
  summary.append(text("span",date(record.triggered_at||record.checked_at)),text("span",statuses[record.status]||record.status,"pill "+record.status),text("span",deliveries[record.delivery]||record.delivery),text("span",record.reason));
  item.append(summary,text("p",`上下文 ${record.context_count} 条 · 召回记忆 ${record.memory_count} 条 · 用时 ${record.duration_ms} 毫秒`,"muted"));
  if(record.next_run_time)item.append(text("p","本轮结束后安排的下一次检查："+date(record.next_run_time),"muted"));
  for(const evidence of record.evidence||[]) item.append(text("p",evidence.source+" · "+evidence.quote,"source"));
  if(record.error||record.memory_errors?.length) item.append(text("p","诊断："+[record.error,...record.memory_errors||[]].filter(Boolean).join(" / "),"muted"));
  if(record.mode==="simulation") {
    const remove=text("button","删除这条模拟记录");
    remove.type="button";
    remove.onclick=()=>run(remove,async()=>{
      const result=await api("simulations/delete",{event_id:record.event_id});
      if($("simulation").firstChild?.dataset.eventId===record.event_id)$("simulation").replaceChildren();
      await refresh();
      notice(result.deleted?"模拟记录已删除":"这条模拟记录已不存在");
    });
    item.append(remove);
  }
  item.dataset.eventId=record.event_id;
  return item;
}
async function refresh() {
  const [stats, data]=await Promise.all([api("stats"),api("records")]);
  $("enabled").textContent=stats.enabled?"已启用":"尚未启用";
  $("model").textContent="判断模型："+stats.provider_id;
  $("skipped").textContent=stats.counts["live:skipped_busy"]||0;
  $("sent").textContent=(stats.counts["live:sent"]||0)+(stats.counts["live:sent_partial"]||0);
  simulationCount=Object.entries(stats.counts).reduce((total,[key,count])=>total+(key.startsWith("simulation:")?count:0),0);
  $("clear-simulations").disabled=simulationCount===0;
  const selected=$("session").value;$("session").replaceChildren();$("next").replaceChildren();
  for(const [index,session] of stats.sessions.entries()) { const option=text("option",`QQ 私聊 ${index+1}`);option.value=session.session_id;option.title=session.session_id;$("session").append(option);$("next").append(text("p",`QQ 私聊 ${index+1}：${date(session.next_run_time)}`)); }
  if(selected&&stats.sessions.some(s=>s.session_id===selected))$("session").value=selected;
  $("records").replaceChildren(...data.records.map(renderRecord));
  if(!data.records.length)$("records").append(text("p","还没有检查记录。可以先运行一次模拟检查。","muted"));
  return stats;
}
async function run(button, operation) {button.disabled=true;try{await operation();}catch(error){notice(error.message,true);}finally{button.disabled=button.id==="clear-simulations"&&simulationCount===0;}}
$("refresh").onclick=()=>run($("refresh"),async()=>{await refresh();notice("记录已刷新");});
$("clear-simulations").onclick=()=>run($("clear-simulations"),async()=>{
  if(!window.confirm("清空全部模拟检查记录？真实触发和发送记录会保留。"))return;
  const result=await api("simulations/delete",{all:true});
  $("simulation").replaceChildren();
  await refresh();
  notice(`已清空 ${result.deleted} 条模拟记录`);
});
$("check").onclick=()=>run($("check"),async()=>{notice("正在读取上下文、检索记忆并判断…");const record=await api("check",{session_id:$("session").value,at:$("at").value?$("at").value+":00+08:00":null});$("simulation").replaceChildren(renderRecord(record));$("simulation").firstChild.open=true;await refresh();notice("模拟检查完成，没有向 QQ 发送消息");});
$("bootstrap").onclick=()=>run($("bootstrap"),async()=>{notice("正在保存并验证日程记忆…");const result=await api("bootstrap",{session_id:$("session").value});notice(`${result.created?"已保存":"已有相同日程"}，记忆 #${result.memory_id}，${result.characters} 字，${result.verified?"召回验证通过":"召回尚未验证通过"}`,!result.verified);await refresh();});
(async()=>{try{if(window.parent!==window){bridge=window.AstrBotPluginPage;if(!bridge)throw Error("请从 AstrBot 的插件页面打开此管理页");await bridge.ready();}const now=new Date(Date.now()+8*3600000);$("at").value=now.toISOString().slice(0,16);const stats=await refresh();notice(stats.enabled?"拦截已启用：明确忙碌时跳过；无法确定时放行":"当前只可模拟检查，尚未启用实际拦截");}catch(error){notice(error.message,true);}})();
