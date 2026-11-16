"""生成一个带探针的预览副本：把页面渲染后的真实 color / 有效背景 / WCAG 比值倒进 DOM。"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent
src = (ROOT / 'index.html').read_text(encoding='utf-8')

PROBE = r"""
<pre id="__probe__" style="display:none"></pre>
<script>
(function(){
  const P = c => { const m = String(c).match(/rgba?\(([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\)/);
    return m ? {r:+m[1], g:+m[2], b:+m[3], a:m[4]===undefined?1:+m[4]} : null; };
  const ov = (f,b) => ({r:f.r*f.a+b.r*(1-f.a), g:f.g*f.a+b.g*(1-f.a), b:f.b*f.a+b.b*(1-f.a), a:1});
  const L = c => { const f = v => { v/=255; return v<=.03928? v/12.92 : Math.pow((v+.055)/1.055,2.4); };
    return .2126*f(c.r)+.7152*f(c.g)+.0722*f(c.b); };
  const R = (a,b) => { const l1=L(a), l2=L(b); return (Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05); };
  function bgOf(el){
    let acc = null, node = el, op = 1;
    while(node && node.nodeType === 1){
      const cs = getComputedStyle(node);
      const c = P(cs.backgroundColor);
      if(c && c.a > 0){
        acc = acc ? ov(acc, c) : (c.a >= 1 ? c : ov(c, {r:11,g:14,b:19,a:1}));
        if(acc.a >= .999) break;
      }
      node = node.parentElement;
    }
    return acc || {r:11,g:14,b:19,a:1};
  }
  function path(el){
    const out = [];
    let n = el, i = 0;
    while(n && n.nodeType === 1 && i < 3){
      out.unshift(n.tagName.toLowerCase() + (n.id?'#'+n.id:'') + (n.className && typeof n.className==='string' ? '.'+n.className.trim().split(/\s+/).join('.') : ''));
      n = n.parentElement; i++;
    }
    return out.join(' > ');
  }
  const rows = [];
  document.querySelectorAll('body *').forEach(el => {
    let text = '';
    el.childNodes.forEach(n => { if(n.nodeType === 3) text += n.nodeValue; });
    text = text.trim().replace(/\s+/g,' ');
    if(!text) return;
    const cs = getComputedStyle(el);
    if(cs.visibility === 'hidden' || cs.display === 'none') return;
    // 祖先 opacity 连乘
    let op = 1, n = el;
    while(n && n.nodeType === 1){ op *= parseFloat(getComputedStyle(n).opacity); n = n.parentElement; }
    if(op < .05) return;
    const fg0 = P(cs.color); if(!fg0) return;
    const bg = bgOf(el);
    const fg = ov({r:fg0.r,g:fg0.g,b:fg0.b,a:fg0.a*op}, bg);
    rows.push({
      p: path(el), t: text.slice(0,14), c: cs.color, b: 'rgb('+[bg.r,bg.g,bg.b].map(Math.round).join(',')+')',
      fs: parseFloat(cs.fontSize), fw: cs.fontWeight, r: +R(fg,bg).toFixed(2)
    });
  });
  document.getElementById('__probe__').textContent = JSON.stringify(rows);

  const SEL = ['#log','#workbenchPanel','main','header','#workbenchBar','#sidebar','#activity','#status',
    '.msg','.msg.user','.msg.error','.msg.perm','.msg.hl','.box:not(.agent)','.wb-card','.wb-task','.dialog','.wb-dialog',
    '#text','.wb-card pre','.wb-task pre','.thinkbox','.tstep .detail','.grid input','#targets button:not(.on)','#targets button.on','#pick','#stop','#send','.chip','.att-chip','.att-item','.cv-item:not(.on)','.cv-item.on',
    '.agent:not(.active)','.agent.active','.agent button','.box button','#btnShare','#addModel','.dot:not(.on):not(.run)','.dot.run','#hlBar','#selBar','.perm',
    '.tstat.ok','.tstat.warn','.tstat.err','.tstep','.tstep.running','.tstep.error','.wb-brand span',
    '#workbenchBar nav button:not(.on)','#workbenchBar nav button.on','.wb-error','.wb-empty','.wb-meta',
    '.wb-card-title small','.wb-actions button','#workbenchPanel button','#coldBanner','.cv-group'];
  const styles = [];
  SEL.forEach(s => {
    let el = null; try { el = document.querySelector(s); } catch(e) {}
    if(!el) { styles.push({s, miss:1}); return; }
    const cs = getComputedStyle(el);
    styles.push({s, bc: cs.backgroundColor, bi: cs.backgroundImage.slice(0,60),
      c: cs.color, bl: cs.borderLeftColor + ' ' + cs.borderLeftWidth,
      bb: cs.borderBottomColor, rad: cs.borderRadius, bs: cs.boxShadow.slice(0,60),
      fs: cs.fontSize, fw: cs.fontWeight, op: cs.opacity, cs: cs.colorScheme});
  });
  const pre2 = document.createElement('pre'); pre2.id = '__styles__'; pre2.style.display = 'none';
  pre2.textContent = JSON.stringify(styles); document.body.appendChild(pre2);
})();
</script>
"""

out = src.replace('</body>', PROBE + '</body>')
(ROOT / '_probe.html').write_text(out, encoding='utf-8')
print('wrote', ROOT / '_probe.html')
