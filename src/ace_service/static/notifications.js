(() => {
  if (window.__audioventuraNotificationsBound) return;
  window.__audioventuraNotificationsBound = true;
  const button = document.querySelector('[data-notifications-enable]');
  const status = document.querySelector('[data-notifications-status]');
  if (!button || !status) return;
  const control = button.closest('.notification-control');
  const configUrl = document.querySelector('meta[name="notifications-config"]')?.content || '/notifications/config';
  const subscriptionUrl = document.querySelector('meta[name="notifications-subscriptions"]')?.content || '/notifications/subscriptions';
  const workerUrl = document.querySelector('meta[name="notifications-worker"]')?.content || '/notification-worker.js';
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  let registration = null;
  let subscription = null;
  const applicationServerKey = (value) => {
    const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
    const decoded = atob(padded);
    return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
  };
  if (!supported) {
    status.textContent = 'Unsupported';
    button.disabled = true;
    return;
  }
  const update = (value) => { status.textContent = value; };
  const hideEnabledControl = () => {
    if (control) control.hidden = true;
  };
  const load = async () => {
    try {
      const response = await fetch(configUrl, { credentials: 'same-origin' });
      const config = await response.json();
      if (!config.enabled) { update('Unavailable'); button.disabled = true; return; }
      const workerLocation = new URL(workerUrl, window.location.origin);
      registration = await navigator.serviceWorker.register(workerLocation, { scope: new URL('./', workerLocation).pathname });
      subscription = await registration.pushManager.getSubscription();
      if (subscription) subscription.__serverId = localStorage.getItem('ace_push_subscription_id') || '';
      if (subscription) hideEnabledControl();
      else if (Notification.permission === 'denied') update('Blocked in browser');
      else update('Enable notifications');
      button.dataset.publicKey = config.public_key || '';
    } catch (_) { update('Unavailable'); }
  };
  button.addEventListener('click', async () => {
    if (Notification.permission === 'denied') { update('Blocked in browser'); return; }
    try {
      if (!registration) await load();
      if (subscription) {
        if (!subscription.__serverId) { update('Retry disable'); return; }
        const response = await fetch(`${subscriptionUrl}/${encodeURIComponent(subscription.__serverId)}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf() }, credentials: 'same-origin' });
        if (!response.ok) { update('Retry disable'); return; }
        await subscription.unsubscribe(); localStorage.removeItem('ace_push_subscription_id'); subscription = null; update('Enable notifications'); return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') { update('Blocked in browser'); return; }
      const key = button.dataset.publicKey;
      subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: applicationServerKey(key) });
      const serialized = subscription.toJSON();
      const response = await fetch(subscriptionUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, credentials: 'same-origin', body: JSON.stringify({ endpoint: serialized.endpoint, keys: serialized.keys, csrf_token: csrf() }) });
      if (!response.ok) { update('Unavailable'); return; }
      const result = await response.json(); subscription.__serverId = result.subscription_id; localStorage.setItem('ace_push_subscription_id', result.subscription_id); hideEnabledControl();
    } catch (_) { update('Unavailable'); }
  });
  load();
})();
