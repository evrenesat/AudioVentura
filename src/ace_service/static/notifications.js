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
  const hideControl = () => {
    if (control) control.hidden = true;
  };
  const showEnrollment = (message = '') => {
    if (control) control.hidden = false;
    button.hidden = false;
    button.disabled = false;
    status.textContent = message;
    status.hidden = !message;
  };
  const showStatus = (message) => {
    if (control) control.hidden = false;
    button.hidden = true;
    button.disabled = false;
    status.textContent = message;
    status.hidden = false;
  };
  if (!supported) {
    hideControl();
    return;
  }
  const load = async () => {
    try {
      const response = await fetch(configUrl, { credentials: 'same-origin' });
      if (!response.ok) { hideControl(); return false; }
      const config = await response.json();
      if (!config.enabled || !config.public_key) { hideControl(); return false; }
      button.dataset.publicKey = config.public_key;
      if (window.AudioventuraServiceWorkerRegistration) {
        registration = await window.AudioventuraServiceWorkerRegistration;
      } else {
        const workerLocation = new URL(workerUrl, window.location.origin);
        registration = await navigator.serviceWorker.register(workerLocation, { scope: new URL('./', workerLocation).pathname });
        window.AudioventuraServiceWorkerRegistration = Promise.resolve(registration);
      }
      if (!registration?.pushManager) { hideControl(); return false; }
      subscription = await registration.pushManager.getSubscription();
      if (subscription) subscription.__serverId = localStorage.getItem('ace_push_subscription_id') || '';
      if (subscription) hideControl();
      else if (Notification.permission === 'denied') showStatus('Notifications blocked in browser');
      else showEnrollment();
      return true;
    } catch (_) { hideControl(); return false; }
  };
  button.addEventListener('click', async () => {
    if (Notification.permission === 'denied') { showStatus('Notifications blocked in browser'); return; }
    try {
      if (!registration && !await load()) return;
      if (subscription) {
        hideControl();
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        if (permission === 'denied') showStatus('Notifications blocked in browser');
        else showEnrollment();
        return;
      }
      const key = button.dataset.publicKey;
      subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: applicationServerKey(key) });
      const serialized = subscription.toJSON();
      const response = await fetch(subscriptionUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, credentials: 'same-origin', body: JSON.stringify({ endpoint: serialized.endpoint, keys: serialized.keys, csrf_token: csrf() }) });
      if (!response.ok) throw new Error('subscription rejected');
      const result = await response.json(); subscription.__serverId = result.subscription_id; localStorage.setItem('ace_push_subscription_id', result.subscription_id); hideControl();
    } catch (_) { showEnrollment('Try again'); }
  });
  load();
})();
