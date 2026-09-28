/* JARVIS v2.2 tools. All outgoing actions require an explicit user click. */
(() => {
  const el = id => document.getElementById(id);
  const open = id => { if (!el(id).open) el(id).showModal(); };
  el('toolsBtn').addEventListener('click', () => open('toolsDialog'));
  document.querySelectorAll('[data-tool]').forEach(button => {
    button.addEventListener('click', () => { if (el('toolsDialog').open) el('toolsDialog').close(); open(button.dataset.tool + 'Dialog'); });
  });
  document.querySelectorAll('[data-close]').forEach(button => {
    button.addEventListener('click', () => el(button.dataset.close).close());
  });
  el('saveAccess').addEventListener('click', () => {
    const code = el('accessCode').value.trim();
    if (!code) { el('accessStatus').textContent = 'Enter the code first.'; return; }
    localStorage.setItem('jarvis_access', code);
    el('accessCode').value = '';
    el('accessStatus').textContent = '';
    el('accessDialog').close();
    toast('Owner access saved in this browser');
  });
  if (!localStorage.getItem('jarvis_access')) setTimeout(() => open('accessDialog'), 250);

  async function jsonRequest(path, body) {
    const response = await api(path, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) open('accessDialog');
      throw Error(typeof data.detail === 'string' ? data.detail : 'Request failed. Please try again.');
    }
    return response;
  }
  const pending = (button, active) => { button.disabled = active; button.style.opacity = active ? '.55' : '1'; };
  el('buildApp').addEventListener('click', async () => {
    const button = el('buildApp'), status = el('studioStatus');
    status.textContent = 'Building your project…'; pending(button, true);
    try {
      const response = await jsonRequest('/api/studio/build', {prompt: el('studioPrompt').value});
      const blob = await response.blob();
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      const match = /filename="([a-z0-9-]+\.zip)"/.exec(response.headers.get('Content-Disposition') || '');
      link.href = url; link.download = match ? match[1] : 'jarvis-app.zip';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status.textContent = 'Project downloaded. Review the code before publishing.';
    } catch (error) { status.textContent = error.message; }
    finally { pending(button, false); }
  });
  el('sendEmail').addEventListener('click', async () => {
    const button = el('sendEmail'), status = el('emailStatus');
    const to = el('emailTo').value.trim(), subject = el('emailSubject').value.trim(), body = el('emailBody').value.trim();
    if (!to || !subject || !body) { status.textContent = 'Complete all three fields.'; return; }
    status.textContent = 'Sending…'; pending(button, true);
    try {
      await jsonRequest('/api/email/send', {to, subject, body});
      status.textContent = 'Email sent to ' + to + '.';
      el('emailBody').value = '';
    } catch (error) { status.textContent = error.message; }
    finally { pending(button, false); }
  });
  el('draftReply').addEventListener('click', async () => {
    const button = el('draftReply'), status = el('waStatus');
    status.textContent = 'Writing a draft…'; pending(button, true); el('waOpen').hidden = true;
    try {
      const response = await jsonRequest('/api/whatsapp/draft', {
        number: el('waNumber').value, received: el('waReceived').value,
        style: el('waStyle').value, examples: el('waExamples').value
      });
      const data = await response.json();
      el('waDraft').value = data.draft;
      el('waOpen').href = data.url; el('waOpen').hidden = false;
      status.textContent = 'Review the reply, then open WhatsApp to send it.';
    } catch (error) { status.textContent = error.message; }
    finally { pending(button, false); }
  });

  const seen = new Set();
  async function pollReminders() {
    if (!localStorage.getItem('jarvis_access')) return;
    try {
      const response = await api('/api/events?session_id=' + encodeURIComponent(sessionId));
      if (!response.ok) return;
      const {events = []} = await response.json();
      const ids = [];
      for (const event of events) {
        if (!event.id || seen.has(event.id)) continue;
        seen.add(event.id); ids.push(event.id);
        addEntry('jarvis', event.text || 'Reminder due');
        if (document.visibilityState === 'visible') speak(event.text || 'Reminder due');
      }
      if (ids.length) await jsonRequest('/api/events/ack', {session_id: sessionId, event_ids: ids});
    } catch (_) { /* Keep reminders pending for the next poll. */ }
  }
  setInterval(pollReminders, 5000);
  setTimeout(pollReminders, 1000);
})();
