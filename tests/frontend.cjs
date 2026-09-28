// Controller smoke tests without a browser or real network.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('web/static/index.html', 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
function element() {
  return {style:{}, dataset:{}, children:[], value:'', classList:{add(){},remove(){}},
    append(...items){this.children.push(...items)}, appendChild(item){this.children.push(item)},
    addEventListener(type, fn){this[type] = fn}, focus(){}};
}
const storage = new Map();
let nextPrompt = null, reloads = 0;
const context = vm.createContext({
  console, URL, URLSearchParams, Date, Math, AbortController,
  crypto:{randomUUID:()=> 'test-session'},
  localStorage:{getItem:k=>storage.get(k), setItem:(k,v)=>storage.set(k,v)},
  location:{search:'?api=https://untrusted.example', reload(){reloads++}},
  window:{prompt:()=>nextPrompt},
  document:{querySelector(s){if(!elements.has(s))elements.set(s,element());return elements.get(s)},
    querySelectorAll:()=>[], createElement:element, addEventListener(){}},
  setTimeout:()=>1, clearTimeout(){}, setInterval:()=>1,
  fetch:async()=>({ok:true,json:async()=>({ai:'offline',user:'Tester'})})
});
vm.runInContext(source,context);
assert.equal(storage.get('jarvis_api'),undefined,'URL must not silently change the brain');
assert.equal(typeof elements.get('#mobileSettingsBtn').click,'function');
nextPrompt = 'javascript:alert(1)'; elements.get('#mobileSettingsBtn').click();
assert.equal(reloads,0); assert.equal(storage.get('jarvis_api'),undefined);
nextPrompt = 'https://brain.example/'; elements.get('#mobileSettingsBtn').click();
assert.equal(storage.get('jarvis_api'),'https://brain.example'); assert.equal(reloads,1);
vm.runInContext("addEntry('jarvis','Safe',{},[{type:'open_url',url:'javascript:alert(1)'}])",context);
const article = elements.get('#feed').children.at(-1);
assert.equal(article.children[1].children.at(-1).children.length,0,'Unsafe links filtered');
console.log('Frontend controller checks passed');
