/* Android app mode (2026-10-25).
   The Google Play app opens the portal full-screen at /dashboard?app=android.
   Google's payment rules: no buying plans, credits or top-ups inside the
   app, and no pointing people elsewhere to pay. So while in the app:
   - <html> gets class "in-app" (base.html hides every billing link/form),
   - cookie px_app=1 tells the server to answer /billing pages with
     app_no_payments.html instead.
   The app shares cookies with Chrome on the phone, so a normal Chrome tab
   (not full-screen) clears the cookie again and reloads a blocked page. */
(function () {
  try {
    var full = window.matchMedia('(display-mode: standalone)').matches;
    var opened = /[?&]app=android(&|$)/.test(location.search) ||
                 document.referrer.indexOf('android-app://') === 0;
    if (opened && full) sessionStorage.setItem('pxApp', '1');
    var inApp = full && sessionStorage.getItem('pxApp') === '1';
    var hasCookie = /(^|;\s*)px_app=1/.test(document.cookie);
    if (inApp) {
      document.documentElement.classList.add('in-app');
      if (!hasCookie) document.cookie = 'px_app=1; path=/; SameSite=Lax; Secure';
    } else if (hasCookie) {
      document.cookie = 'px_app=; path=/; Max-Age=0; SameSite=Lax; Secure';
      if (document.documentElement.getAttribute('data-app-blocked') === '1') location.reload();
    }
  } catch (e) {}
})();
