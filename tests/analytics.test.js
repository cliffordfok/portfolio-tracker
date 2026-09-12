import test from 'node:test';
import assert from 'node:assert/strict';
import { contributionModel, monthlyModel, instrumentModel } from '../js/analytics.js';
const item = (r,u,i=0) => ({instrument_id:'EQUITY:AAPL',symbol:'AAPL',realized:String(r),unrealized:u===null?null:String(u),income:String(i),fees:'2'});
const day = (date,items) => ({date,instruments:items});
function fixture() {
  return {daily:[{date:'2024-01-01'},{date:'2024-01-02'},{date:'2024-02-02'}], analytics:{version:1,open_lots:[],daily:[day('2024-01-01',[item(0,100)]),day('2024-01-02',[item(0,110)]),day('2024-02-02',[item(80,60,7)])]}};
}
test('period contribution subtracts starting unrealized, retains net fees and excludes future',()=>{
  const p=fixture();
  p.analytics.daily.push(day('2024-03-01',[item(999,999)]));
  const m=contributionModel(p,'1M');
  assert.equal(m.baseline,'2024-01-01');
  assert.equal(m.rows[0].unrealized,-40);
  assert.equal(m.total,47);
  assert.equal(contributionModel(p,'ALL').total,147);
});
test('missing endpoint never becomes zero and old snapshots stay unavailable',()=>{
  const p=fixture();p.analytics.daily[0].instruments[0].unrealized=null;
  assert.equal(contributionModel(p,'1M').total,null);
  assert.equal(contributionModel({daily:[]},'ALL'),null);
});
test('fully closed instruments stay in contribution and detail histories; identity is exact',()=>{
  const p=fixture();p.analytics.daily.at(-1).instruments=[item(150,0)];
  p.recent_trades=[{instrument_id:'EQUITY:AAPL',symbol:'AAPL',action:'SELL',occurred_at:'2024-02-02T20:00:00Z'},{instrument_id:'OPTION:AAPL',symbol:'AAPL',action:'BUY',occurred_at:'2024-01-01T20:00:00Z'}];
  assert.equal(contributionModel(p,'1M').total,50);
  assert.equal(instrumentModel(p,'EQUITY:AAPL').trades.length,1);
  assert.deepEqual(instrumentModel(p,'EQUITY:AAPL').lots,[]);
});
const point=(date,r,segment=1)=>({date,daily_return:r===null?null:String(r),data_status:'OK',segment_id:segment});
test('monthly TWR compounds daily returns and compares the same dates',()=>{
  const p={daily:[point('2024-01-30',0.1),point('2024-01-31',-0.1),point('2024-02-01',0.2)]};
  const b={daily:[point('2024-01-30',0.02),point('2024-01-31',0.03),point('2024-02-01',0.1)]};
  const m=monthlyModel(p,b,'ALL');
  assert.ok(Math.abs(m[1].result+0.01)<1e-12);
  assert.ok(Math.abs(m[1].spy-0.0506)<1e-12);
  assert.equal(m[1].start,'2024-01-30');
  assert.ok(Math.abs(m[0].excess-0.1)<1e-12);
});
test('monthly gaps, missing benchmark and segment resets do not fabricate returns',()=>{
  const p={daily:[point('2024-01-30',0.1),point('2024-01-31',null,2)]};
  assert.equal(monthlyModel(p,{daily:[]},'ALL')[0].result,null);
  p.daily[1].daily_return='0.1';
  assert.equal(monthlyModel(p,{daily:[]},'ALL')[0].result,null);
  p.daily[1].segment_id=1;
  assert.equal(monthlyModel(p,{daily:[]},'ALL')[0].excess,null);
  p.data_status='FALLBACK';assert.deepEqual(monthlyModel(p,{},'ALL'),[]);
});
test('SPY uses the same opening valuation base as portfolio',()=>{
  const p={daily:[point('2024-01-02',0),point('2024-01-03',0.1)]};
  const b={daily:[point('2024-01-02',0.2),point('2024-01-03',0.1)]};
  assert.ok(Math.abs(monthlyModel(p,b,'ALL')[0].excess)<1e-12);
});

test('detail selection escapes content, retains selection on refresh and separates portfolios', async()=>{
  const {renderAnalytics}=await import('../js/analytics-view.js');
  const detail={innerHTML:''};
  const select={value:'',addEventListener(type,fn){this[type]=fn;},focus(){}};
  const container={innerHTML:'',querySelector(selector){return selector==='select'?select:detail;},querySelectorAll(){return [];}};
  const p=fixture();
  p.recent_trades=[{instrument_id:'EQUITY:AAPL',symbol:'<script>alert(1)</script>',action:'BUY',occurred_at:'2024-01-02T15:00:00Z',shares:'1',price:'100',fee:'0'}];
  renderAnalytics(container,p,{},'ALL','paper');
  assert.ok(container.innerHTML.includes('&lt;script&gt;'));
  assert.ok(!container.innerHTML.includes('<script>'));
  select.value='EQUITY:AAPL';select.change();
  assert.ok(detail.innerHTML.includes('不隨時間篩選'));
  assert.ok(detail.innerHTML.includes('沒有未平倉批次'));
  detail.innerHTML='';
  renderAnalytics(container,p,{},'1M','paper');
  assert.ok(detail.innerHTML.includes('買賣及收入歷史'));
  detail.innerHTML='';
  renderAnalytics(container,p,{},'ALL','live');
  assert.equal(detail.innerHTML,'');
});
