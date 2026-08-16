const $ = (id) => document.getElementById(id);
const parameterSchemas = {
  ma_cross: [['short_window','短均线',20],['long_window','长均线',60]],
  rsi_reversion: [['window','RSI 周期',14],['buy_below','买入阈值',35],['sell_above','卖出阈值',65]],
  bollinger: [['window','计算周期',20],['std_multiplier','标准差倍数',2]],
  ai_signal: [['entry_probability','开仓概率',0.55],['exit_probability','退出概率',0.45]],
};
let latest = null;

function renderParams() {
  $('strategyParams').innerHTML = parameterSchemas[$('strategy').value].map(([key,label,value]) => `<label>${label}<input data-param="${key}" type="number" value="${value}"></label>`).join('');
}
function formatPct(value){return `${value >= 0 ? '+' : ''}${(value*100).toFixed(2)}%`}
function renderMetrics(m){
  const items=[['累计收益',formatPct(m.total_return),'total_return'],['年化收益',formatPct(m.annual_return),'annual_return'],['夏普比率',m.sharpe_ratio.toFixed(2),'sharpe_ratio'],['最大回撤',formatPct(m.max_drawdown),'max_drawdown'],['胜率',formatPct(m.win_rate),'win_rate']];
  $('metrics').innerHTML=items.map(([label,value,key])=>`<div class="metric"><span>${label}</span><strong class="${key.includes('return')?(m[key]>=0?'positive':'negative'):key==='max_drawdown'?'negative':''}">${value}</strong><small>${key==='sharpe_ratio'?' 风险调整后':key==='win_rate'?` / ${m.closed_trades} 笔平仓`:''}</small></div>`).join('');
}
function drawLineChart(canvas, series, opts={}){
  const dpr=window.devicePixelRatio||1, rect=canvas.getBoundingClientRect(); canvas.width=rect.width*dpr; canvas.height=Number(canvas.getAttribute('height'))*dpr;
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);const w=rect.width,h=Number(canvas.getAttribute('height')),pad={l:48,r:14,t:12,b:28};
  const all=series.flatMap(s=>s.data),min=Math.min(...all),max=Math.max(...all),range=max-min||1;
  ctx.font='9px ui-monospace';ctx.fillStyle='#7a837e';ctx.strokeStyle='#dfddd5';ctx.lineWidth=1;
  for(let i=0;i<5;i++){const y=pad.t+(h-pad.t-pad.b)*i/4;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();const val=max-range*i/4;ctx.fillText(opts.percent?`${(val*100).toFixed(0)}%`:Math.round(val).toLocaleString(),2,y+3)}
  const x=i=>pad.l+(w-pad.l-pad.r)*i/(series[0].data.length-1), y=v=>pad.t+(max-v)/range*(h-pad.t-pad.b);
  series.forEach(s=>{ctx.beginPath();ctx.strokeStyle=s.color;ctx.lineWidth=s.width||1.5;s.data.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.stroke()});
  const labels=latest.equity_curve;[0,Math.floor(labels.length/2),labels.length-1].forEach(i=>{ctx.fillStyle='#7a837e';ctx.fillText(labels[i].date,x(i)-25,h-7)});
  canvas._chart={x,y,pad,w,h,series,min,max};
}
function renderCharts(){
  drawLineChart($('equityChart'),[{data:latest.equity_curve.map(x=>x.equity),color:'#174c3c',width:2},{data:latest.benchmark_curve,color:'#a8aaa4'}]);
  drawLineChart($('drawdownChart'),[{data:latest.equity_curve.map(x=>x.drawdown),color:'#e76f43',width:1.5}],{percent:true});
}
function renderTrades(trades){$('trades').innerHTML=trades.slice().reverse().slice(0,25).map(t=>`<tr><td>${t.date}</td><td class="${t.side.toLowerCase()}">${t.side}</td><td>${t.quantity}</td><td>${t.price.toFixed(2)}</td><td title="${t.reason}">${t.reason}</td></tr>`).join('')||'<tr><td colspan="5">暂无成交</td></tr>'}
function renderOrders(orders){$('orders').innerHTML=orders.slice().reverse().slice(0,50).map(o=>`<tr><td title="${o.order_id}">${o.order_id.slice(0,8)}</td><td>${o.trading_date}</td><td class="${o.side.toLowerCase()}">${o.side}</td><td>${o.order_type}</td><td>${o.quantity} / ${o.filled_quantity}</td><td>${o.average_fill_price?o.average_fill_price.toFixed(2):'—'}</td><td class="status-${o.status.toLowerCase()}">${o.status}</td><td title="${o.reject_reason||''}">${o.reject_reason||'—'}</td></tr>`).join('')||'<tr><td colspan="8">暂无订单</td></tr>'}
async function run(){
  $('runButton').disabled=true;$('runButton').firstElementChild.textContent='计算中…';$('error').textContent='';
  const params={};document.querySelectorAll('[data-param]').forEach(el=>params[el.dataset.param]=Number(el.value));
  const payload={symbol:$('symbol').value,data_source:$('dataSource').value,start_date:$('startDate').value,end_date:$('endDate').value,strategy:$('strategy').value,params,initial_cash:Number($('initialCash').value),days:Number($('days').value),seed:Number($('seed').value),max_position_weight:Number($('maxWeight').value)/100,stop_loss_pct:Number($('stopLoss').value)/100,commission_rate:Number($('commission').value)/10000,taker_fee_rate:Number($('commission').value)/10000,maker_fee_rate:Number($('makerFee').value)/10000,slippage_rate:Number($('slippage').value)/10000,minimum_commission:Number($('minCommission').value),sell_tax_rate:Number($('sellTax').value)/10000,lot_size:Number($('lotSize').value),minimum_notional:Number($('minNotional').value),settlement_days:0,max_daily_loss_pct:Number($('dailyLoss').value)/100,max_drawdown_halt_pct:Number($('drawdownHalt').value)/100,max_volume_participation:Number($('volumeParticipation').value)/100};
  try{const response=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw new Error(data.error||'回测失败');latest=data;renderMetrics(data.metrics);renderTrades(data.trades);renderOrders(data.orders||[]);renderCharts()}
  catch(error){$('error').textContent=error.message}finally{$('runButton').disabled=false;$('runButton').firstElementChild.textContent='运行回测'}
}
async function init(){
  const response=await fetch('/api/strategies'),data=await response.json();$('strategy').innerHTML=data.strategies.map(s=>`<option value="${s.id}">${s.name}</option>`).join('');renderParams();run();
}
$('strategy').addEventListener('change',renderParams);$('runButton').addEventListener('click',run);$('maxWeight').addEventListener('input',e=>$('weightOut').textContent=`${e.target.value}%`);$('stopLoss').addEventListener('input',e=>$('stopOut').textContent=`${e.target.value}%`);window.addEventListener('resize',()=>latest&&renderCharts());
$('dataSource').addEventListener('change',e=>$('realDates').classList.toggle('hidden',e.target.value!=='public'));
$('equityChart').addEventListener('mousemove',e=>{if(!latest)return;const c=e.currentTarget._chart,rect=e.currentTarget.getBoundingClientRect();let i=Math.round((e.clientX-rect.left-c.pad.l)/(c.w-c.pad.l-c.pad.r)*(latest.equity_curve.length-1));i=Math.max(0,Math.min(latest.equity_curve.length-1,i));const p=latest.equity_curve[i],tip=$('chartTooltip');tip.style.display='block';tip.style.left=`${Math.min(e.clientX-rect.left+20,rect.width-150)}px`;tip.style.top=`${Math.max(55,e.clientY-rect.top)}px`;tip.innerHTML=`${p.date}<br>策略 ${p.equity.toFixed(0)}<br>基准 ${latest.benchmark_curve[i].toFixed(0)}`});$('equityChart').addEventListener('mouseleave',()=>$('chartTooltip').style.display='none');
init();
