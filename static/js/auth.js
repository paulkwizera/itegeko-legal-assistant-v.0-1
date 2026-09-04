/* Password visibility toggle -- shared by every page with a password
 * field (login, signup, reset-password, settings) so there's one place
 * to fix instead of four copies of the same handful of lines. */
function togglePasswordVisibility(btn) {
    const input = btn.previousElementSibling;
    if (!input) return;
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    btn.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
    btn.classList.toggle('showing', !showing);
}
